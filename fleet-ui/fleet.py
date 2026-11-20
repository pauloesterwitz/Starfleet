#!/usr/bin/env python3
"""Fleet — one page for llama-swap on the Starfleet cluster.

Pin models so llama-swap keeps them resident, and watch RAM/GPU/load on
every node (Jean-Luc + Kathryn).

llama-swap already owns model lifecycle, so this does not reimplement it:
  GET /v1/models              -> the catalogue
  GET /running                -> what is resident right now
  GET /upstream/<m>/health    -> loads <m> (no inference) and resets its ttl
  POST /api/models/unload/<m> -> evicts <m> (NOT GET /unload?model=, which evicts all)
"Persistently loaded" = we re-hit that health probe before the ttl expires.

Node resources come from one probe run locally and over ssh, not from
llama-swap's /metrics, so both nodes report identically. On GB10 the GPU shares
the system pool, so MemAvailable IS the VRAM number — nvidia-smi reports N/A.

stdlib only.  Run:  python3 fleet.py
"""

import argparse
import base64
import glob
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import wikicore      # Memory Core view: wiki graph + Computer's wiki trails (read-only)

# All llama-swap traffic goes through ctxproxy (:28081). Loads/unloads need the Starfleet key, which this
# process only passes through from the browser (X-Starfleet-Key) and never stores.
SWAP = os.environ.get("FLEET_SWAP", "http://127.0.0.1:28081")
KEY_HDR = "X-Starfleet-Key"
POLICY = "Only Starfleet Command may load models and Agents may not load models on their own. This is a policy!"

# None = run the probe locally.  Anything else is an ssh host.
# Use the *.fritz.box name, never the bare tailnet name: the latter hits
# Tailscale SSH, which wants a browser and breaks BatchMode.
NODES = {"jean-luc": None, "kathryn": "10.100.0.1"}

PIN_FILE = os.path.expanduser("~/.config/fleet-ui/pins.json")
LOAD_TIMES_FILE = os.path.expanduser("~/.config/fleet-ui/load-times.json")
LOAD_HISTORY = 5     # cold-load durations kept per model, for the ETA
MIN_COLD_LOAD = 5.0  # seconds; below this a "cold" touch was really a ttl refresh

# Per-model context override. This is the same file ctx-env.sh reads at launch
# (and ctxproxy.py writes for a sized `model@32768` request) -- see
# llama-swap/ctxproxy.py. Writing it here is the documented contract; ctxproxy
# is a pure request relay with no control endpoint to call instead.
CTX_DIR = os.path.join(os.environ.get("GB10_STATE_DIR") or os.path.expanduser("~/.gb10"), "ctx")
CTX_MAX_DIR = os.path.join(os.environ.get("GB10_STATE_DIR") or os.path.expanduser("~/.gb10"), "ctxmax")
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
    'echo "BOOT:$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"; '
    "echo \"LINK:$(i=$(ip -o route show default 2>/dev/null | awk '{print $5; exit}'); cat /sys/class/net/$i/speed 2>/dev/null)\"; "
    # Live entries only: a reservation whose owner pid is gone is stale and must
    # not be shown as held memory (memcheck.sh prunes those on its next run).
    'echo "RES:$(t=0; for f in $HOME/.gb10/reservations/*; do [ -e "$f" ] || continue; '
    'read -r pp mm _ < "$f" || continue; kill -0 "$pp" 2>/dev/null && t=$((t+mm)); done; echo $t)"'
)

STATE = {"nodes": {}, "models": [], "running": [], "pins": [], "errors": {}}
LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers

# Where ComfyUI actually runs. The `imagegen` member is STARTED by Jean-Luc's llama-swap
# but the container, and therefore every byte of image/video memory, lives on the node
# this URL points at -- for a long time this UI reported it as jean-luc, which was simply
# wrong. Fabric IPs follow the cluster convention (run-sglang-tp2-cluster.sh).
COMFYUI_URL = os.environ.get("FLEET_COMFYUI_URL", "http://10.100.0.1:8188")
_FABRIC_NODES = {"10.100.0.1": "kathryn", "10.100.1.1": "kathryn",
                 "10.100.0.2": "jean-luc", "10.100.1.2": "jean-luc",
                 "127.0.0.1": "jean-luc", "localhost": "jean-luc"}


def comfyui_node():
    """Node name ComfyUI runs on, for the Image Creation rows."""
    host = urllib.parse.urlparse(COMFYUI_URL).hostname or "127.0.0.1"
    return _FABRIC_NODES.get(host, host)


# ---- Image Creation catalog -------------------------------------------------------
# The image/video models live in Speech-to-Image's presets.FLEET_MODELS, not in
# llama-swap's config.yaml: llama-swap sees exactly ONE member here (`imagegen`, the
# ComfyUI container) and cannot tell which model is loaded inside it. Reading that table
# is what lets this UI show a real footprint per image/video model instead of one opaque
# row. Cached on mtime so editing the catalog needs no fleet-ui restart.
IMAGEGEN_REPO = os.environ.get("FLEET_IMAGEGEN_REPO",
                               os.path.expanduser("~/Speech-to-Image"))

# Where each image/video model should run, and what we last warmed there:
#   {"<model id>": {"node": "kathryn", "loaded_at": 1234567890.0}}
# Speech-to-Image reads the same file to route a render, so this one choice decides
# both what the UI shows and where the job actually goes.
IMG_NODE_FILE = os.path.expanduser("~/.config/fleet-ui/image-nodes.json")

# Every node that can run a render, and how to reach its ComfyUI. Fabric IPs, never the
# LAN or tailnet: ComfyUI has no auth (see run-comfyui.sh).
COMFY_ENDPOINTS = {"jean-luc": "http://10.100.0.2:8188",
                   "kathryn":  "http://10.100.0.1:8188"}
DEFAULT_IMG_NODE = os.environ.get("FLEET_DEFAULT_IMAGE_NODE", "kathryn")
_img_cache = {"mtime": None, "models": []}


def image_models():
    """[{id, kind, need_mb, note}] from the imagegen catalog. [] if it isn't there --
    fleet-ui must still start on a box without the image stack."""
    path = os.path.join(IMAGEGEN_REPO, "presets.py")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if mtime == _img_cache["mtime"]:
        return _img_cache["models"]
    models = []
    try:
        if IMAGEGEN_REPO not in sys.path:
            sys.path.insert(0, IMAGEGEN_REPO)
        import importlib
        import presets
        importlib.reload(presets)
        models = [dict(id=k, **v) for k, v in presets.FLEET_MODELS.items()]
    except Exception as e:
        print(f"fleet: image catalog unreadable: {e}", flush=True)
    _img_cache.update(mtime=mtime, models=models)
    return models


