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
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSURL, NSDictionary  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402
from WebKit import (  # noqa: E402
    WKScriptMessage,
    WKWebView,
    WKWebViewConfiguration,
)
from objc import python_method, super as objc_super  # noqa: E402

from ambilight import AmbilightEngine  # noqa: E402
from device import (  # noqa: E402
    apply_dynamic_effect,
    apply_sound_effect,
    oneshot,
    set_brightness,
    set_dynamic_speed,
    set_section_led,
    set_sound_sensitivity,
)
from dialog_gui import autostart_enabled  # noqa: E402
from settings import load_settings, save_settings  # noqa: E402
from sl import PRESETS, STATE_FILE  # noqa: E402


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
        if self.owner.ambi_on:
            self.owner.ambi.stop()
        NSApp.terminate_(None)


class AppDelegate(NSObject):
    def initWithOwner_(self, owner):
        self = objc_super(AppDelegate, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
        # Keep menu-bar icon alive when the window is closed
        return False

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, flag):
        if not flag:
            self.owner.show_window()
        return True


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
        self.ambi = AmbilightEngine()
        self.ambi_on = False
        self.window = None
        self.web = None
        self.status_item = None
        self.menu_target = None
        self.ambi_menu_item = None
        return self

    def run(self):
        NSApplication.sharedApplication()
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        self.app_delegate = AppDelegate.alloc().initWithOwner_(self)
        NSApp.setDelegate_(self.app_delegate)

        try:
            from install_icons import main as install_icons_main

            install_icons_main()
        except Exception:
            pass

        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
            | NSWindowStyleMaskFullSizeContentView
        )
        frame = NSMakeRect(0, 0, 460, 760)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        window.setTitle_("SyncLight")
        window.setTitleVisibility_(NSWindowTitleHidden)
        window.setTitlebarAppearsTransparent_(True)
        window.setBackgroundColor_(NSColor.clearColor())
        window.setOpaque_(False)
        window.setMovableByWindowBackground_(True)
        window.setReleasedWhenClosed_(False)
        if NSScreen.mainScreen():
            window.center()

        config = WKWebViewConfiguration.alloc().init()
        ucc = config.userContentController()
        self.bridge = Bridge.alloc().initWithOwner_(self)
        ucc.addScriptMessageHandler_name_(self.bridge, "synclight")

        web = WKWebView.alloc().initWithFrame_configuration_(
            window.contentView().bounds(), config
        )
        web.setAutoresizingMask_(18)  # width+height flexible
        try:
            web.setUnderPageBackgroundColor_(NSColor.clearColor())
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
        AppHelper.callLater(0.5, self.push_state)

        AppHelper.runEventLoop()

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
    def push_state(self, status=None):
        state = {
            "brightness": self.brightness,
            "speed": self.speed,
            "autostart": autostart_enabled(),
        }
        if status:
            state["status"] = status
        js = f"window.__synclightSet && window.__synclightSet({json.dumps(state)});"

        def _eval():
            if self.web is not None:
                self.web.evaluateJavaScript_completionHandler_(js, None)

        AppHelper.callAfter(_eval)
    @python_method
    def handle_message(self, payload: dict):
        action = str(payload.get("action", ""))

        def work():
            try:
                self._dispatch(action, payload)
                self._persist()
            except Exception as exc:
                self.push_state(str(exc))

        threading.Thread(target=work, daemon=True).start()

    @python_method
    def _dispatch(self, action: str, payload: dict):
        if action == "ambi":
            enabled = bool(payload.get("enabled"))
            if enabled and not self.ambi_on:
                self.ambi.start()
                self.ambi_on = True
                self.push_state("Ambilight on")
            elif not enabled and self.ambi_on:
                self.ambi.stop()
                self.ambi_on = False
                self.push_state("Ambilight off")
            AppHelper.callAfter(self.refresh_status_menu)
            return

        if self.ambi_on and action in ("on", "off", "effect", "sound", "color"):
            self.ambi.stop()
            self.ambi_on = False

        if action == "on":
            oneshot(set_section_led(*self.color))
            oneshot(set_brightness(self.brightness), settle_ms=120)
            self.push_state("On")
        elif action == "off":
            oneshot(set_section_led(0, 0, 0))
            self.push_state("Off")
        elif action == "brightness":
            self.brightness = int(payload.get("value", self.brightness))
            oneshot(set_brightness(self.brightness), settle_ms=120)
            self.push_state(f"Brightness {self.brightness}")
        elif action == "speed":
            self.speed = int(payload.get("value", self.speed))
            oneshot(set_dynamic_speed(self.speed), settle_ms=120)
            self.push_state(f"Speed {self.speed}")
        elif action == "effect":
            idx = int(payload.get("index", 0))
            self.brightness = int(payload.get("brightness", self.brightness))
            self.speed = int(payload.get("speed", self.speed))
            apply_dynamic_effect(idx, speed=self.speed, brightness=self.brightness)
            self.push_state("Effect applied")
        elif action == "sound":
            idx = int(payload.get("index", 0))
            self.brightness = int(payload.get("brightness", self.brightness))
            self.speed = int(payload.get("speed", self.speed))
            self.sensitivity = int(payload.get("sensitivity", self.sensitivity))
            apply_sound_effect(
                idx,
                speed=self.speed,
                brightness=self.brightness,
                sensitivity=self.sensitivity,
            )
            self.push_state("Music effect")
        elif action == "sensitivity":
            self.sensitivity = int(payload.get("value", self.sensitivity))
            oneshot(set_sound_sensitivity(self.sensitivity), settle_ms=120)
            self.push_state(f"Sensitivity {self.sensitivity}")
        elif action == "color":
            name = str(payload.get("name", "warm")).lower()
            if name in PRESETS:
                self.color = PRESETS[name]
                open(STATE_FILE, "w").write("%d %d %d\n" % self.color)
                oneshot(set_section_led(*self.color))
                bri = int(payload.get("brightness", self.brightness))
                self.brightness = bri
                oneshot(set_brightness(bri), settle_ms=120)
                self.push_state(name.title())
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
    def _persist(self):
        self.cfg.update(
            {
                "brightness": self.brightness,
                "effect_speed": self.speed,
                "sensitivity": self.sensitivity,
                "color": list(self.color),
                "autostart_gui": autostart_enabled(),
            }
        )
        save_settings(self.cfg)


def main():
    # Single instance
    try:
        my = os.getpid()
        out = subprocess.check_output(["pgrep", "-f", "glass_gui.py"], text=True)
        for line in out.split():
            pid = int(line)
            if pid != my:
                subprocess.run(["kill", str(pid)], capture_output=True)
    except Exception:
        pass

    # Stop dialog GUI if running
    subprocess.run(["pkill", "-f", "dialog_gui.py"], capture_output=True)

    app = GlassApp.alloc().init()
    app.run()


if __name__ == "__main__":
    main()
