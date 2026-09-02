# Panel theme tooling

Offline tooling for the 3.5" USB panel that Starfleet Command drives. You only
need anything in here when you want to **redesign what the panel looks like**.
Normal day-to-day operation needs none of it — the app pushes live values by
itself (see "How this relates to the app" below).

## What the panel actually is

A **USB HID sensor panel**, not a display — `0483:0065`, manufacturer string
`llhmi.com`, product `llTechCo.,Ltd`. It will never appear in System Settings ▸
Displays, and that is not a missing driver:

- It exposes a single **HID** interface (`bInterfaceClass=3`) on a vendor-defined
  usage page (`0xFF00`), with 64-byte in/out reports. There is no video path.
- It is **USB 2.0 full speed** (12 Mbit/s ≈ 64 KB/s). One 480×320 RGB565 frame is
  307 KB — about 5 seconds per frame. It physically cannot be a desktop display.

Instead it stores a compiled **theme** (`img.dat`) in flash and renders locally,
while the host pushes **numeric values on numbered channels**. Those channels are
just numbered slots; the firmware's own names for them (`cpu_temperature`,
`gpu_usage`, …) are irrelevant, because the labels drawn next to each value live
in the theme, which we author.

**This theme uses 21 channels, and 20 is not the ceiling it looks like.**
`hidss.h` declares `HIDSS_SENSOR_KEY_MAX = 20`, which reads like a firmware
limit and is easy to design around unnecessarily — but count Buren's
`HIDSS_SENSOR_*` enum and it has exactly 20 named entries, so that constant is
the size of *his own sensor list*, not a device constraint. `smartmonitor_hid_lib`
imposes nothing of the kind: it caps only **pairs per packet** (20, which is
just what fits in 64 bytes) and requires the tag to fit in one byte.

Confirmed on this device on 2026-09-02: channel 21 renders. Channels beyond
that are untested — the tag is a `uint8_t`, so more are plausible, but nothing
here has proved it. If you do need more, note that going past 20 channels means
a full push no longer fits one report; `PanelDriver.sendSensors` already splits
across reports, which the same test exercised.

> Separately: this Mac is a **MacBook Air M4**, which supports a maximum of two
> external displays, and both LS27A80s already use them. Even a *real* small
> HDMI display could not have become a third screen here — only DisplayLink
> bypasses that limit. Driving this panel directly was the only workable route.

## Files

| Path | Purpose |
| --- | --- |
| `build_theme.py` | Generates `img.dat` + `theme_preview.png`. Edit this to change the design. |
| `upload_theme.py` | Flashes an `img.dat` to the panel over YMODEM. |
| `smartpanel.py` | Minimal macOS HID driver — useful for poking the panel by hand. |
| `starfleet_panel.py` | **Superseded** by the Swift app; kept as a fallback / worked example of the channel map. |
| `img.dat` | The compiled LCARS theme. |
| `theme_preview.png` | Rendered preview, decoded back out of `img.dat`. |
| `header_zoom.png` | 4× blow-up of the header band — the emblems are 32px, too small to judge in the full preview. |
| `theme_assets/` | Source art: the generated 320×480 background BMP, plus the UFP emblem SVG and the coverage mask derived from it. |
| `vendor/` | Third-party reference implementations — see Attribution. |

## Setup

`.venv` is already created. If it goes missing:

```bash
cd ~/"Starfleet Command/panel-theme" && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python hidapi Pillow
```

Use `uv`, not Homebrew Python — brew's `python@3.14` has a broken `pyexpat`/pip
on this machine.

## Redesigning the panel

```bash
cd ~/"Starfleet Command/panel-theme" && ./.venv/bin/python build_theme.py
```

Edit the layout constants in `build_theme.py`, re-run, and check
`theme_preview.png`. The preview is not an independent mock-up — it is decoded
back out of the compiled `img.dat`, so if it looks right, the record offsets and
glyph payloads are genuinely intact.

Then flash it:

```bash
cd ~/"Starfleet Command/panel-theme" && ./.venv/bin/python upload_theme.py img.dat
```

