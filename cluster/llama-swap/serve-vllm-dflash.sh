#!/bin/bash
# Launched by llama-swap (via memcheck.sh) as a DFlash speculative-decoding backend, on the
# patched vLLM v0.21.0 GB10 image (vllm-dflash-gb10:v0.21). ONE image serves both models.
#
#   cmd: ... memcheck.sh <need_mb> serve-vllm-dflash.sh <gemma4-26b|qwen3.6-35b> --port ${PORT}
#
# The KEY encodes model+size; the llama-swap member, --served-model-name, and container name are
# all "${KEY}-dflash" (e.g. gemma4-26b-dflash) so names are unambiguous across the several gemma4s.
#
# CO-RESIDENT (spark-small, swap:false): run alongside each other + ollama; do NOT evict ollama.
# No-OOM: (1) memcheck refuses unless we fit real MemAvailable -> clean error; (2) we release our
# memcheck reservation once serving so a co-resident sibling isn't double-counted; (3) spark-big is
# exclusive (ds4 never co-resident); (4) trap -> docker stop frees the container VRAM on swap-out.
# GPU: --runtime=nvidia + NVIDIA_VISIBLE_DEVICES=all (not --gpus all — that needs the absent
# nvidia-persistenced socket). Image ENTRYPOINT is ["vllm","serve"], so we pass only model + flags.
set -uo pipefail

IMAGE="vllm-dflash-gb10:v0.21"
MODELS=/home/pauloesterwitz/models
STATE="${GB10_STATE_DIR:-$HOME/.gb10}"
RESV="$STATE/reservations/$$"   # memcheck exec-preserved its PID as $$ — that's our reservation file

KEY="${1:-}"; shift || true
PORT=8000
while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done

# Per-model wiring. Max context 262144 (256K) — the gemmas have sliding-window attention, qwen has
# 2 KV heads. --gpu-memory-utilization must hold the KV AND match memcheck need_mb in config.yaml —
# TUNE together (check `free -g` after load). FP8 (Qwen) auto-detected.
#
# IMAGE is per-model on purpose, mid-migration. DFlash is upstream from vLLM 0.23 ("dflash" is a
# first-class method in vllm/config/speculative.py), so the patched v0.21 image is obsolete — but
# gemma4-26b is the live member and stays on the image it was tuned against until the new models
# prove out on stock. Then flip it and delete vllm-dflash-gb10:v0.21. The other three CANNOT use
# v0.21: its registry has no Qwen3_5MoeForConditionalGeneration and no DiffusionGemmaForBlockDiffusion.
STOCK="${STOCK:-vllm/vllm-openai:v0.25.1-aarch64}"   # override to smoke-test against another tag

# ── GEMMA-4 PIN, 2026-08-20 — TEMPORARY, remove when a v0.27.2+ aarch64 image exists ──────────
# Every Gemma-4 checkpoint we have (prithiv compressed-tensors AND nvidia ModelOpt, 26B AND 31B)
# fails to load on any image carrying transformers >= 5.15:
#   AmbiguousGlobalPerLayerAttributeError: 'head_dim' is a per-layer attribute and may vary across layers
# Gemma-4 has heterogeneous attention — VERIFIED here: transformers builds per_layer_config from our
# own config.json with 30 layers whose head_dim is 256 for some and 512 for others. transformers 5.15
# therefore refuses a global config.head_dim, and vLLM's converter still asks for one
# (vllm/transformers_utils/model_arch_config_convertor.py, flat getattr(config,"head_dim",0)).
# Upstream: https://github.com/vllm-project/vllm/issues/51744 — fixed in vLLM v0.27.2rc0.
# NOT a checkpoint problem: re-downloading a different NVFP4 gemma-4 quant CANNOT fix it, because the
# fault is on the reader's side. Do not go looking for one.
#   transformers 5.14.0 (vllm-nightly-ray:local) -> global head_dim returns 256, works
#   transformers 5.15.0 (v0.27.1-aarch64)        -> raises
#   transformers 5.15.1 (nightly-aarch64)        -> raises
# NOTE the old stack reads 256 globally for a model that also has 512 layers — it serves at 46.2
# tok/s and shows no ill effect, but that is exactly the ambiguity the new transformers refuses.
# TO UNPIN: when vllm/vllm-openai publishes v0.27.2+ for aarch64, point GEMMA_IMAGE at it and
# re-run:  ./test-vllm-upgrade.sh vllm/vllm-openai:v0.27.2-aarch64 1
GEMMA_IMAGE="${GEMMA_IMAGE:-vllm-nightly-ray:local}"

