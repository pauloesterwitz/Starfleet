#!/bin/bash
# serve-ornith-tp2.sh: Ornith-1.5-122B-NVFP4 across BOTH Sparks (tensor parallel 2, Ray + RoCE) as a llama-swap
# starfleet member. Same job as serve-starfleet.sh: set the env that run-ds4-tp2-cluster.sh reads, then exec it.
#
#   cmd: ctx-env.sh <member> serve-ornith-tp2.sh --port ${PORT}
#
# ponytail: its own file, not a new key in serve-starfleet.sh, so a typo here cannot break the live starfleet
# members. Fleet reads the DRYRUN line (sub=, ctx=) exactly as it does for serve-starfleet.sh.
# Knobs (env; to change what llama-swap launches, edit the defaults below, no config.yaml write needed):
#   ENFORCE_EAGER=1   no CUDA graphs at all (the fallback if graph capture hangs or the engine dies)
#   CUDAGRAPH_MODE    PIECEWISE by default: vllm#40969 reports FULL_AND_PIECEWISE hangs on 2x GB10 TP=2,
#                     serve-starfleet.sh reproduced that twice; PIECEWISE is the only stable graph mode there
#   MTP_K             MTP draft tokens (3 = best on the fleet's Qwen3.5-122B; vLLM warns K>1 reuses one MTP layer)
#   KV_BYTES          KV pool per GPU rank (6 GiB; the single-node test showed 12 GiB = 776k tokens)
#   IMAGE             vllm-ray:0.27.1 (vLLM 0.27.1 + Ray 2.56.0), same image id on both nodes
#   MM_LIMIT          --limit-mm-per-prompt JSON, space-free. Default {"image":4,"video":0} = vision tower ON;
#                     {"image":0,"video":0} = text-only. The tower is the stock Qwen3.5-122B one (333 BF16 tensors,
#                     bit-identical). Vision ON since 2026-10-23 (exp-ornith-vision.sh, ornith-bench/vision-probes.jsonl):
#                     4 of 4 image probes right (shapes, chart, slide layout defects, German OCR exact), ~250 tokens per
#                     640x400 image; text unchanged (short 46.4 tok/s, 32k 41.3, collapse 0/40, tools 20/20); load 591 s.
#                     CAUTION: with NO image attached it confidently describes an imaginary one.
set -uo pipefail
LS=/home/pauloesterwitz/llama-swap

PORT=8000
while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done

SUB=Ornith-1.5-122B-NVFP4
SERVED=ornith-1.5-122b-nvfp4-mtp3-starfleet     # must match the member's useModelName in config.yaml
NEED=60000          # MB per node: about 36 GiB weights + 6 GiB KV + activations, graphs, NCCL buffers
UTIL=0.72           # only the startup free-memory check: the KV pool is explicit
MML="${MML_OVERRIDE:-262144}"
MTP_K="${MTP_K:-3}"
KV_BYTES="${KV_BYTES:-6442450944}"
CUDAGRAPH_MODE="${CUDAGRAPH_MODE:-PIECEWISE}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"
ATTN_BACKEND="${ATTN_BACKEND:-}"                    # empty = vLLM's choice (FLASHINFER); TRITON_ATTN unlocks FULL graphs with MTP
CAPTURE_SIZES="${CAPTURE_SIZES:-[4,8,16,32,64]}"
VISION_ON='{"image":4,"video":0}'                    # not inline in ${..:-..}: the JSON's } would end the expansion
MM_LIMIT="${MM_LIMIT:-$VISION_ON}"

# Knob file for experiments and for the chosen config: KEY=VALUE lines in ~/.gb10/ornith-tp2.env. Only the keys below
# are read (no eval), and the file wins over the defaults above. Delete it to get the defaults back.
# Why TRITON_ATTN matters: on GB10 (sm_121) FlashInfer has no trtllm-gen kernels, so it supports full CUDA graphs only for
# single-token decode. With MTP K=3 every decode step is 4 tokens, so vLLM 0.27.1 downgrades any FULL mode to PIECEWISE
# (config/compilation.py resolve_cudagraph_mode_and_sizes). Triton attention supports full graphs always.
KNOBS="${GB10_STATE_DIR:-$HOME/.gb10}/ornith-tp2.env"
if [ -r "$KNOBS" ]; then
  while IFS='=' read -r k v; do
    case "$k" in ENFORCE_EAGER|CUDAGRAPH_MODE|ATTN_BACKEND|MTP_K|KV_BYTES|CAPTURE_SIZES|MM_LIMIT) printf -v "$k" '%s' "$v" ;; esac
  done < "$KNOBS"
fi

# Values are space-free by construction: the cluster script word-splits MODEL_FLAGS.
FLAGS="--kv-cache-dtype fp8 --kv-cache-memory-bytes $KV_BYTES --limit-mm-per-prompt $MM_LIMIT --no-enable-flashinfer-autotune --enable-prefix-caching --max-num-seqs 16 --max-num-batched-tokens 16384 --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder --chat-template /models/$SUB/chat_template.opencode.jinja --speculative-config {\"method\":\"mtp\",\"num_speculative_tokens\":$MTP_K}"
[ "$ENFORCE_EAGER" = 1 ] || FLAGS="$FLAGS --compilation-config {\"cudagraph_mode\":\"$CUDAGRAPH_MODE\",\"cudagraph_capture_sizes\":$CAPTURE_SIZES}"
[ -n "$ATTN_BACKEND" ] && FLAGS="$FLAGS --attention-backend $ATTN_BACKEND"
FLAGS="$FLAGS ${MODEL_FLAGS_EXTRA:-}"

export IMAGE="${IMAGE:-vllm-ray:0.27.1}" MODEL_SUBDIR="$SUB" SERVED_NAME="$SERVED" TP=2 PP=1 USE_ROCE=1 \
       NEED_MB="$NEED" GPU_UTIL="$UTIL" MAX_MODEL_LEN="$MML" MODEL_FLAGS="$FLAGS" PORT="$PORT" ENFORCE_EAGER

if [ "${DRYRUN:-0}" = 1 ]; then
  echo "key=ornith-tp2 sub=$SUB need=${NEED}MB util=$UTIL ctx=$MML port=$PORT served=$SERVED image=$IMAGE eager=$ENFORCE_EAGER graphs=$CUDAGRAPH_MODE attn=${ATTN_BACKEND:-auto} mtp=$MTP_K flags=[$FLAGS]"; exit 0
fi
exec "$LS/run-ds4-tp2-cluster.sh"
