"""OpenCode sessions for Computer (desktop) and the PADD phone page.

Fleet never talks to OpenCode's HTTP port itself: `opencode api` authenticates against the background service of
that machine (locally, or over ssh on the other Spark), so no password is read or stored here. Only the
routes in READ/WRITE below can be reached, bodies are cut down to the fields the UI uses, and models are
filtered. An OpenCode update shows up in contract(): it compares the live /openapi.json with
ocbridge-contract.json (the operations and request fields this code relies on).
Run `python3 ocbridge.py` for the self-check."""
import json, os, re, shlex, subprocess, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OC = "~/.opencode/bin/opencode"
SWAP_PUBLIC = os.environ.get("FLEET_SWAP_PUBLIC", "http://127.0.0.1:28080")
LOCAL_MODELS = {"jean-luc": True, "kathryn": True}    # llama-swap lives on Jean-Luc; Kathryn's OpenCode points at http://jean-luc:28080
AUTO_DIRS = [os.path.expanduser("~/llm-wiki")]       # sessions started by the wiki ingest jobs, hidden behind a switch
HOME = os.path.expanduser("~")                          # the same user name on both Sparks
SID, RID = r"ses_[A-Za-z0-9]{8,40}", r"[A-Za-z0-9_-]{4,64}"
QUERY_OK = {"limit": r"\d{1,3}", "order": r"asc|desc", "cursor": r"[A-Za-z0-9_=-]{1,400}", "archived": r"true|false",
            "search": r".{1,80}", "type": r"[a-z.,]{1,60}", "location[directory]": r"/[^\x00]{0,400}"}

READ = [r"/api/info", r"/api/session", r"/api/session/active", rf"/api/session/{SID}",
        rf"/api/session/{SID}/(message|permission|context)", r"/api/(model|agent|command|mcp|project)"]
WRITE = {rf"/api/session/{SID}/(prompt|interrupt|model|agent|compact|command)": "session",
         rf"/api/session/{SID}/permission/{RID}/reply": "reply",
         r"/api/experimental/mcp/[A-Za-z0-9_.-]{1,64}/(connect|disconnect)": "mcp",
         r"/api/session": "create"}


class OcError(OSError):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


def call(host, method, path, body=None, timeout=25):
    """One `opencode api` call on a node (host None = this machine). Returns the parsed JSON."""
    args = ["api", method, path] + (["-d", json.dumps(body)] if body is not None else [])
    # The service starts on the first call and keeps this environment, so the provider keys in .env are loaded here.
    sh = "set -a; . ~/.config/opencode/.env 2>/dev/null; set +a; exec " + " ".join([OC] + [shlex.quote(a) for a in args])
    cmd = ["bash", "-c", sh] if host is None else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "bash -c " + shlex.quote(sh)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.SubprocessError as e:
        raise OcError(str(e)[:200])
    out = r.stdout.strip()
    m = re.search(r"HTTP (\d{3}) ([^\n]*)", r.stdout + r.stderr)
    if m and not m.group(1).startswith("2"):
        raise OcError(f"OpenCode answered {m.group(1)} {m.group(2)} {(out or '')[:160]}", int(m.group(1)))
    if r.returncode and not out:
        raise OcError((r.stderr.strip() or f"exit {r.returncode}")[-200:])
    if not out:
        return {}
    try:
        return json.loads(out)
    except ValueError:
        raise OcError(out[:200])


def clean_query(q):
    """Only the query keys the UI uses, each with a strict value."""
    out = []
    for k, v in q:
        if k in QUERY_OK and re.fullmatch(QUERY_OK[k], v) and ".." not in v:
            out.append((k, v))
    return urllib.parse.urlencode(out, safe="/[]") if out else ""


def route_kind(method, path):
    pats = READ if method == "GET" else list(WRITE) if method == "POST" else []
    for p in pats:
        if re.fullmatch(p, path):
            return "read" if method == "GET" else WRITE[p]
    return None


def clip(v, n):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"\n... [{len(v) - n} more characters]"
    if isinstance(v, list):
        return [clip(x, n) for x in v]
    if isinstance(v, dict):
        return {k: clip(x, n) for k, x in v.items()}
    return v


