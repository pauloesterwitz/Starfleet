#!/bin/bash
# run-ds4-tp2-cluster.sh — DeepSeek-V4-Flash-NVFP4 on vLLM, tensor-parallel=2 ACROSS BOTH Sparks.
#
#   Jean-Luc (head, fabric 10.100.0.2)  +  Kathryn (worker, fabric 10.100.0.1)  over the 200GbE link.
#   168GB MIXED_PRECISION weights (routed experts NVFP4, attn/shared/head BF16) / TP=2 -> ~84GB
#   weights/shard -> ~95-100GB RESIDENT per node once you add fp8 KV + activations. The reference
#   config is TP=4 (needs 4 GPUs); on 2 Sparks TP=2 is the only option and it fits ONLY on an
#   otherwise-empty node. NOT room for a co-resident small model — this is exclusive-class.
#   CORRECTED 2026-08-28: this script IS lifecycle-managed by llama-swap now. The header used to
#   say "started OUT OF BAND, NOT lifecycle-managed" - true until 2026-08-27, when ds4-nvfp4-tp2
#   became a normal `starfleet` member. ds4-tp2-proxy-guard.sh either launches this script (and
#   owns it, so ttl/eviction tear both nodes down via the trap below) or parks and proxies when a
#   cluster is ALREADY serving on :8000, which is how a systemd/hand-started preload still works.
#   ds4-tp2.service remains valid for starting it by hand. See config.yaml.
#
# Usage:
#   ~/llama-swap/run-ds4-tp2-cluster.sh            # gate both pools, start cluster, serve on 127.0.0.1:8000 (foreground)
#   ~/llama-swap/run-ds4-tp2-cluster.sh down       # tear down head + worker on both nodes
#   DRYRUN=1 ~/llama-swap/run-ds4-tp2-cluster.sh    # print the plan (docker/ssh argv) — no GPU, no network. Self-check.
#
# Safety (your kernel-panic failure mode is OOM):
#   - memcheck.sh gates EACH pool BEFORE its ~84GB shard loads -> clean refuse, never OOM. Jean-Luc
#     locally; Kathryn via HER memcheck over ssh (her pool, her /proc/meminfo).
#   - Page cache is dropped on both nodes first for the big members: memcheck alone is NOT a
#     sufficient gate, because MemAvailable counts reclaimable page cache as available while NVRM
#     will not force reclaim hard enough during a 78 GiB burst. That logic lives in memcheck.sh
#     (NOT "below" in this file - it was moved there 2026-08-26 so every launcher and the REMOTE
#     Kathryn gate get it from one place). memcheck.sh also frees idle ComfyUI weights (17 GiB per
#     node, invisible to `docker stats`) and reaps an orphaned ds4-tp2 cluster.
#   - Teardown trap docker-stops BOTH containers on EXIT/INT/TERM/HUP so neither pool is left holding weights.
#   - NCCL + Ray pinned to the fabric iface / 10.100.0.x — never Tailscale, never the RJ45 LAN.
set -uo pipefail

# ── config (override via env) ───────────────────────────────────────────────────────────────────
IMAGE="${IMAGE:-vllm-nightly-ray:local}"             # nightly-aarch64 + ray 2.56.0 (base image dropped ray); MUST be the IDENTICAL build on both nodes (see save/load step)
MODELS_DIR="${MODELS_DIR:-$HOME/models}"             # user-owned NVFP4 fleet; mounted :/models:ro (matches serve-vllm-dflash.sh)
MODEL_SUBDIR="${MODEL_SUBDIR:-DeepSeek-V4-Flash-NVFP4}"
MODEL="/models/$MODEL_SUBDIR"

