from __future__ import annotations
#!/usr/bin/env python3
"""
GRAEAE Multi-Provider Consensus Engine

Queries multiple AI providers in parallel and returns all responses.

Reliability stack (innermost to outermost):
  _concurrency  — per-provider asyncio.Semaphore; sheds load when a provider
                  is saturated rather than queueing (Triton instance-slot model)
  _circuit_breaker — trips after N consecutive failures; auto-recovers after
                  cooldown (prevents timeout storms against a down provider)
  _rate_limiter — sliding-window RPM guard; stops us hammering a provider
                  before it 429s us
  _quality      — rolling success-rate multiplier on base weight; deprioritises
                  flaky providers without removing them from the pool
  _cache        — in-memory LRU keyed on normalized prompt hash; skips full
                  round-trip for repeated identical queries (1h TTL)
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

from graeae.api_keys import get_key
from graeae._circuit_breaker import CircuitBreakerPool
from graeae._rate_limiter import RateLimiterPool
from graeae._quality import QualityTracker
from graeae._cache import ResponseCache
from graeae._concurrency import ConcurrencyLimiterPool

logger = logging.getLogger(__name__)


@dataclass
class ProviderResponse:
    provider: str
    status: str
    response_text: str
    latency_ms: int
    model_id: str
    final_score: float = 0.0


class GraeaeEngine:
    """Multi-provider consensus reasoning engine."""

    def __init__(self):
        self.providers = {
            "perplexity": {
                "url": "https://api.perplexity.ai/chat/completions",
                "model": "sonar-pro",
                "weight": 0.88,
                "api_type": "openai",
                "key_name": "perplexity",
            },
            "groq": {
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "model": "llama-3.3-70b-versatile",
                "weight": 0.63,
                "api_type": "openai",
                "key_name": "groq",
            },
            "claude-opus": {
                "url": "https://api.anthropic.com/v1/messages",
                "model": "claude-opus-4-6",
                "weight": 0.85,
                "api_type": "anthropic",
                "key_name": "claude-opus",
            },
            "xai": {
                "url": "https://api.x.ai/v1/chat/completions",
                "model": "grok-3",
                "weight": 0.48,
                "api_type": "openai",
                "key_name": "xai",
            },
            "openai": {
                "url": "https://api.openai.com/v1/chat/completions",
                "model": "gpt-5.2",
                "weight": 0.82,
                "api_type": "openai_gpt5",
                "key_name": "openai",
            },
            "gemini": {
                "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent",
                "model": "gemini-3.1-pro-preview",
                "weight": 0.81,
                "api_type": "gemini",
                "key_name": "gemini",
            },
        }
        self._client: Optional[httpx.AsyncClient] = None

        # Reliability stack — instantiated here; _concurrency lazily initialised
        # on first consult() call because asyncio.Semaphore needs a running loop.
        self._circuit_breakers = CircuitBreakerPool(failure_threshold=5, cooldown_seconds=300)
        self._rate_limiters = RateLimiterPool()
        self._quality = QualityTracker({p: cfg["weight"] for p, cfg in self.providers.items()})
        self._cache = ResponseCache(ttl_seconds=3600, max_entries=500)
        self._concurrency: Optional[ConcurrencyLimiterPool] = None

    def _get_concurrency(self) -> ConcurrencyLimiterPool:
        """Lazy-init concurrency pool (requires running event loop)."""
        if self._concurrency is None:
            self._concurrency = ConcurrencyLimiterPool()
        return self._concurrency

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def consult(self, prompt: str, task_type: str = "reasoning", timeout: int = 30) -> Dict:
        """Query eligible providers in parallel and return all responses."""
        task_type = task_type or "reasoning"

        # ── Cache check ──────────────────────────────────────────────────────
        cached = self._cache.get(prompt, task_type)
        if cached is not None:
            logger.info(f"[GRAEAE] cache hit (task_type={task_type})")
            return {"all_responses": cached, "cache_hit": True}

        concurrency = self._get_concurrency()

        # ── Eligibility gate ─────────────────────────────────────────────────
        # A provider is skipped (not queued) if it is:
        #   • circuit-open (repeated recent failures)
        #   • rate-limited (RPM window exhausted)
        #   • saturated (all concurrency slots occupied)
        active: list[str] = []
        skipped: list[str] = []
        for name in self.providers:
            if not self._circuit_breakers.is_allowed(name):
                skipped.append(name)
            elif not self._rate_limiters.is_allowed(name):
                skipped.append(name)
            elif not await concurrency.acquire(name):
                skipped.append(name)
            else:
                active.append(name)

        if skipped:
            logger.info(f"[GRAEAE] skipped providers: {skipped}")

        if not active:
            logger.error("[GRAEAE] all providers unavailable")
            return {
                "all_responses": {
                    name: _unavailable(self.providers[name]["model"])
                    for name in self.providers
                },
                "error": "all providers unavailable",
            }

        # ── Fan-out ──────────────────────────────────────────────────────────
        tasks = [self._query_provider(name, prompt, task_type, timeout) for name in active]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_responses: Dict = {}

        for name, result in zip(active, results):
            concurrency.release(name)
            if isinstance(result, Exception):
                self._circuit_breakers.record_failure(name)
                self._quality.record_failure(name)
                all_responses[name] = {
                    "status": "error",
                    "response_text": "",
                    "latency_ms": 0,
                    "model_id": self.providers[name]["model"],
                    "final_score": 0.0,
                }
            else:
                self._circuit_breakers.record_success(name)
                self._quality.record_success(name, result.get("latency_ms", 0))
                result["final_score"] = self._quality.dynamic_weight(name)
                all_responses[name] = result

        for name in skipped:
            all_responses[name] = _unavailable(self.providers[name]["model"])

        # ── Cache successful result ──────────────────────────────────────────
        if any(r["status"] == "success" for r in all_responses.values()):
            self._cache.set(prompt, task_type, all_responses)

        return {"all_responses": all_responses}

    async def _query_provider(
        self, provider_name: str, prompt: str, task_type: str, timeout: int
    ) -> Dict:
        provider = self.providers[provider_name]
        start = datetime.now(timezone.utc)
        api_type = provider["api_type"]

        if api_type in ("openai", "openai_gpt5"):
            response = await self._query_openai_compatible(provider, prompt, timeout)
        elif api_type == "anthropic":
            response = await self._query_anthropic(provider, prompt, timeout)
        elif api_type == "gemini":
            response = await self._query_gemini(provider, prompt, timeout)
        else:
            raise ValueError(f"Unknown api_type: {api_type}")

        latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        response["latency_ms"] = latency
        response["final_score"] = provider["weight"]  # overridden by quality tracker in consult()
        return response

    async def _query_openai_compatible(self, provider: Dict, prompt: str, timeout: int) -> Dict:
        """Query OpenAI-compatible APIs (Perplexity, Groq, xAI, OpenAI)."""
        api_key = get_key(provider["key_name"])
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        tokens_key = "max_completion_tokens" if provider["api_type"] == "openai_gpt5" else "max_tokens"
        payload = {
            "model": provider["model"],
            "messages": [{"role": "user", "content": prompt}],
            tokens_key: 2000,
            "temperature": 0.7,
        }
        client = await self._get_client()
        resp = await client.post(provider["url"], json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return {
            "status": "success",
            "response_text": data["choices"][0]["message"]["content"],
            "latency_ms": 0,
            "model_id": provider["model"],
        }

    async def _query_anthropic(self, provider: Dict, prompt: str, timeout: int) -> Dict:
        """Query Anthropic Claude API."""
        api_key = get_key(provider["key_name"])
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": provider["model"],
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }
        client = await self._get_client()
        resp = await client.post(provider["url"], json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return {
            "status": "success",
            "response_text": data["content"][0]["text"],
            "latency_ms": 0,
            "model_id": provider["model"],
        }

    async def _query_gemini(self, provider: Dict, prompt: str, timeout: int) -> Dict:
        """Query Google Gemini API."""
        api_key = get_key(provider["key_name"])
        headers = {"x-goog-api-key": api_key}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.7},
        }
        client = await self._get_client()
        resp = await client.post(provider["url"], headers=headers, json=payload, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"No candidates in response: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = parts[0].get("text", "") if parts else ""
        if not text:
            raise RuntimeError(f"Empty content in candidate: {candidates[0]}")
        return {
            "status": "success",
            "response_text": text,
            "latency_ms": 0,
            "model_id": provider["model"],
        }

    def provider_status(self) -> Dict:
        """Circuit breaker, concurrency, rate limiter, quality, and cache stats."""
        status = {
            "circuit_breakers": self._circuit_breakers.status(),
            "rate_limiters": self._rate_limiters.status(),
            "quality": self._quality.status(),
            "cache": self._cache.stats(),
        }
        if self._concurrency:
            status["concurrency"] = self._concurrency.status()
        return status


def _unavailable(model_id: str) -> Dict:
    return {
        "status": "unavailable",
        "response_text": "",
        "latency_ms": 0,
        "model_id": model_id,
        "final_score": 0.0,
    }


# ── Module-level singleton ─────────────────────────────────────────────────────

_graeae_engine: Optional[GraeaeEngine] = None


def get_graeae_engine() -> GraeaeEngine:
    global _graeae_engine
    if _graeae_engine is None:
        _graeae_engine = GraeaeEngine()
    return _graeae_engine
