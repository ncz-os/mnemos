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

import pytest

from mnemos.api.dependencies import UserContext


def _user() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


def _registry_conn(*, model="__any__", row=None, raise_on_query=False):
    """Fake asyncpg connection for the gateway's registry lookup.

    ``row`` is the registry row the lookup for ``model`` should find; ``None``
    means the registry has no such model, which drives the resolver's
    substring-heuristic fallback. Registry reads since F-06 go through the
    persistence ABC, so ``_install`` puts this connection underneath the REAL
    ``PostgresConsultationAuditRepository`` rather than beside a fake pool the
    repository no longer reads.
    """
    from tests._fake_backend import FakePostgresConnection

    registry_rows = [] if row is None else [{"provider": row["provider"], "model_id": model}]
    return FakePostgresConnection(registry_rows=registry_rows, fail=raise_on_query)


class _NamespacedLookupConn:
    """Fake connection for the namespaced ``<provider>/<model>`` resolve path.

    The gateway form looks the whole id up first, then falls back to the
    split ``(provider, model_id)`` pair query. This fake serves the pair
    query — driven by its SQL shape and bound arguments, not by call order —
    so a resolver that reorders or adds a probe still gets the right rows.

    ``pair_rows`` are returned for ``WHERE provider = $N AND model_id = $M``.
    The whole-id lookup (``WHERE model_id = $N`` with no provider filter)
    always misses: that is what makes the namespaced fallback fire.
    """

    def __init__(self, pair_rows=None):
        self.pair_rows = [dict(r) for r in (pair_rows or [])]
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        normalized = " ".join(sql.split()).lower()
        if " from model_registry" not in normalized:
            raise AssertionError(f"unexpected statement: {normalized[:120]!r}")
        filters_provider = "provider = $" in normalized
        filters_model = "model_id = $" in normalized
        if filters_model and not filters_provider:
            # Whole-id (possibly namespaced) lookup: never in the registry.
            return None
        if filters_provider and filters_model:
            provider, model_id = args[0], args[1]
            for row in self.pair_rows:
                if row["provider"] == provider and row["model_id"] == model_id:
                    return {"provider": row["provider"]}
            return None
        raise AssertionError(f"unhandled registry statement: {normalized[:120]!r}")

    async def fetch(self, sql, *args):
        raise AssertionError(f"unexpected fetch on the resolve path: {' '.join(sql.split())[:120]!r}")


def _install(monkeypatch, conn):
    """Install ``conn`` behind the real Postgres repository via the ABC."""
    from tests._fake_backend import install_fake_postgres_backend

    return install_fake_postgres_backend(monkeypatch, conn)


def _install_no_pool(monkeypatch):
    """Pre-lifespan / degenerate state: no persistence backend at all."""
    import mnemos.core.lifecycle as lc
    monkeypatch.setattr(lc, "_persistence_backend", None)
    monkeypatch.setattr(lc, "_pool", None)


# ─── _resolve_provider_for_model ────────────────────────────────────────────


def test_resolve_uses_registry_row_when_present(monkeypatch):
    """Registry stores Anthropic models under provider='anthropic'
    (the Anthropic-side name). engine.providers indexes them under
    'claude' (the GRAEAE-side name). The resolver normalizes back to
    the GRAEAE name so engine.route() can find the dispatch entry —
    a verbatim 'anthropic' return would 503 with 'provider not
    registered'."""
    from mnemos.api.routes import openai_compat

    # A model name the substring heuristic cannot map, so "claude" can only
    # have come from the registry row (the heuristic maps claude-* to the
    # same value and would make this assertion pass without any registry hit).
    conn = _registry_conn(model="acme-opus-9", row={"provider": "anthropic"})
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("acme-opus-9"))
    assert conn.fetchrow_calls, "the registry was never consulted"
    assert result == "claude"


def test_resolve_normalises_registry_name_only_when_mapped(monkeypatch):
    """An operator-registered custom provider (not in _REGISTRY_MAP) must
    pass through the resolver verbatim. Locks in the v3.2 contract that
    registry is the source of truth for provider routing — the new
    GRAEAE-normalization step is additive, not a rename pass."""
    from mnemos.api.routes import openai_compat

    conn = _registry_conn(model="claude-opus-4-6", row={"provider": "my-local-anthropic-proxy"})
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-opus-4-6"))
    assert result == "my-local-anthropic-proxy"


