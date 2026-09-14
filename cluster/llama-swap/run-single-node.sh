#!/bin/bash
# run-single-node.sh — serve ONE model on Jean-Luc alone (no Ray, no Kathryn), for the single-node
# half of the single-vs-TP=2 sweep. Deliberately mirrors run-ds4-tp2-cluster.sh's docker + vLLM flags
# VERBATIM so the only variable between the two runs is parallelism — anything else and the comparison
# is measuring the harness, not the model.
#
# Why not reuse serve-vllm-dflash.sh: that one is llama-swap-managed (per-model KEY wiring, its own
# images, the memcheck reservation-file protocol). Adding sweep models as KEYs would entangle these
# benchmarks with the live llama-swap members. This stays standalone and disposable.
#
# Usage: MODEL_SUBDIR=X SERVED_NAME=y ./run-single-node.sh [up|down]   (DRYRUN=1 prints the plan)
set -uo pipefail

IMAGE="${IMAGE:-vllm-nightly-ray:local}"          # same image as the TP=2 runs — comparability
MODELS_DIR="${MODELS_DIR:-$HOME/models}"
MODEL_SUBDIR="${MODEL_SUBDIR:-}"   # validated AFTER the case below — `down` MUST work without it.
                                   # The bench's TEARDOWN calls `run-single-node.sh down` with no env;
                                   # a ${VAR:?} here aborted teardown, leaving a 97 GB model resident
                                   # for 40 min. That pressure swapped vLLM out and corrupted the sweep.
SERVED_NAME="${SERVED_NAME:-single-node}"
PORT="${PORT:-8000}"                              # same port so ds4-tp2-bench.sh works unchanged
NEED_MB="${NEED_MB:-90000}"                       # memcheck gate: refuse cleanly, never OOM (OOM = kernel panic here)
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
GPU_UTIL="${GPU_UTIL:-0.72}"
CTR="${CTR:-single-node-serve}"                   # NOT ds4-tp2-* : must never collide with a cluster run
MEMCHECK="$HOME/llama-swap/memcheck.sh"

log(){ echo "$(date -Is) [single-node] $*"; }
teardown(){ log "teardown: stopping $CTR"; docker stop -t 15 "$CTR" >/dev/null 2>&1; docker rm -f "$CTR" >/dev/null 2>&1; }
trap teardown EXIT INT TERM HUP

case "${1:-up}" in
  down) teardown; trap - EXIT; exit 0 ;;   # reached with no MODEL_SUBDIR — teardown only needs $CTR
  up|"") : ;;
  *) echo "usage: $0 [up|down]" >&2; exit 2 ;;
esac

[ -n "$MODEL_SUBDIR" ] || { echo "set MODEL_SUBDIR" >&2; trap - EXIT; exit 2; }
MODEL="/models/$MODEL_SUBDIR"

# --runtime=nvidia + NVIDIA_VISIBLE_DEVICES=all, NOT --gpus all: per serve-vllm-dflash.sh, --gpus all
# needs the nvidia-persistenced socket which is absent on this box.
FLAGS="--rm --network host --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all
 --ipc=host --ulimit memlock=-1 --ulimit stack=67108864
 -v $MODELS_DIR:/models:ro
 -e HF_HUB_OFFLINE=1 -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
 -e VLLM_PLE_CPU_OFFLOAD=${VLLM_PLE_CPU_OFFLOAD:-0}"

EAGER=(); [ "${ENFORCE_EAGER:-1}" = "1" ] && EAGER=(--enforce-eager)
read -r -a MFLAGS <<< "${MODEL_FLAGS:---kv-cache-dtype fp8 --moe-backend flashinfer_cutlass}"
# The image ENTRYPOINT is ["vllm","serve"] — pass ONLY model + flags. Prefixing "vllm serve" here
# yields `vllm serve vllm serve /models/...` -> "unrecognized arguments". The cluster script CAN write
# the full command because it overrides --entrypoint /bin/bash for Ray and then `docker exec`s it.
SERVE=("$MODEL"
  --tensor-parallel-size 1
  --trust-remote-code
  "${MFLAGS[@]}"
  --max-model-len "$MAX_MODEL_LEN"
  --gpu-memory-utilization "$GPU_UTIL"
  "${EAGER[@]}"
  --served-model-name "$SERVED_NAME"
  --host 127.0.0.1 --port "$PORT")

if [ "${DRYRUN:-0}" = "1" ]; then
  echo "# GATE:  $MEMCHECK $NEED_MB true"
  echo "# SERVE: docker run -d --name $CTR $FLAGS $IMAGE ${SERVE[*]}"
  trap - EXIT; exit 0
fi

[ -d "$MODELS_DIR/$MODEL_SUBDIR" ] || { log "!!! $MODELS_DIR/$MODEL_SUBDIR not found"; exit 1; }
if ! find "$MODELS_DIR/$MODEL_SUBDIR" -maxdepth 1 -name '*.safetensors' | grep -q .; then
  log "!!! no safetensors in $MODEL_SUBDIR"; exit 1
fi

log "gate Jean-Luc pool (need ${NEED_MB}MB)"
"$MEMCHECK" "$NEED_MB" true || { log "!!! refusing to load — not enough free memory"; exit 1; }

docker rm -f "$CTR" >/dev/null 2>&1
log "starting $CTR ($MODEL_SUBDIR, TP=1, util $GPU_UTIL, ctx $MAX_MODEL_LEN)"
# shellcheck disable=SC2086
docker run -d --name "$CTR" $FLAGS "$IMAGE" "${SERVE[@]}" >/dev/null || { log "!!! docker run failed"; exit 1; }

( for _ in $(seq 1 240); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { log "READY: http://127.0.0.1:$PORT/v1 (served-model-name=$SERVED_NAME)"; break; }
    sleep 5
  done ) &
READY_PID=$!

docker logs -f "$CTR" 2>&1
log "container exited"
# Reap the readiness prober: if the container dies early it would otherwise keep polling for up to
# 20 min, holding the script (and its EXIT trap) open long after there is anything to wait for.
kill "$READY_PID" 2>/dev/null
