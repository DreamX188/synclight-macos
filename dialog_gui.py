#!/usr/bin/env python3
"""
SyncLight control panel (visible macOS dialogs).

Effects, brightness, speed, ambilight, autostart.
"""

from __future__ import annotations

import os
import shutil
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
from singleton import ensure_singleton
from sl import PRESETS, STATE_FILE
from version import display_version, window_title

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

def _is_macho(path: str) -> bool:
    try:
        out = subprocess.check_output(["file", "-b", path], text=True)
        return "Mach-O" in out
    except Exception:
        return False


def _install_app_bundle() -> None:
    from version import BUILD, VERSION

    os.makedirs(f"{APP_PATH}/Contents/MacOS", exist_ok=True)
    os.makedirs(f"{APP_PATH}/Contents/Resources", exist_ok=True)
    with open(f"{APP_PATH}/Contents/Info.plist", "w") as f:
        f.write(
            f"""<?xml version="1.0" encoding="UTF-8"?>
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
  <key>CFBundleShortVersionString</key><string>{VERSION}</string>
  <key>CFBundleVersion</key><string>{BUILD}</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>SyncLight ищет и управляет Bluetooth LED-лентами (потолок и пол).</string>
  <key>NSBluetoothPeripheralUsageDescription</key>
  <string>SyncLight ищет и управляет Bluetooth LED-лентами (потолок и пол).</string>
  <key>NSScreenCaptureUsageDescription</key>
  <string>SyncLight снимает край экрана для Ambilight и зеркала света в комнату.</string>
</dict>
</plist>
"""
        )
    # Dock BLE TCC: CFBundleExecutable must be a real Mach-O. Shell-script
    # launchers never get a usable Bluetooth grant; signed binary keeps
    # SyncLight.app as the responsible identity after exec → glass_gui.
    macos = f"{APP_PATH}/Contents/MacOS"
    os.makedirs(macos, exist_ok=True)
    for stale in ("SyncLight.bin",):
        try:
            os.remove(f"{macos}/{stale}")
        except OSError:
            pass

    launcher = f"{macos}/SyncLight"
    src = os.path.join(ROOT, "native_launch.c")
    glass = os.path.join(ROOT, "glass_gui.py")
    py = "/usr/bin/python3"
    compile_cmd = [
        "clang",
        "-O2",
        "-o",
        launcher,
        src,
        f"-DGLASS_PY=\"{glass}\"",
        f"-DPYTHON_BIN=\"{py}\"",
        "-framework",
        "CoreGraphics",
    ]
    r = subprocess.run(compile_cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(launcher) or not _is_macho(launcher):
        # Last-resort shebang (BLE/Screen TCC attach to Python — weaker)
        sys.stderr.write(
            f"SyncLight Mach-O build failed, using shell fallback:\n{r.stderr or r.stdout}\n"
        )
        with open(launcher, "w") as f:
            f.write(f"#!/bin/bash\nexec {py} \"{glass}\"\n")
        os.chmod(launcher, 0o755)
    else:
        os.chmod(launcher, 0o755)
        subprocess.run(
            ["codesign", "-s", "-", "--force", "--deep", APP_PATH],
            capture_output=True,
        )
    _make_icon(f"{APP_PATH}/Contents/Resources/AppIcon.icns")


def _make_icon(icns_path: str) -> None:
    """Ambilight-style mark: dark tile, soft spectrum halo, glowing screen core."""
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return

    iconset = os.path.expanduser("~/.synclight_icon.iconset")
    subprocess.run(["rm", "-rf", iconset], capture_output=True)
    os.makedirs(iconset, exist_ok=True)

    def _lerp(a, b, t):
        return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))

    def draw(size: int) -> Image.Image:
        # Render at 2× then downscale for smoother arcs
        s = max(64, size * 2)
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        m = max(2, s // 14)
        radius = max(8, s // 4)

        # Soft outer ambient wash (ambilight bleed)
        glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for i in range(8, 0, -1):
            t = i / 8.0
            col = _lerp((255, 92, 40, 0), (255, 140, 60, 55), t)
            inset = m - int(s * 0.04 * (1 - t))
            gd.rounded_rectangle(
                [inset, inset, s - inset, s - inset],
                radius=radius + int(s * 0.03),
                outline=col,
                width=max(2, s // 28),
            )
        glow = glow.filter(ImageFilter.GaussianBlur(radius=max(2, s // 28)))
        img = Image.alpha_composite(img, glow)
        d = ImageDraw.Draw(img)

        # Charcoal rounded tile
        d.rounded_rectangle(
            [m, m, s - m, s - m],
            radius=radius,
            fill=(18, 20, 24, 255),
        )
        # Subtle top sheen
        sheen = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sheen)
        sd.rounded_rectangle(
            [m + 1, m + 1, s - m - 1, m + s // 3],
            radius=radius,
            fill=(255, 255, 255, 22),
        )
        sheen = sheen.filter(ImageFilter.GaussianBlur(radius=max(1, s // 40)))
        img = Image.alpha_composite(img, sheen)
        d = ImageDraw.Draw(img)

        # Spectrum halo — warm top, cool bottom (room ceiling/floor)
        pad = max(4, s // 5)
        stroke = max(3, s // 14)
        ring = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        rd = ImageDraw.Draw(ring)
        # Upper warm arc
        rd.arc(
            [pad, pad, s - pad, s - pad],
            start=200,
            end=340,
            fill=(255, 120, 45, 255),
            width=stroke,
        )
        # Lower cool arc
        rd.arc(
            [pad, pad, s - pad, s - pad],
            start=20,
            end=160,
            fill=(64, 140, 255, 255),
            width=stroke,
        )
        # Side violet bridges
        rd.arc(
            [pad, pad, s - pad, s - pad],
            start=340,
            end=20,
            fill=(180, 80, 255, 220),
            width=max(2, stroke - 1),
        )
        rd.arc(
            [pad, pad, s - pad, s - pad],
            start=160,
            end=200,
            fill=(40, 220, 180, 220),
            width=max(2, stroke - 1),
        )
        ring = ring.filter(ImageFilter.GaussianBlur(radius=max(1, s // 90)))
        img = Image.alpha_composite(img, ring)
        d = ImageDraw.Draw(img)

        # Crisp ring on top of blur
        d.arc(
            [pad, pad, s - pad, s - pad],
            start=205,
            end=335,
            fill=(255, 170, 70, 255),
            width=max(2, stroke // 2),
        )
        d.arc(
            [pad, pad, s - pad, s - pad],
            start=25,
            end=155,
            fill=(90, 170, 255, 255),
            width=max(2, stroke // 2),
        )

        # Glowing screen core
        cx = cy = s // 2
        core_r = max(4, s // 7)
        core = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        cd = ImageDraw.Draw(core)
        cd.ellipse(
            [cx - core_r * 2, cy - core_r * 2, cx + core_r * 2, cy + core_r * 2],
            fill=(255, 160, 60, 40),
        )
        core = core.filter(ImageFilter.GaussianBlur(radius=max(2, s // 20)))
        img = Image.alpha_composite(img, core)
        d = ImageDraw.Draw(img)
        d.ellipse(
            [cx - core_r, cy - core_r, cx + core_r, cy + core_r],
            fill=(255, 220, 150, 255),
        )
        # Tiny highlight
        hr = max(1, core_r // 3)
        d.ellipse(
            [cx - hr, cy - core_r // 2 - hr // 2, cx + hr // 2, cy - core_r // 4],
            fill=(255, 255, 255, 180),
        )

        if size != s:
            img = img.resize((size, size), Image.Resampling.LANCZOS)
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
    for sz, name in pairs:
        draw(sz).save(os.path.join(iconset, name))

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


def main():
    ensure_singleton("dialog")
    notify(f"{window_title()} · одна копия")

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