# ── PARTIAL UPGRADE to v0.27.1, 2026-08-20 ───────────────────────────────────────────────────
# Only keys with a PASSING single-node test on this image point at it. Each one loaded and
# answered 17*23 correctly under .../test-vllm-upgrade.sh + retest-upgrade-failures.sh:
#   qwen3.8-27b (text)  PASS 391      qwen3.6-35b-mtp4  PASS 391 (retest)
#   nemcascade  PASS 391 (serve-nemcascade.sh)   nomic-embed / embeddinggemma PASS dim=768 (serve-embed.sh)
# NOT upgraded, on purpose:
#   gemma4-*        -> GEMMA_IMAGE above: real incompatibility, vllm#51744
#   qwen3.8-27b-vision -> the VISION key was never tested on 0.27.1; only the text key was. The
#                      encoder path is exactly what 0.27.1 changed around, so it stays on the
#                      known-good image until it has its own passing test.
#   every TP=2 / starfleet member -> the sweep never actually exercised v0.27.1 there (IMAGE did
#                      not reach run-ds4-tp2-cluster.sh; the logs show v0.23.1 ran). Untested is
#                      not the same as working — they stay put until TP=2 is retested properly.
# REVERT: set UPGRADED_IMAGE=vllm-nightly-ray:local in the environment, or edit this one line.
# The old image is untouched on both nodes; see VLLM-UPGRADE-REVERT.md.
UPGRADED_IMAGE="${UPGRADED_IMAGE:-vllm/vllm-openai:v0.27.1-aarch64}"
DRAFT=""      # optional — set only for models with a DFlash draft checkpoint
ENVS=()       # optional — extra `docker run -e` vars
case "$KEY" in
  gemma4-26b)
    IMAGE="vllm-dflash-gb10:v0.21"   # ponytail: unchanged until the stock path is proven, then -> $STOCK
    BASE=/models/gemma-4-26B-A4B-it-NVFP4
    # DFlash draft REMOVED 2026-07-20 (Paul: speculative decoding "not working well for agentic coding").
    # Draft weights deleted from both nodes. Serves plain NVFP4 now -> NAME becomes gemma4-26b-nvfp4.
    # Superseded anyway by gemma4-26b-46tps-* (prithiv compressed-tensors, 46.2 vs 27.7 tok/s).
    EXTRA=(--gpu-memory-utilization 0.40 --quantization modelopt --reasoning-parser gemma4 --enable-auto-tool-choice --tool-call-parser gemma4)
    ;;
  gemma4-31b)
    # Gemma4ForConditionalGeneration. No DFlash draft exists for the 31B — no speculative config.
    # IMAGE is v0.21, NOT $STOCK, and that is not laziness: this checkpoint has
    # tie_word_embeddings=true with lm_head excluded from quantization, and stock vLLM >= 0.23 dies
    # on that combination (ParallelLMHead.tie_weights delegates to quant_method.tie_weights, ModelOpt
    # hands an excluded lm_head an UnquantizedLinearMethod, which has no tie_weights ->
    # NotImplementedError). Not fixed in v0.25.1 or main. v0.21 predates the refactor and ties the
    # weight directly. VERIFIED 2026-07-15 on Kathryn: loads, serves, 17*23=391.
    IMAGE="vllm-dflash-gb10:v0.21"
    BASE=/models/Gemma-4-31B-IT-NVFP4
    # MEASURED on Kathryn: 63 GB resident (weights 30.5 GiB + KV 386,317 tokens) at util 0.45.
    # memcheck need_mb must be ~64000, NOT the 40000 first guessed. 256K ctx -> 1.47x concurrency.
    EXTRA=(--gpu-memory-utilization 0.45 --quantization modelopt --reasoning-parser gemma4 --enable-auto-tool-choice --tool-call-parser gemma4)
    ;;
  qwen3.5-122b)
    # EXCLUSIVE (spark-big) — 77.8 GiB, never co-resident. NVIDIA's card says --quantization modelopt_fp4.
    IMAGE="$STOCK"
    BASE=/models/Qwen3.5-122B-A10B-NVFP4
    # DFlash draft REMOVED 2026-07-20 (see gemma4-26b above); draft weights deleted from both nodes.
    EXTRA=(--gpu-memory-utilization 0.80 --quantization modelopt_fp4 --kv-cache-dtype fp8
           --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
           # ponytail: shipped Qwen3.5/3.6 template raises on a 2nd system message, which opencode
           # always sends (agent prompt + harness prompt). Sidecar template renders it instead.
           --chat-template "$BASE/chat_template.opencode.jinja")
    ;;
  qwen3.6-35b)
    # Paul's fast default, NVFP4 single-node. CO-RESIDENT / node-SELECTABLE: wired as two llama-swap
    # members (qwen3.6-35b-jl local, qwen3.6-35b-kathryn via serve-kathryn.sh) — MEASURED 2026-07-17:
    # single-node 27.1 tok/s BEATS TP=2's 23.6 (only ~3B active/token -> nothing for TP to split, so
    # cross-node all-reduce is pure loss; see ds4-tp2-vs-pp-results.md). Hence single-node, per node.
    # IMAGE must be the NIGHTLY: arch is Qwen3_5MoeForConditionalGeneration, which the v0.21 DFlash
    # image lacks; nightly is the exact build that served it in the bench. Serves via Marlin W4A16
    # (no native FP4 path on GB10 for this config) — that is WHY the per-token read is so small.
    # No DFlash draft published -> NAME becomes qwen3.6-35b-nvfp4 (both members useModelName that).
    IMAGE="vllm-nightly-ray:local"
    BASE=/models/Qwen3.6-35B-A3B-NVFP4
    # util 0.35 of 121 GiB ~= 42 GiB: holds the ~22 GiB weights + a generous fp8 KV pool while staying
    # co-resident-friendly (leaves ~79 GiB). memcheck need_mb in config.yaml must track this (~43000).
    # max-model-len stays the CMD default 262144 = this model's exact native max.
    # NO --moe-backend: this checkpoint's modelopt_mixed NVFP4 scheme (u8 weights, group-16 fp8 scales)
    # is REJECTED by flashinfer_cutlass ("kernel does not support quantization scheme QuantKey(...)").
    # Let vLLM auto-select — it picks Marlin W4A16, the exact path the 27.1 tok/s bench ran on.
    # --enforce-eager matches that proven bench (27.1 already beats TP=2's 23.6); cudagraphs unproven
    # for this arch and not worth a startup crash on the default model — revisit for throughput later.
    EXTRA=(--gpu-memory-utilization 0.35 --kv-cache-dtype fp8 --enforce-eager
           --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
           # ponytail: see qwen3.5-122b above — opencode sends 2 system messages.
           --chat-template "$BASE/chat_template.opencode.jinja")
    ;;
  qwen3.6-35b-mtp4)
    # SAME NVFP4 checkpoint/flags as qwen3.6-35b, + native MTP spec=4 (Paul 2026-07-22): 57.1 tok/s
    # single-node, the peak of the MTP curve. MTP reuses the model's own draft heads (has_mtp: 19
    # draft tensors) — no separate draft model, so it fits the same ~43 GB budget. Single-node per
    # node (TP=2 loses on ~3B active/token); served on both -jean-luc and -kathryn members.
    IMAGE="$UPGRADED_IMAGE"   # UPGRADED: PASS 391 on v0.27.1 (retest 15:11)
    BASE=/models/Qwen3.6-35B-A3B-NVFP4
    # SPEED 2026-08-25 (GPU fault fixed): --enforce-eager DROPPED so CUDA graphs capture, and
    # --max-num-seqs 32 overrides the shared 4 below (EXTRA is appended last, argparse takes the
    # last occurrence). The 4 was never a memory limit — at this util the KV pool holds ~1M tokens
    # — it was a pure scheduler cap. Lifting it on qwen3.8 measured 4.2x aggregate throughput
    # (47.8 -> 201.6 tok/s at 32 concurrent) with single-stream unchanged.
    EXTRA=(--gpu-memory-utilization 0.35 --kv-cache-dtype fp8
           --max-num-seqs 32 --max-num-batched-tokens 16384
           --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
           --speculative-config '{"method":"mtp","num_speculative_tokens":4}'
           # ponytail: see qwen3.5-122b above — opencode sends 2 system messages.
           --chat-template "$BASE/chat_template.opencode.jinja")
    ;;
  qwen3.8-27b|qwen3.8-27b-vision)
    # Qwen3.8-27B-MTP-NVFP4 (sakamakismile). DENSE hybrid: 48/64 layers linear attention (GDN),
    # 16 full attention. Native VL.
    #
    # TWO keys share this block — same weights, same tuning, differing ONLY in whether the vision
    # encoder is admitted (MM below). Fleet pattern: qwen3vl32 / qwen3vl32-vision.
    #   qwen3.8-27b         image:0 — the configuration every measured number here came from.
    #   qwen3.8-27b-vision  image:1 — UNMEASURED. The vision encoder reserves activation memory the
    #                       text-only rows never paid for, so do NOT quote 13.6 tok/s for it.
    # NOT using --language-model-only: it would drop the vision tower entirely, which is the
    # opposite of what the vision member is for, and Paul wants the tower available.
    # Reasoning stays ON (--reasoning-parser qwen3, no enable_thinking/reasoning_effort override) —
    # the recipe offers those to cut thinking tokens, and we are deliberately declining them.
    MM='{"image":0,"video":0}'
    [ "$KEY" = qwen3.8-27b-vision ] && MM='{"image":1,"video":0}'
    # Text key UPGRADED (PASS 391 on v0.27.1). Vision key held back: its encoder path was never
    # tested on 0.27.1, and it is the visual-QA endpoint, so it keeps the image it was verified on.
    IMAGE="$UPGRADED_IMAGE"
    [ "$KEY" = qwen3.8-27b-vision ] && IMAGE="vllm-nightly-ray:local"
    BASE=/models/Qwen3.8-27B-MTP-NVFP4
    # CUDA GRAPHS ON — deliberately NO --enforce-eager. This is the one real change vs the sweep:
    # every sweep number (7.9 single / 7.4 TP2 / 9.1 PP2 / 11.1 MTP1 / 11.5 MTP2) was measured with
    # run-single-node.sh's ENFORCE_EAGER=1 default, i.e. cudagraph_mode=NONE and CompilationMode.NONE
    # — verified in qwen38-single.log. That default is a DFlash-era holdover (graphs crashed mid-decode
    # WITH a DFlash draft); there is no DFlash draft here, and vLLM's own DGX Spark guidance is to use
    # graphs unless you have a reason not to. 64 layers at batch 1 is exactly the launch-overhead-bound
    # case graphs fix. NOTE the known risk: the vLLM recipe for this model reports NVFP4 graph capture
    # OOMing on a 32 GB RTX 5090 — that is why util is 0.45 here and not the sweep's 0.35, leaving
    # ~20 GiB above weights+KV for capture. If it ever OOMs at startup, set ENFORCE_EAGER=1 in the env
    # to fall back; the member keeps working, just slower.
    #
    # util 0.45 = ~54.8 GiB of the 121.7 GiB pool. Weights 19.15 GiB. KV is cheap on this arch: only
    # 16 layers are full attention, 4 KV heads x 256 head_dim x 2 (K+V) x 1 byte fp8 = 32 KiB/token,
    # so a FULL 262144-token sequence costs 8 GiB. Pool therefore holds ~4 concurrent max-context
    # sequences. memcheck need_mb in config.yaml must track this (55000).
    #
    # MTP: the checkpoint ships 15 bf16 MTP modules kept out of quantization via
    # quantization_config.ignore, registered as method "qwen3_5_mtp" (NOT the generic "mtp" the
    # other fleet checkpoints use). K=3 is the MEASURED optimum of the full 2026-08-18 sweep:
    # K=1 11.1, K=2 11.5, K=3 12.9, K=4 11.9, K=5 11.7 — a real peak at 3, not a plateau.
    # --max-num-seqs 32 OVERRIDES the shared `--max-num-seqs 4` in the CMD below (EXTRA is appended
    # after it, and argparse takes the last occurrence). Scoped to this key on purpose: the other
    # members' recorded tok/s were all measured at 4, and a global change would silently invalidate
    # them. MEASURED 2026-08-19, control-vs-test with byte-identical argv otherwise:
    #        N=1    N=2    N=4    N=8    N=16    N=32   aggregate tok/s
    #   4:  12.9   21.7   44.4   48.3   47.2    47.8   <- flat from N=8: queuing, not saturation
    #  32:  11.3   21.1   45.2   79.3  133.9   201.6   <- still climbing at N=32
    # 4.2x aggregate throughput at N=32, and per-request latency is BETTER too (6.8 vs 4.2 tok/s),
    # because with the cap the extra requests were only ever waiting. Single-stream is unaffected
    # (13.5 vs 14.0 median). The 273 GB/s pool is read once per forward pass whatever the batch
    # size, so concurrency is close to free here; the cap was the whole ceiling.
    # 32 is where the sweep stopped, NOT where the hardware stops — it had not plateaued.
    # --max-num-batched-tokens 16384 overrides the shared 8192 below (same last-wins trick as
    # --max-num-seqs). It is the vLLM recipe's value for this model, and 8192 was chosen when the
    # scheduler only ever had 4 sequences in flight; at 32 concurrent the per-step token budget is
    # where the pressure moved. UNMEASURED HERE — recipe value, not a local benchmark.
    EXTRA=(--gpu-memory-utilization 0.45 --kv-cache-dtype fp8
           --limit-mm-per-prompt "$MM"
           --enable-prefix-caching --max-num-seqs 32 --max-num-batched-tokens 16384
           --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
           --speculative-config '{"method":"qwen3_5_mtp","num_speculative_tokens":3}')
    ;;
  gemma4-26b-ct)
    # SAME base model as gemma4-26b above, but the prithivMLmods COMPRESSED-TENSORS re-quant
    # (nvfp4-pack-quantized / NVFP4A16) instead of nvidia ModelOpt. MEASURED 2026-07-20: 46.2 tok/s
    # single-node vs the ModelOpt+DFlash member's 27.7 — the fastest model in the fleet. It also
    # TENSOR-PARALLELS, which the ModelOpt build cannot (NotImplementedError, NVFP4 cutlass padding).
    # NIGHTLY image: compressed-tensors needs modern vLLM; v0.21 has neither this quant path nor the arch.
    # NO --moe-backend: let vLLM auto-select (forcing flashinfer_cutlass is exactly what broke qwen3.6).
    # --enforce-eager matches the benched config; cudagraphs untested for this quant path.
    IMAGE="$GEMMA_IMAGE"   # PINNED: transformers>=5.15 breaks Gemma-4 (see GEMMA_IMAGE above)
    BASE=/models/gemma-4-26B-A4B-it-NVFP4A16-prithiv
    # SPEED 2026-08-25 (GPU fault fixed): --enforce-eager DROPPED so CUDA graphs capture, and
    # --max-num-seqs 32 overrides the shared 4 below (EXTRA is appended last, argparse takes the
    # last occurrence). The 4 was never a memory limit — at this util the KV pool holds ~1M tokens,
    # thousands of typical requests — it was a pure scheduler cap. Lifting it on qwen3.8 measured
    # 4.2x aggregate throughput (47.8 -> 201.6 tok/s at 32 concurrent) with single-stream unchanged.
    # NOTE gemma is PINNED to vllm-nightly-ray:local (vllm#51744). CUDA graphs on this quant
    # path are UNTESTED — if it fails to capture, put --enforce-eager back on this line.
    EXTRA=(--gpu-memory-utilization 0.40 --kv-cache-dtype fp8
           --max-num-seqs 32 --max-num-batched-tokens 16384
           --reasoning-parser gemma4 --enable-auto-tool-choice --tool-call-parser gemma4)
    ;;
  *) echo "serve-vllm-dflash: unknown key '$KEY' (want gemma4-26b|gemma4-26b-ct|gemma4-31b|qwen3.5-122b|qwen3.6-35b|qwen3.6-35b-mtp4|qwen3.8-27b)" >&2; exit 1;;
