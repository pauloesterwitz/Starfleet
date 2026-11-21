"""Speech family of Shuttle Bay (the Fleet UI): speech-to-text servers (Whisper, Cohere Transcribe, Voxtral) that Fleet itself
starts and stops, one server per member per node, through ~/llama-swap/speech-ctl (local for Jean-Luc, ssh for Kathryn).

Why not llama-swap members: a config.yaml write hot-reloads llama-swap and restarts the pinned model, and these servers are
not chat models. They follow the image family's pattern instead (rows outside STATE["models"], light-load queue, keyed
POST routes), but with a real probe: every few seconds `speech-ctl status` says what is running where, so the page shows the
truth, not "what we last put there".

Wired into fleet.py by a few lines (see the Shuttle Bay patch): init(), the probe thread, snap["speech"] in /api/state,
POST /api/speech/load|unload, and a stop_all() call inside red_alert. Nothing here touches STATE["models"], so the phone
page (PADD) and every other consumer of that list is unaffected.

  python3 speech.py        self-check of the pure logic (no node is contacted)
"""
import json
import os
import shlex
import subprocess
import threading
import time

REGISTRY = os.environ.get("SPEECH_REGISTRY", os.path.expanduser("~/llama-swap/speech-members.json"))
CTL = "~/llama-swap/speech-ctl"
POLL_S = 6
FLOOR_MB, FLOOR_SWAP_MB = 4096, 6144          # must match speech-ctl: reserve this much beyond a member's need

_cfg = {}        # init(): nodes, key_check, enqueue, dequeue, queued, get_error, clear_error
_node = {}       # node -> last `speech-ctl status` JSON plus "t" (or {"error", "t"})
_busy = {}       # row key -> {"op": "loading"|"unloading", "t0": epoch}
_last = {}       # row key -> outcome of the last load/unload, for the row and the toast
_lock = threading.Lock()
PIN_FILE = os.environ.get("SPEECH_PINS", os.path.expanduser("~/.config/fleet-ui/speech-pins.json"))


def pins():
    """Row keys (member-node) that are pinned."""
    try:
        with open(PIN_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _save_pins(p):
    os.makedirs(os.path.dirname(PIN_FILE), exist_ok=True)
    tmp = PIN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(p), f, indent=2)
    os.replace(tmp, PIN_FILE)


def set_pin(mid, node, on, key):
    """Pin (and load, if it is not up) or unpin one member on one node. Keyed like every load."""
    if mid not in members() or node not in _cfg["nodes"]:
        raise ValueError(f"unknown speech member {mid!r} or node {node!r}")
    _cfg["key_check"](key)
    rk, p = rowkey(mid, node), pins()
    if not on:
        p.discard(rk)
        _save_pins(p)
        return {"ok": True, "pinned": False}
    p.add(rk)
    _save_pins(p)
    with _lock:
        up = ((_node.get(node) or {}).get("members") or {}).get(mid, {}).get("running")
    if up:
        return {"ok": True, "pinned": True}
    return dict(load(mid, node, key), pinned=True)


def init(nodes, key_check, enqueue, dequeue, queued, get_error, clear_error):
    _cfg.update(nodes=nodes, key_check=key_check, enqueue=enqueue, dequeue=dequeue, queued=queued,
                get_error=get_error, clear_error=clear_error)


def members():
    try:
        with open(REGISTRY) as f:
            return json.load(f)["members"]
    except (OSError, ValueError, KeyError):
        return {}


def rowkey(mid, node):
    """Queue/error id of one member on one node. The trailing -<node> is the suffix convention every other row uses."""
    return f"{mid}-{node}"


def ctl(node, *args, timeout=60):
    """Run speech-ctl on `node`; always returns a dict (never raises). ok:false with an error text when it cannot."""
    host = _cfg["nodes"][node]
    cmd = [os.path.expanduser(CTL), *args] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, CTL + " " + shlex.join(args)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "error": str(e)[:200]}
    try:
        j = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        j = {"ok": False, "error": (r.stderr or r.stdout or "no answer from speech-ctl").strip()[-300:]}
    j.setdefault("rc", r.returncode)
    return j


def refresh(node):
    j = ctl(node, "status", timeout=40)
    with _lock:
        _node[node] = dict(j, t=time.time()) if "members" in j else {"error": j.get("error") or "status failed", "t": time.time()}


def probe_worker():
    while True:
        for node in _cfg["nodes"]:
            refresh(node)
        time.sleep(POLL_S)


