#!/usr/bin/env python3
"""
Perceptual colour math for SyncLight ambilight / room FX.

Uses OKLab (Björn Ottosson, 2020) — nearly perceptually uniform — plus
physically-motivated temporal filters so blends don't muddy or flash.

Key relations
-------------
sRGB → linear (IEC 61966-2-1):
    C_lin = C/12.92                         if C ≤ 0.04045
    C_lin = ((C + 0.055)/1.055)^2.4         otherwise

OKLab: linear RGB → LMS → cube-root → Lab-like (L, a, b).
Interpolation in OKLab keeps hue/chroma stable (RGB lerp dips through grey).

Temporal follow (1st-order lag ≡ RC circuit / Newton's cooling):
    dy/dt = (x − y) / τ
    y ← y + (1 − e^{−Δt/τ}) · (x − y)

Critically damped spring (ζ = 1) for snappy-but-smooth ambilight:
    ÿ + 2ωẏ + ω²(y − x) = 0
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]
OKLab = Tuple[float, float, float]  # L, a, b
OKLCh = Tuple[float, float, float]  # L, C, h(rad)


# ── sRGB transfer ────────────────────────────────────────────────────────────

def srgb_to_linear(c: float) -> float:
    c = max(0.0, min(1.0, c))
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    c = max(0.0, c)
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def rgb_to_linear(rgb: Sequence[float]) -> Tuple[float, float, float]:
    r, g, b = (float(rgb[0]) / 255.0, float(rgb[1]) / 255.0, float(rgb[2]) / 255.0)
    return srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b)


def linear_to_rgb(lin: Sequence[float]) -> RGB:
    r = int(round(max(0.0, min(1.0, linear_to_srgb(lin[0]))) * 255.0))
    g = int(round(max(0.0, min(1.0, linear_to_srgb(lin[1]))) * 255.0))
    b = int(round(max(0.0, min(1.0, linear_to_srgb(lin[2]))) * 255.0))
    return r, g, b


# ── OKLab (Ottosson) ─────────────────────────────────────────────────────────

def rgb_to_oklab(rgb: Sequence[float]) -> OKLab:
    lr, lg, lb = rgb_to_linear(rgb)
    # linear sRGB → LMS (M1)
    l = 0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb
    m = 0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb
    s = 0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb
    l_ = math.copysign(abs(l) ** (1.0 / 3.0), l) if l else 0.0
    m_ = math.copysign(abs(m) ** (1.0 / 3.0), m) if m else 0.0
    s_ = math.copysign(abs(s) ** (1.0 / 3.0), s) if s else 0.0
    # LMS' → OKLab (M2)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return L, a, b


def oklab_to_rgb(lab: Sequence[float]) -> RGB:
    L, a, b = float(lab[0]), float(lab[1]), float(lab[2])
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l = l_ * l_ * l_
    m = m_ * m_ * m_
    s = s_ * s_ * s_
    lr = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    lg = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    lb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return linear_to_rgb((lr, lg, lb))


def oklab_to_oklch(lab: Sequence[float]) -> OKLCh:
    L, a, b = float(lab[0]), float(lab[1]), float(lab[2])
    C = math.hypot(a, b)
    h = math.atan2(b, a)
    return L, C, h


def oklch_to_oklab(lch: Sequence[float]) -> OKLab:
    L, C, h = float(lch[0]), float(lch[1]), float(lch[2])
    return L, C * math.cos(h), C * math.sin(h)


# ── Easing ───────────────────────────────────────────────────────────────────

def smoothstep(t: float) -> float:
    """Hermite smoothstep: 3t² − 2t³ (C1 continuous)."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def smootherstep(t: float) -> float:
    """Perlin smootherstep: 6t⁵ − 15t⁴ + 10t³ (C2 continuous)."""
    t = max(0.0, min(1.0, t))
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


# ── Interpolation ────────────────────────────────────────────────────────────

def _lerp_hue(h0: float, h1: float, t: float) -> float:
    """Shortest-arc hue blend on a circle (radians)."""
    d = (h1 - h0 + math.pi) % (2.0 * math.pi) - math.pi
    return h0 + d * t