def load_img_nodes():
    try:
        with open(IMG_NODE_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_img_nodes(d):
    os.makedirs(os.path.dirname(IMG_NODE_FILE), exist_ok=True)
    with open(IMG_NODE_FILE, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)


def img_node(model, prefs=None):
    """Node this image model is assigned to."""
    prefs = load_img_nodes() if prefs is None else prefs
    n = (prefs.get(model) or {}).get("node")
    return n if n in COMFY_ENDPOINTS else DEFAULT_IMG_NODE


def _warm_workflow(model):
    """A deliberately tiny render that forces ComfyUI to load the model's weights.

    ComfyUI has no "load model" API -- weights arrive lazily on the first prompt that
    needs them, and only nodes feeding an output actually execute. So the cheapest
    honest warm-up is a real render at the smallest size the model tolerates, with the
    fewest steps. 256px / 1 step costs a couple of seconds once the weights are read.
    Returns None for a model we have no warm graph for.
    """
    if model == "qwen-image-2.1":
        return {
            "1": {"class_type": "UNETLoader",
                  "inputs": {"unet_name": "qwen_image_2.1_bf16.safetensors",
                             "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": "qwen3vl_8b_bf16.safetensors",
                             "type": "qwen_image"}},
            "3": {"class_type": "VAELoader",
                  "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
            "4": {"class_type": "TextEncodeQwenImage21",
                  "inputs": {"clip": ["2", 0], "prompt": "warmup",
                             "negative_prompt": "", "resolution": 256, "images": {}}},
            "5": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": 256, "height": 256, "batch_size": 1}},
            "6": {"class_type": "KSampler",
                  "inputs": {"seed": 1, "steps": 1, "cfg": 1.0, "sampler_name": "euler",
                             "scheduler": "simple", "denoise": 1.0, "model": ["1", 0],
                             "positive": ["4", 0], "negative": ["4", 1],
                             "latent_image": ["5", 0]}},
            "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
            "8": {"class_type": "PreviewImage", "inputs": {"images": ["7", 0]}},
        }
    return None


def warm_image_model(model, node):
    """Queue the warm-up graph on `node`. Returns (ok, message)."""
    wf = _warm_workflow(model)
    if wf is None:
        return False, (f"no warm-up graph for {model} yet -- it will load on its next "
                       f"real render on {node}")
    url = COMFY_ENDPOINTS[node] + "/prompt"
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"prompt": wf}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            json.load(r)
        return True, f"warming {model} on {node}"
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
            msg = (err.get("error") or {}).get("message") or str(e)
            for ne in (err.get("node_errors") or {}).values():
                for d in ne.get("errors", []):
                    msg = f"{msg}: {d.get('message')}"
                    break
                break
        except Exception:
            msg = str(e)
        return False, msg[:300]
    except (urllib.error.URLError, OSError) as e:
        return False, f"{node} unreachable: {str(e)[:150]}"


def unload_image_models(node):
    """POST /free on a node: drops ComfyUI's weights without killing the container."""
    url = COMFY_ENDPOINTS[node] + "/free"
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        return True, f"unloaded image weights on {node}"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return False, f"{node}: {str(e)[:150]}"


def _family_base(model):
    """Python twin of fleet.html's modelName(): the model with node suffix, -tp2 and tps removed."""
    m = re.sub(r"-(jean-luc|kathryn|fastest-node|starfleet)$", "", model)
    return re.sub(r"-\d+(?:\.\d+)?tps(?=-|$)", "", re.sub(r"-tp2(?=-|$)", "", m))


def _same_family(a, b):
    """Same logical model deployed once per machine (the Machine picker's families)."""
    return len(node_of(a)) == 1 == len(node_of(b)) and _family_base(a) == _family_base(b)


def node_of(model):
    """Which machines a model occupies. Encoded in the name suffix by config.yaml."""
    for suffix, nodes in (
        ("-starfleet", ["jean-luc", "kathryn"]),   # TP=2, spans both
        # ds4-nvfp4-tp2 is TP=2 without carrying the -starfleet suffix, and fell through to
        # the Jean-Luc default -- it has always been drawn on the wrong machine. Harmless
        # while the id itself still read "-tp2" on screen; not once the Model column shows
        # the model rather than the member id.
        ("-tp2", ["jean-luc", "kathryn"]),
        ("-fastest-node", ["dynamic"]),            # pick-node.sh decides at load time
        ("-jean-luc", ["jean-luc"]),
        ("-kathryn", ["kathryn"]),
    ):
        if model.endswith(suffix):
            return nodes
    if model == "imagegen":
        return [comfyui_node()]
    return ["jean-luc"]  # embeds are started by Jean-Luc's llama-swap


TPS_FILE = os.path.expanduser("~/.config/fleet-ui/tps.json")
_tps_override = {}
_tps_mtime = None


def _load_tps_overrides():
    """Measured tok/s for members whose NAME carries no -<N>tps- marker.

    The SPEED column originally had one source: the member id. That works for
    `gemma4-26b-54tps-jean-luc` and not at all for `qwen38fn-long-sglang-tp2-starfleet`,
    so every big TP=2 member rendered as "-". Renaming them is not an option -- the id is
    the model name opencode, litellm/claude-local, the ctx state files and RAG-Lab all
    address, so a rename to carry a number would break every one of them. A measured value
    belongs in data anyway, not in an identifier: it changes when the config changes.
    Reloaded on mtime so editing the file needs no restart. Values may be a bare number or
    {"tps": n, "src": "..."} -- keep the provenance, these numbers go stale silently.
    """
    global _tps_override, _tps_mtime
    try:
        st = os.stat(TPS_FILE).st_mtime
    except OSError:
        return
    if st == _tps_mtime:
        return
    try:
        with open(TPS_FILE) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return
    out = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            v = v.get("tps")
        if isinstance(v, (int, float)):
            out[k] = v
    _tps_override, _tps_mtime = out, st


def tps_of(model):
    """The measured tok/s for a member: the overrides file first, then the number baked
    into the model name, e.g. '...-57tps-mtp4-...' -> 57."""
    _load_tps_overrides()
    if model in _tps_override:
        return _tps_override[model]
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
        elif line.startswith("BOOT:"):
            out["boot"] = line[5:].strip()
        elif line.startswith("LINK:"):
            try:
                out["link_mbps"] = int(line[5:].strip())
            except ValueError:
                pass
    return out if "mem_total_kb" in out else None


def _err_text(e):
    """HTTPError body (the gate's policy message) or the plain error text."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.loads(e.read()).get("error", {}).get("message") or str(e)
        except (ValueError, AttributeError):
            return str(e)
    return str(e)


def swap_get(path, timeout=10, key=None):
    req = urllib.request.Request(SWAP + path, headers={KEY_HDR: key} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def swap_unload(model, timeout=30, key=None):
    """Evict ONE model. llama-swap's plain GET /unload ignores ?model= and unloads
    EVERY member, which killed the pinned Starfleet model whenever any other model was
    unpinned (2026-10-02). The per-model route is POST /api/models/unload/<model>."""
    req = urllib.request.Request(
        SWAP + "/api/models/unload/" + urllib.parse.quote(model, safe=""), method="POST",
        headers={KEY_HDR: key} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def _mem_avail_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def swap_running():
    """Model ids llama-swap still lists as resident (any state), straight from /running."""
    return {r.get("model") for r in json.loads(swap_get("/running")).get("running", [])}


def evict(model, key=None, wait=90):
    """Unload `model`, then PROVE it: poll llama-swap until it no longer lists it, so a
    green button means the process (and its container, via the serve script's stop trap)
    is gone, not merely that the request was accepted. Returns MB of RAM freed on this
    machine (a Kathryn member frees memory over there, so ~0 here is normal for those).
    Raises OSError if the model is still resident after `wait` seconds."""
    before = _mem_avail_mb()
    swap_unload(model, timeout=wait, key=key)
    deadline = time.time() + wait
    while model in swap_running():
        if time.time() > deadline:
            raise OSError(f"{model} is still resident {wait}s after unload; memory NOT freed")
        time.sleep(1)
    time.sleep(2)                       # let the kernel return the pages
    after = _mem_avail_mb()
    return max(0, after - before) if None not in (before, after) else None


_GPU_PS = ("docker ps -q | xargs -r docker inspect -f "
           "'{{.Name}} {{.HostConfig.Runtime}} {{len .HostConfig.DeviceRequests}}'")


def gpu_containers(host):
    """Names of running containers that hold a GPU (nvidia runtime or a device request) on one
    node. That is every model server this stack starts -- vLLM/SGLang/Ray heads and workers,
    embedders, rerankers, ComfyUI -- and none of the plain service containers."""
    cmd = ["bash", "-lc", _GPU_PS] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, _GPU_PS]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise OSError((r.stderr or "docker ps failed").strip()[:200])
    names = []
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) == 3 and (f[1] == "nvidia" or f[2] != "0"):
            names.append(f[0].lstrip("/"))
    return names


def _mcp_containers():
    """[(container name, pid of its `docker run` client or None)] for local containers started with
    `docker run -i` (MCP-style servers). A container belongs to a client when a live `docker run`
    process names it (--name) or started within 4 s of it with the same image. Services without an
    open stdin (qdrant, postgres, open-webui, ...) are never listed."""
    ps = subprocess.run(["ps", "-eo", "pid=,etimes=,args="], capture_output=True, text=True, timeout=20).stdout
    clients = []
    for line in ps.splitlines():
        f = line.split(None, 2)
        if len(f) == 3 and f[0].isdigit() and f[1].isdigit() and f[2].startswith("docker run"):
            clients.append((int(f[0]), int(f[1]), f[2]))
    ids = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=30).stdout.split()
    if not ids:
        return []
    fmt = "{{.Name}}\t{{.Config.OpenStdin}}\t{{.Config.Image}}\t{{.State.StartedAt}}"
    out = subprocess.run(["docker", "inspect", "-f", fmt, *ids], capture_output=True, text=True,
                         timeout=30).stdout
    now, found = time.time(), []
    for line in out.splitlines():
        try:
            name, stdin, image, started = line.split("\t")
            if stdin != "true":
                continue
            ts = datetime.datetime.fromisoformat(re.sub(r"(\.\d{6})\d*Z$", r"\1+00:00", started)).timestamp()
        except ValueError:
            continue
        name, age = name.lstrip("/"), now - ts
        owner = next((pid for pid, e, a in clients
                      if f"--name {name} " in a + " " or (abs(e - age) <= 4 and image in a)), None)
        found.append((name, owner))
    return found


def orphaned_containers():
    """MCP-style containers whose `docker run` client is gone: nothing can talk to them any more."""
    return [name for name, owner in _mcp_containers() if owner is None]


def session_idle_s(launcher_pid):
    """Seconds since the session that launched an MCP container was last active, or None if unknown.
    Claude Code (ccd-cli): age of its session transcript. opencode: age of last input on its terminal.
    Unknown never counts as dormant."""
    try:
        with open(f"/proc/{launcher_pid}/cmdline") as f:
            cmd = f.read().replace("\0", " ")
        if "ccd-cli" in cmd:
            for u in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", cmd):
                for t in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{u}.jsonl")):
                    return time.time() - os.stat(t).st_mtime
        elif "opencode" in cmd:
            tty = subprocess.run(["ps", "-o", "tty=", "-p", str(launcher_pid)], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
            if tty.startswith("pts"):
                return time.time() - os.stat("/dev/" + tty).st_atime
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def dormant_session_containers(hours=24):
    """MCP containers whose launching session has been inactive for more than `hours`. The session
    process itself keeps running; only its container tools go away until it is restarted."""
    out = []
    for name, owner in _mcp_containers():
        if owner is None:
            continue
        try:
            with open(f"/proc/{owner}/stat") as f:
                launcher = int(f.read().rsplit(")", 1)[1].split()[1])     # ppid of the docker client
        except (OSError, ValueError, IndexError):
            continue
        idle = session_idle_s(launcher)
        if idle is not None and idle > hours * 3600:
            out.append(name)
    return out


def _docker_stop(host, names):
    if not names:
        return
    script = "docker stop -t 10 " + " ".join(names)
    cmd = ["bash", "-lc", script] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, script]
    subprocess.run(cmd, capture_output=True, text=True, timeout=180)


DORMANT_HOURS = 24      # Red Alert stops MCP containers of sessions inactive for longer than this


def red_alert(key):
    """Stop everything that holds a model in memory, on every node. Order matters:
    1. the key is proven by llama-swap's unload-all (the gate refuses it otherwise);
    2. pins are cleared, else keepalive/pins would reload them;
    3. wait until llama-swap lists nothing (its serve-script traps stop their containers);
    4. docker stop whatever GPU container is still up -- orphans llama-swap no longer knows."""
    if not key:
        raise urllib.error.HTTPError(SWAP, 403, POLICY, {}, io.BytesIO(
            json.dumps({"error": {"message": POLICY}}).encode()))
    with _load_cv:
        _load_q.clear()                          # queued loads must not start after a Red Alert
    before = _mem_avail_mb()
    with _loading_lock:
        loading = list(_loading)
    with _cancel_lock:
        _cancelled.update(loading)               # their health probes die: not a fault
    try:
        unloaded = sorted(swap_running())
    except (OSError, ValueError):
        unloaded = []
    swap_get("/unload", timeout=120, key=key)
    save_pins(set())
    deadline = time.time() + 90
    while swap_running() and time.time() < deadline:
        time.sleep(1)
    stopped, errors = {}, {}
    for node, host in NODES.items():
        try:
            names = gpu_containers(host)
            _docker_stop(host, names)
            stopped[node] = names
        except (subprocess.SubprocessError, OSError) as e:
            errors[node] = str(e)[:200]
    orphans, dormant = [], []
    try:
        orphans = orphaned_containers()
        dormant = dormant_session_containers(DORMANT_HOURS)
        _docker_stop(None, orphans + dormant)
    except (subprocess.SubprocessError, OSError) as e:
        errors["orphans"] = str(e)[:200]
    with LOCK:
        STATE["errors"].clear()
    time.sleep(2)
    after = _mem_avail_mb()
    freed = max(0, after - before) if None not in (before, after) else None
    return {"ok": not errors and not swap_running(), "unloaded": unloaded, "stopped": stopped,
            "orphans": orphans, "dormant": dormant, "errors": errors, "freed_mb": freed}


def _sudo_reboot(host, delay):
    """Schedule a graceful reboot (systemd stops units and containers first) `delay` seconds
    out, so this call returns before the machine goes down."""
    script = f"sudo -n systemd-run --on-active={delay} --unit=starfleet-autodestruct systemctl reboot"
    cmd = ["bash", "-lc", script] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, script]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise OSError((r.stderr or "reboot scheduling failed").strip()[:200])


# Auto Destruct's typed confirmation. It comes from the service environment and is never in the repo;
# unset means Auto Destruct is disabled. A speed bump only: the Starfleet key is the real gate.
AUTODESTRUCT_CODE = os.environ.get("STARFLEET_AUTODESTRUCT_CODE", "").strip().upper()


JEAN_LUC_REBOOT_S = 10      # Jean-Luc reboots this long after the request returns (Kathryn: 3 s)


class AutoDestructOff(Exception):
    """No code is configured, so Auto Destruct refuses everything."""


def _check_code(code):
    if not AUTODESTRUCT_CODE:      # fail closed: "" would otherwise equal an empty submitted code
        raise AutoDestructOff("Auto Destruct is disabled: set STARFLEET_AUTODESTRUCT_CODE in the "
                              "Fleet UI service environment.")
    if not hmac.compare_digest(str(code or "").strip().upper().encode(), AUTODESTRUCT_CODE.encode()):
        raise PermissionError("Authorization code rejected.")


def auto_destruct_check(key, code):
    """Pre-flight for the 10 s cancel window: prove the code and the Starfleet key, execute nothing.
    The key is proven with a no-op unload of a model that is not resident."""
    _check_code(code)
    return key_check(key)


def key_check(key):
    """Prove the Starfleet key with a no-op unload of a model that is not resident."""
    with LOCK:
        running = {r.get("model") for r in STATE["running"]}
        idle = next((m for m in STATE["models"] if m not in running), None)
    if not key or (idle and not key_ok(idle, key)):
        raise urllib.error.HTTPError(SWAP, 403, POLICY, {}, io.BytesIO(
            json.dumps({"error": {"message": POLICY}}).encode()))
    return {"ok": True}


def auto_destruct(key, code=None):
    """Red Alert (clean stop of every model and GPU container), then reboot Kathryn, then
    Jean-Luc. Kathryn is scheduled first and Jean-Luc JEAN_LUC_REBOOT_S later: this UI runs on Jean-Luc, so
    it must be the last thing to go. If Kathryn cannot be scheduled, Jean-Luc is left alone."""
    _check_code(code)
    alert = red_alert(key)                      # raises 403 without the key
    _sudo_reboot(NODES["kathryn"], 3)
    _sudo_reboot(NODES["jean-luc"], JEAN_LUC_REBOOT_S)
    return {"ok": True, "red_alert": alert, "kathryn_in_s": 3, "jean_luc_in_s": JEAN_LUC_REBOOT_S}


def key_ok(model, key):
    """Cheap key check with no side effect: unloading a model that is not resident is a no-op
    at llama-swap, but the gate answers 403 to a missing or wrong key before it gets there."""
    if not key:
        return False
    try:
        swap_unload(model, key=key)
    except urllib.error.HTTPError as e:
        return e.code != 403
    except OSError:
        return True          # gate unreachable is not a key problem; the load will report it
    return True


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


_load_times_lock = threading.Lock()


def record_load_time(model, secs):
    """Append one cold-load duration, keeping only the last LOAD_HISTORY."""
    # Two loads finishing together shared one .tmp: the first os.replace consumed it and the
    # second raised ENOENT. The lock also covers the read-modify-write, so neither sample is lost.
    with _load_times_lock:
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


# Real progress, read from the engine's own startup narration. The clock alone
# lies badly here: the SAME member has come up in 208s and in 2492s, because
# CUDA-graph capture is 3s against a warm compile cache and 13 minutes against a
# cold one. vLLM and SGLang both say where they are, so take the percentage from
# them and use the clock only to project what is left.
# (label, fraction where the phase starts, where it ends, pattern). A pattern with
# a capture group carries a tqdm percentage that interpolates inside the phase; one
# without means only "this phase has begun".
# Bounds are what the logs of the slowest members actually show: weights dominate
# (GLM-5.3 TP2: load_weight=910s of a ~1800s startup), and the tail after uvicorn
# says "Application startup complete" is NOT nothing -- that member answered /health
# with 503 for another 142s before it was really up.
LOAD_PHASES = (
    ("starting engine",       0.00, 0.06,
     r"gate .*? pool|Init torch distributed begin|ring up:|both nodes up"),
    # "Load weight begin" carries no percentage, but it is the ONLY weight marker
    # some loaders print (SGLang's ModelOpt path for the NVFP4 checkpoints prints no
    # shard tqdm at all) -- without it the bar sits at 0% through the longest phase.
    ("loading weights",       0.06, 0.60,
     r"Load weight begin|(?:Multi-thread loading|Loading (?:safetensors|pt) checkpoint)"
     r" shards:\s*(\d+)%"),
    ("weights loaded",        0.60, 0.66,
     r"Load weight end|Model loading took"),
    ("allocating KV cache",   0.66, 0.70,
     r"KV Cache is allocated|GPU KV cache size"),
    ("capturing CUDA graphs", 0.70, 0.90,
     r"Capturing (?:batches|CUDA graph shapes)[^:\n]*:\s*(\d+)%"),
    ("warming up",            0.90, 0.99,
     r"Engine startup timings|init engine \(profile|Application startup complete|Uvicorn running"),
    ("ready",                 0.99, 1.00,
     r"server is fired up"),
)
LOG_TAIL = 65536     # bytes of log read per poll; the whole narration fits many times over


def _phase_from_text(text):
    """(fraction, label) for the FURTHEST phase named in `text`, or (None, None).

    Furthest, not last: SGLang re-runs the early phases for its draft model, so the
    final matching line in a log is regularly an earlier stage than the load is at.
    """
    best = None
    for i, (label, lo, hi, pat) in enumerate(LOAD_PHASES):
        for m in re.finditer(pat, text):
            if best is None or i >= best[0]:
                pct = m.group(1) if m.lastindex else None
                best = (i, label, lo + (hi - lo) * int(pct) / 100 if pct else lo)
    return (round(best[2], 4), best[1]) if best else (None, None)


def _engine_log(member):
    """The launcher log this member narrates its startup into, or None. Same two
    names observe_pools() already knows: vLLM writes run-<sub>.log, the SGLang
    cluster launcher tees its head's output into sglang-<sub>.log."""
    with _ctx_max_lock:
        sub = _ctx_sub.get(member)
    if not sub:
        return None
    paths = [q for n in (f"run-{sub}.log", f"sglang-{sub}.log")
             for q in [os.path.join(LS_LOGS, n)] if os.path.exists(q)]
    return max(paths, key=os.path.getmtime) if paths else None


def _log_mark(member):
    """(path, size-now) -- where this load's own output will start. Taken when the
    load begins so the PREVIOUS run's "100% Completed" can never be read as this
    one's progress, which is the whole reason the offset exists."""
    path = _engine_log(member)
    try:
        return path, os.path.getsize(path)
    except (OSError, TypeError):
        return path, 0


def load_progress(mark):
    """(fraction, label) for a load in flight, from the bytes its own run appended."""
    path, off = mark or (None, 0)
    if not path:
        return None, None
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            start = off if size >= off else 0      # the launcher truncates on re-run
            fh.seek(max(start, size - LOG_TAIL))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None, None
    return _phase_from_text(text)


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


def read_ctx_ceiling(member):
    """The largest context this member may be SET to, from ~/.gb10/ctxmax/<member>.

    Why this exists: the offered Max is min(checkpoint window, measured KV pool), and the pool only
    reflects the context the member is loaded at RIGHT NOW. A GLM member serving 256k therefore
    advertised 256k -- or 32768 before its pool was ever parsed -- so the Context control could
    never step UP to what the member is actually allowed to serve. serve-sglang.sh clamps to the
    same number, so the control and the launcher agree instead of contradicting each other.
    """
    try:
        with open(os.path.join(CTX_MAX_DIR, member)) as fh:
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
    m = re.search(r"model[_-]path['\"]?\s*[:=]\s*['\"]?/models/([A-Za-z0-9._-]+)", sgl_text or "")
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


def _rope_override_max(argv):
    """The window a rope-scaling override buys, from the launcher's own DRYRUN argv.

    A member can serve PAST its checkpoint's max_position_embeddings when it is started
    with a RoPE override: qwen38fn-long runs Qwen3.8-Flash-Next (262,144 in config.json)
    at 524,288 with `--json-model-override-args {"text_config":{"max_position_embeddings":
    524288,"rope_parameters":{"rope_type":"yarn",...}}}`. Without this, _native_max reads
    the checkpoint and caps the member at half of what it actually serves -- the two
    members share one checkpoint directory, so the file alone cannot tell them apart.
    Only an override that ALSO scales rope counts: raising max_position_embeddings on its
    own does not extend anything, it just removes the guard rail.
    """
    flat = (argv or "").replace("\\", "")
    if "rope_type" not in flat and "rope_scaling" not in flat:
        return None
    m = re.search(r'max_position_embeddings\\?"?\s*:\s*(\d+)', flat)
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
                # a rope override outranks the checkpoint: same weights, wider window
                val = _rope_override_max(argv) or _native_max(sub_)
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


def apply_effort(model, label, key=None):
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
        _CFG_CHANGED[model] = time.time()
        swap_unload(model, key=key)
        return True


def apply_ctx(model, ctx, key=None):
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
        _CFG_CHANGED[model] = time.time()
        swap_unload(model, key=key)
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


def touch(model, key=None):
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
            _loading[model] = {"started": started, "eta": eta_for(model),
                               "mark": _log_mark(model), "progress": None, "phase": None}
    cancelled = False
    with LOCK:
        # ponytail: a row that is actively loading must not still show the previous
        # attempt's error. An error badge reads as "the start was aborted", so leaving
        # a stale one on a healthy in-flight load is a lie. Cleared on entry; re-set
        # below only if THIS attempt actually fails.
        STATE["errors"].pop(model, None)
    try:
        swap_get(f"/upstream/{q}/health", timeout=LOAD_TIMEOUT, key=key)
        elapsed = time.time() - started
        # MIN_COLD_LOAD guards the history against a "cold" touch that was in
        # fact served instantly -- the model came up between poll_swap's 3s
        # snapshot and this call. Averaging those in drags every ETA to zero.
        if cold and elapsed >= MIN_COLD_LOAD:
            record_load_time(model, elapsed)
            ev_append("info", "load-done", "Model loaded", f"{_family_base(model)} is ready (took {int(elapsed // 60)} min {int(elapsed % 60)} s).", go="#/models")
        with LOCK:
            STATE["errors"].pop(model, None)
        return True
    except urllib.error.HTTPError as e:
        msg = _err_text(e)[:400] or f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        msg = str(e)[:400]
    finally:
        if cold:
            with _loading_lock:
                _loading.pop(model, None)
        with _cancel_lock:
            cancelled = model in _cancelled
            _cancelled.discard(model)
    if cancelled:
        with LOCK:
            STATE["errors"].pop(model, None)   # the user asked for this; it is not a fault
        return False
    with LOCK:
        STATE["errors"][model] = msg
    if cold:
        ev_append("info", "load-failed", "Load failed", f"{_family_base(model)} did not start: {msg[:120]}", go="#/models")
    return False


_cancelled = set()        # models whose in-flight cold load the user aborted
_cancel_lock = threading.Lock()


def cancel_load(model, key=None):
    """Abort a cold load in flight.

    Unpin FIRST -- otherwise keepalive puts the model straight back, which is what
    "cancel" must never do. Then ask llama-swap to kill the process the health probe
    is blocked on; the touch() thread unwinds when that connection dies. The bar is
    dropped here rather than there so the page stops showing a load the moment the
    user cancels it, however long the upstream takes to actually die.
    """
    pins = set(load_pins())
    if model in pins:
        pins.discard(model)
        save_pins(pins)
    with _cancel_lock:
        _cancelled.add(model)
    with _loading_lock:
        _loading.pop(model, None)
    swap_unload(model, key=key)


_reloading = set()        # pins currently being re-touched, so we don't double-fire
_reloading_lock = threading.Lock()


# ---------------------------------------------------------------- load queue
# Embedding, reranker and image models are "light": they must never start loading while an LLM
# is loading (they fight it for memory and bandwidth). Their loads go through ONE queue and run
# one after the other, each only when no LLM load is in flight. An LLM load never waits.
# The Starfleet key rides along in memory only until the job runs; nothing is written to disk.

_LIGHT = re.compile(r"embed|rerank", re.I)
_load_q = []                    # FIFO of {"model", "key", "job"}
_load_cv = threading.Condition()
_llm_busy = set()               # LLM members whose load request is in flight
_q_current = None               # light model the worker is loading right now


def is_light(model):
    return bool(_LIGHT.search(model)) or model in {m["id"] for m in image_models()}


def _set_llm_busy(model, on):
    with _load_cv:
        (_llm_busy.add if on else _llm_busy.discard)(model)
        _load_cv.notify_all()


def enqueue_light(model, key, job):
    """Queue `job(model, key)`. Returns its 1-based position, or None if it is already queued/running."""
    with _load_cv:
        if _q_current == model or any(j["model"] == model for j in _load_q):
            return None
        _load_q.append({"model": model, "key": key, "job": job})
        _load_cv.notify_all()
        return len(_load_q)


def dequeue(model):
    """Drop a not-yet-started job (unpin / unload / cancel of a queued model). True if one was removed."""
    with _load_cv:
        before = len(_load_q)
        _load_q[:] = [j for j in _load_q if j["model"] != model]
        return len(_load_q) != before


def queued_ids():
    with _load_cv:
        return [j["model"] for j in _load_q] + ([_q_current] if _q_current else [])


def _next_job(wait=2.0):
    """The next queued job, or None while the queue is empty or an LLM is loading."""
    global _q_current
    with _load_cv:
        if (not _load_q or _llm_busy) and wait:
            _load_cv.wait(timeout=wait)
        if not _load_q or _llm_busy:
            return None
        j = _load_q.pop(0)
        _q_current = j["model"]
        return j


def load_queue_worker():
    global _q_current
    while True:
        j = _next_job()
        if j is None:
            continue
        try:
            j["job"](j["model"], j["key"])
        except Exception as e:                   # a failed light load must not stop the queue
            with LOCK:
                STATE["errors"][j["model"]] = str(e)[:300]
        finally:
            with _load_cv:
                _q_current = None
                _load_cv.notify_all()


def _comfy_wait(node, timeout=900):
    """Block until ComfyUI on `node` has nothing running or pending (the warm-up graph finished)."""
    url, end = COMFY_ENDPOINTS[node] + "/queue", time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                q = json.load(r)
        except (OSError, ValueError):
            return
        if not q.get("queue_running") and not q.get("queue_pending"):
            return
        time.sleep(2)


def _warm_job(model, key, node):
    ok, msg = warm_image_model(model, node)
    if not ok:
        with LOCK:
            STATE["errors"][model] = msg
        return
    _comfy_wait(node)
    prefs = load_img_nodes()
    prefs.setdefault(model, {})["node"] = node
    prefs[model]["loaded_at"] = time.time()
    save_img_nodes(prefs)


def request_load(model, key):
    """Start loading `model` on behalf of the UI. LLMs start at once; light models are queued.
    Returns the queue position for a queued model, else 0."""
    if is_light(model):
        return enqueue_light(model, key, lambda m, k: touch(m, k)) or 0
    _set_llm_busy(model, True)

    def run():
        try:
            touch(model, key)
        finally:
            _set_llm_busy(model, False)

    threading.Thread(target=run, daemon=True).start()
    return 0


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
            # Policy: only a human with the Starfleet key loads. Resident pins get their ttl
            # refreshed; a pin that is not resident waits for a keyed Load in the UI.
            if model in running_ids:
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


# ---------------------------------------------------------------- deep space nas
# Container view of the Synology NAS. Same path as the synology-nas-docker MCP: ssh to the
# `nas-deployer` alias, whose forced command (nas_gate.py on the NAS) owns the policy -- it only
# knows ps/health/logs/control/..., so nothing here can reach a shell or raw docker. Nothing in
# this block loads or unloads a model, so no Starfleet key is involved.
NAS_HOST = "nas-deployer"
NAS_TTL = 8.0            # one ssh round trip takes ~5 s; concurrent pollers share one answer
_NAS = {"at": 0.0, "data": None}
_NAS_LOCK = threading.Lock()


def _nas_gate(*args, timeout=60):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", NAS_HOST,
                        shlex.join(args)], capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    out = r.stdout.decode("utf-8", "replace").strip()
    try:
        return json.loads(out)
    except ValueError:
        raise OSError((r.stderr.decode("utf-8", "replace").strip() or out or f"ssh exit {r.returncode}")[-300:])


def nas_state(force=False):
    """All containers (running and stopped) as Container Manager lists them; cached NAS_TTL seconds."""
    with _NAS_LOCK:
        if force or time.time() - _NAS["at"] > NAS_TTL:
            try:
                rows = _nas_gate("ps", "--all")
                if isinstance(rows, dict):
                    raise OSError(rows.get("error") or rows.get("message") or str(rows)[:200])
                _NAS["data"] = {"containers": rows, "error": None}
            except (OSError, subprocess.SubprocessError) as e:
                # keep the last good list on screen, flag it stale
                old = (_NAS["data"] or {}).get("containers", [])
                _NAS["data"] = {"containers": old, "error": str(e)[:200]}
            _NAS["at"] = time.time()
        return dict(_NAS["data"], at=_NAS["at"])


_NASS = {"at": 0.0, "data": None}
_NASS_LOCK = threading.Lock()


def nas_stats():
    """Host KPIs from the gate's read-only `stats` verb (CPU, RAM, temperatures, network); cached like nas_state.
    An older gate without the verb answers with an error: the last good numbers stay, flagged stale."""
    with _NASS_LOCK:
        if time.time() - _NASS["at"] > NAS_TTL:
            try:
                s = _nas_gate("stats")
                if "error" in s:
                    raise OSError(s.get("message") or s["error"])
                _NASS["data"] = {"stats": s, "error": None}
            except (OSError, subprocess.SubprocessError) as e:
                _NASS["data"] = {"stats": (_NASS["data"] or {}).get("stats"), "error": str(e)[:200]}
            _NASS["at"] = time.time()
        return dict(_NASS["data"], at=_NASS["at"])


def nas_control(target, action):
    if action not in ("start", "stop", "restart", "rm"):
        raise ValueError("action must be start, stop, restart or rm")
    if target not in {c["Names"] for c in nas_state()["containers"]}:
        raise ValueError("unknown container")
    # rm = the gate's `rm` verb: stopped containers only, never frozen projects (the gate decides, not us)
    res = _nas_gate("rm", target) if action == "rm" else _nas_gate("control", target, action, timeout=330)
    _NAS["at"] = 0.0          # next poll re-reads the NAS
    if isinstance(res, dict) and res.get("error"):
        raise OSError(res.get("message") or res["error"])
    return res


# ---------------------------------------------------------------- node containers (Engineering tab)

# One read-only probe per node: running containers, their memory (cgroup memory.current minus
# inactive_file, the number `docker stats` shows), GPU memory per process mapped to its container,
# cumulative CPU time, and the process table (to find who started each container).
CT_PROBE = r"""ids=$(docker ps -q --no-trunc 2>/dev/null)
echo "@I"
[ -n "$ids" ] && docker inspect -f '{{.Id}}|{{.Name}}|{{.Config.Image}}|{{.State.StartedAt}}|{{index .Config.Labels "com.docker.compose.project"}}|{{.HostConfig.Runtime}}|{{if .HostConfig.DeviceRequests}}1{{else}}0{{end}}|{{.HostConfig.RestartPolicy.Name}}|{{.Config.OpenStdin}}' $ids
echo "@C"
for id in $ids; do d=/sys/fs/cgroup/system.slice/docker-$id.scope; [ -d "$d" ] || continue
  echo "$id $(cat $d/memory.current) $(awk '$1=="inactive_file"{print $2}' $d/memory.stat) $(awk '$1=="usage_usec"{print $2}' $d/cpu.stat)"; done
echo "@G"
nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null | while IFS=', ' read -r p m; do
  c=$(grep -o 'docker-[0-9a-f]\{64\}' /proc/$p/cgroup 2>/dev/null | head -1 | cut -c8-); [ -n "$c" ] && echo "$c $m"; done
echo "@P"
ps -eo pid=,ppid=,etimes=,unit=,uunit=,args= | cut -c1-400"""
CT_TTL = 8.0            # concurrent viewers share one probe
CT_POLL = 30            # background probe interval: keeps the CPU-idle clocks running with no tab open
CPU_ACTIVE = 0.01       # cores; a container above this between two probes counts as active
_CT = {"at": 0.0, "data": None}
_CT_LOCK = threading.Lock()
_CT_CPU = {}            # (node, id) -> [usage_usec, t_sample, t_last_active or None, t_first_seen]
_SESSION_LABELS = ("Claude Code", "opencode")
_OWNER_STOP = {"Claude Code", "opencode", "llama-swap", "systemd", "sshd", "init"}


def _ct_probe(host):
    cmd = ["bash", "-c", CT_PROBE] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, CT_PROBE]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
    if "@P" not in r.stdout:
        raise OSError((r.stderr or f"probe exit {r.returncode}").strip()[-200:])
    out, sec = {"I": [], "C": [], "G": [], "P": []}, None
    for line in r.stdout.splitlines():
        if line in ("@I", "@C", "@G", "@P"):
            sec = line[1]
        elif sec and line.strip():
            out[sec].append(line)
    return out


