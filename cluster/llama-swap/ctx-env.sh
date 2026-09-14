#!/bin/bash
# ctx-env.sh <member> <cmd> [args...]
#
# llama-swap member prefix that turns a per-request context size into the env var the
# fleet launchers already honour. ctxproxy.py writes the requested size to
# $GB10_STATE_DIR/ctx/<member> (default ~/.gb10/ctx/<member>) and unloads the member;
# on the next load llama-swap runs this wrapper, which reads that file and exports
# MML_OVERRIDE (serve-vllm-dflash.sh, serve-starfleet.sh), MAX_MODEL_LEN
# (run-single-node.sh, serve-nemcascade.sh, the cluster runners) and CTX_LEN
# (serve-sglang.sh).
#
# All three names, because the launchers do not agree on one: serve-sglang.sh reads
# CTX_LEN ("${CTX_LEN:-$CTX}"), so before it was exported here the two SGLang members
# (qwen38fn, glm53flash) silently ignored every context request -- ctxproxy and Fleet
# both accepted the size, unloaded the model, and reloaded it at the same deployed
# default. Exporting a name a launcher does not read is harmless; missing one is not.
#
#   cmd: ctx-env.sh <member> render-guard.sh memcheck.sh <need_mb> serve-vllm-dflash.sh <key> --port ${PORT}
#
# No file, junk in the file, or a size out of range: change nothing, serve the deployed
# default. This wrapper must never be the reason a model refuses to load.
#
# exec, not a subshell: memcheck.sh and serve-vllm-dflash.sh key their reservation ledger
# on $$, so the pid has to survive the whole chain (same reason render-guard.sh execs).
#
# NOTE the KV budget is NOT reconsidered here. --gpu-memory-utilization and memcheck's
# need_mb stay at their tuned values, so a bigger context buys KV out of the same pool and
# vLLM will refuse at startup if the pool cannot hold it (a clean error, not an OOM).
set -uo pipefail

member="${1:?usage: ctx-env.sh <member> <cmd> [args...]}"; shift
[ $# -gt 0 ] || { echo "ctx-env: no command to run" >&2; exit 2; }

state="${GB10_STATE_DIR:-$HOME/.gb10}/ctx/${member}"
if [ -r "$state" ]; then
  read -r ctx _ < "$state" 2>/dev/null || ctx=""
  case "${ctx:-}" in
    ''|*[!0-9]*)
      [ -n "${ctx:-}" ] && echo "ctx-env[$member]: ignoring non-numeric context '$ctx'" >&2 ;;
    *)
      if [ "$ctx" -ge 256 ] && [ "$ctx" -le 1048576 ]; then
        export MML_OVERRIDE="$ctx" MAX_MODEL_LEN="$ctx" CTX_LEN="$ctx"
        echo "ctx-env[$member]: max-model-len $ctx (requested)" >&2
      else
        echo "ctx-env[$member]: ignoring out-of-range context '$ctx'" >&2
      fi ;;
  esac
fi

# Per-model thinking/reasoning default. Same idea as the context above: fleet writes
# the exact chat-template kwargs JSON to $GB10_STATE_DIR/effort/<member>, and both
# engines accept it at load time as --default-chat-template-kwargs (vLLM and SGLang
# spell it identically; request-level kwargs still win over it).
#
# APPENDED to MODEL_FLAGS_EXTRA, never assigned over it: that variable is the
# launchers' documented A/B hook (serve-starfleet.sh, serve-sglang.sh), and a bench
# run that sets it must keep its flags.
#
# The file must hold COMPACT JSON with no spaces. FLAGS strings are word-split by the
# launchers, so a space would split the JSON into two arguments. Brace expansion is
# not a hazard here -- bash does not brace-expand the result of a variable expansion --
# and quotes inside the value survive for the same reason, which is what the engines'
# json.loads wants. Anything that is not compact single-line JSON is ignored, on the
# same principle as the context file: never be the reason a model fails to load.
effort_file="${GB10_STATE_DIR:-$HOME/.gb10}/effort/${member}"
if [ -r "$effort_file" ]; then
  # IFS= and no trailing field: read the WHOLE line, so a JSON containing a space is
  # detected and reported below rather than silently truncated at the first field.
  # `|| :` not `|| kwargs=""`: read returns non-zero at EOF on a file with no trailing
  # newline, having ALREADY assigned the value -- clearing it there would silently
  # discard a perfectly good kwargs file.
  kwargs=""
  IFS= read -r kwargs < "$effort_file" 2>/dev/null || :
  case "${kwargs:-}" in
    '{'*'}')
      if [ "${kwargs#*[[:space:]]}" = "$kwargs" ]; then
        MODEL_FLAGS_EXTRA="${MODEL_FLAGS_EXTRA:-} --default-chat-template-kwargs $kwargs"
        export MODEL_FLAGS_EXTRA
        echo "ctx-env[$member]: chat-template kwargs $kwargs" >&2
      else
        echo "ctx-env[$member]: ignoring effort kwargs containing whitespace" >&2
      fi ;;
    '') ;;
    *) echo "ctx-env[$member]: ignoring non-JSON effort kwargs '$kwargs'" >&2 ;;
  esac
fi

exec "$@"
