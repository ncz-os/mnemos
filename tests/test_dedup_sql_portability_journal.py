"""Dedup SQL portability and federation-journal shape regressions.

FB102 (audit findings S2, S3, S6, M1, M2). The Oracle / Db2 / MySQL assertions
pin the statement each backend sends, because no such server is reachable from
this tree; the journal join is also proven against a real SQLite database.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
DB2_MIGRATIONS = REPO_ROOT / "mnemos" / "db_migrations" / "migrations_db2"

_COLUMNS = (
    "owner_id",
    "namespace",
    "content_hash",
    "duplicate_count",
    "memory_ids",
    "keep_id",
)
_CANONICAL = ("canonical_id",)


class _Cursor:
    """oracledb / ibm_db cursor that records every statement it is handed."""

    def __init__(self, calls: list[Any], rows: list[dict[str, Any]] | None = None) -> None:
        self._calls = calls
        self._rows = rows or []
        self.description = [(name,) for name in _COLUMNS + _CANONICAL]
        self.rowcount = 0

    async def execute(self, sql: Any, params: Any = ()) -> "_Cursor":
        self._calls.append((" ".join(str(sql).split()), params))
        return self

    async def fetchall(self) -> list[Any]:
        return [tuple(row.get(name) for name in _COLUMNS + _CANONICAL) for row in self._rows]

    async def fetchone(self) -> Any:
        return None

    async def close(self) -> None:
        return None


class _Conn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[Any] = []
        self._rows = rows

    def cursor(self) -> _Cursor:
        return _Cursor(self.calls, self._rows)


def _sql_of(calls: list[Any]) -> list[str]:
    return [sql for sql, _ in calls]


def _group_by_clause(sql: str) -> str:
    upper = " ".join(sql.upper().split())
    start = upper.index("GROUP BY")
    rest = upper[start + len("GROUP BY") :]
    for terminator in ("HAVING", "ORDER BY"):
        if terminator in rest:
            rest = rest[: rest.index(terminator)]
    return rest


# ---------------------------------------------------------------------------
# S2 · a dedup group key must never be a CLOB expression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_dedup_groups_by_a_digest_not_a_clob():
    from mnemos.persistence.oracle import OracleMemoryRepository

    conn = _Conn()
    await OracleMemoryRepository().find_duplicate_content_groups(SimpleNamespace(conn=conn))

    (sql,) = _sql_of(conn.calls)
    group_by = _group_by_clause(sql)
    assert "STANDARD_HASH" in group_by and "'SHA256'" in group_by
    # The CLOB itself must not be a grouping term: that is ORA-00932. Only the
    # digest of the normalized body may appear, never the REPLACE over content.
    assert "REPLACE(" not in group_by
    assert "NVL(" not in group_by


@pytest.mark.asyncio
async def test_oracle_dedup_aggregates_ids_without_the_4000_byte_listagg_cap():
    from mnemos.persistence.oracle import OracleMemoryRepository

    conn = _Conn()
    await OracleMemoryRepository().find_duplicate_content_groups(SimpleNamespace(conn=conn))

    (sql,) = _sql_of(conn.calls)
    upper = " ".join(sql.upper().split())
    # LISTAGG over VARCHAR2 ids dies with ORA-01489 at 4000 bytes; the CLOB
    # form has no such cap and no silent truncation.
    assert "LISTAGG" not in upper
    assert "XMLAGG" in upper
    assert "GETCLOBVAL" in upper


@pytest.mark.asyncio
async def test_db2_dedup_groups_by_a_digest_not_a_clob():
    from mnemos.persistence.db2 import Db2MemoryRepository

    conn = _Conn()
    await Db2MemoryRepository().find_duplicate_content_groups(SimpleNamespace(conn=conn))

    (sql,) = _sql_of(conn.calls)
    group_by = _group_by_clause(sql)
    assert "HASH_SHA256" in group_by
    # SQL0134N: a CLOB cannot be a grouping term -- only its digest may be.
    assert "REPLACE(" not in group_by
    assert "COALESCE(" not in group_by


# ---------------------------------------------------------------------------
# S6 · the keeper is the rated duplicate, not a NULL-rated one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db2_dedup_keeper_order_puts_unrated_rows_last():
    from mnemos.persistence.db2 import Db2MemoryRepository

    conn = _Conn()
    await Db2MemoryRepository().find_duplicate_content_groups(SimpleNamespace(conn=conn))

    (sql,) = _sql_of(conn.calls)
    upper = " ".join(sql.upper().split())
    assert "QUALITY_RATING DESC NULLS LAST" in upper


# ---------------------------------------------------------------------------
# S3 · MySQL's GROUP_CONCAT default truncates the id list
# ---------------------------------------------------------------------------


class _MysqlCursor:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def __aenter__(self) -> "_MysqlCursor":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    async def execute(self, sql: Any, params: Any = None) -> None:
        self._calls.append(" ".join(str(sql).split()))

    async def fetchall(self) -> list[Any]:
        return []

    async def fetchone(self) -> Any:
        return None


class _MysqlConn:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def cursor(self) -> _MysqlCursor:
        return _MysqlCursor(self.calls)


@pytest.mark.asyncio
async def test_mysql_dedup_raises_group_concat_limit_before_selecting():
    from mnemos.persistence.mysql import MysqlMemoryRepository

    conn = _MysqlConn()
    await MysqlMemoryRepository().find_duplicate_content_groups(SimpleNamespace(conn=conn))

    assert len(conn.calls) >= 2
    set_stmt = conn.calls[0].upper()
    assert set_stmt.startswith("SET SESSION GROUP_CONCAT_MAX_LEN")
    limit = int(set_stmt.rsplit("=", 1)[1].strip().rstrip(";"))
    # 1024 is the server default and is what silently truncated the id list.
    assert limit > 1024
    assert "GROUP_CONCAT" in conn.calls[1].upper()


# ---------------------------------------------------------------------------
# M1 · a Db2 journal timestamp is UTC, like every other port
# ---------------------------------------------------------------------------

_DB2_JOURNAL_MIGRATIONS = (
    "0062_federation_journal.sql",
    "0064_federation_journal_dialects.sql",
    "0068_federation_journal_narrow_trigger.sql",
)


@pytest.mark.parametrize("name", _DB2_JOURNAL_MIGRATIONS)
def test_db2_journal_writes_changed_at_in_utc(name):
    body = (DB2_MIGRATIONS / name).read_text()
    assert "federation_changes(" in body
    writes = [
        line for line in body.splitlines() if "INSERT INTO federation_changes(" in line
    ]
    assert writes, f"{name} no longer writes the journal"
    for line in writes:
        marker = line[line.index("SELECT ") + len("SELECT ") :]
        # changed_at is the second selected value and must be UTC. Db2's
        # CURRENT_TIMESTAMP is session-local; every other backend stores UTC.
        assert "CURRENT_TIMESTAMP - CURRENT_TIMEZONE" in line, line
        assert marker and "CURRENT_TIMESTAMP," not in marker.replace(
            "CURRENT_TIMESTAMP - CURRENT_TIMEZONE", ""
        )


def _balanced_expression(text: str) -> None:
    """Fail on an unbalanced parenthesis -- the shape of a mangled function."""
    assert text.count("(") == text.count(")"), text


@pytest.mark.asyncio
async def test_db2_dedup_statement_is_syntactically_closed():
    from mnemos.persistence.db2 import Db2MemoryRepository

    conn = _Conn()
    await Db2MemoryRepository().find_duplicate_content_groups(SimpleNamespace(conn=conn))

    (sql,) = _sql_of(conn.calls)
    _balanced_expression(sql)
    # The digest must wrap the normalized body whole: HASH_SHA256( REPLACE(...) )
    # closes COALESCE's argument list, both REPLACEs, HASH_SHA256, HEX and LOWER.
    assert "LOWER(HEX(HASH_SHA256(REPLACE(REPLACE(COALESCE(content, '')," in sql
    assert "CHR(10))))) ORDER BY created DESC" in sql


@pytest.mark.asyncio
async def test_oracle_dedup_statement_is_syntactically_closed():
    from mnemos.persistence.oracle import OracleMemoryRepository

    conn = _Conn()
    await OracleMemoryRepository().find_duplicate_content_groups(SimpleNamespace(conn=conn))

    (sql,) = _sql_of(conn.calls)
    _balanced_expression(sql)


# ---------------------------------------------------------------------------
# M2 · the current-state join is scoped to the memory
# ---------------------------------------------------------------------------


class _RecordingOps:
    """Journalling _Ops wrapper that keeps the rows the query returned."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.rows: list[Any] = []
        self.sqls: list[str] = []

    async def fetchall(self, sql: str, *params: Any) -> list[Any]:
        self.sqls.append(" ".join(str(sql).split()))
        rows = await self._inner.fetchall(sql, *params)
        self.rows = list(rows)
        return rows

    async def scalar(self, sql: str, *params: Any) -> Any:
        return await self._inner.scalar(sql, *params)

    async def fetchone(self, sql: str, *params: Any) -> Any:
        return await self._inner.fetchone(sql, *params)

    async def execute(self, sql: str, *params: Any) -> Any:
        return await self._inner.execute(sql, *params)


