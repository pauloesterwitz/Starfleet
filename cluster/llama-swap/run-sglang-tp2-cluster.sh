#!/bin/bash
# run-sglang-tp2-cluster.sh — SGLang serving a model TP=2 ACROSS BOTH Sparks.
#
# The SGLang sibling of run-ds4-tp2-cluster.sh. Same shape (gate both pools, start both nodes,
# tear both down on exit) but SIMPLER: SGLang has its own torch.distributed bootstrap
# (--nnodes/--node-rank/--dist-init-addr), so there is NO Ray head/worker and no ring wait.
#
# WHY SGLANG FOR Qwen3.8-Flash-Next, when the fleet is otherwise vLLM (added 2026-08-28):
# vLLM cannot fit this model on 2 Sparks and the reason is structural, not tuning. Its PLE
# n-gram table (51B params) is expanded to BF16 (~95 GB) on rank 0 and every attempt OOM'd the
# head node at ~116/121 GB. VERIFIED IN THIS IMAGE, SGLang does the two things vLLM would not:
#   1. srt/models/qwen4_exp.py builds ngram_embedding as a VocabParallelEmbedding with
#      params_dtype=torch.float8_e4m3fn when the checkpoint sets ple_embedding_dtype=
#      "float8_e4m3fn" — the table STAYS fp8 instead of being dequantized.
#   2. Being VocabParallel, it SHARDS across the TP group: ~48 GB of table becomes ~24 GB/node
#      at TP=2, instead of one 95 GB copy on rank 0.
# Together that is the difference between ~116 GB (OOM) and an estimated ~61 GB per node.
#
# DO NOT pass --ple-offload-embedding for an fp8/NVFP4 checkpoint. That flag moves the table to
# CPU pinned memory UNSHARDED (48 GB on EVERY node) which is strictly worse here; it exists for
# BF16 checkpoints and sglang auto-enables it only when dtype==bfloat16 (arg_groups/overrides.py).
#
# Usage:
#   ~/llama-swap/run-sglang-tp2-cluster.sh              # serve on 127.0.0.1:$PORT (foreground)
#   ~/llama-swap/run-sglang-tp2-cluster.sh down         # tear down both nodes
#   DRYRUN=1 ~/llama-swap/run-sglang-tp2-cluster.sh     # print the plan, touch nothing
#
# Safety: identical contract to the vLLM cluster script — memcheck gates EACH pool before any
# weights load (clean refusal, never OOM), and the teardown trap stops BOTH containers on
# EXIT/INT/TERM/HUP so neither Spark is left holding weights.
#
# ── STATUS 2026-08-28: SERVING. 37.4 tok/s single-stream, CUDA graphs ON. ───────────────────
# Use IMAGE=lmsysorg/sglang:dev-qwen38-next-local (the default via serve-sglang.sh). RETIRED 2026-09-08:
# the old custom sglang-qwen38fn-gb10:local build corrupts decode on GB10. The STOCK
# lmsysorg/sglang:qwen38flashnext CANNOT serve this model on GB10 — see below.
#   WORKS: qwen4_exp loads natively; the 51B PLE table shards across BOTH nodes and STAYS fp8
#          (peaks JL 102GB / KA 97GB — balanced, never OOM, vs vLLM's fatal 116GB on rank 0
#          alone); NCCL over RoCE; NVFP4 MoE; KV cache 603840-625280 tokens, ~23GB free/node.
#
# THE ONE THING THAT BLOCKED IT, AND THE FIX (keep this — it will look like a mystery again):
#   Symptom: at warmup AND at graph capture, always
#     MLIRError: expects `coord` and shape of view are weakly congruent
#     ...in flash_attn cute 'FlashAttentionForwardSm120'
#   Cause (layers/attention/qwen_sparse_attn_backend.py): _resolve_trtllm_sparse_decode() opens
#   with `if not is_sm100_supported(): return None`. GB10 is sm_121 — Blackwell, but NOT sm_100 —
#   so the fast trtllm-gen decode path is refused and QSA falls back to the FA4 cute varlen
#   kernel, which does not JIT-compile for these shapes. Classic FA2 would be preferred "when
#   installed" but is absent (and has no sm_121 kernels anyway).
#   NOT flag-fixable: VERIFIED identical failure with --attention-backend flashinfer, with
#   triton, with --disable-cuda-graph and with --disable-flashinfer-autotune, because the
#   model's own QSA layers select that kernel regardless of the backend flag.
#   FIX: ~/vllm-ray-build/Dockerfile.sglang-gb10 relaxes that gate to accept sm_12x, so GB10
#   takes the trtllm-gen path. One line. With it, warmup and CUDA-graph capture both succeed.
# A newer stock image will not help yet: lmsysorg/sglang:dev-cu13 (mainline) has NO qwen4_exp
# and NO qsa at all — that support currently lives only in the qwen38flashnext tag. Re-check
# after upstream merges; if a future tag handles sm_121 natively, drop the patched image.
set -uo pipefail

