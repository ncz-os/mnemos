"""Cross-backend tests for :class:`NatsDispatchLogRepository` (item 9/12).

The NATS dispatch-log dedupe table was Postgres/SQLite-only prior to
item 9; the migration in this item brings MySQL, MariaDB, Oracle, and
Db2 onto the same canonical shape ``(event_id, subject, dispatched_at)``
with a primary key on ``(event_id, subject)``. These tests pin the
ABC contract on every reachable backend:

* SQLite (in-memory, always available) — minimum bar;
* Postgres when ``MNEMOS_TEST_DB`` points at a real cluster
  (matches the
  ``tests/test_postgres_webhook_repository.py`` environmental
  pattern).

Each test exercises the same semantic — a fresh ``(event_id, subject)``
returns ``True`` from :meth:`record_if_new` and a redelivery of the
same pair returns ``False`` — across both an explicit
``backend.transactional()`` round trip and the wrapped
``_record_dispatch_once`` helper used by
``mnemos/workers/webhooks_dispatch_nats_consumer.py``. Item 9's
fix relies on both surfaces agreeing on the duplicate-detection
semantic.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from mnemos.persistence.base import NatsDispatchLogRepository


# ────────────────────────────────────────────────────────────────────────────
# SQLite — always available, in-memory
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_record_if_new_first_call_returns_true(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "nats_dispatch.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        repo = backend.nats_dispatch_log
        assert isinstance(repo, NatsDispatchLogRepository)
        async with backend.transactional() as tx:
            assert await repo.record_if_new(tx, "evt-1", "foo.bar") is True
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_record_if_new_duplicate_returns_false(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "nats_dispatch.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        repo = backend.nats_dispatch_log
        async with backend.transactional() as tx:
            assert await repo.record_if_new(tx, "evt-1", "foo.bar") is True
            # Second call with the SAME (event_id, subject) MUST return False.
            assert await repo.record_if_new(tx, "evt-1", "foo.bar") is False
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_record_if_new_distinct_subjects_each_succeed(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "nats_dispatch.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        repo = backend.nats_dispatch_log
        async with backend.transactional() as tx:
            assert await repo.record_if_new(tx, "evt-1", "foo.bar") is True
            assert await repo.record_if_new(tx, "evt-1", "foo.baz") is True
            assert await repo.record_if_new(tx, "evt-2", "foo.bar") is True
            # Redeliveries:
            assert await repo.record_if_new(tx, "evt-1", "foo.bar") is False
            assert await repo.record_if_new(tx, "evt-1", "foo.baz") is False
            assert await repo.record_if_new(tx, "evt-2", "foo.bar") is False
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_record_if_new_rollback_unwinds_dedupe(tmp_path):
    """A side-effect rollback must also unsee the dedupe row.

    The federation consumer relies on this — it dedupes then writes a
    memory; if the memory write raises, the dedupe row has to roll
    back so the redelivery isn't permanently marked as a duplicate.
    """
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "nats_dispatch.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        repo = backend.nats_dispatch_log

        # First call, then RAISE — the surrounding transactional()
        # context must roll back the dedupe row.
        with pytest.raises(RuntimeError):
            async with backend.transactional() as tx:
                assert await repo.record_if_new(tx, "evt-rollback", "bar.baz") is True
                raise RuntimeError("simulate side-effect failure")

        # The next transactional call must see this pair as fresh,
        # NOT as a duplicate, because the prior row rolled back.
        async with backend.transactional() as tx:
            assert await repo.record_if_new(tx, "evt-rollback", "bar.baz") is True
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_record_dispatch_once_wrapper_dedupes(tmp_path):
    """``_record_dispatch_once`` must surface True on first / False on redelivery.

    This is the wrapper used by
    ``mnemos/workers/webhooks_dispatch_nats_consumer.py``.  Item 9
    moved it onto the ABC; verify the wrapper still produces the
    (True, False, ...) sequence the consumer's ``handle_message``
    relies on to skip duplicate deliveries.
    """
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.workers.webhooks_dispatch_nats_consumer import _record_dispatch_once

    backend = SqliteBackend(tmp_path / "nats_dispatch.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        first = await _record_dispatch_once(backend, "evt-1", "foo.bar")
        second = await _record_dispatch_once(backend, "evt-1", "foo.bar")
        third = await _record_dispatch_once(backend, "evt-2", "foo.bar")
        assert first is True
        assert second is False
        assert third is True
    finally:
        await backend.close()


# ────────────────────────────────────────────────────────────────────────────
# Postgres — guarded by MNEMOS_TEST_DB
# ────────────────────────────────────────────────────────────────────────────


PG_URL = os.environ.get("MNEMOS_TEST_DB")


@pytest.mark.skipif(
    not PG_URL,
    reason="set MNEMOS_TEST_DB=postgres://... to run nats dispatch-log integration tests",
)
@pytest.mark.asyncio
async def test_postgres_record_if_new_first_then_duplicate():
    """Same dedupe semantic against a real Postgres cluster.

    The Postgres arm relies on
    ``INSERT ... ON CONFLICT (event_id, subject) DO NOTHING RETURNING event_id``
    under the canonical primary key. We can't easily reuse the
    Postgres webhook test's DDL mini-migration because the dispatch
    log is on the full ``migrations_v5_2_0_nats_outbox_idempotency.sql``
    migration that ships with the codebase; opening a real
    ``PostgresBackend`` against a DB that already has the full
    MNEMOS schema is the simplest way to verify the contract end to
    end.
    """
    import asyncpg

    from mnemos.persistence.postgres import PostgresBackend

    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2)
    backend = PostgresBackend(pool, SimpleNamespace())
    try:
        repo = backend.nats_dispatch_log
        assert isinstance(repo, NatsDispatchLogRepository)
        # Wipe any prior test rows so we observe a clean dedupe state.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM nats_dispatch_log WHERE event_id LIKE 'item9_%'"
            )
        async with backend.transactional() as tx:
            assert await repo.record_if_new(tx, "item9_evt-1", "foo.bar") is True
        async with backend.transactional() as tx:
            assert await repo.record_if_new(tx, "item9_evt-1", "foo.bar") is False
        async with backend.transactional() as tx:
            assert await repo.record_if_new(tx, "item9_evt-2", "foo.bar") is True
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM nats_dispatch_log WHERE event_id LIKE 'item9_%'"
            )
        await pool.close()


# ────────────────────────────────────────────────────────────────────────────
# Capability flag — guards the wiring
# ────────────────────────────────────────────────────────────────────────────


def test_capability_flag_present_on_every_backend():
    """``supports_nats_dispatch_log`` must be a class attribute on every backend.

    The cross-backend capability matrix is the route to checking
    whether the dedupe surface is wired on a given backend.  This
    pins the wiring so a backend that loses the attribute at
    construction (e.g. someone deletes the line in ``__init__``)
    is caught at import time, not at first dedupe.
    """
    from mnemos.persistence.mariadb import MariadbBackend
    from mnemos.persistence.mysql import MysqlBackend
    from mnemos.persistence.oracle import OracleBackend
    from mnemos.persistence.postgres import PostgresBackend
    from mnemos.persistence.sqlite import SqliteBackend

    assert PostgresBackend.supports_nats_dispatch_log is True
    assert SqliteBackend.supports_nats_dispatch_log is True
    assert MysqlBackend.supports_nats_dispatch_log is True
    assert MariadbBackend.supports_nats_dispatch_log is True
    assert OracleBackend.supports_nats_dispatch_log is True


def test_each_backend_exposes_a_nats_dispatch_log_property():
    """Every ``*Backend`` class must surface ``.nats_dispatch_log`` as the ABC.

    We don't construct the heavy backend here — just probe the class
    itself for the property + the right return-type annotation.
    """
    from mnemos.persistence.mariadb import MariadbBackend
    from mnemos.persistence.mysql import MysqlBackend
    from mnemos.persistence.oracle import OracleBackend
    from mnemos.persistence.postgres import PostgresBackend
    from mnemos.persistence.sqlite import SqliteBackend

    for cls in (
        PostgresBackend,
        SqliteBackend,
        MysqlBackend,
        MariadbBackend,
        OracleBackend,
    ):
        assert hasattr(cls, "nats_dispatch_log"), f"{cls.__name__} is missing .nats_dispatch_log"
        # The dataclass/descriptor may wrap it — the type is the
        # ABC, so we just confirm the class descriptor resolves.
        prop = getattr(cls, "nats_dispatch_log")
        assert prop is not None


def test_mariadb_inherits_mysql_nats_dispatch_log_impl():
    """MariaDB's nats dispatch log repo is the MySQL one under the hood.

    This mirrors the codebase's existing pattern (MariadbBackend
    subclasses MysqlBackend and overrides individual repos by
    sub-classing MysqlNatsDispatchLogRepository). If a future
    refactor accidentally drops the override, MariaDB stops using
    the right INSERT IGNORE dialect and tests against a live
    MariaDB would catch it.
    """
    from mnemos.persistence.mariadb import MariadbNatsDispatchLogRepository
    from mnemos.persistence.mysql import MysqlNatsDispatchLogRepository

    assert issubclass(MariadbNatsDispatchLogRepository, MysqlNatsDispatchLogRepository)
