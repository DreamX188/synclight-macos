#!/usr/bin/env python3
"""Persisted SyncLight UI settings."""

from __future__ import annotations

import json
import os

SETTINGS_FILE = os.path.expanduser("~/.synclight_settings.json")

DEFAULTS = {
    "brightness": 200,
    "effect_speed": 50,
    "autostart_gui": False,
    "color": [255, 200, 100],
    "last_mode": "static",  # static | ambi | dynamic | sound
    "last_effect_index": 0,
    # Control target: usb | ceiling | floor | all | bt
    "target": "all",
    # Mirror ambilight: top edge → ceiling, bottom L/R → floor
    "mirror_room": True,
    # mss monitor index (1 = first physical display; 0 is virtual all-screens)
    "ambi_monitor": 1,
    "ai_scene": "",
    "bt_ceiling": {"address": "", "name": "", "protocol": "lednetwf"},
    "bt_floor": {"address": "", "name": "", "protocol": "lednetwf"},
    # Friendly labels by BLE address (shared across machines via settings export)
    "bt_labels": {},
    # Wire colour order for LEDnetWF chips if 0x31 path is used: RGB|GRB|BRG|...
    "bt_color_order": "GRB",
    # Yandex / Tuya Wi‑Fi bulb (local)
    "yandex_bulb": {
        "enabled": True,
        "name": "Яндекс-лампа",
        "ip": "",
        "device_id": "",
        "local_key": "",
        "version": "3.3",
    },
    # Tuya Cloud project (Access ID/Secret → local_key)
    "tuya_cloud": {
        "access_id": "",
        "access_secret": "",
        "project_code": "",
        "region": "eu",
    },
}


def load_settings() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            data.update(stored)
    except Exception:
        pass
    return data


def save_settings(data: dict) -> None:
    merged = dict(DEFAULTS)
    merged.update(data)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(merged, f, indent=2)
