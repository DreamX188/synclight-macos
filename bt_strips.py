#!/usr/bin/env python3
"""
Bluetooth LED strip control for apartment ceiling / floor lights.

Supports controllers commonly sold on Ozon / Wildberries:
  - Zengge LEDnetWF / Magic Hue  (this apartment)
  - ELK-BLEDOM / duoCo / Lotus Lantern
  - HappyLighting / Triones
"""

from __future__ import annotations

import asyncio
import colorsys
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("synclight.bt")

WRITE_UUID_CANDIDATES = (
    "0000ff01-0000-1000-8000-00805f9b34fb",
    "0000fff3-0000-1000-8000-00805f9b34fb",
    "0000fff1-0000-1000-8000-00805f9b34fb",
)
NOTIFY_UUID_CANDIDATES = (
    "0000ff02-0000-1000-8000-00805f9b34fb",
    "0000fff4-0000-1000-8000-00805f9b34fb",
)

LEDNET_PREFIXES = ("LEDNETWF", "LEDNET", "MAGIC HUE", "ZENGGE", "IOTBT", "YBCRG")
ELK_PREFIXES = (
    "ELK-BLEDOM", "ELK-BLE", "ELK~", "ELK-", "LED LIGHT STRIP",
    "LED_LIGHT", "XSL-", "MELON", "BLEDOM",
)
HAPPY_PREFIXES = ("LEDBLE", "TRIONES", "MAGIC BLUE", "QHM-", "MINGER")
ROLES = ("ceiling", "floor")


@dataclass
class BtDeviceInfo:
    address: str
    name: str
    rssi: int = -100
    protocol: str = "lednetwf"


@dataclass
class RoleBinding:
    address: str = ""
    name: str = ""
    protocol: str = "lednetwf"


def guess_protocol(name: str) -> str:
    upper = (name or "").upper()
    for p in LEDNET_PREFIXES:
        if p in upper:
            return "lednetwf"
    for p in HAPPY_PREFIXES:
        if upper.startswith(p):
            return "happy"
    for p in ELK_PREFIXES:
        if upper.startswith(p):
            return "elk"
    return "lednetwf" if "LED" in upper else "elk"


def looks_like_led_strip(name: Optional[str], service_uuids: Sequence[str] = ()) -> bool:
    su = {u.lower() for u in service_uuids}
    if any(u.endswith("ffff") or u.endswith("fff0") or "ff01" in u for u in su):
        return True
    if not name:
        return False
    upper = name.upper()
    for p in LEDNET_PREFIXES + ELK_PREFIXES + HAPPY_PREFIXES:
        if p in upper or upper.startswith(p):
            return True
    return any(k in upper for k in ("LED", "STRIP", "LIGHT", "RGB", "LAMP", "BLEDOM"))


class _LednetSeq:
    def __init__(self):
        self.n = 0

    def next(self) -> int:
        self.n = (self.n + 1) & 0xFF
        return self.n


def _sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def lednet_wrap(raw: bytes, seq: int, family: int = 0x0B) -> bytes:
    plen = len(raw)
    pkt = bytearray(8 + plen)
    pkt[0] = 0x00
    pkt[1] = seq & 0xFF
    pkt[2] = 0x80
    pkt[3] = 0x00
    pkt[4] = (plen >> 8) & 0xFF
    pkt[5] = plen & 0xFF
    pkt[6] = (plen + 1) & 0xFF
    pkt[7] = family
    pkt[8:] = raw
    return bytes(pkt)


def _pack_hs(h: int, s: int) -> Tuple[int, int]:
    packed = (max(0, min(360, h)) << 7) | max(0, min(100, s))
    return (packed >> 8) & 0xFF, packed & 0xFF


def lednet_power(on: bool, seq: int) -> bytes:
    mode = 0x23 if on else 0x24
    raw = bytearray([0x3B, mode, 0, 0, 0, 0, 0, 0, 0, 0x32, 0, 0])
    raw.append(_sum8(raw))
    return lednet_wrap(bytes(raw), seq)


