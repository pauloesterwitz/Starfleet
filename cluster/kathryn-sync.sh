#!/bin/bash
# Mirrors selected folders from jean-luc to Kathryn. Jean-Luc is the source of
# truth (matches the existing claude-skills-sync precedent) — --delete makes
# Kathryn an exact copy, so edits made directly on Kathryn get overwritten.
# No `set -e`: one folder's transient failure (e.g. Kathryn briefly
# unreachable) shouldn't stop the others from syncing.
set -uo pipefail
SSHOPTS='ssh -o BatchMode=yes -o ConnectTimeout=10'

# FAST PATH, deliberately ABOVE the download guard below: gpujob dispatches jobs
# to Kathryn by running ~/llama-swap/gpujob-run THERE, so a stale copy means a
# job dies with exit 127. It is 6 KB and never --delete's, so unlike the big
# mirrors it is safe to run while a model download is in flight. gpujob also
# pushes it at submit time; this is the backstop for edits made between runs.
flock -w 300 /home/pauloesterwitz/.kathryn-sync.lock rsync -a -e "$SSHOPTS" \
    /home/pauloesterwitz/llama-swap/gpujob-run /home/pauloesterwitz/llama-swap/GPUJOB.md \
    10.100.0.1:/home/pauloesterwitz/llama-swap/ \
    || echo "$(date -Is) kathryn-sync: gpujob-run fast path failed (non-fatal)"

# fix 3: never --delete-mirror while a download is in flight (partial model / temp files would
# be mirrored or raced). Skip if any dl-*.sh runs or any .incomplete exists on JL.
if pgrep -f 'dl-[a-z0-9-]*\.sh' >/dev/null 2>&1 || find /home/pauloesterwitz/models -maxdepth 2 -name '*.incomplete' 2>/dev/null | grep -q .; then
  echo "$(date -Is) kathryn-sync: downloads in progress — skipping mirror this run"; exit 0
fi

flock -w 7200 /home/pauloesterwitz/.kathryn-sync.lock rsync -a --delete -e "$SSHOPTS" --exclude 'llama-swap.log' --exclude 'llama-swap.tar.gz' \
    /home/pauloesterwitz/llama-swap/ 10.100.0.1:/home/pauloesterwitz/llama-swap/

flock -w 7200 /home/pauloesterwitz/.kathryn-sync.lock rsync -a --delete -e "$SSHOPTS" --exclude output \
    /home/pauloesterwitz/Speech-to-Image/ 10.100.0.1:/home/pauloesterwitz/Speech-to-Image/

flock -w 7200 /home/pauloesterwitz/.kathryn-sync.lock rsync -a --delete --inplace -e "$SSHOPTS" \
    /home/pauloesterwitz/ds4/ 10.100.0.1:/home/pauloesterwitz/ds4/

flock -w 7200 /home/pauloesterwitz/.kathryn-sync.lock rsync -a --delete --inplace -e "$SSHOPTS" \
    /home/pauloesterwitz/models/ 10.100.0.1:/home/pauloesterwitz/models/

# .ollama mirror removed 2026-07-27: ollama was deleted on Kathryn (vLLM-only
# node). Do not re-add - a mirror would resurrect 500+ GB of GGUF copies there.

# output/ excluded: Kathryn's own ComfyUI generates its own images/videos there —
# a --delete mirror from jean-luc would wipe Kathryn's independently generated content.
flock -w 7200 /home/pauloesterwitz/.kathryn-sync.lock rsync -a --delete --inplace -e "$SSHOPTS" --exclude output \
    /home/pauloesterwitz/comfyui-data/ 10.100.0.1:/home/pauloesterwitz/comfyui-data/