esac
# IMAGE_OVERRIDE: opt-in escape hatch for testing a different vLLM build without editing the
# per-key IMAGE lines above. Unset (the normal case) changes nothing at all — each key keeps the
# image it was tuned and measured against. Set it to smoke-test an upgrade candidate:
#   IMAGE_OVERRIDE=vllm/vllm-openai:v0.27.1-aarch64 ./serve-vllm-dflash.sh qwen3.8-27b --port 8000
# The other launchers (run-single-node.sh, run-ds4-tp2-cluster.sh, serve-embed.sh) already take
# IMAGE from the environment; this brings this one in line so ONE variable covers the whole fleet.
IMAGE="${IMAGE_OVERRIDE:-$IMAGE}"

# MML_OVERRIDE: same idea for context length. Unset = the deployed 262144, unchanged. Set it to
# benchmark a different context WITHOUT touching what is served:
#   MML_OVERRIDE=32768 ./serve-vllm-dflash.sh gemma4-26b-ct --port 8000
# Why this exists: the fleet's recorded tok/s (the "46tps"/"57tps" in the member names) were all
# measured by bench-queue.sh at MAX_MODEL_LEN=32768, but every member SERVES at 262144. Those
# numbers therefore describe a configuration nobody runs.

# llama-swap member + served-model-name. Suffix tells the truth about the engine: only the models
# with a draft checkpoint are actually DFlash. gemma4-31b has no draft published and diffusiongemma
# emits a whole block at a time (speculating on a diffusion decoder is meaningless), so calling
# those "-dflash" would be a lie the next reader has to debug. gemma4-26b-dflash keeps its name.
if [ -n "$DRAFT" ]; then NAME="${KEY}-dflash"; else NAME="${KEY}-nvfp4"; fi

