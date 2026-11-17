#!/usr/bin/env python3
"""Legacy Dock helper — SyncLight.app now uses native_launch.c (Mach-O).

Kept so old wrappers that still point here start the GUI instead of failing.
Prefer /Applications/SyncLight.app (reinstall via install_icons.py).
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
GLASS = os.path.join(ROOT, "glass_gui.py")


def main() -> int:
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    try:
        from singleton import prelaunch_cleanup

        prelaunch_cleanup(settle_s=0.35)
    except Exception:
        pass
    py = "/usr/bin/python3"
    os.execv(py, [py, GLASS])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
