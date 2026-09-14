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
MODELS_DIR = os.path.expanduser("~/models")
LS_DIR = os.path.expanduser("~/llama-swap")
CTX_MAX_FILE = os.path.expanduser("~/.config/fleet-ui/ctx-max.json")
CTX_POOL_FILE = os.path.expanduser("~/.config/fleet-ui/ctx-pool.json")
LS_LOGS = os.path.expanduser("~/llama-swap/logs")
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


# ---------------------------------------------------- per-model MAX context

# member -> native max context (from the checkpoint's own config.json), or None
# when it could not be resolved. Cached: resolving means running a launcher in
# DRYRUN mode and reading a file, far too costly for the 3s /api/state poll.
_ctx_max = {}          # member -> usable max = min(checkpoint max, measured KV pool)
_ctx_native = {}       # member -> the checkpoint's own max_position_embeddings
_ctx_sub = {}          # member -> model subdirectory, for locating its logs
_ctx_pool = {}         # member -> {"pool": tokens the KV pool holds, "at_ctx": context measured at}
_ctx_vision = {}       # member -> "on" | "off" | None (no vision tower / not resolvable)
_ctx_deployed = {}     # member -> context its launcher starts it with when no override is set
_ctx_max_lock = threading.Lock()


def _pool_from_vllm_log(path):
    """(pool_tokens, context) from a vLLM run log; last occurrence wins.

    vLLM prints 'GPU KV cache size: 467,200 tokens' and 'Maximum concurrency for
    262,144 tokens' -- the latter is the context it was started with.
    """
    pool = ctx = None
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                m = re.search(r"GPU KV cache size:\s*([\d,]+)\s*tokens", line)
                if m:
                    pool = int(m.group(1).replace(",", ""))
                m = re.search(r"Maximum concurrency for\s*([\d,]+)\s*tokens", line)
                if m:
                    ctx = int(m.group(1).replace(",", ""))
    except OSError:
        return None, None
    return pool, ctx


def _pool_from_sglang_text(text):
    """(pool_tokens, context) from SGLang's summary line; last occurrence wins."""
    pool = ctx = None
    for m in re.finditer(r"max_total_num_tokens=(\d+).*?context_len=(\d+)", text):
        pool, ctx = int(m.group(1)), int(m.group(2))
    return pool, ctx


