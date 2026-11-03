"""Unit tests for the MySQL-family replica-health provisioning helper (M12).

``_ensure_mysql_federation_replica_health`` replays an ALTER-only migration
file from ``migrations_mysql/`` on every ``MysqlBackend.open``. Two things
must hold without a live server:

* the file the helper names actually exists (the first run filed the SQL one
  directory up, where nothing reads it), and
* it carries every column the M12 status route reads, so an upgraded
  database does not fail its first ``record_sync_lag`` UPDATE.

The fake connection below is deliberately minimal: it records statements and
raises MySQL error 1060 on demand, which is the code the helper must swallow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemos.persistence import mysql as mysql_mod

M12_COLUMNS = (
    "last_cursor_ts",
    "lag_seconds",
    "rows_last_sync",
    "consecutive_failures",
    "peer_embed_model",
    "peer_embed_dim",
    "embedding_status",
)


class _FakeCursor:
    def __init__(self, *, error: tuple[int, str] | None = None) -> None:
        self.executed: list[str] = []
        self._error = error

    async def execute(self, statement: str) -> None:
        self.executed.append(statement)
        if self._error is not None:
            raise RuntimeError(*self._error)

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_helper_reads_a_file_that_exists() -> None:
    # The helper must name a file inside migrations_mysql/; resolving the path
    # the same way it does is the only way to see a bad filename.
    directory = Path(mysql_mod.__file__).resolve().parents[1] / "db_migrations" / "migrations_mysql"
    candidates = [p.name for p in directory.glob("*replica_health*.sql")]
    assert candidates, f"no replica-health migration in {directory}"
    assert "0071_federation_replica_health.sql" in candidates


def test_migration_covers_every_m12_column() -> None:
    directory = Path(mysql_mod.__file__).resolve().parents[1] / "db_migrations" / "migrations_mysql"
    sql = (directory / "0071_federation_replica_health.sql").read_text(encoding="utf-8")
    for column in M12_COLUMNS:
        assert f"ADD COLUMN {column} " in sql, f"{column} missing from the MySQL migration"


@pytest.mark.asyncio
async def test_helper_replays_every_column_statement() -> None:
    cursor = _FakeCursor()
    await mysql_mod._ensure_mysql_federation_replica_health(_FakeConn(cursor))

    assert len(cursor.executed) == len(M12_COLUMNS)
    for column, statement in zip(M12_COLUMNS, cursor.executed):
        assert "federation_peers" in statement
        assert f"ADD COLUMN {column} " in statement


@pytest.mark.asyncio
async def test_helper_swallows_duplicate_column_and_keeps_going() -> None:
    # Every statement is a replay on an already-migrated database.
    cursor = _FakeCursor(error=(1060, "Duplicate column name"))
    await mysql_mod._ensure_mysql_federation_replica_health(_FakeConn(cursor))

    assert len(cursor.executed) == len(M12_COLUMNS)


@pytest.mark.asyncio
async def test_helper_propagates_other_errors() -> None:
    cursor = _FakeCursor(error=(1146, "Table 'federation_peers' doesn't exist"))
    with pytest.raises(RuntimeError) as excinfo:
        await mysql_mod._ensure_mysql_federation_replica_health(_FakeConn(cursor))
    assert excinfo.value.args[0] == 1146


@pytest.mark.asyncio
async def test_oauth_provisioning_strips_file_header_comments() -> None:
    """Every executed statement must start with SQL, not with a ``--`` header.

    aiomysql rejects multi-statement text, so a header comment glued to the
    first statement makes it a silent no-op — which is how ``CREATE TABLE
    users`` in 0052 never ran on an upgraded MySQL database.
    """
    cursor = _FakeCursor()
    await mysql_mod._ensure_mysql_oauth_schema(_FakeConn(cursor))

    assert cursor.executed, "the helper executed nothing"
    for statement in cursor.executed:
        assert not statement.lstrip().startswith("--"), f"comment still heads a statement: {statement[:80]!r}"
        assert statement.lstrip(), "empty statement"
    # 0052's first CREATE TABLE is the one that used to be swallowed.
    assert any("CREATE TABLE IF NOT EXISTS users" in s for s in cursor.executed)


class _ArglessFakeCursor(_FakeCursor):
    """Raises an exception with no args — what indexing ``args[0]`` chokes on."""

    async def execute(self, statement: str) -> None:
        self.executed.append(statement)
        raise RuntimeError()


@pytest.mark.asyncio
async def test_helper_does_not_crash_on_an_error_without_args() -> None:
    """An argless exception must surface as itself, not an IndexError.

    The errno guard read ``exc.args[0]`` directly. With no args that raised
    ``IndexError`` from inside the ``except`` block, so the real failure was
    replaced by a confusing startup crash instead of being re-raised.
    """
    cursor = _ArglessFakeCursor()
    with pytest.raises(RuntimeError) as excinfo:
        await mysql_mod._ensure_mysql_federation_replica_health(_FakeConn(cursor))
    assert excinfo.value.args == ()
    assert len(cursor.executed) == 1


def test_mysql_errno_reads_codes_and_tolerates_odd_args() -> None:
    from mnemos.persistence.mysql import _mysql_errno

    assert _mysql_errno(RuntimeError(1060, "dup column")) == 1060
    assert _mysql_errno(RuntimeError(1061, "dup index")) == 1061
    assert _mysql_errno(Exception()) is None
    assert _mysql_errno(Exception("boom")) is None
