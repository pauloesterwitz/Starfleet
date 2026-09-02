#!/usr/bin/env python3
"""Fleet — one page for llama-swap on the Starfleet cluster.

Pin models so llama-swap keeps them resident, and watch RAM/GPU/load on
every node (Jean-Luc + Kathryn).

llama-swap already owns model lifecycle, so this does not reimplement it:
  GET /v1/models              -> the catalogue
  GET /running                -> what is resident right now
  GET /upstream/<m>/health    -> loads <m> (no inference) and resets its ttl
  GET /unload?model=<m>       -> evicts <m>
"Persistently loaded" = we re-hit that health probe before the ttl expires.

Node resources come from one probe run locally and over ssh, not from
llama-swap's /metrics, so both nodes report identically. On GB10 the GPU shares
the system pool, so MemAvailable IS the VRAM number — nvidia-smi reports N/A.

stdlib only.  Run:  python3 fleet.py
"""

import argparse
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SWAP = os.environ.get("FLEET_SWAP", "http://127.0.0.1:28080")

# None = run the probe locally.  Anything else is an ssh host.
# Use the *.fritz.box name, never the bare tailnet name: the latter hits
# Tailscale SSH, which wants a browser and breaks BatchMode.
NODES = {"jean-luc": None, "kathryn": "kathryn.fritz.box"}

PIN_FILE = os.path.expanduser("~/.config/fleet-ui/pins.json")
LOAD_TIMES_FILE = os.path.expanduser("~/.config/fleet-ui/load-times.json")
LOAD_HISTORY = 5     # cold-load durations kept per model, for the ETA
MIN_COLD_LOAD = 5.0  # seconds; below this a "cold" touch was really a ttl refresh

# Per-model context override. This is the same file ctx-env.sh reads at launch
# (and ctxproxy.py writes for a sized `model@32768` request) -- see
# llama-swap/ctxproxy.py. Writing it here is the documented contract; ctxproxy
# is a pure request relay with no control endpoint to call instead.
CTX_DIR = os.path.join(os.environ.get("GB10_STATE_DIR") or os.path.expanduser("~/.gb10"), "ctx")
SWAP_CONFIG = os.path.expanduser("~/llama-swap/config.yaml")
MIN_CTX, MAX_CTX = 256, 1048576  # the bounds ctx-env.sh and ctxproxy both enforce
NODE_POLL = 5        # seconds between resource probes
KEEPALIVE = 240      # seconds between ttl-refresh touches of a resident pin (shortest ttl is 600)
RECHECK = 5           # seconds between checks for a pin that isn't currently running
LOAD_TIMEOUT = 2500  # a cold TP=2 load can take ~20 min; matches healthCheckTimeout

PROBE = (
    'echo "H:$(hostname)"; '
    "awk '/^MemTotal|^MemAvailable/{print $1 $2}' /proc/meminfo; "
    'echo "LOAD:$(cut -d\" \" -f1-3 /proc/loadavg)"; '
    'echo "GPU:$(nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw '
    '--format=csv,noheader,nounits 2>/dev/null | head -1)"; '
    'echo "DOCKER:$(docker ps --format "{{.Names}}" 2>/dev/null | paste -sd, -)"; '
    # Live entries only: a reservation whose owner pid is gone is stale and must
    # not be shown as held memory (memcheck.sh prunes those on its next run).
    'echo "RES:$(t=0; for f in $HOME/.gb10/reservations/*; do [ -e "$f" ] || continue; '
    'read -r pp mm _ < "$f" || continue; kill -0 "$pp" 2>/dev/null && t=$((t+mm)); done; echo $t)"'
)

STATE = {"nodes": {}, "models": [], "running": [], "pins": [], "errors": {}}
LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers

def node_of(model):
    """Which machines a model occupies. Encoded in the name suffix by config.yaml."""
    for suffix, nodes in (
        ("-starfleet", ["jean-luc", "kathryn"]),   # TP=2, spans both
        ("-fastest-node", ["dynamic"]),            # pick-node.sh decides at load time
        ("-jean-luc", ["jean-luc"]),
        ("-kathryn", ["kathryn"]),
    ):
        if model.endswith(suffix):
            return nodes
    return ["jean-luc"]  # embeds + imagegen are started by Jean-Luc's llama-swap


def tps_of(model):
    """The measured tok/s baked into the model name, e.g. '...-57tps-mtp4-...' -> 57."""
    m = re.search(r"-(\d+)tps", model)
    return int(m.group(1)) if m else None