# ── ds4 KILL SWITCH. Deprecated 2026-07-27, REACTIVATED 2026-08-26 — currently SATISFIED. ──
# Guards EVERY ds4 entry point that reaches this script (ds4-tp2.service, a manual run,
# the llama-swap member) while leaving the starfleet models untouched — they pass their own
# MODEL_SUBDIR. It is kept as a deliberate off-switch, not because ds4 is off:
#   turn ds4 OFF again:  rm ~/.ds4-enabled        turn it back on:  touch ~/.ds4-enabled
case "$MODEL_SUBDIR" in
  *DeepSeek-V4*|*ds4*)
    # `down` (teardown) must ALWAYS work — it frees a running cluster and never starts one.
    if [ "${1:-up}" != "down" ] && [ ! -e "$HOME/.ds4-enabled" ]; then
      echo "ds4 ($MODEL_SUBDIR) is deprecated (2026-07-27). Re-activate: touch ~/.ds4-enabled" >&2
      exit 1
    fi ;;
esac
                         # container path vLLM serves
SERVED_NAME="${SERVED_NAME:-ds4-nvfp4-tp2}"          # must match the llama-swap member + its proxy
TP="${TP:-2}"; PP="${PP:-1}"                          # parallelism across the 2 Sparks: TP*PP must = 2 GPUs.
                                                     # TP=2/PP=1 (default) or TP=1/PP=2 (pipeline). Served-name stays constant.
PORT="${PORT:-8000}"                                 # Jean-Luc loopback; llama-swap proxies here
NEED_MB="${NEED_MB:-90000}"                          # per-node fit gate (~78GB shard + KV/activations)
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"              # NOT the checkpoint's 1048576 — a 1M KV cache OOMs instantly; tune DOWN if tight
GPU_UTIL="${GPU_UTIL:-0.72}"                          # MEASURED: weights=77.9GB/shard. 0.85 OOM'd Jean-Luc (NVRM NV_ERR_NO_MEMORY:
                                                     # cross-node TP needs NCCL/CUTLASS buffers the profiler doesn't reserve). 0.72*121=~88GB
                                                     # budget (78 weights + ~9 KV) leaves ~30GB device headroom. Tune UP only on an empty node.

# fabric (from `ip -o addr show` on each node)
HEAD_IP="${HEAD_IP:-10.100.0.2}";   HEAD_IF="${HEAD_IF:-enp1s0f1np1}"    # Jean-Luc
WORKER_HOST="${WORKER_HOST:-kathryn.fritz.box}"                          # LAN name: bare tailnet name breaks BatchMode
WORKER_IP="${WORKER_IP:-10.100.0.1}"                                     # Kathryn fabric IP
WORKER_IF="${WORKER_IF:-}"                                               # auto-detected on Kathryn if empty
RAY_PORT="${RAY_PORT:-6379}"

HEAD_CTR=ds4-tp2-head
WORKER_CTR=ds4-tp2-worker
MEMCHECK="$HOME/llama-swap/memcheck.sh"
RING_TIMEOUT="${RING_TIMEOUT:-180}"                  # seconds to wait for both Ray nodes to join
SSH="ssh -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new"

log(){ echo "$(date -Is) [ds4-tp2] $*" >&2; }

# ── teardown: BOTH nodes, always ────────────────────────────────────────────────────────────────
_torn=0
teardown(){
  [ "$_torn" = "1" ] && return 0; _torn=1   # idempotent: signal-handler + EXIT trap both call this
  log "teardown: stopping $HEAD_CTR (local) + $WORKER_CTR (@$WORKER_HOST)"
  docker stop -t 15 "$HEAD_CTR" >/dev/null 2>&1 || true
  docker rm   -f    "$HEAD_CTR" >/dev/null 2>&1 || true
  $SSH "$WORKER_HOST" "docker stop -t 15 $WORKER_CTR >/dev/null 2>&1; docker rm -f $WORKER_CTR >/dev/null 2>&1" || true
}

case "${1:-up}" in
  down) teardown; exit 0 ;;
  up|"") : ;;
  *) echo "usage: $0 [up|down]   (DRYRUN=1 to print the plan)" >&2; exit 2 ;;
esac

