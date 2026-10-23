#!/usr/bin/env python3
"""Build a PORTRAIT Starfleet "cluster status" theme (``img.dat``) for the
3.5" llhmi / llTechCo USB HID smart panel (USB 0483:0065).

    ./.venv/bin/python build_theme.py

Outputs (next to this script):
    img.dat             the compiled theme, ready to be flashed BY THE USER
    theme_preview.png   320x480 preview, decoded back OUT of img.dat
    header_zoom.png     4x blow-up of the header band, to judge the emblems
    theme_assets/       generated background bitmap

This script NEVER touches the hardware.  It does not import
``smartmonitor_hid.transport`` (YMODEM/upload/reset) at all -- see
``_load_compiler_modules()``, which loads only the pure-offline modules.

--------------------------------------------------------------------------
FILE FORMAT NOTES
--------------------------------------------------------------------------
``img.dat`` is:

    [ 64 slots x 64 bytes = 4096 byte widget block ][ resource blob ]

Offset 0 is a little-endian **slot count** (``SMARTMONITOR_DEFAULT_SLOT_COUNT
= 150``), NOT a record -- see ``inject_rotate()`` for the hardware evidence.
Widget records live in slots 1..63; the rotate record is one of them.
Resource payloads (images, text bitmaps, glyph atlases) live at offsets
>= 4096 and are referenced by absolute byte offset.

``struct hidss_widget_rotate`` (vendor/usb-smart-screen/inc/hidss.h):
    u8 type;      // 0x96
    u8 rotate;    // ENUM INDEX, not degrees: 0=0deg 1=90deg 2=180deg 3=270deg
    u8 padding[62];

All multi-byte record fields are BIG endian; resource pixel data is
little-endian RGB565, row major (matches ``tg/image.c:to_rgb_565()``).

--------------------------------------------------------------------------
THE WORD-ATLAS TRICK (status fields)
--------------------------------------------------------------------------
A number widget (record 0x92) renders a value by splitting it into decimal
digits and blitting one bitmap per digit out of a 12-cell glyph atlas.  The
atlas is just ``sum(glyph_widths) * glyph_height`` bytes of 8-bit coverage,
cells concatenated in order ``0123456789.-``, and the record carries the 12
cell widths as big-endian u16 (record bytes 22..45).

Nothing in that says the cells have to contain *digits*.  So six fields on
this panel put WORDS in the atlas and let a single-digit channel value select
one: the two session status fields (0=NONE 1=WORKING 2=STALLED 3=WAITING
4=IDLE) and, per node, a resident-model BRAND field with a VARIANT field drawn
flush against it.  See ``atlas_cell_words()`` and ``ThemeBuilder.word_field()``.

That brand/variant split is what gets the model roster onto the panel at all.
An atlas has twelve cells and a plain integer reaches ten of them, so ONE
field can name ten things -- fewer than the roster has families.  Two fields
side by side multiply instead of add: ten brands x ten variants covers all 28
members with nothing left over for a catch-all to swallow.  They cannot share
one field's two digits, because both digits index the SAME atlas -- "QWEN" and
"3.6" would have to be cells of one ten-cell set, and the brands alone fill
it.

Two properties make this safe rather than clever:

* The width field is u16, so an ~80px cell is nowhere near overflowing it,
  and the vendor's own digit atlas already uses *unequal* widths (digits 17,
  '.' 8, '-' 14) -- proof the firmware indexes cells through the width table
  rather than a fixed stride.
* Even so, every cell in a word atlas is padded to the SAME width.  Then
  cumulative-width indexing and fixed-stride indexing land on exactly the
  same bytes, so the trick does not depend on which one the firmware uses.

Any of cells 0..9 a field does not define holds a filler word, so an
out-of-range enum degrades visibly instead of drawing garbage, and cells 10/11
(the '.' and '-' slots, unreachable from a plain integer) hold a dash.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import sys
import types
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_PKG_DIR = SCRIPT_DIR / "vendor" / "smartmonitor_hid_lib" / "src" / "smartmonitor_hid"
ASSET_DIR = SCRIPT_DIR / "theme_assets"
OUT_IMGDAT = SCRIPT_DIR / "img.dat"
OUT_PREVIEW = SCRIPT_DIR / "theme_preview.png"
OUT_PREVIEW_SCREENSAVER = SCRIPT_DIR / "theme_preview_screensaver.png"
OUT_HEADER_ZOOM = SCRIPT_DIR / "header_zoom.png"

SLOT_SIZE = 64
SLOT_COUNT = 64                     # HIDSS_WIDGET_MAX
WIDGET_BLOCK_SIZE = SLOT_SIZE * SLOT_COUNT   # 4096
MAX_RECORDS = SLOT_COUNT - 1        # slot 0 is the compiler's header

RECORD_ROTATE = 0x96
ROTATE_0, ROTATE_90, ROTATE_180, ROTATE_270 = 0, 1, 2, 3

# CONFIRMED ON HARDWARE 2026-08-25.  The panel's canvas is natively 320x480
# portrait, so the layout below is already the right shape; ROTATE_180 is what
# turns it the right way up for how this panel is mounted.
#
# Rotation only takes effect when the rotate record sits among the REGULAR
# records -- see inject_rotate() for why, and do not "fix" it back to offset 0.
ROTATE = ROTATE_180
PANEL_W, PANEL_H = 320, 480


# --------------------------------------------------------------------------
# 1.  Load the compiler WITHOUT pulling in any hardware/transport code
# --------------------------------------------------------------------------
def _load_compiler_modules() -> types.SimpleNamespace:
    """Import smartmonitor_hid.{theme,render,imgdat,compiler} in isolation.

    ``import smartmonitor_hid`` would execute the package ``__init__``, which
    imports ``.transport`` (YMODEM upload / reset / flash).  We register a
    synthetic package pointing at the same directory and import only the
    offline modules, so the upload code paths are never even loaded.
    """
    pkg_name = "smhid_offline"
    spec = importlib.machinery.ModuleSpec(pkg_name, None, is_package=True)
    spec.submodule_search_locations = [str(LIB_PKG_DIR)]
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg

    def sub(name: str):
        full = f"{pkg_name}.{name}"
        sub_spec = importlib.util.spec_from_file_location(full, LIB_PKG_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(sub_spec)
        sys.modules[full] = module
        sub_spec.loader.exec_module(module)
        setattr(pkg, name, module)
        return module

    theme = sub("theme")
    render = sub("render")
    imgdat = sub("imgdat")
    compiler = sub("compiler")

    banned = [m for m in sys.modules if m.endswith((".transport", ".client", ".service"))]
    assert not banned, f"hardware modules must not be imported: {banned}"
    return types.SimpleNamespace(theme=theme, render=render, imgdat=imgdat, compiler=compiler)


LIB = _load_compiler_modules()
FontSpec = LIB.theme.FontSpec
Geometry = LIB.theme.Geometry
SensorSpec = LIB.theme.SensorSpec
Widget = LIB.theme.Widget
WidgetParent = LIB.theme.WidgetParent
Theme = LIB.theme.Theme
ThemeBundle = LIB.theme.ThemeBundle


# --------------------------------------------------------------------------
# 2.  macOS font resolution
# --------------------------------------------------------------------------
# render.py resolves fonts by shelling out to fontconfig's `fc-match`, which
# either is missing on macOS or resolves to something unrelated.  We replace
# render.load_font with an explicit table of real macOS font files.  DIN
# Condensed / DIN Alternate are the closest system stand-ins for the LCARS
# look (Swiss 911 Ultra Compressed).
FONT_FILES = {
    "DIN Condensed Bold": "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
    "DIN Alternate Bold": "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf",
    "Arial Narrow Bold": "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
}
FALLBACK_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def _mac_load_font(font, font_path=None, pixel_size=None):
    """Drop-in replacement for smartmonitor_hid.render.load_font (macOS)."""
    if font is None:
        raise ValueError("every widget in this theme must carry a FontSpec")
    path = font_path or FONT_FILES.get(font.name) or FALLBACK_FONT
    if not Path(path).is_file():
        raise FileNotFoundError(f"font file missing: {path}")
    size = pixel_size or LIB.render.points_to_pixels(font.size)
    return ImageFont.truetype(path, size)


LIB.render.load_font = _mac_load_font


# --------------------------------------------------------------------------
# 2b.  Antialiased static text
# --------------------------------------------------------------------------
# The library hard-binarises static text (`binary_threshold=160`, i.e.
# `255 if v >= 160 else 0`).  At these sizes that eats thin stems and leaves
# ragged, uneven letterforms -- what shows up on the panel as a "distorted"
# font with missing pixels.
#
# Nothing about the file format requires it.  The payload is 8-bit coverage
# either way (`Image.tobytes()` on an "L" image); binarising only forces those
# bytes to 0 or 255, it does not change the size or layout of the payload.  And
# the firmware demonstrably blits real 8-bit coverage already, because
# `render_number_glyph_payload` never binarises and every number on the panel
# goes through it.  So this is the same code path with smoother values.
#
# NOTE: compiler.py does `from .render import render_static_text_payload` -- a
# direct binding -- so patching `LIB.render` would NOT affect the compiled
# output.  The patch has to land in the compiler's own namespace.
TEXT_GAMMA = 0.85   # <1 thickens strokes, >1 thins them; 1.0 = raw coverage

_orig_static_text_payload = LIB.render.render_static_text_payload


def _static_text_payload(text, font, **kwargs):
    """render_static_text_payload with antialiasing kept and the vendor crop off.

    Two library behaviours are suppressed here:

    1. ``binary_threshold`` (see above) -- destroys antialiasing.
    2. ``vendor_mode`` -- for any text CONTAINING A SPACE it re-crops the image
       to ``int(textlength(text)) - 1``, i.e. the *advance* width less a pixel.
       But ``_render_mask`` has already sized the bitmap to the ink bounding box
       (``textbbox``) and drawn it flush at ``-left``, so that crop can only ever
       cut ink off the right-hand edge.  Measured on this theme's own labels:
       "CLUSTER STATUS" and "PWR W" lost 2px, "AGENT SESSIONS"/"GPU %"/"TEMP C"/
       "RAM %" 1px each -- which is why the final S of CLUSTER STATUS was
       visibly chopped.  "STARFLEET" was spared only because it has no space.
    """
    kwargs.pop("binary_threshold", None)
    kwargs.pop("vendor_mode", None)
    rendered = _orig_static_text_payload(text, font, binary_threshold=None,
                                         vendor_mode=False, **kwargs)
    if TEXT_GAMMA == 1.0:
        return rendered
    # LANCZOS-free path, but the same dimming applies: thin bright-on-dark
    # strokes read faint once antialiased, so lift coverage slightly.
    image = Image.frombytes("L", (rendered.width, rendered.height), rendered.payload)
    image = image.point(
        lambda value: max(0, min(255, round(((value / 255.0) ** TEXT_GAMMA) * 255)))
    )
    return type(rendered)(width=rendered.width, height=rendered.height,
                          payload=image.tobytes())


# Both the compiler (which produces the real payloads) and this script's own
# title measurement must agree, or the emblems get placed against a title
# extent that isn't the one on screen.
LIB.compiler.render_static_text_payload = _static_text_payload
LIB.render.render_static_text_payload = _static_text_payload


def px(points: int) -> int:
    """Qt point size -> pixel size at the compiler's assumed 96 dpi."""
    return LIB.render.points_to_pixels(points)


# --------------------------------------------------------------------------
# 3.  Palette + sensor channels
# --------------------------------------------------------------------------
BG_NAVY = 0x05060E      # near-black navy, the field colour of the whole panel
AMBER = 0xFFB634        # LCARS amber/gold -- Jean-Luc
VALUE_GOLD = 0xFFC966   # slightly lighter gold for live numbers
BLUE = 0x8FD0FF         # LCARS light blue, used for captions
LILAC = 0xC49BE0        # second node accent -- Kathryn
SALMON = 0xFF7A5C       # sessions accent
GOLD_DIM = 0x6B4A10     # hairline rules
BLUE_DIM = 0x2B5580

OPAQUE = 0xFF000000     # ARGB alpha byte; 0xFF == fully opaque on this device

# Arbitrary sensor channels 1..20 (HIDSS_SENSOR_KEY_MIN/MAX).  The firmware's
# original names (cpu_temperature, ...) are irrelevant -- the theme supplies
# the labels, the host just writes these keys with report type 0x02.
# KEEP IN SYNC WITH PanelController.swift AND README.md.
CH_JL_GPU, CH_JL_TEMP, CH_JL_RAM, CH_JL_PWR = 1, 2, 3, 4
CH_KT_GPU, CH_KT_TEMP, CH_KT_RAM, CH_KT_PWR = 5, 6, 7, 8
CH_AGENTS = 9
# Two most-recently-active agent sessions across both Sparks.
CH_S1_STATUS, CH_S1_AGE, CH_S1_DONE, CH_S1_TOTAL = 10, 11, 12, 13
CH_S2_STATUS, CH_S2_AGE, CH_S2_DONE, CH_S2_TOTAL = 14, 15, 16, 17
# Resident model per machine: a BRAND field with a VARIANT field beside it.
CH_JL_BRAND, CH_JL_VARIANT = 18, 19
CH_KT_BRAND, CH_KT_VARIANT = 20, 21
# Screensaver switch: 0 shows the dashboard, 1 covers it with the "Incoming
# Transmission" screen.  Every screensaver widget is bound to this ONE channel.
CH_SCREENSAVER = 22
MODEL_FIELD_PAIRS = ((CH_JL_BRAND, CH_JL_VARIANT), (CH_KT_BRAND, CH_KT_VARIANT))

