#!/usr/bin/env python3
"""
Adaptive context watcher — frontmost app + screen mood + fullscreen video.

Uses AppKit for the active app, CoreGraphics (ctypes) for fullscreen detection,
and a cheap mss sample for motion/luma / letterbox / video presence.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("synclight.context")

RGB = Tuple[int, int, int]

# Native / streaming players (fullscreen alone → cinema)
_PLAYER_HINTS = (
    "tv",
    "netflix",
    "youtube",
    "iina",
    "quicktime",
    "plex",
    "kino",
    "mpv",
    "vlc",
    "disney",
    "prime",
    "ivi",
    "kinopoisk",
    "moonlight",
    "infuse",
    "photos",
)
# Browsers (HTML5 fullscreen YouTube / Kinopoisk / Netflix in-tab)
_BROWSER_HINTS = (
    "safari",
    "chrome",
    "chromium",
    "firefox",
    "yandex",
    "edge",
    "arc",
    "brave",
    "opera",
    "vivaldi",
    "orion",
)
# Bundle-id fragments (more reliable than localized names)
_BROWSER_BUNDLES = (
    "com.apple.safari",
    "com.google.chrome",
    "com.google.chrome.canary",
    "ru.yandex.desktop.yandex-browser",
    "org.mozilla.firefox",
    "com.microsoft.edgemac",
    "company.thebrowser.browser",
    "com.brave.browser",
    "com.operasoftware.opera",
    "com.vivaldi.vivaldi",
    "com.kagi.kagibrowser",
)
_VIDEO_HINTS = _PLAYER_HINTS + _BROWSER_HINTS


def frontmost_app() -> Tuple[str, str, int]:
    """Return (localizedName, bundleId, pid)."""
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return "", "", 0
        name = str(app.localizedName() or "")
        bid = str(app.bundleIdentifier() or "")
        pid = int(app.processIdentifier() or 0)
        return name, bid, pid
    except Exception:
        return "", "", 0


def _screen_rects() -> list:
    try:
        from AppKit import NSScreen

        out = []
        for s in NSScreen.screens() or []:
            f = s.frame()
            out.append(
                {
                    "x": float(f.origin.x),
                    "y": float(f.origin.y),
                    "w": float(f.size.width),
                    "h": float(f.size.height),
                }
            )
        return out
    except Exception:
        return []


def _cg_window_list() -> list:
    """On-screen windows as Python dicts (CoreGraphics + ObjC bridge)."""
    try:
        import ctypes
        import ctypes.util

        import objc

        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        # OnScreenOnly | ExcludeDesktopElements
        opts = (1 << 0) | (1 << 4)
        ref = cg.CGWindowListCopyWindowInfo(opts, 0)
        if not ref:
            return []
        arr = objc.objc_object(c_void_p=ref)
        out = []
        for w in list(arr):
            try:
                out.append(dict(w))
            except Exception:
                continue
        return out
    except Exception as exc:
        log.debug("window list: %s", exc)
        return []


def detect_fullscreen(pid: int = 0, *, browser: bool = False) -> dict:
    """
    Detect near-fullscreen window for `pid`.

    Browsers (Safari / Yandex / Chrome HTML5 video) often report ~90–99% cover
    (menu bar, notch, FS transition) — use a looser threshold for them.
    """
    result = {
        "fullscreen": False,
        "cover": 0.0,
        "letterbox_hint": False,
        "browser_fs": False,
    }
    if pid <= 0:
        return result

    screens = _screen_rects()
    if not screens:
        return result

    wins = _cg_window_list()
    best = 0.0
    for w in wins:
        try:
            if int(w.get("kCGWindowOwnerPID") or 0) != pid:
                continue
            layer = int(w.get("kCGWindowLayer") or 0)
            if layer > 25:
                continue
            b = w.get("kCGWindowBounds") or {}
            ww = float(b.get("Width", 0) or 0)
            wh = float(b.get("Height", 0) or 0)
            if ww < 320 or wh < 240:
                continue
            area = ww * wh
            for s in screens:
                sw = max(1.0, s["w"])
                sh = max(1.0, s["h"])
                sa = sw * sh
                cover = min(1.0, area / sa)
                edge_w = min(1.05, ww / sw)
                edge_h = min(1.05, wh / sh)
                score = min(1.0, cover, edge_w, edge_h)
                if browser and edge_w >= 0.96 and edge_h >= 0.88:
                    score = max(score, min(1.0, min(edge_w, edge_h)))
                if score > best:
                    best = score
                # Cinematic letterbox: wide film / 16:9 on 16:10 (shorter than display)
                if ww >= sw * 0.90 and 0.48 <= (wh / sh) <= 0.99:
                    result["letterbox_hint"] = True
                # Pillarbox / tall window on ultrawide — still theater
                if wh >= sh * 0.90 and 0.48 <= (ww / sw) <= 0.96:
                    result["letterbox_hint"] = True
        except Exception:
            continue

    result["cover"] = float(best)
    thr = 0.88 if browser else 0.95
    result["fullscreen"] = best >= thr or result["letterbox_hint"]
    result["browser_fs"] = bool(browser and best >= 0.88)
    return result


def looks_like_video_app(app_name: str, bundle_id: str) -> bool:
    blob = f"{app_name} {bundle_id}".lower()
    return any(h in blob for h in _VIDEO_HINTS)


def looks_like_player(app_name: str, bundle_id: str) -> bool:
    blob = f"{app_name} {bundle_id}".lower()
    return any(h in blob for h in _PLAYER_HINTS)


def looks_like_browser(app_name: str, bundle_id: str) -> bool:
    name = (app_name or "").lower()
    bid = (bundle_id or "").lower()
    if any(h in name or h in bid for h in _BROWSER_HINTS):
        return True
    return any(b in bid for b in _BROWSER_BUNDLES)


def _grab_luma_sat(sct, region: dict) -> Tuple[float, float]:
    """Return (mean Rec.709 luma 0..255, mean RGB saturation 0..1)."""
    shot = sct.grab(region)
    raw = shot.bgra
    step = 20 * 4
    luma_s = sat_s = n = 0.0
    for i in range(0, len(raw) - 3, step):
        b, g, r = raw[i], raw[i + 1], raw[i + 2]
        luma_s += 0.0722 * b + 0.7152 * g + 0.2126 * r
        mx, mn = max(r, g, b), min(r, g, b)
        sat_s += ((mx - mn) / mx) if mx > 8 else 0.0
        n += 1.0
    if n <= 0:
        return 255.0, 0.0
    return luma_s / n, sat_s / n


def sample_letterbox_bars() -> bool:
    """True when top+bottom are dark and the middle has picture."""
    try:
        import mss
    except Exception:
        return False
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1]
            w, h = int(mon["width"]), int(mon["height"])
            left, top = int(mon["left"]), int(mon["top"])
            band = max(8, h // 16)
            top_l, _ = _grab_luma_sat(
                sct, {"left": left, "top": top, "width": w, "height": band}
            )
            bot_l, _ = _grab_luma_sat(
                sct,
                {
                    "left": left,
                    "top": top + h - band,
                    "width": w,
                    "height": band,
                },
            )
            mid_l, mid_s = _grab_luma_sat(
                sct,
                {
                    "left": left + w // 5,
                    "top": top + h // 4,
                    "width": max(64, (3 * w) // 5),
                    "height": max(48, h // 3),
                },
            )
            if top_l < 28 and bot_l < 28 and mid_l > 24:
                return True
            if top_l < 45 and bot_l < 45 and mid_s > 0.12 and mid_l > 22:
                return True
            return False
    except Exception as exc:
        log.debug("letterbox sample: %s", exc)
        return False


def sample_fs_video_hint() -> dict:
    """Cues that the display shows video content (not settings / blank UI)."""
    out = {
        "video_like": False,
        "mid_luma": 128.0,
        "mid_sat": 0.0,
        "edge_dark": False,
    }
    try:
        import mss
    except Exception:
        return out
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1]
            w, h = int(mon["width"]), int(mon["height"])
            left, top = int(mon["left"]), int(mon["top"])
            band = max(6, h // 22)
            top_l, _ = _grab_luma_sat(
                sct, {"left": left, "top": top, "width": w, "height": band}
            )
            bot_l, _ = _grab_luma_sat(
                sct,
                {
                    "left": left,
                    "top": top + h - band,
                    "width": w,
                    "height": band,
                },
            )
            mid_l, mid_s = _grab_luma_sat(
                sct,
                {
                    "left": left + w // 6,
                    "top": top + h // 6,
                    "width": max(80, (2 * w) // 3),
                    "height": max(60, (2 * h) // 3),
                },
            )
            edge_dark = (top_l + bot_l) * 0.5 < mid_l - 12
            video_like = bool(
                (mid_s > 0.08 and 8 < mid_l < 220)
                or (edge_dark and mid_l > 20)
                or (mid_s > 0.15)
            )
            out.update(
                {
                    "video_like": video_like,
                    "mid_luma": mid_l,
                    "mid_sat": mid_s,
                    "edge_dark": edge_dark,
                }
            )
            return out
    except Exception as exc:
        log.debug("fs video hint: %s", exc)
        return out


def sample_screen_mood() -> Tuple[RGB, float]:
    """Downsample center band; return (avg_rgb, motion_proxy 0..1)."""
    try:
        import mss
    except Exception:
        return (128, 128, 128), 0.0

    if not hasattr(sample_screen_mood, "_prev"):
        sample_screen_mood._prev = None  # type: ignore

    try:
        with mss.mss() as sct:
            mon = sct.monitors[1]
            region = {
                "left": mon["left"] + mon["width"] // 5,
                "top": mon["top"] + mon["height"] // 4,
                "width": max(64, (3 * mon["width"]) // 5),
                "height": max(48, mon["height"] // 3),
            }
            shot = sct.grab(region)
            raw = shot.bgra
            step = 12 * 4
            rs = gs = bs = n = 0
            for i in range(0, len(raw) - 3, step):
                bs += raw[i]
                gs += raw[i + 1]
                rs += raw[i + 2]
                n += 1
            if n <= 0:
                return (128, 128, 128), 0.0
            avg = (rs // n, gs // n, bs // n)
            luma = 0.2126 * avg[0] + 0.7152 * avg[1] + 0.0722 * avg[2]
            prev = sample_screen_mood._prev  # type: ignore
            motion = 0.0
            if prev is not None:
                motion = float(min(1.0, abs(luma - prev) / 28.0))
            sample_screen_mood._prev = float(luma)  # type: ignore
            return avg, motion
    except Exception as exc:
        log.debug("screen sample: %s", exc)
        return (128, 128, 128), 0.0


def _video_owner_pids() -> List[Tuple[str, int]]:
    """(owner_name, pid) for on-screen browser/player windows."""
    found: dict = {}
    for w in _cg_window_list():
        try:
            owner = str(w.get("kCGWindowOwnerName") or "")
            pid = int(w.get("kCGWindowOwnerPID") or 0)
            if pid <= 0:
                continue
            if looks_like_browser(owner, "") or looks_like_player(owner, ""):
                found[pid] = owner
        except Exception:
            continue
    return [(n, p) for p, n in found.items()]


def classify_cinema(
    *,
    name: str,
    bid: str,
    pid: int,
    avg: RGB,
    motion: float,
) -> dict:
    """
    Cinema classification independent of frontmost app.

    Primary signal: an on-screen Safari/Yandex/(Chrome) window that looks
    like video — Ambilight follows that window even if Cursor is focused.
    """
    browser = looks_like_browser(name, bid)
    player = looks_like_player(name, bid)
    video_app = looks_like_video_app(name, bid) or browser

    browser_tgt = None
    try:
        from browser_video import browser_video_active, find_browser_video_target

        browser_tgt = find_browser_video_target()
    except Exception:
        browser_tgt = None

    browser_win = browser_tgt is not None
    browser_playing = bool(browser_tgt and browser_video_active(browser_tgt))
    theater_app = (browser_tgt or {}).get("owner") or name

    # Legacy FS cover (still useful for native players)
    candidates: List[Tuple[str, int, bool]] = [(name, pid, browser)]
    for owner, opid in _video_owner_pids():
        if opid == pid:
            continue
        candidates.append((owner, opid, looks_like_browser(owner, "")))

    best_fs = {
        "fullscreen": False,
        "cover": 0.0,
        "letterbox_hint": False,
        "browser_fs": False,
    }
    any_player_fs = False
    for owner, opid, is_browser in candidates:
        if opid <= 0:
            continue
        fs = detect_fullscreen(opid, browser=is_browser)
        if float(fs.get("cover") or 0) >= float(best_fs.get("cover") or 0):
            best_fs = fs
            if not browser_win:
                theater_app = owner or theater_app
        if (fs.get("fullscreen") or fs.get("browser_fs")) and looks_like_player(
            owner, ""
        ):
            any_player_fs = True

    bars = sample_letterbox_bars()
    hint = sample_fs_video_hint()
    if bars:
        best_fs["letterbox_hint"] = True
    cover = float(best_fs.get("cover") or 0)
    if browser_tgt:
        # Approximate cover from window area vs primary 1080p
        cover = max(cover, min(1.0, float(browser_tgt.get("area") or 0) / (1920 * 1080)))
    video_like = bool(hint.get("video_like")) or browser_playing
    fs_on = bool(best_fs.get("fullscreen")) or browser_playing or (
        browser_win and cover >= 0.85
    )

    # Browser window with video (or large theater window) wins — no focus needed
    raw_cinema = (
        browser_playing
        or (browser_win and (video_like or bars or cover >= 0.85))
        or (player and bool(best_fs.get("fullscreen")))
        or any_player_fs
    )
    return {
        "app": theater_app or name,
        "bundle": bid,
        "pid": pid,
        "screen": avg,
        "motion": motion,
        "fullscreen": fs_on,
        "fullscreen_cover": cover,
        "letterbox": bool(best_fs.get("letterbox_hint")) or bars,
        "browser": browser or browser_win,
        "browser_fs": browser_win,
        "video_app": video_app or browser_win,
        "video_like": video_like,
        "browser_window": browser_win,
        "raw_cinema": bool(raw_cinema),
        "cinematic": bool(raw_cinema),
        "ts": time.time(),
    }


class ContextWatcher:
    """Background loop that notifies owner when context changes."""

    def __init__(
        self,
        on_tick: Callable[[dict], None],
        interval: float = 0.8,
        sample_screen: bool = True,
    ):
        self.on_tick = on_tick
        self.interval = interval
        self.sample_screen = sample_screen
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last: dict = {}
        # Hysteresis: arm after 2 hits, hold ~5s after leave
        self._cinema_hits = 0
        self._cinema_hold = 0
        self._cinema_latched = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ai-context", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _loop(self) -> None:
        last_key = None
        while not self._stop.is_set():
            wait = self.interval
            try:
                name, bid, pid = frontmost_app()
                avg, motion = (128, 128, 128), 0.0
                if self.sample_screen:
                    avg, motion = sample_screen_mood()
                info = classify_cinema(
                    name=name, bid=bid, pid=pid, avg=avg, motion=motion
                )
                raw = bool(info.get("raw_cinema"))
                if raw:
                    self._cinema_hits = min(8, self._cinema_hits + 1)
                else:
                    self._cinema_hits = max(0, self._cinema_hits - 1)

                # Arm after 2 consecutive theater readings (~0.7–1s)
                if self._cinema_hits >= 2:
                    self._cinema_hold = 14  # ~5–6s grace when chrome pops up
                    self._cinema_latched = True
                elif self._cinema_hold > 0:
                    self._cinema_hold -= 1
                else:
                    self._cinema_latched = False

                info["cinematic"] = bool(self._cinema_latched)
                self.last = info

                browser = bool(info.get("browser"))
                player = bool(info.get("video_app")) and not browser
                if self._cinema_latched:
                    wait = 0.30
                elif browser or player:
                    wait = 0.40
                else:
                    wait = self.interval

                key = (
                    info.get("app"),
                    info.get("bundle"),
                    bool(info.get("fullscreen")),
                    bool(info.get("cinematic")),
                    round(float(info.get("motion") or 0), 2),
                )
                prev_cinema = bool(last_key[3]) if last_key else False
                if key != last_key or bool(info["cinematic"]) != prev_cinema:
                    last_key = key
                    try:
                        self.on_tick(info)
                    except Exception:
                        log.exception("on_tick")
            except Exception:
                log.exception("context loop")
                wait = self.interval
            self._stop.wait(wait)