# ── RUN LOG: TRUNCATED every launch, so it can never carry a previous run's failure ────────────────
# WHY (2026-08-26). ds4-tp2-bench.sh watches this file for failure markers to fail fast instead of
# waiting out its 40-minute readiness loop. That only works if the file describes THIS run. It was
# a shared, append-only scratch log, so it still held the 2026-07-17 PP=2 crash ("Executor failed"
# x4, "EngineDeadError", "died unexpectedly", "CLUSTER_SCRIPT_EXITED rc=137") six weeks later.
# A bench started against a perfectly healthy cluster matched one of those on its FIRST pass,
# declared "FAILED to serve", and fired TEARDOWN into the healthy model. The bench script's own
# header documents this happening once already, and it nearly happened again today.
# `tee` without -a truncates on open, so the staleness class is gone rather than worked around:
# there is no window in which this file describes anything but the current launch. Deleting the
# file instead would only have traded a false-positive (kills a healthy cluster) for a
# false-negative (bench waits the full 40 min on a real failure).
# Output still reaches journald/stdout as before, so `journalctl --user -u ds4-tp2` is unchanged.
# PER-MODEL log, added 2026-08-28. It was one shared file, which fixed the staleness bug but
# created a forensics one: EVERY starfleet member goes through this script, so each launch wiped
# the previous model's failure evidence. Debugging four failed models from the compat matrix was
# impossible because only the last one's log survived. Per-model keeps both properties: still
# truncated per run (so ds4-tp2-bench.sh can never match a stale marker) but a failure stays
# readable until THAT model runs again.
# ds4-tp2-test.log is kept as a symlink to the most recent run, because ds4-tp2-bench.sh defaults
# to that path and watches it for fast-fail markers.
RUN_LOG="${RUN_LOG:-$HOME/llama-swap/logs/run-${MODEL_SUBDIR}.log}"
mkdir -p "$(dirname "$RUN_LOG")"
# Not under DRYRUN: the plan below is a read-only self-check, and Fleet runs it to learn
# each member's checkpoint and context. Opening the log first made every such probe
# truncate the last REAL run's log and re-point the symlink -- on 2026-09-14 it wiped
# ds4's 2026-09-07 run log, and Kathryn's hourly mirror held the only surviving copy.
if [ -z "${DRYRUN:-}" ]; then
  ln -sfn "$RUN_LOG" "$HOME/llama-swap/ds4-tp2-test.log" 2>/dev/null || true
  exec > >(tee "$RUN_LOG") 2>&1
fi

# Shared docker flags. Values are space-free by construction, so a plain word-split string works both
# locally (head) and embedded in the ssh command (worker) — one definition, no array/ssh serialization.
# ponytail: intentional word-splitting; keep every value here space-free.
common_flags(){ # $1=fabric_ip $2=iface $3=models_dir
  # USE_ROCE=1 -> RDMA over the ConnectX-7 (rocep1s0f1, ACTIVE on enp1s0f1np1 on both nodes): map
  #   /dev/infiniband in, enable the IB path, pin the active HCA (the f0/P2p ones are DOWN). This is
  #   what makes cross-node TP=2 all-reduce fast. Default 0 -> TCP sockets (proven 17.7 tok/s).
  local ib
  if [ "${USE_ROCE:-0}" = "1" ]; then
    # MUST use --device, NOT `-v /dev/infiniband`: the bind-mount makes the nodes VISIBLE but Docker's
    # device cgroup DENIES access -> ibv_devinfo "Failed to open device" -> NCCL finds no usable IB
    # device and SILENTLY falls back to NET/Socket. That was the real RoCE blocker (VERIFIED 2026-07-17).
    # The image's rdma-core 39.0 vs host 50.0 was a RED HERRING — 39.0 opens the ConnectX-7 fine once
    # the device cgroup allows it. --cap-add=IPC_LOCK + memlock=-1 for RDMA pinned memory.
    ib="-e NCCL_IB_DISABLE=0 -e NCCL_IB_HCA=${ROCE_HCA:-rocep1s0f1} --cap-add=IPC_LOCK --device /dev/infiniband/rdma_cm"
    for u in 0 1 2 3; do ib="$ib --device /dev/infiniband/uverbs$u"; done
  else
    ib="-e NCCL_IB_DISABLE=1"
  fi
  echo "--rm --network host --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all" \
       "--ipc=host --ulimit memlock=-1 --ulimit stack=67108864" \
       "-v $3:/models:ro" \
       "-e VLLM_HOST_IP=$1 -e NCCL_SOCKET_IFNAME=$2 -e GLOO_SOCKET_IFNAME=$2" \
       "$ib -e NCCL_DEBUG=${NCCL_DEBUG:-WARN} -e NCCL_DEBUG_SUBSYS=${NCCL_DEBUG_SUBSYS:-INIT} -e HF_HUB_OFFLINE=1 -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -e RAY_memory_monitor_refresh_ms=${RAY_MEM_MONITOR_MS:-250} -e VLLM_FLASHINFER_WORKSPACE_BUFFER_SIZE=${VLLM_FI_WS:-413138944} -e VLLM_PLE_CPU_OFFLOAD=${VLLM_PLE_CPU_OFFLOAD:-0}"
}

