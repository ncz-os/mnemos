"""Search parity under filters, index failures, and restrictive audiences."""

import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.persistence import SqliteBackend
from mnemos.persistence import sqlite as storage
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from tests.test_sqlite_vec_candidate_path import _insert_memory, _persist_embedding


@pytest_asyncio.fixture(params=["async", "sync"])
async def backend(request, tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    if request.param == "sync":
        monkeypatch.setattr(storage, "aiosqlite", None)
    db = SqliteBackend(tmp_path / "search.db", SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))
    await db.open()
    try:
        assert db._vec_loaded
        assert db.memories._vec_index_complete
        yield db
    finally:
        await db.close()


def _visible(owner="alice"):
    return VisibilityFilter(scope=VisibilityScope.OWN_ONLY, user_id=owner, namespace=owner, group_ids=())


async def _add(db, tx, mid, vector, owner="alice"):
    await _insert_memory(db, tx, memory_id=mid, content=mid, owner_id=owner, namespace=owner)
    await _persist_embedding(db, tx, mid, vector)


@pytest.mark.asyncio
async def test_filters_bind_consistently_on_native_and_fallback_paths(backend):
    async with backend.transactional() as tx:
        await _add(backend, tx, "match", [1, 0, 0])
        await storage._execute(
            tx.conn,
            "UPDATE memories SET subcategory='sub', source_provider='provider', "
            "source_model='model', source_agent='agent' WHERE id='match'",
        )
        await storage._execute(tx.conn, "INSERT INTO memory_tags(memory_id,tag) VALUES ('match','needle')")
        await _add(backend, tx, "other", [1, 0, 0])
        filters = dict(
            category="solutions",
            subcategory="sub",
            source_provider="provider",
            source_model="model",
            source_agent="agent",
            tags=["needle"],
        )
        for native in (True, False):
            backend.memories._vec_index_complete = native
            rows = await backend.memories.semantic_search(
                tx, embedding=[1, 0, 0], limit=1, visibility=_visible(), **filters
            )
            assert [r["id"] for r in rows] == ["match"]


@pytest.mark.asyncio
async def test_zero_survivors_and_candidate_cap_do_not_hide_authorized_rows(backend):
    async with backend.transactional() as tx:
        await _add(backend, tx, "wanted", [0.5, 0.866, 0])
        for i in range(120):
            await _add(backend, tx, f"foreign-{i}", [1, 0, 0], "bob")
        # Even when growth is exhausted, the authoritative scan must find it.
        backend.memories._vec_candidate_cap = 100
        rows = await backend.memories.semantic_search(tx, embedding=[1, 0, 0], limit=1, visibility=_visible())
        assert [r["id"] for r in rows] == ["wanted"]


@pytest.mark.asyncio
async def test_failed_index_write_cannot_hide_canonical_update(backend, monkeypatch):
    async with backend.transactional() as tx:
        await _add(backend, tx, "winner", [0, 1, 0])
        await _add(backend, tx, "runner-up", [0.5, 0.866, 0])
        execute = storage._execute

        async def fail_vec_insert(conn, sql, params=()):
            if sql.startswith("INSERT INTO memory_embedding_vec"):
                raise sqlite3.OperationalError("simulated vector index write failure")
            return await execute(conn, sql, params)

        monkeypatch.setattr(storage, "_execute", fail_vec_insert)
        await backend.memories.upsert_memory_embedding(tx, "winner", [1, 0, 0])
        assert not backend.memories._vec_index_complete
        rows = await backend.memories.semantic_search(tx, embedding=[1, 0, 0], limit=1, visibility=_visible())
        assert [r["id"] for r in rows] == ["winner"]


@pytest.mark.asyncio
async def test_native_candidates_use_cosine_for_non_unit_vectors(backend):
    async with backend.transactional() as tx:
        await _add(backend, tx, "same-direction", [100, 0, 0])
        for i in range(120):
            await _add(backend, tx, f"nearby-{i}", [1, 1, 0])
        rows = await backend.memories.semantic_search(tx, embedding=[1, 0, 0], limit=1, visibility=_visible())
        assert [r["id"] for r in rows] == ["same-direction"]