Takes ~90 s for 343 blocks. **Quit Starfleet Command first** (or untick "Send to
USB panel") so the app isn't writing to the device mid-flash. The panel
re-enumerates afterwards; give it ~10 s before expecting it back.

Flashing writes the **theme** region only — `upload_theme` never enters the
bootloader, only a *firmware* upload would. A bad theme is re-flashable.

## Header emblems

The title is flanked by two marks drawn **into the background bitmap**, not as
widgets — static art needs no records, and background pixels are immune to the
number-widget box repaint. Both are rasterised at 8× and reduced with LANCZOS;
at 32px an aliased edge is mush.

- **Right — the Starfleet delta.** Geometry reused verbatim from
  `assets/gen-starfleet-mark.swift`, so the panel, the menu bar icon and the app
  icon all draw the same shape. ⚠️ Those coordinates are CoreGraphics
  (origin bottom-left); Pillow's origin is top-left, so `delta_polygon()` flips
  every y through `DELTA_H - y`. Miss that and the delta is upside down with no
  error — `_assert_delta_upright()` is the tripwire, checking that the first
  inked scanline is a single narrow run near the middle, that a low scanline
  splits in two (the notch between the wings), and that the bottom half carries
  more ink than the top.
- **Left — the United Federation of Planets emblem**, the real one, tinted
  near-white and reduced in a single LANCZOS step from `ufp_emblem_mask.png`.
  Near-white rather than palette blue: it matches the emblem's own
  white-on-blue and reads better than blue against the navy at this size.

Two things about the emblem mask are worth knowing before touching it:

- **Its alpha floor is 37, not 0.** The flag's blue field survives as a
  constant ~14% haze across the whole rectangle, so tinting straight through
  it stamps a visibly lighter box onto the navy. `_ufp_alpha()` rescales the
  floor to zero first.
- **It is cropped flush to its ink** — the wreath touches all four edges, so
  the mark has no internal padding to borrow clearance from. That is why it is
  32px tall rather than the 36px that would fill the gutter: at 36 its bright
  top row would butt straight into the top bar.

`validate()` decodes the background back out of `img.dat` and checks each
emblem's box for ink coverage and hue, that neither touches the title text, the
rule, or any number widget's repaint box, and that the field behind the title
stays flat.

### Artwork provenance

| Mark | Source | Licence |
| --- | --- | --- |
| Starfleet delta | `../assets/gen-starfleet-mark.swift` — the project's own generator, itself transcribed from [`File:Delta-shield.svg`](https://commons.wikimedia.org/wiki/File:Delta-shield.svg) on Wikimedia Commons (its `ffb634` foreground path) | public domain |
| UFP emblem | `theme_assets/ufp_flag.svg` — [`File:United_Federation_of_Planets_Flag.svg`](https://commons.wikimedia.org/wiki/File:United_Federation_of_Planets_Flag.svg), author Peppo (2017), rev. Oren neu dag (2023) | public domain — Commons rates it ineligible for copyright ("consists entirely of information that is common property"), and the author released it PD worldwide |

**Regenerating `ufp_emblem_mask.png`** (only needed if the emblem art changes):
render `ufp_flag.svg` with headless Chrome at 4× , then crop to the tallest
band of ink to drop the flag field and the "UNITED FEDERATION of PLANETS"
wordmark, leaving just the wreath + starfield roundel. The result is a coverage
mask — R, G, B and A all carry the same value — and `build_theme.py` takes
`.split()[3]`. Keep the crop flush to the ink; `FED_SIZE` assumes no padding.

## Channel map — keep in sync

`build_theme.py` and `PanelController.swift` must agree. The labels live in the
theme, so swapping two channels on one side only would silently mislabel them
on screen with no error anywhere.

| Ch | Meaning | Ch | Meaning |
| --- | --- | --- | --- |
| 1 | Jean-Luc GPU % | 5 | Kathryn GPU % |
| 2 | Jean-Luc GPU temp °C | 6 | Kathryn GPU temp °C |
| 3 | Jean-Luc RAM % | 7 | Kathryn RAM % |
| 4 | Jean-Luc GPU watts | 8 | Kathryn GPU watts |
| 9 | actively-generating agent sessions, both nodes | | |

With 21 channels the map no longer fits one 64-byte report (`[type][count]`
plus 20 × `[channel][u16]`), so `PanelDriver.sendSensors` splits a push across
reports. There is no begin/commit around a sensor write — the firmware applies
each pair as it arrives — so the only effect is that the last fields land a
fraction of a millisecond after the first.

The two session rows show the **most recently active** sessions across both
Sparks, row 1 above row 2:

| Ch | Session 1 | Ch | Session 2 |
| --- | --- | --- | --- |
| 10 | status enum | 14 | status enum |
| 11 | minutes since last activity | 15 | minutes since last activity |
| 12 | todos completed | 16 | todos completed |
| 13 | todos total | 17 | todos total |

Status enum — send exactly these integers:

| Value | Word | Value | Word |
| --- | --- | --- | --- |
| 0 | NONE (no such session) | 3 | WAITING |
| 1 | WORKING | 4 | IDLE |
| 2 | STALLED | 5…9 | UNKNOWN (never send these) |

The resident model per machine rides on each node's heading line, as **two**
fields drawn flush against each other — a brand and a variant:

| Ch | Meaning | Ch | Meaning |
| --- | --- | --- | --- |
| 18 | Jean-Luc model brand | 19 | Jean-Luc model variant |
| 20 | Kathryn model brand | 21 | Kathryn model variant |

Two fields rather than one because **an atlas has twelve cells and a plain
integer reaches ten of them** — fewer than the roster has families, which is
why an earlier single-field version collapsed most of the roster to `OTHER`.
Two fields *multiply* where one could only enumerate: 10 brands × 10 variants
covers all 28 members. They cannot be the two **digits** of one field, because
both digits index the *same* atlas — `QWEN` and `3.6` would have to be cells of
one ten-cell set, and the brands alone fill it.

Brand cells rasterise flush **right** and variant cells flush **left**, so the
two always meet at the seam between the boxes with `WORD_CELL_PAD` of gap:
`QWEN` + `3.6` reads as one word, and a brand needing no qualifier just ends at
the seam. `build_theme.py` asserts that abutment against the compiled record
geometry, so the halves cannot drift apart unnoticed.

| Value | Brand (ch18/20) | Variant (ch19/21) |
| --- | --- | --- |
| 0 | — (nothing resident) | *(blank — an unqualified brand)* |
| 1 | QWEN | 3.5 |
| 2 | GEMMA4 | 3.6 |
| 3 | NEM | 3.8 |
| 4 | MINIMAX | 235B |
| 5 | DEEPSEEK | 3VL |
| 6 | GLM5.3 | CASC |
| 7 | HUNYUAN | OTRON |
| 8 | AUX (embeddings / imagegen) | 31B |
| 9 | OTHER | TP2 |

Between them these cover **every** member of the current 28-model roster —
`OTHER` is a tripwire for a family added to llama-swap and not yet added to
`PanelModel`, not a bucket anything currently falls into.

⚠️ The app selects a cell *by index* while this file decides what is drawn *in*
it, so the two agree only by position. `check_swift_sync()` cross-checks
`PanelController.swift`'s `Brand`/`Variant` raw values and the four channel
constants on every build, because inserting a brand on one side alone fails
nowhere — it just draws the wrong word for everything after it.

### How a number widget draws a *word*

The status and model fields are ordinary number widgets (record `0x92`). A number widget
renders a value by splitting it into decimal digits and blitting one bitmap per
digit out of a 12-cell glyph atlas — and nothing requires those cells to contain
digits. The status atlas holds **words** in cells 0…4, so a channel value of
`1` draws the cell that contains `WORKING`; the brand and variant atlases use
all ten cells the same way (the variant atlas's cell 0 is blank by design —
it is what an unqualified brand selects).

Two things keep that honest rather than fragile:

- Cell widths are big-endian `u16` in the record (bytes 22…45), so an ~93px cell
  is nowhere near overflowing the field.
- Every cell in a word atlas is padded to the **same** width. The vendor's own
  digit atlas uses unequal widths (digits 17, `.` 8, `-` 14), which proves the
  firmware indexes through the width table — but with uniform cells,
  cumulative-width and fixed-stride indexing address identical bytes, so the
  trick does not depend on which one the firmware actually does.

`build_theme.py` verifies this offline: it decodes each atlas back out of the
compiled `img.dat` and checks the cell bytes against a fresh rasterisation of
each word, plus per-cell ink bounding boxes. On the status fields, values above
4 land on `UNKNOWN` cells rather than on garbage; the brand and variant atlases
define all ten, so there is nothing out of range to land on.

## Text rendering — don't let the library binarise it

`render_static_text_payload()` takes a `binary_threshold`, and the bundled
compiler hardcodes `binary_threshold=160`:

```python
image.point(lambda value: 255 if value >= binary_threshold else 0)
```

At these sizes that eats thin stems and leaves ragged, uneven letterforms — on
the panel it reads as a distorted font with missing pixels. `build_theme.py`
patches it out (see the "Antialiased static text" section) and applies a mild
`TEXT_GAMMA` lift instead, because thin bright-on-dark strokes read faint once
properly antialiased.

Two things make this safe:

- **The format is unchanged.** The payload is 8-bit coverage either way
  (`Image.tobytes()` on an `"L"` image); binarising only forces those bytes to
  `0` or `255`. Same size, same layout.
- **The firmware already blits real 8-bit coverage** — `render_number_glyph_payload`
  never binarises, and every number on the panel goes through it. Antialiased
  text is the same code path with smoother values.

⚠️ **The patch must target `LIB.compiler`, not `LIB.render`.** `compiler.py` does
`from .render import render_static_text_payload`, which is a direct binding —
rebinding the name on the `render` module would leave the compiler still calling
the original, and the compiled output would silently stay binarised. (This is
unlike `load_font`, which `render.py` looks up as a module global at call time,
so patching `LIB.render.load_font` *does* work.)

To confirm the fix survives a rebuild: decode the `0x93` payloads out of
`img.dat` and count distinct byte values. Binarised text has exactly **2**;
antialiased text has 70–190, with roughly 9–29% of pixels at intermediate
values.

### …and don't let it apply the `vendor_mode` crop either

`vendor_mode=True` re-crops the bitmap, but **only for text containing a
space**, to `int(textlength(text)) - 1` — the *advance* width less a pixel.
`_render_mask` has already sized the bitmap to the ink bounding box
(`textbbox`) and drawn it flush at `-left`, so that crop can only ever cut ink
off the right-hand edge. Measured on this theme's own labels:

| Label | ink width | cropped to | lost |
| --- | --- | --- | --- |
| `CLUSTER STATUS` | 88 | 86 | **2px** |
| `PWR W` | 36 | 34 | **2px** |
| `AGENT SESSIONS`, `GPU %`, `TEMP C`, `RAM %` | — | — | 1px each |
| `STARFLEET` | 160 | *(not cropped)* | 0 — no space in it |

Two pixels off `CLUSTER STATUS` visibly chopped the final `S`. The wrapper
forces `vendor_mode=False`, so every label keeps its full ink extent.

The space-only trigger is what makes this sneaky: single-word labels look
perfect while every multi-word one is quietly shaved, so it reads as a font
problem rather than a cropping bug.

## Orientation — the rotate record must NOT go at offset 0

Rotation works, but only when the rotate record sits **among the regular
records** (slots 1…63). This is the single most easily-wasted afternoon in this
whole project, so:

> ❌ Rotate record at **offset 0** — silently does nothing. `ROTATE_90` and
> `ROTATE_270` (180° apart) render *identically*, and a cold power-cycle
> doesn't help either. There is no error; it just never rotates.
>
> ✅ Rotate record in the **first free slot in 1…63**, leaving offset 0 as the
> compiler's own `96 00` header — works.

The two reference implementations disagree about offset 0, and for this
firmware **`smartmonitor_hid_lib` is the one that's right**: offset 0 is a
little-endian *slot count* (`SMARTMONITOR_DEFAULT_SLOT_COUNT = 150`), not a
record. Buren's C generator (`tg/output.c:format_image`) writes rotate there,
which is presumably correct for the device generation he had.

The confusion is helped along by a coincidence: `150 == 0x96 ==
HIDSS_WIDGET_ROTATE`, and the byte after it is `0 == HIDSS_WIDGET_ROTATE_0`, so
the compiler's header is *byte-for-byte* a valid rotate-0 record. It looks like
the rotate record is already there. It isn't.

`ROTATE` is set to `ROTATE_180`: the canvas is natively 320×480 portrait, so the
layout is already the right shape and only needed turning the right way up for
how this panel is mounted. `inject_rotate()` handles the placement.

(The second USB-C port is just cable routing for either mounting — it is *not* a
portrait-vs-landscape selector.)

## Protocol cheat-sheet

64-byte HID output reports, no report IDs. Byte 0 is the report type:

| Byte 0 | Report | Layout |
| --- | --- | --- |
| `0x00` | widget | `[type][n]` then n × `[id][u16be value]` |
| `0x01` | command | `0x01` + `"reset\0"` |
| `0x02` | sensor | `[type][n]` then n × `[channel][s16be value]`, max 20 **pairs per report** (not a cap on channel *ids* — send more channels in more reports) |
| `0x03` | datetime | `[03][01][15][yr-2000][mon][day][hr][min][sec][blTimeout][brightness 1..100]` |

**Debugging tip that saves hours:** the *backlight brightness* byte in the
datetime report is the only field that works regardless of what the loaded theme
draws. If sensor writes seem to do nothing, send brightness 10 then 100 — if the
panel visibly dims, your packets are landing and the problem is that the theme
has nothing bound to those channels. That single test is what unblocked this
whole thing.

Theme format: `img.dat` = 64-byte record slots (slot 0 is rotate) followed by
resources aligned to `0x1000`. Record types: `0x94` splash, `0x81` background,
`0x84` image, `0x92` number (sensor-driven digits), `0x93` static text, `0x96`
rotate. Widget types 1/2/3/5 rasterise from scratch via Pillow; **type 6
(datetime) needs donor glyphs from a vendor theme we don't have — avoid it.**

## How this relates to the app

Runtime pushing lives in the Swift app, not here:

- `Sources/OpencodeMonitor/PanelDriver.swift` — raw HID over `IOHIDManager`.
- `Sources/OpencodeMonitor/PanelController.swift` — feeds the panel from the
  existing `StatusPoller`s, so the panel costs **zero extra SSH**.
- Toggle: "Send to USB panel" in the menu bar dropdown.

## Attribution

Both vendored projects are Linux-only (`/dev/hidraw`); the macOS transports here
and in the Swift app were written against their documented protocol.

- `vendor/usb-smart-screen` — James Buren's `hidss`, **MIT**. Branch `hidss`,
  commit `37c4b21` (2024-03-02), from <https://gitlab.com/braewoods/usb-smart-screen>.
  The GitHub `braewoods/hidss` repo was deleted; GitLab is the surviving copy.
  `inc/hidss.h` and `ctl/device.c` are the authoritative protocol description,
  and are what `PanelDriver.swift` was transcribed from. Two of its constants
  describe *his* build rather than this device, though: rotate-at-offset-0 (see
  above) and `HIDSS_SENSOR_KEY_MAX = 20`, which is the length of his named-sensor
  enum — this panel happily renders channel 21.
- `vendor/smartmonitor_hid_lib` — **GPL-3.0**, commit `c7d7ceb` (2026-05-01),
  from <https://github.com/Agentry433/smartmonitor_hid_lib>. Used by
  `build_theme.py` for its `.ui` → `img.dat` compiler and glyph rendering.

⚠️ Note the licence split: the GPL dependency is confined to this offline theme
builder. The shipped app's `PanelDriver.swift` derives from the **MIT** C
reference, so the app itself is not GPL-encumbered — keep it that way if you ever
distribute Starfleet Command.