def parse_probe(text):
    """Turn PROBE's output into a dict. Returns None if it did not run."""
    out = {"containers": []}
    for line in text.splitlines():
        if line.startswith("H:"):
            out["host"] = line[2:]
        elif line.startswith("MemTotal:"):
            out["mem_total_kb"] = int(line.split(":")[1])
        elif line.startswith("MemAvailable:"):
            out["mem_avail_kb"] = int(line.split(":")[1])
        elif line.startswith("LOAD:"):
            out["load"] = line[5:].strip()
        elif line.startswith("GPU:"):
            parts = [p.strip() for p in line[4:].split(",")]
            if len(parts) == 3:
                out["gpu_util"], out["gpu_temp"], out["gpu_power"] = parts
        elif line.startswith("DOCKER:"):
            names = line[7:].strip()
            out["containers"] = [n for n in names.split(",") if n]
        elif line.startswith("RES:"):
            try:
                out["reserved_mb"] = int(line[4:].strip() or 0)
            except ValueError:
                out["reserved_mb"] = 0
    return out if "mem_total_kb" in out else None


def swap_get(path, timeout=10):
    with urllib.request.urlopen(SWAP + path, timeout=timeout) as r:
        return r.read().decode()


def load_pins():
    try:
        with open(PIN_FILE) as f:
            return list(json.load(f))
    except (OSError, ValueError):
        return []


def save_pins(pins):
    os.makedirs(os.path.dirname(PIN_FILE), exist_ok=True)
    with open(PIN_FILE, "w") as f:
        json.dump(sorted(pins), f, indent=2)


# ------------------------------------------------- cold-load progress + ETA

# Cold loads in flight: model -> {"started": ts, "eta": secs|None}. Published
# on /api/state so the page can show a progress bar for a load that takes
# minutes (a cold TP=2 load runs ~20 min) instead of just sitting on "stopped".
_loading = {}
_loading_lock = threading.Lock()


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def load_times():
    """Observed cold-load durations per model, oldest first."""
    try:
        with open(LOAD_TIMES_FILE) as f:
            data = json.load(f)
        return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError, AttributeError):
        return {}


def record_load_time(model, secs):
    """Append one cold-load duration, keeping only the last LOAD_HISTORY."""
    hist = load_times()
    hist[model] = (hist.get(model, []) + [round(secs, 1)])[-LOAD_HISTORY:]
    os.makedirs(os.path.dirname(LOAD_TIMES_FILE), exist_ok=True)
    tmp = LOAD_TIMES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(hist, f, indent=2, sort_keys=True)
    os.replace(tmp, LOAD_TIMES_FILE)  # atomic -- never leave a half-written file


def eta_for(model, hist=None):
    """Expected cold-load seconds -- None until the model has loaded once.

    Median, not mean: one slow load that had to evict a big neighbour first
    shouldn't permanently inflate the estimate for the normal case.
    """
    return _median((load_times() if hist is None else hist).get(model, []))


# ------------------------------------------------- per-model context window

_ctx_lock = threading.Lock()


def wired_members(path=SWAP_CONFIG):
    """Members whose cmd runs through ctx-env.sh -- the only ones a context size
    can reach. Parsed the same way ctxproxy.py does it, and re-read per call so a
    member added to config.yaml works without restarting Fleet."""
    found, current = set(), None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key = re.match(r"^  ([A-Za-z0-9._:-]+):\s*$", line)
                if key:
                    current = key.group(1)
                elif current and line.startswith("    cmd:") and "ctx-env.sh" in line:
                    found.add(current)
    except OSError:
        pass
    return found


