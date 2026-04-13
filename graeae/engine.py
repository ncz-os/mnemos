#!/usr/bin/env python3
from __future__ import annotations
"""
GRAEAE Multi-Provider Consensus Engine

Queries multiple AI providers in parallel and returns all responses.

Provider registry
-----------------
Providers are declared in config.toml under [graeae.providers.<name>] — no
code changes needed to add or modify a provider. Keys are resolved from
~/.api_keys_master.json (or $MNEMOS_KEYS_PATH). Built-in defaults are used
as a fallback when config.toml has no [graeae.providers] section.

API adapter styles (the "api" field in config.toml):
  "openai"    — OpenAI-compatible chat completions (Perplexity, Groq, xAI, OpenAI)
  "anthropic" — Anthropic Messages API
  "gemini"    — Google Gemini generateContent API

GPT-5 series is detected by model name ("gpt-5") and automatically uses
max_completion_tokens instead of max_tokens — no separate api_type needed.

Reliability stack (innermost to outermost):
  _concurrency     — asyncio.Semaphore per provider; sheds load when saturated
                     (Triton instance-slot model — skip, don't queue)
  _circuit_breaker — trips after 5 failures; recovers via HALF_OPEN probe
  _rate_limiter    — sliding-window RPM guard
  _quality         — rolling success-rate multiplier on base weight
  _cache           — in-memory LRU keyed on normalised prompt hash (1h TTL)
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
from graeae.elo_sync import get_elo_weights

logger = logging.getLogger(__name__)


@dataclass
class ProviderResponse:
    provider: str
    status: str
    response_text: str
    latency_ms: int
    model_id: str
    final_score: float = 0.0



# Built-in provider defaults — used when config.toml has no [graeae.providers] section.
# Operators override these (or add new providers) via config.toml exclusively.
_BUILTIN_PROVIDERS: dict[str, dict] = {
    # These are conservative public defaults — override via config.toml [graeae.providers].
    # Any provider with an invalid key or model will be skipped by the engine at runtime.
    "perplexity": {
        "url": "https://api.perplexity.ai/chat/completions",
        "model": "sonar-pro", "weight": 0.88, "api": "openai", "key_name": "perplexity",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile", "weight": 0.78, "api": "openai", "key_name": "groq",
    },
    "claude": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-3-5-sonnet-20241022", "weight": 0.85, "api": "anthropic", "key_name": "claude",
    },
    "xai": {
        "url": "https://api.x.ai/v1/chat/completions",
        "model": "grok-2-latest", "weight": 0.84, "api": "openai", "key_name": "xai",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o", "weight": 0.82, "api": "openai", "key_name": "openai",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent",
        "model": "gemini-1.5-pro", "weight": 0.81, "api": "gemini", "key_name": "gemini",
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "model": "meta/llama-3.3-70b-instruct", "weight": 0.80, "api": "openai", "key_name": "nvidia",
    },
    "together": {
        "url": "https://api.together.xyz/v1/chat/completions",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "weight": 0.78, "api": "openai", "key_name": "together_ai",
    },
}


def _load_providers() -> dict[str, dict]:
    """Load provider registry from config.toml [graeae.providers].

    Falls back to _BUILTIN_PROVIDERS if the section is absent.
    Providers with enabled=false are excluded.
    The TOML 'api' field is kept as-is; dispatch in _query_provider() reads it.
    """
    try:
        from config import GRAEAE_CONFIG
        registry = GRAEAE_CONFIG.get("providers", {})
    except ImportError:
        registry = {}

    if not registry:
        logger.debug("[GRAEAE] no providers in config.toml — using built-in defaults")
        return {k: dict(v) for k, v in _BUILTIN_PROVIDERS.items()}

    providers: dict[str, dict] = {}
    for name, cfg in registry.items():
        if not cfg.get("enabled", True):
            logger.info(f"[GRAEAE] provider '{name}' disabled in config.toml — skipping")
            continue
        required = {"url", "model", "weight", "api", "key_name"}
        missing = required - cfg.keys()
        if missing:
            logger.warning(f"[GRAEAE] provider '{name}' missing fields {missing} — skipping")
            continue
        providers[name] = dict(cfg)

    if not providers:
        logger.warning("[GRAEAE] config.toml [graeae.providers] is empty — using built-in defaults")
        return {k: dict(v) for k, v in _BUILTIN_PROVIDERS.items()}

    logger.info(f"[GRAEAE] loaded {len(providers)} providers from config.toml: {list(providers)}")
    return providers


class GraeaeEngine:
    """Multi-provider consensus reasoning engine."""

    def __init__(self):
        self.providers = _load_providers()
        self._client: Optional[httpx.AsyncClient] = None

        # Seed base weights from Arena.ai Elo leaderboard if available.
        # Uses on-disk cache — falls back to config.toml weights silently.
        elo = get_elo_weights(force_refresh=False)
        if elo:
            for name, w in elo.items():
                if name in self.providers:
                    self.providers[name]["weight"] = w
            logger.info(f"[GRAEAE] Elo weights applied for: {[p for p in elo if p in self.providers]}")

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
        api = provider["api"]

        if api == "openai":
            response = await self._query_openai_compatible(provider, prompt, timeout)
        elif api == "anthropic":
            response = await self._query_anthropic(provider, prompt, timeout)
        elif api == "gemini":
            response = await self._query_gemini(provider, prompt, timeout)
        else:
            raise ValueError(f"Unknown api style '{api}' for provider '{provider_name}'")

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
        # GPT-5 series uses max_completion_tokens; all other OpenAI-compat APIs use max_tokens
        tokens_key = "max_completion_tokens" if provider["model"].startswith("gpt-5") else "max_tokens"
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
