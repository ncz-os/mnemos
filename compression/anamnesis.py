#!/usr/bin/env python3
"""
ANAMNESIS: LLM fact extraction for long-term archival (Tier 3)

Named for Platonic recollection — recovering what must not be forgotten.
Extracts atomic facts from memories >30 days old via LLM on GPU provider.

Pattern: Mem0-style semantic chunking + fact extraction.
Performance: 500ms-2s per memory (offline batch via distillation worker)
Output: Compact JSON array of atomic facts + structured fields

Recommended: Local GPU (vLLM/Ollama). Fallback: Skip extraction if unreachable (non-critical).
"""

import asyncio
import json
import logging
import os
import time
from typing import Dict, List, Optional

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

# GPU provider endpoint
_GPU_PROVIDER_HOST = os.getenv("GPU_PROVIDER_HOST", "http://localhost")
_GPU_PROVIDER_PORT = os.getenv("GPU_PROVIDER_PORT", "8000")
_GPU_PROVIDER_TIMEOUT = float(os.getenv("GPU_PROVIDER_TIMEOUT", "30.0"))


class ANAMNESIS:
    """LLM-based fact extraction for archival via GPU provider."""

    def __init__(self, gpu_url: Optional[str] = None, timeout: float = _GPU_PROVIDER_TIMEOUT):
        """
        Initialize ANAMNESIS extractor.

        Args:
            gpu_url: GPU provider inference endpoint
            timeout: Request timeout in seconds
        """
        if gpu_url:
            self.gpu_url = gpu_url.rstrip("/")
        else:
            host = _GPU_PROVIDER_HOST.rstrip("/")
            port = _GPU_PROVIDER_PORT
            self.gpu_url = f"{host}:{port}"
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def extract_facts(
        self, text: str, memory_id: str, category: str = "facts"
    ) -> Dict:
        """
        Extract atomic facts from memory for archival.

        Args:
            text: Memory content to extract from
            memory_id: Memory ID (for tracing)
            category: Memory category (affects extraction strategy)

        Returns:
            {
                'facts': List[str],  # Atomic facts
                'entities': List[str],  # Named entities
                'concepts': List[str],  # Key concepts
                'summary': str,  # One-line summary
                'extraction_method': str,
                'error': Optional[str]
            }
        """
        if not text or len(text) < 50:
            return {
                "facts": [],
                "entities": [],
                "concepts": [],
                "summary": text[:100],
                "extraction_method": "none",
                "error": "Text too short",
            }

        try:
            client = await self._get_client()

            # Build extraction prompt (category-aware)
            prompt = self._build_extraction_prompt(text, category)

            # Query GPU provider
            response = await client.post(
                f"{self.gpu_url}/v1/completions",
                json={
                    "prompt": prompt,
                    "max_tokens": 1000,
                    "temperature": 0.1,
                    "top_p": 0.9,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()

            result = response.json()
            extraction_response = result.get("choices", [{}])[0].get("text", "").strip()

            # Parse extracted facts
            parsed = self._parse_extraction_response(extraction_response)

            return {
                "facts": parsed.get("facts", []),
                "entities": parsed.get("entities", []),
                "concepts": parsed.get("concepts", []),
                "summary": parsed.get("summary", text[:100]),
                "extraction_method": "anamnesis",
                "error": None,
            }

        except Exception as e:
            logger.warning(f"[ANAMNESIS] Fact extraction failed for {memory_id}: {e}")
            return {
                "facts": [],
                "entities": [],
                "concepts": [],
                "summary": text[:100],
                "extraction_method": "anamnesis",
                "error": str(e),
            }

    def _build_extraction_prompt(self, text: str, category: str) -> str:
        """Build category-aware fact extraction prompt."""
        category_guidance = {
            "solutions": "Extract key solutions, techniques, and implementation details.",
            "patterns": "Extract architectural patterns, design principles, and abstractions.",
            "decisions": "Extract decision rationale, alternatives considered, and tradeoffs.",
            "infrastructure": "Extract system specifications, configurations, and endpoints.",
            "projects": "Extract project goals, deliverables, milestones, and owners.",
            "facts": "Extract general factual statements, definitions, and relationships.",
        }

        guidance = category_guidance.get(category, category_guidance["facts"])

        return f"""Extract atomic facts from this memory for long-term archival.

Category: {category}
Guidance: {guidance}

Format output as JSON with these keys:
- facts: List of 1-2 sentence atomic facts (max 10)
- entities: Named entities, people, systems, tools (max 5)
- concepts: Key concepts or themes (max 5)
- summary: One-line summary

Memory:
{text[:1000]}

Output ONLY valid JSON, no extra text:
"""

    def _parse_extraction_response(self, response: str) -> Dict:
        """Parse GPU provider's fact extraction response."""
        try:
            # Extract JSON from response (handle markdown code blocks)
            json_str = response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0]

            parsed = json.loads(json_str.strip())
            return {
                "facts": parsed.get("facts", [])[:10],
                "entities": parsed.get("entities", [])[:5],
                "concepts": parsed.get("concepts", [])[:5],
                "summary": parsed.get("summary", "")[:200],
            }
        except Exception as e:
            logger.warning(f"[ANAMNESIS] Failed to parse extraction response: {e}")
            return {
                "facts": [],
                "entities": [],
                "concepts": [],
                "summary": "",
            }

    async def batch_extract(self, memories: List[Dict]) -> List[Dict]:
        """
        Extract facts from multiple memories (for distillation worker).

        Args:
            memories: List of {id, content, category} dicts

        Returns:
            List of extraction results with memory IDs
        """
        tasks = [
            self.extract_facts(m["content"], m.get("id", ""), m.get("category", "facts"))
            for m in memories
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = []
        for i, (memory, result) in enumerate(zip(memories, results)):
            if isinstance(result, Exception):
                output.append(
                    {
                        "memory_id": memory.get("id"),
                        "error": str(result),
                    }
                )
            else:
                output.append(
                    {
                        "memory_id": memory.get("id"),
                        **result,
                    }
                )
        return output

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


def _render_extraction(facts: List[str], summary: str) -> str:
    """Render a succinct text artifact from the structured extraction.

    compressed_content for the contest needs to be text the downstream
    reader consumes — not JSON, not a Python dict. Summary plus a
    bulleted fact list is short, legible, and competes cleanly with
    LETHE/ALETHEIA prose outputs. Entities and concepts stay in
    manifest since they're retrieval/indexing metadata, not reading
    content.
    """
    lines: List[str] = []
    if summary:
        lines.append(summary.strip())
    if facts:
        if lines:
            lines.append("")
        lines.extend(f"- {f.strip()}" for f in facts if f and f.strip())
    return "\n".join(lines)


def _self_score(facts: List[str], entities: List[str], concepts: List[str], summary: str) -> float:
    """Self-assessed quality score for an ANAMNESIS extraction.

    Rough heuristic — the judge LLM (Task #8 benchmark, v3.2 contest
    scoring) replaces this with a real fidelity measurement. Until then
    engines need SOME number so the composite scorer can rank them.

    Scale:
      0.85  — all four fields populated (summary + facts + entities + concepts)
      0.70  — summary + facts but nothing else
      0.50  — summary only (extraction mostly failed, but we got a one-liner)
      0.30  — nothing populated (we shouldn't be here; succeeded() would be False)
    """
    has_summary = bool(summary and summary.strip())
    has_facts = any(bool(f and f.strip()) for f in facts)
    has_entities = any(bool(e and e.strip()) for e in entities)
    has_concepts = any(bool(c and c.strip()) for c in concepts)

    if has_summary and has_facts and (has_entities or has_concepts):
        return 0.85
    if has_summary and has_facts:
        return 0.70
    if has_summary:
        return 0.50
    return 0.30


class ANAMNESISEngine(CompressionEngine):
    """ANAMNESIS under the v3.1 CompressionEngine ABC.

    Composes the async ANAMNESIS fact extractor and adapts its
    structured output into the contest's text-shaped compressed_content
    contract. Summary + bulleted facts land in compressed_content (what
    the downstream consumer reads instead of the original); entities /
    concepts / raw dict stay in manifest for indexing and audit.

    Identifier-preservation: an LLM extraction pass paraphrases freely,
    so the honest report is IdentifierPolicy.OFF regardless of what
    the request asked for.

    gpu_intent=GPU_REQUIRED: ANAMNESIS has no CPU path. Task #6 GPU
    batcher pre-checks endpoint availability and skips gpu_required
    engines with reject_reason='disabled' when the endpoint is
    unreachable. Until the batcher lands, a GPU outage surfaces as an
    error result and the contest records reject_reason='error'.

    Category hint: ANAMNESIS's extraction prompt is category-aware
    (solutions / patterns / decisions / infrastructure / projects /
    facts) with per-category guidance. The engine pulls the category
    from request.task_type if present, then from request.metadata.
    get('category'), then defaults to 'facts'.
    """

    id = "anamnesis"
    label = "ANAMNESIS — LLM fact extraction (GPU)"
    version = "1.0"
    gpu_intent = GPUIntent.GPU_REQUIRED

    _CATEGORY_KEYS = (
        "solutions",
        "patterns",
        "decisions",
        "infrastructure",
        "projects",
        "facts",
    )

    def __init__(
        self,
        gpu_url: Optional[str] = None,
        timeout: float = _GPU_PROVIDER_TIMEOUT,
        core: Optional[ANAMNESIS] = None,
    ) -> None:
        super().__init__()
        self._core = core or ANAMNESIS(gpu_url=gpu_url, timeout=timeout)

    def _resolve_category(self, request: CompressionRequest) -> str:
        for candidate in (
            request.task_type,
            request.metadata.get("category") if request.metadata else None,
        ):
            if candidate and candidate in self._CATEGORY_KEYS:
                return candidate
        return "facts"

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        started = time.perf_counter()
        category = self._resolve_category(request)
        original_tokens = len(request.content.split())
        guard = get_guard(self._core.gpu_url)
        if not await guard.is_available():
            elapsed = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={
                    "category": category,
                    "gpu_url": self._core.gpu_url,
                    "circuit_state": guard.state.value,
                    "circuit_last_error": guard.last_error,
                },
                error=f"gpu_guard circuit open for {self._core.gpu_url}",
            )
        try:
            core_out = await self._core.extract_facts(
                text=request.content,
                memory_id=request.memory_id,
                category=category,
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "ANAMNESISEngine.compress raised for %s", request.memory_id
            )
            await guard.record_failure(exc)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={"category": category, "gpu_url": self._core.gpu_url},
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        err = core_out.get("error")
        facts = list(core_out.get("facts") or [])
        entities = list(core_out.get("entities") or [])
        concepts = list(core_out.get("concepts") or [])
        summary = core_out.get("summary") or ""

        base_manifest = {
            "category": category,
            "extraction_method": core_out.get("extraction_method"),
            "gpu_url": self._core.gpu_url,
            "facts": facts,
            "entities": entities,
            "concepts": concepts,
            "summary": summary,
        }

        if err is not None:
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest=base_manifest,
                error=err,
            )

        rendered = _render_extraction(facts, summary)
        if not rendered:
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=True,
                identifier_policy=IdentifierPolicy.OFF,
                manifest=base_manifest,
                error="empty extraction (no summary and no facts)",
            )

        compressed_tokens = len(rendered.split())
        compression_ratio = (
            compressed_tokens / original_tokens if original_tokens > 0 else 1.0
        )
        quality = _self_score(facts, entities, concepts, summary)

        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compressed_content=rendered,
            compression_ratio=compression_ratio,
            quality_score=quality,
            elapsed_ms=elapsed_ms,
            judge_model=None,
            gpu_used=True,
            identifier_policy=IdentifierPolicy.OFF,
            manifest=base_manifest,
        )

    async def close(self) -> None:
        await self._core.close()