def test_resolve_handles_gateway_namespaced_slash_id(monkeypatch):
    """`<provider>/<bare_api_id>` is the gateway's namespacing convention.
    Many upstream APIs have slash-bearing IDs natively (NVIDIA's
    `meta/llama-3.3-70b-instruct`, Together's `Qwen/Qwen3-…`); the
    resolver must split on the FIRST slash and look up the tail with
    the head as a provider filter so the gateway form resolves the same
    way as the bare upstream form."""
    from mnemos.api.routes import openai_compat

    conn = _NamespacedLookupConn(
        pair_rows=[{"provider": "together", "model_id": "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"}]
    )
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model(
        "together/Qwen/Qwen3-235B-A22B-Instruct-2507-tput"
    ))
    assert result == "together"
    # The namespaced lookup fired as a (provider, model_id) pair query with
    # the head and the tail split on the FIRST slash.
    pair_calls = [
        args for sql, args in conn.fetchrow_calls
        if "provider = $" in sql and "model_id = $" in sql
    ]
    assert pair_calls == [("together", "Qwen/Qwen3-235B-A22B-Instruct-2507-tput")]


def test_resolve_handles_registry_name_in_namespaced_head(monkeypatch):
    """Callers may use either GRAEAE name (`claude/`) or registry name
    (`anthropic/`) as the namespace head. The namespaced lookup must
    accept either by re-mapping the head through _REGISTRY_MAP for the
    WHERE clause and normalising the final answer back to the GRAEAE
    name for the caller."""
    from mnemos.api.routes import openai_compat

    conn = _NamespacedLookupConn(
        pair_rows=[{"provider": "anthropic", "model_id": "claude-opus-4-7"}]
    )
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model(
        "anthropic/claude-opus-4-7"
    ))
    # GRAEAE-normalised: 'anthropic' → 'claude'.
    assert result == "claude"
    # The pair lookup fired with the head as the registry-provider filter
    # ('anthropic' is not a GRAEAE provider key, so it passes through).
    pair_calls = [
        args for sql, args in conn.fetchrow_calls
        if "provider = $" in sql and "model_id = $" in sql
    ]
    assert pair_calls == [("anthropic", "claude-opus-4-7")]


def test_resolve_falls_back_to_heuristic_on_registry_miss(monkeypatch):
    from mnemos.api.routes import openai_compat

    conn = _registry_conn(row=None)
    _install(monkeypatch, conn)

    # "claude-*" is in _FALLBACK_PROVIDER_MAP
    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-custom-local"))
    assert result == "claude"


def test_resolve_returns_none_on_complete_miss(monkeypatch):
    """Registry miss + no substring match -> None. Caller raises 400."""
    from mnemos.api.routes import openai_compat

    conn = _registry_conn(row=None)
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("unknown-xyz-v9"))
    assert result is None


def test_resolve_falls_back_when_db_query_raises(monkeypatch):
    """Transient DB failures must NOT 500 the gateway. Fall through
    to substring heuristic."""
    from mnemos.api.routes import openai_compat

    conn = _registry_conn(raise_on_query=True)
    _install(monkeypatch, conn)

    # gpt-* hits the fallback heuristic
    result = asyncio.run(openai_compat._resolve_provider_for_model("gpt-5-mini"))
    assert result == "openai"


def test_resolve_falls_back_when_pool_missing(monkeypatch):
    """Pre-lifespan state: _pool is None. Still fall through to
    heuristic so /v1/chat/completions stays usable during startup
    or in degenerate environments."""
    from mnemos.api.routes import openai_compat

    _install_no_pool(monkeypatch)
    result = asyncio.run(openai_compat._resolve_provider_for_model("llama-3.3-70b-versatile"))
    assert result == "groq"


# ─── _route_to_provider ─────────────────────────────────────────────────────


def test_route_unknown_model_raises_404(monkeypatch):
    """Unknown model_id is an explicit OpenAI-style 404, NOT a silent
    route to groq. Operators see the failure at the edge instead of
    getting garbage responses from the wrong provider."""
    from mnemos.api.routes import openai_compat

    conn = _registry_conn(row=None)
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat._route_to_provider(
            model="definitely-not-a-real-model-9999",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.7, max_tokens=100,
            user=_user(),
        ))
    assert exc.value.status_code == 404
    assert exc.value.detail["error"]["code"] == "model_not_found"


def test_route_uses_registry_hit_over_heuristic(monkeypatch):
    """If the registry says model X belongs to provider A but the
    substring heuristic would say B, the registry wins. Allows
    operators to re-map providers without editing code."""
    from mnemos.api.routes import openai_compat

    # Registry says "claude-*" is served by a custom provider
    # registered as "my-local-anthropic-proxy". The substring
    # heuristic would say "claude". Registry wins.
    conn = _registry_conn(model="claude-opus-4-6", row={"provider": "my-local-anthropic-proxy"})
    _install(monkeypatch, conn)

    result = asyncio.run(openai_compat._resolve_provider_for_model("claude-opus-4-6"))
    assert result == "my-local-anthropic-proxy"
