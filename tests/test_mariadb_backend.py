from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mnemos.persistence.mariadb import (
    MariadbBackend,
    MariadbMemoryRepository,
    _parse_mariadb_dsn,
    create_mariadb_pool,
)
from mnemos.persistence.mysql import _parse_mysql_dsn
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


class _AsyncCursorContext:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self._cursor

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _tx_for_cursor(cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_AsyncCursorContext(cursor))
    return SimpleNamespace(conn=conn)


def _visibility() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="alice",
        namespace="ns1",
        group_ids=frozenset(),
    )


@pytest.mark.asyncio
async def test_mariadb_insert_memory_uses_mariadb_vector_constructor() -> None:
    repo = MariadbMemoryRepository()
    repo._expected_embedding_dim = 3
    cursor = MagicMock()
    captured: dict[str, object] = {}

    async def execute(sql, params):
        captured["sql"] = sql
        captured["params"] = tuple(params)
        cursor.rowcount = 1

    cursor.execute = AsyncMock(side_effect=execute)
    tx = _tx_for_cursor(cursor)

    result = await repo.insert_memory(
        tx,
        memory_id="mem1",
        content="hello",
        category="facts",
        subcategory=None,
        metadata_json="{}",
        quality_rating=3,
        owner_id="alice",
        namespace="ns1",
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content="hello",
        embedding=[0.1, 0.2, 0.3],
        created=None,
        updated=None,
    )

    sql = " ".join(str(captured["sql"]).split()).lower()
    assert result == "INSERT 0 1"
    assert "vec_fromtext(%s)" in sql
    assert "to_vector" not in sql
    assert "vector_distance" not in sql
    assert captured["params"][15] == "[0.1000000,0.2000000,0.3000000]"


@pytest.mark.asyncio
async def test_mariadb_semantic_search_uses_mariadb_vector_distance() -> None:
    repo = MariadbMemoryRepository()
    repo._expected_embedding_dim = 3
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
    captured: dict[str, object] = {}

    async def execute(sql, params):
        captured["sql"] = sql
        captured["params"] = tuple(params)

    cursor.execute = AsyncMock(side_effect=execute)
    tx = _tx_for_cursor(cursor)

    await repo.semantic_search(
        tx,
        embedding=[0.1, 0.2, 0.3],
        limit=7,
        visibility=_visibility(),
        boost_recency=False,
    )

    sql = " ".join(str(captured["sql"]).split()).lower()
    assert "vec_distance_cosine(m.embedding, vec_fromtext(%s)) as rank_score" in sql
    assert "order by rank_score asc" in sql
    assert "to_vector" not in sql
    assert "vector_distance" not in sql
    assert captured["params"] == ("[0.1000000,0.2000000,0.3000000]", "alice", "ns1", 7)


@pytest.mark.asyncio
async def test_mariadb_upsert_embedding_uses_mariadb_vector_constructor() -> None:
    repo = MariadbMemoryRepository()
    repo._expected_embedding_dim = 3
    cursor = MagicMock()
    captured: dict[str, object] = {}

    async def execute(sql, params):
        captured["sql"] = sql
        captured["params"] = tuple(params)

    cursor.execute = AsyncMock(side_effect=execute)
    tx = _tx_for_cursor(cursor)

    await repo.upsert_memory_embedding(tx, "mem1", [0.1, 0.2, 0.3])

    sql = " ".join(str(captured["sql"]).split()).lower()
    assert "vec_fromtext(%s)" in sql
    assert "to_vector" not in sql
    assert "vector_distance" not in sql
    assert captured["params"] == ("[0.1000000,0.2000000,0.3000000]", "mem1")


def test_mariadb_dsn_reuses_mysql_parser() -> None:
    dsn = "mariadb://user%40example:p%40ss@db.example:3307/mnemos"
    assert _parse_mariadb_dsn(dsn) == _parse_mysql_dsn(dsn)
    assert _parse_mariadb_dsn(dsn) == {
        "host": "db.example",
        "port": 3307,
        "db": "mnemos",
        "charset": "utf8mb4",
        "autocommit": False,
        "user": "user@example",
        "password": "p@ss",
    }


def test_mariadb_backend_flags_and_repositories() -> None:
    backend = MariadbBackend(None, SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))

    assert backend.supports_mariadb_vector is True
    assert backend.inline_embedding_searchable is True
    assert isinstance(backend.memories, MariadbMemoryRepository)


@pytest.mark.asyncio
async def test_mariadb_live_ping_when_dsn_configured() -> None:
    dsn = os.getenv("MARIADB_DSN")
    if not dsn:
        pytest.skip("MARIADB_DSN unset")
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    pool = await create_mariadb_pool(dsn)
    backend = MariadbBackend(pool, SimpleNamespace(database=SimpleNamespace(embedding_dim=768)))
    try:
        await backend.open()
        assert await backend.ping() is True
    finally:
        await backend.close()
