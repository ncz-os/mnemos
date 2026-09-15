"""Regression test for the round-97 P1 concurrent migration race.

``mnemos serve --workers N`` boots N processes against a shared
Postgres database. On a genuinely fresh database every worker process
independently decides "the schema isn't established yet, let me
establish it" and races the others into the migration runner. DDL like
``CREATE OR REPLACE VIEW`` / ``CREATE OR REPLACE FUNCTION`` is safe to
run repeatedly IN SEQUENCE but not concurrently -- two sessions racing
to create the same catalog entry can both attempt the underlying
``pg_type`` insert and one loses to ``duplicate key value violates
unique constraint "pg_type_typname_nsp_index"``.

The fix: ``ensure_postgres_schema`` and ``ensure_postgres_oauth_schema``
in ``mnemos/persistence/schema.py`` wrap the migration run in a
session-scoped Postgres advisory lock acquired via a non-blocking
polling variant. Only one worker actually applies migrations at a
time; the others poll for the lock and run their no-op replay when
they finally acquire it. The polling variant is required because the
synchronous ``pg_advisory_lock`` leaves the waiter with an open
virtual transaction id, which ``CREATE INDEX CONCURRENTLY`` then waits
for, producing a second-order deadlock between the lock-holder's
CONCURRENTLY and the lock-waiters' still-open vxids.

These tests require a real PostgreSQL instance reachable via
``MNEMOS_TEST_DB`` (matching the convention used by
``tests/test_postgres_webhook_repository.py`` and
``tests/test_nats_dispatch_log_repository.py``). When ``MNEMOS_TEST_DB``
is unset the tests are skipped.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from mnemos.persistence.schema import (
    _POSTGRES_SCHEMA_LOCK_KEY,
    _pg_advisory_session_lock_polling,
    ensure_postgres_oauth_schema,
    ensure_postgres_schema,
)


PG_URL = os.environ.get("MNEMOS_TEST_DB")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="set MNEMOS_TEST_DB=postgres://... to run concurrent migration race tests",
)


# ────────────────────────────────────────────────────────────────────────────
# Lock-key + smoke regression: the lock key is the same fixed well-known
# hash for every caller, so concurrent processes serialize.
# ────────────────────────────────────────────────────────────────────────────


def test_postgres_schema_lock_key_is_stable():
    """The lock key is a hash of the fixed string ``"mnemos_schema_migration"``
    -- NOT per-principal like the GRAEAE quota case. Every worker process
    must race on the SAME key so only one actually applies migrations.
    """
    import hashlib

    expected = (
        int.from_bytes(
            hashlib.sha256(b"mnemos_schema_migration").digest()[:8],
            "big",
            signed=False,
        )
        & 0x7FFFFFFFFFFFFFFF
    )
    assert _POSTGRES_SCHEMA_LOCK_KEY == expected
    # And it's a signed-int64 (positive after the &-mask; never negative).
    assert 0 < _POSTGRES_SCHEMA_LOCK_KEY < 2**63


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────


async def _drop_db(admin_dsn: str, dbname: str) -> None:
    """Drop the named test database against ``admin_dsn`` (which must NOT be ``dbname``)."""
    conn = await asyncpg.connect(admin_dsn)
    try:
        # Postgres refuses ``DROP DATABASE`` against an open connection;
        # force-disconnect any lingering sessions first.
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            dbname,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    finally:
        await conn.close()


async def _create_db(admin_dsn: str, dbname: str) -> None:
    """Create the named test database against ``admin_dsn``."""
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()


def _admin_dsn(test_db_dsn: str) -> str:
    """Build an admin DSN pointing at the ``postgres`` database so we can
    CREATE / DROP a fresh test database. Swaps the path component of
    the input URL.
    """
    parts = urlsplit(test_db_dsn)
    return urlunsplit(parts._replace(path="/postgres"))


@pytest_asyncio.fixture
async def fresh_pg_pool() -> AsyncIterator[tuple[asyncpg.Pool, str]]:
    """Yield (pool, dbname) against a TRULY fresh Postgres database.

    Creates a dedicated test database (``mnemos_migration_race_<pid>``),
    opens a small connection pool against it, yields for the test, then
    drops the database in teardown so the next test starts clean.

    This is the pattern required to actually reproduce the round-97 P1
    race: a fresh DB means the migration runner hits real CREATE OR
    REPLACE DDL (not no-op replays), which is where the
    ``pg_type_typname_nsp_index`` unique-key collision fires.

    The fixture also pre-creates the ``mnemos`` NOLOGIN role and the
    ``mnemos_user`` login role that several migrations GRANT to
    (migrations_model_registry.sql:73-76, migrations_v1_multiuser.sql,
    etc.). The docker-compose staging setup creates ``mnemos_user``
    via POSTGRES_USER and ``mnemos`` via the initdb mount (see
    ``mnemos/db_migrations/0_init_roles_staging.sql``); the installer
    creates both via its own role-creation path. In an isolated test
    DB we have to do it ourselves. Postgres roles are cluster-wide
    (not per-DB), so guard each with a try/except for re-runs.
    """
    test_db = f"mnemos_migration_race_{os.getpid()}"
    admin = _admin_dsn(PG_URL)
    await _drop_db(admin, test_db)
    await _create_db(admin, test_db)

    parts = urlsplit(PG_URL)
    test_dsn = urlunsplit(parts._replace(path=f"/{test_db}"))

    pool = await asyncpg.create_pool(test_dsn, min_size=2, max_size=4)
    try:
        async with pool.acquire() as conn:
            for ddl in (
                "CREATE ROLE mnemos NOLOGIN",
                "CREATE ROLE mnemos_user LOGIN",
                "GRANT mnemos TO mnemos_user",
                "GRANT mnemos TO CURRENT_USER",
            ):
                try:
                    await conn.execute(ddl)
                except asyncpg.DuplicateObjectError:
                    pass  # cluster-wide; another test already created it
        yield pool, test_db
    finally:
        await pool.close()
        await _drop_db(admin, test_db)


# ────────────────────────────────────────────────────────────────────────────
# The actual race regression: N concurrent ensure_postgres_schema() calls
# against a fresh DB must ALL complete without a pg_type_typname_nsp_index
# unique-key collision or a vxid-based CONCURRENTLY deadlock.
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_ensure_postgres_schema_succeeds_against_fresh_db(
    fresh_pg_pool: tuple[asyncpg.Pool, str],
) -> None:
    """Round-97 P1: ``mnemos serve --workers N`` cold-start race.

    N>=2 concurrent calls to ``ensure_postgres_schema`` against a
    TRULY fresh database must ALL complete successfully. Without the
    session-scoped advisory lock (and without the polling variant that
    keeps the waiter's vxid closed between tries), workers racing the
    schema migration fail with::

        RuntimeError: Postgres schema migration ... failed at
        `CREATE OR REPLACE VIEW ... AS`: duplicate key value violates
        unique constraint "pg_type_typname_nsp_index"

    AND, even with a blocking advisory lock, the waiters' open vxids
    deadlock the lock-holder's ``CREATE INDEX CONCURRENTLY`` against
    the waiters' ``pg_advisory_lock`` calls (verified on a real
    cluster with N=2 against a fresh DB).

    Both workers see fresh DB → both run the real migrations → with
    the fix they serialize on the polling schema lock → both return
    clean.

    Real concurrency: ``asyncio.gather`` of N coroutines that each
    acquire their own connection from the same pool and call
    ``ensure_postgres_schema`` for real. No mocks, no threading
    tricks — this is the same shape the production
    ``uvicorn --workers 2`` boot uses, just collapsed into one
    process.
    """
    pool, dbname = fresh_pg_pool

    settings = SimpleNamespace()
    # N>=2 reproduces the production `--workers 2` shape; we use 4 to
    # also catch any N>2-specific failure (e.g. lock-acquisition
    # ordering issues under higher contention).
    n_workers = 4
    results = await asyncio.gather(
        *(ensure_postgres_schema(pool, settings) for _ in range(n_workers)),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, (
        f"{len(failures)}/{n_workers} concurrent ensure_postgres_schema "
        f"calls against fresh DB raised: "
        f"{[type(f).__name__ + ': ' + str(f) for f in failures]}"
    )

    # And the resulting schema is actually correct — not partially
    # applied. Probe a representative sample of tables and the
    # ``memories_import_chunk_key_uniq`` CONCURRENTLY-built unique
    # index whose existence proves the lock didn't deadlock against
    # the waiter's vxid.
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        table_names = {row["tablename"] for row in rows}

        concurrently_idx = await conn.fetchval(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'memories_import_chunk_key_uniq'"
        )

    expected_tables = {
        "memories",
        "memory_versions",
        "memory_branches",
        "compression_quality_log",
        "graeae_consultations",
        "state",
        "journal",
        "entities",
        "kg_triples",
        "sessions",
    }
    missing = expected_tables - table_names
    assert not missing, (
        f"concurrent migration left the schema incomplete: "
        f"missing {sorted(missing)} from public tables {sorted(table_names)}"
    )
    assert concurrently_idx == "memories_import_chunk_key_uniq", (
        "CREATE INDEX CONCURRENTLY on memories.import_chunk_key did "
        "not complete — the lock waited on a vxid held by another "
        "worker and deadlocked (round-97 P1 second-order effect)."
    )


@pytest.mark.asyncio
async def test_concurrent_ensure_postgres_schema_replays_idempotently(
    fresh_pg_pool: tuple[asyncpg.Pool, str],
) -> None:
    """Second call after a successful concurrent first pair must be a clean
    no-op (idempotent replay), not raise.

    The lock alone doesn't make the migrations idempotent — the
    migration runner already does (IF NOT EXISTS + the benign-replay
    guard). This test confirms that after the first concurrent pair
    succeeds, a third sequential call sees the target state and
    completes silently.
    """
    pool, dbname = fresh_pg_pool
    settings = SimpleNamespace()

    await asyncio.gather(
        ensure_postgres_schema(pool, settings),
        ensure_postgres_schema(pool, settings),
    )
    # Third call: everything is at the target, replay must be clean.
    await ensure_postgres_schema(pool, settings)


@pytest.mark.asyncio
async def test_concurrent_ensure_postgres_oauth_schema_succeeds_against_fresh_db(
    fresh_pg_pool: tuple[asyncpg.Pool, str],
) -> None:
    """Round-97 P1 (oauth path): same race exposure for the dedicated
    OAuth-database bootstrap helper. N concurrent calls against a fresh
    database must ALL succeed.
    """
    pool, dbname = fresh_pg_pool

    n_workers = 4
    results = await asyncio.gather(
        *(ensure_postgres_oauth_schema(pool) for _ in range(n_workers)),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, (
        f"{len(failures)}/{n_workers} concurrent ensure_postgres_oauth_schema "
        f"calls against fresh DB raised: "
        f"{[type(f).__name__ + ': ' + str(f) for f in failures]}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Lock-acquisition observability: ensure_postgres_schema must take the
# session-scoped advisory lock on the well-known key for the duration
# of the migration run. This is a behavior-level pin — if someone
# refactors the lock away (e.g. swaps to a transaction-scoped lock
# that would break CREATE INDEX CONCURRENTLY, or drops the lock
# entirely) the test fails.
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_postgres_schema_holds_advisory_session_lock_during_run(
    fresh_pg_pool: tuple[asyncpg.Pool, str], monkeypatch
) -> None:
    """Behavioral pin: while ``_apply_postgres_migrations`` is running
    on connection A, a second connection B must NOT be able to acquire
    the schema lock (the lock is held). After the migration returns,
    a subsequent caller can re-acquire it (the lock was released).

    This proves both halves of the contract:

    * the lock is acquired at the start of the critical section, AND
    * the lock is released at the end (the finally-clause unlock), so
      a failed migration attempt doesn't leave the lock permanently
      held and deadlock every subsequent worker startup forever.
    """
    pool, dbname = fresh_pg_pool

    observed: dict[str, bool] = {"lock_held_during_run": False}

    # Capture the real _apply_postgres_migrations BEFORE we
    # monkey-patch; our slow version must call it directly (not via
    # the now-overwritten module attribute) or it recurses into
    # itself forever.
    from mnemos.persistence import schema as schema_mod

    real_apply = schema_mod._apply_postgres_migrations

    async def _slow_apply(conn, paths, embedding_dim):
        # Probe: try to acquire the same session lock from a SECOND
        # connection in the same pool. With the fix in place this
        # MUST block — we use ``pg_try_advisory_lock`` (non-blocking)
        # so the probe doesn't itself sit blocked at the lock and
        # contaminate the test. With the fix the probe returns
        # ``False`` (lock held by conn); without the fix it would
        # return ``True``.
        async with pool.acquire() as probe_conn:
            row = await probe_conn.fetchrow(
                "SELECT pg_try_advisory_lock($1) AS got",
                _POSTGRES_SCHEMA_LOCK_KEY,
            )
            observed["lock_held_during_run"] = not bool(row["got"])

        # Now do the REAL migration apply so the test exercises the
        # same code path as ``ensure_postgres_schema`` (and the
        # follow-up ``_ensure_postgres_embedding_shape`` call in the
        # caller finds the ``memories`` table it expects).
        await real_apply(conn, paths, embedding_dim)

    monkeypatch.setattr("mnemos.persistence.schema._apply_postgres_migrations", _slow_apply)

    await ensure_postgres_schema(pool, SimpleNamespace())

    assert observed.get("lock_held_during_run") is True, (
        "expected another connection to be BLOCKED from acquiring "
        "the schema-migration advisory lock while "
        "_apply_postgres_migrations is running — the lock is not "
        "being held for the duration of the critical section."
    )

    # And the lock was released: a fresh attempt after the call
    # returned must succeed immediately.
    async with pool.acquire() as probe_conn:
        row = await probe_conn.fetchrow(
            "SELECT pg_try_advisory_lock($1) AS got",
            _POSTGRES_SCHEMA_LOCK_KEY,
        )
        assert bool(row["got"]) is True, (
            "schema-migration advisory lock was not released after "
            "ensure_postgres_schema returned — a failed migration "
            "would deadlock every subsequent worker startup forever."
        )
        await probe_conn.execute("SELECT pg_advisory_unlock($1)", _POSTGRES_SCHEMA_LOCK_KEY)


@pytest.mark.asyncio
async def test_pg_advisory_session_lock_polling_eventually_acquires(
    fresh_pg_pool: tuple[asyncpg.Pool, str],
) -> None:
    """The polling variant must acquire the lock once the holder releases,
    even after many failed ``pg_try_advisory_lock`` attempts. Pins the
    polling contract that the production code relies on.
    """
    pool, _dbname = fresh_pg_pool

    async with pool.acquire() as holder_conn:
        await holder_conn.execute("SELECT pg_advisory_lock($1)", _POSTGRES_SCHEMA_LOCK_KEY)

        async with pool.acquire() as waiter_conn:
            # Hold the lock for 1 second from the holder side, then
            # release. The waiter polls at 100ms intervals (we pass
            # the parameter for test speed).
            async def release_after():
                await asyncio.sleep(1.0)
                await holder_conn.execute("SELECT pg_advisory_unlock($1)", _POSTGRES_SCHEMA_LOCK_KEY)

            release_task = asyncio.create_task(release_after())
            await _pg_advisory_session_lock_polling(waiter_conn, _POSTGRES_SCHEMA_LOCK_KEY, poll_interval=0.1)
            await release_task

            try:
                # Confirm we now hold it (a second waiter would block).
                async with pool.acquire() as third_conn:
                    row = await third_conn.fetchrow(
                        "SELECT pg_try_advisory_lock($1) AS got",
                        _POSTGRES_SCHEMA_LOCK_KEY,
                    )
                    assert bool(row["got"]) is False, (
                        "expected the schema lock to still be held after the "
                        "polling acquisition returned; the polling helper "
                        "appears to have released it instead of leaving it "
                        "for the caller to release."
                    )
            finally:
                # Release the lock before waiter_conn returns to the pool,
                # otherwise the next acquirer of that pooled connection
                # would inherit the schema lock.
                await waiter_conn.execute("SELECT pg_advisory_unlock($1)", _POSTGRES_SCHEMA_LOCK_KEY)

        # Waiter connection's exit releases the conn (lock-free now).