IMAGE="${IMAGE:-lmsysorg/sglang:qwen38flashnext}"     # arm64 + native qwen4_exp (verified in-image)
MODELS_DIR="${MODELS_DIR:-$HOME/models}"
# NOT `${MODEL_SUBDIR:?}` — that made `down` unusable. TEARDOWN MUST ALWAYS WORK, exactly as
# run-ds4-tp2-cluster.sh guarantees for its own `down`. MEASURED 2026-09-02: with an orphaned
# pair holding ~89 GB, `./run-sglang-tp2-cluster.sh down` aborted on the unset variable before
# reaching the case below, printed nothing useful, and left the memory stranded — so the one
# command you reach for in exactly that emergency was the one that did not work. The validation
# now lives after the case statement, where only the `up` path needs it.
MODEL_SUBDIR="${MODEL_SUBDIR:-}"
MODEL="/models/$MODEL_SUBDIR"
SERVED_NAME="${SERVED_NAME:-sglang-tp2}"
PORT="${PORT:-8100}"                                  # Jean-Luc loopback; llama-swap proxies here
DIST_PORT="${DIST_PORT:-20000}"                       # torch.distributed rendezvous on the fabric
TP="${TP:-2}"
NEED_MB="${NEED_MB:-70000}"
MEM_FRACTION="${MEM_FRACTION:-0.75}"                  # SGLang's --mem-fraction-static (KV+weights)
CTX_LEN="${CTX_LEN:-16384}"
MODEL_FLAGS="${MODEL_FLAGS:-}"

HEAD_IP="${HEAD_IP:-10.100.0.2}";  HEAD_IF="${HEAD_IF:-enp1s0f1np1}"
WORKER_HOST="${WORKER_HOST:-kathryn.fritz.box}"
WORKER_IP="${WORKER_IP:-10.100.0.1}"
WORKER_IF="${WORKER_IF:-}"

HEAD_CTR=sglang-tp2-head
WORKER_CTR=sglang-tp2-worker

# ── PER-LAUNCH OWNERSHIP ──────────────────────────────────────────────────────────────────────
# The container NAMES stay fixed on purpose — fleet-ui/fleet.py does `docker logs
# sglang-tp2-head`, and the bench scripts and memcheck's reaper all match on those names.
# Renaming per launch would give the same safety but break every one of those. Instead each
# launch STAMPS the containers it creates with its own id, and teardown refuses to kill a
# container carrying somebody else's stamp.
#
# WHY (2026-09-02): every launcher arms `trap teardown EXIT` against these fixed names, so a
# launcher exiting killed whatever cluster was live — including one it never started. NINE
# orphaned launchers from an Aug-28 benchmark run were still alive with armed traps, silently
# killing every load for hours; llama-swap showed "group: model unloaded" and OpenCode showed
# "upstream command exited prematurely". flock (below) stops two launchers coexisting, but this
# stamp is the actual guarantee: even if the lock is bypassed, lost, or the file is deleted, a
# teardown can only ever destroy containers this process created.
RUN_ID="${RUN_ID:-$$-$(date +%s)}"
OWNER_LABEL="sglang-tp2-run"

