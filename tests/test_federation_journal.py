"""Real SQLite/PostgreSQL mutation, cursor and replay regressions."""

from datetime import datetime, timezone
import os
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
import uuid

import pytest
import pytest_asyncio

from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect
from mnemos.domain.federation import _store_memories, _apply_withdrawal
from mnemos.api.routes.federation import _feed_item_from_row


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def backend(request, tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    if request.param == "sqlite":
        from mnemos.persistence.sqlite import SqliteBackend

        instance = SqliteBackend(tmp_path / "journal.db", settings)
        await instance.open()
        try:
            yield instance
        finally:
            await instance.close()
    else:
        dsn = os.getenv("MNEMOS_TEST_DB")
        if not dsn:
            pytest.skip("set MNEMOS_TEST_DB for real PostgreSQL journal tests")
        import asyncpg
        from mnemos.persistence.postgres import PostgresBackend

        name = "mnemos_journal_" + uuid.uuid4().hex[:16]
        admin = await asyncpg.connect(dsn)
        pool = None
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
            target = urlunsplit(urlsplit(dsn)._replace(path="/" + name))
            pool = await asyncpg.create_pool(target, min_size=1, max_size=4)
            instance = PostgresBackend(pool, settings)
            await instance.open()
            yield instance
        finally:
            if pool:
                await pool.close()
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.close()


async def insert(backend, mid="journal_one", namespace="A", mode=644):
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO memories(id,content,category,owner_id,namespace,permission_mode,created,updated) "
            "VALUES (?,?,?,?,?,?,?,?)",
            mid,
            "ordinary facts",
            "facts",
            "alice",
            namespace,
            mode,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2020, 1, 1, tzinfo=timezone.utc),
        )


async def mutate(backend, sql, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).execute(sql, *params)


async def feed(backend, cursor=None, namespaces=None, limit=100):
    async with backend.transactional() as tx:
        return await backend.federation.feed_query(
            tx,
            since_updated=cursor["cursor_updated"] if cursor else None,
            since_id=cursor["cursor_id"] if cursor else None,
            namespaces=namespaces or [],
            categories=[],
            limit=limit,
            prefer_compressed=False,
        )


@pytest.mark.asyncio
async def test_incremental_soft_then_hard_delete_retains_revocation(backend):
    await insert(backend)
    first = (await feed(backend))[0]
    # Actual worker state transition: deleted_at changes without updated.
    await mutate(backend, "UPDATE memories SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", "journal_one")
    soft = await feed(backend, first)
    assert len(soft) == 1 and soft[0]["type"] == "withdrawal"
    assert soft[0]["federation_sequence"] > first["federation_sequence"]
    await mutate(backend, "DELETE FROM memories WHERE id = ?", "journal_one")
    late = await feed(backend, first)
    assert late and all(row["type"] == "withdrawal" for row in late)
    assert all("content" not in row for row in late)


@pytest.mark.asyncio
async def test_namespace_move_revokes_old_scope_but_multi_scope_keeps_live(backend):
    await insert(backend)
    first = (await feed(backend, namespaces=["A"]))[0]
    await mutate(backend, "UPDATE memories SET namespace = ? WHERE id = ?", "B", "journal_one")
    assert (await feed(backend, first, ["A"]))[0]["type"] == "withdrawal"
    assert (await feed(backend, first, ["B"]))[0]["type"] is None
    assert (await feed(backend, first, ["A", "B"]))[0]["type"] is None
    await mutate(backend, "UPDATE memories SET namespace = ? WHERE id = ?", "vault", "journal_one")
    events = await feed(backend, first, ["A", "B"])
    assert all(row["type"] == "withdrawal" and row["namespace"] != "vault" for row in events)


@pytest.mark.asyncio
async def test_journal_cursor_handles_equal_timestamps_and_rollback(backend):
    await insert(backend)
    first = (await feed(backend))[0]
    for content in ("revision two", "revision three"):
        await mutate(backend, "UPDATE memories SET content = ? WHERE id = ?", content, "journal_one")
    second = (await feed(backend, first, limit=1))[0]
    third = (await feed(backend, second, limit=1))[0]
    assert second["cursor_id"] != third["cursor_id"]
    assert second["content"] == third["content"] == "revision three"
    assert await feed(backend, third) == []
    with pytest.raises(RuntimeError):
        async with backend.transactional() as tx:
            await _Ops(tx, transaction_dialect(tx)).execute("DELETE FROM memories WHERE id = ?", "journal_one")
            raise RuntimeError("rollback")
    assert await feed(backend, third) == []


def memory(seq, content="replica facts"):
    return {
        "id": "remote",
        "content": content,
        "category": "facts",
        "namespace": "A",
        "created": "2020-01-01T00:00:00Z",
        "updated": "2020-01-01T00:00:00Z",
        "federation_sequence": seq,
    }


