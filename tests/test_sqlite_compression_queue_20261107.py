"""FB1006: ``SqliteCompressionQueueRepository`` contract + queue semantics.

Contract assertions (bases, eight-method set, parameter parity, dialect guard) plus a
migrated temporary SQLite database: enqueue then dequeue returns the same item exactly
once, ordering follows ``priority DESC, enqueued_at``, re-enqueueing a pending memory is a
no-op, and marking done removes the item from the pending set.

DIVERGENCE (brief premise vs code): ``attempts`` is incremented at claim time, so an item
abandoned mid-run already carries an attempt; and ``enqueued_at`` has second resolution, so
equal-priority items enqueued in the same second have no defined order.
"""

from __future__ import annotations

import inspect

import pytest
import pytest_asyncio

from mnemos.persistence.base import CompressionQueueRepository
from mnemos.persistence.sqlite import (
    SqliteBackend,
    SqliteCompressionQueueRepository,
    SqliteTransaction,
    _SqliteRepository,
    _fetch_all,
)

METHODS = (
    "dequeue_compression",
    "enqueue_all_compression",
    "enqueue_compression",
    "get_compressed_variants_count",
    "get_queue_stats",
    "mark_compression_done",
    "mark_compression_failed",
    "sweep_stale_compression",
)
DOCUMENTED_COLUMNS = {
    "id",
    "memory_id",
    "owner_id",
    "reason",
    "status",
    "priority",
    "scoring_profile",
    "attempts",
    "enqueued_at",
    "started_at",
    "finished_at",
    "error",
}


def _params(func: object) -> list[tuple[str, object, object]]:
    return [(param.name, param.kind, param.default) for param in inspect.signature(func).parameters.values()]


class _Settings:
    class database:
        embedding_dim = 1024


@pytest_asyncio.fixture
async def backend(tmp_path):
    be = SqliteBackend(tmp_path / "queue.db", _Settings())
    await be.open()
    yield be
    await be.close()


async def _add_memory(be: SqliteBackend, memory_id: str) -> None:
    async with be.transactional() as tx:
        await be.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=f"content for {memory_id}",
            category="facts",
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
            verbatim_content=f"content for {memory_id}",
            created=None,
            updated=None,
        )


async def _enqueue(
    repo: SqliteCompressionQueueRepository, tx, memory_ids: list[str], *, priority: int = 0
) -> list[str]:
    return await repo.enqueue_compression(
        tx,
        memory_ids=memory_ids,
        reason="manual",
        priority=priority,
        scoring_profile="balanced",
    )


def test_subclasses_the_documented_mixin_and_base() -> None:
    assert SqliteCompressionQueueRepository.__bases__ == (
        _SqliteRepository,
        CompressionQueueRepository,
    )
    mro = SqliteCompressionQueueRepository.__mro__
    assert _SqliteRepository in mro
    assert CompressionQueueRepository in mro


def test_repository_is_concrete() -> None:
    assert inspect.isabstract(SqliteCompressionQueueRepository) is False
    assert SqliteCompressionQueueRepository.__abstractmethods__ == frozenset()


def test_construction_is_argument_free_and_inert() -> None:
    repo = SqliteCompressionQueueRepository()

    assert type(repo) is SqliteCompressionQueueRepository
    assert vars(repo) == {}


def test_documented_methods_are_defined_concrete_and_async() -> None:
    for name in METHODS:
        assert name in SqliteCompressionQueueRepository.__dict__, name
        method = SqliteCompressionQueueRepository.__dict__[name]
        assert inspect.iscoroutinefunction(method), name
        assert getattr(method, "__isabstractmethod__", False) is False, name


def test_parameter_contracts_match_the_base_declaration() -> None:
    for name in METHODS:
        assert _params(SqliteCompressionQueueRepository.__dict__[name]) == _params(
            getattr(CompressionQueueRepository, name)
        ), name


def test_dialect_guard_rejects_a_foreign_transaction() -> None:
    with pytest.raises(TypeError, match="SqliteTransaction"):
        SqliteCompressionQueueRepository._conn(object())


