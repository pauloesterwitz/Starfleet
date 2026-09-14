#!/bin/bash
# pick-node.sh <key> <need_mb> --port <port>
# Dynamic node selection for a single-node model: serve <key> on whichever Spark has the most free
# memory RIGHT NOW, then delegate to the SAME proven paths the static -jl/-kathryn members use.
#
# Works as ONE llama-swap member because both paths land on 127.0.0.1:${PORT}:
#   - Jean-Luc: render-guard -> memcheck -> serve-vllm-dflash (local container, -p 127.0.0.1:PORT)
#   - Kathryn : serve-kathryn.sh (ssh + memcheck THERE + `ssh -L PORT` tunnel back to our loopback)
# So `proxy: http://127.0.0.1:${PORT}` in config.yaml is correct whichever node wins.
#
# Metric = MemAvailable. GB10 is unified memory, so free RAM IS the load constraint; a node already
# serving a model shows low avail, so this also routes around a busy node. The chosen node's own
# memcheck remains the authority on fit — pick-node only decides WHERE to try; memcheck refuses cleanly
# if it turns out not to fit (never OOM). Kathryn unreachable -> fail safe to local Jean-Luc.
set -uo pipefail

LS=/home/pauloesterwitz/llama-swap
HOST=kathryn.fritz.box
KEY="${1:?usage: pick-node.sh <key> <need_mb> --port <port>}"
NEED="${2:?usage: pick-node.sh <key> <need_mb> --port <port>}"
shift 2
PORT=8000; while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done

jl=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
# ConnectTimeout so a dead Kathryn can't hang a model load; empty result -> 0 -> Jean-Luc wins.
k=$(timeout 12 ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST" \
      "awk '/MemAvailable/{print int(\$2/1024)}' /proc/meminfo" 2>/dev/null)
[ -n "${k:-}" ] || k=0

# Prefer the node with more free memory; if the freer one can't fit need_mb but the other can, flip.
# Tie -> Jean-Luc (local, no ssh/tunnel overhead).
if [ "$jl" -ge "$k" ]; then first=jl fa=$jl second=kathryn sa=$k; else first=kathryn fa=$k second=jl sa=$jl; fi
if [ "$fa" -lt "$NEED" ] && [ "$sa" -ge "$NEED" ]; then choice=$second; else choice=$first; fi

echo "pick-node[$KEY]: JL=${jl}MB Kathryn=${k}MB need=${NEED}MB -> $choice" >&2
if [ "${DRYRUN:-0}" = 1 ]; then echo "$choice"; exit 0; fi

if [ "$choice" = jl ]; then
  exec "$LS/render-guard.sh" "$LS/memcheck.sh" "$NEED" "$LS/serve-vllm-dflash.sh" "$KEY" --port "$PORT"
else
  exec "$LS/serve-kathryn.sh" "$KEY" "$NEED" --port "$PORT"
fi
