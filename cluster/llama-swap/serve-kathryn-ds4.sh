#!/bin/bash
# serve-kathryn-ds4.sh — run DeepSeek-V4-Flash (ds4-server, GGUF) on KATHRYN's GB10 pool as a
# member of JEAN-LUC's llama-swap. Same ssh/tunnel pattern as serve-kathryn-nemcascade.sh.
#
# ds4 is not one of serve-vllm-dflash.sh's KEY-dispatched models (different engine entirely:
# ds4-server reading ~/ds4/ds4flash.gguf), so this takes no <key>/<need_mb> args, just the port.
#
# Unlike serve-nemcascade.sh, serve-ds4.sh does NOT bake in its own memcheck gate -- on Jean-Luc
# that chain lives in config.yaml's cmd. So it is reproduced here, running on KATHRYN, against
# KATHRYN's pool: render-guard (no render in flight) -> reclaim-dflash (clear leaked vLLM) ->
# memcheck 108000 (clean error instead of an OOM). Same numbers as the Jean-Luc member.
#
#   cmd: serve-kathryn-ds4.sh --port ${PORT}
#   proxy: http://127.0.0.1:${PORT}
set -uo pipefail

PORT=8000
while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done

# LAN name, NOT the bare tailnet name: see serve-kathryn.sh for why.
HOST=kathryn.fritz.box

CTX="${MML_OVERRIDE:-}"
case "$CTX" in ""|*[!0-9]*) CTX="" ;; esac

# Thinking/effort kwargs, if ctx-env.sh set any on THIS node: carry them across so Kathryn
# serves with the same default. Single-quoted remotely, and anything that could break out of
# those quotes or inject a command is dropped -- this string is interpolated into a remote shell.
MFE="${MODEL_FLAGS_EXTRA:-}"
case "$MFE" in *\'*|*\;*|*\`*|*\$*|*\&*|*\|*) MFE="" ;; esac

REMOTE="${CTX:+MML_OVERRIDE=$CTX }${MFE:+MODEL_FLAGS_EXTRA='$MFE' }"
REMOTE="$REMOTE\$HOME/llama-swap/render-guard.sh \$HOME/llama-swap/reclaim-dflash.sh"
REMOTE="$REMOTE /usr/bin/env MEMCHECK_RECLAIM_OLLAMA=1 \$HOME/llama-swap/memcheck.sh 108000"
REMOTE="$REMOTE \$HOME/ds4/serve-ds4.sh --port ${PORT}"

# DRYRUN=1 must NOT touch the network -- the contract Fleet requires before it probes a
# launcher. Kathryn runs the very same serve-ds4.sh, so ask the local copy rather than
# restating the GGUF path here: one source of truth, and it cannot drift.
if [ "${DRYRUN:-0}" = 1 ]; then
  exec "$HOME/ds4/serve-ds4.sh" --port "$PORT"
fi

exec ssh -tt \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
  -o StrictHostKeyChecking=accept-new \
  -L "${PORT}:127.0.0.1:${PORT}" \
  "$HOST" "$REMOTE"
