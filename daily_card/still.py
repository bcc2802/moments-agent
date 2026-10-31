"""Seeded cinematic still used when a TMDB image is not available.

The picture is generated. It is not a frame from the film and is labeled as
such on the card.
"""
from __future__ import annotations

import math
from io import BytesIO


PALETTES = (
    ((186, 206, 214), (232, 214, 186), (92, 108, 112), (244, 214, 160)),
    ((168, 196, 206), (236, 206, 150), (122, 108, 78), (255, 214, 140)),
    ((176, 170, 196), (232, 186, 168), (112, 78, 86), (255, 196, 160)),
    ((150, 176, 198), (198, 210, 214), (70, 86, 102), (230, 236, 240)),
    ((150, 186, 176), (214, 206, 170), (64, 96, 90), (236, 220, 170)),
    ((120, 132, 148), (196, 160, 132), (62, 52, 48), (220, 170, 120)),
    ((198, 176, 168), (236, 196, 160), (120, 84, 64), (255, 210, 160)),
    ((132, 156, 176), (176, 196, 188), (48, 72, 78), (210, 220, 210)),
)


def _hash(ix: int, iy: int, seed: int) -> float:
    number = (ix * 374761393 + iy * 668265263 + seed * 1442695041) & 0xFFFFFFFF
    number = (number ^ (number >> 13)) * 1274126177 & 0xFFFFFFFF
    return (number & 0xFFFFFF) / 0xFFFFFF


def _smooth(value: float) -> float:
    return value * value * (3 - 2 * value)


def _noise(x: float, y: float, seed: int) -> float:
    x0, y0 = math.floor(x), math.floor(y)
    fx, fy = x - x0, y - y0
    sx, sy = _smooth(fx), _smooth(fy)
    n00 = _hash(x0, y0, seed)
    n10 = _hash(x0 + 1, y0, seed)
    n01 = _hash(x0, y0 + 1, seed)
    n11 = _hash(x0 + 1, y0 + 1, seed)
    return (n00 * (1 - sx) + n10 * sx) * (1 - sy) + (n01 * (1 - sx) + n11 * sx) * sy


def _fbm(x: float, y: float, seed: int) -> float:
    total, amplitude, frequency = 0.0, 0.5, 1.0
    for octave in range(4):
        total += amplitude * _noise(x * frequency, y * frequency, seed + octave * 17)
        amplitude *= 0.5
        frequency *= 2.0
    return total


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _grade(color: tuple[int, int, int]) -> tuple[int, int, int]:
    luma = (color[0] + color[1] + color[2]) / (3 * 255)
    if luma < 0.5:
        strength = (0.5 - luma) * 28
        shifted = (color[0] - strength * 0.4, color[1] + strength * 0.15, color[2] + strength * 0.45)
    else:
        strength = (luma - 0.5) * 24
        shifted = (color[0] + strength * 0.5, color[1] + strength * 0.2, color[2] - strength * 0.25)
    return tuple(max(0, min(255, int(channel))) for channel in shifted)


def generate_still(width: int, height: int, seed: int) -> bytes:
    """Return a PNG landscape graded like a film frame."""
    try:
        from PIL import Image
    except ImportError as exc:
        from daily_card.errors import DailyCardError
        raise DailyCardError("需要安装 Pillow：python -m pip install pillow") from exc

    palette = PALETTES[seed % len(PALETTES)]
    sky_top, sky_horizon, ground, sun = palette
    sample_w, sample_h = max(420, width // 2), max(280, height // 2)
    sun_x = sample_w * (0.62 if seed % 2 else 0.36)
    sun_y = sample_h * 0.40
    ridges = []
    for layer, (base, amp) in enumerate(((0.46, 0.05), (0.58, 0.06), (0.72, 0.05))):
        ridges.append([
            base + amp * _fbm((x / sample_w) * (1.4 + layer * 0.7), 0.15, seed + 11 * (layer + 1))
            for x in range(sample_w)
        ])
    hill_colors = (
        _mix(sky_horizon, ground, 0.28),
        _mix(sky_horizon, ground, 0.55),
        _mix(ground, (28, 24, 28), 0.35),
    )
    buffer = bytearray(sample_w * sample_h * 3)
    for y in range(sample_h):
        ny = y / sample_h
        row = y * sample_w * 3
        for x in range(sample_w):
            nx = x / sample_w
            color = _mix(sky_top, sky_horizon, min(1.0, ny / 0.62))
            cloud = _fbm(nx * 2.2 + 4, ny * 1.4, seed + 5)
            if ny < 0.58:
                color = _mix(color, (255, 255, 255), max(0.0, cloud - 0.55) * 0.65)
            dx = (x - sun_x) / sample_w
            dy = (y - sun_y) / sample_h
            dist = math.sqrt(dx * dx + dy * dy)
            glow = max(0.0, 1 - dist / 0.42) ** 2
            color = _mix(color, sun, min(1.0, glow * 0.75))
            if dist < 0.045:
                color = _mix(sun, (255, 248, 230), 0.72)
            for layer, ridge in enumerate(ridges):
                edge = ridge[x]
                if ny <= edge:
                    continue
                shade = hill_colors[layer]
                depth = min(1.0, (ny - edge) / 0.24)
                shade = _mix(shade, (22, 18, 24), 0.15 + depth * 0.45)
                if ny < edge + 0.018:
                    shade = _mix(color, shade, (ny - edge) / 0.018)
                grain = (_noise(nx * 5 + layer, ny * 2, seed + layer) - 0.5) * 14
                color = tuple(max(0, min(255, int(channel + grain))) for channel in shade)
            if ny > 0.86:
                color = _mix(color, (12, 10, 14), (ny - 0.86) / 0.14 * 0.45)
            vignette = max(0.72, 1 - ((nx - 0.5) ** 2 * 1.1 + (ny - 0.42) ** 2) * 0.85)
            color = _grade(tuple(max(0, min(255, int(channel * vignette))) for channel in color))
            buffer[row + x * 3:row + x * 3 + 3] = bytes(color)
    image = Image.frombytes("RGB", (sample_w, sample_h), bytes(buffer))
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    stream = BytesIO()
    image.save(stream, format="PNG", optimize=True)
    return stream.getvalue()