def fit_from(nd, m):
    """Would member `m` fit on a node whose last status is `nd`? Same formula as speech-ctl fit, from the cached numbers."""
    if not nd or "avail_mb" not in nd:
        return {"ok": False, "need_mb": m.get("need_mb"), "reason": (nd or {}).get("error") or "node not probed yet"}
    swap = nd.get("swap_used_pct") or 0
    floor = int(os.environ["SPEECH_FLOOR_MB"]) if "SPEECH_FLOOR_MB" in os.environ else (FLOOR_SWAP_MB if swap > 80 else FLOOR_MB)
    eff = max(0, nd["avail_mb"] - (nd.get("reserved_mb") or 0))
    need = int(m["need_mb"])
    ok = eff >= need + floor
    return {"ok": ok, "need_mb": need, "avail_mb": eff, "floor_mb": floor, "swap_used_pct": swap,
            "reason": "fits" if ok else f"needs {need/1024:.1f} GB plus {floor/1024:.1f} GB reserve, {eff/1024:.1f} GB free (swap {swap} % used)"}


def state():
    """The "speech" object of /api/state. Cheap (cached probes) and never raises."""
    try:
        ms = members()
        with _lock:
            nodes, busy, last = {n: dict(v) for n, v in _node.items()}, {k: dict(v) for k, v in _busy.items()}, dict(_last)
        queued = set(_cfg["queued"]()) if _cfg else set()
        pinned = pins()
        now = time.time()
        rows = []
        for mid, m in ms.items():
            per = {}
            for node in _cfg.get("nodes", {}):
                nd = nodes.get(node) or {}
                s = (nd.get("members") or {}).get(mid) or {}
                rk = rowkey(mid, node)
                if rk in busy:
                    st = busy[rk]["op"]
                elif rk in queued:
                    st = "queued"
                elif s.get("running"):
                    st = "ready" if s.get("ready") else "loading"
                elif "members" not in nd:
                    st = "unknown"
                else:
                    st = "off"
                host = nd.get("fabric_ip")
                per[node] = {"state": st, "port": m.get("port"), "url": f"http://{host}:{m.get('port')}" if host and s.get("running") else None, "pinned": rk in pinned,
                             "mem_mb": s.get("mem_mb"), "since": s.get("since"), "fit": fit_from(nd, m),
                             "error": _cfg["get_error"](rk), "last": last.get(rk), "busy_s": round(now - busy[rk]["t0"]) if rk in busy else None}
            rows.append({"id": mid, **{k: m.get(k) for k in ("label", "engine", "family", "precision", "params_b", "languages", "need_mb", "api", "endpoint", "note", "port", "max_audio")},
                         "per_node": per})
        return {"nodes": {n: {**{k: v.get(k) for k in ("avail_mb", "free_mb", "cached_mb", "reserved_mb", "swap_used_pct", "fabric_ip", "error")},
                              "age_s": round(now - v.get("t", now))} for n, v in nodes.items()},
                "members": rows}
    except Exception as e:                        # the state endpoint must survive anything in here
        return {"nodes": {}, "members": [], "error": str(e)[:200]}


def load(mid, node, key):
    """Queue a load. Raises HTTPError(403) without the Starfleet key (key_check), PermissionError if it does not fit or is already busy,
    ValueError for an unknown member or node. The load itself waits behind any LLM load, like every light model."""
    m = members().get(mid)
    if not m or node not in _cfg["nodes"]:
        raise ValueError(f"unknown speech member {mid!r} or node {node!r}")
    _cfg["key_check"](key)
    with _lock:
        nd = dict(_node.get(node) or {})
        if rowkey(mid, node) in _busy:
            raise PermissionError(f"{mid} is already {_busy[rowkey(mid, node)]['op']} on {node}")
    f = fit_from(nd, m)
    if not f["ok"]:
        raise PermissionError(f"{m.get('label', mid)} does not fit on {node}: {f['reason']}. Unload something there first.")
    rk = rowkey(mid, node)
    _cfg["clear_error"](rk)

    def job(_model, _key):                        # runs on the load-queue worker; a raised error lands in STATE["errors"][rk]
        with _lock:
            _busy[rk] = {"op": "loading", "t0": time.time()}
        try:
            j = ctl(node, "start", mid, timeout=int((m.get("ready") or {}).get("timeout_s", 300)) + 180)
            with _lock:
                _last[rk] = {"op": "load", "t": time.time(), "ok": bool(j.get("ok")), "ready_s": j.get("ready_s")}
            if not j.get("ok"):
                raise RuntimeError(j.get("error") or j.get("reason") or "speech-ctl start failed")
        finally:
            with _lock:
                _busy.pop(rk, None)
            refresh(node)

    return {"ok": True, "queued": _cfg["enqueue"](rk, key, job) or 0}


