#!/usr/bin/env python3
"""
MPEG-inspired video ambilight analyser.

Treats screen frames like a compressed video stream (not a UI screenshot):

  • Rec.709 YCbCr — same luma/chroma split as MPEG / H.264
  • Macroblock grid — like 16×16 coded blocks
  • SAD motion / scene-cut — P-frame blend vs I-frame snap

Outputs
  • Monitor USB — true edge ambilight (perimeter bands only)
  • Room ceiling — average of upper 40% of the picture
  • Room floor   — average of lower 60% of the picture

(MetalFX is an upscaler — not used here; region averages are the right tool.)
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

from color_math import average_oklab, lerp_oklab, weighted_average_oklab

log = logging.getLogger("synclight.mpeg")

RGB = Tuple[int, int, int]
YCbCr = Tuple[float, float, float]


# ── Rec.709 (HD video / MPEG) ────────────────────────────────────────────────

def rgb_to_ycbcr(r: int, g: int, b: int) -> YCbCr:
    """Full-range Rec.709 YCbCr (same matrix family as MPEG-2/H.264 HD)."""
    rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
    y = 0.2126 * rf + 0.7152 * gf + 0.0722 * bf
    cb = -0.1146 * rf - 0.3854 * gf + 0.5000 * bf
    cr = 0.5000 * rf - 0.4542 * gf - 0.0458 * bf
    return y, cb, cr


def ycbcr_to_rgb(y: float, cb: float, cr: float) -> RGB:
    r = y + 1.5748 * cr
    g = y - 0.1873 * cb - 0.4681 * cr
    b = y + 1.8556 * cb
    return (
        max(0, min(255, int(round(r * 255)))),
        max(0, min(255, int(round(g * 255)))),
        max(0, min(255, int(round(b * 255)))),
    )


def _sat_boost_ycbcr(y: float, cb: float, cr: float, gain: float = 1.35) -> RGB:
    """Chroma gain in YCbCr — keeps luma (video-safe), punches LED colour."""
    return ycbcr_to_rgb(y, cb * gain, cr * gain)


# ── Macroblock grid ──────────────────────────────────────────────────────────

class MpegFrameAnalyser:
    """
    Stateful analyser: feed BGRA frames, get LED colours + room mood.

    Scene cuts reset temporal state (I-frame). Between cuts, motion-adaptive
    blend acts like a light P-frame temporal filter.
    """

    def __init__(
        self,
        *,
        mb_cols: int = 20,
        mb_rows: int = 12,
        led_count: int = 65,
        chroma_gain: float = 1.38,
    ) -> None:
        self.mb_cols = max(8, int(mb_cols))
        self.mb_rows = max(6, int(mb_rows))
        self.led_count = max(1, int(led_count))
        self.chroma_gain = float(chroma_gain)
        self._prev_y: Optional[List[float]] = None
        self._prev_leds: Optional[List[RGB]] = None
        self._prev_room_t: Optional[RGB] = None
        self._prev_room_b: Optional[RGB] = None
        self._frame_w = 0
        self._frame_h = 0
        self._sticky_insets = (0, 0, 0, 0)
        self.last_motion = 0.0  # 0..1 average normalised SAD
        self.last_scene_cut = False
        self.last_flash = False

    def reset(self) -> None:
        self._prev_y = None
        self._prev_leds = None
        self._prev_room_t = None
        self._prev_room_b = None
        self._frame_w = 0
        self._frame_h = 0
        self._sticky_insets = (0, 0, 0, 0)
        self.last_motion = 0.0
        self.last_scene_cut = False
        self.last_flash = False

    def set_led_count(self, n: int) -> None:
        self.led_count = max(1, int(n))
        self._prev_leds = None

    @staticmethod
    def grid_for_frame(width: int, height: int, *, cinema: bool = True) -> Tuple[int, int]:
        """
        Macroblock grid matched to capture aspect.

        16:10 laptop / display, 16:9 TV, ultrawide film letterbox windows,
        and 4:3 all need different col/row density so edges hit the picture.
        """
        w = max(1, int(width))
        h = max(1, int(height))
        r = w / float(h)
        if r >= 2.25:  # ~2.35–2.4 film strip / cropped window
            return (32, 12) if cinema else (28, 10)
        if r >= 1.95:  # ~21:9
            return (30, 14) if cinema else (26, 12)
        if abs(r - 16.0 / 10.0) <= 0.10:  # 16:10 (1.6)
            return (28, 18) if cinema else (22, 14)
        if abs(r - 16.0 / 9.0) <= 0.10:  # 16:9
            return (28, 16) if cinema else (22, 12)
        if r <= 1.40:  # 4:3 / portrait-ish
            return (20, 16) if cinema else (16, 14)
        return (28, 16) if cinema else (20, 12)

    def _ensure_grid(self, width: int, height: int) -> None:
        """Rebuild macroblock layout when frame size / aspect changes."""
        if width == self._frame_w and height == self._frame_h:
            return
        cinema = self.mb_cols >= 24 or self.mb_rows >= 14
        cols, rows = self.grid_for_frame(width, height, cinema=cinema)
        if cols != self.mb_cols or rows != self.mb_rows:
            self.mb_cols, self.mb_rows = cols, rows
            self._prev_y = None
            self._sticky_insets = (0, 0, 0, 0)
        self._frame_w, self._frame_h = int(width), int(height)

    # ── capture macroblocks ──────────────────────────────────────────────

    def _macroblocks_bgra(
        self, data: bytes, width: int, height: int
    ) -> Tuple[List[RGB], List[float], List[YCbCr]]:
        """
        Average each macroblock → RGB + Y + full YCbCr.
        Skips near-black blocks for chroma (letterbox), keeps them for motion.
        """
        cols, rows = self.mb_cols, self.mb_rows
        cell_w = max(1, width // cols)
        cell_h = max(1, height // rows)
        # Subsample inside block (MPEG-ish — not every pixel)
        step = max(2, min(cell_w, cell_h) // 4)
        pitch = width * 4
        rgbs: List[RGB] = []
        ys: List[float] = []
        yccs: List[YCbCr] = []

        for br in range(rows):
            for bc in range(cols):
                x0 = bc * cell_w
                y0 = br * cell_h
                x1 = min(width, (bc + 1) * cell_w)
                y1 = min(height, (br + 1) * cell_h)
                rs = gs = bs = n = 0
                y = y0
                while y < y1:
                    row_off = y * pitch
                    x = x0
                    while x < x1:
                        o = row_off + x * 4
                        if o + 2 < len(data):
                            bs += data[o]
                            gs += data[o + 1]
                            rs += data[o + 2]
                            n += 1
                        x += step
                    y += step
                if n <= 0:
                    rgb = (0, 0, 0)
                else:
                    rgb = (rs // n, gs // n, bs // n)
                ycc = rgb_to_ycbcr(*rgb)
                rgbs.append(rgb)
                ys.append(ycc[0])
                yccs.append(ycc)
        return rgbs, ys, yccs

    def _letterbox_insets(self, ys: Sequence[float]) -> Tuple[int, int, int, int]:
        """
        Detect black bars (letterbox / pillarbox) in macroblock luma.

        Handles 16:9 film on 16:10 displays, ultrawide 2.35:1 bars, and
        pillarboxed 4:3. Caps inset so dark scenes don't eat half the frame.
        Sticky hysteresis avoids flicker on noisy bars.
        """
        cols, rows = self.mb_cols, self.mb_rows
        thr = 0.055  # true black bars (not dark cinematic grade)

        def row_dark(r: int) -> bool:
            sl = ys[r * cols : (r + 1) * cols]
            # Need almost the whole row black — dark film ≠ letterbox
            return sum(1 for v in sl if v < thr) >= max(2, (cols * 5) // 6)

        def col_dark(c: int) -> bool:
            sl = [ys[r * cols + c] for r in range(rows)]
            return sum(1 for v in sl if v < thr) >= max(2, (rows * 5) // 6)

        # Max bar depth: ~30% total (2.39:1 on 16:10 ≈ 25–30% bars)
        max_v = max(1, rows // 3)
        max_h = max(1, cols // 4)

        top = 0
        while top < max_v and row_dark(top):
            top += 1
        bot = 0
        while bot < max_v and row_dark(rows - 1 - bot):
            bot += 1
        left = 0
        while left < max_h and col_dark(left):
            left += 1
        right = 0
        while right < max_h and col_dark(cols - 1 - right):
            right += 1

        # Reject asymmetric junk (UI chrome on one side only)
        if top > 0 and bot == 0 and top <= 2:
            top = 0
        if bot > 0 and top == 0 and bot <= 2:
            bot = 0
        if left != right and abs(left - right) >= 2:
            left = right = min(left, right)

        # Keep a usable content window
        if top + bot >= rows - 4:
            top = bot = 0
        if left + right >= cols - 4:
            left = right = 0

        raw = (top, bot, left, right)
        # Sticky: grow immediately, shrink slowly (2 frames of proof)
        st = getattr(self, "_sticky_insets", (0, 0, 0, 0))
        out = []
        for i in range(4):
            if raw[i] >= st[i]:
                out.append(raw[i])
            elif raw[i] + 1 < st[i]:
                out.append(max(raw[i], st[i] - 1))
            else:
                out.append(st[i])
        self._sticky_insets = (out[0], out[1], out[2], out[3])
        return self._sticky_insets

    def _motion_and_cut(
        self, ys: Sequence[float]
    ) -> Tuple[float, bool, float]:
        """
        Returns (motion 0..1, scene_cut, flash_strength).
        Scene cut ≈ I-frame: mean SAD above threshold.
        Flash = sudden luma rise (explosions / cuts to white).
        """
        if self._prev_y is None or len(self._prev_y) != len(ys):
            self._prev_y = list(ys)
            return 0.0, True, 0.0  # first frame = I-frame

        n = len(ys)
        sad = 0.0
        d_luma = 0.0
        for i in range(n):
            d = ys[i] - self._prev_y[i]
            sad += abs(d)
            d_luma += d
        self._prev_y = list(ys)
        mean_sad = sad / max(1, n)  # Y is 0..1 → SAD mean ~0..1
        mean_dl = d_luma / max(1, n)
        motion = min(1.0, mean_sad / 0.12)
        # I-frame / scene cut: large structural change
        scene_cut = mean_sad > 0.18
        flash = max(0.0, min(1.0, (mean_dl - 0.08) / 0.25)) if mean_dl > 0.08 else 0.0
        return motion, scene_cut, flash

    def _edge_colors(
        self,
        rgbs: Sequence[RGB],
        yccs: Sequence[YCbCr],
        ys: Sequence[float],
        insets: Tuple[int, int, int, int],
    ) -> Tuple[List[RGB], List[RGB], List[RGB], List[RGB], RGB, RGB]:
        """
        Two outputs:

        1) Monitor USB — *real* ambilight: only screen-edge bands
           (top / bottom / left / right), like Philips bias lighting.

        2) Room BT — spatial bands of the *picture*:
           ceiling ← average of upper ~30% of the frame
           floor   ← average of lower ~35% of the frame
           (middle ignored so bands stay distinct and snappy)
        """
        cols, rows = self.mb_cols, self.mb_rows
        it, ib, il, ir = insets
        r0, r1 = it, rows - 1 - ib
        c0, c1 = il, cols - 1 - ir
        ch = max(1, r1 - r0 + 1)
        cw = max(1, c1 - c0 + 1)
        # Edge depth: deeper after letterbox crop so 16:10 / wide film still
        # paints from the picture, not residual bar pixels
        wide = cw > ch * 1.7
        depth_v = max(1, min(ch // 3, max(1, int(round(ch * (0.22 if wide else 0.18))))))
        depth_h = max(1, min(cw // 3, max(1, int(round(cw * (0.12 if wide else 0.15))))))
        # Room bands: top 30% ceiling / bottom 35% floor (gap in the middle)
        ceil_r1 = r0 + max(1, int(round(ch * 0.30)))
        floor_r0 = r1 - max(1, int(round(ch * 0.35)))
        ceil_r1 = min(r1 - 1, max(r0 + 1, ceil_r1))
        floor_r0 = max(ceil_r1 + 1, min(r1, floor_r0))
        split = ceil_r1  # used below as exclusive end of ceiling band
        gain = self.chroma_gain

        def pick(r: int, c: int) -> RGB:
            r = max(r0, min(r1, r))
            c = max(c0, min(c1, c))
            i = r * cols + c
            ycc = yccs[i]
            y = ys[i]
            # Skip residual letterbox / pillarbox cells only
            if y < 0.035:
                return (0, 0, 0)
            return _sat_boost_ycbcr(ycc[0], ycc[1], ycc[2], gain)

        def blend_cells(samples: List[Tuple[RGB, float]]) -> RGB:
            if not samples:
                return (0, 0, 0)
            return weighted_average_oklab(
                [s[0] for s in samples], [s[1] for s in samples]
            )

        # ── Monitor perimeter (true ambilight) ───────────────────────────
        top: List[RGB] = []
        bot: List[RGB] = []
        for c in range(c0, c1 + 1):
            ts: List[Tuple[RGB, float]] = []
            bs: List[Tuple[RGB, float]] = []
            for d in range(depth_v):
                ts.append((pick(r0 + d, c), 1.0 / (1.0 + 0.35 * d)))
                bs.append((pick(r1 - d, c), 1.0 / (1.0 + 0.35 * d)))
            top.append(blend_cells(ts))
            bot.append(blend_cells(bs))

        left: List[RGB] = []
        right: List[RGB] = []
        for r in range(r0, r1 + 1):
            ls: List[Tuple[RGB, float]] = []
            rs: List[Tuple[RGB, float]] = []
            for d in range(depth_h):
                ls.append((pick(r, c0 + d), 1.0 / (1.0 + 0.35 * d)))
                rs.append((pick(r, c1 - d), 1.0 / (1.0 + 0.35 * d)))
            left.append(blend_cells(ls))
            right.append(blend_cells(rs))

        # ── Room: upper ~30% → ceiling, lower ~35% → floor (fast, distinct) ─
        # Middle of the frame is ignored so ceiling/floor don't wash together.
        # Weight by chroma·luma so dark YouTube chrome / white UI don't wash
        # the mood into a muddy brown the LEDs barely show.
        def _pop_w(rgb: RGB) -> float:
            mx = max(rgb)
            mn = min(rgb)
            if mx < 14:
                return 0.0
            sat = (mx - mn) / max(1, mx)
            lum = mx / 255.0
            # Neutrals still contribute a little (brightness follow); colour wins
            if sat < 0.08:
                return 0.04 * (0.3 + 0.7 * lum)
            return max(0.08, (0.2 + 5.5 * sat * sat) * (0.3 + 0.7 * lum))

        def _vivid_mood(samples: List[Tuple[RGB, float]], fallback: RGB) -> RGB:
            if not samples:
                return fallback
            # Keep the most colourful quartile so grey UI can't wash the mood
            ranked = sorted(
                samples,
                key=lambda s: (max(s[0]) - min(s[0])) / max(1, max(s[0])),
                reverse=True,
            )
            keep = max(3, len(ranked) // 4)
            vivid = ranked[:keep]
            # If even the vivid set is grey, fall back to whole blend
            best_sat = (max(vivid[0][0]) - min(vivid[0][0])) / max(1, max(vivid[0][0]))
            if best_sat < 0.08:
                return blend_cells(samples) if samples else fallback
            return blend_cells(vivid)

        ceiling_samples: List[Tuple[RGB, float]] = []
        floor_samples: List[Tuple[RGB, float]] = []
        for r in range(r0, ceil_r1 + 1):
            for c in range(c0, c1 + 1):
                rgb = pick(r, c)
                pw = _pop_w(rgb)
                if pw <= 0.0:
                    continue
                dist = (r - r0) / max(1, ceil_r1 - r0)
                # Prefer the very top of the picture for ceiling
                w = (1.15 - 0.55 * dist) * pw
                cx = abs((c - c0) / max(1, cw - 1) - 0.5) * 2.0
                w *= 1.0 - 0.25 * cx
                ceiling_samples.append((rgb, max(0.05, w)))
        # Skip macOS Dock band (~5%) so floor mood tracks the picture, not chrome
        dock = max(0, int(round(ch * 0.05)))
        floor_r1 = max(floor_r0, r1 - dock)
        for r in range(floor_r0, floor_r1 + 1):
            for c in range(c0, c1 + 1):
                rgb = pick(r, c)
                pw = _pop_w(rgb)
                if pw <= 0.0:
                    continue
                dist = (r - floor_r0) / max(1, floor_r1 - floor_r0)
                # Prefer the very bottom of the picture for floor
                w = (0.70 + 0.45 * dist) * pw
                cx = abs((c - c0) / max(1, cw - 1) - 0.5) * 2.0
                w *= 1.0 - 0.20 * cx
                floor_samples.append((rgb, max(0.05, w)))

        fb_t = average_oklab(top) if top else (0, 0, 0)
        fb_b = average_oklab(bot) if bot else (0, 0, 0)
        mood_t = _vivid_mood(ceiling_samples, fb_t)
        mood_b = _vivid_mood(floor_samples, fb_b)
        return top, bot, left, right, mood_t, mood_b

    @staticmethod
    def _pad(edge: List[RGB], n: int) -> List[RGB]:
        if not edge:
            return [(0, 0, 0)] * n
        if len(edge) == n:
            return edge
        out: List[RGB] = []
        for i in range(n):
            idx = min(len(edge) - 1, int((i + 0.5) * len(edge) / n))
            out.append(edge[idx])
        return out

    def _to_leds(
        self,
        top: List[RGB],
        bot: List[RGB],
        left: List[RGB],
        right: List[RGB],
        *,
        edge_number: int = 3,
        reverse: bool = False,
    ) -> List[RGB]:
        """Map edges → SyncLight order, resample to led_count."""
        cols, rows = self.mb_cols, self.mb_rows
        top = self._pad(top, cols)
        bot = self._pad(bot, cols)
        left = self._pad(left, rows)
        right = self._pad(right, rows)

        border: List[RGB] = []
        if reverse:
            border.extend(reversed(right))
            border.extend(reversed(top))
            border.extend(reversed(left))
            if edge_number >= 4:
                border.extend(bot)
        else:
            # left(bottom→top) → top → right(top→bottom)
            border.extend(reversed(left))
            border.extend(top)
            border.extend(right)
            if edge_number >= 4:
                border.extend(reversed(bot))

        n = self.led_count
        if not border:
            return [(0, 0, 0)] * n
        src = len(border)
        out: List[RGB] = []
        for i in range(n):
            idx = min(src - 1, int((i + 0.5) * src / n))
            out.append(border[idx])
        return out

    def analyse_bgra(
        self,
        data: bytes,
        width: int,
        height: int,
        *,
        edge_number: int = 3,
        reverse: bool = False,
    ) -> Tuple[List[RGB], RGB, RGB, dict]:
        """
        Full MPEG-style pass.

        Returns (leds, room_top, room_bot, meta).
        meta: motion, scene_cut, flash, flash_strength
        """
        self._ensure_grid(width, height)
        rgbs, ys, yccs = self._macroblocks_bgra(data, width, height)
        insets = self._letterbox_insets(ys)
        motion, scene_cut, flash_s = self._motion_and_cut(ys)
        self.last_motion = motion
        self.last_scene_cut = scene_cut
        self.last_flash = flash_s > 0.25

        top, bot, left, right, mood_t, mood_b = self._edge_colors(
            rgbs, yccs, ys, insets
        )
        leds = self._to_leds(
            top, bot, left, right, edge_number=edge_number, reverse=reverse
        )

        # Temporal:
        #  • USB: flashes punch through; scene cuts snap; else motion-adaptive P-blend
        #  • Room: never chase flashes; real cuts (not flash) may snap mood
        first = self._prev_leds is None or len(self._prev_leds) != len(leds)
        # Only treat strong global brightening as a flash (muzzle / lightning).
        # Mild motion used to trip is_flash and freeze room mood on grey.
        is_flash = flash_s > 0.55
        real_cut = scene_cut and not is_flash  # white-outs ≠ scene change for room

        if first:
            out_leds = leds
            out_rt, out_rb = mood_t, mood_b
        else:
            a = 0.50 + 0.50 * motion
            if is_flash:
                a = min(1.0, a + 0.40 * flash_s)
            elif real_cut:
                a = 1.0  # I-frame snap on USB
            out_leds = [
                lerp_oklab(self._prev_leds[i], leds[i], a) for i in range(len(leds))
            ]
            if is_flash and self._prev_room_t is not None:
                # Hold room ambient through strong flashes only
                out_rt = self._prev_room_t
                out_rb = self._prev_room_b or mood_b
            elif real_cut:
                out_rt, out_rb = mood_t, mood_b
            else:
                # Room tracks video mood tightly (colour + brightness)
                ra = 0.42 + 0.55 * min(1.0, motion)
                out_rt = lerp_oklab(self._prev_room_t or mood_t, mood_t, ra)
                out_rb = lerp_oklab(self._prev_room_b or mood_b, mood_b, ra)

        self._prev_leds = list(out_leds)
        self._prev_room_t = out_rt
        self._prev_room_b = out_rb

        meta = {
            "motion": motion,
            "scene_cut": scene_cut,
            "flash": self.last_flash,
            "flash_strength": flash_s,
            "insets": insets,
        }
        return out_leds, out_rt, out_rb, meta
