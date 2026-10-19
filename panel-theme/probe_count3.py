"""Probe 3: is the limit on widget COUNT, or on atlas BYTES?

probe_count2.py: with the labels baked, 28 number widgets were serviced and the 29th
was not.  Raw atlas bytes do not explain it unless static text counts double, which
is a fit, not a finding.  Two explanations, opposite designs:

  count cap   -> big atlases are fine, widgets must be few
  byte budget -> widget count is free, but a full-screen cover cannot fit

This adds 28 widgets whose atlases are ~64 bytes each.  A count cap serves ~7 of them.
A byte budget serves all 28.  Rows are coloured so they can be counted from a photo.

    ./.venv/bin/python probe_count3.py <out-dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

import build_theme as bt
import probe_count2 as p2

COLOURS = (0xFF4040, 0xFFB634, 0x40FF80, 0x60B0FF)          # one per row of 7
CHANNELS = list(range(23, 51))                              # 28 widgets, one channel each
DOT = 8


def build(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    bg_path = bt.build_background(out_dir / "starfleet_bg.bmp")
    bt.ThemeBuilder.text = p2.baking_text
    builder = bt.build_widgets(screensaver=False)
    for i, channel in enumerate(CHANNELS):
        atlas = bt.make_bitmap_atlas(f"dot{i}", {1: Image.new("L", (DOT, DOT), 255)}, DOT)
        row, col = divmod(i, 7)
        builder.bitmap_field(channel, atlas, COLOURS[row], 40 + col * 36, 196 + row * 24)

    background = Image.open(bg_path).convert("RGB")
    for mask, x, y, colour in p2.BAKED:
        background.paste(Image.new("RGB", mask.size, bt._rgb(colour)), (x, y), mask)
    background.save(bg_path)

    bundle, _ = bt.build_bundle(bg_path, builder)
    data = bt.inject_rotate(bt.LIB.compiler.compile_theme_bundle(bundle), bt.ROTATE)
    out = out_dir / "img.dat"
    out.write_bytes(data)
    numbers = sum(1 for w in bundle.theme.widgets if w.widget_type == 5)
    print(f"{out}  {len(data):,} bytes | {numbers} number widgets "
          f"(21 dashboard + {len(CHANNELS)} dots of {DOT * DOT * 12 // 12 + 11 * DOT} bytes each)")
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]))
