#!/usr/bin/env python3
"""Bluetooth helpers for SyncLight — TCC diagnostics for Dock vs Terminal."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional, Tuple

# CBManagerAuthorization
NOT_DETERMINED = 0
RESTRICTED = 1
DENIED = 2
ALLOWED = 3


def launched_as_app() -> bool:
    """True when started via SyncLight.app (Dock) — TCC identity is SyncLight."""
    exe = sys.executable or ""
    env = os.environ
    if "SyncLight.app" in exe:
        return True
    # Bundle resources / app transport
    for key in ("PACKAGE_PATH", "APP_LOCATION", "__CFBundleIdentifier"):
        val = env.get(key) or ""
        if "synclight" in val.lower():
            return True
    # Parent path heuristic: argv[0] under .app
    try:
        a0 = os.path.abspath(sys.argv[0])
        if "SyncLight.app" in a0:
            return True
    except Exception:
        pass
    return False


def bluetooth_authorization() -> int:
    try:
        from CoreBluetooth import CBCentralManager

        return int(CBCentralManager.authorization())
    except Exception:
        return NOT_DETERMINED


def authorization_label(code: Optional[int] = None) -> str:
    code = bluetooth_authorization() if code is None else code
    return {
        NOT_DETERMINED: "не запрошено",
        RESTRICTED: "ограничено системой",
        DENIED: "запрещено",
        ALLOWED: "разрешено",
    }.get(code, str(code))


def is_ble_authorized() -> bool:
    return bluetooth_authorization() not in (DENIED, RESTRICTED)


def process_hint() -> str:
    # From Dock, child Python still counts as SyncLight for BLE TCC
    if launched_as_app():
        return "SyncLight"
    exe = sys.executable or ""
    if "Python.app" in exe or exe.endswith("/Python") or "python" in exe.lower():
        return "Python"
    return "SyncLight"


def request_bluetooth_permission(timeout: float = 4.0) -> Tuple[int, str]:
    code = bluetooth_authorization()
    who = process_hint()
    if code == ALLOWED:
        return code, f"Bluetooth разрешён ({who})"
    if code == DENIED:
        return code, (
            f"Bluetooth запрещён для «{who}». "
            f"Настройки → Конфиденциальность и безопасность → Bluetooth → включи «{who}» "
            "и «Python». Затем нажми «Подключить»."
        )
    if code == RESTRICTED:
        return code, "Bluetooth ограничен системой (родительский контроль / MDM)."
    return (
        code,
        f"Статус Bluetooth: {authorization_label(code)}. "
        f"Разреши доступ, в списке Bluetooth включи «{who}» и «Python».",
    )


def open_bluetooth_settings() -> None:
    urls = [
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Bluetooth",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth",
    ]
    for url in urls:
        try:
            subprocess.run(["open", url], check=False)
            return
        except Exception:
            continue


_screen_asked = False
_settings_opened = False
_ASKED_FLAG = os.path.expanduser(
    "~/Library/Application Support/SyncLight/screen_tcc_asked"
)


def _persist_asked() -> None:
    try:
        os.makedirs(os.path.dirname(_ASKED_FLAG), exist_ok=True)
        with open(_ASKED_FLAG, "w") as f:
            f.write("1\n")
    except Exception:
        pass


def _was_asked_before() -> bool:
    try:
        return os.path.isfile(_ASKED_FLAG)
    except Exception:
        return False


def _cg_screen_preflight() -> Optional[bool]:
    """True/False if API works; None if unavailable."""
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore

        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        pass
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def _cg_screen_request() -> Optional[bool]:
    """System dialog — call at most once ever for this install."""
    try:
        from Quartz import CGRequestScreenCaptureAccess  # type: ignore

        return bool(CGRequestScreenCaptureAccess())
    except Exception:
        pass
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGRequestScreenCaptureAccess())
    except Exception:
        return None


def open_screen_recording_settings() -> None:
    urls = [
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    ]
    for url in urls:
        try:
            subprocess.run(["open", url], check=False)
            return
        except Exception:
            continue


def frame_is_privacy_grey(raw: bytes, width: int = 0, height: int = 0) -> bool:
    """True when buffer is macOS TCC placeholder (flat ~#5E5E5E), not real video."""
    if not raw or len(raw) < 16:
        return True
    step = max(4, len(raw) // 2500)
    if width > 0 and height > 0:
        step = max(4, (width * height * 4) // 2500)
    sat_s = n = 0.0
    lum_min, lum_max = 255.0, 0.0
    near_flat = 0.0
    rs = gs = bs = 0.0
    for i in range(0, len(raw) - 3, step):
        b, g, r = raw[i], raw[i + 1], raw[i + 2]
        rs += r
        gs += g
        bs += b
        mx, mn = max(r, g, b), min(r, g, b)
        sat_s += ((mx - mn) / mx) if mx > 8 else 0.0
        lum = 0.0722 * b + 0.7152 * g + 0.2126 * r
        lum_min = min(lum_min, lum)
        lum_max = max(lum_max, lum)
        if abs(r - g) <= 2 and abs(g - b) <= 2 and 70 <= r <= 120:
            near_flat += 1.0
        n += 1.0
    if n <= 0:
        return True
    sat = sat_s / n
    contrast = lum_max - lum_min
    flat_pct = near_flat / n
    # Classic privacy grey: almost no chroma, low contrast, mid-grey plateau
    if sat < 0.02 and contrast < 22.0:
        return True
    if flat_pct > 0.72 and sat < 0.05:
        return True
    mean_r, mean_g, mean_b = rs / n, gs / n, bs / n
    if (
        abs(mean_r - mean_g) < 2.0
        and abs(mean_g - mean_b) < 2.0
        and 85 <= mean_r <= 105
        and sat < 0.045
        and contrast < 40.0
    ):
        return True
    return False


def _mss_capture_works() -> bool:
    """Real proof of Screen Recording — CGPreflight can lie across exec/TCC ids.

    Denied TCC often still returns a buffer (flat grey) — require chroma or
    luma variance so ghost frames don't count as success.
    """
    try:
        import mss

        with mss.MSS() as sct:
            mons = sct.monitors
            if len(mons) < 2:
                return False
            shot = sct.grab(mons[1])
            if not shot or shot.width < 16 or shot.height < 16:
                return False
            if frame_is_privacy_grey(shot.bgra, shot.width, shot.height):
                return False
            raw = shot.bgra
            step = max(4, (shot.width * shot.height * 4) // 2000)
            sat_s = n = 0.0
            lum_min, lum_max = 255.0, 0.0
            for i in range(0, len(raw) - 3, step):
                b, g, r = raw[i], raw[i + 1], raw[i + 2]
                mx, mn = max(r, g, b), min(r, g, b)
                sat_s += ((mx - mn) / mx) if mx > 8 else 0.0
                lum = 0.0722 * b + 0.7152 * g + 0.2126 * r
                lum_min = min(lum_min, lum)
                lum_max = max(lum_max, lum)
                n += 1.0
            if n <= 0:
                return False
            sat = sat_s / n
            return sat > 0.02 or (lum_max - lum_min) > 25.0
    except Exception:
        return False


def ensure_screen_access(*, prompt: bool = True, open_settings: bool = False) -> bool:
    """
    Screen Recording for Ambilight. Ask at most once ever (persisted).

    Trusts a live colourful mss grab — never CGRequest on every launch
    (that was the endless «Запись экрана» dialog while SyncLight was already listed).
    """
    global _screen_asked, _settings_opened
    if _mss_capture_works():
        return True
    # Already asked this process or a previous run — never spam the system dialog.
    if _screen_asked or _was_asked_before():
        _screen_asked = True
        # Preflight True with privacy-grey grab = SyncLight listed but toggle off
        if open_settings and not _settings_opened and not _mss_capture_works():
            _settings_opened = True
            # Do NOT auto-open settings every boot — only when caller insists once
            if prompt:
                open_screen_recording_settings()
        return _mss_capture_works()
    pre = _cg_screen_preflight()
    if pre is None:
        return True  # old macOS / no API — let mss try
    if prompt and not _screen_asked:
        _screen_asked = True
        _persist_asked()
        # Only Request when clearly never determined — not when toggle is merely off
        if pre is False:
            _cg_screen_request()
        if _mss_capture_works():
            return True
        if open_settings and not _settings_opened:
            _settings_opened = True
            open_screen_recording_settings()
        return _mss_capture_works()
    return _mss_capture_works()


def screen_permission_help() -> str:
    """Short Russian hint: which toggles to enable for Ambilight."""
    who = process_hint()
    return (
        "Ambilight нужен доступ «Запись экрана».\n"
        "Системные настройки → Конфиденциальность и безопасность → "
        "Запись экрана и системного звука → включи:\n"
        f"  • SyncLight\n  • Python\n"
        f"(сейчас процесс: {who})"
    )


def ble_error_hint(exc: BaseException, *, open_settings: bool = False) -> str:
    msg = str(exc).lower()
    who = process_hint()
    if any(x in msg for x in ("authoriz", "unauthorized", "not permitted", "permission")):
        # Never auto-open Settings on every retry — permission is often already
        # granted (Python/SyncLight); opening the pane feels like a new prompt.
        if open_settings and bluetooth_authorization() in (DENIED, RESTRICTED):
            open_bluetooth_settings()
        return (
            f"macOS блокирует Bluetooth для «{who}». "
            f"Если переключатель уже включён — перезапусти SyncLight из Dock. "
            f"Иначе: Настройки → Bluetooth → «SyncLight» и «Python»."
        )
    if any(x in msg for x in ("powered off", "turned off", "not powered")):
        return (
            "Bluetooth выключен или занят. "
            "Включи в строке меню и нажми «Подключить»."
        )
    return str(exc)


def python_app_binary() -> Optional[str]:
    candidates = [
        "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python",
        "/Library/Frameworks/Python.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python",
        "/usr/bin/python3",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return sys.executable