@pytest.mark.asyncio
async def test_receiver_rejects_stale_delete_and_resurrection_same_timestamp(backend):
    async with backend.transactional() as tx:
        await _store_memories(backend.federation, tx, "peer", [memory(10)])
        assert (
            await _apply_withdrawal(
                backend.federation,
                tx,
                "peer",
                {
                    "id": "remote",
                    "withdrawn_at": "2020-01-01T00:00:00Z",
                    "federation_sequence": 9,
                },
            )
            == 0
        )
        await _store_memories(backend.federation, tx, "peer", [memory(11, "new revision same timestamp")])
        row = await _Ops(tx, transaction_dialect(tx)).fetchone(
            "SELECT content FROM memories WHERE id = ?", "fed:peer:remote"
        )
        assert row["content"] == "new revision same timestamp"
        assert (
            await _apply_withdrawal(
                backend.federation,
                tx,
                "peer",
                {
                    "id": "remote",
                    "withdrawn_at": "2020-01-01T00:00:00Z",
                    "federation_sequence": 12,
                },
            )
            == 1
        )
        assert await _store_memories(backend.federation, tx, "peer", [memory(11)]) == (0, 0)
        assert (
            await _Ops(tx, transaction_dialect(tx)).fetchone("SELECT id FROM memories WHERE id = ?", "fed:peer:remote")
            is None
        )
        assert await _store_memories(backend.federation, tx, "peer", [memory(13)]) == (1, 0)


@pytest.mark.asyncio
async def test_source_wire_has_sequence_and_no_revoked_content(backend):
    await insert(backend)
    first = (await feed(backend))[0]
    assert _feed_item_from_row(first).federation_sequence == first["federation_sequence"]
    await mutate(backend, "UPDATE memories SET permission_mode = 600 WHERE id = ?", "journal_one")
    event = _feed_item_from_row((await feed(backend, first))[0]).model_dump()
    assert event["type"] == "withdrawal" and "content" not in event


@pytest.mark.asyncio
async def test_single_memory_authorized_fetch_returns_durable_delete(backend):
    await insert(backend)
    await mutate(backend, "DELETE FROM memories WHERE id = ?", "journal_one")
    async with backend.transactional() as tx:
        event = await backend.federation.get_feed_memory(tx, "journal_one", namespaces=["A"], categories=[])
        assert event["type"] == "withdrawal" and event["federation_sequence"] > 1
        assert (
            await backend.federation.get_feed_memory(tx, "journal_one", namespaces=["unrelated"], categories=[]) is None
        )


@pytest.mark.asyncio
async def test_full_peer_cursor_is_persistent_and_transactional(backend):
    from mnemos.persistence.federation_journal import load_peer_cursor, save_peer_cursor

    async with backend.transactional() as tx:
        await save_peer_cursor(tx, "peer", "opaque-journal-cursor", "scope-a")
    with pytest.raises(RuntimeError):
        async with backend.transactional() as tx:
            await save_peer_cursor(tx, "peer", "uncommitted-cursor", "scope-a")
            raise RuntimeError("rollback page")
    async with backend.transactional() as tx:
        assert await load_peer_cursor(tx, "peer", "scope-a") == "opaque-journal-cursor"
        assert await load_peer_cursor(tx, "peer", "scope-b") is None


@pytest.mark.asyncio
async def test_missing_consolidation_target_does_not_keep_stale_loser(backend):
    async with backend.transactional() as tx:
        await _store_memories(backend.federation, tx, "peer", [memory(1)])
        await _store_memories(
            backend.federation,
            tx,
            "peer",
            [
                {
                    "id": "remote",
                    "type": "consolidation",
                    "consolidated_into": "later",
                    "consolidated_at": "2020-01-01T00:00:00Z",
                    "federation_sequence": 2,
                }
            ],
        )
        assert (
            await _Ops(tx, transaction_dialect(tx)).fetchone("SELECT id FROM memories WHERE id = ?", "fed:peer:remote")
            is None
        )


@pytest.mark.asyncio
async def test_late_commit_is_published_after_cursor_without_blocking_other_writer(backend):
    if not hasattr(backend, "_pool"):
        pytest.skip("SQLite is intrinsically a single-writer database")
    import asyncio

    inserted, release = asyncio.Event(), asyncio.Event()

    async def delayed_transaction():
        async with backend.transactional() as tx:
            ops = _Ops(tx, transaction_dialect(tx))
            await ops.execute(
                "INSERT INTO memories(id,content,category,owner_id,namespace,permission_mode) VALUES(?,?,?,?,?,?)",
                "late",
                "late transaction",
                "facts",
                "alice",
                "A",
                644,
            )
            inserted.set()
            await release.wait()

    task = asyncio.create_task(delayed_transaction())
    try:
        await asyncio.wait_for(inserted.wait(), 3)
        await asyncio.wait_for(insert(backend, "early"), 3)
        initial = await asyncio.wait_for(feed(backend), 3)
        assert [row["id"] for row in initial] == ["early"]
    finally:
        release.set()
        await task
    late = await feed(backend, initial[-1])
    assert [row["id"] for row in late] == ["late"]
    assert late[0]["federation_sequence"] > initial[0]["federation_sequence"]