HEAD_RAY="ray start --head --node-ip-address=$HEAD_IP --port=$RAY_PORT --block"
WORKER_RAY="ray start --address=$HEAD_IP:$RAY_PORT --node-ip-address=$WORKER_IP --block"

# vLLM serve flags. DeepSeek NVFP4 config from your run-deepseek-v4-flash.sh (the authoritative source)
# + TP=2 + Ray backend. Mixed-precision NVFP4 should auto-detect from the checkpoint; if it doesn't,
# add `--quantization modelopt_fp4`. --enforce-eager first: your patch_cudagraph_align.py notes CUDA
# graphs crash mid-decode WITH DFlash; there is NO DFlash here, so cudagraphs (ENFORCE_EAGER=0) are
# worth trying for throughput. Default stays eager (proven stable at util 0.72).
EAGER=(); [ "${ENFORCE_EAGER:-1}" = "1" ] && EAGER=(--enforce-eager)
# Model-specific flags. DeepSeek default: cutlass MoE (NOT b12x — swiglu_limit=10.0 clamp; on GB10
# sm_121 cutlass is the working backend, trtllm needs family 100) + fp8 KV. Override MODEL_FLAGS for
# other models, e.g. gemma NVFP4: MODEL_FLAGS="--quantization modelopt".
#
# TOOL CALLING ADDED 2026-08-27. The default was missing --enable-auto-tool-choice /
# --tool-call-parser / --reasoning-parser, which every serve-starfleet.sh key has carried since
# 2026-08-10. That was invisible until ds4-nvfp4-tp2 became a first-class llama-swap member:
# serve-starfleet.sh sets MODEL_FLAGS explicitly for its keys, but ds4-tp2-proxy-guard.sh does NOT,
# so ds4 fell through to this default and would have failed EVERY opencode session at step 0 with
#   '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
# The model loads and plain chat works, which is exactly why this hides until an agent client tries.
# Parser names verified against the image registries (the method serve-starfleet.sh prescribes):
#   vllm/tool_parsers/__init__.py  -> "deepseek_v4" -> DeepSeekV4ParserToolAdapter
#   vllm/reasoning/__init__.py     -> "deepseek_v4" -> DeepSeekV4ParserReasoningAdapter
# A reasoning parser also matters here for a second reason: the single-node GGUF leaked raw
# chain-of-thought into message.content ("Hmm, the user said ...") because it has none.
read -r -a MFLAGS <<< "${MODEL_FLAGS:---kv-cache-dtype fp8 --moe-backend flashinfer_cutlass --enable-auto-tool-choice --tool-call-parser deepseek_v4 --reasoning-parser deepseek_v4}"
SERVE=(vllm serve "$MODEL"
  --tensor-parallel-size "$TP"
  --pipeline-parallel-size "$PP"
  --distributed-executor-backend ray
  --trust-remote-code
  "${MFLAGS[@]}"
  --max-model-len "$MAX_MODEL_LEN"
  --gpu-memory-utilization "$GPU_UTIL"
  "${EAGER[@]}"
  --served-model-name "$SERVED_NAME"
  --host 127.0.0.1 --port "$PORT")

