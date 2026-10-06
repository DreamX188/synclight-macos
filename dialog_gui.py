#!/usr/bin/env python3
"""
SyncLight control panel (visible macOS dialogs).

Effects, brightness, speed, ambilight, autostart.
"""

from __future__ import annotations

import os
import sys
import traceback

# Always resolve imports relative to this file (Finder launches have no cwd)
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import subprocess

from ambilight import AmbilightEngine
from device import (
    DYNAMIC_EFFECTS,
    SOUND_EFFECTS,
    apply_dynamic_effect,
    apply_sound_effect,
    oneshot,
    set_brightness,
    set_dynamic_speed,
    set_section_led,
)
from settings import load_settings, save_settings
from sl import PRESETS, STATE_FILE

GUI_PLIST = os.path.expanduser(
    "~/Library/LaunchAgents/com.robobloq.synclight.gui.plist"
)
APP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dist", "SyncLight.app"
)
DIALOG_SCRIPT = os.path.abspath(__file__)
GLASS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glass_gui.py")
PYTHON = "/usr/bin/python3"
if not os.path.exists(PYTHON):
    PYTHON = sys.executable


def _as_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def choose(options, prompt, title="SyncLight"):
    items = ", ".join(f'"{_as_escape(o)}"' for o in options)
    script = f'''
try
  set theChoice to choose from list {{{items}}} with prompt "{_as_escape(prompt)}" with title "{title}" OK button name "OK" cancel button name "Back"
  if theChoice is false then
    return "Back"
  end if
  return item 1 of theChoice
on error errMsg number errNum
  return "ERROR:" & errMsg
end try
'''
    try:
        return subprocess.check_output(["osascript", "-e", script], text=True).strip()
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or str(exc)).strip()
        return f"ERROR:{err}"


def notify(text):
    text = _as_escape(str(text)[:180])
    subprocess.run(
        ["osascript", "-e", f'display notification "{text}" with title "SyncLight"'],
        capture_output=True,
    )


def dialog(message, buttons=("OK",)):
    buttons = list(buttons)[:3]
    btn = ", ".join(f'"{_as_escape(b)}"' for b in buttons)
    default = _as_escape(buttons[0])
    script = f'''
try
  display dialog "{_as_escape(message)}" with title "SyncLight" buttons {{{btn}}} default button "{default}"
  return button returned of result
on error errMsg
  return "ERROR:" & errMsg
end try
'''
    try:
        return subprocess.check_output(["osascript", "-e", script], text=True).strip()
    except subprocess.CalledProcessError as exc:
        return f"ERROR:{(exc.stderr or str(exc)).strip()}"


def show_error(exc: BaseException) -> None:
    msg = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    detail = traceback.format_exc()[-500:]
    dialog(f"Ошибка:\\n{msg}\\n\\n{detail}")


def autostart_enabled() -> bool:
    return os.path.exists(GUI_PLIST)


def set_autostart(enabled: bool) -> None:
    if enabled:
        os.makedirs(os.path.dirname(GUI_PLIST), exist_ok=True)
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.robobloq.synclight.gui</string>
  <key>ProgramArguments</key>
  <array>
    <string>{PYTHON}</string>
    <string>{DIALOG_SCRIPT}</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
"""
        with open(GUI_PLIST, "w") as f:
            f.write(body)
        subprocess.run(["launchctl", "unload", GUI_PLIST], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", GUI_PLIST], capture_output=True)
    else:
        if os.path.exists(GUI_PLIST):
            subprocess.run(["launchctl", "unload", GUI_PLIST], capture_output=True)
            try:
                os.remove(GUI_PLIST)
            except OSError:
                pass


def ensure_desktop_icon() -> str:
    """Install app bundle; put a Desktop shortcut if macOS allows Desktop access."""
    _install_app_bundle()
    subprocess.run(["xattr", "-cr", APP_PATH], capture_output=True)

    desktop = os.path.expanduser("~/Desktop")
    target = os.path.join(desktop, "SyncLight.app")

    # 1) Prefer Finder AppleScript — prompts for Desktop permission instead of crashing
    script = f'''
tell application "Finder"
  try
    set desk to path to desktop folder
    set appPath to POSIX file "{APP_PATH}" as alias
    try
      delete (every item of desk whose name is "SyncLight.app")
    end try
    try
      delete (every item of desk whose name is "SyncLight.command")
    end try
    make new alias file at desk to appPath with properties {{name:"SyncLight"}}
    return "ok"
  on error errMsg
    return "fail:" & errMsg
  end try
end tell
'''
    try:
        out = subprocess.check_output(
            ["osascript", "-e", script], text=True, timeout=30
        ).strip()
        if out.startswith("ok"):
            return target
    except Exception:
        pass

    # 2) Direct copy (works if Terminal/Python already has Desktop access)
    try:
        os.makedirs(desktop, exist_ok=True)
        subprocess.run(["rm", "-rf", target], capture_output=True)
        subprocess.run(["cp", "-R", APP_PATH, target], check=True)
        subprocess.run(["xattr", "-cr", target], capture_output=True)
        return target
    except Exception:
        pass

    # 3) Fallback: no Desktop write rights — app still works from ~/Applications
    return APP_PATH

def _install_app_bundle() -> None:
    os.makedirs(f"{APP_PATH}/Contents/MacOS", exist_ok=True)
    os.makedirs(f"{APP_PATH}/Contents/Resources", exist_ok=True)
    with open(f"{APP_PATH}/Contents/Info.plist", "w") as f:
        f.write(
            """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>SyncLight</string>
  <key>CFBundleDisplayName</key><string>SyncLight</string>
  <key>CFBundleIdentifier</key><string>com.robobloq.synclight.gui</string>
  <key>CFBundleExecutable</key><string>SyncLight</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""
        )
    with open(f"{APP_PATH}/Contents/MacOS/SyncLight", "w") as f:
        f.write(
            "#!/bin/bash\n"
            f'cd "{ROOT}" || exit 1\n'
            f'exec "{PYTHON}" "{GLASS_SCRIPT}"\n'
        )
    os.chmod(f"{APP_PATH}/Contents/MacOS/SyncLight", 0o755)
    _make_icon(f"{APP_PATH}/Contents/Resources/AppIcon.icns")


