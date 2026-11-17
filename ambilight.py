#!/usr/bin/env python3
"""
macOS screen-sync ambilight for Robobloq SyncLight.

Captures the primary display edges via mss, maps colors onto the LED strip
using the SC setSyncScreen (0x80) protocol.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

import mss

from color_math import (
    CriticallyDampedFollower,
    OklabFollower,
    average_oklab,
    boost_oklch,
    gaussian_weights,
    lerp_oklab,
    lift_for_leds,
    weighted_average_oklab,
)
from device import Device, shared
from mpeg_video import MpegFrameAnalyser

log = logging.getLogger(__name__)

DEFAULT_LED_COUNT = 65
DEFAULT_FPS = 55  # desktop / windowed UI
VIDEO_FPS = 75  # fullscreen HTML5 / player theater
SMOOTHING_FRAMES = 5  # legacy UI path only
# mss monitors[0] is the virtual "all displays" desktop — real screens start at 1
DEFAULT_MONITOR = 1


def _aspect_grid(width: int, height: int) -> tuple[int, int]:
    """Finer grid than 16×9 so top/center/bottom bands stay spatially honest."""
    ratio = int(width / height * 10) / 10
    targets = {
        (24, 14): int(16 / 9 * 10) / 10,
        (20, 12): int(16 / 10 * 10) / 10,
        (16, 12): int(4 / 3 * 10) / 10,
        (28, 12): int(21 / 9 * 10) / 10,
    }
    for cols_rows, r in targets.items():
        if abs(ratio - r) < 0.05:
            return cols_rows
    return (24, 14)


def _boost(rgb: tuple[int, int, int], saturation: float = 1.28) -> tuple[int, int, int]:
    """Perceptual chroma lift in OKLCh (not HSV)."""
    return boost_oklch(rgb, chroma_gain=saturation, light_gain=1.05)


def _compress(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Soft energy limit so whites don't clip hard on the strip."""
    r, g, b = rgb
    total = r + g + b
    if total > 280:
        scale = 280.0 / total
        r = int(r * scale)
        g = int(g * scale)
        b = int(b * scale)
    return r, g, b


