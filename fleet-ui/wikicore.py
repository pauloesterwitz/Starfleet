"""Memory Core: the LLM wiki as one graph, and the path Computer took through it. Read-only.

The graph comes from memory-core's own link scanner (memory_core.graph, run with memory-core's venv python), so the map
shows exactly the relations Computer's wiki_search and wiki_related follow. It only reads the vault, so it works while the
memory-core service is stopped. The last good map is also kept on disk, so a Fleet restart still shows the core. The trails come from Computer's chat files
(fleet.py COMPUTER_DIR): every wiki_* call with its arguments, what it returned, and what the answer cited.
Nothing here writes to the wiki, the chats or the container."""
import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

MEMORY_CORE = os.path.expanduser(os.environ.get("FLEET_MEMORY_CORE", "~/memory-core"))   # the wiki MCP server's project folder
TTL = 600          # seconds a graph export is reused; a stale one is served while the next builds in the background
MAX_CHATS = 40     # newest chats scanned for trails
MAX_HITS = 40      # hits kept per step
CACHE_FILE = os.path.expanduser("~/.cache/fleet-ui/wiki-graph.json")

# Runs with memory-core's python (python -). graph.get only stats and parses the vault.
_EXPORT = r'''
import json, os
from pathlib import Path
from memory_core import graph
v = Path(os.environ["WIKI_VAULT"])
g = graph.get(v)
idx = {p: i for i, p in enumerate(g.nodes)}
nodes = [[p, g.parsed[p]["title"], g.parsed[p]["type"], g.parsed[p]["tags"][:5], round(g.in_weight.get(p, 0), 2),
          g.summary.get(p, "")] for p in g.nodes]
edges = [[idx[u], idx[w], "r" if "related" in ts else "s" if "source" in ts else "l"] for (u, w), ts in g.dedge.items()]
log = v / "wiki/log.md"
print(json.dumps({"nodes": nodes, "edges": edges, "stubs": len(g.pages) - len(g.nodes),
                  "log_mtime": log.stat().st_mtime if log.is_file() else None}, ensure_ascii=False, separators=(",", ":")))
'''

_lock = threading.Lock()
_cache = {"at": 0.0, "json": None, "error": None, "note": None, "busy": False}


def _export_cmd():
    """(argv, note): memory-core's venv python reading the vault; the note says when the service is down."""
    py = os.path.join(MEMORY_CORE, ".venv", "bin", "python")
    if not os.access(py, os.X_OK):
        raise OSError(f"{py} not found")
    argv = ["env", f"PYTHONPATH={MEMORY_CORE}", "WIKI_VAULT=" + os.path.expanduser("~/llm-wiki"), py, "-"]
    r = subprocess.run(["systemctl", "--user", "is-active", "memory-core"], capture_output=True, text=True, timeout=10)
    note = None if r.stdout.strip() == "active" else "memory-core is stopped: Computer cannot use the wiki until it runs again."
    return argv, note


def _refresh():
    data, err, note = None, None, None
    try:
        argv, note = _export_cmd()
        r = subprocess.run(argv, input=_EXPORT, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise OSError((r.stderr or "export failed").strip().splitlines()[-1][:300])
        json.loads(r.stdout)                 # refuse to cache garbage
        data = r.stdout.strip()
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), mode=0o700, exist_ok=True)
            with open(CACHE_FILE + ".tmp", "w", encoding="utf-8") as f:
                f.write(data)
            os.chmod(CACHE_FILE + ".tmp", 0o600)
            os.replace(CACHE_FILE + ".tmp", CACHE_FILE)
        except OSError:
            pass                             # the disk copy is a convenience only
    except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError) as e:
        err = f"wiki graph from memory-core: {str(e)[:300]}"
    with _lock:
        if data:
            _cache["json"], _cache["at"] = data, time.time()
        elif _cache["json"] is None and os.path.isfile(CACHE_FILE):
            try:
                with open(CACHE_FILE, encoding="utf-8") as f:
                    _cache["json"] = f.read()
                _cache["at"] = os.path.getmtime(CACHE_FILE)
                err = (err or "") + " Showing the last saved map."
            except OSError:
                pass
        _cache["error"], _cache["note"], _cache["busy"] = err, note, False


def graph_json():
    """JSON text {graph, at, error, note, refreshing}. The first call waits for the export (about 4 s); later calls never wait."""
    with _lock:
        start = time.time() - _cache["at"] > TTL and not _cache["busy"]
        if start:
            _cache["busy"] = True
        first = _cache["json"] is None
    if start:
        if first:
            _refresh()
        else:
            threading.Thread(target=_refresh, daemon=True).start()
    with _lock:
        return ('{"graph":' + (_cache["json"] or "null") + ',"at":' + json.dumps(_cache["at"]) + ',"error":'
                + json.dumps(_cache["error"]) + ',"note":' + json.dumps(_cache["note"]) + ',"refreshing":'
                + json.dumps(_cache["busy"]) + "}")


# ---------------------------------------------------------------- trails

_HIT = re.compile(r"^\s*\d+\.\s+`(wiki/[^`]+?\.md)`\s*\|\s*(.*?)\s*\|\s*([^|]*?)\s*\|\s*(.*)$")
_VIA = re.compile(r"via \[\[([^\]|#]+)[^\]]*\]\]\s*\((\w+)\)")
_PATH = re.compile(r"wiki/[^\n`<>|\"\]]*?\.md")
_WLINK = re.compile(r"\[\[([^\]|#^\n]+)")


def _paths(text):
    seen, out = set(), []
    for p in _PATH.findall(urllib.parse.unquote(text or "")):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _text(content):
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content or ""