# Kill a container only if it carries OUR stamp. $1=container $2=optional ssh host.
kill_if_ours(){
  local ctr="$1" host="${2:-}" owner
  if [ -n "$host" ]; then
    owner=$($SSH "$host" "docker inspect --format '{{index .Config.Labels \"$OWNER_LABEL\"}}' $ctr 2>/dev/null" 2>/dev/null | tr -d '\r')
  else
    owner=$(docker inspect --format "{{index .Config.Labels \"$OWNER_LABEL\"}}" "$ctr" 2>/dev/null)
  fi
  # No container at all -> nothing to do. Unlabelled -> pre-2026-09-02 container, ours to clean.
  [ -z "$owner" ] && owner="$RUN_ID"
  if [ "$owner" != "$RUN_ID" ]; then
    log "NOT killing $ctr — owned by run $owner, not us ($RUN_ID)"
    return 1
  fi
  if [ -n "$host" ]; then $SSH "$host" "docker kill $ctr >/dev/null 2>&1" >/dev/null 2>&1
  else docker kill "$ctr" >/dev/null 2>&1; fi
  return 0
}
MEMCHECK="$HOME/llama-swap/memcheck.sh"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new"
log(){ echo "$(date -Is) [sglang-tp2] $*" >&2; }

WORKER_LOG="${WORKER_LOG:-$HOME/llama-swap/.sglang-tp2-worker.log}"

_torn=0
teardown(){
  [ "$_torn" = "1" ] && return 0; _torn=1
  # Kill the readiness pinger FIRST. It holds the write end of the RUN_LOG tee pipe, and bash
  # blocks on exit until that tee drains — see the note at the pinger. Without this the launcher
  # finishes all its work and then hangs indefinitely holding the flock.
  [ -n "${READY_PINGER:-}" ] && kill "$READY_PINGER" 2>/dev/null
  # KILL BOTH NODES FIRST, collect evidence second. llama-swap SIGKILLs this wrapper if it
  # does not exit promptly, and everything still queued at that moment simply never runs.
  # The old order (ssh log fetch, then two SEQUENTIAL `docker stop -t 15` across two hosts,
  # 30-40s worst case) reliably lost that race and orphaned the pair -- MEASURED 2026-09-02:
  # head+worker survived an unload, kept answering /health on the old port, and the next load
  # re-attached to the stale server instead of restarting, so a context change silently did
  # nothing. memcheck.sh's reaper does not catch that case either: it only reaps a container
  # that is NOT serving, and this one was perfectly healthy -- just no longer owned.
  #
  # `docker kill`, not `stop -t 15`: this is a teardown, there is nothing to flush, and the
  # grace period we actually get is far shorter than 30s. Both nodes in parallel.
  # OWNERSHIP-CHECKED (2026-09-02). Only containers stamped with OUR RUN_ID are killed, so an
  # exiting launcher can never destroy a cluster it did not create. `down` sweeps regardless
  # (FORCE_TEARDOWN=1) because there the operator IS asking to kill whatever is there.
  log "teardown: killing $HEAD_CTR (local) + $WORKER_CTR (@$WORKER_HOST)  [run $RUN_ID]"
  if [ "${FORCE_TEARDOWN:-0}" = "1" ]; then
    docker kill "$HEAD_CTR" >/dev/null 2>&1 &                                   _k1=$!
    $SSH "$WORKER_HOST" "docker kill $WORKER_CTR >/dev/null 2>&1" >/dev/null 2>&1 & _k2=$!
  else
    kill_if_ours "$HEAD_CTR" &                                                  _k1=$!
    kill_if_ours "$WORKER_CTR" "$WORKER_HOST" &                                 _k2=$!
  fi
  # WAIT ONLY FOR THE TWO KILL JOBS — never a bare `wait`. MEASURED 2026-09-07: a bare `wait`
  # here waits for EVERY child, which includes the `tee` from `exec > >(tee "$RUN_LOG")`. That
  # tee cannot exit while this script still holds the write end of its pipe, so teardown blocked
  # here forever, holding the flock, with zero containers running — and the tee-kill further down
  # at "Kill the RUN_LOG tee" never got the chance to run because it sits BELOW this line. That
  # deadlock is what made every later load fail with "another sglang-tp2 launcher already holds
  # the lock", which llama-swap reports to callers as "upstream command exited prematurely".
  # Three earlier attempts missed this because they all targeted the tee rather than the wait.
  wait "$_k1" "$_k2" 2>/dev/null || true

  # SAVE THE WORKER'S LOG. On any failure the worker holds the ONLY copy of the real error:
  # the head just reports "Connection closed by peer / remote worker crashing", which says
  # nothing about WHY. Learned the hard way 2026-08-28 — a whole debug cycle was lost to an
  # unreadable remote crash. Still safe to read here: these containers run WITHOUT --rm, so a
  # killed container keeps its logs until the explicit `docker rm` below. Doing it after the
  # kill means the GPU memory is already free even if we are SIGKILLed mid-fetch.
  $SSH "$WORKER_HOST" "docker logs $WORKER_CTR 2>&1 | tail -400" > "$WORKER_LOG" 2>/dev/null || true
  [ -s "$WORKER_LOG" ] && log "worker log saved to $WORKER_LOG"

  docker rm -f "$HEAD_CTR" >/dev/null 2>&1 || true
  $SSH "$WORKER_HOST" "docker rm -f $WORKER_CTR >/dev/null 2>&1" || true

  # RELEASE THE RUN_LOG PIPE. `exec > >(tee "$RUN_LOG")` above makes this shell's stdout the
  # write end of a pipe into tee, and bash will not exit until that tee exits — while tee will
  # not exit until EVERY holder of the write end is closed, including this shell itself.
  # Result: a launcher that has finished all its work sits in do_wait forever, HOLDING THE FLOCK,
  # so nothing can ever launch again. MEASURED twice on 2026-09-03: 45 min and 47 min, zero
  # containers, and it failed two benchmark arms before being spotted. Killing the readiness
  # pinger (above) removes one holder; this removes the last one — us. Pointing stdout at
  # /dev/null closes the pipe, tee sees EOF and exits, and bash can finally leave.
  exec >/dev/null 2>&1
  # Kill the RUN_LOG tee. `TEE_PID=$!` after a >(...) substitution is NOT reliable — bash does
  # not consistently set $! for process substitutions, and MEASURED 2026-09-03 it stayed empty
  # while the tee ran happily as our child, wedging the launcher in do_wait for 50 minutes with
  # the flock held. Finding it by parentage works regardless of bash version.
  for _t in $(pgrep -P $$ -x tee 2>/dev/null); do kill "$_t" 2>/dev/null; done
  [ -n "${TEE_PID:-}" ] && kill "$TEE_PID" 2>/dev/null
}

