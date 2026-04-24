"""APOLLO — schema-aware dense encoding compression engine.

Part of the going-forward compression stack (LETHE + ANAMNESIS +
APOLLO). Designed for LLM-to-LLM wire use: encodes memories into
typed key:value dense forms that downstream LLMs parse natively in
fewer tokens than the prose equivalent.

v3.3 S-IC (landed):
  * APOLLOEngine under CompressionEngine ABC.
  * Schema registry pattern; PortfolioSchema the first concrete schema.
  * Rule-based detection + encoding — no LLM calls on the fast path.
  * Rule-based narration (S-II replaces with cached small-LLM readback).

v3.3 S-II (this slice):
  * ANAMNESIS-pattern LLM fallback (httpx against GPU_PROVIDER_HOST)
    for content that misses every registered schema. With fallback
    enabled (default), supports() is unconditionally True so the
    contest's audit log records APOLLO's decision on every memory.
  * Fallback dense form:
        summary=<one line>;facts=[f1|f2|...];
        entities=[e1|e2|...];concepts=[c1|c2|...]
    Strict parser rejects malformed output rather than shipping
    broken encodings downstream.
  * GPUGuard circuit integration — open circuit short-circuits to
    an error result, honest gpu_used flag on every path.

Still ahead (S-II tail and S-III):
  * GET /v1/memories/{id}/narrate endpoint with cached small-LLM
    readback.
  * Judge-LLM fidelity scoring replacing the heuristic quality
    score (fallback currently pins to 0.65 — see QUALITY SCORE
    block below).
  * Additional schemas: decision, person, event.

See ROADMAP.md "Apollo Program" for the full staged plan.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import List, Optional

import httpx

from .apollo_schemas import PortfolioSchema, Schema
from .base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)
from .gpu_guard import get_guard

logger = logging.getLogger(__name__)

# GPU provider endpoint (shared with ANAMNESIS via the same env vars —
# single GPU host runs both engines' fallback calls).
_GPU_PROVIDER_HOST = os.getenv("GPU_PROVIDER_HOST", "http://localhost")
_GPU_PROVIDER_PORT = os.getenv("GPU_PROVIDER_PORT", "8000")
_GPU_PROVIDER_TIMEOUT = float(os.getenv("GPU_PROVIDER_TIMEOUT", "30.0"))


# Default schema registry. Order matters: first match wins.
# Specific-before-general is the rule; additional schemas insert ahead
# of any future generic fallback schema.
_DEFAULT_SCHEMAS: List[Schema] = [
    PortfolioSchema(),
]


# LLM-fallback prompt. Designed for the ANAMNESIS-style output
# contract: one line, four required sections, pipe-separated lists.
# Strict form lets the parser validate and reject malformed returns.
_FALLBACK_PROMPT = """\
You are extracting structured facts from a memory for dense LLM-to-LLM encoding.
Read the content below and emit ONE line in this exact form:

summary=<one-line summary, max 100 chars>;facts=[fact1|fact2|...];entities=[name1|name2|...];concepts=[concept1|concept2|...]

Rules:
- All four sections required. Empty sections render as summary=;facts=[];entities=[];concepts=[]
- Use '|' as the in-list separator (never commas)
- No quotes around values
- No newlines, no markdown, no explanation — one line of output, exactly.
- Preserve proper nouns, IDs, numbers, and other identifiers verbatim.

Memory content:
{content}

