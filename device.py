#!/usr/bin/env python3
"""
Shared USB HID I/O for Robobloq SyncLight (VID 0x1A86 / PID 0xFE07).

Keeps an exclusive open handle while ambilight is running, and supports
one-shot writes for CLI / menu presets.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Optional

import hid

VENDOR_ID = 0x1A86
PRODUCT_ID = 0xFE07
INTERFACE = 0
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.robobloq.synclight.plist")
MAX_CHUNK = 64
INTER_CHUNK_GAP_S = 0.0005  # 500 µs — device MCU needs a pause between reports


def driver_loaded() -> bool:
    try:
        out = subprocess.check_output(["launchctl", "list"], text=True)
        return "com.robobloq.synclight" in out
    except subprocess.CalledProcessError:
        return False


def unload_driver() -> bool:
    """Unload sleep-sync LaunchAgent. Returns True if it was loaded."""
    if not (driver_loaded() and os.path.exists(PLIST)):
        return False
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
    time.sleep(0.35)
    return True


def load_driver() -> None:
    if os.path.exists(PLIST):
        subprocess.run(["launchctl", "load", "-w", PLIST], capture_output=True)


def _find_path() -> bytes:
    devices = hid.enumerate(VENDOR_ID, PRODUCT_ID)
    target = next(
        (d for d in devices if d.get("interface_number", 0) == INTERFACE),
        devices[0] if devices else None,
    )
    if target is None:
        raise RuntimeError("SyncLight not found. Is it plugged in?")
    return target["path"]


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


class SessionId:
    def __init__(self) -> None:
        self._id = 0

    def next(self) -> int:
        self._id = (self._id % 254) + 1
        return self._id


_SID = SessionId()


def build_rb(action: int, payload: bytes = b"") -> bytes:
    mid = _SID.next()
    total = 6 + len(payload)
    buf = bytearray(total)
    buf[0:2] = b"RB"
    buf[2] = total
    buf[3] = mid
    buf[4] = action
    buf[5 : 5 + len(payload)] = payload
    buf[-1] = _checksum(buf[:-1])
    return bytes(buf)


def build_sc_sync(colors: bytes) -> bytes:
    """SC setSyncScreen (0x80). colors = packed [idx,R,G,B,idx] * N."""
    mid = _SID.next()
    total = 7 + len(colors)
    buf = bytearray(total)
    buf[0] = 0x53  # S
    buf[1] = 0x43  # C
    buf[2] = (total >> 8) & 0xFF
    buf[3] = total & 0xFF
    buf[4] = mid
    buf[5] = 0x80
    buf[6 : 6 + len(colors)] = colors
    buf[-1] = _checksum(buf[:-1])
    return bytes(buf)


def encode_led_colors(leds: list[tuple[int, int, int]]) -> bytes:
    out = bytearray(len(leds) * 5)
    for i, (r, g, b) in enumerate(leds):
        n = (i + 1) & 0xFF
        o = i * 5
        out[o] = n
        out[o + 1] = r & 0xFF
        out[o + 2] = g & 0xFF
        out[o + 3] = b & 0xFF
        out[o + 4] = n
    return bytes(out)


def set_section_led(r: int, g: int, b: int, lamps: int = 65) -> bytes:
    """0x86 — official payload uses 1-based LED range ending at 254 (=all)."""
    r, g, b = r & 0xFF, g & 0xFF, b & 0xFF
    if 0 < lamps < 254:
        la = lamps & 0xFF
        payload = bytes([1, r, g, b, la, (la + 1) & 0xFF, 0, 0, 0, 254])
    else:
        payload = bytes([1, r, g, b, 254])
    return build_rb(0x86, payload)


def set_led_effect(effect_type: int, effect_index: int) -> bytes:
    """0x85 — effect_type: 2=dynamic, 3=sound (device mic). Index 0..6."""
    return build_rb(0x85, bytes([effect_type & 0xFF, effect_index & 0xFF]))


def set_brightness(value: int) -> bytes:
    """0x87 — brightness 5..255."""
    value = max(5, min(255, int(value)))
    return build_rb(0x87, bytes([value]))


def set_dynamic_speed(speed: int) -> bytes:
    """0x8A — firmware: low=fast, high=slow. UI value 5..100 (100=fastest)."""
    ui = max(5, min(100, int(speed)))
    # Official SyncLight sends (100 - slider)
    device = max(5, min(100, 100 - ui))
    return build_rb(0x8A, bytes([device]))


def set_sound_sensitivity(value: int) -> bytes:
    """0x8B — mic sensitivity 5..100."""
    value = max(5, min(100, int(value)))
    return build_rb(0x8B, bytes([value]))


# Firmware exposes exactly 7 effects per type (indices 0..6)
DYNAMIC_EFFECTS = [
    (0, "Rainbow Flow"),
    (1, "Breathing"),
    (2, "Color Chase"),
    (3, "Meteor"),
    (4, "Sparkle"),
    (5, "Gradient"),
    (6, "Marquee"),
]

SOUND_EFFECTS = [
    (0, "Rhythm Wave"),
    (1, "Rhythm Pulse"),
    (2, "Rhythm Spectrum"),
    (3, "Rhythm Flash"),
    (4, "Rhythm Gradient"),
    (5, "Rhythm Chase"),
    (6, "Rhythm Rainbow"),
]

class Device:
    """Thread-safe exclusive HID connection."""

    def __init__(self) -> None:
        self._dev: Optional[hid.Device] = None
        self._lock = threading.Lock()
        self._held_driver = False

    @property
    def open(self) -> bool:
        return self._dev is not None

    def connect(self, *, release_driver: bool = True) -> None:
        with self._lock:
            if self._dev is not None:
                return
            if release_driver:
                self._held_driver = unload_driver()
            path = _find_path()
            self._dev = hid.Device(path=path)

    def disconnect(self, *, restore_driver: bool = True) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except Exception:
                    pass
                self._dev = None
            restore = restore_driver and self._held_driver
            self._held_driver = False
        if restore:
            load_driver()

    def write_packet(self, packet: bytes) -> None:
        with self._lock:
            if self._dev is None:
                raise RuntimeError("Device not connected")
            offset = 0
            first = True
            while offset < len(packet):
                if not first:
                    time.sleep(INTER_CHUNK_GAP_S)
                first = False
                chunk = packet[offset : offset + MAX_CHUNK]
                self._dev.write(bytes([0x00]) + chunk)
                offset += MAX_CHUNK

    def send_sync_frame(self, leds: list[tuple[int, int, int]]) -> None:
        self.write_packet(build_sc_sync(encode_led_colors(leds)))

    def send_solid(self, r: int, g: int, b: int) -> None:
        self.write_packet(set_section_led(r, g, b))


# Shared singleton used by GUI ambilight mode
shared = Device()


def _write_chunks(dev: hid.Device, packet: bytes) -> None:
    offset = 0
    first = True
    while offset < len(packet):
        if not first:
            time.sleep(INTER_CHUNK_GAP_S)
        first = False
        chunk = packet[offset : offset + MAX_CHUNK]
        dev.write(bytes([0x00]) + chunk)
        offset += MAX_CHUNK


def oneshot(packet: bytes, *, settle_ms: int = 0) -> None:
    """Open briefly, write, close — used by CLI / preset buttons."""
    # Don't steal the device from an active ambilight/exclusive session
    if shared.open:
        shared.write_packet(packet)
        if settle_ms:
            time.sleep(settle_ms / 1000.0)
        return

    was_loaded = unload_driver() if driver_loaded() else False
    try:
        path = _find_path()
        dev = hid.Device(path=path)
        try:
            _write_chunks(dev, packet)
            if settle_ms:
                time.sleep(settle_ms / 1000.0)
        finally:
            dev.close()
    finally:
        if was_loaded:
            load_driver()


def oneshot_many(packets: list[bytes], *, settle_ms: int = 200) -> None:
    """Send several control packets in one open session."""
    if shared.open:
        for p in packets:
            shared.write_packet(p)
            time.sleep(settle_ms / 1000.0)
        return

    was_loaded = unload_driver() if driver_loaded() else False
    try:
        path = _find_path()
        dev = hid.Device(path=path)
        try:
            for p in packets:
                _write_chunks(dev, p)
                time.sleep(settle_ms / 1000.0)
        finally:
            dev.close()
    finally:
        if was_loaded:
            load_driver()


def apply_dynamic_effect(
    index: int, speed: int = 50, brightness: int = 200, lamps: int = 65
) -> None:
    """Match SyncLight/SyncRGB: brightness → clear → effect(2,i) → speed."""
    index = max(0, min(6, int(index)))
    oneshot_many(
        [
            set_brightness(brightness),
            set_section_led(0, 0, 0, lamps=lamps),
            set_led_effect(2, index),
            set_dynamic_speed(speed),
        ],
        settle_ms=80,
    )


def apply_sound_effect(
    index: int,
    speed: int = 50,
    brightness: int = 200,
    sensitivity: int = 70,
    lamps: int = 65,
) -> None:
    """Match SyncLight: brightness → clear → effect(3,i) → speed/sensitivity."""
    index = max(0, min(6, int(index)))
    oneshot_many(
        [
            set_brightness(brightness),
            set_section_led(0, 0, 0, lamps=lamps),
            set_led_effect(3, index),
            set_dynamic_speed(speed),
            set_sound_sensitivity(sensitivity),
        ],
        settle_ms=80,
    )