def _ps_table(lines):
    t = {}
    for line in lines:
        f = line.split(None, 5)
        if len(f) == 6 and f[0].isdigit() and f[1].isdigit() and f[2].isdigit():
            unit = f[4] if f[4] != "-" else f[3]                       # a user unit beats user@UID.service
            t[int(f[0])] = (int(f[1]), int(f[2]), f[5], unit)          # ppid, age s, args, systemd unit
    return t


def _proc_label(args):
    a0 = args.split()[0] if args.split() else "?"
    base = os.path.basename(a0)
    if "ccd-cli" in a0 or base == "claude":
        return "Claude Code"
    if "opencode" in a0:
        return "opencode"
    if "llama-swap/llama-swap" in a0:
        return "llama-swap"
    if base in ("bash", "sh", "dash", "python3", "python") and len(args.split()) > 1:
        arg = next((x for x in args.split()[1:] if not x.startswith("-")), "")
        return os.path.basename(arg) or base
    return {"systemd": "systemd", "sshd": "sshd"}.get(base, base)


def _owner_chain(ps, pid):
    """('llama-swap › run-x.sh', pid of the Claude Code/opencode session or None) for a client process:
    walk its ancestors to the first agent, llama-swap, sshd or systemd."""
    unit = ps[pid][3] if pid in ps else "-"
    labels, session, seen = [], None, 0
    while pid in ps and pid > 1 and seen < 8:
        seen += 1
        pid = ps[pid][0]
        if pid not in ps:
            break
        lab = _proc_label(ps[pid][2])
        if lab in _SESSION_LABELS and session is None:
            session = pid
        labels.append(lab)
        if lab in _OWNER_STOP:
            break
    labels = [x for x in labels if x not in ("systemd", "init", "server", "node")] or labels[-1:]
    if len(labels) > 1:
        labels = [labels[-1], labels[0]]       # the top-level owner, then the direct launcher
    if labels == ["systemd"] and unit not in ("-", ""):
        labels = [f"systemd · {unit}"]
    return " › ".join(dict.fromkeys(labels)), session


def _find_client(ps, name, image, age, used):
    """pid of the process that runs or follows this container: a `docker run|logs|attach` naming it,
    else a `docker run` of the same image started within 4 s of it (random-named MCP containers)."""
    hits = [pid for pid, (_, _, a, _) in ps.items()
            if "docker " in a and (f"--name {name} " in a + " " or f" {name} " in f" {a} ") and pid not in used]
    hits.sort(key=lambda p: " run " not in ps[p][2])          # prefer the `docker run` client
    if hits:
        return hits[0]
    for pid, (_, et, a, _) in ps.items():
        if pid not in used and a.startswith("docker run") and image in a and abs(et - age) <= 4:
            return pid
    return None


def _cpu_idle(node, cid, usec, now):
    e = _CT_CPU.get((node, cid))
    if e is None:
        _CT_CPU[(node, cid)] = e = [usec, now, None, now]
    elif now - e[1] >= 1:
        if (usec - e[0]) / 1e6 / (now - e[1]) > CPU_ACTIVE:
            e[2] = now
        e[0], e[1] = usec, now
    return now - (e[2] or e[3]), e[2] is None


def _parse_ts(started):
    return datetime.datetime.fromisoformat(re.sub(r"(\.\d{6})\d*Z$", r"\1+00:00", started)
                                           .replace("Z", "+00:00")).timestamp()


def _ct_rows(node, raw, other_ps, now):
    ps, used, rows = _ps_table(raw["P"]), set(), []
    cg = {}
    for line in raw["C"]:
        f = line.split()
        if len(f) == 4:
            cg[f[0]] = (int(f[1]) - int(f[2] or 0), int(f[3]))
    gpu = {}
    for line in raw["G"]:
        f = line.split()
        if len(f) == 2 and f[1].isdigit():
            gpu[f[0]] = gpu.get(f[0], 0) + int(f[1])
    for line in raw["I"]:
        f = line.split("|")
        if len(f) != 9:
            continue
        cid, name, image, started, project, runtime, devreq, policy, stdin = f
        project = "" if project == "<no value>" else project
        name = name.lstrip("/")
        try:
            st = _parse_ts(started)
        except ValueError:
            st = None
        is_gpu = runtime == "nvidia" or devreq == "1"
        client = _find_client(ps, name, image, now - (st or now), used)
        owner, session = (None, None)
        if client:
            used.add(client)
            owner, session = _owner_chain(ps, client)
        elif other_ps:
            c2 = _find_client(other_ps[1], name, image, -1e9, set())
            if c2:
                owner = _owner_chain(other_ps[1], c2)[0] + f" (on {other_ps[0]})"
        if not owner:
            owner = (f"compose · {project}" if project else
                     f"docker · restart {policy}" if policy in ("always", "unless-stopped") else "detached")
        ram, usec = cg.get(cid, (None, None))
        idle, idle_ge = _cpu_idle(node, cid, usec, now) if usec is not None else (None, False)
        sess_idle = session_idle_s(session) if session and NODES.get(node) is None else None
        kind = "gpu" if is_gpu else "session" if session else "service"
        rows.append({"node": node, "name": name, "image": image, "started": st, "project": project or None,
                     "ram_mb": round(ram / 1048576) if ram is not None else None,
                     "gpu_mb": gpu.get(cid), "owner": owner, "kind": kind,
                     "session_idle_s": round(sess_idle) if sess_idle is not None else None,
                     "cpu_idle_s": round(idle) if idle is not None else None, "cpu_idle_ge": idle_ge,
                     "stoppable": not is_gpu})
    live = {(node, line.split("|")[0]) for line in raw["I"]}
    for k in [k for k in _CT_CPU if k[0] == node and k not in live]:
        del _CT_CPU[k]                                # vanished containers
    return rows


def containers_state(force=False):
    """Running containers on every node, biggest first; cached CT_TTL seconds."""
    with _CT_LOCK:
        if force or time.time() - _CT["at"] > CT_TTL:
            raw, errs = {}, {}

            def grab(node, host):
                try:
                    raw[node] = _ct_probe(host)
                except (OSError, subprocess.SubprocessError) as e:
                    errs[node] = str(e)[:200]
            ts = [threading.Thread(target=grab, args=(n, h)) for n, h in NODES.items()]
            [t.start() for t in ts]
            [t.join() for t in ts]
            now, rows = time.time(), []
            for node in NODES:
                if node not in raw:
                    rows += [r for r in ((_CT["data"] or {}).get("containers") or []) if r["node"] == node]
                    continue
                other = next(((n, _ps_table(raw[n]["P"])) for n in raw if n != node), None)
                rows += _ct_rows(node, raw[node], other, now)
            heads = {r["name"][:-5]: r["owner"] for r in rows if r["name"].endswith("-head")}
            for r in rows:     # a TP worker is started detached over ssh: it belongs to its head's launcher
                if r["name"].endswith("-worker") and r["owner"] == "detached" and r["name"][:-7] in heads:
                    r["owner"] = heads[r["name"][:-7]] + " (worker)"
            rows.sort(key=lambda r: -((r["ram_mb"] or 0) + (r["gpu_mb"] or 0)))
            _CT["data"], _CT["at"] = {"containers": rows, "errors": errs}, now
        return dict(_CT["data"], at=_CT["at"])


def ct_worker():
    while True:
        try:
            containers_state(force=True)
        except Exception as e:          # never let the clock thread die
            print(f"containers probe: {e}", flush=True)
        time.sleep(CT_POLL)


def container_stop(node, name, key):
    """docker stop one container. Needs the Starfleet key. GPU containers are model servers that
    llama-swap manages: they are refused here and are unloaded from the Model Registry instead."""
    key_check(key)                                   # 403 without a valid key
    if node not in NODES:
        raise ValueError("unknown node")
    row = next((r for r in containers_state(force=True)["containers"]
                if r["node"] == node and r["name"] == name), None)
    if row is None:
        raise ValueError(f"{name} is not running on {node}")
    if not row["stoppable"]:
        raise PermissionError("Model server: unload it in the Model Registry, not here.")
    script = "docker stop -t 10 " + shlex.quote(name)
    host = NODES[node]
    cmd = ["bash", "-c", script] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, script]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise OSError((r.stderr or f"docker stop exit {r.returncode}").strip()[-200:])
    _CT["at"] = 0.0
    return {"ok": True, "node": node, "name": name, "freed_mb": row["ram_mb"]}


# ---------------------------------------------------------------- Memory Alpha (off-site backup)

# Kopia on the NAS backs the NAS volume up to the object storage bucket "memory-alpha" (object lock,
# compliance mode). Fleet holds no Kopia or bucket credentials: it reads the status lines the
# kopia-check container logs every 6 h (the same lines the kopia-watch alerting reads), the init
# container's policy output and the container states, all through read-only verbs of the NAS gate.
# "Back up now" starts the one-shot kopia-snapshot container with the gate's `control start`.
MA_TTL = 20.0
MA_SNAP_H, MA_MAINT_H = 30, 48          # same thresholds as kopia-watch
_MA = {"at": 0.0, "data": None}
_MA_POLICY = {"at": 0.0, "data": None}
_MA_LOCK = threading.Lock()


def _gate_logs(name, tail):
    r = _nas_gate("logs", name, "--tail", str(tail))
    if not isinstance(r, dict) or "logs" not in r:
        r = r if isinstance(r, dict) else {}
        raise OSError(r.get("message") or r.get("error") or "no logs")
    return r["logs"]


def _tagged(logs, tag):
    """[(timestamp, payload)] of the lines '<ts> TAG payload', oldest first."""
    out = []
    for line in logs.splitlines():
        ts, _, rest = line.partition(" ")
        if rest.startswith(tag + " "):
            out.append((ts, rest[len(tag) + 1:]))
    return out


def _iso(ts):
    return datetime.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", ts).replace("Z", "+00:00")).timestamp()


def _snap_summary(payload):
    j = json.loads(payload)
    sn = j[-1] if isinstance(j, list) else j
    st = sn.get("stats") or {}
    return {"start": _iso(sn["startTime"]), "end": _iso(sn["endTime"]), "size": st.get("totalSize"),
            "files": st.get("fileCount"), "dirs": st.get("dirCount"), "errors": st.get("errorCount", 0),
            "incomplete": sn.get("incompleteReason") or ("incomplete" if sn.get("incomplete") else None),
            "description": sn.get("description") or ""}


