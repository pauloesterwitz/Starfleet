#!/bin/bash
# serve-kathryn-embed.sh — run an embedding model (any serve-embed.sh key) on
# KATHRYN's GB10 pool as a member of JEAN-LUC's llama-swap. Same ssh/tunnel pattern as
# serve-kathryn.sh, but embeddings aren't one of serve-vllm-dflash.sh's KEY-dispatched
# models — they're served by serve-embed.sh, which is self-contained and gates via its own
# internal memcheck.sh call (unlike serve-kathryn.sh's explicit external memcheck chain),
# same style as serve-kathryn-nemcascade.sh. serve-embed.sh and the model weights already
# exist on Kathryn at the same paths as Jean-Luc (verified 2026-09-14) — no sync needed.
#
#   cmd: serve-kathryn-embed.sh <serve-embed.sh key> --port ${PORT}
#   proxy: http://127.0.0.1:${PORT}
set -uo pipefail

KEY="${1:?usage: serve-kathryn-embed.sh <nomic-embed-text|embeddinggemma|bge-reranker-v2-m3|harrier-embed-0.6b> --port <port>}"
shift
PORT=8000
while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done

# LAN name, NOT the bare tailnet name: see serve-kathryn.sh for why.
HOST=kathryn.fritz.box
REMOTE="\$HOME/llama-swap/serve-embed.sh ${KEY} --port ${PORT}"

# DRYRUN=1 must NOT touch the network — same convention as serve-kathryn-nemcascade.sh.
if [ "${DRYRUN:-0}" = 1 ]; then
  exec "$HOME/llama-swap/serve-embed.sh" "$KEY" --port "$PORT"
fi

exec ssh -tt \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
  -o StrictHostKeyChecking=accept-new \
  -L "${PORT}:127.0.0.1:${PORT}" \
  "$HOST" "$REMOTE"
