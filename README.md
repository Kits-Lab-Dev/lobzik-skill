# Навык «Лобзик» для ИИ-агентов

Навык учит ИИ-агента работать с отладчиком [«Лобзик»](https://wiki.kitslab.ru/debugers/fretsaw/)
(KitsLab, Black Magic Debug на STM32L432): прошивать плату, сбрасывать, читать память
и регистры на живой плате, снимать лог UART, разбирать зависания и HardFault,
включать питание цели и обновлять прошивку самого Лобзика.

Формат — открытый стандарт [Agent Skills](https://agentskills.io): папка `lobzik/` с `SKILL.md`,
скриптом и справкой; `SKILL.md` — обычный Markdown, его может прочитать любой агент. Скрипт `lobzik/scripts/lobzik.py` работает и без агента — из терминала.

## Установка

Скопируйте папку `lobzik/` в каталог навыков вашего агента:

| Агент | Куда |
|---|---|
| Claude Code | `~/.claude/skills/lobzik/` (для всех проектов) или `.claude/skills/lobzik/` в проекте |
| Другие агенты с Agent Skills | каталог навыков агента — см. его документацию |
| Агент без поддержки навыков | положите папку в проект и напишите в `AGENTS.md`: «для работы с отладчиком читай `lobzik/SKILL.md`» |

```bash
git clone https://github.com/Kits-Lab-Dev/lobzik-skill
cp -r lobzik-skill/lobzik ~/.claude/skills/
```

Агент сам вспомнит про навык, когда понадобится прошить плату или посмотреть, что с ней.

## Что нужно на компьютере

- Python 3.8+, `pip install pyserial`
- GDB под архитектуру платы: `arm-none-eabi-gdb` ([Arm GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads)
  или STM32CubeCLT) либо `gdb-multiarch`; для RISC-V — `riscv-none-elf-gdb`
- Для обновления прошивки Лобзика: `pip install pyusb libusb-package` и
  STM32CubeProgrammer или `dfu-util`

## Без агента

```bash
python lobzik/scripts/lobzik.py list
python lobzik/scripts/lobzik.py flash build/app.elf
python lobzik/scripts/lobzik.py uart --reset --time 10
python lobzik/scripts/lobzik.py --help
```

## Прошивка Лобзика

Свежая прошивка — в [релизах](https://github.com/Kits-Lab-Dev/blackmagic/releases/latest)
(`fretsaw_bmp.bin`). Обновить: `python lobzik/scripts/lobzik.py reflash` — скрипт сам скачает
её и запишет, кнопку нажимать не нужно (с прошивки от 30.09.2026).

## Лицензия

MIT — см. `LICENSE`.
