#!/bin/bash
# serve-starfleet.sh <key> --port <port>
# Serve a model across BOTH Sparks (TP=2 + RoCE) as a llama-swap member. Thin KEY->config table in
# front of run-ds4-tp2-cluster.sh — same idiom as serve-vllm-dflash.sh for the single-node models.
#
# Why a wrapper instead of inlining env in config.yaml: the per-model MODEL_FLAGS contain SPACES
# ("--kv-cache-dtype fp8 --moe-backend ..."), and llama-swap word-splits the cmd string, so quoted
# env values would not survive. The table keeps every value here, where quoting is under our control.
#
# EVERY value below is the config that was actually MEASURED — do not "tidy" them:
#   gemma4-26b-ct  48.2 tok/s  (agg@16 453.8)   util 0.72, no --moe-backend (auto-select)
#   qwen3.5-122b   20.9 tok/s  (agg@16 160.3)   util 0.72, flashinfer_cutlass
#   nemotron-120b  19.0 tok/s  (agg@16 124.9)   util 0.80, no --moe-backend
#   qwen3-235b     18.6 tok/s  (agg@16 111.9)   util 0.72, flashinfer_cutlass  (won't fit one node)
# NOTE on --moe-backend: it is NOT universal. flashinfer_cutlass rejects some NVFP4 schemes outright
# (qwen3.6's modelopt_mixed, and the gemma compressed-tensors path) — omit it and let vLLM choose.
#
# TOOL CALLING (added 2026-08-10): every member below now passes --enable-auto-tool-choice
# plus a --tool-call-parser / --reasoning-parser pair. This is NOT optional polish. opencode
# (and every other agent client) sends tool_choice:"auto", and vLLM rejects that at REQUEST
# time — '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be
# set' — if the flags were not present at SERVE time. The model loads fine and plain chat
# works, so the gap only shows up as a broken agent session. Parser names are per-checkpoint
# (they describe the markup the model actually emits) — copy them from the checkpoint's own
# docs/vllm_deploy_guide.md or its chat_template.jinja, never guess, and check the name is
# registered in the image: vllm/tool_parsers/__init__.py + vllm/reasoning/__init__.py.
set -uo pipefail

LS=/home/pauloesterwitz/llama-swap
KEY="${1:?usage: serve-starfleet.sh <key> --port <port>}"; shift
PORT=8000; while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8000}"; shift 2 || shift;; *) shift;; esac; done

