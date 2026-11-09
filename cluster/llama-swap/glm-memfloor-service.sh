#!/bin/bash
# Production memory guard for the GLM member. Until 2026-09-20 watchdog-memfloor.py only ran during
# benchmarks; with the context ceiling raised to 471k the member deliberately spends its margin down
# to the 3 GB trip point, so the guard has to be resident.
#
# WHAT IT DOES: kills the sglang cluster when either node drops below the floor, but ONLY when the
# head is actually running the GLM checkpoint - it will not touch qwen3.8 or anything else. The
# failure mode becomes "model killed, reload it" instead of the kernel choosing a victim among
# Paul's own sessions, which on unified memory is what an OOM here would do.
#
# LOOP GUARD: GLM is pinned, so fleet-ui reloads it after a kill. If memory is still tight it would
# trip again, so a trip sleeps 120 s before exiting; with RestartSec=30 that bounds it to roughly
# one kill per 2.5 minutes, and every trip sends a Telegram.
set -uo pipefail
LS=/home/pauloesterwitz/llama-swap
python3 $LS/watchdog-memfloor.py --floor-gb "${FLOOR_GB:-3}" \
  --guard-model /models/GLM-5.3-Flash-AWQ-kda-w8 \
  --log "$LS/.glm-memfloor.log"
rc=$?
if [ "$rc" = 3 ]; then
  $LS/tg-notify.sh "Memory guard fired on the Sparks: GLM was killed because a node fell below ${FLOOR_GB:-3} GB free. It reloads automatically since it is pinned. If this repeats, the context is set too high for how busy Jean-Luc is - lower it in fleet-ui." >/dev/null 2>&1
  sleep 120
fi
exit $rc
