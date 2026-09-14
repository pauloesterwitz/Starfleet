#!/bin/bash
# reap-orphan-sglang.sh — kill sglang-tp2 containers whose owning launcher is DEAD.
#
# WHY (MEASURED 2026-09-08 04:03): a cluster sat up for 2 HOURS holding 107GB with its launcher
# PID dead, llama-swap reporting idle, and its port not responding. llama-swap SIGKILLs the
# wrapper when it does not exit fast enough on TTL, so the EXIT trap never runs and both nodes'
# containers leak. memcheck.sh's reaper only runs when a load is ATTEMPTED — with no traffic,
# nothing ever triggered it, and the pool stayed hostage until the next load would have been
# refused by the memory gate.
#
# ponytail: a timer calling the launcher's own `down`, not a second teardown implementation.
# SAFETY — refuses to act unless ALL of these hold:
#   1. the owner stamp's PID is dead        (a live launcher owns its containers)
#   2. llama-swap does not list the model   (never touch something it is serving)
#   3. the container is older than GRACE    (never race a cold start)
# END-TO-END TESTED 2026-09-08 08:50. Reproduced the real failure (SIGKILL the launcher so the
# EXIT trap never runs, leaving containers with a dead owner holding 105GB) and the timer reaped
# it unaided 5.5 min later, clearing BOTH nodes and freeing 112GB:
#   "reaping orphan sglang-tp2-head (owner PID 3156950 dead, age 1131s, llama-swap idle)"
# Re-run that test after changing this script or the launcher's teardown — an untested safety net
# is worse than none, because it invites the assumption that leaks get cleaned up.
set -uo pipefail
LS="$(cd "$(dirname "$0")" && pwd)"
GRACE_S="${ORPHAN_GRACE_S:-600}"
CTR=sglang-tp2-head
WCTR=sglang-tp2-worker
WORKER_HOST="${WORKER_HOST:-kathryn.fritz.box}"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10"
reap(){  # $1 = reason. REAPER_DRYRUN=1 prints the decision instead of acting.
  if [ "${REAPER_DRYRUN:-0}" = 1 ]; then echo "DRYRUN, would reap: $1"; return; fi
  logger -t reap-orphan-sglang "reaping $1"
  FORCE_TEARDOWN=1 "$LS/run-sglang-tp2-cluster.sh" down >/dev/null 2>&1
  logger -t reap-orphan-sglang "teardown finished"
}

id=$(docker ps --filter "name=$CTR" -q 2>/dev/null) || exit 0
if [ -z "$id" ]; then
  # WORKER-ONLY ORPHAN (added 2026-09-14). Everything below keys on the HEAD, and so does
  # memcheck.sh's reaper, so a worker orphaned by itself on Kathryn (head gone, SSH kill failed)
  # was invisible to both. Not harmless: the launcher gates Kathryn's memory BEFORE its own
  # `docker rm -f` of a stale worker, so the gate refuses and the cleanup never runs. Every
  # sglang-tp2 load would fail with "Kathryn: won't fit" until someone ran `down` by hand.
  w=$($SSH "$WORKER_HOST" "docker inspect $WCTR --format '{{index .Config.Labels \"sglang-tp2-run\"}}|{{.State.StartedAt}}|{{.State.Running}}'" 2>/dev/null) || exit 0
  [ -n "$w" ] || exit 0
  IFS='|' read -r wstamp wstarted wrunning <<<"$w"
  [ "$wrunning" = "true" ] || exit 0            # an exited container holds no memory
  wage=$(( $(date +%s) - $(date -d "$wstarted" +%s 2>/dev/null || echo 0) ))
  [ "$wage" -lt "$GRACE_S" ] && exit 0          # the worker starts ~2s before the head: never race that
  wpid=$(echo "$wstamp" | grep -oE '^[0-9]+')
  [ -n "$wpid" ] || exit 0
  kill -0 "$wpid" 2>/dev/null && exit 0         # the owning launcher runs on THIS host
  curl -s -m 5 http://127.0.0.1:28080/running 2>/dev/null | grep -q 'sglang-tp2' && exit 0
  reap "orphan (worker-only) $WCTR @$WORKER_HOST (owner PID $wpid dead, age ${wage}s, no head, llama-swap idle)"
  exit 0
fi

started=$(docker inspect "$CTR" --format '{{.State.StartedAt}}' 2>/dev/null)
age=$(( $(date +%s) - $(date -d "$started" +%s 2>/dev/null || echo 0) ))
[ "$age" -lt "$GRACE_S" ] && exit 0

stamp=$(docker inspect "$CTR" --format '{{index .Config.Labels "sglang-tp2-run"}}' 2>/dev/null)
pid=$(echo "$stamp" | grep -oE '^[0-9]+')
# No stamp = built before per-launch identity; leave it alone rather than guess.
[ -n "$pid" ] || exit 0
kill -0 "$pid" 2>/dev/null && exit 0   # owner alive: not an orphan

# Owner is dead. Last guard: is llama-swap serving this model right now?
if curl -s -m 5 http://127.0.0.1:28080/running 2>/dev/null | grep -q 'sglang-tp2'; then
  logger -t reap-orphan-sglang "owner $pid dead but llama-swap lists the model as running — leaving it"
  exit 0
fi

reap "orphan $CTR (owner PID $pid dead, age ${age}s, llama-swap idle)"
