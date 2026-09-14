#!/bin/bash

# ── Paul's ds4 kill switch. ds4 was deprecated 2026-07-27 and REACTIVATED 2026-08-26. ──
# The switch is kept (it lives in the launcher, not just config.yaml, so a config restore by
# another session cannot silently bring ds4 back). It is currently SATISFIED.
# Turn ds4 off again with:  rm ~/.ds4-enabled
if [ ! -e "$HOME/.ds4-enabled" ]; then
  echo "ds4 is disabled. Re-activate: touch ~/.ds4-enabled" >&2
  exit 1
fi

# ds4-tp2-proxy-guard.sh — the llama-swap `cmd` for the `ds4-nvfp4-tp2` member.
#
# TWO MODES, chosen by whether something already serves on :8000.
#
#   ALREADY RUNNING  -> park (sleep infinity) and let llama-swap proxy to it. We do NOT own that
#                       cluster, so when llama-swap unloads this member it kills only the sleep;
#                       a systemd-started (`systemctl --user start ds4-tp2`) or hand-started
#                       cluster keeps running. This is the "persistently preloaded" case.
#   NOT RUNNING      -> launch it ourselves in the foreground. We DO own it: run-ds4-tp2-cluster.sh
#                       traps EXIT/INT/TERM/HUP and tears down BOTH nodes, and llama-swap unloads a
#                       member by killing its cmd, so eviction and ttl free both Sparks correctly.
#
# WHY THIS CHANGED 2026-08-26. It used to exit 1 here and demand a manual systemctl, on the
# reasoning that "a 2-node Ray cluster is too fragile to hot-swap, and its ~14min cold load would
# hang any client". That no longer separates ds4 from its peers:
#   - Cold load MEASURED at 931s (15m31s), and the starfleet members llama-swap ALREADY manages
#     load slower or comparably (qwen3-235b 1218s, qwen3.5-122b 1150s, hy3 longer still). The
#     global healthCheckTimeout is 2400s precisely to cover them.
#   - The launch is now reliable: the NVRM OOM that made it feel fragile was page cache, fixed in
#     memcheck.sh on 2026-08-26.
# So ds4 is now a normal starfleet member: llama-swap starts it, evicts it, and ttls it like the
# rest, while a preloaded cluster is still respected via the park mode above.
set -uo pipefail
PORT="${PORT:-8000}"

# DRYRUN=1 short-circuits BEFORE the liveness probe: the answer must not depend on whether
# a cluster happens to be up, and must never end in `sleep infinity`.
# run-ds4-tp2-cluster.sh already implements DRYRUN (prints the docker/ssh plan; no GPU, no
# network), so hand straight over to it.
if [ "${DRYRUN:-0}" = 1 ]; then
  exec /home/pauloesterwitz/llama-swap/run-ds4-tp2-cluster.sh
fi

if curl -sf -m 5 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
  echo "ds4-nvfp4-tp2: cluster already serving on :${PORT} (preloaded elsewhere) - proxying, not managing it." >&2
  exec sleep infinity
fi

echo "ds4-nvfp4-tp2: nothing on :${PORT}, starting the TP=2 cluster (~15min cold load, BOTH Sparks)." >&2
exec /home/pauloesterwitz/llama-swap/run-ds4-tp2-cluster.sh