def slim_message(m):
    """Cut long tool input/output and reasoning so a phone is not sent megabytes."""
    for c in m.get("content") or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            c["text"] = clip(c.get("text", ""), 8000)
        elif c.get("type") == "reasoning":
            c["text"] = clip(c.get("text", ""), 2500)
            c.pop("native", None)
        elif c.get("type") == "tool" and isinstance(c.get("state"), dict):
            s = c["state"]
            s["input"] = clip(s.get("input"), 1200)
            s["content"] = clip(s.get("content"), 2500)
            s.pop("metadata", None)
    m.pop("snapshot", None)
    return m


# ---------------------------------------------------------------- models
def loaded_models():
    try:
        with urllib.request.urlopen(SWAP_PUBLIC + "/running", timeout=3) as r:
            return {x["model"] for x in json.load(r).get("running", []) if x.get("state") == "ready"}
    except (OSError, ValueError):
        return None


def pick_models(node, raw):
    """Local (llama-swap), Anthropic and free models. Everything else on a cloud meter is hidden."""
    loaded, out = loaded_models(), []
    for m in raw:
        prov, cost = m.get("providerID"), m.get("cost")
        free = bool(cost) and all(not c.get("input") and not c.get("output") for c in cost)
        if prov == "llamaswap":
            if not LOCAL_MODELS.get(node):
                continue
            group = "local"
        elif prov == "anthropic":
            group = "anthropic"
        elif free:
            group = "free"
        else:
            continue
        if m.get("enabled") is False or m.get("status") not in (None, "active") or m.get("type", "chat") != "chat":
            continue
        out.append({"id": m["id"], "providerID": prov, "name": m.get("name") or m["id"], "group": group,
                    "loaded": None if group != "local" or loaded is None else m["id"] in loaded,
                    "ctx": (m.get("limit") or {}).get("context"), "vision": "image" in (m.get("capabilities") or {}).get("input", []),
                    "variants": [v["id"] for v in m.get("variants", [])]})
    order = {"local": 0, "anthropic": 1, "free": 2}
    out.sort(key=lambda x: (order[x["group"]], not x["loaded"], x["name"].lower()))
    return out


def model_allowed(node, ref):
    if not isinstance(ref, dict):
        return False
    raw = call(HOSTS[node], "GET", "/api/model")["data"]
    m = next((x for x in pick_models(node, raw) if x["id"] == ref.get("id") and x["providerID"] == ref.get("providerID")), None)
    if not m or m["loaded"] is False:
        return False
    return ref.get("variant") in (None, "default") or ref["variant"] in m["variants"]


# ---------------------------------------------------------------- the one entry point
HOSTS = {"jean-luc": None, "kathryn": "10.100.0.1"}      # fleet.py overwrites this with its NODES
_cache, _lock = {}, threading.Lock()


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def projects(node):
    d = cached(("proj", node), 30, lambda: call(HOSTS[node], "GET", "/api/project"))
    d = d.get("data", []) if isinstance(d, dict) else d      # 2.4.0 answers a bare list
    out = {HOME: {"dir": HOME, "active": 0}}                  # the home folder is always offered (a fresh node has no projects yet)
    for p in d:
        out[p["canonical"]] = {"dir": p["canonical"], "active": (p.get("time") or {}).get("active", 0)}
    return sorted(out.values(), key=lambda x: -x["active"])


def overview(node, auto=False):
    host = HOSTS[node]
    info = call(host, "GET", "/api/info", timeout=15)
    with ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(call, host, "GET", "/api/session?limit=80&archived=false")
        f2 = ex.submit(call, host, "GET", "/api/session/active")
        rows, active = f1.result()["data"], f2.result().get("data") or {}
    sess = []
    for s in rows:
        d = (s.get("location") or {}).get("directory", "")
        t = s.get("time") or {}
        sess.append({"id": s["id"], "title": s.get("title") or "(untitled)", "dir": d, "model": s.get("model"),
                     "tokens": (s.get("tokens") or {}).get("input", 0), "outcome": s.get("outcome"),
                     "updated": t.get("updated"), "busy": s["id"] in active or (t.get("busy", 0) > t.get("idle", 0)),
                     "auto": d in AUTO_DIRS, "waiting": 0})
    watch = [s for s in sess if s["busy"]][:8]
    with ThreadPoolExecutor(4) as ex:
        for s, r in zip(watch, ex.map(lambda s: _try(call, host, "GET", f"/api/session/{s['id']}/permission"), watch)):
            s["waiting"] = len((r or {}).get("data") or [])
    return {"node": node, "version": info.get("version"), "host": info.get("hostname"), "sessions": sess,
            "autoCount": sum(1 for s in sess if s["auto"]), "local": LOCAL_MODELS.get(node, False)}


