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


def main() -> None:
    clean_desktop()
    app_path = install_apps()
    print("app installed:", app_path, "exists=", os.path.isdir(app_path))
    desktop_alias(app_path)
    if os.path.isdir(app_path):
        subprocess.run(["open", "-R", app_path], capture_output=True)
    print("done")


if __name__ == "__main__":
    main()
