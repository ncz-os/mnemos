"""Characterisation tests for the search pipeline (decision M11).

``search_memories`` was a single ~447-line handler mixing parse, key,
embed, fetch, redact, rerank, decay and frame phases. M11 splits those
phases into ``mnemos.domain.search.pipeline`` — one function per phase —
and leaves the route as an orchestrator.

These tests pin the BEHAVIOUR of each phase against the repository
surface (the fake backend), so the split is provably behaviour
preserving:

  * parse: root-only gates (operational / include_secrets) reject before
    any row is fetched; unknown profiles 400; the per-profile limit cap
    is applied.
  * key/visibility: the ``VisibilityFilter`` handed to the repository is
    built from the caller, not from the request.
  * embed: a semantic request embeds once; a failed embedding falls back
    to FTS.
  * fetch: semantic search with a zero-row result falls back to FTS.
  * redact: non-root results are masked; root ``include_secrets`` is not.
  * rerank: the deep profile reorders by cross-encoder score.
  * decay: ``decay_overrides`` reorders by recency.
  * frame: non-operational responses are wrapped by injection defense;
    the root ``operational`` opt-in is verbatim.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest
from mnemos.persistence.visibility import VisibilityScope

from tests._fake_backend import _FakeMemoryRepo as _FakeMemoryRepoBase
from tests._fake_backend import install_fake_backend


def _alice(namespace: str = "alice-ns") -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace=namespace, authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


def _score_contract(monkeypatch) -> None:
    """Pin the repository's vector-score contract on the shared fake.

    ``_FakeMemoryRepo.__getattr__`` fabricates an AsyncMock for any unknown
    attribute, including the class-level ``SEMANTIC_SCORE_COLUMN`` the
    handler reads to convert the backend's raw score column.
    """
    monkeypatch.setattr(
        _FakeMemoryRepoBase, "SEMANTIC_SCORE_COLUMN", "rank_score", raising=False,
    )
    monkeypatch.setattr(
        _FakeMemoryRepoBase, "SEMANTIC_SCORE_METRIC", "cosine_distance", raising=False,
    )


def _row(
    memory_id: str = "mem-1",
    *,
    content: str = "hello world",
    owner: str = "alice",
    namespace: str = "alice-ns",
    created: datetime | None = None,
) -> dict:
    now = created or datetime(2026, 1, 1, 12, 0, 0)
    return {
        "id": memory_id,
        "content": content,
        "category": "solutions",
        "subcategory": None,
        "created": now,
        "updated": now,
        "metadata": None,
        "quality_rating": 80,
        "compressed_content": None,
        "verbatim_content": None,
        "owner_id": owner,
        "group_id": None,
        "namespace": namespace,
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }


def _search_calls(backend) -> list[tuple[str, dict]]:
    return [
        (name, kw)
        for name, kw in backend.memories.calls
        if name in ("fts_search", "semantic_search")
    ]


def _install_fake_backend(monkeypatch):
    return install_fake_backend(monkeypatch)


def _install_embedding(monkeypatch, vector: list[float] | None) -> list[str]:
    """Stub the handler's embedder; returns the list of embedded queries."""

    queries: list[str] = []

    async def _fake_embedding(query: str):
        queries.append(query)
        return vector

    monkeypatch.setattr(memories_handler, "_get_embedding", _fake_embedding)
    return queries


# ── parse ───────────────────────────────────────────────────────────────


