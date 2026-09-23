#!/bin/bash
# llama-swap preflight for the GB10 unified pool: refuse to launch a model that
# won't fit RIGHT NOW, so llama-swap returns an error to the caller instead of
# loading it and OOM-killing the whole box. Read-only: it never evicts anything.
#
#   cmd: memcheck.sh <need_mb> <server-cmd> [args...]
#
# Fit test:  MemAvailable - live_reservations  >=  need_mb
#   - live_reservations: memory image/video jobs reserved via gate.py's ledger
#     (~/.gb10/reservations) but may not have allocated yet — the OOM-race gap.
#   - MEMCHECK_RECLAIM_OLLAMA=1 used to count ollama-resident models as available for
#     a server that evicts ollama on start (serve-ds4.sh). REMOVED 2026-09-23: the ollama
#     backend has been off since 2026-07-27 and uninstalled since, so the branch could
#     never fire. The env var is now ignored wherever it is still set.
#
# On success it records ITS OWN footprint in the ledger (exec keeps this pid), so
# a concurrent image/video admission counts the loading model too. Symmetric.
set -euo pipefail

need=$1; shift
state="${GB10_STATE_DIR:-$HOME/.gb10}"

# ── DROP PAGE CACHE for big loads: MemAvailable is necessary but NOT sufficient ────────────────
# WHY (2026-08-26). Two ds4 TP=2 launches died ~60s in with
#   NVRM: Check failed: Out of memory [NV_ERR_NO_MEMORY] ... _memdescAllocInternal
# while THIS CHECK passed with MemAvailable = 114 GiB on both nodes. The check was not wrong:
# MemAvailable counts reclaimable page cache, and each node held 22-25 GiB of it after streaming
# 78.5 GiB of safetensors off NVMe. NVRM does not force reclaim hard enough during a burst that
# large, so the allocation failed against cache the kernel would gladly have dropped. Dropping it
# first (cache 22 GiB -> 0, free 93 -> 115 GiB) fixed it with NO other change: same util, same
# context, next attempt served cleanly. So the fit arithmetic below was never the bug; the
# difference between "available" and "genuinely free" was.
#
# Lives HERE rather than in each launcher because every path (config.yaml cmd chains,
# run-single-node.sh, run-ds4-tp2-cluster.sh, run-m3ar-cluster.sh, serve-kathryn.sh over ssh)
# funnels through this one gate, including the REMOTE gate on Kathryn. One place, whole fleet.
#
# Threshold, not unconditional: a 43-55 GiB co-resident member fits alongside 22 GiB of cache
# fine, and dropping cache for those just costs everyone a needless re-read. 80000 sits in the gap
# between the small members (43000-55000) and the big ones (ds4 GGUF 108000, ds4 TP=2 90000,
# hy3/nemotron 100000, 397B 118000).
# Non-fatal by design (note the || true, required under `set -e`): a failed drop must never block
# a load that may well succeed anyway. It just warns, so a later OOM has an obvious cause.
# ── REAP AN ORPHANED ds4-tp2 CLUSTER ──────────────────────────────────────────────────────────
# The TP=2 cluster leaks. llama-swap unloads a member by killing its cmd, and
# run-ds4-tp2-cluster.sh has an EXIT/TERM trap that docker-stops both nodes - but the trap does
# not always win, and a missed teardown strands ds4-tp2-head + ds4-tp2-worker holding ~90 GiB
# EACH. Observed three times on 2026-08-27/28 (15h, 6h and mid-matrix). The damage is not just
# wasted memory: with 90 GiB stranded, memcheck correctly refuses EVERY other model, and
# llama-swap reports the useless "upstream command exited prematurely" - which is precisely what
# produced 21 bogus FAILs in the first OpenCode compat matrix.
#
# Orphan test: the head container exists but NOTHING answers on :8000. That is unambiguous.
#   - During ds4's own launch this cannot misfire: these gates run BEFORE the script starts any
#     container (see the gate section of run-ds4-tp2-cluster.sh).
#   - While ds4 is legitimately serving, :8000 answers, so we leave it alone.
#   - Guarded on the HEAD container existing LOCALLY, so this is a no-op on Kathryn. That matters:
#     Kathryn runs only a Ray worker with no API server, so her :8000 is dead even when the
#     cluster is perfectly healthy, and an unguarded check there would kill a live worker.
#   - PORT 8000 IS HARDCODED HERE ON PURPOSE, AND IT IS A COUPLING. It is safe ONLY because the
#     ds4-nvfp4-tp2 member deliberately does NOT pass `--port ${PORT}` in its cmd and its
#     `proxy:` is pinned to 127.0.0.1:8000, so the cluster always serves on 8000 (VERIFIED in
#     logs/run-DeepSeek-V4-Flash-NVFP4.log: `--port 8000`). If anyone ever switches that member
#     to a dynamic ${PORT}, THIS PROBE MUST READ THE PORT BACK FROM THE CONTAINER the way the
#     sglang block below does - otherwise it will find nothing on a healthy cluster and reap
#     ~90 GiB x2 of live model. The sglang block hit exactly that bug by hardcoding 8100.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx ds4-tp2-head; then
  if ! curl -sf -m 5 http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
    echo "memcheck: reaping ORPHANED ds4-tp2 cluster (head container up, nothing serving on :8000)" >&2
    docker rm -f ds4-tp2-head >/dev/null 2>&1 || true
    ssh -o BatchMode=yes -o ConnectTimeout=10 kathryn.fritz.box \
      'docker rm -f ds4-tp2-worker >/dev/null 2>&1' >/dev/null 2>&1 || true
    sleep 3
  fi