def lerp_oklab(a: Sequence[float], b: Sequence[float], t: float) -> RGB:
    """
    Blend two sRGB colours in OKLCh (cylindrical OKLab).

    Hue takes the short arc; chroma/lightness lerp linearly in LCh —
    avoids the grey mud of RGB and the hue flips of HSV.
    """
    t = max(0.0, min(1.0, float(t)))
    La, Ca, ha = oklab_to_oklch(rgb_to_oklab(a))
    Lb, Cb, hb = oklab_to_oklch(rgb_to_oklab(b))
    # Near-achromatic: don't spin hue from noise
    if Ca < 0.02 and Cb < 0.02:
        L = La + (Lb - La) * t
        return oklab_to_rgb((L, 0.0, 0.0))
    if Ca < 0.02:
        ha = hb
    elif Cb < 0.02:
        hb = ha
    L = La + (Lb - La) * t
    C = Ca + (Cb - Ca) * t
    h = _lerp_hue(ha, hb, t)
    return oklab_to_rgb(oklch_to_oklab((L, C, h)))


def lerp_oklab_eased(a: Sequence[float], b: Sequence[float], t: float) -> RGB:
    return lerp_oklab(a, b, smootherstep(t))


def mix_oklab(a: Sequence[float], b: Sequence[float], alpha: float) -> RGB:
    """Weighted mix toward b (alpha in 0..1) in OKLab — for EMA steps."""
    return lerp_oklab(a, b, alpha)


def average_oklab(colors: Iterable[Sequence[float]]) -> RGB:
    """Perceptual mean of colours (equal weight in OKLab)."""
    labs = [rgb_to_oklab(c) for c in colors]
    if not labs:
        return (0, 0, 0)
    n = float(len(labs))
    L = sum(x[0] for x in labs) / n
    a = sum(x[1] for x in labs) / n
    b = sum(x[2] for x in labs) / n
    return oklab_to_rgb((L, a, b))


def weighted_average_oklab(
    colors: Sequence[Sequence[float]], weights: Sequence[float]
) -> RGB:
    if not colors:
        return (0, 0, 0)
    wsum = float(sum(weights)) or 1.0
    L = a = b = 0.0
    for c, w in zip(colors, weights):
        lab = rgb_to_oklab(c)
        wf = float(w) / wsum
        L += lab[0] * wf
        a += lab[1] * wf
        b += lab[2] * wf
    return oklab_to_rgb((L, a, b))


def gaussian_weights(n: int, sigma: float = 0.85) -> List[float]:
    """
    Causal-ish weights for a short history window (newest last).
    w_i ∝ exp(−(n-1-i)² / (2σ²))
    """
    if n <= 1:
        return [1.0]
    out = []
    for i in range(n):
        d = float(n - 1 - i)
        out.append(math.exp(-(d * d) / (2.0 * sigma * sigma)))
    return out


# ── Temporal filters ─────────────────────────────────────────────────────────

def exp_alpha(dt: float, tau: float) -> float:
    """
    Exact discrete coefficient for dy/dt = (x−y)/τ.
    α = 1 − e^{−Δt/τ}; α→1 when τ→0 (instant), α→0 when τ→∞ (frozen).
    """
    if tau <= 1e-9:
        return 1.0
    dt = max(0.0, float(dt))
    return 1.0 - math.exp(-dt / float(tau))


class OklabFollower:
    """
    First-order lag in OKLab — silky ambilight / room follow.

    tau_s: time constant (seconds). ~0.045–0.08 feels cinematic;
    lower = snappier.
    """

    __slots__ = ("tau", "_lab", "_t")

    def __init__(self, tau_s: float = 0.055) -> None:
        self.tau = max(0.008, float(tau_s))
        self._lab: Optional[OKLab] = None
        self._t: Optional[float] = None

    def reset(self, rgb: Optional[Sequence[float]] = None) -> None:
        self._lab = rgb_to_oklab(rgb) if rgb is not None else None
        self._t = None

    @property
    def ready(self) -> bool:
        return self._lab is not None

    def rgb(self) -> RGB:
        if self._lab is None:
            return (0, 0, 0)
        return oklab_to_rgb(self._lab)

    def update(
        self,
        target: Sequence[float],
        now: Optional[float] = None,
        *,
        alpha_scale: float = 1.0,
        hold: bool = False,
    ) -> RGB:
        """
        Follow target in OKLab. hold=True freezes state (flash gate for room).
        alpha_scale < 1 slows catch-up further without changing τ.
        """
        import time as _time

        t = float(now if now is not None else _time.monotonic())
        lab_t = rgb_to_oklab(target)
        if self._lab is None or self._t is None:
            self._lab = lab_t
            self._t = t
            return oklab_to_rgb(lab_t)
        if hold:
            self._t = t
            return oklab_to_rgb(self._lab)
        dt = max(0.0, t - self._t)
        self._t = t
        a = exp_alpha(dt, self.tau) * max(0.0, min(1.0, float(alpha_scale)))
        L0, a0, b0 = self._lab
        self._lab = (
            L0 + (lab_t[0] - L0) * a,
            a0 + (lab_t[1] - a0) * a,
            b0 + (lab_t[2] - b0) * a,
        )
        return oklab_to_rgb(self._lab)


