"""SqliteCompressionQueueRepository — job 019e7049 GAP1 CHILD E.

Exercises the backend-agnostic compression-queue ABC end-to-end on the
SQLite backend: enqueue -> dequeue (priority order, attempts bump) ->
mark_done/failed -> stale sweep terminalization. SQLite is ABC-completeness
only (not a hive contest target), but it must honour the identical schema +
feature set + semantics as Postgres/Oracle.
"""

from __future__ import annotations

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    class _S:
        class database:
            embedding_dim = 1024

    be = SqliteBackend(tmp_path / "mcq.db", _S())
    await be.open()
    yield be
    await be.close()


async def _add_memory(be, mem_id: str, *, content: str = "x", category: str = "facts"):
    async with be.transactional() as tx:
        await be.memories.insert_memory(
            tx,
            memory_id=mem_id,
            content=content,
            category=category,
            subcategory=None,
            metadata_json="{}",
            quality_rating=75,
            owner_id="alice",
            namespace="alice-ns",
            permission_mode=0,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=content,
            created=None,
            updated=None,
        )


@pytest.mark.asyncio
async def test_enqueue_dequeue_mark_done(backend):
    await _add_memory(backend, "mem-a")
    async with backend.transactional() as tx:
        enq = await backend.compression_queue.enqueue_compression(
            tx, memory_ids=["mem-a"], reason="manual", priority=0, scoring_profile="balanced"
        )
    assert enq == ["mem-a"]

    async with backend.transactional() as tx:
        claimed = await backend.compression_queue.dequeue_compression(tx, limit=5)
    assert len(claimed) == 1
    row = claimed[0]
    assert row["memory_id"] == "mem-a"
    assert row["attempts"] == 1  # post-claim increment
    qid = row["id"]

    async with backend.transactional() as tx:
        await backend.compression_queue.mark_compression_done(tx, queue_id=qid)
        again = await backend.compression_queue.dequeue_compression(tx, limit=5)
    assert again == []  # nothing left pending


@pytest.mark.asyncio
async def test_enqueue_skips_unknown_memory(backend):
    async with backend.transactional() as tx:
        enq = await backend.compression_queue.enqueue_compression(
            tx, memory_ids=["ghost"], reason="manual", priority=0, scoring_profile="balanced"
        )
    assert enq == []


@pytest.mark.asyncio
async def test_dequeue_priority_order(backend):
    await _add_memory(backend, "mem-lo")
    await _add_memory(backend, "mem-hi")
    async with backend.transactional() as tx:
        await backend.compression_queue.enqueue_compression(
            tx, memory_ids=["mem-lo"], reason="manual", priority=1, scoring_profile="balanced"
        )
        await backend.compression_queue.enqueue_compression(
            tx, memory_ids=["mem-hi"], reason="manual", priority=9, scoring_profile="balanced"
        )
    async with backend.transactional() as tx:
        first = await backend.compression_queue.dequeue_compression(tx, limit=1)
    assert first[0]["memory_id"] == "mem-hi"  # higher priority first


@pytest.mark.asyncio
async def test_enqueue_all_only_uncompressed(backend):
    await _add_memory(backend, "mem-1")
    await _add_memory(backend, "mem-2")
    async with backend.transactional() as tx:
        n = await backend.compression_queue.enqueue_all_compression(
            tx,
            reason="scheduled",
            priority=0,
            scoring_profile="balanced",
            category=None,
            only_uncompressed=True,
            limit=100,
        )
    assert n == 2


@pytest.mark.asyncio
async def test_sweep_terminalization(backend):
    # Drive a row to 'running' then force the three terminalization cases by
    # editing attempts/error directly, mirroring the PG/Oracle sweep contract.
    await _add_memory(backend, "mem-s")
    async with backend.transactional() as tx:
        await backend.compression_queue.enqueue_compression(
            tx, memory_ids=["mem-s"], reason="manual", priority=0, scoring_profile="balanced"
        )
        claimed = await backend.compression_queue.dequeue_compression(tx, limit=1)
    qid = claimed[0]["id"]

    # attempts >= max + real content error + stale -> failed.
    async with backend.transactional() as tx:
        await tx.conn.execute(
            "UPDATE memory_compression_queue SET attempts=3, error='boom', "
            "started_at=datetime('now','-1 hour') WHERE id=?",
            (qid,),
        )
        swept = await backend.compression_queue.sweep_stale_compression(tx, stale_threshold_secs=600, max_attempts=3)
    assert swept == 1
    async with backend.transactional() as tx:
        cur = await tx.conn.execute("SELECT status FROM memory_compression_queue WHERE id=?", (qid,))
        st = (await cur.fetchone())["status"]
    assert st == "failed"


@pytest.mark.asyncio
async def test_sweep_infra_retry_resets_and_decrements(backend):
    await _add_memory(backend, "mem-i")
    async with backend.transactional() as tx:
        await backend.compression_queue.enqueue_compression(
            tx, memory_ids=["mem-i"], reason="manual", priority=0, scoring_profile="balanced"
        )
        claimed = await backend.compression_queue.dequeue_compression(tx, limit=1)
    qid = claimed[0]["id"]
    async with backend.transactional() as tx:
        await tx.conn.execute(
            "UPDATE memory_compression_queue SET attempts=3, error='infra_retry: x', "
            "started_at=datetime('now','-1 hour') WHERE id=?",
            (qid,),
        )
        swept = await backend.compression_queue.sweep_stale_compression(tx, stale_threshold_secs=600, max_attempts=3)
    assert swept == 1
    async with backend.transactional() as tx:
        cur = await tx.conn.execute("SELECT status, attempts FROM memory_compression_queue WHERE id=?", (qid,))
        row = await cur.fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 2  # decremented from 3