_UNITS = {"": 1, "B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15,
          "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}


def _blob_summary(payload):
    """`kopia blob stats` output (one line): blob count and total bytes in the bucket."""
    c = re.search(r"Count:\s*([\d,]+)", payload)
    t = re.search(r"Total:\s*([\d.,]+)\s*([KMGTP]?i?B)?", payload, re.I)
    if not t:
        return None
    return {"count": int(c.group(1).replace(",", "")) if c else None,
            "bytes": int(float(t.group(1).replace(",", "")) * _UNITS.get((t.group(2) or "").upper(), 1))}


def _ma_policy():
    """Snapshot times and retention as kopia-init last applied them; re-read every 10 minutes."""
    if time.time() - _MA_POLICY["at"] > 600 or _MA_POLICY["data"] is None:
        logs, pol = _gate_logs("kopia-kopia-init-1", 80), {"times": [], "keep": {}}
        for line in logs.splitlines():
            m = re.search(r"setting snapshot times to \[([^\]]*)\]", line)
            if m:
                pol["times"] = [t.strip() for t in m.group(1).split(",") if t.strip()]
            m = re.search(r'setting "number of (\w+) backups to keep" to (\d+)', line)
            if m:
                pol["keep"][m.group(1)] = int(m.group(2))
        _MA_POLICY["data"], _MA_POLICY["at"] = pol, time.time()
    return _MA_POLICY["data"]


def _next_time(times, now):
    """Next local occurrence of any 'H:MM' in times, as epoch seconds."""
    best = None
    for t in times:
        try:
            h, m = (int(x) for x in t.split(":"))
        except ValueError:
            continue
        d = datetime.datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
        if d.timestamp() <= now:
            d += datetime.timedelta(days=1)
        best = min(best or d.timestamp(), d.timestamp())
    return best


def memory_alpha_state(force=False):
    with _MA_LOCK:
        if not force and _MA["data"] and time.time() - _MA["at"] < MA_TTL:
            return dict(_MA["data"], at=_MA["at"])
        now, errors, d = time.time(), [], {}
        try:
            ps = _nas_gate("ps", "--all", "--project", "kopia")
            if isinstance(ps, dict):
                raise OSError(ps.get("message") or ps.get("error") or str(ps)[:200])
            d["containers"] = [{"name": c["Names"], "state": c["State"], "status": c["Status"]} for c in ps]
            logs = _gate_logs("kopia-check", 8)
            snaps, maint, blob = _tagged(logs, "SNAP"), _tagged(logs, "MAINT"), _tagged(logs, "BLOB")
            d["checked"] = _iso(snaps[-1][0]) if snaps else None
            try:
                d["last"] = _snap_summary(snaps[-1][1]) if snaps else None
            except (ValueError, KeyError, IndexError):
                d["last"], _ = None, errors.append("snapshot status unreadable: " + snaps[-1][1][:160])
            d["stored"] = _blob_summary(blob[-1][1]) if blob else None
            if maint:
                m = maint[-1][1]
                nf = re.search(r'"nextFullMaintenance":"([^"]+)"', m)
                nq = re.search(r'"nextQuickMaintenance":"([^"]+)"', m)
                d["maintenance"] = {"locks": '"extendObjectLocks":true' in m,
                                    "next_full": _iso(nf.group(1)) if nf else None,
                                    "next_quick": _iso(nq.group(1)) if nq else None}
            d["policy"] = _ma_policy()
            d["next_snapshot"] = _next_time(d["policy"]["times"], now)
            names = {c["name"]: c for c in d["containers"]}
            d["trigger_ready"] = "kopia-snapshot" in names
            if d["trigger_ready"]:
                ml = _gate_logs("kopia-snapshot", 6)
                man = {"state": names["kopia-snapshot"]["state"]}
                st, sn, er, ex = (_tagged(ml, t) for t in ("START", "SNAPNOW", "SNAPERR", "EXIT"))
                man["started"] = _iso(st[-1][1].strip()) if st else None
                man["exit"] = int(ex[-1][1]) if ex and ex[-1][1].strip().lstrip("-").isdigit() else None
                man["error"] = er[-1][1][:300] if er else None
                try:
                    man["snapshot"] = _snap_summary(sn[-1][1]) if sn else None
                except (ValueError, KeyError, IndexError):
                    man["snapshot"] = None
                d["manual"] = man
                if man["snapshot"] and (not d.get("last") or man["snapshot"]["end"] > d["last"]["end"]):
                    d["last"] = man["snapshot"]                  # a manual run newer than kopia-check's line
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(str(e)[:200])
        problems = []
        last = d.get("last")
        if last:
            if (now - last["end"]) / 3600 > MA_SNAP_H:
                problems.append("last snapshot %d h ago" % ((now - last["end"]) / 3600))
            if last.get("incomplete"):
                problems.append("last snapshot incomplete: %s" % last["incomplete"])
            if last.get("errors"):
                problems.append("%d errors in the last snapshot" % last["errors"])
        mt = d.get("maintenance")
        if mt:
            if not mt["locks"]:
                problems.append("object lock renewal is off")
            if mt["next_full"] and (now - mt["next_full"]) / 3600 + 24 > MA_MAINT_H:
                problems.append("full maintenance (lock renewal) last ran over %d h ago" % MA_MAINT_H)
        d["problems"], d["errors"] = problems, errors
        d["status"] = "error" if errors and not last else "warn" if problems or errors else "ok"
        _MA["data"], _MA["at"] = d, now
        return dict(d, at=now)


def memory_alpha_backup(key):
    """Start a snapshot now. Needs the Starfleet key and the kopia-snapshot one-shot container."""
    key_check(key)
    st = memory_alpha_state(force=True)
    if not st.get("trigger_ready"):
        raise PermissionError("kopia-snapshot is not deployed yet: rebuild the kopia project once in "
                              "DSM Container Manager.")
    if (st.get("manual") or {}).get("state") == "running":
        raise PermissionError("A snapshot is already running.")
    res = _nas_gate("control", "kopia-snapshot", "start", timeout=90)
    if isinstance(res, dict) and res.get("error"):
        raise OSError(res.get("message") or res["error"])
    _MA["at"] = 0.0
    return {"ok": True}


# ---------------------------------------------------------------- Defiant (web server)

# The web server, reached over ssh (BatchMode, key only). The host comes from the service environment
# (FLEET_DEFIANT_HOST, e.g. a systemd drop-in) so no address lives in the code; unset = view disabled.
# One read-only probe: host figures, CPU and network over one second, disk, pending updates, reboot
# flag, containers and the Caddyfile's site blocks. The sites themselves are checked from here over
# HTTPS: status, response time and certificate expiry.
DEFIANT_HOST = os.environ.get("FLEET_DEFIANT_HOST", "").strip()
DEF_TTL = 20.0
_DEF = {"at": 0.0, "data": None}
_DEF_LOCK = threading.Lock()
DEF_PROBE = r"""echo "H:$(hostname)"; echo "UP:$(cut -d' ' -f1 /proc/uptime)"; echo "LOAD:$(cut -d' ' -f1-3 /proc/loadavg)"
echo "CPUS:$(nproc)"; . /etc/os-release; echo "OS:$PRETTY_NAME"; echo "KERNEL:$(uname -r)"
awk '/^MemTotal|^MemAvailable/{print "MEM:" $1 $2}' /proc/meminfo
dev=$(ip -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')
c1=$(head -1 /proc/stat); n1=$(grep " $dev:" /proc/net/dev); sleep 1; c2=$(head -1 /proc/stat); n2=$(grep " $dev:" /proc/net/dev)
echo "CPU1:$c1"; echo "CPU2:$c2"; echo "NET1:$n1"; echo "NET2:$n2"
echo "DISK:$(df -B1 --output=size,used,avail / | tail -1)"
echo "REBOOT:$([ -f /var/run/reboot-required ] && echo yes || echo no)"
echo "UPD:$(apt list --upgradable 2>/dev/null | grep -c upgradable)"
docker ps -a --format 'CTR:{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null
grep -h -E '^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}([ ,]|$).*\{[[:space:]]*$' /srv/caddy/Caddyfile 2>/dev/null | sed 's/[ ,{].*//' | sed 's/^/SITE:/'
for s in /srv/sites/*/index.html; do [ -f "$s" ] && echo "DEPLOY:$(basename "$(dirname "$s")")|$(stat -c %Y "$s")"; done"""


def _site_check(domain):
    """HTTPS GET of the site's root: status, milliseconds, certificate days left."""
    import ssl
    import socket
    r = {"domain": domain, "status": None, "ms": None, "cert_days": None, "error": None}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                exp = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
                r["cert_days"] = int((exp - time.time()) // 86400)
        t0 = time.time()
        req = urllib.request.Request(f"https://{domain}/", method="GET",
                                     headers={"User-Agent": "starfleet-fleet-status"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                r["status"] = resp.status
        except urllib.error.HTTPError as e:
            r["status"] = e.code
        r["ms"] = round((time.time() - t0) * 1000)
    except (OSError, ssl.SSLError, ValueError) as e:
        r["error"] = str(e)[:120]
    return r


def _cpu_pct(a, b):
    x, y = [int(v) for v in a.split()[1:9]], [int(v) for v in b.split()[1:9]]
    idle, total = (y[3] + y[4]) - (x[3] + x[4]), sum(y) - sum(x)
    return round(100 * (1 - idle / total), 1) if total > 0 else None


def defiant_state(force=False):
    with _DEF_LOCK:
        if not force and _DEF["data"] and time.time() - _DEF["at"] < DEF_TTL:
            return dict(_DEF["data"], at=_DEF["at"])
        if not DEFIANT_HOST:
            return {"configured": False, "status": "off", "at": time.time()}
        d, errors, now = {"configured": True, "containers": [], "sites": []}, [], time.time()
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", DEFIANT_HOST, DEF_PROBE],
                               capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
            if "H:" not in r.stdout:
                raise OSError((r.stderr or f"ssh exit {r.returncode}").strip()[-200:])
            kv, sites, deploys = {}, [], {}
            for line in r.stdout.splitlines():
                k, _, v = line.partition(":")
                if k == "CTR":
                    n, stt, sts = (v.split("|") + ["", ""])[:3]
                    d["containers"].append({"name": n, "state": stt, "status": sts})
                elif k == "SITE":
                    if not v.startswith("www.") and v not in sites:
                        sites.append(v)
                elif k == "DEPLOY":
                    n, _, ts = v.partition("|")
                    deploys[n] = int(ts) if ts.isdigit() else None
                elif k == "MEM":
                    kv[v.split(":")[0]] = int(re.sub(r"\D", "", v.split(":", 1)[1]))
                else:
                    kv[k] = v.strip()
            mem_t, mem_a = kv.get("MemTotal"), kv.get("MemAvailable")
            disk = [int(x) for x in kv.get("DISK", "0 0 0").split()]
            n1, n2 = kv.get("NET1", "").split(":", 1)[-1].split(), kv.get("NET2", "").split(":", 1)[-1].split()
            d.update({"host": kv.get("H"), "os": kv.get("OS"), "kernel": kv.get("KERNEL"),
                      "uptime_s": float(kv.get("UP") or 0), "load": kv.get("LOAD"), "cpus": int(kv.get("CPUS") or 0),
                      "cpu_pct": _cpu_pct(kv["CPU1"], kv["CPU2"]) if "CPU1" in kv else None,
                      "mem_total_kb": mem_t, "mem_avail_kb": mem_a,
                      "disk_total": disk[0], "disk_used": disk[1], "disk_avail": disk[2],
                      "rx_bps": int(n2[0]) - int(n1[0]) if len(n1) > 8 and len(n2) > 8 else None,
                      "tx_bps": int(n2[8]) - int(n1[8]) if len(n1) > 8 and len(n2) > 8 else None,
                      "reboot_required": kv.get("REBOOT") == "yes", "updates": int(kv.get("UPD") or 0)})
            checks = {}
            ts = [threading.Thread(target=lambda dm=dm: checks.__setitem__(dm, _site_check(dm))) for dm in sites]
            [t.start() for t in ts]
            [t.join() for t in ts]
            d["sites"] = [dict(checks.get(dm) or {"domain": dm}, deployed=deploys.get(dm)) for dm in sites]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
            errors.append(str(e)[:200])
        problems = []
        for st in d["sites"]:
            if st.get("error") or not st.get("status") or (st["status"] >= 400 and st["status"] != 401):   # 401 = password-protected on purpose
                problems.append(f"{st['domain']}: {st.get('error') or st.get('status')}")
            elif st.get("cert_days") is not None and st["cert_days"] < 14:
                problems.append(f"{st['domain']}: certificate expires in {st['cert_days']} days")
        if d.get("disk_total") and d["disk_used"] / d["disk_total"] > 0.85:
            problems.append("disk over 85 % full")
        for c in d["containers"]:
            if c["state"] != "running":
                problems.append(f"container {c['name']} is {c['state']}")
        d["problems"], d["errors"] = problems, errors
        d["status"] = "error" if errors else "warn" if problems else "ok"
        _DEF["data"], _DEF["at"] = d, now
        return dict(d, at=now)


# ---------------------------------------------------------------- Computer (knowledge chat)

# A chat for knowledge work against the model that is ALREADY resident: Computer never loads a model.
# Requests go to llama-swap through ctxproxy WITHOUT the Starfleet key, so even a race with an unload
# ends in the gate's 403, never in a load. Tools come from MCP servers (wiki read tools, vane, google
# search). Wiki content must not reach the internet: web tools stay available after a wiki read, but every
# web call passes web_guard() first, which refuses secrets, customer-specific terms, personal data and pasted
# text (see below). Every /api/computer call needs the Starfleet key; chats stay on this host.
COMPUTER_DIR = os.path.expanduser(os.environ.get("FLEET_COMPUTER_DIR", "~/.local/share/fleet-ui/computer"))
ASR_GATE = os.path.expanduser(os.environ.get("FLEET_ASR_GATE", "~/.whatsapp-bridge/asr_gate.sh"))
ENV_FILE = os.path.expanduser("~/.config/opencode/.env")
COMPUTER_MCP = {      # name -> how to reach it; tools = the allowed subset (None = all); web = talks to the internet
    "wiki": {"http": "http://127.0.0.1:18081/mcp", "token_env": "WIKI_MCP_TOKEN", "web": False,
             "link_rewrite": ("http://127.0.0.1:18081", os.environ.get("FLEET_WIKI_PUBLIC_URL", "")),
             "tools": {"wiki_overview", "wiki_related", "wiki_search", "wiki_read"}},
    "vane": {"cmd": ["node", os.path.expanduser("~/.local/share/opencode/vane-mcp-server/dist/server.js")],
             "web": True, "tools": {"vane_search", "vane_deep_research"}},
    "google": {"cmd": ["node", os.path.expanduser("~/google-search-mcp/dist/server.js")], "web": True, "tools": None},
}
try:                  # optional override file, same shape as COMPUTER_MCP
    with open(os.path.expanduser("~/.config/fleet-ui/computer-mcp.json")) as _f:
        COMPUTER_MCP = {k: dict(v, tools=set(v["tools"]) if v.get("tools") else None) for k, v in json.load(_f).items()}
except (OSError, ValueError):
    pass
COMPUTER_ROUNDS = 8           # model <-> tool round trips per message
TOOL_TEXT_CAP = 12000         # characters of one tool result passed to the model
HISTORY_CHARS = 240000        # characters of history sent with a request (oldest turns drop first)
_NOT_CHAT = re.compile(r"embed|rerank|imagegen", re.I)


def _deny(msg=POLICY):
    raise urllib.error.HTTPError(SWAP, 403, msg, {}, io.BytesIO(json.dumps({"error": {"message": msg}}).encode()))


def computer_auth(key):
    """Fail closed: the key counts only when ctxproxy's own check (GET /starfleet/check, answered before its
    guard and without llama-swap) says 204. Anything else, including no answer, rejects. No cache: the check
    is a local call, and a rotated key takes effect at once."""
    if not key:
        _deny()
    try:
        req = urllib.request.Request(SWAP + "/starfleet/check", headers={KEY_HDR: key}, method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status == 204:
                return
    except urllib.error.HTTPError as e:
        if e.code == 403:
            _deny()
    except OSError:
        pass
    _deny("Cannot verify the Starfleet key right now (gate not answering); try again in a moment.")


def _env_value(name):
    """One variable from the opencode env file (shell syntax, quoted or not), never the whole file."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        with open(ENV_FILE) as f:
            for line in f:
                m = re.match(r"\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*)$", line)
                if m:
                    v = m.group(1).strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                        v = v[1:-1]
                    return v
    except OSError:
        pass
    return ""


# ---- web guard: every argument of a web tool call (vane, google) is checked before it leaves this host.
# Refused, with the reason given back to the model so it can rephrase: a secret (a value of a KEY/TOKEN/
# SECRET/PASSWORD variable in the env file or Fleet's own env, or a known key format), a customer-specific
# term (built-in names, wiki entity pages tagged customer/* that are people, organisations, projects or
# internal systems, plus web-guard.json "block"), personal data (email, phone, IBAN), or long pasted text.
# Public products tagged customer/* (Qdrant, Azure OpenAI, ...) are deliberately NOT blocked: their subtype
# is product/tool, and searching for public tech is the point. web-guard.json "allow" un-blocks a term.
WEB_GUARD_FILE = os.path.expanduser("~/.config/fleet-ui/web-guard.json")   # {"block": [...], "allow": [...]}
WIKI_DIR = os.path.expanduser(os.environ.get("FLEET_WIKI_DIR", "~/llm-wiki/wiki"))
WEB_ARG_MAX = 300             # characters per argument: a query, not a pasted page
_CUSTOMER_TERMS = ()          # built-in terms: none in code; this host's list lives in web-guard.json "block"
_CUSTOMER_SUBTYPES = {"organization", "person", "project", "program", "initiative", "system", "data-system",
                      "business-domain"}
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASS|PAT\b|_PAT|AUTH|CREDENTIAL|PRIVATE|CODE", re.I)
_SECRET_RE = re.compile(
    r"\bsk-[a-z0-9_-]{16,}|\b(?:ghp|gho|ghs|ghu|github_pat)_[a-z0-9_]{20,}|\bglpat-[a-z0-9_-]{16,}"
    r"|\bxox[abprs]-[a-z0-9-]{10,}|\bakia[0-9a-z]{16}\b|\baiza[0-9a-z_-]{30,}|\beyj[a-z0-9_-]{8,}\.eyj"
    r"|-----begin [a-z ]*private key|\bbearer\s+[a-z0-9._~+/-]{16,}"
    r"|\b(?:password|passwort|passwd|pwd|secret|token|api[-_ ]?key)\s*[:=]\s*\S+", re.I)
# A random-looking token: 24+ characters mixing upper case, lower case and digits (a hex hash is not one).
_TOKEN_RE = re.compile(r"(?<![\w+/=-])(?=[\w+/=-]*[a-z])(?=[\w+/=-]*[A-Z])(?=[\w+/=-]*\d)[\w+/=-]{24,}")
_PII_RE = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+|\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b"
                     r"|(?<![\w.])(?:\+|00)\d[\d ()/-]{7,}\d|(?<![\w.])0\d{2,5}[ /-]\d{3,}(?:[ -]\d+)*")
_WG = {"at": 0.0}
_WG_LOCK = threading.Lock()


def _wg_norm(s):
    return re.sub(r"[\s_-]+", " ", s.lower()).strip()


def _wg_lists():
    """Customer terms (one regex) and secret values, rebuilt at most every 5 minutes. Values stay in memory."""
    with _WG_LOCK:
        if time.time() - _WG["at"] < 300:
            return _WG
        terms, allow = set(_CUSTOMER_TERMS), set()
        try:
            with open(WEB_GUARD_FILE) as f:
                cfg = json.load(f)
            terms |= {_wg_norm(t) for t in cfg.get("block", [])}
            allow = {_wg_norm(t) for t in cfg.get("allow", [])}
        except (OSError, ValueError, AttributeError):
            pass
        for p in glob.glob(os.path.join(WIKI_DIR, "entities", "*.md")):
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    m = re.match(r"---\n(.*?)\n---", f.read(6000), re.S)
            except OSError:
                continue
            fm = m.group(1) if m else ""
            st = re.search(r"^subtype:\s*(\S+)", fm, re.M)
            if "customer/" not in fm or not st or st.group(1) not in _CUSTOMER_SUBTYPES:
                continue
            names = [os.path.basename(p)[:-3]] + re.findall(r'^title:\s*"?([^"\n]+?)"?\s*$', fm, re.M)
            al = re.search(r"^aliases:\s*\n((?:[ \t]+-.*\n?)+)", fm, re.M)
            names += re.findall(r'-\s*"?([^"\n]+?)"?\s*$', al.group(1), re.M) if al else []
            for n in map(_wg_norm, names):
                n = re.sub(r"^\d+ ", "", n)                 # "01_Acme" -> "acme"
                person = st.group(1) == "person"
                if len(n) >= 3 and (" " in n or not person):     # people by full name only, never a first name
                    terms.add(n)
                    if person and n.count(" ") == 1:
                        terms.add(" ".join(reversed(n.split())))     # "miller thomas" -> "thomas miller"
        terms -= allow
        alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) or r"(?!)"   # empty: match nothing
        secrets = set()
        try:
            with open(ENV_FILE) as f:
                for line in f:
                    m = re.match(r"\s*(?:export\s+)?(\w+)\s*=\s*(.*)$", line)
                    if m and _SECRET_NAME.search(m.group(1)):
                        secrets.add(m.group(2).strip().strip("'\""))
        except OSError:
            pass
        secrets |= {v for k, v in os.environ.items() if _SECRET_NAME.search(k)}
        _WG.update(at=time.time(), terms=re.compile(r"(?<!\w)(?:" + alt + r")(?!\w)"),
                   secrets=[s for s in secrets if len(s) >= 8])
        return _WG


def web_guard(args):
    """None when a web tool call may leave this host, else why not. Never echoes a matched secret."""
    texts = []

    def walk(v):
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(args)
    lists = _wg_lists()
    for t in texts:
        if len(t) > WEB_ARG_MAX:
            return f"an argument is longer than {WEB_ARG_MAX} characters; send a short query, never pasted text"
        if any(s in t for s in lists["secrets"]) or _SECRET_RE.search(t) or _TOKEN_RE.search(t):
            return "it contains a secret (API key, token or password)"
        if _PII_RE.search(t):
            return "it contains personal data (email address, phone number or IBAN)"
        m = lists["terms"].search(_wg_norm(t))
        if m:
            return f"it contains the customer-specific term \"{m.group(0)}\""
    return None


def _child_env():
    """Environment for MCP children: an allowlist. The servers load their own keys from their own files."""
    return {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "USER", "TMPDIR", "XDG_RUNTIME_DIR",
                                       "DOCKER_HOST") if k in os.environ}


class _StdioMCP:
    """Minimal MCP client over stdio (newline-delimited JSON-RPC). One long-lived child per server,
    requests serialised by a lock; a dead child is restarted on the next call."""

    def __init__(self, name, cmd):
        self.name, self.cmd, self.p, self.q, self.n = name, cmd, None, None, 0
        self.lock = threading.Lock()

    def _start(self):
        import queue
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1, env=_child_env())
        self.q = queue.Queue()

        def reader(p, q):
            for line in p.stdout:
                try:
                    q.put(json.loads(line))
                except ValueError:
                    pass
            q.put(None)
        threading.Thread(target=reader, args=(self.p, self.q), daemon=True).start()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "starfleet-computer", "version": "1"}}, 60)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _rpc(self, method, params, timeout):
        import queue
        self.n += 1
        i = self.n
        self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        end = time.time() + timeout
        while True:
            try:
                msg = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                self._kill()
                raise OSError(f"{self.name}: no answer to {method} within {timeout} s")
            if msg is None:
                self.p = None
                raise OSError(f"{self.name}: server exited")
            if msg.get("id") == i:
                if "error" in msg:
                    raise OSError(f"{self.name}: {str(msg['error'].get('message'))[:200]}")
                return msg.get("result") or {}

    def _kill(self):
        if self.p is not None:
            try:
                self.p.kill()
                self.p.wait(5)
            except (OSError, subprocess.SubprocessError):
                pass
        self.p = None

    def call(self, method, params, timeout=120):
        with self.lock:
            if self.p is None or self.p.poll() is not None:
                try:
                    self._start()
                except (OSError, subprocess.SubprocessError, ValueError):
                    self._kill()
                    raise
            return self._rpc(method, params, timeout)


class _HttpMCP:
    """Minimal MCP client over Streamable HTTP (JSON or SSE answers, session id header)."""

    def __init__(self, name, url, token_env):
        self.name, self.url, self.token_env, self.sid, self.n = name, url, token_env, None, 0
        self.lock = threading.Lock()

    def _post(self, obj, timeout):
        hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
        tok = _env_value(self.token_env) if self.token_env else ""
        if tok:
            hdr["Authorization"] = "Bearer " + tok
        if self.sid:
            hdr["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(obj).encode(), headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            ctype, body = r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
        if "id" not in obj:
            return None
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if msg.get("id") == obj["id"]:
                        return msg
            raise OSError(f"{self.name}: no answer in the event stream")
        return json.loads(body) if body.strip() else None

    def _rpc(self, method, params, timeout):
        self.n += 1
        msg = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}, timeout)
        if msg and "error" in msg:
            raise OSError(f"{self.name}: {str(msg['error'].get('message'))[:200]}")
        return (msg or {}).get("result") or {}

    def _init(self):
        self.sid = None
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "starfleet-computer", "version": "1"}}, 30)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, 30)

    def call(self, method, params, timeout=120):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if not self.sid:
                        self._init()
                    return self._rpc(method, params, timeout)
                except urllib.error.HTTPError as e:
                    if e.code in (400, 404) and attempt == 0:      # session expired: initialise once more
                        self.sid = None
                        continue
                    raise OSError(f"{self.name}: HTTP {e.code}")
                except urllib.error.URLError as e:
                    raise OSError(f"{self.name}: {e.reason}")


_MCP = {}
_MCP_TOOLS = {"at": 0.0, "tools": {}, "errors": {}}    # tool name -> (server, OpenAI tool spec)
_MCP_LOCK = threading.Lock()


def _mcp(name):
    with _MCP_LOCK:
        if name not in _MCP:
            c = COMPUTER_MCP[name]
            _MCP[name] = _HttpMCP(name, c["http"], c.get("token_env")) if c.get("http") else _StdioMCP(name, c["cmd"])
        return _MCP[name]


def computer_tools(force=False):
    """The allowed tools of every configured MCP server as OpenAI function specs; re-listed every 10 min."""
    if not force and _MCP_TOOLS["tools"] and time.time() - _MCP_TOOLS["at"] < 600:
        return _MCP_TOOLS
    tools, errors = {}, {}
    for name, c in COMPUTER_MCP.items():
        try:
            for t in _mcp(name).call("tools/list", {}, 60).get("tools", []):
                if c.get("tools") is None or t["name"] in c["tools"]:
                    tools[t["name"]] = (name, {"type": "function", "function": {
                        "name": t["name"], "description": (t.get("description") or "")[:1500],
                        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
            errors[name] = str(e)[:200]
    _MCP_TOOLS.update(at=time.time(), tools=tools, errors=errors)
    return _MCP_TOOLS


def _tool_call(name, args):
    """Run one tool: (text for the model, [image data URLs for the screen])."""
    server = _MCP_TOOLS["tools"][name][0]
    timeout = 900 if "deep" in name else 180
    res = _mcp(server).call("tools/call", {"name": name, "arguments": args}, timeout)
    texts, images = [], []
    for part in res.get("content") or []:
        if part.get("type") == "text":
            texts.append(part.get("text") or "")
        elif part.get("type") == "image" and part.get("data"):
            images.append(f"data:{part.get('mimeType') or 'image/png'};base64,{part['data']}")
    text = "\n".join(texts) or ("(no text)" if not images else "(image)")
    link = COMPUTER_MCP[server].get("link_rewrite")     # local-only URLs in results -> addresses the browser can open
    if link and link[1]:
        text = text.replace(link[0], link[1])
    if res.get("isError"):
        text = "TOOL ERROR: " + text
    return text[:TOOL_TEXT_CAP] + (" [truncated]" if len(text) > TOOL_TEXT_CAP else ""), images


def computer_models():
    """Resident models that can chat (no embedder, reranker or image stack), with their vision flag."""
    with LOCK:
        running = [r.get("model") for r in STATE.get("running", []) if r.get("state") in ("ready", None)]
    with _ctx_max_lock:
        visions = dict(_ctx_vision)
    return [{"id": m, "vision": visions.get(m) == "on"} for m in running if m and not _NOT_CHAT.search(m)]


COMPUTER_SYSTEM = (
    "You are Computer, the knowledge assistant of Starfleet Command, running on the user's own hardware. "
    "Answer in the language of the user's message. Be precise and concise; use markdown: headings, lists, "
    "tables and fenced code blocks with a language. "
    "Tools: wiki_* search and read the user's private knowledge wiki (start with wiki_search or wiki_overview, "
    "read the relevant pages, cite them as markdown links with the URL the tool gives). vane_* and google_* "
    "search the public internet: use them for current or public facts, also after reading the wiki. Never put "
    "private or customer information from the conversation or the wiki into a web query: no customer, company "
    "or people names, no internal project names, no secrets, no copied text. Search for the generic, public "
    "topic instead. A guard refuses such queries and says why; then rephrase generically. "
    "Say which sources you used. "
    "If you do not know, say so instead of guessing.")


def _chat_path(cid):
    if not re.fullmatch(r"[a-f0-9]{16}", cid or ""):
        raise ValueError("bad chat id")
    return os.path.join(COMPUTER_DIR, cid + ".json")


def computer_chat_load(cid):
    with open(_chat_path(cid), encoding="utf-8") as f:
        return json.load(f)


def computer_chat_save(chat):
    import tempfile
    os.makedirs(COMPUTER_DIR, mode=0o700, exist_ok=True)
    p = _chat_path(chat["id"])
    fd, tmp = tempfile.mkstemp(dir=COMPUTER_DIR, suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(chat, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


_CHAT_BUSY = set()           # chat ids with a message in flight: a second send on the same chat is refused
_CHAT_BUSY_LOCK = threading.Lock()


def computer_chats():
    out = []
    for p in glob.glob(os.path.join(COMPUTER_DIR, "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                c = json.load(f)
            out.append({"id": c["id"], "title": c.get("title") or "Untitled", "updated": c.get("updated", 0),
                        "model": c.get("model")})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(out, key=lambda c: -c["updated"])


def _history(chat, vision=True):
    """OpenAI messages for the model: system prompt + newest turns that fit HISTORY_CHARS."""
    msgs, total = [], 0
    last_user = max((i for i, m in enumerate(chat["messages"]) if m["role"] == "user"), default=-1)
    for idx, m in reversed(list(enumerate(chat["messages"]))):
        if isinstance(m.get("content"), list) and (idx != last_user or not vision):   # images only in the newest turn
            m = dict(m, content=" ".join(p.get("text", "[image]") if p.get("type") == "text" else "[image]"
                                         for p in m["content"]))
        size = len(json.dumps(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or ""))
        if total + size > HISTORY_CHARS and msgs:
            break
        msgs.append({k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id", "name") if k in m})
        total += size
    msgs.reverse()
    while msgs and msgs[0]["role"] == "tool":        # never start with an orphaned tool result
        msgs.pop(0)
    return [{"role": "system", "content": COMPUTER_SYSTEM}] + msgs


def _stream_completion(model, messages, tools, emit):
    """One streamed completion from llama-swap (no Starfleet key: a non-resident model is refused, never
    loaded). Returns (content, reasoning, tool_calls)."""
    body = {"model": model, "messages": messages, "stream": True, "max_tokens": 8192}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(SWAP + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    content, reasoning, calls, finished, reason = [], [], {}, False, None
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                finished = True
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("error"):
                raise OSError("model error: " + str(chunk["error"])[:200])
            choice = (chunk.get("choices") or [{}])[0]
            reason = choice.get("finish_reason") or reason
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
                emit({"type": "reasoning", "text": delta["reasoning_content"]})
            if delta.get("content"):
                content.append(delta["content"])
                emit({"type": "token", "text": delta["content"]})
            for tc in delta.get("tool_calls") or []:
                c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                c["id"] = tc.get("id") or c["id"]
                fn = tc.get("function") or {}
                c["name"] += fn.get("name") or ""
                c["arguments"] += fn.get("arguments") or ""
    if not finished:
        raise OSError("the model stream ended early (model unloaded?); this answer is incomplete")
    if reason == "length" and calls:
        raise OSError("the answer hit the token limit in the middle of a tool call; no tool was run")
    return "".join(content), "".join(reasoning), [calls[i] for i in sorted(calls)]


COMPUTER_TOOLS_PER_ROUND = 4      # tool calls run per model response; the rest are answered "skipped"
COMPUTER_DEADLINE = 1200           # seconds per message, all rounds and tools included


def computer_send(body, emit):
    """Handle one user message end to end, streaming events through emit(dict). The chat is saved even
    when the turn fails, with an error note."""
    if not isinstance(body, dict):
        raise ValueError("bad request")
    model, text, cid = body.get("model"), body.get("text") or "", body.get("id")
    tools_in, images_in = body.get("tools") or [], body.get("images") or []
    if not isinstance(model, str) or not isinstance(text, str) or not isinstance(tools_in, list) \
            or not isinstance(images_in, list) or (cid is not None and not isinstance(cid, str)):
        raise ValueError("bad request")
    text = text.strip()
    images = [u for u in images_in if isinstance(u, str) and u.startswith("data:image/")][:4]
    if not text and not images:
        raise ValueError("empty message")
    models = {m["id"]: m for m in computer_models()}
    if model not in models:
        raise PermissionError("That model is not loaded. Computer only uses resident models; "
                              "load one in the Model Registry first.")
    enabled = {t for t in tools_in if isinstance(t, str) and t in COMPUTER_MCP}
    if cid:
        chat = computer_chat_load(cid)
    else:
        import secrets
        chat = {"id": secrets.token_hex(8), "title": (text or "Image")[:60], "created": time.time(), "messages": []}
    with _CHAT_BUSY_LOCK:
        if chat["id"] in _CHAT_BUSY:
            raise PermissionError("This chat is still answering; wait for it to finish.")
        _CHAT_BUSY.add(chat["id"])
    try:
        _computer_turn(chat, model, models[model]["vision"], text, images, enabled, emit)
    except Exception as e:                       # keep what the turn produced, note the failure, re-raise
        chat["messages"].append({"role": "assistant", "content": f"(interrupted: {str(e)[:200]})",
                                 "error": True, "at": time.time()})
        raise
    finally:
        chat["updated"] = time.time()
        try:
            computer_chat_save(chat)
        finally:
            with _CHAT_BUSY_LOCK:
                _CHAT_BUSY.discard(chat["id"])
    emit({"type": "done", "id": chat["id"]})


def _computer_turn(chat, model, vision, text, images, enabled, emit):
    chat["model"] = model
    content = text if not images or not vision else (
        [{"type": "text", "text": text or "Describe the image."}] +
        [{"type": "image_url", "image_url": {"url": u}} for u in images])
    chat["messages"].append({"role": "user", "content": content, "at": time.time()})
    emit({"type": "chat", "id": chat["id"], "title": chat["title"], "wiki_used": bool(chat.get("wiki_used"))})
    reg = computer_tools()
    deadline = time.time() + COMPUTER_DEADLINE
    for _ in range(COMPUTER_ROUNDS):
        if time.time() > deadline:
            raise OSError("stopped: this message took longer than %d minutes" % (COMPUTER_DEADLINE // 60))
        if model not in {m["id"] for m in computer_models()}:
            raise PermissionError("The model was unloaded during the answer; Computer never reloads it.")
        specs = [spec for name, (srv, spec) in reg["tools"].items()
                 if srv in enabled]
        reply, reasoning, calls = _stream_completion(model, _history(chat, vision), specs, emit)
        msg = {"role": "assistant", "content": reply, "at": time.time()}
        if reasoning:
            msg["reasoning"] = reasoning
        if calls:
            msg["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                                  "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                 for i, c in enumerate(calls)]
        chat["messages"].append(msg)
        if not calls:
            return
        for n, tc in enumerate(msg["tool_calls"]):
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            emit({"type": "tool_call", "name": name, "args": args})
            srv = (reg["tools"].get(name) or (None,))[0]
            images_out, guarded = [], False
            if n >= COMPUTER_TOOLS_PER_ROUND:
                result = "Skipped: at most %d tool calls per step." % COMPUTER_TOOLS_PER_ROUND
            elif time.time() > deadline:
                result = "Skipped: the time limit for this message was reached."
            elif srv is None or srv not in enabled:
                result = f"Tool {name} is not available."
            elif COMPUTER_MCP[srv]["web"] and (why := web_guard(args)):
                guarded = True
                result = (f"Refused by the web guard: {why}. Nothing was sent. Search again with a short, generic "
                          "query about the public topic, without customer, company or people names, internal "
                          "project names, secrets or copied text.")
            else:
                try:
                    result, images_out = _tool_call(name, args)
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
                    result = f"TOOL ERROR: {str(e)[:300]}"
                if not COMPUTER_MCP[srv]["web"]:
                    chat["wiki_used"] = True
            emit({"type": "tool_result", "name": name, "text": result[:600], "images": images_out[:4],
                  "guarded": guarded})
            chat["messages"].append({"role": "tool", "tool_call_id": tc["id"], "name": name, "content": result,
                                     "images": images_out[:4], "at": time.time()})
    emit({"type": "error", "text": f"Stopped after {COMPUTER_ROUNDS} tool rounds."})


def computer_transcribe(wav):
    """Speech to text with whisper.cpp on the Sparks, through the same ASR gate the voice-memo bridge uses:
    it is memory-guarded and steps aside when a Spark is busy, so this tries every node in turn."""
    if not wav.startswith(b"RIFF"):
        raise ValueError("expected a WAV file")
    errors = []
    for node, host in NODES.items():
        cmd = [ASR_GATE] if host is None else [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, os.environ.get("FLEET_ASR_GATE_REMOTE",
                                                                                        "~/.whatsapp-bridge/asr_gate.sh")]
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
            try:
                out, _ = p.communicate(wav, timeout=300)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
                raise OSError("timed out")
            j = json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])
        except (ValueError, IndexError, OSError, subprocess.SubprocessError) as e:
            errors.append(f"{node}: {str(e)[:80]}")
            continue
        if j.get("busy"):
            errors.append(f"{node}: busy")
            continue
        return {"text": " ".join(s.get("text", "").strip() for s in j.get("segments") or []).strip(),
                "language": j.get("language"), "node": node}
    if all(e.endswith("busy") for e in errors):
        raise PermissionError("Both Sparks are too busy for transcription right now; try again shortly.")
    raise OSError("transcription failed: " + "; ".join(errors))


# ---------------------------------------------------------------- agent sessions (phone interface)
# What the Mac app reads over ssh, for a client that has no ssh: ~/bin/opencode-status.py and
# ~/bin/opencode-export-tail.py on each node (cluster/, deployed by sync-hosts.sh). Session titles and
# transcripts can hold confidential work, so both routes sit behind the Starfleet key (/api/computer/).

SESS_TTL = 5
SESS_TOOLS = ("opencode", "claude-code")
_SESS = {"at": 0.0, "data": {"nodes": {}, "errors": {}}}
_SESS_LOCK = threading.Lock()
_SESS_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _sess_script(host, name, args, timeout):
    """Run one of the status tools on a node and return its JSON. args must already be validated: over ssh
    they pass through the remote shell (quoted here), and the script path keeps its ~ for that shell."""
    cmd = [os.path.expanduser("~/bin/" + name), *args] if host is None else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
        " ".join(["~/bin/" + name] + [shlex.quote(a) for a in args])]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout)
    except subprocess.SubprocessError as e:
        raise OSError(str(e)[:200])
    except ValueError:
        raise OSError((r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")[-200:])


def sessions_state():
    """Coding-agent sessions on every node, cached SESS_TTL seconds. A node that fails keeps its last good
    list and reports the error next to it."""
    with _SESS_LOCK:
        if time.time() - _SESS["at"] > SESS_TTL:
            got = {}

            def one(node, host):
                try:
                    got[node] = _sess_script(host, "opencode-status.py", [], 20)
                except OSError as e:
                    got[node] = {"__err": str(e)}
            threads = [threading.Thread(target=one, args=nh) for nh in NODES.items()]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            nodes, errors = {}, {}
            for node in NODES:
                d = got.get(node)
                if not isinstance(d, dict) or "__err" in d:
                    errors[node] = (d or {}).get("__err", "no answer") if isinstance(d, dict) else "no answer"
                    nodes[node] = dict(_SESS["data"]["nodes"].get(node) or {"host": None, "sessions": []}, error=errors[node])
                else:
                    nodes[node] = {"host": d.get("host"), "sessions": d.get("sessions") or [], "error": d.get("error")}
            _SESS["data"], _SESS["at"] = {"nodes": nodes, "errors": errors}, time.time()
        return dict(_SESS["data"], at=_SESS["at"])


def session_tail(node, sid, tool):
    """The last messages of one session. The export tool globs on the id, so the pattern is what keeps a
    request from naming anything but a session."""
    if node not in NODES or tool not in SESS_TOOLS or not _SESS_ID.fullmatch(sid or ""):
        raise ValueError("unknown session")
    return _sess_script(NODES[node], "opencode-export-tail.py", [sid, "5", tool], 45)


# ---------------------------------------------------------------- messages (phone interface)
# What went wrong or needs you, written down once and kept, so the phone can show it after the fact and push
# it when the app is closed. One watcher thread compares what Fleet already collects with a few rules
# (ev_detect: pure functions, testable without a server). An EPISODE opens when a rule has held long enough
# ("for_s": a flapping probe never opens one), may repeat as a reminder, and resolves when the rule stops
# holding. Open episodes are saved, so a restart does not announce them again.
# Levels: "act" is pushed to a phone, "notice" is a list entry that counts as unread, "info" is list only.
# Texts carry node and model names, counts and generic words, never a session title or a path: a push passes
# through Apple.

EV_DIR = os.path.expanduser(os.environ.get("FLEET_EVENTS_DIR", "~/.local/share/fleet-ui"))
EV_FILE = os.path.join(EV_DIR, "events.jsonl")
EV_STATE = os.path.join(EV_DIR, "events-state.json")
EV_CONF = os.path.expanduser("~/.config/fleet-ui/events.json")    # optional: {"mute": ["web-down:app.example.org", "link-slow:"]} (key prefixes)
EV_KEEP = 500            # entries kept
EV_REMIND_S = 6 * 3600   # an open act episode repeats after this, twice at most
EV_HEAVY = 120           # seconds between the slow checks (backup, web server, sessions, containers)
EV_GRACE = 45            # seconds after start before the first look: the probes must have run
EV_PUSH_PER_H = 6        # most pushes an hour; a burst becomes one summary
EV_MEM_LOW, EV_MEM_OK = 3.0, 3.5     # GB free: opens below the memory guard's floor. Closes just above it: Jean-Luc idles near 3.9
                                     # with a big model resident (measured), so a higher mark would never be reached
EV_STALE_H = 30                      # last off-site snapshot older than this (same as MA_SNAP_H)

_EV = {"last": 0, "items": [], "lines": 0, "boots": {}, "ma": None, "blob": ""}
_EV_LOCK = threading.RLock()
_EP = {}                 # episode key -> {"spec", "since", "open", "gone", "sent", "reminders"}
_AD_AT = 0.0             # when Auto Destruct last ran: reboots shortly after are expected
_CFG_CHANGED = {}        # model -> when its context or effort was changed (the unload that follows is ours)


def _ev_hash(text, n=10):
    """An opaque short id: session ids and episode keys must not leave the machine as they are."""
    return hashlib.sha1(str(text).encode()).hexdigest()[:n]


def ev_load():
    items, st = [], {}
    try:
        with open(EV_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    items.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    try:
        with open(EV_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        pass
    with _EV_LOCK:
        _EV["items"], _EV["lines"] = items[-EV_KEEP:], len(items)
        _EV["last"] = max([e.get("id", 0) for e in items] + [st.get("last", 0)])
        _EV["boots"] = dict(st.get("boots") or {})
        for k, e in (st.get("eps") or {}).items():
            _EP[k] = {"spec": e["spec"], "since": e["since"], "open": True, "gone": None,
                      "sent": e.get("sent", 0), "reminders": e.get("reminders", 0)}


def ev_save_state():
    with _EV_LOCK:
        blob = json.dumps({"last": _EV["last"], "boots": _EV["boots"],
                           "eps": {k: {"spec": e["spec"], "since": e["since"], "sent": e["sent"], "reminders": e["reminders"]}
                                   for k, e in _EP.items() if e["open"]}})
        if blob == _EV["blob"]:
            return
        _EV["blob"] = blob
    try:
        os.makedirs(EV_DIR, exist_ok=True)
        tmp = EV_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, EV_STATE)
    except OSError as e:
        print(f"events: cannot save state: {e}", flush=True)


def ev_append(level, kind, title, text, go="", node="", state="point", episode="", reminder=0):
    """Add one entry. level: act | notice | info; state: open | resolved | point."""
    with _EV_LOCK:
        _EV["last"] += 1
        e = {"id": _EV["last"], "at": round(time.time(), 1), "kind": kind, "level": level, "state": state,
             "episode": episode, "title": title, "text": text[:400], "go": go, "node": node, "reminder": reminder}
        _EV["items"].append(e)
        del _EV["items"][:-EV_KEEP]
        try:
            os.makedirs(EV_DIR, exist_ok=True)
            with open(EV_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            _EV["lines"] += 1
            if _EV["lines"] > EV_KEEP + 100:           # trim: rewrite the newest EV_KEEP lines
                tmp = EV_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in _EV["items"]))
                os.replace(tmp, EV_FILE)
                _EV["lines"] = len(_EV["items"])
        except OSError as ex:
            print(f"events: cannot write {EV_FILE}: {ex}", flush=True)
    return e


def ev_audit(title, text, go=""):
    """A one-off record of something done through Fleet (Red Alert, a stop, a backup start): list only."""
    ev_append("info", "audit", title, text, go=go)


def events_json():
    with _EV_LOCK:
        return {"events": list(_EV["items"][-200:]), "latest": _EV["last"],
                "open": sorted(k for k, e in _EP.items() if e["open"]), "at": time.time()}


def ev_step(eps, conds, hold, now):
    """Pure episode logic. conds: key -> spec for every rule that holds right now. hold: key prefixes whose
    source is unknown at the moment (episodes under them are left alone). Returns [(action, key, spec, n)]
    with action open | reminder | resolve."""
    acts = []
    for key, spec in conds.items():
        e = eps.get(key)
        if e is None:
            e = eps[key] = {"spec": spec, "since": now, "open": False, "gone": None, "sent": 0, "reminders": 0}
        e["spec"], e["gone"] = spec, None
        if not e["open"]:
            if now - e["since"] >= spec.get("for_s", 0):
                e["open"], e["sent"] = True, now
                acts.append(("open", key, spec, 0))
        elif spec.get("remind") and e["reminders"] < 2 and now - e["sent"] >= EV_REMIND_S:
            e["reminders"] += 1
            e["sent"] = now
            acts.append(("reminder", key, spec, e["reminders"]))
    for key in list(eps):
        if key in conds or any(key.startswith(p) for p in hold):
            continue
        e = eps[key]
        if e["gone"] is None:
            e["gone"] = now
        if now - e["gone"] >= e["spec"].get("clear_s", 0):
            if e["open"]:
                acts.append(("resolve", key, e["spec"], 0))
            del eps[key]
    return acts


def ev_filter(conds, eps, mute):
    """Drop muted rules, and forget their episodes without a word (a muted rule is not a recovery)."""
    for k in [k for k in eps if any(k.startswith(p) for p in mute)]:
        del eps[k]
    return {k: v for k, v in conds.items() if not any(k.startswith(p) for p in mute)}


def _ev_mute():
    try:
        with open(EV_CONF, encoding="utf-8") as f:
            return [str(p) for p in (json.load(f).get("mute") or []) if p]
    except (OSError, ValueError, AttributeError):
        return []


def ev_apply(acts):
    """Write the entries for ev_step's actions. Returns [(event, push text)] for the ones to push."""
    out = []
    for act, key, spec, n in acts:
        kw = dict(kind=spec["kind"], title=spec["title"], go=spec.get("go", ""), node=spec.get("node", ""), episode=key)
        if act == "resolve":
            ev_append(spec["level"], text=spec.get("ok") or "Back to normal.", state="resolved", **kw)
        else:
            text = spec["text"] if act == "open" else "Still open: " + spec["text"]
            out.append((ev_append(spec["level"], text=text, state="open", reminder=n, **kw),
                        spec.get("push") or (spec["title"] if spec["level"] == "act" else spec["text"])))
    return out


def ev_detect(w, open_keys):
    """The rules. w is a snapshot of what Fleet knows (see ev_world); open_keys the open episodes (for the
    hysteresis). Returns (conds, hold). A source that is None is unknown: its episodes are held, not closed."""
    now, conds, hold = w["now"], {}, []
    label = lambda n: n.title()                                           # "jean-luc" -> "Jean-Luc"
    # models: llama-swap down, or a pin that is not running
    swap = w["errors"].get("_swap")
    if swap:
        conds["swap-down"] = dict(kind="swap-down", level="act", title="llama-swap not answering", text=swap[:140], go="#/models",
                                  push="llama-swap does not answer. No model can load.", for_s=120, remind=True,
                                  ok="llama-swap answers again.")
        hold.append("model-lost:")                                        # the running list is stale meanwhile
    else:
        known = w.get("models")
        for m in w["pins"]:
            if known and m not in known:            # renamed or removed in config.yaml; the phone cannot unpin it
                conds["pin-unknown:" + m] = dict(kind="pin-unknown", level="notice", title="Pinned model missing", go="#/models", for_s=300,
                                                 text=f"{_family_base(m)} is pinned but is not in the llama-swap configuration any more.",
                                                 ok=f"{_family_base(m)} is no longer pinned.")
                continue
            if m in w["running"] or m in w["busy"] or now - w["cfg"].get(m, 0) < 1800:
                continue
            if len(node_of(m)) > 1 and any(len(node_of(r)) > 1 for r in w["running"] if r != m):
                continue                            # another two-node model took its place in the exclusive group: the user's own choice
            nm, err = _family_base(m), (w["errors"].get(m) or "").strip().replace("\n", " ")
            conds["model-lost:" + m] = dict(
                kind="model-lost", level="act", title="Pinned model lost", go="#/models", for_s=120, remind=True,
                text=f"{nm} is pinned but not running." + (f" Last error: {err[:120]}" if err else "") +
                     " Fleet does not reload it by itself: open Models and press Load.",
                push=f"{nm} is pinned but not running. Open Models and press Load.",
                ok=f"{nm} is running again, or no longer pinned.")
    # nodes
    for n, info in w["nodes"].items():
        lab = label(n)
        if info is None:
            hold += [f"node-down:{n}", f"memory-low:{n}", f"link-slow:{n}"]
            continue
        if info.get("error"):
            conds[f"node-down:{n}"] = dict(kind="node-down", level="act", title=f"{lab} not answering", node=n, go="#/bridge",
                                           text=f"{lab} does not answer. {info['error'][:100]}".strip(), push=f"{lab} does not answer.",
                                           for_s=90, remind=True, ok=f"{lab} answers again.")
            hold += [f"memory-low:{n}", f"link-slow:{n}"]                 # no numbers while it is down
            continue
        avail = (info.get("mem_avail_kb") or 0) / 1048576
        if avail < (EV_MEM_OK if f"memory-low:{n}" in open_keys else EV_MEM_LOW):
            conds[f"memory-low:{n}"] = dict(
                kind="memory-low", level="act", title=f"{lab} low on memory", node=n, go=f"#/systems/engineering/{n}", for_s=30, remind=True,
                text=f"Only {avail:.1f} GB of memory free on {lab}. Loads may be refused or stopped. Open Engineering to see what holds it.",
                push=f"Only {avail:.1f} GB of memory free on {lab}.", clear_s=300, ok=f"{lab} has enough free memory again.")
        mbps = info.get("link_mbps")
        if not isinstance(mbps, int) or mbps <= 0:
            hold.append(f"link-slow:{n}")                                   # no reading (cable out): unknown, not fixed
        elif mbps < 1000:
            conds[f"link-slow:{n}"] = dict(kind="link-slow", level="notice", title=f"{lab}'s network is slow", node=n, go="#/bridge", for_s=300, clear_s=300,
                                           text=f"{lab}'s network port runs at {mbps} Mb/s instead of 1000. Backups and downloads crawl. Check the cable and plug.",
                                           ok=f"{lab}'s network port is back to full speed.")
    # off-site backup
    a = w.get("alpha")
    if a is None:
        hold += ["backup"]
    else:
        last = a.get("last") or {}
        age_h = (now - last["end"]) / 3600 if last.get("end") else None
        stale = age_h is not None and age_h > EV_STALE_H
        if stale or a.get("status") == "error":
            txt = (f"The last off-site snapshot is {int(age_h)} hours old." if stale else
                   "Memory Alpha cannot be read: " + ((a.get("errors") or ["no answer"])[0])[:100])
            conds["backup"] = dict(kind="backup", level="act", title="Backup failed or stale", text=txt + " Open Memory Alpha.",
                                   push=txt if stale else "The off-site backup cannot be checked.",
                                   go="#/systems/alpha", for_s=120 if stale else 900, clear_s=300, remind=True, ok="The off-site backup is current again.")
        elif a.get("problems"):
            conds["backup-warn"] = dict(kind="backup-warn", level="notice", title="Backup needs a look", text=a["problems"][0][:140] + ".",
                                        go="#/systems/alpha", for_s=300, clear_s=300, ok="The backup warning is gone.")
    # web server
    d = w.get("defiant")
    if d is None:
        hold += ["web-host", "web-down:", "cert:", "reboot-needed"]
    elif d.get("configured") is not False:
        if d.get("errors") and not d.get("sites"):
            conds["web-host"] = dict(kind="web-down", level="act", title="Web server not answering", go="#/systems/defiant", for_s=300, remind=True,
                                     text="Defiant cannot be checked: " + d["errors"][0][:100], push="The web server cannot be checked.",
                                     clear_s=300, ok="The web server answers again.")
            hold += ["web-down:", "cert:", "reboot-needed"]
        else:
            if not d.get("sites"):
                hold += ["web-down:", "cert:"]
            for s in d.get("sites") or []:
                dm = s.get("domain")
                if s.get("error") or not s.get("status") or (s["status"] >= 400 and s["status"] != 401):
                    conds[f"web-down:{dm}"] = dict(kind="web-down", level="act", title="Web site down", go="#/systems/defiant", for_s=200, remind=True,
                                                   text=f"{dm} does not answer ({s.get('error') or s.get('status')}). Open Defiant.",
                                                   push="A web site on Defiant does not answer.", clear_s=300, ok=f"{dm} answers again.")
                elif s.get("cert_days") is not None and s["cert_days"] < 14:
                    conds[f"cert:{dm}"] = dict(kind="cert", level="notice", title="Certificate ends soon", go="#/systems/defiant",
                                               text=f"The certificate of {dm} ends in {s['cert_days']} days.", clear_s=300, ok=f"The certificate of {dm} is renewed.")
            if d.get("reboot_required"):
                conds["reboot-needed"] = dict(kind="reboot-needed", level="notice", title="Web server needs a reboot", go="#/systems/defiant",
                                              text="Defiant has installed updates and needs a reboot.", ok="Defiant has rebooted.")
    # coding sessions waiting for you: stalled for 30 minutes to 6 hours (older ones are abandoned, not waiting)
    s = w.get("sessions")
    if s is None:
        hold.append("sessions-waiting:")
    else:
        for n, v in (s.get("nodes") or {}).items():
            if not v or v.get("error"):
                hold.append(f"sessions-waiting:{n}:")
                continue
            for x in v.get("sessions") or []:
                age, key = x.get("updated_secs_ago") or 0, f"sessions-waiting:{n}:{_ev_hash(x.get('id'))}"
                if x.get("stalled") and age >= 1800 and (age <= 21600 or key in open_keys):
                    conds[key] = dict(kind="sessions-waiting", level="act", title="Session waiting", node=n, go="#/systems/sessions", for_s=0, clear_s=600,
                                      text=f"A coding session on {label(n)} has been waiting for you for {int(age / 60)} minutes. Open Sessions.",
                                      push=f"A coding session on {label(n)} has been waiting for you for 30 minutes or more.",
                                      ok="A waiting session is moving again or was closed.")
    # containers of idle sessions that hold memory
    c = w.get("containers")
    if c is None:
        hold.append("idle-sessions:")
    else:
        for n in w["nodes"]:
            if (c.get("errors") or {}).get(n):
                hold.append(f"idle-sessions:{n}")
                continue
            rows = [r for r in c.get("containers") or [] if r.get("node") == n and r.get("kind") == "session" and (r.get("session_idle_s") or 0) > 86400]
            gb = sum(r.get("ram_mb") or 0 for r in rows) / 1024
            if len(rows) >= 5 and gb >= 3:
                conds[f"idle-sessions:{n}"] = dict(kind="idle-sessions", level="notice", title="Idle sessions hold memory", node=n,
                                                   go=f"#/systems/engineering/{n}", for_s=600,
                                                   text=f"{len(rows)} containers of coding sessions idle for over a day hold {gb:.0f} GB on {label(n)}. Stop the ones you no longer use.",
                                                   ok=f"The idle sessions on {label(n)} no longer hold much memory.")
    return conds, hold


def ev_world(now, heavy):
    with LOCK:
        nodes = {n: (dict(v) if isinstance(v, dict) else None) for n, v in STATE["nodes"].items()}
        running = {r.get("model") for r in STATE["running"]}
        errors = dict(STATE["errors"])
        models = set(STATE["models"])
    for n in NODES:
        nodes.setdefault(n, None)
    with _loading_lock:
        loading = set(_loading)
    return dict(heavy, now=now, nodes=nodes, running=running, errors=errors, pins=load_pins(), models=models,
                busy=loading | set(queued_ids()) | set(_llm_busy), cfg=dict(_CFG_CHANGED))


def _ev_heavy():
    out = {}
    for k, fn in (("alpha", memory_alpha_state), ("defiant", defiant_state), ("sessions", sessions_state), ("containers", containers_state)):
        try:
            out[k] = fn()
        except Exception as e:                    # a failing source is unknown, never "all clear"
            print(f"events: {k}: {e.__class__.__name__}: {str(e)[:120]}", flush=True)
            out[k] = None
    return out


def ev_points(w):
    """One-off entries: a node that rebooted on its own, a manual backup that ended."""
    for n, info in w["nodes"].items():
        boot = info and info.get("boot")
        if not boot:
            continue
        with _EV_LOCK:
            prev, _EV["boots"][n] = _EV["boots"].get(n), boot
        with _EV_LOCK:
            planned = any(e.get("title") == "Auto Destruct ran" and w["now"] - e.get("at", 0) < 1200 for e in _EV["items"][-50:])
        if prev and prev != boot and w["now"] - _AD_AT > 1200 and not planned:
            ev_append("notice", "node-rebooted", f"{n.title()} rebooted", go="#/bridge", node=n,
                      text=f"{n.title()} started again (noticed at {time.strftime('%H:%M')}). If you did not expect it, check the memory guard and the power.")
    a = w.get("alpha")
    if a is not None:
        m = a.get("manual") or {}
        cur = m.get("state")
        prev, _EV["ma"] = _EV["ma"], cur
        if prev == "running" and cur and cur != "running":
            ok = m.get("exit") in (0, None) and not m.get("error")
            ev_append("info", "backup-done", "Backup finished" if ok else "Backup failed", go="#/systems/alpha",
                      text="The backup you started has finished." if ok else f"The backup you started ended with an error: {(m.get('error') or 'exit ' + str(m.get('exit')))[:100]}")


def events_worker():
    ev_load()
    time.sleep(EV_GRACE)
    heavy, heavy_at = {}, 0.0
    while True:
        try:
            if time.time() - heavy_at >= EV_HEAVY:
                heavy, heavy_at = _ev_heavy(), time.time()
            now = time.time()
            w = ev_world(now, heavy)
            with _EV_LOCK:
                conds, hold = ev_detect(w, {k for k, e in _EP.items() if e["open"]})
                conds = ev_filter(conds, _EP, _ev_mute())
                pairs = ev_apply(ev_step(_EP, conds, hold, now))
            ev_points(w)
            ev_save_state()
            push_dispatch(pairs)
        except Exception as e:
            print(f"events: {e.__class__.__name__}: {e}", flush=True)
        time.sleep(5)


# ---------------------------------------------------------------- push (Apple Push Notification service)
# Needs the paid Apple Developer Program: a key made at developer.apple.com (a .p8 file) kept on this host,
# readable by this user only, with its ids in the service environment (a drop-in with EnvironmentFile, like
# the Auto Destruct code): APNS_KEY_FILE, APNS_KEY_ID, APNS_TEAM_ID, optionally APNS_TOPIC. Without them the
# sender is off and everything else keeps working. The request goes out through curl, which speaks HTTP/2;
# the token is signed here with `cryptography` and handed to curl on stdin, never on the command line.

PUSH_DEV_FILE = os.path.expanduser("~/.config/fleet-ui/push-devices.json")
APNS_TOPIC = "com.oesterwitz.starfleetcommand.ios"
APNS_HOSTS = {"development": "api.sandbox.push.apple.com", "production": "api.push.apple.com"}
_PUSH_SENT = []                   # timestamps of recent pushes
_PUSH_LOCK = threading.Lock()
_APNS = {"jwt": None, "iat": 0}


def push_devices():
    try:
        with open(PUSH_DEV_FILE, encoding="utf-8") as f:
            return [d for d in json.load(f) if isinstance(d, dict) and d.get("token")]
    except (OSError, ValueError):
        return []


def _push_save(devs):
    os.makedirs(os.path.dirname(PUSH_DEV_FILE), exist_ok=True)
    tmp = PUSH_DEV_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(devs, f)
    os.replace(tmp, PUSH_DEV_FILE)


def push_register(token, env):
    if not re.fullmatch(r"[0-9a-f]{32,200}", token or "") or env not in APNS_HOSTS:
        raise ValueError("not a device token")
    with _PUSH_LOCK:
        devs = [d for d in push_devices() if d["token"] != token] + [{"token": token, "env": env, "added": int(time.time())}]
        _push_save(devs[-5:])
        return min(len(devs), 5)


def push_unregister(token):
    with _PUSH_LOCK:
        devs = [d for d in push_devices() if d["token"] != token]
        _push_save(devs)
        return len(devs)


def push_status():
    devs = push_devices()
    key, kid, tid = (os.environ.get(k) or "" for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID"))
    if not (key and kid and tid):
        reason = "No push key on Fleet yet."
    elif not os.path.isfile(os.path.expanduser(key)):
        reason = "The push key file is missing."
    else:
        reason = ""
    return {"sender": "off" if reason else "ready", "reason": reason, "devices": len(devs), "env": devs[0]["env"] if devs else ""}


def _apns_jwt():
    """The provider token: ES256 over {iss: team, iat}; Apple accepts one for up to an hour."""
    now = int(time.time())
    if _APNS["jwt"] and now - _APNS["iat"] < 3000:
        return _APNS["jwt"]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    with open(os.path.expanduser(os.environ["APNS_KEY_FILE"]), "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    head = b64(json.dumps({"alg": "ES256", "kid": os.environ["APNS_KEY_ID"]}, separators=(",", ":")).encode())
    body = b64(json.dumps({"iss": os.environ["APNS_TEAM_ID"], "iat": now}, separators=(",", ":")).encode())
    r, s = utils.decode_dss_signature(key.sign(head + b"." + body, ec.ECDSA(hashes.SHA256())))
    _APNS["jwt"], _APNS["iat"] = (head + b"." + body + b"." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))).decode(), now
    return _APNS["jwt"]


def apns_post(dev, payload, collapse=""):
    """One push to one device. Returns (http status, Apple's reason). The headers (with the token) go to curl on stdin."""
    host = os.environ.get("FLEET_APNS_HOST") or APNS_HOSTS[dev["env"]]
    collapse = re.sub(r"[^A-Za-z0-9:._-]", "-", collapse)[:60]     # a header line in curl's config: no quote or newline gets through
    conf = "\n".join([f'url = "https://{host}/3/device/{dev["token"]}"',
                      f'header = "authorization: bearer {_apns_jwt()}"',
                      f'header = "apns-topic: {os.environ.get("APNS_TOPIC") or APNS_TOPIC}"',
                      'header = "apns-push-type: alert"', 'header = "apns-priority: 10"'] +
                     ([f'header = "apns-collapse-id: {collapse}"'] if collapse else []))
    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:              # 0600; removed on close
        json.dump(payload, f)
        f.flush()
        r = subprocess.run(["curl", "-sS", "--http2", "-m", "15", "-K", "-", "--data-binary", "@" + f.name, "-w", "\n%{http_code}"],
                           input=conf, capture_output=True, text=True, timeout=25)
    body, _, code = r.stdout.rpartition("\n")
    try:
        reason = json.loads(body).get("reason", "") if body.strip() else ""
    except ValueError:
        reason = body[:80]
    code = int(code) if code.strip().isdigit() else 0
    if not code and not reason:
        reason = f"curl exit {r.returncode}: {(r.stderr or '').strip()[:100]}"       # curl's own error text holds no header
    return code, reason


def push_send(title, body, go="", badge=0, collapse=""):
    """Send to every registered device. Returns how many Apple accepted. Dead tokens are dropped."""
    if push_status()["sender"] != "ready":
        return 0
    payload = {"aps": {"alert": {"title": title[:80], "body": body[:200]}, "sound": "default", "badge": int(badge), "thread-id": collapse[:60] or "starfleet"}, "go": go}
    if collapse == "test":
        payload["test"] = True                                              # the app shows this one even while it is open
    sent = 0
    for dev in push_devices():
        try:
            code, reason = apns_post(dev, payload, collapse)
        except (OSError, subprocess.SubprocessError, ImportError, ValueError, KeyError) as e:
            print(f"push: {e.__class__.__name__}: {str(e)[:120]}", flush=True)
            continue
        if code == 200:
            sent += 1
        else:
            print(f"push: Apple answered {code} {reason}", flush=True)
            if code == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                push_unregister(dev["token"])
    return sent


_PUSH_PENDING = []                # [(not before, (title, text, go, thread), tries)]: sends that failed or waited for budget


def push_dispatch(pairs):
    """Push the act-level entries that just opened, and retry earlier failures (called every tick, also with nothing new).
    At most EV_PUSH_PER_H an hour: more than three at once become one summary; waiting coding sessions are one push of their
    own, and the last two slots of the hour are kept for the other kinds."""
    now = time.time()
    due = [(e, t) for e, t in pairs if e["level"] == "act" and e["state"] == "open"]
    if not (due or _PUSH_PENDING) or push_status()["sender"] != "ready" or not push_devices():
        return
    msgs = []                                                   # (title, text, go, thread, tries)
    with _PUSH_LOCK:
        for item in [x for x in _PUSH_PENDING if x[0] <= now]:
            _PUSH_PENDING.remove(item)
            msgs.append(item[1] + (item[2],))
    waiting = [x for x in due if x[0].get("kind") == "sessions-waiting"]
    rest = [x for x in due if x[0].get("kind") != "sessions-waiting"]
    if len(rest) > 3:
        msgs.append((f"{len(rest)} things need you", "; ".join(e["title"] for e, _ in rest[:3]) + "...", "#/command/messages", "summary", 0))
    else:
        msgs += [(e["title"], t, e["go"], _ev_hash(e["episode"], 20), 0) for e, t in rest]
    if len(waiting) == 1:
        e, t = waiting[0]
        msgs.append((e["title"], t, e["go"], "sessions", 0))
    elif waiting:
        msgs.append(("Sessions waiting", f"{len(waiting)} coding sessions are waiting for you.", "#/systems/sessions", "sessions", 0))
    with _EV_LOCK:
        badge = sum(1 for k, e in _EP.items() if e["open"] and e["spec"].get("level") == "act")
    for title, text, go, thread, tries in msgs:
        with _PUSH_LOCK:
            _PUSH_SENT[:] = [t for t in _PUSH_SENT if now - t < 3600]
            room = EV_PUSH_PER_H - len(_PUSH_SENT)
        if room <= (2 if thread == "sessions" else 0):
            print("push: hourly limit reached", flush=True)
            continue
        if push_send(title, text, go, badge, thread):
            with _PUSH_LOCK:
                _PUSH_SENT.append(now)
        elif tries < 3:                                         # a timeout or an Apple 5xx: try again in a minute
            with _PUSH_LOCK:
                _PUSH_PENDING.append((now + 60, (title, text, go, thread), tries + 1))


# ---------------------------------------------------------------- http

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")


class Handler(BaseHTTPRequestHandler):
    timeout = 60          # socket operations: a client that stalls a read or write is dropped

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse(self, obj):
        self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
        self.wfile.flush()

    def _computer_post(self, n):
        try:
            computer_auth(self.headers.get(KEY_HDR))
        except urllib.error.HTTPError as e:
            self.close_connection = True          # the body is never read without a valid key
            return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
        if n > (64 << 20):
            return self._send(413, '{"error":"too large"}', "application/json")
        data = self.rfile.read(n)
        if self.path == "/api/computer/transcribe":
            try:
                return self._send(200, json.dumps(computer_transcribe(data)), "application/json")
            except PermissionError as e:
                return self._send(409, json.dumps({"error": str(e)}), "application/json")
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                return self._send(502, json.dumps({"error": str(e)[:200]}), "application/json")
        try:
            body = json.loads(data or b"{}")
        except ValueError:
            return self._send(400, '{"error":"bad json"}', "application/json")
        if self.path.startswith("/api/computer/push/"):
            try:
                if self.path == "/api/computer/push/register":
                    return self._send(200, json.dumps({"ok": True, "devices": push_register(body.get("token"), body.get("env"))}), "application/json")
                if self.path == "/api/computer/push/unregister":
                    return self._send(200, json.dumps({"ok": True, "devices": push_unregister(body.get("token") or "")}), "application/json")
                if self.path == "/api/computer/push/test":
                    if push_status()["sender"] != "ready":
                        return self._send(409, json.dumps({"error": "Push is not set up on Fleet yet."}), "application/json")
                    if not push_devices():
                        return self._send(409, json.dumps({"error": "No iPhone has registered yet."}), "application/json")
                    ev_append("info", "test", "Test message", "A test from Fleet. If you read this as a notification, push works.", go="#/command/messages")
                    sent = push_send("Starfleet test", "Push works. This test came from Fleet.", "#/command/messages", 0, "test")
                    return self._send(200 if sent else 502, json.dumps({"ok": bool(sent), "sent": sent} if sent else {"error": "Apple did not accept the test. See the Fleet log."}), "application/json")
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            return self._send(404, '{"error":"unknown"}', "application/json")
        if self.path == "/api/computer/delete":
            try:
                os.remove(_chat_path(body.get("id")))
            except (OSError, ValueError) as e:
                return self._send(404, json.dumps({"error": str(e)[:200]}), "application/json")
            return self._send(200, '{"ok":true}', "application/json")
        if self.path == "/api/computer/send":
            self._sse_start()
            gone = []

            def emit(obj):                       # a closed browser tab must not abort the turn or lose it
                if gone:
                    return
                try:
                    self._sse(obj)
                except (BrokenPipeError, ConnectionError, OSError):
                    gone.append(1)
            try:
                computer_send(body, emit)
            except PermissionError as e:
                emit({"type": "error", "text": str(e)})
            except urllib.error.HTTPError as e:
                emit({"type": "error", "text": "Model: " + _err_text(e)[:300]})
            except Exception as e:               # anything else: report it, never leave the stream hanging
                emit({"type": "error", "text": str(e)[:300] or e.__class__.__name__})
            return
        return self._send(404, '{"error":"unknown"}', "application/json")

    def do_GET(self):
        if self.path.startswith("/vendor/"):
            name = self.path[len("/vendor/"):]
            if not re.fullmatch(r"[a-z0-9.-]+\.(js|css)", name):
                return self._send(404, "", "text/plain")
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", name)
            if not os.path.isfile(p):
                return self._send(404, "", "text/plain")
            with open(p, "rb") as f:
                return self._send(200, f.read(), "text/css" if name.endswith(".css") else "application/javascript")
        if self.path.startswith("/api/computer/"):
            try:
                computer_auth(self.headers.get(KEY_HDR))
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/api/computer/models":
                    t = computer_tools()
                    return self._send(200, json.dumps({"models": computer_models(),
                        "servers": {n: {"web": c["web"], "tools": sorted(k for k, v in t["tools"].items() if v[0] == n),
                                        "error": t["errors"].get(n)} for n, c in COMPUTER_MCP.items()}}), "application/json")
                if u.path == "/api/computer/chats":
                    return self._send(200, json.dumps({"chats": computer_chats()}), "application/json")
                if u.path == "/api/computer/chat":
                    return self._send(200, json.dumps(computer_chat_load((q.get("id") or [""])[0])), "application/json")
                if u.path == "/api/computer/wiki/graph":      # Memory Core (wikicore.py)
                    return self._send(200, wikicore.graph_json(), "application/json")
                if u.path == "/api/computer/wiki/trails":
                    return self._send(200, json.dumps({"trails": wikicore.trails(COMPUTER_DIR)}), "application/json")
                if u.path == "/api/computer/wiki/page":       # Memory Core's Open button: one page's markdown, read-only
                    p = (q.get("p") or [""])[0]
                    return self._send(200, json.dumps({"p": p, "md": wikicore.page_md(p)}, ensure_ascii=False),
                                      "application/json; charset=utf-8")
                if u.path == "/api/computer/push":              # push: is the sender set up, how many phones
                    return self._send(200, json.dumps(push_status()), "application/json")
                if u.path == "/api/computer/sessions":         # coding-agent sessions, for the phone interface
                    return self._send(200, json.dumps(sessions_state()), "application/json")
                if u.path == "/api/computer/sessions/tail":
                    return self._send(200, json.dumps(session_tail(*((q.get(k) or [""])[0] for k in ("node", "id", "tool")))),
                                      "application/json")
            except (OSError, ValueError) as e:
                return self._send(404, json.dumps({"error": str(e)[:200]}), "application/json")
            return self._send(404, '{"error":"unknown"}', "application/json")
        if self.path == "/":                                   # a phone gets the phone interface
            return self._send(200, page(page_for(self.headers.get("User-Agent"))), "text/html; charset=utf-8")
        if self.path == "/desktop":
            return self._send(200, page(), "text/html; charset=utf-8")
        if self.path == "/padd":
            if not os.path.isfile(PADD_FILE):
                return self._send(404, "not found", "text/plain")
            return self._send(200, page(PADD_FILE), "text/html; charset=utf-8")
        if self.path == "/sounds/tng_red_alert1.mp3":
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds",
                                  "tng_red_alert1.mp3"), "rb") as f:
                return self._send(200, f.read(), "audio/mpeg")
        if self.path == "/api/events":
            return self._send(200, json.dumps(events_json()), "application/json")
        if self.path == "/api/nas":
            return self._send(200, json.dumps(nas_state()), "application/json")
        if self.path == "/api/memoryalpha":
            return self._send(200, json.dumps(memory_alpha_state()), "application/json")
        if self.path == "/api/defiant":
            return self._send(200, json.dumps(defiant_state()), "application/json")
        if self.path == "/api/containers":
            return self._send(200, json.dumps(containers_state()), "application/json")
        if self.path == "/api/nas/stats":
            return self._send(200, json.dumps(nas_stats()), "application/json")
        if self.path == "/ds9.jpg":      # optional real photo; the page falls back to its drawn station
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ds9.jpg")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            return self._send(404, "", "text/plain")
        if self.path == "/api/boot":
            with open("/proc/sys/kernel/random/boot_id") as f:
                return self._send(200, json.dumps({"boot": f.read().strip()}), "application/json")
        if self.path == "/api/jobs":
            return self._send(200, json.dumps(jobs_snapshot()), "application/json")
        if self.path == "/api/state":
            with LOCK:
                snap = json.loads(json.dumps(STATE))
            snap["pins"] = load_pins()
            snap["queued"] = queued_ids()
            snap["llm_loading"] = sorted(_llm_busy)
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
                    "ctx_max": read_ctx_ceiling(m) or maxes.get(m),
                    "ctx_native": natives.get(m),
                    "ctx_deployed": deployeds.get(m),
                    "vision": visions.get(m),
                    "effort": read_effort(m),
                    "effort_options": [o["label"] for o in (_effort_opts.get(m) or [])],
                    "ctx_pool": (pools.get(m) or {}).get("pool"),
                    "kind": "llm",
                }
                for m in snap["models"]
            ]
            _img_prefs = load_img_nodes()
            # Image Creation rows. These are NOT llama-swap members -- they live inside
            # the single `imagegen` container -- so they carry no ttl/context/tps and no
            # pin/unload controls. What they do carry is the footprint the admission gate
            # reserves on the ComfyUI node, which is the number that matters when an LLM
            # load and a render compete for the same pool.
            snap["models"] += [
                {
                    "id": im["id"],
                    "nodes": [img_node(im["id"], _img_prefs)],
                    "tps": None,
                    "pinned": False,
                    "error": None,
                    "load_eta": None,
                    "ctx": None,
                    "ctx_capable": False,
                    "ctx_max": None,
                    "ctx_native": None,
                    "ctx_deployed": None,
                    "vision": None,
                    "effort": None,
                    "effort_options": [],
                    "ctx_pool": None,
                    "kind": im.get("kind", "image"),
                    "need_mb": im.get("need_mb"),
                    "note": im.get("note"),
                    # Our own record of the last warm-up, not something ComfyUI can be
                    # asked: it exposes no loaded-model list, and memcheck.sh may call
                    # /free behind our back. Treat it as "we put it there", not gospel.
                    "loaded_at": (_img_prefs.get(im["id"]) or {}).get("loaded_at"),
                    "img_nodes": sorted(COMFY_ENDPOINTS),
                }
                # kind="judge" is excluded: the visual-QA critic is a real llama-swap
                # member (qwen3vl32-vision-starfleet) and already has a Model Registry
                # row on its true node. It lives in the catalog only so the image
                # pipeline and the fleet quote one number for it -- listing it here too
                # would both duplicate it and put it on the wrong machine.
                for im in image_models() if im.get("kind") != "judge"
            ]
            now = time.time()
            with _loading_lock:
                inflight = [(m, dict(v)) for m, v in _loading.items()]
            live = []
            for m, v in inflight:
                frac, phase = load_progress(v.get("mark"))
                # Never walk backwards: once the tail has scrolled past the last
                # phase line, a fresh read finds nothing, and a bar that drops from
                # 60% to a sweep reads as a failure rather than as a quiet log.
                if frac is None or frac < (v.get("progress") or 0):
                    frac, phase = v.get("progress"), v.get("phase")
                else:
                    with _loading_lock:
                        if m in _loading:
                            _loading[m]["progress"], _loading[m]["phase"] = frac, phase
                live.append({"model": m, "elapsed": round(now - v["started"], 1),
                             "eta": v["eta"], "progress": frac, "phase": phase})
            snap["loading"] = sorted(live, key=lambda d: d["model"])
            return self._send(200, json.dumps(snap), "application/json")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            return self._send(400, '{"error":"bad Content-Length"}', "application/json")
        if self.path.startswith("/api/computer/"):
            return self._computer_post(n)
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except ValueError:
            return self._send(400, '{"error":"bad json"}', "application/json")
        model = body.get("model")
        key = self.headers.get(KEY_HDR)
        if self.path == "/api/autodestruct":
            try:
                res = (auto_destruct_check if body.get("check") else auto_destruct)(key, body.get("code"))
            except AutoDestructOff as e:
                return self._send(503, json.dumps({"error": str(e)}), "application/json")
            except PermissionError as e:
                return self._send(403, json.dumps({"error": str(e)}), "application/json")
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            except (urllib.error.URLError, OSError, subprocess.SubprocessError) as e:
                return self._send(502, json.dumps({"error": f"reboot NOT scheduled: {str(e)[:180]}"}),
                                  "application/json")
            print(f"AUTO DESTRUCT: {res}", flush=True)
            if not body.get("check"):
                global _AD_AT
                _AD_AT = time.time()
                ev_audit("Auto Destruct ran", "Both Sparks were scheduled to reboot.", go="#/bridge")
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/api/nas/control":
            try:
                res = nas_control(body.get("target"), body.get("action"))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            except (OSError, subprocess.SubprocessError) as e:
                return self._send(502, json.dumps({"error": str(e)[:200]}), "application/json")
            print(f"NAS {body.get('action')} {body.get('target')}: ok", flush=True)
            ev_audit("NAS container changed", f"{str(body.get('action'))[:12]} {str(body.get('target'))[:60]} on the NAS.", go="#/systems/nas")
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/api/memoryalpha/backup":
            try:
                res = memory_alpha_backup(key)
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            except PermissionError as e:
                return self._send(409, json.dumps({"error": str(e)}), "application/json")
            except (urllib.error.URLError, OSError, subprocess.SubprocessError) as e:
                return self._send(502, json.dumps({"error": str(e)[:200]}), "application/json")
            print("MEMORY ALPHA backup started", flush=True)
            ev_audit("Backup started", "A manual off-site backup was started.", go="#/systems/alpha")
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/api/containers/stop":
            try:
                res = container_stop(body.get("node"), body.get("name"), key)
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            except PermissionError as e:
                return self._send(409, json.dumps({"error": str(e)}), "application/json")
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            except (urllib.error.URLError, OSError, subprocess.SubprocessError) as e:
                return self._send(502, json.dumps({"error": str(e)[:200]}), "application/json")
            print(f"CONTAINER stop {res['node']}/{res['name']}: ok", flush=True)
            ev_audit("Container stopped", f"{str(res['name'])[:60]} on {str(res['node']).title()} was stopped, freeing {res.get('freed_mb', 0)} MB.", go="#/systems/engineering")
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/api/redalert":
            try:
                res = key_check(key) if body.get("check") else red_alert(key)
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            except (urllib.error.URLError, OSError, subprocess.SubprocessError) as e:
                return self._send(502, json.dumps({"error": str(e)[:200]}), "application/json")
            print(f"RED ALERT: {res}", flush=True)
            if not body.get("check"):
                ev_audit("Red Alert ran", f"Unloaded {len(res.get('unloaded') or [])} models, stopped "
                         f"{sum(len(v) for v in (res.get('stopped') or {}).values())} containers, freed {(res.get('freed_mb') or 0) // 1024} GB."
                         + (f" {len(res['errors'])} errors." if res.get("errors") else ""), go="#/bridge")
            return self._send(200, json.dumps(res), "application/json")
        known = STATE["models"]
        img_ids = {m["id"] for m in image_models()}
        # image models are not llama-swap members, so they are never in STATE["models"]
        if not model or (known and model not in known and model not in img_ids):
            return self._send(400, '{"error":"unknown model"}', "application/json")

        if self.path == "/api/pin":
            pins = set(load_pins())
            if body.get("pin"):
                with LOCK:
                    resident = any(r.get("model") == model for r in STATE["running"])
                if not resident and not key_ok(model, key):
                    return self._send(403, json.dumps({"error": POLICY}), "application/json")
                # Pins are per member, and the Machine picker only switches which twin a row
                # shows. A pin left on the other twin was re-touched by keepalive and evicted
                # this one (rerankers share a swap:true group), so the model bounced back to its
                # old machine. Pinning one twin moves the pin off its siblings.
                pins -= {s for s in pins if s != model and _same_family(s, model)}
                pins.add(model)
                save_pins(pins)
                request_load(model, key)
            else:
                pins.discard(model)
                dequeue(model)
                save_pins(pins)
            return self._send(200, '{"ok":true}', "application/json")

        if self.path == "/api/effort":
            label = body.get("effort")
            if label is not None and not isinstance(label, str):
                return self._send(400, '{"error":"effort must be a string or null"}',
                                  "application/json")
            try:
                reloaded = apply_effort(model, label, key)
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
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
                reloaded = apply_ctx(model, ctx, key)
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            except OSError as e:
                return self._send(500, json.dumps({"error": str(e)[:200]}), "application/json")
            return self._send(200, json.dumps({"ok": True, "reloaded": reloaded}),
                              "application/json")

        if self.path == "/api/unload":
            dequeue(model)
            pins = set(load_pins())
            pins.discard(model)          # unpin too, else keepalive reloads it
            save_pins(pins)
            with LOCK:
                resident = any(r.get("model") == model for r in STATE["running"])
            with _loading_lock:
                loading = model in _loading
            try:
                if loading:
                    cancel_load(model, key)      # a pinned model still starting: abort the load
                elif resident:
                    freed = evict(model, key)
                    print(f"unload {model}: evicted, {freed} MB freed on this machine", flush=True)
                    return self._send(200, json.dumps({"ok": True, "freed_mb": freed}),
                                      "application/json")
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            except (urllib.error.URLError, OSError) as e:
                return self._send(502, json.dumps({"error": str(e)[:200]}), "application/json")
            return self._send(200, '{"ok":true}', "application/json")

        if self.path == "/api/cancel":
            dequeue(model)
            try:
                cancel_load(model, key)
            except urllib.error.HTTPError as e:
                return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            except (urllib.error.URLError, OSError) as e:
                return self._send(502, json.dumps({"error": str(e)[:200]}), "application/json")
            return self._send(200, '{"ok":true}', "application/json")

        if self.path == "/api/imgnode":
            node = body.get("node")
            if node not in COMFY_ENDPOINTS:
                return self._send(400, json.dumps(
                    {"error": f"unknown node {node!r}; have {sorted(COMFY_ENDPOINTS)}"}),
                    "application/json")
            if body.get("warm", True):                    # a warm-up IS a load: same key rule as llama-swap
                try:
                    key_check(key)
                except urllib.error.HTTPError as e:
                    return self._send(403, json.dumps({"error": _err_text(e)}), "application/json")
            prefs = load_img_nodes()
            entry = prefs.setdefault(model, {})
            moved = entry.get("node") not in (None, node)
            entry["node"] = node
            if moved:
                # the weights are on the old node now, not this one
                entry.pop("loaded_at", None)
            save_img_nodes(prefs)

            if not body.get("warm", True):
                return self._send(200, json.dumps({"ok": True, "node": node}),
                                  "application/json")

            # The warm-up waits its turn behind any LLM load and other light loads. A failed warm
            # is not a failed assignment: the preference is saved either way, so a render still
            # routes correctly once the node is reachable.
            pos = enqueue_light(model, key, lambda m, k, n=node: _warm_job(m, k, n))
            return self._send(200, json.dumps({"ok": True, "node": node, "queued": pos or 0}),
                              "application/json")

        if self.path == "/api/imgunload":
            if dequeue(model):
                return self._send(200, '{"ok":true}', "application/json")
            prefs = load_img_nodes()
            node = img_node(model, prefs)
            ok, msg = unload_image_models(node)
            if ok:
                # /free drops EVERY model on that node, so clear the flag for all of
                # them rather than pretending only this one went.
                for mid, e in prefs.items():
                    if e.get("node", DEFAULT_IMG_NODE) == node:
                        e.pop("loaded_at", None)
                save_img_nodes(prefs)
            return self._send(200 if ok else 502,
                              json.dumps({"ok": ok, "node": node, "message": msg}),
                              "application/json")

        self._send(404, '{"error":"not found"}', "application/json")


# ---------------------------------------------------------------- page

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet.html")
# The phone interface (padd.html with vendor/padd*.js): "/" serves it to a phone, /padd to anyone, and
# /desktop is always the desktop page. Touch padd.off next to this file and phones get the desktop page
# at "/" again, without a restart.
PADD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "padd.html")
PADD_OFF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "padd.off")
_PHONE_UA = re.compile(r"iPhone|iPod|Android.+Mobile")


