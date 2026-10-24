"""/v1/models registry-backed (v3.1.2 Tier 3).

Pins the new behavior: /v1/models queries model_registry instead of
returning a hardcoded list. Discovery is registry-only: chat routing
can still use heuristics, but model listing and lookup do not synthesize
metadata for unregistered models.
"""

from __future__ import annotations

import asyncio

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import openai_compat


def _user() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


def _conn(**kwargs):
    """Fake asyncpg connection for the gateway's registry reads.

    Since F-06 the repository reaches storage through the persistence ABC,
    so a fake pool alone is invisible to it. ``_install`` wires this
    connection underneath the REAL ``PostgresConsultationAuditRepository``,
    which keeps every assertion below aimed at production behaviour (SQL,
    failure handling, row mapping) rather than at a stub of it.
    """
    from tests._fake_backend import FakePostgresConnection

    return FakePostgresConnection(**kwargs)


def _install(monkeypatch, conn):
    """Install ``conn`` behind the real Postgres repository via the ABC."""
    from tests._fake_backend import install_fake_postgres_backend

    return install_fake_postgres_backend(monkeypatch, conn)


def _install_no_pool(monkeypatch):
    """Simulate a pre-lifespan state: no persistence backend at all."""
    import mnemos.core.lifecycle as lc
    monkeypatch.setattr(lc, "_persistence_backend", None)
    monkeypatch.setattr(lc, "_pool", None)


# ---- /v1/models ------------------------------------------------------------


def test_list_models_returns_registry_rows(monkeypatch):
    # Distinct graeae_weight so the expected order is the ORDER BY the
    # Postgres repository really emits (graeae_weight DESC NULLS LAST,
    # model_id ASC) — not the insertion order of this list.
    rows = [
        {"provider": "openai",    "model_id": "gpt-5",             "display_name": "GPT-5",             "graeae_weight": 0.95},
        {"provider": "anthropic", "model_id": "claude-4.5-sonnet", "display_name": "Claude 4.5 Sonnet", "graeae_weight": 0.85},
        {"provider": "gemini",    "model_id": "gemini-2.5-pro",    "display_name": "Gemini 2.5 Pro",    "graeae_weight": 0.75},
    ]
    _install(monkeypatch, _conn(registry_rows=rows))

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    ids = [m.id for m in resp.data]
    owners = {m.id: m.owned_by for m in resp.data}

    assert ids == ["gpt-5", "claude-4.5-sonnet", "gemini-2.5-pro"]
    assert owners["gpt-5"] == "OpenAI"
    assert owners["claude-4.5-sonnet"] == "Anthropic"
    assert owners["gemini-2.5-pro"] == "Google"


def test_list_models_empty_registry_returns_empty(monkeypatch):
    _install(monkeypatch, _conn(registry_rows=[]))

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    assert resp.data == []


def test_list_models_db_failure_returns_empty_without_synthesizing(monkeypatch):
    _install(monkeypatch, _conn(fail=True))

    # Must not synthesize unregistered models through transient DB blips.
    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    assert resp.data == []


def test_list_models_no_pool_returns_empty(monkeypatch):
    _install_no_pool(monkeypatch)

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    assert resp.data == []


def test_list_models_unknown_provider_capitalized(monkeypatch):
    """An unmapped provider key (say an operator added 'cohere' locally)
    should still produce a reasonable owned_by string."""
    rows = [{"provider": "cohere", "model_id": "command-r+", "display_name": None}]
    _install(monkeypatch, _conn(registry_rows=rows))

    resp = asyncio.run(openai_compat.list_models(authorization=None, user=_user()))
    assert resp.data[0].owned_by == "Cohere"


# ---- /v1/models/{model_id} -------------------------------------------------


def test_get_model_hits_registry(monkeypatch):
    _install(monkeypatch, _conn(registry_rows=[{"provider": "openai", "model_id": "gpt-5"}]))

    result = asyncio.run(openai_compat.get_model(
        "gpt-5", authorization=None, user=_user(),
    ))
    assert result.id == "gpt-5"
    assert result.owned_by == "OpenAI"


def test_get_model_alias_resolved_before_lookup(monkeypatch):
    """An alias like 'best-coding' should resolve to its concrete model
    first, then that concrete id is what hits the registry."""
    # Use a known alias from MODEL_ALIASES
    alias = next(iter(openai_compat.MODEL_ALIASES))
    concrete = openai_compat.MODEL_ALIASES[alias]

    # Only the CONCRETE id exists in the registry: if the alias were sent
    # as-is the lookup would miss and the route would 404.
    conn = _conn(registry_rows=[{"provider": "openai", "model_id": concrete}])
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat.get_model(
        alias, authorization=None, user=_user(),
    ))

    assert result.id == concrete
    # The registry query must have been passed the RESOLVED id, not the alias.
    assert conn.fetchrow_calls, "expected a registry fetchrow call"
    assert conn.fetchrow_calls[0][1] == (concrete,)


def test_get_model_unknown_returns_404(monkeypatch):
    """A model not in the registry returns 404. Chat routing may still
    use provider heuristics, but discovery must not overstate availability."""
    _install(monkeypatch, _conn(registry_rows=[]))

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.get_model(
            "my-local-model", authorization=None, user=_user(),
        ))
    assert exc.value.status_code == 404
    assert exc.value.detail == "model not found"