def _step(name, args, result, at):
    """One tool call as a trail step."""
    result = result or ""
    st = {"at": at, "error": result.startswith(("TOOL ERROR", "Tool ")),
          "blocked": result.startswith("Refused: web search is off")}
    if not name.startswith("wiki_"):
        q = args.get("q") or args.get("query") or args.get("url") or ""
        return dict(st, tool="web", name=name, arg=str(q)[:200], hits=[])
    tool = name[5:]
    st.update(tool=tool, name=name, hits=[])
    if tool == "search":
        st["arg"] = str(args.get("query", ""))[:200]
        for ln in result.splitlines():
            m = _HIT.match(ln)
            if m:
                via = _VIA.search(m.group(4))
                st["hits"].append({"p": m.group(1), "how": m.group(4)[:120],
                                   "via": via.group(1).strip() if via else None, "rel": via.group(2) if via else None,
                                   "text": "text fallback" in m.group(4)})
        if not st["hits"]:
            st["hits"] = [{"p": p} for p in _paths(result)]
    elif tool in ("read", "related"):
        st["arg"] = str(args.get("path", ""))[:200]
        first = result.split("\n", 1)[0]
        st["target"] = first[2:].strip() if first.startswith("# wiki/") else st["arg"]
        if tool == "related":
            st["depth"] = args.get("depth", 1)
            st["hits"] = [{"p": p} for p in _paths(result) if p != st["target"]]
    else:                                   # overview and anything newer
        st["arg"] = ""
        m = re.search(r"Pages: (\d+)", result)
        st["pages"] = int(m.group(1)) if m else None
    st["hits"] = st["hits"][:MAX_HITS]
    return st


def trails(chat_dir):
    """[{id, title, updated, model, steps}] for the newest chats that used the wiki, newest first."""
    files = sorted(glob.glob(os.path.join(chat_dir, "*.json")), key=lambda p: -os.path.getmtime(p))[:MAX_CHATS]
    out = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                chat = json.load(f)
            msgs = chat["messages"]
        except (OSError, ValueError, KeyError):
            continue
        results = {m.get("tool_call_id"): m for m in msgs if m.get("role") == "tool"}
        steps, used = [], False
        for m in msgs:
            if m.get("role") == "user":
                steps.append({"tool": "ask", "arg": _text(m.get("content"))[:240], "at": m.get("at"), "hits": []})
            elif m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    res = results.get(tc.get("id")) or {}
                    s = _step(fn.get("name") or "", args if isinstance(args, dict) else {}, res.get("content"),
                              res.get("at") or m.get("at"))
                    used = used or s["tool"] not in ("web", "ask")
                    steps.append(s)
                if not m.get("tool_calls") and (m.get("content") or "").strip():
                    c = m["content"]
                    steps.append({"tool": "answer", "at": m.get("at"), "hits": [],
                                  "cites": (_paths(c) + [w.strip() for w in _WLINK.findall(c)])[:MAX_HITS]})
        if used:
            out.append({"id": chat.get("id"), "title": chat.get("title") or "Untitled", "model": chat.get("model"),
                        "updated": chat.get("updated") or os.path.getmtime(path), "steps": steps})
    return out


# ---------------------------------------------------------------- one page (the Open button)

WIKI_ROOT = os.path.expanduser(os.environ.get("FLEET_WIKI_ROOT", "~/llm-wiki"))   # its wiki/ holds the pages
PAGE_MAX = 1_000_000        # characters returned of one page


def page_md(rel):
    """Markdown of one page by its graph path ("wiki/concepts/X.md"), read straight from disk, so it works while the
    memory-core service is stopped. Only .md files inside the wiki folder; anything resolving outside it is refused."""
    base = os.path.realpath(os.path.join(WIKI_ROOT, "wiki"))
    if not isinstance(rel, str) or not rel.startswith("wiki/") or not rel.lower().endswith(".md") or "\0" in rel:
        raise ValueError("not a wiki page")
    path = os.path.realpath(os.path.join(base, rel[len("wiki/"):]))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise ValueError("not a wiki page")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(PAGE_MAX)


if __name__ == "__main__":       # self-check: python3 wikicore.py
    for bad in ("wiki/../etc/passwd.md", "../wiki/x.md", "/etc/passwd", "wiki/x.txt", "wiki/no-such-page.md", None):
        try:
            page_md(bad)
            raise AssertionError(f"page_md accepted {bad!r}")
        except ValueError:
            pass
    s = _step("wiki_search", {"query": "example router"},
              "Search \"x\"\n1. `wiki/entities/Example Router.md` | Example Router | entity | entry (catalogue match: title)\n"
              "2. `wiki/entities/Example Server (v2).md` | Example Server (v2) | entity | via [[Example Router]] (related): x\n", 1.0)
    assert [h["p"] for h in s["hits"]] == ["wiki/entities/Example Router.md", "wiki/entities/Example Server (v2).md"]
    assert s["hits"][1]["via"] == "Example Router" and s["hits"][1]["rel"] == "related" and s["hits"][0]["via"] is None
    r = _step("wiki_read", {"path": "Example Router"}, "# wiki/entities/Example Router.md\nOpen in browser: x", 2.0)
    assert r["target"] == "wiki/entities/Example Router.md"
    w = _step("google_search", {"q": "x"}, "Refused: web search is off for the rest of this answer", 3.0)
    assert w["tool"] == "web" and w["blocked"]
    assert _paths("open: http://h/p/wiki/concepts/A%20B%20%28c%29.md)") == ["wiki/concepts/A B (c).md"]
    print("wikicore self-check ok")
