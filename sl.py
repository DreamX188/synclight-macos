#!/usr/bin/env python3
"""
sl — SyncLight CLI

Usage:
  sl on                  Turn light on (warm white)
  sl off                 Turn light off
  sl color warm          Warm white  (255, 200, 100)
  sl color white         Pure white  (255, 255, 255)
  sl color cool          Cool white  (200, 220, 255)
  sl color red           Red         (255,   0,   0)
  sl color green         Green       (  0, 255,   0)
  sl color blue          Blue        (  0, 100, 255)
  sl color purple        Purple      (150,   0, 255)
  sl color R G B         Custom RGB  e.g.  sl color 255 128 0
  sl ambi                Start ambilight (Ctrl+C to stop)
  sl effect <0-11>       Hardware dynamic effect
  sl sound <0-6>         Sound-reactive effect (strip mic)
  sl brightness <5-255>  Brightness
  sl speed <5-100>       Effect speed
"""

import os
import sys

try:
    import hid  # noqa: F401
except ImportError:
    print("Missing dependency: pip3 install hid")
    sys.exit(1)

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

STATE_FILE = os.path.expanduser("~/.synclight")

PRESETS = {
    "warm":   (255, 200, 100),
    "white":  (255, 255, 255),
    "cool":   (200, 220, 255),
    "red":    (255,   0,   0),
    "green":  (  0, 255,   0),
    "blue":   (  0, 100, 255),
    "purple": (150,   0, 255),
}

# Back-compat exports used by gui.py
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.robobloq.synclight.plist")


def _driver_loaded():
    from device import driver_loaded
    return driver_loaded()


def _set_color(r, g, b):
    return set_section_led(r, g, b)


def _send(data):
    oneshot(data)


def cmd_on():
    try:
        r, g, b = (int(x) for x in open(STATE_FILE).read().strip().split())
    except Exception:
        r, g, b = 255, 200, 100
    try:
        _send(_set_color(r, g, b))
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)
    print(f"Light on  rgb({r}, {g}, {b})")


def cmd_off():
    try:
        _send(_set_color(0, 0, 0))
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)
    print("Light off")


def cmd_color(args):
    if not args:
        print("Usage: sl color <name|R G B>")
        print("Names:", ", ".join(PRESETS))
        sys.exit(1)

    if args[0] in PRESETS:
        r, g, b = PRESETS[args[0]]
    elif len(args) == 3:
        try:
            r, g, b = int(args[0]), int(args[1]), int(args[2])
            if not all(0 <= v <= 255 for v in (r, g, b)):
                raise ValueError
        except ValueError:
            print("RGB values must be integers 0-255")
            sys.exit(1)
    else:
        print(f"Unknown colour '{args[0]}'. Available:", ", ".join(PRESETS))
        sys.exit(1)

    open(STATE_FILE, "w").write(f"{r} {g} {b}\n")
    try:
        _send(_set_color(r, g, b))
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)
    print(f"Color → rgb({r}, {g}, {b})")


def cmd_ambi():
    from ambilight import AmbilightEngine
    print("Ambilight running — Ctrl+C to stop")
    print("If the strip stays dark, grant Screen Recording to Terminal/Python")
    print("in System Settings → Privacy & Security → Screen Recording")
    eng = AmbilightEngine()
    eng.start()
    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping…")
        eng.stop()


def cmd_effect(args):
    if not args:
        for i, name in DYNAMIC_EFFECTS:
            print(f"  {i}: {name}")
        sys.exit(0)
    idx = int(args[0])
    apply_dynamic_effect(idx)
    print(f"Effect → {DYNAMIC_EFFECTS[idx][1] if idx < len(DYNAMIC_EFFECTS) else idx}")


def cmd_sound(args):
    if not args:
        for i, name in SOUND_EFFECTS:
            print(f"  {i}: {name}")
        sys.exit(0)
    idx = int(args[0])
    apply_sound_effect(idx)
    print(f"Sound → {SOUND_EFFECTS[idx][1] if idx < len(SOUND_EFFECTS) else idx}")


def cmd_brightness(args):
    if not args:
        print("Usage: sl brightness <5-255>")
        sys.exit(1)
    val = int(args[0])
    oneshot(set_brightness(val), settle_ms=150)
    print(f"Brightness → {val}")


def cmd_speed(args):
    if not args:
        print("Usage: sl speed <5-100>")
        sys.exit(1)
    val = int(args[0])
    oneshot(set_dynamic_speed(val), settle_ms=150)
    print(f"Speed → {val}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0].lower()
    if cmd == "on":
        cmd_on()
    elif cmd == "off":
        cmd_off()
    elif cmd == "color":
        cmd_color(args[1:])
    elif cmd in ("ambi", "ambilight", "sync"):
        cmd_ambi()
    elif cmd == "effect":
        cmd_effect(args[1:])
    elif cmd == "sound":
        cmd_sound(args[1:])
    elif cmd in ("brightness", "bri"):
        cmd_brightness(args[1:])
    elif cmd == "speed":
        cmd_speed(args[1:])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
