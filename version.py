#!/usr/bin/env python3
"""SyncLight release identity — bump when BT protocol or GUI behaviour changes."""

from __future__ import annotations

APP_NAME = "SyncLight"

# App UI / overall release
VERSION = "1.5.0"

# Bluetooth room-strip stack (worker + LEDnetWF 0x31/0x3B/0x38 effects)
BT_VERSION = "3"

# Calendar build stamp
BUILD = "20261118a"


def short_version() -> str:
    return VERSION


def bt_label() -> str:
    return f"BT{BT_VERSION}"


def display_version() -> str:
    return f"{VERSION} · {bt_label()}"


def window_title() -> str:
    return f"{APP_NAME} {display_version()}"


def as_dict() -> dict:
    return {
        "app": APP_NAME,
        "version": VERSION,
        "bt_version": BT_VERSION,
        "build": BUILD,
        "display": display_version(),
    }