fi

# ── SAME REAP, FOR THE SGLANG TP=2 CLUSTER (added 2026-08-28 with the first SGLang member) ────
# run-sglang-tp2-cluster.sh strands exactly like the ds4 one: same two-node shape, same trap that
# does not always win, and an orphaned sglang-tp2-head + sglang-tp2-worker hold ~100 GiB EACH —
# MORE than ds4. Without this block that memory is invisible to every gate: memcheck would keep
# refusing every vLLM model on the fleet while the real culprit sat there unnoticed, which is the
# precise failure this whole file exists to prevent.
# Same unambiguous orphan test, on SGLang's port: head container up, nothing answering /health.
#   - Cannot misfire during its own launch: the gates run BEFORE any container starts.
#   - While it is legitimately serving, /health answers, so it is left alone.
#   - Guarded on the HEAD container existing LOCALLY -> no-op on Kathryn, whose node-rank-1
#     container never serves an API and would otherwise look "dead" while perfectly healthy.
#   - The port is READ BACK FROM THE CONTAINER, never assumed. llama-swap assigns each member a
#     DYNAMIC ${PORT} (observed 5821, not the 8100 default), so a hardcoded probe would find
#     nothing on a perfectly healthy cluster and reap it. That is not hypothetical: the first
#     version of this block hardcoded 8100 and killed a live llama-swap-managed cluster on its
#     very first run. If the port cannot be read, we DO NOT reap — refusing to load is a
#     recoverable annoyance, killing a healthy 100 GiB cluster is not.
#   - An AGE GUARD is required on top of the health probe. This cluster cold-starts for ~10-15
#     min (170 GiB of weights over two nodes), and /health is legitimately dead that whole time.
#     The ds4 block gets away without one because its gates run before its OWN containers start —
#     but that argument does not hold for a DIFFERENT model's launch, which runs this same file
#     while sglang is mid-load and would happily kill it. Measured: the age-less version reaped a
#     healthy cluster 39 s into its cold start. So: only consider a container orphaned once it is
#     older than the cold-start window and still not serving.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx sglang-tp2-head; then
  _sgl_port=$(docker inspect sglang-tp2-head --format '{{join .Config.Cmd " "}}' 2>/dev/null \
              | grep -oE -- '--port [0-9]+' | awk '{print $2}' | head -1)
  _sgl_started=$(docker inspect sglang-tp2-head --format '{{.State.StartedAt}}' 2>/dev/null)
  _sgl_age=$(python3 -c "
import sys,datetime
s='''${_sgl_started}'''.strip()
try:
    t=datetime.datetime.fromisoformat(s.replace('Z','+00:00').split('.')[0]+'+00:00')
    print(int((datetime.datetime.now(datetime.timezone.utc)-t).total_seconds()))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
  # OWNERSHIP, not just health. The health probe alone missed the case that actually cost us:
  # MEASURED 2026-09-02 — an unload orphaned head+worker, they kept answering /health perfectly
  # on their old port for TWO HOURS holding ~89 GB, llama-swap reported `running: []` because it
  # no longer owned them, and every subsequent load died at this very gate ("need 85000MB, have
  # 16625MB") which OpenCode surfaced as "upstream command exited prematurely". A healthy
  # container that nothing owns is still an orphan.
  # llama-swap is the authority on ownership: if /running lists no sglang member while the head
  # container exists past the cold-start grace, it is stranded regardless of how well it answers.
  # Fail-safe: if /running cannot be reached at all we do NOT reap — an unreachable router is not
  # evidence of an orphan, and killing a live 100 GB cluster on a bad curl is far worse than
  # refusing one load.
  _ls_running=$(curl -sf -m 5 http://127.0.0.1:28080/running 2>/dev/null)
  _ls_reachable=$?
  _sgl_owned=1
  if [ "$_ls_reachable" -eq 0 ] && ! printf '%s' "$_ls_running" | grep -q 'sglang-tp2'; then
    _sgl_owned=0
  fi

  # SECOND, INDEPENDENT OWNERSHIP SIGNAL (2026-09-02): the launcher stamps every container it
  # creates with a `sglang-tp2-run=<launcher-pid>-<epoch>` label. If the process named in that
  # label is gone, nothing is supervising this cluster — it is abandoned no matter what
  # llama-swap's /running says or how well /health answers. This catches the case the
  # /running check cannot: llama-swap restarted (so its view is empty of history) while the
  # containers from a previous supervisor kept running.
  # Only ever tightens the verdict, never loosens it: an unlabelled container (pre-2026-09-02)
  # or an unreadable label leaves _sgl_owned alone rather than declaring an orphan.
  _sgl_owner=$(docker inspect --format '{{index .Config.Labels "sglang-tp2-run"}}' sglang-tp2-head 2>/dev/null)
  if [ -n "$_sgl_owner" ]; then
    _sgl_owner_pid="${_sgl_owner%%-*}"
    if [ -n "$_sgl_owner_pid" ] && ! kill -0 "$_sgl_owner_pid" 2>/dev/null; then
      _sgl_owned=0
    fi
  fi

  # ORDER MATTERS. The unowned test is checked FIRST and gets its own, much SHORTER grace.
  # The long 1500s grace exists only to protect a COLD START — and a cold start is always
  # listed by llama-swap as "starting", so it is never unowned. An unowned container is
  # therefore an orphan at any age, and applying the cold-start grace to it just hides the
  # problem: MEASURED 2026-09-02, a 24-minute-old orphan holding ~89 GB slipped through
  # untouched because 24 min < the 25 min grace, and the next load was refused at this gate.
  # 120s only covers the brief window where the launcher has started containers but
  # llama-swap has not yet registered the member.
  if [ "$_sgl_owned" -eq 0 ] && [ "${_sgl_age:-0}" -ge "${SGLANG_TP2_OWN_GRACE_S:-120}" ]; then
    echo "memcheck: reaping UNOWNED sglang-tp2 cluster (age ${_sgl_age}s, llama-swap does not list it)" >&2
    docker rm -f sglang-tp2-head >/dev/null 2>&1 || true
    ssh -o BatchMode=yes -o ConnectTimeout=10 kathryn.fritz.box \
      'docker rm -f sglang-tp2-worker >/dev/null 2>&1' >/dev/null 2>&1 || true
    sleep 3
  elif [ "${_sgl_age:-0}" -lt "${SGLANG_TP2_GRACE_S:-1500}" ]; then
    : # still inside the cold-start window AND owned — never reap, it is loading
  elif [ -n "$_sgl_port" ] && ! curl -sf -m 5 "http://127.0.0.1:${_sgl_port}/health" >/dev/null 2>&1; then
    echo "memcheck: reaping ORPHANED sglang-tp2 cluster (head up, nothing serving on :${_sgl_port})" >&2
    docker rm -f sglang-tp2-head >/dev/null 2>&1 || true
    ssh -o BatchMode=yes -o ConnectTimeout=10 kathryn.fritz.box \
      'docker rm -f sglang-tp2-worker >/dev/null 2>&1' >/dev/null 2>&1 || true
    sleep 3
  elif [ -z "$_sgl_port" ]; then
    echo "memcheck: sglang-tp2-head up but its --port could not be read; NOT reaping (fail safe)" >&2
  fi
fi

# Reservations are read FIRST: they tell us whether an image/video render is in flight, which
# gates whether the reclaim step below is allowed to touch ComfyUI.
res=0
if [ -d "$state/reservations" ]; then
  for f in "$state"/reservations/*; do
    [ -e "$f" ] || continue
    read -r pid mb _ < "$f" 2>/dev/null || continue
    if kill -0 "$pid" 2>/dev/null; then
      res=$((res + mb))
    else
      rm -f "$f" 2>/dev/null || true   # stale: owner is gone
    fi
  done
fi

# Reclaim BOTH kinds of "present but not really needed" memory, then measure. Only for big loads,
# and ONLY when no render holds a reservation (res==0) so an in-flight ComfyUI job is never killed.
if [ "$need" -ge "${MEMCHECK_DROPCACHE_MIN_MB:-80000}" ] && [ "$res" -eq 0 ]; then

  # (a) PAGE CACHE. Two ds4 TP=2 launches died ~60s in with NVRM NV_ERR_NO_MEMORY while this very
  # check passed at MemAvailable 114 GiB: MemAvailable counts reclaimable page cache, and each node
  # held 22-25 GiB of it after streaming 78.5 GiB of safetensors off NVMe. NVRM does not force
  # reclaim hard enough during a burst that large. Dropping it first fixed the launch with no other
  # change. The fit arithmetic below was never wrong; "available" vs "genuinely free" was.
  sync 2>/dev/null || true
  sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null \
    || echo "memcheck: WARN could not drop page cache on $(hostname) (needs passwordless sudo) - continuing, may OOM" >&2

  # (b) IDLE COMFYUI WEIGHTS. ComfyUI keeps its diffusion models resident after a render: MEASURED
  # 2026-08-26 at 17079 MiB on EACH node while completely idle. That is invisible to `docker stats`
  # (which showed 63-818 MiB, the container cgroup, and misled an earlier diagnosis) and shows up
  # only in `nvidia-smi --query-compute-apps`. It cost a ds4 TP=2 launch: weights loaded, then
  # "Available KV cache memory: -4.39 GiB" (Jean-Luc) / "-11.41 GiB" (Kathryn) -> engine dead.
  # /free is ComfyUI's own endpoint: it unloads models WITHOUT killing the container, which reloads
  # them on the next generation. MEASURED effect: 17079 -> 789 MiB per node, pool 96 -> 112 GiB.
  # render-guard.sh does NOT cover this: it only refuses a load while a >=78 GB VIDEO reservation is
  # live, i.e. it protects renders from LLMs, never the reverse.
  curl -sf -m 10 -X POST "http://127.0.0.1:${COMFYUI_PORT:-8188}/free" \
       -H "Content-Type: application/json" \
       -d '{"unload_models":true,"free_memory":true}' >/dev/null 2>&1 \
    || true   # not running / not reachable is fine and common
fi

avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)

eff=$((avail - res))
if [ "$eff" -lt "$need" ]; then
  echo "memcheck: refusing to load — need ${need}MB, have ${eff}MB" \
       "(avail=${avail} -reserved=${res})" >&2
  exit 1
fi

mkdir -p "$state/reservations"
echo "$$ $need" > "$state/reservations/$$"   # exec preserves this pid
exec "$@"