case "$KEY" in
  # gemma4-26b-ct) key REMOVED 2026-08-26 with its member — single-node is faster (see config.yaml).
  qwen3.5-122b)
    SUB=Qwen3.5-122B-A10B-NVFP4;             NEED=55000; UTIL=0.72; MML=262144
    FLAGS="--kv-cache-dtype fp8 --moe-backend flashinfer_cutlass --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --chat-template /models/Qwen3.5-122B-A10B-NVFP4/chat_template.opencode.jinja" ;;
  qwen3.5-122b-mtp3)
    # SAME NVFP4 checkpoint as qwen3.5-122b, + native MTP spec=3 (Paul 2026-07-22, eval grading).
    # UNMEASURED: keep NEED/UTIL at the measured non-MTP values and re-tune if the MTP KV pushes
    # memory. If this checkpoint carries no MTP module, vLLM aborts at startup (clean, not OOM) -
    # the non-MTP qwen3.5-122b-21tps-starfleet member is untouched and stays the fallback.
    SUB=Qwen3.5-122B-A10B-NVFP4;             NEED=55000; UTIL=0.72; MML=262144
    FLAGS='--kv-cache-dtype fp8 --moe-backend flashinfer_cutlass --speculative-config {"method":"mtp","num_speculative_tokens":3} --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --chat-template /models/Qwen3.5-122B-A10B-NVFP4/chat_template.opencode.jinja' ;;
  nemotron-120b)
    SUB=NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4; NEED=100000; UTIL=0.80; MML=262144
    FLAGS="--kv-cache-dtype fp8 --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser nemotron_v3" ;;
  qwen3-235b)
    SUB=Qwen3-235B-A22B-NVFP4;               NEED=90000; UTIL=0.72; MML=40960
    FLAGS="--kv-cache-dtype fp8 --moe-backend flashinfer_cutlass --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3" ;;
  minimaxawq)
    # cyankiwi AWQ int4 — head-to-head vs nvidia NVFP4 minimax (25.4 TP2). AWQ Marlin int4 is
    # more mature on GB10 sm_121 than NVFP4 (which falls back to Marlin anyway). NO --moe-backend
    # (cutlass is NVFP4-only; AWQ auto-selects awq_marlin). TP=2-only (~122 GiB > one node).
    SUB=MiniMax-M2.7-AWQ-4bit;                NEED=90000; UTIL=0.72; MML=196608
    FLAGS="--kv-cache-dtype fp8 --enable-auto-tool-choice --tool-call-parser minimax_m2 --reasoning-parser minimax_m2_append_think" ;;
  gemma4-31b-ct)
    # 18.6 tok/s TP=2 (vs 10.4 single). compressed-tensors -> NO --moe-backend (auto-select Marlin;
    # forcing cutlass rejects this quant). Small model -> util 0.35.
    # PINNED to transformers<5.15 — Gemma-4 heterogeneous head_dim; vllm#51744, fixed in
    # v0.27.2rc0 (no aarch64 image yet). Without this an IMAGE bump in run-ds4-tp2-cluster.sh
    # would silently break this member. See the GEMMA_IMAGE block in serve-vllm-dflash.sh.
    export IMAGE="${GEMMA_IMAGE:-vllm-nightly-ray:local}"
    SUB=gemma-4-31B-it-NVFP4-prithiv;         NEED=45000; UTIL=0.35; MML=262144
    FLAGS="--kv-cache-dtype fp8 --enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4" ;;
  qwen3vl32)
    # --reasoning-parser qwen3 REMOVED 2026-08-28. It was silently eating every reply.
    # MEASURED on qwenvl235-vision with "explain why the sky is blue": finish_reason=stop,
    # 62 completion tokens, reasoning=324 chars containing the COMPLETE correct answer, and
    # content="" - empty. All three Qwen3-VL members behaved identically (chat WARN in the
    # compat matrix). The qwen3 parser expects a </think> terminator that Qwen3-VL does not
    # emit, so it classifies the whole output as reasoning. OpenCode renders content, not
    # reasoning, so the user sees a BLANK assistant message while the model is working fine.
    # These checkpoints are not chain-of-thought models in this mode; they need no reasoning
    # parser. Tool calling is unaffected (--tool-call-parser hermes, which already tested PASS).
    # 18.9 tok/s TP=2 (vs 10.6 single). Vision-language (Qwen3VLForConditionalGeneration) — the
    # --limit-mm-per-prompt keeps profiling from reserving the vision-encoder activation (the 122B
    # lesson) so it serves text at full KV. JSON value is space-free -> survives the word-split.
    SUB=Qwen3-VL-32B-Instruct-NVFP4;          NEED=45000; UTIL=0.35; MML=262144
    FLAGS='--kv-cache-dtype fp8 --limit-mm-per-prompt {"image":0,"video":0} --enable-auto-tool-choice --tool-call-parser hermes' ;;
  qwen3vl32-vision)
    # SAME checkpoint/hardware as qwen3vl32 above, but with the vision encoder ENABLED
    # (image:1). Added 2026-07-27: ollama is deprecated, so the visual-QA loops (pptx,
    # imagegen, paperbanana critic) need a real multimodal endpoint and the text-only
    # member above answers "At most 0 image(s) may be provided". UNMEASURED: the vision
    # activation reserves memory the 18.9 tok/s text row did not, so do NOT quote that
    # number for this member — re-bench before adding it to the leaderboard.
    SUB=Qwen3-VL-32B-Instruct-NVFP4;          NEED=45000; UTIL=0.35; MML=262144
    FLAGS='--kv-cache-dtype fp8 --limit-mm-per-prompt {"image":1,"video":0} --enable-auto-tool-choice --tool-call-parser hermes' ;;
  nemotron-120b-mtp3)
    # Same NVFP4 checkpoint as nemotron-120b + native MTP spec=3 -> 36.1 tok/s TP=2 (vs 19.0 non-MTP, +90%).
    # --no-enable-flashinfer-autotune ADDED 2026-08-28: this member died every launch with
    # "RayWorkerProc rank=[0] died unexpectedly" ~2.5 min AFTER memory profiling succeeded
    # (KV sized fine at 59.1/55.41 GiB), i.e. not a fit problem. The log window before the death
    # is wall-to-wall flashinfer autotuning of trtllm::fused_moe::gemm1/gemm2 plus two
    # "No available shared memory broadcast block found in 60 seconds" warnings - the worker is
    # wedged in autotune long enough that the executor gives up on it. qwen38flashnext already
    # carried this flag for the same reason, so the failure mode was known on this hardware.
    SUB=NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4; NEED=100000; UTIL=0.80; MML=262144
    FLAGS='--kv-cache-dtype fp8 --no-enable-flashinfer-autotune --speculative-config {"method":"mtp","num_speculative_tokens":3} --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser nemotron_v3' ;;
  # qwenvl235) key REMOVED 2026-08-25 with its member — text-only VLM is not a useful config.
  qwenvl235-vision)
    # SAME checkpoint/hardware as qwenvl235 above, with the vision encoder ENABLED (image:1).
    # Added 2026-07-27 alongside qwen3vl32-vision: the text-only member answers
    # "At most 0 image(s) may be provided". The vision activation costs memory the
    # measured 16.7 tok/s text row did not pay for — UNMEASURED, re-bench before quoting.
    SUB=Qwen3-VL-235B-A22B-MLPerf-NVFP4;       NEED=90000; UTIL=0.72; MML=262144
    FLAGS='--kv-cache-dtype fp8 --moe-backend flashinfer_cutlass --limit-mm-per-prompt {"image":1,"video":0} --enable-auto-tool-choice --tool-call-parser hermes' ;;
  qwen397v2)
    # EXPERIMENTAL knife-edge: 397B at 12.3 tok/s TP=2 - serves with only 2-4 GiB free/node. Text-only frees
    # the vision reservation; needs Ray memory-monitor OFF + tiny KV. NEED 118000 so it only loads near-empty.
    SUB=Qwen3.5-397B-A17B-NVFP4-V2;            NEED=118000; UTIL=0.898; MML=2048
    export RAY_MEM_MONITOR_MS=0 VLLM_FI_WS=134217728
    FLAGS='--kv-cache-dtype fp8 --moe-backend flashinfer_cutlass --max-num-seqs 1 --limit-mm-per-prompt {"image":0,"video":0} --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3' ;;
  qwen38mtp3)
    # Qwen3.8-27B-MTP-NVFP4 at MTP K=3 across BOTH Sparks. The sweep's headline result: 14.9 tok/s,
    # vs 12.9 single-node at the same K and 7.4 for TP=2 with MTP OFF. That inversion is the point —
    # bare TP=2 LOSES to single-node here (64 layers x ~128 all-reduces/token over a fabric giving
    # vLLM ~9-10 GB/s effective), but MTP verifies 4 tokens per forward pass, so that fixed comms
    # cost is amortised across them while the halved per-node weight read (19.15 -> ~9.6 GiB) still
    # pays. Speculation is what makes tensor-parallel worth it on this model; do NOT drop the
    # --speculative-config here expecting a slower-but-working member — you get 7.4, not 14.9.
    # PP=2 is NOT an option at any K: Qwen3_5ForConditionalGeneration lacks SupportsPP, and vLLM
    # raises NotImplementedError from speculative.py while verifying the draft (qwen38mtp3pp2 FAIL).
    # HISTORY, because the obvious "just turn graphs on" has already been tried and it failed:
    # MEASURED 2026-08-18, global CUDA graphs under TP=2 gave 7.1 tok/s (worse than single-node's
    # 13.6) and then killed the engine with
    #   TimeoutError: RPC call to sample_tokens timed out   (multiproc_executor -> shm_broadcast)
    # Not memory — that run had MORE KV than single-node (2,167,447 tokens / 8.27x vs 866,979 /
    # 3.31x). It is speculative SAMPLING across the node boundary: with graphs captured the
    # per-step sample_tokens RPC stalls, draft steps are lost, and MTP acceptance collapses from
    # 2.12 to 1.50 of a possible 4.0. Falling back to global eager gave 12.0 — still under
    # single-node, and still HTTP 500 at N=8 — which is why no member points here.
    #
    # NOW TESTING (2026-08-20, from the vLLM recipe): enforce_eager INSIDE --speculative-config
    # applies eager to the DRAFT ONLY, leaving CUDA graphs on the target. That is exactly the split
    # the failure calls for — graphs helped the target (+5% single-node) and broke the draft's
    # cross-node sampling — and it was not expressible before we knew the key existed.
    # ENFORCE_EAGER=0 therefore goes back ON: target graphs, draft eager. UNVERIFIED — if this
    # still 500s or lands under 13.6, this key stays unwired and single-node remains the answer.
    SUB=Qwen3.8-27B-MTP-NVFP4;                 NEED=55000; UTIL=0.45; MML=262144
    export ENFORCE_EAGER=0
    FLAGS='--kv-cache-dtype fp8 --limit-mm-per-prompt {"image":0,"video":0} --enable-prefix-caching --speculative-config {"method":"qwen3_5_mtp","num_speculative_tokens":3,"enforce_eager":true} --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3' ;;
  qwen38flashnext)
    # Qwen3.8-Flash-Next (released 2026-08-26, Qwen4-architecture preview): 125B/6B-active MoE
    # + a 51B-param n-gram embedding table + 4B MTP, 512 experts. Inferact community NVFP4
    # (routed experts only in 4-bit — attention/QSA/GDN/vision/n-gram/MTP stay BF16/FP8).
    # REAL total per model.safetensors.index.json: 170.19 GiB (metadata.total_size) — NOT the
    # ~118GB first reported by web research (2026-08-26 correction; that number was wrong).
    # Barely smaller than the official FP8 (172.78 GiB) since only the routed experts shrink.
    # Does NOT fit one Spark even alone — TP=2-forced, same as qwen3-235b/hy3mtp.
    # NEED/UTIL below are the OPTIMISTIC-sharding estimate (~85GB/node if the n-gram table
    # splits evenly under TP=2), picked deliberately over the worst-case ~110GB/node (n-gram
    # replicated per rank) because the worst-case number exceeds BOTH Sparks' real idle ceiling
    # (measured 2026-08-26: Jean-Luc ~108GB, Kathryn ~96GB avail when idle — well under the
    # nominal 121 GiB pool) and would never even attempt a load. If the pessimistic case is what
    # actually happens, vLLM raises a clean CUDA OOM inside the container (memcheck's host-level
    # gate already ensures no other big consumer is competing, so this fails safe either way) —
    # that failure IS the answer to which sharding behavior is real. Re-measure and tighten once
    # a load succeeds, per every other row in this file. UNMEASURED end to end otherwise:
    # same-day community quant, never loaded on this fleet, tok/s unknown.
    # Parser names verified against Qwen's own vLLM recipe (recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next)
    # examples, not guessed: qwen3_coder/qwen3 matches this fleet's existing Qwen convention.
    # Starts text-only (image:0) and at MML=16384 — raise via ctxproxy
    # (@<tokens>) only after confirming headroom; do not jump straight to 262144.
    # IMAGE pinned 2026-08-28 to vllm-qwen38fn-ray:local — the DEDICATED qwen38-flash-next
    # vLLM image (arm64-cu130) + the pinned ray==2.56.0 layer (Dockerfile.qwen38fn in
    # ~/vllm-ray-build). The fleet's default images CANNOT serve this model: their vLLM/
    # transformers predate qwen4_exp (see the DISABLED note in config.yaml). This image's
    # vLLM registers Qwen4ExpForConditionalGeneration + Qwen3_8FlashNextMTP natively
    # (verified via ModelRegistry). Must exist on BOTH nodes (docker save|load to Kathryn).
    export IMAGE="${QWEN38FN_IMAGE:-vllm-qwen38fn-ray:local}"
    # VLLM_PLE_CPU_OFFLOAD=1: REQUIRED here, and it must actually reach the CONTAINER —
    # run-ds4-tp2-cluster.sh's common_flags forwards it since 2026-08-28 (it silently didn't
    # before, so the first three attempts all ran with offload OFF regardless of this export).
    # MEASURED without offload (attempts 2+3, identical): head node OOM-killed by Ray at
    # ~116/121.69 GB (95%) at INIT, 0/21 checkpoint shards loaded. Read of the impl
    # (vllm/models/qwen3_8_flash_next/nvidia/ple_layer.py): non-offload allocates the PLE
    # n-gram table as a TP-sharded VocabParallelEmbedding but in params dtype (BF16) —
    # ~51GB/rank for the 51B-param table — plus ~40GB/rank other weights + head overhead
    # blows the pool at skeleton-allocation time. WITH offload, ONE CPU-side process per node
    # owns the table in FP8 (~51GB once, not per rank) and GPU ranks retain only the scale;
    # on unified memory that is the SMALLER layout (~95GB head worst case), not an extra copy.
    # FINAL VERDICT 2026-08-28 (attempt 4, offload confirmed ACTIVE — "PleOffload: spawning
    # worker rank=0" in the log): loading reached 12/21 shards (vs 0/21 without offload) and
    # STILL hit Ray's 95% kill line at 115.73/121.69 GB on the head. Rank 0 carries its ~40GB
    # TP shard PLUS the whole PLE table in the same unified pool — a real ~5-15GB capacity
    # shortfall BEFORE any KV cache, not a config problem. Four attempts, three distinct
    # root causes fixed (qwen4_exp image, fp8-KV rejection, env not forwarded); this last
    # wall is physics. UNBLOCK candidates: (a) a quant with the PLE table in 4-bit
    # (~45GB saved -> fits easily), (b) vLLM gaining PLE placement on a non-rank-0 node,
    # (c) knife-edge retry with RAY_MEM_MONITOR_MS=0 + swap — ATTEMPTED 2026-08-28 with
    # Paul's authorization (attempt 5: monitor off, UTIL 0.45, fresh 15GB swap, external
    # 3GB-floor guard): Jean-Luc hit 120/121 GiB used + ALL 15GB swap at only 5/21 shards
    # loaded; the guard tore down cleanly before a kernel OOM. Head-node demand exceeds
    # 136 GiB with 76% of the checkpoint still unloaded. (c) is CLOSED — do not retry it.
    # Remaining unblocks are (a) a 4-bit-PLE quant or (b) vLLM PLE placement off rank 0.
    # Measured peaks during the attempt-3/4 loads: Jean-Luc 113 GiB AND Kathryn 109 GiB of
    # 121 — BOTH nodes ride the ceiling.
    export VLLM_PLE_CPU_OFFLOAD=1
    # NO --kv-cache-dtype fp8 here, unlike every other key. MEASURED 2026-08-28: the engine
    # refuses it at startup — "NotImplementedError: Qwen3.8-Flash-Next QSA requires a BF16
    # main KV cache". BF16 KV costs 2x fp8 per token, but only 12/48 layers are full-attention
    # (rest are GDN linear-attention with constant state), so at MML 16384 the hit is trivial.
    # UTIL 0.70 -> 0.45 for the knife-edge attempt (Paul authorized, 2026-08-28): vLLM
    # eagerly allocates the whole util pool; 0.45 = ~55GB (40 weights + ~15 KV) instead of
    # ~85GB — about 30GB less head-node pressure on top of the CPU-side PLE table. Plenty
    # for MML 16384. Re-raise only with measured headroom.
    # WEIGHTS DELETED 2026-08-28 (Paul asked to clear the qwen3.8-flash downloads). This key
    # cannot run until ~/models/Qwen3.8-Flash-Next-NVFP4-inferact is re-downloaded via
    # ~/models/dl-qwen38flashnext.sh. Superseded anyway: qwen38fn-radixark is the same model,
    # 44GB smaller on disk, and fails identically (same PLE-in-BF16 root cause, documented there).
    SUB=Qwen3.8-Flash-Next-NVFP4-inferact;     NEED=95000; UTIL=0.45; MML=16384
    FLAGS='--no-enable-flashinfer-autotune --enable-prefix-caching --limit-mm-per-prompt {"image":0,"video":0} --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3' ;;
  qwen38fn-radixark)
    # SAME model as qwen38flashnext above, DIFFERENT quant — this is the one that can actually fit.
    # RadixArk NVFP4, 125.96 GiB total (HF API file sum, not the card's rounded "135 GB"), vs
    # 170.19 GiB for Inferact. The whole difference is the PLE n-gram table: shipped here as FP8
    # in model-plefp8-*.safetensors (47.69 GiB) instead of BF16 (~102 GB). That ~44 GB is exactly
    # what overflowed the head node on every Inferact attempt (see the qwen38flashnext notes).
    # Its card says "SGLang only", but that is documentation, not a constraint: config.json is
    # plain modelopt/NVFP4 (quant_algo NVFP4, quant_method modelopt, ordinary `ignore` list
    # covering embed/attn/mtp/shared_expert/ple/visual/lm_head) — the same scheme vLLM already
    # loads for every other NVFP4 member in this file. Nothing SGLang-specific is in the weights.
    # Same image and same two hard-won flags as the Inferact key: dedicated qwen4_exp-aware image,
    # BF16 KV (QSA rejects fp8), PLE CPU offload forwarded into the container.
    # UNMEASURED: NEED/UTIL are estimates (head ~= 39GB TP shard + 47.7GB FP8 PLE + KV ~= 87GB).
    # Re-measure and tighten on first successful load, per every other row in this file.
    export IMAGE="${QWEN38FN_IMAGE:-vllm-qwen38fn-ray:local}"
    export VLLM_PLE_CPU_OFFLOAD=1
    # UTIL 0.45 -> 0.38, MEASURED 2026-08-28. At 0.45 the head node died at 68/206 shards with
    # 116.23/121.69 GB. The budget is arithmetic, not mystery: vLLM reserves util*121 on the GPU
    # side (0.45 = 54GB) while the PLE offload process on rank 0 separately holds the FP8 n-gram
    # table (47.7GB) in the SAME unified pool, plus ~14GB torch/CUDA/Ray overhead = ~116GB.
    # The table is NOT being dequantized to BF16 here (that is SGLang's path) — 47.7GB is real.
    # 0.38 = ~46GB, still covering the ~39GB per-rank weight shard with ~7GB KV (ample at MML
    # 16384), for ~108GB total and ~13GB headroom. Note the load is ASYMMETRIC: rank 0 (Jean-Luc)
    # carries the whole PLE table while Kathryn sat at 51GB — do not size this off Kathryn.
    # NOTE: do NOT add --swap-space here. This vLLM build removed that flag; passing it aborts
    # the server at argparse with "unrecognized arguments" before any weight loads (tried 2026-08-28).
    #
    # ROOT CAUSE, FOUND 2026-08-28 — the disk saving is a MIRAGE, and this is why no current
    # quant fits. Ray's own OOM report names the culprit process:
    #     PID 2014  64.02 GB  PleOffloadWorker      <- still climbing at 69/206 shards
    #     PID 1885   2.79 GB  RayWorkerProc (TP0)
    # RadixArk ships the PLE table as FP8 on disk (47.7GB) BUT its config.json puts "*.ple.*" in
    # the quantization_config `ignore` list, so vLLM sees the table as UNQUANTIZED and expands it
    # to BF16 (~95GB) at load — exactly what the card means by "dequantized to BF16 at load time".
    # Hence Inferact (BF16 PLE, 170GiB) and RadixArk (FP8 PLE, 126GiB) die at the SAME ~116GB.
    # Proof it is not the KV budget: dropping UTIL 0.45 -> 0.38 moved the death point by 0.1GB
    # (116.23 -> 116.34), because the OOM happens during weight load, before KV is ever allocated.
    # WHAT WOULD ACTUALLY UNBLOCK: a quant whose quantization_config *includes* the PLE/ngram
    # table as a quantized target (4-bit, or even FP8 kept as FP8) so vLLM's
    # _get_ple_embedding_quant_method path keeps it compressed in the offload worker. Storing
    # compressed bytes on disk is NOT enough. Do not re-test another quant without first checking
    # its config.json: if "*.ple.*" / ngram is in `ignore`, it will fail here exactly like these two.
    SUB=Qwen3.8-Flash-Next-NVFP4-radixark;     NEED=95000; UTIL=0.38; MML=16384
    FLAGS='--no-enable-flashinfer-autotune --enable-prefix-caching --limit-mm-per-prompt {"image":0,"video":0} --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3' ;;
  qwen38fn-4p89)
    # Qwen3.8-Flash-Next, local-inference-lab NVFP4-4p89 — the FIRST quant that satisfies the
    # screening rule written in the qwen38flashnext block: the PLE n-gram table is genuinely
    # quantized, not merely stored compressed. Evidence from its own config.json:
    #     ple_embedding_dtype = nvfp4        <- the table itself is 4-bit
    #     quant_algo = MIXED_PRECISION, and "*.ple.*" is NOT in the `ignore` list
    # Contrast the two that failed here: radixark/Inferact both put "*.ple.*" in `ignore`, so
    # vLLM expanded the table to BF16 (~95GB) and OOM'd the head node at ~116GB every time.
    # 98.66 GiB total on disk in 34 uniform shards (no separate model-plefp8-* files — the PLE
    # is quantized inline). At 4 bits the 51B-param table is ~25GB rather than ~95GB.
    # BUDGET ESTIMATE (UNMEASURED — re-measure and tighten on first successful load):
    #   head = ~37GB TP shard + ~25GB PLE + ~14GB torch/CUDA/Ray = ~76GB, vs the 121GB ceiling.
    # UTIL 0.45: the earlier 0.45->0.38 experiment proved util is NOT the binding constraint
    # (death point moved 0.1GB), so use the roomier value now that the real hog is gone.
    # Same three hard-won settings as the sibling keys: qwen4_exp-aware image, BF16 KV (QSA
    # rejects fp8 KV), and PLE offload forwarded into the container by run-ds4-tp2-cluster.sh.
    # RESULT 2026-08-28: the MEMORY problem is SOLVED by this quant — no OOM at any point, on
    # either topology. It now fails on KERNEL SHAPE constraints instead, in BOTH configurations:
    #   TP=2 (this key):  ValueError: Unsupported model: input feature size is not a multiple of 16
    #                     (NVFP4 group_size=16; splitting these dims over 2 ranks leaves a non-
    #                      multiple shard — this checkpoint simply cannot tensor-parallel by 2)
    #   TP=1 (single node, tested via run-single-node.sh at util 0.65, 98.66GiB on one Spark):
    #                     AssertionError: mm_mxfp8 requires N >= 128, got N=96.
    #                     out_features is too small for mm_mxfp8
    # i.e. the aggressive 4-bit PLE packing produced a layer with out_features=96 that this
    # build's MXFP8 kernel refuses. Single-node peaked at ~97GB/121GB with 14GB swap free, so
    # capacity was never the blocker there either — the arithmetic in this block was right.
    # KEEP the weights: both errors are vLLM-build constraints, not checkpoint corruption, so a
    # future image bump may fix either one. Re-test with:
    #   env -u DRYRUN ./serve-starfleet.sh qwen38fn-4p89 --port 9999          (TP=2)
    #   IMAGE=vllm-qwen38fn-ray:local MODEL_SUBDIR=Qwen3.8-Flash-Next-NVFP4-4p89 SERVED_NAME=x \
    #     NEED_MB=100000 GPU_UTIL=0.65 MAX_MODEL_LEN=16384 VLLM_PLE_CPU_OFFLOAD=1 PORT=9998 \
    #     ./run-single-node.sh                                                 (TP=1)
    export IMAGE="${QWEN38FN_IMAGE:-vllm-qwen38fn-ray:local}"
    export VLLM_PLE_CPU_OFFLOAD=1
    SUB=Qwen3.8-Flash-Next-NVFP4-4p89;         NEED=95000; UTIL=0.45; MML=16384
    FLAGS='--no-enable-flashinfer-autotune --enable-prefix-caching --limit-mm-per-prompt {"image":0,"video":0} --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3' ;;
  *) echo "serve-starfleet: unknown key '$KEY' (want gemma4-26b-ct|qwen3.5-122b|qwen3.5-122b-mtp3|nemotron-120b|qwen3-235b|minimaxawq|gemma4-31b-ct|qwen3vl32|qwen3vl32-vision|qwenvl235-vision|qwen38mtp3|qwen38flashnext|qwen38fn-radixark|qwen38fn-4p89)" >&2; exit 1;;
