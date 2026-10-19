"""Probe 2: which limit is it -- widgets in total, number widgets, or distinct channels?

The dashboard sits at exactly 40 widgets (19 static text + 21 number), 21 number
widgets and 21 distinct channels, and the 9 screensaver widgets bound to a 22nd
channel never draw.  Three different limits all land on those same numbers, and they
need different fixes.  This theme separates them:

  * the 19 static labels are BAKED INTO THE BACKGROUND, so total widgets fall to 21
  * four extra digit widgets reuse channels the dashboard already has (18..21)
  * five more digit widgets use brand-new channels (23..27)

Reading the glass:
    all nine digits visible   -> the limit was TOTAL widgets (40): bake the text, done
    only the four reused      -> the limit is DISTINCT channels (21)
    none of the nine          -> the limit is NUMBER widgets (21)

    ./.venv/bin/python probe_count2.py <out-dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

import build_theme as bt

REUSED = (18, 19, 20, 21)            # channels the dashboard already binds
NEW = (23, 24, 25, 26, 27)           # channels it does not
BAKED: list[tuple[Image.Image, int, int, int]] = []     # (mask, x, y, colour)


def baking_text(self, text, spec, color, x, y, *, center=False, right=False) -> int:
    """ThemeBuilder.text, minus the widget: draw the label into BAKED instead."""
    font = self._font(spec, color, text=text)
    rendered = bt.LIB.render.render_static_text_payload(
        text, font, vendor_mode=True, binary_threshold=160)
    if center:
        x = (bt.PANEL_W - rendered.width) // 2
    elif right:
        x -= rendered.width
    mask = Image.frombytes("L", (rendered.width, rendered.height), rendered.payload)
    BAKED.append((mask, x, y, color))
    self.placed.append(bt.Placed(f"baked {text!r}", x, y, rendered.width, rendered.height))
    return rendered.width


def build(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    bg_path = bt.build_background(out_dir / "starfleet_bg.bmp")
    bt.ThemeBuilder.text = baking_text
    builder = bt.build_widgets(screensaver=False)

    # the probe digits (z-order is irrelevant: they sit on the empty middle band)
    for i, channel in enumerate(REUSED):
        builder.number(channel, bt.F_VALUE, 0xFFFFFF, 70 + i * 50, 196, digits=1)
    for i, channel in enumerate(NEW):
        builder.number(channel, bt.F_VALUE, 0xFFFFFF, 50 + i * 50, 250, digits=1)

    background = Image.open(bg_path).convert("RGB")
    for mask, x, y, colour in BAKED:
        background.paste(Image.new("RGB", mask.size, bt._rgb(colour)), (x, y), mask)
    background.save(bg_path)

    bundle, _ = bt.build_bundle(bg_path, builder)
    data = bt.inject_rotate(bt.LIB.compiler.compile_theme_bundle(bundle), bt.ROTATE)
    out = out_dir / "img.dat"
    out.write_bytes(data)
    kinds = {}
    for w in bundle.theme.widgets:
        kinds[w.widget_type] = kinds.get(w.widget_type, 0) + 1
    print(f"{out}  {len(data):,} bytes | baked labels: {len(BAKED)} | widgets by type: {kinds} "
          f"| distinct channels: {len({w.sensor.fast_sensor for w in bundle.theme.widgets if w.sensor})}")
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]))