# Status enum -> word, selected by the value the app sends on the status
# channel.  Index in this tuple IS the wire value.
STATUS_WORDS = ("NONE", "WORKING", "STALLED", "WAITING", "IDLE")
STATUS_FILLER = "UNKNOWN"   # atlas cells 5..9: enum values we never send
ATLAS_PUNCT = "-"           # atlas cells 10/11: the '.' and '-' slots

# Resident model, split across two adjacent fields.  Index IS the wire value.
#
# BRAND cells rasterise flush RIGHT and VARIANT cells flush LEFT, so the two
# always meet at the seam between the boxes with exactly WORD_CELL_PAD of gap
# between the ink: "QWEN" + "3.6" reads as one word, and a brand needing no
# qualifier simply ends at the seam.  Brand cell 0 is an em dash for "nothing
# resident"; variant cell 0 is deliberately EMPTY, which is what every
# unqualified brand selects.
#
# Between them these cover every member of the 28-model roster -- OTHER is a
# tripwire for a family added to llama-swap and not yet added here, not a
# bucket anything currently falls into.  Keep in sync with PanelController.swift.
# Cell 7 was HUNYUAN until 2026-10-23: no hy3 member is served any more, and
# the ten-cell ceiling leaves no free cell for Ornith-1.5-122B otherwise.
MODEL_BRANDS = ("—", "QWEN", "GEMMA4", "NEM", "MINIMAX",
                "DEEPSEEK", "GLM5.3", "ORNITH", "AUX", "OTHER")
MODEL_VARIANTS = ("", "3.5", "3.6", "3.8", "235B",
                  "3VL", "CASC", "OTRON", "31B", "TP2")

# Only used to draw theme_preview.png -- never written to the device.
SAMPLE_VALUES = {
    CH_JL_GPU: 96, CH_JL_TEMP: 71, CH_JL_RAM: 63, CH_JL_PWR: 112,
    CH_KT_GPU: 42, CH_KT_TEMP: 58, CH_KT_RAM: 37, CH_KT_PWR: 74,
    CH_AGENTS: 2,
    CH_S1_STATUS: 1, CH_S1_AGE: 3, CH_S1_DONE: 4, CH_S1_TOTAL: 9,      # WORKING
    CH_S2_STATUS: 3, CH_S2_AGE: 47, CH_S2_DONE: 12, CH_S2_TOTAL: 12,   # WAITING
    # A composed pair on one node and the widest brand on the other, so the
    # preview shows the seam and the longest string the field can hold.
    CH_SCREENSAVER: 0,                      # the dashboard; the second preview sets this to 1
    CH_JL_BRAND: 1, CH_JL_VARIANT: 2,                                  # QWEN3.6
    CH_KT_BRAND: 5, CH_KT_VARIANT: 9,                                  # DEEPSEEK TP2
}


# --------------------------------------------------------------------------
# 4.  Layout constants
# --------------------------------------------------------------------------
# Portrait 320x480.  Every coordinate is the TOP-LEFT of the rendered bitmap,
# which is how the firmware places both static text and number widgets.
#
# Vertical budget, from the top:
#     0.. 66   header      (title, subtitle, rule)
#    74..224   JEAN-LUC    (heading + resident model + 2x2 metric grid)
#   234..384   KATHRYN     (same)
#   388..468   AGENT SESSIONS (count + column captions + 2 session rows)
#
# The resident-model row costs no vertical space at all: the node heading only
# uses ~103px of a 284px-wide line, so the model caption and word ride on the
# heading line beside the machine name.  That is the only free space on the
# panel -- a node block cannot grow (its two metric rows already sit 2-4px
# apart) and adding a third row per node would have needed ~90px that does not
# exist.
MARGIN_X = 18
GUTTER = 18
COL_W = (PANEL_W - 2 * MARGIN_X - GUTTER) // 2          # 133
COL_X = (MARGIN_X, MARGIN_X + COL_W + GUTTER)           # 18, 169
PILL_X, PILL_W = 6, 8                                   # left accent bars

TITLE_Y = 8
SUBTITLE_Y = 50
HEADER_RULE_Y = 66
TITLE_TEXT = "STARFLEET"
# Emblems flanking the title.  The title ink is ~36px tall and the gutters
# either side of it are ~62px wide, so these sit comfortably clear of both the
# text and the rule at HEADER_RULE_Y.  The delta keeps its 50:80 aspect, so
# DELTA_SIZE is its HEIGHT and it comes out ~23px wide.
# Both are HEIGHTS; each mark keeps its own aspect (the UFP roundel is 1.30:1
# so it comes out 42px wide, the delta is 50:80 so it comes out 23px).
#
# 32 for the roundel, not the 36 that would fill the gutter: the emblem
# artwork is cropped flush to its ink, so its top row is already bright, and
# at 36 it would butt straight into the top bar (which ends at y=6) with no
# gap at all.  At 32, centred on the title's optical centre, it clears the bar
# by 2px and leaves ~10px either side.  The delta gets away with 36 because
# its tip tapers to a point -- its first rows are nearly empty.
FED_SIZE = 32
DELTA_SIZE = 36

NODE_TOP = (74, 234)            # heading y for JEAN-LUC / KATHRYN
NODE_RULE_DY = 30               # hairline under the node heading
NODE_ROW_DY = (36, 94)          # caption y, relative to heading y
NODE_VALUE_DY = 16              # value y, relative to its caption y
NODE_BLOCK_H = 150              # height of the left accent pill

# Resident model, sharing the heading line.  The offsets centre each item's
# INK on the machine name's ink (which runs top+0..top+23), not their boxes --
# the three fonts have very different descents.  The word box bottom lands at
# top+26, clear of the hairline at top+30.
NODE_MODEL_CAPTION_GAP = 6      # between the "MODEL" caption and the brand box
NODE_MODEL_CAPTION_DY = 5
NODE_MODEL_DY = 2

SECTION_RULE_Y = (226, 384)     # full-width rules closing each node block

# --- agent sessions block --------------------------------------------------
# Shrunk on request: the row font drops 22pt -> 18pt (29px boxes -> 24px), and
# the whole band goes from 388..477 to 388..468, leaving a real bottom margin
# instead of 3px.  Both rows are kept -- smaller, not fewer.
SESS_TOP = 388
SESS_COUNT_Y = 388              # the ch9 "how many are generating" number
SESS_COUNT_X = 118
SESS_CAPTION_Y = 392            # "AGENT SESSIONS" / "MIN" / "TODO"
SESS_RULE_Y = 414               # hairline between the captions and the rows
SESS_ROW_Y = (418, 444)         # top of each session row
SESS_PILL_BOTTOM = 470          # the left accent pill wraps the rows
SESS_STATUS_X = MARGIN_X        # word-atlas status field
SESS_AGE_X = 150                # minutes since last activity
SESS_TODO_X = 200               # todos done / total
SESS_GAP = 3                    # padding either side of the "/"

# Fonts: (family key, point size)
F_TITLE = ("DIN Condensed Bold", 36)
F_SUBTITLE = ("DIN Condensed Bold", 12)
F_NODE = ("DIN Condensed Bold", 24)
F_CAPTION = ("DIN Condensed Bold", 12)      # was 16pt -- shrunk to free room
F_VALUE = ("DIN Alternate Bold", 26)        # the big live numbers, unchanged
F_SESSION = ("DIN Condensed Bold", 18)      # session rows -- was 22pt
F_COUNT = ("DIN Condensed Bold", 18)
# 18pt fits the widest model word (NEMOTRON, 82px) in an 86px cell, which
# leaves 34px of right margin on the heading line.
F_MODEL = ("DIN Condensed Bold", 18)

NODES = (
    {
        "name": "JEAN-LUC",
        "accent": AMBER,
        "channels": (CH_JL_GPU, CH_JL_TEMP, CH_JL_RAM, CH_JL_PWR),
        "brand_channel": CH_JL_BRAND,
        "variant_channel": CH_JL_VARIANT,
    },
    {
        "name": "KATHRYN",
        "accent": LILAC,
        "channels": (CH_KT_GPU, CH_KT_TEMP, CH_KT_RAM, CH_KT_PWR),
        "brand_channel": CH_KT_BRAND,
        "variant_channel": CH_KT_VARIANT,
    },
)
# Cell order is row-major: (GPU, TEMP) then (RAM, PWR).
CELL_CAPTIONS = ("GPU %", "TEMP C", "RAM %", "PWR W")

SESSIONS = (
    (CH_S1_STATUS, CH_S1_AGE, CH_S1_DONE, CH_S1_TOTAL),
    (CH_S2_STATUS, CH_S2_AGE, CH_S2_DONE, CH_S2_TOTAL),
)


# --------------------------------------------------------------------------
# 5.  Word glyph atlas
# --------------------------------------------------------------------------
WORD_ATLAS_MARK = "|"           # FontSpec.text sentinel: "NONE|WORKING|..."
WORD_CELL_PAD = 4               # right-hand padding inside each uniform cell
ATLAS_CELLS = 12                # 0123456789.- -- the atlas is always 12 cells


@dataclass
class WordAtlas:
    """A 12-cell glyph atlas whose cells hold words instead of digits."""
    words: list[str]
    cell_w: int
    cell_h: int
    ink_boxes: list[tuple[int, int, int, int]]   # per-cell ink bbox, post-gamma
    cells: list[bytes]          # exact per-cell bytes, for byte-level checks
    report_count: int = 10      # leading cells that are real wire values
    align: str = "left"         # which cell edge the ink is flush against

    @property
    def payload(self) -> bytes:
        return b"".join(self.cells)

    @property
    def widths(self) -> list[int]:
        # Uniform on purpose: cumulative-width and fixed-stride indexing then
        # address exactly the same bytes.
        return [self.cell_w] * len(self.cells)


def atlas_cell_words(words: tuple[str, ...], filler: str) -> list[str]:
    """Pad a word list out to the atlas's 12 cells, in wire-value order.

    Cells 0..9 are what a single decimal digit can select; any the caller does
    not define get ``filler`` so an unexpected value degrades visibly instead
    of drawing garbage.  Cells 10/11 are the '.' and '-' slots, unreachable
    from a plain integer, so they get a dash.
    """
    cells = list(words)
    if len(cells) > 10:
        raise ValueError(f"a digit can only select 10 cells, got {len(cells)}")
    cells += [filler] * (10 - len(cells))
    cells += [ATLAS_PUNCT, ATLAS_PUNCT]
    assert len(cells) == ATLAS_CELLS
    return cells


# FontSpec.text -> cell alignment.  The compiler re-rasterises each atlas from
# the FontSpec alone (see _glyph_payload_dispatch), long after the builder that
# chose the alignment has returned, so it has to be recoverable from the text.
WORD_ATLAS_ALIGN: dict[str, str] = {}


def _word_mask(word: str, image_font):
    """``_render_mask`` sizes the bitmap to the ink bounding box, which the
    empty string has none of.  The blank variant cell -- what an unqualified
    brand selects -- gets a 1px transparent stand-in instead of a crash; it is
    the narrowest mask, so it never drives ``cell_w``."""
    if not word:
        return Image.new("L", (1, 1), 0)
    return LIB.render._render_mask(word, image_font)


def _apply_gamma(image: Image.Image, gamma: float | None) -> Image.Image:
    if gamma is None or gamma == 1.0:
        return image
    return image.point(
        lambda value: max(0, min(255, round(((value / 255.0) ** gamma) * 255)))
    )


