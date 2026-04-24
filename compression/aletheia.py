#!/usr/bin/env python3
"""
ALETHEIA: Token-level LLM compression via GPU (Tier 2)

Named for "un-concealment / disclosure" — reveals the essential truth through LLM analysis.
Uses LLMLingua-2-style token-level importance scoring via OpenAI-compatible local/remote GPU.

Performance: 200-500ms per compression (batch mode), 95% quality, high reduction.
Runs offline via distillation worker; not real-time.

Recommended: Local GPU (vLLM/Ollama on host or LAN). Fallback: LETHE (CPU) if GPU unreachable.
"""

import logging
import os
import time
from typing import Dict, Optional

import httpx

from .base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)
from .gpu_guard import get_guard

logger = logging.getLogger(__name__)

# GPU provider endpoint (vLLM, Ollama, or compatible OpenAI API)
_GPU_PROVIDER_HOST = os.getenv("GPU_PROVIDER_HOST", "http://localhost")
_GPU_PROVIDER_PORT = os.getenv("GPU_PROVIDER_PORT", "8000")
_GPU_PROVIDER_TIMEOUT = float(os.getenv("GPU_PROVIDER_TIMEOUT", "30.0"))

# Fallback to CPU LETHE if GPU unavailable
_FALLBACK_TO_LETHE = os.getenv("ALETHEIA_FALLBACK_LETHE", "true").lower() == "true"


