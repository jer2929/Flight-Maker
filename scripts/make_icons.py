#!/usr/bin/env python3
"""Generate the PWA icon set with no external imaging deps.

Draws a "go/no-go check" mark — a green check inside a blue ring on the app's
dark panel colour — and rasterises it to PNG at the sizes a PWA needs, using
super-sampled coverage for clean anti-aliased edges. Pure stdlib (zlib/struct)
so it runs anywhere, including hosts without Pillow/ImageMagick.

Run:  python scripts/make_icons.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"

# App palette (from web/style.css :root)
BG = (15, 20, 25)        # --bg  #0f1419
LINE = (59, 130, 246)    # --accent #3b82f6 (the "minimums" threshold line)
ARROW = (46, 200, 113)   # brightened --go: margin above your minimums
NOGO = (92, 30, 26)      # dark --nogo: the no-go zone below your minimums
TRANSPARENT = (0, 0, 0, 0)

# The minimums line sits in the lower third; everything below it is no-go.
MIN_Y = 0.620

SS = 3  # super-sampling factor per axis (3x3 = 9 samples/pixel)


def _len(x: float, y: float) -> float:
    return math.hypot(x, y)


def _seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from point P to segment AB."""
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    seg2 = vx * vx + vy * vy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / seg2))
    return _len(px - (ax + t * vx), py - (ay + t * vy))


def _sample(u: float, v: float, maskable: bool):
    """Return the RGBA colour at normalised coords (u,v) in [0,1].

    ``maskable`` packs the art into the central safe zone and fills the whole
    square edge-to-edge (the platform applies its own mask)."""
    # Scale/centre the artwork. Maskable keeps content within ~62% safe zone.
    scale = 0.62 if maskable else 1.0
    cx = cy = 0.5
    x = (u - cx) / scale + cx
    y = (v - cy) / scale + cy

    # Background panel: full-bleed for maskable, rounded squircle otherwise.
    if maskable:
        inside_bg = 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0
    else:
        r = 0.22
        dx = abs(u - 0.5) - (0.5 - r)
        dy = abs(v - 0.5) - (0.5 - r)
        d = _len(max(dx, 0.0), max(dy, 0.0)) - r
        inside_bg = d <= 0.0
    if not inside_bg:
        return TRANSPARENT

    art = 0.0 <= x <= 1.0  # within the (scaled) artwork band horizontally

    # Green up-arrow: the margin you keep ABOVE your minimums (go).
    arrow_half = 0.046
    d_arrow = min(
        _seg_dist(x, y, 0.50, 0.500, 0.50, 0.250),   # shaft
        _seg_dist(x, y, 0.50, 0.250, 0.385, 0.380),  # left head
        _seg_dist(x, y, 0.50, 0.250, 0.615, 0.380),  # right head
    )
    if d_arrow <= arrow_half:
        return (*ARROW, 255)

    # The "minimums" threshold: a dashed line across the lower third, like the
    # decision line on an approach. Dash pattern runs along x.
    line_half = 0.028
    dash_on = ((x - 0.5) / 0.150) % 1.0 < 0.60
    if art and 0.07 <= x <= 0.93 and abs(y - MIN_Y) <= line_half and dash_on:
        return (*LINE, 255)

    # Below the line = the no-go zone, tinted red.
    if art and y > MIN_Y + line_half:
        return (*NOGO, 255)

    return (*BG, 255)


def _render(size: int, maskable: bool) -> bytes:
    """Render to raw RGBA bytes with SSxSS super-sampling."""
    rows = bytearray()
    inv = 1.0 / size
    sub = [(s + 0.5) / SS for s in range(SS)]
    nsamp = SS * SS
    for py in range(size):
        rows.append(0)  # PNG filter type 0 (none) per scanline
        for px in range(size):
            ar = ag = ab = aa = 0
            for sy in sub:
                v = (py + sy) * inv
                for sx in sub:
                    u = (px + sx) * inv
                    c = _sample(u, v, maskable)
                    if len(c) == 4 and c[3] == 0:
                        continue
                    ar += c[0]; ag += c[1]; ab += c[2]; aa += 255
            # Average over all samples (uncovered samples contribute alpha 0).
            if aa == 0:
                rows.extend((0, 0, 0, 0))
            else:
                cov = aa // 255  # number of covered samples
                rows.extend((ar // cov, ag // cov, ab // cov, aa // nsamp))
    return bytes(rows)


def _png(size: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(raw, 9)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


def write_icon(name: str, size: int, maskable: bool = False) -> None:
    raw = _render(size, maskable)
    (WEB / name).write_bytes(_png(size, raw))
    print(f"  {name}  ({size}x{size}{', maskable' if maskable else ''})")


if __name__ == "__main__":
    print("Generating PWA icons into web/ ...")
    write_icon("icon-192.png", 192)
    write_icon("icon-512.png", 512)
    write_icon("icon-maskable-512.png", 512, maskable=True)
    write_icon("apple-touch-icon.png", 180)
    write_icon("favicon-32.png", 32)
    print("Done.")