Output (one line only):"""


# Strict parser. Accepts lines of the exact shape produced by
# _FALLBACK_PROMPT. Tolerates LLM preamble/suffix by scanning newlines.
_FALLBACK_RE = re.compile(
    r"^summary=(?P<summary>[^;]*);"
    r"facts=\[(?P<facts>[^\]]*)\];"
    r"entities=\[(?P<entities>[^\]]*)\];"
    r"concepts=\[(?P<concepts>[^\]]*)\]$"
)


# QUALITY SCORE for LLM-fallback results.
# The balanced contest profile uses a 0.70 quality floor; pinning
# fallback at 0.65 makes APOLLO lose to a schema-matching peer
# (confidence typically >0.80) and also lose to ANAMNESIS on memories
# where ANAMNESIS produces a cleaner extraction. The floor ensures
# fallback never wins by default when a better-shaped output exists.
# The judge-LLM scoring work in S-II tail replaces this pin with
# narrated-derivative-vs-root fidelity.
_FALLBACK_QUALITY_SCORE = 0.65


class APOLLOEngine(CompressionEngine):
    """APOLLO under the CompressionEngine ABC.

    Two-path compress():

      1. **Schema path (fast):** iterate registered schemas;
         first DetectionResult wins; encode via the schema. No LLM,
         no GPU, IdentifierPolicy.STRICT by construction. This is
         the v3.3 S-IC shape; it remains the preferred output
         whenever a schema matches.

      2. **LLM fallback (S-II):** when no schema matches and
         `enable_llm_fallback=True` (default), call the GPU provider
         with an ANAMNESIS-pattern prompt. Output is a strict
         one-line dense form (summary / facts / entities / concepts).
         Parser rejects malformed output rather than shipping broken
         encodings. gpu_used=True; IdentifierPolicy.OFF (LLM may
         paraphrase).

    supports() returns True when fallback is enabled. When an
    operator disables fallback (`enable_llm_fallback=False`),
    supports() falls back to the S-IC shape: True only if some
    registered schema matches.
    """

    id = "apollo"
    label = "APOLLO — schema-aware dense encoding"
    version = "0.2"
    gpu_intent = GPUIntent.GPU_OPTIONAL

    def __init__(
        self,
        schemas: Optional[List[Schema]] = None,
        *,
        enable_llm_fallback: bool = True,
        gpu_url: Optional[str] = None,
        timeout: float = _GPU_PROVIDER_TIMEOUT,
    ) -> None:
        super().__init__()
        self._schemas: List[Schema] = (
            schemas if schemas is not None else list(_DEFAULT_SCHEMAS)
        )
        self._enable_llm_fallback = enable_llm_fallback
        if gpu_url:
            self.gpu_url = gpu_url.rstrip("/")
        else:
            host = _GPU_PROVIDER_HOST.rstrip("/")
            port = _GPU_PROVIDER_PORT
            self.gpu_url = f"{host}:{port}"
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def supports(self, request: CompressionRequest) -> bool:
        """Eligible for every memory when LLM fallback is on.

        When fallback is off the engine falls back to the S-IC
        behavior: only memories that match a registered schema.
        """
        if self._enable_llm_fallback:
            return True
        content = request.content or ""
        return any(
            schema.detect(content) is not None for schema in self._schemas
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        """Release the http client. distillation_worker calls this on
        shutdown; tests call it between cases."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        started = time.perf_counter()
        content = request.content or ""
        original_tokens = len(content.split())

        # Schema fast path.
        for schema in self._schemas:
            match = schema.detect(content)
            if match is None:
                continue
            encoded = schema.encode(match)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            compressed_tokens = len(encoded.split())
            ratio = (
                compressed_tokens / original_tokens if original_tokens > 0 else 1.0
            )
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                compressed_content=encoded,
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                compression_ratio=ratio,
                quality_score=match.confidence,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={
                    "path": "schema",
                    "schema_id": match.schema_id,
                    "schema_version": match.schema_version,
                    "schema_confidence": match.confidence,
                    "schema_notes": match.notes,
                    "field_count": len(match.fields),
                },
            )

        # LLM fallback.
        if self._enable_llm_fallback:
            return await self._llm_fallback(
                request=request,
                content=content,
                original_tokens=original_tokens,
                started=started,
            )

        # Fallback disabled and no schema matched — clean error result.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            original_tokens=original_tokens,
            elapsed_ms=elapsed_ms,
            gpu_used=False,
            identifier_policy=IdentifierPolicy.STRICT,
            manifest={
                "path": "schema_only_no_match",
                "schemas_tried": [s.id for s in self._schemas],
            },
            error="no_schema_match",
        )

    # ── LLM fallback ────────────────────────────────────────────────────
    #
    # Split out for test isolation — tests that want to exercise the
    # fallback path directly monkeypatch _get_client to return a fake
    # httpx-like client, or replace _llm_fallback entirely.

    async def _llm_fallback(
        self,
        *,
        request: CompressionRequest,
        content: str,
        original_tokens: int,
        started: float,
    ) -> CompressionResult:
        """Invoke the GPU-backed LLM to extract facts when no schema matches.

        Short-circuits to error when the GPU circuit is open.
        """
        guard = get_guard(self.gpu_url)
        admitted, probe_token = await guard.is_available()
        if not admitted:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={
                    "path": "fallback",
                    "gpu_url": self.gpu_url,
                    "circuit_state": guard.state.value,
                    "circuit_last_error": guard.last_error,
                },
                error=f"gpu_guard circuit open for {self.gpu_url}",
            )

        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.gpu_url}/v1/completions",
                json={
                    "prompt": _FALLBACK_PROMPT.format(content=content[:4000]),
                    "max_tokens": 512,
                    "temperature": 0.1,
                    "top_p": 0.9,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
            raw = (
                payload.get("choices", [{}])[0].get("text", "")
                if isinstance(payload, dict) else ""
            ).strip()
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "[APOLLO fallback] LLM call failed for %s: %s",
                request.memory_id, exc,
            )
            await guard.record_failure(exc, probe_token=probe_token)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={"path": "fallback", "gpu_url": self.gpu_url},
                error=f"{type(exc).__name__}: {exc}",
            )

        # HTTP 2xx received — signal success to the guard so a probe
        # coming out of HALF_OPEN closes the breaker. Parse failure is
        # a prompt/model issue, not a GPU-health issue, so we report
        # success even when parsing fails.
        await guard.record_success(probe_token=probe_token)

        encoded = _normalize_fallback_output(raw)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if encoded is None:
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=True,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={
                    "path": "fallback",
                    "gpu_url": self.gpu_url,
                    "raw_output_preview": raw[:200],
                },
                error="fallback_parse_failed",
            )

        compressed_tokens = len(encoded.split())
        ratio = (
            compressed_tokens / original_tokens if original_tokens > 0 else 1.0
        )
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            compressed_content=encoded,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=ratio,
            quality_score=_FALLBACK_QUALITY_SCORE,
            elapsed_ms=elapsed_ms,
            gpu_used=True,
            identifier_policy=IdentifierPolicy.OFF,
            manifest={
                "path": "fallback",
                "gpu_url": self.gpu_url,
                "output_shape": "summary;facts;entities;concepts",
            },
        )


def _normalize_fallback_output(raw: str) -> Optional[str]:
    """Strip LLM output and verify it matches the expected shape.

    Returns the sanitized one-line encoded string or None on parse
    failure. Tolerates preamble/suffix by scanning lines for the first
    match.
    """
    if not raw:
        return None
    for candidate in (line.strip() for line in raw.splitlines() or [raw]):
        if _FALLBACK_RE.match(candidate):
            return candidate
    return None
