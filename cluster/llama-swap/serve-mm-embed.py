#!/usr/bin/env python3
"""serve-mm-embed.py - minimal OpenAI-compatible /v1/embeddings server for MULTIMODAL
sentence-transformers checkpoints that vLLM cannot serve yet.

Written 2026-10-14 for google/embeddinggemma-2 (text + image + audio + video into one 768-d
space). vLLM 0.27.1 has no EmbeddingGemma2 architecture and its Transformers backend is on
transformers 5.15, which predates the model, so serve-embed.sh runs THIS inside the same
vllm image (for its torch/CUDA) with a newer transformers + sentence-transformers overlaid
from ~/llama-swap/st-libs via PYTHONPATH. Delete this file once vLLM registers the architecture
and move the key back to the vLLM case table.

Run by serve-embed.sh, never by hand:   python3 serve-mm-embed.py --port 8000 [--max-model-len N]
Environment: MODEL_DIR, SERVED_NAME, GPUFRAC (hard torch memory cap), VISION_TOKENS.

CALLER CONTRACT (the same shape as harrier / qwen3-embed: the server applies NO prompts)
  query     "task: search result | query: <text>"
  document  "title: none | text: <content>"        (or "title: <title> | text: ...")
  image     no prefix; pass it as an input object, see below
A bare query silently costs retrieval quality instead of erroring, as with the other embedders.

INPUT  POST /v1/embeddings  {"model": ..., "input": [item, ...], "dimensions": 768|512|256|128}
  item = "plain text"
       | {"image": "data:image/png;base64,..."}                      one image
       | {"text": "caption <|image|>", "image": ["data:...", ...]}    interleaved text + images
  Images are accepted ONLY as data: URIs. No http(s) and no file paths: the container never
  fetches anything, so page content cannot leave the box and there is no SSRF surface.
OUTPUT standard OpenAI embeddings list. Vectors are L2-normalised. A truncated `dimensions`
  is RE-normalised after slicing (Matryoshka); skipping that silently degrades scores.
"""
import argparse, base64, binascii, io, os, re, threading, time

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from PIL import Image
from sentence_transformers import SentenceTransformer

MODEL_DIR = os.environ["MODEL_DIR"]
NAME = os.environ.get("SERVED_NAME", os.path.basename(MODEL_DIR))
VISION_TOKENS = int(os.environ.get("VISION_TOKENS", "280"))      # 70|140|280|560|1120
GPUFRAC = float(os.environ.get("GPUFRAC", "0.05"))
MATRYOSHKA = {768, 512, 256, 128}
MAX_ITEMS, MAX_IMAGES_PER_ITEM, MAX_PIXELS = 64, 8, 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8000)
ap.add_argument("--max-model-len", type=int, default=0)         # informational for fleet-ui
ARGS, _ = ap.parse_known_args()

if torch.cuda.is_available():
    # A hard in-process cap: on the unified-memory GB10 an over-allocation here must raise inside
    # this container, not push the whole box toward the OOM that memcheck.sh exists to prevent.
    torch.cuda.set_per_process_memory_fraction(GPUFRAC)
    DEVICE = "cuda"
else:
    DEVICE = "cpu"

t0 = time.time()
MODEL = SentenceTransformer(MODEL_DIR, device=DEVICE, model_kwargs={"torch_dtype": torch.bfloat16})
if ARGS.max_model_len:
    MODEL.max_seq_length = min(MODEL.max_seq_length or ARGS.max_model_len, ARGS.max_model_len)
LOCK = threading.Lock()                     # one GPU model, one encode at a time
DIM = MODEL.get_sentence_embedding_dimension()
IMG_KW = {"image": {"max_soft_tokens": VISION_TOKENS}}


def _decode_image(uri):
    if not isinstance(uri, str) or not uri.startswith("data:"):
        raise HTTPException(400, "images must be data: URIs (no http(s), no file paths)")
    try:
        raw = base64.b64decode(uri.split(",", 1)[1], validate=False)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except (IndexError, binascii.Error, OSError, Image.DecompressionBombError) as e:
        raise HTTPException(400, f"bad image: {type(e).__name__}")