def _try(fn, *a):
    try:
        return fn(*a)
    except OSError:
        return None


def detail(node, sid, limit=40):
    host = HOSTS[node]
    if not re.fullmatch(SID, sid or ""):
        raise OcError("bad session id", 400)
    with ThreadPoolExecutor(4) as ex:
        fs = ex.submit(call, host, "GET", f"/api/session/{sid}")
        fm = ex.submit(call, host, "GET", f"/api/session/{sid}/message?limit={int(limit)}&order=desc")
        fp = ex.submit(_try, call, host, "GET", f"/api/session/{sid}/permission")
        fa = ex.submit(call, host, "GET", "/api/session/active")
        s, msgs, perms, act = fs.result()["data"], fm.result()["data"], fp.result(), fa.result().get("data") or {}
    msgs = [slim_message(m) for m in reversed(msgs)]
    t = s.get("time") or {}
    return {"node": node, "session": s, "busy": sid in act or t.get("busy", 0) > t.get("idle", 0), "messages": msgs,
            "permissions": clip((perms or {}).get("data") or [], 600)}


def mcp(node, directory):
    q = clean_query([("location[directory]", directory)])
    r = call(HOSTS[node], "GET", "/api/mcp" + ("?" + q if q else ""))
    out = [{"name": x["name"], "status": (x.get("status") or {}).get("status"), "error": clip((x.get("status") or {}).get("error"), 300)}
           for x in r["data"]]
    out.sort(key=lambda x: (x["status"] == "connected", x["name"].lower()))
    return {"dir": (r.get("location") or {}).get("directory"), "servers": out}


def proxy(node, method, path, query, body):
    """A UI request that passes the allow-list. Returns JSON for the page."""
    if node not in HOSTS:
        raise OcError("unknown node", 404)
    kind = route_kind(method, path)
    if not kind:
        raise OcError("this route is not offered", 403)
    host, q = HOSTS[node], clean_query(query)
    full = path + ("?" + q if q else "")
    if kind == "read":
        if path == "/api/model":
            return {"models": pick_models(node, call(host, "GET", full)["data"])}
        if path == "/api/agent":
            return {"agents": [{"id": a["id"], "name": a.get("name") or a["id"], "description": a.get("description", "")}
                               for a in call(host, "GET", full)["data"] if a.get("mode") == "primary" and not a.get("hidden")]}
        if path == "/api/command":
            return {"commands": [{"name": c["name"], "description": clip(c.get("description", ""), 120)}
                                 for c in call(host, "GET", full)["data"]]}
        return call(host, "GET", full)
    if not isinstance(body, dict):
        raise OcError("body must be an object", 400)
    if kind == "create":
        d = ((body.get("location") or {}).get("directory"))
        if d not in {p["dir"] for p in projects(node)}:
            raise OcError("start sessions only in a folder OpenCode already knows", 400)
        b = {"location": {"directory": d}}
        for k in ("title", "agent"):
            if isinstance(body.get(k), str) and body[k]:
                b[k] = body[k][:200]
        if body.get("model"):
            if not model_allowed(node, body["model"]):
                raise OcError("that model is not available here (hidden, or local and not loaded)", 400)
            b["model"] = body["model"]
        return call(host, "POST", path, b)
    if kind == "session":
        act = path.rsplit("/", 1)[1]
        if act == "interrupt":
            return call(host, "POST", path, {})
        if act == "model":
            if not model_allowed(node, body.get("model")):
                raise OcError("that model is not available here (hidden, or local and not loaded)", 400)
            return call(host, "POST", path, {"model": body["model"]})
        if act == "agent":
            return call(host, "POST", path, {"agent": str(body.get("agent", ""))[:80]})
        if act == "compact":
            return call(host, "POST", path, {})
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 60000:
            raise OcError("empty or too long", 400)
        b = {"text": text}
        if body.get("delivery") in ("steer", "queue"):
            b["delivery"] = body["delivery"]
        if act == "command":
            b["name"] = str(body.get("name", ""))[:120]
        return call(host, "POST", path, b)
    if kind == "reply":
        if body.get("decision") not in ("once", "always", "reject"):
            raise OcError("decision must be once, always or reject", 400)
        b = {"decision": body["decision"]}
        if isinstance(body.get("message"), str):
            b["message"] = body["message"][:500]
        return call(host, "POST", path, b)
    return call(host, "POST", full, {})                       # mcp connect / disconnect