case "${1:-up}" in
  # `down` is the operator explicitly asking to clear the box, so it sweeps regardless of who
  # owns the containers — that is the whole point of the emergency command.
  down) FORCE_TEARDOWN=1 teardown; exit 0 ;;   # never needs MODEL_SUBDIR — see the note above
  up|"") : ;;
  *) echo "usage: $0 [up|down]   (DRYRUN=1 to print the plan)" >&2; exit 2 ;;
esac

# Only the `up` path actually needs a model; `down` above has already exited.
[ -n "$MODEL_SUBDIR" ] || { echo "set MODEL_SUBDIR" >&2; exit 2; }

# ── PER-MODEL RUN LOG, added 2026-08-29 (parity with run-ds4-tp2-cluster.sh) ──────────────────
# This launcher had NO log of its own, so when qwen38fn-sglang-tp2-starfleet failed under
# llama-swap the only evidence was llama-swap's useless "upstream command exited prematurely" -
# the engine's actual stderr went nowhere. That is the same forensics gap the vLLM launcher had.
# TRUNCATED per launch (tee without -a), so a file can never describe a previous run, and
# per-MODEL so one model's failure survives until that same model runs again.
# Output still reaches stdout/journald exactly as before.
RUN_LOG="${RUN_LOG:-$HOME/llama-swap/logs/sglang-${MODEL_SUBDIR}.log}"
mkdir -p "$(dirname "$RUN_LOG")"
exec > >(tee "$RUN_LOG") 2>&1
# PID of the tee started by the process substitution above. bash keeps its OWN descriptor for a
# >(...) substitution, so redirecting stdout later (exec >/dev/null) does NOT close the pipe and
# bash still blocks in do_wait for this tee on exit — MEASURED three times on 2026-09-03, each a
# finished launcher stuck 45+ min holding the flock with zero containers, failing benchmark arms.
# Killing this pid in teardown is the only reliable release.
TEE_PID=$!

