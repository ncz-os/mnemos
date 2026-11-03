"""FB137 audit M10 (2026-11-03): stable ``list_memories`` ordering + paging.

Contract under test — ``list_memories`` keeps offset paging and results
order otherwise unchanged; what changes is that ``ORDER BY created DESC``
now carries a unique ``id ASC`` tiebreaker on ALL FIVE backends (sqlite,
postgres, mysql joined oracle/db2 which already had it), so identical
timestamps can no longer duplicate or drop rows across offset pages.

Layered evidence:
- LIVE behavior on the one backend a test can drive end-to-end — SQLite:
  250 rows written through the normal ``insert_memory`` API with an
  IDENTICAL explicit ``created`` timestamp, paged with limit 33 — no
  duplicate, no gap, every page's ``COUNT(*)`` total intact.
- Emitted-SQL goldens for ALL FIVE backends off recording fakes (no live
  MySQL/Postgres/Oracle/Db2 servers): the exact collapsed SQL each backend
  emits, pinning (a) the tiebreaker, (b) the untouched WHERE/paging shape,
  (c) an ORDER BY-free COUNT query.

UNPROVEN (no servers): real MySQL/Postgres execution semantics, and ALL of
Oracle/Db2 including parse+execution — the emitted SQL is asserted as a
golden, not executed; that stays unproven until a live backend exists.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.persistence import db2 as db2_mod
from mnemos.persistence import mysql as mysql_mod
from mnemos.persistence import oracle as oracle_mod
from mnemos.persistence import postgres as postgres_mod
from mnemos.persistence import sqlite as sqlite_mod
from mnemos.persistence.db2 import Db2MemoryRepository, _Db2AsyncCursor
from mnemos.persistence.mysql import MysqlMemoryRepository
from mnemos.persistence.oracle import OracleMemoryRepository
from mnemos.persistence.postgres import PostgresMemoryRepository
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.types import MEMORY_COLS
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

_IDENTICAL_TS = "2026-11-03 10:00:00"

# The 5-backend scalar: ties after ``created DESC`` break on ``id ASC``.
_PG_LIKE_ORDER = "ORDER BY created DESC, id ASC"
_M_AND_ORACLE_ORDER = "ORDER BY m.created DESC, m.id ASC"


def _bypass() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )


async def _insert_memory(backend: SqliteBackend, memory_id: str) -> None:
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=f"fb137 {memory_id}",
            category="projects",
            subcategory=None,
            metadata_json="{}",
            quality_rating=75,
            owner_id="root",
            namespace="default",
            permission_mode=700,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=f"fb137 {memory_id}",
            embedding=None,
            created=_IDENTICAL_TS,
            updated=_IDENTICAL_TS,
        )


def _collapsed(sql: str) -> str:
    return " ".join(sql.split())


def _sqlite_select_list() -> str:
    out: list[str] = []
    for raw in MEMORY_COLS.split(","):
        col = raw.strip()
        if col == "compressed_content":
            out.append("NULL AS compressed_content")
        elif col in {"created", "updated"}:
            out.append(f"replace(datetime({col}), ' ', 'T') AS {col}")
        else:
            out.append(col)
    return ", ".join(out)


def _pg_select_list() -> str:
    return MEMORY_COLS


def _mysql_select_list() -> str:
    return ("m.id, m.content, m.category, m.subcategory, m.metadata, "
            "m.quality_rating, m.compressed_content, m.verbatim_content, "
            "m.owner_id, m.namespace, m.permission_mode, m.source_model, "
            "m.source_provider, m.source_session, m.source_agent, "
            "m.group_id, m.created, m.updated, m.archived_at, "
            "m.recall_count, m.last_recalled_at, m.consolidated_into")


def _oracle_select_list() -> str:
    return ("m.id, m.content, m.category, m.subcategory, m.metadata, "
            "m.quality_rating, m.compressed_content, m.verbatim_content, "
            "m.owner_id, m.namespace, m.permission_mode, m.source_model, "
            "m.source_provider, m.source_session, m.source_agent, "
            "m.group_id, m.created, m.updated, m.archived_at, "
            "m.consolidated_into, m.deleted_at")


# ────────────────────────────── LIVE sqlite: the paging scenario itself ─────


@pytest.mark.asyncio
async def test_sqlite_250_identical_creates_page_without_duplicate_or_gap(
    tmp_path: Any,
) -> None:
    """Insert 250 rows with the SAME created timestamp; page with limit=33.

    The offset scan must return every id exactly once (no duplicate) and
    cover all inserted ids (no gap), with every page reporting the full
    COUNT(*) total. WITHOUT a unique tiebreaker, tie rows could repeat or
    vanish between pages under equal timestamps — the audit M10 scenario.
    """
    backend = SqliteBackend(tmp_path / "fb137-paging.db", SimpleNamespace())
    await backend.open()
    try:
        inserted = [f"fb137-{i:04d}-{uuid.uuid4().hex[:8]}" for i in range(250)]
        for memory_id in inserted:
            await _insert_memory(backend, memory_id)

        seen: list[str] = []
        offset = 0
        limit = 33
        while True:
            async with backend.transactional() as tx:
                rows, total = await backend.memories.list_memories(
                    tx, visibility=_bypass(), limit=limit, offset=offset
                )
            assert total == 250, "COUNT(*) total must survive the tiebreaker"
            seen.extend(row["id"] for row in rows)
            offset += limit
            if not rows:
                break
        assert len(seen) == len(set(seen)), "duplicate row across offset pages"
        assert set(seen) == set(inserted), "gap: missing rows across offset pages"
        expected = sorted(inserted)  # identical created -> pure id ASC ties
        assert seen == expected, "tie rows must page in stable id ASC order"
    finally:
        await backend.close()


# ────────────────────────────── sqlite emitted-SQL golden (live, recorded) ──


@pytest.mark.asyncio
async def test_sqlite_emitted_sql_golden(tmp_path: Any, monkeypatch: Any) -> None:
    backend = SqliteBackend(tmp_path / "fb137-sql.db", SimpleNamespace())
    await backend.open()
    seen_sql: list[str] = []
    original_all = sqlite_mod._fetch_all
    original_val = sqlite_mod._fetch_val

    async def _recording_all(conn: Any, sql: str, params: Any = ()) -> list[Any]:
        seen_sql.append(_collapsed(sql))
        return await original_all(conn, sql, params)

    async def _recording_val(conn: Any, sql: str, params: Any = ()) -> Any:
        seen_sql.append(_collapsed(sql))
        return await original_val(conn, sql, params)

    monkeypatch.setattr(sqlite_mod, "_fetch_all", _recording_all)
    monkeypatch.setattr(sqlite_mod, "_fetch_val", _recording_val)
    try:
        await _insert_memory(backend, "fb137-sql")
        async with backend.transactional() as tx:
            rows, total = await backend.memories.list_memories(
                tx, visibility=_bypass(), limit=25, offset=0
            )
    finally:
        await backend.close()

    assert total == 1 and len(rows) == 1
    base = "FROM memories WHERE deleted_at IS NULL AND archived_at IS NULL"
    assert seen_sql == [
        f"SELECT COUNT(*) {base}",
        f"SELECT {_sqlite_select_list()} {base} "
        f"{_PG_LIKE_ORDER} LIMIT ? OFFSET ?",
    ]


# ────────────────────────────── postgres: recording fake, no server ─────────


class _AsyncpgFetchSpy:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self._calls.append((_collapsed(sql), args))
        return []

    async def fetchval(self, sql: str, *args: Any) -> int:
        self._calls.append((_collapsed(sql), args))
        return 5


@pytest.mark.asyncio
async def test_postgres_emitted_sql_golden(monkeypatch: Any) -> None:
    calls: list[tuple[str, Any]] = []

    def _fake_tx(tx: Any) -> Any:
        return SimpleNamespace(conn=_AsyncpgFetchSpy(calls))

    monkeypatch.setattr(postgres_mod, "_postgres_tx", _fake_tx)
    repo = PostgresMemoryRepository()
    rows, total = await repo.list_memories(
        None, visibility=_bypass(), limit=20, offset=40
    )
    assert (rows, total) == ([], 5)
    base = "FROM memories WHERE deleted_at IS NULL AND archived_at IS NULL"
    assert [sql for sql, _ in calls] == [
        f"SELECT {_pg_select_list()} {base} {_PG_LIKE_ORDER} LIMIT $1 OFFSET $2",
        f"SELECT COUNT(*) {base}",
    ]


# ────────────────────────────── mysql: recording fake, no server ────────────


class _MysqlCursorSpy:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls
        self.description = (("X",),)

    async def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append((_collapsed(sql), params))

    async def fetchone(self) -> tuple[int]:
        return (5,)

    async def fetchall(self) -> list[Any]:
        return []


class _MysqlConnSpy:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._cursor = _MysqlCursorSpy(calls)

    def cursor(self) -> Any:
        cursor = self._cursor

        class _CM:
            async def __aenter__(self) -> _MysqlCursorSpy:
                return cursor

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _CM()


@pytest.mark.asyncio
async def test_mysql_emitted_sql_golden() -> None:
    calls: list[tuple[str, Any]] = []
    fake_tx = SimpleNamespace(conn=_MysqlConnSpy(calls))
    repo = MysqlMemoryRepository()
    rows, total = await repo.list_memories(
        fake_tx, visibility=_bypass(), limit=20, offset=40
    )
    assert (rows, total) == ([], 5)
    base = "FROM memories m WHERE m.deleted_at IS NULL AND m.archived_at IS NULL"
    assert [sql for sql, _ in calls] == [
        f"SELECT COUNT(*) AS cnt {base}",
        f"SELECT {_mysql_select_list()} {base} "
        f"{_M_AND_ORACLE_ORDER} LIMIT %s OFFSET %s",
    ]


# ────────────────────────────── oracle: already tiebroken — pin, no server ──


class _OracleCursorSpy:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls
        self.description = (("X",),)

    async def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append((_collapsed(sql), params))

    async def fetchone(self) -> tuple[int]:
        return (5,)

    async def fetchall(self) -> list[Any]:
        return []

    async def close(self) -> None:
        return None


class _OracleConnSpy:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._cursor = _OracleCursorSpy(calls)

    def cursor(self) -> _OracleCursorSpy:
        return self._cursor


@pytest.mark.asyncio
async def test_oracle_emitted_sql_golden() -> None:
    calls: list[tuple[str, Any]] = []
    fake_tx = SimpleNamespace(conn=_OracleConnSpy(calls))
    repo = OracleMemoryRepository()
    rows, total = await repo.list_memories(
        fake_tx, visibility=_bypass(), limit=20, offset=40
    )
    assert (rows, total) == ([], 5)
    base = "FROM memories m WHERE m.deleted_at IS NULL AND m.archived_at IS NULL"
    assert [sql for sql, _ in calls] == [
        f"SELECT COUNT(*) {base}",
        f"SELECT {_oracle_select_list()} {base} "
        f"{_M_AND_ORACLE_ORDER} OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",
    ]


# ────────────────────────────── db2: already tiebroken — pin, no server ─────


class _Db2SyncCursorSpy:
    """Sync cursor driven by _Db2AsyncCursor through asyncio.to_thread."""

    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls
        self.description = (("X",),)
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append((_collapsed(sql), params))

    def fetchone(self) -> tuple[int]:
        return (5,)

    def fetchall(self) -> list[Any]:
        return []

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_db2_emitted_sql_golden() -> None:
    calls: list[tuple[str, Any]] = []
    fake_conn = SimpleNamespace(cursor=lambda: _Db2AsyncCursor(_Db2SyncCursorSpy(calls)))
    fake_tx = SimpleNamespace(conn=fake_conn)
    repo = Db2MemoryRepository()
    rows, total = await repo.list_memories(
        fake_tx, visibility=_bypass(), limit=20, offset=40
    )
    assert (rows, total) == ([], 5)
    base = "FROM memories m WHERE m.deleted_at IS NULL AND m.archived_at IS NULL"
    assert [sql for sql, _ in calls] == [
        f"SELECT COUNT(*) {base} WITH UR",
        f"SELECT {_oracle_select_list()} {base} "
        f"{_M_AND_ORACLE_ORDER} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY WITH UR",
    ]


# ────────────────────────────── the scalar: all five keep the tiebreaker ────


@pytest.mark.asyncio
async def test_all_five_backends_share_the_id_tiebreaker() -> None:
    """Source-level scalar: no backend may drop the unique tie again.

    Oracle and db2 shipped ``m.id ASC`` before the audit; sqlite, postgres
    and mysql gained it in FB137. This is the cheap five-way guard against
    a silent rollback anywhere.
    """
    import inspect

    from mnemos.persistence.types import Row  # noqa: F401  (Row contract below)

    src = " ".join(inspect.getsource(sqlite_mod.SqliteMemoryRepository.list_memories).split())
    assert _PG_LIKE_ORDER in src
    src = " ".join(inspect.getsource(postgres_mod.PostgresMemoryRepository.list_memories).split())
    assert _PG_LIKE_ORDER in src
    src = " ".join(inspect.getsource(mysql_mod.MysqlMemoryRepository.list_memories).split())
    assert _M_AND_ORACLE_ORDER in src
    src = " ".join(inspect.getsource(oracle_mod.OracleMemoryRepository.list_memories).split())
    assert _M_AND_ORACLE_ORDER in src
    src = " ".join(inspect.getsource(db2_mod.Db2MemoryRepository.list_memories).split())
    assert _M_AND_ORACLE_ORDER in src
