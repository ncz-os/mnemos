"""Cross-backend parity for the MPF export primitives added for CHARON.

These are the primitives CHARON's backend-neutral export/import path depends
on. Before they existed, that path reached into ``mnemos.db.portability_repo``
-- a raw-asyncpg, Postgres-only module -- so MNEMOS's primary import/export
surface silently worked on exactly one of six backends.

What is exercised here:

* ``fetch_visible_export_memory_ids`` and ``fetch_deletion_log_for_export``
  (new ABC methods, no prior counterpart anywhere).
* the ``include_secrets`` vault gate and the ``record_cursor`` keyset on
  ``fetch_memory_export``.
* ``fetch_server_now`` returning a timezone-AWARE timestamp.
* ``transactional(isolation=..., readonly=...)``.
* ``Transaction.savepoint()`` nesting.

SQLite runs everywhere (in-process, no container). Postgres runs only when
``MNEMOS_TEST_DB`` is set -- the same env var the rest of this suite and the
CI integration jobs already use.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.persistence.sqlite import SqliteBackend

PG_URL = os.getenv("MNEMOS_TEST_DB") or os.getenv("MNEMOS_TEST_PG_URL")

# Ids and owner are unique per process run. Cleanup is then a convenience
# rather than a correctness requirement: rows left behind by an aborted run
# can never collide with a later one. This matters because memory_versions
# and memory_branches are written by the mnemos_version_snapshot TRIGGER, not
# by the test, so "delete what I inserted" does not describe the real
# footprint and deleting the branch rows breaks the trigger's own invariant.
_RUN = uuid.uuid4().hex[:12]
OWNER = f"parity_owner_{_RUN}"
NS = "parity_ns"
MEM_IDS = [f"pm_{_RUN}_{i}" for i in range(3)]
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=3))


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    backend = SqliteBackend(tmp_path / "export-primitives.db", _settings())
    await backend.open()
    try:
        yield backend
    finally:
        await backend.close()


async def _seed_memories(backend) -> None:
    """Three live rows (two visible, one vaulted) plus one soft-deleted."""
    async with backend.transactional() as tx:
        for idx, namespace in ((0, NS), (1, NS), (2, VAULT_NAMESPACE)):
            await backend.memories.insert_memory(
                tx,
                memory_id=MEM_IDS[idx],
                content=f"content {idx}",
                category="facts",
                subcategory=None,
                metadata_json="{}",
                quality_rating=50,
                owner_id=OWNER,
                namespace=namespace,
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content=f"verbatim {idx}",
                created=T0 + timedelta(minutes=idx),
                updated=T0 + timedelta(minutes=idx),
            )


@pytest.mark.asyncio
async def test_sqlite_export_primitives(sqlite_backend):
    await _check_export_primitives(sqlite_backend)


@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="set MNEMOS_TEST_DB to run the Postgres half")
async def test_postgres_export_primitives():
    import asyncpg

    from mnemos.persistence.postgres import PostgresBackend

    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2)
    backend = PostgresBackend(pool, _settings())
    try:
        # Clean before AND after: a prior aborted run can leave rows behind,
        # and memory_versions does not cascade from memories, so deleting the
        # parent alone leaves the version rows the mnemos_version_snapshot
        # trigger created -- which then collide on (memory_id, version_num).
        # Every statement is scoped to this test's synthetic owner / id prefix
        # so it can never touch real data.
        await _pg_cleanup(pool)
        await _check_export_primitives(backend)
    finally:
        await _pg_cleanup(pool)
        await pool.close()


async def _pg_cleanup(pool) -> None:
    """Best-effort teardown for this run's synthetic rows.

    Order matters, and NOT in the direction FK dependencies suggest. Deleting
    a memories row fires the v3_5 tombstone trigger, which requires that
    memory's 'main' branch row to still exist and raises MN001
    ("branch main for memory X is missing") if it does not. So memories MUST
    be deleted FIRST, while its branch is intact; the version/branch rows the
    trigger touches are swept afterwards.

    Everything is scoped to this run's unique owner/ids so it can never reach
    real data, and because the ids are run-unique a failed cleanup cannot
    break a later run either.
    """
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE owner_id = $1", OWNER)
        await conn.execute("DELETE FROM memory_versions WHERE owner_id = $1", OWNER)
        await conn.execute(
            "DELETE FROM memory_branches WHERE memory_id = ANY($1::text[])", MEM_IDS
        )


async def _check_export_primitives(backend) -> None:
    await _seed_memories(backend)

    async with backend.transactional(isolation="repeatable_read", readonly=True) as tx:
        # --- fetch_server_now must be timezone-aware ------------------------
        now = await backend.fetch_server_now(tx)
        assert isinstance(now, datetime)
        assert now.tzinfo is not None, (
            "fetch_server_now returned a naive datetime; comparing it against a "
            "timestamptz column silently shifts by the server's UTC offset"
        )

        # --- include_secrets gates the vault namespace ----------------------
        redacted = await backend.memories.fetch_memory_export(
            tx,
            effective_owner=OWNER,
            effective_ns=None,
            category=None,
            limit=50,
            offset=0,
            include_secrets=False,
        )
        ids = [r["id"] for r in redacted]
        assert MEM_IDS[2] not in ids, "vault row leaked with include_secrets=False"
        assert {MEM_IDS[0], MEM_IDS[1]} <= set(ids)

        unredacted = await backend.memories.fetch_memory_export(
            tx,
            effective_owner=OWNER,
            effective_ns=None,
            category=None,
            limit=50,
            offset=0,
            include_secrets=True,
        )
        assert MEM_IDS[2] in [r["id"] for r in unredacted], (
            "include_secrets=True must include vault rows -- it is the root-only "
            "backup escape hatch"
        )

        # The projection must carry verbatim_content, or the importer cannot
        # compare a stored row against an inbound payload without a second,
        # driver-specific query.
        assert "verbatim_content" in dict(redacted[0])

        # --- record_cursor resumes strictly after the keyset ----------------
        first = await backend.memories.fetch_memory_export(
            tx,
            effective_owner=OWNER,
            effective_ns=None,
            category=None,
            limit=1,
            offset=0,
            include_secrets=False,
        )
        assert len(first) == 1
        cursor = (first[0]["created"], first[0]["id"])
        after = await backend.memories.fetch_memory_export(
            tx,
            effective_owner=OWNER,
            effective_ns=None,
            category=None,
            limit=50,
            offset=0,
            include_secrets=False,
            record_cursor=cursor,
        )
        after_ids = [r["id"] for r in after]
        assert first[0]["id"] not in after_ids, "keyset re-emitted its own anchor row"
        assert after_ids == sorted(after_ids), "export must stay in (created, id) order"

        # --- fetch_visible_export_memory_ids --------------------------------
        visible = await backend.memories.fetch_visible_export_memory_ids(
            tx,
            memory_ids=[*MEM_IDS, f"pm_{_RUN}_absent"],
            effective_owner=OWNER,
            effective_ns=None,
            include_secrets=False,
        )
        assert visible == {MEM_IDS[0], MEM_IDS[1]}, (
            "visibility must drop the vault row and the nonexistent id, and keep "
            f"the two live ones; got {visible!r}"
        )
        assert (
            await backend.memories.fetch_visible_export_memory_ids(
                tx,
                memory_ids=[],
                effective_owner=None,
                effective_ns=None,
            )
            == set()
        )
        # Cross-tenant: another owner's scope must see nothing of ours.
        assert (
            await backend.memories.fetch_visible_export_memory_ids(
                tx,
                memory_ids=[MEM_IDS[0], MEM_IDS[1]],
                effective_owner="somebody_else",
                effective_ns=None,
            )
            == set()
        )

        # --- fetch_deletion_log_for_export ----------------------------------
        # Shape and filter coverage: an empty log is still a valid answer, and
        # every optional predicate must be accepted by every dialect. This is
        # the query that had no ABC counterpart at all before.
        rows = await backend.memories.fetch_deletion_log_for_export(
            tx,
            effective_owner=OWNER,
            effective_ns=NS,
            hard_limit=10,
            from_executed_at=T0,
            to_executed_at=now,
            cursor_executed_at=T0,
            cursor_id=str(uuid.uuid4()),
            export_as_of=now,
            include_secrets=False,
        )
        assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_sqlite_savepoint_isolates_failure(sqlite_backend):
    """A failed nested scope must not poison the enclosing transaction.

    This is the property the MPF importer relies on for per-entry failure
    isolation. On Postgres it came free (asyncpg turns a nested
    ``conn.transaction()`` into a SAVEPOINT); every other backend needed it
    built.
    """
    backend = sqlite_backend
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=f"sp_keep_{_RUN}",
            content="kept",
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=50,
            owner_id=OWNER,
            namespace=NS,
            permission_mode=600,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content="kept",
            created=T0,
            updated=T0,
        )

        with pytest.raises(RuntimeError):
            async with tx.savepoint():
                await backend.memories.insert_memory(
                    tx,
                    memory_id=f"sp_rollback_{_RUN}",
                    content="discarded",
                    category="facts",
                    subcategory=None,
                    metadata_json="{}",
                    quality_rating=50,
                    owner_id=OWNER,
                    namespace=NS,
                    permission_mode=600,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content="discarded",
                    created=T0,
                    updated=T0,
                )
                raise RuntimeError("force savepoint rollback")

        # The outer transaction survived and can still be used.
        assert await backend.memories.fetch_memory_by_id(tx, f"sp_keep_{_RUN}") is not None
        assert await backend.memories.fetch_memory_by_id(tx, f"sp_rollback_{_RUN}") is None


@pytest.mark.asyncio
async def test_sqlite_rejects_repeatable_read_for_writes(sqlite_backend):
    """SQLite refuses rather than silently downgrading.

    A DEFERRED transaction that later writes escalates its lock mid-flight and
    can fail with SQLITE_BUSY after doing partial work. Telling the caller up
    front is strictly better than pretending the level was honored.
    """
    with pytest.raises(ValueError, match="read-only"):
        async with sqlite_backend.transactional(isolation="repeatable_read"):
            pass

    with pytest.raises(ValueError, match="unsupported isolation level"):
        async with sqlite_backend.transactional(isolation="read_uncommitted"):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_sqlite_snapshot_is_pinned_at_transaction_entry(tmp_path):
    """The WAL read snapshot must be taken at BEGIN, not at first real query.

    ``BEGIN DEFERRED`` acquires nothing until a statement touches the
    database. Without the probe read in ``transactional()``, a row committed
    between BEGIN and the export's first query would be visible -- a weaker
    guarantee than the other five backends, silently.

    Two connections to the same file are used so the writer is genuinely
    concurrent rather than the reader's own connection.
    """
    db_path = tmp_path / "snapshot.db"
    reader = SqliteBackend(db_path, _settings())
    await reader.open()
    writer = SqliteBackend(db_path, _settings())
    await writer.open()
    try:
        async with writer.transactional() as tx:
            await writer.memories.insert_memory(
                tx,
                memory_id="snap_before",
                content="before",
                category="facts",
                subcategory=None,
                metadata_json="{}",
                quality_rating=50,
                owner_id=OWNER,
                namespace=NS,
                permission_mode=600,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content="before",
                created=T0,
                updated=T0,
            )

        async with reader.transactional(
            isolation="repeatable_read", readonly=True
        ) as rtx:
            # Snapshot is pinned here, before any export query runs.
            async with writer.transactional() as wtx:
                await writer.memories.insert_memory(
                    wtx,
                    memory_id="snap_during",
                    content="during",
                    category="facts",
                    subcategory=None,
                    metadata_json="{}",
                    quality_rating=50,
                    owner_id=OWNER,
                    namespace=NS,
                    permission_mode=600,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content="during",
                    created=T0 + timedelta(minutes=5),
                    updated=T0 + timedelta(minutes=5),
                )

            rows = await reader.memories.fetch_memory_export(
                rtx,
                effective_owner=OWNER,
                effective_ns=NS,
                category=None,
                limit=50,
                offset=0,
            )
            ids = {r["id"] for r in rows}
            assert "snap_before" in ids
            assert "snap_during" not in ids, (
                "the export snapshot saw a row committed after it began -- the "
                "BEGIN DEFERRED probe read did not pin the WAL snapshot"
            )
    finally:
        await reader.close()
        await writer.close()
