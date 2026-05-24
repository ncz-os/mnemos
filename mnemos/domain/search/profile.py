"""Retrieval profile enum + caller-value resolver (v6.2 M-2.2.3)."""

from __future__ import annotations

from enum import Enum


class SearchProfile(str, Enum):
    """Retrieval profile.

    - ``fast``: semantic-only, no rerank. Targets <100ms p99 for agent
      skills (portfolio_ask etc).
    - ``balanced``: current behavior — semantic + FTS union,
      recency-weighted. Default for interactive callers.
    - ``deep``: semantic + cross-encoder rerank top-100 → top-30 via
      MEDUSA :8091 bge-reranker-v2-m3. p99 ≤ 5s for `synthesize` /
      `narrate` consumers.
    """

    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"


_ALLOWED = {p.value for p in SearchProfile}


def resolve_profile(raw: str | None, *, default: SearchProfile = SearchProfile.BALANCED) -> SearchProfile:
    """Validate caller-supplied profile value.

    Returns ``default`` when ``raw`` is None or empty. Raises
    ``ValueError`` for unknown values — route handler maps that to
    HTTP 400.
    """
    if not raw:
        return default
    if raw not in _ALLOWED:
        raise ValueError(f"unknown retrieval profile {raw!r}; allowed: {sorted(_ALLOWED)}")
    return SearchProfile(raw)
