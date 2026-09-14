#!/bin/bash
# serve-kathryn-nemcascade.sh — run Nemotron-Cascade-2-30B on KATHRYN's GB10 pool as a member of
# JEAN-LUC's llama-swap. Same ssh/tunnel pattern as serve-kathryn.sh, but nemcascade isn't one of
# serve-vllm-dflash.sh's KEY-dispatched models — it's its own self-contained, host-relative script
# (serve-nemcascade.sh bakes in NEED_MB/util/flags and gates via $HOME/llama-swap/memcheck.sh on
# whichever machine runs it), so this wrapper needs no <key>/<need_mb> args, just the port.
#
#   cmd: serve-kathryn-nemcascade.sh --port ${PORT}
#   proxy: http://127.0.0.1:${PORT}
set -uo pipefail

PORT=8000
while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done

# LAN name, NOT the bare tailnet name: see serve-kathryn.sh for why.
HOST=kathryn.fritz.box

CTX="${MML_OVERRIDE:-}"
case "$CTX" in ""|*[!0-9]*) CTX="" ;; esac


# Thinking/effort kwargs, if ctx-env.sh set any on THIS node: carry them across too, so
# Kathryn serves with the same default. Single-quoted remotely, and anything that could
# break out of those quotes or inject a command is dropped -- this string is interpolated
# into a remote shell command, same reason CTX above is digits-only.
MFE="${MODEL_FLAGS_EXTRA:-}"
case "$MFE" in *\'*|*\;*|*\`*|*\$*|*\&*|*\|*) MFE="" ;; esac
REMOTE="${CTX:+MML_OVERRIDE=$CTX }${MFE:+MODEL_FLAGS_EXTRA='$MFE' }\$HOME/llama-swap/serve-nemcascade.sh --port ${PORT}"

# DRYRUN=1 must NOT touch the network. Kathryn runs the very same serve-nemcascade.sh, so
# ask the local copy rather than duplicating the checkpoint name here -- one source of
# truth, and it cannot drift if that launcher's model ever changes.
if [ "${DRYRUN:-0}" = 1 ]; then
  exec "$HOME/llama-swap/serve-nemcascade.sh" --port "$PORT"
fi

exec ssh -tt \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
  -o StrictHostKeyChecking=accept-new \
  -L "${PORT}:127.0.0.1:${PORT}" \
  "$HOST" "$REMOTE"
