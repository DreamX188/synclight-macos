#!/usr/bin/env python3
"""
Yandex Smart Bulb (YNDX-00010 / Tuya) — local Wi‑Fi control via tinytuya.

The bulb on the LAN is a rebranded Tuya device (port 6668). SyncLight talks
to it directly — no Alice cloud required once you paste the Local Key.

Get Local Key once:
  1) Pair bulb in Smart Life / Tuya Smart (or keep Yandex link)
  2) Cloud project at https://iot.tuya.com → Access ID + Secret (SyncLight stores them)
  3) Devices → Link App Account → scan QR with Smart Life
  4) SyncLight → «Ключ из облака» or: python3 -m tinytuya wizard
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("synclight.yandex")


def fetch_local_key_cloud(
    *,
    access_id: str,
    access_secret: str,
    device_id: str = "",
    region: str = "eu",
) -> Dict[str, Any]:
    """
    Pull local_key via Tuya Cloud (tinytuya) after Smart Life account is linked.
    Returns {ok, local_key, devices, error}.
    """
    access_id = (access_id or "").strip()
    access_secret = (access_secret or "").strip()
    device_id = (device_id or "").strip()
    region = (region or "eu").strip().lower() or "eu"
    if not access_id or not access_secret:
        return {"ok": False, "error": "Нет Access ID / Access Secret", "devices": []}
    try:
        import tinytuya
    except ImportError as exc:
        return {"ok": False, "error": f"tinytuya: {exc}", "devices": []}
    try:
        cloud = tinytuya.Cloud(
            apiKey=access_id, apiSecret=access_secret, apiRegion=region
        )
        devices = cloud.getdevices() or []
    except Exception as exc:
        return {"ok": False, "error": str(exc), "devices": []}
    if not isinstance(devices, list):
        return {
            "ok": False,
            "error": str(devices) if devices else "Пустой ответ облака",
            "devices": [],
        }
    simplified = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "")
        key = str(d.get("key") or d.get("local_key") or "")
        simplified.append(
            {
                "id": did,
                "name": str(d.get("name") or ""),
                "key": key,
                "ip": str(d.get("ip") or ""),
            }
        )
    match = None
    if device_id:
        for d in simplified:
            if d["id"] == device_id and d["key"]:
                match = d
                break
    if match is None:
        for d in simplified:
            if d["key"]:
                match = d
                break
    if not match:
        return {
            "ok": False,
            "error": (
                "В облаке нет устройств. Devices → Link App Account → "
                "QR в Smart Life (тот же дата-центр)."
            ),
            "devices": simplified,
        }
    return {
        "ok": True,
        "local_key": match["key"],
        "device_id": match["id"],
        "name": match.get("name") or "",
        "devices": simplified,
        "error": "",
    }

# YNDX-00010 / Yeelight lemon 2 style DPs (LocalTuya)
DP_POWER = "20"
DP_MODE = "21"
DP_BRIGHT = "22"  # 10..1000
DP_TEMP = "23"
DP_COLOR = "24"


def scan_lan(timeout: float = 6.0) -> List[Dict[str, Any]]:
    """Broadcast-discover Tuya bulbs on Wi‑Fi. Returns [{ip, id, version, productKey, mac}]."""
    try:
        import tinytuya
    except ImportError as exc:
        raise RuntimeError("Установи tinytuya: pip3 install tinytuya") from exc

    found: Dict[str, Dict[str, Any]] = {}
    try:
        raw = tinytuya.deviceScan(verbose=False, maxretry=max(2, int(timeout / 2)))
    except Exception as exc:
        log.warning("tuya scan: %s", exc)
        raw = {}
    if not isinstance(raw, dict):
        return []
    for ip, info in raw.items():
        if not isinstance(info, dict):
            continue
        did = str(info.get("gwId") or info.get("id") or "")
        if not did:
            continue
        found[did] = {
            "ip": str(info.get("ip") or ip),
            "id": did,
            "version": str(info.get("version") or "3.3"),
            "productKey": str(info.get("productKey") or ""),
            "mac": str(info.get("mac") or ""),
            "name": str(info.get("name") or "Яндекс-лампа"),
        }
    return sorted(found.values(), key=lambda d: d["ip"])


class YandexBulb:
    """Thread-safe local controller for one Tuya/Yandex bulb."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.ip = ""
        self.device_id = ""
        self.local_key = ""
        self.version = "3.3"
        self.name = "Яндекс-лампа"
        self.enabled = True
        self.last_error = ""
        self._dev = None
        self._last_rgb: Tuple[int, int, int] = (-1, -1, -1)
        self._last_ts = 0.0

    def configure(
        self,
        *,
        ip: str = "",
        device_id: str = "",
        local_key: str = "",
        version: str = "3.3",
        name: str = "",
        enabled: bool = True,
    ) -> None:
        with self._lock:
            changed = (
                ip != self.ip
                or device_id != self.device_id
                or local_key != self.local_key
                or str(version) != self.version
            )
            self.ip = (ip or "").strip()
            self.device_id = (device_id or "").strip()
            self.local_key = (local_key or "").strip()
            self.version = str(version or "3.3").strip() or "3.3"
            if name:
                self.name = name.strip() or self.name
            self.enabled = bool(enabled)
            if changed:
                self._close_unlocked()

    def load_from_settings(self, cfg: dict) -> None:
        raw = cfg.get("yandex_bulb") or {}
        if not isinstance(raw, dict):
            raw = {}
        self.configure(
            ip=str(raw.get("ip") or ""),
            device_id=str(raw.get("device_id") or ""),
            local_key=str(raw.get("local_key") or ""),
            version=str(raw.get("version") or "3.3"),
            name=str(raw.get("name") or "Яндекс-лампа"),
            enabled=bool(raw.get("enabled", True)),
        )

    def dump(self) -> dict:
        return {
            "ip": self.ip,
            "device_id": self.device_id,
            "local_key": self.local_key,
            "version": self.version,
            "name": self.name,
            "enabled": self.enabled,
        }

    def configured(self) -> bool:
        return bool(self.enabled and self.ip and self.device_id and self.local_key)

    def status(self) -> dict:
        st = {
            "bound": bool(self.ip and self.device_id),
            "configured": self.configured(),
            "connected": False,
            "enabled": self.enabled,
            "name": self.name,
            "ip": self.ip,
            "device_id": self.device_id,
            "has_key": bool(self.local_key),
            "version": self.version,
            "last_error": self.last_error,
            "power": None,
            "color": None,
        }
        if not self.configured():
            if self.ip and self.device_id and not self.local_key:
                st["last_error"] = (
                    "Нужен Local Key (Tuya). Скан нашёл лампу — вставь ключ ниже."
                )
            return st
        try:
            d = self._device()
            raw = d.status()
            if not isinstance(raw, dict) or raw.get("Error"):
                self.last_error = str(
                    (raw or {}).get("Error") or (raw or {}).get("Err") or raw
                )
                st["last_error"] = self.last_error
                self._close_unlocked()
                return st
            dps = raw.get("dps") or {}
            st["connected"] = True
            st["power"] = bool(dps.get(DP_POWER) or dps.get(20))
            st["raw_dps"] = {str(k): v for k, v in dps.items()}
            self.last_error = ""
            st["last_error"] = ""
        except Exception as exc:
            self.last_error = str(exc)
            st["last_error"] = self.last_error
            with self._lock:
                self._close_unlocked()
        return st

    def _close_unlocked(self) -> None:
        self._dev = None

    def _device(self):
        with self._lock:
            if self._dev is not None:
                return self._dev
            if not self.configured():
                raise RuntimeError("Яндекс-лампа не настроена (ip / id / local_key)")
            import tinytuya

            dev = tinytuya.BulbDevice(self.device_id, self.ip, self.local_key)
            try:
                ver = float(self.version)
            except Exception:
                ver = 3.3
            dev.set_version(ver)
            dev.set_socketTimeout(4)
            try:
                dev.set_socketPersistent(True)
            except Exception:
                pass
            self._dev = dev
            return dev

    def turn(self, on: bool) -> None:
        d = self._device()
        if on:
            d.turn_on()
        else:
            d.turn_off()

    def set_color(self, r: int, g: int, b: int, brightness: Optional[int] = None) -> None:
        r = max(0, min(255, int(r)))
        g = max(0, min(255, int(g)))
        b = max(0, min(255, int(b)))
        d = self._device()
        # Ensure colour mode + power
        try:
            d.turn_on()
        except Exception:
            pass
        try:
            d.set_mode("colour")
        except Exception:
            try:
                d.set_value(DP_MODE, "colour")
            except Exception:
                pass
        d.set_colour(r, g, b)
        if brightness is not None:
            self.set_brightness(brightness)
        self._last_rgb = (r, g, b)

    def set_brightness(self, level: int) -> None:
        """level: 0..255 SyncLight scale → Tuya 10..1000."""
        level = max(0, min(255, int(level)))
        tuya = max(10, min(1000, int(round(level * 1000 / 255)))) if level else 10
        d = self._device()
        try:
            d.set_brightness_percentage(max(1, int(round(level * 100 / 255))))
        except Exception:
            d.set_value(DP_BRIGHT, tuya)

    def set_ambient(
        self, r: int, g: int, b: int, min_interval: float = 0.35
    ) -> None:
        """Throttled colour push for ambilight mirror."""
        if not self.configured():
            return
        now = time.monotonic()
        rgb = (max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b))))
        if now - self._last_ts < min_interval:
            return
        if self._last_rgb[0] >= 0:
            dr = abs(rgb[0] - self._last_rgb[0])
            dg = abs(rgb[1] - self._last_rgb[1])
            db = abs(rgb[2] - self._last_rgb[2])
            if dr + dg + db < 18:
                return
        self._last_ts = now
        try:
            self.set_color(*rgb)
            self._last_rgb = rgb
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            with self._lock:
                self._close_unlocked()


_bulb: Optional[YandexBulb] = None
_bulb_lock = threading.Lock()


def get_yandex_bulb() -> YandexBulb:
    global _bulb
    with _bulb_lock:
        if _bulb is None:
            _bulb = YandexBulb()
            try:
                from settings import load_settings

                _bulb.load_from_settings(load_settings())
            except Exception:
                pass
        return _bulb