class _SqlRecorder:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def fetchall(self, sql: str, *params: Any) -> list[Any]:
        self.sqls.append(" ".join(str(sql).split()))
        return []


@pytest.mark.asyncio
async def test_current_states_join_is_scoped_by_memory_id():
    from mnemos.persistence import federation_journal as journal

    ops = _SqlRecorder()
    await journal._current_states(ops, ["mem-1"])
    assert "e.memory_id = latest.memory_id" in ops.sqls[0]


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    from mnemos.core import config as core_config

    core_config.reload_settings()
    from mnemos.persistence.sqlite import SqliteBackend

    instance = SqliteBackend(
        tmp_path / "journal-seq.db",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=3)),
    )
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_current_states_returns_one_row_per_memory_with_duplicate_sequences(
    sqlite_backend,
):
    from mnemos.persistence import federation_journal as journal
    from mnemos.persistence.sqlite import _execute

    async with sqlite_backend.transactional() as tx:
        for memory_id, body in (("dup-a", "body a"), ("dup-b", "body b")):
            await _execute(
                tx.conn,
                "INSERT INTO memories (id, content, owner_id, namespace) VALUES (?, ?, ?, ?)",
                (memory_id, body, "user-1", "default"),
            )
        # seq is assigned from one clock, so two rows sharing a value means the
        # journal diverged. The join must still never mix two memories.
        await _execute(
            tx.conn,
            "UPDATE federation_changes SET seq = 7 WHERE memory_id IN (?, ?)",
            ("dup-a", "dup-b"),
        )
        ops = _RecordingOps(journal._ops(tx))
        current = await journal._current_states(ops, ["dup-a", "dup-b"])

    joined = [row["journal_memory_id"] for row in ops.rows]
    assert sorted(joined) == ["dup-a", "dup-b"]
    assert set(current) == {"dup-a", "dup-b"}
    assert current["dup-a"]["content"] == "body a"
    assert current["dup-b"]["content"] == "body b"
