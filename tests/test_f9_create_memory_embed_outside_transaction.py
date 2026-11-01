"""F-9: ``create_memory`` must not embed while the transaction is open.

Source: Fable federation report (2026-10-31, commit 0073bb4), F-9 PERF api:

    "F-9 PERF api: move embedding out of the ``create_memory``
    transaction (mirror the bulk path; today ``routes/memories.py:1755``,
    ``:1814`` embed inside the transaction, holding a pool connection
    or the SQLite global lock) (S)."

Re-verified against master 68d5891a (48 commits newer than the
report's 0073bb4): the embedding call lived at
``mnemos/api/routes/memories.py`` inside the
``async with backend.transactional() as tx:`` block, while
``bulk_create_memories`` had already moved its batch embed
(``_get_embeddings_batch``) OUTSIDE the transaction. The line numbers
in the report have moved; the defect it described was still present.

Why this matters on each backend:

* Postgres: ``backend.transactional()`` pins ONE pool connection for
  the whole transaction (the pool defaults to 5 connections / 20 max).
  Embedding inside the transaction holds that connection for the whole
  model round trip.
* SQLite: ``SqliteBackend.transactional()`` takes ONE global
  ``asyncio.Lock`` for the entire transaction (see
  ``mnemos/persistence/sqlite.py`` ``async with self._lock:``). Embedding
  inside the transaction blocks every other SQLite reader and writer in
  the process.

What must NOT change: the vector is still written co-transactionally
(passed inline to ``insert_memory``), and an embedding failure still
degrades to ``embedding=NULL`` with the row committed.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories
from mnemos.domain.models import MemoryCreateRequest

from tests._fake_backend import install_fake_backend


def _user() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _memory_row(memory_id: str = "mem_test") -> dict:
    return {
        "id": memory_id,
        "content": "remember this",
        "category": "facts",
        "subcategory": None,
        "metadata": {},
        "quality_rating": 75,
        "verbatim_content": "remember this",
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "compressed_content": None,
        "created": "2026-04-29T12:00:00",
        "updated": "2026-04-29T12:00:00",
    }


class _TrackingBackend:
    """Fake backend that records whether the embed ran inside the tx.

    ``install_fake_backend`` gives a plain ``FakeBackend`` whose
    ``transactional()`` yields a throwaway namespace. Here we need to
    know, at embed time, whether a transaction was open — so we wrap
    the fake's context manager and flip a flag around the yield.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.in_transaction = False
        self.embed_calls = 0
        self.embed_inside_tx = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_create_memory_embeds_outside_the_transaction(monkeypatch):
    """The embedding generation must NOT run inside the transaction.

    This is the regression the F-9 step fixes: on the base, the embed
    call is between ``async with backend.transactional()`` and the
    ``insert_memory`` call, so the model round trip runs while a pool
    connection (Postgres) or the global lock (SQLite) is held.
    """
    inner = install_fake_backend(monkeypatch)
    inner.memories.configure_return("get_memory", _memory_row())

    monkeypatch.setattr(memories, "_schedule_outbox_deliveries", lambda ids: None)
    monkeypatch.setattr(memories, "_publish_nats_with_timeout", _noop_publish)
    monkeypatch.setattr(memories, "_invalidate_caches_after_mutation", _noop_invalidate)

    state = {"in_tx": False, "embed_inside_tx": 0, "embed_calls": 0}

    async def _fake_embed(text: str) -> list[float]:
        state["embed_calls"] += 1
        if state["in_tx"]:
            state["embed_inside_tx"] += 1
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(memories, "_get_embedding", _fake_embed)

    # Wrap the fake backend's transactional() so we can observe whether
    # a transaction is open when the embed runs.
    original_transactional = inner.transactional

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _tracking_transactional(*args, **kwargs):
        state["in_tx"] = True
        try:
            async with original_transactional(*args, **kwargs) as tx:
                yield tx
        finally:
            state["in_tx"] = False

    monkeypatch.setattr(inner, "transactional", _tracking_transactional)

    response = await memories.create_memory(
        MemoryCreateRequest(content="remember this", category="facts"),
        Response(),
        user=_user(),
    )

    assert response.id == "mem_test"
    assert state["embed_calls"] == 1, "the create path must still embed exactly once"
    assert state["embed_inside_tx"] == 0, (
        "F-9 regression: create_memory generated the embedding INSIDE the "
        "transaction. The model round trip must run before the transaction "
        "opens so no pool connection and no SQLite global lock is held "
        "while the embedder runs (mirror the bulk_create_memories path)."
    )


