"""Snapshot orchestration and bounded, fail-closed disk export regressions."""

import io
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar

import pytest

from mnemos.tools.export_stream import DiskMemories, iter_pages, write_export


def record(number):
    return {
        "id": f"mem_{number}",
        "kind": "memory",
        "payload": {
            "created": "2026-01-01T00:00:00",
            "content": "x" * 100,
        },
    }


def pages(count, size=2):
    for start in range(0, count, size):
        yield {
            "mpf_version": "0.2",
            "records": [record(i) for i in range(start, min(count, start + size))],
            "kg_triples": [{"id": "kg1", "subject_literal": "shared"}],
        }
    yield {"export_complete": True, "record_count": count}


@pytest.mark.parametrize("jsonl", [False, True])
def test_spool_preserves_all_records_dedupes_sidecars_and_publishes(tmp_path, jsonl):
    out = tmp_path / "export"
    assert write_export(pages(5), out, jsonl=jsonl) == 5
    if jsonl:
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert [r["id"] for r in rows[:-1]] == [f"mem_{i}" for i in range(5)]
        assert rows[-1]["kg_triples"] == [{"id": "kg1", "subject_literal": "shared"}]
    else:
        value = json.loads(out.read_text())
        assert value["record_count"] == 5
        assert len(value["kg_triples"]) == 1


@pytest.mark.parametrize("failure", ["truncated", "divergent", "count", "repeated"])
def test_failed_stream_preserves_existing_output_and_cleans_spool(tmp_path, failure):
    out = tmp_path / "export"
    out.write_text("previous complete file")
    data = list(pages(4))
    if failure == "truncated":
        data.pop()
    elif failure == "divergent":
        data[1]["kg_triples"][0]["subject_literal"] = "changed"
    elif failure == "count":
        data[-1]["record_count"] = 99
    else:
        data[1]["records"] = data[0]["records"]
    with pytest.raises(ValueError):
        write_export(iter(data), out)
    assert out.read_text() == "previous complete file"
    assert list(tmp_path.iterdir()) == [out]


def test_disk_flat_records_are_reiterable_and_cleanup():
    memories = DiskMemories(pages(5))
    assert all("mpf_sidecars" not in line for line in memories.path.read_text().splitlines())
    path = memories.path
    try:
        assert len(memories) == 5
        assert [m["id"] for m in memories] == [m["id"] for m in memories]
    finally:
        memories.close()
    assert not path.exists()


def test_ndjson_negotiation_and_truncation(monkeypatch):
    requests = []

    class Response(io.BytesIO):
        headers: ClassVar[dict] = {"Content-Type": "application/x-ndjson"}

    def open_(request, timeout):
        requests.append(request)
        return Response(b'{"records":[]}\n')

    monkeypatch.setattr("urllib.request.urlopen", open_)
    with pytest.raises(ValueError, match="completion"):
        list(iter_pages("http://localhost", "secret", {"limit": 2}))
    assert "stream=true" in requests[0].full_url
    assert requests[0].get_header("Authorization") == "Bearer secret"


def _pg_backend(pool):
    """Real PostgresBackend over a throwaway-schema pool.

    The streaming entry points take a persistence backend now. Using the
    genuine PostgresBackend (not a double) is deliberate: these are the tests
    that prove the snapshot guarantee holds against real MVCC, so the
    transaction handling under test must be the real implementation.
    """
    from types import SimpleNamespace as _NS

    from mnemos.persistence.postgres import PostgresBackend

    return PostgresBackend(pool, _NS(database=_NS(embedding_dim=3)))