# ── DRYRUN: print the whole plan and exit (no GPU, no ssh) — the runnable self-check ──────────────
if [ -n "${DRYRUN:-}" ]; then
  wif="${WORKER_IF:-<detected-on-kathryn-at-runtime>}"
  if [ "$NEED_MB" -ge 80000 ]; then
    echo "# DROP CACHE:       inside each gate below, per node (NEED_MB $NEED_MB >= 80000). See memcheck.sh."
  else
    echo "# DROP CACHE:       skipped (NEED_MB $NEED_MB < 80000, small member)"
  fi
  echo "# GATE (Jean-Luc):  $MEMCHECK $NEED_MB true"
  echo "# GATE (Kathryn):   $SSH $WORKER_HOST \"\$HOME/llama-swap/memcheck.sh $NEED_MB true\""
  echo "# HEAD (Jean-Luc):"
  echo "  docker run -d --name $HEAD_CTR $(common_flags "$HEAD_IP" "$HEAD_IF" "$MODELS_DIR") --entrypoint /bin/bash $IMAGE -c '$HEAD_RAY'"
  echo "# WORKER (Kathryn, over ssh):"
  echo "  docker run -d --name $WORKER_CTR $(common_flags "$WORKER_IP" "$wif" "\$HOME/models") --entrypoint /bin/bash $IMAGE -c '$WORKER_RAY'"
  echo "# SERVE (exec into head, foreground):"
  echo "  docker exec $HEAD_CTR ${SERVE[*]}"
  echo "# TEARDOWN on exit: docker stop $HEAD_CTR  +  ssh $WORKER_HOST docker stop $WORKER_CTR"
  exit 0
fi

# ── preflight: weights on BOTH nodes (fail clean, don't start a doomed cluster) ───────────────────
have_weights(){ [ -n "$(find "$1/$MODEL_SUBDIR" -maxdepth 1 -name '*.safetensors' 2>/dev/null | head -1)" ]; }
if ! have_weights "$MODELS_DIR"; then
  log "MISSING weights on Jean-Luc ($MODELS_DIR/$MODEL_SUBDIR). Run ~/models/dl-ds4-nvfp4.sh first."; exit 1
fi
if ! $SSH "$WORKER_HOST" "f=\$(find \$HOME/models/$MODEL_SUBDIR -maxdepth 1 -name '*.safetensors' 2>/dev/null | head -1); [ -n \"\$f\" ]"; then
  log "MISSING weights on Kathryn (~/models/$MODEL_SUBDIR). dl-ds4-nvfp4.sh rsyncs them over — let it finish."; exit 1
fi

# detect Kathryn's fabric iface for WORKER_IP, unless pinned via env
if [ -z "$WORKER_IF" ]; then
  WORKER_IF=$($SSH "$WORKER_HOST" "ip -o -4 addr show | awk -v ip='$WORKER_IP' '\$4 ~ \"^\"ip\"/\" {print \$2; exit}'" 2>/dev/null)
  [ -n "$WORKER_IF" ] || { log "could not find Kathryn's iface for $WORKER_IP — set WORKER_IF=..."; exit 1; }
fi
log "fabric: head $HEAD_IP/$HEAD_IF  worker $WORKER_IP/$WORKER_IF"

