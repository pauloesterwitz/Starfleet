#!/usr/bin/env python3
"""Declare each llamaswap model's real context in opencode.json.

context = the size the model is actually SERVED at (from the launcher scripts),
or the Fleet/ctxproxy override in ~/.gb10/ctx/<member> when one is set -- that
override IS the deployed size once the model reloads, so it must win.

Deliberately NOT the checkpoint's native maximum: declaring more than the server
was started with is exactly what produced
  "max_completion_tokens is too large: 32000 ... at most 16384"
"""
import json, os, sys

# Served context per model, traced to its launcher:
#   serve-vllm-dflash.sh  --max-model-len ${MML_OVERRIDE:-262144}  (all keys)
#   serve-kathryn*.sh / pick-node.sh  delegate to the above with the same key
#   serve-starfleet.sh    per-key MML=
#   serve-sglang.sh       per-key CTX=
#   serve-nemcascade.sh   MAX_MODEL_LEN=${MML_OVERRIDE:-262144}
#   ds4/serve-ds4.sh      --ctx 409600
#   run-ds4-tp2-cluster.sh  MAX_MODEL_LEN=${MAX_MODEL_LEN:-32768}
SERVED = {
    "deepseek-v4-flash": 409600,
    "ds4-nvfp4-tp2": 32768,
    "gemma4-26b-54tps-jean-luc": 262144,
    "gemma4-26b-56tps-fastest-node": 262144,
    "gemma4-26b-57tps-kathryn": 262144,
    "gemma4-31b-19tps-starfleet": 262144,
    "glm53-flash-sglang-tp2-starfleet": 65536,
    "hy3-mtp-21tps-starfleet": 98304,
    "minimax-awq-35tps-starfleet": 196608,
    "nemcascade-49tps-kathryn": 262144,
    "nemcascade-61tps-jean-luc": 262144,
    "nemotron-120b-mtp3-starfleet": 262144,
    "qwen3-235b-20tps-starfleet": 40960,
    "qwen3.5-122b-mtp3-starfleet": 262144,
    "qwen3.6-35b-85tps-mtp4-jean-luc": 262144,
    "qwen3.6-35b-86tps-mtp4-fastest-node": 262144,
    "qwen3.6-35b-87tps-mtp4-kathryn": 262144,
    "qwen3.8-27b-19tps-jean-luc": 262144,
    "qwen3.8-27b-20tps-kathryn": 262144,
    "qwen3.8-27b-vision-jean-luc": 262144,
    "qwen3.8-27b-vision-kathryn": 262144,
    "qwen38fn-sglang-tp2-starfleet": 16384,
    "qwen3vl32-19tps-starfleet": 262144,
    "qwen3vl32-vision-starfleet": 262144,
    "qwenvl235-vision-starfleet": 262144,
}
CTX_DIR = os.path.expanduser("~/.gb10/ctx")
CFG = os.path.expanduser("~/.config/opencode/opencode.json")


def override(member):
    try:
        with open(os.path.join(CTX_DIR, member)) as fh:
            v = int(fh.read().strip().split()[0])
        return v if 256 <= v <= 1048576 else None
    except (OSError, ValueError, IndexError):
        return None


cfg = json.load(open(CFG))
models = cfg["provider"]["llamaswap"]["models"]
missing = sorted(set(models) - set(SERVED))
if missing:
    sys.exit("no served context known for: %s" % missing)

print("%-38s %9s %9s %9s  %s" % ("MODEL", "SERVED", "OVERRIDE", "CONTEXT", "OUTPUT"))
for m in sorted(models):
    ov = override(m)
    ctx = ov or SERVED[m]
    # A quarter of the window, capped at 32000 -- opencode's own ceiling, observed
    # live: declaring 32768 made it send 32000 anyway, so declare what it will
    # actually send. Must stay under `ctx` (the server
    # rejects a bigger completion budget than its context) but well clear of the
    # low end: these models think by default and the thinking tokens come out of
    # this same budget, so too small returns empty content.
    out = min(32000, ctx // 4)
    models[m]["limit"] = {"context": ctx, "output": out}
    print("%-38s %9d %9s %9d  %d%s" % (
        m, SERVED[m], ov if ov else "-", ctx, out, "   <- Fleet override" if ov else ""))

json.dump(cfg, open(CFG, "w"), indent=1, ensure_ascii=False)
print("\nwrote", CFG)