def _prepare(item):
    """-> (is_multimodal, value for SentenceTransformer.encode, rough token count)"""
    if isinstance(item, str):
        return False, item, max(1, len(item) // 4)
    if not isinstance(item, dict) or not ({"text", "image"} & set(item)):
        raise HTTPException(400, "each input must be a string or an object with text and/or image")
    imgs = item.get("image")
    imgs = [] if imgs is None else imgs if isinstance(imgs, list) else [imgs]
    if len(imgs) > MAX_IMAGES_PER_ITEM:
        raise HTTPException(400, f"at most {MAX_IMAGES_PER_ITEM} images per input")
    pil = [_decode_image(u) for u in imgs]
    text = item.get("text")
    if text is None:
        value = {"image": pil[0] if len(pil) == 1 else pil}
    else:
        # the model wants one <|image|> placeholder per image; add the missing ones at the end
        missing = len(pil) - len(re.findall(r"<\|image\|>", text))
        if missing > 0:
            text = text + " <|image|>" * missing
        value = {"text": text, "image": pil}
    return True, value, (len(text or "") // 4) + len(pil) * VISION_TOKENS


def _encode(items):
    """Text-only inputs go in one batch, multimodal in another (they need processing_kwargs)."""
    prepared = [_prepare(i) for i in items]
    out = [None] * len(prepared)
    text_ix = [k for k, p in enumerate(prepared) if not p[0]]
    mm_ix = [k for k, p in enumerate(prepared) if p[0]]
    with LOCK, torch.inference_mode():
        if text_ix:
            vecs = MODEL.encode([prepared[k][1] for k in text_ix], batch_size=16,
                                convert_to_numpy=True, show_progress_bar=False)
            for k, v in zip(text_ix, vecs):
                out[k] = v
        if mm_ix:
            vecs = MODEL.encode([prepared[k][1] for k in mm_ix], batch_size=4,
                                convert_to_numpy=True, show_progress_bar=False,
                                processing_kwargs=IMG_KW)
            for k, v in zip(mm_ix, vecs):
                out[k] = v
    return np.asarray(out, dtype=np.float32), sum(p[2] for p in prepared)


def _norm(m):
    return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-12, None)


app = FastAPI(title="serve-mm-embed")


@app.get("/health")
def health():
    return {"status": "ok", "model": NAME, "dim": DIM, "device": DEVICE}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": NAME, "object": "model", "owned_by": "local"}]}


@app.post("/v1/embeddings")
def embeddings(req: dict):
    items = req.get("input")
    if isinstance(items, (str, dict)):
        items = [items]
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        raise HTTPException(400, f"input must be a non-empty list of at most {MAX_ITEMS} items")
    dims = req.get("dimensions")
    if dims is not None and dims not in MATRYOSHKA:
        raise HTTPException(400, f"dimensions must be one of {sorted(MATRYOSHKA, reverse=True)}")
    try:
        vecs, ntok = _encode(items)
    except HTTPException:
        raise
    except torch.cuda.OutOfMemoryError:
        raise HTTPException(503, "out of memory under the GPUFRAC cap; send fewer or smaller images")
    vecs = _norm(vecs)
    if dims and dims < vecs.shape[1]:
        vecs = _norm(vecs[:, :dims])                  # re-normalise after Matryoshka truncation
    b64 = req.get("encoding_format") == "base64"
    data = [{"object": "embedding", "index": i,
             "embedding": base64.b64encode(v.tobytes()).decode() if b64 else v.tolist()}
            for i, v in enumerate(vecs)]
    return JSONResponse({"object": "list", "data": data, "model": NAME,
                         "usage": {"prompt_tokens": ntok, "total_tokens": ntok}})


if __name__ == "__main__":
    # Warm both paths BEFORE serving so /health only goes green once text AND image work.
    _encode(["task: search result | query: warmup"])
    _encode([{"image": "data:image/png;base64," + base64.b64encode(
        (lambda b: (Image.new("RGB", (64, 64), (200, 30, 30)).save(b, "PNG"), b.getvalue())[1])(io.BytesIO())
    ).decode()}])
    print(f"serve-mm-embed: READY {NAME} dim={DIM} device={DEVICE} vision_tokens={VISION_TOKENS} "
          f"gpufrac={GPUFRAC} loaded_in={time.time() - t0:.0f}s", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=ARGS.port, log_level="warning")
