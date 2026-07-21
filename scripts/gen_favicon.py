#!/usr/bin/env python3
"""Generate the CiteVyn favicon raster assets from one geometric definition (#221).

Run from the repository root::

    uv run python scripts/gen_favicon.py

Writes ``favicon.ico`` (16+32+48), ``apple-touch-icon.png`` (180) and the manifest's
``icon-192.png`` / ``icon-512.png`` into ``frontend/public/``. ``favicon.svg`` is
hand-authored and is the *design* source of truth; this script mirrors the same geometry
so the raster fallbacks cannot drift from it silently —
``backend/tests/test_frontend_assets.py`` re-runs :func:`build` and asserts the committed
bytes still match, so a hand-edited binary fails the build.

Deliberately stdlib-only (``zlib`` + ``struct``). Pillow / ImageMagick / rsvg are not
available in this repo's toolchain or in CI, and adding an image dependency to render a
32x32 square is not a trade worth making. The mark is two analytic primitives, so exact
coverage is cheaper to compute than to rasterize with a general engine.

The mark
--------
A full-bleed rounded square in the brand highlighter yellow carrying a bold "C" with
squared (bracket-like) terminals. The tile supplies its own contrast field, which is what
makes it legible against BOTH light and dark browser chrome — a glyph-only mark would
disappear into one of them. No finer detail than a single letterform survives 16x16.
"""

from __future__ import annotations

import math
import pathlib
import struct
import zlib

# Brand tokens — frontend/src/styles/tokens.css (--hl light, --ink).
YELLOW = (0xFF, 0xD7, 0x5E)
INK = (0x1C, 0x1B, 0x19)

# Geometry, expressed on a 32x32 design grid (the SVG viewBox).
GRID = 32.0
CORNER_R = 7.0
CX = CY = 16.0
GLYPH_OUTER_R = 10.25
GLYPH_INNER_R = 5.75
# Half-angle of the "C" opening, measured from the +x axis. 50 degrees leaves a gap
# wide enough to read as a C rather than an O once the mark is only 16px wide.
OPENING_HALF_DEG = 50.0

SAMPLES = 6  # per-axis supersampling → 36 coverage samples per pixel


def _inside_rounded_square(x: float, y: float) -> bool:
    """Whether design-grid point ``(x, y)`` is inside the rounded tile."""
    if not (0.0 <= x <= GRID and 0.0 <= y <= GRID):
        return False
    # Clamp toward the nearest corner circle centre; inside the straight edges the
    # clamped delta is zero, so one expression covers edges and corners alike.
    dx = max(CORNER_R - x, 0.0, x - (GRID - CORNER_R))
    dy = max(CORNER_R - y, 0.0, y - (GRID - CORNER_R))
    return dx * dx + dy * dy <= CORNER_R * CORNER_R


def _inside_glyph(x: float, y: float) -> bool:
    """Whether design-grid point ``(x, y)`` is inside the "C" stroke."""
    dx, dy = x - CX, y - CY
    dist = math.hypot(dx, dy)
    if not (GLYPH_INNER_R <= dist <= GLYPH_OUTER_R):
        return False
    # Cut the opening on the +x side. atan2 is measured with y growing downward, which
    # is irrelevant here because the opening is symmetric about the x axis.
    return abs(math.degrees(math.atan2(dy, dx))) > OPENING_HALF_DEG


def render(size: int) -> bytes:
    """Render the mark at ``size``x``size`` as raw RGBA bytes."""
    scale = GRID / size
    step = 1.0 / (SAMPLES + 1)
    rows = bytearray()
    for py in range(size):
        for px in range(size):
            tile = glyph = 0
            for sy in range(1, SAMPLES + 1):
                for sx in range(1, SAMPLES + 1):
                    x = (px + sx * step) * scale
                    y = (py + sy * step) * scale
                    if _inside_rounded_square(x, y):
                        tile += 1
                        if _inside_glyph(x, y):
                            glyph += 1
            total = SAMPLES * SAMPLES
            if not tile:
                rows += bytes((0, 0, 0, 0))
                continue
            # Composite ink over yellow by glyph coverage, then apply tile coverage as
            # alpha so the rounded corners stay antialiased against the browser chrome.
            g = glyph / total
            rgb = tuple(round(INK[i] * g + YELLOW[i] * (1 - g)) for i in range(3))
            rows += bytes((*rgb, round(255 * tile / total)))
    return bytes(rows)


def _png(size: int, rgba: bytes) -> bytes:
    """Encode raw RGBA as a PNG (stdlib zlib, no filtering)."""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type 0 (None)
        raw += rgba[y * stride : (y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _ico(sizes: tuple[int, ...]) -> bytes:
    """Pack PNG-compressed entries into an ICO container."""
    pngs = [_png(s, render(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(header) + 16 * len(sizes)
    entries, blobs = bytearray(), bytearray()
    for size, png in zip(sizes, pngs, strict=True):
        # 0 in the width/height byte means 256; every size we ship is < 256.
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    return header + bytes(entries) + bytes(blobs)


def build() -> dict[str, bytes]:
    """The generated assets, keyed by filename. Pure — used by the drift test."""
    return {
        "favicon.ico": _ico((16, 32, 48)),
        # 180 = iOS home screen. 192/512 are the two sizes the web app manifest spec
        # expects; shipping the manifest without them would recreate exactly the
        # referenced-but-absent-asset bug this change exists to fix.
        "apple-touch-icon.png": _png(180, render(180)),
        "icon-192.png": _png(192, render(192)),
        "icon-512.png": _png(512, render(512)),
    }


def main() -> None:
    out = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "public"
    out.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (out / name).write_bytes(data)
        print(f"wrote {out / name} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
