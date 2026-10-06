#!/usr/bin/env python3
"""
SyncLight — visible Cocoa control window for macOS.
"""

from __future__ import annotations

import threading

import rumps
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from objc import super as objc_super

from ambilight import AmbilightEngine
from device import oneshot, set_section_led
from sl import PRESETS, STATE_FILE


def load_color():
    try:
        parts = open(STATE_FILE).read().strip().split()
        return tuple(int(x) for x in parts)
    except Exception:
        return (255, 200, 100)


def save_color(r, g, b):
    open(STATE_FILE, "w").write(f"{r} {g} {b}\n")


class Controller(NSObject):
    def init(self):
        self = objc_super(Controller, self).init()
        if self is None:
            return None
        self.color = load_color()
        self.ambi = AmbilightEngine(on_error=self.ambi_error)
        self.ambi_on = False
        self._busy = False
        self.window = None
        self.status = None
        self.ambi_btn = None
        return self

    def buildWindow(self):
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(240, 240, 380, 460),
            style,
            NSBackingStoreBuffered,
            False,
        )
        window.setTitle_("SyncLight")
        window.setReleasedWhenClosed_(False)
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 380, 460))
        window.setContentView_(content)

        def label(text, y, size=14, bold=False):
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(24, y, 332, 28))
            field.setStringValue_(text)
            field.setBezeled_(False)
            field.setDrawsBackground_(False)
            field.setEditable_(False)
            field.setSelectable_(False)
            font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
            field.setFont_(font)
            field.setTextColor_(NSColor.labelColor())
            content.addSubview_(field)
            return field

        label("SyncLight", 410, size=22, bold=True)
        self.status = label("Ready — use the buttons below", 378, size=12)

        self.ambi_btn = NSButton.alloc().initWithFrame_(NSMakeRect(24, 320, 332, 40))
        self.ambi_btn.setTitle_("Start Ambilight")
        self.ambi_btn.setBezelStyle_(1)
        self.ambi_btn.setTarget_(self)
        self.ambi_btn.setAction_("toggleAmbi:")
        content.addSubview_(self.ambi_btn)

        on_btn = NSButton.alloc().initWithFrame_(NSMakeRect(24, 268, 100, 36))
        on_btn.setTitle_("On")
        on_btn.setBezelStyle_(1)
        on_btn.setTarget_(self)
        on_btn.setAction_("turnOn:")
        content.addSubview_(on_btn)

        off_btn = NSButton.alloc().initWithFrame_(NSMakeRect(140, 268, 100, 36))
        off_btn.setTitle_("Off")
        off_btn.setBezelStyle_(1)
        off_btn.setTarget_(self)
        off_btn.setAction_("turnOff:")
        content.addSubview_(off_btn)

        color_btn = NSButton.alloc().initWithFrame_(NSMakeRect(256, 268, 100, 36))
        color_btn.setTitle_("Color…")
        color_btn.setBezelStyle_(1)
        color_btn.setTarget_(self)
        color_btn.setAction_("pickColor:")
        content.addSubview_(color_btn)

        label("Presets", 230, size=12)
        names = list(PRESETS.keys())
        for i, name in enumerate(names):
            col = i % 4
            row = i // 4
            btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(24 + col * 84, 180 - row * 40, 76, 32)
            )
            btn.setTitle_(name.title())
            btn.setBezelStyle_(1)
            btn.setTag_(i)
            btn.setTarget_(self)
            btn.setAction_("preset:")
            content.addSubview_(btn)

        tip = label(
            "If Ambilight stays dark:\nSystem Settings → Privacy → Screen Recording → allow SyncLight/Python",
            40,
            size=11,
        )
        tip.setFrame_(NSMakeRect(24, 20, 332, 50))

        self.window = window
        window.center()
        window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        return window

    def set_status_text(self, text):
        if self.status:
            self.status.setStringValue_(text)

    def run_async(self, fn, ok="OK"):
        if self._busy:
            return

        def worker():
            self._busy = True
            try:
                fn()
                rumps.Timer(lambda _: self.set_status_text(ok), 0.01).start()
            except Exception as exc:
                msg = str(exc)
                rumps.Timer(lambda _: self.set_status_text(msg), 0.01).start()
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    def turnOn_(self, _sender):
        if self.ambi_on:
            self.stop_ambi()
        r, g, b = self.color
        self.run_async(lambda: oneshot(set_section_led(r, g, b)), f"On rgb({r},{g},{b})")

    def turnOff_(self, _sender):
        if self.ambi_on:
            self.stop_ambi()
        self.run_async(lambda: oneshot(set_section_led(0, 0, 0)), "Off")

    def pickColor_(self, _sender):
        response = rumps.Window(
            message="RGB as: R G B   (example: 255 128 0)",
            title="SyncLight color",
            default_text="%d %d %d" % self.color,
            ok="Set",
            cancel="Cancel",
            dimensions=(280, 24),
        ).run()
        if not response.clicked:
            return
        try:
            r, g, b = (int(x) for x in response.text.split())
            assert all(0 <= v <= 255 for v in (r, g, b))
        except Exception:
            self.set_status_text("Bad RGB")
            return
        if self.ambi_on:
            self.stop_ambi()
        self.color = (r, g, b)
        save_color(r, g, b)
        self.run_async(lambda: oneshot(set_section_led(r, g, b)), f"rgb({r},{g},{b})")

    def preset_(self, sender):
        names = list(PRESETS.keys())
        name = names[int(sender.tag())]
        if self.ambi_on:
            self.stop_ambi()
        self.color = PRESETS[name]
        save_color(*self.color)
        r, g, b = self.color
        self.run_async(lambda: oneshot(set_section_led(r, g, b)), name.title())

    def toggleAmbi_(self, _sender):
        if self.ambi_on:
            self.stop_ambi()
            self.turnOn_(None)
            return
        self.ambi_on = True
        if self.ambi_btn:
            self.ambi_btn.setTitle_("Stop Ambilight")
        self.set_status_text("Ambilight running…")
        self.ambi.start()

    def stop_ambi(self):
        self.ambi.stop()
        self.ambi_on = False
        if self.ambi_btn:
            self.ambi_btn.setTitle_("Start Ambilight")
        self.set_status_text("Ambilight stopped")

    def ambi_error(self, msg):
        rumps.Timer(lambda _: self.set_status_text("Error: " + msg), 0.01).start()


class SyncLightMenu(rumps.App):
    def __init__(self, controller: Controller):
        super().__init__("SL", quit_button=None)
        self.controller = controller
        self.menu = [
            rumps.MenuItem("Show Window", callback=self.show_window),
            rumps.MenuItem("Start / Stop Ambilight", callback=self.toggle_ambi),
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

    def show_window(self, _):
        if self.controller.window is None:
            self.controller.buildWindow()
        else:
            self.controller.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def toggle_ambi(self, _):
        self.controller.toggleAmbi_(None)

    def quit_app(self, _):
        if self.controller.ambi_on:
            self.controller.stop_ambi()
        rumps.quit_application()


def main():
    NSApplication.sharedApplication()
    NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = Controller.alloc().init()
    controller.buildWindow()
    # Keep menu bar too
    SyncLightMenu(controller).run()


if __name__ == "__main__":
    main()