COLOR_ORDERS = ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR")


def remap_rgb(order: str, r: int, g: int, b: int) -> Tuple[int, int, int]:
    """Map logical RGB → wire byte order for addressable chips."""
    o = (order or "RGB").upper()
    table = {
        "RGB": (r, g, b),
        "RBG": (r, b, g),
        "GRB": (g, r, b),
        "GBR": (g, b, r),
        "BRG": (b, r, g),
        "BGR": (b, g, r),
    }
    return table.get(o, (r, g, b))


def _configured_color_order() -> str:
    try:
        from settings import load_settings

        return str(load_settings().get("bt_color_order") or "GRB").upper()
    except Exception:
        return "GRB"


def lednet_color_31(r: int, g: int, b: int, seq: int) -> bytes:
    """
    Solid RGB. Mode 0xF0 = RGB only (WW/CW ignored).
    Persist 0x0F = don't save to flash (needed for fast ambilight streaming).
    """
    r, g, b = lednet_wire_rgb(r, g, b)
    raw = bytearray(
        [
            0x31,
            r & 0xFF,
            g & 0xFF,
            b & 0xFF,
            0x00,
            0x00,
            0xF0,
            0x0F,
        ]
    )
    raw.append(_sum8(raw))
    return lednet_wrap(bytes(raw), seq)


def lednet_wire_rgb(r: int, g: int, b: int) -> Tuple[int, int, int]:
    """Apply configured chip order. Default GRB — fixes swapped red/green on WS2812."""
    return remap_rgb(_configured_color_order(), r, g, b)


def lednet_color_3b(r: int, g: int, b: int, bri_pct: int, seq: int) -> bytes:
    """
    Symphony / BLE v5 solid color via HSV.

    IMPORTANT: bytes 7–9 are a 24-bit delay in ms on many LEDnetWF firmwares —
    putting R,G,B there makes blue lag ~255ms, green ~65s, red hours.
    Use delay=0; hue/sat carry the colour.

    RGB is remapped for chip order first (GRB default): firmware converts
    HSV→RGB→wire without its own remap, so R/G would otherwise swap.
    """
    r, g, b = lednet_wire_rgb(r, g, b)
    hh, ss, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    h, s = int(hh * 360), int(ss * 100)
    hs_hi, hs_lo = _pack_hs(h, s)
    bri = max(1, min(100, bri_pct))
    raw = bytearray(
        [
            0x3B,
            0xA1,
            hs_hi,
            hs_lo,
            bri,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,  # delay ms = 0
            0x00,
            0x00,
        ]
    )
    raw.append(_sum8(raw))
    return lednet_wrap(bytes(raw), seq)


def lednet_brightness(level: int, seq: int) -> bytes:
    pct = max(1, min(100, int(round(level * 100 / 255)) if level > 100 else level))
    raw = bytearray([0x3B, 0x01, 0x00, 0x00, pct, 0x00, pct, 0, 0, 0, 0, 0])
    raw.append(_sum8(raw))
    return lednet_wrap(bytes(raw), seq)


# UI dynamic indices 0..6 → LEDnetWF built-in effect IDs (0x38)
LEDNET_EFFECT_IDS = (1, 5, 7, 10, 15, 20, 25)


def lednet_effect(effect_id: int, speed: int, bri_pct: int, seq: int) -> bytes:
    """LEDnetWF / Magic Hue dynamic mode: 0x38 eid speed bri + checksum."""
    eid = max(1, min(255, int(effect_id)))
    sp = max(1, min(100, int(speed)))
    br = max(1, min(100, int(bri_pct)))
    raw = bytearray([0x38, eid, sp, br])
    raw.append(_sum8(raw))
    return lednet_wrap(bytes(raw), seq)


def _lednet_effect_id(ui_index: int) -> int:
    idx = int(ui_index)
    if 0 <= idx < len(LEDNET_EFFECT_IDS):
        return LEDNET_EFFECT_IDS[idx]
    return max(1, min(255, idx))