async def _noop_publish(*args, **kwargs):
    return None


async def _noop_invalidate(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_create_memory_still_writes_the_vector_inline(monkeypatch):
    """Moving the embed out of the transaction must not change WHAT is
    committed: the vector is still passed inline to ``insert_memory`` so
    the VECTOR column lands in the same transaction as the row."""
    inner = install_fake_backend(monkeypatch)
    inner.memories.configure_return("get_memory", _memory_row())

    monkeypatch.setattr(memories, "_schedule_outbox_deliveries", lambda ids: None)
    monkeypatch.setattr(memories, "_publish_nats_with_timeout", _noop_publish)
    monkeypatch.setattr(memories, "_invalidate_caches_after_mutation", _noop_invalidate)

    async def _fake_embed(text: str) -> list[float]:
        return [0.5, 0.6, 0.7]

    monkeypatch.setattr(memories, "_get_embedding", _fake_embed)

    await memories.create_memory(
        MemoryCreateRequest(content="remember this", category="facts"),
        Response(),
        user=_user(),
    )

    insert_calls = [c for c in inner.memories.calls if c[0] == "insert_memory"]
    assert len(insert_calls) == 1
    assert insert_calls[0][1]["embedding"] == [0.5, 0.6, 0.7]


@pytest.mark.asyncio
async def test_create_memory_embedding_failure_still_commits_the_row(monkeypatch):
    """An embedding failure must degrade to ``embedding=NULL`` and the
    row must still commit — the try/except around the embed generation
    survives the move out of the transaction (the 2026-06-05 Codex
    adversarial-review gate)."""
    inner = install_fake_backend(monkeypatch)
    inner.memories.configure_return("get_memory", _memory_row())

    monkeypatch.setattr(memories, "_schedule_outbox_deliveries", lambda ids: None)
    monkeypatch.setattr(memories, "_publish_nats_with_timeout", _noop_publish)
    monkeypatch.setattr(memories, "_invalidate_caches_after_mutation", _noop_invalidate)

    async def _boom(text: str) -> list[float]:
        raise RuntimeError("embedder exploded")

    monkeypatch.setattr(memories, "_get_embedding", _boom)

    response = await memories.create_memory(
        MemoryCreateRequest(content="remember this", category="facts"),
        Response(),
        user=_user(),
    )

    assert response.id == "mem_test"
    insert_calls = [c for c in inner.memories.calls if c[0] == "insert_memory"]
    assert len(insert_calls) == 1
    assert insert_calls[0][1]["embedding"] is None
    assert inner.commits == 1
    assert inner.rollbacks == 0


@pytest.mark.asyncio
async def test_create_memory_transaction_error_still_rolls_back(monkeypatch):
    """The move must not change the failure contract: a DB error inside
    the transaction rolls back and surfaces as HTTP 500."""
    inner = install_fake_backend(monkeypatch)
    inner.memories.configure_raise("insert_memory", RuntimeError("db exploded"))

    monkeypatch.setattr(memories, "_schedule_outbox_deliveries", lambda ids: None)
    monkeypatch.setattr(memories, "_publish_nats_with_timeout", _noop_publish)
    monkeypatch.setattr(memories, "_invalidate_caches_after_mutation", _noop_invalidate)

    async def _fake_embed(text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(memories, "_get_embedding", _fake_embed)

    with pytest.raises(HTTPException) as exc:
        await memories.create_memory(
            MemoryCreateRequest(content="remember this", category="facts"),
            Response(),
            user=_user(),
        )

    assert exc.value.status_code == 500
    assert inner.commits == 0
    assert inner.rollbacks == 1
