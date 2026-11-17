#!/usr/bin/env python3
"""Route on/off/color/brightness to USB SyncLight, BT strips, Yandex bulb."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from bt_client import get_bt_client
from device import oneshot, set_brightness, set_section_led
from yandex_bulb import get_yandex_bulb


def normalize_target(target: Optional[str]) -> str:
    t = (target or "all").lower().strip()
    aliases = {
        "monitor": "usb",
        "both": "bt",
        "all_bt": "bt",
        "room": "bt",
        "потолок": "ceiling",
        "пол": "floor",
        "монитор": "usb",
        "комната": "bt",
        "все": "all",
        "lamp": "bulb",
        "yandex": "bulb",
        "лампа": "bulb",
        "яндекс": "bulb",
    }
    return aliases.get(t, t)


def includes_usb(target: str) -> bool:
    return normalize_target(target) in ("usb", "all")


def includes_bulb(target: str) -> bool:
    return normalize_target(target) in ("all", "bulb", "bt")


def includes_bt_roles(target: str) -> Sequence[str]:
    t = normalize_target(target)
    if t in ("all", "bt"):
        return ("ceiling", "floor")
    if t in ("ceiling", "floor"):
        return (t,)
    return ()


def _bt_key(roles: Sequence[str]) -> str:
    return "bt" if len(roles) > 1 else roles[0]


def apply_power(
    target: str,
    on: bool,
    color: Tuple[int, int, int] = (255, 200, 100),
    brightness: int = 200,
) -> str:
    notes = []
    t = normalize_target(target)
    if includes_usb(t):
        try:
            if on:
                oneshot(set_section_led(*color))
                oneshot(set_brightness(brightness), settle_ms=80)
            else:
                oneshot(set_section_led(0, 0, 0))
            notes.append("монитор")
        except Exception as exc:
            notes.append(f"монитор: {exc}")
    roles = includes_bt_roles(t)
    if roles:
        try:
            bt = get_bt_client()
            # One shot: color+power together so «Все» updates room in sync
            key = _bt_key(roles)
            if on:
                bt.apply(key, power=True, color=color, brightness=brightness)
            else:
                bt.apply(key, power=False)
            notes.append("комната")
        except Exception as exc:
            notes.append(f"BT: {exc}")
    if includes_bulb(t):
        try:
            bulb = get_yandex_bulb()
            if bulb.configured():
                if on:
                    bulb.set_color(*color, brightness=brightness)
                else:
                    bulb.turn(False)
                notes.append("лампа")
        except Exception as exc:
            notes.append(f"лампа: {exc}")
    return " · ".join(notes) or "нет цели"


def apply_color(
    target: str, r: int, g: int, b: int, brightness: Optional[int] = None
) -> str:
    notes = []
    t = normalize_target(target)
    if includes_usb(t):
        try:
            oneshot(set_section_led(r, g, b))
            if brightness is not None:
                oneshot(set_brightness(brightness), settle_ms=80)
            notes.append("монитор")
        except Exception as exc:
            notes.append(f"монитор: {exc}")
    roles = includes_bt_roles(t)
    if roles:
        try:
            bt = get_bt_client()
            key = _bt_key(roles)
            kwargs = {"color": (r, g, b), "power": True}
            if brightness is not None:
                kwargs["brightness"] = brightness
            bt.apply(key, **kwargs)
            notes.append("комната")
        except Exception as exc:
            notes.append(f"BT: {exc}")
    if includes_bulb(t):
        try:
            bulb = get_yandex_bulb()
            if bulb.configured():
                bulb.set_color(r, g, b, brightness=brightness)
                notes.append("лампа")
        except Exception as exc:
            notes.append(f"лампа: {exc}")
    return " · ".join(notes) or "нет цели"


def apply_brightness(target: str, value: int) -> str:
    notes = []
    t = normalize_target(target)
    if includes_usb(t):
        try:
            oneshot(set_brightness(value), settle_ms=80)
            notes.append("монитор")
        except Exception as exc:
            notes.append(f"монитор: {exc}")
    roles = includes_bt_roles(t)
    if roles:
        try:
            get_bt_client().apply(_bt_key(roles), brightness=value)
            notes.append("комната")
        except Exception as exc:
            notes.append(f"BT: {exc}")
    if includes_bulb(t):
        try:
            bulb = get_yandex_bulb()
            if bulb.configured():
                bulb.set_brightness(value)
                notes.append("лампа")
        except Exception as exc:
            notes.append(f"лампа: {exc}")
    return " · ".join(notes) or "нет цели"
