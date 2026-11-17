#!/usr/bin/env python3
"""
Mac-driven animated lighting for room BT strips (+ optional Yandex bulb).

LEDnetWF built-in 0x38 effects are unreliable on many firmwares, so SyncLight
runs the animation here and pushes solid colours — same approach as ambilight.
"""

from __future__ import annotations

import colorsys
import logging
import threading
import time
from typing import Callable, List, Optional, Sequence, Tuple

from color_math import lerp_oklab, smootherstep

log = logging.getLogger("synclight.room_fx")

RGB = Tuple[int, int, int]

# UI / scene effect index → colour palette for Mac animation
# Slightly softened peaks — less neon flash, easier on the eyes
PALETTES: dict = {
    0: [(50, 230, 175), (70, 145, 235), (120, 235, 210), (35, 190, 155)],  # aurora
    1: [(240, 55, 125), (240, 155, 55), (95, 55, 235), (55, 205, 235)],  # party
    2: [(235, 40, 175), (55, 205, 235), (240, 90, 55), (175, 55, 235)],  # neon
    3: [(55, 235, 130), (240, 210, 55), (55, 155, 235), (240, 90, 55)],  # game
    4: [(240, 110, 55), (240, 55, 90), (240, 175, 70), (195, 55, 55)],  # warm chase
    5: [(175, 80, 235), (240, 55, 175), (90, 125, 235), (240, 110, 195)],  # music
    6: [(235, 30, 90), (30, 235, 155), (95, 40, 235), (240, 195, 30)],  # party hard
}


def _scale(rgb: RGB, bri: int) -> RGB:
    f = max(0.05, min(1.0, bri / 255.0))
    return (int(rgb[0] * f), int(rgb[1] * f), int(rgb[2] * f))


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    """Perceptual blend (OKLCh) — default for FX morphs."""
    return lerp_oklab(a, b, t)


def palette_for(effect_index: int, base: Optional[RGB] = None) -> List[RGB]:
    idx = int(effect_index) % max(1, len(PALETTES))
    pal = list(PALETTES.get(idx) or PALETTES[0])
    if base and sum(base) > 30:
        # Tint palette toward scene colour in OKLab
        pal = [lerp_oklab(p, base, 0.35) for p in pal]
        pal.insert(0, base)
    return pal


ApplyFn = Callable[[str, RGB], None]  # role|bulb → rgb


class RoomFxEngine:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if join and t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=0.6)

    def start(
        self,
        *,
        apply: ApplyFn,
        effect_index: int = 0,
        base_color: Optional[RGB] = None,
        brightness: int = 200,
        speed: int = 55,
        split_roles: bool = True,
    ) -> None:
        """Start Mac animation. `apply(role, rgb)` must send colour (ceiling/floor/bulb)."""
        self.stop(join=True)
        self._stop.clear()
        pal = palette_for(effect_index, base_color)
        # Dense soft morph (~14–22 Hz): more mid-frames → less lag feel vs HID.
        # speed 5..100 → slower/faster colour travel, not harsh jumps.
        sp = max(5, min(100, int(speed)))
        interval = 0.072 - (sp / 100.0) * 0.026  # ~14..22 Hz
        phase_step = 0.028 + (sp / 100.0) * 0.032  # full blend ~1.5–2.5 s

        def _loop() -> None:
            log.info(
                "room fx start effect=%s bri=%s hz~%.0f",
                effect_index,
                brightness,
                1.0 / max(0.04, interval),
            )
            i = 0
            phase = 0.0
            next_t = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if now < next_t:
                    if self._stop.wait(min(0.012, next_t - now)):
                        break
                    continue
                next_t = now + interval
                try:
                    n = len(pal)
                    a = pal[i % n]
                    b = pal[(i + 1) % n]
                    c = pal[(i + 2) % n]
                    # Perlin smootherstep × OKLCh — C² continuous, no grey mud
                    t = smootherstep(phase % 1.0)
                    top = _scale(lerp_oklab(a, b, t), brightness)
                    bot = _scale(lerp_oklab(b, c, t), brightness)
                    if not split_roles:
                        bot = top
                    apply("ceiling", top)
                    apply("floor", bot)
                    apply("bulb", _scale(lerp_oklab(top, bot, 0.5), brightness))
                    phase += phase_step
                    if phase >= 1.0:
                        phase -= 1.0
                        i = (i + 1) % n
                except Exception as exc:
                    log.debug("room fx tick: %s", exc)
            log.info("room fx stop")

        self._thread = threading.Thread(target=_loop, name="room-fx", daemon=True)
        self._thread.start()


# Hue rainbow helper for “party” intensity
def rainbow(hue: float, bri: int = 200) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
    return _scale((int(r * 255), int(g * 255), int(b * 255)), bri)
