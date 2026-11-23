"""RAM per model per machine for Shuttle Bay's Model Registry: how much of each Spark's 121 GB unified pool a model claims.

Nothing here loads or starts a model. The number comes from each member's own launcher, read through its read-only DRYRUN path
(the same probe fleet.py already uses for context sizes):

  engine claim   vLLM and SGLang take a FRACTION of the node's total pool the moment they start, whatever the weights weigh
                 (--gpu-memory-utilization / memfrac / util=). claim = fraction x that node's total memory. This is the figure that
                 decides what else fits (Jean-Luc showed ~3 GB free with the 0.88 Qwen3.8 resident), so it is the one shown.
  gate need      the launcher's own admission floor (need=...MB, `memcheck.sh N`, serve-kathryn.sh <key> <N>). Used where a server
                 has no fraction (ds4, the llama.cpp servers) and kept next to the claim as the second number.

A member that runs across both Sparks (TP=2) claims its figure on EACH of them. A member the launcher cannot answer for (the
Kathryn wrappers that ignore DRYRUN) borrows its Jean-Luc twin's figure through the launcher key, exactly like the context sizes.
Image/video rows carry the catalog's need_mb; speech rows the registry's need_mb, or the measured mem_mb while running.

  python3 modelram.py      self-check of the parsing and the twin/TP=2 rules (no launcher is run)
"""
import re

POOL_FALLBACK_MB = 124608      # a GB10's total memory as the kernel reports it (127,598,700 kB)
_UTIL = (re.compile(r"--gpu-memory-utilization[=\s]+([0-9.]+)"), re.compile(r"\b(?:util|memfrac)=([0-9.]+)"),
         re.compile(r"\bGPUFRAC=([0-9.]+)"))       # serve-embed's torch-cap server (embeddinggemma2): a hard cap, not a vLLM claim
_NEED = re.compile(r"\bneed=(\d+)MB")
_MEMCHECK = re.compile(r"memcheck\.sh\s+(\d+)")
_KATHRYN = re.compile(r"serve-kathryn\.sh\s+\S+\s+(\d+)")


def parse_dryrun(out):
    """(utilisation fraction | None, gate need in MB | None) from a launcher's DRYRUN text."""
    flat = (out or "").replace("\\", "").replace('"', "")
    util = next((float(m.group(1)) for rx in _UTIL for m in [rx.search(flat)] if m), None)
    need = _NEED.search(flat)
    return (util if util and 0 < util <= 1 else None), (int(need.group(1)) if need else None)


def gate_from_cmd(cmd):
    """The admission floor written into a member's llama-swap cmd, if any."""
    m = _MEMCHECK.search(cmd or "") or _KATHRYN.search(cmd or "")
    return int(m.group(1)) if m else None


def figure(util, need, pool_mb):
    """(MB claimed per machine, basis text) from what the launcher reports."""
    if util:
        mb = round(util * pool_mb)
        return mb, f"engine claim: {util:g} of the {pool_mb / 1024:.0f} GB pool" + (f" (admission floor {need / 1000:.0f} GB)" if need else "")
    if need:
        return need, "admission floor of the launcher (no pool fraction: weights + cache are sized to it)"
    return None, None


def resolve(cmds, probe, nodes_of, pool_of):
    """{member: {"ram_mb": {node: MB}, "ram_basis": str, "ram_gate_mb": MB|None}} for every llama-swap member.

    cmds      member -> llama-swap cmd line            probe(cmd) -> (key, util, need) from the launcher's DRYRUN, key "" if unknown
    nodes_of  member -> ["jean-luc"] | [both] | ["dynamic"]      pool_of(node) -> total MB of that node ("dynamic": the smaller one)
    """
    seen, by_key = {}, {}
    for member, cmd in cmds.items():
        key, util, need = probe(cmd)
        need = need or gate_from_cmd(cmd)
        seen[member] = (key, util, need)
        if key and (util or need) and key not in by_key:
            by_key[key] = (util, need)
    out = {}
    for member, (key, util, need) in seen.items():
        twin = re.sub(r"-kathryn$", "", member)            # deepseek-v4-flash-kathryn has no key: its twin is the unsuffixed member
        if not util and not need and twin != member and twin in seen:
            util, need = seen[twin][1], seen[twin][2]
        if not util and key in by_key:            # the Kathryn / fastest-node twin of a Jean-Luc launcher
            util, need = by_key[key][0], need or by_key[key][1]
        nodes = nodes_of(member) or []
        mb_basis = [(figure(util, need, pool_of(n)), n) for n in nodes]
        if not mb_basis or mb_basis[0][0][0] is None:
            continue
        out[member] = {"ram_mb": {n: f[0] for f, n in mb_basis}, "ram_basis": mb_basis[0][0][1], "ram_gate_mb": need}
    return out


def selfcheck():
    u, n = parse_dryrun("sub=X need=55000MB util=0.72 ctx=1")
    assert (u, n) == (0.72, 55000), (u, n)
    u, n = parse_dryrun("docker run x --gpu-memory-utilization 0.40 --max-model-len 8")
    assert (u, n) == (0.40, None), (u, n)
    u, n = parse_dryrun("docker run x --gpu-memory-utilization\\ 0.2 y")
    assert u == 0.2, u
    u, n = parse_dryrun("key=glm53awq need=99593MB memfrac=0.950 ctx=1")
    assert (u, n) == (0.95, 99593)
    assert parse_dryrun("-e GPUFRAC=0.05 --entrypoint")[0] == 0.05
    assert parse_dryrun("nothing here") == (None, None) and parse_dryrun("util=7")[0] is None
    assert gate_from_cmd("a/memcheck.sh 108000 a/serve-ds4.sh") == 108000 and gate_from_cmd("a/serve-kathryn.sh gemma 50000 --port 1") == 50000
    mb, why = figure(0.72, 55000, 124608)
    assert mb == 89718 and "0.72" in why and "55 GB" in why, (mb, why)
    assert figure(None, 108000, 124608)[0] == 108000 and figure(None, None, 1) == (None, None)
    cmds = {"a-jean-luc": "x/serve-vllm-dflash.sh k1", "a-kathryn": "x/serve-kathryn.sh k1 50000", "tp-starfleet": "x/serve-starfleet.sh k2",
            "ds4": "m/memcheck.sh 108000 x/serve-ds4.sh", "bad": "x/serve-unknown.sh"}
    probes = {"a-jean-luc": ("k1", 0.40, None), "a-kathryn": ("k1", None, None), "tp-starfleet": ("k2", 0.5, 45000), "ds4": ("", None, None),
              "bad": ("", None, None)}
    nodes = {"a-jean-luc": ["jean-luc"], "a-kathryn": ["kathryn"], "tp-starfleet": ["jean-luc", "kathryn"], "ds4": ["jean-luc"], "bad": ["jean-luc"]}
    r = resolve(cmds, lambda c: probes[next(k for k, v in cmds.items() if v == c)], nodes.get, lambda n: 100000)
    assert r["a-kathryn"]["ram_mb"] == {"kathryn": 40000} and r["a-kathryn"]["ram_gate_mb"] == 50000, "Kathryn twin borrows the Jean-Luc fraction"
    assert r["tp-starfleet"]["ram_mb"] == {"jean-luc": 50000, "kathryn": 50000}, "TP=2 claims its figure on each machine"
    assert r["ds4"]["ram_mb"] == {"jean-luc": 108000} and "bad" not in r, "no fraction: the gate; nothing known: no row"
    print("ok - modelram.py: DRYRUN parsing, claim formula, Kathryn twins, TP=2 on both machines")


if __name__ == "__main__":
    selfcheck()
