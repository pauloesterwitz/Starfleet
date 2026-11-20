"""Memory Core's Edit button: save one wiki page's markdown and/or rename it, as the human editing in Obsidian would.

Runs with memory-core's venv python (PYTHONPATH=~/memory-core), so it takes the same vault locks, name rules and link
resolver as memory-core's own Apply. Request JSON on stdin: {"rel": "wiki/x/Old.md", "sha": <sha256 of the bytes the
editor loaded>, "text": <new markdown or null>, "stem": <new file name without .md, or null>}. A rename rewrites every
[[link]] and ![[embed]] that resolves to the page, and the page's entries in .raw/.manifest.json address_map. Every file
it changes is copied to WIKI_EDIT_BACKUP/<time>/<rel> first. Answer JSON on stdout: {"rel", "sha", "relinked"} or
{"error", "conflict"?}.  Self-check: python wikiedit.py --selfcheck (builds a throwaway vault in /tmp)."""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from memory_core import vaultio as vio

_LINK = re.compile(r"(!?)\[\[([^\]\n]{1,300})\]\]")
_CODE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.S)


class Refused(ValueError):
    pass


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def relink(text: str, old_rel: str, new_stem: str, smap: dict) -> tuple[str, int]:
    """Point every wikilink/embed that resolves to old_rel at new_stem; keeps path prefix, .md, #heading, |alias.
    Code spans and blocks are left alone."""
    n = 0

    def sub(m):
        nonlocal n
        tgt, suf = re.match(r"([^|#^]*)(.*)", m.group(2), re.S).groups()
        clean = tgt.rstrip("\\")
        if vio.resolve_link(clean, smap) != [old_rel]:
            return m.group(0)
        t = clean.strip()
        md = t.endswith(".md")
        base = t[:-3] if md else t
        prefix = base.rsplit("/", 1)[0] + "/" if "/" in base else ""
        n += 1
        return f"{m.group(1)}[[{prefix}{new_stem}{'.md' if md else ''}{tgt[len(clean):]}{suf}]]"
    parts = _CODE.split(text)
    return "".join(p if i % 2 else _LINK.sub(sub, p) for i, p in enumerate(parts)), n


def edit(vault: Path, bdir: Path, req: dict) -> dict:
    rel, want, text, stem = vio.nfc(str(req.get("rel") or "")), req.get("sha"), req.get("text"), req.get("stem")
    parts = rel.split("/")
    if (parts[0] != "wiki" or not rel.endswith(".md") or rel in vio.MANAGED or "\x00" in rel or "\\" in rel
            or any(p in ("", ".", "..") or p.startswith(".") for p in parts)):
        raise Refused("only wiki pages can be edited here (index, log and hot are managed by memory-core)")
    fp = vio.real_inside(vault, rel)
    if not fp.is_file():
        raise Refused("the page no longer exists")
    if text is not None and (not isinstance(text, str) or len(text.encode()) > 2_000_000):
        raise Refused("the text is missing or larger than 2 MB")
    old_stem = parts[-1][:-3]
    stem = vio.nfc(stem.strip()) if isinstance(stem, str) and stem.strip() else old_stem
    new_rel, linkers, mkeys = rel, [], []
    if stem != old_stem:
        if len(parts) < 3:
            raise Refused("top-level pages keep their name")
        err = vio.check_stem(stem)
        if err:
            raise Refused(err + ' (not allowed: / \\ : * ? " < > | # ^ [ ])')
        new_rel = "/".join(parts[:-1] + [stem + ".md"])
        smap = vio.stem_map(vault)
        if smap.get(stem.lower(), [rel]) != [rel]:
            raise Refused(f"a page named {stem!r} already exists")
        for r in vio.walk_wiki(vault):
            if r.endswith(".md") and r != rel and relink((vault / r).read_text(encoding="utf-8", errors="replace"),
                                                         rel, stem, smap)[1]:
                linkers.append(r)
        try:
            mkeys = [k for k, v in vio.read_manifest(vault).get("address_map", {}).items() if v == rel]
        except (OSError, ValueError):
            mkeys = []
    targets = sorted({rel, new_rel, *linkers} | ({".raw/.manifest.json"} if mkeys else set()))
    held, pid = [], os.getpid()
    try:
        for p in targets:
            try:
                got = vio.lock_acquire(vault, p, pid)
            except TimeoutError:
                got = False
            if not got:
                raise Refused(f"{p} is being written by memory-core right now; try again in a minute")
            held.append(p)
        raw = fp.read_bytes()
        if sha(raw) != want:
            return {"error": "the page changed on disk since you opened it; reopen it and edit again", "conflict": True}
        if new_rel != rel and (vault / new_rel).exists() and new_rel.lower() != rel.lower():
            raise Refused(f"a page named {stem!r} already exists")
        stamp = bdir / time.strftime("%Y%m%d-%H%M%S")
        changed, smap = {}, vio.stem_map(vault) if new_rel != rel else {}
        for r in linkers:
            t = (vault / r).read_text(encoding="utf-8")
            nt, k = relink(t, rel, stem, smap)
            if k:
                changed[r] = nt
        body = raw.decode("utf-8", errors="replace") if text is None else text
        if text is not None and text != raw.decode("utf-8", errors="replace") and vio.split_fm(body)[0]:
            body = vio.bump_updated(body)
        if new_rel != rel:
            body = relink(body, rel, stem, smap)[0]         # links to itself
        for r in [rel, *changed] + ([".raw/.manifest.json"] if mkeys else []):
            (stamp / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vault / r, stamp / r)
        for r, t in changed.items():
            vio.atomic_write(vault / r, t, newline=t.endswith("\n"))
        if mkeys:
            m = vio.read_manifest(vault)
            for k in mkeys:
                if m["address_map"].get(k) == rel:
                    m["address_map"][k] = new_rel
            vio.write_manifest(vault, m)
        vio.atomic_write(vault / new_rel, body, newline=body.endswith("\n"))
        if new_rel != rel:
            os.unlink(fp) if (vault / new_rel).resolve() != fp.resolve() else None
        return {"rel": new_rel, "sha": sha((vault / new_rel).read_bytes()), "relinked": sorted(changed),
                "backup": str(stamp)}
    finally:
        for p in held:
            try:
                vio.lock_release(vault, p)
            except Exception:
                pass