class CriticallyDampedFollower:
    """
    Critically damped harmonic oscillator in OKLab (ζ = 1):

        ÿ + 2ωẏ + ω²(y − x) = 0

    Settles without overshoot. ω = 2π·f_n; f_n ≈ 6–10 Hz for LEDs.
    """

    __slots__ = ("omega", "_y", "_v", "_t")

    def __init__(self, natural_hz: float = 8.0) -> None:
        self.omega = 2.0 * math.pi * max(1.0, float(natural_hz))
        self._y: Optional[List[float]] = None
        self._v = [0.0, 0.0, 0.0]
        self._t: Optional[float] = None

    def reset(self, rgb: Optional[Sequence[float]] = None) -> None:
        self._y = list(rgb_to_oklab(rgb)) if rgb is not None else None
        self._v = [0.0, 0.0, 0.0]
        self._t = None

    def update(self, target: Sequence[float], now: Optional[float] = None) -> RGB:
        import time as _time

        t = float(now if now is not None else _time.monotonic())
        xt = list(rgb_to_oklab(target))
        if self._y is None or self._t is None:
            self._y = xt
            self._v = [0.0, 0.0, 0.0]
            self._t = t
            return oklab_to_rgb(xt)
        dt = max(1e-4, min(0.05, t - self._t))
        self._t = t
        w = self.omega
        # Semi-implicit Euler, critically damped
        for i in range(3):
            err = self._y[i] - xt[i]
            acc = -2.0 * w * self._v[i] - w * w * err
            self._v[i] += acc * dt
            self._y[i] += self._v[i] * dt
        return oklab_to_rgb(self._y)


def boost_oklch(
    rgb: Sequence[float],
    *,
    chroma_gain: float = 1.28,
    light_gain: float = 1.06,
) -> RGB:
    """
    Saturation/brightness lift in OKLCh (perceptual) instead of HSV —
    preserves hue and avoids the neon crush of naive HSV boosts.
    """
    L, C, h = oklab_to_oklch(rgb_to_oklab(rgb))
    # Mild lift of darks (gamma-ish on L) so edge blacks still glow
    L = min(1.0, (L * light_gain) ** 0.96)
    C = min(0.4, C * chroma_gain)
    return oklab_to_rgb(oklch_to_oklab((L, C, h)))


def lift_for_leds(
    rgb: Sequence[float],
    *,
    min_l: float = 0.42,
    chroma_gain: float = 1.65,
    punch: int = 190,
    track_luma: bool = False,
) -> RGB:
    """
    Make colours visible on LED strips without inventing fake hues.

    Greys stay neutral. Real colours get chroma boost.
    punch>0 normalizes peak for USB edge strips; track_luma=True keeps
    scene brightness (dark video → dim LEDs, bright → bright).
    """
    r0 = max(0, min(255, int(round(float(rgb[0])))))
    g0 = max(0, min(255, int(round(float(rgb[1])))))
    b0 = max(0, min(255, int(round(float(rgb[2])))))
    mx = max(r0, g0, b0)
    mn = min(r0, g0, b0)
    if mx < 4:
        return (0, 0, 0)

    sat = (mx - mn) / max(1, mx)  # 0 = grey, 1 = pure
    L, C, h = oklab_to_oklch(rgb_to_oklab((r0, g0, b0)))

    # Neutral / UI chrome → grey that still follows luma
    if sat < 0.10 or C < 0.022:
        if track_luma:
            level = max(0, min(255, int(round(mx * 1.15))))
        else:
            floor = max(0, int(punch * 0.35)) if punch > 0 else 0
            level = max(floor, min(255, int(round(mx * 1.35 + 24))))
        return (level, level, level)

    if track_luma:
        # Chroma pop without flattening dark/bright scenes
        L = max(float(min_l), min(1.0, L * 1.12))
        C = min(0.42, max(C * chroma_gain, 0.04))
        return oklab_to_rgb(oklch_to_oklab((L, C, h)))

    L = max(float(min_l), min(1.0, L * 1.35))
    C = min(0.45, max(C * chroma_gain, 0.06))
    r, g, b = oklab_to_rgb(oklch_to_oklab((L, C, h)))
    peak = max(r, g, b, 1)
    if punch > 0 and peak < int(punch):
        scale = float(punch) / float(peak)
        r = min(255, int(round(r * scale)))
        g = min(255, int(round(g * scale)))
        b = min(255, int(round(b * scale)))
    return r, g, b