# Shared docker flags. Space-free by construction so one string works locally and inside ssh.
# --security-opt seccomp=unconfined: docker's default profile blocks io_uring_setup, which the
# PLE reader uses; without it the reader silently falls back to preadv and gets much slower.
#
# SGLANG_ENABLE_TP_MEMORY_INBALANCE_CHECK=0 is DELIBERATE, not a workaround for a real fault.
# sglang refuses to start if any rank has <90% of another rank's free VRAM
# (distributed/bootstrap.py::_check_tp_memory_balance); with the check off it merely warns.
# On this fleet the imbalance is PERMANENT and expected: Jean-Luc also hosts ComfyUI, vane and
# the MCP containers, so she measured 97.7 GB free against Kathryn's 113.5 GB — 97.7 < 102.1
# (=113.5*0.9) and the launch aborted, twice. Nothing was wrong; the nodes are just not twins.
# We do NOT lose the protection it provides: memcheck.sh gates BOTH pools before any weights
# load, and the bench/monitor wrapper tears both nodes down at a hard free-memory floor.
# Set SGLANG_TP_BALANCE_CHECK=1 to restore the upstream behaviour.
common_flags(){ # $1=fabric_ip $2=iface $3=models_dir
  # NO --rm. A crashed worker must remain inspectable: with --rm the container (and the only
  # copy of the real traceback) evaporates the instant it exits, and the head only ever reports
  # "Connection closed by peer / remote worker crashing". teardown() does `docker rm -f` on both
  # nodes anyway, and each launch starts with `docker rm -f`, so nothing accumulates.
  # --label stamps THIS launch's id onto the container. teardown()'s kill_if_ours reads it back
  # and refuses to kill anything carrying a different id; memcheck's reaper reads it to tell an
  # abandoned cluster from a live one. Space-free by construction, like every value here.
  echo "--network host --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all" \
       "--label $OWNER_LABEL=$RUN_ID" \
       "--ipc=host --shm-size=32g --ulimit memlock=-1 --ulimit stack=67108864" \
       "--security-opt seccomp=unconfined" \
       "-v $3:/models:ro" \
       "-e NCCL_SOCKET_IFNAME=$2 -e GLOO_SOCKET_IFNAME=$2" \
       "-e NCCL_IB_DISABLE=0 -e NCCL_IB_HCA=${ROCE_HCA:-rocep1s0f1} --cap-add=IPC_LOCK --device /dev/infiniband/rdma_cm" \
       "--device /dev/infiniband/uverbs0 --device /dev/infiniband/uverbs1" \
       "--device /dev/infiniband/uverbs2 --device /dev/infiniband/uverbs3" \
       "-e NCCL_DEBUG=${NCCL_DEBUG:-WARN} -e HF_HUB_OFFLINE=1" \
       "-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" \
       "-e SGLANG_ENABLE_TP_MEMORY_INBALANCE_CHECK=${SGLANG_TP_BALANCE_CHECK:-0}"
}

read -r -a MFLAGS <<< "$MODEL_FLAGS"
SGL=(python3 -m sglang.launch_server
  --model-path "$MODEL"
  --served-model-name "$SERVED_NAME"
  --tp-size "$TP"
  --nnodes 2
  --dist-init-addr "$HEAD_IP:$DIST_PORT"
  --mem-fraction-static "$MEM_FRACTION"
  --context-length "$CTX_LEN"
  --trust-remote-code
  "${MFLAGS[@]}")

if [ -n "${DRYRUN:-}" ]; then
  wif="${WORKER_IF:-<detected-on-kathryn>}"
  echo "# GATE (Jean-Luc):  $MEMCHECK $NEED_MB true"
  echo "# GATE (Kathryn):   $SSH $WORKER_HOST \"\$HOME/llama-swap/memcheck.sh $NEED_MB true\""
  echo "# HEAD (rank 0, serves :$PORT):"
  echo "  docker run -d --name $HEAD_CTR $(common_flags "$HEAD_IP" "$HEAD_IF" "$MODELS_DIR") $IMAGE ${SGL[*]} --node-rank 0 --host 127.0.0.1 --port $PORT"
  echo "# WORKER (rank 1, @$WORKER_HOST):"
  echo "  docker run -d --name $WORKER_CTR $(common_flags "$WORKER_IP" "$wif" "\$HOME/models") $IMAGE ${SGL[*]} --node-rank 1 --host 127.0.0.1 --port $PORT"
  echo "# TEARDOWN on exit: docker stop $HEAD_CTR + ssh $WORKER_HOST docker stop $WORKER_CTR"
  exit 0
