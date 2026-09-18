"""CHARON round-trip against a REAL SQLite backend, in-process.

Why this file exists: until the backend-neutral rewiring, MNEMOS's primary
import/export path went through ``mnemos.db.portability_repo`` -- raw asyncpg,
Postgres only. The HTTP routes hid that behind a
``require_postgres_pool_or_503`` gate, and the one branch that *looked* like a
neutral escape hatch handed a driver connection to code that immediately
called asyncpg-only methods on it, so lifting the gate would have produced an
``AttributeError``, not a working SQLite export.

These tests run the real route functions against a real ``SqliteBackend`` with
no Postgres anywhere, so "it works on another backend" is demonstrated rather
than asserted. They need no container and run everywhere the unit suite does.

The snapshot test is the important one: it is the SQLite counterpart of
``test_streaming_export.py``'s live-Postgres concurrent-modification test,
which is the primary regression class for this code path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

pytest.importorskip("aiosqlite")

OWNER = "alice"
NS = "alice-ns"
T0 = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)


def _root():
    from mnemos.api.dependencies import UserContext

    return UserContext(
        user_id="root_admin",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=3))


async def _seed(backend, ids, *, content="Paris is the capital of France."):
    async with backend.transactional() as tx:
        for idx, mid in enumerate(ids):
            await backend.memories.insert_memory(
                tx,
                memory_id=mid,
                content=f"{content} ({idx})",
                category="facts",
                subcategory="geography",
                metadata_json=json.dumps({"src": "charon-sqlite-test"}),
                quality_rating=85,
                owner_id=OWNER,
                namespace=NS,
                permission_mode=600,
                source_model="claude-opus-4-7",
                source_provider="anthropic",
                source_session="session_charon",
                source_agent="tester",
                verbatim_content=f"{content} ({idx})",
                created=T0 + timedelta(minutes=idx),
                updated=T0 + timedelta(minutes=idx),
            )


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    """A real SqliteBackend installed as the lifecycle persistence backend."""
    import mnemos.core.lifecycle as lc
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "charon-roundtrip.db", _settings())
    await backend.open()
    monkeypatch.setattr(lc, "_persistence_backend", backend)
    monkeypatch.setattr(lc, "_pool", None)
    monkeypatch.setattr(lc, "_rls_enabled", False)
    monkeypatch.setattr(lc, "_cache", None)
    try:
        yield backend
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_export_round_trips_through_import(sqlite_backend, tmp_path):
    """export -> wipe -> import reproduces the records on SQLite.

    This is the whole point of the rewiring: before it, this call raised
    rather than exporting.
    """
    from mnemos.api.routes import portability

    ids = ["mem_sqlite_1", "mem_sqlite_2"]
    await _seed(sqlite_backend, ids)

    envelope = await portability.export_memories(
        category=None,
        limit=1000,
        offset=0,
        owner_id=OWNER,
        namespace=NS,
        include_sidecars=False,
        user=_root(),
    )
    assert envelope.record_count == 2
    exported = {r.id for r in envelope.records}
    assert exported == set(ids)
    # Ordering is the export's contract, not an accident.
    assert [r.id for r in envelope.records] == ids

    # Restore into a SECOND, EMPTY database -- the actual backup/restore
    # scenario, and stronger than wiping the source in place.
    import mnemos.core.lifecycle as lc
    from mnemos.persistence.sqlite import SqliteBackend

    target = SqliteBackend(tmp_path / "charon-restore.db", _settings())
    await target.open()
    prior = lc._persistence_backend
    lc._persistence_backend = target
    try:
        stats = await portability.import_memories(envelope=envelope, preserve_owner=True, user=_root())
        assert stats.failed == 0, stats.errors
        assert stats.imported == 2, stats

        async with target.transactional(isolation="repeatable_read", readonly=True) as tx:
            restored = await target.memories.fetch_memory_export(
                tx,
                effective_owner=OWNER,
                effective_ns=NS,
                category=None,
                limit=100,
                offset=0,
            )
        assert [r["id"] for r in restored] == ids
        by_id = {r["id"]: r for r in restored}
        for record in envelope.records:
            assert by_id[record.id]["content"] == record.payload["content"]
            assert by_id[record.id]["owner_id"] == OWNER
            assert by_id[record.id]["namespace"] == NS
    finally:
        lc._persistence_backend = prior
        await target.close()


@pytest.mark.asyncio
async def test_sqlite_re_import_is_idempotent(sqlite_backend):
    """A re-import of an envelope this server just produced is a no-op.

    This is the case that regressed on Postgres (a NULL verbatim_content
    round-tripped as `content` and the importer then rejected its own
    envelope). It also covers the SQLite-specific conflict signal: SQLite
    RAISES DuplicateMemoryError where every other backend returns
    "INSERT 0 0", so an importer that only checks the return string counts
    every skip as a failure here.
    """
    from mnemos.api.routes import portability

    ids = ["mem_sqlite_idem"]
    await _seed(sqlite_backend, ids)

    envelope = await portability.export_memories(
        category=None,
        limit=1000,
        offset=0,
        owner_id=OWNER,
        namespace=NS,
        include_sidecars=False,
        user=_root(),
    )

    first = await portability.import_memories(envelope=envelope, preserve_owner=True, user=_root())
    assert first.failed == 0, first.errors
    assert first.imported == 0, "rows already exist; nothing should be inserted"
    assert first.skipped == 1, first

    second = await portability.import_memories(envelope=envelope, preserve_owner=True, user=_root())
    assert second.failed == 0, second.errors
    assert second.skipped == first.skipped


@pytest.mark.asyncio
async def test_sqlite_export_is_scoped_and_redacts_by_default(sqlite_backend):
    """Tenant scope and the vault gate hold on SQLite too.

    These are SQL predicates in the backend implementation, so they are
    exactly the kind of thing that can be right on one dialect and wrong on
    another.
    """
    from mnemos.core.secret_detection import VAULT_NAMESPACE

    from mnemos.api.routes import portability

    await _seed(sqlite_backend, ["mem_scope_mine"])
    async with sqlite_backend.transactional() as tx:
        await sqlite_backend.memories.insert_memory(
            tx,
            memory_id="mem_scope_vault",
            content="hunter2",
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=50,
            owner_id=OWNER,
            namespace=VAULT_NAMESPACE,
            permission_mode=600,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content="hunter2",
            created=T0,
            updated=T0,
        )
        await sqlite_backend.memories.insert_memory(
            tx,
            memory_id="mem_scope_other",
            content="someone else's",
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=50,
            owner_id="bob",
            namespace="bob-ns",
            permission_mode=600,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content="someone else's",
            created=T0,
            updated=T0,
        )

    envelope = await portability.export_memories(
        category=None,
        limit=1000,
        offset=0,
        owner_id=OWNER,
        namespace=NS,
        include_sidecars=False,
        user=_root(),
    )
    ids = {r.id for r in envelope.records}
    assert "mem_scope_mine" in ids
    assert "mem_scope_vault" not in ids, "vault row must not export by default"
    assert "mem_scope_other" not in ids, "cross-tenant row must not export"


@pytest.mark.asyncio
async def test_sqlite_stream_holds_one_snapshot_under_concurrent_writes(tmp_path, monkeypatch):
    """The SQLite export must see a consistent snapshot, like the other five.

    SQLite reaches that guarantee differently -- there is no REPEATABLE READ
    level, only a WAL read snapshot pinned at transaction entry -- so this is
    the test that the substitute mechanism actually delivers the property.
    It mirrors test_streaming_export.py's live-Postgres concurrent
    DELETE/UPDATE/INSERT test.

    The writer is a SECOND backend over the same database file, because a
    SqliteBackend serializes everything through one connection behind a lock:
    writing through the same instance would prove nothing about concurrency.
    """
    import mnemos.core.lifecycle as lc
    from mnemos.persistence.sqlite import SqliteBackend

    from mnemos.domain.portability.stream import stream_export

    db_path = tmp_path / "charon-snapshot.db"
    reader = SqliteBackend(db_path, _settings())
    await reader.open()
    writer = SqliteBackend(db_path, _settings())
    await writer.open()
    monkeypatch.setattr(lc, "_persistence_backend", reader)
    monkeypatch.setattr(lc, "_pool", None)

    try:
        ids = [f"mem_snap_{i}" for i in range(4)]
        await _seed(writer, ids)

        stream = stream_export(
            reader,
            user=_root(),
            category=None,
            limit=2,
            offset=0,
            owner_id=OWNER,
            namespace=NS,
            include_sidecars=False,
        )

        # First page opens (and pins) the snapshot.
        first = json.loads(await anext(stream))
        assert [r["id"] for r in first["records"]] == ids[:2]

        # Now write concurrently, from a different connection.
        async with writer.transactional() as wtx:
            await writer.memories.insert_memory(
                wtx,
                memory_id="mem_snap_new",
                content="committed mid-export",
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
                verbatim_content="committed mid-export",
                created=T0 + timedelta(hours=1),
                updated=T0 + timedelta(hours=1),
            )

        rest = [json.loads(line) async for line in stream]
        streamed = [r["id"] for r in first["records"]] + [r["id"] for page in rest for r in page.get("records", [])]

        assert "mem_snap_new" not in streamed, (
            f"a row committed AFTER the export began leaked into its snapshot; streamed={streamed}"
        )
        assert streamed == ids, f"snapshot must be the pre-write state; got {streamed}"

        completion = rest[-1]
        assert completion.get("export_complete") is True
        assert completion["record_count"] == len(ids)
    finally:
        await reader.close()
        await writer.close()


@pytest.mark.asyncio
async def test_sqlite_export_rejects_write_isolation_request(sqlite_backend):
    """SQLite refuses a repeatable-read WRITE transaction instead of faking it.

    Documented behaviour, asserted so it cannot quietly become a silent
    downgrade later: a DEFERRED transaction that writes escalates its lock
    mid-flight and can fail after partial work.
    """
    with pytest.raises(ValueError, match="read-only"):
        async with sqlite_backend.transactional(isolation="repeatable_read"):
            pass