class ALETHEIA:
    """GPU-based token-level compression via LLMLingua-2 (OpenAI-compatible endpoint)."""

    def __init__(
        self,
        gpu_url: Optional[str] = None,
        timeout: float = _GPU_PROVIDER_TIMEOUT,
        disable_fallback: bool = False,
    ):
        """
        Initialize ALETHEIA compressor.

        Args:
            gpu_url: GPU provider inference endpoint (default: env GPU_PROVIDER_HOST:PORT)
            timeout: Request timeout in seconds
            disable_fallback: When True, GPU failure returns an error result
                rather than silently falling back to LETHE. ALETHEIAEngine
                in the v3.1 competitive-selection path sets this True so
                LETHE's contest entry isn't shadowed by ALETHEIA's fallback.
        """
        if gpu_url:
            self.gpu_url = gpu_url.rstrip("/")
        else:
            host = _GPU_PROVIDER_HOST.rstrip("/")
            port = _GPU_PROVIDER_PORT
            self.gpu_url = f"{host}:{port}"
        self.timeout = timeout
        self.disable_fallback = disable_fallback
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def compress(self, text: str, target_ratio: float = 0.3) -> Dict:
        """
        Compress text using LLMLingua-2 via GPU provider.

        Args:
            text: Input text to compress
            target_ratio: Target compression ratio (0.3 = 30% of original tokens)

        Returns:
            {
                'original_tokens': int,
                'compressed_tokens': int,
                'compression_ratio': float,
                'compression_percentage': float,
                'compressed_text': str,
                'quality_score': float,
                'method': str,
                'error': Optional[str]
            }
        """
        if not text or len(text) < 20:
            return {
                "original_tokens": len(text.split()),
                "compressed_tokens": len(text.split()),
                "compression_ratio": 1.0,
                "compression_percentage": 0.0,
                "compressed_text": text,
                "quality_score": 1.0,
                "method": "none",
                "error": None,
            }

        try:
            client = await self._get_client()

            # Query GPU provider for token importance scoring
            prompt = self._build_scoring_prompt(text, target_ratio)
            response = await client.post(
                f"{self.gpu_url}/v1/completions",
                json={
                    "prompt": prompt,
                    "max_tokens": 500,
                    "temperature": 0.1,
                    "top_p": 0.9,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()

            result = response.json()
            scoring_response = result.get("choices", [{}])[0].get("text", "").strip()

            # Parse GPU provider's token importance scores
            compressed_text, token_indices, used_fallback = self._parse_compressed_tokens(
                text, scoring_response
            )

            # Calculate metrics
            original_tokens = len(text.split())
            compressed_tokens = len(compressed_text.split())
            compression_ratio = compressed_tokens / original_tokens if original_tokens > 0 else 1.0

            # Honest quality reporting: 0.95 for real LLM-scored importance;
            # 0.60 when we fell back to first-N tokens because the model's
            # response was unparseable. A quality-first scoring profile
            # should see the fallback penalty clearly; balanced will mostly
            # still value the speed/ratio advantage over the cost.
            quality_score = 0.60 if used_fallback else 0.95
            method = "aletheia_parse_fallback" if used_fallback else "aletheia"

            return {
                "original_tokens": original_tokens,
                "compressed_tokens": compressed_tokens,
                "compression_ratio": compression_ratio,
                "compression_percentage": (1.0 - compression_ratio) * 100,
                "compressed_text": compressed_text,
                "quality_score": quality_score,
                "method": method,
                "error": None,
            }

        except Exception as e:
            logger.error(f"[ALETHEIA] GPU compression failed: {e}")
            if _FALLBACK_TO_LETHE and not self.disable_fallback:
                logger.info("[ALETHEIA] Falling back to LETHE (CPU)")
                from .lethe import LETHE
                lethe = LETHE(mode="token")
                result = lethe.compress(text, target_ratio)
                result["method"] = "aletheia_fallback_lethe"
                return result
            else:
                return {
                    "original_tokens": len(text.split()),
                    "compressed_tokens": len(text.split()),
                    "compression_ratio": 1.0,
                    "compression_percentage": 0.0,
                    "compressed_text": text,
                    "quality_score": 0.5,
                    "method": "aletheia",
                    "error": str(e),
                }

    def _build_scoring_prompt(self, text: str, target_ratio: float) -> str:
        """Build prompt for GPU provider to score token importance."""
        target_count = max(5, int(len(text.split()) * target_ratio))
        return f"""Score the importance of each token in this text for compression to {target_count} tokens.
Preserve critical information: names, numbers, verbs, key nouns.
Remove: articles, prepositions, conjunctions, repetition.

Text:
{text[:1000]}

Output format: comma-separated list of token indices to KEEP (0-indexed).
Only output the indices, no explanation.
"""

    def _parse_compressed_tokens(self, text: str, scoring_response: str) -> tuple:
        """Parse GPU provider's token importance response.

        Returns (compressed_text, indices, used_fallback). used_fallback is
        True when the parser couldn't recover valid token indices from the
        model's output — an exception during parsing, OR a successful
        parse that produced zero valid indices (which happens when the
        model returns whitespace, punctuation, or off-spec text, as
        Qwen2.5-Coder routinely does against ALETHEIA's index-list
        prompt). The caller uses this flag to report an honest
        quality_score and method label.
        """
        tokens = text.split()
        try:
            # Parse comma-separated indices
            indices = [int(x.strip()) for x in scoring_response.split(",") if x.strip().isdigit()]
            indices = [i for i in indices if i < len(tokens)]
            if not indices:
                # Parse "succeeded" but yielded no valid indices — treat
                # as parse failure so we hit the first-N fallback below.
                # Before this check the engine silently returned empty
                # content with quality_score=0.95, which live testing
                # exposed as an adapter/contest-visible degenerate.
                raise ValueError("no valid token indices in scoring response")
            indices.sort()

            selected_tokens = [tokens[i] for i in indices]
            compressed_text = " ".join(selected_tokens)
            return compressed_text, indices, False
        except Exception as e:
            logger.warning(f"[ALETHEIA] Failed to parse scoring response: {e}")
            # Fallback: return first N tokens
            target_count = max(5, int(len(tokens) * 0.3))
            return " ".join(tokens[:target_count]), list(range(target_count)), True

    async def health_check(self) -> bool:
        """Check if GPU provider is reachable."""
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.gpu_url}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Clean up HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class ALETHEIAEngine(CompressionEngine):
    """ALETHEIA under the v3.1 CompressionEngine ABC.

    Composes the async ALETHEIA HTTP client with disable_fallback=True
    so a GPU outage becomes an honest error result rather than a
    silent LETHE output. LETHE runs as its own peer in the contest;
    letting ALETHEIA fall back to LETHE would shadow LETHE's entry and
    distort the audit log.

    Identifier-preservation: the current ALETHEIA implementation relies
    on a small-LLM importance-score pass, which paraphrases tokens
    freely. Honest reported policy is IdentifierPolicy.OFF — the
    judge can penalize policy mismatch when the request asks for
    STRICT.

    gpu_intent=GPU_REQUIRED: this engine has no self-contained CPU
    path. Task #6's gpu_batcher pre-checks endpoint availability and
    skips gpu_required engines with reject_reason='disabled' when the
    GPU endpoint is unreachable. Until the batcher lands, ALETHEIA
    returns an error result in the GPU-down case and the contest
    records it with reject_reason='error'.
    """

    id = "aletheia"
    label = "ALETHEIA — LLM-assisted token compression (GPU)"
    version = "1.0"
    gpu_intent = GPUIntent.GPU_REQUIRED

    def __init__(
        self,
        gpu_url: Optional[str] = None,
        timeout: float = _GPU_PROVIDER_TIMEOUT,
        core: Optional[ALETHEIA] = None,
    ) -> None:
        super().__init__()
        self._core = core or ALETHEIA(
            gpu_url=gpu_url,
            timeout=timeout,
            disable_fallback=True,
        )

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        started = time.perf_counter()
        guard = get_guard(self._core.gpu_url)
        admitted, probe_token = await guard.is_available()
        if not admitted:
            elapsed = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=len(request.content.split()),
                elapsed_ms=elapsed,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={
                    "gpu_url": self._core.gpu_url,
                    "circuit_state": guard.state.value,
                    "circuit_last_error": guard.last_error,
                },
                error=f"gpu_guard circuit open for {self._core.gpu_url}",
            )

        try:
            core_out = await self._core.compress(
                request.content,
                target_ratio=request.target_ratio,
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.exception("ALETHEIAEngine.compress raised for %s", request.memory_id)
            await guard.record_failure(exc, probe_token=probe_token)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=len(request.content.split()),
                elapsed_ms=elapsed,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        err = core_out.get("error")
        if err is not None:
            await guard.record_failure(None, probe_token=probe_token)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=core_out.get("original_tokens", 0),
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={"method": core_out.get("method"), "gpu_url": self._core.gpu_url},
                error=err,
            )

        await guard.record_success(probe_token=probe_token)
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            original_tokens=core_out["original_tokens"],
            compressed_tokens=core_out["compressed_tokens"],
            compressed_content=core_out["compressed_text"],
            compression_ratio=core_out["compression_ratio"],
            quality_score=core_out["quality_score"],
            elapsed_ms=elapsed_ms,
            judge_model=None,
            gpu_used=True,
            identifier_policy=IdentifierPolicy.OFF,
            manifest={
                "method": core_out.get("method"),
                "gpu_url": self._core.gpu_url,
                "compression_percentage": core_out.get("compression_percentage"),
            },
        )

    async def close(self) -> None:
        await self._core.close()
