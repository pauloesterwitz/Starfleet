#!/bin/bash
# serve-embed.sh — llama-swap-managed EMBEDDING members (vLLM pooling runner).
#
# Added 2026-07-27 to close the gap left by switching the ollama backend off: nomic-embed-text and
# embeddinggemma were `ollama` member aliases, so when ollama went away every local RAG embedding
# path lost its endpoint and there was no vLLM replacement wired. These are the replacement.
#
# Deliberately simpler than serve-vllm-dflash.sh: these models are 0.5-1.2 GB (not 43-90 GB), so
# there is no reservation-file protocol and no render-guard — they fit in the co-resident pool at
# any time. memcheck still gates with a small NEED_MB so a genuinely exhausted pool fails clean
# rather than OOMing (an OOM here is a kernel panic, see memcheck.sh).
#
# Usage: serve-embed.sh <key> --port N        (DRYRUN=1 prints the argv and exits)
set -uo pipefail

# UPGRADED to v0.27.1 2026-08-20: nomic-embed-text and embeddinggemma both PASS (dim=768) on the
# retest. Previously nightly-aarch64. NOTE both models failed the FIRST sweep with "no-serve" —
# that was this file's `exec` on line ~85 making the launcher vanish from ps, which the old
# harness mistook for a dead server. The models were fine.
# Revert: IMAGE=vllm/vllm-openai:nightly-aarch64 in the environment.
IMAGE="${IMAGE:-vllm/vllm-openai:v0.27.1-aarch64}"
MODELS="${MODELS:-$HOME/models}"
MEMCHECK="$HOME/llama-swap/memcheck.sh"
NEED_MB="${NEED_MB:-4000}"

KEY="${1:-}"; shift || true
PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$KEY" ]  || { echo "serve-embed: no model key given" >&2; exit 2; }
[ -n "$PORT" ] || { echo "serve-embed: --port is required" >&2; exit 2; }

# EXTRA per key. --trust-remote-code is needed for nomic (custom NomicBert code in the repo);
# embeddinggemma is a stock Gemma3TextModel and does not need it.
EXTRA=()
case "$KEY" in
  nomic-embed-text)
    BASE="/models/nomic-embed-text-v1.5"
    EXTRA=(--trust-remote-code)
    ;;
  embeddinggemma)
    BASE="/models/embeddinggemma-300m"
    ;;
  bge-reranker-v2-m3)
    # Cross-encoder reranker, not an embedder: native XLMRobertaForSequenceClassification
    # (num_labels=1) — --runner pooling's default --convert auto detects the classify/score
    # task on its own, no explicit --convert flag needed. Exposes /v1/score and /v1/rerank.
    BASE="/models/bge-reranker-v2-m3"
    ;;
  harrier-embed-0.6b)
    # microsoft/harrier-oss-v1-0.6b, added 2026-09-23. Multilingual MTEB-v2 SOTA-class embedder
    # (69.0), 1024-dim, 32k tokens. Unlike the two 768-dim embedders above it is a DECODER
    # (config.json architectures: ["Qwen3Model"], 28 layers, hidden 1024) with LAST-TOKEN pooling,
    # not mean/CLS — hence the POOLER override below. "Qwen3Model" is NOT a registered vLLM arch
    # (checked against v0.27.1's ModelRegistry), so --convert embed is what makes vLLM wrap the
    # bare Qwen3 body as a pooling model instead of failing the arch lookup.
    #
    # CALLER CONTRACT — this model is ASYMMETRIC. Queries must be prefixed
    #   "Instruct: <task>\nQuery: <text>"   (see config_sentence_transformers.json for the three
    # stock task strings; web_search_query is the retrieval one). Documents are embedded RAW, no
    # prefix. vLLM does not apply prompts server-side, so the caller owns this — embedding a query
    # without the prefix silently costs retrieval quality rather than erroring.
    # 1024-dim, so RAG_EMBED_DIM=768 callers are NOT drop-in compatible with this one.
    # KEY is harrier-EMBED-0.6b, not harrier-oss-0.6b: fleet-ui's Embeddings filter selects
    # members by name (/embed|rerank/i in fleet.html), so a name carrying "embed" keeps this
    # model in that filter with no fleet-ui change — same reason nomic-embed-text reads the way
    # it does. The weights directory below keeps the upstream repo name.
    BASE="/models/harrier-oss-v1-0.6b"
    POOLER='{"pooling_type": "LAST", "use_activation": true}'
    EXTRA=(--convert embed)
    ;;

  *)
    echo "serve-embed: unknown key '$KEY' (want: nomic-embed-text | embeddinggemma | bge-reranker-v2-m3 | harrier-embed-0.6b)" >&2
    exit 2
    ;;
esac

NAME="$KEY"

# use_activation:true applies the model's output activation — L2 normalization for an embed
# task (WITHOUT it vLLM returns UNNORMALIZED vectors, measured L2 ~22.8 for nomic, while ollama
# always returned unit vectors) or sigmoid for a classify/rerank task (turns the raw logit into
# a 0-1 relevance score, matching BAAI's own reference scoring). Verified for embed: with this
# flag the L2 norm is 1.000000.
POOLER="${POOLER:-{\"use_activation\": true\}}"

docker rm -f "$NAME" >/dev/null 2>&1 || true
cleanup() { echo "serve-embed: stopping $NAME" >&2; docker stop -t 10 "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM HUP

# --init so SIGTERM reaches vLLM (PID 1); --rm auto-cleans.
# --served-model-name = the llama-swap member so proxied requests match.
# gpu-memory-utilization 0.08 (~9 GB of the 121 GB pool): these models are tiny, and a high value
# would have vLLM reserve pool an LLM member then cannot get.
CMD=(docker run --rm --name "$NAME" --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all --ipc=host --init
    -p 127.0.0.1:"${PORT}":8000
    -v "$MODELS":/models:ro
    "$IMAGE"
    "$BASE"
      --runner pooling
      --served-model-name "$NAME"
      --pooler-config "$POOLER"
      "${EXTRA[@]}"
      --gpu-memory-utilization 0.08
      --host 0.0.0.0 --port 8000)

# ponytail: DRYRUN=1 prints the argv and exits — the self-check for the case table above, runnable
# with no GPU and no pool pressure. `DRYRUN=1 ./serve-embed.sh <key> --port 8000` per key.
if [ -n "${DRYRUN:-}" ]; then printf '%q ' "${CMD[@]}"; echo; trap - EXIT INT TERM HUP; exit 0; fi

# Moved BELOW the DRYRUN exit 2026-09-23: it used to sit above the CMD block, which made the
# self-check above unrunnable whenever the pool was full — exactly the situation in which you
# most want to check a new key's argv without loading anything.
if [ -x "$MEMCHECK" ]; then
  "$MEMCHECK" "$NEED_MB" || { echo "serve-embed: pool too full for $KEY (${NEED_MB}MB)" >&2; exit 1; }
fi

exec "${CMD[@]}"