@pytest.mark.asyncio
async def test_stream_holds_one_snapshot_uses_keyset_and_drains_deletion_cursor(
    monkeypatch,
):
    from mnemos.domain.portability import stream

    active = []
    calls = []

    sentinel_tx = SimpleNamespace(name="tx")

    class Backend:
        @asynccontextmanager
        async def transactional(self, **kwargs):
            # The whole export must run at repeatable_read + readonly, and in
            # exactly ONE such scope -- that is the snapshot guarantee.
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            active.append("transaction")
            try:
                yield sentinel_tx
            finally:
                active.pop()

    backend = Backend()

    async def export(be, tx, **kwargs):
        assert be is backend
        assert tx is sentinel_tx
        assert active == ["transaction"]
        calls.append(kwargs.copy())
        if kwargs.get("deletion_log_cursor"):
            page = {"records": [record(999)], "deletion_log": [{"id": "d2"}]}
        elif kwargs.get("record_cursor"):
            page = {"records": [record(2)], "deletion_log": [{"id": "d1"}]}
        else:
            page = {
                "records": [record(0), record(1)],
                "deletion_log": [{"id": "d1"}],
                "deletion_log_next_cursor": "next",
            }
        return SimpleNamespace(model_dump=lambda **_: page)

    monkeypatch.setattr(stream, "export_memories", export)
    result = [json.loads(line) async for line in stream.stream_export(backend, limit=2, offset=0)]
    assert not active
    assert calls[1]["record_cursor"] == (
        datetime(2026, 1, 1, tzinfo=UTC),
        "mem_1",
    )
    assert calls[1]["offset"] == 0
    assert calls[2]["record_cursor"] is None
    assert [r["id"] for page in result[:-1] for r in page["records"]] == [
        "mem_0",
        "mem_1",
        "mem_2",
    ]
    assert [r["id"] for page in result[:-1] for r in page.get("deletion_log", [])] == [
        "d1",
        "d2",
    ]
    assert result[-1] == {"export_complete": True, "record_count": 3}


@pytest.mark.asyncio
async def test_stream_close_releases_transaction_and_connection(monkeypatch):
    from mnemos.domain.portability import stream

    released = []

    @asynccontextmanager
    async def resource(name):
        try:
            yield SimpleNamespace(name=name)
        finally:
            released.append(name)

    # One scope now instead of connection+transaction: the backend owns
    # checkout, so closing the generator releases exactly the transaction.
    backend = SimpleNamespace(transactional=lambda **_: resource("transaction"))

    async def export(*_, **kwargs):
        return SimpleNamespace(model_dump=lambda **_: {"records": [record(0)]})

    monkeypatch.setattr(stream, "export_memories", export)
    generator = stream.stream_export(backend, limit=1, offset=0)
    await anext(generator)
    await generator.aclose()
    assert released == ["transaction"]


@pytest.mark.asyncio
async def test_repository_keyset_parameters_retain_tenant_scope():
    from mnemos.db.portability_repo import fetch_memory_export

    captured = []

    async def fetch(sql, *args):
        captured.append((sql, args))
        return []

    await fetch_memory_export(
        SimpleNamespace(fetch=fetch),
        effective_owner="alice",
        effective_ns="private",
        category=None,
        limit=2,
        offset=0,
        record_cursor=(datetime(2026, 1, 1, tzinfo=UTC), "mem_1"),
    )
    sql, args = captured[0]
    assert "owner_id = $2" in sql and "namespace = $3" in sql
    assert "(created, id) > ($4, $5)" in sql
    assert "ORDER BY created ASC, id ASC" in sql
    assert args[-4:] == (datetime(2026, 1, 1, tzinfo=UTC), "mem_1", 2, 0)


