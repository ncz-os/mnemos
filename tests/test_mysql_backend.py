from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mnemos.persistence.mysql import MysqlMemoryRepository
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


pytestmark = pytest.mark.asyncio


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


async def test_mysql_semantic_search_without_recency_uses_visibility_params_once():
    repo = MysqlMemoryRepository()
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
    captured: dict[str, object] = {}

    async def execute(sql, params):
        captured["sql"] = sql
        captured["params"] = tuple(params)

    cursor.execute = AsyncMock(side_effect=execute)
    tx = _tx_for_cursor(cursor)
    embedder = MagicMock()
    embedder.embed.return_value = [0.25, 0.5, 0.75]

    with patch("mnemos.core.lifecycle.get_embedder", return_value=embedder, create=True):
        from mnemos.core import lifecycle

        embedding = lifecycle.get_embedder().embed("needle")

    await repo.semantic_search(
        tx,
        embedding=embedding,
        limit=7,
        visibility=VisibilityFilter(
            scope=VisibilityScope.OWN_ONLY,
            user_id="alice",
            namespace="ns1",
            group_ids=frozenset(),
        ),
        boost_recency=False,
    )

    sql = str(captured["sql"])
    compact_sql = " ".join(sql.split()).lower()
    params = captured["params"]

    assert "recency" not in compact_sql
    assert "timestampdiff" not in compact_sql
    assert "created_at" not in compact_sql
    assert compact_sql.count("vec_fromtext(%s)") == 1
    assert "where" in compact_sql
    assert "m.owner_id = %s and m.namespace = %s" in compact_sql
    assert "order by rank_score asc" in compact_sql
    assert params == ("[0.2500000,0.5000000,0.7500000]", "alice", "ns1", 7)
    assert len(params) == 4
    assert params[1:3] == ("alice", "ns1")
    embedder.embed.assert_called_once_with("needle")
