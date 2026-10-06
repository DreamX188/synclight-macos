#!/usr/bin/env python3
"""
SyncLight menu-bar GUI for macOS.

  python3 gui.py
  # or after install:  sl-gui
"""

from __future__ import annotations

import os
import threading

import rumps
from AppKit import NSApplication, NSColor, NSColorPanel
from Foundation import NSNotificationCenter, NSObject
from objc import super as objc_super

from ambilight import AmbilightEngine
from device import driver_loaded, load_driver, oneshot, set_section_led, unload_driver
from sl import PRESETS, STATE_FILE, PLIST


def _load_color():
    try:
        parts = open(STATE_FILE).read().strip().split()
        return tuple(int(x) for x in parts)
    except Exception:
        return (255, 200, 100)


def _save_color(r, g, b):
    open(STATE_FILE, "w").write(f"{r} {g} {b}\n")


def _apply(r, g, b, *, persist=True):
    if persist and not (r == 0 and g == 0 and b == 0):
        _save_color(r, g, b)
    oneshot(set_section_led(r, g, b))


class _ColorPanelDelegate(NSObject):
    def initWithCallback_(self, callback):
        self = objc_super(_ColorPanelDelegate, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def colorDidChange_(self, _notification):
        color = NSColorPanel.sharedColorPanel().color()
        try:
            rgb = color.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
        except Exception:
            rgb = color
        if rgb is None:
            return
        r = int(round(rgb.redComponent() * 255))
        g = int(round(rgb.greenComponent() * 255))
        b = int(round(rgb.blueComponent() * 255))
        self._callback(r, g, b)


class SyncLightApp(rumps.App):
    def __init__(self):
        super().__init__("SL", quit_button=None)
        self._on = True
        self._busy = False
        self._color = _load_color()
        self._panel_delegate = None
        self._ambi = AmbilightEngine(on_error=self._ambi_error)
        self._ambi_on = False

        self.power_item = rumps.MenuItem("Turn Off", callback=self.toggle_power)
        self.ambi_item = rumps.MenuItem("Ambilight: Off", callback=self.toggle_ambi)
        self.driver_item = rumps.MenuItem(
            "Auto sleep: …", callback=self.toggle_driver
        )

        preset_items = [
            rumps.MenuItem(label, callback=self._make_preset(name))
            for name, label in (
                ("warm", "Warm"),
                ("white", "White"),
                ("cool", "Cool"),
                ("red", "Red"),
                ("green", "Green"),
                ("blue", "Blue"),
                ("purple", "Purple"),
            )
        ]

        self.menu = [
            self.power_item,
            self.ambi_item,
            None,
            *preset_items,
            None,
            rumps.MenuItem("Pick Color…", callback=self.pick_color),
            None,
            self.driver_item,
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]
        self._refresh_labels()

    def _make_preset(self, name):
        def handler(_):
            if self._ambi_on:
                self._stop_ambi()
            r, g, b = PRESETS[name]
            self._color = (r, g, b)
            self._on = True
            self._refresh_labels()
            self._run_async(lambda: _apply(r, g, b))

        return handler

    def _refresh_labels(self):
        if self._ambi_on:
            self.title = "SL · Ambi"
        else:
            self.title = "SL · On" if self._on else "SL · Off"
        self.power_item.title = "Turn Off" if self._on else "Turn On"
        self.ambi_item.state = self._ambi_on
        self.ambi_item.title = "Ambilight: On" if self._ambi_on else "Ambilight: Off"
        loaded = driver_loaded()
        self.driver_item.state = loaded
        self.driver_item.title = "Auto sleep: On" if loaded else "Auto sleep: Off"

    def _run_async(self, fn):
        if self._busy:
            return

        def worker():
            self._busy = True
            try:
                fn()
            except Exception as exc:
                rumps.notification("SyncLight", "Error", str(exc))
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _ambi_error(self, msg):
        rumps.notification("SyncLight Ambilight", "Error", msg)

    def _stop_ambi(self):
        self._ambi.stop()
        self._ambi_on = False
        self._refresh_labels()

    def toggle_ambi(self, _):
        if self._ambi_on:
            self._stop_ambi()
            # restore last solid color
            r, g, b = self._color
            self._on = True
            self._refresh_labels()
            self._run_async(lambda: _apply(r, g, b))
            return

        self._ambi_on = True
        self._on = True
        self._refresh_labels()
        rumps.notification(
            "SyncLight",
            "Ambilight",
            "If strip stays dark: System Settings → Privacy → Screen Recording → allow Python/Terminal",
        )
        self._ambi.start()

    def toggle_power(self, _):
        if self._ambi_on:
            self._stop_ambi()
        if self._on:
            self._on = False
            self._refresh_labels()
            self._run_async(lambda: _apply(0, 0, 0, persist=False))
        else:
            self._on = True
            self._refresh_labels()
            r, g, b = self._color
            self._run_async(lambda: _apply(r, g, b))

    def pick_color(self, _):
        if self._ambi_on:
            self._stop_ambi()
        panel = NSColorPanel.sharedColorPanel()
        r, g, b = self._color
        panel.setColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                r / 255.0, g / 255.0, b / 255.0, 1.0
            )
        )
        panel.setShowsAlpha_(False)
        if self._panel_delegate is None:
            self._panel_delegate = _ColorPanelDelegate.alloc().initWithCallback_(
                self._on_panel_color
            )
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self._panel_delegate,
                "colorDidChange:",
                "NSColorPanelColorDidChangeNotification",
                panel,
            )
        panel.orderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def _on_panel_color(self, r, g, b):
        self._color = (r, g, b)
        self._on = True
        self._refresh_labels()
        self._run_async(lambda: _apply(r, g, b))

    def toggle_driver(self, sender):
        if not os.path.exists(PLIST):
            rumps.alert(
                "Driver not installed",
                "Run ./install.sh in the SyncLight folder first.",
            )
            return
        if self._ambi_on:
            rumps.alert("Ambilight is on", "Turn off Ambilight before changing Auto sleep.")
            return
        if sender.state:
            unload_driver()
            sender.state = False
        else:
            load_driver()
            sender.state = True
        self._refresh_labels()

    def quit_app(self, _):
        if self._ambi_on:
            self._stop_ambi()
        rumps.quit_application()


def main():
    NSApplication.sharedApplication()
    SyncLightApp().run()


if __name__ == "__main__":
    main()