def page(path=PAGE_FILE):
    """Read the page off disk each request so design edits need only a browser refresh."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def page_for(user_agent):
    """The page "/" serves to this client. An iPad and the Mac app keep the desktop page."""
    phone = bool(user_agent and _PHONE_UA.search(user_agent))
    return PADD_FILE if phone and os.path.isfile(PADD_FILE) and not os.path.exists(PADD_OFF) else PAGE_FILE


# ---------------------------------------------------------------- entry

def _ct_selfcheck():
    ps = _ps_table(["1 0 99 - - /sbin/init", "10 1 90 - - /usr/bin/llama-swap/llama-swap -config x",
                    "20 10 50 llama-swap.service - /bin/bash /h/run-x.sh",
                    "30 20 40 llama-swap.service - docker logs -f x-head",
                    "40 1 30 user@1000.service - /home/u/.opencode/bin/opencode",
                    "41 40 30 user@1000.service - docker run -i --rm img:1 stdio"])
    assert _owner_chain(ps, 30) == ("llama-swap › run-x.sh", None), _owner_chain(ps, 30)
    assert _find_client(ps, "x-head", "i", 0, set()) == 30
    assert _find_client(ps, "rand_name", "img:1", 31, set()) == 41
    assert _owner_chain(ps, 41) == ("opencode", 40)


def _ev_selfcheck():
    """The message rules, the episode logic, the store and the push sender, against synthetic worlds."""
    global EV_DIR, EV_FILE, EV_STATE, PUSH_DEV_FILE, push_send, apns_post
    saved = (EV_DIR, EV_FILE, EV_STATE, PUSH_DEV_FILE, push_send, apns_post, subprocess.run, dict(_EV), dict(_EP), dict(_PUSH_SENT and {} or {}),
             {k: os.environ.get(k) for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "FLEET_APNS_HOST")}, dict(_APNS))
    tmp = tempfile.mkdtemp(prefix="fleet-ev-")
    try:
        EV_DIR, EV_FILE, EV_STATE, PUSH_DEV_FILE = tmp, os.path.join(tmp, "events.jsonl"), os.path.join(tmp, "events-state.json"), os.path.join(tmp, "push.json")
        _EV.update(last=0, items=[], lines=0, boots={}, ma=None, blob="")
        _EP.clear()
        _PUSH_SENT.clear()
        for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "FLEET_APNS_HOST"):
            os.environ.pop(k, None)
        names = lambda acts: [a[0] for a in acts]

        # ---- episodes: debounce, once, reminders, flapping, clear delay, held sources
        spec = dict(kind="x", level="act", title="T", text="t", for_s=120, remind=True, ok="fine")
        eps = {}
        assert ev_step(eps, {"k": spec}, [], 0) == [] and ev_step(eps, {"k": spec}, [], 119) == []
        assert names(ev_step(eps, {"k": spec}, [], 121)) == ["open"] and ev_step(eps, {"k": spec}, [], 130) == []
        assert names(ev_step(eps, {"k": spec}, [], 121 + EV_REMIND_S)) == ["reminder"]
        assert names(ev_step(eps, {"k": spec}, [], 121 + 2 * EV_REMIND_S)) == ["reminder"]
        assert ev_step(eps, {"k": spec}, [], 121 + 3 * EV_REMIND_S) == [], "two reminders at most"
        assert names(ev_step(eps, {}, [], 10 ** 6)) == ["resolve"] and eps == {}
        eps = {}
        ev_step(eps, {"k": spec}, [], 0)
        ev_step(eps, {}, [], 50)                                                   # flapped away: the clock starts over
        ev_step(eps, {"k": spec}, [], 60)
        assert ev_step(eps, {"k": spec}, [], 150) == [] and names(ev_step(eps, {"k": spec}, [], 181)) == ["open"]
        slow = dict(spec, for_s=0, clear_s=600)
        eps = {}
        ev_step(eps, {"k": slow}, [], 0)
        assert ev_step(eps, {}, [], 100) == [] and ev_step(eps, {"k": slow}, [], 200) == [] and "k" in eps
        ev_step(eps, {}, [], 300)
        assert names(ev_step(eps, {}, [], 901)) == ["resolve"]
        eps = {}
        ev_step(eps, {"model-lost:a": dict(spec, for_s=0)}, [], 0)
        assert ev_step(eps, {}, ["model-lost:"], 5000) == [] and "model-lost:a" in eps, "an unknown source closes nothing"

        # ---- rules
        W = lambda **kw: dict({"now": 100000.0, "nodes": {"jean-luc": {"mem_avail_kb": 20 * 1048576, "link_mbps": 1000}, "kathryn": {"mem_avail_kb": 20 * 1048576}},
                               "running": {"a"}, "errors": {}, "pins": [], "busy": set(), "cfg": {}, "alpha": None, "defiant": None, "sessions": None,
                               "containers": None}, **kw)
        D = lambda w, open_keys=(): ev_detect(w, set(open_keys))
        assert D(W(pins=["a"]))[0] == {}
        c, h = D(W(pins=["b"], errors={"b": "boom"}))
        assert c["model-lost:b"]["for_s"] == 120 and c["model-lost:b"]["level"] == "act" and "boom" in c["model-lost:b"]["text"] and "boom" not in c["model-lost:b"]["push"]
        assert D(W(pins=["b"], busy={"b"}))[0] == {}, "loading or queued is not lost"
        assert D(W(pins=["b"], cfg={"b": 100000 - 100}))[0] == {}, "our own unload after a context change"
        assert "model-lost:b" in D(W(pins=["b"], cfg={"b": 100000 - 2000}))[0]
        c, h = D(W(pins=["b"], errors={"_swap": "down"}))
        assert list(c) == ["swap-down"] and "model-lost:" in h
        c, h = D(W(nodes={"jean-luc": {"mem_avail_kb": 20 * 1048576}, "kathryn": {"error": "timed out"}}))
        assert c["node-down:kathryn"]["for_s"] == 90 and "memory-low:kathryn" in h
        assert D(W(nodes={"jean-luc": None, "kathryn": None}))[0] == {}
        low = lambda gb: W(nodes={"jean-luc": {"mem_avail_kb": int(gb * 1048576)}, "kathryn": {"mem_avail_kb": 20 * 1048576}})
        assert "memory-low:jean-luc" in D(low(2.5))[0] and D(low(4))[0] == {}
        ml = ["memory-low:jean-luc"]
        assert "memory-low:jean-luc" in D(low(3.2), ml)[0] and D(low(3.9), ml)[0] == {} and D(low(7), ml)[0] == {}, "closes at the idle level of this machine"
        assert ev_detect(low(2.5), set())[0]["memory-low:jean-luc"]["clear_s"] == 300
        assert "link-slow:jean-luc" in D(W(nodes={"jean-luc": {"mem_avail_kb": 20 * 1048576, "link_mbps": 100}, "kathryn": {"mem_avail_kb": 20 * 1048576}}))[0]
        now = 100000.0
        A = lambda **kw: W(alpha=dict({"status": "ok", "problems": [], "errors": [], "last": {"end": now - 29 * 3600}}, **kw))
        assert D(A())[0] == {} and "backup" in D(A(last={"end": now - 31 * 3600}))[0]
        assert D(A(status="error", last=None, errors=["gate down"]))[0]["backup"]["for_s"] == 900
        assert list(D(A(status="warn", problems=["object lock renewal is off"]))[0]) == ["backup-warn"]
        assert D(W())[1].count("backup") == 1, "backup unknown while its source is None"
        site = lambda **kw: dict({"domain": "example.org", "status": 200}, **kw)
        Df = lambda **kw: W(defiant=dict({"configured": True, "sites": [site()], "errors": [], "reboot_required": False}, **kw))
        assert D(Df())[0] == {} and D(Df(configured=False))[0] == {}
        assert "web-down:example.org" in D(Df(sites=[site(status=502)]))[0] and "web-down:example.org" in D(Df(sites=[site(status=None, error="timeout")]))[0]
        assert D(Df(sites=[site(status=401)]))[0] == {}, "401 is a protected site, not a down one"
        assert "cert:example.org" in D(Df(sites=[site(cert_days=9)]))[0] and "reboot-needed" in D(Df(reboot_required=True))[0]
        c, h = D(Df(sites=[], errors=["ssh failed"]))
        assert "web-host" in c and "web-down:" in h
        K1 = "sessions-waiting:jean-luc:" + _ev_hash("s1")
        Ss = lambda *x, **kw: W(sessions={"nodes": {"jean-luc": dict({"error": None, "sessions": list(x)}, **kw)}})
        ses = lambda age, **kw: dict({"id": "s1", "stalled": True, "updated_secs_ago": age}, **kw)
        assert K1 in D(Ss(ses(3000)))[0] and D(Ss(ses(600)))[0] == {}
        assert D(Ss(ses(30000)))[0] == {} and K1 in D(Ss(ses(30000)), [K1])[0]
        assert "s1" not in "".join(D(Ss(ses(3000)))[0]), "a session id never appears in an episode key"
        assert D(Ss(ses(3000, stalled=False)))[0] == {} and "client secret" not in json.dumps(D(Ss(ses(3000, title="client secret")))[0]), "a session title never reaches a message"
        assert "sessions-waiting:jean-luc:" in D(Ss(ses(3000), error="ssh down"))[1]
        rows = lambda n, ram=800: [{"node": "jean-luc", "kind": "session", "session_idle_s": 90000, "ram_mb": ram} for _ in range(n)]
        assert "idle-sessions:jean-luc" in D(W(containers={"containers": rows(5), "errors": {}}))[0] and D(W(containers={"containers": rows(4), "errors": {}}))[0] == {}

        eps = {"web-down:a.example": {"spec": spec, "since": 0, "open": True, "gone": None, "sent": 0, "reminders": 0},
               "web-down:b.example": {"spec": spec, "since": 0, "open": True, "gone": None, "sent": 0, "reminders": 0}}
        kept = ev_filter({"web-down:a.example": spec, "web-down:b.example": spec, "node-down:x": spec}, eps, ["web-down:a.example"])
        assert sorted(kept) == ["node-down:x", "web-down:b.example"] and sorted(eps) == ["web-down:b.example"], "a muted rule is dropped silently"

        assert D(W(pins=["x-starfleet"], running={"y-starfleet"}))[0] == {}, "pushed out by another two-node model: the user's own choice"
        assert "model-lost:x-starfleet" in D(W(pins=["x-starfleet"], running={"a"}))[0]
        c = D(W(pins=["gone"], models={"a"}))[0]
        assert list(c) == ["pin-unknown:gone"] and c["pin-unknown:gone"]["level"] == "notice"
        assert "model-lost:b" in D(W(pins=["b"], models={"a", "b"}))[0]
        assert "10.0.0.1" not in D(A(status="error", last=None, errors=["ssh: connect to host 10.0.0.1"]))[0]["backup"]["push"], "no raw error through Apple"
        assert "link-slow:jean-luc" in D(W(nodes={"jean-luc": {"mem_avail_kb": 20 * 1048576}, "kathryn": {"mem_avail_kb": 20 * 1048576, "link_mbps": 1000}}))[1]
        assert "web-down:" in D(Df(sites=[]))[1], "an empty site list is unknown"
        assert ev_detect(A(last={"end": now - 31 * 3600}), set())[0]["backup"]["clear_s"] == 300
        w = {"now": time.time(), "nodes": {"kathryn": {"boot": "new"}}, "alpha": None}
        _EV["boots"], _EV["items"] = {"kathryn": "old"}, []
        ev_audit("Auto Destruct ran", "x")
        ev_points(w)
        assert not any(e["kind"] == "node-rebooted" for e in _EV["items"]), "a planned reboot is not news"
        _EV["boots"], _EV["items"] = {"kathryn": "old"}, []
        ev_points(w)
        assert _EV["items"][-1]["kind"] == "node-rebooted"
        _EV.update(last=0, items=[], lines=0, boots={}, ma=None, blob="")
        _EP.clear()
        try:
            os.remove(EV_FILE)
        except OSError:
            pass

        # ---- store: write, restart, no repeat, trim
        eps = {}
        ev_step(eps, {"k": spec}, [], 0)
        with _EV_LOCK:
            _EP.update(eps)
            pairs = ev_apply(ev_step(_EP, {"k": spec}, [], 200))
        assert len(pairs) == 1 and pairs[0][0]["id"] == 1 and pairs[0][0]["state"] == "open" and pairs[0][1] == "T", "an act entry without a push text pushes only its title"
        ev_save_state()
        saved_state = dict(_EV)
        _EV.update(last=0, items=[], lines=0, boots={}, blob="")
        _EP.clear()
        ev_load()
        assert _EV["last"] == 1 and len(_EV["items"]) == 1 and _EP["k"]["open"], "an open episode survives a restart"
        assert ev_step(_EP, {"k": spec}, [], 300) == [], "and is not announced again"
        j = events_json()
        assert j["latest"] == 1 and j["open"] == ["k"] and j["events"][0]["title"] == "T"
        for i in range(EV_KEEP + 150):
            ev_append("info", "audit", "t", "x" * 600)
        with open(EV_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) <= EV_KEEP + 100 and json.loads(lines[-1])["id"] == _EV["last"] and len(events_json()["events"]) == 200
        assert len(json.loads(lines[-1])["text"]) == 400

        # ---- push: registration, key check, signature, the curl call, dead tokens, limits
        assert push_status()["sender"] == "off" and "key" in push_status()["reason"]
        for bad in ("", "xyz", "A" * 64, "ab" * 3):
            try:
                push_register(bad, "development")
                raise AssertionError("accepted " + bad)
            except ValueError:
                pass
        try:
            push_register("ab" * 32, "nowhere")
            raise AssertionError("accepted env")
        except ValueError:
            pass
        assert push_register("ab" * 32, "development") == 1 and push_register("ab" * 32, "development") == 1 and push_register("cd" * 32, "production") == 2
        assert oct(os.stat(PUSH_DEV_FILE).st_mode & 0o777) == "0o600"
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        keyobj = ec.generate_private_key(ec.SECP256R1())
        kf = os.path.join(tmp, "AuthKey_TEST.p8")
        with open(kf, "wb") as f:
            f.write(keyobj.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.environ.update(APNS_KEY_FILE=kf, APNS_KEY_ID="KEYID12345", APNS_TEAM_ID="TEAMID6789")
        assert push_status()["sender"] == "ready" and push_status()["devices"] == 2
        _APNS.update(jwt=None, iat=0)
        jwt = _apns_jwt()
        h64, b64_, s64 = jwt.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)
        assert json.loads(base64.urlsafe_b64decode(pad(h64))) == {"alg": "ES256", "kid": "KEYID12345"} and json.loads(base64.urlsafe_b64decode(pad(b64_)))["iss"] == "TEAMID6789"
        raw = base64.urlsafe_b64decode(pad(s64))
        assert len(raw) == 64
        keyobj.public_key().verify(utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                                   (h64 + "." + b64_).encode(), ec.ECDSA(hashes.SHA256()))
        calls = []

        class _R:
            def __init__(self, out): self.stdout = out
        def fake_run(args, **kw):
            calls.append((args, kw.get("input", ""), open(args[args.index("--data-binary") + 1][1:]).read()))
            return _R(calls_out[0])
        subprocess.run = fake_run
        calls_out = ['\n200']
        dev = {"token": "ab" * 32, "env": "development"}
        assert apns_post(dev, {"aps": {"alert": {"title": "T"}}, "go": "#/models"}, "c1") == (200, "")
        args, cin, body = calls[-1]
        assert jwt not in " ".join(args) and "authorization: bearer " + jwt in cin and "api.sandbox.push.apple.com/3/device/" + "ab" * 32 in cin
        assert "apns-collapse-id: c1" in cin and json.loads(body)["go"] == "#/models" and "--http2" in args
        apns_post(dev, {}, 'x"\noutput = "/tmp/pwned')
        assert '\noutput' not in calls[-1][1] and 'apns-collapse-id: x--output---' in calls[-1][1], "no curl option can be smuggled in through an episode key"
        assert apns_post({"token": "cd" * 32, "env": "production"}, {}, "")[0] == 200 and "https://api.push.apple.com/3/device/" in calls[-1][1]
        calls_out[0] = '{"reason":"BadDeviceToken"}\n400'
        assert apns_post(dev, {}, "") == (400, "BadDeviceToken")
        calls_out[0] = '{"reason":"BadDeviceToken"}\n400'
        assert push_send("T", "b", "#/models", 1, "c") == 0 and push_devices() == [], "dead tokens are dropped"
        push_register("ab" * 32, "development")
        calls_out[0] = '\n200'
        assert push_send("T", "b", "", 1, "c") == 1
        subprocess.run = saved[6]
        sent = []
        push_send = lambda title, text, go="", badge=0, collapse="": sent.append((title, text, go, collapse)) or 1
        mk = lambda i: ({"level": "act", "state": "open", "kind": "model-lost", "title": f"T{i}", "go": "#/models", "episode": f"e{i}"}, f"text {i}")
        push_dispatch([mk(1)])
        assert sent == [("T1", "text 1", "#/models", _ev_hash("e1", 20))], "the push thread is opaque"
        sent.clear()
        push_dispatch([mk(i) for i in range(5)])
        assert len(sent) == 1 and sent[0][0] == "5 things need you" and "T0" in sent[0][1]
        sent.clear()
        push_dispatch([({"level": "notice", "state": "open", "title": "n", "go": "", "episode": "n"}, "n"), ({"level": "act", "state": "resolved", "title": "r", "go": "", "episode": "r"}, "r")])
        assert sent == [], "only act entries that opened are pushed"
        for _ in range(8):
            push_dispatch([mk(1)])
        assert len(_PUSH_SENT) == EV_PUSH_PER_H, "hourly limit"
        ses = lambda i: ({"level": "act", "state": "open", "kind": "sessions-waiting", "title": "Session waiting", "go": "#/systems/sessions", "episode": f"s{i}"}, "x")
        _PUSH_SENT.clear()
        sent.clear()
        push_dispatch([ses(1), ses(2), ses(3)])
        assert len(sent) == 1 and sent[0][0] == "Sessions waiting" and "3 coding sessions" in sent[0][1], "waiting sessions are one push"
        _PUSH_SENT[:] = [time.time()] * (EV_PUSH_PER_H - 2)
        sent.clear()
        push_dispatch([ses(1)])
        assert sent == [], "the last two slots of the hour are for the other kinds"
        push_dispatch([mk(1)])
        assert len(sent) == 1
        _PUSH_SENT.clear()
        _PUSH_PENDING.clear()
        ok = [0]
        push_send = lambda *a, **k: ok[0]
        push_dispatch([mk(1)])
        assert len(_PUSH_PENDING) == 1 and _PUSH_PENDING[0][2] == 1, "a failed send waits for a retry"
        _PUSH_PENDING[0] = (0,) + _PUSH_PENDING[0][1:]
        ok[0] = 1
        push_dispatch([])
        assert _PUSH_PENDING == [] and len(_PUSH_SENT) == 1, "and goes out on a later tick"
    finally:
        (EV_DIR, EV_FILE, EV_STATE, PUSH_DEV_FILE, push_send, apns_post, subprocess.run) = saved[:7]
        _EV.clear(); _EV.update(saved[7]); _EP.clear(); _EP.update(saved[8]); _PUSH_SENT.clear()
        for k, v in saved[10].items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        _APNS.clear(); _APNS.update(saved[11])
        shutil_rmtree = __import__("shutil").rmtree
        shutil_rmtree(tmp, ignore_errors=True)


def selfcheck():
    global _q_current, AUTODESTRUCT_CODE
    _ct_selfcheck()
    _ev_selfcheck()
    # phone interface: "/" serves it to phones only, and a session request can name nothing but a session
    assert page_for("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Starfleet Command/1.0 (macOS)") == PAGE_FILE
    assert page_for(None) == PAGE_FILE and page_for("Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)") == PAGE_FILE
    assert _PHONE_UA.search("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148 StarfleetCommand/1.0")
    assert _PHONE_UA.search("Mozilla/5.0 (Linux; Android 15; Pixel 9) Chrome/130 Mobile Safari/537.36")
    for _bad in (("jean-luc", "../../etc/passwd", "opencode"), ("jean-luc", "a*", "opencode"), ("jean-luc", "a b c d e f", "opencode"),
                 ("jean-luc", "", "opencode"), ("jean-luc", "x" * 65, "opencode"), ("jean-luc", "short", "opencode"),
                 ("nowhere", "ses_0123456789", "opencode"), ("jean-luc", "ses_0123456789", "sh"),
                 ("jean-luc", "ses_0123456789;id", "claude-code")):
        try:
            session_tail(*_bad)
            raise AssertionError(f"session_tail accepted {_bad}")
        except ValueError:
            pass
    # web guard: public tech passes; customer names, people, secrets, personal data and pasted text do not.
    # Runs on a fixed test list (customer term "acme corp"/"acme"), not this host's real one.
    _wg_saved = dict(_WG)
    try:
        _WG.update(at=time.time(), terms=re.compile(r"(?<!\w)(?:acme corp|acme)(?!\w)"), secrets=[])
        assert web_guard({"query": "qdrant hybrid search tutorial"}) is None
        assert web_guard({"query": "Azure OpenAI rate limits", "focusMode": "webSearch"}) is None
        assert web_guard({"query": "what is new in vllm?"}) is None
        for bad in ("Acme Corp RAG evaluation", "ACME-rollout", "password: hunter22", "sk-proj-abcdefghijklmnop1234",
                    "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9", "x@example.com", "+49 621 1234567", "y" * (WEB_ARG_MAX + 1)):
            assert web_guard({"query": bad}), bad
        assert web_guard({"history": [["human", "Acme"]]}), "nested arguments are checked too"
    finally:
        _WG.clear()
        _WG.update(_wg_saved)
    assert re.compile(r"(?<!\w)(?:" + (r"(?!)") + r")(?!\w)").search("? ! ") is None, "an empty list matches nothing"
    # A pin missing from `running` (evicted by ttl, or by llama-swap swapping in a
    # different member of its swap:true group) must be touched immediately, same
    # tick, regardless of the refresh clock -- that's the whole fix.
    assert _pins_needing_touch(["a", "b"], {"b"}, due_for_refresh=False) == ["a"]
    assert _pins_needing_touch(["a", "b"], {"a", "b"}, due_for_refresh=False) == []
    assert _pins_needing_touch(["a", "b"], {"a", "b"}, due_for_refresh=True) == ["a", "b"]
    assert _pins_needing_touch([], {"a"}, due_for_refresh=True) == []

    # Auto Destruct fails closed. With no code configured an empty submitted code must not match an
    # empty configured one; with one configured, the case and padding of what was typed do not matter.
    _code = AUTODESTRUCT_CODE
    try:
        AUTODESTRUCT_CODE = ""
        for sent in ("", None, "ANY"):
            try:
                _check_code(sent)
                raise AssertionError("an unset code must refuse everything")
            except AutoDestructOff:
                pass
        AUTODESTRUCT_CODE = "AB-1"
        _check_code(" ab-1 ")
        for sent in ("", None, "AB-2"):
            try:
                _check_code(sent)
                raise AssertionError("a wrong code must be rejected")
            except PermissionError:
                pass
    finally:
        AUTODESTRUCT_CODE = _code

    # Light loads wait for any LLM load, then run strictly one after the other, FIFO.
    assert is_light("embeddinggemma2") and is_light("qwen3-reranker-8b-kathryn") and not is_light("ornith-1.5-122b")
    _llm_busy.add("some-llm")
    assert enqueue_light("e1", "k", None) == 1 and enqueue_light("r1", "k", None) == 2
    assert enqueue_light("e1", "k", None) is None                 # no duplicates
    assert _next_job(wait=0) is None                              # held back while an LLM loads
    assert dequeue("r1") and not dequeue("r1")
    _llm_busy.clear()
    assert enqueue_light("r2", "k", None) == 2
    assert _next_job(wait=0)["model"] == "e1" and _q_current == "e1"
    assert enqueue_light("e1", "k", None) is None                 # still running: not queueable again
    _q_current = None
    assert _next_job(wait=0)["model"] == "r2"
    _q_current = None
    assert _next_job(wait=0) is None

    # evict() must only return once llama-swap has stopped listing the model, and must
    # raise (not pretend) if it never does.
    global swap_unload, swap_running
    _u, _r = swap_unload, swap_running
    try:
        calls, listed = [], [True, True, False]
        swap_unload = lambda m, timeout=30, key=None: calls.append((m, key))
        swap_running = lambda: {"m"} if listed.pop(0) else set()
        evict("m", "k", wait=30)
        assert calls == [("m", "k")] and not listed
        swap_running = lambda: {"m"}
        try:
            evict("m", "k", wait=0)
            raise AssertionError("evict must fail while the model is still resident")
        except OSError:
            pass
    finally:
        swap_unload, swap_running = _u, _r

    # ETA maths: median (not mean) of recorded cold loads, and None while a
    # model has no history -- the page shows an indeterminate bar for that.
    assert _median([]) is None
    assert _median([10]) == 10
    assert _median([30, 10, 20]) == 20
    assert eta_for("never-loaded", {}) is None
    assert eta_for("m", {"m": [100, 10, 12]}) == 12

    # Load progress comes from the engine's own narration, and must read the
    # FURTHEST phase in the log rather than whichever line happens to be last.
    assert _phase_from_text("") == (None, None)
    assert _phase_from_text("Init torch distributed begin.") == (0.0, "starting engine")
    assert _phase_from_text(
        "Loading safetensors checkpoint shards:  40% Completed | 2/5") == (0.276, "loading weights")
    # SGLang names the same phase differently, and it is the LONGEST one -- missing
    # it left the bar frozen at 0% for the 15 minutes the weights actually take.
    assert _phase_from_text(
        "Multi-thread loading shards:  50% Completed | 23/46 [08:12<08:03, 21.0s/it]"
    ) == (0.33, "loading weights")
    # the loader that prints no percentage at all still has to move off "starting"
    assert _phase_from_text("[TP0] Load weight begin. avail mem=75.06 GB") == (
        0.06, "loading weights")
    # weights done, then capture at 0%: the later phase wins even though its own
    # percentage is lower -- this is exactly the SGLang draft-model replay case.
    assert _phase_from_text(
        "Load weight end. elapsed=737.14 s\n"
        "Capturing batches (bs=1 avail_mem=17.01 GB):   0%|") == (0.70, "capturing CUDA graphs")
    assert _phase_from_text(
        "Capturing batches (bs=1 avail_mem=17.01 GB): 100%|") == (0.90, "capturing CUDA graphs")
    # uvicorn is up long before the model answers: that is 0.99, not done.
    assert _phase_from_text("INFO: Application startup complete.") == (0.90, "warming up")
    assert _phase_from_text("The server is fired up and ready to roll!") == (0.99, "ready")
    # A model with no resolved checkpoint has no log to read, and that must be a
    # quiet "no progress" rather than an exception on every poll.
    assert load_progress(_log_mark("definitely-not-a-member")) == (None, None)
    assert load_progress(None) == (None, None)

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
    # A rope override must lift the member past its checkpoint's own window, and a plain
    # argv must not be mistaken for one.
    assert _rope_override_max(
        'flags=[--json-model-override-args {"text_config":{"max_position_embeddings":524288,'
        '"rope_parameters":{"rope_type":"yarn","factor":2.0}}}]') == 524288
    assert _rope_override_max("flags=[--context-length 524288]") is None
    assert _rope_override_max("") is None
    # The SPEED column must prefer a measured override over whatever the name happens to say.
    # Load the real overrides file FIRST: tps_of() reloads it on mtime, and that reload
    # rebinds _tps_override wholesale -- so without this the values injected below are gone
    # by the time the first assert calls tps_of(), and this check fails on any machine that
    # actually has a tps.json. Priming it makes the reload a no-op and the injection stick.
    _load_tps_overrides()
    _tps_override.clear(); _tps_override["qwen38fn-long-sglang-tp2-starfleet"] = 23.7
    _tps_override["gemma4-26b-54tps-jean-luc"] = 51.0
    assert tps_of("qwen38fn-long-sglang-tp2-starfleet") == 23.7   # name has no marker
    assert tps_of("gemma4-26b-54tps-jean-luc") == 51.0            # override beats the name
    assert tps_of("qwen3.6-35b-57tps-mtp4-jean-luc") == 57        # falls back to the name
    assert tps_of("nomic-embed-text") is None                     # neither: stays blank
    _tps_override.clear()
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
    # TP=2 without the -starfleet suffix still spans both, and -tp2 in the MIDDLE of a name
    # must not be mistaken for it -- that member's own -starfleet suffix decides.
    assert node_of("ds4-nvfp4-tp2") == ["jean-luc", "kathryn"]
    assert node_of("qwen38fn-long-sglang-tp2-starfleet") == ["jean-luc", "kathryn"]
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
    for fn in (poll_nodes, poll_swap, keepalive, ctx_max_worker, load_queue_worker, ct_worker, events_worker):
        threading.Thread(target=fn, daemon=True).start()
    pins = load_pins()
    print(f"Fleet on http://{host}:{a.port}   (llama-swap {SWAP})", flush=True)
    print(f"pinned: {', '.join(pins) if pins else 'none'}", flush=True)
    ThreadingHTTPServer((host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
