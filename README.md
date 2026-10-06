# SyncLight for macOS

Modern macOS control app for the **Robobloq SyncLight / QuikLight** USB LED strip  
(VID `0x1A86`, PID `0xFE07`).

Auto sleep/wake, screen ambilight, hardware effects, music mode, CLI, and a Liquid Glass GUI with a menu-bar icon.

> Not affiliated with Robobloq. Protocol reverse-engineered from the official SyncLight app and community projects ([SyncRGB](https://github.com/Tonic-Jin/SyncRGB), [quicklight-linux](https://github.com/jrcn1991/quicklight-linux)).

---

## Features

| Feature | Description |
|---|---|
| **Liquid Glass GUI** | Translucent control window + menu-bar icon |
| **Ambilight** | Screen-edge sync (~20 fps) via SC `setSyncScreen` |
| **Dynamic effects** | 7 firmware effects (Rainbow, Breathing, Chase, …) |
| **Music mode** | Device-mic rhythm effects + sensitivity |
| **Brightness / speed** | Matches official SyncLight scaling (speed inverted as on device) |
| **Sleep driver** | Turns the strip off when the display sleeps, restores on wake |
| **CLI** | `sl on \| off \| color \| effect \| ambi …` |
| **Autostart** | Optional Login Item for the GUI |

---

## Requirements

- macOS 12+ (Apple Silicon or Intel)
- Python 3.9+
- USB-connected Robobloq SyncLight strip
- **Screen Recording** permission for Ambilight  
  (System Settings → Privacy & Security → Screen Recording → allow Python / SyncLight)

### Python packages

```bash
pip3 install --user \
  hid \
  mss \
  'pyobjc-core==10.3.2' \
  'pyobjc-framework-Cocoa==10.3.2' \
  'pyobjc-framework-WebKit==10.3.2' \
  Pillow
```

You also need **libhidapi**. If `import hid` fails, build/install hidapi (Homebrew: `brew install hidapi`) or place `libhidapi.dylib` where the `hid` package can load it (this project documents a `~/.local/lib` approach for machines without Homebrew sudo).

---

## Quick start

```bash
git clone https://github.com/DreamX188/synclight-macos.git
cd synclight-macos

pip3 install --user -r requirements.txt
./install.sh          # sleep/wake LaunchAgent + CLI helpers
python3 install_icons.py   # Applications + Desktop alias
python3 glass_gui.py       # Liquid Glass UI
```

Or open **Applications → SyncLight**.

Menu bar: look for the lightbulb / **SL** icon (Show / Ambilight / Quit).

---

## GUI

```bash
python3 glass_gui.py
# or
sl-gui
```

- **Ambilight / On / Off**
- **Effects** — firmware animations (indices 0–6)
- **Music** — mic-reactive modes + sensitivity slider
- **Colors** — warm / cool / white / RGB presets
- **Brightness** & **Speed**
- **Open at Login**

Closing the window keeps the menu-bar icon alive.

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
sl speed 70          # UI scale: higher = faster
sl ambi              # ambilight until Ctrl+C
```

List effects:

```bash
sl effect
sl sound
```

---

## Sleep / wake driver

Installed by `./install.sh` as LaunchAgent `com.robobloq.synclight`.

```bash
./install.sh              # install + start
./install.sh --uninstall  # remove

tail -f ~/Library/Logs/SyncLight.log
```

The GUI temporarily releases the HID device when you change colors/effects, then reloads the agent.

---

## Project layout

```
synclight.py      # display sleep/wake daemon
sl.py             # CLI
device.py         # HID protocol (RB / SC)
ambilight.py      # screen capture → setSyncScreen
glass_gui.py      # Liquid Glass WKWebView UI + menu bar
ui/               # HTML / CSS / JS for the glass UI
install.sh        # LaunchAgent + PATH helpers
install_icons.py  # SyncLight.app → Applications + Desktop
story.md          # reverse-engineering notes (original)
```

---

## Protocol notes

USB HID interface `0`, report ID `0x00` prepended on write.

**RB** (control): `"RB" + len + id + action + payload + checksum`  
**SC** (ambilight): `"SC" + len16be + id + 0x80 + [idx,R,G,B,idx]*N + checksum`

| Action | Code | Role |
|---|---|---|
| setSyncScreen | `0x80` | Per-LED screen sync |
| setLedEffect | `0x85` | type `2` dynamic / `3` music, index `0..6` |
| setSectionLED | `0x86` | Solid / clear |
| setBrightness | `0x87` | `5..255` |
| setDynamicSpeed | `0x8A` | device: low=fast (UI inverted) |
| setSoundSensitivity | `0x8B` | mic sensitivity |

Applying a dynamic effect follows the official order:  
brightness → clear section → `setLedEffect(2, i)` → speed.

---

## Permissions

| Permission | Why |
|---|---|
| Screen Recording | Ambilight capture (`mss`) |
| Desktop / Files | Optional Desktop alias (GUI still works from Applications) |
| Automation (Finder) | Creating Desktop alias via AppleScript |

---

## Credits

- Original sleep-driver idea & RE story: [jakebuild/synclight](https://github.com/jakebuild/synclight)
- Protocol / effect flow: SyncLight Electron app, [SyncRGB](https://github.com/Tonic-Jin/SyncRGB), [quicklight-linux](https://github.com/jrcn1991/quicklight-linux)

## License

MIT — see upstream project intent; add a `LICENSE` file if you redistribute.
