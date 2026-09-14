#!/bin/bash
# llama-swap prefix shim for the NON-container members (ds4, ollama). The spark-exclusive group
# means when we start ds4 or ollama, no DFlash vLLM container should be alive. serve-vllm-dflash.sh
# normally stops its container on swap-out, but a wrapper SIGKILLed before its trap runs could
# orphan the container (its VRAM isn't owned by the PID llama-swap tracks). Force-remove both
# DFlash containers before the real command loads, so a leak can never co-reside and OOM the pool.
# ollama has no memcheck backstop (cmd is `sleep infinity`), so this is what closes that path.
#
#   cmd: reclaim-dflash.sh <real-cmd> [args...]
#
# exec preserves this pid, so llama-swap keeps tracking the real server exactly as before.
docker rm -f gemma4-26b-dflash >/dev/null 2>&1 || true
exec "$@"