@pytest.mark.asyncio
async def test_live_postgres_snapshot_survives_concurrent_delete_insert_update():
    """Optional real MVCC proof using an isolated throwaway schema only."""
    import os
    import uuid

    asyncpg = pytest.importorskip("asyncpg")
    dsn = os.getenv("MNEMOS_TEST_DB")
    if not dsn:
        pytest.skip("MNEMOS_TEST_DB required for live PostgreSQL snapshot test")
    from mnemos.domain.portability.stream import stream_export

    schema = "charon_stream_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"""CREATE TABLE {schema}.memories (
            id TEXT PRIMARY KEY, content TEXT, category TEXT, subcategory TEXT,
            created TIMESTAMPTZ NOT NULL, updated TIMESTAMPTZ, deleted_at TIMESTAMPTZ,
            owner_id TEXT, namespace TEXT, permission_mode TEXT, quality_rating INTEGER,
            source_model TEXT, source_provider TEXT, source_session TEXT, source_agent TEXT,
            metadata JSONB, provenance TEXT, morpheus_run_id UUID, source_memories TEXT[],
            federation_source TEXT, verbatim_content TEXT,
            -- The backend-neutral MemoryRepository.fetch_memory_export
            -- projection is wider than the old Postgres-only repo query was:
            -- it also returns group_id, archived_at, consolidated_into and
            -- embedding. A throwaway schema missing them fails with
            -- UndefinedColumnError rather than silently omitting them.
            group_id TEXT, archived_at TIMESTAMPTZ, consolidated_into TEXT,
            embedding TEXT)""")
        await admin.execute(
            f"INSERT INTO {schema}.memories(id,content,category,created) SELECT 'mem_'||n, 'original', 'test', '2026-01-01Z'::timestamptz FROM generate_series(0,3) n"
        )
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, server_settings={"search_path": schema})
        stream = stream_export(
            _pg_backend(pool),
            user=SimpleNamespace(role="root", user_id="review", namespace="default"),
            category=None,
            limit=2,
            offset=0,
            owner_id=None,
            namespace=None,
            include_sidecars=False,
        )
        first = json.loads(await anext(stream))
        await admin.execute(f"DELETE FROM {schema}.memories WHERE id='mem_2'")
        await admin.execute(f"UPDATE {schema}.memories SET content='changed' WHERE id='mem_3'")
        await admin.execute(
            f"INSERT INTO {schema}.memories(id,content,category,created) VALUES('mem_4','new','test','2026-01-01Z')"
        )
        rest = [json.loads(line) async for line in stream]
        records = first["records"] + [record for page in rest[:-1] for record in page["records"]]
        assert [r["id"] for r in records] == ["mem_0", "mem_1", "mem_2", "mem_3"]
        assert {r["payload"]["content"] for r in records} == {"original"}
        assert rest[-1] == {"export_complete": True, "record_count": 4}
        # Cross-page provenance is preserved only for the authenticated scope.
        await admin.execute(f"""INSERT INTO {schema}.memories
            (id,content,category,created,owner_id,namespace,provenance,source_memories)
            VALUES ('summary','summary','test','2026-01-01Z','alice','private','morpheus_local',
                    ARRAY['zsource','foreign']),
                   ('zsource','source','test','2026-01-01Z','alice','private',NULL,NULL),
                   ('foreign','private','test','2026-01-01Z','bob','private',NULL,NULL)""")
        scoped = [
            json.loads(line)
            async for line in stream_export(
                _pg_backend(pool),
                user=SimpleNamespace(role="user", user_id="alice", namespace="private"),
                category=None,
                limit=1,
                offset=0,
                owner_id=None,
                namespace=None,
                include_sidecars=False,
                mpf_version="0.2",
            )
        ]
        assert scoped[0]["records"][0]["provenance"]["wasInfluencedBy"] == [{"type": "memory", "id": "zsource"}]
        assert "foreign" not in json.dumps(scoped)
        async with pool.acquire() as conn:
            assert not conn.is_in_transaction()
    finally:
        if pool:
            await pool.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


_MEMORIES_DDL = """CREATE TABLE {schema}.memories (
    id TEXT PRIMARY KEY, content TEXT, category TEXT, subcategory TEXT,
    created TIMESTAMPTZ NOT NULL, updated TIMESTAMPTZ, deleted_at TIMESTAMPTZ,
    owner_id TEXT, namespace TEXT, permission_mode TEXT, quality_rating INTEGER,
    source_model TEXT, source_provider TEXT, source_session TEXT, source_agent TEXT,
    metadata JSONB, provenance TEXT, morpheus_run_id UUID, source_memories TEXT[],
    federation_source TEXT, verbatim_content TEXT,
    -- See the sibling DDL above: the backend-neutral export projection also
    -- selects these four, so a throwaway schema without them fails with
    -- UndefinedColumnError.
    group_id TEXT, archived_at TIMESTAMPTZ, consolidated_into TEXT,
    embedding TEXT)"""


def _flat_records(lines):
    """Flatten every page/record line into one ordered list of records."""
    return [record for line in lines for record in (line.get("records") or [])]


@pytest.mark.asyncio
async def test_live_postgres_per_record_stream_snapshot_and_buffered_equivalence(monkeypatch, tmp_path):
    """Per-record framing must change WHEN bytes ship, never WHICH bytes.

    This test owns its own schema and inserts its own fixture. The reverted
    Feature 4/5 attempt appended its per-record assertions to the END of the
    legacy test's body -- after that body had already deleted mem_2, inserted
    mem_4 and inserted three more tenant-scoped rows -- and then asserted the
    freshly started export still equalled the ORIGINAL four-row fixture. The
    export was started after those writes, so all seven live rows were
    legitimately inside its snapshot; the assertion was unsatisfiable by any
    implementation and the (correct) result was misread as a broken snapshot.
    A snapshot test must therefore start the export BEFORE the concurrent
    writes, which is what the first half of this test does.
    """
    import os
    import uuid

    asyncpg = pytest.importorskip("asyncpg")
    dsn = os.getenv("MNEMOS_TEST_DB")
    if not dsn:
        pytest.skip("MNEMOS_TEST_DB required for live PostgreSQL snapshot test")
    from mnemos.domain.portability.stream import stream_export, stream_export_records

    schema = "charon_perrec_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(_MEMORIES_DDL.format(schema=schema))
        await admin.execute(
            f"INSERT INTO {schema}.memories(id,content,category,created) "
            f"SELECT 'mem_'||n, 'original', 'test', '2026-01-01Z'::timestamptz "
            f"FROM generate_series(0,3) n"
        )
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, server_settings={"search_path": schema})
        options = {
            "user": SimpleNamespace(role="root", user_id="review", namespace="default"),
            "category": None,
            "limit": 2,
            "offset": 0,
            "owner_id": None,
            "namespace": None,
            "include_sidecars": False,
        }

        # ── 1. Snapshot: concurrent writes land AFTER the first record ──
        # Stride 1 forces a fresh keyset sub-batch per record, so every
        # record after the first is fetched strictly after the concurrent
        # DELETE/UPDATE/INSERT commit. Any of them leaking in would mean the
        # sub-batches are not sharing the outer repeatable-read snapshot.
        monkeypatch.setenv("MNEMOS_EXPORT_RECORD_BATCH", "1")
        backend = _pg_backend(pool)
        stream = stream_export_records(backend, **options)
        lines = [json.loads(await anext(stream))]  # header line
        lines.append(json.loads(await anext(stream)))  # first record -> snapshot open
        await admin.execute(f"DELETE FROM {schema}.memories WHERE id='mem_2'")
        await admin.execute(f"UPDATE {schema}.memories SET content='changed' WHERE id='mem_3'")
        await admin.execute(
            f"INSERT INTO {schema}.memories(id,content,category,created) VALUES('mem_4','new','test','2026-01-01Z')"
        )
        lines += [json.loads(line) async for line in stream]

        records = _flat_records(lines)
        assert [r["id"] for r in records] == ["mem_0", "mem_1", "mem_2", "mem_3"]
        assert {r["payload"]["content"] for r in records} == {"original"}
        assert lines[-1] == {"export_complete": True, "record_count": 4}
        # The header must carry envelope metadata: mnemos.tools.export_stream
        # takes it from the FIRST page, and mpf_validate.py requires it.
        assert lines[0]["records"] == [] and lines[0]["mpf_version"]
        assert lines[0]["source_system"] and lines[0]["source_version"]
        # Intra-page streaming actually happened: one line per record, never
        # a buffered page of two. Without this the test would still pass on
        # the legacy page-at-a-time implementation.
        record_lines = [line for line in lines if line.get("records")]
        assert len(record_lines) == 4
        assert all(len(line["records"]) == 1 for line in record_lines)
        async with pool.acquire() as conn:
            assert not conn.is_in_transaction()

        # ── 2. Equivalence with the buffered path, at every stride ──
        # Same data, same options: the per-record path must reproduce the
        # legacy path's records exactly -- same dicts, same order -- whatever
        # sub-batch stride it fetches with.
        monkeypatch.delenv("MNEMOS_EXPORT_RECORD_BATCH", raising=False)
        legacy = [json.loads(line) async for line in stream_export(backend, **options)]
        expected = _flat_records(legacy)
        assert [r["id"] for r in expected] == ["mem_0", "mem_1", "mem_3", "mem_4"]
        for stride in ("1", "2", "3", "100"):
            monkeypatch.setenv("MNEMOS_EXPORT_RECORD_BATCH", stride)
            streamed = [json.loads(line) async for line in stream_export_records(backend, **options)]
            assert _flat_records(streamed) == expected, f"stride={stride} diverged"
            assert streamed[-1] == {
                "export_complete": True,
                "record_count": len(expected),
            }

        # ── 3. The existing client consumes the new framing unchanged ──
        # write_export takes envelope metadata from the first page, tolerates
        # record-less pages and checks the completion count against the summed
        # record lists -- so per-record framing needs no coalescing shim on the
        # reader side. Proven here against real server output, not a mock.
        monkeypatch.delenv("MNEMOS_EXPORT_RECORD_BATCH", raising=False)
        collected = [json.loads(line) async for line in stream_export_records(backend, **options)]
        published_path = tmp_path / "per-record-export.json"
        assert write_export(iter(collected), published_path) == len(expected)
        published = json.loads(published_path.read_text())
        assert published["mpf_version"]
        assert [r["id"] for r in published["records"]] == [r["id"] for r in expected]

        # ── 4. v0.2 provenance scoping survives sub-batching ──
        # Page-level scoping seeds in_scope_ids from the whole page; sub-batch
        # scoping seeds it from one batch and leans on
        # fetch_visible_export_memory_ids, whose predicate is a strict
        # superset. Both must therefore keep 'zsource' (same tenant) and drop
        # 'foreign' (other tenant) identically.
        await admin.execute(f"""INSERT INTO {schema}.memories
            (id,content,category,created,owner_id,namespace,provenance,source_memories)
            VALUES ('summary','summary','test','2026-01-01Z','alice','private','morpheus_local',
                    ARRAY['zsource','foreign']),
                   ('zsource','source','test','2026-01-01Z','alice','private',NULL,NULL),
                   ('foreign','private','test','2026-01-01Z','bob','private',NULL,NULL)""")
        scoped_options = {
            **options,
            "user": SimpleNamespace(role="user", user_id="alice", namespace="private"),
            "limit": 3,
            "mpf_version": "0.2",
        }
        monkeypatch.delenv("MNEMOS_EXPORT_RECORD_BATCH", raising=False)
        legacy_scoped = _flat_records([json.loads(line) async for line in stream_export(backend, **scoped_options)])
        assert [r["id"] for r in legacy_scoped] == ["summary", "zsource"]
        for stride in ("1", "2"):
            monkeypatch.setenv("MNEMOS_EXPORT_RECORD_BATCH", stride)
            streamed_scoped = [json.loads(line) async for line in stream_export_records(backend, **scoped_options)]
            assert _flat_records(streamed_scoped) == legacy_scoped, (
                f"v0.2 provenance scoping diverged at stride={stride}"
            )
            assert "foreign" not in json.dumps(streamed_scoped)

        # Closing after a real row and cancelling an active SQL fetch must
        # release the snapshot, not only closing immediately after the header.
        partial = stream_export_records(backend, **options)
        await anext(partial)
        await anext(partial)
        await partial.aclose()
        assert pool.get_idle_size() == pool.get_size()

        import asyncio

        from mnemos.persistence.postgres import _postgres_tx

        # Records are fetched through the backend's MemoryRepository now, not
        # through a module-level repo function, so the delay is injected there.
        memories_repo = backend.memories
        original_fetch = memories_repo.fetch_memory_export
        fetching = asyncio.Event()
        calls = 0

        async def delayed_fetch(tx, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                fetching.set()
                # Block INSIDE the export's own transaction, so cancelling the
                # consumer has to unwind a genuinely in-flight query.
                await _postgres_tx(tx).conn.execute("SELECT pg_sleep(30)")
            return await original_fetch(tx, **kwargs)

        monkeypatch.setenv("MNEMOS_EXPORT_RECORD_BATCH", "1")
        monkeypatch.setattr(memories_repo, "fetch_memory_export", delayed_fetch)

        async def consume():
            async for _ in stream_export_records(backend, **options):
                pass

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(fetching.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert pool.get_idle_size() == pool.get_size()
        async with pool.acquire() as conn:
            assert not conn.is_in_transaction()
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        if pool:
            await pool.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


@pytest.mark.asyncio
async def test_per_record_stream_holds_one_snapshot_and_releases_on_close():
    """One connection, one repeatable-read transaction, released on cancel."""
    from mnemos.domain.portability import stream

    released = []

    class Backend:
        @asynccontextmanager
        async def transactional(self, **kwargs):
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            try:
                yield SimpleNamespace(name="tx")
            finally:
                released.append("transaction")

    async def never_called(*args, **kwargs):
        raise AssertionError("record fetch must not run before the first pull")
        yield  # pragma: no cover

    monkeypatch_target = stream._iter_page_records
    stream._iter_page_records = never_called
    try:
        generator = stream.stream_export_records(
            Backend(),
            user=SimpleNamespace(role="root", user_id="u", namespace="n"),
            category=None,
            limit=2,
            offset=0,
            owner_id=None,
            namespace=None,
            include_sidecars=False,
        )
        # First pull is the header line; it must not have touched the DB yet.
        header = json.loads(await anext(generator))
        assert header["records"] == [] and header["mpf_version"]
        await generator.aclose()
    finally:
        stream._iter_page_records = monkeypatch_target
    assert released == ["transaction"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"include_secrets": True}, "include_secrets requires root"),
        ({"owner_id": "bob"}, "cross-owner export requires root"),
        ({"namespace": "other"}, "cross-namespace export requires root"),
    ],
)
async def test_per_record_stream_enforces_export_authorization(overrides, detail):
    """Authorization is shared with export_memories, never re-derived.

    The reverted Feature 4/5 attempt resolved the tenant scope inline in its
    streaming function and, doing so, dropped these three gates entirely -- a
    non-root caller asking for include_secrets would have received unredacted
    vault-namespace rows instead of a 403. Both paths now go through
    _resolve_export_scope, so the gates cannot diverge again.

    The refusal must also happen BEFORE a pool connection is taken: opening a
    snapshot only to abandon it would hold a connection for nothing.
    """
    from fastapi import HTTPException

    from mnemos.domain.portability.stream import stream_export_records

    class Backend:
        def transactional(self, **_):
            raise AssertionError("authorization must reject before opening a transaction")

    options = {
        "user": SimpleNamespace(role="user", user_id="alice", namespace="private"),
        "category": None,
        "limit": 2,
        "offset": 0,
        "owner_id": None,
        "namespace": None,
        "include_sidecars": False,
    }
    options.update(overrides)
    generator = stream_export_records(Backend(), **options)
    with pytest.raises(HTTPException) as raised:
        await anext(generator)
    assert raised.value.status_code == 403
    assert detail in raised.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("per_record", [False, True])
async def test_stream_route_rejects_cursor_before_sending_headers(monkeypatch, per_record):
    from fastapi import HTTPException

    from mnemos.api.routes import portability

    # The route no longer has a Postgres-pool gate to stub out; it resolves a
    # persistence backend instead. Give it one so the cursor rejection (which
    # must happen BEFORE any streaming begins) is what the test observes.
    monkeypatch.setattr(portability, "backend_or_503", lambda: SimpleNamespace())
    with pytest.raises(HTTPException) as caught:
        await portability.export_memories(
            user=SimpleNamespace(role="user", user_id="alice", namespace="private"),
            category=None,
            limit=2,
            offset=0,
            owner_id=None,
            namespace=None,
            include_sidecars=True,
            include_unattached_kg=False,
            include_secrets=False,
            mpf_version="0.2",
            deletion_log_from=None,
            deletion_log_to=None,
            deletion_log_cursor="not-a-valid-cursor",
            stream=True,
            stream_records=per_record,
        )
    assert caught.value.status_code == 400


def test_per_record_spool_batches_disk_commits(monkeypatch, tmp_path):
    import sqlite3

    real_connect = sqlite3.connect
    commits = []

    class Connection(sqlite3.Connection):
        def commit(self):
            commits.append(True)
            return super().commit()

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kw: real_connect(*args, factory=Connection, **kw),
    )
    data = ({"records": [record(i)]} for i in range(2500))
    from itertools import chain

    stream = chain(data, [{"export_complete": True, "record_count": 2500}])
    assert write_export(stream, tmp_path / "export.json") == 2500
    assert len(commits) == 3