esac

# SPEED 2026-08-25: raise the scheduler cap for every TP=2 member. vLLM's default here was
# never tuned, and the measured win on qwen3.8 was 4.2x aggregate throughput at 32 concurrent
# with single-stream unchanged. Per-key FLAGS come FIRST in the argv, so a key that sets its
# own --max-num-seqs still wins (argparse takes the last occurrence).
FLAGS="$FLAGS --max-num-seqs ${MAXSEQS:-32} --max-num-batched-tokens ${BATCHTOK:-16384}"

# MODEL_FLAGS_EXTRA: opt-in hook to append flags without editing the per-key table. Unset changes
# nothing. Used to test cudagraph_mode=PIECEWISE, the mode vllm#40969 reports as the only stable
# graph mode on 2x DGX Spark GB10 TP=2 (the default FULL_AND_PIECEWISE hangs; we reproduced that
# twice today on a healthy GPU). Value must be SPACE-FREE inside JSON — the launcher word-splits.
FLAGS="$FLAGS ${MODEL_FLAGS_EXTRA:-}"

# CUDA graphs deliberately NOT enabled for TP=2 yet. Measured 2026-08-18 on qwen3.8: graphs
# under TP=2 gave 7.1 tok/s and killed the engine with "RPC call to sample_tokens timed out".
# That was on the THROTTLED GPU though, so it deserves a retest now the fault is fixed —
# set ENFORCE_EAGER=0 in the environment to try it, one member at a time.

# MML_OVERRIDE: benchmark a different context without changing the deployed value (see the same
# hook in serve-vllm-dflash.sh). Unset = each key's own MML above, unchanged.
MML="${MML_OVERRIDE:-$MML}"

# SERVED_NAME must match the member's useModelName in config.yaml.
export MODEL_SUBDIR="$SUB" SERVED_NAME="${KEY}-starfleet" TP=2 PP=1 USE_ROCE=1 \
       NEED_MB="$NEED" GPU_UTIL="$UTIL" MAX_MODEL_LEN="${MML:-32768}" MODEL_FLAGS="$FLAGS" PORT="$PORT"

# ctx= is the context this member starts with when no override is set; Fleet reads it to
# cap "Max" until the model's KV pool has been measured once (same field serve-sglang.sh
# and serve-nemcascade.sh print).
if [ "${DRYRUN:-0}" = 1 ]; then
  echo "key=$KEY sub=$SUB need=${NEED}MB util=$UTIL ctx=${MML:-32768} port=$PORT served=${KEY}-starfleet flags=[$FLAGS]"; exit 0
fi
exec "$LS/run-ds4-tp2-cluster.sh"