def render_word_atlas(font: FontSpec, words: list[str], gamma: float | None = 1.4,
                      report_count: int = 10, align: str = "left") -> WordAtlas:
    """Rasterise ``words`` into a uniform-width glyph atlas.

    Mirrors ``render.render_number_glyph_payload``: same ``_render_mask`` (so
    every cell shares one baseline), same gamma, same concatenation order --
    only the cell contents and the uniform padding differ.

    ``align`` places the ink inside each cell.  "left" is what a standalone
    field wants, so its words all share a left edge.  "right" is for the brand
    half of a brand+variant pair: the ink then ends WORD_CELL_PAD short of the
    cell's right edge, which is exactly where the variant field's box begins,
    so the two halves meet at a constant gap whatever the brand's width.
    """
    assert align in ("left", "right"), align
    image_font = LIB.render.load_font(font)
    masks = [_word_mask(word, image_font) for word in words]
    cell_h = max(mask.height for mask in masks)
    cell_w = max(mask.width for mask in masks) + WORD_CELL_PAD

    cells: list[bytes] = []
    ink_boxes: list[tuple[int, int, int, int]] = []
    for mask in masks:
        cell = Image.new("L", (cell_w, cell_h), 0)
        # Right-aligned cells still keep WORD_CELL_PAD clear on the right --
        # that pad IS the inter-word gap once the variant field abuts them.
        left = 0 if align == "left" else cell_w - WORD_CELL_PAD - mask.width
        cell.paste(mask, (left, 0))
        cell = _apply_gamma(cell, gamma)
        cells.append(cell.tobytes())
        # Measured AFTER gamma, because gamma rounds the faintest antialiasing
        # to zero and can shave a column off the ink box.
        ink_boxes.append(cell.getbbox() or (0, 0, 0, 0))
    return WordAtlas(words=words, cell_w=cell_w, cell_h=cell_h,
                     ink_boxes=ink_boxes, cells=cells, report_count=report_count,
                     align=align)


def font_atlas_words(font: FontSpec | None) -> list[str] | None:
    """A word list smuggled through FontSpec.text, or None for a digit atlas.

    ``FontSpec.text`` is only read for static-text widgets, so for number
    widgets it is free real estate -- which is how the word list reaches the
    compiler without patching the compiler's own data model.
    """
    if font is None or not font.text or WORD_ATLAS_MARK not in font.text:
        return None
    return font.text.split(WORD_ATLAS_MARK)


_RENDER_DIGIT_ATLAS = LIB.render.render_number_glyph_payload


def _glyph_payload_dispatch(font, glyphs=LIB.render.DEFAULT_NUMBER_GLYPHS,
                            font_path=None, pixel_size=None, gamma=None):
    """Stand-in for render_number_glyph_payload that understands word atlases
    and bitmap atlases."""
    if font is not None and (font.text or "").startswith(BITMAP_ATLAS_MARK):
        atlas = BITMAP_ATLASES[font.text[len(BITMAP_ATLAS_MARK):]]
        return atlas.widths, atlas.height, atlas.payload
    words = font_atlas_words(font)
    if words is None:
        return _RENDER_DIGIT_ATLAS(font, glyphs, font_path, pixel_size, gamma)
    atlas = render_word_atlas(font, words, gamma=gamma,
                              align=WORD_ATLAS_ALIGN.get(font.text or "", "left"))
    return atlas.widths, atlas.cell_h, atlas.payload


# compiler.py did `from .render import render_number_glyph_payload`, so the
# name has to be replaced in the compiler's namespace, not render's.
LIB.compiler.render_number_glyph_payload = _glyph_payload_dispatch


# --------------------------------------------------------------------------
# 5b.  Bitmap glyph atlas: artwork, not text, in the atlas cells
# --------------------------------------------------------------------------
# A number widget draws ONE atlas cell per decimal digit of its value, and a cell
# is just an 8-bit coverage mask drawn in the widget's colour.  Nothing says the
# mask has to be a glyph, so a value can select a whole picture.  That is the
# only runtime switch this panel has: the theme is flashed (90 s) and static
# text, images and the background cannot be hidden, but a number widget can go
# from blank to a bitmap the moment its channel changes.
BITMAP_ATLAS_MARK = "\x01bitmap:"      # FontSpec.text prefix, then a registry key


@dataclass
class BitmapAtlas:
    """12 cells of coverage masks, keyed by the cell a value selects."""
    key: str
    widths: list[int]
    height: int
    payload: bytes
    cells: dict[int, Image.Image]            # only the cells that carry ink

    @property
    def box_width(self) -> int:
        return max(self.widths)


# registry key -> atlas, read back by _glyph_payload_dispatch at compile time
BITMAP_ATLASES: dict[str, BitmapAtlas] = {}
# channel -> the atlases bound to it, so validate() can check the compiled bytes.
# A list, because widgets may share a channel (the screensaver's do).
BITMAP_CHANNELS: dict[int, list[BitmapAtlas]] = {}


def make_bitmap_atlas(key: str, cells: dict[int, Image.Image], height: int,
                      uniform: bool = False) -> BitmapAtlas:
    """Build an atlas from ``{cell index: "L" mask}``.

    Every cell shares one ``height`` (the record carries a single glyph height).
    A cell left out is BLANK -- zero coverage -- and 1px wide, so a picture
    costs ``width * height`` bytes once rather than twelve times.  That is only
    safe if the firmware locates cell N by summing the width table.  The vendor's
    own digit atlas proves it for cells 10 and 11 but NOT for 0..9, whose digits
    are all 17px, so fixed-stride and cumulative indexing agree there.
    ``uniform=True`` pads every cell to the widest, which is correct under either
    reading, at twelve times the size.
    """
    assert all(0 <= index < ATLAS_CELLS for index in cells), cells.keys()
    widest = max((mask.width for mask in cells.values()), default=1)
    widths, chunks = [], []
    for index in range(ATLAS_CELLS):
        mask = cells.get(index)
        width = widest if uniform else (mask.width if mask else 1)
        canvas = Image.new("L", (width, height), 0)
        if mask is not None:
            assert mask.mode == "L" and mask.height <= height, (key, index, mask.size)
            canvas.paste(mask, (0, 0))
        widths.append(width)
        chunks.append(canvas.tobytes())
    atlas = BitmapAtlas(key=key, widths=widths, height=height,
                        payload=b"".join(chunks), cells=dict(cells))
    BITMAP_ATLASES[key] = atlas
    return atlas


# --------------------------------------------------------------------------
# 6.  Header emblems (static vector art, drawn into the background bitmap)
# --------------------------------------------------------------------------
def _rgb(color: int) -> tuple[int, int, int]:
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def quantise_rgb565(color: int) -> tuple[int, int, int]:
    """What a 24-bit colour looks like after the RGB565 round trip.

    The background is stored as RGB565, so BG_NAVY (0x05060E) comes back out
    of img.dat as (0, 4, 8).  Comparing decoded pixels against the original
    24-bit value would call every single pixel "ink".
    """
    red, green, blue = _rgb(color)
    return ((red >> 3) * 255 // 31, (green >> 2) * 255 // 63, (blue >> 3) * 255 // 31)


def _pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    raw = image.convert("RGB").tobytes()
    return [tuple(raw[index:index + 3]) for index in range(0, len(raw), 3)]



# Both marks are rasterised at SUPERSAMPLE x and reduced with LANCZOS.  At
# ~32px an aliased edge is mush, and the panel is RGB565, so the art is kept
# to flat fills over the flat BG_NAVY header field -- no gradients to band.
SUPERSAMPLE = 8

# --- the Starfleet delta ---------------------------------------------------
# Geometry lifted verbatim from assets/gen-starfleet-mark.swift, which
# transcribed it from the community delta-shield vector recreation.  The app
# icon and the menu bar icon draw this same path, so the panel matches them.
#
# ORIGIN WARNING: these are CoreGraphics coordinates (origin BOTTOM-left,
# already y-flipped once from the source SVG).  Pillow's origin is TOP-left,
# so every y goes through `DELTA_H - y` on the way in -- skip that and the
# delta comes out upside down.  _assert_delta_upright() is the tripwire.
DELTA_W, DELTA_H = 50.0, 80.0
DELTA_PATH_CG = (
    # (start, control1, control2, end)
    ((25.744, 80.0), (7.542, 54.103), (0.986, 29.184), (0.0, 0.0)),
    ((0.0, 0.0), (6.006, 5.85), (25.126, 29.151), (31.949, 30.501)),
    ((31.949, 30.501), (36.499, 31.402), (40.502, 25.914), (49.966, 7.904)),
    ((49.966, 7.904), (47.658, 33.220), (37.140, 63.422), (25.744, 80.0)),
)
BEZIER_STEPS = 96               # per curve, at supersampled scale


def _cubic_points(p0, c1, c2, p1, steps: int) -> list[tuple[float, float]]:
    """Flatten one cubic bezier -- Pillow has no curve primitive."""
    points = []
    for step in range(steps + 1):
        t = step / steps
        u = 1.0 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        points.append((
            a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0],
            a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1],
        ))
    return points


def delta_polygon(width: float, height: float) -> list[tuple[float, float]]:
    """The delta as one closed polygon, in Pillow (top-left origin) space."""
    scale_x, scale_y = width / DELTA_W, height / DELTA_H
    points: list[tuple[float, float]] = []
    for segment in DELTA_PATH_CG:
        for x, y in _cubic_points(*segment, steps=BEZIER_STEPS):
            points.append((x * scale_x, (DELTA_H - y) * scale_y))   # <- the flip
    return points


def _row_runs(mask: Image.Image, y: int, threshold: int = 128) -> list[tuple[int, int]]:
    """Contiguous ink runs on one scanline, as (start, end) pairs."""
    row = mask.crop((0, y, mask.width, y + 1)).tobytes()
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x, value in enumerate(row):
        if value >= threshold and start is None:
            start = x
        elif value < threshold and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, mask.width))
    return runs


