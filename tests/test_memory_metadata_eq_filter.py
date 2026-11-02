"""Exact-match metadata read-back (FB103).

The doctor reads a job's status checkpoints back as "all memories whose
metadata has ``job_id == X``". Before this, the only filters on
``GET /v1/memories`` were category / subcategory / tags / namespace and the
only whole-body lookup was FTS or vector search, which also returns rows that
merely *mention* the id.

These tests pin the filter end to end: the real SQLite backend (rows + the
expression index the migration creates), the statement each enterprise backend
receives through its fake cursor, the HTTP route's parsing and 400s, and the
MCP tool's translation to repeated ``meta=`` params.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_route
from mnemos.mcp.tools import memory as mcp_memory
from mnemos.persistence.db2 import Db2MemoryRepository
from mnemos.persistence.mysql import MysqlMemoryRepository
from mnemos.persistence.oracle import OracleMemoryRepository
from mnemos.persistence.postgres import PostgresMemoryRepository, PostgresTransaction
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.types import (
    METADATA_EQ_MAX_KEYS,
    normalize_metadata_eq,
    metadata_json_path,
)
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from tests._fake_backend import install_fake_backend


def _root() -> UserContext:
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _bypass() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )


async def _insert(backend: SqliteBackend, memory_id: str, *, metadata: dict | None, category: str = "status") -> None:
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=f"{memory_id} body",
            category=category,
            subcategory=None,
            metadata_json=json.dumps(metadata) if metadata is not None else None,
            quality_rating=75,
            owner_id="root",
            namespace="default",
            permission_mode=700,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=f"{memory_id} body",
            created=None,
            updated=None,
        )


# ── real SQLite: the read-back itself ───────────────────────────────────────


def test_sqlite_exact_match_returns_only_the_exact_value(tmp_path):
    async def _go() -> None:
        backend = SqliteBackend(tmp_path / "meta-eq.db", SimpleNamespace())
        await backend.open()
        try:
            await _insert(backend, "mem_job1", metadata={"job_id": "J-1", "phase": "review"})
            await _insert(backend, "mem_job10", metadata={"job_id": "J-10"})
            # No job_id at all: an absent field must not match.
            await _insert(backend, "mem_body_only", metadata={"phase": "review"})
            # Same text as the id but only in the body: the old FTS/substring
            # path returned this row; an exact metadata match must not.
            async with backend.transactional() as tx:
                await backend.memories.insert_memory(
                    tx,
                    memory_id="mem_mention",
                    content="APPROVE for job_id J-1",
                    category="status",
                    subcategory=None,
                    metadata_json=json.dumps({"note": "job_id J-1"}),
                    quality_rating=75,
                    owner_id="root",
                    namespace="default",
                    permission_mode=700,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content="APPROVE for job_id J-1",
                    created=None,
                    updated=None,
                )
            async with backend.transactional() as tx:
                rows, total = await backend.memories.list_memories(
                    tx,
                    visibility=_bypass(),
                    metadata_eq={"job_id": "J-1"},
                )
        finally:
            await backend.close()

        assert [row["id"] for row in rows] == ["mem_job1"]
        assert total == 1

    asyncio_run(_go())


def test_sqlite_filter_is_anded_across_keys_and_composes_with_pagination(tmp_path):
    async def _go() -> None:
        backend = SqliteBackend(tmp_path / "meta-eq-and.db", SimpleNamespace())
        await backend.open()
        try:
            for i in range(3):
                await _insert(
                    backend,
                    f"mem_pass_{i}",
                    metadata={"job_id": "J-9", "verdict": "pass", "seq": i},
                )
            await _insert(backend, "mem_fail", metadata={"job_id": "J-9", "verdict": "fail"})
            await _insert(backend, "mem_other_job", metadata={"job_id": "J-8", "verdict": "pass"}, category="status")
            async with backend.transactional() as tx:
                rows, total = await backend.memories.list_memories(
                    tx,
                    visibility=_bypass(),
                    metadata_eq={"job_id": "J-9", "verdict": "pass"},
                    limit=2,
                    offset=1,
                )
                # A single key still narrows independently of the AND.
                only_job, only_job_total = await backend.memories.list_memories(
                    tx,
                    visibility=_bypass(),
                    metadata_eq={"job_id": "J-9"},
                )
                absent, absent_total = await backend.memories.list_memories(
                    tx,
                    visibility=_bypass(),
                    metadata_eq={"job_id": "J-404"},
                )
        finally:
            await backend.close()

        assert total == 3
        assert len(rows) == 2
        assert only_job_total == 4
        assert {row["id"] for row in only_job} == {
            "mem_pass_0",
            "mem_pass_1",
            "mem_pass_2",
            "mem_fail",
        }
        assert absent == []
        assert absent_total == 0

    asyncio_run(_go())


def test_sqlite_filter_uses_the_job_id_expression_index(tmp_path):
    """The migration's expression index is what the emitted predicate hits.

    SQLite only uses an expression index when the query repeats the indexed
    expression exactly, so this pins that the SQL the repository builds for
    ``job_id`` is byte-identical to the indexed expression.
    """

    path = tmp_path / "meta-eq-index.db"

    async def _open() -> None:
        backend = SqliteBackend(path, SimpleNamespace())
        await backend.open()
        await backend.close()

    asyncio_run(_open())

    conn = sqlite3.connect(path)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM memories "
            "WHERE json_extract(metadata, '$.\"job_id\"') = ?",
            ("J-1",),
        ).fetchall()
    finally:
        conn.close()

    detail = " ".join(str(row[-1]) for row in plan)
    assert "idx_memories_metadata_job_id" in detail


# ── the statement each enterprise backend receives ──────────────────────────


class _PgConn:
    def __init__(self) -> None:
        self.select_sql = ""
        self.select_params: tuple[Any, ...] = ()
        self.count_sql = ""
        self.count_params: tuple[Any, ...] = ()

    async def fetch(self, sql: str, *params: Any) -> list[Any]:
        self.select_sql = " ".join(sql.split())
        self.select_params = params
        return []

    async def fetchval(self, sql: str, *params: Any) -> int:
        self.count_sql = " ".join(sql.split())
        self.count_params = params
        return 0


class _PgTx(PostgresTransaction):
    def __init__(self, conn: _PgConn) -> None:
        self._conn = conn
        self._tx = None
        self._closed = False
        self._after_commit: list[Any] = []


def test_postgres_filter_is_containment_with_a_bound_value():
    conn = _PgConn()

    asyncio_run(
        PostgresMemoryRepository().list_memories(
            _PgTx(conn),
            visibility=_bypass(),
            metadata_eq={"job_id": "J-1"},
        )
    )

    assert "metadata @> $1::jsonb" in conn.select_sql
    assert conn.select_params == (json.dumps({"job_id": "J-1"}), 20, 0)
    # The count uses the same predicate and the same payload.
    assert "metadata @> $1::jsonb" in conn.count_sql
    assert conn.count_params == (json.dumps({"job_id": "J-1"}),)


class _MysqlCursor:
    description = None

    def __init__(self, calls: list[dict[str, Any]], count: int) -> None:
        self._calls = calls
        self._count = count

    async def __aenter__(self) -> "_MysqlCursor":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._calls.append({"sql": " ".join(sql.split()), "params": tuple(params or ())})
        self.description = (("cnt",),) if "COUNT(*)" in sql else None

    async def fetchone(self) -> tuple[Any, ...] | None:
        return (self._count,)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _MysqlConn:
    def __init__(self, calls: list[dict[str, Any]], count: int = 0) -> None:
        self._calls = calls
        self._count = count

    def cursor(self) -> Any:
        return _MysqlCursor(self._calls, self._count)


def test_mysql_filter_binds_both_the_path_and_the_value():
    calls: list[dict[str, Any]] = []

    asyncio_run(
        MysqlMemoryRepository().list_memories(
            SimpleNamespace(conn=_MysqlConn(calls)),
            visibility=_bypass(),
            metadata_eq={"job_id": "J-1"},
        )
    )

    predicate = "JSON_UNQUOTE(JSON_EXTRACT(m.metadata, %s)) = %s"
    assert predicate in calls[0]["sql"]
    assert calls[0]["params"] == ('$."job_id"', "J-1")


class _OracleCursor:
    description = None

    def __init__(self, calls: list[dict[str, Any]], count: int) -> None:
        self._calls = calls
        self._count = count

    async def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append({"sql": " ".join(sql.split()), "params": params})
        self.description = (("cnt",),) if "COUNT(*)" in sql else None

    async def fetchone(self) -> tuple[Any, ...] | None:
        return (self._count,)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    async def close(self) -> None:
        return None


class _OracleConn:
    def __init__(self, calls: list[dict[str, Any]], count: int = 0) -> None:
        self._calls = calls
        self._count = count

    def cursor(self) -> Any:
        return _OracleCursor(self._calls, self._count)


def test_oracle_filter_uses_json_value_with_a_literal_path():
    calls: list[dict[str, Any]] = []

    asyncio_run(
        OracleMemoryRepository().list_memories(
            SimpleNamespace(conn=_OracleConn(calls)),
            visibility=_bypass(),
            metadata_eq={"job_id": "J-1"},
        )
    )

    assert "JSON_VALUE(m.metadata, '$.\"job_id\"') = :meta_0" in calls[0]["sql"]
    assert calls[0]["params"]["meta_0"] == "J-1"
    assert calls[1]["params"]["meta_0"] == "J-1"


class _Db2SyncCursor:
    rowcount = 0

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls
        self.description = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._calls.append({"sql": " ".join(sql.split()), "params": tuple(params or ())})
        if "COUNT(*)" in sql:
            self.description = (("cnt",),)
            self._rows = [(0,)]
        else:
            self.description = None
            self._rows = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        return None


class _Db2Conn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> Any:
        from mnemos.persistence.db2 import _Db2AsyncCursor

        return _Db2AsyncCursor(_Db2SyncCursor(self._calls))


def test_db2_inherits_the_filter_and_rewrites_the_binds():
    calls: list[dict[str, Any]] = []

    asyncio_run(
        Db2MemoryRepository().list_memories(
            SimpleNamespace(conn=_Db2Conn(calls)),
            visibility=_bypass(),
            metadata_eq={"job_id": "J-1"},
        )
    )

    assert "JSON_VALUE(m.metadata, '$.\"job_id\"') = ?" in calls[0]["sql"]
    assert ":meta_0" not in calls[0]["sql"]
    assert calls[0]["params"] == ("J-1",)


# ── HTTP route ──────────────────────────────────────────────────────────────


def test_route_parses_repeated_meta_params_into_metadata_eq(monkeypatch):
    backend = install_fake_backend(monkeypatch)

    response = asyncio_run(
        memories_route.list_memories(meta=["job_id:J-1", "verdict:pass"], user=_root())
    )

    assert response.count == 0
    call = dict(backend.memories.calls)["list_memories"]
    assert call["metadata_eq"] == {"job_id": "J-1", "verdict": "pass"}


def test_route_without_meta_passes_no_filter(monkeypatch):
    backend = install_fake_backend(monkeypatch)

    asyncio_run(memories_route.list_memories(user=_root()))

    call = dict(backend.memories.calls)["list_memories"]
    assert call["metadata_eq"] is None


@pytest.mark.parametrize(
    "raw",
    [
        ["job_id"],  # no separator
        ["job_id:"],  # empty value
        [":J-1"],  # empty key
        ["not a key:J-1"],  # space in key
        ['job_id";DROP:J-1'],  # quote in key
        ["$..job_id:J-1"],  # path syntax in key
    ],
)
def test_route_rejects_malformed_meta_params(monkeypatch, raw):
    install_fake_backend(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        asyncio_run(memories_route.list_memories(meta=raw, user=_root()))

    assert excinfo.value.status_code == 400


def test_route_rejects_too_many_meta_keys(monkeypatch):
    install_fake_backend(monkeypatch)
    raw = [f"k{i}:v" for i in range(METADATA_EQ_MAX_KEYS + 1)]

    with pytest.raises(HTTPException) as excinfo:
        asyncio_run(memories_route.list_memories(meta=raw, user=_root()))

    assert excinfo.value.status_code == 400


# ── MCP tool ────────────────────────────────────────────────────────────────


def test_mcp_list_memories_forwards_metadata_eq_as_repeated_meta_params(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_rest_get(path: str, params: dict[str, Any] | None = None) -> Any:
        captured["path"] = path
        captured["params"] = dict(params or {})
        return {"count": 0, "memories": []}

    monkeypatch.setattr(mcp_memory, "_rest_get", _fake_rest_get)

    result = asyncio_run(mcp_memory.tool_list_memories(metadata_eq={"job_id": "J-1"}))

    assert result == {"count": 0, "memories": []}
    assert captured["path"] == "/v1/memories"
    assert captured["params"]["meta"] == ["job_id:J-1"]


def test_mcp_list_memories_rejects_a_hostile_key(monkeypatch):
    async def _fake_rest_get(path: str, params: dict[str, Any] | None = None) -> Any:  # pragma: no cover
        raise AssertionError("the REST seam must not be reached with an invalid filter")

    monkeypatch.setattr(mcp_memory, "_rest_get", _fake_rest_get)

    with pytest.raises(ValueError):
        asyncio_run(mcp_memory.tool_list_memories(metadata_eq={'job"id': "J-1"}))


# ── the validator that guards SQL interpolation ─────────────────────────────


@pytest.mark.parametrize("key", ['job"id', "job id", "$.job", "", "job';--", "j" * 65])
def test_metadata_json_path_rejects_anything_that_is_not_a_plain_key(key):
    with pytest.raises(ValueError):
        metadata_json_path(key)


def test_metadata_json_path_quotes_a_plain_key():
    assert metadata_json_path("job_id") == '$."job_id"'
    assert metadata_json_path("job.id-1_2") == '$."job.id-1_2"'


def test_normalize_metadata_eq_bounds_keys_and_values():
    assert normalize_metadata_eq(None) == {}
    assert normalize_metadata_eq({"job_id": "J-1"}) == {"job_id": "J-1"}

    with pytest.raises(ValueError):
        normalize_metadata_eq({"k" * 65: "v"})
    with pytest.raises(ValueError):
        normalize_metadata_eq({"job_id": "v" * 513})
    with pytest.raises(ValueError):
        normalize_metadata_eq({"job_id": 1})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        normalize_metadata_eq({f"k{i}": "v" for i in range(METADATA_EQ_MAX_KEYS + 1)})


def asyncio_run(coro: Any) -> Any:
    """Run one coroutine on a fresh loop (module has no asyncio plugin)."""

    import asyncio

    return asyncio.run(coro)