def route(node, method, path, query, body):
    """fleet.py's entry: '@name' paths are the composites, anything else goes through proxy()."""
    q = dict(query)
    if path == "@overview":
        return overview(node, False)
    if path == "@detail":
        return detail(node, q.get("id", ""), int(q.get("limit", 40)))
    if path == "@mcp":
        return mcp(node, q.get("dir", ""))
    if path == "@projects":
        return {"projects": projects(node)}
    if path == "@contract":
        return contract(node)
    return proxy(node, method, path, query, body)


# ---------------------------------------------------------------- updates
def _snapshot(spec):
    pin = json.load(open(os.path.join(HERE, "ocbridge-contract.json")))
    live = {}
    for key in pin:
        m, p = key.split(" ", 1)
        o = (spec.get("paths", {}).get(p) or {}).get(m.lower())
        if not o:
            live[key] = None
            continue
        sch = ((o.get("requestBody") or {}).get("content", {}).get("application/json") or {}).get("schema") or {}
        live[key] = [o.get("operationId"), sorted(sch.get("properties", {})), sorted(sch.get("required", []))]
    return pin, live


def contract(node):
    """Does this node's OpenCode still offer what the UI uses? latest = newest release OpenCode announces."""
    host = HOSTS[node]
    spec, info = call(host, "GET", "/openapi.json", timeout=30), call(host, "GET", "/api/info")
    pin, live = _snapshot(spec)
    broken, added = [], []
    for k, v in pin.items():
        lv = live[k]
        if lv is None or lv[0] != v[0]:
            broken.append(f"{k}: gone or renamed")
        else:
            for f in v[2]:
                if f not in lv[2] and f not in lv[1]:
                    broken.append(f"{k}: field {f} is gone")
            added += [f"{k}: new field {f}" for f in lv[2] if f not in v[2]]      # a newly REQUIRED field breaks the call
    latest = cached("latest", 3600, _latest)
    return {"node": node, "version": info.get("version"), "latest": latest, "ok": not broken, "broken": broken,
            "newRequired": [a for a in added if a], "ops": len(spec.get("paths", {}))}


def _latest():
    try:
        with urllib.request.urlopen(urllib.request.Request("https://opencode.ai/update/api/latest/cli/npm", headers={"User-Agent": "fleet-ui"}), timeout=6) as r:
            return json.load(r).get("version")
    except (OSError, ValueError):
        return None


def selfcheck():
    assert route_kind("GET", "/api/session") == "read" and route_kind("GET", "/api/session/ses_abcdefgh12/message") == "read"
    assert route_kind("POST", "/api/session/ses_abcdefgh12/prompt") == "session"
    assert route_kind("POST", "/api/session") == "create" and route_kind("POST", "/api/session/ses_abcdefgh12/permission/per_1234/reply") == "reply"
    for bad in [("GET", "/api/credential"), ("POST", "/api/session/ses_abcdefgh12/shell"), ("POST", "/api/experimental/fs/write"),
                ("GET", "/api/config"), ("POST", "/api/pty"), ("GET", "/api/session/../config"), ("POST", "/api/session/ses_abcdefgh12/revert")]:
        assert route_kind(*bad) is None, bad
    assert clean_query([("limit", "5"), ("evil", "1"), ("cursor", "a/../b"), ("location[directory]", "/home/x")]) == "limit=5&location[directory]=/home/x"
    raw = [{"id": "a", "providerID": "huggingface", "cost": [{"input": 1, "output": 2}], "type": "chat"},
           {"id": "b", "providerID": "opencode", "cost": [{"input": 0, "output": 0}], "type": "chat"},
           {"id": "c", "providerID": "anthropic", "cost": [{"input": 8, "output": 40}], "type": "chat"},
           {"id": "d", "providerID": "llamaswap", "cost": [], "type": "chat"}]
    assert [m["id"] for m in pick_models("kathryn", raw)] == ["d", "c", "b"] and {m["id"] for m in pick_models("jean-luc", raw)} == {"b", "c", "d"}
    LOCAL_MODELS["kathryn"] = False
    assert [m["id"] for m in pick_models("kathryn", raw)] == ["c", "b"]
    LOCAL_MODELS["kathryn"] = True
    assert len(clip("x" * 5000, 100)) < 200
    print("ocbridge self-check ok")


if __name__ == "__main__":
    selfcheck()
