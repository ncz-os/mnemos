"""Gateway provider resolution via model_registry (v3.2).

Codex memory-OS audit 019dbd11 flagged: "gateway/provider selection
is still heuristic and brittle: substring routing plus a default-to-
Groq fallback." The v3.2 fix queries model_registry first and falls
back to substring heuristics only when the registry has no row.
Unknown models are now rejected with 400 instead of silently
routing to Groq.

Tests:
  - registry hit -> provider comes from DB row
  - registry miss with a known substring -> fallback heuristic
  - registry miss AND no substring match -> 400 (was silent groq)
  - DB query failure -> falls through to heuristic (no 500)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from api.auth import UserContext


def _user() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, *, row=None, raise_on_query=False):
        self._row = row
        self._raise = raise_on_query
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        if self._raise:
            raise RuntimeError("db blip")
        return self._row


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import api.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def _install_no_pool(monkeypatch):
    import api.lifecycle as lc
    monkeypatch.setattr(lc, "_pool", None)


# ─── _resolve_provider_for_model ────────────────────────────────────────────


def test_resolve_uses_registry_row_when_present(monkeypatch):
    from api.handlers import openai_compat

    conn = _Conn(row={"provider": "anthropic"})
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-opus-4-6"))
    assert result == "anthropic"


def test_resolve_falls_back_to_heuristic_on_registry_miss(monkeypatch):
    from api.handlers import openai_compat

    conn = _Conn(row=None)
    _install(monkeypatch, conn)

    # "claude-*" is in _FALLBACK_PROVIDER_MAP
    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-custom-local"))
    assert result == "claude"


def test_resolve_returns_none_on_complete_miss(monkeypatch):
    """Registry miss + no substring match -> None. Caller raises 400."""
    from api.handlers import openai_compat

    conn = _Conn(row=None)
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("unknown-xyz-v9"))
    assert result is None


def test_resolve_falls_back_when_db_query_raises(monkeypatch):
    """Transient DB failures must NOT 500 the gateway. Fall through
    to substring heuristic."""
    from api.handlers import openai_compat

    conn = _Conn(raise_on_query=True)
    _install(monkeypatch, conn)

    # gpt-* hits the fallback heuristic
    result = asyncio.run(openai_compat._resolve_provider_for_model("gpt-5-mini"))
    assert result == "openai"


def test_resolve_falls_back_when_pool_missing(monkeypatch):
    """Pre-lifespan state: _pool is None. Still fall through to
    heuristic so /v1/chat/completions stays usable during startup
    or in degenerate environments."""
    from api.handlers import openai_compat

    _install_no_pool(monkeypatch)
    result = asyncio.run(openai_compat._resolve_provider_for_model("llama-3.3-70b-versatile"))
    assert result == "groq"


# ─── _route_to_provider ─────────────────────────────────────────────────────


def test_route_unknown_model_raises_400(monkeypatch):
    """v3.2 contract: unknown model_id is an explicit 400, NOT a
    silent route to groq. Operators see the failure at the edge
    instead of getting garbage responses from the wrong provider."""
    from api.handlers import openai_compat

    conn = _Conn(row=None)
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat._route_to_provider(
            model="definitely-not-a-real-model-9999",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.7, max_tokens=100,
            user=_user(),
        ))
    assert exc.value.status_code == 400
    assert "unknown model" in exc.value.detail
    assert "model_registry" in exc.value.detail


def test_route_uses_registry_hit_over_heuristic(monkeypatch):
    """If the registry says model X belongs to provider A but the
    substring heuristic would say B, the registry wins. Allows
    operators to re-map providers without editing code."""
    from api.handlers import openai_compat

    # Registry says "claude-*" is served by a custom provider
    # registered as "my-local-anthropic-proxy". The substring
    # heuristic would say "claude". Registry wins.
    conn = _Conn(row={"provider": "my-local-anthropic-proxy"})
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-opus-4-6"))
    assert result == "my-local-anthropic-proxy"
