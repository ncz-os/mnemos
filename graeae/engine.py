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

from graeae.api_keys import _PROVIDER_ENV_VARS, get_key


def _env_var_hint(key_name: str) -> str:
    """Return the env-var name an operator would export to bypass
    the Provider Registry File for a given key_name. Used in error
    messages so the hint is actionable."""
    return _PROVIDER_ENV_VARS.get(key_name, f"<{key_name.upper()}_API_KEY>")
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
#
# Model IDs refreshed 2026-04-23 to current frontier (v3.1.2 Defect 3).
# Operators who want earlier generations override via config.toml.
# Defaults assume each provider's "flagship available to API" tier.
_BUILTIN_PROVIDERS: dict[str, dict] = {
    "together": {
        "url": "https://api.together.xyz/v1/chat/completions",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "weight": 0.80, "api": "openai", "key_name": "together_ai",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile", "weight": 0.80, "api": "openai", "key_name": "groq",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-5.2-chat-latest", "weight": 0.88, "api": "openai", "key_name": "openai",
    },
    "claude": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-opus-4-6", "weight": 0.90, "api": "anthropic", "key_name": "claude",
    },
    "perplexity": {
        "url": "https://api.perplexity.ai/chat/completions",
        "model": "sonar-pro", "weight": 0.88, "api": "openai", "key_name": "perplexity",
    },
    "xai": {
        "url": "https://api.x.ai/v1/chat/completions",
        "model": "grok-4-1-fast", "weight": 0.86, "api": "openai", "key_name": "xai",
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "model": "meta/llama-3.3-70b-instruct", "weight": 0.80, "api": "openai", "key_name": "nvidia",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-preview:generateContent",
        "model": "gemini-3-pro-preview", "weight": 0.88, "api": "gemini", "key_name": "gemini",
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

    async def consult(
        self,
        prompt: str,
        task_type: str = "reasoning",
        timeout: int = 30,
        selection: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict:
        """Query eligible providers in parallel and return all responses.

        `selection` (v3.2 Custom Query mode) is an optional
        `{provider_name: model_override_or_None}` dict. When set, only
        those providers are considered for the fan-out; every other
        registered provider is omitted (not marked unavailable). A
        `model_override` value, if not None, overrides
        `self.providers[name]["model"]` for that one call.

        When `selection` is None, behavior is unchanged — every
        registered provider is considered (current auto-lineup).
        """
        task_type = task_type or "reasoning"

        # ── Cache check ──────────────────────────────────────────────────────
        # Include the selection (or lack thereof) in the cache key so a
        # Custom Query for "frontier only" doesn't get served the cached
        # all-providers response for the same prompt.
        cache_tag = _selection_cache_tag(selection) if selection else ""
        cache_key_task = f"{task_type}{cache_tag}"
        cached = self._cache.get(prompt, cache_key_task)
        if cached is not None:
            logger.info(f"[GRAEAE] cache hit (task_type={cache_key_task})")
            return {"all_responses": cached, "cache_hit": True}

        concurrency = self._get_concurrency()

        # ── Selection-aware iteration list ───────────────────────────────────
        # If Custom Query set a lineup, respect it verbatim; unknown
        # provider names should have been rejected by the caller before
        # reaching the engine, but we guard defensively.
        if selection is not None:
            candidate_providers = [p for p in selection if p in self.providers]
        else:
            candidate_providers = list(self.providers)

        # ── Eligibility gate ─────────────────────────────────────────────────
        # A provider is skipped (not queued) if it is:
        #   • circuit-open (repeated recent failures)
        #   • rate-limited (RPM window exhausted)
        #   • saturated (all concurrency slots occupied)
        active: list[str] = []
        skipped: list[str] = []
        for name in candidate_providers:
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
                    for name in candidate_providers
                },
                "error": "all providers unavailable",
            }

        # ── Fan-out ──────────────────────────────────────────────────────────
        # When a selection supplied a per-provider model override, apply
        # it temporarily for the duration of this query. We deep-copy the
        # provider config and pass it through a throwaway self.providers
        # snapshot via _query_provider — but simpler: route through the
        # same _query_provider and let it pick the current config. For the
        # override we swap `self.providers[name]["model"]` in place, fan
        # out, then restore. This keeps _query_provider untouched.
        restored: Dict[str, str] = {}
        if selection is not None:
            for name, override in selection.items():
                if override and name in self.providers:
                    restored[name] = self.providers[name]["model"]
                    self.providers[name]["model"] = override
        try:
            tasks = [self._query_provider(name, prompt, task_type, timeout) for name in active]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for name, original in restored.items():
                self.providers[name]["model"] = original

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
            self._cache.set(prompt, cache_key_task, all_responses)

        # ── Compute consensus fields (v3.2) ──────────────────────────────────
        # ConsultationResponse has exposed consensus_response,
        # consensus_score, winning_muse, cost, latency_ms since v3.0
        # but the engine only emitted all_responses; consultation_id
        # callers saw all five as None. Compute them here from
        # all_responses so the contract is honored instead of
        # aspirational.
        consensus = _compute_consensus(all_responses)
        return {"all_responses": all_responses, **consensus}

    async def route(
        self, provider: str, model: str, prompt: str, task_type: str = "reasoning", timeout: int = 30
    ) -> Dict:
        """Single-provider pass-through — consensus skipped, eligibility
        gates applied.

        Used by MNEMOS gateway (`/v1/chat/completions`) for explicit
        model selection. Before v3.2 this path deliberately skipped
        the reliability stack "caller responsible for load management";
        operators pointed out that the gateway was effectively the
        weakest surface of the service because openai_compat did not
        actually implement any load management. v3.2 closes that gap:
        the circuit breaker, rate limiter, and concurrency guard are
        applied here exactly as they are in consult(), so one
        misbehaving provider can't take down the gateway while
        consultations keep working.

        Args:
            provider: Provider name (must exist in self.providers)
            model: Override model name (optional; uses provider config if None)
            prompt: Query text
            task_type: Task type for logging/tracking
            timeout: Request timeout in seconds

        Returns:
            Dict with status, response_text, latency_ms, model_id, error
        """
        if provider not in self.providers:
            logger.warning(f"[GRAEAE] unknown provider '{provider}' — returning unavailable")
            return _unavailable(
                model or provider,
                error=f"provider '{provider}' not registered in this deployment",
            )

        provider_config = dict(self.providers[provider])
        if model:
            provider_config["model"] = model

        # Key-missing is a common failure and silently produces a 401/403
        # upstream with no visible reason. Pre-check the key and emit a
        # targeted error so operators don't have to tail debug logs.
        api_key = get_key(provider_config["key_name"])
        if not api_key:
            logger.error(
                "[GRAEAE] route(%s) failed: missing api_key (key_name=%s) — "
                "set the %s environment variable or add the key to the "
                "Provider Registry File (MNEMOS_KEYS_PATH / "
                "~/.config/mnemos/api_keys.json / ~/.api_keys_master.json)",
                provider,
                provider_config["key_name"],
                _env_var_hint(provider_config["key_name"]),
            )
            return _unavailable(
                provider_config["model"],
                error=(
                    f"missing api_key for provider '{provider}' "
                    f"(key_name={provider_config['key_name']})"
                ),
            )

        # v3.2 reliability gate: circuit-breaker → rate-limiter →
        # concurrency. Mirrors the consult() eligibility loop so
        # gateway traffic is first-class not second-class.
        if not self._circuit_breakers.is_allowed(provider):
            logger.info("[GRAEAE] route(%s) refused: circuit open", provider)
            return _unavailable(
                provider_config["model"],
                error=f"provider '{provider}' circuit open",
            )
        if not self._rate_limiters.is_allowed(provider):
            logger.info("[GRAEAE] route(%s) refused: rate limited", provider)
            return _unavailable(
                provider_config["model"],
                error=f"provider '{provider}' rate-limited",
            )
        concurrency = self._get_concurrency()
        if not await concurrency.acquire(provider):
            logger.info("[GRAEAE] route(%s) refused: concurrency saturated", provider)
            return _unavailable(
                provider_config["model"],
                error=f"provider '{provider}' concurrency saturated",
            )

        try:
            try:
                result = await self._query_provider(provider, prompt, task_type, timeout)
            except Exception as e:
                # Record the failure against the breaker so repeated
                # gateway-path failures actually trip it, and quality
                # tracker so the weight reflects reality.
                self._circuit_breakers.record_failure(provider)
                self._quality.record_failure(provider)
                logger.error(f"[GRAEAE] route({provider}) failed: {e}")
                return _unavailable(
                    provider_config["model"],
                    error=f"{type(e).__name__}: {e}",
                )
            # Success path — credit the breaker + quality tracker so
            # the gateway's successes count toward reopening a
            # half-open circuit, not just consultations' successes.
            self._circuit_breakers.record_success(provider)
            self._quality.record_success(provider, result.get("latency_ms", 0))
            logger.debug(
                f"[GRAEAE] route({provider}, {model or 'default'}) → {result['status']}"
            )
            return result
        finally:
            concurrency.release(provider)

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
        # GPT-5 series has two API quirks:
        #   1. Uses max_completion_tokens instead of max_tokens.
        #   2. Only accepts temperature=1 (returns 400 on any other value).
        # Other OpenAI-compat providers take the traditional shape.
        is_gpt5 = provider["model"].startswith("gpt-5")
        tokens_key = "max_completion_tokens" if is_gpt5 else "max_tokens"
        payload: Dict = {
            "model": provider["model"],
            "messages": [{"role": "user", "content": prompt}],
            tokens_key: 2000,
        }
        if not is_gpt5:
            payload["temperature"] = 0.7
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


