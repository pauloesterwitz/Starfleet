#!/usr/bin/env python3
"""glm-size.py <ctx> <weights_gb> <state_gb>  ->  "MEMFRAC NEED_MB MAX_TOTAL_TOKENS"

Sizes a GLM-5.3-Flash member from the context it is actually being asked to serve, instead of the
three hardcoded numbers (NEED / MEMFRAC / CTX) that kept disagreeing with each other and with the
machine. Called from serve-sglang.sh; lives in its own file so no quoting has to survive a bash
case statement (an inline version broke the launcher on 2026-09-18).

THE MODEL, all measured on the two Sparks:
  weights      87.8 GB with the MTP draft layer, 79.6 GB without
  KV           12.93 kB/token, bf16 ("KV Cache is allocated ... #tokens: 57472, KV size: 0.68 GB")
  state        the pool also holds the KDA/mamba state cache, which is NOT proportional to context:
               ~3.0 GB with MTP (draft buffers on top), ~2.0 GB without. This is what made MTP look
               context-limited: at a FIXED mem-fraction of 0.86 the state took the pool and KV was
               squeezed to 0.11 GB (8,576 tokens) at ctx 131072. Sized deliberately, 256k with MTP
               needs 87.8 + 3.4 + 3.0 = ~94 GB and fits in the ~115 GB a quiet node has.
  mem-fraction a share of the memory present AT LAUNCH, weights first: pool = mf x 0.945 x free - W

The 0.95 ceiling is safe ONLY alongside --max-total-tokens (printed here): SGLang allocates the
smaller of the fraction budget and that cap, so a generous fraction can no longer become a 15 GB
cache the way it did twice on 2026-09-18 (1,182,720 and 933,952 tokens for a 32k member).
"""
import sys

KV_KB_PER_TOKEN = 12.93
# Runtime memory is NOT constant: a member also consumes memory while actually USING its context,
# on top of the preallocated pool. MEASURED 2026-09-19, free memory before vs after one full-context
# request: 256k 11.1 -> 8.4 GB (~3 GB), 512k 7.7 -> 2.8 GB (~5 GB), i.e. roughly 10 kB per token of
# context on top of everything else. Budgeting only the load-time figure is what made a 512k member
# look acceptable at 7.7 GB free and then drop under the 3 GB floor the moment it was used.
RUNTIME_BASE_GB = 8.0     # CUDA graphs + workspaces, with --cuda-graph-max-bs 4
RUNTIME_PER_TOKEN_KB = 10.0
SLACK = 1.10              # let the pool hold one full-context request plus a little

def size(ctx: int, weights: float, state: float, free: float):
    # Above 384k the 10% pool slack is itself ~0.7 GB, and the state cache is smaller because the
    # squeeze drops to 6 mamba slots. Both matter when the whole margin is ~3 GB.
    slack = 1.02 if ctx >= 393216 else SLACK
    state = state * 0.75 if ctx >= 393216 else state
    pool = ctx * KV_KB_PER_TOKEN / 1e6 * slack + state
    runtime = RUNTIME_BASE_GB + ctx * RUNTIME_PER_TOKEN_KB / 1e6
    mf = min(0.95, max(0.86, (weights + pool) / (0.945 * max(free, 1.0))))
    # the gate must cover the IN-USE footprint, not just the load, or the member passes and then
    # starves the node the first time someone sends a full-context prompt
    return mf, int((weights + pool + runtime) * 1000), int(ctx * (1.02 if ctx >= 393216 else 1.05))

def free_gb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) / 1048576
    return 0.0

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        # 256k with MTP must fit a quiet node, and must ask for a pool that really holds 256k
        mf, need, maxtok = size(262144, 87.8, 3.0, 115.0)
        pool = mf * 0.945 * 115.0 - 87.8
        assert maxtok >= 262144, maxtok
        assert pool >= 262144 * KV_KB_PER_TOKEN / 1e6 + 3.0, pool
        assert 115.0 - 87.8 - pool - (RUNTIME_BASE_GB + 262144 * RUNTIME_PER_TOKEN_KB / 1e6) > 0, "no headroom"
        # and 512k with MTP must now be flagged as NOT fitting a 114 GB node once in use
        _, need512, _ = size(524288, 87.8, 3.0, 114.0)
        assert need512 > 105000, f"512k+MTP should still read as demanding, got {need512} MB"
        # a busy node must NOT be talked into a fit: the fraction clamps and memcheck refuses
        mf2, need2, _ = size(262144, 87.8, 3.0, 96.0)
        assert mf2 == 0.95 and need2 > 96000, (mf2, need2)
        # 32k must stay cheap
        _, need3, maxtok3 = size(32768, 87.8, 3.0, 115.0)
        assert maxtok3 < 40000 and need3 < need, (need3, maxtok3)
        print("glm-size selftest passed")
        raise SystemExit(0)
    ctx = int(sys.argv[1]); weights = float(sys.argv[2]); state = float(sys.argv[3])
    mf, need, maxtok = size(ctx, weights, state, free_gb())
    print(f"{mf:.3f} {need} {maxtok}")