def _make_icon(icns_path: str) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return

    iconset = os.path.expanduser("~/.synclight_icon.iconset")
    subprocess.run(["rm", "-rf", iconset], capture_output=True)
    os.makedirs(iconset, exist_ok=True)

    def draw(size: int) -> Image.Image:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        m = max(1, size // 10)
        d.rounded_rectangle(
            [m, m, size - m, size - m],
            radius=max(2, size // 5),
            fill=(26, 29, 33, 255),
        )
        pad = max(2, size // 5)
        d.arc(
            [pad, pad, size - pad, size - pad],
            start=200,
            end=340,
            fill=(255, 140, 40, 255),
            width=max(2, size // 12),
        )
        d.arc(
            [pad, pad, size - pad, size - pad],
            start=20,
            end=160,
            fill=(47, 111, 237, 255),
            width=max(2, size // 12),
        )
        c = size // 2
        r = max(2, size // 10)
        d.ellipse([c - r, c - r, c + r, c + r], fill=(255, 200, 100, 255))
        return img

    pairs = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for s, name in pairs:
        draw(s).save(os.path.join(iconset, name))

    subprocess.run(
        ["iconutil", "-c", "icns", iconset, "-o", icns_path],
        capture_output=True,
    )


def load_color():
    try:
        parts = open(STATE_FILE).read().strip().split()
        return tuple(int(x) for x in parts)
    except Exception:
        return (255, 200, 100)


def save_color(r, g, b):
    open(STATE_FILE, "w").write(f"{r} {g} {b}\n")


def _stop_other_instances() -> None:
    mypid = os.getpid()
    try:
        out = subprocess.check_output(["pgrep", "-f", "dialog_gui.py"], text=True)
    except subprocess.CalledProcessError:
        return
    for line in out.split():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != mypid:
            subprocess.run(["kill", str(pid)], capture_output=True)


def main():
    _stop_other_instances()

    cfg = load_settings()
    color = tuple(cfg.get("color") or load_color())
    if len(color) != 3:
        color = load_color()
    brightness = int(cfg.get("brightness", 200))
    speed = int(cfg.get("effect_speed", 50))
    ambi = AmbilightEngine()
    ambi_on = False
    mode = cfg.get("last_mode", "static")

    icon_path = APP_PATH
    try:
        icon_path = ensure_desktop_icon()
    except Exception as exc:
        # Never block the control panel because of Desktop TCC
        notify(f"Иконка: {exc}")

    on_desktop = icon_path.startswith(os.path.expanduser("~/Desktop"))
    if on_desktop:
        where = "На рабочем столе появилась иконка SyncLight."
    else:
        where = (
            "Ярлык на рабочий стол недоступен (macOS блокирует Desktop).\n"
            "Запуск: Applications → SyncLight\n"
            "или: Системные настройки → Конфиденциальность → Файлы и папки\n"
            "→ разреши Desktop для Terminal/Python."
        )

    dialog(f"SyncLight готов.\n{where}\nДальше откроется меню управления.")
    while True:
        try:
            auto = "ON" if autostart_enabled() else "OFF"
            state = "Ambilight" if ambi_on else mode
            choice = choose(
                [
                    "Ambilight",
                    "Effects",
                    "Music effects",
                    "Brightness",
                    "Effect speed",
                    "Light On",
                    "Light Off",
                    "Colors",
                    f"Autostart: {auto}",
                    "Quit",
                ],
                f"Mode: {state} | Brightness: {brightness} | Speed: {speed}",
            )

            if choice.startswith("ERROR:"):
                dialog(choice)
                continue

            if choice in ("Quit",):
                if ambi_on:
                    ambi.stop()
                cfg.update(
                    {
                        "brightness": brightness,
                        "effect_speed": speed,
                        "color": list(color),
                        "last_mode": mode,
                        "autostart_gui": autostart_enabled(),
                    }
                )
                save_settings(cfg)
                break

            if choice == "Back":
                continue

            if choice == "Ambilight":
                if ambi_on:
                    ambi.stop()
                    ambi_on = False
                    mode = "static"
                    notify("Ambilight OFF")
                else:
                    ambi.start()
                    ambi_on = True
                    mode = "ambi"
                    notify("Ambilight ON")

            elif choice == "Effects":
                names = [f"{i}. {n}" for i, n in DYNAMIC_EFFECTS]
                pick = choose(names + ["Back"], "Dynamic effects")
                if pick.startswith("ERROR:"):
                    dialog(pick)
                elif pick not in ("Back", "", "false"):
                    idx = names.index(pick)
                    effect_i = DYNAMIC_EFFECTS[idx][0]
                    if ambi_on:
                        ambi.stop()
                        ambi_on = False
                    apply_dynamic_effect(effect_i, speed=speed, brightness=brightness)
                    mode = "dynamic"
                    cfg["last_effect_index"] = effect_i
                    notify(DYNAMIC_EFFECTS[idx][1])

            elif choice == "Music effects":
                names = [f"{i}. {n}" for i, n in SOUND_EFFECTS]
                pick = choose(names + ["Back"], "Sound reactive (strip mic)")
                if pick.startswith("ERROR:"):
                    dialog(pick)
                elif pick not in ("Back", "", "false"):
                    idx = names.index(pick)
                    effect_i = SOUND_EFFECTS[idx][0]
                    if ambi_on:
                        ambi.stop()
                        ambi_on = False
                    apply_sound_effect(effect_i, speed=speed, brightness=brightness)
                    mode = "sound"
                    notify(SOUND_EFFECTS[idx][1])

            elif choice == "Brightness":
                pick = choose(
                    ["10%", "25%", "40%", "55%", "70%", "85%", "100%", "Back"],
                    f"Brightness now: {brightness}",
                )
                mapping = {
                    "10%": 25,
                    "25%": 64,
                    "40%": 102,
                    "55%": 140,
                    "70%": 178,
                    "85%": 217,
                    "100%": 255,
                }
                if pick in mapping:
                    brightness = mapping[pick]
                    oneshot(set_brightness(brightness), settle_ms=150)
                    notify(f"Brightness {pick}")

            elif choice == "Effect speed":
                pick = choose(
                    ["Very slow", "Slow", "Normal", "Fast", "Very fast", "Back"],
                    f"Speed now: {speed}",
                )
                mapping = {
                    "Very slow": 10,
                    "Slow": 25,
                    "Normal": 50,
                    "Fast": 75,
                    "Very fast": 100,
                }
                if pick in mapping:
                    speed = mapping[pick]
                    oneshot(set_dynamic_speed(speed), settle_ms=150)
                    notify(f"Speed {pick}")

            elif choice == "Light On":
                if ambi_on:
                    ambi.stop()
                    ambi_on = False
                oneshot(set_section_led(*color))
                oneshot(set_brightness(brightness), settle_ms=150)
                mode = "static"
                notify("Light ON")

            elif choice == "Light Off":
                if ambi_on:
                    ambi.stop()
                    ambi_on = False
                oneshot(set_section_led(0, 0, 0))
                mode = "off"
                notify("Light OFF")

            elif choice == "Colors":
                names = [n.title() for n in PRESETS]
                pick = choose(names + ["Back"], "Static color")
                key = pick.lower()
                if key in PRESETS:
                    if ambi_on:
                        ambi.stop()
                        ambi_on = False
                    color = PRESETS[key]
                    save_color(*color)
                    oneshot(set_section_led(*color))
                    oneshot(set_brightness(brightness), settle_ms=150)
                    mode = "static"
                    notify(pick)

            elif choice.startswith("Autostart"):
                enabled = autostart_enabled()
                pick = dialog(
                    f"Автозапуск SyncLight при входе в macOS.\n"
                    f"Сейчас: {'включён' if enabled else 'выключен'}",
                    buttons=("Enable", "Disable", "Cancel"),
                )
                if pick == "Enable":
                    set_autostart(True)
                    notify("Autostart ON")
                elif pick == "Disable":
                    set_autostart(False)
                    notify("Autostart OFF")

            cfg.update(
                {
                    "brightness": brightness,
                    "effect_speed": speed,
                    "color": list(color),
                    "last_mode": mode,
                    "autostart_gui": autostart_enabled(),
                }
            )
            save_settings(cfg)

        except Exception as exc:
            show_error(exc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            show_error(exc)
        except Exception:
            print(exc, file=sys.stderr)
        sys.exit(1)
