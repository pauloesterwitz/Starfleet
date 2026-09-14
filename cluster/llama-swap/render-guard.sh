#!/bin/bash
# render-guard.sh — llama-swap member guard. Refuse to (re)start an LLM while a
# heavy VIDEO render holds a gate reservation, so a background-triggered load
# (ds4, ollama, any big model) can't OOM-kill an in-flight ComfyUI video render.
#
#   cmd: render-guard.sh <real-server-cmd> [args...]
#
# Reads the SAME ledger as memcheck.sh (~/.gb10/reservations, "<pid> <mb>"). A
# video render publishes >=78000 MB there for its whole duration (video_gen.py /
# gate.job_slot). If such a live reservation exists we exit 1 -> llama-swap
# returns a clean error to the caller and the model never loads. Otherwise we
# exec the real command (exec preserves the pid for memcheck's own reservation).
set -uo pipefail
state="${GB10_STATE_DIR:-$HOME/.gb10}"

if [ -d "$state/reservations" ]; then
  for f in "$state"/reservations/*; do
    [ -e "$f" ] || continue
    read -r pid mb _ < "$f" 2>/dev/null || continue
    if [ "${mb:-0}" -ge 78000 ] && kill -0 "$pid" 2>/dev/null; then
      echo "render-guard: refusing LLM load — video render (pid $pid, ${mb}MB) holds the GPU" >&2
      exit 1
    fi
  done
fi

exec "$@"
