"""
MNEMOS local inference backend — unified interface for background distillation.

Selects the backend via DISTILLATION_BACKEND env var:
  ollama    (default) — Ollama /v1/completions at OLLAMA_HOST
  llamacpp            — llama-server /v1/completions at EXTERNAL_INFERENCE_ENDPOINT

Both expose an OpenAI-compatible /v1/completions endpoint so the HTTP call is
identical; the concrete subclasses differ only in default URL, model field, and
context-window handling.
"""
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS  = 800


class DistillationBackend(ABC):
    """Abstract base — subclasses implement complete(); quality eval is shared."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = _DEFAULT_TEMPERATURE,
    ) -> str:
        """Return raw model completion text, or raise on error."""

    async def evaluate_quality(self, original: str, compressed: str) -> int:
        """Score compression quality 0–100. Shared across backends."""
        prompt = (
            "Rate this compression quality 0-100.\n"
            "100 = all critical info preserved. 0 = info lost.\n\n"
            f"ORIGINAL: {original[:300]}\n"
            f"COMPRESSED: {compressed[:300]}\n\n"
            "Score (0-100):"
        )
        try:
            raw = await self.complete(prompt, max_tokens=3, temperature=0.1)
            parts = raw.split()
            digits = "".join(c for c in (parts[0] if parts else "")) if parts else ""
            return max(0, min(100, int(digits))) if digits else 75
        except Exception as exc:
            logger.warning(f"[backend] quality eval failed: {exc}")
            return 75

    async def close(self) -> None:
        """Release HTTP client. Override in subclasses."""


class OllamaBackend(DistillationBackend):
    """Ollama /v1/completions — default, recommended for new installs."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 180.0,
    ):
        self.base_url = (base_url or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.model    = model or os.getenv("DISTILLATION_MODEL", "phi-3.5-mini")
        self._client  = httpx.AsyncClient(timeout=timeout)

    async def complete(
        self,
        prompt: str,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = _DEFAULT_TEMPERATURE,
    ) -> str:
        resp = await self._client.post(
            f"{self.base_url}/v1/completions",
            json={"model": self.model, "prompt": prompt,
                  "temperature": temperature, "top_p": 0.9},
        )
        resp.raise_for_status()
        return resp.json().get("choices", [{}])[0].get("text", "").strip()

    async def health_check(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()


class LlamaCppBackend(DistillationBackend):
    """llama-server /v1/completions — for installs running llama.cpp separately."""

    _MAX_PROMPT_CHARS = 6000  # ~1500 tokens; safe margin below 3072-token context

    def __init__(
        self,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 90.0,
    ):
        self.endpoint = (endpoint or os.getenv("EXTERNAL_INFERENCE_ENDPOINT", "http://localhost:8000")).rstrip("/")
        self.model    = model or os.getenv("EXTERNAL_INFERENCE_MODEL", "Llama-2-7B-Chat-Q4_K_M.gguf")
        self._client  = httpx.AsyncClient(timeout=timeout)

    def _truncate(self, text: str) -> str:
        if len(text) <= self._MAX_PROMPT_CHARS:
            return text
        return text[:self._MAX_PROMPT_CHARS] + "\n[TRUNCATED]"

    async def complete(
        self,
        prompt: str,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = _DEFAULT_TEMPERATURE,
    ) -> str:
        resp = await self._client.post(
            f"{self.endpoint}/v1/completions",
            json={"model": self.model, "prompt": self._truncate(prompt),
                  "temperature": temperature, "top_p": 0.9, "max_tokens": max_tokens},
        )
        resp.raise_for_status()
        return resp.json().get("choices", [{}])[0].get("text", "").strip()

    async def health_check(self) -> bool:
        try:
            r = await self._client.get(f"{self.endpoint}/health", timeout=5.0)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()


def get_backend(override: Optional[str] = None) -> DistillationBackend:
    """
    Factory — returns a configured backend.

    Selection order: override arg → DISTILLATION_BACKEND env var → "ollama".

    Examples
    --------
    DISTILLATION_BACKEND=ollama      OLLAMA_HOST=http://192.168.x.x:11434
    DISTILLATION_BACKEND=llamacpp    EXTERNAL_INFERENCE_ENDPOINT=http://...
    """
    name = (override or os.getenv("DISTILLATION_BACKEND", "ollama")).lower()
    if name == "llamacpp":
        logger.info("[backend] LlamaCppBackend selected")
        return LlamaCppBackend()
    logger.info("[backend] OllamaBackend selected (default)")
    return OllamaBackend()
