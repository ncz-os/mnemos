"""MySQL ``semantic_search`` recency-boost dialect probes.

Driver-free, DB-free tests that capture the MySQL SQL and positional
params emitted by ``MysqlMemoryRepository.semantic_search``. The recency
boost must keep MySQL 9 vector top-K index eligibility by ordering on the
bare vector distance and applying the age adjustment in Python.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from mnemos.persistence.mysql import MysqlFederationRepository, MysqlMemoryRepository
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _visibility() -> VisibilityFilter:
    return VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace="default")


class _FakeMysqlCursor:
    rowcount = 0

    def __init__(self, rows: list[tuple[Any, ...]], calls: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._calls = calls
        self.description = (("id",), ("content",), ("created",), ("updated",), ("rank_score",))

    async def __aenter__(self) -> _FakeMysqlCursor:
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        return None

    async def execute(self, sql: str, params: list[Any]) -> None:
        self._calls.append({"sql": sql, "params": tuple(params)})

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeMysqlConn:
    def __init__(self, rows: list[tuple[Any, ...]], calls: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._calls = calls

    def cursor(self) -> _FakeMysqlCursor:
        return _FakeMysqlCursor(self._rows, self._calls)


async def _run_semantic_search(
    *,
    rows: list[tuple[Any, ...]] | None = None,
    limit: int = 5,
    boost_recency: bool,
    recency_weight: float = 0.1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    repo = MysqlMemoryRepository()
    repo._expected_embedding_dim = 3
    tx = SimpleNamespace(conn=_FakeMysqlConn(rows or [], calls))
    result = await repo.semantic_search(
        tx,
        embedding=[0.1, 0.2, 0.3],
        limit=limit,
        visibility=_visibility(),
        boost_recency=boost_recency,
        recency_weight=recency_weight,
    )
    return result, calls


@pytest.mark.asyncio
async def test_mysql_semantic_search_orders_by_bare_vector_distance() -> None:
    for boost_recency in (False, True):
        _result, calls = await _run_semantic_search(boost_recency=boost_recency)
        sql = calls[0]["sql"]
        sql_upper = " ".join(sql.upper().split())

        assert "VECTOR_DISTANCE(M.EMBEDDING, TO_VECTOR(%S), 'COSINE') AS RANK_SCORE" in sql_upper
        assert "ORDER BY RANK_SCORE ASC" in sql_upper
        assert "TIMESTAMPDIFF" not in sql_upper
        assert "NOW(6)" not in sql_upper
        assert "/ 86400" not in sql_upper
        assert "1.0 / (1.0 +" not in sql_upper
        assert "M.CREATED" in sql_upper
        assert "M.UPDATED" in sql_upper


@pytest.mark.asyncio
async def test_mysql_semantic_search_overfetches_only_when_recency_boosted() -> None:
    _result, calls = await _run_semantic_search(limit=5, boost_recency=False)
    assert calls[0]["params"][-1] == 5

    _result, calls = await _run_semantic_search(limit=5, boost_recency=True)
    assert calls[0]["params"][-1] == 20

    _result, calls = await _run_semantic_search(limit=100, boost_recency=True)
    assert calls[0]["params"][-1] == 200


@pytest.mark.asyncio
async def test_mysql_semantic_search_recency_rerank_is_conservative() -> None:
    today = datetime.now(timezone.utc).date()
    old = today - timedelta(days=30)
    today_iso = f"{today.isoformat()}T00:00:00Z"
    old_iso = f"{old.isoformat()}T00:00:00Z"
    fetched_rows = [
        ("valid-old-best", "a", old_iso, old_iso, 0.20),
        ("valid-old-next", "b", old_iso, old_iso, 0.25),
        ("valid-fresh", "c", today_iso, today_iso, 0.31),
        ("corrupt-updated", "bad timestamp", old_iso, "not-a-date", 0.27),
        ("rank-none", "none", today_iso, today_iso, None),
        ("rank-nan", "nan", today_iso, today_iso, float("nan")),
        ("valid-fresh-late", "d", today_iso, today_iso, 0.50),
    ]

    result, calls = await _run_semantic_search(rows=fetched_rows, limit=3, boost_recency=True, recency_weight=0.1)

    ids = [row["id"] for row in result]
    assert ids == ["valid-old-best", "valid-fresh", "valid-old-next"]
    assert {"corrupt-updated", "rank-none", "rank-nan"}.isdisjoint(ids)
    assert {row["id"]: row["rank_score"] for row in result} == {
        "valid-old-best": 0.20,
        "valid-fresh": 0.31,
        "valid-old-next": 0.25,
    }
    assert len(result) <= 3
    assert calls[0]["params"][-1] == 12


@pytest.mark.asyncio
async def test_mysql_federation_feed_embedding_bind_precedes_filters() -> None:
    calls: list[dict[str, Any]] = []
    tx = SimpleNamespace(conn=_FakeMysqlConn([], calls))
    repo = MysqlFederationRepository()
    since = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    with (
        patch("mnemos.core.config.embed_http_model_override", return_value="embed-model"),
        patch("mnemos.core.config.get_settings"),
    ):
        await repo.feed_query(
            tx,
            since_updated=since,
            since_id="cursor-id",
            namespaces=["tenant-a"],
            categories=["keep"],
            limit=25,
            prefer_compressed=False,
            include_embedding=True,
        )

    sql = " ".join(calls[0]["sql"].split()).lower()
    params = calls[0]["params"]

    assert "%s as embedding_model" in sql
    assert sql.index("%s as embedding_model") < sql.index("where m.federation_source is null")
    assert params == (
        "embed-model",
        since,
        since,
        "cursor-id",
        "tenant-a",
        "keep",
        since,
        since,
        "cursor-id",
        "tenant-a",
        "keep",
        25,
    )
