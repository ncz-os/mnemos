"""Cross-backend compression-queue integration tests — job 019e7049 CHILD E.

Exercises the backend-agnostic CompressionQueueRepository ABC across every
available backend: SQLite (always), Postgres (MNEMOS_TEST_DB), Oracle
(ORACLE_DSN). Each arm runs the same contract:
  * enqueue → dequeue → mark_done lifecycle
  * attempts increment during the claim
  * dup-pending dedup (same memory re-enqueued while pending → skipped)
  * stale-sweep terminalization + infra_retry reset

ABC-completeness gate — not a hive target.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio


# ── Backend detection ────────────────────────────────────────────────────────
PG_URL = os.environ.get("MNEMOS_TEST_DB")
ORACLE_DSN = os.environ.get("ORACLE_DSN")


def _backend_params() -> list[str]:
    """Enumerate available compression-queue backends.

    SQLite always runs.  Postgres / Oracle arms skip cleanly when their
    respective env vars are absent.
    """
    params = ["sqlite"]
    if PG_URL:
        params.append("postgres")
    if ORACLE_DSN:
        params.append("oracle")
    return params


# ── Shared helpers ────────────────────────────────────────────────────────────


async def _add_memory_sqlite(be: Any, mem_id: str, *, content: str = "x", category: str = "facts") -> None:
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


async def _add_memory_pg(be: Any, mem_id: str, *, content: str = "x", category: str = "facts") -> None:
    async with be.transactional() as tx:
        conn = tx.conn
        await conn.execute(
            "INSERT INTO memories (id, content, category, subcategory, metadata, "
            "content_hash, quality_rating, verbatim_content, owner_id, namespace, "
            "permission_mode) "
            "VALUES ($1, $2, $3, NULL, '{}', "
            "encode(sha256($2::bytea), 'hex'), 75, $2, 'alice', 'alice-ns', 0)",
            mem_id, content, category,
        )


async def _add_memory_oracle(be: Any, mem_id: str, *, content: str = "x", category: str = "facts") -> None:
    import hashlib
    async with be.transactional() as tx:
        conn = tx._conn
        cursor = await conn.cursor()
        try:
            ch = hashlib.sha256(content.encode()).hexdigest()
            await cursor.execute(
                "INSERT INTO memories (id, content, category, subcategory, metadata, "
                "content_hash, quality_rating, verbatim_content, owner_id, namespace, "
                "permission_mode) "
                "VALUES (:id, :content, :category, NULL, '{}', "
                ":hash, 75, :content, 'alice', 'alice-ns', 0)",
                {"id": mem_id, "content": content, "category": category, "hash": ch},
            )
        finally:
            await cursor.close()


async def _cleanup_pg(be: Any, prefix: str) -> None:
    async with be.transactional() as tx:
        await tx.conn.execute(
            "DELETE FROM memory_compression_queue WHERE owner_id LIKE $1", f"{prefix}%"
        )
        await tx.conn.execute(
            "DELETE FROM memories WHERE owner_id LIKE $1", f"{prefix}%"
        )


async def _cleanup_oracle(be: Any, prefix: str) -> None:
    async with be.transactional() as tx:
        cursor = await tx._conn.cursor()
        try:
            await cursor.execute(
                "DELETE FROM memory_compression_queue WHERE owner_id LIKE :p",
                {"p": f"{prefix}%"},
            )
            await cursor.execute(
                "DELETE FROM memories WHERE owner_id LIKE :p",
                {"p": f"{prefix}%"},
            )
        finally:
            await cursor.close()


def _unique_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ── Fixture: build one backend per param ──────────────────────────────────────


@pytest_asyncio.fixture(params=_backend_params())
async def backend_case(request, tmp_path):
    """Yield (name, backend, unique_prefix) for the current backend arm.

    SQLite uses an in-memory DB.  Postgres/Oracle arms skip when their
    DSN env var is absent.  Cleanup is best-effort per arm.
    """
    arm = request.param
    prefix = f"cqcb_{arm}_{uuid.uuid4().hex[:8]}"

    if arm == "sqlite":
        from mnemos.persistence.sqlite import SqliteBackend

        be = SqliteBackend(":memory:", SimpleNamespace(database=SimpleNamespace(embedding_dim=768)))
        await be.open()
        try:
            yield (arm, be, prefix)
        finally:
            await be.close()

    elif arm == "postgres":
        import asyncpg
        from mnemos.persistence.postgres import PostgresBackend

        pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2)
        be = PostgresBackend(pool, SimpleNamespace(database=SimpleNamespace(embedding_dim=768)))
        try:
            yield (arm, be, prefix)
        finally:
            await _cleanup_pg(be, prefix)
            await pool.close()

    elif arm == "oracle":
        from mnemos.persistence.oracle import OracleBackend

        be = OracleBackend(ORACLE_DSN, SimpleNamespace(database=SimpleNamespace(embedding_dim=768)))
        await be.open()
        try:
            yield (arm, be, prefix)
        finally:
            await _cleanup_oracle(be, prefix)
            await be.close()


async def _add_memory(backend_case: tuple, mem_id: str, **kw: Any) -> None:
    """Add a memory row via the backend-appropriate path."""
    arm, be, _prefix = backend_case
    if arm == "sqlite":
        await _add_memory_sqlite(be, mem_id, **kw)
    elif arm == "postgres":
        await _add_memory_pg(be, mem_id, **kw)
    elif arm == "oracle":
        await _add_memory_oracle(be, mem_id, **kw)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_dequeue_mark_done_lifecycle(backend_case):
    """Full enqueue → dequeue → mark_done cycle on every backend."""
    arm, be, prefix = backend_case
    mid = _unique_id(prefix)

    await _add_memory(backend_case, mid)

    # Enqueue
    async with be.transactional() as tx:
        enq = await be.compression_queue.enqueue_compression(
            tx, memory_ids=[mid], reason="manual", priority=0, scoring_profile="balanced"
        )
    assert enq == [mid], f"[{arm}] enqueue should return the enqueued id"

    # Dequeue — claims the pending row, increments attempts
    async with be.transactional() as tx:
        claimed = await be.compression_queue.dequeue_compression(tx, limit=5)
    assert len(claimed) == 1, f"[{arm}] should claim exactly one row"
    row = claimed[0]
    assert row["memory_id"] == mid, f"[{arm}] claimed memory_id mismatch"
    assert row["attempts"] >= 1, f"[{arm}] attempts should be >=1 after claim, got {row['attempts']}"
    qid = row["id"]

    # Mark done — queue is drained
    async with be.transactional() as tx:
        await be.compression_queue.mark_compression_done(tx, queue_id=qid)
        again = await be.compression_queue.dequeue_compression(tx, limit=5)
    assert again == [], f"[{arm}] queue should be empty after mark_done"


@pytest.mark.asyncio
async def test_attempts_increment(backend_case):
    """Each dequeue increments attempts; verify across two dequeues."""
    arm, be, prefix = backend_case
    mid = _unique_id(prefix)

    await _add_memory(backend_case, mid)

    async with be.transactional() as tx:
        await be.compression_queue.enqueue_compression(
            tx, memory_ids=[mid], reason="manual", priority=0, scoring_profile="balanced"
        )
        # First dequeue: attempts → 1
        c1 = await be.compression_queue.dequeue_compression(tx, limit=1)
        assert c1[0]["attempts"] == 1, f"[{arm}] first dequeue attempts should be 1, got {c1[0]['attempts']}"

    # Reset to pending so we can dequeue again (simulates retry)
    async with be.transactional() as tx:
        if arm == "sqlite":
            await tx.conn.execute(
                "UPDATE memory_compression_queue SET status='pending', started_at=NULL WHERE id=?",
                (c1[0]["id"],),
            )
        elif arm == "postgres":
            await tx.conn.execute(
                "UPDATE memory_compression_queue SET status='pending', started_at=NULL WHERE id=$1",
                str(c1[0]["id"]),
            )
        elif arm == "oracle":
            cursor = await tx._conn.cursor()
            try:
                await cursor.execute(
                    "UPDATE memory_compression_queue SET status='pending', started_at=NULL WHERE id=:id",
                    {"id": c1[0]["id"]},
                )
            finally:
                await cursor.close()

    # Second dequeue: attempts → 2
    async with be.transactional() as tx:
        c2 = await be.compression_queue.dequeue_compression(tx, limit=1)
        assert c2[0]["attempts"] == 2, f"[{arm}] second dequeue attempts should be 2, got {c2[0]['attempts']}"


@pytest.mark.asyncio
async def test_dup_pending_dedup(backend_case):
    """Re-enqueuing the same memory while a pending row exists is a no-op."""
    arm, be, prefix = backend_case
    mid = _unique_id(prefix)

    await _add_memory(backend_case, mid)

    # First enqueue
    async with be.transactional() as tx:
        enq1 = await be.compression_queue.enqueue_compression(
            tx, memory_ids=[mid], reason="manual", priority=0, scoring_profile="balanced"
        )
    assert enq1 == [mid], f"[{arm}] first enqueue should succeed"

    # Second enqueue of same memory while still pending → deduped
    async with be.transactional() as tx:
        enq2 = await be.compression_queue.enqueue_compression(
            tx, memory_ids=[mid], reason="manual", priority=0, scoring_profile="balanced"
        )
    assert enq2 == [], f"[{arm}] second enqueue should be deduped (no-op)"

    # Only one row in the queue
    async with be.transactional() as tx:
        claimed = await be.compression_queue.dequeue_compression(tx, limit=5)
    assert len(claimed) == 1, f"[{arm}] only one row should exist after dedup"
    assert claimed[0]["memory_id"] == mid

    # After marking done, a re-enqueue IS allowed
    async with be.transactional() as tx:
        await be.compression_queue.mark_compression_done(tx, queue_id=claimed[0]["id"])
        enq3 = await be.compression_queue.enqueue_compression(
            tx, memory_ids=[mid], reason="reprocess", priority=0, scoring_profile="balanced"
        )
    assert enq3 == [mid], f"[{arm}] re-enqueue after done should succeed"


@pytest.mark.asyncio
async def test_sweep_terminalization(backend_case):
    """Stale 'running' row with content error → terminalized as 'failed'."""
    arm, be, prefix = backend_case
    mid = _unique_id(prefix)

    await _add_memory(backend_case, mid)

    async with be.transactional() as tx:
        await be.compression_queue.enqueue_compression(
            tx, memory_ids=[mid], reason="manual", priority=0, scoring_profile="balanced"
        )
        claimed = await be.compression_queue.dequeue_compression(tx, limit=1)
    qid = claimed[0]["id"]

    # Set attempts >= max with a real content error and an old started_at
    async with be.transactional() as tx:
        if arm == "sqlite":
            await tx.conn.execute(
                "UPDATE memory_compression_queue SET attempts=3, error='boom', "
                "started_at=datetime('now','-1 hour') WHERE id=?",
                (qid,),
            )
        elif arm == "postgres":
            await tx.conn.execute(
                "UPDATE memory_compression_queue SET attempts=3, error='boom', "
                "started_at=NOW() - INTERVAL '1 hour' WHERE id=$1",
                str(qid),
            )
        elif arm == "oracle":
            cursor = await tx._conn.cursor()
            try:
                await cursor.execute(
                    "UPDATE memory_compression_queue SET attempts=3, error='boom', "
                    "started_at=SYSTIMESTAMP - INTERVAL '1' HOUR WHERE id=:id",
                    {"id": qid},
                )
            finally:
                await cursor.close()

    async with be.transactional() as tx:
        swept = await be.compression_queue.sweep_stale_compression(
            tx, stale_threshold_secs=600, max_attempts=3
        )
    assert swept == 1, f"[{arm}] sweep should reclaim one row"

    # Verify terminalized
    async with be.transactional() as tx:
        if arm == "sqlite":
            cur = await tx.conn.execute("SELECT status FROM memory_compression_queue WHERE id=?", (qid,))
            st = (await cur.fetchone())["status"]
        elif arm == "postgres":
            st = await tx.conn.fetchval("SELECT status FROM memory_compression_queue WHERE id=$1", str(qid))
        elif arm == "oracle":
            cursor = await tx._conn.cursor()
            try:
                await cursor.execute("SELECT status FROM memory_compression_queue WHERE id=:id", {"id": qid})
                st = (await cursor.fetchone())[0]
            finally:
                await cursor.close()
    assert st == "failed", f"[{arm}] terminalized status should be 'failed', got {st!r}"


@pytest.mark.asyncio
async def test_sweep_infra_retry_reset(backend_case):
    """Stale 'running' row with infra_retry error → reset to 'pending' with
    decremented attempts."""
    arm, be, prefix = backend_case
    mid = _unique_id(prefix)

    await _add_memory(backend_case, mid)

    async with be.transactional() as tx:
        await be.compression_queue.enqueue_compression(
            tx, memory_ids=[mid], reason="manual", priority=0, scoring_profile="balanced"
        )
        claimed = await be.compression_queue.dequeue_compression(tx, limit=1)
    qid = claimed[0]["id"]

    async with be.transactional() as tx:
        if arm == "sqlite":
            await tx.conn.execute(
                "UPDATE memory_compression_queue SET attempts=3, error='infra_retry: x', "
                "started_at=datetime('now','-1 hour') WHERE id=?",
                (qid,),
            )
        elif arm == "postgres":
            await tx.conn.execute(
                "UPDATE memory_compression_queue SET attempts=3, error='infra_retry: x', "
                "started_at=NOW() - INTERVAL '1 hour' WHERE id=$1",
                str(qid),
            )
        elif arm == "oracle":
            cursor = await tx._conn.cursor()
            try:
                await cursor.execute(
                    "UPDATE memory_compression_queue SET attempts=3, error='infra_retry: x', "
                    "started_at=SYSTIMESTAMP - INTERVAL '1' HOUR WHERE id=:id",
                    {"id": qid},
                )
            finally:
                await cursor.close()

    async with be.transactional() as tx:
        swept = await be.compression_queue.sweep_stale_compression(
            tx, stale_threshold_secs=600, max_attempts=3
        )
    assert swept == 1, f"[{arm}] sweep should reclaim one row"

    # Verify reset to pending with decremented attempts
    async with be.transactional() as tx:
        if arm == "sqlite":
            cur = await tx.conn.execute(
                "SELECT status, attempts FROM memory_compression_queue WHERE id=?", (qid,)
            )
            row = await cur.fetchone()
        elif arm == "postgres":
            row = await tx.conn.fetchrow(
                "SELECT status, attempts FROM memory_compression_queue WHERE id=$1", str(qid)
            )
        elif arm == "oracle":
            cursor = await tx._conn.cursor()
            try:
                await cursor.execute(
                    "SELECT status, attempts FROM memory_compression_queue WHERE id=:id", {"id": qid}
                )
                raw = await cursor.fetchone()
                row = {"status": raw[0], "attempts": raw[1]} if raw else None
            finally:
                await cursor.close()
    assert row["status"] == "pending", f"[{arm}] infra_retry should reset to 'pending', got {row['status']!r}"
    assert row["attempts"] == 2, f"[{arm}] infra_retry should decrement attempts to 2, got {row['attempts']}"


@pytest.mark.asyncio
async def test_enqueue_skips_unknown_memory(backend_case):
    """Enqueue of a non-existent memory id returns []."""
    arm, be, prefix = backend_case
    async with be.transactional() as tx:
        enq = await be.compression_queue.enqueue_compression(
            tx, memory_ids=[_unique_id(prefix)], reason="manual", priority=0, scoring_profile="balanced"
        )
    assert enq == [], f"[{arm}] unknown memory should yield empty enqueue"


@pytest.mark.asyncio
async def test_dequeue_priority_order(backend_case):
    """Higher-priority rows are dequeued first."""
    arm, be, prefix = backend_case
    lo = _unique_id(prefix)
    hi = _unique_id(prefix)
    await _add_memory(backend_case, lo)
    await _add_memory(backend_case, hi)

    async with be.transactional() as tx:
        await be.compression_queue.enqueue_compression(
            tx, memory_ids=[lo], reason="manual", priority=1, scoring_profile="balanced"
        )
        await be.compression_queue.enqueue_compression(
            tx, memory_ids=[hi], reason="manual", priority=9, scoring_profile="balanced"
        )

    async with be.transactional() as tx:
        first = await be.compression_queue.dequeue_compression(tx, limit=1)
    assert first[0]["memory_id"] == hi, f"[{arm}] highest priority (9) should dequeue before priority 1"
