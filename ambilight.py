#!/usr/bin/env python3
"""
macOS screen-sync ambilight for Robobloq SyncLight.

Captures the primary display edges via mss, maps colors onto the LED strip
using the SC setSyncScreen (0x80) protocol.
"""

from __future__ import annotations

import colorsys
import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

import mss

from device import Device, shared

log = logging.getLogger(__name__)

DEFAULT_LED_COUNT = 65
DEFAULT_FPS = 20
SMOOTHING_FRAMES = 8


def _aspect_grid(width: int, height: int) -> tuple[int, int]:
    ratio = int(width / height * 10) / 10
    targets = {
        (16, 9): int(16 / 9 * 10) / 10,
        (8, 5): int(16 / 10 * 10) / 10,
        (4, 3): int(4 / 3 * 10) / 10,
        (21, 9): int(21 / 9 * 10) / 10,
    }
    for cols_rows, r in targets.items():
        if abs(ratio - r) < 0.05:
            return cols_rows if cols_rows != (21, 9) else (16, 9)
    return (16, 9)


def _boost(rgb: tuple[int, int, int], saturation: float = 1.35) -> tuple[int, int, int]:
    r, g, b = [c / 255.0 for c in rgb]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    s = min(1.0, s * saturation)
    # mild lift so dark edges still glow a bit
    v = min(1.0, v * 1.08)
    rr, gg, bb = colorsys.hsv_to_rgb(h, s, v)
    return int(rr * 255), int(gg * 255), int(bb * 255)


