#!/bin/bash
# serve-nemcascade.sh --port <port>
# llama-swap member wrapper for Nemotron-Cascade-2-30B-A3B-NVFP4 (NemotronH = hybrid Mamba-2 +
# attention + MoE). Wires the EXACT config the NVFP4 sweep measured at 49.1 tok/s single-node (beats
# TP=2 at 40.9; hybrid-Mamba shards poorly): run-single-node.sh + vllm-nightly-ray:local, TP=1,
# --trust-remote-code --enforce-eager, --kv-cache-dtype fp8, util 0.35, memcheck need 45000. Own
# container (nemcascade-ls) so it never collides with the disposable bench single-node-serve.
# Single-word args only (llama-swap word-splits the cmd and ignores quotes).
set -uo pipefail
PORT=8000; while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done
# MAX_MODEL_LEN 262144 = native max_position_embeddings (config.json). Same util 0.35 budget that
# already serves qwen3.6-35b at 262144 — safe headroom, since NemotronH's Mamba-2 layers don't grow
# KV with context the way plain attention does (only the interleaved attention layers do).
# UPGRADED to v0.27.1 2026-08-20: PASS 391 on the sweep (stage 1). run-single-node.sh defaults
# IMAGE to vllm-nightly-ray:local, so it has to be set here or the exec inherits the old image.
# Revert: IMAGE=vllm-nightly-ray:local in the environment, or drop IMAGE from the export below.
export IMAGE="${IMAGE:-vllm/vllm-openai:v0.27.1-aarch64}"
export CTR=nemcascade-ls PORT MODEL_SUBDIR=Nemotron-Cascade-2-30B-A3B-NVFP4 \
       SERVED_NAME=nemcascade-single NEED_MB=45000 GPU_UTIL=0.35 MAX_MODEL_LEN="${MML_OVERRIDE:-262144}" \
       ENFORCE_EAGER=0 \
       MODEL_FLAGS="--kv-cache-dtype fp8 --max-num-seqs 32 --max-num-batched-tokens 16384 --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser nemotron_v3 ${MODEL_FLAGS_EXTRA:-}"
# SPEED 2026-08-25: ENFORCE_EAGER=0 (run-single-node.sh defaults it to 1, so it must be set here)
# plus --max-num-seqs 32. NemotronH is hybrid Mamba-2 + attention; its Mamba state does not grow
# with context, so the KV pool easily holds far more than 4 sequences.
# Tool flags per the model card (README.md "Usage / vLLM"); run-single-node.sh is a benchmark script
# and never set them, so every agent request (tool_choice=auto) 400'd and OpenCode aborted at step 0.
# DRYRUN=1 prints what would be served and exits, touching nothing -- same contract as
# serve-starfleet.sh / serve-sglang.sh. Fleet probes launchers this way to learn each
# member's checkpoint (for its max context and its thinking options), and refuses to run
# any launcher that lacks this branch, because running one for real starts a model.
if [ "${DRYRUN:-0}" = 1 ]; then
  echo "sub=$MODEL_SUBDIR served=$SERVED_NAME need=${NEED_MB}MB util=$GPU_UTIL ctx=$MAX_MODEL_LEN port=$PORT flags=[$MODEL_FLAGS]"
  exit 0
fi

exec /home/pauloesterwitz/llama-swap/run-single-node.sh up