def test_no_attribute_fallback_is_defined() -> None:
    assert "__getattr__" not in SqliteCompressionQueueRepository.__dict__
    repo = SqliteCompressionQueueRepository()
    with pytest.raises(AttributeError):
        repo.clear_queue


@pytest.mark.asyncio
async def test_sqlite_schema_accepts_the_documented_queue_table(backend) -> None:
    async with backend.transactional() as tx:
        tables = await _fetch_all(
            tx.conn,
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_compression_queue'",
        )
        columns = await _fetch_all(tx.conn, "PRAGMA table_info(memory_compression_queue)")
        foreign_keys = await _fetch_all(tx.conn, "PRAGMA foreign_key_list(memory_compression_queue)")

    assert tables == [{"name": "memory_compression_queue"}]
    names = {column["name"] for column in columns}
    assert DOCUMENTED_COLUMNS <= names
    primary_key = {column["name"]: column["pk"] for column in columns if column["pk"]}
    assert primary_key == {"id": 1}
    assert any(fk["from"] == "memory_id" and fk["table"] == "memories" for fk in foreign_keys)


@pytest.mark.asyncio
async def test_enqueue_then_dequeue_returns_the_item_once(backend) -> None:
    await _add_memory(backend, "mem-q1")
    repo = SqliteCompressionQueueRepository()

    async with backend.transactional() as tx:
        assert await _enqueue(repo, tx, ["mem-q1"]) == ["mem-q1"]

    async with backend.transactional() as tx:
        claimed = await repo.dequeue_compression(tx, limit=5)
        assert [row["memory_id"] for row in claimed] == ["mem-q1"]
        assert claimed[0]["attempts"] == 1  # bumped at claim time
        queue_id = claimed[0]["id"]

    async with backend.transactional() as tx:
        assert await repo.dequeue_compression(tx, limit=5) == []

    async with backend.transactional() as tx:
        assert queue_id
        assert isinstance(tx, SqliteTransaction)


@pytest.mark.asyncio
async def test_higher_priority_is_claimed_first(backend) -> None:
    await _add_memory(backend, "mem-low")
    await _add_memory(backend, "mem-high")
    repo = SqliteCompressionQueueRepository()

    async with backend.transactional() as tx:
        await _enqueue(repo, tx, ["mem-low"], priority=0)
        await _enqueue(repo, tx, ["mem-high"], priority=5)

    async with backend.transactional() as tx:
        claimed = await repo.dequeue_compression(tx, limit=2)

    assert [row["memory_id"] for row in claimed] == ["mem-high", "mem-low"]


@pytest.mark.asyncio
async def test_reenqueueing_a_pending_memory_enqueues_nothing(backend) -> None:
    await _add_memory(backend, "mem-dup")
    repo = SqliteCompressionQueueRepository()

    async with backend.transactional() as tx:
        assert await _enqueue(repo, tx, ["mem-dup"]) == ["mem-dup"]
        assert await _enqueue(repo, tx, ["mem-dup"]) == []
        rows = await _fetch_all(tx.conn, "SELECT COUNT(*) AS n FROM memory_compression_queue")

    assert rows == [{"n": 1}]


@pytest.mark.asyncio
async def test_mark_done_removes_the_item_from_pending(backend) -> None:
    await _add_memory(backend, "mem-done")
    repo = SqliteCompressionQueueRepository()

    async with backend.transactional() as tx:
        await _enqueue(repo, tx, ["mem-done"])
        claimed = await repo.dequeue_compression(tx, limit=1)
        await repo.mark_compression_done(tx, queue_id=claimed[0]["id"])
        status = await _fetch_all(
            tx.conn,
            "SELECT status FROM memory_compression_queue WHERE id = ?",
            (claimed[0]["id"],),
        )
        assert await repo.dequeue_compression(tx, limit=5) == []

    assert status == [{"status": "done"}]


@pytest.mark.asyncio
async def test_queue_stats_are_integer_counts(backend) -> None:
    await _add_memory(backend, "mem-stats")
    repo = SqliteCompressionQueueRepository()

    async with backend.transactional() as tx:
        await _enqueue(repo, tx, ["mem-stats"])
        stats = await repo.get_queue_stats(tx)

    assert isinstance(stats, dict)
    assert stats
    assert all(isinstance(value, int) for value in stats.values())
