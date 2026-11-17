#!/usr/bin/env python3
"""
Locate Safari / Yandex / Chrome windows that are showing video and return
an mss capture region — independent of frontmost app and which monitor is
"primary".

Also captures a specific window via CGWindowListCreateImage (more reliable
than display-region mss when Screen Recording TCC is partial).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("synclight.browser_video")

RGB = Tuple[int, int, int]

# Owner names / fragments (localized + English)
_BROWSER_OWNERS = (
    "safari",
    "yandex",
    "яндекс",
    "chrome",
    "chromium",
    "firefox",
    "edge",
    "arc",
    "brave",
    "opera",
    "vivaldi",
)
_VIDEO_TITLE_HINTS = (
    "youtube",
    "youtu",
    "netflix",
    "кинопоиск",
    "kinopoisk",
    "ivi",
    "okko",
    "twitch",
    "vimeo",
    "prime",
    "disney",
    "hulu",
    "player",
    "видео",
    "video",
    "watch",
    "фильм",
    "сериал",
)


def _owner_is_browser(owner: str) -> bool:
    o = (owner or "").lower()
    return any(h in o for h in _BROWSER_OWNERS)


def _title_looks_video(title: str) -> bool:
    t = (title or "").lower()
    return any(h in t for h in _VIDEO_TITLE_HINTS)


def _window_dicts() -> List[dict]:
    try:
        from context_watch import _cg_window_list

        return list(_cg_window_list() or [])
    except Exception as exc:
        log.debug("window list: %s", exc)
        return []


def list_browser_windows() -> List[Dict[str, Any]]:
    """All on-screen browser windows with pixel bounds for mss."""
    out: List[Dict[str, Any]] = []
    for w in _window_dicts():
        try:
            owner = str(w.get("kCGWindowOwnerName") or "")
            if not _owner_is_browser(owner):
                continue
            layer = int(w.get("kCGWindowLayer") or 0)
            if layer > 25:
                continue
            b = w.get("kCGWindowBounds") or {}
            x = int(float(b.get("X", 0) or 0))
            y = int(float(b.get("Y", 0) or 0))
            ww = int(float(b.get("Width", 0) or 0))
            wh = int(float(b.get("Height", 0) or 0))
            if ww < 400 or wh < 300:
                continue
            title = str(w.get("kCGWindowName") or "")
            # Always crop browser chrome a bit — even FS 1080 still has title bar
            # when not true macOS fullscreen; helps mss region avoid grey UI.
            inset_top = min(80, max(28, wh // 12))
            cy = y + inset_top
            ch = max(240, wh - inset_top)
            wid = int(w.get("kCGWindowNumber") or 0)
            out.append(
                {
                    "owner": owner,
                    "pid": int(w.get("kCGWindowOwnerPID") or 0),
                    "window_id": wid,
                    "title": title,
                    "left": x,
                    "top": cy,
                    "width": ww,
                    "height": ch,
                    "area": ww * ch,
                    "video_title": _title_looks_video(title),
                    "layer": layer,
                }
            )
        except Exception:
            continue
    return out


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


def capture_window_bgra(window_id: int) -> Optional[Tuple[bytes, int, int]]:
    """
    Capture one window by CGWindowID → (BGRA bytes, width, height).

    Retina may return 2× logical size; Ambilight only needs colours, so OK.
    Returns None on failure / empty.
    """
    wid = int(window_id or 0)
    if wid <= 0:
        return None
    try:
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cg.CGWindowListCreateImage.restype = ctypes.c_void_p
        cg.CGWindowListCreateImage.argtypes = [
            _CGRect,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        cg.CGImageGetWidth.argtypes = [ctypes.c_void_p]
        cg.CGImageGetWidth.restype = ctypes.c_size_t
        cg.CGImageGetHeight.argtypes = [ctypes.c_void_p]
        cg.CGImageGetHeight.restype = ctypes.c_size_t
        cg.CGImageGetBytesPerRow.argtypes = [ctypes.c_void_p]
        cg.CGImageGetBytesPerRow.restype = ctypes.c_size_t
        cg.CGImageGetDataProvider.argtypes = [ctypes.c_void_p]
        cg.CGImageGetDataProvider.restype = ctypes.c_void_p
        cg.CGDataProviderCopyData.argtypes = [ctypes.c_void_p]
        cg.CGDataProviderCopyData.restype = ctypes.c_void_p
        cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        null = _CGRect(_CGPoint(0, 0), _CGSize(0, 0))
        # kCGWindowListOptionIncludingWindow = 1<<3 = 8
        # kCGWindowImageBoundsIgnoreFraming = 1<<0 = 1
        img = cg.CGWindowListCreateImage(null, 8, ctypes.c_uint32(wid), 1)
        if not img:
            return None
        w = int(cg.CGImageGetWidth(img))
        h = int(cg.CGImageGetHeight(img))
        bpr = int(cg.CGImageGetBytesPerRow(img))
        if w < 16 or h < 16:
            return None
        prov = cg.CGImageGetDataProvider(img)
        cdata = cg.CGDataProviderCopyData(prov)
        if not cdata:
            return None
        try:
            length = int(cf.CFDataGetLength(cdata))
            ptr = cf.CFDataGetBytePtr(cdata)
            src = (ctypes.c_ubyte * length).from_address(ptr)
            # Pack tightly to width*4 if bytes-per-row has padding
            if bpr == w * 4 and length >= w * h * 4:
                raw = bytes(src[: w * h * 4])
            else:
                rows = []
                for y in range(h):
                    off = y * bpr
                    rows.append(bytes(src[off : off + w * 4]))
                raw = b"".join(rows)
            return raw, w, h
        finally:
            cf.CFRelease(cdata)
    except Exception as exc:
        log.debug("capture_window_bgra: %s", exc)
        return None


def _bgra_chroma_score(raw: bytes, step: int = 160) -> float:
    """Rough mean saturation 0..1 from BGRA."""
    if not raw or len(raw) < 4:
        return 0.0
    sat_s = n = 0.0
    for i in range(0, len(raw) - 3, max(4, step)):
        b, g, r = raw[i], raw[i + 1], raw[i + 2]
        mx, mn = max(r, g, b), min(r, g, b)
        sat_s += ((mx - mn) / mx) if mx > 8 else 0.0
        n += 1.0
    return float(sat_s / n) if n else 0.0


def _region_video_score(sct, region: dict) -> float:
    """0..1 — colourful / mid-luma content inside region (playing video)."""
    try:
        shot = sct.grab(region)
        raw = shot.bgra
        step = 40 * 4
        sat_s = lum_s = n = 0.0
        for i in range(0, len(raw) - 3, step):
            b, g, r = raw[i], raw[i + 1], raw[i + 2]
            mx, mn = max(r, g, b), min(r, g, b)
            sat_s += ((mx - mn) / mx) if mx > 8 else 0.0
            lum_s += 0.0722 * b + 0.7152 * g + 0.2126 * r
            n += 1.0
        if n <= 0:
            return 0.0
        sat = sat_s / n
        lum = lum_s / n
        score = sat * 2.0
        if 18 < lum < 210:
            score += 0.35
        if sat > 0.08:
            score += 0.25
        return float(min(1.5, score))
    except Exception:
        return 0.0


def find_browser_video_target(
    sct=None,
    *,
    prefer_owners: Tuple[str, ...] = ("yandex", "яндекс", "safari"),
) -> Optional[Dict[str, Any]]:
    """
    Best Safari/Yandex/(Chrome) window to drive Ambilight from.

    Returns mss region dict plus metadata, or None if no browser window.
    """
    wins = list_browser_windows()
    if not wins:
        return None

    own_sct = False
    if sct is None:
        try:
            import mss

            sct = mss.MSS()
            own_sct = True
        except Exception:
            sct = None

    try:
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for w in wins:
            region = {
                "left": w["left"],
                "top": w["top"],
                "width": w["width"],
                "height": w["height"],
            }
            owner_l = w["owner"].lower()
            pref = 0.0
            for i, hint in enumerate(prefer_owners):
                if hint in owner_l:
                    pref = 1.0 - 0.05 * i
                    break
            area_n = min(1.0, w["area"] / (1920 * 1080))
            # Strong boost for YouTube/etc titles so a playing Yandex tab
            # beats an idle Safari window.
            title_b = 1.35 if w["video_title"] else 0.0
            content = _region_video_score(sct, region) if sct is not None else 0.2
            # Fullscreen-ish browser (near display size) beats small windows
            fs_b = 0.6 if w["area"] >= int(0.85 * 1920 * 1080) else 0.0
            score = pref * 1.2 + area_n * 0.8 + title_b + content * 1.5 + fs_b
            scored.append((score, {**w, "region": region, "content_score": content}))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]
    finally:
        if own_sct and sct is not None:
            try:
                sct.close()
            except Exception:
                pass


def browser_video_active(target: Optional[dict] = None) -> bool:
    """True when a browser window looks like it is showing video."""
    t = target if target is not None else find_browser_video_target()
    if not t:
        return False
    if t.get("video_title"):
        return True
    if float(t.get("content_score") or 0) >= 0.35:
        return True
    return int(t.get("area") or 0) >= int(0.7 * 1920 * 1080)