# ── fit gates: refuse (clean) if either pool can't hold ~84GB right now ────────────────────────────
# NOTE (2026-08-26): each gate below now ALSO drops that node's page cache first when need_mb is
# large, because MemAvailable counts reclaimable cache and NVRM will not force reclaim hard enough
# during a 78 GiB burst (it OOM'd twice with this gate green). That logic lives in memcheck.sh so
# every launcher AND the remote Kathryn gate get it from one place; see the comment there. It is
# deliberately NOT duplicated here.
# ponytail: gate-only (memcheck ... true), NOT a held reservation. A held 100GB reservation would
# double-count against MemAvailable once the shard is resident. Trade-off: no concurrent-load race
# guard during the ~10min cold load — fine for a cluster you start deliberately on a near-empty node.
log "gate Jean-Luc pool (need ${NEED_MB}MB)"
"$MEMCHECK" "$NEED_MB" true || { log "Jean-Luc: won't fit — free the pool (unload ollama/llama-swap models) and retry"; exit 1; }
log "gate Kathryn pool (need ${NEED_MB}MB, remote)"
$SSH "$WORKER_HOST" "\$HOME/llama-swap/memcheck.sh $NEED_MB true" || { log "Kathryn: won't fit — free her pool and retry"; exit 1; }

# Armed only now — nothing to tear down before this point.
# A signal must tear down AND EXIT. Previously `trap teardown EXIT INT TERM HUP` only tore down and
# let the script RUN ON, so a `systemctl stop` during the load fell out of the ring-wait loop and
# printed a bogus "ring did NOT form — check fabric" + exit 1 (sending you after a fabric fault that
# doesn't exist, and leaving the unit "failed"). Exit 143 = SIGTERM, which the unit's
# SuccessExitStatus accepts, so a clean stop reports "inactive".
trap teardown EXIT
trap '_sig=$?; log "signal received — tearing down"; teardown; exit 143' INT TERM HUP

# ── start Ray: head, then worker ──────────────────────────────────────────────────────────────────
docker rm -f "$HEAD_CTR" >/dev/null 2>&1 || true
$SSH "$WORKER_HOST" "docker rm -f $WORKER_CTR >/dev/null 2>&1" || true

log "starting Ray head ($HEAD_CTR)"
docker run -d --name "$HEAD_CTR" $(common_flags "$HEAD_IP" "$HEAD_IF" "$MODELS_DIR") \
  --entrypoint /bin/bash "$IMAGE" -c "$HEAD_RAY" >/dev/null

log "waiting for head Ray to accept connections"
for _ in $(seq 1 30); do docker exec "$HEAD_CTR" ray status >/dev/null 2>&1 && break; sleep 2; done

log "starting Ray worker ($WORKER_CTR @ $WORKER_HOST)"
$SSH "$WORKER_HOST" "docker run -d --name $WORKER_CTR $(common_flags "$WORKER_IP" "$WORKER_IF" "\$HOME/models") --entrypoint /bin/bash $IMAGE -c '$WORKER_RAY'" >/dev/null

log "waiting for the 2-node ring (timeout ${RING_TIMEOUT}s)"
ring_ok=""
for _ in $(seq 1 $((RING_TIMEOUT/5))); do
  if docker exec "$HEAD_CTR" python3 -c "import ray;ray.init(address='auto');import sys;sys.exit(0 if sum(bool(n['Alive']) for n in ray.nodes())>=2 else 1)" >/dev/null 2>&1; then
    ring_ok=1; break
  fi
  sleep 5
done
[ -n "$ring_ok" ] || { log "ring did NOT form — check fabric ($HEAD_IP<->$WORKER_IP) and NCCL iface. Aborting."; exit 1; }
log "ring up: 2 nodes / 2 GPUs. Loading model (cold start ~10-15min)."

# background readiness ping — cold start is long; log when /health goes green
( for _ in $(seq 1 240); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { log "READY: http://127.0.0.1:$PORT/v1 (served-model-name=$SERVED_NAME)"; break; }; sleep 5; done ) &

# ── serve (foreground; trap tears down both nodes when this ends) ──────────────────────────────────
docker exec "$HEAD_CTR" "${SERVE[@]}"