@pytest.mark.parametrize(
    "dialect,placeholder,limit",
    [
        ("postgres", "$1", "LIMIT 7"),
        ("sqlite", "?", "LIMIT 7"),
        ("mysql", "%s", "LIMIT 7"),
        ("oracle", ":1", "FETCH FIRST 7 ROWS ONLY"),
        ("db2", "?", "FETCH FIRST 7 ROWS ONLY"),
    ],
)
@pytest.mark.asyncio
async def test_current_journal_dialect_sql_and_scope_binds(monkeypatch, dialect, placeholder, limit):
    from mnemos.persistence import federation_journal as journal

    calls = []

    class Capture(_Ops):
        def __init__(self):
            self.dialect = dialect

        async def execute(self, query, *params):
            calls.append((self.sql(query), params))

        async def scalar(self, query, *params):
            return 5

        async def fetchone(self, query, *params):
            return None

        async def fetchall(self, query, *params):
            calls.append((self.sql(query), params))
            return []

    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setattr(journal, "_ops", lambda tx: Capture())
    assert (
        await journal.feed_query(
            None,
            None,
            since_updated=None,
            since_id="journal:5",
            namespaces=["tenant"],
            categories=["facts"],
            limit=7,
            prefer_compressed=False,
        )
        == []
    )
    query, params = next(call for call in calls if "SELECT e.*" in call[0])
    assert placeholder in query and limit in query
    assert "e.old_exportable = 1" in query and "e.new_exportable = 1" in query
    assert "e.old_public = 1" in query and "e.new_public = 1" in query
    assert params == (5, "tenant", "facts", "tenant", "facts")
    assert "tenant" not in query and "facts" not in query


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "vault"),
        ("federation_source", "peer"),
        ("permission_mode", 600),
        ("deleted_at", "now"),
        ("archived_at", "now"),
        ("consolidated_into", "other"),
    ],
)
def test_current_journal_revalidates_all_live_visibility_gates(monkeypatch, field, value):
    from mnemos.persistence.federation_journal import _authorized

    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    row = {"id": "one", "namespace": "A", "category": "facts", "permission_mode": 644}
    assert _authorized(row, ["A"], ["facts"])
    assert not _authorized({**row, field: value}, ["A"], ["facts"])


@pytest.mark.asyncio
async def test_single_memory_fetch_ignores_unpublished_tail(backend):
    await insert(backend)
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        for revision in range(260):
            await ops.execute("UPDATE memories SET content = ? WHERE id = ?", f"revision {revision}", "journal_one")
    async with backend.transactional() as tx:
        event = await backend.federation.get_feed_memory(tx, "journal_one", namespaces=["A"], categories=[])
        assert event["cursor_id"] == "journal:256"
        assert event["federation_sequence"] == 256
        assert event["content"] == "revision 259"
    async with backend.transactional() as tx:
        event = await backend.federation.get_feed_memory(tx, "journal_one", namespaces=["A"], categories=[])
        assert event["cursor_id"] == "journal:261"
        assert event["federation_sequence"] == 261


@pytest.mark.asyncio
async def test_sparse_filtered_feed_advances_empty_checkpoint(backend, monkeypatch):
    from mnemos.api.routes import federation as handler
    from mnemos.domain.federation import _decode_feed_cursor

    await insert(backend, namespace="B")
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        for revision in range(256):
            await ops.execute("UPDATE memories SET content = ? WHERE id = ?", f"revision {revision}", "journal_one")
    await insert(backend, mid="visible_tail", namespace="A")
    monkeypatch.setattr(handler, "require_federation_backend", lambda: backend)
    first = await handler.federation_feed(
        None, None, since=None, namespace="A", category=None, limit=100, prefer_compressed=False, copy_embeddings=False
    )
    assert first.memories == [] and first.has_more is True
    assert _decode_feed_cursor(first.next_cursor).memory_id == "journal:256"
    second = await handler.federation_feed(
        None,
        None,
        since=first.next_cursor,
        namespace="A",
        category=None,
        limit=100,
        prefer_compressed=False,
        copy_embeddings=False,
    )
    assert [m.id for m in second.memories] == ["visible_tail"]
    assert second.has_more is False
    assert _decode_feed_cursor(second.next_cursor).memory_id == "journal:258"