# Remove any stale container holding the GPU; tear down on swap-out so its VRAM is freed.
docker rm -f "$NAME" >/dev/null 2>&1 || true
cleanup() { echo "serve-vllm-dflash: stopping $NAME" >&2; docker stop -t 10 "$NAME" >/dev/null 2>&1 || true; }
# HUP matters when this runs over ssh (the kathryn members): llama-swap kills the local ssh, the
# remote pty closes, and bash gets SIGHUP — not TERM. Without HUP here the trap never fires and the
# container is orphaned, holding 64GB of Kathryn's pool that her memcheck cannot reclaim.
trap cleanup EXIT INT TERM HUP

# DFlash draft via --speculative-config (num_speculative_tokens=15), only for models that have a
# draft checkpoint — an empty --speculative-config model path makes vLLM abort at startup.
SPEC=()
[ -n "$DRAFT" ] && SPEC=(--speculative-config "{\"method\":\"dflash\",\"model\":\"$DRAFT\",\"num_speculative_tokens\":15}")

# --init so SIGTERM reaches vLLM (PID 1); --rm auto-cleans.
# --served-model-name = the llama-swap member so proxied requests match.
# MODEL_FLAGS_EXTRA: the same opt-in hook serve-starfleet.sh and serve-sglang.sh expose.
# ctx-env.sh appends --default-chat-template-kwargs to it, so the per-model thinking
# default reaches this engine too. Unquoted on purpose -- the value is whitespace-separated
# flags; ctx-env.sh guarantees the JSON payload itself contains no whitespace.
EXTRA+=(${MODEL_FLAGS_EXTRA:-})

