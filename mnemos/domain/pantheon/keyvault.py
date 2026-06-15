"""BYOK provider-key resolution seam for PANTHEON.

Resolves a provider API key for a (tenant, provider). Two concrete resolvers +
a chain:

  * ``EnvKeyResolver`` — tenant-agnostic, provider→env-var (the current fleet
    behavior; shared keys).
  * ``MappedKeyResolver`` — per-tenant BYOK from an injected store. The store is
    expected to hold keys encrypted at rest and decrypt on read — that envelope
    crypto is the store's concern (deferred); this is only the lookup contract.
  * ``ChainKeyResolver`` — try per-tenant BYOK first, fall back to shared env
    (the product default: a tenant's own key wins, else the platform key).

Resolved keys are secrets — callers MUST keep them out of logs / routing audit.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping

_DEFAULT_ENV_MAP: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google_gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "together": "TOGETHER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "deepseek-direct": "DEEPSEEK_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "ngc": "NVIDIA_API_KEY",
    "eih": "EIH_API_KEY",
    "openai": "OPENAI_API_KEY",
}


class KeyResolver(ABC):
    @abstractmethod
    def resolve(self, tenant: str, provider: str) -> str | None:
        """Return the API key for (tenant, provider), or None if unknown."""


class EnvKeyResolver(KeyResolver):
    """Shared, tenant-agnostic keys from environment variables."""

    def __init__(self, env: Mapping[str, str] | None = None, env_map: Mapping[str, str] | None = None):
        self._env = env if env is not None else os.environ
        self._map = dict(env_map) if env_map is not None else dict(_DEFAULT_ENV_MAP)

    def resolve(self, tenant: str, provider: str) -> str | None:
        var = self._map.get(provider)
        return self._env.get(var) if var else None


class MappedKeyResolver(KeyResolver):
    """Per-tenant BYOK from an injected store.

    ``store`` is either a mapping keyed by ``(tenant, provider)`` or a callable
    ``(tenant, provider) -> key | None`` (e.g. a decrypting vault lookup).
    """

    def __init__(self, store: Mapping[tuple[str, str], str] | Callable[[str, str], str | None]):
        self._store = store

    def resolve(self, tenant: str, provider: str) -> str | None:
        if callable(self._store):
            return self._store(tenant, provider)
        return self._store.get((tenant, provider))


class ChainKeyResolver(KeyResolver):
    """Try each resolver in order; first non-empty key wins (BYOK then shared)."""

    def __init__(self, *resolvers: KeyResolver):
        self._resolvers = resolvers

    def resolve(self, tenant: str, provider: str) -> str | None:
        for resolver in self._resolvers:
            key = resolver.resolve(tenant, provider)
            if key:
                return key
        return None
