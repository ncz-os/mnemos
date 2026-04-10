#!/usr/bin/env python3
"""
Phi-3.5 Mini OpenVINO Inference Server
Ollama-compatible API running locally on PYTHIA.

Endpoints:
  POST /api/generate   — text completion (stream=false only)
  GET  /api/tags       — list available models (health check)
  GET  /health         — simple health check

Port: 11435 (distinct from CERBERUS Ollama at :11434)
Device: GPU (Intel Iris Xe via OpenVINO) with CPU fallback
"""
import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

import openvino_genai as ov_genai
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pipe
    logger.info(f"Loading Phi-3.5 Mini from {MODEL_PATH} on device={DEVICE}")
    t0 = time.time()
    try:
        _pipe = ov_genai.LLMPipeline(MODEL_PATH, DEVICE)
        logger.info(f"Model loaded in {time.time() - t0:.1f}s")
    except Exception as e:
        logger.warning(f"GPU load failed ({e}), falling back to CPU")
        _pipe = ov_genai.LLMPipeline(MODEL_PATH, "CPU")
        logger.info(f"Model loaded on CPU in {time.time() - t0:.1f}s")
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
    if _pipe is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    if request.stream:
        raise HTTPException(status_code=400, detail="Streaming not supported; use stream=false")

    opts = request.options or {}
    config = ov_genai.GenerationConfig()
    config.max_new_tokens = int(opts.get("num_predict", opts.get("max_new_tokens", 600)))
    config.temperature = float(opts.get("temperature", 0.1))
    if config.temperature < 0.05:
        config.do_sample = False
    else:
        config.do_sample = True
        config.top_p = float(opts.get("top_p", 0.9))

    # Build prompt — prepend system message if provided
    full_prompt = request.prompt
    if request.system:
        full_prompt = f"<|system|>\n{request.system}<|end|>\n<|user|>\n{request.prompt}<|end|>\n<|assistant|>\n"
    elif "<|user|>" not in request.prompt and "<|system|>" not in request.prompt:
        # Auto-wrap bare prompts in Phi-3.5 chat template
        full_prompt = f"<|user|>\n{request.prompt}<|end|>\n<|assistant|>\n"

    logger.info(f"Generating: {len(full_prompt)} chars prompt, max_tokens={config.max_new_tokens}")
    t0 = time.time()

    # Run inference in executor to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    response_text = await loop.run_in_executor(
        None, lambda: _pipe.generate(full_prompt, config)
    )

    elapsed_ns = int((time.time() - t0) * 1e9)
    tokens = len(response_text.split())
    logger.info(f"Generated {tokens} tokens in {elapsed_ns/1e9:.2f}s")

    return GenerateResponse(
        model=request.model,
        response=response_text,
        done=True,
        total_duration=elapsed_ns,
        eval_count=tokens,
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PHI_PORT", "11435"))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)  # single worker — model isn't fork-safe