def _selfcheck():
    with tempfile.TemporaryDirectory() as d:
        v = Path(d)
        for r, t in {"wiki/entities/Old Name.md": "---\ntitle: Old\nupdated: 2020-01-01\n---\nSelf [[Old Name]].\n",
                     "wiki/concepts/A.md": "See [[Old Name|alias]], [[entities/Old Name#H]], ![[Old Name]], `[[Old Name]]`, [[A]].\n",
                     "wiki/index.md": "- [[Old Name]]\n", "wiki/concepts/Taken.md": "x\n"}.items():
            (v / r).parent.mkdir(parents=True, exist_ok=True)
            (v / r).write_text(t, encoding="utf-8")
        (v / ".raw").mkdir()
        (v / ".raw/.manifest.json").write_text(json.dumps({"address_map": {"c-1": "wiki/entities/Old Name.md"}}))
        b = v / "bk"
        s0 = sha((v / "wiki/entities/Old Name.md").read_bytes())
        assert edit(v, b, {"rel": "wiki/entities/Old Name.md", "sha": "x", "text": "y"})["conflict"]
        for bad in ({"rel": "wiki/index.md", "sha": s0}, {"rel": "wiki/../x.md", "sha": s0},
                    {"rel": "wiki/entities/Old Name.md", "sha": s0, "stem": "Taken"},
                    {"rel": "wiki/entities/Old Name.md", "sha": s0, "stem": "a/b"}):
            try:
                edit(v, b, bad)
                raise AssertionError(f"accepted {bad}")
            except Refused:
                pass
        r = edit(v, b, {"rel": "wiki/entities/Old Name.md", "sha": s0, "stem": "New Name",
                        "text": "---\ntitle: New\nupdated: 2020-01-01\n---\nSelf [[Old Name]].\n"})
        assert r["rel"] == "wiki/entities/New Name.md" and not (v / "wiki/entities/Old Name.md").exists(), r
        assert r["relinked"] == ["wiki/concepts/A.md", "wiki/index.md"], r
        a = (v / "wiki/concepts/A.md").read_text()
        assert a == "See [[New Name|alias]], [[entities/New Name#H]], ![[New Name]], `[[Old Name]]`, [[A]].\n", a
        p = (v / "wiki/entities/New Name.md").read_text()
        assert "Self [[New Name]]" in p and "title: New" in p and "2020-01-01" not in p, p
        assert json.loads((v / ".raw/.manifest.json").read_text())["address_map"]["c-1"] == "wiki/entities/New Name.md"
        assert (Path(r["backup"]) / "wiki/entities/Old Name.md").is_file()
        assert not list((v / ".vault-meta/locks").glob("*.lock"))
    print("wikiedit self-check ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selfcheck"]:
        _selfcheck()
        sys.exit(0)
    try:
        out = edit(Path(os.environ["WIKI_VAULT"]), Path(os.environ["WIKI_EDIT_BACKUP"]), json.load(sys.stdin))
    except (Refused, vio.VaultError, OSError, ValueError) as e:
        out = {"error": str(e)[:300]}
    print(json.dumps(out, ensure_ascii=False))
