"""Coverage for the embedding backfill worker.

The worker exists because embedding is otherwise inline-only: federation
pulls arrive with no vector at all, and on the join-table backends (SQLite,
MariaDB) the inline create path writes ``memories.embedding`` while
``semantic_search`` reads ``memory_embeddings``. Both leave rows that look
stored but are unreachable by a semantic query.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from mnemos.workers.embedding_worker import embed_pending_batch, embedding_worker_loop


class _FakeMemories:
    def __init__(self, pending, *, raise_not_implemented=False):
        self._pending = list(pending)
        self._raise = raise_not_implemented
        self.upserted: list[tuple[str, list[float]]] = []
        self.requested_limits: list[int] = []

    async def fetch_memories_missing_embeddings(self, tx, limit):
        if self._raise:
            raise NotImplementedError("backend cannot enumerate unembedded memories")
        self.requested_limits.append(limit)
        batch, self._pending = self._pending[:limit], self._pending[limit:]
        return batch

    async def upsert_memory_embedding(self, tx, memory_id, embedding):
        self.upserted.append((memory_id, list(embedding)))


class _FakeBackend:
    def __init__(self, memories):
        self.memories = memories

    @asynccontextmanager
    async def transactional(self):
        yield SimpleNamespace()


def _patch_embedder(monkeypatch, impl):
    monkeypatch.setattr("mnemos.runtime.embedder.embed_text", impl)


@pytest.mark.asyncio
async def test_embeds_rows_that_semantic_search_cannot_reach(monkeypatch):
    memories = _FakeMemories([{"id": "mem_a", "content": "alpha"}, {"id": "mem_b", "content": "beta"}])

    async def _embed(text):
        return [0.5, 0.5]

    _patch_embedder(monkeypatch, _embed)

    embedded = await embed_pending_batch(_FakeBackend(memories), batch_size=10)

    assert embedded == 2
    assert [m for m, _ in memories.upserted] == ["mem_a", "mem_b"]


@pytest.mark.asyncio
async def test_batch_size_is_passed_through_to_the_backend(monkeypatch):
    memories = _FakeMemories([{"id": "mem_a", "content": "alpha"}])

    async def _embed(text):
        return [1.0]

    _patch_embedder(monkeypatch, _embed)

    await embed_pending_batch(_FakeBackend(memories), batch_size=7)

    assert memories.requested_limits == [7]


@pytest.mark.asyncio
async def test_one_bad_row_does_not_block_the_rest_of_the_batch(monkeypatch):
    memories = _FakeMemories(
        [
            {"id": "mem_bad", "content": "boom"},
            {"id": "mem_good", "content": "fine"},
        ]
    )

    async def _embed(text):
        if text == "boom":
            raise RuntimeError("embedder exploded")
        return [0.1]

    _patch_embedder(monkeypatch, _embed)

    embedded = await embed_pending_batch(_FakeBackend(memories), batch_size=10)

    # The failing row is skipped, not retried in-loop, and must not take the
    # rows queued behind it down with it.
    assert embedded == 1
    assert [m for m, _ in memories.upserted] == ["mem_good"]


@pytest.mark.asyncio
async def test_empty_vector_is_not_written(monkeypatch):
    memories = _FakeMemories([{"id": "mem_a", "content": "alpha"}])

    async def _embed(text):
        return []

    _patch_embedder(monkeypatch, _embed)

    assert await embed_pending_batch(_FakeBackend(memories), batch_size=10) == 0
    assert memories.upserted == []


@pytest.mark.asyncio
async def test_nothing_pending_is_not_an_error(monkeypatch):
    memories = _FakeMemories([])

    async def _embed(text):  # pragma: no cover - must never be called
        raise AssertionError("embedder called with nothing pending")

    _patch_embedder(monkeypatch, _embed)

    assert await embed_pending_batch(_FakeBackend(memories), batch_size=10) == 0


@pytest.mark.asyncio
async def test_loop_stops_on_a_backend_that_cannot_enumerate(monkeypatch):
    """Looping forever on NotImplementedError would look healthy in the
    process list while doing nothing at all."""
    memories = _FakeMemories([], raise_not_implemented=True)

    async def _embed(text):  # pragma: no cover
        raise AssertionError("should not embed")

    _patch_embedder(monkeypatch, _embed)

    await asyncio.wait_for(
        embedding_worker_loop(_FakeBackend(memories), interval_secs=0, idle_interval_secs=0),
        timeout=5,
    )


@pytest.mark.asyncio
async def test_loop_is_cancellable(monkeypatch):
    memories = _FakeMemories([])

    async def _embed(text):  # pragma: no cover
        raise AssertionError("should not embed")

    _patch_embedder(monkeypatch, _embed)

    task = asyncio.create_task(
        embedding_worker_loop(_FakeBackend(memories), interval_secs=0.01, idle_interval_secs=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