fi

# ── SINGLE INSTANCE. Non-negotiable, because the teardown trap is destructive. ────────────────
# Every launcher arms `trap teardown EXIT` against the FIXED container names sglang-tp2-head /
# -worker. So a second launcher does not merely duplicate work: whenever ANY of them exits it
# kills whichever cluster is live, including one it never started.
# MEASURED 2026-09-02: NINE orphaned launchers from a 2026-08-28 benchmark session were still
# alive (reparented to init, traps armed). Every successful load was being killed within minutes
# by one of them, which llama-swap reported as "group: model unloaded" and OpenCode showed as
# "upstream command exited prematurely". Hours of apparently random failures, one cause.
# flock -n: if another launcher holds the lock we exit IMMEDIATELY and — critically — BEFORE the
# teardown trap is armed below, so a rejected duplicate can never kill the running cluster.
exec 200>"$HOME/.gb10/sglang-tp2.lock"
if ! flock -n 200; then
  # A lock holder with NO containers has finished its work and is merely failing to exit — it
  # cannot be serving anything, so waiting on it is pointless. Give it a short grace (a real
  # launcher between `docker run` calls is only briefly container-less), then take the lock.
  # Without this a single wedged launcher blocks every subsequent launch indefinitely; that is
  # what killed benchmark arms for 45 minutes on 2026-09-03 before the pinger fix below.
  _stale=1
  for _ in $(seq 1 6); do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q sglang-tp2-head; then _stale=0; break; fi
    sleep 5
  done
  if [ "$_stale" = "1" ] && flock -n 200; then
    log "took the lock from a holder that has no containers (finished but not exited)"
  else
    log "another sglang-tp2 launcher already holds the lock — refusing to start a second one"
    log "(if that is stale: ~/llama-swap/run-sglang-tp2-cluster.sh down, then retry)"
    exit 1
  fi
fi

[ -n "$WORKER_IF" ] || WORKER_IF=$($SSH "$WORKER_HOST" "ip -o -4 addr show | awk '\$4 ~ /^10\.100\.0\.1\// {print \$2; exit}'" 2>/dev/null)
[ -n "$WORKER_IF" ] || { log "could not detect Kathryn's fabric iface"; exit 1; }
log "fabric: head $HEAD_IP/$HEAD_IF  worker $WORKER_IP/$WORKER_IF"

# gate_wait: retry the memory gate for a bounded window instead of refusing instantly.
# WHY (MEASURED 2026-09-07): llama-swap's starfleet group is swap:true, so switching cluster
# models starts THIS launcher while the outgoing model is still releasing its ~100GB. The gate
# then saw 12.9GB free, exited within 9s, and llama-swap surfaced "upstream command exited
# prematurely" — the 500 Paul hit in OpenCode and in Starfleet Command. Nothing was actually
# wrong: the memory arrives seconds later. Waiting turns a spurious failure into a slow success.
# ponytail: a bounded retry, not swap-state detection — if the memory never frees we exit exactly
# as before, so the OOM protection is unchanged. Tune with GATE_WAIT_S=0 to restore old behavior.
GATE_WAIT_S="${GATE_WAIT_S:-150}"
gate_wait() {  # $1=label  $2=command...
  local label="$1"; shift
  local deadline=$(( SECONDS + GATE_WAIT_S ))
  while :; do
    "$@" && return 0
    [ "$SECONDS" -ge "$deadline" ] && return 1
    log "$label: not enough free yet — retrying (another model may still be unloading)"
    sleep 10
  done
}
# STALE-CONTAINER CLEANUP BEFORE THE GATES (moved 2026-09-14). It used to run AFTER them, which
# deadlocked on a worker orphaned alone on Kathryn: her gate saw the orphan's ~75 GB as used,
# refused "Kathryn: won't fit", and exited before reaching the `docker rm -f` that would have
# freed it -- so every sglang-tp2 load failed until someone ran `down` by hand. Safe here: the
# flock (taken above) guarantees no other launcher is running, so anything left is stale.
docker rm -f "$HEAD_CTR" >/dev/null 2>&1 || true
$SSH "$WORKER_HOST" "docker rm -f $WORKER_CTR >/dev/null 2>&1" || true
log "gate Jean-Luc pool (need ${NEED_MB}MB, up to ${GATE_WAIT_S}s)"
gate_wait "Jean-Luc" "$MEMCHECK" "$NEED_MB" true || { log "Jean-Luc: won't fit — free the pool and retry"; exit 1; }
log "gate Kathryn pool (need ${NEED_MB}MB, remote)"
gate_wait "Kathryn" $SSH "$WORKER_HOST" "\$HOME/llama-swap/memcheck.sh $NEED_MB true" || { log "Kathryn: won't fit — free her pool and retry"; exit 1; }

