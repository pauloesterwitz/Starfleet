#!/bin/bash
# serve-kathryn.sh — run a model on KATHRYN's GB10 pool as a member of JEAN-LUC's llama-swap.
#
#   cmd: serve-kathryn.sh <key> <need_mb> --port ${PORT}
#   proxy: http://127.0.0.1:${PORT}
#
# Why this exists: llama-swap arbitrates ONE memory pool, and memcheck.sh reads /proc/meminfo of the
# machine it runs on. A model on Kathryn consumes KATHRYN's 121GB, which Jean-Luc's memcheck cannot
# see and must not gate. So memcheck runs REMOTELY, inside the ssh, against Kathryn's own pool and
# her own reservation ledger. Jean-Luc's spark-big/spark-small groups stay about Jean-Luc's pool.
# One endpoint (:28080) fronts both machines; each gates its own memory. No new software.
#
# The kathryn group in config.yaml MUST be exclusive:false — loading a Kathryn model must never
# evict ds4 on Jean-Luc, and vice versa. They are independent pools.
set -uo pipefail

KEY="${1:?usage: serve-kathryn.sh <key> <need_mb> --port <port>}"
NEED="${2:?usage: serve-kathryn.sh <key> <need_mb> --port <port>}"
shift 2
PORT=8000
while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done

# LAN name, NOT the bare tailnet name: `kathryn` hits Tailscale SSH, which wants a browser re-auth
# and breaks BatchMode. `kathryn.fritz.box` hits real OpenSSH.
HOST=kathryn.fritz.box

# Context size, if ctx-env.sh set one for this member on THIS node: carry it across the ssh so
# Kathryn serves the requested --max-model-len too. Digits only (it is interpolated into a remote
# shell command), and empty means the remote keeps serve-vllm-dflash.sh's deployed default.
CTX="${MML_OVERRIDE:-}"
case "$CTX" in ""|*[!0-9]*) CTX="" ;; esac


# Thinking/effort kwargs, if ctx-env.sh set any on THIS node: carry them across too, so
# Kathryn serves with the same default. Single-quoted remotely, and anything that could
# break out of those quotes or inject a command is dropped -- this string is interpolated
# into a remote shell command, same reason CTX above is digits-only.
MFE="${MODEL_FLAGS_EXTRA:-}"
case "$MFE" in *\'*|*\;*|*\`*|*\$*|*\&*|*\|*) MFE="" ;; esac
REMOTE="${CTX:+MML_OVERRIDE=$CTX }${MFE:+MODEL_FLAGS_EXTRA='$MFE' }\$HOME/llama-swap/memcheck.sh ${NEED} \$HOME/llama-swap/serve-vllm-dflash.sh ${KEY} --port ${PORT}"

# -tt  : force a pty. When llama-swap swaps this member out it kills the local ssh; the pty then
#        closes and the remote bash gets SIGHUP, firing serve-vllm-dflash.sh's trap (which now
#        includes HUP) to docker-stop the container. Without this, 64GB is orphaned on Kathryn
#        with nothing on Jean-Luc able to reclaim it.
# -L   : tunnel OUR ${PORT} to KATHRYN's 127.0.0.1:${PORT}. The container stays bound to loopback on
#        Kathryn — the model is never exposed on the LAN. llama-swap proxies to our local end.
# ExitOnForwardFailure: if our port is already taken, fail loudly instead of serving a dead tunnel.
# ServerAliveInterval : notice a dropped link instead of hanging a member in "ready" forever.
exec ssh -tt \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
  -o StrictHostKeyChecking=accept-new \
  -L "${PORT}:127.0.0.1:${PORT}" \
  "$HOST" "$REMOTE"
