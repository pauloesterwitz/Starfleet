#!/bin/bash
# serve-sglang.sh <key> --port <port>
# SGLang counterpart of serve-starfleet.sh: a thin KEY -> config table in front of
# run-sglang-tp2-cluster.sh. Same reason for existing as the vLLM one — the per-model flag
# strings contain SPACES, and llama-swap word-splits the cmd string, so the values have to live
# in a script where quoting is under our control rather than in config.yaml.
#
# This is the fleet's FIRST SGLang path. Everything else is vLLM. See config.yaml and
# run-sglang-tp2-cluster.sh for why this model specifically cannot use vLLM on two Sparks.
set -uo pipefail

LS=/home/pauloesterwitz/llama-swap
KEY="${1:?usage: serve-sglang.sh <key> --port <port>}"; shift
PORT=8100; while [ $# -gt 0 ]; do case "$1" in --port) PORT="${2:-8100}"; shift 2 || shift;; *) shift;; esac; done

case "$KEY" in
  qwen38fn)
    # Qwen3.8-Flash-Next, hn7305 NVFP4-Spark checkpoint (122 GiB; PLE shipped fp8 AND declared
    # quantized, which is what lets SGLang keep it fp8 instead of expanding to BF16).
    # MEASURED 2026-08-28: 37.4 tok/s single-stream, peaks JL 102GB / KA 97GB.
    #
    # IMAGE is the GB10-patched build, NOT stock: upstream refuses the fast QSA decode path on
    # anything that is not sm_100 and the Blackwell fallback kernel does not compile on sm_121.
    # See ~/vllm-ray-build/Dockerfile.sglang-gb10. Stock image = does not serve at all.
    #
    # --disable-flashinfer-autotune: the autotuner JIT-compiles the same broken cute kernel
    # during warmup. Harmless to skip; it only searches kernel configs.
    # --reasoning-parser qwen3-thinking: NOT plain qwen3. This model starts in thinking mode and
    # emits a closing </think> with no opening tag; the qwen3 parser then classifies nothing as
    # reasoning and the raw chain-of-thought lands in message.content. Same failure the
    # Qwen3-VL members hit (see the qwen3vl32 notes in serve-starfleet.sh).
    # MEM_FRACTION 0.75 leaves ~23GB/node after weights+KV at 16K context — measured, not guessed.
    #
    # GOTCHA — SMALL max_tokens RETURNS AN EMPTY content. This model thinks by default, and the
    # thinking tokens come out of the SAME max_tokens budget. MEASURED 2026-08-28 through
    # llama-swap: max_tokens=60 -> content='' with reasoning_tokens=60 (it never stopped thinking);
    # max_tokens=400 on the same prompt -> a correct answer, 29 reasoning + 52 content tokens.
    # So a blank reply here is almost always budget starvation, NOT the parser bug that the
    # Qwen3-VL members had (that one leaked think-text INTO content instead; fixed by using
    # qwen3-thinking above). Callers should allow a few hundred tokens, or send
    # chat_template_kwargs {"enable_thinking": false} to turn thinking off per request.
    # CORRECTION 2026-09-17, MEASURED: do NOT send enable_thinking=false. With the qwen3-thinking
    # parser the whole reply then lands in reasoning_content and `content` comes back EMPTY
    # (finish_reason=stop after 29-39 tokens, the complete answer sitting in reasoning_content).
    # Leave thinking on and allow a few hundred to ~2000 max_tokens; a vision answer took 174.
    #
    # VISION TOWER: CONFIGURED AND WORKING, verified 2026-09-17. config.json has
    # language_model_only=False, so qwen3_vl.py:1243 builds Qwen3VLMoeVisionModel, unquantized
    # (model.visual.* is in the quantization ignore list). A 320x240 probe (red rectangle, blue
    # circle, "47") added 82 image tokens over an identical text-only prompt and was described
    # exactly. Send images as OpenAI image_url content parts through llama-swap; no flags needed.
    # IMAGE SWITCHED 2026-09-07 to the UPSTREAM image. The custom sglang-qwen38fn-gb10:local build
    # (Dockerfile.sglang-gb10) is RETIRED -- see the ROOT CAUSE block below. That build patched
    # _resolve_trtllm_sparse_decode() to accept sm_121; upstream now ships a purpose-built GB10
    # kernel (qwen38_qsa_sm121_varlen) and explicitly refuses the path we were forcing, with the
    # comment: "Do not widen it to every SM12x device: it silently corrupts long-context decode on
    # SM121/GB10." Serving unpatched is both correct and slightly faster.
    IMAGE=lmsysorg/sglang:dev-qwen38-next-local
    SUB=Qwen3.8-Flash-Next-NVFP4-Spark
    SERVED=qwen38fn-sglang-tp2
    # MEMFRAC 0.75 -> 0.80, 2026-09-02, to make the ADVERTISED context actually usable.
    # The symptom: a 216,186-token request was rejected with "Input length exceeds the maximum
    # allowed length (199802 tokens)" while context_len said 253,952. context_len is not the
    # binding limit — max_total_num_tokens (the KV cache) is, and at 0.75 it only held 199,808.
    # MEASURED from this member's own log: 30.44 GB free after weights, 23.84 GB after KV+graphs,
    # i.e. the KV cache is ~6.6 GB for 199,808 tokens = ~34.6 KB/token. Reaching 253,952 tokens
    # therefore needs ~8.4 GB, only ~1.8 GB more. KV is this cheap because just 12 of the 48
    # layers are full attention; the rest are Gated-DeltaNet linear attention with constant state.
    # 0.80 buys ~6 GB of budget for a ~1.8 GB need — deliberate margin, still leaving ~18 GB free
    # on Jean-Luc (measured 22 GB at 0.75). Do NOT push toward 0.85: this node also hosts
    # ComfyUI/vane/MCP and the whole point of memcheck is that we never OOM.
    # VERIFY after any change: max_total_num_tokens in the run log must be >= the ctx override in
    # ~/.gb10/ctx/qwen38fn-sglang-tp2-starfleet, or the advertised context is a lie again.
    #
    # MEASURED OUTCOME at 0.80: max_total_num_tokens = 481,984 (was 199,808 at 0.75), i.e. +282,176
    # tokens for +6 GB of budget — far more than the ~1.8 GB the per-token estimate predicted,
    # because the extra budget lands almost entirely in KV once the weights are already resident.
    # Headroom after: Jean-Luc ~15 GB free, Kathryn ~24 GB (was 22/32 at 0.75).
    # NOTE this is now OVER-provisioned: max_position_embeddings is 262,144, so a single sequence
    # can never use more than that, and this member serves one request at a time. ~0.77 would
    # still clear 262,144 while returning ~4 GB of headroom to a node that also hosts ComfyUI,
    # vane and the MCP fleet. Left at 0.80 deliberately — it is measured and working — but if
    # Jean-Luc ever gets tight, trimming here is the cheapest place to find memory.
    NEED=85000; MEMFRAC=0.80; CTX=16384
    #
    # ── MTP, MEASURED 2026-08-29. +35% single-stream. This is the whole tuning story. ─────────
    #   config                 single   agg@8   acceptance
    #   baseline (no MTP)       36.5    115.5      -
    #   no CUDA graphs          14.9     58.8      -     <- graphs are worth 2.4x, keep them ON
    #   --ep-size 2             38.1    115.0      -     <- helps ALONE (+4%)...
    #   MTP K=3                 49.4    101.3    1.75    <- WINNER (this config)
    #   MTP K=3 + --ep-size 2   35.8    105.2    1.61    <- ...but EP+MTP together is WORSE than
    #                                                       MTP alone. Do not stack them: the
    #                                                       expert all-to-all disrupts the
    #                                                       cross-node draft/verify cycle.
    #   MTP K=1                 45.5    115.6    1.43    <- keeps FULL aggregate throughput
    #   MTP K=2                 47.7    104.6    1.56
    #   MTP K=4                 FAIL — "Qwen QSA requires speculative_num_draft_tokens <= the QSA
    #                           compress ratio (4); got 5". indexer_compress_ratio=4 in config.json
    #                           makes K=3 (4 draft tokens) the ARCHITECTURAL CEILING. Do not retry K>3.
    # Acceptance rises monotonically with K (1.43/1.56/1.75) and tok/s follows, so K=3 is the peak.
    # NOTE this model does NOT behave like qwen3.8-27b, where K=2 beat K=3 — hence the sweep.
    # TRADE-OFF: MTP costs ~12% aggregate throughput (115.5 -> 101.3). K=3 is right for
    # interactive/agent use (one request at a time). If this member is ever put under real
    # concurrent load, switch to K=1: 45.5 single AND 115.6 agg, i.e. +25% latency for free.
    # Re-run with: MODEL_FLAGS_EXTRA=... ~/llama-swap/bench-sglang-mtp-k.sh
    #
    # ── RE-MEASURED AT mem-fraction 0.80 (2026-09-03). The table above was all taken at 0.75. ──
    #   config          0.75 single -> 0.80 single    note
    #   baseline            36.5   ->   38.2          the higher fraction COSTS NOTHING; it helps
    #   no CUDA graphs      14.9   ->   15.0          graphs still worth 2.5x. Keep them on.
    #   --ep-size 2         38.1   ->   38.4          now only +0.2 over baseline = NOISE.
    #   MTP K=3 (this)      49.4   ->   45.5          agg@8 101.3 -> 107.7, acceptance 1.75 -> 2.04
    # READ THE MTP ROW CAREFULLY: single-stream dropped 3.9 but aggregate ROSE 6.4 and acceptance
    # improved. The bigger KV cache costs a little per-step latency while drafting better.
    # MTP still wins decisively (45.5 vs 38.2 baseline) but the margin is +19%, not the +35% the
    # table above records at 0.75. Quote 45.5, not 49.4, for the config actually deployed.
    # EP IS NOW POINTLESS EITHER WAY: +0.2 alone (noise) and already known-harmful with MTP.
    # ── GOTCHA: A WALL OF "!" MEANS NaN LOGITS, NOT A CONFUSED MODEL. (2026-09-07) ────────────
    # Symptom: a long OpenCode session suddenly emits nothing but "!!!!!!!!" until max_tokens.
    # VERIFIED mechanism, read out of this checkpoint and the image, not guessed:
    #   1. token id 0 in this tokenizer is literally "!" (vocab.json: {"!": 0}).
    #   2. the logit row goes NaN, and EVERY comparison against NaN is false, so the sampler's
    #      running max never updates and argmax returns index 0 -> "!". CORRECTED 2026-09-07:
    #      this is NOT sanitize_nan_logits() doing it. That helper would map NaN to -1e30 (which
    #      also argmaxes to 0), but environ.py has SGLANG_SANITIZE_NAN_LOGITS = EnvBool(False),
    #      so it returns early and never runs. Consequence for the fix: setting that env var
    #      does NOT stop the "!" — it only adds a throttled NaN warning to the log. Useful as a
    #      DIAGNOSTIC (it proves NaN and names the tensor), useless as a cure.
    # Either way the model is emitting numerical garbage silently instead of erroring.
    #
    # MEASURED 2026-09-07: the collapse is INTERMITTENT and happens MID-GENERATION, not after a
    # long context. Reproduced on a 62-token prompt: coherent reasoning for hundreds of tokens,
    # then a run of "!" to the max_tokens cap. An identical rerun produced 3260 clean chars and
    # zero "!". So it is a per-generation numerical edge case; any test needs a RATE over N
    # samples, never a single observation.
    # PRIME SUSPECT is MTP x the Qwen sparse-attention (QSA) path, which are coupled here:
    # qwen_sparse_attn_backend.py should_reuse_mtp_sparse_indices() returns True for every decode
    # step once seeded — "Draft decode steps reuse the draft-extend selection" — i.e. the sparse
    # token selection is computed at draft-extend and REUSED as the sequence keeps growing.
    # Second suspect is our own sm_121 patch: upstream gates this kernel on sm_100 and we relaxed
    # it (Dockerfile.sglang-gb10). Both only engage on LONG sequences, which matches "after a while".
    # TO DIAGNOSE: drop the four --speculative-* flags below. If the "!" stops, it is the MTP/QSA
    # interaction and the cost of avoiding it is the ~19% MTP gain. Not yet tested — the pool was
    # held by ds4 when this was written.
    # Do not enable it; the mtp-k3-ep2 arm was not re-measured at 0.80 because both of its
    # inputs are settled — it failed on benchmark-harness lock contention, not on merit.

    #
    # ── WHAT IT ACTUALLY DOES IN PRODUCTION (2026-09-02, 248 decode samples) ──────────────────
    # The table above is a 16K-context bench with synthetic prompts. A live session at ~254K
    # context (via ctxproxy @253952, ~170K tokens resident) measured from this member's own run
    # log (logs/sglang-Qwen3.8-Flash-Next-NVFP4-Spark.log):
    #   throughput  p25 32.0   median 37.9   p75 50.2   max 75.6  tok/s
    #   acceptance  p25 1.90   median 2.38   p75 2.98   max 4.00
    # Read that carefully before quoting 49.4 at anyone: 49.4 is the SHORT-CONTEXT best case and
    # the p75 here reproduces it. The median is lower because of KV pressure at 170K tokens, NOT
    # because drafting got worse — acceptance in real use (2.38) is markedly BETTER than in the
    # bench (1.75), i.e. real prose is more predictable than the bench prompts, and occasionally
    # hits the 4.00 ceiling outright. Long context costs more than speculation gains.
    # MTP DISABLED 2026-09-07 — IT CORRUPTS OUTPUT. Do not re-enable without re-running the
    # collapse test (~/llama-swap/... ratetest.py, 10 samples, reasoning-heavy prompts, 1200 tok).
    #   MTP ON : 6/10 requests degenerated into a run of "!" (token id 0), median 26.2 tok/s
    #   MTP OFF: 0/10 collapses,                                          median 36.9 tok/s
    # Turning MTP OFF is FASTER in practice as well as correct: the +35%/49.4 tok/s advantage in
    # the table above is real ONLY for short benchmark prompts. On long reasoning generations the
    # speculation dies mid-stream (the server log shows "accept rate: 0.00" and it never recovers),
    # so MTP pays the draft cost and accepts nothing. Clean-run speed is a tie (37.2 on vs 36.9
    # off) — the entire measured MTP win evaporates on real agent workloads.
    # It also halved the runaway-reasoning cases (never-answered 5/10 -> 2/10).
    # K=1 TESTED TOO, 2026-09-07 08:54 — ALSO CORRUPT. Do not "just lower K" to get the speed back:
    #   K=3  60% collapse (6/10)   26.2 tok/s
    #   K=1  40% collapse (8/20)   45.4 tok/s   <- FASTEST measured, and still unusable
    #   off   0% collapse (0/30)   38.2 tok/s   <- deployed
    # The rate scales monotonically with draft depth (60/40/0), which is what condemns speculation
    # itself rather than any single K. K=1 is +19% and hands you garbage in 2 of every 5 replies.
    # MTP on this model/hardware is a dead end until the upstream MTP x sparse-attention path (or
    # our sm_121 gate patch) is fixed; re-test with ratetest.py, n>=20, before ever re-enabling.
    #
    # ── EXHAUSTIVE SWEEP 2026-09-07 12:40-14:20. EVERY tunable lever tried. ALL CORRUPT. ──────
    #   config                              collapse        tok/s
    #   MTP K=3                             60%  (6/10)     26.2
    #   MTP K=1                             40%  (8/20)     45.4
    #   K=1 + draft-attention-backend triton 20% (4/20)     47.4   <- n too small to be real (p=0.30)
    #   K=1 + use-rejection-sampling        27%  (16/60)    47.6   <- looked like 5% at n=20. IT WAS NOISE.
    #   K=1 + rejection-sampling + triton   50%  (20/40)    30.4   <- worst of both, and slowest
    #   MTP OFF (deployed)                   0%  (0/30)     38.2   <- the only correct config
    # ~150 speculative samples, ~54 collapses. Zero in 30 without speculation.
    # LESSON THAT COST AN HOUR: at n=20 a 5% reading passed a significance test (p=0.0197) and looked
    # like a fix. n=40 put the SAME config at 38%. Never accept a low collapse count below n=40 --
    # this failure mode is bursty, so small samples swing wildly and flatter whatever you just tried.
    # Levers NOT worth trying next: --speculative-eagle-topk and --speculative-accept-threshold-*
    # both raise ACCEPTANCE, and acceptance is not the problem -- K=3 had the best acceptance (2.04)
    # and the worst corruption (60%). The defect is in the speculative path itself, not its tuning.
    #
    # ── ROOT CAUSE FOUND 2026-09-07 (web research + upstream issues). ARCHITECTURAL, NOT TUNABLE. ──
    # This model is HYBRID: config text_config.layer_types shows 3 of every 4 layers are
    # "linear_attention" (Gated DeltaNet), only every 4th is full attention. GDN layers carry a
    # RECURRENT conv/SSM state -- our own server log prints it as "mamba num: 4, mamba usage".
    # Speculative decoding runs the target over ALL N draft tokens, which advances that recurrent
    # state N steps, but then only k <= N tokens are accepted. The state must be rewound to step k.
    # IT CANNOT BE. Quoting sgl-project/sglang#25587: "Conv1d with SiLU activation at each step is
    # non-linear and non-invertible. The correct K-1 history after accepting k tokens is the window
    # state that existed after step k and before step k+1 -- this intermediate state is not
    # preserved anywhere." Every PARTIAL acceptance therefore injects drift, and the drift
    # ACCUMULATES across decoding until the logits blow up to NaN and argmax falls to token 0 ("!").
    # This explains every observation we made, which no tuning hypothesis did:
    #   - collapse happens MID-generation after hundreds of tokens (drift needs to accumulate)
    #   - it is intermittent (drift must cross a threshold)
    #   - it scales with draft VOLUME, not acceptance quality (more partial accepts = more drift)
    #   - rejection sampling did not help (it changes WHICH tokens are accepted, not the rewind)
    #   - a different draft attention backend did not help (the bug is in conv state, not attention)
    # THIS BUILD IS AFFECTED: sglang 0.0.0.dev1+gd91c3682b has NO conv-state snapshot/restore --
    # grep for rollback in srt/ returns only grammar and cache rollbacks, nothing for mamba state.
    # Independent confirmation of the exact symptom: vllm-project/vllm#47087 "Native MTP speculative
    # decoding degenerates into garbage token loops on deep agentic conversations (Qwen3-MoE)" --
    # upstream has no root cause there either, and its only workaround is the same as ours: turn
    # MTP off. Also sgl-project/sglang#18102 (Qwen3-Next tool calls break under spec decoding).
    # THE UPSTREAM FIX (sglang#25587) is snapshot-based: save the K-1 conv window after each draft
    # token, then scatter the snapshot at the accepted-token count back into conv_states. Until an
    # image ships that, MTP on this model is unsafe at ANY setting. Re-check after every image
    # upgrade with ~/llama-swap/check-mtp-lossless.sh (temp-0 equivalence, far more sensitive than
    # collapse-rate sampling: a single diverging token proves it, no n=40 needed).
    # Root cause not isolated further; prime suspects remain the MTP x Qwen-sparse-attention index
    # reuse (should_reuse_mtp_sparse_indices) and our sm_121 kernel-gate patch. Both only matter
    # while speculation is on, so disabling it sidesteps them.
    # MTP RE-ENABLED 2026-09-07 on the upstream image. It was disabled earlier today because it
    # collapsed 40-60% of replies into "!" -- but that was OUR PATCHED IMAGE. On the upstream
    # kernel, MEASURED: 0 collapses in 60 samples, 0 bad long-context stages (2k->100k), 49.2 tok/s
    # (vs 37-38 with MTP off). K=1 not K=3: K=3 was never re-tested on this image, and K=1 already
    # carries the win. Raise K only after a fresh n>=40 ratetest.py run.
    # CAVEAT, stated honestly: losslessness is NOT PROVEN. check-mtp-lossless.sh returns
    # INCONCLUSIVE here -- two identical MTP-off launches already diverge (similarity 0.18-0.24),
    # because MoE routing + cross-node all-reduce make greedy decode non-reproducible on TP=2.
    # The case for MTP rests on the 0/60 collapse rate, not on a correctness proof.
    # ORPHAN REAPER ADDED 2026-09-08: reap-orphan-sglang.sh + a systemd user timer every 5 min.
    # An sglang cluster was found holding 107GB for TWO HOURS with its launcher PID dead, port
    # dead and llama-swap idle — llama-swap SIGKILLs the wrapper on TTL when it does not exit
    # fast enough, so the EXIT trap never runs and both nodes leak containers. memcheck's reaper
    # only fires when a load is ATTEMPTED, so with no traffic nothing cleaned it up.
    # Check it with: systemctl --user list-timers reap-orphan-sglang.timer
    # SOAK-VALIDATED 2026-09-07 22:20 on the deployed production path:
    #   long context 2k/20k/60k/120k  -> 0 bad stages (needle retrieved at every size)
    #   60 consecutive requests       -> 0 collapses, 48.8 tok/s, no speed decay
    #   long context RE-RUN AFTER     -> 0 bad stages   <- the drift check that matters most:
    #     the original failure was ACCUMULATED state drift, so a post-soak long-context pass is
    #     where a degrading recurrent state would surface. It did not.
    # Cumulative on this image with MTP: 0 collapses in 140 samples, tool calls 3/3.
    #
    # ── K SWEEP ON THE FIXED IMAGE, 2026-09-08. K=1 CONFIRMED OPTIMAL. DO NOT RE-RUN. ──────────
    #   K=1  0/40 collapse   48.8 tok/s   (replicate: 48.8 — reproducible to 0.1)
    #   K=2  0/40 collapse   50.4 tok/s   (replicate: 48.7 — the 50.4 was NOISE)
    #   K=3  0/40 collapse   47.4 tok/s
    # Measured back-to-back under identical conditions, K=1 and K=2 are a dead heat (48.8 vs 48.7)
    # and K=3 is slower. K=1 stays. The first K=2 reading looked like a 3% win and did not survive
    # a replicate — same trap as the rejection-sampling "fix" the day before. Always replicate a
    # favourable single measurement before acting on it.
    # THE REAL FINDING HERE: every K is CLEAN on this image (0 collapses at K=1, K=2 and K=3, all
    # passing long context to 120k). On the old patched image the same sweep gave 40-60% corruption.
    # That is the final proof that the sm_121 kernel patch was the defect, not speculation.
    # THROUGHPUT, MEASURED PROPERLY 2026-09-10: 48.7 tok/s SUSTAINED, not the mid-40s previously
    # reported. Three back-to-back rate tests on ONE load gave 46.1 / 48.7 / 48.7 -- the first
    # measurement after a load reads ~2.6 low and the warm figure replicates to 0.1. Every earlier
    # number in this file taken as a single post-load run (45.3, 46.9, 47.3, 49.9) was therefore a
    # COLD reading. Quote 48.7 for sustained use; expect ~46 on the first requests after a load.
    # WIDER SAMPLE 2026-09-11: warm readings 48.5 / 48.6 / 48.7 / 48.7 / 49.8 / 50.4. A single
    # 44.6 outlier on 2026-09-11 did NOT replicate (three runs on one load gave 50.4/48.5/48.6)
    # -- treat anything in 48-50 as normal and only investigate if THREE consecutive warm runs
    # land below 47. Single readings here swing +-2 tok/s and have twice sent me chasing ghosts.
    #
    # OPERATIONAL GOTCHA: llama-swap WATCHES config.yaml and hot-reloads on any write. Editing
    # it while a cluster model is loading ABORTS that load (MEASURED 2026-09-11 13:47: the
    # reload fired 1s after a write and killed the in-flight load, leaving an orphan container).
    # Only write config.yaml when the fleet is idle. This file is not watched, so notes go here.
    # IF "!" RUNS EVER REAPPEAR: drop the four --speculative-* flags first, then re-read this block.
    FLAGS="--moe-runner-backend flashinfer_cutlass --disable-flashinfer-autotune --reasoning-parser qwen3-thinking --tool-call-parser qwen3_coder --speculative-algorithm NEXTN --speculative-num-steps 1 --speculative-eagle-topk 1 --speculative-num-draft-tokens 2" ;;
  qwen38fn-long)
    # Qwen3.8-Flash-Next at 512k, via a YaRN rope override. SIBLING of `qwen38fn` above, which
    # stays at the native 262,144 with MTP K=1 -- this key changes nothing for that member.
    # Precedent: glm53awq / glm53awq-long are the same split.
    #
    # WHY A ROPE OVERRIDE. max_position_embeddings is 262144 and rope_parameters ships
    # rope_type "default", so 262144 is a hard wall, not a memory limit: the KV pool at
    # mem-fraction 0.80 already holds ~690k tokens. YaRN factor 2.0 doubles the window and
    # costs NO extra memory. Qwen's own recipe (model card, "Processing Long Texts") is
    # factor 4.0 -> 1M; 2.0 is chosen because Qwen advises sizing the factor to real need,
    # and because static YaRN rescales short prompts too.
    #
    # EVERY KEY OF rope_parameters IS RESTATED ON PURPOSE. --json-model-override-args REPLACES
    # the dict, it does not merge into it. Drop mrope_section and the model silently loses
    # mRoPE while the server still reports healthy -- vision breaks, text degrades, no error.
    # max_position_embeddings must be raised in the SAME override: get_context_length() reads
    # `rope_scaling` (absent on this model), so without it --context-length 524288 hard-fails
    # with a ValueError. Do NOT paper over that with SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN.
    # The JSON MUST stay compact: FLAGS is word-split, a single space breaks it into two args.
    # The nested-text_config override is a no-op on many sglang builds (sgl-project#27974);
    # it is FIXED in dev-qwen38-next-local (config.py merges via PretrainedConfig.update).
    # The MTP head inherits it automatically (qwen4_exp_mtp.py does config = config.text_config).
    #
    # MEASURED 2026-09-20 at 479,655 prompt tokens (1.83x the native window), needle at three
    # depths, 3/3 exact recall. MTP K=1 vs K=3 at that size:
    #   K=1  decode 49.6-51.1 tok/s   prefill 1415-2199   KV pool 692,224
    #   K=3  decode 66.2     tok/s    prefill 2208.8      KV pool 675,776   <- +31%, this config
    # Decode does NOT fall off with context (short-context K=1 was 45.5-48.9).
    # Short prompts (EN/DE/code) were diffed against the unscaled member: no visible tax.
    #
    # STABILITY, MEASURED. K=3 is the config that collapsed 60% of the time on the OLD patched
    # image (K=1 40%, MTP off 0%) -- the recurrent conv/SSM state cannot be rewound on a partial
    # acceptance (sgl-project/sglang#25587), which is why that was called architectural.
    # On THIS image (dev-qwen38-next-local, unpatched) it is clean:
    #   ratetest-glm53.py -n 40 against this member, 2026-09-20: 0/40 collapsed, max repeated-char
    #   run 0, min unique-word ratio 0.38, every sample run to the full 1200-token budget.
    # Results: ~/llama-swap/.yarn/ratetest-k3.json. The 262k member's K=1 reference was 0/80, so
    # this arm is half that sample size -- re-run with -n 40 again if you want parity.
    # K=3 is also the architectural ceiling (indexer_compress_ratio 4 => num_draft_tokens <= 4).
    # IF "!" RUNS OR GARBAGE APPEAR: set --speculative-num-steps 1 and
    # --speculative-num-draft-tokens 2 below (that is exactly the proven K=1 config), or run
    # ~/llama-swap/bench-sglang-mtp-k.sh for a proper collapse rate first.
    IMAGE=lmsysorg/sglang:dev-qwen38-next-local
    SUB=Qwen3.8-Flash-Next-NVFP4-Spark
    SERVED=qwen38fn-long-sglang-tp2
    NEED=85000; MEMFRAC=0.80; CTX=${CTX_LEN:-524288}
    # ── THE ROPE FACTOR FOLLOWS THE REQUESTED CONTEXT. Do not hard-code it. ──────────────────
    # fleet-ui / ctxproxy let a context be picked per load (~/.gb10/ctx/<member>), so a fixed
    # factor is wrong at every size but one: at 262144 it would apply YaRN to a window that is
    # already trained (Qwen warns static YaRN costs short-prompt quality), and at 393216 it
    # would scale for 512k. factor = CTX / 262144, and BELOW the native window no override is
    # emitted at all -- the model then runs exactly as the 262k member does.
    QNATIVE=262144
    MAXTOK=$(( CTX + 32768 )); [ "$MAXTOK" -gt 758144 ] && MAXTOK=758144   # measured pool ceiling at 0.80
    if [ "$CTX" -gt "$QNATIVE" ]; then
      QFACTOR=$(python3 -c "print(f'{$CTX/$QNATIVE:.4f}')")
      QROPE="--json-model-override-args {\"text_config\":{\"max_position_embeddings\":${CTX},\"rope_parameters\":{\"rope_type\":\"yarn\",\"factor\":${QFACTOR},\"original_max_position_embeddings\":${QNATIVE},\"rope_theta\":10000000,\"mrope_section\":[11,11,10],\"mrope_interleaved\":true,\"partial_rotary_factor\":0.25}}}"
      echo "serve-sglang[qwen38fn-long]: ctx $CTX > native $QNATIVE -> YaRN factor $QFACTOR, pool $MAXTOK" >&2
    else
      QROPE=""
      echo "serve-sglang[qwen38fn-long]: ctx $CTX <= native $QNATIVE -> no rope override, pool $MAXTOK" >&2
    fi
    # ── SPEED, SWEPT AT THIS CONFIG 2026-09-20 (6 arms, ctx 524288, .yarn/sweep-512k.log) ──────
    # The 2026-08-29 K table does NOT transfer: it was measured at ctx 16384, where K=3 won at
    # 49.4 tok/s. At 512k every arm is roughly half that, and the ordering changes.
    #   arm                  single   agg@8   acceptance
    #   k3-asis               19.1     57.3     1.97     <- unbounded, what was wired first
    #   k3-bounded            20.9     30.4     2.33
    #   k2-bounded            19.3     29.1     1.80
    #   k1-bounded            21.4     30.0     1.52     <- WINNER, this config
    #   nomtp-bounded         17.9     28.5     -        <- MTP is only worth +20% here, not +35%
    #   k3-bounded-nograph    10.6     15.6     1.70     <- CUDA GRAPHS ARE WORTH 2x. NEVER DISABLE.
    # agg@8 looks like a regression on the bounded arms and is not one: --max-running-requests 2
    # caps concurrency, and this member cannot serve 8 long contexts anyway -- the pool holds
    # ~557k tokens, about ONE 480k sequence. Single-stream is the number that matters here.
    #
    # BUT THE single COLUMN ABOVE DOES NOT SURVIVE A LONGER MEASUREMENT, so do not quote it as a
    # speed win. That harness generates 192 tokens and divides by WALL TIME INCLUDING PREFILL, so
    # it is sensitive to per-request overhead, not to decode rate. Measured again with
    # .yarn/decode-bench.py (1500 tokens, temp 0, timed first-token-to-last):
    #     k3-asis    23.6 tok/s   (24.5 / 23.6 / 23.2)
    #     k1-bounded 23.7 tok/s   (22.9 / 23.8 / 23.7)
    # i.e. STEADY-STATE DECODE IS UNCHANGED. What bounding actually buys is resources, verified
    # on the reload: free memory after graph capture 18.78 -> 25.74 GB (~7 GB back), verify-graph
    # capture 129.3 s / 2.91 GB -> 94.6 s / 1.35 GB, captured bs list [1..17] -> [1,2], pool
    # 758,144 -> 557,056 tokens. K=1 is kept over K=3 because it ties on speed, frees that
    # memory, and is the better-validated arm (0/80 vs 0/40 collapses).
    # K=1 also happens to be the collapse-proven setting (0/80 on the 262k member; K=3 measured
    # 0/40 here 2026-09-20, .yarn/ratetest-k3.json), so the fast arm is also the safe one.
    #
    # THE BOUNDING FLAGS ARE DELIBERATE, same idiom as glm53awq-long:
    #   --max-total-tokens 557056 : the default built a 758,144-token pool for a member whose
    #       window is 524,288. The surplus is dead memory.
    #   --cuda-graph-max-bs 4 : the default captured graphs for bs up to 17 (129 s of capture
    #       for the verify graph alone) on a member that serves one request at a time.
    #   --max-running-requests 2 : concurrency this member can actually honour at 512k.
    FLAGS="--moe-runner-backend flashinfer_cutlass --disable-flashinfer-autotune --reasoning-parser qwen3-thinking --tool-call-parser qwen3_coder --speculative-algorithm NEXTN --speculative-num-steps 1 --speculative-eagle-topk 1 --speculative-num-draft-tokens 2 --max-running-requests 2 --cuda-graph-max-bs 4 --max-total-tokens ${MAXTOK} ${QROPE}" ;;
  glm53flash)
    # GLM-5.3-Flash (zai-org) NVFP4-A16 by LibertAI: 320B total / 18B active MoE, natively
    # multimodal, 181.3 GiB. TP=2-FORCED — ~90.5 GiB of weights per Spark, the largest member
    # on this fleet (hy3 is 168 GiB). The first GLM to combine SPARSE (11 DeepSeek-sparse
    # layers) and LINEAR (34 KDA) attention, which is what drives every flag below.
    # Weight-only NVFP4: only the routed-expert FFN is quantized; attention, vision tower,
    # shared experts, routers, MTP head, embeddings and lm_head all stay BF16.
    #
    # glm5_next is NOT in sglang main (sgl-project/sglang#36507) — support ships only in the
    # per-model image tag. dev-cu13 and qwen38flashnext both lack it.
    #
    # EVERY FLAG IS LOAD-BEARING ON sm_121 (from the vendor card, which verified this exact
    # config on 2x GB10). Do not trim them:
    #   --disable-shared-experts-fusion : the shared expert is BF16 here (only ROUTED experts
    #       are quantized); fusion would pack it into the NVFP4 buffer and the load fails on a
    #       shape mismatch.
    #   --dsa-{prefill,decode}-backend tilelang : the only DSA backend with a NoPE
    #       (tail_dim == 0) kernel. flashinfer_sparse_mla hardcodes a rope-bearing 448+64 page
    #       layout; all four flashmla_* are excluded by index_kpool=4.
    #   --kv-cache-dtype bfloat16 : TileLang on CUDA is bf16-KV-only, and merely OMITTING an
    #       fp8 flag is not enough — the DSA default re-selects fp8.
    #   --moe-runner-backend flashinfer_cutlass : the NVFP4 MoE runner auto-picks a datacenter-
    #       Blackwell backend on (12,1). marlin also works but repacks at ~19 GiB/rank, which
    #       does not fit at TP=2 here.
    #   --reasoning-parser glm45 : this model opens its reasoning block from the CHAT TEMPLATE
    #       and emits only a closing </think>. Without the parser the whole trace lands in
    #       message.content ending in a bare </think> and reasoning_content stays null — the
    #       same shape the Qwen3-VL and qwen38fn members hit. deepseek-r1 also splits it, but
    #       glm45 additionally excludes <tool_call>/</tool_call>/<eop>/<|user|> from the
    #       reasoning span, which matters once tools are on. (On vLLM the choice INVERTS: there
    #       glm45 is an alias for the GLM-4.7-MoE engine and discards the reply entirely.)
    #   --tool-call-parser glm47 : NOT `glm`. glm is the GLM-4.5 format and FAILS SILENTLY on
    #       GLM-5.x — finish_reason "stop", tool_calls null, content "" (the parser matched
    #       enough to swallow the call but not enough to emit it). An empty content next to
    #       tool_calls:null is the tell.
    #
    # SIZING — the KDA state cache, NOT the KV cache, is the constraint. Each of the 34 KDA
    # linear-attention layers needs a PER-REQUEST recurrent state, so context length and
    # concurrency compete directly. Vendor-measured on 2x GB10 at mem-fraction 0.84:
    #   max-running-requests 8 @ ctx 131072 -> FAILS ("mamba cache too small ... max_num_reqs=0")
    #   max-running-requests 2 @ ctx  65536 -> OK: KV 212,864 tok (2.52 GB), mamba 31 slots (2.19 GB)
    # If that error appears, LOWER --max-running-requests before lowering context: the state
    # cache is per-request, so concurrency is the cheaper thing to give up. Practical
    # consequence: this is a ONE-USER-AT-A-TIME model despite the 1M-context spec.
    #
    # NEED 103000 is the largest gate on the fleet bar the 397B member. Jean-Luc has been
    # measured at only 97.7 GB free while hosting ComfyUI/vane/MCP — expect to stop ComfyUI
    # before this will launch. That is memcheck refusing cleanly, not a fault.
    #
    # MEASURED 2026-08-29: 14.6 tok/s single-stream (14.4/14.6/14.6 across three prompts —
    # unusually tight). Cold load 480s. LibertAI published no throughput figure, so this is the
    # first number for this checkpoint on any hardware. Aggregate @8 in flight was ALSO 14.4,
    # i.e. concurrency buys exactly nothing: --max-running-requests 2 plus a 9-slot mamba cache
    # serialises everything. Treat this as a strictly one-request-at-a-time model.
    #
    # MTP IS NOT AVAILABLE HERE — DO NOT ADD --speculative-algorithm NEXTN. It fails at WEIGHT
    # LOADING, identically for K=1/2/3 (K never matters; nothing is ever drafted):
    #   glm5_next_nextn.py:84 load_weights -> fused_moe_triton/layer.py:703 _load_w13
    #   RuntimeError: The size of tensor a (4096) must match the size of tensor b (2048) at
    #   non-singleton dimension 1
    # 4096 = 2 x 2048 is the signature of a BF16 tensor going into an NVFP4-sized slot (NVFP4
    # packs two values per byte). This checkpoint deliberately keeps the MTP head in BF16, but
    # SGLang's NextN module sizes its MoE params as NVFP4. Reproduced on BOTH the stock and the
    # patched image. The vendor card's "speculative decoding works" line is in its vLLM section;
    # its verified SGLang recipe carries no speculative flags. Re-test only after upstream
    # sgl-project/sglang#36507 lands, not by re-adding the flags.
    #
    # CUDA graphs are ON (the default) and working. Not swept: the two available levers here are
    # MTP, which is broken, and graphs, which are already at their best setting.
    #
    # PREFILL IS THE REAL BOTTLENECK, not decode. MEASURED: 4020 prompt tokens in 41s = 98 tok/s.
    # Extrapolated to the configured 65536 ctx that is ~11 MINUTES before the first output token.
    # The card's 1M context is a theoretical ceiling on this hardware, not a usable one. Plan long
    # -document work around prefill, not around the 14 tok/s decode.
    #
    # GOTCHA — SMALL max_tokens RETURNS EMPTY content, same class as the qwen38fn member above.
    # This model thinks hard by default and the trace comes out of the SAME budget. MEASURED
    # through llama-swap: max_tokens=300 -> finish_reason 'length', all 300 tokens reasoning,
    # content ''. max_tokens=900 on the identical prompt -> finish 'stop', 609 tokens, correct
    # answer, and 2491 CHARACTERS of reasoning for a one-sentence question. Callers should allow
    # ~1000 tokens, or send chat_template_kwargs {"enable_thinking": false} per request. A blank
    # reply here is budget starvation, NOT the glm-vs-glm47 tool-parser bug.
    #
    # IMAGE is the GB10-patched build, NOT stock — CONFIRMED REQUIRED 2026-08-29, exactly as the
    # vendor card warned. The stock tag loads the weights and runs DSA prefill, then dies at
    # DECODE warmup with
    #   tvm.error.InternalError: Failed to set the allowed dynamic shared memory size to 169984
    # because this model is NoPE (tail_dim==0) and so takes sparse_attention_fwd_kernel_v1,
    # whose stock tile (block_I=64, num_stages=2, threads=256) asks for 169,984 B of dynamic
    # shared memory against GB10's 101,376 B ceiling. Not flag-fixable: the tile is baked into
    # the kernel factory's defaults. ~/vllm-ray-build/Dockerfile.sglang-glm53-gb10 passes the
    # vendor's GB10 tile (block_I=32, num_stages=1, threads=128) at the call site, guarded on
    # sm_12x so datacenter Blackwell keeps the faster stock tile. Built on BOTH Sparks.
    IMAGE=sglang-glm53-gb10:local
    SUB=GLM-5.3-Flash-NVFP4
    SERVED=glm53-flash-sglang-tp2
    NEED=103000; MEMFRAC=0.84; CTX=65536
    FLAGS="--attention-backend dsa --dsa-prefill-backend tilelang --dsa-decode-backend tilelang --moe-runner-backend flashinfer_cutlass --kv-cache-dtype bfloat16 --disable-shared-experts-fusion --reasoning-parser glm45 --tool-call-parser glm47 --max-running-requests 2" ;;
  glm53awq)
    # GLM-5.3-Flash AWQ-W4A16 with KDA attention at INT8 (JJ48-24, "experimental" in its own name).
    # 172 GiB vs LibertAI's 181.3, and the difference is exactly where it matters for DECODE.
    #
    # WHY THIS CHECKPOINT AT ALL. Measured per-token read on the NVFP4 build is 20.33 GiB, 78% of
    # it BF16 tensors, and 9.17 GiB of that is KDA attention I could NOT quantize myself: NVFP4
    # carries a per-tensor scalar weight_scale_2, and q/k/v_proj merge into QKVParallelLinear,
    # where two scalars cannot become one param (that is what killed the s1 checkpoint,
    # linear.py:759). AWQ/INT8 use GROUP-WISE scale tensors which have an output dim and merge
    # correctly - so the blocker was NVFP4-specific, not architectural.
    #   KDA q,k,v,b,f_a,g_a,o_proj  9.17 GiB BF16 -> 4.59 INT8
    #   shared experts + dense MLP  2.96 GiB BF16 -> 0.76 INT4
    #   DSA attention, lm_head, router, embeddings, indexer, mHC: still BF16
    # => per-token read ~12.9 GiB vs 20.33. PREDICTED ~19.3 tok/s against a 14.5 baseline.
    #
    # SGLANG, NOT vLLM, despite the checkpoint being vLLM-authored and benchmarked on A100s.
    # On GB10 specifically vLLM needs Libertai's hand-written sparse-MLA CUDA kernel (no vLLM MLA
    # backend accepts this model's NoPE dims on sm_121: the sparse decode path asserts pe_dim==64
    # and qk_rope_head_dim is 0) plus the two patches the model card lists. That is a CUDA port
    # before the first token. Our SGLang path already serves this architecture on GB10.
    # VERIFIED IN-IMAGE before committing to the download: compressed_tensors wNa16 supports
    # num_bits [4, 8] (uint4b8 / uint8b128) and channel-wise strategy, which is exactly the
    # checkpoint's mixed W4A16-group128 + W8A16-channel layout.
    #
    # IMAGE is still the GB10-patched one: the TileLang shared-memory ceiling is a property of
    # the hardware and the NoPE kernel, not of the quantization, so it applies here too.
    #
    # moe-runner-backend is deliberately AUTO, not flashinfer_cutlass. That backend is the NVFP4
    # MoE path; this checkpoint's experts are INT4 wNa16 and need the matching runner.
    # --disable-shared-experts-fusion IS REQUIRED. TESTED 2026-09-10 and it is NOT the leftover
    # it looked like. The NVFP4 rationale (shared expert is BF16, fusing it into an NVFP4 buffer
    # fails on shape) genuinely does not apply here - shared experts carry weight_packed on disk
    # and are named in the checkpoint's 8-bit target list. Fusion still fails, just differently:
    #   --enforce-shared-experts-fusion -> RuntimeError: The size of tensor a (128) must match
    #   the size of tensor b (256) at non-singleton dimension 0
    # Another factor-of-2 clash, so the fused path mis-sizes quantized shared experts too.
    # Control arm in the same sweep: 30.6 tok/s, acceptance 2.58. Do not re-test this.
    # MEASURED 2026-09-09: 22.3 tok/s single-stream, 31.1 aggregate @8 - against 14.5 for the
    # NVFP4 build. +54%, and ahead of the 19.3 the bandwidth model predicted. CUDA graphs are
    # worth 1.39x here (22.3 vs 16.0 with --disable-cuda-graph): keep them ON.
    #
    # SIX load failures got here, each a real SGLang-vs-vLLM difference, all fixed in the
    # CHECKPOINT's config.json (kept in the model dir, .bak files alongside):
    #   1. qkv_proj absent from the 8-bit target list -> resolved to group_0's 4-bit `Linear`
    #      catch-all -> param 512 vs data 1024. Added it.
    #   2. packed_modules_mapping absent. SGLang reads it from the CHECKPOINT's
    #      quantization_config; vLLM reads the model class, so a vLLM-authored checkpoint omits
    #      it. Added.
    #   3. that indirection still does not rescue a fused name the targets never list, so
    #      SGLang's own fused spellings were added to the 8-bit target regex directly.
    #   4. the FUSION-patched image cannot serve quantized weights at all
    #      ('BasevLLMParameter' has no attribute 'output_dim') - upstream's
    #      `quant_config is None` gate is protecting that path, not being over-broad. Use the
    #      STOCK image. See Dockerfile.sglang-glm53-fuse's postscript.
    #   5. Marlin cannot repack b_proj: it emits 64 values (one per head) = 32 per rank at TP=2,
    #      below the tile minimum, and SGLang's wNa16 is Marlin-only with no override. Fixed by
    #      transplanting the ORIGINAL BF16 b_proj from the LibertAI checkpoint (same base model,
    #      so exact - no dequant arithmetic to get wrong) and ignoring it. Costs ~0.02 GiB.
    #   6. serves.
    #
    # ── MTP WORKS, AND IT IS THE BIGGEST SINGLE WIN. MEASURED 2026-09-09 ──────────────────────
    #   config              single   agg@8   acceptance
    #   no MTP               22.3     31.1      -
    #   NEXTN K=1            28.6     28.5    1.77
    #   NEXTN K=2            31.3     31.3    2.38   <- WINNER, and what is served below
    #   NEXTN K=3            27.9     30.1    2.75
    #   --disable-cuda-graph 15.0     25.3      -    <- graphs are worth 1.49x, never disable
    # K=2 is STRICTLY better than no MTP: same aggregate (31.3 vs 31.1) AND +40% single-stream.
    #
    # NOTE THE SHAPE: acceptance rises monotonically (1.77/2.38/2.75) but throughput PEAKS AT K=2
    # and falls at K=3. More drafts are accepted yet it gets slower, because each extra draft
    # position must be advanced through the KDA recurrent state and rolled back on rejection.
    # GLM-5.3 therefore follows qwen3.8-27b (K=2 peak), NOT qwen38fn (K=3 peak) - the fleet note
    # that the optimum is per-model is correct, and assuming qwen38fn's K=3 would have shipped
    # the SLOWEST of the three. Do not "tidy" K upward. K=4 is impossible (index_kpool=4).
    #
    # WHAT MADE MTP LOAD AT ALL. K=1/2/3 previously died identically at
    #   AttributeError: 'ColumnParallelLinear' object has no attribute 'weight'
    #   deepseek_weight_loader.py:559 post_load_weights <- glm5_next_nextn.py:84
    # SGLang has a hook for exactly this case - Glm5NextForConditionalGenerationNextN.
    # _resolve_nextn_quant_config - which builds the NextN draft block BF16 when the checkpoint
    # declares it unquantized. But it tests for the EXACT LITERAL string
    # "model.layers.45.*" in quantization_config.ignore. The checkpoint spells that as a regex
    # (re:.*layers\.45\..*), which an `in` test on a list of strings cannot see, so the hook
    # never fired, the draft modules were built quantized, and kv_b_proj exposed weight_packed
    # instead of .weight. Adding the literal to the ignore list fixes it; it is inert for normal
    # matching because our module prefixes are model.language_model.layers.45.*.
    #
    # MoE KERNEL TUNING - INVESTIGATED 2026-09-11, NOT WORTH IT. The server logs
    #   "Using default MoE kernel config. Performance might be sub-optimal! Config file not
    #    found at .../configs/triton_3_7_1/E=288,N=1024,device_name=NVIDIA_GB10.json"
    # which looks like a free win, and SGLang ships a tuner for it. It is not worth the run:
    # the warning is emitted AFTER "GLM5 NextN layer ... using BF16 draft modules", i.e. it is
    # the MTP DRAFT layer's BF16 MoE asking - ONE layer - while the 42 MoE layers of the main
    # model are INT4 and go through Marlin, which that tuner does not touch at all. The
    # dtype-less filename is the tell: a quantized shape would carry ",dtype=int4_w4a16".
    # Tuning int4 instead produces E=288,N=512,dtype=int4_w4a16.json, which the runtime never
    # reads. ~1280 configs per shape, two shapes, for low-single-digit upside on one layer.
    # (The tuner also needs two patches to run here at all: ray, and a Glm5Next branch in its
    #  architecture dispatch - see ~/vllm-ray-build/Dockerfile.sglang-moetune.)
    #
    # MTP needs draft buffers ON TOP of one KDA recurrent state per request, hence
    # --max-running-requests 1 and --mem-fraction-static 0.86. At 0.84/2 the state cache goes
    # negative. Aggregate does not suffer: 31.3 either way.
    IMAGE=sglang-glm53-gb10:local
    SUB=GLM-5.3-Flash-AWQ-kda-w8
    SERVED=glm53-awq-sglang-tp2
    #
    # ── MTP IS SAFE HERE, UNLIKE QWEN3.8-FLASH-NEXT. MEASURED 2026-09-17, n=40. ───────────────
    # The qwen member above disables MTP because partial acceptance corrupts a recurrent state that
    # cannot be rewound (60% of replies collapsed into "!"). GLM-5.3 is hybrid too - 34 of 45 layers
    # are KDA linear attention - so the same question had to be asked, at the same sample size.
    #   MTP K=2   0 collapses / 40 long reasoning generations, median 28.6 tok/s
    #   MTP off   0 collapses / 40 (same prompts),             median 22.1 tok/s
    #   acceptance 533 samples: median rate 0.62, median len 2.25, ZERO zero-acceptance events
    # +30% on REAL reasoning workloads, not just short benches - the opposite of qwen, where the
    # bench win evaporated in practice ("accept rate: 0.00 and it never recovers"). Three samples
    # tripped the collapse detector and all three were the model computing correctly
    # ("1.0000000000000002" etc.); ratetest-glm53.py no longer flags digit runs under 30 chars.
    # WHY IT IS SAFE: this image HAS the rollback qwen's lacks. kda_backend.py writes each draft
    # token's post-state into a speculative `intermediate_ssm` scratch and spec_utils.py:1121 calls
    # update_mamba_state_after_mtp_verify() to commit the accepted-length state. Missing scratch
    # RAISES rather than drifting silently. Re-verify with ~/llama-swap/mtp-stability-glm53.sh
    # (n>=40) after any image upgrade - never with a smaller n, see the qwen n=20 lesson above.
    #
    # --max-mamba-cache-size 8 IS REQUIRED, not tuning. Without it this member refuses to start,
    # intermittently, depending on how much memory is free at launch (2026-09-17 02:34 failure):
    #   "Hybrid (mamba/linear-attention) state cache is too small to serve any requests.
    #    max_mamba_cache_size=2, mamba_ratio=5, resulting max_num_reqs=0"
    # GLM needs 5 recurrent-state slots PER REQUEST; SGLang derived 2 that evening, so floor(2/5)=0.
    # Forcing 8 makes the launch deterministic for ~1 GB (conv 0.02 + ssm 0.62 + intermediate 0.41).
    # --max-total-tokens ${MAXTOK} BOUNDS THE POOL ABSOLUTELY, and that is the real fix. A derived
    # FRACTION is inherently racy: it is computed from MemAvailable at one instant but applied to
    # whatever exists when SGLang allocates. Measured twice on 2026-09-18 - 0.95 of a 96.9 GB
    # reading allocated against 105.7 GB and built a 1,182,720-token pool (1.5 GB free left), then
    # 0.92 of a 104 GB reading built 933,952 tokens (3.2 GB free). Both for a member serving ctx
    # 32768, which needs ~65k tokens. 131072 is 4x the context, costs 1.7 GB, and cannot drift.
    # MEMFRAC IS DERIVED, NOT FIXED (2026-09-18). 0.86 assumed ~112 GB free and produced a NEGATIVE
    # KV pool whenever Jean-Luc was busier: mem-fraction-static is a share of the memory present at
    # launch and the 87.8 GB of weights (incl. the MTP draft) come out of it FIRST, so the pool is
    # mf x 0.945 x free - 87.8. At 101 GB free, 0.86 gives -5.3 GB and SGLang refuses to start.
    # This is why the member started on some days and not others. Target a ~1.8 GB pool (the state
    # cache is 1.05 GB of it, leaving ~58k tokens of KV - ample at ctx 32768), clamped to [0.86, 0.92].
    # THE UPPER CLAMP MATTERS. It was 0.95, and on 2026-09-18 the fraction was derived at a moment
    # when only 96.9 GB was free (so it hit the clamp) but ALLOCATED against 105.7 GB once idle
    # sessions had been closed. SGLang then built a 1,182,720-token KV pool - ~15 GB of cache for a
    # 32k member - and left 1.5 GB free on each node, under the 3 GB watchdog floor and one new
    # session away from OOM. 0.92 with a 3 GB pool target keeps ~10 GB of headroom instead.
    # LONG-CONTEXT CALLERS: send chat_template_kwargs {"enable_thinking": false}. VERIFIED
    # 2026-09-19 at ctx 262144 - a needle 250k tokens behind the question came back exact with
    # thinking off, while every run WITH thinking returned nothing because the model was still
    # reasoning when max_tokens ran out. This is the opposite of the qwen38fn member above, where
    # enable_thinking=false strands the answer in reasoning_content. Retrieval here is fine; the
    # answer budget was the whole problem.
    # Sized from the context actually requested (glm-size.py, which carries the measured model
    # and a selftest). CTX_LEN comes from ~/.gb10/ctx/<member> via ctx-env.sh, so raising the
    # context here re-sizes the pool, the fraction and the memcheck gate together instead of
    # leaving three hardcoded numbers to disagree. 87.8 GB of weights, 3.0 GB of state+draft.
    # HARD CEILING 439296 (471k), Paul's choice 2026-09-20. Derived, not guessed: free memory after
    # one full-context request is free - 87.8 (weights) - pool - 8 (runtime) - ctx x 10 kB (in-use
    # state), and at today's ~113 GB free that hits the 3 GB watchdog floor at ~483k tokens.
    #   floor 6 GB -> 345k   5 GB -> 387k   4 GB -> 429k   3 GB -> 471k   512k would need ~2 GB
    # 512k is NOT merely against policy: it sits BELOW the 3 GB trip point, so the guard would fire
    # mid-request. 471k lands exactly ON it, which is why Paul chose 429k instead (2026-09-20): a
    # full GB of clearance, still 64% more context than the 256k default. glm-memfloor.service
    # guards it either way, so the failure mode is "model killed, reload it" rather than the kernel
    # choosing a victim among Paul's own sessions.
    # Clamped HERE rather than only in fleet-ui, so a stray ~/.gb10/ctx value cannot exceed it.
    CTX=${CTX_LEN:-32768}
    if [ "$CTX" -gt 439296 ]; then
      echo "serve-sglang[glm53awq]: context $CTX exceeds the 439296 (471k) ceiling - clamping" >&2
      CTX=439296
    fi
    # The export below resolves CTX_LEN="${CTX_LEN:-$CTX}", so an override would otherwise WIN over
    # the clamp and the ceiling would be cosmetic (caught 2026-09-20: ctx=999999 survived it).
    # Write the clamped value back. Only this profile does so - qwen38fn relies on the passthrough.
    CTX_LEN="$CTX"
    read -r MEMFRAC NEED MAXTOK <<< "$($LS/glm-size.py "$CTX" 87.8 3.0)"

    # THE LONG-CONTEXT SQUEEZE DID NOT WORK - removed 2026-09-20, do not re-derive it.
    # Tried at 512k+MTP: prefill chunk 8192->4096, CUDA graphs bs 4->2, mamba slots 8->6, pool slack
    # 10%->2%. Free memory on the HEAD node after one real 500k request: 2.8 GB with the squeeze and
    # 2.8 GB without it, to the decimal. So the ~3.4 GB consumed during a long request is not
    # activations and not graphs - it is head-node state that scales with sequence length (scheduler,
    # radix tree, DSA indexer metadata), which no flag here touches. Kathryn ends at 8.6 GB; Jean-Luc
    # is the binding node because it carries that state. 512k WITH MTP is therefore not viable:
    # it serves correctly (31-32 tok/s on a 510,710-token prompt) but leaves the node under its floor.
    # Use the no-MTP member for 512k - its weights are 8.2 GB smaller, which is the missing headroom.
    # Worth keeping in mind: chunk 4096 cost NOTHING in prefill (1075 vs 1067 tok/s).
    MAMBA=8; GRAPHBS=4; SQUEEZE=""