def _compute_consensus(all_responses: Dict[str, Dict]) -> Dict:
    """Roll up per-provider responses into consensus fields.

    Emits:
      consensus_response — text of the highest-scoring successful
                           provider (winning muse). Empty string if
                           no provider succeeded.
      consensus_score    — the winner's final_score, or 0.0.
      winning_muse       — the provider name of the winner, or None.
      cost               — sum of per-provider `cost` fields (0.0
                           when a provider didn't report one). Matches
                           the consultation-persist path's existing
                           fallback that used the engine-reported cost
                           when present.
      latency_ms         — max latency across providers (parallel
                           fan-out: wall-clock to all_responses is
                           dominated by the slowest successful call).

    Contract: returns ALL keys even when there's no winner so callers
    never have to check for "field present" vs "field set". A
    no-winner consultation has consensus_response="", consensus_score=
    0.0, winning_muse=None, cost=0.0, latency_ms=0.
    """
    successes = [
        (name, resp)
        for name, resp in all_responses.items()
        if resp.get("status") == "success"
    ]
    if successes:
        winner_name, winner_resp = max(
            successes, key=lambda kv: kv[1].get("final_score", 0.0)
        )
    else:
        winner_name, winner_resp = None, None

    total_cost = 0.0
    for resp in all_responses.values():
        c = resp.get("cost")
        if isinstance(c, (int, float)):
            total_cost += float(c)

    latencies = [
        int(resp.get("latency_ms", 0) or 0)
        for resp in all_responses.values()
    ]
    max_latency = max(latencies) if latencies else 0

    return {
        "consensus_response": winner_resp.get("response_text", "") if winner_resp else "",
        "consensus_score": float(winner_resp.get("final_score", 0.0)) if winner_resp else 0.0,
        "winning_muse": winner_name,
        "cost": total_cost,
        "latency_ms": max_latency,
    }


