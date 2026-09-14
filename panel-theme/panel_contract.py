#!/usr/bin/env python3
"""The contract between Starfleet Command's panel code and the theme flashed onto the panel.

The app writes plain numbers into the panel's sensor channels; the flashed theme
(img.dat) decides what each number LOOKS like. Both sides must agree on:
  * which channel carries which model field (build_theme.py CH_JL_BRAND ...
    == PanelController.swift jeanLucBrand ...)
  * how many cells, in which order, the brand / variant / status atlases hold

When they drift nothing errors -- the panel just draws the wrong words. 2026-09-14:
the panel had been re-flashed with two model fields per node while the app was
still the build with one, so GLM-5.3 (old enum "other" = 9, sent to channels 18
and 19) rendered as brand 9 "OTHER" + variant 9 "TP2".

Two moments can create that skew, and each gets its own check:
  * package.sh      `check`  Swift and build_theme.py must agree, else the build
                             fails; also warns if the panel runs a different theme.
                    `stamp`  records the contract this build speaks in the bundle.
  * upload_theme.py          refuses to flash a theme the installed app was not
                             built for (verify_flash), and records what it flashed.

Standard library only: package.sh runs this with the system python3, which lacks
the PIL / hidapi that build_theme.py and upload_theme.py need -- so build_theme.py
is read with `ast`, never imported.

STARFLEET_APP=/path/to/Starfleet Command.app overrides where the app is looked for.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
THEME_SRC = HERE / "build_theme.py"
THEME_IMG = HERE / "img.dat"
SWIFT_SRC = ROOT / "Sources" / "OpencodeMonitor" / "PanelController.swift"
FLASHED = HERE / ".flashed-contract.json"     # machine-local: what is on THIS panel
STAMP = Path("Contents") / "Resources" / "panel-contract.json"
APP_NAME = "Starfleet Command.app"

# theme constant -> the Swift constant that must hold the same channel number
CHANNELS = {
    "CH_JL_BRAND": "jeanLucBrand",
    "CH_JL_VARIANT": "jeanLucVariant",
    "CH_KT_BRAND": "kathrynBrand",
    "CH_KT_VARIANT": "kathrynVariant",
}
# (label, atlas in build_theme.py, Swift enum, how a Swift case is matched to its cell)
#   "name"    the case name IS the word   (glm53 ~ "GLM5.3", idle ~ "IDLE")
#   "comment" the word is the trailing comment on the case   (v35 = 1 // 3.5)
ATLASES = (
    ("brand", "MODEL_BRANDS", "Brand", "name"),
    ("variant", "MODEL_VARIANTS", "Variant", "comment"),
    ("status", "STATUS_WORDS", "PanelStatus", "name"),
)


class ContractError(Exception):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def theme_contract(path: Path = THEME_SRC) -> dict:
    """The contract as build_theme.py declares it -- parsed, not imported."""
    path = Path(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Tuple):        # CH_JL_BRAND, CH_JL_VARIANT = 18, 19
                names = [t.id for t in target.elts if isinstance(t, ast.Name)]
            elif isinstance(target, ast.Name):
                names = [target.id]
            else:
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                continue
            if len(names) == 1:
                found[names[0]] = value
            elif isinstance(value, (tuple, list)) and len(value) == len(names):
                found.update(zip(names, value))
    wanted = list(CHANNELS) + [atlas for _, atlas, _, _ in ATLASES]
    missing = [n for n in wanted if n not in found]
    if missing:
        raise ContractError(f"{path.name}: cannot find {', '.join(missing)}")
    contract = {"channels": {n: int(found[n]) for n in CHANNELS}}
    for label, atlas, _, _ in ATLASES:
        contract[label] = [str(w) for w in found[atlas]]
    return contract


def _swift_enum(lines: list, name: str, path: Path) -> list:
    """[(case, raw value, trailing comment)] of `enum <name>: UInt16`, in source order.

    Line-based rather than one regex over the braces: PanelStatus carries an init
    with nested braces after its cases, and the cases always come first.
    """
    start = next((i for i, l in enumerate(lines)
                  if re.search(rf"\benum {name}: UInt16\s*\{{", l)), None)
    if start is None:
        raise ContractError(f"{path.name}: no `enum {name}: UInt16`")
    cases = []
    for line in lines[start + 1:]:
        s = line.strip()
        m = re.match(r"case (\w+) = (\d+)\s*(?://\s*(.*))?$", s)
        if m:
            cases.append((m.group(1), int(m.group(2)), (m.group(3) or "").strip()))
        elif not s or s.startswith("//"):
            continue
        else:
            break                                    # init / closing brace: cases are over
    if not cases:
        raise ContractError(f"{path.name}: `enum {name}` has no `case x = n` lines")
    return cases


def swift_contract(path: Path = SWIFT_SRC) -> dict:
    path = Path(path)
    src = path.read_text(encoding="utf-8")
    channels = {}
    for theme_name, swift_name in CHANNELS.items():
        m = re.search(rf"static let {swift_name}: UInt8 = (\d+)", src)
        if not m:
            raise ContractError(f"{path.name}: no `static let {swift_name}: UInt8 = n`")
        channels[theme_name] = int(m.group(1))
    lines = src.splitlines()
    out = {"channels": channels}
    for label, _, enum, _ in ATLASES:
        out[label] = _swift_enum(lines, enum, path)
    return out


def mismatches(swift: Path = SWIFT_SRC, theme: Path = THEME_SRC) -> list:
    """Every way PanelController.swift and build_theme.py disagree. [] = in sync."""
    t, s = theme_contract(theme), swift_contract(swift)
    problems = []
    for name, channel in t["channels"].items():
        if s["channels"][name] != channel:
            problems.append(f"channel: {name} = {channel} in build_theme.py, but "
                            f"{CHANNELS[name]} = {s['channels'][name]} in PanelController.swift")
    for label, atlas, enum, how in ATLASES:
        words, cases = t[label], s[label]
        values = [v for _, v, _ in cases]
        if values != list(range(len(values))):
            problems.append(f"{label}: Swift `{enum}` raw values are not 0..n in order: {values}")
        if len(cases) != len(words):
            problems.append(f"{label}: Swift `{enum}` has {len(cases)} cases, "
                            f"build_theme.py {atlas} has {len(words)} cells")
            continue
        for (case, value, comment), word in zip(cases, words):
            if value == 0:
                continue   # the blank / em-dash cell -- spelled differently on each side
            if how == "comment" and not comment:
                continue   # undocumented case: the count check above still covers it
            swift_word = case if how == "name" else comment
            if _norm(swift_word) != _norm(word):
                problems.append(f"{label} {value}: Swift `{enum}.{case}` reads "
                                f"{swift_word!r}, but build_theme.py cell {value} is {word!r}")
    return problems


def fingerprint(contract: dict) -> str:
    blob = json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def app_bundles() -> list:
    override = os.environ.get("STARFLEET_APP")
    if override:
        return [Path(override)]
    candidates = (ROOT / APP_NAME, Path("/Applications") / APP_NAME,
                  Path.home() / "Applications" / APP_NAME)
    return [p for p in candidates if p.is_dir()]


def _read_fingerprint(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("fingerprint")
    except (OSError, ValueError, AttributeError):
        return None


def _stale_theme_warning(img: Path, src: Path = THEME_SRC):
    try:
        if Path(img).stat().st_mtime < Path(src).stat().st_mtime:
            return (f"{Path(img).name} is older than {Path(src).name} -- it has not been "
                    f"rebuilt since the theme source last changed. Re-run build_theme.py.")
    except OSError:
        pass
    return None


# ------------------------------------------------------------------ flash time

def verify_flash(theme_file, theme_src: Path = THEME_SRC):
    """(blockers, warnings) for flashing `theme_file` now. No blockers = safe.

    A blocker is an installed app that would write a different channel layout than
    this theme draws -- exactly the 2026-09-14 failure.
    """
    want = fingerprint(theme_contract(theme_src))
    blockers, warnings = [], []
    stale = _stale_theme_warning(Path(theme_file), theme_src)
    if stale:
        warnings.append(stale)
    bundles = app_bundles()
    if not bundles:
        warnings.append(f"no {APP_NAME} found (repo root, /Applications, ~/Applications), "
                        f"so nothing confirms the app speaks this theme")
    for bundle in bundles:
        have = _read_fingerprint(bundle / STAMP)
        if have is None:
            blockers.append(f"{bundle.name} ({bundle.parent}) has no panel-contract stamp: it "
                            f"predates this check, so its channel layout is unknown. "
                            f"Rebuild it with ./package.sh first.")
        elif have != want:
            blockers.append(f"{bundle.name} ({bundle.parent}) was built for panel contract "
                            f"{have}, this theme is {want}. After flashing, the app would write "
                            f"the wrong channels and the panel would show wrong words "
                            f"(2026-09-14: GLM-5.3 as 'OTHER TP2'). Rebuild it with "
                            f"./package.sh first.")
    return blockers, warnings


def record_flash(theme_file, theme_src: Path = THEME_SRC) -> None:
    """Remember which contract is on the panel, so package.sh can tell whether a new
    build still matches it. Only called after a flash succeeded."""
    theme_file = Path(theme_file)
    FLASHED.write_text(json.dumps({
        "fingerprint": fingerprint(theme_contract(theme_src)),
        "theme_file": str(theme_file.resolve()),
        "theme_sha256": hashlib.sha256(theme_file.read_bytes()).hexdigest(),
        "flashed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, indent=1) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- package time

def cmd_check(args) -> int:
    try:
        problems = mismatches(args.swift, args.theme)
        fp = fingerprint(theme_contract(args.theme))
    except (ContractError, OSError, SyntaxError) as exc:
        print(f"panel contract: cannot check -- {exc}", file=sys.stderr)
        return 1
    if problems:
        print("panel contract: PanelController.swift and build_theme.py DISAGREE -- "
              "not building an app that would drive the panel wrong:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"panel contract {fp}: PanelController.swift and build_theme.py agree")
    stale = _stale_theme_warning(THEME_IMG, args.theme)
    if stale:
        print(f"  warning: {stale}", file=sys.stderr)
    flashed = _read_fingerprint(FLASHED)
    if flashed is None:
        print("  note: no record of which theme is on the panel yet "
              "(upload_theme.py writes one from its next flash on)", file=sys.stderr)
    elif flashed != fp:
        print(f"  WARNING: the panel was last flashed with contract {flashed}, this build "
              f"speaks {fp}. Flash panel-theme/img.dat after this build, or the panel "
              f"will show wrong words.", file=sys.stderr)
    return 0


def cmd_stamp(args) -> int:
    contract = theme_contract(args.theme)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"fingerprint": fingerprint(contract), "contract": contract},
                              indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def cmd_verify_flash(args) -> int:
    blockers, warnings = verify_flash(args.theme_file, args.theme)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for b in blockers:
        print(f"MISMATCH: {b}", file=sys.stderr)
    print("flash check: " + ("OK" if not blockers else "would be refused"))
    return 1 if blockers else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="Swift vs build_theme.py; fails on any mismatch")
    c.add_argument("--swift", type=Path, default=SWIFT_SRC)
    c.add_argument("--theme", type=Path, default=THEME_SRC)
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("stamp", help="write this contract's fingerprint to OUT")
    s.add_argument("out")
    s.add_argument("--theme", type=Path, default=THEME_SRC)
    s.set_defaults(fn=cmd_stamp)
    v = sub.add_parser("verify-flash", help="what upload_theme.py checks, without flashing")
    v.add_argument("theme_file", nargs="?", default=str(THEME_IMG))
    v.add_argument("--theme", type=Path, default=THEME_SRC)
    v.set_defaults(fn=cmd_verify_flash)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
