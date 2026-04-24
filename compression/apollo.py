"""APOLLO — schema-aware dense encoding compression engine (v3.3 S-IC).

Part of the going-forward compression stack (LETHE + ANAMNESIS +
APOLLO). Designed for LLM-to-LLM wire use: encodes memories into
typed key:value dense forms that downstream LLMs parse natively in
fewer tokens than the prose equivalent.

v3.3 S-IC (this slice):
  * APOLLOEngine under CompressionEngine ABC.
  * Schema registry pattern; PortfolioSchema is the first concrete
    schema.
  * Rule-based detection + encoding — no LLM calls.
  * Rule-based narration (Schema.narrate default or override); S-II
    replaces with a cached small-LLM readback.
  * supports() returns False when no schema matches, so the contest
    skips APOLLO rather than recording a predictable loss.

v3.3 S-II follow-ups (scheduled):
  * ANAMNESIS-pattern LLM fallback for fact-shaped content that
    misses every registered schema (APOLLO then supports all
    memories, not just schema-matching ones).
  * GET /v1/memories/{id}/narrate endpoint, cached.
  * Judge-LLM fidelity scoring replaces the self-reported
    confidence as quality_score.
  * Additional schemas: decision, person, event.

See ROADMAP.md "Apollo Program" for the full three-stage plan.
"""
from __future__ import annotations

import time
from typing import List, Optional

from .apollo_schemas import PortfolioSchema, Schema
from .base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)


# Default schema registry. Order matters: first match wins. When
# additional schemas land, specific-before-general is the rule —
# portfolio before decision before generic fact extraction.
_DEFAULT_SCHEMAS: List[Schema] = [
    PortfolioSchema(),
]


class APOLLOEngine(CompressionEngine):
    """APOLLO under the CompressionEngine ABC.

    Fast path: iterate the registered schemas in order; first
    DetectionResult wins; return the schema's encoded dense form.
    supports() skips APOLLO entirely when no schema matches, so the
    contest's audit log stays clean (no 'no_schema_match' loser
    rows for every non-portfolio memory).

    When S-II adds ANAMNESIS-pattern LLM fallback, supports() will
    always return True; the fallback runs in compress() after the
    schema loop exits without a match.

    Identifier preservation: STRICT by construction — schemas
    transcribe exact tickers / IDs / identifiers into the encoded
    form without paraphrase.

    gpu_intent=GPU_OPTIONAL: the schema-match fast path is pure
    regex (no GPU). S-II's LLM fallback uses the GPU host when
    present, degrades to CPU when not.
    """

    id = "apollo"
    label = "APOLLO — schema-aware dense encoding"
    version = "0.1"
    gpu_intent = GPUIntent.GPU_OPTIONAL

    def __init__(self, schemas: Optional[List[Schema]] = None) -> None:
        super().__init__()
        self._schemas: List[Schema] = (
            schemas if schemas is not None else list(_DEFAULT_SCHEMAS)
        )

    def supports(self, request: CompressionRequest) -> bool:
        """Eligible only when some registered schema matches.

        This is the S-IC behavior. S-II's LLM fallback lifts this to
        unconditionally True; the fallback takes over when the
        schema loop in compress() exits without a match.
        """
        content = request.content or ""
        return any(schema.detect(content) is not None for schema in self._schemas)

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        started = time.perf_counter()
        content = request.content or ""
        original_tokens = len(content.split())

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
                quality_score=match.confidence,  # judge-LLM replaces in S-II
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={
                    "schema_id": match.schema_id,
                    "schema_version": match.schema_version,
                    "schema_confidence": match.confidence,
                    "schema_notes": match.notes,
                    "field_count": len(match.fields),
                },
            )

        # Defense-in-depth: supports() already filters this case out
        # of the contest, but if an operator calls compress() directly
        # (or registers APOLLO via a non-standard path) we still want
        # a clean error result instead of raising.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            original_tokens=original_tokens,
            elapsed_ms=elapsed_ms,
            gpu_used=False,
            identifier_policy=IdentifierPolicy.STRICT,
            manifest={
                "schemas_tried": [s.id for s in self._schemas],
            },
            error="no_schema_match",
        )