trap teardown EXIT
trap '_sig=$?; log "signal received — tearing down"; teardown; exit 143' INT TERM HUP


# Worker FIRST: it blocks on the rendezvous until the head appears, so starting it first
# removes a race where the head initializes distributed before the worker is listening.
#
# ${SGL[*]} flattened into one string is fine for the HEAD (word-split locally, no
# re-parse), but the WORKER command travels over ssh as a single string that a SECOND
# shell (on Kathryn) parses — any quotes embedded in an element (e.g.
# --default-chat-template-kwargs {"reasoning_effort":"xhigh"}) get eaten by that second
# parse, corrupting the JSON. printf %q re-escapes each element so Kathryn's shell
# reconstructs the original argv unchanged.
sgl_remote=""
for _a in "${SGL[@]}"; do sgl_remote+=" $(printf '%q' "$_a")"; done

log "starting worker rank 1 ($WORKER_CTR @ $WORKER_HOST)"
$SSH "$WORKER_HOST" "docker run -d --name $WORKER_CTR $(common_flags "$WORKER_IP" "$WORKER_IF" "\$HOME/models") $IMAGE$sgl_remote --node-rank 1 --host 127.0.0.1 --port $PORT" >/dev/null \
  || { log "worker failed to start"; exit 1; }

log "starting head rank 0 ($HEAD_CTR, serves 127.0.0.1:$PORT)"
docker run -d --name "$HEAD_CTR" $(common_flags "$HEAD_IP" "$HEAD_IF" "$MODELS_DIR") \
  $IMAGE ${SGL[*]} --node-rank 0 --host 127.0.0.1 --port "$PORT" >/dev/null \
  || { log "head failed to start"; exit 1; }

log "both nodes up; loading model (cold start ~10-15min). Following head logs."
# PID tracked so teardown can kill it. This subshell inherits the script's stdout, which is a
# pipe into the `tee` of the RUN_LOG (exec > >(tee ...) above). bash will not exit until that
# tee exits, and tee will not exit until every holder of the pipe's write end is gone — so a
# still-sleeping readiness pinger pins the whole launcher alive AFTER its work is finished.
# MEASURED 2026-09-03: a finished launcher sat in do_wait for 45 minutes with no containers
# left, holding the flock, and every subsequent benchmark arm failed with "another sglang-tp2
# launcher already holds the lock". Killing this pinger in teardown closes the last fd.
( for _ in $(seq 1 320); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { log "READY: http://127.0.0.1:$PORT (served-model-name=$SERVED_NAME)"; break; }
    sleep 5
  done ) &
READY_PINGER=$!

# Foreground on the head's logs: when it exits, the trap tears down BOTH nodes.
# BACKGROUND + wait, NEVER a bare foreground `docker logs -f`. MEASURED 2026-09-10: bash does
# NOT interrupt a running foreground command to service a trap -- it queues the handler until
# that command returns. `docker logs -f` returns only when the container dies, so on llama-swap's
# TTL unload the SIGTERM sat queued, `teardown` never ran, llama-swap escalated to SIGKILL, and
# BOTH nodes' containers were left running with a dead owner. That leaked ~100GB on essentially
# every unload; the reaper log showed orphans aged 4398s and 1936s in one 24h window.
# `wait` IS interruptible by traps, so backgrounding the follow lets teardown fire immediately.
# reap-orphan-sglang.sh stays as the backstop, but it should now rarely have anything to do.
# TESTED 2026-09-10 01:19: SIGTERM to a live launcher (exactly what llama-swap sends on TTL)
# now tears down BOTH nodes in 9s. Before the fix the same signal left them running until the
# reaper caught them 30-70 min later. Re-run the test after touching this line or teardown.
docker logs -f "$HEAD_CTR" &
LOGS_PID=$!
wait "$LOGS_PID"
