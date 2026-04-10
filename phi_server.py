#!/usr/bin/env python3
"""
Phi-3.5 Mini OpenVINO Inference Server
Ollama-compatible API running locally on api-host.

Endpoints:
  POST /api/generate   — text completion (stream=false only)
  GET  /api/tags       — list available models (health check)
  GET  /health         — simple health check

Port: 11435 (distinct from inference-server Ollama at :11434)
Device: GPU (Intel Iris Xe via OpenVINO) with CPU fallback
"""
import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
import openvino_genai as ov_genai
from fastembed import TextEmbedding
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger("phi-server")

MODEL_PATH = os.getenv(
    "PHI_MODEL_PATH",
    "/opt/mnemos/models/phi-3.5-mini-int4-ov",
)
DEVICE = os.getenv("PHI_DEVICE", "GPU")   # GPU = Intel Iris Xe; CPU = fallback

_pipe: Optional[ov_genai.LLMPipeline] = None
_inference_lock: Optional[asyncio.Lock] = None   # created in lifespan; LLMPipeline is not concurrent-safe
_embed_model: Optional[TextEmbedding] = None     # fastembed nomic-embed-text-v1.5, CPU, 768-dim


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pipe, _inference_lock
    _inference_lock = asyncio.Lock()
    logger.info(f"Loading Phi-3.5 Mini from {MODEL_PATH} on device={DEVICE}")
    t0 = time.time()
    try:
        _pipe = ov_genai.LLMPipeline(MODEL_PATH, DEVICE)
        logger.info(f"Model loaded in {time.time() - t0:.1f}s")
    except Exception as e:
        logger.warning(f"GPU load failed ({e}), falling back to CPU")
        _pipe = ov_genai.LLMPipeline(MODEL_PATH, "CPU")
        logger.info(f"Model loaded on CPU in {time.time() - t0:.1f}s")
    # Load embedding model (CPU, runs independently of GPU LLM)
    global _embed_model
    logger.info("Loading nomic-embed-text-v1.5 embedding model (CPU)")
    t1 = time.time()
    _embed_model = TextEmbedding("nomic-ai/nomic-embed-text-v1.5")
    logger.info(f"Embedding model ready in {time.time() - t1:.1f}s")
    yield
    logger.info("Phi server shutting down")


app = FastAPI(title="Phi-3.5 OpenVINO Server", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Models ────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    model: str = "phi-3.5-mini"
    prompt: str
    stream: bool = False
    system: Optional[str] = None
    options: Optional[dict] = None


class GenerateResponse(BaseModel):
    model: str
    response: str
    done: bool = True
    total_duration: Optional[int] = None   # nanoseconds (Ollama compat)
    eval_count: Optional[int] = None


# OpenAI-compatible completions (used by distillation_worker.py)
class CompletionRequest(BaseModel):
    model: str = "phi-3.5-mini"
    prompt: str
    max_tokens: Optional[int] = None
    temperature: float = 0.1
    top_p: float = 0.9
    stream: bool = False
    options: Optional[dict] = None   # absorb Ollama-style options if passed


class CompletionChoice(BaseModel):
    text: str
    index: int = 0
    finish_reason: str = "stop"


class CompletionResponse(BaseModel):
    id: str = "cmpl-phi"
    object: str = "text_completion"
    model: str
    choices: list[CompletionChoice]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "model_loaded": _pipe is not None, "device": DEVICE}


@app.get("/api/tags")
async def list_models():
    """Ollama-compatible model listing."""
    return {
        "models": [
            {
                "name": "phi-3.5-mini",
                "model": "phi-3.5-mini",
                "size": 1_900_000_000,
                "details": {"family": "phi", "parameter_size": "3.8B", "quantization_level": "INT4"},
            }
        ]
    }


@app.post("/api/generate", response_model=GenerateResponse)
async def generate(request: GenerateRequest):
    """Ollama-compatible generation endpoint."""
    if request.stream:
        raise HTTPException(status_code=400, detail="Streaming not supported; use stream=false")
    opts = request.options or {}
    max_tokens = int(opts.get("num_predict", opts.get("max_new_tokens", 600)))
    temperature = float(opts.get("temperature", 0.1))
    top_p = float(opts.get("top_p", 0.9))

    # Apply system message if provided
    prompt = request.prompt
    if request.system:
        prompt = f"<|system|>\n{request.system}<|end|>\n<|user|>\n{prompt}<|end|>\n<|assistant|>\n"

    text, elapsed_ns, tokens = await _run_inference(prompt, max_tokens, temperature, top_p)
    logger.info(f"Generated {tokens} tokens in {elapsed_ns/1e9:.2f}s")
    return GenerateResponse(
        model=request.model,
        response=text,
        done=True,
        total_duration=elapsed_ns,
        eval_count=tokens,
    )


async def _run_inference(prompt: str, max_tokens: int, temperature: float, top_p: float) -> tuple[str, int, int]:
    """Shared inference helper. Returns (response_text, elapsed_ns, token_count)."""
    if _pipe is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    config = ov_genai.GenerationConfig()
    config.max_new_tokens = max_tokens
    config.temperature = temperature
    if temperature < 0.05:
        config.do_sample = False
    else:
        config.do_sample = True
        config.top_p = top_p

    # Auto-wrap bare prompts in Phi-3.5 chat template
    if "<|user|>" not in prompt and "<|system|>" not in prompt:
        prompt = f"<|user|>\n{prompt}<|end|>\n<|assistant|>\n"

    logger.info(f"Generating: {len(prompt)} chars, max_tokens={max_tokens}")
    t0 = time.time()
    loop = asyncio.get_running_loop()
    async with _inference_lock:  # type: ignore[union-attr]  # serialize: LLMPipeline cannot handle concurrent calls
        text = await loop.run_in_executor(None, lambda: _pipe.generate(prompt, config))
    elapsed_ns = int((time.time() - t0) * 1e9)
    return text, elapsed_ns, len(text.split())


@app.post("/v1/completions", response_model=CompletionResponse)
async def openai_completions(request: CompletionRequest):
    """OpenAI-compatible completions endpoint (used by distillation_worker.py)."""
    opts = request.options or {}
    max_tokens = request.max_tokens or int(opts.get("num_predict", 600))
    temperature = float(opts.get("temperature", request.temperature))
    top_p = float(opts.get("top_p", request.top_p))

    text, _, _ = await _run_inference(request.prompt, max_tokens, temperature, top_p)
    return CompletionResponse(
        model=request.model,
        choices=[CompletionChoice(text=text)],
    )


class EmbedRequest(BaseModel):
    model: str = "nomic-embed-text"
    prompt: str


@app.post("/api/embeddings")
async def embeddings(request: EmbedRequest):
    """Ollama-compatible embeddings endpoint using fastembed nomic-embed-text-v1.5."""
    if _embed_model is None:
        raise HTTPException(status_code=503, detail="Embedding model not loaded")
    loop = asyncio.get_running_loop()
    vec = await loop.run_in_executor(
        None,
        lambda: list(_embed_model.embed([request.prompt]))[0].tolist(),
    )
    return {"embedding": vec}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PHI_PORT", "11435"))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)  # single worker — model isn't fork-safe
