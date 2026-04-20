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
from typing import Dict, List, Optional

import httpx

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
