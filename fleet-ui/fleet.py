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
    'echo "DOCKER:$(docker ps --format "{{.Names}}" 2>/dev/null | paste -sd, -)"'
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
    try:
        swap_get(f"/upstream/{q}/health", timeout=LOAD_TIMEOUT)
        with LOCK:
            STATE["errors"].pop(model, None)
        return True
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:400] or f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        msg = str(e)[:400]
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
        if self.path == "/api/state":
            with LOCK:
                snap = json.loads(json.dumps(STATE))
            snap["pins"] = load_pins()
            snap["models"] = [
                {
                    "id": m,
                    "nodes": node_of(m),
                    "tps": tps_of(m),
                    "pinned": m in snap["pins"],
                    "error": snap["errors"].get(m),
                }
                for m in snap["models"]
            ]
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