def _bri_to_pct(level: Optional[int], fallback: int = 100) -> int:
    if level is None:
        return fallback
    if level > 100:
        return max(1, min(100, int(round(level * 100 / 255))))
    return max(1, min(100, int(level)))


def _friendly_bt_error(exc: BaseException) -> str:
    """Map bleak/CoreBluetooth noise to Russian."""
    try:
        from bt_auth import ble_error_hint

        return ble_error_hint(exc)
    except Exception:
        pass
    msg = str(exc)
    low = msg.lower()
    if any(x in low for x in ("authoriz", "unauthorized", "not permitted", "permission")):
        return (
            "macOS блокирует Bluetooth. "
            "Настройки → Bluetooth → включи SyncLight и Python, затем «Подключить»."
        )
    if any(x in low for x in ("powered off", "turned off", "not powered")):
        return "Bluetooth выключен или занят. Включи в строке меню и подключи комнату."
    if "timeout" in low or "timed out" in low:
        return "Таймаут BT. Подойди ближе к лентам и нажми «Подключить»."
    return msg


def elk_frame(*payload: int) -> bytes:
    data = list(payload)
    if len(data) != 7:
        raise ValueError("ELK payload must be 7 bytes")
    return bytes([0x7E, *data, 0xEF])


def elk_power(on: bool) -> bytes:
    return elk_frame(0x04, 0x04, 0x01 if on else 0x00, 0x00, 0x01 if on else 0x00, 0xFF, 0x00)


def elk_color(r: int, g: int, b: int) -> bytes:
    return elk_frame(0x00, 0x05, 0x03, r & 0xFF, g & 0xFF, b & 0xFF, 0x00)


def elk_brightness(level: int) -> bytes:
    pct = max(5, min(100, int(round(level * 100 / 255)))) if level > 100 else max(1, min(100, level))
    return elk_frame(0x04, 0x01, pct & 0xFF, 0x01, 0xFF, 0xFF, 0x00)


def elk_effect(mode: int) -> bytes:
    return elk_frame(0x05, 0x03, 0x80 | (mode & 0x7F), 0x03, 0xFF, 0xFF, 0x00)


def happy_power(on: bool) -> bytes:
    return bytes([0xCC, 0x23 if on else 0x24, 0x33])


def happy_color(r: int, g: int, b: int) -> bytes:
    return bytes([0x56, r & 0xFF, g & 0xFF, b & 0xFF, 0x00, 0xF0, 0xAA])


class _AsyncRunner:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="bt-bleak", daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, timeout: Optional[float] = 30.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)