def read_ctx(member):
    """The size currently recorded for `member`, or None meaning "deployed default".

    Out-of-range or junk reads as None for the same reason ctx-env.sh ignores it:
    a bad override must never be why a model refuses to load.
    """
    try:
        with open(os.path.join(CTX_DIR, member)) as fh:
            ctx = int(fh.read().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return ctx if MIN_CTX <= ctx <= MAX_CTX else None


def read_all_ctx():
    try:
        names = os.listdir(CTX_DIR)
    except OSError:
        return {}
    return {n: c for n in names for c in [read_ctx(n)] if c is not None}


def apply_ctx(model, ctx):
    """Record a context size for `model` (None clears it, back to the deployed
    default) and evict it if it is resident, so it comes back at the new size.

    vLLM allocates its KV pool at startup, so a context change is only a cold
    reload away -- there is no live resize. A pinned model is re-touched by
    keepalive within RECHECK seconds; an unpinned one simply stays down until
    next use. Returns True if a running model was unloaded.
    """
    path = os.path.join(CTX_DIR, model)
    # One lock, mirroring ctxproxy: a write and its unload must not interleave.
    with _ctx_lock:
        if read_ctx(model) == ctx:
            return False
        if ctx is None:
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            os.makedirs(CTX_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write("%d\n" % ctx)
            os.replace(tmp, path)  # atomic -- ctx-env.sh never reads a half file
        with LOCK:
            resident = any(r.get("model") == model for r in STATE["running"])
        if not resident:
            return False
        swap_get("/unload?model=" + urllib.parse.quote(model, safe=""))
        return True


# ---------------------------------------------------------------- workers

def probe_node(name, host):
    cmd = ["bash", "-lc", PROBE] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, PROBE
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return parse_probe(r.stdout) or {"error": (r.stderr or "probe failed").strip()[:200]}
    except (subprocess.SubprocessError, OSError) as e:
        return {"error": str(e)[:200]}


def poll_nodes():
    while True:
        for name, host in NODES.items():
            info = probe_node(name, host)
            with LOCK:
                STATE["nodes"][name] = info
        time.sleep(NODE_POLL)


def poll_swap():
    while True:
        try:
            models = json.loads(swap_get("/v1/models"))["data"]
            running = json.loads(swap_get("/running"))["running"]
            with LOCK:
                STATE["models"] = [m["id"] for m in models]
                STATE["running"] = running
                STATE["errors"].pop("_swap", None)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            with LOCK:
                STATE["errors"]["_swap"] = f"llama-swap unreachable: {e}"
        time.sleep(3)


def touch(model):
    """Load `model` (or reset its ttl). Blocks until the backend is ready — call in a thread.

    memcheck.sh inside llama-swap refuses a load that will not fit and returns a
    clean error instead of OOMing the box; we surface that text rather than
    second-guessing the fit here.
    """
    q = urllib.parse.quote(model, safe="")
    # A touch of an already-resident model is only a ttl refresh and returns
    # near-instantly; a touch of something not currently running is a real cold
    # load. Only the latter is worth publishing as progress or timing.
    with LOCK:
        cold = not any(r.get("model") == model for r in STATE["running"])
    started = time.time()
    if cold:
        with _loading_lock:
            _loading[model] = {"started": started, "eta": eta_for(model)}
    try:
        swap_get(f"/upstream/{q}/health", timeout=LOAD_TIMEOUT)
        elapsed = time.time() - started
        # MIN_COLD_LOAD guards the history against a "cold" touch that was in
        # fact served instantly -- the model came up between poll_swap's 3s
        # snapshot and this call. Averaging those in drags every ETA to zero.
        if cold and elapsed >= MIN_COLD_LOAD:
            record_load_time(model, elapsed)
        with LOCK:
            STATE["errors"].pop(model, None)
        return True
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:400] or f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        msg = str(e)[:400]
    finally:
        if cold:
            with _loading_lock:
                _loading.pop(model, None)
    with LOCK:
        STATE["errors"][model] = msg
    return False


_reloading = set()        # pins currently being re-touched, so we don't double-fire
_reloading_lock = threading.Lock()


def _touch_async(model):
    """Run touch() in its own thread. touch() blocks for the life of a cold load
    (up to LOAD_TIMEOUT) -- without this, one stuck pin starves every other pin's
    keepalive, since the old code touched pins one at a time in a single loop."""
    with _reloading_lock:
        if model in _reloading:
            return
        _reloading.add(model)

    def run():
        try:
            touch(model)
        finally:
            with _reloading_lock:
                _reloading.discard(model)

    threading.Thread(target=run, daemon=True).start()


def _pins_needing_touch(pins, running_ids, due_for_refresh):
    """Pure decision logic (no network, no threads) so it's unit-testable in
    selfcheck: touch a pin if it's missing from `running_ids` (evicted -- by
    ttl, or by llama-swap's own group-exclusivity swapping in a different
    model -- either way it must come straight back), or if a periodic ttl
    refresh is due, in which case every pin gets refreshed regardless."""
    return [m for m in pins if due_for_refresh or m not in running_ids]


def keepalive():
    """Keep every pinned model resident. 'Persistent' has to mean more than
    outrunning the idle ttl: a pin can also be evicted at any moment by
    llama-swap's own group-exclusivity rule (e.g. STARFLEET is swap:true --
    only one member of that group can be resident, so requesting a different
    one evicts ours). We can't and shouldn't override that -- it's deliberate,
    memory-driven config -- so instead we watch STATE["running"] (refreshed
    every 3s by poll_swap) and re-touch a pin the moment it's no longer there,
    on top of the periodic ttl refresh for pins that are still up.
    """
    last_refresh = 0.0
    while True:
        pins = load_pins()
        with LOCK:
            running_ids = {r.get("model") for r in STATE["running"]}
        due_for_refresh = time.time() - last_refresh >= KEEPALIVE
        for model in _pins_needing_touch(pins, running_ids, due_for_refresh):
            _touch_async(model)
        if due_for_refresh:
            last_refresh = time.time()
        time.sleep(RECHECK)


# ---------------------------------------------------------------- jobs

PUEUE = os.path.expanduser("~/.local/bin/pueue")

# gpujob wraps every command twice: `ssh ... kathryn 'gpujob-run --need-mb N ... -- REAL'`.
# The Mac app wants REAL, so peel both layers off for display only.
_SSH_WRAP = re.compile(r"^ssh\s+.*?fritz\.box\s+'(?P<inner>.*)'\s*$", re.S)
_RUN_WRAP = re.compile(r"gpujob-run\s+(?P<opts>.*?)\s+--\s+(?P<real>.*)$", re.S)


def _unwrap(cmd):
    """-> (real_command, need_mb, idle_s). Falls back to the raw string."""
    m = _SSH_WRAP.match(cmd.strip())
    if m:
        # shlex.quote escaped every inner ' as '"'"' to survive the ssh arg.
        # Undo it or the Mac app shows that noise instead of the command.
        cmd = m.group("inner").replace("""'"'"'""", "'")
    m = _RUN_WRAP.search(cmd)
    if not m:
        return cmd.strip(), None, None
    opts = m.group("opts")

    def opt(flag):
        o = re.search(flag + r"\s+(\d+)", opts)
        return int(o.group(1)) if o else None

    return m.group("real").strip(), opt("--need-mb"), opt("--idle-s")


def _job_state(status):
    """pueue's tagged union -> one flat state string plus its timestamps.

    'planned' is the state the Mac app cares about most: stashed WITH an
    enqueue_at is a job deliberately parked until tonight, not a stuck one.
    """
    if isinstance(status, str):                       # "Queued", "Paused", ...
        return status.lower(), {}
    kind, body = next(iter(status.items()))
    body = body or {}
    if kind == "Stashed":
        at = body.get("enqueue_at")
        return ("planned" if at else "stashed"), {"starts_at": at}
    if kind == "Running":
        return "running", {"start": body.get("start"), "enqueued_at": body.get("enqueued_at")}
    if kind == "Done":
        return "done", {
            "start": body.get("start"), "end": body.get("end"),
            "result": body.get("result") if isinstance(body.get("result"), str)
            else next(iter(body.get("result", {})), None),
        }
    return kind.lower(), body


def jobs_snapshot():
    """pueue tasks + per-node capacity, in ONE flat shape for the Mac app."""
    snap = {"jobs": [], "nodes": {}, "queue": {}, "error": None}
    try:
        r = subprocess.run([PUEUE, "status", "--json"],
                           capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        snap["error"] = f"pueue unreachable: {str(e)[:200]}"
        data = {"tasks": {}, "groups": {}}

    for t in data.get("tasks", {}).values():
        state, times = _job_state(t.get("status"))
        real, need_mb, idle_s = _unwrap(t.get("command", ""))
        # NOTE: t["envs"] is deliberately dropped — it carries the submitting
        # shell's whole environment, tokens included. Never serve it.
        snap["jobs"].append({
            "id": t.get("id"), "node": t.get("group"), "label": t.get("label"),
            "state": state, "cmd": real, "need_mb": need_mb, "idle_s": idle_s,
            "created_at": t.get("created_at"), **times,
        })
    snap["jobs"].sort(key=lambda j: (j["state"] != "running", j["id"]))

    for g, gi in data.get("groups", {}).items():
        snap["queue"][g] = {"status": gi.get("status"),
                            "parallel": gi.get("parallel_tasks")}

    with LOCK:
        nodes = json.loads(json.dumps(STATE["nodes"]))
        running = json.loads(json.dumps(STATE["running"]))
    for name, info in nodes.items():
        avail = info.get("mem_avail_kb")
        snap["nodes"][name] = {
            "free_mb": avail // 1024 if avail else None,
            "reserved_mb": info.get("reserved_mb"),
            "gpu_util": info.get("gpu_util"),
            "load": info.get("load"),
            "models": [m["model"] for m in running if name in node_of(m["model"])],
            "error": info.get("error"),
            # Split on purpose: "planned" is parked until its clock, "queued" is
            # waiting for this node's slot right now. The Mac app shows them apart.
            "planned": sum(1 for j in snap["jobs"]
                           if j["node"] == name and j["state"] == "planned"),
            "queued": sum(1 for j in snap["jobs"]
                          if j["node"] == name and j["state"] == "queued"),
            "running": sum(1 for j in snap["jobs"]
                           if j["node"] == name and j["state"] == "running"),
        }
    return snap


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, page(), "text/html; charset=utf-8")
        if self.path == "/api/jobs":
            return self._send(200, json.dumps(jobs_snapshot()), "application/json")
        if self.path == "/api/state":
            with LOCK:
                snap = json.loads(json.dumps(STATE))
            snap["pins"] = load_pins()
            times = load_times()  # read once, not once per model
            ctxs, wired = read_all_ctx(), wired_members()
            snap["models"] = [
                {
                    "id": m,
                    "nodes": node_of(m),
                    "tps": tps_of(m),
                    "pinned": m in snap["pins"],
                    "error": snap["errors"].get(m),
                    "load_eta": eta_for(m, times),
                    "ctx": ctxs.get(m),
                    "ctx_capable": m in wired,
                }
                for m in snap["models"]
            ]
            now = time.time()
            with _loading_lock:
                snap["loading"] = sorted(
                    ({"model": m, "elapsed": round(now - v["started"], 1), "eta": v["eta"]}
                     for m, v in _loading.items()),
                    key=lambda d: d["model"],
                )
            return self._send(200, json.dumps(snap), "application/json")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except ValueError:
            return self._send(400, '{"error":"bad json"}', "application/json")
        model = body.get("model")
        known = STATE["models"]
        if not model or (known and model not in known):
            return self._send(400, '{"error":"unknown model"}', "application/json")

        if self.path == "/api/pin":
            pins = set(load_pins())
            if body.get("pin"):
                pins.add(model)
                save_pins(pins)
                threading.Thread(target=touch, args=(model,), daemon=True).start()
            else:
                pins.discard(model)
                save_pins(pins)
            return self._send(200, '{"ok":true}', "application/json")

        if self.path == "/api/ctx":
            raw = body.get("ctx")
            if raw in (None, "", 0):
                ctx = None                      # clear -> serve the deployed default
            else:
                try:
                    ctx = int(raw)
                except (TypeError, ValueError):
                    return self._send(400, '{"error":"context size must be an integer"}',
                                      "application/json")
                if not MIN_CTX <= ctx <= MAX_CTX:
                    return self._send(400, json.dumps(
                        {"error": f"context size {ctx} is outside {MIN_CTX}..{MAX_CTX}"}),
                        "application/json")
                if model not in wired_members():
                    return self._send(400, json.dumps(
                        {"error": f"{model} has no dynamic context size -- its cmd in "
                                  f"config.yaml is not wired through ctx-env.sh"}),
                        "application/json")
            try:
                reloaded = apply_ctx(model, ctx)
            except OSError as e:
                return self._send(500, json.dumps({"error": str(e)[:200]}), "application/json")
            return self._send(200, json.dumps({"ok": True, "reloaded": reloaded}),
                              "application/json")

        if self.path == "/api/unload":
            pins = set(load_pins())
            pins.discard(model)          # unpin too, else keepalive reloads it
            save_pins(pins)
            try:
                swap_get("/unload?model=" + urllib.parse.quote(model, safe=""))
            except (urllib.error.URLError, OSError) as e:
                return self._send(502, json.dumps({"error": str(e)[:200]}), "application/json")
            return self._send(200, '{"ok":true}', "application/json")

        self._send(404, '{"error":"not found"}', "application/json")


# ---------------------------------------------------------------- page

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet.html")


def page():
    """Read the page off disk each request so design edits need only a browser refresh."""
    with open(PAGE_FILE, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- entry

def selfcheck():
    # A pin missing from `running` (evicted by ttl, or by llama-swap swapping in a
    # different member of its swap:true group) must be touched immediately, same
    # tick, regardless of the refresh clock -- that's the whole fix.
    assert _pins_needing_touch(["a", "b"], {"b"}, due_for_refresh=False) == ["a"]
    assert _pins_needing_touch(["a", "b"], {"a", "b"}, due_for_refresh=False) == []
    assert _pins_needing_touch(["a", "b"], {"a", "b"}, due_for_refresh=True) == ["a", "b"]
    assert _pins_needing_touch([], {"a"}, due_for_refresh=True) == []

    # ETA maths: median (not mean) of recorded cold loads, and None while a
    # model has no history -- the page shows an indeterminate bar for that.
    assert _median([]) is None
    assert _median([10]) == 10
    assert _median([30, 10, 20]) == 20
    assert eta_for("never-loaded", {}) is None
    assert eta_for("m", {"m": [100, 10, 12]}) == 12

    # Context overrides: bounds match ctx-env.sh/ctxproxy, junk and unknown
    # members read as "no override" rather than as a bogus size.
    assert (MIN_CTX, MAX_CTX) == (256, 1048576)
    assert read_ctx("definitely-not-a-member") is None
    if os.path.exists(SWAP_CONFIG):
        assert wired_members(), f"no ctx-env.sh-wired members found in {SWAP_CONFIG}"

    # _touch_async must actually dedup in flight, not just in theory: fire it
    # twice for the same model while the first call is still "loading" and
    # confirm the second is dropped rather than spawning a second thread.
    global touch
    real_touch = touch
    calls = []
    started = threading.Event()
    release = threading.Event()

    def fake_touch(model):
        calls.append(model)
        started.set()
        release.wait(timeout=2)

    touch = fake_touch
    try:
        _touch_async("x")
        assert started.wait(timeout=2), "fake_touch never ran"
        _touch_async("x")  # "x" is already reloading -- must be a no-op
        time.sleep(0.05)
        assert calls == ["x"], f"expected exactly one in-flight touch, got {calls}"
    finally:
        release.set()
        time.sleep(0.05)
        touch = real_touch
        with _reloading_lock:
            _reloading.clear()

    assert node_of("qwen3.5-122b-mtp3-starfleet") == ["jean-luc", "kathryn"]
    assert node_of("qwen3.6-35b-57tps-mtp4-kathryn") == ["kathryn"]
    assert node_of("qwen3.8-27b-14tps-jean-luc") == ["jean-luc"]
    assert node_of("gemma4-26b-46tps-fastest-node") == ["dynamic"]
    assert node_of("nomic-embed-text") == ["jean-luc"]
    assert tps_of("qwen3.6-35b-57tps-mtp4-jean-luc") == 57
    assert tps_of("nomic-embed-text") is None

    p = parse_probe("H:Kathryn\nMemTotal:127600816\nMemAvailable:30610188\n"
                    "LOAD:1.54 0.69 0.29\nGPU:1, 45, 11.58\nDOCKER:a,b\n")
    assert p["host"] == "Kathryn" and p["mem_avail_kb"] == 30610188
    assert p["gpu_util"] == "1" and p["gpu_temp"] == "45" and p["gpu_power"] == "11.58"
    assert p["containers"] == ["a", "b"]
    assert parse_probe("H:x\nDOCKER:\n") is None          # no meminfo -> unusable
    assert parse_probe("H:x\nMemTotal:1\nMemAvailable:1\nDOCKER:\n")["containers"] == []

    models = json.loads(swap_get("/v1/models"))["data"]
    json.loads(swap_get("/running"))
    assert "id=\"rows\"" in page(), "fleet.html missing the model table"
    print(f"selfcheck ok — llama-swap reachable, {len(models)} models, page {len(page())} bytes")


def default_host():
    # ponytail: bind the tailnet address when there is one, so the page is reachable
    # from Paul's Mac but not from anyone else on the LAN. Falls back to loopback
    # (then use: ssh -L 8090:127.0.0.1:8090 jean-luc.fritz.box).
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return out.splitlines()[0] if out else "127.0.0.1"
    except (subprocess.SubprocessError, OSError):
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=None, help="bind address (default: tailnet IP)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()

    host = a.host or default_host()
    for fn in (poll_nodes, poll_swap, keepalive):
        threading.Thread(target=fn, daemon=True).start()
    pins = load_pins()
    print(f"Fleet on http://{host}:{a.port}   (llama-swap {SWAP})", flush=True)
    print(f"pinned: {', '.join(pins) if pins else 'none'}", flush=True)
    ThreadingHTTPServer((host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
