# Лобзик напрямую из GDB

Нужен, когда команд `lobzik.py` не хватает: пошаговая отладка, точки останова,
нестандартная последовательность. Для разовых действий удобнее `lobzik.py gdb "..."`
(он сам подставит порт): `lobzik.py gdb --attach --elf app.elf "break main" "continue"`.

## Подключение

```
arm-none-eabi-gdb build/app.elf
(gdb) target extended-remote COM5          # Linux: /dev/ttyACM0, macOS: /dev/cu.usbmodem<SN>1
(gdb) monitor frequency 2M                 # без этого запись в разы медленнее
(gdb) monitor swd_scan                     # JTAG: monitor jtag_scan
(gdb) attach 1                             # номер из списка скана; ядро останавливается
(gdb) load                                 # записать секции ELF во flash
(gdb) compare-sections                     # сверить
(gdb) run                                  # или continue; detach — отпустить ядро
```

`monitor` можно сокращать до `mon`. Список команд зонда — `mon help`; после `attach`
у некоторых целей (STM32, К1921ВГ015) добавляются свои.

## Пакетный режим (для агента)

```
arm-none-eabi-gdb -batch -nx -ex "file build/app.elf" -ex "target extended-remote COM5" \
  -ex "mon frequency 2M" -ex "mon swd_scan" -ex "attach 1" -ex "load" \
  -ex "compare-sections" -ex "detach"
```

- `-batch` выходит после последней команды; GDB не остаётся висеть на порту.
- `compare-sections` требует `file <elf>` в начале: `load <elf>` сам по себе не задаёт
  исполняемый файл, и сверка отвечает «command cannot be used without an exec file».
- **Windows, COM10 и выше**: порт пишется как `\\.\COM10`. В аргументе `-ex` из Python —
  строка `"\\\\.\\COM10"`. В `.gdb`-файле обратные слэши GDB тоже обрабатывает:
  `\\.\COM10` в файле превращается в `\.\COM10` и не открывается, пиши `\\\\.\\COM10`.
  COM1–COM9 работают и просто как `COM5`.
- `.gdb`-файлы сохраняй с переводами строк LF: с CRLF блоки `commands ... end` не разбираются.
- Таймаут на запуск GDB ставь с запасом: запись 1 МБ на 2 МГц — около минуты.

## Полезное

| Задача | Команды |
|---|---|
| сброс Cortex-M без nRST | `set mem inaccessible-by-default off`, `set *(unsigned int*)0xE000ED0C = 0x05FA0004`, `detach` |
| импульс nRST | `mon reset` (контакт 12 должен быть подключён) |
| подключение под сбросом | `mon connect_rst enable` перед сканом (цель, которая сразу уходит в сон или перенастраивает SWD-выводы) |
| долго останавливается | `mon halt_timeout 5000` |
| регистры периферии вне карты памяти | `set mem inaccessible-by-default off` |
| питание цели | `mon tpwr enable` / `disable` |
| RTT вместо UART | `mon rtt enable`, затем вывод — на UART-порт Лобзика |
| стереть flash целиком | `mon erase_mass` (поддерживается не всеми целями) |

`detach` отпускает ядро — программа продолжает работать. `mon reset` отключает GDB
от цели: после него снова `swd_scan` и `attach`.

## VS Code (Cortex-Debug)

```json
{
  "name": "Лобзик",
  "type": "cortex-debug",
  "request": "launch",
  "servertype": "bmp",
  "BMPGDBSerialPort": "COM5",
  "interface": "swd",
  "executable": "${workspaceFolder}/build/app.elf",
  "gdbPath": "arm-none-eabi-gdb",
  "svdFile": "${workspaceFolder}/STM32H723.svd",
  "preLaunchCommands": ["mon frequency 2M"]
}
```

Пока идёт отладка в VS Code, порт GDB занят — `lobzik.py` его не откроет.
UART-порт при этом свободен: лог можно читать параллельно (`lobzik.py uart`).
