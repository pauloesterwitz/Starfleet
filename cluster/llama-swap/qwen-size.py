#!/usr/bin/env python3
"""qwen-size.py <ctx>  ->  "MEMFRAC NEED_MB MAX_TOTAL_TOKENS SHORT_GB"

Sizes the qwen38fn SGLang member (Qwen3.8-Flash-Next NVFP4, TP=2, MTP K=1) from the context it
is asked to serve and the memory free RIGHT NOW, instead of a fixed --mem-fraction-static 0.80.
Sibling of glm-size.py; own file for the same quoting reason.

WHY (2026-11-16 22:27 load): 0.80 of 94.03 GB free left a 2 GB pool = 79,808 tokens, while the
same 0.80 on a quiet ~112 GB node had built 481,984. OpenCode then sent 215k-token prompts and
SGLang rejected them ("maximum allowed length 79802"). A fixed fraction makes the window depend
on whatever else happened to be running at load time.

THE MODEL, measured from that load's log (logs/sglang-Qwen3.8-Flash-Next-NVFP4-Spark.log):
  consumed   73.0 GB from launch to pool (94.03 -> 21.04: weights 71.65 + MTP 1.03 + dist init)
  KV         12.53 kB/token per node incl. the MTP draft KV (0.92 + 0.08 GB for 79,808 tokens)
  state      0.91 GB mamba/linear-attention state (10 slots), fixed, not per token
  SGLang     pool budget = free_at_launch x mf - consumed   (21.04 - 0.20 x 94.03 = 2.2 GB, matches)

Policy: aim the fraction at a pool for TWO full-context requests (the member runs 2 at a time),
clamped to [0.80, 0.88]; --max-total-tokens caps the pool at that, so a generous fraction on a
quiet node frees memory instead of building a 15 GB cache. SHORT_GB > 0 means even 0.88 cannot
hold ONE full-context request: the caller refuses to load rather than serve a shrunken window.
"""
import sys

CONSUMED_GB = 73.0
KV_GB_PER_TOKEN = 12.53e-6
STATE_GB = 0.91
RUNTIME_GB = 8.0          # CUDA graphs, workspaces, in-use growth; only feeds the memcheck gate
MF_MIN, MF_MAX = 0.80, 0.88

def pool_gb(tokens: int) -> float:
    return tokens * KV_GB_PER_TOKEN + STATE_GB

def size(ctx: int, free: float):
    target = 2 * ctx
    mf = min(MF_MAX, max(MF_MIN, (CONSUMED_GB + pool_gb(target)) / max(free, 1.0)))
    # ponytail: free is read once here but SGLang measures it again at launch; the 2-request target
    # is the drift margin (~3.5 GB at 94 GB free). Jean-Luc only: Kathryn is normally freer.
    short = max(0.0, pool_gb(int(ctx * 1.05)) - (mf * free - CONSUMED_GB))
    need_mb = int((CONSUMED_GB + pool_gb(int(ctx * 1.05)) + RUNTIME_GB) * 1000)
    return mf, need_mb, target, short

def free_gb() -> float:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) / 1048576
    return 0.0

if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        # last night's 94.03 GB must now hold a full 256k request (it built 79,808 at 0.80)
        mf, need, maxtok, short = size(262144, 94.03)
        assert short == 0 and mf * 94.03 - CONSUMED_GB >= pool_gb(int(262144 * 1.05)), (mf, short)
        assert 0.80 < mf <= MF_MAX and maxtok == 524288, (mf, maxtok)
        # a quiet node stays at the old 0.80; the token cap, not the fraction, bounds the pool
        assert size(262144, 112.0)[0] == MF_MIN
        # a busy node is refused, not loaded with a shrunken window
        assert size(262144, 84.0)[3] > 0
        # need stays near the old hand-set 85000 MB gate
        assert 84000 < need < 87000, need
        print("qwen-size selftest passed")
        raise SystemExit(0)
    mf, need, maxtok, short = size(int(sys.argv[1]), free_gb())
    print(f"{mf:.3f} {need} {maxtok} {short:.1f}")