def _avg_rgb(colors: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    """Perceptual mean (OKLab) — RGB mean muddy-greys mixed hues."""
    if not colors:
        return (0, 0, 0)
    return average_oklab(colors)


class ColorExtractor:
    def __init__(
        self,
        led_count: int = DEFAULT_LED_COUNT,
        edge_number: int = 3,
        reverse: bool = False,
        smoothing: bool = True,
        saturation: float = 1.28,
    ) -> None:
        self.led_count = led_count
        self.edge_number = edge_number
        self.reverse = reverse
        self.smoothing = smoothing
        self.saturation = saturation
        self.cols = 0
        self.rows = 0
        self.history: deque[list[tuple[int, int, int]]] = deque(maxlen=SMOOTHING_FRAMES)
        # Per-LED critically damped OKLab followers (ζ=1, ~8 Hz) — legacy UI path
        self._followers: list[CriticallyDampedFollower] = []
        self._ensure_followers(led_count)
        # MPEG-style video analyser (YCbCr + macroblocks + scene cuts)
        self._mpeg = MpegFrameAnalyser(
            mb_cols=20, mb_rows=12, led_count=led_count, chroma_gain=saturation
        )
        self.use_mpeg = True  # video path; set False to fall back to screenshot blend
        # Cinema / fullscreen video: sample past letterbox, richer chroma, deeper blacks
        self.cinematic = False
        self._letterbox = (0, 0, 0, 0)  # top, bot, left, right inset rows/cols
        self._black_thr = 14
        # Dual-band: USB = edge ambilight (fast); room = 40%/60% regions (slower)
        # Snappy room bands — top→ceiling, bottom→floor track video quickly
        self._room_follow_top = OklabFollower(tau_s=0.05)
        self._room_follow_bot = OklabFollower(tau_s=0.05)
        self._prev_mean_luma: Optional[float] = None
        self.last_flash = False
        self.last_flash_strength = 0.0
        self.last_motion = 0.0
        self.last_scene_cut = False

    def _ensure_followers(self, n: int) -> None:
        n = max(1, int(n))
        if len(self._followers) == n:
            return
        # ~12 Hz — monitor strip stays responsive to flashes
        self._followers = [CriticallyDampedFollower(natural_hz=12.0) for _ in range(n)]

    def reset_temporal(self) -> None:
        """Clear history + spring state (display change / ambilight restart)."""
        self.history.clear()
        for f in self._followers:
            f.reset()
        self._mpeg.reset()
        self._mpeg.set_led_count(self.led_count)
        self._room_follow_top.reset()
        self._room_follow_bot.reset()
        self._letterbox = (0, 0, 0, 0)
        self._prev_mean_luma = None
        self.last_flash = False
        self.last_flash_strength = 0.0
        self.last_motion = 0.0
        self.last_scene_cut = False

    def set_cinematic(self, enabled: bool, *, force: bool = False) -> None:
        """Fullscreen-video profile: MPEG path + stronger chroma + denser grid."""
        on = bool(enabled)
        if on == self.cinematic and not force:
            return
        changed = on != self.cinematic
        self.cinematic = on
        self.use_mpeg = True
        # Theater: richer chroma; grid adapts to 16:10 / 16:9 / ultrawide per frame
        self._mpeg.chroma_gain = (self.saturation * 1.55) if on else self.saturation
        # Seed a sensible default; analyse_bgra retunes to real frame aspect
        if on:
            self._mpeg.mb_cols, self._mpeg.mb_rows = 28, 18  # 16:10-friendly default
        else:
            self._mpeg.mb_cols, self._mpeg.mb_rows = 20, 12
        self._mpeg._frame_w = 0
        self._mpeg._frame_h = 0
        self._room_follow_top.tau = 0.028 if on else 0.05
        self._room_follow_bot.tau = 0.028 if on else 0.05
        # Only wipe temporal state when entering/leaving cinema — force=True
        # used to reset every tick and freeze room on a grey first frame.
        if changed:
            hz = 22.0 if on else 14.0
            self._followers = [
                CriticallyDampedFollower(natural_hz=hz) for _ in self._followers
            ]
            self.history.clear()
            self._mpeg.reset()
            if on:
                self._mpeg.mb_cols, self._mpeg.mb_rows = 28, 18
            else:
                self._mpeg.mb_cols, self._mpeg.mb_rows = 20, 12
            self._room_follow_top.reset()
            self._room_follow_bot.reset()

    def _detect_letterbox(
        self, grid: list[tuple[int, int, int]], cols: int, rows: int
    ) -> tuple[int, int, int, int]:
        """
        Find black-bar insets (top, bottom, left, right) in grid cells.
        Used so fullscreen 21:9 / 2.39:1 movies drive LEDs from content, not bars.
        """
        thr = self._black_thr

        def row_black(r: int) -> bool:
            cells = [grid[r * cols + c] for c in range(cols)]
            return sum(1 for c in cells if self._is_black(c, thr)) >= max(1, cols * 3 // 4)

        def col_black(c: int) -> bool:
            cells = [grid[r * cols + c] for r in range(rows)]
            return sum(1 for x in cells if self._is_black(x, thr)) >= max(1, rows * 3 // 4)

        top = 0
        while top < rows // 3 and row_black(top):
            top += 1
        bot = 0
        while bot < rows // 3 and row_black(rows - 1 - bot):
            bot += 1
        left = 0
        while left < cols // 3 and col_black(left):
            left += 1
        right = 0
        while right < cols // 3 and col_black(cols - 1 - right):
            right += 1
        # Need a real content window
        if top + bot >= rows - 2:
            top = bot = 0
        if left + right >= cols - 2:
            left = right = 0
        return top, bot, left, right

    def extract_bgra(
        self, data: bytes, width: int, height: int
    ) -> tuple[
        list[tuple[int, int, int]],
        tuple[int, int, int],
        tuple[int, int, int],
    ]:
        """
        Prefer MPEG-style video path (YCbCr macroblocks + scene cuts).
        Falls back to screenshot honest-ambilight if use_mpeg is False.
        """
        if self.use_mpeg:
            return self._extract_mpeg(data, width, height)
        return self._extract_legacy(data, width, height)

    def _extract_mpeg(
        self, data: bytes, width: int, height: int
    ) -> tuple[
        list[tuple[int, int, int]],
        tuple[int, int, int],
        tuple[int, int, int],
    ]:
        self.cols = self._mpeg.mb_cols
        self.rows = self._mpeg.mb_rows
        leds, mood_t, mood_b, meta = self._mpeg.analyse_bgra(
            data,
            width,
            height,
            edge_number=self.edge_number,
            reverse=self.reverse,
        )
        self._letterbox = tuple(meta.get("insets") or (0, 0, 0, 0))  # type: ignore
        self.last_motion = float(meta.get("motion") or 0)
        self.last_scene_cut = bool(meta.get("scene_cut"))
        flash_s = float(meta.get("flash_strength") or 0)
        self.last_flash = bool(meta.get("flash")) or flash_s > 0.25
        self.last_flash_strength = flash_s

        now = time.monotonic()
        # Dual-band room: extra lag on top of MPEG mood; snap on I-frame cuts.
        # Only hold on strong flashes (explosions) — mild luma bumps used to
        # freeze the room on the first grey frame during browser video.
        if self.last_scene_cut or flash_s > 0.75:
            if flash_s > 0.75 and not self.last_scene_cut:
                # Hard flash: keep previous room if we have one
                room_top = self._room_follow_top.rgb() if self._room_follow_top.ready else mood_t
                room_bottom = (
                    self._room_follow_bot.rgb() if self._room_follow_bot.ready else mood_b
                )
            else:
                self._room_follow_top.reset(mood_t)
                self._room_follow_bot.reset(mood_b)
                room_top, room_bottom = mood_t, mood_b
        else:
            room_top = self._room_follow_top.update(mood_t, now=now, hold=False)
            room_bottom = self._room_follow_bot.update(mood_b, now=now, hold=False)
        return leds, room_top, room_bottom

    def _extract_legacy(
        self, data: bytes, width: int, height: int
    ) -> tuple[
        list[tuple[int, int, int]],
        tuple[int, int, int],
        tuple[int, int, int],
    ]:
        if self.cols == 0:
            self.cols, self.rows = _aspect_grid(width, height)

        grid = self._capture_grid(data, width, height, self.cols, self.rows)
        insets = self._detect_letterbox(grid, self.cols, self.rows)
        if sum(insets) > 0:
            self._letterbox = insets
            self._lb_hits = getattr(self, "_lb_hits", 0) + 1
        else:
            self._lb_hits = max(0, getattr(self, "_lb_hits", 0) - 2)
            if self._lb_hits == 0:
                self._letterbox = (0, 0, 0, 0)
        cinema = self.cinematic or getattr(self, "_lb_hits", 0) >= 10

        top_e, bot_e, left_e, right_e, mood_top, mood_bot = self._honest_regions(
            grid, self.cols, self.rows
        )
        border = self._assemble_border(top_e, bot_e, left_e, right_e)
        mapped = self._map_to_leds(border)

        sat = self.saturation * (1.18 if cinema else 1.0)
        processed = [_compress(_boost(c, sat)) for c in mapped]
        mood_top = _compress(_boost(mood_top, sat))
        mood_bot = _compress(_boost(mood_bot, sat))

        now = time.monotonic()
        if self.smoothing:
            self.history.append(processed)
            blended = self._average()
            self._ensure_followers(len(blended))
            leds = [
                self._followers[i].update(blended[i], now=now)
                for i in range(len(blended))
            ]
        else:
            leds = processed

        room_top = self._room_follow_top.update(mood_top, now=now)
        room_bottom = self._room_follow_bot.update(mood_bot, now=now)
        return leds, room_top, room_bottom

    def room_from_leds(
        self, leds: list[tuple[int, int, int]]
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """
        Derive ceiling/floor from monitor LED colors (exact visual match).

        Strip layout (edge 3): left(bottom→top) → top → right(top→bottom)
        Ceiling ← top LEDs; floor ← bottom of left/right (+ bottom edge if 4).
        """
        n = len(leds)
        if n == 0:
            return (0, 0, 0), (0, 0, 0)

        cols = max(1, self.cols)
        rows = max(1, self.rows)
        left_n = rows
        top_n = cols
        right_n = rows
        bot_n = cols if self.edge_number >= 4 else 0
        total = max(1, left_n + top_n + right_n + bot_n)

        def _slice(a: float, b: float) -> list[tuple[int, int, int]]:
            i0 = max(0, min(n, int(a * n)))
            i1 = max(i0 + 1, min(n, int(b * n)))
            return leds[i0:i1]

        f0 = 0.0
        f1 = left_n / total
        f2 = (left_n + top_n) / total
        f3 = (left_n + top_n + right_n) / total

        left = _slice(f0, f1)
        top = _slice(f1, f2)
        right = _slice(f2, f3)
        bottom = _slice(f3, 1.0) if bot_n else []

        ceiling = _avg_rgb(top) if top else _avg_rgb(leds)

        floor_parts: list[tuple[int, int, int]] = []
        if left:
            # left runs bottom→top → first chunk = bottom of monitor
            k = max(1, (len(left) * 40 + 99) // 100)
            floor_parts.extend(left[:k])
        if right:
            # right runs top→bottom → last chunk = bottom
            k = max(1, (len(right) * 40 + 99) // 100)
            floor_parts.extend(right[-k:])
        if bottom:
            floor_parts.extend(bottom)
        floor = _avg_rgb(floor_parts) if floor_parts else _avg_rgb(leds)
        return ceiling, floor

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

    def _content_bounds(
        self, cols: int, rows: int
    ) -> tuple[int, int, int, int]:
        """Inclusive content window after letterbox/pillarbox insets."""
        it, ib, il, ir = self._letterbox
        r0 = max(0, it)
        r1 = max(r0, rows - 1 - max(0, ib))
        c0 = max(0, il)
        c1 = max(c0, cols - 1 - max(0, ir))
        return r0, r1, c0, c1

    def _honest_regions(
        self, grid: list[tuple[int, int, int]], cols: int, rows: int
    ) -> tuple[
        list[tuple[int, int, int]],
        list[tuple[int, int, int]],
        list[tuple[int, int, int]],
        list[tuple[int, int, int]],
        tuple[int, int, int],
        tuple[int, int, int],
    ]:
        """
        True bias-lighting sample — not just 1px behind the bezel.

        For each horizontal column:
          top LED  ← deep top band + upper-center of the frame
          bot LED  ← deep bottom band + lower-center of the frame
        Vertical sides similarly. Room ceiling/floor = OKLab mean of those
        top/bottom bands (honest top→ceiling, bottom→floor).
        """
        r0, r1, c0, c1 = self._content_bounds(cols, rows)
        ch = max(1, r1 - r0 + 1)
        cw = max(1, c1 - c0 + 1)
        # How far into the picture we look (~22–28% — classic Philips-style depth)
        depth_v = max(2, min(ch // 3, max(2, int(round(ch * 0.28)))))
        depth_h = max(2, min(cw // 3, max(2, int(round(cw * 0.22)))))
        # Center pull rows (upper / lower thirds of content)
        r_mid_hi = r0 + max(1, int(ch * 0.32))
        r_mid_lo = r0 + min(ch - 1, int(ch * 0.68))
        c_mid = c0 + cw // 2

        def cell(r: int, c: int) -> tuple[int, int, int]:
            r = max(r0, min(r1, r))
            c = max(c0, min(c1, c))
            return grid[r * cols + c]

        top: list[tuple[int, int, int]] = []
        bottom: list[tuple[int, int, int]] = []
        for c in range(c0, c1 + 1):
            # Top: edge → inward, plus a center-upper sample
            ts: list[tuple[int, int, int]] = []
            tw: list[float] = []
            for d in range(depth_v):
                ts.append(cell(r0 + d, c))
                tw.append(1.0 / (1.0 + 0.28 * d))
            ts.append(cell(r_mid_hi, c))
            tw.append(0.62)  # center of frame influences upper strip
            # Mild mid-column pull so sky/face in center shows on top LEDs
            ts.append(cell(r_mid_hi, c_mid))
            tw.append(0.22)
            top.append(weighted_average_oklab(ts, tw))

            bs: list[tuple[int, int, int]] = []
            bw: list[float] = []
            for d in range(depth_v):
                bs.append(cell(r1 - d, c))
                bw.append(1.0 / (1.0 + 0.28 * d))
            bs.append(cell(r_mid_lo, c))
            bw.append(0.62)
            bs.append(cell(r_mid_lo, c_mid))
            bw.append(0.22)
            bottom.append(weighted_average_oklab(bs, bw))

        left: list[tuple[int, int, int]] = []
        right: list[tuple[int, int, int]] = []
        for r in range(r0, r1 + 1):
            # Vertical: map top-of-screen → top of side strip, bottom → bottom
            # Blend side edge with horizontal center at same row
            ls: list[tuple[int, int, int]] = []
            lw: list[float] = []
            for d in range(depth_h):
                ls.append(cell(r, c0 + d))
                lw.append(1.0 / (1.0 + 0.30 * d))
            ls.append(cell(r, c_mid))
            lw.append(0.48)
            left.append(weighted_average_oklab(ls, lw))

            rs: list[tuple[int, int, int]] = []
            rw: list[float] = []
            for d in range(depth_h):
                rs.append(cell(r, c1 - d))
                rw.append(1.0 / (1.0 + 0.30 * d))
            rs.append(cell(r, c_mid))
            rw.append(0.48)
            right.append(weighted_average_oklab(rs, rw))

        # Pad to full grid width/height so _map_to_leds proportions stay stable
        def _pad(
            edge: list[tuple[int, int, int]], n: int
        ) -> list[tuple[int, int, int]]:
            if not edge:
                return [(0, 0, 0)] * n
            if len(edge) == n:
                return edge
            out: list[tuple[int, int, int]] = []
            for i in range(n):
                idx = min(len(edge) - 1, int((i + 0.5) * len(edge) / n))
                out.append(edge[idx])
            return out

        top = _pad(top, cols)
        bottom = _pad(bottom, cols)
        left = _pad(left, rows)
        right = _pad(right, rows)

        # Room: upper 40% of frame → ceiling, lower 60% → floor
        split = r0 + max(1, int(round(ch * 0.40)))
        split = min(r1, max(r0 + 1, split))
        ceil_cells: list[tuple[int, int, int]] = []
        floor_cells: list[tuple[int, int, int]] = []
        for r in range(r0, split):
            for c in range(c0, c1 + 1):
                ceil_cells.append(cell(r, c))
        for r in range(split, r1 + 1):
            for c in range(c0, c1 + 1):
                floor_cells.append(cell(r, c))
        mood_top = average_oklab(ceil_cells) if ceil_cells else average_oklab(top)
        mood_bot = average_oklab(floor_cells) if floor_cells else average_oklab(bottom)
        return top, bottom, left, right, mood_top, mood_bot

    def _assemble_border(
        self,
        top: list[tuple[int, int, int]],
        bottom: list[tuple[int, int, int]],
        left: list[tuple[int, int, int]],
        right: list[tuple[int, int, int]],
    ) -> list[tuple[int, int, int]]:
        """
        USB SyncLight order: left(bottom→top) → top → right(top→bottom) [→ bottom].

        Side strips use the honest left/right (already top→bottom in list order);
        reversed(left) yields bottom→top as firmware expects.
        """
        colors: list[tuple[int, int, int]] = []
        if self.reverse:
            colors.extend(reversed(right))
            colors.extend(reversed(top))
            colors.extend(reversed(left))
            if self.edge_number >= 4:
                colors.extend(bottom)
        else:
            colors.extend(reversed(left))
            colors.extend(top)
            colors.extend(right)
            if self.edge_number >= 4:
                colors.extend(reversed(bottom))
        return colors

    def _border(
        self, grid: list[tuple[int, int, int]], cols: int, rows: int
    ) -> list[tuple[int, int, int]]:
        """Backward-compatible wrapper around honest region sampling."""
        top, bot, left, right, _, _ = self._honest_regions(grid, cols, rows)
        return self._assemble_border(top, bot, left, right)

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
        """Gaussian-weighted OKLab mean over recent frames (newest weighted highest)."""
        n = len(self.history)
        if n == 0:
            return [(0, 0, 0)] * self.led_count
        if n == 1:
            return list(self.history[0])
        led_n = len(self.history[0])
        weights = gaussian_weights(n, sigma=0.9)
        result: list[tuple[int, int, int]] = []
        for i in range(led_n):
            samples = [frame[i] for frame in self.history]
            result.append(weighted_average_oklab(samples, weights))
        return result


def leds_from_room_colors(
    top: tuple[int, int, int],
    bot: tuple[int, int, int],
    *,
    led_count: int = DEFAULT_LED_COUNT,
    cols: int = 16,
    rows: int = 9,
    edge_number: int = 3,
) -> list[tuple[int, int, int]]:
    """
    Build a SyncLight USB frame that mirrors room ceiling/floor colors.

    Layout matches ColorExtractor: left(bottom→top) → top → right(top→bottom).
    """
    n = max(1, int(led_count))
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    left_n = rows
    top_n = cols
    right_n = rows
    bot_n = cols if edge_number >= 4 else 0
    total = max(1, left_n + top_n + right_n + bot_n)

    def _fill(count: int, start: tuple[int, int, int], end: tuple[int, int, int]):
        if count <= 0:
            return []
        if count == 1:
            return [start]
        # Spatial gradient in OKLCh — no grey mud along the strip edge
        return [lerp_oklab(start, end, i / (count - 1)) for i in range(count)]

    # Proportional LED counts
    n_left = max(1, int(round(n * left_n / total)))
    n_top = max(1, int(round(n * top_n / total)))
    n_right = max(1, int(round(n * right_n / total)))
    n_bot = max(0, n - n_left - n_top - n_right) if bot_n else 0
    # Fix rounding so sum == n
    used = n_left + n_top + n_right + n_bot
    if used < n:
        n_top += n - used
    elif used > n:
        n_top = max(1, n_top - (used - n))

    # left: bottom→top (bot → top), top: solid ceiling, right: top→bottom
    out: list[tuple[int, int, int]] = []
    out.extend(_fill(n_left, bot, top))
    out.extend([top] * n_top)
    out.extend(_fill(n_right, top, bot))
    if n_bot:
        out.extend([bot] * n_bot)
    if len(out) < n:
        out.extend([top] * (n - len(out)))
    return out[:n]


def list_displays() -> list[dict]:
    """Physical displays for the Ambilight source picker (mss index ≥ 1)."""
    out: list[dict] = []
    try:
        with mss.MSS() as sct:
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    continue
                w, h = int(m.get("width") or 0), int(m.get("height") or 0)
                out.append(
                    {
                        "index": i,
                        "width": w,
                        "height": h,
                        "left": int(m.get("left") or 0),
                        "top": int(m.get("top") or 0),
                        "label": f"Дисплей {i} · {w}×{h}",
                    }
                )
    except Exception as exc:
        log.warning("list_displays: %s", exc)
    return out


def pick_richest_monitor(sct, *, current: int = 1) -> int:
    """
    Choose the display with the most colourful / non-black picture.
    Keeps `current` if scores are close (avoid flicker between monitors).
    """
    best_i, best_score = current, -1.0
    try:
        mons = sct.monitors
        for i in range(1, len(mons)):
            shot = sct.grab(mons[i])
            raw = shot.bgra
            step = 48 * 4
            sat_s = lum_s = n = 0.0
            for j in range(0, len(raw) - 3, step):
                b, g, r = raw[j], raw[j + 1], raw[j + 2]
                mx, mn = max(r, g, b), min(r, g, b)
                sat_s += ((mx - mn) / mx) if mx > 10 else 0.0
                lum_s += 0.0722 * b + 0.7152 * g + 0.2126 * r
                n += 1.0
            if n <= 0:
                continue
            sat = sat_s / n
            lum = lum_s / n
            # Prefer colourful mid-bright content over black/blank screens
            score = sat * 2.5 + (0.35 if 25 < lum < 220 else 0.0)
            if i == current:
                score += 0.12  # hysteresis
            if score > best_score:
                best_score = score
                best_i = i
    except Exception as exc:
        log.debug("pick monitor: %s", exc)
        return max(1, int(current or 1))
    return max(1, int(best_i or 1))


class AmbilightEngine:
    def __init__(
        self,
        device: Optional[Device] = None,
        led_count: int = DEFAULT_LED_COUNT,
        fps: int = DEFAULT_FPS,
        monitor_index: int = DEFAULT_MONITOR,
        on_error: Optional[Callable[[str], None]] = None,
        on_frame: Optional[Callable[[list[tuple[int, int, int]]], None]] = None,
    ) -> None:
        self.device = device or shared
        self.led_count = led_count
        self.fps = fps
        self.monitor_index = max(1, int(monitor_index or DEFAULT_MONITOR))
        self.on_error = on_error
        self.on_frame = on_frame
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._extractor = ColorExtractor(led_count=led_count)
        self._base_fps = max(1, int(fps))
        self.running = False
        self.last_average: tuple[int, int, int] = (0, 0, 0)
        # Room strips are single-color: top edge → ceiling, bottom L/R → floor
        self.last_room_top: tuple[int, int, int] = (0, 0, 0)
        self.last_room_bottom: tuple[int, int, int] = (0, 0, 0)
        self._last_good_paint: Optional[list] = None
        self._last_good_room_top: tuple[int, int, int] = (0, 0, 0)
        self._last_good_room_bot: tuple[int, int, int] = (0, 0, 0)
        self._browser_video = False
        self.last_source: str = ""
        self.last_grab_mean: tuple[int, int, int] = (0, 0, 0)

    def set_monitor(self, index: int) -> None:
        """Switch capture display (takes effect on the next frame)."""
        self.monitor_index = max(1, int(index or DEFAULT_MONITOR))
        # Force grid rebuild for new aspect ratio
        self._extractor.cols = 0
        self._extractor.rows = 0
        self._extractor.reset_temporal()

    def set_cinematic(self, enabled: bool, *, force: bool = False) -> None:
        """Enable fullscreen-video: MPEG path + higher capture rate."""
        was = bool(getattr(self._extractor, "cinematic", False))
        self._extractor.set_cinematic(enabled, force=force)
        self.fps = VIDEO_FPS if enabled else self._base_fps
        if bool(enabled) and (not was or force):
            log.info("Ambilight cinema ON @ %d fps", self.fps)
        elif was and not bool(enabled):
            log.info("Ambilight cinema OFF @ %d fps", self.fps)

    @property
    def cinematic(self) -> bool:
        return bool(getattr(self._extractor, "cinematic", False))

    @property
    def last_flash(self) -> bool:
        return bool(getattr(self._extractor, "last_flash", False))

    @property
    def last_flash_strength(self) -> float:
        return float(getattr(self._extractor, "last_flash_strength", 0.0) or 0.0)

    def room_from_leds(
        self, leds: list[tuple[int, int, int]]
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """Ceiling/floor colors matched to the USB monitor LED frame."""
        return self._extractor.room_from_leds(leds)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            from bt_auth import ensure_screen_access

            # GUI already probed once; never re-trigger the system TCC dialog here
            ensure_screen_access(prompt=False)
        except Exception:
            pass
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ambilight", daemon=True)
        self._thread.start()

    def stop(self, *, quick: bool = False) -> None:
        """Stop capture. quick=True: never block UI (close / quit path)."""
        self._stop.set()
        self.running = False
        join_s = 0.15 if quick else 3.0
        if self._thread:
            self._thread.join(timeout=join_s)
            if not self._thread.is_alive():
                self._thread = None
        if quick:
            return
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

    def _resolve_monitor(self, sct) -> dict:
        mons = sct.monitors
        idx = self.monitor_index
        if idx < 1 or idx >= len(mons):
            idx = 1 if len(mons) > 1 else 0
        return mons[idx]

    def _loop(self) -> None:
        try:
            self.device.connect(release_driver=True)
            self.running = True
            log.info(
                "Ambilight started (%d LEDs @ %d fps, display %d, mpeg=%s)",
                self.led_count,
                self.fps,
                self.monitor_index,
                getattr(self._extractor, "use_mpeg", False),
            )
        except Exception as exc:
            self._emit_error(f"Connect failed: {exc}")
            self.running = False
            return

        try:
            with mss.MSS() as sct:
                last_key = None
                last_pick = 0.0
                browser_region = None
                browser_meta = ""
                browser_wid = 0
                while not self._stop.is_set():
                    t0 = time.perf_counter()
                    interval = 1.0 / max(1, int(self.fps))
                    try:
                        now_m = time.monotonic()
                        # Prefer Safari/Yandex window — ignore frontmost app / monitor
                        if now_m - last_pick > 0.8:
                            last_pick = now_m
                            try:
                                from browser_video import (
                                    browser_video_active,
                                    find_browser_video_target,
                                )

                                tgt = find_browser_video_target(sct)
                                if tgt and tgt.get("region"):
                                    new_reg = dict(tgt["region"])
                                    browser_meta = (
                                        f"{tgt.get('owner')} "
                                        f"{new_reg.get('width')}x{new_reg.get('height')}"
                                    )
                                    browser_wid = int(tgt.get("window_id") or 0)

                                    def _far(a, b) -> bool:
                                        if not a:
                                            return True
                                        return (
                                            abs(int(a.get("left", 0)) - int(b.get("left", 0))) > 48
                                            or abs(int(a.get("top", 0)) - int(b.get("top", 0))) > 48
                                            or abs(int(a.get("width", 0)) - int(b.get("width", 0))) > 96
                                            or abs(int(a.get("height", 0)) - int(b.get("height", 0))) > 96
                                        )

                                    if _far(browser_region, new_reg):
                                        log.info(
                                            "Ambilight → browser video %s wid=%s score=%.2f",
                                            browser_meta,
                                            browser_wid,
                                            float(tgt.get("content_score") or 0),
                                        )
                                        browser_region = new_reg
                                    if not getattr(self._extractor, "cinematic", False):
                                        self.set_cinematic(True)
                                    self._browser_video = browser_video_active(tgt)
                                else:
                                    if browser_region is not None:
                                        log.info("Ambilight ← browser gone, fallback monitor")
                                        self.set_cinematic(False)
                                    browser_region = None
                                    browser_meta = ""
                                    browser_wid = 0
                                    self._browser_video = False
                            except Exception as exc:
                                log.warning("browser target: %s", exc)
                                browser_region = None
                                browser_wid = 0

                        if browser_region:
                            key = (
                                "browser",
                                browser_wid or 0,
                                int(browser_region.get("left", 0)) // 64,
                                int(browser_region.get("top", 0)) // 64,
                                int(browser_region.get("width", 0)) // 96,
                                int(browser_region.get("height", 0)) // 96,
                            )
                            grab = browser_region
                        else:
                            if len(sct.monitors) > 2:
                                rich = pick_richest_monitor(
                                    sct, current=self.monitor_index
                                )
                                if rich != self.monitor_index:
                                    log.info(
                                        "Ambilight monitor %d → %d",
                                        self.monitor_index,
                                        rich,
                                    )
                                    self.monitor_index = rich
                            key = ("mon", self.monitor_index)
                            grab = self._resolve_monitor(sct)

                        if key != last_key:
                            last_key = key
                            self._extractor.cols = 0
                            self._extractor.rows = 0
                            self._extractor.reset_temporal()
                            if browser_region:
                                log.info("Ambilight source browser %s", browser_meta)
                                self.last_source = f"browser:{browser_meta}"
                            else:
                                self.last_source = f"mon:{self.monitor_index}"

                        # mss region first — CGWindow often returns privacy-grey on
                        # HW-decoded YouTube/FS video even when Screen Recording is ON.
                        raw_b = None
                        bw = bh = 0
                        try:
                            from bt_auth import frame_is_privacy_grey
                        except Exception:
                            frame_is_privacy_grey = None  # type: ignore

                        try:
                            shot = sct.grab(grab)
                            raw_b, bw, bh = shot.bgra, shot.width, shot.height
                            if frame_is_privacy_grey and frame_is_privacy_grey(
                                raw_b, bw, bh
                            ):
                                raw_b = None
                        except Exception as exc:
                            log.debug("mss grab: %s", exc)
                            raw_b = None
                        if raw_b is None and browser_wid > 0:
                            try:
                                from browser_video import capture_window_bgra

                                cap = capture_window_bgra(browser_wid)
                                if cap and not (
                                    frame_is_privacy_grey
                                    and frame_is_privacy_grey(cap[0], cap[1], cap[2])
                                ):
                                    raw_b, bw, bh = cap
                            except Exception as exc:
                                log.debug("window capture: %s", exc)
                        if raw_b is None:
                            # Last resort: whatever mss returns (may be privacy grey)
                            shot = sct.grab(grab)
                            raw_b, bw, bh = shot.bgra, shot.width, shot.height

                        privacy = bool(
                            frame_is_privacy_grey
                            and frame_is_privacy_grey(raw_b, bw, bh)
                        )
                        try:
                            step = max(4, (bw * bh * 4) // 2500)
                            rs = gs = bs = nn = 0
                            for i in range(0, len(raw_b) - 3, step):
                                bs += raw_b[i]
                                gs += raw_b[i + 1]
                                rs += raw_b[i + 2]
                                nn += 1
                            self.last_grab_mean = (
                                (rs // nn, gs // nn, bs // nn) if nn else (0, 0, 0)
                            )
                        except Exception:
                            self.last_grab_mean = (0, 0, 0)

                        def _chroma_ok(rgb) -> bool:
                            try:
                                r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
                            except Exception:
                                return False
                            mx, mn = max(r, g, b), min(r, g, b)
                            return mx >= 12 and (mx - mn) / max(1, mx) >= 0.08

                        grab_m = self.last_grab_mean
                        grab_vivid = _chroma_ok(grab_m)

                        # Privacy-grey only: hold last *colourful* frame.
                        # If grab itself has chroma, never hold — that was the
                        # overnight stuck-grey bug (grab green, room frozen @55).
                        if privacy and not grab_vivid and getattr(
                            self, "_last_good_paint", None
                        ):
                            paint = self._last_good_paint
                            room_top = self._last_good_room_top
                            room_bottom = self._last_good_room_bot
                            self.last_room_top = room_top
                            self.last_room_bottom = room_bottom
                            if self.on_frame:
                                try:
                                    self.on_frame(paint)
                                except Exception:
                                    pass
                            try:
                                self.device.send_sync_frame(paint)
                            except Exception:
                                pass
                            elapsed = time.perf_counter() - t0
                            delay = interval - elapsed
                            if delay > 0:
                                self._stop.wait(delay)
                            continue

                        colors, room_top, room_bottom = self._extractor.extract_bgra(
                            raw_b, bw, bh
                        )
                        self.last_room_top = room_top
                        self.last_room_bottom = room_bottom
                        if colors:
                            n = len(colors)
                            self.last_average = (
                                sum(c[0] for c in colors) // n,
                                sum(c[1] for c in colors) // n,
                                sum(c[2] for c in colors) // n,
                            )
                        cinema = bool(getattr(self._extractor, "cinematic", False)) or bool(
                            browser_region
                        )
                        if cinema:
                            paint = [
                                lift_for_leds(c, min_l=0.42, chroma_gain=1.9, punch=195)
                                for c in (colors or [])
                            ]
                        else:
                            paint = [
                                lift_for_leds(c, min_l=0.35, chroma_gain=1.5, punch=175)
                                for c in (colors or [])
                            ]

                        vivid = (
                            _chroma_ok(room_top)
                            or _chroma_ok(room_bottom)
                            or any(_chroma_ok(c) for c in (paint or [])[:8])
                        )
                        # Only cache colourful frames — never poison with grey
                        if vivid and not privacy:
                            self._last_good_paint = list(paint)
                            self._last_good_room_top = room_top
                            self._last_good_room_bot = room_bottom
                        elif privacy and not grab_vivid:
                            if getattr(self, "_last_good_paint", None):
                                paint = self._last_good_paint
                                room_top = self._last_good_room_top
                                room_bottom = self._last_good_room_bot
                                self.last_room_top = room_top
                                self.last_room_bottom = room_bottom
                            else:
                                elapsed = time.perf_counter() - t0
                                delay = interval - elapsed
                                if delay > 0:
                                    self._stop.wait(delay)
                                continue
                        if self.on_frame:
                            try:
                                self.on_frame(paint)
                            except Exception:
                                pass
                        self.device.send_sync_frame(paint)
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