def _assert_delta_upright(mask: Image.Image) -> None:
    """Fail loudly if the delta came out flipped.

    A correct delta-shield is a narrow tip at the TOP and two wing points at
    the BOTTOM with a notch between them.  Three independent tells:
      1. the first inked scanline is a single narrow run near the middle,
      2. some scanline low down splits into two runs (the notch),
      3. the bottom half carries much more ink than the top half.
    """
    width, height = mask.size
    runs_by_row = [_row_runs(mask, y) for y in range(height)]
    inked = [y for y, runs in enumerate(runs_by_row) if runs]
    if not inked:
        raise AssertionError("delta mask is empty")

    top_runs = runs_by_row[inked[0]]
    if len(top_runs) != 1:
        raise AssertionError(f"delta tip row has {len(top_runs)} runs, expected 1 (is it flipped?)")
    tip_centre = (top_runs[0][0] + top_runs[0][1]) / 2 / width
    if not 0.35 < tip_centre < 0.65:
        raise AssertionError(f"delta tip sits at x={tip_centre:.2f} of the width, expected centred")
    if (top_runs[0][1] - top_runs[0][0]) > width * 0.45:
        raise AssertionError("delta top row is wide -- that is a wing edge, so the mark is upside down")

    notched = [y for y, runs in enumerate(runs_by_row) if len(runs) >= 2]
    if not notched:
        raise AssertionError("delta has no notch scanline -- wings/notch missing or flipped")
    if min(notched) < height * 0.5:
        raise AssertionError("delta notch appears in the top half -- the mark is upside down")

    def ink(rows) -> int:
        return sum(end - start for row in rows for start, end in row)

    top_ink = ink(runs_by_row[:height // 2])
    bottom_ink = ink(runs_by_row[height // 2:])
    if top_ink >= bottom_ink:
        raise AssertionError(
            f"delta top half has {top_ink}px of ink vs {bottom_ink}px below -- upside down"
        )


def render_delta(height: int, color: int, background: int = BG_NAVY) -> Image.Image:
    """The Starfleet delta, LANCZOS-reduced from a SUPERSAMPLE x rasterisation."""
    width = max(1, round(height * DELTA_W / DELTA_H))
    big = Image.new("L", (width * SUPERSAMPLE, height * SUPERSAMPLE), 0)
    ImageDraw.Draw(big).polygon(
        delta_polygon(width * SUPERSAMPLE, height * SUPERSAMPLE), fill=255
    )
    _assert_delta_upright(big)

    mask = big.resize((width, height), Image.Resampling.LANCZOS)
    emblem = Image.new("RGB", (width, height), _rgb(background))
    emblem.paste(Image.new("RGB", (width, height), _rgb(color)), (0, 0), mask)
    return emblem


# --- the United Federation of Planets emblem -------------------------------
# ARTWORK PROVENANCE
#   theme_assets/ufp_flag.svg -- "United Federation of Planets Flag", from
#   Wikimedia Commons (File:United_Federation_of_Planets_Flag.svg), author
#   Peppo (2017), revised by Oren neu dag (2023).  PUBLIC DOMAIN: Commons
#   rates it ineligible for copyright ("consists entirely of information that
#   is common property"), and the author additionally released it PD
#   worldwide.
#   theme_assets/ufp_emblem_mask.png is that SVG rendered at 4x by headless
#   Chrome and cropped to the wreath + starfield roundel, dropping the flag
#   field and the "UNITED FEDERATION of PLANETS" wordmark.  See the README
#   for how to regenerate it.
#
# The PNG is a coverage mask: R, G, B and A all carry the same value.  Two
# things about it matter:
#
#   * Its floor is 37, not 0 -- the flag's blue field survives as a constant
#     ~14% haze over the whole rectangle.  Tinting straight through it stamps
#     a visibly lighter box onto the navy, so the floor is rescaled to zero
#     first (UFP_ALPHA_FLOOR).
#   * It is cropped flush to the ink -- the wreath touches all four edges, so
#     the emblem has no internal padding to borrow clearance from.
UFP_MASK = ASSET_DIR / "ufp_emblem_mask.png"
UFP_ALPHA_FLOOR = 37        # the mask's own background level; rescaled to 0
UFP_GAMMA = 0.85            # lifts the thin roundel ring, which LANCZOS dims
UFP_TINT = 0xEBF5FF         # near-white: the emblem's real white-on-blue


@lru_cache(maxsize=1)
def _ufp_alpha() -> Image.Image:
    """The emblem coverage mask, with its background floor rescaled to zero."""
    if not UFP_MASK.is_file():
        raise FileNotFoundError(f"UFP emblem mask missing: {UFP_MASK}")
    alpha = Image.open(UFP_MASK).split()[3]
    span = 255 - UFP_ALPHA_FLOOR
    return alpha.point(
        lambda value: max(0, min(255, round((value - UFP_ALPHA_FLOOR) * 255 / span)))
    )


def render_federation(height: int, color: int = UFP_TINT,
                      background: int = BG_NAVY) -> Image.Image:
    """The real UFP emblem, tinted and reduced to panel scale.

    One LANCZOS step straight from the full-size mask -- chaining resizes
    would soften it twice.  Unlike the delta this is not drawn at SUPERSAMPLE
    and reduced; the source is already ~29x the final height, which serves the
    same purpose.
    """
    alpha = _ufp_alpha()
    width = round(height * alpha.width / alpha.height)
    mask = alpha.resize((width, height), Image.Resampling.LANCZOS)
    if UFP_GAMMA != 1.0:
        mask = mask.point(
            lambda value: max(0, min(255, round(((value / 255.0) ** UFP_GAMMA) * 255)))
        )
    emblem = Image.new("RGB", (width, height), _rgb(background))
    emblem.paste(Image.new("RGB", (width, height), _rgb(color)), (0, 0), mask)
    return emblem


# --------------------------------------------------------------------------
# 7.  Background artwork
# --------------------------------------------------------------------------
def _lcars_end_blocks(draw: ImageDraw.ImageDraw, y: int, color: tuple[int, int, int]) -> None:
    for index in range(3):
        block_x = PANEL_W - MARGIN_X - 36 + index * 13
        draw.rectangle((block_x, y - 2, block_x + 9, y + 2), fill=color)


# Filled in by build_background(), consumed by validate() and the header zoom.
EMBLEM_BOXES: dict[str, tuple[int, int, int, int]] = {}
TITLE_INK_BOX: tuple[int, int, int, int] = (0, 0, 0, 0)


def make_font(spec: tuple[str, int], color: int, text: str = "") -> FontSpec:
    name, size = spec
    argb = OPAQUE | color
    return FontSpec(
        text=text, name=name, color_raw=f"{argb:08x}", color=argb, size=size,
        bold_value=1, italic_value=0, bold=True, italic=False,
    )


def measure_title() -> tuple[int, int, int, int]:
    """Where the title's INK actually lands, as (x, y, x2, y2).

    The emblems are placed against this rather than against eyeballed numbers:
    the title is centred, so its extent depends on the font metrics, and the
    mask is taller than the ink (it carries the font's full descent).
    """
    font = make_font(F_TITLE, AMBER, text=TITLE_TEXT)
    rendered = LIB.render.render_static_text_payload(
        TITLE_TEXT, font, vendor_mode=True, binary_threshold=160
    )
    x = (PANEL_W - rendered.width) // 2
    mask = Image.frombytes("L", (rendered.width, rendered.height), rendered.payload)
    left, top, right, bottom = mask.getbbox()
    return (x + left, TITLE_Y + top, x + right, TITLE_Y + bottom)


def _paste_emblem(image: Image.Image, emblem: Image.Image, name: str,
                  centre_x: int, centre_y: int) -> tuple[int, int, int, int]:
    """Stamp an emblem into the background and record where it went.

    The emblem is rendered ON the flat BG_NAVY field rather than composited
    through an alpha channel: LANCZOS over transparency fringes the edges, and
    the header gutters are uniform BG_NAVY anyway.  That is asserted here --
    if any chrome ever moves under an emblem, this fails instead of silently
    stamping a navy rectangle over it.
    """
    x = centre_x - emblem.width // 2
    y = centre_y - emblem.height // 2
    box = (x, y, x + emblem.width, y + emblem.height)
    existing = set(_pixels(image.crop(box)))
    if existing != {_rgb(BG_NAVY)}:
        raise AssertionError(f"{name} would cover non-background pixels at {box}: {existing}")
    image.paste(emblem, (x, y))
    EMBLEM_BOXES[name] = box
    return box


def build_background(path: Path) -> Path:
    """Draw the static LCARS chrome and save it as a 24bpp BMP.

    Saved as .bmp (not .png) on purpose: the compiler emits PNG backgrounds as
    3-byte ARGB565 and everything else as 2-byte RGB565, and the plain RGB565
    path is the one the C reference implementation confirms.

    All decoration lives in the left gutter and in the horizontal gaps -- the
    areas that sit behind text and live numbers are left flat BG_NAVY, so that
    a value redraw looks identical whether the firmware repaints the region
    from the background image or just fills it with the record's background
    colour.
    """
    image = Image.new("RGB", (PANEL_W, PANEL_H), _rgb(BG_NAVY))
    draw = ImageDraw.Draw(image)

    # --- top LCARS bar ----------------------------------------------------
    draw.rounded_rectangle((0, 0, 236, 6), radius=3, fill=_rgb(AMBER))
    draw.rounded_rectangle((244, 0, 282, 6), radius=3, fill=_rgb(SALMON))
    draw.rounded_rectangle((290, 0, PANEL_W - 1, 6), radius=3, fill=_rgb(BLUE))

    # --- rule under the header -------------------------------------------
    draw.rectangle((MARGIN_X, HEADER_RULE_Y, 236, HEADER_RULE_Y + 2), fill=_rgb(AMBER))
    draw.rectangle((244, HEADER_RULE_Y, PANEL_W - MARGIN_X, HEADER_RULE_Y + 2), fill=_rgb(BLUE))

    # --- per-node chrome --------------------------------------------------
    for node, top in zip(NODES, NODE_TOP):
        accent = _rgb(node["accent"])
        draw.rounded_rectangle(
            (PILL_X, top, PILL_X + PILL_W, top + NODE_BLOCK_H),
            radius=PILL_W // 2,
            fill=accent,
        )
        hair = top + NODE_RULE_DY
        draw.rectangle((MARGIN_X, hair, PANEL_W - MARGIN_X - 44, hair), fill=_rgb(GOLD_DIM))
        _lcars_end_blocks(draw, hair, accent)

    # --- section rules ----------------------------------------------------
    for rule_y in SECTION_RULE_Y:
        draw.rectangle((MARGIN_X, rule_y, PANEL_W - MARGIN_X, rule_y + 1), fill=_rgb(BLUE_DIM))

    # --- agent sessions block --------------------------------------------
    draw.rounded_rectangle(
        (PILL_X, SESS_TOP, PILL_X + PILL_W, SESS_PILL_BOTTOM),
        radius=PILL_W // 2,
        fill=_rgb(SALMON),
    )
    draw.rectangle(
        (MARGIN_X, SESS_RULE_Y, PANEL_W - MARGIN_X - 44, SESS_RULE_Y),
        fill=_rgb(GOLD_DIM),
    )
    _lcars_end_blocks(draw, SESS_RULE_Y, _rgb(SALMON))

    # --- header emblems ---------------------------------------------------
    # Static art, so they cost no records -- and being background pixels they
    # are also immune to the number-widget box repaint that the overlap check
    # in validate() guards against.  Centred in the gutters either side of the
    # title, on the title's own optical centre.
    global TITLE_INK_BOX
    TITLE_INK_BOX = measure_title()
    title_x, title_y, title_x2, title_y2 = TITLE_INK_BOX
    centre_y = (title_y + title_y2) // 2

    federation = render_federation(FED_SIZE)
    _paste_emblem(image, federation, "federation",
                  centre_x=(MARGIN_X + title_x) // 2, centre_y=centre_y)

    delta = render_delta(DELTA_SIZE, AMBER)
    _paste_emblem(image, delta, "delta",
                  centre_x=(title_x2 + PANEL_W - MARGIN_X) // 2, centre_y=centre_y)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


# --------------------------------------------------------------------------
# 7.  Widget construction
# --------------------------------------------------------------------------
@dataclass
class Placed:
    """Book-keeping for the layout report."""
    label: str
    x: int
    y: int
    w: int
    h: int


# channel -> the WordAtlas compiled into that channel's number widget, so
# validate() can check the bytes in the file against the words we meant.
WORD_ATLASES: dict[int, WordAtlas] = {}


class ThemeBuilder:
    # Static labels are drawn INTO THE BACKGROUND IMAGE rather than being widgets of
    # their own.  The panel stops servicing widgets past a limit (see MAX_WIDGETS), and
    # nineteen labels were what stood between the dashboard and room for the
    # screensaver.  They look the same -- the same masks, blended the same way -- and
    # they gain one thing: a number widget's repaint, or a cover going blank, restores
    # the background underneath, so a label baked in comes back by itself.
    bake_labels = True

    def __init__(self) -> None:
        self.widgets: list[Widget] = []
        self.placed: list[Placed] = []
        self.baked: list[tuple[Image.Image, int, int, int]] = []     # mask, x, y, colour
        self._next_id = 0

    _font = staticmethod(make_font)

    def _alloc_id(self) -> int:
        # The compiler writes widget_id = global_id + 1, so global_id must be
        # unique per widget and stay below 255 (HIDSS_WIDGET_KEY_MAX).
        global_id = self._next_id
        self._next_id += 1
        return global_id

    def text(self, text: str, spec, color: int, x: int, y: int, *,
             center: bool = False, right: bool = False) -> int:
        """Static text widget (type 2 -> record 0x93).  Returns its width.

        ``right`` reads ``x`` as the RIGHT edge instead of the left, for labels
        placed relative to something whose own width decided the layout.
        """
        font = self._font(spec, color, text=text)
        rendered = LIB.render.render_static_text_payload(
            text, font, vendor_mode=True, binary_threshold=160
        )
        if center:
            x = (PANEL_W - rendered.width) // 2
        elif right:
            x -= rendered.width
        if self.bake_labels:
            mask = Image.frombytes("L", (rendered.width, rendered.height), rendered.payload)
            self.baked.append((mask, x, y, color))
            self.placed.append(Placed(f"baked {text!r}", x, y, rendered.width, rendered.height))
            return rendered.width
        self.widgets.append(
            Widget(
                global_id=self._alloc_id(),
                same_type_id=-1,
                parent_name="panel",
                object_name=f"text_{len(self.widgets)}",
                widget_type=2,
                geometry=Geometry(x=x, y=y, width=rendered.width, height=rendered.height),
                font=font,
            )
        )
        self.placed.append(Placed(f"text {text!r}", x, y, rendered.width, rendered.height))
        return rendered.width

    def _number_widget(self, channel: int, font: FontSpec, x: int, y: int,
                       width: int, height: int, label: str, ink_w: int) -> None:
        self.widgets.append(
            Widget(
                global_id=self._alloc_id(),
                same_type_id=-1,
                parent_name="panel",
                object_name=f"value_ch{channel}",
                widget_type=5,
                geometry=Geometry(x=x, y=y, width=width, height=height),
                font=font,
                sensor=SensorSpec(
                    fast_sensor=channel,
                    sensor_type_name="cluster",
                    sensor_name=f"ch{channel}",
                    reading_name="value",
                    is_div_1204=False,
                ),
                # 0 == left aligned inside the widget box.
                raw_fields={"hAlign": "0"},
            )
        )
        self.placed.append(Placed(label, x, y, ink_w, height))

    def number(self, channel: int, spec, color: int, x: int, y: int, digits: int = 3) -> int:
        """Live number widget (type 5 -> record 0x92) bound to a sensor channel.

        ``digits`` sizes the box: the firmware appears to repaint the box when
        the value changes, so it has to be wide enough for the widest value
        this channel can produce, and must not overlap anything else.
        """
        font = self._font(spec, color)
        glyph_widths, glyph_height, _ = _glyph_payload_dispatch(font, gamma=1.4)
        digit_w = max(glyph_widths[:10])
        width = digit_w * digits
        self._number_widget(channel, font, x, y, width, glyph_height,
                            f"num ch{channel}", width)
        return width

    def word_field(self, channel: int, spec, color: int, x: int, y: int,
                   words: tuple[str, ...], filler: str, align: str = "left") -> int:
        """Live *word* widget: a number widget over a word glyph atlas.

        The box is the atlas cell width, which is the widest word plus
        WORD_CELL_PAD -- so no word can be clipped by construction, and
        validate() re-proves it against the compiled bytes.
        """
        cells = atlas_cell_words(words, filler)
        text = WORD_ATLAS_MARK.join(cells)
        WORD_ATLAS_ALIGN[text] = align
        font = self._font(spec, color, text=text)
        atlas = render_word_atlas(font, cells, gamma=1.4, report_count=len(words),
                                  align=align)
        WORD_ATLASES[channel] = atlas
        self._number_widget(channel, font, x, y, atlas.cell_w, atlas.cell_h,
                            f"word ch{channel}", atlas.cell_w)
        return atlas.cell_w


def _bitmap_field(self: "ThemeBuilder", channel: int, atlas: BitmapAtlas, color: int,
                  x: int, y: int, alpha: int = 0xFF) -> None:
    """Live bitmap widget: a number widget whose cells are ``atlas``.

    The box is the widest cell, so no picture can be clipped by construction.
    """
    font = FontSpec(
        text=BITMAP_ATLAS_MARK + atlas.key, name=F_NUMBER_FONT_NAME, size=12,
        color_raw=f"{(alpha << 24) | color:08x}", color=(alpha << 24) | color,
        bold_value=0, italic_value=0, bold=False, italic=False,
    )
    BITMAP_CHANNELS.setdefault(channel, []).append(atlas)
    self._number_widget(channel, font, x, y, atlas.box_width, atlas.height,
                        f"bitmap ch{channel}", atlas.box_width)


ThemeBuilder.bitmap_field = _bitmap_field
F_NUMBER_FONT_NAME = F_VALUE[0]     # unused by the dispatch, but the record wants a font


def word_field_width(spec, words: tuple[str, ...], filler: str) -> int:
    """The width ``ThemeBuilder.word_field`` will take, without placing it.

    Layout needs the two model fields' widths before either is built, to sit
    the pair against the right margin.  Alignment does not affect the width.
    """
    cells = atlas_cell_words(words, filler)
    font = make_font(spec, 0, text=WORD_ATLAS_MARK.join(cells))
    return render_word_atlas(font, cells, gamma=1.4).cell_w


# --------------------------------------------------------------------------
# 9a.  The screensaver: "Incoming Transmission", for when there is nothing to show
# --------------------------------------------------------------------------
# Shown when no DGX Spark is reachable, while the Mac is locked, and at start-up
# before the first status arrives.  After futurephonic's "LCARS - Incoming
# Transmission" wallpaper, re-composed for a 320x480 portrait panel.
#
# HOW IT SWITCHES.  The panel stores one theme and re-flashing takes 90 s, and
# nothing in the vendor's widget set hides or swaps anything.  What a value CAN do
# is pick a number widget's atlas cell -- so the screensaver is a set of widgets
# whose cell 0 is blank and whose cell 1 is a piece of the picture, all bound to
# CH_SCREENSAVER.  Value 0 leaves the dashboard showing; value 1 draws them on top.
#
# MEASURED ON HARDWARE 2026-10-19 (probe_overlay.py / probe_send.py), which is
# what makes this possible at all -- none of it is documented:
#   * overlapping widgets draw in DEFINITION order, later on top, static text
#     included: the screensaver widgets are built last for exactly that reason;
#   * a widget's repaint composites correctly over what is beneath it: no box, no
#     smear, no hole punched in a cover, and a widget UNDER a cover can keep
#     updating without showing through;
#   * after the cover goes blank the stripes, grid, static text and live digits
#     all come back, so no "poke" of the dashboard is needed on the way out;
#   * atlas cells may have DIFFERENT widths (the firmware sums the width table);
#   * channels well beyond 21 work (22, 40, 100, 200 tried) and several widgets
#     may share one channel.
# THE LARGEST NUMBER-WIDGET BOX PROVEN ON THIS PANEL is 320x236 (probe_overlay.py).
# A single 320x480 box -- a full-screen cover as one widget -- made the panel fall
# back to its bootloader screen, showing only "boot", on 2026-10-19; re-flashing
# the previous theme recovered it.  Why is not established: the atlas was 159 KB and
# the box 153,600 px, and either could be the limit (a repaint buffer is the obvious
# guess).  The total theme size is NOT the cause -- 662 KB against the vendor's own
# 4 MiB cap -- so the cover is built from stripes, each well inside what is proven,
# and validate() refuses any box bigger than the proven one.  The real boundary is
# somewhere between 236 and 480 rows and has not been looked for.
MAX_PROVEN_BOX = (320, 240)     # width, height
#
# FIRMWARE LIMITS, measured on 2026-10-19/20 with probe_count*.py.  The panel
# silently stops servicing widgets past some count, and refuses (shows only "boot")
# well past it.  Three separate limits are consistent with every observation, each
# reached first by a different probe -- these are the largest values SEEN WORKING,
# not documented caps, and validate() holds the build to them:
#
#   probe                        widgets  numbers  channels   result
#   dashboard, labels as widgets   40       21        21      the 9 extras NEVER drew
#   labels baked, + 9 digits       28       28        24      served 28 of 30 numbers
#   labels baked, + 28 dots        26       26        26      served 26 of 49 numbers
#
# so: <= 40 widgets in all, <= 28 NUMBER widgets, <= 26 DISTINCT channels.  The probes
# also showed byte size is NOT the constraint (tiny atlases got no further than big
# ones), and that widgets sharing a channel cost no extra channel.  34 or more number
# widgets made the panel fall back to its bootloader screen.
MAX_WIDGETS, MAX_NUMBERS, MAX_CHANNELS = 40, 28, 26
SS_COVER_STRIPES = 2            # 480 / 2 = 240 rows each: the proven maximum
SS_AMBER = AMBER                # the dashboard's own amber
SS_VIOLET = 0x9999FF            # classic LCARS periwinkle, as in the reference's bars
SS_DISC_LEVEL = 102             # the roundel's field: periwinkle at 40% coverage, ~(64,64,110)
SS_WHITE = 0xF4F4FF             # wreath and stars
SS_MARGIN = 14
SS_BAR_H = 18
SS_TOP_BAR_Y = 16
SS_EMBLEM_H, SS_EMBLEM_Y = 164, 46
SS_TITLE_Y = 226
SS_LINES_Y = 326
SS_BOTTOM_BAR_Y = PANEL_H - 16 - SS_BAR_H
SS_FONT = "DIN Condensed Bold"
# Rows each colour layer is cut into, one widget per band.  Chosen by hand, because the
# panel services at most MAX_NUMBERS widgets and the dashboard already uses 21, so the
# whole screensaver is SEVEN: two cover stripes plus five of these.  Every band is at
# most MAX_PROVEN_BOX tall, and a layer's bands are exactly where its ink is:
#   violet  the header bar and the roundel's field (rows 0-199), the lines and the footer bar (320-479)
#   amber   the header's label and cap (0-59), the title and the footer's caps (220-479)
#   white   the wreath and stars
SS_BANDS = {"violet": ((0, 200), (320, 480)), "amber": ((0, 60), (220, 480)),
            "white": ((0, 480),)}


def _ss_text(word: str, size: int) -> Image.Image:
    """Ink-tight coverage mask for ``word``, rendered the way the widgets render."""
    font = make_font((SS_FONT, size), SS_WHITE)
    return LIB.render._render_mask(word, LIB.render.load_font(font))


def _ss_bar(draw: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int,
            round_left: bool, round_right: bool) -> None:
    r = (y1 - y0) // 2
    draw.rectangle((x0 + (r if round_left else 0), y0, x1 - (r if round_right else 0), y1), fill=255)
    if round_left:
        draw.ellipse((x0, y0, x0 + 2 * r, y1), fill=255)
    if round_right:
        draw.ellipse((x1 - 2 * r, y0, x1, y1), fill=255)


def screensaver_layers() -> list[tuple[str, int, Image.Image]]:
    """(name, colour, full-canvas "L" mask) per colour.  Layers never overlap, so
    their drawing order is free -- which is deliberate: it is the one thing about
    overlapping widgets that is not obvious."""
    canvas = lambda: Image.new("L", (PANEL_W, PANEL_H), 0)     # noqa: E731
    amber, violet, white = canvas(), canvas(), canvas()
    da, dv = ImageDraw.Draw(amber), ImageDraw.Draw(violet)
    left, right, cap = SS_MARGIN, PANEL_W - SS_MARGIN, SS_BAR_H

    # header: [violet cap][LCARS ONLINE][======= violet bar =======][amber cap]
    y0, y1 = SS_TOP_BAR_Y, SS_TOP_BAR_Y + SS_BAR_H
    _ss_bar(dv, left, y0, left + cap, y1, True, False)
    label = _ss_text("LCARS ONLINE", 15)
    label_x = left + cap + 8
    amber.paste(255, (label_x, y0 + (SS_BAR_H - label.height) // 2), label)
    _ss_bar(da, right - cap, y0, right, y1, False, True)
    dv.rectangle((label_x + label.width + 8, y0, right - cap - 4, y1), fill=255)

    # the roundel: the wreath and stars in white over a field.  The field is the VIOLET
    # layer at partial coverage rather than a colour of its own -- a widget draws one
    # colour but each pixel of its mask has its own strength, so a dim violet disc costs
    # no widget, and widgets are what the panel runs out of.  Drawn 4x and reduced so
    # its edge is smooth.
    emblem = _ufp_alpha()
    width = round(SS_EMBLEM_H * emblem.width / emblem.height)
    emblem = emblem.resize((width, SS_EMBLEM_H), Image.Resampling.LANCZOS)
    white.paste(255, ((PANEL_W - width) // 2, SS_EMBLEM_Y), emblem)
    diameter = round(SS_EMBLEM_H * 0.84)      # measured off the reference: ~0.84 of the wreath's height
    cx, cy = PANEL_W // 2, SS_EMBLEM_Y + SS_EMBLEM_H // 2
    big = Image.new("L", (diameter * 4, diameter * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, diameter * 4 - 1, diameter * 4 - 1), fill=SS_DISC_LEVEL)
    violet.paste(big.resize((diameter, diameter), Image.Resampling.LANCZOS),
                 (cx - diameter // 2, cy - diameter // 2))   # nothing else violet is in this box

    # title, stacked: "INCOMING TRANSMISSION" does not fit across 320px in one line
    # Spaced by INK, not by the mask: the mask carries the font's own padding, which
    # left a 20px hole between the two lines.
    y = SS_TITLE_Y
    for word, size in (("INCOMING", 42), ("TRANSMISSION", 36)):
        mask = _ss_text(word, size)
        top, bottom = mask.getbbox()[1], mask.getbbox()[3]
        amber.paste(255, ((PANEL_W - mask.width) // 2, y - top), mask)
        y += bottom - top + 9

    # the three violet lines, reflowed for portrait
    y = SS_LINES_Y
    for line, gap in (("TO: CAPT. J.L. PICARD", 3), ("U.S.S. ENTERPRISE  \u2022  NCC-1701-E", 14),
                      ("FROM: STARFLEET COMMAND", 3), ("COMMAND AUTHORIZATION REQUIRED", 0)):
        mask = _ss_text(line, 17)
        violet.paste(255, ((PANEL_W - mask.width) // 2, y), mask)
        y += mask.height + gap

    # footer: [amber cap][======= violet bar =======][amber cap]
    y0, y1 = SS_BOTTOM_BAR_Y, SS_BOTTOM_BAR_Y + SS_BAR_H
    _ss_bar(da, left, y0, left + cap, y1, True, False)
    _ss_bar(da, right - cap, y0, right, y1, False, True)
    dv.rectangle((left + cap + 4, y0, right - cap - 4, y1), fill=255)

    return [("violet", SS_VIOLET, violet), ("amber", SS_AMBER, amber), ("white", SS_WHITE, white)]


def screensaver_pieces() -> list[tuple[str, int, int, int, Image.Image]]:
    """(label, colour, x, y, cropped mask), cover first.

    Each layer is cut into the bands of SS_BANDS and each band cropped to its ink.
    """
    stripe = PANEL_H // SS_COVER_STRIPES
    assert stripe * SS_COVER_STRIPES == PANEL_H and stripe <= MAX_PROVEN_BOX[1]
    pieces = [(f"cover {i}", BG_NAVY, 0, i * stripe, Image.new("L", (PANEL_W, stripe), 255))
              for i in range(SS_COVER_STRIPES)]
    for name, colour, mask in screensaver_layers():
        for y0, y1 in SS_BANDS[name]:
            band = mask.crop((0, y0, PANEL_W, y1))
            box = band.getbbox()
            assert box is not None, f"the {name} layer has no ink in rows {y0}-{y1}"
            assert box[3] - box[1] <= MAX_PROVEN_BOX[1], \
                f"{name} rows {y0}-{y1}: ink is {box[3] - box[1]} tall, over {MAX_PROVEN_BOX[1]}"
            # ink that falls OUTSIDE every band would silently go missing
            pieces.append((f"{name} y{y0}", colour, box[0], y0 + box[1],
                           band.crop((box[0], box[1], box[2], box[3]))))
    for name, _colour, mask in screensaver_layers():
        covered = Image.new("L", mask.size, 0)
        for y0, y1 in SS_BANDS[name]:
            covered.paste(255, (0, y0, PANEL_W, y1))
        stray = ImageChops.subtract(mask, covered).getbbox()
        assert stray is None, f"the {name} layer has ink outside its bands at {stray}"
    return pieces


def add_screensaver(builder: "ThemeBuilder") -> None:
    """Append the screensaver widgets.  They MUST be added last: later widgets draw
    on top, and the cover has to sit above every dashboard widget."""
    for label, colour, x, y, mask in screensaver_pieces():
        atlas = make_bitmap_atlas(f"ss-{label}", {1: mask}, mask.height)
        builder.bitmap_field(CH_SCREENSAVER, atlas, colour, x, y)


def build_widgets(screensaver: bool = True) -> ThemeBuilder:
    builder = ThemeBuilder()

    # --- header -----------------------------------------------------------
    builder.text(TITLE_TEXT, F_TITLE, AMBER, 0, TITLE_Y, center=True)
    builder.text("CLUSTER STATUS", F_SUBTITLE, BLUE, 0, SUBTITLE_Y, center=True)

    # --- one block per DGX Spark node -------------------------------------
    for node, top in zip(NODES, NODE_TOP):
        builder.text(node["name"], F_NODE, node["accent"], MARGIN_X, top)
        # Resident model, riding on the heading line beside the machine name:
        # BRAND then VARIANT, boxes abutting so the two halves read as one
        # word.  The pair is right-aligned to the margin and the caption fills
        # what is left, so a wider word list moves the caption rather than
        # silently running off the panel (which validate() would catch anyway).
        brand_w = word_field_width(F_MODEL, MODEL_BRANDS, MODEL_BRANDS[-1])
        variant_w = word_field_width(F_MODEL, MODEL_VARIANTS, MODEL_VARIANTS[0])
        brand_x = PANEL_W - MARGIN_X - brand_w - variant_w
        builder.text("MODEL", F_CAPTION, BLUE,
                     brand_x - NODE_MODEL_CAPTION_GAP, top + NODE_MODEL_CAPTION_DY,
                     right=True)
        builder.word_field(node["brand_channel"], F_MODEL, node["accent"],
                           brand_x, top + NODE_MODEL_DY,
                           MODEL_BRANDS, MODEL_BRANDS[-1], align="right")
        builder.word_field(node["variant_channel"], F_MODEL, node["accent"],
                           brand_x + brand_w, top + NODE_MODEL_DY,
                           MODEL_VARIANTS, MODEL_VARIANTS[0], align="left")
        for index, (caption, channel) in enumerate(zip(CELL_CAPTIONS, node["channels"])):
            col_x = COL_X[index % 2]
            caption_y = top + NODE_ROW_DY[index // 2]
            builder.text(caption, F_CAPTION, BLUE, col_x, caption_y)
            builder.number(channel, F_VALUE, VALUE_GOLD, col_x,
                           caption_y + NODE_VALUE_DY, digits=3)

    # --- agent sessions: header line, then two rows -----------------------
    builder.text("AGENT SESSIONS", F_CAPTION, BLUE, MARGIN_X, SESS_CAPTION_Y)
    builder.number(CH_AGENTS, F_COUNT, SALMON, SESS_COUNT_X, SESS_COUNT_Y, digits=2)
    builder.text("MIN", F_CAPTION, BLUE, SESS_AGE_X, SESS_CAPTION_Y)
    builder.text("TODO", F_CAPTION, BLUE, SESS_TODO_X, SESS_CAPTION_Y)

    for (status_ch, age_ch, done_ch, total_ch), row_y in zip(SESSIONS, SESS_ROW_Y):
        builder.word_field(status_ch, F_SESSION, SALMON, SESS_STATUS_X, row_y,
                           STATUS_WORDS, STATUS_FILLER)
        builder.number(age_ch, F_SESSION, BLUE, SESS_AGE_X, row_y, digits=4)
        done_w = builder.number(done_ch, F_SESSION, BLUE, SESS_TODO_X, row_y, digits=2)
        slash_x = SESS_TODO_X + done_w + SESS_GAP
        slash_w = builder.text("/", F_SESSION, BLUE_DIM, slash_x, row_y)
        builder.number(total_ch, F_SESSION, BLUE,
                       slash_x + slash_w + SESS_GAP, row_y, digits=2)

    # LAST, on purpose: widgets draw in definition order and these cover the rest.
    # (``screensaver=False`` is for probe_count.py, which needs the bare dashboard.)
    if screensaver:
        add_screensaver(builder)
    return builder


def bake_into_background(path: Path, builder: "ThemeBuilder") -> None:
    """Paint the builder's baked labels onto the saved background image."""
    if not builder.baked:
        return
    image = Image.open(path).convert("RGB")
    for mask, x, y, colour in builder.baked:
        image.paste(Image.new("RGB", mask.size, _rgb(colour)), (x, y), mask)
    image.save(path)


def build_bundle(background: Path, builder: "ThemeBuilder | None" = None) -> tuple[ThemeBundle, "ThemeBuilder"]:
    parent = WidgetParent(
        object_name="panel",
        widget_type=1,
        geometry=Geometry(x=0, y=0, width=PANEL_W, height=PANEL_H),
        # backgroundType != 0 -> the compiler clears `is_color`, so the device
        # uses the image.  The colour is still stored, and we keep it equal to
        # the image's field colour so a solid-colour repaint is invisible.
        background_type=1,
        background_color_raw=f"{OPAQUE | BG_NAVY:08x}",
        background_color=OPAQUE | BG_NAVY,
        background_image_path=background.name,
        image_delay=0,
    )
    builder = builder or build_widgets()
    bake_into_background(background, builder)
    theme = Theme(path="", widget_parents=[parent], widgets=builder.widgets)
    return ThemeBundle(ui_path="", base_dir=str(background.parent), theme=theme,
                       startup_pic=None), builder


# --------------------------------------------------------------------------
# 8.  Rotate record injection
# --------------------------------------------------------------------------
def inject_rotate(data: bytes, rotate: int) -> bytes:
    """Append ``struct hidss_widget_rotate`` as a REGULAR record.

    CONFIRMED ON HARDWARE 2026-08-25.  The two reference implementations
    disagree about offset 0, and for THIS firmware the Python library is the
    one that's right:

    * Buren's C generator (``tg/output.c:format_image``) writes the rotate
      record at offset 0.  Putting it there does NOTHING on this panel --
      ROTATE_90 and ROTATE_270 (180 degrees apart) rendered identically, and a
      cold power-cycle didn't change it either.
    * ``smartmonitor_hid_lib`` treats offset 0 as a little-endian slot count
      (``SMARTMONITOR_DEFAULT_SLOT_COUNT = 150``).  Leaving it alone and
      putting the rotate record in the first free slot instead DOES work.

    So: leave slot 0 as the compiler wrote it, and drop the rotate record into
    the first unused slot in 1..63.
    """
    assert len(data) >= WIDGET_BLOCK_SIZE, "compiled theme is shorter than the widget block"
    out = bytearray(data)
    for index in range(1, SLOT_COUNT):
        start = index * SLOT_SIZE
        if any(out[start:start + SLOT_SIZE]):
            continue
        out[start] = RECORD_ROTATE
        out[start + 1] = rotate & 0xFF
        return bytes(out)
    raise RuntimeError("no free slot for the rotate record")


# --------------------------------------------------------------------------
# 9.  Validation
# --------------------------------------------------------------------------
def _rgb565_to_rgb(value: int) -> tuple[int, int, int]:
    red = (value >> 11) & 0x1F
    green = (value >> 5) & 0x3F
    blue = value & 0x1F
    return (red * 255 // 31, green * 255 // 63, blue * 255 // 31)


def real_records(parsed) -> list:
    """Records that a real device would see: slots 1..63 only.

    ``parse_imgdat`` reads offset 0..4 as a little-endian slot count and walks
    that many slots -- 150 of them -- but the firmware only has 64
    (HIDSS_WIDGET_MAX).  Anything at index >= 64 is resource data being
    misparsed as records, and has to be ignored.
    """
    return [record for record in parsed.records if record.index < SLOT_COUNT]


def _boxes_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _record_box(record) -> tuple[int, int, int, int] | None:
    fields = record.fields
    if record.record_type == 0x93:
        return (int(fields["x"]), int(fields["y"]),
                int(fields["rendered_width"]), int(fields["rendered_height"]))
    if record.record_type == 0x92:
        return (int(fields["x"]), int(fields["y"]),
                int(fields["width"]), int(fields["height"]))
    return None


SWIFT_PANEL = Path(__file__).resolve().parent.parent / "Sources" / "OpencodeMonitor" / "PanelController.swift"


def check_swift_sync() -> list[str]:
    """Cross-check the app's copy of the model tables against this one.

    The app decides WHICH atlas cell to select and this file decides WHAT is
    drawn in it, so the two agree only by index.  Adding a brand on one side
    alone does not fail anywhere -- the panel just draws the wrong word for
    every model after the insertion point, which looks like a matching bug in
    the app and is miserable to track down from a photo of a 3.5" screen.

    Deliberately structural, not semantic: it checks the enums are the same
    length as the word tuples with contiguous raw values, and that the four
    channel numbers agree.  Mapping ``case v36`` to ``"3.6"`` would need a
    naming convention neither side wants to be bound by.
    """
    try:
        source = SWIFT_PANEL.read_text()
    except OSError:
        # The theme builder is usable on its own, so a missing app checkout is
        # not a reason to refuse to build a theme.  main() reports that the
        # cross-check did not run, rather than letting it pass silently.
        return []

    problems: list[str] = []

    def raw_values(enum_name: str) -> list[int]:
        match = re.search(rf"enum {enum_name}: UInt16 {{(.*?)\n        }}", source, re.S)
        if match is None:
            problems.append(f"{SWIFT_PANEL.name}: no `enum {enum_name}: UInt16` found")
            return []
        return [int(v) for v in re.findall(r"case \w+ = (\d+)", match.group(1))]

    for enum_name, words in (("Brand", MODEL_BRANDS), ("Variant", MODEL_VARIANTS)):
        values = raw_values(enum_name)
        if not values:
            continue
        if values != list(range(len(words))):
            problems.append(
                f"{SWIFT_PANEL.name}: {enum_name} raw values {values} != "
                f"0..{len(words) - 1}, the {len(words)} cells this theme draws"
            )

    for const, expected in (("jeanLucBrand", CH_JL_BRAND), ("jeanLucVariant", CH_JL_VARIANT),
                            ("kathrynBrand", CH_KT_BRAND), ("kathrynVariant", CH_KT_VARIANT),
                            ("screensaver", CH_SCREENSAVER)):
        match = re.search(rf"static let {const}: UInt8 = (\d+)", source)
        if match is None:
            problems.append(f"{SWIFT_PANEL.name}: no `{const}` channel constant found")
        elif int(match.group(1)) != expected:
            problems.append(
                f"{SWIFT_PANEL.name}: {const} is channel {match.group(1)}, "
                f"this theme binds it to {expected}"
            )
    return problems


def validate(data: bytes, expected_channels: list[int]) -> dict:
    imgdat = LIB.imgdat
    parsed = imgdat.parse_imgdat(data, path=str(OUT_IMGDAT))
    records = real_records(parsed)
    problems: list[str] = []

    # -- the rotate record, which lives among the regular slots ------------
    rotate_slots = [
        index for index in range(1, SLOT_COUNT)
        if data[index * SLOT_SIZE] == RECORD_ROTATE
    ]
    if len(rotate_slots) != 1:
        problems.append(f"expected exactly 1 rotate record in slots 1..63, got {len(rotate_slots)}")
    else:
        found = data[rotate_slots[0] * SLOT_SIZE + 1]
        if found != ROTATE:
            problems.append(f"rotate byte is {found}, expected {ROTATE}")
        if any(data[rotate_slots[0] * SLOT_SIZE + 2:(rotate_slots[0] + 1) * SLOT_SIZE]):
            problems.append("rotate record padding is not zero")
    # Slot 0 must stay the compiler's own header -- putting a rotate record
    # here instead is what silently did nothing (see inject_rotate).
    if data[0] != 0x96 or data[1] != 0x00:
        problems.append(f"slot 0 header is {data[0]:02x} {data[1]:02x}, expected 96 00")

    # -- record inventory --------------------------------------------------
    counts: dict[int, int] = {}
    for record in records:
        counts[record.record_type] = counts.get(record.record_type, 0) + 1
    if counts.get(0x81, 0) != 1:
        problems.append(f"expected exactly 1 background record, got {counts.get(0x81, 0)}")
    if len(records) > MAX_RECORDS:
        problems.append(f"{len(records)} records exceeds the {MAX_RECORDS} usable slots")

    # -- records must occupy slots 1..N with no gaps ------------------------
    expected_indices = list(range(1, len(records) + 1))
    if [record.index for record in records] != expected_indices:
        problems.append("records are not contiguous starting at slot 1")

    # -- every number widget carries the right sensor channel ---------------
    seen_channels = [
        int(record.fields["fast_sensor"]) for record in records if record.record_type == 0x92
    ]
    if seen_channels != expected_channels:
        problems.append(f"number channels {seen_channels} != expected {expected_channels}")
    # The app addresses these by number, so the set must be a contiguous 1..N
    # with nothing skipped and nothing bound twice -- a duplicate would make two
    # fields shadow each other and a gap means a channel the app writes to
    # lands nowhere.  (The ORDER is just widget creation order and does not
    # matter: the model field is built with its node's heading.)
    # The screensaver's widgets deliberately SHARE CH_SCREENSAVER (hardware-checked
    # 2026-10-19: several widgets on one channel all update), so it is counted once.
    n_widgets = len([r for r in records if r.record_type in (0x92, 0x93)])
    for what, have, limit in (("widgets", n_widgets, MAX_WIDGETS),
                              ("number widgets", len(seen_channels), MAX_NUMBERS),
                              ("distinct channels", len(set(seen_channels)), MAX_CHANNELS)):
        if have > limit:
            problems.append(
                f"{have} {what}, but the most the panel has been seen to service is {limit}: "
                "the extras are silently never drawn (and well past it the panel shows only "
                "'boot') -- see MAX_WIDGETS")
    ss_count = seen_channels.count(CH_SCREENSAVER)
    if ss_count < 2:
        problems.append(f"the screensaver should be several widgets on ch{CH_SCREENSAVER}, "
                        f"found {ss_count}")
    distinct = sorted(set(seen_channels))
    dashboard = [c for c in seen_channels if c != CH_SCREENSAVER]
    if sorted(dashboard + [CH_SCREENSAVER]) != list(range(1, len(dashboard) + 2)) \
            or len(dashboard) != len(set(dashboard)):
        problems.append(
            f"sensor channels are not a contiguous 1..N set with nothing bound twice "
            f"(ch{CH_SCREENSAVER} aside): {distinct}"
        )

    # -- widget ids unique --------------------------------------------------
    widget_ids = [
        int(record.fields["widget_id"]) for record in records if "widget_id" in record.fields
    ]
    if len(widget_ids) != len(set(widget_ids)):
        problems.append(f"duplicate widget ids: {widget_ids}")

    # -- resources: inside the file, non-empty, correctly sized -------------
    for record in records:
        field_name = imgdat.resource_field_name(record)
        if field_name is None:
            continue
        offset = int(record.fields[field_name])
        size = imgdat.resource_payload_size(record) or 0
        name = f"slot {record.index} ({record.record_type_name})"
        if offset < WIDGET_BLOCK_SIZE:
            problems.append(f"{name}: resource offset {offset} is inside the widget block")
            continue
        if offset + size > len(data):
            problems.append(f"{name}: resource {offset}+{size} runs past EOF ({len(data)})")
            continue
        if size <= 0:
            problems.append(f"{name}: empty resource payload")
            continue
        if not any(data[offset:offset + size]):
            problems.append(f"{name}: resource payload is all zero bytes (blank glyphs?)")

    # -- glyph atlases: every cell a value can select must be non-blank -----
    word_report: list[str] = []
    for record in records:
        if record.record_type != 0x92:
            continue
        channel = int(record.fields["fast_sensor"])
        offset = int(record.fields["glyph_bitmap_offset"])
        height = int(record.fields["glyph_bitmap_height"])
        widths = [int(value) for value in record.fields["glyph_widths"]][:ATLAS_CELLS]
        atlas = WORD_ATLASES.get(channel)

        cells: list[bytes] = []
        cursor = offset
        for width in widths:
            cells.append(data[cursor:cursor + width * height])
            cursor += width * height

        # -- bitmap atlas (the screensaver): the bytes must be a picture we built
        if channel in BITMAP_CHANNELS:
            payload = data[offset:cursor]
            if not any(a.payload == payload and a.widths == widths and a.height == height
                       for a in BITMAP_CHANNELS[channel]):
                problems.append(f"slot {record.index} (ch{channel}): bitmap atlas bytes in the "
                                "file are not any picture this build produced")
            elif not any(cells[1]):
                problems.append(f"slot {record.index} (ch{channel}): cell 1 (the picture) is blank")
            if any(cells[0]):
                problems.append(f"slot {record.index} (ch{channel}): cell 0 must be blank, or "
                                "the dashboard would never show")
            continue

        for index, cell in enumerate(cells[:10]):
            if any(cell):
                continue
            # The variant atlas's cell 0 is blank BY DESIGN -- it is what an
            # unqualified brand selects.  Any other empty cell means a glyph
            # payload went missing.
            if atlas is not None and atlas.words[index] == "":
                continue
            what = f"word {atlas.words[index]!r}" if atlas else f"digit '{index}'"
            problems.append(f"slot {record.index} (ch{channel}): {what} cell is blank")

        if atlas is None:
            continue

        # -- word atlas: the bytes in the file must BE the words -----------
        if len(set(widths)) != 1:
            problems.append(
                f"ch{channel}: word cells are not uniform width ({widths}) -- "
                "fixed-stride firmware indexing would break"
            )
        if widths[0] != atlas.cell_w or height != atlas.cell_h:
            problems.append(
                f"ch{channel}: atlas cell {widths[0]}x{height} != rasterised "
                f"{atlas.cell_w}x{atlas.cell_h}"
            )
        for index, (word, cell) in enumerate(zip(atlas.words, cells)):
            expected = atlas.cells[index]
            if cell != expected:
                problems.append(f"ch{channel}: cell {index} ({word!r}) bytes differ from the rasterised word")
                continue
            image = Image.frombytes("L", (widths[index], height), cell)
            bbox = image.getbbox()
            if bbox is None:
                # Only the empty variant cell is allowed to carry no ink.
                if word != "":
                    problems.append(f"ch{channel}: cell {index} ({word!r}) has no ink")
                elif index < atlas.report_count:
                    # Say so out loud -- a silently absent row would read as a
                    # cell that failed to rasterise rather than one that is
                    # blank on purpose.
                    word_report.append(
                        f"    ch{channel} value {index} -> {'(blank)':<8} "
                        f"no ink, by design (an unqualified brand)"
                    )
                continue
            if bbox != atlas.ink_boxes[index]:
                problems.append(
                    f"ch{channel}: cell {index} ({word!r}) ink box {bbox} "
                    f"!= rasterised {atlas.ink_boxes[index]}"
                )
            # Flush against whichever edge the atlas aligns to.  Left-aligned
            # words share a left edge, so the two session rows' columns line
            # up; a right-aligned brand cell must END where the variant field's
            # box begins, or the two halves of one model name would not meet.
            # 1px of slack because gamma can round the outermost antialiased
            # column away.
            if atlas.align == "left":
                if bbox[0] > 1:
                    problems.append(
                        f"ch{channel}: cell {index} ({word!r}) ink starts at x={bbox[0]}, expected 0..1")
            else:
                seam = widths[index] - WORD_CELL_PAD
                if not seam - 1 <= bbox[2] <= seam:
                    problems.append(
                        f"ch{channel}: cell {index} ({word!r}) ink ends at x={bbox[2]}, "
                        f"expected the seam at {seam}"
                    )
            if bbox[2] > widths[index]:
                problems.append(f"ch{channel}: cell {index} ({word!r}) ink overflows its cell")
            if index < atlas.report_count:
                word_report.append(
                    f"    ch{channel} value {index} -> {word or '(blank)':<8} "
                    f"ink {bbox[2] - bbox[0]:>3}px "
                    f"of {widths[index]}px cell, rows {bbox[1]}..{bbox[3]}"
                )

        box = _record_box(record)
        if box and widths[0] > box[2]:
            problems.append(f"ch{channel}: widget box {box[2]}px is narrower than the {widths[0]}px word cell")

    # -- the two halves of a model name have to meet -----------------------
    # Everything else about the brand/variant trick is proved per-atlas above;
    # this is the one property that lives BETWEEN two widgets.  If the variant
    # box drifts off the brand box's right edge, nothing errors -- the panel
    # just draws "QWEN   3.6" with a hole in it, or overlaps the two.
    model_boxes = {
        int(record.fields["fast_sensor"]): _record_box(record)
        for record in records if record.record_type == 0x92
    }
    for brand_ch, variant_ch in MODEL_FIELD_PAIRS:
        brand_box = model_boxes.get(brand_ch)
        variant_box = model_boxes.get(variant_ch)
        if brand_box is None or variant_box is None:
            problems.append(
                f"ch{brand_ch}/ch{variant_ch}: a model field pair is missing its widget"
            )
            continue
        seam = brand_box[0] + brand_box[2]
        if variant_box[0] != seam:
            problems.append(
                f"ch{variant_ch}: variant box starts at x={variant_box[0]}, not on the "
                f"ch{brand_ch} seam at x={seam} -- the halves of a model name would not meet"
            )
        if variant_box[1] != brand_box[1]:
            problems.append(
                f"ch{variant_ch}: variant box top y={variant_box[1]} != "
                f"ch{brand_ch} top y={brand_box[1]} -- the halves would not share a baseline"
            )

    # -- geometry: on-screen, and number boxes must not overlap anything ---
    boxed = [(record, _record_box(record)) for record in records]
    boxed = [(record, box) for record, box in boxed if box is not None]
    for record, (x, y, w, h) in boxed:
        if x < 0 or y < 0 or x + w > PANEL_W or y + h > PANEL_H:
            problems.append(
                f"slot {record.index}: box {x},{y} {w}x{h} leaves the {PANEL_W}x{PANEL_H} canvas"
            )
    # A number widget repaints its whole box on every update, so anything its
    # box covers would get erased.  Static-on-static overlap is harmless --
    # those are painted once at theme load and never repainted.
    #
    # The screensaver widgets are the exception, and are overlays BY DESIGN: they sit
    # on top of everything.  The rule above is about the dashboard's own widgets;
    # hardware shows an overlay repaints cleanly (see section 9a), so what matters
    # for them is ORDER, checked next.
    overlay = lambda r: (r.record_type == 0x92                                   # noqa: E731
                         and int(r.fields["fast_sensor"]) == CH_SCREENSAVER)
    for i, (record_a, box_a) in enumerate(boxed):
        for record_b, box_b in boxed[i + 1:]:
            if record_a.record_type != 0x92 and record_b.record_type != 0x92:
                continue
            if overlay(record_a) or overlay(record_b):
                continue
            if _boxes_overlap(box_a, box_b):
                problems.append(
                    f"slot {record_a.index} {box_a} overlaps slot {record_b.index} {box_b} "
                    "(a number repaint would erase it)"
                )

    # Overlays draw on top only if they come AFTER everything else, and the cover
    # (the dashboard-coloured full-screen piece) has to be the first of them.
    overlay_slots = [r.index for r in records if overlay(r)]
    other_slots = [r.index for r in records if not overlay(r) and r.record_type != RECORD_ROTATE]
    if overlay_slots and other_slots and min(overlay_slots) < max(other_slots):
        problems.append(
            f"screensaver slots {overlay_slots[0]}.. start before the last other widget "
            f"(slot {max(other_slots)}): they would draw UNDER it and the dashboard would show through"
        )
    if overlay_slots != list(range(overlay_slots[0], overlay_slots[0] + len(overlay_slots))):
        problems.append(f"screensaver slots are not contiguous: {overlay_slots}")
    ss_boxes = [_record_box(r) for r in records if overlay(r)]
    stripe = PANEL_H // SS_COVER_STRIPES
    want = [(0, i * stripe, PANEL_W, stripe) for i in range(SS_COVER_STRIPES)]
    if ss_boxes[:SS_COVER_STRIPES] != want:
        problems.append(f"the first {SS_COVER_STRIPES} screensaver widgets should be the cover, "
                        f"tiling the screen as {want}; got {ss_boxes[:SS_COVER_STRIPES]}")
    for record in records:
        if record.record_type != 0x92:
            continue
        box = _record_box(record)
        if box and (box[2] > MAX_PROVEN_BOX[0] or box[3] > MAX_PROVEN_BOX[1]):
            problems.append(
                f"slot {record.index} (ch{int(record.fields['fast_sensor'])}): box {box[2]}x{box[3]} "
                f"is bigger than the largest proven on hardware, {MAX_PROVEN_BOX[0]}x{MAX_PROVEN_BOX[1]} "
                f"-- a 320x480 box made the panel show only 'boot'")

    emblem_report = _check_emblems(data, records, problems)

    return {
        "parsed": parsed,
        "records": records,
        "counts": counts,
        "problems": problems,
        "rotate_slots": rotate_slots,
        "word_report": word_report,
        "emblem_report": emblem_report,
    }


def decode_background(data: bytes, records: list) -> Image.Image | None:
    """Pull the background bitmap back out of the compiled img.dat."""
    for record in records:
        if record.record_type != 0x81:
            continue
        width, height = int(record.fields["width"]), int(record.fields["height"])
        offset = int(record.fields["asset_offset"])
        raw = data[offset:offset + width * height * 2]
        return Image.frombytes("RGB", (width, height), raw, "raw", "BGR;16")
    return None


def _classify_hue(mean_r: float, mean_b: float) -> str:
    """Coarse hue class of an ink colour, robust to RGB565 quantisation.

    Ratios rather than absolute levels, so partial coverage (which pulls every
    channel toward the navy background together) does not change the verdict.
    """
    if mean_r > mean_b * 1.5:
        return "amber"
    if mean_b > mean_r * 1.5:
        return "blue"
    return "white"


def _check_emblems(data: bytes, records: list, problems: list[str]) -> list[str]:
    """Prove both emblems survived into the compiled background.

    Not "did build_background draw them" -- that is just Pillow talking to
    itself.  This decodes the RGB565 background back out of img.dat and looks
    at the actual pixels the firmware will blit.
    """
    report: list[str] = []
    background = decode_background(data, records)
    if background is None:
        problems.append("no background record to check the emblems against")
        return report

    navy = quantise_rgb565(BG_NAVY)
    expectations = {
        # name: (expected ink hue, human description)
        "federation": ("white", "Federation emblem"),
        "delta": ("amber", "Starfleet delta"),
    }
    for name, (expected_hue, label) in expectations.items():
        box = EMBLEM_BOXES.get(name)
        if box is None:
            problems.append(f"{label}: never placed into the background")
            continue

        pixels = _pixels(background.crop(box))
        ink = [pixel for pixel in pixels if pixel != navy]
        coverage = len(ink) / len(pixels)
        if coverage < 0.08:
            problems.append(
                f"{label}: only {coverage:.1%} of its box has ink -- did it render?"
            )
            continue

        # Judge the hue on solid ink only.  Averaging every non-navy pixel
        # would drag the mean toward the background through all the faint
        # antialiased edges -- and the UFP starfield is mostly faint specks.
        solid = [pixel for pixel in ink if max(pixel) >= 64]
        if not solid:
            problems.append(f"{label}: no solid ink in its box, only faint edges")
            continue
        mean_r = sum(pixel[0] for pixel in solid) / len(solid)
        mean_g = sum(pixel[1] for pixel in solid) / len(solid)
        mean_b = sum(pixel[2] for pixel in solid) / len(solid)
        hue = _classify_hue(mean_r, mean_b)
        if hue != expected_hue:
            problems.append(
                f"{label}: ink is {hue} (mean R{mean_r:.0f} G{mean_g:.0f} B{mean_b:.0f}), "
                f"expected {expected_hue}"
            )
        if expected_hue == "white" and mean_b < 120:
            problems.append(
                f"{label}: near-white ink should be bright, but mean B is only {mean_b:.0f}"
            )

        # Must clear the title text and the rule below the header.
        title_box = TITLE_INK_BOX
        if _boxes_overlap((box[0], box[1], box[2] - box[0], box[3] - box[1]),
                          (title_box[0], title_box[1],
                           title_box[2] - title_box[0], title_box[3] - title_box[1])):
            problems.append(f"{label}: box {box} overlaps the title ink {title_box}")
        if box[3] > HEADER_RULE_Y:
            problems.append(f"{label}: box {box} reaches the rule at y={HEADER_RULE_Y}")
        if box[0] < MARGIN_X or box[2] > PANEL_W - MARGIN_X:
            problems.append(f"{label}: box {box} breaks the {MARGIN_X}px margin")

        # And no number widget's repaint box may reach it.
        for record in records:
            record_box = _record_box(record)
            if record.record_type != 0x92 or record_box is None:
                continue
            if int(record.fields["fast_sensor"]) == CH_SCREENSAVER:
                continue          # an overlay, on top of the whole panel by design
            if _boxes_overlap((box[0], box[1], box[2] - box[0], box[3] - box[1]), record_box):
                problems.append(
                    f"{label}: box {box} is inside number widget slot {record.index}'s repaint box"
                )

        report.append(
            f"    {label:<18} box {box}  {coverage:.0%} ink, {hue} "
            f"(solid mean R{mean_r:.0f} G{mean_g:.0f} B{mean_b:.0f})"
        )

    # Nothing but the title may be in the title's box.  The title is baked into the
    # background now (see ThemeBuilder.bake_labels), so the box is no longer flat navy
    # -- it is navy plus the title's own antialiasing.  Every pixel must therefore lie
    # on the line from the field colour to the title colour; anything off it is art
    # that has wandered in, and would cost the text its contrast.
    title_box = TITLE_INK_BOX
    if title_box[2] > title_box[0]:
        amber = quantise_rgb565(AMBER)
        span = [a - n for a, n in zip(amber, navy)]
        for pixel in set(_pixels(background.crop(title_box))):
            t = (pixel[0] - navy[0]) / span[0]
            expected = [n + t * d for n, d in zip(navy, span)]
            if not -0.05 <= t <= 1.05 or any(abs(p - e) > 14 for p, e in zip(pixel, expected)):
                problems.append(f"background behind the title has {pixel}, which is neither the "
                                f"navy field nor the title's own colour")
                break

    # Every number widget repaints its whole box when its value changes, so
    # background chrome underneath it is liable to be erased.  The layout has
    # always kept the art in the gutters and the gaps; this asserts it rather
    # than leaving it to be re-checked by eye each time a row moves.
    for record in records:
        if record.record_type != 0x92:
            continue
        if int(record.fields["fast_sensor"]) == CH_SCREENSAVER:
            continue              # an overlay: it composites over whatever is beneath
        box = _record_box(record)
        if box is None:
            continue
        x, y, width, height = box
        under = set(_pixels(background.crop((x, y, x + width, y + height))))
        if under != {navy}:
            channel = int(record.fields["fast_sensor"])
            problems.append(
                f"ch{channel}: background under its repaint box {box} is not flat navy "
                f"({sorted(under)[:3]}) -- a value update would erase that art"
            )
    return report


def save_header_zoom(preview: Image.Image, path: Path, scale: int = 4) -> Path:
    """A 4x blow-up of the header band, for judging the emblems.

    NEAREST on purpose: this is meant to show the real pixels the panel gets,
    magnified -- a smooth resample would flatter the art and hide aliasing.
    """
    band = preview.crop((0, 0, PANEL_W, HEADER_RULE_Y + 6))
    zoom = band.resize((band.width * scale, band.height * scale), Image.Resampling.NEAREST)
    zoom.save(path)
    return path


# --------------------------------------------------------------------------
# 10.  Preview rendered straight out of the compiled file
# --------------------------------------------------------------------------
def render_preview(data: bytes, path: Path,
                   overrides: dict[int, int] | None = None) -> Image.Image:
    """Decode img.dat and draw what the firmware should put on the glass.

    Everything here comes from the compiled bytes -- background pixels, text
    bitmaps, glyph atlases, colours, coordinates -- so the preview doubles as
    an end-to-end check that all offsets and payloads are intact.  Number
    widgets are drawn exactly the way the firmware has to: split the value
    into decimal digits, and blit one atlas cell per digit.  For a word atlas
    that means a value of 1 draws cell 1, which is the word WORKING.
    """
    imgdat = LIB.imgdat
    records = real_records(imgdat.parse_imgdat(data))
    canvas = Image.new("RGB", (PANEL_W, PANEL_H), (0, 0, 0))

    def paste_mask(mask: Image.Image, x: int, y: int, color: tuple[int, int, int], alpha: int) -> None:
        if alpha != 255:
            mask = mask.point(lambda value: value * alpha // 255)
        canvas.paste(Image.new("RGB", mask.size, color), (x, y), mask)

    for record in records:
        fields = record.fields

        if record.record_type == 0x81:                      # background
            width, height = int(fields["width"]), int(fields["height"])
            offset = int(fields["asset_offset"])
            raw = data[offset:offset + width * height * 2]
            background = Image.frombytes("RGB", (width, height), raw, "raw", "BGR;16")
            canvas.paste(background, (0, 0))

        elif record.record_type == 0x93:                    # static text
            width, height = int(fields["rendered_width"]), int(fields["rendered_height"])
            offset = int(fields["text_bitmap_offset"])
            mask = Image.frombytes("L", (width, height), data[offset:offset + width * height])
            paste_mask(mask, int(fields["x"]), int(fields["y"]),
                       _rgb565_to_rgb(int(fields["font_color_rgb565"])), int(fields["font_alpha"]))

        elif record.record_type == 0x92:                    # live number / word
            channel = int(fields["fast_sensor"])
            height = int(fields["glyph_bitmap_height"])
            widths = [int(value) for value in fields["glyph_widths"]][:ATLAS_CELLS]
            offset = int(fields["glyph_bitmap_offset"])
            cells: list[Image.Image] = []
            cursor = offset
            for width in widths:
                cells.append(
                    Image.frombytes("L", (width, height), data[cursor:cursor + width * height])
                )
                cursor += width * height

            text = str((overrides or {}).get(channel, SAMPLE_VALUES.get(channel, 0)))
            indices = [int(char) if char.isdigit() else (10 if char == "." else 11) for char in text]
            total = sum(widths[index] for index in indices)
            strip = Image.new("L", (max(1, total), height), 0)
            cursor = 0
            for index in indices:
                strip.paste(cells[index], (cursor, 0))
                cursor += widths[index]

            x, y = int(fields["x"]), int(fields["y"])
            box_w, align = int(fields["width"]), int(fields["h_align"])
            if align == 1:
                x += max(0, (box_w - total) // 2)
            elif align == 2:
                x += max(0, box_w - total)
            paste_mask(strip, x, y, _rgb565_to_rgb(int(fields["font_color_rgb565"])),
                       int(fields["font_alpha"]))

    canvas.save(path)
    return canvas


# --------------------------------------------------------------------------
# 11.  Entry point
# --------------------------------------------------------------------------
def main() -> int:
    background = build_background(ASSET_DIR / "starfleet_bg.bmp")
    print(f"background art  : {background}")

    bundle, builder = build_bundle(background)
    print(f"widgets         : {len(bundle.theme.widgets)} "
          f"({sum(1 for w in bundle.theme.widgets if w.widget_type == 2)} static text, "
          f"{sum(1 for w in bundle.theme.widgets if w.widget_type == 5)} number)")

    compiled = LIB.compiler.compile_theme_bundle(bundle)
    # 4096 is HIDSS_WIDGET_BLOCK_SIZE: with <= 63 records the compiler always
    # starts the resource blob exactly there, which is what the firmware wants.
    assert len(compiled) > WIDGET_BLOCK_SIZE
    data = inject_rotate(compiled, ROTATE)
    OUT_IMGDAT.write_bytes(data)

    expected_channels = [
        widget.sensor.fast_sensor for widget in bundle.theme.widgets if widget.widget_type == 5
    ]
    result = validate(data, expected_channels)
    result["problems"] += check_swift_sync()
    swift_check = "OK" if SWIFT_PANEL.exists() else f"SKIPPED ({SWIFT_PANEL} not found)"

    print()
    print(f"img.dat         : {OUT_IMGDAT}  ({len(data):,} bytes, "
          f"{len(data) / 1024 / 1024:.2f} MiB)")
    print(f"slot 0 header   : {data[0]:02x} {data[1]:02x} (compiler slot count, left alone)")
    for slot in result["rotate_slots"]:
        print(f"rotate record   : slot {slot}, value {data[slot * SLOT_SIZE + 1]} "
              f"({[0, 90, 180, 270][data[slot * SLOT_SIZE + 1]]} degrees)")
    print(f"records         : {len(result['records'])} in slots 1..{len(result['records'])} "
          f"of {MAX_RECORDS} usable")
    for record_type, count in sorted(result["counts"].items()):
        print(f"    0x{record_type:02x} {LIB.imgdat.record_type_name(record_type):<20} x{count}")
    print(f"resource blob   : starts at {WIDGET_BLOCK_SIZE}, "
          f"{len(data) - WIDGET_BLOCK_SIZE:,} bytes")

    if result["word_report"]:
        print()
        print("word atlas (decoded back out of img.dat):")
        for line in result["word_report"]:
            print(line)

    print()
    print("layout:")
    for item in builder.placed:
        print(f"    {item.label:<24} x={item.x:>3} y={item.y:>3}  {item.w:>3}x{item.h:<3}")

    if result["emblem_report"]:
        print()
        print("header emblems (decoded back out of img.dat):")
        for line in result["emblem_report"]:
            print(line)
        print(f"    title ink        {TITLE_INK_BOX}")

    preview = render_preview(data, OUT_PREVIEW)
    render_preview(data, OUT_PREVIEW_SCREENSAVER, {CH_SCREENSAVER: 1})
    zoom = save_header_zoom(preview, OUT_HEADER_ZOOM)
    print()
    print(f"preview         : {OUT_PREVIEW}")
    print(f"screensaver     : {OUT_PREVIEW_SCREENSAVER}")
    print(f"header zoom     : {zoom}")

    if result["problems"]:
        print()
        print("VALIDATION FAILED:")
        for problem in result["problems"]:
            print(f"    - {problem}")
        return 1
    print("validation      : OK")
    print(f"app cross-check : {swift_check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