def _selection_cache_tag(selection: Optional[Dict[str, Optional[str]]]) -> str:
    """Deterministic string suffix for the response cache key when a
    Custom Query selection is active. Different lineups must not share
    a cache entry — two callers asking the same prompt under different
    lineups expect different result sets.
    """
    if not selection:
        return ""
    parts = sorted(
        f"{name}={override or ''}"
        for name, override in selection.items()
    )
    return "|" + ",".join(parts)


def _unavailable(model_id: str, error: str = "") -> Dict:
    """Uniform shape for provider failures.

    `error` (v3.1.2 diagnostic) is a short human-readable cause — e.g.
    "missing api_key", "HTTP 401 Unauthorized", "timeout after 30s" —
    surfaced by callers in logs and 503 responses so operators can
    diagnose without running the stack under DEBUG logging.
    """
    return {
        "status": "unavailable",
        "response_text": "",
        "latency_ms": 0,
        "model_id": model_id,
        "final_score": 0.0,
        "error": error,
    }


# ── Module-level singleton ─────────────────────────────────────────────────────

_graeae_engine: Optional[GraeaeEngine] = None


def get_graeae_engine() -> GraeaeEngine:
    global _graeae_engine
    if _graeae_engine is None:
        _graeae_engine = GraeaeEngine()
    return _graeae_engine
