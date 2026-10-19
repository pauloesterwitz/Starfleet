"""Throwaway probe: can a number widget cover the dashboard and then reveal it again?

The panel stores ONE theme and re-flashing takes 90 s, so a screensaver for the
"no Spark connected" state cannot be a second theme.  It has to live inside the
dashboard theme and be switched by a value.  The only thing on this device that a
value can switch is a number widget's atlas cell -- and whether that is enough
depends on firmware behaviour that no document describes:

  U1  does a number widget's box repaint RESTORE what was underneath, or fill it?
  U2  when two widgets overlap, which one draws on top (record order)?
  U3  after a cover goes blank, does STATIC TEXT underneath come back?
  U4  can cells of an atlas have DIFFERENT widths (cumulative vs fixed-stride)?
  U5  do channels beyond 21 work (22, 40, 100, 200)?
  U6  can two widgets be bound to the SAME channel?

This builds a theme that answers all six on the glass, and ``probe_send.py`` drives
it.  It is meant to be read by eye: every test is labelled on the panel and the
expected result is in ``EXPECTED`` below.

    ./.venv/bin/python probe_overlay.py <out-dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import build_theme as bt

# channels -- deliberately spread out to exercise U5
CH_COUNTER_UNDER, CH_COVER, CH_COUNTER_OVER, CH_MODE = 1, 2, 3, 4
CH_SHARED = 6
CH_WORDS = 7
CH_FAR = (22, 40, 100, 200)

COVER = (0, 96, 320, 236)          # x, y, w, h -- big on purpose (U1/U2/U3)
BLACK, WHITE = 0x000000, 0xFFFFFF

EXPECTED = """\
phase  what you should see if the firmware behaves the way the design needs
-----  --------------------------------------------------------------------
OFF    stripes + grid everywhere; STATIC TEXT at top-left of the middle band;
       counter A (left digits) and counter B (right digits) both counting
ON     a solid AMBER rectangle hides the stripes, the grid, the STATIC TEXT and
       counter A.  Counter B (defined AFTER the cover) stays visible on top.
OFF 2  the amber is gone and the stripes AND grid are back; STATIC TEXT is back;
       counter A is back.   <- the one that decides the whole design (U1/U3)
words  the three-word field cycles ONE / TWO / THREE, each complete (U4)
far    four small digits 1 2 3 4 along the bottom (U5)
shared two digits showing 5 5 (U6)
"""


def background() -> Image.Image:
    """Bands of strong colour with a 1px white grid: any erased region shows."""
    image = Image.new("RGB", (bt.PANEL_W, bt.PANEL_H), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    bands = [(200, 40, 40), (240, 170, 40), (40, 170, 80),
             (40, 170, 200), (60, 70, 220), (150, 90, 200)]
    band_h = bt.PANEL_H // len(bands)
    for i, colour in enumerate(bands):
        draw.rectangle((0, i * band_h, bt.PANEL_W, (i + 1) * band_h), fill=colour)
    for x in range(0, bt.PANEL_W, 40):
        draw.line((x, 0, x, bt.PANEL_H), fill=(255, 255, 255), width=1)
    for y in range(0, bt.PANEL_H, 40):
        draw.line((0, y, bt.PANEL_W, y), fill=(255, 255, 255), width=1)
    draw.text((6, 6), "BAKED INTO BACKGROUND", fill=(0, 0, 0),
              font=ImageFont.load_default(size=14))
    return image


def word_mask(word: str) -> Image.Image:
    font = bt.make_font(("DIN Condensed Bold", 18), WHITE)
    return bt.LIB.render._render_mask(word, bt.LIB.render.load_font(font))


def build(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    bg_path = out_dir / "probe_bg.bmp"
    background().save(bg_path)

    builder = bt.ThemeBuilder()
    cx, cy, cw, ch = COVER

    # --- widgets, in DRAW order: that order is what U2 measures ---------------
    builder.text("STATIC TEXT", ("DIN Condensed Bold", 22), BLACK, 10, cy + 6)      # U3
    builder.number(CH_COUNTER_UNDER, bt.F_VALUE, WHITE, 200, cy + 4, digits=2)      # A
    solid = Image.new("L", (cw, ch), 255)
    cover = bt.make_bitmap_atlas("cover", {1: solid}, ch)                           # U4
    builder.bitmap_field(CH_COVER, cover, bt.AMBER, cx, cy)
    builder.number(CH_COUNTER_OVER, bt.F_VALUE, WHITE, 200, cy + 100, digits=2)     # B

    # three words of DIFFERENT widths in cells 1..3 (U4)
    words = bt.make_bitmap_atlas(
        "words", {1: word_mask("ONE"), 2: word_mask("TWO"), 3: word_mask("THREE")}, 24)
    builder.bitmap_field(CH_WORDS, words, WHITE, 10, 330)

    # far channels (U5): plain digits, shown on the bottom band
    for i, channel in enumerate(CH_FAR):
        builder.number(channel, bt.F_VALUE, WHITE, 10 + i * 40, 400, digits=1)
    # one channel, two widgets (U6)
    builder.number(CH_SHARED, bt.F_VALUE, WHITE, 200, 330, digits=1)
    builder.number(CH_SHARED, bt.F_VALUE, WHITE, 240, 330, digits=1)
    builder.number(CH_MODE, bt.F_VALUE, WHITE, 280, 440, digits=1)

    parent = bt.WidgetParent(
        object_name="panel", widget_type=1,
        geometry=bt.Geometry(x=0, y=0, width=bt.PANEL_W, height=bt.PANEL_H),
        background_type=1, background_color_raw=f"{bt.OPAQUE | bt.BG_NAVY:08x}",
        background_color=bt.OPAQUE | bt.BG_NAVY,
        background_image_path=bg_path.name, image_delay=0)
    theme = bt.Theme(path="", widget_parents=[parent], widgets=builder.widgets)
    bundle = bt.ThemeBundle(ui_path="", base_dir=str(bg_path.parent), theme=theme,
                            startup_pic=None)
    data = bt.inject_rotate(bt.LIB.compiler.compile_theme_bundle(bundle), bt.ROTATE)
    out = out_dir / "img.dat"
    out.write_bytes(data)

    parsed = bt.real_records(bt.LIB.imgdat.parse_imgdat(data))
    kinds = {}
    for record in parsed:
        kinds[record.record_type] = kinds.get(record.record_type, 0) + 1
    print(f"probe theme : {out}  ({len(data):,} bytes)")
    print(f"records     : {len(parsed)}  {{{', '.join(f'0x{k:02x}: {v}' for k, v in sorted(kinds.items()))}}}")
    print(f"cover atlas : widths {cover.widths}, height {cover.height}")
    print(f"words atlas : widths {words.widths}, height {words.height}")
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("probe_out"))
    print()
    print(EXPECTED)
