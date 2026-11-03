"""FB135 (audit M6): soft-deleted memories are invisible to get/list/FTS.

SQLite must match Postgres: a row with ``deleted_at IS NOT NULL`` is hidden
from ``get_memory``, ``list_memories`` and ``fts_search``, and becomes
visible again when undeleted. Postgres is the reference (its three methods
all filter ``deleted_at IS NULL``); the sqlite arm is the one under test.

Live arms: SQLite always; Postgres via ``MNEMOS_TEST_DB`` (throwaway
database per test, same shape as
tests/test_api_key_creation_cross_backend.py), skipped otherwise.

There is no repo-level undelete API, so T4 restores with one raw
``UPDATE memories SET deleted_at = NULL`` per arm, stated plainly.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _vis() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
        exclude_namespaces=(),
    )


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def backend(request, tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    if request.param == "sqlite":
        from mnemos.persistence.sqlite import SqliteBackend

        instance = SqliteBackend(tmp_path / "m6_deleted_at.db", settings)
        await instance.open()
        try:
            yield request.param, instance
        finally:
            await instance.close()
    else:
        dsn = os.getenv("MNEMOS_TEST_DB")
        if not dsn:
            pytest.skip("set MNEMOS_TEST_DB for live PostgreSQL M6 tests")
        import asyncpg

        from mnemos.persistence.postgres import PostgresBackend
        from urllib.parse import urlsplit, urlunsplit

        name = "mnemos_m6_" + uuid.uuid4().hex[:16]
        admin = await asyncpg.connect(dsn)
        pool = None
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
            target = urlunsplit(urlsplit(dsn)._replace(path="/" + name))
            pool = await asyncpg.create_pool(target, min_size=1, max_size=4)
            instance = PostgresBackend(pool, settings)
            await instance.open()
            yield request.param, instance
        finally:
            if pool is not None:
                await pool.close()
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.close()


async def _seed(backend, *, content: str, namespace: str = "m6-ns") -> str:
    mid = f"m6_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=mid,
            content=content,
            category="facts",
            subcategory=None,
            metadata_json=json.dumps({}),
            quality_rating=80,
            owner_id="m6-user",
            namespace=namespace,
            permission_mode=600,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=content,
            embedding=None,
            created=now,
            updated=now,
        )
    return mid


async def _undelete_raw(arm: str, backend, memory_id: str) -> None:
    """No repo-level undelete exists; clear the tombstone with one UPDATE."""
    async with backend.transactional() as tx:
        conn = backend.memories._conn(tx)
        if arm == "sqlite":
            await conn.execute("UPDATE memories SET deleted_at = NULL WHERE id = ?", (memory_id,))
        else:
            await conn.execute("UPDATE memories SET deleted_at = NULL WHERE id = $1", memory_id)


@pytest.mark.asyncio
async def test_soft_deleted_memory_invisible_to_get(backend):
    _arm, instance = backend
    mid = await _seed(instance, content="m6 get-target alpha")
    async with instance.transactional() as tx:
        assert await instance.memories.get_memory(tx, mid, visibility=_vis()) is not None
        await instance.memories.soft_delete_memory(tx, mid, visibility=_vis())
    async with instance.transactional() as tx:
        assert await instance.memories.get_memory(tx, mid, visibility=_vis()) is None


@pytest.mark.asyncio
async def test_soft_deleted_memory_invisible_to_list(backend):
    _arm, instance = backend
    live = await _seed(instance, content="m6 list-live beta")
    gone = await _seed(instance, content="m6 list-gone beta")
    async with instance.transactional() as tx:
        await instance.memories.soft_delete_memory(tx, gone, visibility=_vis())
    async with instance.transactional() as tx:
        rows, total = await instance.memories.list_memories(tx, visibility=_vis(), limit=50)
    ids = [r["id"] for r in rows]
    assert live in ids
    assert gone not in ids
    assert total == len(rows) == 1


@pytest.mark.asyncio
async def test_soft_deleted_memory_invisible_to_fts(backend):
    _arm, instance = backend
    live_term = "m6livequuxterm"
    gone_term = "m6gonequuxterm"
    await _seed(instance, content=f"the quick {live_term} fox")
    gone = await _seed(instance, content=f"the slow {gone_term} turtle")
    async with instance.transactional() as tx:
        # Control: the live row is searchable before the delete.
        found_live = await instance.memories.fts_search(
            tx, query=live_term, limit=10, visibility=_vis()
        )
        assert [r["id"] for r in found_live]
        await instance.memories.soft_delete_memory(tx, gone, visibility=_vis())
    async with instance.transactional() as tx:
        assert await instance.memories.fts_search(
            tx, query=gone_term, limit=10, visibility=_vis()
        ) == []
        # The delete must not blind the index to live rows.
        assert await instance.memories.fts_search(
            tx, query=live_term, limit=10, visibility=_vis()
        ) != []


@pytest.mark.asyncio
async def test_undelete_restores_get_list_fts_visibility(backend):
    arm, instance = backend
    term = "m6resurrectterm"
    mid = await _seed(instance, content=f"back from the tomb {term}")
    async with instance.transactional() as tx:
        await instance.memories.soft_delete_memory(tx, mid, visibility=_vis())
    async with instance.transactional() as tx:
        assert await instance.memories.get_memory(tx, mid, visibility=_vis()) is None
    await _undelete_raw(arm, instance, mid)
    async with instance.transactional() as tx:
        assert await instance.memories.get_memory(tx, mid, visibility=_vis()) is not None
        rows, _total = await instance.memories.list_memories(tx, visibility=_vis(), limit=50)
        assert mid in [r["id"] for r in rows]
        found = await instance.memories.fts_search(tx, query=term, limit=10, visibility=_vis())
        assert mid in [r["id"] for r in found]
