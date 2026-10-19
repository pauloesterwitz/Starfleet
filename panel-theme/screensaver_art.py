"""Render the screensaver to a PNG without touching the device.

The artwork lives in ``build_theme.py`` (``screensaver_layers()``), because the
builder has to compile it into the theme and a second copy here would drift.  This
is only a viewer: it composites the same colour layers the widgets will draw.

    ./.venv/bin/python screensaver_art.py [out.png]
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

import build_theme as bt


def composite() -> Image.Image:
    canvas = Image.new("RGB", (bt.PANEL_W, bt.PANEL_H), bt._rgb(bt.BG_NAVY))
    for _name, colour, mask in bt.screensaver_layers():
        canvas.paste(Image.new("RGB", canvas.size, bt._rgb(colour)), (0, 0), mask)
    return canvas


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("screensaver_preview.png")
    composite().save(out)
    print(f"wrote {out}")
    total = 0
    for label, _colour, x, y, mask in bt.screensaver_pieces():
        total += mask.width * mask.height
        print(f"  {label:<12} at ({x:>3},{y:>3})  {mask.width:>3}x{mask.height:<3}  {mask.width * mask.height:>7,} bytes")
    print(f"  {'':<12} {'':<18}  {total:>7,} bytes of artwork")