CMD=(docker run --rm --name "$NAME" --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all --ipc=host --init
    "${ENVS[@]}"
    -p 127.0.0.1:"${PORT}":8000
    -v "$MODELS":/models:ro
    "$IMAGE"
    "$BASE"
      --served-model-name "$NAME"
      --trust-remote-code
      --max-model-len "${MML_OVERRIDE:-262144}" --max-num-seqs 4
      --max-num-batched-tokens 8192
      "${EXTRA[@]}"
      "${SPEC[@]}"
      --host 0.0.0.0 --port 8000)

# ponytail: DRYRUN=1 prints the argv and exits — the self-check for the case table above, runnable
# with no GPU and no pool pressure. `DRYRUN=1 ./serve-vllm-dflash.sh <key> --port 8000` per key.
if [ -n "${DRYRUN:-}" ]; then printf '%q ' "${CMD[@]}"; echo; trap - EXIT INT TERM; exit 0; fi

"${CMD[@]}" &
DPID=$!

# Release the memcheck reservation once vLLM is serving (VRAM now in MemAvailable) so a co-resident
# sibling isn't double-counted out. Held during load as a concurrent-load race guard.
( while kill -0 "$DPID" 2>/dev/null; do
    curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && { rm -f "$RESV" 2>/dev/null; break; }
    sleep 3
  done ) &

wait "$DPID"