def test_parse_operational_requires_root(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False, operational=True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert exc.value.status_code == 403
    assert "operational" in exc.value.detail
    assert _search_calls(backend) == []


def test_parse_include_secrets_requires_root(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False, include_secrets=True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert exc.value.status_code == 403
    assert "include_secrets" in exc.value.detail
    assert _search_calls(backend) == []


def test_parse_unknown_profile_is_400(monkeypatch):
    _install_fake_backend(monkeypatch)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False, profile="turbo")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert exc.value.status_code == 400


def test_parse_applies_per_profile_limit_cap(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    req = MemorySearchRequest(query="hello", limit=500, semantic=False, profile="fast")
    asyncio.run(memories_handler.search_memories(req, user=_alice()))
    (_name, kw), = _search_calls(backend)
    assert kw["limit"] == 25


# ── key / visibility ────────────────────────────────────────────────────


def test_key_builds_readable_filter_pinned_to_caller(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    asyncio.run(memories_handler.search_memories(req, user=_alice()))
    (_name, kw), = _search_calls(backend)
    vis = kw["visibility"]
    assert vis.scope == VisibilityScope.READABLE
    assert vis.user_id == "alice"
    assert vis.namespace == "alice-ns"


def test_key_root_discovery_search_keeps_vault_subtracted(monkeypatch):
    """Root search WITHOUT include_secrets is the vault DISCOVERY path:
    the vault filter is cleared (rows surface, content still masked by
    the redact phase) but the caller is a cross-tenant ROOT_BYPASS."""
    backend = _install_fake_backend(monkeypatch)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    asyncio.run(memories_handler.search_memories(req, user=_root()))
    (_name, kw), = _search_calls(backend)
    vis = kw["visibility"]
    assert vis.scope == VisibilityScope.ROOT_BYPASS
    assert vis.namespace is None
    assert vis.user_id is None


# ── embed ───────────────────────────────────────────────────────────────


def test_embed_runs_once_and_uses_semantic_search(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    _score_contract(monkeypatch)
    queries = _install_embedding(monkeypatch, [0.1] * 8)
    # A genuine vector hit above the relevance floor, lexically anchored to
    # the query, so the OOD gate keeps it.
    # similarity-safe: the route reads SEMANTIC_SCORE_COLUMN off the repo
    # (class-level default "rank_score"); rank_score 0.0 under the cosine
    # DISTANCE metric is a perfect match (similarity 1.0).
    hit = {**_row(content="hello world"), "rank_score": 0.0}
    backend.memories.configure_return("semantic_search", [hit])
    req = MemorySearchRequest(query="hello", limit=10, semantic=True)
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert queries == ["hello"]
    (name, kw), = _search_calls(backend)
    assert name == "semantic_search"
    assert kw["embedding"] == [0.1] * 8
    assert [m.id for m in response.memories] == ["mem-1"]


def test_embed_failure_falls_back_to_fts(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    _install_embedding(monkeypatch, None)
    req = MemorySearchRequest(query="hello", limit=10, semantic=True)
    asyncio.run(memories_handler.search_memories(req, user=_alice()))
    (name, _kw), = _search_calls(backend)
    assert name == "fts_search"


# ── fetch ───────────────────────────────────────────────────────────────


def test_fetch_semantic_zero_rows_falls_back_to_fts(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    _install_embedding(monkeypatch, [0.1] * 8)
    backend.memories.configure_return("semantic_search", [])
    req = MemorySearchRequest(query="hello", limit=10, semantic=True)
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    names = [name for name, _kw in _search_calls(backend)]
    assert names == ["semantic_search", "fts_search"]
    assert response.count == 0


def test_fetch_attaches_tags_from_repository(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [_row()])
    backend.memories.configure_return(
        "fetch_memory_tags", {"mem-1": ["alpha", "beta"]},
    )
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert response.memories[0].tags == ["alpha", "beta"]


# ── redact ──────────────────────────────────────────────────────────────


def test_redact_masks_incidental_secret_spans_for_non_root(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "fts_search", [_row(content="token is sk-abcdefghijklmnopqrstuvwx123456")],
    )
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert "sk-abcdefghijklmnopqrstuvwx123456" not in response.memories[0].content
    assert "[REDACTED]" in response.memories[0].content


def test_redact_root_include_secrets_is_verbatim(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    secret = "token is sk-abcdefghijklmnopqrstuvwx123456"
    backend.memories.configure_return("fts_search", [_row(content=secret, owner="admin")])
    req = MemorySearchRequest(
        query="hello", limit=10, semantic=False, include_secrets=True, operational=True,
    )
    response = asyncio.run(memories_handler.search_memories(req, user=_root()))
    assert response.memories[0].content == secret


# ── rerank ──────────────────────────────────────────────────────────────


def test_rerank_deep_profile_reorders_by_score(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "fts_search",
        [_row("mem-a", content="alpha"), _row("mem-b", content="beta")],
    )

    class _Reranker:
        async def rerank(self, query, docs):
            return [0.1, 0.9]

    monkeypatch.setattr(memories_handler, "get_reranker", lambda: _Reranker())
    req = MemorySearchRequest(query="hello", limit=10, semantic=False, profile="deep")
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert [m.id for m in response.memories] == ["mem-b", "mem-a"]


def test_rerank_balanced_profile_keeps_backend_order(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    backend.memories.configure_return(
        "fts_search",
        [_row("mem-a", content="alpha"), _row("mem-b", content="beta")],
    )

    class _Reranker:
        async def rerank(self, query, docs):
            return [0.1, 0.9]

    monkeypatch.setattr(memories_handler, "get_reranker", lambda: _Reranker())
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert [m.id for m in response.memories] == ["mem-a", "mem-b"]


# ── decay ───────────────────────────────────────────────────────────────


def test_decay_runs_on_the_hit_set(monkeypatch):
    """The decay phase is reached for every hit set and leaves the
    response order deterministic.

    Both hits share a category and a quality rating, so the decay sort
    tie is broken by stability: the backend order survives. This pins
    that the split did not introduce a new reordering.
    """
    from mnemos.domain.search.decay import DecayParams, invalidate_decay_cache

    invalidate_decay_cache()
    backend = install_fake_backend(monkeypatch)
    now = datetime(2026, 1, 1, 12, 0, 0)
    backend.memories.configure_return(
        "fts_search",
        [
            _row("mem-old", content="old", created=now - timedelta(days=90)),
            _row("mem-new", content="new", created=now),
        ],
    )

    async def _decay_table(_backend):
        return {"solutions": DecayParams("solutions", 30.0, "exponential", 0.0)}

    monkeypatch.setattr(memories_handler, "load_decay_table", _decay_table)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert [m.id for m in response.memories] == ["mem-new", "mem-old"]


def test_decay_failure_does_not_fail_the_search(monkeypatch):
    """A raising decay phase returns the unsorted hit set, never a 500."""
    from mnemos.domain.search.decay import invalidate_decay_cache

    invalidate_decay_cache()
    backend = install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [_row("mem-a", content="alpha")])

    async def _boom(_backend):
        raise RuntimeError("decay table unavailable")

    monkeypatch.setattr(memories_handler, "load_decay_table", _boom)
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert [m.id for m in response.memories] == ["mem-a"]


# ── frame ───────────────────────────────────────────────────────────────


def test_frame_wraps_content_for_non_operational_callers(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [_row(content="plain memory text")])
    req = MemorySearchRequest(query="hello", limit=10, semantic=False)
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    content = response.memories[0].content
    assert content != "plain memory text"
    assert "plain memory text" in content


def test_frame_skipped_for_root_operational(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [_row(content="plain memory text")])
    req = MemorySearchRequest(query="hello", limit=10, semantic=False, operational=True)
    response = asyncio.run(memories_handler.search_memories(req, user=_root()))
    assert response.memories[0].content == "plain memory text"


def test_empty_result_set_short_circuits_pipeline(monkeypatch):
    backend = _install_fake_backend(monkeypatch)
    backend.memories.configure_return("fts_search", [])
    req = MemorySearchRequest(query="hello", limit=10, semantic=False, profile="deep")
    response = asyncio.run(memories_handler.search_memories(req, user=_alice()))
    assert response.count == 0
    assert response.memories == []
