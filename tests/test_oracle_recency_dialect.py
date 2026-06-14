"""Oracle ``semantic_search`` recency-boost dialect probes.

Driver-free, DB-free tests that capture the Oracle SQL and execute params
emitted by ``OracleMemoryRepository.semantic_search``. The recency boost
must keep Oracle 23ai vector top-K index eligibility by ordering on the
bare vector distance and applying the age adjustment in Python.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.persistence.oracle import OracleMemoryRepository
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _visibility() -> VisibilityFilter:
    return VisibilityFilter(scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace="default")


class _FakeOracleCursor:
    rowcount = 0

    def __init__(self, rows: list[tuple[Any, ...]], calls: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._calls = calls
        self.description = (("id",), ("content",), ("created",), ("updated",), ("rank_score",))

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        self._calls.append({"sql": sql, "params": params})

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    async def close(self) -> None:
        return None


class _FakeOracleConn:
    def __init__(self, rows: list[tuple[Any, ...]], calls: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._calls = calls

    def cursor(self) -> _FakeOracleCursor:
        return _FakeOracleCursor(self._rows, self._calls)


async def _run_semantic_search(
    *,
    rows: list[tuple[Any, ...]] | None = None,
    limit: int = 5,
    boost_recency: bool,
    recency_weight: float = 0.1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    tx = SimpleNamespace(conn=_FakeOracleConn(rows or [], calls))
    result = await OracleMemoryRepository().semantic_search(
        tx,
        embedding=[0.1, 0.2, 0.3],
        limit=limit,
        visibility=_visibility(),
        boost_recency=boost_recency,
        recency_weight=recency_weight,
    )
    return result, calls


@pytest.mark.asyncio
async def test_oracle_semantic_search_orders_by_bare_vector_distance() -> None:
    for boost_recency in (False, True):
        _result, calls = await _run_semantic_search(boost_recency=boost_recency)
        sql = calls[0]["sql"]
        sql_upper = " ".join(sql.upper().split())

        assert "ORDER BY VECTOR_DISTANCE(M.EMBEDDING, TO_VECTOR(:Q), COSINE) ASC" in sql_upper
        assert "(VECTOR_DISTANCE(M.EMBEDDING, TO_VECTOR(:Q), COSINE)) AS RANK_SCORE" in sql_upper
        assert "- :W" not in sql_upper
        assert "SYSDATE - CAST(M.UPDATED AS DATE)" not in sql_upper
        assert "1.0 / (1.0 +" not in sql_upper
        assert "w" not in calls[0]["params"]
        # The Python recency re-rank's date fallback reads row["created"]; if the
        # SELECT stops projecting it, the fallback silently dies (corrupt/NULL
        # updated -> date.min instead of created). Lock the projection here.
        assert "M.CREATED" in sql_upper
        assert "M.UPDATED" in sql_upper


@pytest.mark.asyncio
async def test_oracle_semantic_search_overfetches_only_when_recency_boosted() -> None:
    _result, calls = await _run_semantic_search(limit=5, boost_recency=False)
    assert calls[0]["params"]["limit"] == 5

    _result, calls = await _run_semantic_search(limit=5, boost_recency=True)
    assert calls[0]["params"]["limit"] == 20

    _result, calls = await _run_semantic_search(limit=100, boost_recency=True)
    assert calls[0]["params"]["limit"] == 200


@pytest.mark.asyncio
async def test_oracle_semantic_search_recency_rerank_is_conservative() -> None:
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
        ("valid-fresh-late", "d", today_iso, today_iso, 0.50),
    ]

    result, calls = await _run_semantic_search(rows=fetched_rows, limit=3, boost_recency=True, recency_weight=0.1)

    ids = [row["id"] for row in result]
    assert ids == ["valid-old-best", "valid-fresh", "valid-old-next"]
    assert {"corrupt-updated", "rank-none"}.isdisjoint(ids)
    assert {row["id"]: row["rank_score"] for row in result} == {
        "valid-old-best": 0.20,
        "valid-fresh": 0.31,
        "valid-old-next": 0.25,
    }
    assert len(result) <= 3
    assert calls[0]["params"]["limit"] == 12