ne; what breaks the
    # node is the IN-USE cost, which is dominated by prefill activations (they scale with the chunk
    # size) and by CUDA graphs captured for batch sizes this member can never reach. Measured
    # 2026-09-19 without these: 7.7 GB free after load, 2.8 GB after one 500k request - under the
    # floor. Halving the chunk costs some prefill throughput and buys headroom back.
    # 6 mamba slots, not 8: MTP needs 5 per request and this member serves one at a time.
    MAMBA=8; GRAPHBS=4; SQUEEZE=""
    if [ "$CTX" -ge 393216 ]; then
      MAMBA=6; GRAPHBS=2; SQUEEZE="--chunked-prefill-size 4096"
    fi
    FLAGS="--attention-backend dsa --dsa-prefill-backend tilelang --dsa-decode-backend tilelang --moe-runner-backend auto --kv-cache-dtype bfloat16 --disable-shared-experts-fusion --reasoning-parser glm45 --tool-call-parser glm47 --max-running-requests 1 --max-mamba-cache-size ${MAMBA} --cuda-graph-max-bs ${GRAPHBS} ${SQUEEZE} --max-total-tokens ${MAXTOK} --speculative-algorithm NEXTN --speculative-eagle-topk 1 --speculative-num-steps 2 --speculative-num-draft-tokens 3" ;;
  glm53awq-long)
    # LONG-CONTEXT PROFILE: same checkpoint as glm53awq, MTP REMOVED, 640k window.
    # MEASURED 2026-09-16 (~112 GB free/node, ctx-sweep.py, raw data ~/llama-swap/.ctxsweep-glm53):
    #   prompt    prefill       decode        KV pool actually built: 662,400 tokens
    #    32k     1246 tok/s    21.8 tok/s
    #   262k     1095 tok/s    21.6 tok/s
    #   500k      910 tok/s    21.2 tok/s
    # DECODE IS FLAT IN CONTEXT: 500k costs 3% against 32k. What long context costs is MTP -
    # 31.5 -> 21.8 tok/s at the same 32k. That trade is why this is a SEPARATE key rather than a
    # bigger CTX on glm53awq: you pick speed or window, and 500k is nearly free once MTP is gone.
    #
    # WHY MTP CANNOT STAY. The draft layer is 8.2 GB of WEIGHTS, and --mem-fraction-static must
    # cover the weights FIRST (SGLang says so: "minimum viable = 1 - available/pre"). What is left
    # then builds ~57k tokens of KV with MTP, against 662k without. Raising CTX with MTP on makes
    # it WORSE, not better: at CTX=131072 the pool collapsed to 8,576 tokens, too small to even
    # accept a 22k-token prompt, because the per-slot KDA/SSM state cache grows with the window.
    #
    # 1M IS NOT REACHABLE ON THIS HARDWARE - settled 2026-09-16, not pending. bf16 KV for 1M is
    # ~13.6 GB plus the fixed state cache, which leaves under 1 GB of the 10-14 GB of runtime
    # memory each node needs for CUDA graphs, state cache and prefill workspaces. An fp8 KV cache
    # would halve it, but NO DSA backend does fp8 on sm_121: tilelang hardcodes bf16 on CUDA,
    # flashmla_sparse_q8 rejects anything but sm_9x by explicit check, flashmla_kv is Hopper, and
    # trtllm loads and then dies at CUDA-graph capture with "TllmGenFmhaRunner: Unsupported
    # architecture". Do not spend another night on it without a new SGLang image.
    #
    # MEMFRAC 0.86 is not tuned, it DEGRADES GRACEFULLY: the pool is simply whatever remains after
    # the weights, so less free memory means a smaller window, never a failed launch.
    # DELIBERATELY NOT IN config.yaml: any write there hot-reloads llama-swap and kills the pinned
    # model. Add the member by hand in a quiet moment if you want it swappable.
    IMAGE=sglang-glm53-gb10:local
    SUB=GLM-5.3-Flash-AWQ-kda-w8
    SERVED=glm53-awq-long-sglang-tp2
    # 524288, not 655360: Paul asked for the ladder to reach 512k (2026-09-18). The measured pool
    # at 655360 was 662,400 tokens, so 512k+margin is comfortably inside what this config builds.
    # --max-total-tokens bounds it absolutely - the same drift that twice handed the 32k member a
    # ~15 GB cache would otherwise apply here at a much larger scale.
    # Same derivation, without the MTP draft: 79.6 GB of weights, 2.0 GB of state.
    CTX=${CTX_LEN:-262144}
    read -r MEMFRAC NEED MAXTOK <<< "$($LS/glm-size.py "$CTX" 79.6 2.0)"
    FLAGS="--attention-backend dsa --dsa-prefill-backend tilelang --dsa-decode-backend tilelang --moe-runner-backend auto --kv-cache-dtype bfloat16 --disable-shared-experts-fusion --reasoning-parser glm45 --tool-call-parser glm47 --max-running-requests 1 --max-mamba-cache-size 8 --cuda-graph-max-bs 4 --max-total-tokens ${MAXTOK}" ;;
  *) echo "serve-sglang: unknown key '$KEY' (want qwen38fn|qwen38fn-long|glm53flash|glm53awq|glm53awq-long)" >&2; exit 1 ;;
esac

# MODEL_FLAGS_EXTRA: opt-in hook to append flags without editing this table (same idiom as
# serve-starfleet.sh). Used for A/B runs, e.g. MODEL_FLAGS_EXTRA="--ep-size 2" to test expert
# parallelism against plain TP, or --enable-torch-compile once the kernel path is stable.
FLAGS="$FLAGS ${MODEL_FLAGS_EXTRA:-}"

export IMAGE MODEL_SUBDIR="$SUB" SERVED_NAME="$SERVED" NEED_MB="$NEED" \
       MEM_FRACTION="${MEM_FRACTION:-$MEMFRAC}" CTX_LEN="${CTX_LEN:-$CTX}" \
       MODEL_FLAGS="$FLAGS" PORT="$PORT"

if [ "${DRYRUN:-0}" = 1 ]; then
  echo "key=$KEY image=$IMAGE sub=$SUB served=$SERVED need=${NEED}MB memfrac=$MEM_FRACTION ctx=$CTX_LEN port=$PORT flags=[$FLAGS]"
  exit 0
fi
exec "$LS/run-sglang-tp2-cluster.sh"
