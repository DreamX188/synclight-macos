#!/usr/bin/env python3
"""
SyncLight Liquid Glass GUI — WKWebView + translucent UI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from AppKit import (  # noqa: E402
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSColor,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSScreen,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowCollectionBehaviorFullScreenPrimary,
    NSWindowCollectionBehaviorManaged,
)
from Foundation import NSURL, NSDictionary  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402
from WebKit import (  # noqa: E402
    WKScriptMessage,
    WKWebView,
    WKWebViewConfiguration,
)
from objc import python_method, super as objc_super  # noqa: E402

from ai_scenes import (  # noqa: E402
    circadian_color,
    get_scene,
    resolve_auto,
    scenes_payload,
)
from ambilight import AmbilightEngine, leds_from_room_colors, list_displays  # noqa: E402
from bt_client import _bt_log, get_bt_client  # noqa: E402
from color_math import OklabFollower, lift_for_leds  # noqa: E402
from context_watch import ContextWatcher  # noqa: E402
from device import (  # noqa: E402
    oneshot,
    set_brightness,
    set_dynamic_speed,
    set_section_led,
    set_sound_sensitivity,
    shared as usb_shared,
)
from dialog_gui import autostart_enabled  # noqa: E402
from settings import load_settings, save_settings  # noqa: E402
from singleton import ensure_singleton  # noqa: E402
from sl import PRESETS, STATE_FILE  # noqa: E402
from targets import apply_brightness, apply_color, apply_power, normalize_target  # noqa: E402
from room_fx import RoomFxEngine  # noqa: E402
from yandex_bulb import (  # noqa: E402
    fetch_local_key_cloud,
    get_yandex_bulb,
    scan_lan as scan_yandex_lan,
)
from version import as_dict as version_dict, display_version, window_title  # noqa: E402


UI_DIR = os.path.join(ROOT, "ui")


def _load_color():
    try:
        parts = open(STATE_FILE).read().strip().split()
        return tuple(int(x) for x in parts)
    except Exception:
        return (255, 200, 100)


class Bridge(NSObject):
    def initWithOwner_(self, owner):
        self = objc_super(Bridge, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message: WKScriptMessage):
        body = message.body()
        if isinstance(body, NSDictionary):
            payload = dict(body)
        elif isinstance(body, dict):
            payload = body
        else:
            try:
                payload = json.loads(str(body))
            except Exception:
                return
        # Normalize pyobjc types
        clean = {}
        for k, v in payload.items():
            clean[str(k)] = v
        self.owner.handle_message(clean)


class MenuTarget(NSObject):
    def initWithOwner_(self, owner):
        self = objc_super(MenuTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def showWindow_(self, _sender):
        self.owner.show_window()

    def toggleAmbi_(self, _sender):
        self.owner.handle_message(
            {"action": "ambi", "enabled": not self.owner.ambi_on}
        )
        AppHelper.callLater(0.2, self.owner.refresh_status_menu)

    def quitApp_(self, _sender):
        self.owner.shutdown_fast(reason="menu-quit")
        NSApp.terminate_(None)


class WindowDelegate(NSObject):
    """Close traffic-light must quit the whole app (no zombie menu-bar instance)."""

    def initWithOwner_(self, owner):
        self = objc_super(WindowDelegate, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def windowShouldClose_(self, _sender):
        # Hide to menu bar while Ambilight/room are live — killing the process
        # mid-video left strips stuck and looked like «не работает».
        # Full quit stays on menu «Quit» / Dock Quit.
        try:
            if getattr(self.owner, "ambi_on", False) or getattr(
                self.owner, "mirror_room", False
            ):
                if self.owner.window is not None:
                    self.owner.window.orderOut_(None)
                return False
        except Exception:
            pass
        self.owner.shutdown_fast(reason="window-close")
        return True


class AppDelegate(NSObject):
    def initWithOwner_(self, owner):
        self = objc_super(AppDelegate, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
        # Keep running in menu bar while Ambilight is on (крестик только прячет окно).
        try:
            if getattr(self.owner, "ambi_on", False):
                return False
        except Exception:
            pass
        return True

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, flag):
        if not flag:
            self.owner.show_window()
        return True

    def applicationWillTerminate_(self, _app):
        self.owner.shutdown_fast(reason="will-terminate")
        # Do NOT restore KeepAlive USB daemon — it steals the strip from the next launch.


class GlassApp(NSObject):
    def init(self):
        self = objc_super(GlassApp, self).init()
        if self is None:
            return None
        self.cfg = load_settings()
        self.color = tuple(self.cfg.get("color") or _load_color())
        if len(self.color) != 3:
            self.color = _load_color()
        self.brightness = int(self.cfg.get("brightness", 200))
        self.speed = int(self.cfg.get("effect_speed", 55))
        self.sensitivity = int(self.cfg.get("sensitivity", 70))
        self.target = normalize_target(self.cfg.get("target", "all"))
        self.mirror_room = bool(self.cfg.get("mirror_room", True))
        self.bt = get_bt_client()
        self.bulb = get_yandex_bulb()
        try:
            self.bulb.load_from_settings(self.cfg)
        except Exception:
            pass
        mon = int(self.cfg.get("ambi_monitor") or 1)
        self.ambi = AmbilightEngine(
            on_frame=self._on_ambi_frame, monitor_index=mon, fps=55
        )
        self.ambi_on = False
        self.scene_id = str(self.cfg.get("ai_scene") or "")
        self.scene_live = False  # auto / circadian keep adapting
        self.context_info = {}
        # Fast poll — catch Safari/Yandex HTML5 fullscreen video enter/exit
        self.watcher = ContextWatcher(on_tick=self._on_context, interval=0.8)
        self._scene_lock = threading.Lock()
        self._last_auto_key = None
        self.window = None
        self.web = None
        self.status_item = None
        self.menu_target = None
        self.ambi_menu_item = None
        self.boot_note = ""
        self._ui_scenes_sent = False
        self._shutting_down = False
        self._room_ambi_stop = threading.Event()
        self._room_ambi_thread: Optional[threading.Thread] = None
        self.room_fx = RoomFxEngine()
        self._room_fx_idx = 0
        # Ambilight auto-armed for browser/player fullscreen video
        self._ambi_auto_fs = False
        self._ambi_auto_suppress = False  # user turned ambi off during FS
        return self

    @python_method
    def shutdown_fast(self, reason: str = "") -> None:
        """Quit path: never block AppKit on BLE/USB teardown."""
        if self._shutting_down:
            _bt_log(f"GUI shutdown_fast skip (already) reason={reason}")
            return
        self._shutting_down = True
        _bt_log(f"GUI shutdown_fast BEGIN reason={reason}")
        t0 = time.time()
        try:
            self._stop_scene_watch()
        except Exception as exc:
            _bt_log(f"GUI stop_scene_watch: {exc}")
        try:
            self.ambi_on = False
            self._stop_room_ambi()
            try:
                self.room_fx.stop(join=False)
            except Exception:
                pass
            self.ambi.stop(quick=True)
        except Exception as exc:
            _bt_log(f"GUI ambi.stop: {exc}")
        try:
            self.bt.close()
        except Exception as exc:
            _bt_log(f"GUI bt.close: {exc}")
        _bt_log(f"GUI shutdown_fast END {time.time() - t0:.3f}s reason={reason}")

    def run(self):
        NSApplication.sharedApplication()
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        self.app_delegate = AppDelegate.alloc().initWithOwner_(self)
        NSApp.setDelegate_(self.app_delegate)

        # Standard macOS titlebar so traffic lights never overlay content,
        # and the window can be dragged freely between monitors.
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        screen = NSScreen.mainScreen()
        width, height = 1180.0, 760.0
        if screen is not None:
            vf = screen.visibleFrame()
            width = min(width, max(900.0, vf.size.width - 80))
            height = min(height, max(600.0, vf.size.height - 80))
            x = vf.origin.x + (vf.size.width - width) / 2.0
            y = vf.origin.y + (vf.size.height - height) / 2.0
            frame = NSMakeRect(x, y, width, height)
        else:
            frame = NSMakeRect(80, 80, width, height)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        window.setTitle_(window_title())
        window.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.043, 0.051, 0.063, 1.0)
        )
        window.setOpaque_(True)
        window.setMovableByWindowBackground_(False)
        window.setReleasedWhenClosed_(True)
        window.setMinSize_((900, 600))
        self.window_delegate = WindowDelegate.alloc().initWithOwner_(self)
        window.setDelegate_(self.window_delegate)
        try:
            window.setFrameAutosaveName_("SyncLightMainWindow")
        except Exception:
            pass
        try:
            window.setCollectionBehavior_(
                NSWindowCollectionBehaviorFullScreenPrimary
                | NSWindowCollectionBehaviorManaged
            )
        except Exception:
            pass
        # Restore last position/size if saved; otherwise use centered frame
        if not window.setFrameUsingName_("SyncLightMainWindow"):
            window.setFrame_display_(frame, True)

        config = WKWebViewConfiguration.alloc().init()
        ucc = config.userContentController()
        self.bridge = Bridge.alloc().initWithOwner_(self)
        ucc.addScriptMessageHandler_name_(self.bridge, "synclight")

        web = WKWebView.alloc().initWithFrame_configuration_(
            window.contentView().bounds(), config
        )
        web.setAutoresizingMask_(18)  # width+height flexible
        try:
            web.setUnderPageBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.043, 0.051, 0.063, 1.0)
            )
        except Exception:
            pass

        index = os.path.join(UI_DIR, "index.html")
        url = NSURL.fileURLWithPath_(index)
        base = NSURL.fileURLWithPath_(UI_DIR + "/")
        web.loadFileURL_allowingReadAccessToURL_(url, base)

        window.setContentView_(web)
        window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

        self.window = window
        self.web = web
        self.setup_status_item()

        # Push initial state on the AppKit main thread
        def _boot_state():
            self.push_state(self.boot_note or f"SyncLight {display_version()}")

        AppHelper.callLater(0.45, _boot_state)
        AppHelper.callLater(0.7, self._ensure_ble_auth)
        # Always watch for Safari / Yandex / Chrome fullscreen video
        AppHelper.callLater(1.0, self._start_fs_video_watch)

        AppHelper.runEventLoop()

    @python_method
    def _start_fs_video_watch(self) -> None:
        """Start Ambilight for everyday use; FS video only upgrades cinema profile."""
        try:
            from bt_auth import ensure_screen_access, open_screen_recording_settings

            # Quiet probe only — never CGRequest / Settings on every boot
            if not ensure_screen_access(prompt=False, open_settings=False):
                self.push_state("Включи SyncLight → Запись экрана (тумблер справа ON)")
                _bt_log("screen TCC missing — capture grey until toggle ON")
            else:
                _bt_log("screen TCC ok")
            # Always-on Ambilight (desktop + video). FS only switches cinema.
            if not self.ambi_on:
                threading.Thread(
                    target=self._start_ambilight_engine,
                    kwargs={"reason": "boot"},
                    daemon=True,
                    name="ambi-boot",
                ).start()
        except Exception as exc:
            _bt_log(f"screen probe: {exc}")
        try:
            self.watcher.start()
            _bt_log("fs-video watch on (Safari/Yandex/Chrome)")
        except Exception as exc:
            _bt_log(f"fs-video watch: {exc}")

    @python_method
    def _start_ambilight_engine(self, reason: str = "ui") -> None:
        """Reliable Ambilight start: USB edge sync + room 40%/60% mirror."""
        if self._shutting_down:
            return
        if self.ambi_on and self.ambi.running and reason != "ui":
            return
        try:
            try:
                self.room_fx.stop(join=False)
            except Exception:
                pass
            from bt_auth import (
                ensure_screen_access,
                open_screen_recording_settings,
                screen_permission_help,
            )

            # Never pop system/osascript dialogs here — SyncLight is often already
            # listed; Request spam was the «постоянно спрашивает разрешение» bug.
            ok = ensure_screen_access(prompt=False, open_settings=False)
            _bt_log(f"ambilight start begin reason={reason} screen={ok}")
            if not ok:
                self.push_state("Ambilight · включи SyncLight в «Запись экрана» (переключатель ON)")
                _bt_log("ambilight BLOCKED: no screen capture (toggle off?)")
                return
            # Boot: _ensure_ble_auth owns the first connect — parallel connects
            # make Bleak report «Bluetooth device is turned off».
            if self.mirror_room and reason != "boot":
                try:
                    self.bt.connect_bound()
                    self.bt.force_ambient()
                    self.bt.apply("bt", power=True)
                except Exception as exc:
                    _bt_log(f"ambi bt wake: {exc}")
            # Desktop by default; cinema only when already in FS / auto-fs
            try:
                cinema = reason == "auto-fs" or bool(
                    (self.context_info or {}).get("cinematic")
                )
                self.ambi.set_cinematic(cinema, force=True)
            except Exception:
                pass
            if not self.ambi.running:
                self.ambi.start()
            self.ambi_on = True
            # Never auto-stop on FS leave — Ambilight stays until user toggles off
            self._ambi_auto_fs = False
            self._fs_status_latched = bool(cinema)
            self._start_room_ambi()
            try:
                self.watcher.start()
            except Exception:
                pass
            # Soft wake so strips visibly change
            try:
                from device import shared as usb

                if not usb.open:
                    usb.connect(release_driver=True)
                warm, cool = (255, 90, 40), (40, 100, 255)
                usb.send_sync_frame(leds_from_room_colors(warm, cool))
                if self.mirror_room and reason != "boot":
                    self.bt.force_ambient()
                    self.bt.set_ambient(warm, cool, force=True)
            except Exception as exc:
                _bt_log(f"ambi handshake: {exc}")
            if cinema:
                msg = "Ambilight ON · FS видео"
            elif self.mirror_room:
                msg = "Ambilight ON · монитор+комната"
            else:
                msg = "Ambilight ON · только USB"
            self.push_state(msg)
            try:
                AppHelper.callAfter(self.refresh_status_menu)
            except Exception:
                pass
            _bt_log(
                f"ambilight STARTED reason={reason} "
                f"running={self.ambi.running} mirror={self.mirror_room}"
            )
        except Exception as exc:
            _bt_log(f"ambilight start FAIL: {exc}")
            self.push_state(f"Ambilight ошибка: {exc}")

    @python_method
    def _ensure_ble_auth(self):
        def _warm():
            from bt_auth import ble_error_hint, ensure_screen_access

            # Quiet probe — never Request / open Settings from BLE warm-up.
            ensure_screen_access(prompt=False, open_settings=False)

            last = None
            for attempt in range(1, 4):
                try:
                    if attempt > 1:
                        try:
                            self.bt.restart_worker()
                        except Exception:
                            pass
                        time.sleep(0.5)
                    st = self.bt.status()
                    bound = any(
                        (st.get(r) or {}).get("bound") for r in ("ceiling", "floor")
                    )
                    if not bound:
                        self.push_state("Назначь потолок и пол во вкладке Комната BT")
                        return
                    online0 = sum(
                        1
                        for r in ("ceiling", "floor")
                        if (st.get(r) or {}).get("connected")
                    )
                    if online0 >= 2 and attempt == 1:
                        # Both strips already up — don't thrash Bleak
                        self.mirror_room = True
                        self.push_state(
                            f"BT: уже online {online0}/2", refresh_bt=True
                        )
                        return
                    self.push_state(f"BT: подключаю комнату… ({attempt}/3)")
                    note = self.bt.connect_bound(force=(attempt > 1 or online0 < 2))
                    st2 = self.bt.status()
                    online = sum(
                        1
                        for r in ("ceiling", "floor")
                        if (st2.get(r) or {}).get("connected")
                    )
                    self.mirror_room = True
                    if online < 2:
                        raise RuntimeError(note or "ленты offline после connect")
                    self.push_state(f"BT: {note} · online {online}/2", refresh_bt=True)
                    return
                except Exception as exc:
                    last = exc
                    # Soft message — never open Privacy panes on retries
                    self.push_state(
                        f"BT повтор {attempt}/3: {ble_error_hint(exc, open_settings=False)}",
                        refresh_bt=True,
                    )
                    time.sleep(0.7 * attempt)
            if last is not None:
                self.push_state(
                    ble_error_hint(last, open_settings=False), refresh_bt=True
                )

        threading.Thread(target=_warm, daemon=True).start()

    @python_method
    def setup_status_item(self):
        self.menu_target = MenuTarget.alloc().initWithOwner_(self)
        item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = item.button()
        if button is not None:
            # Prefer SF Symbol template image; fall back to text
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "light.max", "SyncLight"
            )
            if img is not None:
                img.setTemplate_(True)
                button.setImage_(img)
            else:
                button.setTitle_("SL")
            button.setToolTip_("SyncLight")

        menu = NSMenu.alloc().init()
        show = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show SyncLight", "showWindow:", ""
        )
        show.setTarget_(self.menu_target)
        menu.addItem_(show)

        ambi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Ambilight", "toggleAmbi:", ""
        )
        ambi.setTarget_(self.menu_target)
        menu.addItem_(ambi)
        self.ambi_menu_item = ambi

        menu.addItem_(NSMenuItem.separatorItem())

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "quitApp:", "q"
        )
        quit_item.setTarget_(self.menu_target)
        menu.addItem_(quit_item)

        item.setMenu_(menu)
        self.status_item = item
        self.refresh_status_menu()

    @python_method
    def refresh_status_menu(self):
        if self.ambi_menu_item is not None:
            title = "Ambilight: On" if self.ambi_on else "Ambilight: Off"
            self.ambi_menu_item.setTitle_(title)

    @python_method
    def show_window(self):
        if self.window is not None:
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
    @python_method
    def _on_ambi_frame(self, colors):
        """
        Dual-band ambilight:
          • USB monitor strip — already painted with fast/flash colours
          • Room BT — slow scene mood from AmbilightEngine (flashes gated out)
        """
        if (
            self._shutting_down
            or not self.ambi_on
            or not self.mirror_room
            or self.room_fx.running
        ):
            return
        # Prefer extractor mood: top 40% → ceiling, bottom 60% → floor
        top = getattr(self.ambi, "last_room_top", (0, 0, 0))
        bot = getattr(self.ambi, "last_room_bottom", (0, 0, 0))
        if top == (0, 0, 0) and bot == (0, 0, 0):
            try:
                top, bot = self.ambi.room_from_leds(colors or [])
            except Exception:
                return
        if top == (0, 0, 0) and bot == (0, 0, 0):
            return
        cinema = bool(getattr(self.ambi, "cinematic", False))
        # Skip near-neutral privacy/UI greys — they became «приглушённый белый»
        def _alive(c) -> bool:
            try:
                r, g, b = int(c[0]), int(c[1]), int(c[2])
            except Exception:
                return False
            mx, mn = max(r, g, b), min(r, g, b)
            if mx < 8:
                return False
            sat = (mx - mn) / max(1, mx)
            # Allow dark colorful; reject flat mid-grey plateau
            if sat < 0.06 and 70 <= mx <= 130 and abs(r - g) <= 4 and abs(g - b) <= 4:
                return False
            return True

        if not _alive(top) and not _alive(bot):
            return
        # track_luma: dark scenes stay dim, bright scenes punch — bri follows video
        if cinema:
            top = lift_for_leds(
                top, min_l=0.06, chroma_gain=2.05, punch=0, track_luma=True
            )
            bot = lift_for_leds(
                bot, min_l=0.05, chroma_gain=1.95, punch=0, track_luma=True
            )
        else:
            top = lift_for_leds(
                top, min_l=0.08, chroma_gain=1.65, punch=0, track_luma=True
            )
            bot = lift_for_leds(
                bot, min_l=0.07, chroma_gain=1.55, punch=0, track_luma=True
            )
        try:
            self.bt.set_ambient(top, bot)
            n = getattr(self, "_ambi_push_n", 0) + 1
            self._ambi_push_n = n
            if n == 1 or n % 60 == 0:
                tag = "cinema" if cinema else "desk"
                raw_t = getattr(self.ambi, "last_room_top", (0, 0, 0))
                grab_m = getattr(self.ambi, "last_grab_mean", (0, 0, 0))
                led_avg = getattr(self.ambi, "last_average", (0, 0, 0))
                src = getattr(self.ambi, "last_source", "") or f"mon={getattr(self.ambi,'monitor_index','?')}"
                _bt_log(
                    f"room paint #{n} [{tag}] {src} top={top} bot={bot} "
                    f"raw={raw_t} grab={grab_m} leds={led_avg}"
                )
        except Exception as exc:
            _bt_log(f"room ambi push: {exc}")
        if getattr(self, "bulb", None) is not None and self.bulb.configured():
            try:
                self.bulb.set_ambient(*top, min_interval=0.10)
            except Exception as exc:
                _bt_log(f"bulb ambi: {exc}")

    @python_method
    def _start_room_ambi(self) -> None:
        """Arm Mac→room mirror; colors stream from _on_ambi_frame each capture."""
        self._stop_room_ambi()
        if not self.mirror_room:
            return
        # Wake strips once so the first colors are visible
        def _wake():
            try:
                self.bt.connect_bound()
                self.bt.apply("bt", power=True)
                # Re-identify ceiling / floor: cyan = потолок, orange = пол
                try:
                    self.bt.apply("ceiling", color=(0, 200, 255))
                    time.sleep(0.35)
                    self.bt.apply("floor", color=(255, 120, 30))
                    time.sleep(0.35)
                    self.bt.apply("ceiling", color=(0, 200, 255))
                    time.sleep(0.25)
                    self.bt.apply("floor", color=(255, 120, 30))
                    _bt_log("room identify: ceiling=cyan floor=orange")
                    self.push_state("Потолок=голубой · Пол=оранжевый (если наоборот — свапни роли)")
                except Exception as exc:
                    _bt_log(f"room identify: {exc}")
                _bt_log("room ambi armed")
            except Exception as exc:
                _bt_log(f"room ambi wake: {exc}")

        threading.Thread(target=_wake, name="room-ambi-wake", daemon=True).start()

    @python_method
    def _stop_room_ambi(self) -> None:
        self._room_ambi_stop.set()
        t = self._room_ambi_thread
        self._room_ambi_thread = None
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=0.4)

    @python_method
    def _stop_scene_watch(self):
        self.scene_live = False
        try:
            self.watcher.stop()
        except Exception:
            pass

    @python_method
    def _sync_cinematic_ambi(self, info: dict) -> None:
        """
        Fullscreen video in Safari / Yandex / Chrome / players → MPEG ambilight.

        Auto-starts on enter; stops on leave if auto-armed. Manual Ambilight
        stays on and only switches the cinema sampling profile.
        """
        want = bool(info.get("cinematic"))
        was = bool(getattr(self, "_was_cinema", False))

        if want and not was:
            # Rising edge → enter theater
            self._was_cinema = True
            if self._ambi_auto_suppress:
                self._ambi_auto_suppress = False
            app = info.get("app") or "видео"
            if (
                not self.ambi_on
                and not self._ambi_auto_suppress
                and not self.room_fx.running
            ):
                self._arm_auto_fs_ambi(info)
            elif self.ambi_on:
                try:
                    self.ambi.set_cinematic(True, force=True)
                except Exception as exc:
                    _bt_log(f"cinema enter: {exc}")
                self._fs_status_latched = True
                self.push_state(f"Ambilight · FS видео · {app}")
                _bt_log(
                    f"cinema ENTER app={app} cover={info.get('fullscreen_cover')} "
                    f"letterbox={info.get('letterbox')} video_like={info.get('video_like')} "
                    f"motion={info.get('motion')}"
                )
                try:
                    self.bt.force_ambient()
                except Exception:
                    pass
        elif want and was:
            # Stay in theater — keep profile on (idempotent)
            if self.ambi_on and not getattr(self.ambi, "cinematic", False):
                try:
                    self.ambi.set_cinematic(True, force=True)
                except Exception:
                    pass
        elif not want and was:
            # Leave theater profile but KEEP Ambilight running for desktop
            self._was_cinema = False
            self._fs_status_latched = False
            app = info.get("app") or ""
            _bt_log(f"cinema LEAVE app={app} (ambi stays on)")
            if self.ambi_on:
                try:
                    self.ambi.set_cinematic(False, force=True)
                except Exception:
                    pass
                self.push_state("Ambilight ON")
            self._ambi_auto_suppress = False

    @python_method
    def _arm_auto_fs_ambi(self, info: dict) -> None:
        """Ensure Ambilight is on and switch to cinema for FS video."""
        app = info.get("app") or "браузер"
        _bt_log(
            f"auto-fs ambi ARM app={app} cover={info.get('fullscreen_cover')} "
            f"video_like={info.get('video_like')}"
        )
        if self.ambi_on:
            try:
                self.ambi.set_cinematic(True, force=True)
            except Exception:
                pass
            self.push_state(f"Ambilight ON · FS видео · {app}")
            return
        threading.Thread(
            target=self._start_ambilight_engine,
            kwargs={"reason": "auto-fs"},
            daemon=True,
            name="ambi-fs",
        ).start()
        self.push_state(f"Ambilight ON · FS видео · {app}")

    @python_method
    def _disarm_auto_fs_ambi(self) -> None:
        """Stop ambilight that was auto-started for fullscreen video."""
        try:
            self._stop_room_ambi()
            self.ambi.stop()
            self.ambi_on = False
            try:
                self.ambi.set_cinematic(False)
            except Exception:
                pass
            self._ambi_auto_fs = False
            self.push_state("Ambilight · FS выкл")
            AppHelper.callAfter(self.refresh_status_menu)
            _bt_log("auto-fs ambi OFF")
        except Exception as exc:
            _bt_log(f"auto-fs disarm: {exc}")

    @python_method
    def _on_context(self, info: dict):
        self.context_info = info or {}
        # Always adapt ambilight sampling when theater / fullscreen video appears
        self._sync_cinematic_ambi(info)
        if not self.scene_live:
            return
        with self._scene_lock:
            sid = self.scene_id
            if sid == "circadian":
                rgb, bri, phase = circadian_color()
                key = (phase, bri)
                if key == self._last_auto_key:
                    return
                self._last_auto_key = key
                self._apply_scene_visual(
                    color=rgb,
                    brightness=bri,
                    usb_mode="static",
                    mirror=False,
                    status=f"Сутки · {phase}",
                )
                return
            if sid != "auto":
                return
            act, scene = resolve_auto(
                info.get("app") or "",
                info.get("bundle") or "",
                tuple(info.get("screen") or (128, 128, 128)),
                float(info.get("motion") or 0),
                fullscreen=bool(info.get("fullscreen")),
                cinematic=bool(info.get("cinematic")),
                video_app=bool(info.get("video_app")),
            )
            key = (act, scene.usb_mode, bool(info.get("cinematic")))
            if key == self._last_auto_key:
                return
            self._last_auto_key = key
            app = info.get("app") or "—"
            tag = " · FS" if info.get("cinematic") or info.get("fullscreen") else ""
            self._apply_resolved_scene(
                scene, status=f"Авто ИИ · {scene.title} · {app}{tag}"
            )

    @python_method
    def _apply_resolved_scene(self, scene, status: str = ""):
        color = scene.color or (200, 200, 220)
        self._apply_scene_visual(
            color=color,
            brightness=scene.brightness,
            usb_mode=scene.usb_mode,
            effect_index=scene.effect_index,
            sound_index=scene.sound_index,
            room_effect=scene.room_effect,
            mirror=scene.mirror,
            accent_floor=scene.accent_floor,
            status=status or f"Сцена · {scene.title}",
        )

    @python_method
    def _apply_scene_visual(
        self,
        *,
        color,
        brightness: int,
        usb_mode: str = "static",
        effect_index: int = 0,
        sound_index: int = 0,
        room_effect=None,
        mirror: bool = False,
        accent_floor=None,
        status: str = "",
    ):
        self.color = tuple(int(c) for c in color)
        self.brightness = int(brightness)
        self.mirror_room = bool(mirror)
        # Scenes always drive the whole setup (monitor + room), not the zone chip.
        t = "all"

        # Stop previous ambi unless staying in ambi
        if usb_mode != "ambi" and self.ambi_on:
            self._stop_room_ambi()
            self.ambi.stop()
            self.ambi_on = False

        # Room FX (Mac animation) — stop unless this scene needs it
        if usb_mode not in ("effect", "sound"):
            try:
                self.room_fx.stop(join=False)
            except Exception:
                pass

        try:
            if usb_mode == "ambi":
                try:
                    self.room_fx.stop(join=False)
                except Exception:
                    pass
                if not self.ambi_on:
                    if self.mirror_room:
                        try:
                            self.bt.connect_bound()
                        except Exception:
                            pass
                    self.ambi.start()
                    self.ambi_on = True
                    self._start_room_ambi()
                elif self.mirror_room:
                    self._start_room_ambi()
                else:
                    self._stop_room_ambi()
            elif usb_mode == "effect":
                # One Mac FX drives USB monitor + room together
                fx_idx = int(room_effect if room_effect is not None else effect_index)
                self._start_room_fx(fx_idx, base=self.color, paint_usb=True)
            elif usb_mode == "sound":
                fx_idx = int(room_effect if room_effect is not None else 5)
                self._start_room_fx(fx_idx, base=self.color, paint_usb=True)
            else:
                msg = apply_color("all", *self.color, brightness=self.brightness)
                _bt_log(f"scene static: {msg}")
                if accent_floor:
                    try:
                        self.bt.apply(
                            "floor",
                            color=tuple(accent_floor),
                            brightness=self.brightness,
                        )
                    except Exception as exc:
                        _bt_log(f"scene accent floor: {exc}")
        except Exception as exc:
            self.push_state(f"Сцена: {exc}")
            return

        AppHelper.callAfter(self.refresh_status_menu)
        self.push_state(status or f"Сцена · {t}")

    @python_method
    def _paint_usb_sync(self, top, bot) -> None:
        """Paint monitor strip from the same ceiling/floor colors as the room."""
        try:
            if not usb_shared.open:
                usb_shared.connect(release_driver=True)
            leds = leds_from_room_colors(tuple(top), tuple(bot))
            usb_shared.send_sync_frame(leds)
        except Exception as exc:
            _bt_log(f"usb sync paint: {exc}")

    @python_method
    def _push_fx_pair(self, top, bot, *, do_usb: bool, do_room: bool) -> None:
        """
        Coalesce monitor+room to the latest pair so both advance together
        without HID/BLE queue buildup (no stutter / speed mismatch).

        Room BLE is kicked first; USB HID waits a hair so both land closer
        in time (HID was racing ahead of the strips).
        """
        if not hasattr(self, "_fx_pair_lock"):
            self._fx_pair_lock = threading.Lock()
            self._fx_pair_pending = None
            self._fx_pair_pump = False
            self._fx_follow_top = OklabFollower(tau_s=0.04)
            self._fx_follow_bot = OklabFollower(tau_s=0.04)
        with self._fx_pair_lock:
            self._fx_pair_pending = (tuple(top), tuple(bot), do_usb, do_room)
            start = not self._fx_pair_pump
            if start:
                self._fx_pair_pump = True
        if not start:
            return

        def _pump():
            while True:
                with self._fx_pair_lock:
                    pending = self._fx_pair_pending
                    self._fx_pair_pending = None
                    if pending is None:
                        # Re-check under lock to avoid dropping a just-queued frame
                        if self._fx_pair_pending is not None:
                            continue
                        self._fx_pair_pump = False
                        return
                top_c, bot_c, paint_u, paint_r = pending
                # OKLab RC-lag: perceptually uniform, exact e^{−Δt/τ} coefficient
                top_s = self._fx_follow_top.update(top_c)
                bot_s = self._fx_follow_bot.update(bot_c)
                # BLE first so room write is already in flight when HID paints
                if paint_r:
                    try:
                        self.bt.set_ambient(top_s, bot_s)
                    except Exception as exc:
                        _bt_log(f"fx room: {exc}")
                if paint_u:
                    if paint_r:
                        time.sleep(0.022)
                    self._paint_usb_sync(top_s, bot_s)
                if (
                    paint_r
                    and getattr(self, "bulb", None) is not None
                    and self.bulb.configured()
                ):
                    try:
                        self.bulb.set_ambient(*top_s, min_interval=0.06)
                    except Exception:
                        pass

        threading.Thread(target=_pump, name="fx-pair", daemon=True).start()

    @python_method
    def _start_room_fx(self, effect_index: int, base=None, paint_usb: bool = True) -> None:
        """Mac animation for Light/AI effects — room + USB stay in sync."""
        self._room_fx_idx = int(effect_index)
        self._stop_room_ambi()
        if not hasattr(self, "_fx_follow_top"):
            self._fx_follow_top = OklabFollower(tau_s=0.04)
            self._fx_follow_bot = OklabFollower(tau_s=0.04)
        self._fx_follow_top.reset()
        self._fx_follow_bot.reset()
        t = normalize_target(self.target)
        # Light scenarios: always sync monitor↔room unless zone is room-only
        do_usb = bool(paint_usb) and t in ("usb", "all")
        do_room = t in ("ceiling", "floor", "bt", "all", "usb", "bulb")
        try:
            if do_room:
                self.bt.connect_bound()
        except Exception as exc:
            _bt_log(f"room fx connect: {exc}")
        try:
            self.bt.force_ambient()
        except Exception:
            pass
        if do_usb:
            try:
                if not usb_shared.open:
                    usb_shared.connect(release_driver=True)
            except Exception as exc:
                _bt_log(f"usb fx connect: {exc}")

        buf: dict = {}

        def _apply(role: str, rgb) -> None:
            try:
                if role == "bulb":
                    return  # bulb follows in _push_fx_pair with ceiling tint
                buf[role] = tuple(rgb)
                if "ceiling" in buf and "floor" in buf:
                    top = buf.pop("ceiling")
                    bot = buf.pop("floor")
                    self._push_fx_pair(top, bot, do_usb=do_usb, do_room=do_room)
            except Exception as exc:
                _bt_log(f"room fx {role}: {exc}")

        self.room_fx.start(
            apply=_apply,
            effect_index=int(effect_index),
            base_color=tuple(base) if base else self.color,
            brightness=self.brightness,
            speed=self.speed,
            split_roles=True,
        )
        _bt_log(f"room fx started idx={effect_index} usb={do_usb} room={do_room}")

    @python_method
    def push_state(self, status=None, refresh_bt: bool = False):
        # Default: cached status (non-blocking). refresh_bt=True after connect/scan.
        if refresh_bt:
            try:
                self.bt.status()
            except Exception:
                pass
        ctx = self.context_info or {}
        state = {
            "brightness": self.brightness,
            "speed": self.speed,
            "autostart": autostart_enabled(),
            "target": self.target,
            "mirror_room": self.mirror_room,
            "ambi": self.ambi_on,
            "ambi_monitor": int(getattr(self.ambi, "monitor_index", 1) or 1),
            "displays": list_displays(),
            "bt": getattr(self.bt, "last_status", None) or {},
            "bulb": self.bulb.status() if getattr(self, "bulb", None) else {},
            "version": version_dict(),
            "version_label": display_version(),
            "ai_scene": self.scene_id,
            "ai_live": self.scene_live,
            "ai_context": {
                "app": ctx.get("app") or "",
                "motion": round(float(ctx.get("motion") or 0), 2),
            },
        }
        if not self._ui_scenes_sent:
            state["ai_scenes"] = scenes_payload()
            self._ui_scenes_sent = True
        if status:
            state["status"] = status
        js = f"window.__synclightSet && window.__synclightSet({json.dumps(state, ensure_ascii=False)});"

        def _eval():
            if self.web is not None:
                self.web.evaluateJavaScript_completionHandler_(js, None)

        AppHelper.callAfter(_eval)

    @python_method
    def handle_message(self, payload: dict):
        action = str(payload.get("action", ""))
        if "target" in payload:
            self.target = normalize_target(payload.get("target"))

        def work():
            try:
                self._dispatch(action, payload)
                self._persist()
            except Exception as exc:
                try:
                    from bt_auth import ble_error_hint

                    self.push_state(ble_error_hint(exc))
                except Exception:
                    self.push_state(str(exc))

        threading.Thread(target=work, daemon=True).start()

    @python_method
    def _dispatch(self, action: str, payload: dict):
        if action == "set_target":
            self.target = normalize_target(payload.get("target", self.target))
            # Persist so «Все» sticks across restarts
            try:
                self.cfg["target"] = self.target
                save_settings(self.cfg)
            except Exception:
                pass
            self.push_state(f"Цель: {self.target}")
            return

        if action == "bt_scan":
            self.push_state("Сканирование…")
            found = self.bt.scan(timeout=8.0)
            self.push_state(f"Найдено устройств: {len(found)}", refresh_bt=True)
            return

        if action == "bt_status":
            self.push_state(refresh_bt=True)
            return

        if action == "bt_connect":
            force = bool(payload.get("force"))
            self.push_state("BT: подключаю…")
            msg = self.bt.connect_bound(force=force)
            st = self.bt.status()
            online = sum(
                1 for r in ("ceiling", "floor") if (st.get(r) or {}).get("connected")
            )
            if online == 0:
                # One hard retry
                msg = self.bt.connect_bound(force=True)
                st = self.bt.status()
                online = sum(
                    1 for r in ("ceiling", "floor") if (st.get(r) or {}).get("connected")
                )
            self.push_state(f"BT: {msg} · online {online}/2", refresh_bt=True)
            return

        if action == "bt_assign":
            role = str(payload.get("role", ""))
            address = str(payload.get("address", ""))
            name = str(payload.get("name", ""))
            protocol = str(payload.get("protocol", "lednetwf"))
            self.bt.assign(role, address, name=name, protocol=protocol)
            label = "Потолок" if role == "ceiling" else "Пол"
            self.push_state(f"{label}: {name or address}")
            return

        if action == "bt_rename":
            address = str(payload.get("address", "")).strip()
            label = str(payload.get("label", "")).strip()
            if not address:
                self.push_state("Нет адреса для переименования")
                return
            self.bt.rename(address, label)
            self.push_state(f"Имя: {label or 'сброшено'}", refresh_bt=True)
            return

        if action == "bt_add":
            role = str(payload.get("role", "ceiling"))
            address = str(payload.get("address", "")).strip()
            name = str(payload.get("name", "")).strip()
            protocol = str(payload.get("protocol", "lednetwf") or "lednetwf")
            if not address:
                self.push_state("Укажи BLE-адрес устройства")
                return
            if name:
                self.bt.rename(address, name)
            self.bt.assign(role, address, name=name, protocol=protocol)
            where = "Потолок" if role == "ceiling" else "Пол"
            self.push_state(f"Добавлено → {where}: {name or address}", refresh_bt=True)
            return

        if action == "bt_clear":
            role = str(payload.get("role", ""))
            self.bt.clear_role(role)
            where = "Потолок" if role == "ceiling" else "Пол"
            self.push_state(f"{where}: сброшен", refresh_bt=True)
            return

        if action == "bt_swap":
            note = self.bt.swap_roles()
            self.push_state(f"{note} · готово")
            return

        if action == "bt_test_rgb":
            def _flash():
                try:
                    for name, rgb in (
                        ("красный", (255, 0, 0)),
                        ("зелёный", (0, 255, 0)),
                        ("синий", (0, 0, 255)),
                    ):
                        self.bt.apply("bt", color=rgb, brightness=220, power=True)
                        try:
                            if self.bulb.configured():
                                self.bulb.set_color(*rgb, brightness=220)
                        except Exception:
                            pass
                        self.push_state(f"Тест: {name}")
                        time.sleep(1.2)
                    self.push_state("Тест R/G/B готов — цвета должны совпасть", refresh_bt=True)
                except Exception as exc:
                    self.push_state(f"Тест цветов: {exc}", refresh_bt=True)

            threading.Thread(target=_flash, daemon=True).start()
            return

        if action == "bulb_scan":
            self.push_state("Ищу Яндекс/Tuya лампу в Wi‑Fi…")
            try:
                found = scan_yandex_lan(timeout=7.0)
            except Exception as exc:
                self.push_state(f"Скан лампы: {exc}")
                return
            if not found:
                self.push_state("Лампа не найдена в сети (должна быть в той же Wi‑Fi)")
                return
            # Prefer first device; keep existing local_key
            d0 = found[0]
            prev_key = self.bulb.local_key
            self.bulb.configure(
                ip=d0["ip"],
                device_id=d0["id"],
                local_key=prev_key,
                version=d0.get("version") or "3.3",
                name=d0.get("name") or "Яндекс-лампа",
                enabled=True,
            )
            self.cfg["yandex_bulb"] = self.bulb.dump()
            save_settings(self.cfg)
            if prev_key:
                try:
                    st = self.bulb.status()
                    ok = st.get("connected")
                except Exception:
                    ok = False
                self.push_state(
                    f"Лампа {d0['ip']} · {'online' if ok else 'ключ не подошёл'}",
                    refresh_bt=True,
                )
            else:
                self.push_state(
                    f"Найдена {d0['ip']} · вставь Local Key и «Сохранить»",
                    refresh_bt=True,
                )
            return

        if action == "bulb_save":
            self.bulb.configure(
                ip=str(payload.get("ip") or self.bulb.ip),
                device_id=str(payload.get("device_id") or self.bulb.device_id),
                local_key=str(payload.get("local_key") or ""),
                version=str(payload.get("version") or self.bulb.version or "3.3"),
                name=str(payload.get("name") or self.bulb.name),
                enabled=bool(payload.get("enabled", True)),
            )
            self.cfg["yandex_bulb"] = self.bulb.dump()
            save_settings(self.cfg)
            try:
                st = self.bulb.status()
                if st.get("connected"):
                    self.push_state(f"Лампа online · {self.bulb.ip}", refresh_bt=True)
                else:
                    self.push_state(
                        f"Сохранено · {st.get('last_error') or 'проверь Local Key'}",
                        refresh_bt=True,
                    )
            except Exception as exc:
                self.push_state(f"Лампа: {exc}", refresh_bt=True)
            return

        if action == "bulb_test":
            def _bulb_flash():
                try:
                    if not self.bulb.configured():
                        self.push_state("Сначала сохрани IP + Local Key лампы")
                        return
                    self.bulb.set_color(255, 80, 40, brightness=200)
                    time.sleep(0.8)
                    self.bulb.set_color(40, 180, 255, brightness=200)
                    time.sleep(0.8)
                    self.bulb.set_color(255, 200, 100, brightness=180)
                    self.push_state("Лампа: тест цвета ok", refresh_bt=True)
                except Exception as exc:
                    self.push_state(f"Лампа тест: {exc}", refresh_bt=True)

            threading.Thread(target=_bulb_flash, daemon=True).start()
            return

        if action == "bulb_cloud_key":
            cloud = self.cfg.get("tuya_cloud") or {}
            yb = self.cfg.get("yandex_bulb") or {}
            access_id = str(
                payload.get("access_id")
                or cloud.get("access_id")
                or yb.get("access_id")
                or ""
            )
            access_secret = str(
                payload.get("access_secret")
                or cloud.get("access_secret")
                or yb.get("access_secret")
                or ""
            )
            region = str(
                payload.get("region")
                or cloud.get("region")
                or yb.get("data_center")
                or "eu"
            )
            self.push_state("Тяну Local Key из Tuya Cloud…")

            def _pull():
                try:
                    res = fetch_local_key_cloud(
                        access_id=access_id,
                        access_secret=access_secret,
                        device_id=self.bulb.device_id,
                        region=region,
                    )
                    if not res.get("ok"):
                        self.push_state(
                            str(res.get("error") or "Cloud: нет ключа"),
                            refresh_bt=True,
                        )
                        return
                    key = str(res["local_key"])
                    did = str(res.get("device_id") or self.bulb.device_id)
                    self.bulb.configure(
                        ip=self.bulb.ip,
                        device_id=did,
                        local_key=key,
                        version=self.bulb.version,
                        name=str(res.get("name") or self.bulb.name),
                        enabled=True,
                    )
                    self.cfg["yandex_bulb"] = self.bulb.dump()
                    self.cfg.setdefault("tuya_cloud", {})
                    self.cfg["tuya_cloud"].update(
                        {
                            "access_id": access_id,
                            "access_secret": access_secret,
                            "region": region,
                        }
                    )
                    save_settings(self.cfg)
                    st = self.bulb.status()
                    self.push_state(
                        f"Ключ получен · {'online' if st.get('connected') else st.get('last_error') or 'ok'}",
                        refresh_bt=True,
                    )
                except Exception as exc:
                    self.push_state(f"Cloud key: {exc}", refresh_bt=True)

            threading.Thread(target=_pull, daemon=True).start()
            return

        if action == "mirror_room":
            self.mirror_room = bool(payload.get("enabled"))
            if self.ambi_on:
                if self.mirror_room:
                    self._start_room_ambi()
                else:
                    self._stop_room_ambi()
            self.push_state(
                "Mac→комната вкл" if self.mirror_room else "Зеркало комнаты выкл"
            )
            return

        if action == "ambi_monitor":
            idx = int(payload.get("index") or 1)
            displays = list_displays()
            valid = {int(d["index"]) for d in displays} or {1}
            if idx not in valid:
                idx = min(valid)
            self.ambi.set_monitor(idx)
            self.cfg["ambi_monitor"] = idx
            save_settings(self.cfg)
            label = next(
                (d["label"] for d in displays if int(d["index"]) == idx),
                f"Дисплей {idx}",
            )
            self.push_state(f"Ambilight · {label}")
            return

        if action == "ai_scene":
            sid = str(payload.get("id") or "")
            # Off main/WebKit thread — BT apply must not freeze UI
            threading.Thread(
                target=self._activate_scene, args=(sid,), daemon=True, name="ai-scene"
            ).start()
            return

        if action == "ai_stop":
            self._stop_scene_watch()
            self.scene_id = ""
            self.push_state("AI-сцены выкл")
            return

        if action == "ambi":
            self._stop_scene_watch()
            enabled = bool(payload.get("enabled"))
            if "mirror" in payload:
                self.mirror_room = bool(payload.get("mirror"))
            if enabled and not self.ambi_on:
                self._ambi_auto_suppress = False
                threading.Thread(
                    target=self._start_ambilight_engine,
                    kwargs={"reason": "ui"},
                    daemon=True,
                    name="ambi-start",
                ).start()
                return
            elif not enabled and self.ambi_on:
                self._stop_room_ambi()
                self.ambi.stop()
                self.ambi_on = False
                # If FS video still on, don't auto-restart until they leave FS
                if self._ambi_auto_fs or bool(
                    (self.context_info or {}).get("cinematic")
                ):
                    self._ambi_auto_suppress = True
                self._ambi_auto_fs = False
                try:
                    self.ambi.set_cinematic(False)
                except Exception:
                    pass
                # Keep fs-video watcher running for Safari/Yandex re-entry
                self.push_state("Ambilight выкл")
            AppHelper.callAfter(self.refresh_status_menu)
            return

        if action in ("on", "off", "effect", "sound", "color"):
            self._stop_scene_watch()
            self.scene_id = ""

        if self.ambi_on and action in ("on", "off", "effect", "sound", "color"):
            self._stop_room_ambi()
            self.ambi.stop()
            self.ambi_on = False

        # Static / power leave Mac room animation
        if action in ("on", "off", "color") and self.room_fx.running:
            try:
                self.room_fx.stop(join=False)
            except Exception:
                pass

        if action == "on":
            msg = apply_power(self.target, True, self.color, self.brightness)
            self.push_state(f"On · {msg}")
        elif action == "off":
            msg = apply_power(self.target, False)
            self.push_state(f"Off · {msg}")
        elif action == "brightness":
            self.brightness = int(payload.get("value", self.brightness))
            if self.room_fx.running:
                # Restart FX so brightness scaling updates
                self._start_room_fx(getattr(self, "_room_fx_idx", 0), base=self.color)
                self.push_state(f"Brightness {self.brightness}")
            else:
                msg = apply_brightness(self.target, self.brightness)
                self.push_state(f"Brightness {self.brightness} · {msg}")
        elif action == "speed":
            self.speed = int(payload.get("value", self.speed))
            # Firmware speed is USB-only
            if normalize_target(self.target) in ("usb", "all"):
                oneshot(set_dynamic_speed(self.speed), settle_ms=120)
            if self.room_fx.running:
                self._start_room_fx(getattr(self, "_room_fx_idx", 0), base=self.color)
            self.push_state(f"Speed {self.speed}")
        elif action == "effect":
            idx = int(payload.get("index", 0))
            self.brightness = int(payload.get("brightness", self.brightness))
            self.speed = int(payload.get("speed", self.speed))
            t = normalize_target(self.target)
            # One Mac FX → USB monitor + room (no separate firmware effect)
            if t in ("ceiling", "floor", "bt", "all", "usb", "bulb"):
                self._start_room_fx(idx, base=self.color, paint_usb=True)
            else:
                try:
                    self.room_fx.stop(join=False)
                except Exception:
                    pass
            self.push_state("Эффект · монитор+комната")
        elif action == "sound":
            idx = int(payload.get("index", 0))
            self.brightness = int(payload.get("brightness", self.brightness))
            self.speed = int(payload.get("speed", self.speed))
            self.sensitivity = int(payload.get("sensitivity", self.sensitivity))
            t = normalize_target(self.target)
            if t in ("ceiling", "floor", "bt", "all", "usb", "bulb"):
                self._start_room_fx(5 if idx == 0 else idx, base=self.color, paint_usb=True)
            self.push_state("Музыка · монитор+комната")
        elif action == "sensitivity":
            self.sensitivity = int(payload.get("value", self.sensitivity))
            oneshot(set_sound_sensitivity(self.sensitivity), settle_ms=120)
            self.push_state(f"Sensitivity {self.sensitivity}")
        elif action == "color":
            name = str(payload.get("name", "warm")).lower()
            if name in PRESETS:
                self.color = PRESETS[name]
                open(STATE_FILE, "w").write("%d %d %d\n" % self.color)
                bri = int(payload.get("brightness", self.brightness))
                self.brightness = bri
                msg = apply_color(self.target, *self.color, brightness=bri)
                self.push_state(f"{name.title()} · {msg}")
        elif action == "autostart":
            enabled = bool(payload.get("enabled"))
            # Launch glass GUI at login (not the old dialog UI)
            self._set_autostart_glass(enabled)
            self.push_state("Autostart on" if enabled else "Autostart off")

    @python_method
    def _set_autostart_glass(self, enabled: bool):
        plist = os.path.expanduser(
            "~/Library/LaunchAgents/com.robobloq.synclight.gui.plist"
        )
        if enabled:
            os.makedirs(os.path.dirname(plist), exist_ok=True)
            body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.robobloq.synclight.gui</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>{os.path.join(ROOT, "glass_gui.py")}</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
"""
            with open(plist, "w") as f:
                f.write(body)
            subprocess.run(["launchctl", "unload", plist], capture_output=True)
            subprocess.run(["launchctl", "load", "-w", plist], capture_output=True)
        else:
            if os.path.exists(plist):
                subprocess.run(["launchctl", "unload", plist], capture_output=True)
                try:
                    os.remove(plist)
                except OSError:
                    pass

    @python_method
    def _activate_scene(self, sid: str):
        scene = get_scene(sid)
        if scene is None:
            self.push_state(f"Неизвестная сцена: {sid}")
            return
        self.scene_id = sid
        self._last_auto_key = None
        if scene.kind in ("auto", "circadian"):
            self.scene_live = True
            self.watcher.start()
            # Immediate tick
            if scene.kind == "circadian":
                rgb, bri, phase = circadian_color()
                self._apply_scene_visual(
                    color=rgb,
                    brightness=bri,
                    usb_mode="static",
                    status=f"Сутки · {phase}",
                )
            else:
                from context_watch import (
                    detect_fullscreen,
                    frontmost_app,
                    looks_like_browser,
                    looks_like_player,
                    looks_like_video_app,
                    sample_letterbox_bars,
                    sample_screen_mood,
                )

                name, bid, pid = frontmost_app()
                avg, motion = sample_screen_mood()
                browser = looks_like_browser(name, bid)
                player = looks_like_player(name, bid)
                video_app = looks_like_video_app(name, bid) or browser
                fs = detect_fullscreen(pid, browser=browser)
                bars = sample_letterbox_bars() if (browser or player) else False
                lum = (avg[0] + avg[1] + avg[2]) / 3.0
                fs_on = bool(fs.get("fullscreen"))
                cover = float(fs.get("cover") or 0)
                cinematic = (
                    (player and fs_on)
                    or (
                        browser
                        and fs_on
                        and (
                            bars
                            or motion > 0.06
                            or (cover >= 0.97 and lum < 130)
                            or (cover >= 0.99 and motion > 0.02)
                        )
                    )
                    or (fs_on and bars)
                )
                info = {
                    "app": name,
                    "bundle": bid,
                    "pid": pid,
                    "screen": avg,
                    "motion": motion,
                    "fullscreen": fs_on,
                    "cinematic": cinematic,
                    "video_app": video_app,
                    "browser": browser,
                    "browser_fs": bool(fs.get("browser_fs")),
                    "letterbox": bool(fs.get("letterbox_hint")) or bars,
                }
                self.context_info = info
                self._sync_cinematic_ambi(info)
                act, resolved = resolve_auto(
                    name,
                    bid,
                    avg,
                    motion,
                    fullscreen=bool(fs.get("fullscreen")),
                    cinematic=cinematic,
                    video_app=video_app,
                )
                self._last_auto_key = (act, resolved.usb_mode, cinematic)
                tag = " · FS" if cinematic or fs.get("fullscreen") else ""
                self._apply_resolved_scene(
                    resolved,
                    status=f"Авто ИИ · {resolved.title} · {name or '—'}{tag}",
                )
            return

        # One-shot template / activity
        self._stop_scene_watch()
        self.scene_live = False
        self._apply_resolved_scene(scene, status=f"Сцена · {scene.title}")

    @python_method
    def _persist(self):
        self.cfg.update(
            {
                "brightness": self.brightness,
                "effect_speed": self.speed,
                "sensitivity": self.sensitivity,
                "color": list(self.color),
                "autostart_gui": autostart_enabled(),
                "target": self.target,
                "mirror_room": self.mirror_room,
                "ambi_monitor": int(getattr(self.ambi, "monitor_index", 1) or 1),
                "ai_scene": self.scene_id,
                "yandex_bulb": self.bulb.dump() if getattr(self, "bulb", None) else self.cfg.get("yandex_bulb"),
                "tuya_cloud": self.cfg.get("tuya_cloud") or {},
            }
        )
        save_settings(self.cfg)


def main():
    boot = ensure_singleton("glass")
    app = GlassApp.alloc().init()
    app.boot_note = boot
    app.run()


if __name__ == "__main__":
    main()