@dataclass
class _Conn:
    client: object
    info: BtDeviceInfo
    write_uuid: str
    seq: _LednetSeq = field(default_factory=_LednetSeq)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BtStripManager:
    def __init__(self):
        self._runner = _AsyncRunner()
        self.bindings: Dict[str, RoleBinding] = {
            "ceiling": RoleBinding(),
            "floor": RoleBinding(),
        }
        self._conns: Dict[str, _Conn] = {}
        self.last_scan: List[BtDeviceInfo] = []
        self.last_error: str = ""
        self._last_ambient_top: Tuple[int, int, int] = (-1, -1, -1)
        self._last_ambient_bot: Tuple[int, int, int] = (-1, -1, -1)
        self._last_ambient_ts = 0.0
        # Mac-driven room ambilight: latest colors + coalescing pump (controllers
        # have no real ambilight — Mac pushes solid color commands).
        self._ambi_pending: Optional[Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = None
        self._ambi_pump_running = False
        self._ambi_lock = threading.Lock()

    def load_bindings(self, cfg: dict) -> None:
        for role in ROLES:
            raw = cfg.get(f"bt_{role}") or {}
            if isinstance(raw, str):
                raw = {"address": raw}
            self.bindings[role] = RoleBinding(
                address=str(raw.get("address") or ""),
                name=str(raw.get("name") or ""),
                protocol=str(raw.get("protocol") or "lednetwf"),
            )

    def dump_bindings(self) -> dict:
        return {
            f"bt_{role}": {
                "address": b.address,
                "name": b.name,
                "protocol": b.protocol,
            }
            for role, b in self.bindings.items()
        }

    def status(self) -> dict:
        labels: Dict[str, str] = {}
        try:
            from settings import load_settings

            raw = load_settings().get("bt_labels") or {}
            if isinstance(raw, dict):
                labels = {str(k): str(v) for k, v in raw.items() if v}
        except Exception:
            pass

        def _role_status(role: str) -> dict:
            bind = self.bindings[role]
            display = labels.get(bind.address) or bind.name
            return {
                "bound": bool(bind.address),
                "connected": role in self._conns
                and bool(getattr(self._conns[role].client, "is_connected", False)),
                "name": display,
                "address": bind.address,
                "label": labels.get(bind.address, ""),
            }

        return {
            "ceiling": _role_status("ceiling"),
            "floor": _role_status("floor"),
            "last_error": self.last_error,
            "labels": labels,
            "scan": [
                {
                    "address": d.address,
                    "name": labels.get(d.address) or d.name,
                    "rssi": d.rssi,
                    "protocol": d.protocol,
                    "label": labels.get(d.address, ""),
                }
                for d in self.last_scan
            ],
        }

    def scan(self, timeout: float = 8.0) -> List[BtDeviceInfo]:
        try:
            self.last_scan = self._runner.submit(self._scan_async(timeout), timeout=timeout + 5)
            self.last_error = ""
        except Exception as exc:
            self.last_error = _friendly_bt_error(exc)
            log.exception("BT scan failed")
            self.last_scan = []
        return self.last_scan

    async def _scan_async(self, timeout: float) -> List[BtDeviceInfo]:
        from bleak import BleakScanner

        found: Dict[str, BtDeviceInfo] = {}
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for addr, (dev, adv) in devices.items():
            name = (dev.name or adv.local_name or "").strip()
            uuids = list(adv.service_uuids or [])
            if not looks_like_led_strip(name, uuids):
                continue
            rssi = adv.rssi if adv.rssi is not None else -100
            prev = found.get(addr)
            if prev is None or rssi > prev.rssi:
                found[addr] = BtDeviceInfo(
                    address=addr,
                    name=name or f"LED {addr[-5:]}",
                    rssi=rssi,
                    protocol=guess_protocol(name),
                )
        return sorted(found.values(), key=lambda d: d.rssi, reverse=True)

    def assign(self, role: str, address: str, name: str = "", protocol: str = "") -> None:
        if role not in ROLES:
            raise ValueError(f"Unknown role {role}")
        # Prefer saved friendly label when assigning from scan / manual add
        label = ""
        try:
            from settings import load_settings

            label = str((load_settings().get("bt_labels") or {}).get(address) or "")
        except Exception:
            pass
        self.bindings[role] = RoleBinding(
            address=address,
            name=label or name,
            protocol=protocol or guess_protocol(name or label),
        )
        try:
            self._runner.submit(self._disconnect_role(role), timeout=8)
        except Exception:
            pass

    def rename(self, address: str, label: str) -> None:
        """Set a human-friendly name for a BLE address (and any bound role)."""
        address = (address or "").strip()
        label = (label or "").strip()
        if not address:
            raise ValueError("Нет адреса устройства")
        for role, bind in self.bindings.items():
            if bind.address == address:
                self.bindings[role] = RoleBinding(
                    address=bind.address,
                    name=label or bind.name,
                    protocol=bind.protocol,
                )

    def clear(self, role: str) -> None:
        if role not in ROLES:
            return
        try:
            self._runner.submit(self._disconnect_role(role), timeout=8)
        except Exception:
            pass
        self.bindings[role] = RoleBinding()

    def connect_bound(self, *, force: bool = False) -> str:
        roles = [r for r in ROLES if self.bindings[r].address]
        if not roles:
            raise RuntimeError("Сначала назначь Потолок и Пол во вкладке Комната")
        labels = {"ceiling": "потолок", "floor": "пол"}

        if force:
            try:
                self.disconnect_all()
            except Exception:
                pass
            time.sleep(0.2)

        st0 = self.status()
        if not force and all(st0.get(r, {}).get("connected") for r in roles):
            return "уже подключено: " + ", ".join(labels.get(r, r) for r in roles)

        # Drop only dead sockets
        for role in roles:
            conn = self._conns.get(role)
            alive = bool(conn and getattr(conn.client, "is_connected", False))
            if conn and not alive:
                try:
                    self._runner.submit(self._disconnect_role(role), timeout=3)
                except Exception:
                    self._conns.pop(role, None)

        ok: List[str] = []
        bad: List[str] = []
        for role in roles:
            if not force and self.status().get(role, {}).get("connected"):
                ok.append(labels.get(role, role))
                continue
            try:
                self.ensure_connected([role])
                if self.status().get(role, {}).get("connected"):
                    ok.append(labels.get(role, role))
                else:
                    # One forced rebuild
                    try:
                        self._runner.submit(self._disconnect_role(role), timeout=3)
                    except Exception:
                        self._conns.pop(role, None)
                    time.sleep(0.3)
                    self.ensure_connected([role])
                    if self.status().get(role, {}).get("connected"):
                        ok.append(labels.get(role, role))
                    else:
                        bad.append(labels.get(role, role))
            except Exception as exc:
                bad.append(f"{labels.get(role, role)} ({_friendly_bt_error(exc)})")
        if not ok:
            raise RuntimeError(
                "Нет связи с лентами: " + ("; ".join(bad) if bad else "неизвестно")
            )
        msg = "подключено: " + ", ".join(ok)
        if bad:
            msg += " · нет связи: " + "; ".join(bad)
        self.last_error = "" if not bad else "; ".join(bad)
        return msg

    def ensure_connected(self, roles: Optional[Sequence[str]] = None) -> None:
        roles = list(roles or ROLES)
        try:
            self._runner.submit(self._ensure_connected(roles), timeout=40)
            self.last_error = ""
        except Exception as exc:
            self.last_error = _friendly_bt_error(exc)
            raise RuntimeError(self.last_error) from exc

    def swap_roles(self) -> None:
        """Swap ceiling ↔ floor bindings (and live connections if any)."""
        a, b = self.bindings["ceiling"], self.bindings["floor"]
        self.bindings["ceiling"], self.bindings["floor"] = b, a
        ca = self._conns.pop("ceiling", None)
        cb = self._conns.pop("floor", None)
        if ca is not None:
            self._conns["floor"] = ca
        if cb is not None:
            self._conns["ceiling"] = cb

    async def _ensure_connected(self, roles: Sequence[str]) -> None:
        from bleak import BleakClient

        for role in roles:
            bind = self.bindings.get(role)
            if not bind or not bind.address:
                continue
            conn = self._conns.get(role)
            if conn and conn.client.is_connected:
                continue
            if conn:
                await self._disconnect_role(role)

            last_err = None
            for attempt in range(4):
                client = None
                try:
                    client = BleakClient(bind.address, timeout=15.0)
                    await client.connect()
                    # bleak ≥0.22: services available after connect; force refresh if empty
                    try:
                        services = client.services
                        if services is None or len(list(services)) == 0:
                            await client.get_services()
                    except Exception:
                        try:
                            await client.get_services()
                        except Exception:
                            pass
                    write_uuid = await self._find_uuid(client, WRITE_UUID_CANDIDATES)
                    if not write_uuid:
                        await client.disconnect()
                        raise RuntimeError(f"{role}: нет writable GATT")
                    notify_uuid = await self._find_uuid(client, NOTIFY_UUID_CANDIDATES)
                    if notify_uuid:
                        try:
                            await client.start_notify(notify_uuid, lambda *_: None)
                        except Exception:
                            pass
                    info = BtDeviceInfo(
                        address=bind.address,
                        name=bind.name or bind.address,
                        protocol=bind.protocol or guess_protocol(bind.name),
                    )
                    self._conns[role] = _Conn(client=client, info=info, write_uuid=write_uuid)
                    # Connected — do not power-on / flash here (that hung UI & lit the room)
                    await asyncio.sleep(0.05)
                    last_err = None
                    break
                except Exception as exc:
                    last_err = exc
                    if client is not None:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                    await asyncio.sleep(0.5 * (attempt + 1))
            if last_err:
                log.warning("connect %s failed: %r", role, last_err)
                try:
                    from bt_client import _bt_log

                    _bt_log(
                        f"RAW {role} FAIL addr={bind.address} "
                        f"name={bind.name!r} err={last_err!r}"
                    )
                except Exception:
                    pass
                raise RuntimeError(f"{role}: {_friendly_bt_error(last_err)}")
            try:
                from bt_client import _bt_log

                _bt_log(f"RAW {role} OK addr={bind.address} name={bind.name!r}")
            except Exception:
                pass

    async def _find_uuid(self, client, candidates: Sequence[str]) -> Optional[str]:
        writable = []
        for svc in client.services:
            for char in svc.characteristics:
                props = set(char.properties or [])
                if candidates is NOTIFY_UUID_CANDIDATES:
                    if "notify" in props:
                        writable.append(char)
                elif "write" in props or "write-without-response" in props:
                    writable.append(char)
        for want in candidates:
            for char in writable:
                if char.uuid.lower() == want.lower() or want.lower() in char.uuid.lower():
                    return char.uuid
        return writable[0].uuid if writable else None

    async def _disconnect_role(self, role: str) -> None:
        conn = self._conns.pop(role, None)
        if not conn:
            return
        try:
            if conn.client.is_connected:
                await conn.client.disconnect()
        except Exception:
            pass

    def disconnect_all(self) -> None:
        try:
            self._runner.submit(self._disconnect_all(), timeout=10)
        except Exception:
            pass

    async def _disconnect_all(self) -> None:
        for role in list(self._conns):
            await self._disconnect_role(role)

    def _resolve_targets(self, target: str) -> List[str]:
        t = (target or "bt").lower()
        if t in ("all_bt", "bt", "both"):
            return [r for r in ROLES if self.bindings[r].address]
        if t in ROLES:
            return [t] if self.bindings[t].address else []
        return []

    def apply(
        self,
        target: str,
        *,
        power: Optional[bool] = None,
        color: Optional[Tuple[int, int, int]] = None,
        brightness: Optional[int] = None,
        effect: Optional[int] = None,
        bri_pct: Optional[int] = None,
    ) -> None:
        roles = self._resolve_targets(target)
        if not roles:
            raise RuntimeError("Нет привязанных BT-лент")
        self.ensure_connected(roles)
        self._runner.submit(
            self._apply_async(
                roles,
                power=power,
                color=color,
                brightness=brightness,
                effect=effect,
                bri_pct=bri_pct,
            ),
            timeout=25,
        )

    def set_ambient(
        self,
        top_rgb: Tuple[int, int, int],
        bottom_rgb: Optional[Tuple[int, int, int]] = None,
        min_interval: float = 0.0,
        force: bool = False,
    ) -> None:
        """
        Queue Mac-side colors for the room (returns immediately).

        Used by ambilight + Mac scene FX. Controllers are solid-color only —
        Mac pushes 0x3B. ceiling←top, floor←bottom.
        """

        def _clamp(c: Tuple[int, int, int]) -> Tuple[int, int, int]:
            return (
                max(0, min(255, int(c[0]))),
                max(0, min(255, int(c[1]))),
                max(0, min(255, int(c[2]))),
            )

        top = _clamp(top_rgb)
        bot = _clamp(bottom_rgb if bottom_rgb is not None else top_rgb)
        with self._ambi_lock:
            if force:
                self._last_ambient_top = (-1, -1, -1)
                self._last_ambient_bot = (-1, -1, -1)
            self._ambi_pending = (top, bot)
            start_pump = not self._ambi_pump_running
            if start_pump:
                self._ambi_pump_running = True
        if start_pump:
            threading.Thread(
                target=self._ambient_pump, name="bt-room-ambi", daemon=True
            ).start()

    def _ambient_pump(self) -> None:
        """Coalesce pending colors and stream to BLE (~30–40 Hz)."""
        try:
            while True:
                with self._ambi_lock:
                    pending = self._ambi_pending
                    self._ambi_pending = None
                if pending is None:
                    with self._ambi_lock:
                        self._ambi_pump_running = False
                    return
                top, bot = pending

                def _delta(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> int:
                    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])

                skip = (
                    self._last_ambient_top[0] >= 0
                    and _delta(top, self._last_ambient_top) < 2
                    and _delta(bot, self._last_ambient_bot) < 2
                )
                if not skip:
                    self._last_ambient_top = top
                    self._last_ambient_bot = bot
                    self._last_ambient_ts = time.monotonic()
                    try:
                        self._runner.submit(
                            self._ambient_stream_async(top, bot), timeout=1.5
                        )
                    except Exception as exc:
                        self.last_error = _friendly_bt_error(exc)
                with self._ambi_lock:
                    more = self._ambi_pending is not None
                if more:
                    continue
                # ~40 Hz — snappier room sync with USB edge strip
                time.sleep(0.022)
        finally:
            with self._ambi_lock:
                self._ambi_pump_running = False

    async def _ambient_write_role(
        self, role: str, rgb: Tuple[int, int, int]
    ) -> None:
        conn = self._conns.get(role)
        if not conn or not getattr(conn.client, "is_connected", False):
            try:
                await self._ensure_connected([role])
            except Exception:
                return
            conn = self._conns.get(role)
        if not conn:
            return
        frames = self._stream_color_frames(conn, rgb)
        async with conn.lock:
            for frame in frames:
                try:
                    await conn.client.write_gatt_char(
                        conn.write_uuid, frame, response=False
                    )
                except Exception:
                    try:
                        await self._disconnect_role(role)
                        await self._ensure_connected([role])
                        conn = self._conns.get(role)
                        if not conn:
                            return
                        await conn.client.write_gatt_char(
                            conn.write_uuid, frame, response=False
                        )
                    except Exception:
                        return

    async def _ambient_stream_async(
        self, top: Tuple[int, int, int], bot: Tuple[int, int, int]
    ) -> None:
        # Ceiling + floor in parallel — roughly 2× faster room updates
        jobs: List[Tuple[str, Tuple[int, int, int]]] = []
        if self.bindings["ceiling"].address:
            jobs.append(("ceiling", top))
        if self.bindings["floor"].address:
            jobs.append(("floor", bot))
        if not jobs:
            return
        await asyncio.gather(
            *[self._ambient_write_role(role, rgb) for role, rgb in jobs],
            return_exceptions=True,
        )

    def _stream_color_frames(
        self, conn: "_Conn", rgb: Tuple[int, int, int]
    ) -> List[bytes]:
        """Minimal packets for rapid Mac→controller color streaming."""
        r, g, b = rgb
        proto = conn.info.protocol
        if proto == "lednetwf":
            # Brightness follows video luma (HSV V), not a fixed 100%
            vv = max(r, g, b) / 255.0
            bri = 0 if vv < 0.02 else max(3, min(100, int(round(vv * 100))))
            # 0x3B HSV (Symphony) + 0x31 RGB solid — some firmwares ignore one
            # path after effects; dual-write makes room ambi actually visible.
            if bri <= 0:
                return [
                    lednet_color_3b(0, 0, 0, 1, conn.seq.next()),
                    lednet_color_31(0, 0, 0, conn.seq.next()),
                ]
            return [
                lednet_color_3b(r, g, b, bri, conn.seq.next()),
                lednet_color_31(r, g, b, conn.seq.next()),
            ]
        if proto == "happy":
            return [happy_color(r, g, b)]
        return [elk_color(r, g, b)]

    async def _apply_async(
        self,
        roles: Sequence[str],
        *,
        power: Optional[bool],
        color: Optional[Tuple[int, int, int]],
        brightness: Optional[int],
        effect: Optional[int],
        bri_pct: Optional[int],
    ) -> None:
        for role in roles:
            # Refresh dead connections
            conn = self._conns.get(role)
            if not conn or not conn.client.is_connected:
                await self._ensure_connected([role])
                conn = self._conns.get(role)
            if not conn:
                continue
            frames = self._build_frames(
                conn, power=power, color=color, brightness=brightness, effect=effect, bri_pct=bri_pct
            )
            async with conn.lock:
                for frame in frames:
                    try:
                        await conn.client.write_gatt_char(conn.write_uuid, frame, response=False)
                    except Exception:
                        await self._disconnect_role(role)
                        await self._ensure_connected([role])
                        conn = self._conns.get(role)
                        if not conn:
                            raise
                        await conn.client.write_gatt_char(conn.write_uuid, frame, response=False)
                    await asyncio.sleep(0.06)

    def _build_frames(
        self,
        conn: _Conn,
        *,
        power: Optional[bool],
        color: Optional[Tuple[int, int, int]],
        brightness: Optional[int],
        effect: Optional[int],
        bri_pct: Optional[int],
    ) -> List[bytes]:
        frames: List[bytes] = []
        proto = conn.info.protocol
        if proto == "lednetwf":
            if power is True:
                frames.append(lednet_power(True, conn.seq.next()))
            if power is False:
                frames.append(lednet_power(False, conn.seq.next()))
            if color is not None:
                r, g, b = color
                pct = bri_pct if bri_pct is not None else 100
                if brightness is not None:
                    # HA: brightness 0–255 scales into 0–100 for 0x3B
                    pct = _bri_to_pct(brightness, pct)
                frames.append(lednet_power(True, conn.seq.next()))
                # LEDnetWF / Magic Hue Symphony: colour via 0x3B HSV (delay=0).
                # Do not follow with 0x31 — it overrides HSV with wire-order RGB.
                frames.append(lednet_color_3b(r, g, b, pct, conn.seq.next()))
            if brightness is not None and effect is None:
                frames.append(lednet_brightness(brightness, conn.seq.next()))
            if effect is not None:
                pct = bri_pct if bri_pct is not None else _bri_to_pct(brightness, 100)
                frames.append(lednet_power(True, conn.seq.next()))
                frames.append(
                    lednet_effect(_lednet_effect_id(effect), 70, pct, conn.seq.next())
                )
        elif proto == "happy":
            if power is True:
                frames.append(happy_power(True))
            if power is False:
                frames.append(happy_power(False))
            if color is not None:
                frames.append(happy_power(True))
                frames.append(happy_color(*color))
            if brightness is not None:
                v = max(1, min(255, brightness))
                frames.append(happy_color(v, v, v))
            # HappyLighting has no reliable effect opcode here — solid color fallback
            if effect is not None and color is None:
                frames.append(happy_power(True))
                frames.append(happy_color(255, 80, 180))
        else:
            if power is True:
                frames.append(elk_power(True))
            if power is False:
                frames.append(elk_power(False))
            if color is not None:
                frames.append(elk_power(True))
                frames.append(elk_color(*color))
            if brightness is not None:
                frames.append(elk_brightness(brightness))
            if effect is not None:
                frames.append(elk_power(True))
                frames.append(elk_effect(effect))
        return frames


_manager: Optional[BtStripManager] = None
_manager_lock = threading.Lock()


def get_bt_manager() -> BtStripManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = BtStripManager()
        return _manager


def blink_identify(role: str, color: Tuple[int, int, int] = (0, 180, 255)) -> None:
    mgr = get_bt_manager()
    mgr.apply(role, color=color)
    time.sleep(0.45)
    mgr.apply(role, color=(255, 255, 255))
    time.sleep(0.25)
    mgr.apply(role, color=color)