@pytest.mark.asyncio
async def test_backfill_repairs_stale_vector_with_same_row_count(backend):
    async with backend.transactional() as tx:
        await _add(backend, tx, "winner", [1, 0, 0])
        await storage._execute(tx.conn, "UPDATE memory_embedding_vec SET embedding='[0,1,0]' WHERE memory_id='winner'")
        assert await backend._backfill_vec_table(tx.conn)
        packed = await storage._fetch_val(
            tx.conn, "SELECT embedding=vec_f32('[1,0,0]') FROM memory_embedding_vec WHERE memory_id='winner'"
        )
        assert packed == 1


@pytest.mark.asyncio
async def test_failed_index_write_invalidates_other_backend_reader(backend, monkeypatch):
    async with backend.transactional() as tx:
        await _add(backend, tx, "winner", [0, 1, 0])
        await _add(backend, tx, "runner-up", [0.5, 0.866, 0])
    other = SqliteBackend(backend._db_path, backend._settings)
    await other.open()
    execute = storage._execute

    async def fail_vec_insert(conn, sql, params=()):
        if sql.startswith("INSERT INTO memory_embedding_vec"):
            raise sqlite3.OperationalError("simulated index failure in other process")
        return await execute(conn, sql, params)

    try:
        monkeypatch.setattr(storage, "_execute", fail_vec_insert)
        async with backend.transactional() as tx:
            await backend.memories.upsert_memory_embedding(tx, "winner", [1, 0, 0])
        async with other.transactional() as tx:
            result = await other.memories.semantic_search(tx, embedding=[1, 0, 0], limit=1, visibility=_visible())
            assert [row["id"] for row in result] == ["winner"]
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_direct_canonical_embedding_update_invalidates_native_index(backend):
    async with backend.transactional() as tx:
        await _add(backend, tx, "winner", [0, 1, 0])
        await _add(backend, tx, "runner-up", [0.5, 0.866, 0])
        await storage._execute(tx.conn, "UPDATE memory_embeddings SET embedding='[1,0,0]' WHERE memory_id='winner'")
        result = await backend.memories.semantic_search(tx, embedding=[1, 0, 0], limit=1, visibility=_visible())
        assert [row["id"] for row in result] == ["winner"]


@pytest.mark.asyncio
async def test_stored_zero_vectors_cannot_displace_real_cosine_match(backend):
    async with backend.transactional() as tx:
        for i in range(120):
            await _add(backend, tx, f"zero-{i}", [0, 0, 0])
        await _add(backend, tx, "winner", [1, 0, 0])
        result = await backend.memories.semantic_search(tx, embedding=[1, 0, 0], limit=1, visibility=_visible())
        assert [row["id"] for row in result] == ["winner"]


@pytest.mark.asyncio
async def test_restart_rejects_unchanged_zero_vector_index(backend):
    async with backend.transactional() as tx:
        await _add(backend, tx, "zero", [0, 0, 0])
        await _add(backend, tx, "winner", [1, 0, 0])
    other = SqliteBackend(backend._db_path, backend._settings)
    await other.open()
    try:
        assert not other.memories._vec_index_complete
        async with other.transactional() as tx:
            result = await other.memories.semantic_search(tx, embedding=[1, 0, 0], limit=1, visibility=_visible())
            assert [row["id"] for row in result] == ["winner"]
    finally:
        await other.close()


@pytest.mark.parametrize(
    "vector", [[0, 0, 0], [float("nan"), 0, 0], [float("inf"), 0, 0], [1e-100, 0, 0], [1e30, 0, 0]]
)
def test_float32_unsafe_cosine_vectors_abstain(vector):
    assert not storage._vec_cosine_safe(vector)
    assert storage._vec_cosine_safe([100, 0, 0])