def unload(mid, node, key):
    """Stop one member on one node and prove its memory came back. Returns speech-ctl's JSON (freed_mb, cache_dropped_mb, leftovers)."""
    if mid not in members() or node not in _cfg["nodes"]:
        raise ValueError(f"unknown speech member {mid!r} or node {node!r}")
    _cfg["key_check"](key)
    rk = rowkey(mid, node)
    p = pins()
    if rk in p:                                   # Unload unpins too, else it would be a pin that is not running
        p.discard(rk)
        _save_pins(p)
    if _cfg["dequeue"](rk):
        return {"ok": True, "dequeued": True}
    with _lock:
        if rk in _busy and _busy[rk]["op"] == "unloading":
            raise PermissionError(f"{mid} is already unloading on {node}")
        _busy[rk] = {"op": "unloading", "t0": time.time()}
    try:
        j = ctl(node, "stop", mid, timeout=200)
    finally:
        with _lock:
            _busy.pop(rk, None)
    with _lock:
        _last[rk] = {"op": "unload", "t": time.time(), **{k: j.get(k) for k in ("ok", "freed_mb", "free_gain_mb", "cache_dropped_mb", "leftovers_alive")}}
    _cfg["clear_error"](rk)
    refresh(node)
    return j


def stop_all():
    """For Red Alert: stop every speech server that is running, on every node. Returns {row key: speech-ctl answer}."""
    res = {}
    _save_pins(set())                             # Red Alert clears every pin, like the LLM pins
    for node in _cfg.get("nodes", {}):
        st = ctl(node, "status", timeout=40)
        for mid, s in (st.get("members") or {}).items():
            if s.get("running"):
                res[rowkey(mid, node)] = ctl(node, "stop", mid, timeout=200)
        refresh(node)
    return res


# ---------------------------------------------------------------- self-check (pure logic, no node contacted)
def selfcheck():
    global REGISTRY
    import tempfile
    reg = {"members": {"a": {"label": "A", "engine": "native", "need_mb": 3000, "port": 1, "ready": {"timeout_s": 5}},
                       "b": {"label": "B", "engine": "docker", "need_mb": 50000, "port": 2}}}
    d = tempfile.mkdtemp()
    json.dump(reg, open(f"{d}/r.json", "w"))
    REGISTRY = f"{d}/r.json"
    errs, queued = {"b-kathryn": "boom"}, []
    init({"jean-luc": None, "kathryn": "10.100.0.1"}, key_check=lambda k: None if k == "k" else (_ for _ in ()).throw(PermissionError("403")),
         enqueue=lambda rk, key, job: queued.append((rk, job)) or len(queued), dequeue=lambda rk: rk in [q[0] for q in queued],
         queued=lambda: [q[0] for q in queued], get_error=errs.get, clear_error=lambda rk: errs.pop(rk, None))
    nd_ok = {"avail_mb": 19000, "reserved_mb": 0, "swap_used_pct": 40, "fabric_ip": "10.100.0.1", "t": time.time(), "members": {"a": {"running": True, "ready": True, "mem_mb": 2500}, "b": {"running": False}}}
    nd_tight = {"avail_mb": 11000, "reserved_mb": 0, "swap_used_pct": 90, "fabric_ip": "10.100.0.2", "t": time.time(), "members": {"a": {"running": False}, "b": {"running": False}}}
    _node.update(kathryn=nd_ok, **{"jean-luc": nd_tight})
    assert fit_from(nd_ok, reg["members"]["a"])["ok"] and not fit_from(nd_ok, reg["members"]["b"])["ok"], "19 GB: the 3 GB member fits, the 50 GB one does not"
    assert not fit_from(nd_tight, {"need_mb": 6000})["ok"] and fit_from(nd_tight, {"need_mb": 4800})["ok"], "swap over 80 % raises the reserve to 6 GB: 11000 - 6144 leaves 4856 MB"
    assert not fit_from({"error": "ssh down"}, reg["members"]["a"])["ok"], "an unprobed node never fits"
    s = state()
    row = {r["id"]: r for r in s["members"]}
    assert row["a"]["per_node"]["kathryn"]["state"] == "ready" and row["a"]["per_node"]["kathryn"]["url"] == "http://10.100.0.1:1"
    assert row["a"]["per_node"]["jean-luc"]["state"] == "off" and row["b"]["per_node"]["kathryn"]["error"] == "boom"
    try:
        load("a", "kathryn", "wrong")
        raise SystemExit("load without the key must raise")
    except PermissionError:
        pass
    try:
        load("b", "kathryn", "k")
        raise SystemExit("a 50 GB member must be refused with a reason")
    except PermissionError as e:
        assert "does not fit" in str(e)
    assert load("a", "kathryn", "k") == {"ok": True, "queued": 1} and queued[0][0] == "a-kathryn", "a fitting load is queued under its row key"
    assert state()["members"][0]["per_node"]["kathryn"]["state"] == "queued"
    assert unload("a", "kathryn", "k") == {"ok": True, "dequeued": True}, "unloading a queued member just dequeues it"
    try:
        load("zzz", "kathryn", "k")
        raise SystemExit("unknown member must raise")
    except ValueError:
        pass
    print("ok - speech.py: fit formula, row states, keyed load queue, dequeue, unknown ids")


if __name__ == "__main__":
    selfcheck()
