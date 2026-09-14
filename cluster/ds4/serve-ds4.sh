#!/bin/bash

# ── DEPRECATED 2026-07-27 (Paul): ds4 is OFF until explicitly re-activated. ──
# This guard lives in the launcher (not just llama-swap's config.yaml) so a config
# restore by another session cannot silently bring ds4 back. Re-activate with:
#     touch ~/.ds4-enabled
if [ ! -e "$HOME/.ds4-enabled" ]; then
  echo "ds4 is deprecated (2026-07-27). Re-activate: touch ~/.ds4-enabled" >&2
  exit 1
fi

# Launched by llama-swap as the "deepseek-v4-flash" backend.
#
# Before loading the ~81GB ds4 model, evict any Ollama-resident models so ds4 gets the
# full unified-memory pool. This closes the one gap llama-swap can't cover on its own:
# the Ollama daemon is external (started outside llama-swap), so llama-swap's group-swap
# can stop *ds4* when you switch to Ollama, but it cannot unload Ollama's OWN models when
# you switch back to ds4. This wrapper does that, giving airtight "never co-resident".
OLLAMA=/usr/local/bin/ollama
for m in $("$OLLAMA" ps 2>/dev/null | awk 'NR>1 && NF>0 {print $1}'); do
    echo "serve-ds4: evicting ollama model $m before loading ds4" >&2
    "$OLLAMA" stop "$m" 2>/dev/null || true
done
# exec (not a subshell) so llama-swap tracks/kills the ds4-server PID directly.
# --ctx 409600 (400K): set on request. NOTE: >256K may leave the fast pinned-KV mode for the
# slower managed/pageable KV cache (256K was the fast daily driver) — watch t/s at long context.
# MTP speculative decoding: OFF. Measured 2026-07-08 on GB10: cost-neutral at best
# (sampled path, depth 2: 14.17 vs 14.13 t/s no-MTP, weight-bandwidth-bound) while
# the draft GGUF holds ~3.8 GB of the pool. The engine still supports it:
#   export DS4_MTP_SAMPLE=1   # sampled path (temp>0 / think mode); greedy needs temp 0
#   --mtp /home/pauloesterwitz/ds4/gguf/DeepSeek-V4-Flash-MTP-Q4K-Q8_0-F32.gguf \
#   --mtp-draft 2 --mtp-margin 3
# DRYRUN=1 prints the plan and exits without evicting anything or starting the server --
# the contract Fleet requires before it will probe a launcher at all.
#
# NOTE this one reports a GGUF, not a checkpoint directory: ds4flash.gguf lives outside
# ~/models and carries its metadata inside the file. So Fleet stays unable to derive this
# member's max context or thinking options from it -- the branch makes the launcher
# probe-safe and self-describing, not magically introspectable.
if [ "${DRYRUN:-0}" = 1 ]; then
  echo "engine=ds4-server gguf=/home/pauloesterwitz/ds4/ds4flash.gguf ctx=409600 (no HF checkpoint dir)"
  exit 0
fi

exec /home/pauloesterwitz/ds4/ds4-server --cuda \
    -m /home/pauloesterwitz/ds4/ds4flash.gguf \
    --ctx 409600 --host 127.0.0.1 \
    --kv-disk-dir /home/pauloesterwitz/.ds4/server-kv --kv-disk-space-mb 8192 \
    "$@"
