"""NG-MN-13: PATCH of a memory's content must refresh its stored embedding.

Regression: ``PATCH /memories/{id}`` with new content committed the new
text but left the old vector in place, so semantic search kept matching
the previous text. ``update_memory`` never called the embedder or
``upsert_memory_embedding`` (the only in-module call is the bulk create
path); the replica embed worker (``domain/federation/local_embed.py``)
covers federated rows only.

Acceptance (from the task):

* Failing route test first (SQLite + fake embedder): PATCH content ->
  the stored vector equals embed(new content).
* Embedding is computed after the update commits (outside the
  transaction, like create).
* On embedder failure the stale vector is cleared (embedding NULL) and
  a WARNING logged so the backfill picks the row up.
* Metadata-only PATCH does not re-embed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_route
from mnemos.domain.models import MemoryCreateRequest, MemoryUpdateRequest
from mnemos.persistence.sqlite import SqliteBackend


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    backend = SqliteBackend(
        tmp_path / "patch-reembed.sqlite3",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=8)),
    )
    await backend.open()
    try:
        yield backend
    finally:
        await backend.close()


def _user(user_id: str = "alice") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _embed(text: str) -> list[float]:
    """Deterministic fake embedder: vector is a pure function of text."""
    seed = sum(ord(c) for c in text) % 100
    return [float(seed + i) / 100.0 for i in range(8)]


async def _stored_vector(backend: SqliteBackend, memory_id: str):
    """Read the raw stored ``memories.embedding`` JSON for one row."""
    from mnemos.persistence.sqlite import _fetch_val

    async with backend.transactional() as tx:
        raw = await _fetch_val(tx.conn, "SELECT embedding FROM memories WHERE id = ?", (memory_id,))
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else list(raw)


@pytest.fixture
def _route_no_embed(monkeypatch):
    async def _fake_embed(text: str):
        return _embed(text)

    async def _no_publish(*_args, **_kwargs):
        return None

    async def _no_invalidate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(memories_route, "_get_embedding", _fake_embed)
    monkeypatch.setattr(memories_route, "_publish_nats_with_timeout", _no_publish)
    monkeypatch.setattr(memories_route, "_invalidate_caches_after_mutation", _no_invalidate)


@pytest.mark.asyncio
async def test_patch_content_reembeds_stored_vector(
    sqlite_backend, tmp_path, monkeypatch, _route_no_embed
):
    """PATCH content -> stored vector equals embed(new content)."""
    from fastapi import Response

    monkeypatch.setattr(lifecycle, "_persistence_backend", sqlite_backend)
    created = await memories_route.create_memory(
        MemoryCreateRequest(content="the quick brown fox", category="facts"),
        Response(),
        user=_user(),
    )
    # Create stores the vector inline (dim guard off on sqlite -> raw 8-dim OK).
    vec_before = await _stored_vector(sqlite_backend, created.id)
    assert vec_before == _embed("the quick brown fox"), (
        f"setup: create must store embed(content); got {vec_before!r}"
    )

    # Track whether the re-embed happens inside the transaction.
    state = {"in_tx": False, "embed_inside_tx": 0, "embed_calls": 0}

    async def _tracking_embed(text: str):
        state["embed_calls"] += 1
        if state["in_tx"]:
            state["embed_inside_tx"] += 1
        return _embed(text)

    monkeypatch.setattr(memories_route, "_get_embedding", _tracking_embed)
    original_transactional = sqlite_backend.transactional

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _tracking_transactional(*args, **kwargs):
        state["in_tx"] = True
        try:
            async with original_transactional(*args, **kwargs) as tx:
                yield tx
        finally:
            state["in_tx"] = False

    monkeypatch.setattr(sqlite_backend, "transactional", _tracking_transactional)

    await memories_route.update_memory(
        created.id,
        MemoryUpdateRequest(content="completely different zebra text"),
        user=_user(),
    )
    vec_after = await _stored_vector(sqlite_backend, created.id)
    assert vec_after == _embed("completely different zebra text"), (
        "NG-MN-13 regression: PATCH kept the old embedding "
        f"(before={vec_before!r} after={vec_after!r})"
    )
    assert vec_after != vec_before
    assert state["embed_calls"] >= 1, "PATCH content must call the embedder"
    assert state["embed_inside_tx"] == 0, (
        "re-embed must run AFTER the update commits (outside the transaction)"
    )


@pytest.mark.asyncio
async def test_patch_metadata_only_does_not_reembed(
    sqlite_backend, tmp_path, monkeypatch, _route_no_embed
):
    """Metadata-only PATCH must not touch the stored vector."""
    from fastapi import Response

    calls: list[str] = []

    async def _counting_embed(text: str):
        calls.append(text)
        return _embed(text)

    monkeypatch.setattr(memories_route, "_get_embedding", _counting_embed)
    monkeypatch.setattr(lifecycle, "_persistence_backend", sqlite_backend)
    created = await memories_route.create_memory(
        MemoryCreateRequest(content="stable content here", category="facts"),
        Response(),
        user=_user(),
    )
    vec_before = await _stored_vector(sqlite_backend, created.id)
    n_calls_after_create = len(calls)

    await memories_route.update_memory(
        created.id,
        MemoryUpdateRequest(metadata={"note": "just a label"}),
        user=_user(),
    )
    vec_after = await _stored_vector(sqlite_backend, created.id)
    assert vec_after == vec_before, "metadata-only PATCH must not change the stored vector"
    assert len(calls) == n_calls_after_create, "metadata-only PATCH must not call the embedder"


@pytest.mark.asyncio
async def test_patch_embed_failure_clears_stale_vector(
    sqlite_backend, tmp_path, monkeypatch, _route_no_embed, caplog
):
    """On embedder failure the stale vector is cleared (NULL) + WARNING logged."""
    from fastapi import Response

    state = {"fail": False}

    async def _flaky_embed(text: str):
        if state["fail"]:
            raise RuntimeError("model exploded")
        return _embed(text)

    monkeypatch.setattr(memories_route, "_get_embedding", _flaky_embed)
    monkeypatch.setattr(lifecycle, "_persistence_backend", sqlite_backend)
    created = await memories_route.create_memory(
        MemoryCreateRequest(content="original text here", category="facts"),
        Response(),
        user=_user(),
    )
    vec_before = await _stored_vector(sqlite_backend, created.id)
    assert vec_before == _embed("original text here")

    state["fail"] = True
    with caplog.at_level(logging.WARNING, logger="mnemos.api.routes.memories"):
        await memories_route.update_memory(
            created.id,
            MemoryUpdateRequest(content="new text after failure"),
            user=_user(),
        )

    vec_after = await _stored_vector(sqlite_backend, created.id)
    assert vec_after is None, (
        "embedder failure must clear the stale vector (NULL) so the "
        f"backfill picks the row up; got {vec_after!r}"
    )
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "embedder failure on PATCH must log a WARNING"
    )