def _compress(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = rgb
    total = r + g + b
    if total > 255:
        scale = 255.0 / total
        r = int(r * scale)
        g = int(g * scale)
        b = int(b * scale)
    return r, g, b


class ColorExtractor:
    def __init__(
        self,
        led_count: int = DEFAULT_LED_COUNT,
        edge_number: int = 3,
        reverse: bool = False,
        smoothing: bool = True,
        saturation: float = 1.35,
    ) -> None:
        self.led_count = led_count
        self.edge_number = edge_number
        self.reverse = reverse
        self.smoothing = smoothing
        self.saturation = saturation
        self.cols = 0
        self.rows = 0
        self.history: deque[list[tuple[int, int, int]]] = deque(maxlen=SMOOTHING_FRAMES)

    def extract_bgra(self, data: bytes, width: int, height: int) -> list[tuple[int, int, int]]:
        if self.cols == 0:
            self.cols, self.rows = _aspect_grid(width, height)

        grid = self._capture_grid(data, width, height, self.cols, self.rows)
        border = self._border(grid, self.cols, self.rows)
        mapped = self._map_to_leds(border)

        processed = [_compress(_boost(c, self.saturation)) for c in mapped]

        if self.smoothing:
            self.history.append(processed)
            return self._average()
        return processed

    def _capture_grid(
        self, data: bytes, width: int, height: int, cols: int, rows: int
    ) -> list[tuple[int, int, int]]:
        cell_w = max(1, width // cols)
        cell_h = max(1, height // rows)
        step = max(4, cell_w // 8, cell_h // 8)
        pitch = width * 4
        grid: list[tuple[int, int, int]] = []

        for row in range(rows):
            for col in range(cols):
                x0 = col * cell_w
                y0 = row * cell_h
                x1 = min(width, (col + 1) * cell_w)
                y1 = min(height, (row + 1) * cell_h)
                rs = gs = bs = count = 0
                y = y0
                while y < y1:
                    x = x0
                    row_off = y * pitch
                    while x < x1:
                        o = row_off + x * 4
                        if o + 2 < len(data):
                            # BGRA
                            bs += data[o]
                            gs += data[o + 1]
                            rs += data[o + 2]
                            count += 1
                        x += step
                    y += step
                if count:
                    grid.append((rs // count, gs // count, bs // count))
                else:
                    grid.append((0, 0, 0))
        return grid

    @staticmethod
    def _is_black(c: tuple[int, int, int], thr: int = 10) -> bool:
        return c[0] < thr and c[1] < thr and c[2] < thr

    def _border(
        self, grid: list[tuple[int, int, int]], cols: int, rows: int
    ) -> list[tuple[int, int, int]]:
        top1 = [grid[c] for c in range(cols)]
        top2 = [grid[cols + c] for c in range(cols)] if rows > 1 else top1
        top = top2 if all(self._is_black(c) for c in top1) else top1

        bot1 = [grid[(rows - 1) * cols + c] for c in range(cols)]
        bot2 = (
            [grid[(rows - 2) * cols + c] for c in range(cols)] if rows > 2 else bot1
        )
        bottom = bot2 if all(self._is_black(c) for c in bot1) else bot1

        left = [grid[r * cols] for r in range(rows)]
        right = [grid[r * cols + cols - 1] for r in range(rows)]

        colors: list[tuple[int, int, int]] = []
        if self.reverse:
            colors.extend(reversed(right))
            colors.extend(reversed(top))
            colors.extend(reversed(left))
            if self.edge_number >= 4:
                colors.extend(bottom)
        else:
            # SyncLight order: left(bottom→top) → top → right(top→bottom)
            colors.extend(reversed(left))
            colors.extend(top)
            colors.extend(right)
            if self.edge_number >= 4:
                colors.extend(reversed(bottom))
        return colors

    def _map_to_leds(
        self, border: list[tuple[int, int, int]]
    ) -> list[tuple[int, int, int]]:
        if not border or self.led_count <= 0:
            return [(0, 0, 0)] * self.led_count
        src = len(border)
        out: list[tuple[int, int, int]] = []
        for i in range(self.led_count):
            idx = min(src - 1, int((i + 0.5) * src / self.led_count))
            out.append(border[idx])
        return out

    def _average(self) -> list[tuple[int, int, int]]:
        n = len(self.history)
        if n == 0:
            return [(0, 0, 0)] * self.led_count
        if n == 1:
            return list(self.history[0])
        led_n = len(self.history[0])
        result: list[tuple[int, int, int]] = []
        for i in range(led_n):
            rs = gs = bs = 0
            for frame in self.history:
                r, g, b = frame[i]
                rs += r
                gs += g
                bs += b
            result.append((rs // n, gs // n, bs // n))
        return result


class AmbilightEngine:
    def __init__(
        self,
        device: Optional[Device] = None,
        led_count: int = DEFAULT_LED_COUNT,
        fps: int = DEFAULT_FPS,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.device = device or shared
        self.led_count = led_count
        self.fps = fps
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._extractor = ColorExtractor(led_count=led_count)
        self.running = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ambilight", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.running = False
        try:
            self.device.disconnect(restore_driver=True)
        except Exception:
            pass

    def _emit_error(self, msg: str) -> None:
        log.warning(msg)
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception:
                pass

    def _loop(self) -> None:
        interval = 1.0 / max(1, self.fps)
        try:
            self.device.connect(release_driver=True)
            self.running = True
            log.info("Ambilight started (%d LEDs @ %d fps)", self.led_count, self.fps)
        except Exception as exc:
            self._emit_error(f"Connect failed: {exc}")
            self.running = False
            return

        try:
            with mss.MSS() as sct:
                # monitors[0] is virtual all-monitors; [1] is first display
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                while not self._stop.is_set():
                    t0 = time.perf_counter()
                    try:
                        shot = sct.grab(mon)
                        # mss raw is BGRA
                        colors = self._extractor.extract_bgra(
                            shot.bgra, shot.width, shot.height
                        )
                        self.device.send_sync_frame(colors)
                    except Exception as exc:
                        self._emit_error(str(exc))
                        time.sleep(0.5)
                    elapsed = time.perf_counter() - t0
                    delay = interval - elapsed
                    if delay > 0:
                        self._stop.wait(delay)
        finally:
            self.running = False
            try:
                self.device.disconnect(restore_driver=True)
            except Exception:
                pass
            log.info("Ambilight stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    eng = AmbilightEngine()
    eng.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        eng.stop()


if __name__ == "__main__":
    main()
