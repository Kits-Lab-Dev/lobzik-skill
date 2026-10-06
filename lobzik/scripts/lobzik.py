#!/usr/bin/env python3
"""Лобзик (KitsLab) — Black Magic Probe на STM32L432: прошивка, сброс, память,
регистры, лог UART, питание цели и перешивка самого зонда.

Зонд — GDB-сервер, OpenOCD не нужен. Скрипт запускает GDB в пакетном режиме
и выходит, поэтому порт зонда после него свободен.

    python lobzik.py list                       # зонды и их порты
    python lobzik.py info                       # версия, напряжение, цели
    python lobzik.py flash build/app.elf        # записать, сверить, сбросить
    python lobzik.py reset                      # сброс цели
    python lobzik.py read 0x48022400 4          # 4 слова памяти
    python lobzik.py eval SystemCoreClock --elf build/app.elf
    python lobzik.py regs --elf build/app.elf   # где стоит ядро, разбор HardFault
    python lobzik.py uart --reset --time 10     # лог с загрузки
    python lobzik.py power on                   # 3.3 В на цель от зонда
    python lobzik.py reflash fretsaw_bmp.bin    # обновить прошивку Лобзика
    python lobzik.py gdb "mon help"             # любые команды GDB

Нужны: Python 3.8+, pyserial; GDB под архитектуру цели (arm-none-eabi-gdb,
gdb-multiarch или riscv*-gdb). Для reflash ещё pyusb + libusb-package
и STM32_Programmer_CLI или dfu-util.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("Нужен pyserial: pip install pyserial")

BMP_VID, BMP_PID = 0x1D50, 0x6018
GDB_IF, UART_IF, DFU_IF = 0, 2, 4  # интерфейсы USB зонда
ST_DFU_VID, ST_DFU_PID = 0x0483, 0xDF11
FLASH_BASE = "0x08000000"
DEFAULT_FREQ = "2M"  # SWD 2 МГц: запись ~23 КБ/с, по умолчанию зонд в разы медленнее

# Регистры SCB Cortex-M для разбора отказов
SCB = {"ICSR": 0xE000ED04, "VTOR": 0xE000ED08, "SHCSR": 0xE000ED24, "CFSR": 0xE000ED28,
       "HFSR": 0xE000ED2C, "DFSR": 0xE000ED30, "MMFAR": 0xE000ED34, "BFAR": 0xE000ED38}
AIRCR, AIRCR_SYSRESET = 0xE000ED0C, 0x05FA0004

CFSR_BITS = {
    0: "IACCVIOL: выборка команды из запрещённой области (MPU/XN)",
    1: "DACCVIOL: доступ к данным в запрещённую область (MPU)",
    3: "MUNSTKERR: MemManage при выходе из исключения",
    4: "MSTKERR: MemManage при входе в исключение (переполнение стека?)",
    5: "MLSPERR: MemManage при ленивом сохранении FPU",
    7: "MMARVALID: адрес в MMFAR действителен",
    8: "IBUSERR: ошибка шины при выборке команды",
    9: "PRECISERR: точная ошибка шины данных, адрес в BFAR",
    10: "IMPRECISERR: неточная ошибка шины (запись через буфер, адрес неизвестен)",
    11: "UNSTKERR: BusFault при выходе из исключения",
    12: "STKERR: BusFault при входе в исключение (переполнение стека?)",
    13: "LSPERR: BusFault при ленивом сохранении FPU",
    15: "BFARVALID: адрес в BFAR действителен",
    16: "UNDEFINSTR: неизвестная команда (испорченный PC, прыжок в данные)",
    17: "INVSTATE: неверное состояние (переход на чётный адрес, бит Thumb = 0)",
    18: "INVPC: неверный EXC_RETURN (испорчен стек или LR)",
    19: "NOCP: команда сопроцессора при выключенном FPU",
    24: "UNALIGNED: невыровненный доступ при UNALIGN_TRP",
    25: "DIVBYZERO: деление на ноль при DIV_0_TRP",
}
HFSR_BITS = {1: "VECTTBL: ошибка чтения таблицы векторов",
             30: "FORCED: эскалация MemManage/BusFault/UsageFault — смотри CFSR",
             31: "DEBUGEVT: событие отладки"}


def log(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- порты

def _iface_num(p):
    # Windows: "1-2.3:x.0"; Linux: "1-2.3:1.0" — последнее число = интерфейс USB
    m = re.search(r":(?:x|\d+)\.(\d+)$", p.location or "")
    return int(m.group(1)) if m else None


def probes():
    """[{serial, gdb, uart}] подключённых Лобзиков."""
    by_sn = {}
    for p in serial.tools.list_ports.comports():
        if p.vid == BMP_VID and p.pid == BMP_PID:
            by_sn.setdefault(p.serial_number or "?", []).append(p)
    res = []
    for sn, ports in sorted(by_sn.items()):
        gdb = uart = None
        for p in ports:
            name = (p.interface or p.description or "").lower()
            if "gdb" in name:
                gdb = p.device
            elif "uart" in name:
                uart = p.device
        if not (gdb and uart):
            # Windows не даёт имя интерфейса, а у первого из них — и номер:
            # GDB — тот порт, что не UART (интерфейс 2)
            u = [p for p in ports if _iface_num(p) == UART_IF]
            g = [p for p in ports if _iface_num(p) in (GDB_IF, None) and p not in u]
            if len(u) == 1 and len(g) == 1:
                gdb, uart = g[0].device, u[0].device
            elif len(ports) == 2:
                # macOS: /dev/cu.usbmodem<SN>1 — GDB, ...3 — UART
                a, b = sorted(p.device for p in ports)
                gdb, uart = a, b
        res.append({"serial": sn, "gdb": gdb, "uart": uart})
    return res


def pick_probe(args):
    found = probes()
    if args.serial:
        found = [p for p in found if p["serial"] == args.serial]
    if not found:
        sys.exit("Лобзик не найден (USB 1d50:6018). Проверьте кабель; в DFU-режиме зонд "
                 "виден как STM32 BOOTLOADER — тогда нужна перешивка (reflash).")
    if len(found) > 1:
        sys.exit("Подключено несколько Лобзиков, укажите --serial: "
                 + ", ".join(p["serial"] for p in found))
    p = found[0]
    if args.port:
        p["gdb"] = args.port
    if args.uart_port:
        p["uart"] = args.uart_port
    return p


def gdb_target(port):
    # COM10 и выше Windows открывает только как \\.\COM10; для младших тоже работает
    if os.name == "nt" and port.upper().startswith("COM"):
        return "\\\\.\\" + port
    return port


# ---------------------------------------------------------------- GDB

def elf_machine(path):
    """'arm', 'riscv' или None по заголовку ELF."""
    try:
        with open(path, "rb") as f:
            h = f.read(20)
    except OSError:
        return None
    if h[:4] != b"\x7fELF":
        return None
    m = int.from_bytes(h[18:20], "little")
    return {40: "arm", 243: "riscv"}.get(m)


def find_gdb(arch=None, explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("LOBZIK_GDB")
    if env:
        return env
    arm = ["arm-none-eabi-gdb", "gdb-multiarch"]
    riscv = ["riscv-none-elf-gdb", "riscv32-unknown-elf-gdb", "riscv64-unknown-elf-gdb",
             "riscv-none-embed-gdb", "gdb-multiarch"]
    names = riscv if arch == "riscv" else arm + riscv
    for n in names:
        exe = shutil.which(n)
        if exe:
            return exe
    if os.name == "nt" and arch != "riscv":
        found = sorted(glob.glob(r"C:\ST\STM32CubeCLT_*\GNU-tools-for-STM32\bin\arm-none-eabi-gdb.exe"))
        if found:
            return found[-1]
    sys.exit("GDB не найден. Нужен arm-none-eabi-gdb (Arm GNU Toolchain, STM32CubeCLT), "
             "gdb-multiarch или riscv*-gdb; путь можно задать через --gdb или LOBZIK_GDB.")


def run_gdb(probe, commands, args, elf=None, timeout=None):
    """GDB в пакетном режиме: подключиться к зонду, выполнить команды, выйти."""
    exe = find_gdb(elf_machine(elf) if elf else None, args.gdb)
    cmd = [exe, "-batch", "-nx"]
    if elf:
        cmd += ["-ex", f"file {elf.replace(os.sep, '/')}"]
    cmd += ["-ex", f"target extended-remote {gdb_target(probe['gdb'])}"]
    for c in commands:
        cmd += ["-ex", c]
    if args.verbose:
        log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout or args.timeout)
    except subprocess.TimeoutExpired:
        sys.exit(f"GDB не ответил за {timeout or args.timeout} с. Зонд мог зависнуть: "
                 "переподключите USB Лобзика и повторите.")
    out = r.stdout + r.stderr
    if "Could not connect" in out or "No such file" in out or "Access is denied" in out \
            or "Permission denied" in out:
        sys.exit(f"Порт GDB {probe['gdb']} не открылся — его держит другая программа "
                 f"(VS Code/Cortex-Debug, другой GDB, монитор порта)?\n{out}")
    return out


def attach_cmds(args):
    """Найти цель и подключиться к ней. Подключение останавливает ядро."""
    cmds = []
    if args.freq:
        cmds.append(f"mon frequency {args.freq}")
    if args.halt_timeout:
        cmds.append(f"mon halt_timeout {args.halt_timeout}")
    if args.tpwr:
        cmds.append("mon tpwr enable")
    cmds.append("mon jtag_scan" if args.jtag else "mon swd_scan")
    cmds += [f"attach {args.target}", "set mem inaccessible-by-default off"]
    return cmds


def check_attached(out):
    if re.search(r"No usable targets|SW-DP scan failed|JTAG device scan failed|"
                 r"Attaching to Remote target failed|Don't know how to attach", out):
        hint = ""
        m = re.search(r"Target voltage:\s*([\d.]+)V", out)
        if m and float(m.group(1)) < 1.0:
            hint = (f"\nНа цели {m.group(1)} В — нет питания или не подключён VTref "
                    "(контакт 3). Питание от зонда: --tpwr или power on.")
        if "Available Targets" in out and "Attaching to Remote target failed" in out:
            sys.exit("Цель видна, но ядро не остановилось за отведённое время.\n" + out.strip() +
                     "\nПовторите с --halt-timeout 5000 (ядро во сне или в странном состоянии "
                     "после сбоя). Не помогло — сбросьте цель: reset --reset hw (если nRST "
                     "подключён), кнопка сброса на плате или питание.")
        sys.exit("Цель не найдена.\n" + out.strip() + hint +
                 "\nПроверьте шлейф, питание цели, режим (--jtag для JTAG-целей), "
                 "понизьте частоту (--freq 500k).")


def reset_cmds(mode):
    """Команды сброса после подключения. sys — SYSRESETREQ (Cortex-M), hw — nRST."""
    if mode == "none":
        return []
    if mode == "hw":
        return ["mon reset"]
    if mode.startswith("mon "):
        return [mode]
    # SYSRESETREQ: ядро сбрасывает само себя, отладка при этом отваливается —
    # это нормально, поэтому сразу detach
    return [f"set *(unsigned int*){AIRCR:#x} = {AIRCR_SYSRESET:#x}"]


# ---------------------------------------------------------------- команды

def cmd_list(args):
    found = probes()
    if not found:
        log("Лобзиков не найдено.")
        return
    for p in found:
        log(f"SN {p['serial']}: GDB {p['gdb']}, UART {p['uart']}")


def cmd_info(args):
    p = pick_probe(args)
    log(f"Лобзик SN {p['serial']} (порты GDB {p['gdb']}, UART {p['uart']})")
    cmds = ["mon version"]
    if args.freq:
        cmds.append(f"mon frequency {args.freq}")
    cmds.append("mon jtag_scan" if args.jtag else "mon swd_scan")
    out = run_gdb(p, cmds, args)
    log(out.strip())


def cmd_gdb(args):
    p = pick_probe(args)
    cmds = list(args.commands)
    if args.attach:
        cmds = attach_cmds(args) + cmds + ["detach"]
    log(run_gdb(p, cmds, args, elf=args.elf).strip())


def cmd_flash(args):
    p = pick_probe(args)
    elf = os.path.abspath(args.file)
    if not os.path.exists(elf):
        sys.exit(f"Нет файла {elf}")
    is_elf = elf_machine(elf) is not None
    cmds = attach_cmds(args)
    if is_elf:
        cmds.append("load")
        if not args.no_verify:
            cmds.append("compare-sections")
    else:
        # .bin/.hex без символов: GDB пишет как есть
        addr = args.address or FLASH_BASE
        if elf.lower().endswith(".bin"):
            cmds.append(f"restore {elf.replace(os.sep, '/')} binary {addr}")
        else:
            cmds.append(f"load {elf.replace(os.sep, '/')}")
    # с --log сброс делает uart_capture, уже открыв порт, — иначе начало лога потеряется
    cmds += reset_cmds("none" if args.log else args.reset) + ["detach"]
    t0 = time.time()
    out = run_gdb(p, cmds, args, elf=elf if is_elf else None, timeout=max(args.timeout, 600))
    check_attached(out)
    bad = [ln for ln in out.splitlines() if "MIS-MATCHED" in ln]
    rate = re.search(r"Transfer rate: .*", out)
    if args.verbose or bad or re.search(r"error", out, re.I):
        log(out.strip())
    if bad:
        sys.exit("Сверка не прошла:\n" + "\n".join(bad))
    if not rate and is_elf:
        sys.exit("Загрузка не подтверждена:\n" + out.strip())
    log(f"Записано за {time.time() - t0:.0f} с" + (f", {rate.group(0).rstrip('.')}" if rate else "")
        + ("" if args.no_verify or not is_elf else ", сверено")
        + ("" if args.reset == "none" else f", сброс ({args.reset})"))
    if args.log and args.reset != "none":
        log(f"--- лог UART {args.log:g} с")
        ok = uart_capture(p, args, args.log, args.reset, until=args.until, baud=args.baud)
        if not ok:
            sys.exit(f"[«{args.until}» не встретилось за {args.log:g} с]")


def released(out):
    """Строка о том, что ядро отпущено (detach прошёл) и программа идёт дальше."""
    return ("(ядро отпущено, программа на плате продолжает работу)" if "detached" in out
            else "(внимание: detach не подтверждён — ядро может стоять; reset)")


def cmd_reset(args):
    p = pick_probe(args)
    if args.reset == "hw":
        # импульс на nRST (контакт 12); если nRST не подключён, ничего не произойдёт
        out = run_gdb(p, ["mon reset"], args)
    else:
        out = run_gdb(p, attach_cmds(args) + reset_cmds(args.reset) + ["detach"], args)
        check_attached(out)
    if args.verbose:
        log(out.strip())
    log(f"Сброс ({args.reset}) выполнен")


def cmd_read(args):
    p = pick_probe(args)
    out = run_gdb(p, attach_cmds(args) + [f"x/{args.count}x{args.size} {args.address}", "detach"],
                  args, elf=args.elf)
    check_attached(out)
    # строки дампа: "0x48022400:" или "0x20000010 <var>:"
    log("\n".join(ln for ln in out.splitlines() if re.match(r"^\s*0x[0-9a-fA-F]+( <[^>]*>)?:", ln)
                  or "Cannot access" in ln) or out.strip())
    log(released(out))


def cmd_write(args):
    p = pick_probe(args)
    c = {"w": "unsigned int", "h": "unsigned short", "b": "unsigned char"}[args.size]
    out = run_gdb(p, attach_cmds(args) + [f"set *({c}*)({args.address}) = {args.value}",
                                          f"x/1x{args.size} {args.address}", "detach"], args)
    check_attached(out)
    log("\n".join(ln for ln in out.splitlines() if re.match(r"^\s*0x[0-9a-fA-F]+( <[^>]*>)?:", ln))
        or out.strip())
    log(released(out))


def cmd_eval(args):
    p = pick_probe(args)
    out = run_gdb(p, attach_cmds(args) + [("print/x " if args.hex else "print ") + e for e in args.expr] + ["detach"],
                  args, elf=args.elf)
    check_attached(out)
    log("\n".join(ln for ln in out.splitlines() if ln.startswith("$") or "No symbol" in ln)
        or out.strip())
    log(released(out))


def load_map(path):
    """Функции из GNU ld .map: [(адрес, имя)] по возрастанию.

    Берутся и глобальные символы, и секции .text.<имя> (-ffunction-sections):
    по ним видны static-функции, которых нет среди символов."""
    syms = []
    pending = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = re.match(r"^\s+0x([0-9a-fA-F]{8,16})\s+([A-Za-z_][\w.$]*)\s*$", ln)
            if m:
                syms.append((int(m.group(1), 16), m.group(2)))
                pending = None
                continue
            m = re.match(r"^\s*\.text\.([\w.$]+)(?:\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+))?", ln)
            if m:
                if m.group(2) and int(m.group(3), 16):
                    syms.append((int(m.group(2), 16), m.group(1)))
                    pending = None
                else:
                    pending = m.group(1)  # длинное имя: адрес на следующей строке
                continue
            if pending:
                m = re.match(r"^\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)", ln)
                if m and int(m.group(2), 16):
                    syms.append((int(m.group(1), 16), pending))
                pending = None
    syms = [s for s in syms if s[0]]
    syms.sort()
    return syms


def sym_for(syms, addr):
    lo, hi, best = 0, len(syms) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if syms[mid][0] <= addr:
            best, lo = syms[mid], mid + 1
        else:
            hi = mid - 1
    return f"{best[1]}+{addr - best[0]:#x}" if best else "?"


def decode(value, bits):
    return [f"  [{b}] {txt}" for b, txt in sorted(bits.items()) if value >> b & 1]


def cmd_regs(args):
    p = pick_probe(args)
    cmds = attach_cmds(args) + ["info registers"]
    if args.elf:
        cmds += ["bt 20"]
    for name, addr in SCB.items():
        cmds.append(f'printf "{name}=%08x\\n", *(unsigned int*){addr:#x}')
    if args.stack:
        cmds.append(f"x/{args.stack}xw $sp")
    if not args.keep_halted:
        cmds.append("detach")
    out = run_gdb(p, cmds, args, elf=args.elf)
    check_attached(out)
    log(out.strip())
    regs = dict((k, int(v, 16)) for k, v in re.findall(r"^(\w+)=([0-9a-f]{8})$", out, re.M))
    core = dict((k, int(v, 16)) for k, v in re.findall(r"^(pc|lr|sp|xpsr)\s+0x([0-9a-f]+)", out, re.M))
    log("\n--- разбор")
    if "xpsr" in core:
        exc = core["xpsr"] & 0x1FF
        names = {0: "поток (не в прерывании)", 2: "NMI", 3: "HardFault", 4: "MemManage",
                 5: "BusFault", 6: "UsageFault", 11: "SVCall", 14: "PendSV", 15: "SysTick"}
        log(f"IPSR={exc}: " + names.get(exc, f"прерывание IRQ{exc - 16}" if exc >= 16 else "?"))
    if args.map and core:
        syms = load_map(args.map)
        for r in ("pc", "lr"):
            if r in core:
                log(f"{r.upper()} {core[r]:#010x} = {sym_for(syms, core[r] & ~1)}")
    if regs.get("CFSR"):
        log(f"CFSR={regs['CFSR']:#010x}")
        for s in decode(regs["CFSR"], CFSR_BITS):
            log(s)
        if regs["CFSR"] >> 7 & 1:
            log(f"  MMFAR = {regs['MMFAR']:#010x}")
        if regs["CFSR"] >> 15 & 1:
            log(f"  BFAR  = {regs['BFAR']:#010x}")
    if regs.get("HFSR"):
        log(f"HFSR={regs['HFSR']:#010x}")
        for s in decode(regs["HFSR"], HFSR_BITS):
            log(s)
    if regs and not regs.get("CFSR") and not regs.get("HFSR"):
        log("CFSR/HFSR = 0: отказа не было. Ядро стоит в обычном коде — "
            "ищите бесконечный цикл или ожидание (PC/LR выше).")
    if core.get("lr", 0) & 0xFFFFFFF0 == 0xFFFFFFF0:
        log("LR — EXC_RETURN: ядро в обработчике исключения. Кадр стека прерванного кода: "
            "R0-R3, R12, LR, PC, xPSR по адресу SP (бит 2 LR = 1 — PSP, 0 — MSP); "
            "смотрите --stack 8.")
    if args.keep_halted:
        log("Ядро оставлено остановленным: продолжить — reset или gdb \"continue\".")
    else:
        log(released(out))


def uart_capture(p, args, seconds, reset_mode=None, idle=0, until=None, send=None,
                 eol="\r\n", raw=False, baud=115200):
    """Слушать UART цели. Выход — по первому из условий: seconds, idle с тишины, until."""
    if not p["uart"]:
        sys.exit("UART-порт Лобзика не найден")
    try:
        ser = serial.Serial(p["uart"], baud, timeout=0.1)
    except serial.SerialException as e:
        sys.exit(f"UART {p['uart']} не открылся (занят терминалом?): {e}")
    with ser:
        # выбросить то, что накопилось в буферах ПК и зонда до нашего запуска
        ser.reset_input_buffer()
        stale_end = time.time() + 0.3
        while time.time() < stale_end:
            ser.read(4096)
        if reset_mode:
            # сброс — после открытия порта, чтобы не потерять начало лога
            out = run_gdb(p, attach_cmds(args) + reset_cmds(reset_mode) + ["detach"], args)
            check_attached(out)
        if send is not None:
            ser.write((send + eol).encode())
        end = time.time() + seconds
        idle_end = None
        tail = ""
        while time.time() < end:
            data = ser.read(4096)
            if data:
                text = data.decode("utf-8", errors="replace")
                if not raw:
                    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\x00", "")
                sys.stdout.write(text)
                sys.stdout.flush()
                tail = (tail + text)[-4096:]
                if until and until in tail:
                    log(f"\n[встретилось «{until}»]")
                    return True
                idle_end = time.time() + idle if idle else None
            elif idle_end and time.time() > idle_end:
                break
    log("")
    return until is None


def cmd_uart(args):
    p = pick_probe(args)
    ok = uart_capture(p, args, args.time, args.reset_mode if args.reset else None, args.idle,
                      args.until, args.send, args.eol.encode().decode("unicode_escape"),
                      args.raw, args.baud)
    if not ok:
        sys.exit(f"[«{args.until}» не встретилось]")


def cmd_power(args):
    p = pick_probe(args)
    if args.state == "status":
        out = run_gdb(p, ["mon swd_scan"], args)
        m = re.search(r"Target voltage:\s*(.*)", out)
        log(f"Напряжение цели: {m.group(1) if m else '?'}")
        return
    out = run_gdb(p, [f"mon tpwr {'enable' if args.state == 'on' else 'disable'}", "mon swd_scan"], args)
    m = re.search(r"Target voltage:\s*(.*)", out)
    if args.verbose:
        log(out.strip())
    if "already powered" in out.lower():
        log("Цель уже запитана от своего источника — зонд питание не включает (это защита). "
            f"Напряжение цели: {m.group(1) if m else '?'}")
    else:
        log(f"Питание цели {'включено' if args.state == 'on' else 'выключено'}; "
            f"напряжение цели: {m.group(1) if m else '?'}")


def _dfu_present():
    try:
        import libusb_package
        import usb.core
        return usb.core.find(idVendor=ST_DFU_VID, idProduct=ST_DFU_PID,
                             backend=libusb_package.get_libusb1_backend()) is not None
    except ImportError:
        return None


def cmd_reflash(args):
    fw = os.path.abspath(args.bin)
    if not os.path.exists(fw):
        sys.exit(f"Нет файла {fw}")
    if not fw.lower().endswith(".bin"):
        sys.exit("Нужен .bin прошивки Лобзика (пишется с 0x08000000)")
    # 1. В загрузчик ST: DFU_DETACH на интерфейс 4 (прошивка зонда с 30.09.2026);
    #    со старой прошивкой — кнопка BOOT при подключении USB
    if _dfu_present():
        log("Лобзик уже в DFU (STM32 BOOTLOADER)")
    else:
        try:
            import libusb_package
            import usb.core
            import usb.util
        except ImportError:
            sys.exit("Для перехода в DFU без кнопки нужно: pip install pyusb libusb-package\n"
                     "Или вручную: зажмите кнопку на Лобзике, подключите USB, отпустите.")
        be = libusb_package.get_libusb1_backend()
        devs = list(usb.core.find(find_all=True, idVendor=BMP_VID, idProduct=BMP_PID, backend=be))
        if args.serial:
            devs = [d for d in devs if usb.util.get_string(d, d.iSerialNumber) == args.serial]
        if not devs:
            sys.exit("Лобзик не найден ни в работе, ни в DFU")
        if len(devs) > 1:
            sys.exit("Подключено несколько Лобзиков — оставьте один или укажите --serial")
        try:
            devs[0].ctrl_transfer(0x21, 0, 1000, DFU_IF, None, timeout=1000)
        except usb.core.USBError as e:
            # на Windows без драйвера WinUSB на интерфейсе 4 запрос не уходит
            sys.exit(f"DFU_DETACH не прошёл ({e}). Переведите вручную: кнопка + USB.")
        log("DFU_DETACH отправлен, жду загрузчик ST...")
        end = time.time() + 10
        while time.time() < end and not _dfu_present():
            time.sleep(0.5)
        if not _dfu_present():
            sys.exit("Лобзик не ушёл в DFU: прошивка зонда старая (до 30.09.2026) — "
                     "переведите кнопкой: зажать, подключить USB, отпустить.")
    # 2. Запись
    cli = shutil.which("STM32_Programmer_CLI") or (sorted(glob.glob(
        r"C:\ST\STM32CubeCLT_*\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe")) or [None])[-1]
    dfu_util = shutil.which("dfu-util")
    if cli:
        r = subprocess.run([cli, "-c", "port=usb1", "-d", fw, FLASH_BASE, "-v"],
                           capture_output=True, text=True, errors="replace")
        ok = "Download verified successfully" in r.stdout
        if not ok:
            sys.exit("Запись не прошла:\n" + r.stdout[-2000:])
        subprocess.run([cli, "-c", "port=usb1", "-s", FLASH_BASE], capture_output=True)
    elif dfu_util:
        r = subprocess.run([dfu_util, "-d", "0483:df11", "-a", "0", "-s", f"{FLASH_BASE}:leave",
                            "-D", fw], capture_output=True, text=True, errors="replace")
        if r.returncode not in (0, 74):  # 74: загрузчик ушёл с шины после leave — норма
            sys.exit("dfu-util не записал:\n" + r.stdout + r.stderr)
    else:
        sys.exit("Нужен STM32_Programmer_CLI (STM32CubeProgrammer) или dfu-util")
    log("Записано, жду зонд...")
    end = time.time() + 15
    while time.time() < end:
        if probes():
            break
        time.sleep(0.5)
    found = probes()
    if not found:
        sys.exit("После записи зонд не появился — переподключите USB")
    p = found[0]
    out = run_gdb(p, ["mon version"], args)
    log(next((ln for ln in out.splitlines() if "Black Magic" in ln), out.strip()))


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="Лобзик (Black Magic Probe, KitsLab)")
    ap.add_argument("--serial", help="серийный номер зонда, если их несколько")
    ap.add_argument("--port", help="порт GDB вручную (COM5, /dev/ttyACM0)")
    ap.add_argument("--uart-port", help="порт UART вручную")
    ap.add_argument("--gdb", help="путь к GDB (иначе ищется по архитектуре ELF)")
    ap.add_argument("--jtag", action="store_true", help="JTAG вместо SWD (RISC-V, К1921ВГ015)")
    ap.add_argument("--freq", default=DEFAULT_FREQ,
                    help="частота SWD/JTAG, по умолчанию 2M (зонд берёт ближайшую: 2M → 2.5 МГц)")
    ap.add_argument("--target", default="1", help="номер цели из скана для attach")
    ap.add_argument("--tpwr", action="store_true", help="перед сканом включить питание цели")
    ap.add_argument("--halt-timeout", type=int,
                    help="сколько ждать остановки ядра при attach, мс (у зонда по умолчанию 2000)")
    ap.add_argument("--timeout", type=int, default=120,
                    help="таймаут GDB, с (для flash не меньше 600)")
    ap.add_argument("-v", "--verbose", action="store_true", help="показать вывод GDB")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="подключённые зонды и порты")
    sub.add_parser("info", help="версия зонда, напряжение и цели")

    s = sub.add_parser("flash", help="записать ELF/BIN/HEX, сверить, сбросить")
    s.add_argument("file")
    s.add_argument("--address", help="адрес для .bin (по умолчанию 0x08000000)")
    s.add_argument("--no-verify", action="store_true")
    s.add_argument("--reset", default="sys",
                   help="sys (SYSRESETREQ, Cortex-M), hw (nRST), none, или 'mon ...'")
    s.add_argument("--log", type=float, default=0,
                   help="после записи снять лог UART N с, начиная с самого сброса")
    s.add_argument("--until", help="с --log: закончить, когда встретится строка")
    s.add_argument("--baud", type=int, default=115200, help="скорость UART для --log")

    s = sub.add_parser("reset", help="сброс цели")
    s.add_argument("--reset", default="sys", help="sys, hw или 'mon ...'")

    s = sub.add_parser("read", help="прочитать память")
    s.add_argument("address", help="адрес или выражение (&var при --elf)")
    s.add_argument("count", nargs="?", default=1, type=int)
    s.add_argument("--size", choices="whb", default="w", help="слово/полуслово/байт")
    s.add_argument("--elf")

    s = sub.add_parser("write", help="записать слово в память/регистр")
    s.add_argument("address")
    s.add_argument("value")
    s.add_argument("--size", choices="whb", default="w")

    s = sub.add_parser("eval", help="значение выражения/переменной (нужен ELF с символами)")
    s.add_argument("expr", nargs="+")
    s.add_argument("--hex", action="store_true", help="вывести в шестнадцатеричном виде")
    s.add_argument("--elf", required=True)

    s = sub.add_parser("regs", help="остановить ядро, регистры, разбор отказа")
    s.add_argument("--elf", help="ELF с символами — будет backtrace")
    s.add_argument("--map", help=".map линкера — имена функций для PC/LR без символов")
    s.add_argument("--stack", type=int, default=0, help="показать N слов стека")
    s.add_argument("--keep-halted", action="store_true", help="не отпускать ядро")

    s = sub.add_parser("uart", help="читать UART цели через зонд")
    s.add_argument("--baud", type=int, default=115200)
    s.add_argument("--time", type=float, default=10, help="слушать не дольше N с")
    s.add_argument("--idle", type=float, default=0,
                   help="выйти раньше, если N с нет данных (после первых данных)")
    s.add_argument("--until", help="выйти, когда встретится строка")
    s.add_argument("--send", help="отправить строку в цель")
    s.add_argument("--eol", default="\\r\\n", help="конец строки для --send")
    s.add_argument("--reset", action="store_true", help="сбросить цель после открытия порта")
    s.add_argument("--reset-mode", default="sys", help="sys, hw или 'mon ...'")
    s.add_argument("--raw", action="store_true", help="не вырезать цветовые коды")

    s = sub.add_parser("power", help="питание цели 3.3 В от зонда")
    s.add_argument("state", choices=["on", "off", "status"])

    s = sub.add_parser("reflash", help="обновить прошивку самого Лобзика")
    s.add_argument("bin")

    s = sub.add_parser("gdb", help="произвольные команды GDB")
    s.add_argument("commands", nargs="+")
    s.add_argument("--attach", action="store_true", help="сначала скан и attach, в конце detach")
    s.add_argument("--elf")

    args = ap.parse_args()
    globals()["cmd_" + args.cmd](args)


if __name__ == "__main__":
    main()
