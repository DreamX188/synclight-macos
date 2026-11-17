#!/usr/bin/env python3
"""Install SyncLight.app into Applications and place a Desktop alias. Cleans old icons."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from dialog_gui import APP_PATH, _install_app_bundle  # noqa: E402

USER_APPS = os.path.expanduser("~/Applications")
SYSTEM_APPS = "/Applications"


def _finder(script: str) -> str:
    try:
        return subprocess.check_output(
            ["osascript", "-e", script], text=True, timeout=60
        ).strip()
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", "ignore") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return f"fail:{err or exc}"
    except Exception as exc:
        return f"fail:{exc}"


def clean_desktop() -> None:
    script = '''
tell application "Finder"
  set desk to path to desktop folder
  try
    delete (every item of desk whose name starts with "SyncLight")
  end try
end tell
return "ok"
'''
    print("clean desktop:", _finder(script))


def install_apps() -> str:
    # Build canonical bundle under project/dist
    os.makedirs(os.path.dirname(APP_PATH), exist_ok=True)
    _install_app_bundle()
    if not os.path.isdir(APP_PATH):
        raise RuntimeError(f"Failed to build app at {APP_PATH}")

    os.makedirs(USER_APPS, exist_ok=True)
    user_target = os.path.join(USER_APPS, "SyncLight.app")
    if os.path.abspath(user_target) != os.path.abspath(APP_PATH):
        subprocess.run(["rm", "-rf", user_target], capture_output=True)
        shutil.copytree(APP_PATH, user_target)
        subprocess.run(["xattr", "-cr", user_target], capture_output=True)

    sys_target = os.path.join(SYSTEM_APPS, "SyncLight.app")
    try:
        subprocess.run(["rm", "-rf", sys_target], capture_output=True)
        shutil.copytree(APP_PATH, sys_target)
        subprocess.run(["xattr", "-cr", sys_target], capture_output=True)
        if os.path.isdir(sys_target):
            return sys_target
    except Exception as exc:
        print("system Applications copy skipped:", exc)

    return user_target if os.path.isdir(user_target) else APP_PATH


def desktop_alias(app_path: str) -> None:
    # Verify app exists before aliasing
    if not os.path.isdir(app_path):
        print("desktop alias: skip, app missing", app_path)
        return
    script = f'''
tell application "Finder"
  set desk to path to desktop folder
  set appPath to POSIX file "{app_path}" as alias
  try
    delete (every item of desk whose name starts with "SyncLight")
  end try
  make new alias file at desk to appPath with properties {{name:"SyncLight"}}
  return "ok"
end tell
'''
    print("desktop alias:", _finder(script))


PYTHON_APP = (
    "/Library/Developer/CommandLineTools/Library/Frameworks/"
    "Python3.framework/Versions/3.9/Resources/Python.app"
)


def install_python_bluetooth_helpers() -> None:
    """Put Desktop shortcuts so user can add Python to Bluetooth privacy with +."""
    if not os.path.isdir(PYTHON_APP):
        print("python helpers: Python.app not found at", PYTHON_APP)
        return

    script = f'''
tell application "Finder"
  set desk to path to desktop folder
  set pyPath to POSIX file "{PYTHON_APP}" as alias
  try
    delete (every item of desk whose name is "Python для SyncLight")
  end try
  make new alias file at desk to pyPath with properties {{name:"Python для SyncLight"}}
  return "ok"
end tell
'''
    print("python alias:", _finder(script))

    help_app = os.path.expanduser("~/Desktop/Добавить Python в Bluetooth.app")
    subprocess.run(["rm", "-rf", help_app], capture_output=True)
    macos = os.path.join(help_app, "Contents", "MacOS")
    os.makedirs(macos, exist_ok=True)
    with open(os.path.join(help_app, "Contents", "Info.plist"), "w") as f:
        f.write(
            """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Добавить Python в Bluetooth</string>
  <key>CFBundleDisplayName</key><string>Добавить Python в Bluetooth</string>
  <key>CFBundleIdentifier</key><string>com.robobloq.synclight.addpython</string>
  <key>CFBundleExecutable</key><string>addpython</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict>
</plist>
"""
        )
    runner = os.path.join(macos, "addpython")
    with open(runner, "w") as f:
        f.write(
            f"""#!/bin/bash
PYAPP="{PYTHON_APP}"
open -R "$PYAPP"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth" 2>/dev/null || true
osascript <<'EOF'
display dialog "Как добавить Python в Bluetooth:

1. Внизу слева в окне Bluetooth нажми +
2. В Finder уже открыт Python — нажми «Open»
3. Включи тумблер у Python
4. Запусти SyncLight → «Подключить BT»

Можно перетащить с рабочего стола ярлык «Python для SyncLight» в список." buttons {{"Понятно"}} default button 1 with title "SyncLight"
EOF
"""
        )
    os.chmod(runner, 0o755)
    print("python helper app:", help_app)


def main(silent: bool = True) -> None:
    """Install app bundle. silent=True: never open Finder/Settings windows."""
    clean_desktop()
    app_path = install_apps()
    print("app installed:", app_path, "exists=", os.path.isdir(app_path))
    desktop_alias(app_path)
    # Python Bluetooth helper is optional and opens Finder — skip by default.
    if not silent:
        install_python_bluetooth_helpers()
        if os.path.isdir(app_path):
            subprocess.run(["open", "-R", app_path], capture_output=True)
    print("done")


if __name__ == "__main__":
    main()
