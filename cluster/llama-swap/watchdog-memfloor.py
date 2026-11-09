#!/usr/bin/env python3
"""
Hard free-memory floor for the SGLang TP=2 cluster. The kill-switch that lets a context test
run with thin margins without betting the box.

WHY IT EXISTS: run-sglang-tp2-cluster.sh:234 has long claimed "the bench/monitor wrapper tears
both nodes down at a hard free-memory floor". No such code existed anywhere in the fleet. On
GB10 unified memory an OOM does not just kill the model - it can take the whole node with it
(llama-swap, ComfyUI, the MCP servers, and any agent session running there). This makes the
worst case "the model restarts" instead of "the box hangs".

WHAT IT DOES: every second, reads MemAvailable on Jean-Luc (local) and Kathryn (one persistent
ssh stream, not a new ssh per sample). If EITHER falls below --floor-gb, it kills the cluster on
both nodes and exits 3. It records the minimum seen per node - the real headroom figure a
context test is trying to establish.

THE SAFETY CATCH THAT MATTERS: every SGLang TP=2 member in this fleet uses the SAME container
names (sglang-tp2-head / sglang-tp2-worker). A naive `docker kill sglang-tp2-head` could kill a
pinned Qwen3.8-Flash-Next. So it kills ONLY if the head's --model-path is the checkpoint it was
told to guard (--guard-model). Anything else: it logs and refuses.
"""
import argparse, subprocess, sys, time, json

ap = argparse.ArgumentParser()
ap.add_argument("--floor-gb", type=float, required=True)
ap.add_argument("--guard-model", required=True, help="e.g. /models/GLM-5.3-Flash-AWQ-kda-w8")
ap.add_argument("--log", required=True)
ap.add_argument("--dry-run", action="store_true", help="report a trip but never kill anything")
a = ap.parse_args()
KA = "10.100.0.1"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]

def log(msg):
    with open(a.log, "a") as f: f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")

def local_free():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"): return int(line.split()[1]) / 1048576
    return None

def ka_stream():
    return subprocess.Popen(SSH + [KA, "while :; do awk '/MemAvailable/{printf \"%.2f\\n\", $2/1048576}' "
                                       "/proc/meminfo; sleep 1; done"],
                            stdout=subprocess.PIPE, text=True, bufsize=1)

def head_model():
    r = subprocess.run(["docker", "inspect", "sglang-tp2-head", "--format", "{{json .Config.Cmd}}"],
                       capture_output=True, text=True)
    if r.returncode: return None
    cmd = json.loads(r.stdout or "[]")
    return cmd[cmd.index("--model-path") + 1] if "--model-path" in cmd else None

def teardown(reason):
    m = head_model()
    if m != a.guard_model:
        log(f"TRIP ({reason}) but head runs {m!r}, not {a.guard_model!r} - REFUSING to kill")
        return
    if a.dry_run:
        log(f"TRIP ({reason}) - dry run, not killing"); return
    log(f"TRIP ({reason}) - killing both nodes")
    subprocess.run(["docker", "kill", "sglang-tp2-head"], capture_output=True)
    subprocess.run(SSH + [KA, "docker kill sglang-tp2-worker"], capture_output=True)

import select
ka = ka_stream(); ka_val = None; min_jl = min_ka = 1e9; last_note = 0
log(f"armed: floor {a.floor_gb} GB, guarding {a.guard_model}{' (dry run)' if a.dry_run else ''}")
while True:
    jl = local_free()
    if select.select([ka.stdout], [], [], 2.5)[0]:
        line = ka.stdout.readline().strip()
        if line: ka_val = float(line)
    if ka.poll() is not None:              # stream died: restart it, never go blind on Kathryn
        log("kathryn stream lost - reconnecting"); ka = ka_stream(); ka_val = None
    min_jl = min(min_jl, jl)
    if ka_val is not None: min_ka = min(min_ka, ka_val)
    if time.time() - last_note > 30:
        log(f"free jl={jl:.2f} ka={ka_val if ka_val is None else round(ka_val,2)}  min jl={min_jl:.2f} ka={min_ka:.2f}")
        last_note = time.time()
    tripped = jl < a.floor_gb or (ka_val is not None and ka_val < a.floor_gb)
    if tripped:
        teardown(f"jl={jl:.2f} ka={ka_val} < floor {a.floor_gb}")
        log(f"MIN jl={min_jl:.2f} ka={min_ka:.2f}")
        sys.exit(3)
    time.sleep(1)
