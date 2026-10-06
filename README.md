# SyncLight for macOS

**macOS-драйвер и приложение для USB-амбилайт лент**, которые продаются на **[Ozon](https://www.ozon.ru/)**, **[Wildberries](https://www.wildberries.ru/)** и в других магазинах под брендами **Robobloq SyncLight / QuikLight** (USB HID VID `0x1A86`, PID `0xFE07`).

Официальное ПО рассчитано в основном на Windows. Этот проект даёт полноценную работу на Mac: сон/пробуждение дисплея, амбилайт с экрана, эффекты прошивки, музыкальный режим, CLI и Liquid Glass GUI с иконкой в строке меню.

> Не связано с Robobloq. Протокол восстановлен по официальному приложению SyncLight и открытым проектам ([SyncRGB](https://github.com/Tonic-Jin/SyncRGB), [quicklight-linux](https://github.com/jrcn1991/quicklight-linux)).

---

## English

**macOS driver / control app for USB ambilight LED strips** sold on **Ozon**, **Wildberries**, and similar marketplaces as **Robobloq SyncLight / QuikLight** (VID `0x1A86`, PID `0xFE07`).

Official apps target Windows. This project brings full Mac support: display sleep/wake, screen ambilight, firmware effects, music mode, CLI, and a Liquid Glass GUI with a menu-bar icon.

> Not affiliated with Robobloq. Protocol reverse-engineered from the SyncLight app and community projects ([SyncRGB](https://github.com/Tonic-Jin/SyncRGB), [quicklight-linux](https://github.com/jrcn1991/quicklight-linux)).

---

## Для кого / Who is this for

| RU | EN |
|---|---|
| Купили амбилайт на Ozon / Wildberries, а на Mac нет нормального драйвера | Bought an ambilight strip on Ozon / Wildberries; no proper Mac driver |
| Лента определяется как Robobloq SyncLight / QuikLight по USB | Strip shows up as Robobloq SyncLight / QuikLight over USB |
| Нужны амбилайт с экрана, эффекты и музыкальный режим как в Windows-приложении | Want screen sync, effects, and music mode like the Windows app |

Если лента не того чипа/протокола — этот проект не подойдёт. Проверяйте VID/PID: `1A86:FE07`.

---

## Возможности / Features

| | RU | EN |
|---|---|---|
| **GUI** | Полупрозрачное Liquid Glass окно + иконка в меню | Translucent Liquid Glass window + menu-bar icon |
| **Амбилайт** | Синхронизация с краями экрана (~20 fps) | Screen-edge sync (~20 fps) via SC `setSyncScreen` |
| **Эффекты** | 7 прошивочных эффектов (радуга, дыхание, …) | 7 firmware effects (Rainbow, Breathing, Chase, …) |
| **Музыка** | Реакция на микрофон ленты + чувствительность | Device-mic rhythm effects + sensitivity |
| **Яркость / скорость** | Как в оригинальном SyncLight | Matches official SyncLight scaling |
| **Сон Mac** | Выключает ленту при засыпании дисплея | Turns strip off on display sleep, restores on wake |
| **CLI** | `sl on \| off \| color \| effect \| ambi …` | Same CLI |
| **Автозапуск** | Галочка «открывать при входе» | Optional Login Item |

---

## Требования / Requirements

- macOS 12+ (Apple Silicon или Intel)
- Python 3.9+
- USB-лента Robobloq SyncLight / QuikLight
- **Запись экрана** для амбилайта  
  (Системные настройки → Конфиденциальность и безопасность → Запись экрана → разрешить Python / SyncLight)

### Пакеты Python

```bash
pip3 install --user -r requirements.txt
```

Нужен также **libhidapi** (`brew install hidapi` или `libhidapi.dylib` в `~/.local/lib`).

---

## Быстрый старт / Quick start

```bash
git clone https://github.com/DreamX188/synclight-macos.git
cd synclight-macos

pip3 install --user -r requirements.txt
./install.sh               # драйвер сна/пробуждения + CLI
python3 install_icons.py   # Applications + ярлык на рабочий стол
python3 glass_gui.py       # Liquid Glass UI
```

Или откройте **Программы → SyncLight**.

В строке меню: иконка лампочки / **SL** (Показать / Амбилайт / Выход).

---

## GUI

```bash
python3 glass_gui.py
# или
sl-gui
```

- **Ambilight / On / Off**
- **Effects** — анимации прошивки (индексы 0–6)
- **Music** — режимы под микрофон + чувствительность
- **Colors** — тёплый / холодный / белый / RGB
- **Brightness** & **Speed**
- **Open at Login**

Закрытие окна не завершает приложение — остаётся иконка в меню.

---

## CLI

```bash
sl on
sl off
sl color warm
sl color 255 128 0
sl effect 0          # Rainbow Flow
sl effect 1          # Breathing
sl sound 0           # Rhythm Wave
sl brightness 200
sl speed 70          # выше = быстрее
sl ambi              # амбилайт до Ctrl+C
```

Список эффектов:

```bash
sl effect
sl sound
```

---

## Драйвер сна / Sleep–wake driver

Ставится через `./install.sh` как LaunchAgent `com.robobloq.synclight`.

```bash
./install.sh              # установить и запустить
./install.sh --uninstall  # удалить

tail -f ~/Library/Logs/SyncLight.log
```

GUI временно освобождает HID при смене цвета/эффекта, затем перезагружает агент.

---

## Структура проекта / Layout

```
synclight.py      # демон сна/пробуждения дисплея
sl.py             # CLI
device.py         # HID-протокол (RB / SC)
ambilight.py      # захват экрана → setSyncScreen
glass_gui.py      # Liquid Glass UI + строка меню
ui/               # HTML / CSS / JS
install.sh        # LaunchAgent + PATH
install_icons.py  # SyncLight.app → Applications + Desktop
story.md          # заметки по реверсу (оригинал)
```

---

## Протокол / Protocol notes

USB HID interface `0`, report ID `0x00` перед записью.

**RB** (управление): `"RB" + len + id + action + payload + checksum`  
**SC** (амбилайт): `"SC" + len16be + id + 0x80 + [idx,R,G,B,idx]*N + checksum`

| Action | Code | Role |
|---|---|---|
| setSyncScreen | `0x80` | Per-LED screen sync |
| setLedEffect | `0x85` | type `2` dynamic / `3` music, index `0..6` |
| setSectionLED | `0x86` | Solid / clear |
| setBrightness | `0x87` | `5..255` |
| setDynamicSpeed | `0x8A` | device: low=fast (UI inverted) |
| setSoundSensitivity | `0x8B` | mic sensitivity |

Порядок включения динамического эффекта (как в официальном ПО):  
яркость → очистка секции → `setLedEffect(2, i)` → скорость.

---

## Разрешения / Permissions

| Permission | Зачем / Why |
|---|---|
| Запись экрана / Screen Recording | Амбилайт (`mss`) |
| Desktop / Files | Ярлык на рабочем столе (из Applications всё равно работает) |
| Automation (Finder) | Создание алиаса через AppleScript |

---

## Credits

- Идея sleep-драйвера и RE-story: [jakebuild/synclight](https://github.com/jakebuild/synclight)
- Протокол / эффекты: SyncLight Electron, [SyncRGB](https://github.com/Tonic-Jin/SyncRGB), [quicklight-linux](https://github.com/jrcn1991/quicklight-linux)

## License

MIT