def _sglang_container_text():
    """SGLang's numbers exist only in its container log -- the wrapper log under
    ~/llama-swap/logs holds launcher output only. Read it while the container is
    still there; the observation is cached so it outlives the container."""
    try:
        r = subprocess.run(["docker", "logs", "sglang-tp2-head"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "") + (r.stderr or "")
    except (subprocess.SubprocessError, OSError):
        return ""


def load_pool_cache():
    try:
        with open(CTX_POOL_FILE) as fh:
            return {k: v for k, v in json.load(fh).items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_pool_cache():
    try:
        os.makedirs(os.path.dirname(CTX_POOL_FILE), exist_ok=True)
        tmp = CTX_POOL_FILE + ".tmp"
        with _ctx_max_lock:
            snap = dict(_ctx_pool)
        with open(tmp, "w") as fh:
            json.dump(snap, fh, indent=2, sort_keys=True)
        os.replace(tmp, CTX_POOL_FILE)
    except OSError:
        pass


def observe_pools():
    """Record how many tokens each model's KV pool actually holds.

    A model can be started with a context LARGER than its pool: Qwen3.8-Flash-Next
    at 262144 came up with a 253952-token pool, so the top ~8k of the advertised
    window had no KV behind it. Measuring this is what lets `Max` mean "the largest
    context this model can really serve" rather than merely what the checkpoint allows.

    Engines report it differently: vLLM into ~/llama-swap/logs/run-<sub>.log,
    SGLang only into its container's log.
    """
    with _ctx_max_lock:
        subs = dict(_ctx_sub)
    if not subs:
        return
    sgl_text = _sglang_container_text()
    sgl_pool, sgl_ctx = _pool_from_sglang_text(sgl_text) if sgl_text else (None, None)
    # SGLang echoes its args as server_args=ServerArgs(model_path='/models/X', ...),
    # NOT as the --model-path form the docker cmd uses. Match both.
    m = re.search(r"model[_-]path[=\s]+['\"]?/models/([A-Za-z0-9._-]+)", sgl_text or "")
    sgl_sub = m.group(1) if m else None

    changed = False
    for member, sub_ in subs.items():
        pool = ctx = None
        if sgl_sub and sub_ == sgl_sub and sgl_pool:
            pool, ctx = sgl_pool, sgl_ctx
        else:
            for name in (f"run-{sub_}.log", f"sglang-{sub_}.log"):
                pth = os.path.join(LS_LOGS, name)
                if os.path.exists(pth):
                    p2, c2 = _pool_from_vllm_log(pth)
                    if p2:
                        pool, ctx = p2, c2
                        break
        if not pool:
            continue
        with _ctx_max_lock:
            prev = _ctx_pool.get(member) or {}
            if prev.get("pool") == pool and prev.get("at_ctx") == ctx:
                continue
            _ctx_pool[member] = {"pool": pool, "at_ctx": ctx}
            _ctx_max[member] = _usable_max(_ctx_native.get(member), pool,
                                           _ctx_deployed.get(member))
        changed = True
    if changed:
        save_pool_cache()


def _launcher_and_key(cmd):
    """(abs launcher path, key) from a member's llama-swap cmd, or (None, None).

    The key is the first bare token after the launcher; members whose launcher
    takes no key (serve-nemcascade.sh, ds4) yield an empty key, which is fine --
    the launcher still reports its model in DRYRUN.
    """
    parts = cmd.split()
    for i, tok in enumerate(parts):
        # "ds4-" covers ds4-tp2-proxy-guard.sh, which is the member's real entry point
        # even though it is not named serve-*. The other shims in a cmd chain
        # (ctx-env.sh, render-guard.sh, reclaim-dflash.sh, memcheck.sh) must NOT match.
        if tok.endswith(".sh") and os.path.basename(tok).startswith(
                ("serve-", "run-", "pick-", "ds4-")):
            launcher, key = tok, ""
            if i + 1 < len(parts) and not parts[i + 1].startswith("-"):
                key = parts[i + 1]
            return launcher, key
    return None, None


def _supports_dryrun(launcher):
    """Whether a launcher implements DRYRUN, i.e. whether probing it is READ-ONLY.

    serve-kathryn.sh, serve-kathryn-nemcascade.sh and pick-node.sh do NOT: running
    them ignores DRYRUN and actually launches, opening an ssh tunnel and starting a
    model on the other node. Observed doing exactly that on 2026-09-02 -- a probe
    must never start a server. Those members get their max from their Jean-Luc twin
    via the launcher-key fallback below, so nothing is lost by skipping them.
    """
    try:
        with open(launcher, errors="replace") as fh:
            return "DRYRUN" in fh.read()
    except OSError:
        return False


def _model_subdir(launcher, key):
    """Ask the launcher itself, via its own DRYRUN path, which checkpoint it serves.

    DRYRUN beats re-parsing each script's case block: they do not write it the same
    way (serve-vllm-dflash.sh has no SUB= at all, it prints a whole docker run) and
    a key's checkpoint can change without this needing to know.
    """
    if not _supports_dryrun(launcher):
        return None, ""
    try:
        r = subprocess.run([launcher] + ([key] if key else []) + ["--port", "1"],
                           capture_output=True, text=True, timeout=20,
                           env=dict(os.environ, DRYRUN="1"))
    except (subprocess.SubprocessError, OSError):
        return None, ""
    out = (r.stdout or "") + (r.returncode and (r.stderr or "") or "")
    m = re.search(r"\bsub=([A-Za-z0-9._-]+)", out)          # serve-sglang / serve-starfleet
    if not m:
        m = re.search(r"/models/([A-Za-z0-9._-]+)", out)     # serve-vllm-dflash's docker run
    return (m.group(1) if m else None), out


def _has_vision_tower(subdir):
    try:
        with open(os.path.join(MODELS_DIR, subdir, "config.json")) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return False
    return "vision_config" in cfg or "vision_config" in (cfg.get("text_config") or {})


def _vision_from(argv, has_tower):
    """'on' | 'off' | None -- does this member actually SERVE images?

    A different question from "does the checkpoint have a vision tower". Most of this
    fleet carries one and is deliberately started text-only, so the tower alone would
    be a misleading flag.

      --limit-mm-per-prompt {"image":0}   -> off (deliberately disabled)
      --limit-mm-per-prompt {"image":N>0} -> on
      no such flag, tower present         -> on: vLLM defaults an unspecified modality
                                             to 999 (config/multimodal.py), and SGLang's
                                             enable_multimodal=None means auto-detect
      no tower                            -> None, nothing to report

    serve-vllm-dflash.sh prints its argv through printf %q, so the JSON arrives escaped
    as \\{\\"image\\":0\\}; flatten it before matching or every dflash member reads as
    "no flag" when it in fact has one.
    """
    if not has_tower:
        return None
    flat = (argv or "").replace("\\", "")
    m = re.search(r'limit[-_]mm[-_]per[-_]prompt\s*\S*?"?image"?\s*:\s*(\d+)', flat)
    if m:
        return "on" if int(m.group(1)) > 0 else "off"
    if re.search(r"language[-_]model[-_]only", flat):
        return "off"
    return "on"


def _deployed_ctx(argv):
    """The context a launcher starts its model with when no override is set, from its
    own DRYRUN output: `ctx=N` (serve-sglang, serve-starfleet, serve-nemcascade) or
    `--max-model-len N` (serve-vllm-dflash's docker run, the ds4 cluster plan).
    None when the launcher does not report one."""
    flat = (argv or "").replace("\\", "").replace('"', "")
    m = re.search(r"\bctx=(\d+)", flat) or re.search(r"--max-model-len[=\s]+(\d+)", flat)
    return int(m.group(1)) if m else None


def _usable_max(native, pool, deployed):
    """The largest context worth offering as "Max".

    min(checkpoint window, measured KV pool) once the pool is known -- vLLM refuses at
    startup any max_model_len its pool cannot hold, so the pool is the real ceiling.
    Until then the launcher's configured context stands in for the pool, because the
    checkpoint's own number can be far beyond what fits: DeepSeek-V4-Flash declares
    1048576, and run-ds4-tp2-cluster.sh ships 32768 precisely because "a 1M KV cache
    OOMs instantly". The model's first load measures its pool and lifts this cap.
    """
    if not native:
        return pool            # no checkpoint number: only a measurement can say
    if pool:
        return min(native, pool)
    if deployed:
        return min(native, deployed)
    return native


def _native_max(subdir):
    """max_position_embeddings from the checkpoint's config.json.

    text_config wins for the multimodal checkpoints: their vision tower carries a
    smaller one (gemma-4-31B: text 262144, vision 131072) and the text window is
    what a context request actually sets.
    """
    try:
        with open(os.path.join(MODELS_DIR, subdir, "config.json")) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    for holder in (cfg.get("text_config") or {}, cfg):
        v = holder.get("max_position_embeddings")
        if isinstance(v, int) and MIN_CTX <= v <= MAX_CTX:
            return v
    return None


def load_ctx_max_cache():
    try:
        with open(CTX_MAX_FILE) as fh:
            return {k: v for k, v in json.load(fh).items() if isinstance(v, int)}
    except (OSError, ValueError, AttributeError):
        return {}


def member_cmds(path=SWAP_CONFIG):
    """member -> its llama-swap cmd line, from config.yaml.

    Not from /running: that only carries a cmd for models that are LOADED, and the
    max context matters most for one that is not.
    """
    cmds, current = {}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key = re.match(r"^  ([A-Za-z0-9._:-]+):\s*$", line)
                if key:
                    current = key.group(1)
                elif current and line.strip().startswith("cmd:"):
                    cmds[current] = line.strip()[4:].strip()
                    current = None
    except OSError:
        pass
    return cmds


def resolve_ctx_max():
    """Fill _ctx_max for every member. Runs off-thread; never blocks a request."""
    cmds = member_cmds()
    if not cmds:
        return
    changed = False
    by_key = {}          # launcher key -> (native max, subdir, vision), for the delegates
    pending = {}
    for model, cmd in cmds.items():
        with _ctx_max_lock:
            if _ctx_sub.get(model) and _ctx_native.get(model):
                continue
        launcher, key = _launcher_and_key(cmd)
        val = sub_ = vis = dep = None
        if launcher:
            # One probe, every answer: the same DRYRUN argv names the checkpoint, the
            # context the member is started with, and its multimodal limit.
            sub_, argv = _model_subdir(launcher, key)
            dep = _deployed_ctx(argv)
            if sub_:
                val = _native_max(sub_)
                vis = _vision_from(argv, _has_vision_tower(sub_))
        if val and key:
            by_key[key] = (val, sub_, vis, dep)
        pending[model] = (key, val, sub_, vis, dep)

    # serve-kathryn.sh and pick-node.sh only delegate -- to the other node, or to
    # whichever node is free -- so their DRYRUN names no checkpoint locally and they
    # resolve to None. They serve the SAME launcher key as their Jean-Luc twin
    # (gemma4-26b-ct, qwen3.6-35b-mtp4, ...), and the key IS the model identity, so
    # borrow that twin's answer rather than leaving half the roster without a Max.
    for model, (key, val, sub_, vis, dep) in pending.items():
        if not val and key and key in by_key:
            val, sub_, vis, dep = by_key[key]
        with _ctx_max_lock:
            _ctx_native[model] = val
            _ctx_vision[model] = vis
            _ctx_deployed[model] = dep
            if sub_:
                _ctx_sub[model] = sub_
            pool = (_ctx_pool.get(model) or {}).get("pool")
            _ctx_max[model] = _usable_max(val, pool, dep)
        if val:
            changed = True
    if changed:
        try:
            os.makedirs(os.path.dirname(CTX_MAX_FILE), exist_ok=True)
            tmp = CTX_MAX_FILE + ".tmp"
            with _ctx_max_lock:
                snap = {k: v for k, v in _ctx_max.items() if v}
            with open(tmp, "w") as fh:
                json.dump(snap, fh, indent=2, sort_keys=True)
            os.replace(tmp, CTX_MAX_FILE)
        except OSError:
            pass


def ctx_max_worker():
    """Resolve lazily: checkpoints do not change under a running fleet, but the KV
    pool is re-measured every pass so a model that has just loaded at a new context
    updates its usable max on its own."""
    with _ctx_max_lock:
        _ctx_native.update(load_ctx_max_cache())
        _ctx_pool.update(load_pool_cache())
        # _ctx_max deliberately NOT seeded from the cache: until the first resolve has
        # read each launcher's configured context, that would briefly offer the raw
        # checkpoint window as Max -- 1048576 on ds4.
    while True:
        try:
            resolve_ctx_max()
            resolve_effort_options()
            observe_pools()
        except Exception:                       # never take the server down for this
            pass
        time.sleep(120)


# ------------------------------------------------ per-model thinking effort

# The chat template is the contract: whatever variable it branches on is what a
# caller -- or a load-time default -- can actually set. Anything else is guesswork,
# and Qwen3.8's template raises outright on an unsupported reasoning_effort.
EFFORT_DIR = os.path.join(os.environ.get("GB10_STATE_DIR") or os.path.expanduser("~/.gb10"), "effort")
_effort_opts = {}      # member -> [{"label": str, "kwargs": dict|None}]


def _chat_template(subdir):
    t = ""
    d = os.path.join(MODELS_DIR, subdir)
    for name in ("chat_template.jinja", "chat_template.json", "tokenizer_config.json"):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            continue
        try:
            raw = open(p, errors="replace").read()
        except OSError:
            continue
        if name.endswith(".json"):
            try:
                v = json.loads(raw).get("chat_template")
                raw = v if isinstance(v, str) else (json.dumps(v) if v else "")
            except ValueError:
                raw = ""
        t += raw or ""
    return t


def effort_options_for(subdir):
    """Options this checkpoint actually accepts. [] when it exposes no control -- the
    UI then shows nothing to pick, rather than a setting the model would reject."""
    return _opts_from_text(_chat_template(subdir))


def _opts_from_text(t):
    """Split out from effort_options_for so selfcheck can exercise the parsing without
    a checkpoint on disk."""
    if not t:
        return []
    opts = [{"label": "Default", "kwargs": None}]

    # Graded reasoning_effort. Take the literal set the template validates against so
    # we can never offer a value it would refuse (Qwen3.8: xhigh|medium|low; GLM: low|high).
    vals = []
    for grp in re.findall(r"reasoning_effort\s*(?:not\s+)?in\s*[\(\[]([^\)\]]+)", t):
        vals += re.findall(r"['\"]([a-z]+)['\"]", grp)
    if vals:
        for v in sorted(set(vals)):
            opts.append({"label": v, "kwargs": {"reasoning_effort": v}})
        return opts

    if re.search(r"low_effort", t):                       # Nemotron-3-Super: binary
        opts.append({"label": "low", "kwargs": {"low_effort": True}})
        return opts

    if re.search(r"[\{\(\s\.]enable_thinking\b", t):
        opts.append({"label": "thinking on", "kwargs": {"enable_thinking": True}})
        opts.append({"label": "thinking off", "kwargs": {"enable_thinking": False}})
        if re.search(r"reasoning_budget|thinking_budget", t):   # Nemotron-Cascade
            key = "reasoning_budget" if "reasoning_budget" in t else "thinking_budget"
            for n in (256, 1024, 4096):
                opts.append({"label": f"budget {n}", "kwargs": {"enable_thinking": True, key: n}})
        return opts
    return []


def resolve_effort_options():
    with _ctx_max_lock:
        subs = dict(_ctx_sub)
    for member, sub_ in subs.items():
        if member in _effort_opts:
            continue
        try:
            _effort_opts[member] = effort_options_for(sub_)
        except Exception:
            _effort_opts[member] = []


def read_effort(member):
    """The label currently set, or None. Matched back from the stored kwargs so the
    UI shows the same option the user picked."""
    try:
        with open(os.path.join(EFFORT_DIR, member)) as fh:
            raw = fh.read().strip()
        cur = json.loads(raw)
    except (OSError, ValueError):
        return None
    for o in _effort_opts.get(member) or []:
        if o["kwargs"] == cur:
            return o["label"]
    return raw[:40]        # set by hand / no longer offered -- show it rather than lie


def apply_effort(model, label):
    """Record the chat-template kwargs for `label` and evict the model if resident, so
    it comes back with the new default. Returns True if a running model was unloaded.

    Written COMPACT: ctx-env.sh appends the JSON to MODEL_FLAGS_EXTRA, which the
    launchers word-split, so a single space would split it into two arguments.
    """
    opts = _effort_opts.get(model) or []
    match = next((o for o in opts if o["label"] == label), None) if label else \
        {"label": "Default", "kwargs": None}
    if match is None:
        raise ValueError(f"{model} does not accept effort {label!r}")
    path = os.path.join(EFFORT_DIR, model)
    with _ctx_lock:
        if match["kwargs"] is None:
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            os.makedirs(EFFORT_DIR, exist_ok=True)
            blob = json.dumps(match["kwargs"], separators=(",", ":"))
            assert " " not in blob, "kwargs JSON must be compact"
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(blob + "\n")
            os.replace(tmp, path)
        with LOCK:
            resident = any(r.get("model") == model for r in STATE["running"])
        if not resident:
            return False
        swap_get("/unload?model=" + urllib.parse.quote(model, safe=""))
        return True


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
            with _ctx_max_lock:
                maxes, natives, pools = dict(_ctx_max), dict(_ctx_native), dict(_ctx_pool)
                visions = dict(_ctx_vision)
                deployeds = dict(_ctx_deployed)
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
                    "ctx_max": maxes.get(m),
                    "ctx_native": natives.get(m),
                    "ctx_deployed": deployeds.get(m),
                    "vision": visions.get(m),
                    "effort": read_effort(m),
                    "effort_options": [o["label"] for o in (_effort_opts.get(m) or [])],
                    "ctx_pool": (pools.get(m) or {}).get("pool"),
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

        if self.path == "/api/effort":
            label = body.get("effort")
            if label is not None and not isinstance(label, str):
                return self._send(400, '{"error":"effort must be a string or null"}',
                                  "application/json")
            try:
                reloaded = apply_effort(model, label)
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            except OSError as e:
                return self._send(500, json.dumps({"error": str(e)[:200]}), "application/json")
            return self._send(200, json.dumps({"ok": True, "reloaded": reloaded}),
                              "application/json")

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
    # ctx-env.sh is a prefix shim, not the launcher -- the key belongs to the real one.
    assert _launcher_and_key("/x/ctx-env.sh m /x/serve-sglang.sh qwen38fn --port 1") == (
        "/x/serve-sglang.sh", "qwen38fn")
    # a launcher that takes no key must not swallow its own flag as one
    assert _launcher_and_key("/x/ctx-env.sh m /x/serve-nemcascade.sh --port 1") == (
        "/x/serve-nemcascade.sh", "")
    assert _launcher_and_key("no launcher here") == (None, None)
    # A launcher without DRYRUN must never be executed by the probe.
    assert _supports_dryrun("/nonexistent/serve-nope.sh") is False
    # Both spellings of the model path must resolve -- SGLang logs the second one.
    for _t in ("--model-path /models/Some-Model-NVFP4 --tp-size 2",
               "server_args=ServerArgs(model_path='/models/Some-Model-NVFP4', tp_size=2)"):
        assert re.search(r"model[_-]path[=\s]+['\"]?/models/([A-Za-z0-9._-]+)", _t).group(1) == \
            "Some-Model-NVFP4"
    # Effort options must come from the template, never from a fixed list: Qwen3.8
    # accepts low/medium/xhigh and raises on anything else, GLM accepts low/high.
    # Verbatim shape from Qwen3.8-Flash-Next: the validated name is a LONGER identifier
    # containing reasoning_effort, which is exactly what the matcher has to cope with.
    _q = ("{%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}"
          "{%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}")
    assert [o["label"] for o in _opts_from_text(_q)] == ["Default", "low", "medium", "xhigh"]
    _g = "{%- set e = reasoning_effort if reasoning_effort in ['low','high'] else 'max' -%}"
    assert [o["label"] for o in _opts_from_text(_g)] == ["Default", "high", "low"]
    _t = "{%- set enable_thinking = enable_thinking if enable_thinking is defined else True %}"
    assert [o["label"] for o in _opts_from_text(_t)] == ["Default", "thinking on", "thinking off"]
    assert _opts_from_text("no controls here at all") == []
    # every offered kwargs must serialise without whitespace -- ctx-env.sh word-splits it
    for _o in _opts_from_text(_q) + _opts_from_text(_t):
        if _o["kwargs"]:
            assert " " not in json.dumps(_o["kwargs"], separators=(",", ":"))

    assert _pool_from_sglang_text(
        "max_total_num_tokens=409536, chunked=8192, context_len=253952") == (409536, 253952)
    # serve-kathryn.sh still ignores DRYRUN and would launch for real over ssh, so the
    # guard MUST keep excluding it. Not listed here, deliberately: pick-node.sh and
    # serve-kathryn-nemcascade.sh both implement DRYRUN read-only (the former echoes the
    # chosen node, the latter defers to the local serve-nemcascade.sh instead of ssh-ing),
    # so probing those is safe.
    for _l in ("serve-kathryn.sh",):
        _p = os.path.join(LS_DIR, _l)
        if os.path.exists(_p):
            assert not _supports_dryrun(_p), f"{_l} now claims DRYRUN; re-verify it is read-only"
    # The ds4 entry point is not named serve-*; the matcher must still find it, and the
    # generic shims in a cmd chain must still be ignored.
    # Vision reflects what the member is STARTED with, not merely what the checkpoint
    # could do -- and the dflash argv arrives printf %q-escaped.
    assert _vision_from('--limit-mm-per-prompt {"image":0,"video":0}', True) == "off"
    assert _vision_from('--limit-mm-per-prompt {"image":1,"video":0}', True) == "on"
    assert _vision_from('--limit-mm-per-prompt \\{\\"image\\":0,\\"video\\":0\\}', True) == "off"
    assert _vision_from("--kv-cache-dtype fp8", True) == "on"      # no flag -> engine default
    assert _vision_from("--kv-cache-dtype fp8", False) is None     # no tower -> nothing to say
    # Max before any KV measurement is the launcher's configured context, never the raw
    # checkpoint window: ds4 declares 1048576 and ships 32768 because 1M OOMs.
    assert _usable_max(1048576, None, 32768) == 32768
    assert _usable_max(1048576, 409536, 32768) == 409536       # a measured pool wins
    assert _usable_max(262144, 409536, 16384) == 262144        # never past the checkpoint
    assert _usable_max(262144, None, None) == 262144
    assert _usable_max(None, None, 409600) is None             # no checkpoint number
    assert _deployed_ctx("key=q sub=S need=1MB ctx=16384 port=1") == 16384
    assert _deployed_ctx('docker run x --max-model-len "32768" --tp 2') == 32768
    assert _deployed_ctx("--max-model-len 262144 --max-num-seqs 4") == 262144
    assert _deployed_ctx("key=q sub=S flags=[--kv-cache-dtype fp8]") is None
    assert _launcher_and_key("/x/ctx-env.sh m /x/ds4-tp2-proxy-guard.sh") == (
        "/x/ds4-tp2-proxy-guard.sh", "")
    assert _launcher_and_key("/x/ctx-env.sh m /x/render-guard.sh /x/memcheck.sh 1 /x/serve-ds4.sh --port 1") == (
        "/x/serve-ds4.sh", "")
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
    for fn in (poll_nodes, poll_swap, keepalive, ctx_max_worker):
        threading.Thread(target=fn, daemon=True).start()
    pins = load_pins()
    print(f"Fleet on http://{host}:{a.port}   (llama-swap {SWAP})", flush=True)
    print(f"pinned: {', '.join(pins) if pins else 'none'}", flush=True)
    ThreadingHTTPServer((host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
