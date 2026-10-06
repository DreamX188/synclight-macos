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
