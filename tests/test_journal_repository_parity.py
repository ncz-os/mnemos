"""JournalRepository driver-free parity tests — assert each backend emits
the right dialect tokens for the 3 JournalRepository methods.

Uses mock-cursor (asyncpg.Connection / oracledb.AsyncCursor / aiosqlite) to
verify SQL shape without a running DB.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_asyncpg_conn(
    fetchrow_result: Any = None, fetch_result: list | None = None, execute_result: str = "UPDATE 1"
) -> AsyncMock:
    """Build a mock asyncpg.Connection that captures the last query args."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])
    conn.execute = AsyncMock(return_value=execute_result)
    return conn


def _make_ora_cursor(
    fetchone_result: Any = None, fetchall_result: list | None = None, rowcount: int = 1, description: list | None = None
) -> MagicMock:
    """Build a mock oracledb cursor that captures the last query args."""
    cur = MagicMock()
    cur.fetchone = AsyncMock(return_value=fetchone_result)
    cur.fetchall = AsyncMock(return_value=fetchall_result or [])
    cur.rowcount = rowcount
    cur.description = description or [
        ("ID",),
        ("ENTRY_DATE",),
        ("TOPIC",),
        ("CONTENT",),
        ("METADATA",),
        ("CREATED",),
    ]
    cur.execute = AsyncMock()
    cur.close = AsyncMock()
    return cur


def _make_sqlite_conn(fetchone_result: Any = None, fetchall_result: list | None = None, rowcount: int = 1) -> MagicMock:
    """Build a mock oracledb connection that wraps a mock cursor."""
    cur = _make_ora_cursor(
        fetchone_result=fetchone_result,
        fetchall_result=fetchall_result,
        rowcount=rowcount,
        description=[
            ("id",),
            ("entry_date",),
            ("topic",),
            ("content",),
            ("metadata",),
            ("created",),
        ],
    )
    cur.cursor = MagicMock(return_value=cur)  # for oracledb pattern: conn.cursor()
    return cur


# ── Postgres tests ───────────────────────────────────────────────────────────


class TestPostgresJournalRepository:
    """Postgres dialect token tests — mock asyncpg.Connection."""

    @pytest.fixture
    def sut(self):
        from mnemos.persistence.postgres import PostgresJournalRepository

        return PostgresJournalRepository()

    @pytest.fixture
    def tx(self):
        from mnemos.persistence.postgres import PostgresTransaction

        conn = _make_asyncpg_conn(
            fetchrow_result={
                "id": "entry-1",
                "entry_date": "2025-12-01",
                "topic": "test",
                "content": "hello",
                "metadata": '{"k":"v"}',
                "created": "2025-12-01 12:00:00",
            },
        )
        raw_tx = AsyncMock()
        return PostgresTransaction(conn, raw_tx)

    async def test_create_journal_entry_with_explicit_date(self, sut, tx):
        result = await sut.create_journal_entry(
            tx,
            entry_id="e1",
            owner_id="alice",
            namespace="ns",
            entry_date=date(2025, 12, 1),
            topic="test",
            content="hello",
            metadata={"k": "v"},
        )
        assert result["id"] == "entry-1"
        assert "entry_date" in result
        # PG uses $N binds
        call_args = tx.conn.fetchrow.call_args
        sql = str(call_args[0][0])
        assert "$1" in sql
        assert "RETURNING" in sql

    async def test_create_journal_entry_without_date(self, sut, tx):
        result = await sut.create_journal_entry(
            tx,
            entry_id="e2",
            owner_id="alice",
            namespace="ns",
            entry_date=None,
            topic="test",
            content="hello",
            metadata=None,
        )
        assert result["id"] == "entry-1"
        sql = str(tx.conn.fetchrow.call_args[0][0])
        assert "CURRENT_DATE" in sql

    async def test_list_journal_entries_by_date(self, sut, tx):
        await sut.list_journal_entries(
            tx,
            owner_id="alice",
            namespace="ns",
            entry_date=date(2025, 12, 1),
        )
        sql = str(tx.conn.fetch.call_args[0][0])
        assert "entry_date = $3" in sql
        assert "deleted_at IS NULL" in sql

    async def test_list_journal_entries_by_topic(self, sut, tx):
        await sut.list_journal_entries(
            tx,
            owner_id="alice",
            namespace="ns",
            topic="debug",
        )
        sql = str(tx.conn.fetch.call_args[0][0])
        assert "topic = $3" in sql

    async def test_list_journal_entries_by_search(self, sut, tx):
        await sut.list_journal_entries(
            tx,
            owner_id="alice",
            namespace="ns",
            search="findme",
        )
        sql = str(tx.conn.fetch.call_args[0][0])
        assert "ILIKE" in sql

    async def test_list_journal_entries_no_filter(self, sut, tx):
        await sut.list_journal_entries(
            tx,
            owner_id="alice",
            namespace="ns",
        )
        sql = str(tx.conn.fetch.call_args[0][0])
        assert "deleted_at IS NULL" in sql

    async def test_delete_journal_entry_soft(self, sut, tx):
        result = await sut.delete_journal_entry(
            tx,
            entry_id="e1",
            owner_id="alice",
            namespace="ns",
        )
        assert result is True
        sql = str(tx.conn.execute.call_args[0][0])
        assert "UPDATE journal SET deleted_at" in sql
        assert "deleted_at IS NULL" in sql

    async def test_delete_journal_entry_not_found(self, sut, tx):
        tx.conn.execute = AsyncMock(return_value="UPDATE 0")
        result = await sut.delete_journal_entry(
            tx,
            entry_id="missing",
            owner_id="alice",
            namespace="ns",
        )
        assert result is False


# ── Oracle tests ─────────────────────────────────────────────────────────────


class TestOracleJournalRepository:
    """Oracle dialect token tests — mock oracledb cursor."""

    @pytest.fixture
    def sut(self):
        from mnemos.persistence.oracle import OracleJournalRepository

        return OracleJournalRepository()

    @pytest.fixture
    def tx(self):
        cur = _make_ora_cursor(
            fetchone_result=("e1", "2025-12-01", "test", "hello", '{"k":"v"}', "2025-12-01 12:00:00"),
        )
        from mnemos.persistence.oracle import _OracleTransaction

        conn = _make_sqlite_conn(
            fetchone_result=("e1", "2025-12-01", "test", "hello", '{"k":"v"}', "2025-12-01 12:00:00"),
        )
        # Override the cursor to be our tracked mock
        conn.cursor = MagicMock(return_value=cur)
        return _OracleTransaction(conn)

    async def test_create_journal_entry_with_date(self, sut, tx):
        result = await sut.create_journal_entry(
            tx,
            entry_id="e1",
            owner_id="alice",
            namespace="ns",
            entry_date="2025-12-01",
            topic="test",
            content="hello",
            metadata={"k": "v"},
        )
        assert result is not None

    async def test_create_journal_entry_without_date(self, sut, tx):
        result = await sut.create_journal_entry(
            tx,
            entry_id="e2",
            owner_id="alice",
            namespace="ns",
            entry_date=None,
            topic="test",
            content="hello",
            metadata=None,
        )
        assert result is not None

    async def test_list_journal_entries_by_date_oracle(self, sut, tx):
        result = await sut.list_journal_entries(
            tx,
            owner_id="alice",
            namespace="ns",
            entry_date="2025-12-01",
        )
        assert isinstance(result, list)

    async def test_list_journal_entries_by_search_oracle(self, sut, tx):
        result = await sut.list_journal_entries(
            tx,
            owner_id="alice",
            namespace="ns",
            search="findme",
        )
        assert isinstance(result, list)

    async def test_delete_journal_entry_oracle(self, sut, tx):
        result = await sut.delete_journal_entry(
            tx,
            entry_id="e1",
            owner_id="alice",
            namespace="ns",
        )
        assert result is True


# ── SQLite tests ─────────────────────────────────────────────────────────────


class TestSqliteJournalRepository:
    """SQLite dialect token tests — mock aiosqlite.Connection."""

    @pytest.fixture
    def sut(self):
        from mnemos.persistence.sqlite import SqliteJournalRepository

        return SqliteJournalRepository()

    @pytest.fixture
    def tx(self):
        from mnemos.persistence.sqlite import SqliteTransaction

        conn = AsyncMock()
        conn.execute = AsyncMock()  # for BEGIN/COMMIT/ROLLBACK
        tx = SqliteTransaction(conn)
        return tx

    async def test_create_journal_entry_with_date_sqlite(self, sut, tx):
        with patch("mnemos.persistence.sqlite._fetch_one") as fetch_one:
            fetch_one.return_value = {
                "id": "e1",
                "entry_date": "2025-12-01",
                "topic": "test",
                "content": "hello",
                "metadata": '{"k":"v"}',
                "created": "2025-12-01 12:00:00",
            }
            result = await sut.create_journal_entry(
                tx,
                entry_id="e1",
                owner_id="alice",
                namespace="ns",
                entry_date=date(2025, 12, 1),
                topic="test",
                content="hello",
                metadata={"k": "v"},
            )
            assert result["id"] == "e1"
            sql = str(fetch_one.call_args[0][1])
            assert "?" in sql
            assert "RETURNING" in sql

    async def test_create_journal_entry_without_date_sqlite(self, sut, tx):
        with patch("mnemos.persistence.sqlite._fetch_one") as fetch_one:
            fetch_one.return_value = {
                "id": "e1",
                "entry_date": "2025-12-01",
                "topic": "test",
                "content": "hello",
                "metadata": "{}",
                "created": "2025-12-01 12:00:00",
            }
            result = await sut.create_journal_entry(
                tx,
                entry_id="e1",
                owner_id="alice",
                namespace="ns",
                entry_date=None,
                topic="test",
                content="hello",
                metadata=None,
            )
            assert result["id"] == "e1"
            sql = str(fetch_one.call_args[0][1])
            assert "date('now')" in sql

    async def test_list_journal_entries_by_date_sqlite(self, sut, tx):
        with patch("mnemos.persistence.sqlite._fetch_all") as fetch_all:
            fetch_all.return_value = []
            await sut.list_journal_entries(
                tx,
                owner_id="alice",
                namespace="ns",
                entry_date=date(2025, 12, 1),
            )
            sql = str(fetch_all.call_args[0][1])
            assert "entry_date = ?" in sql

    async def test_list_journal_entries_by_search_sqlite(self, sut, tx):
        with patch("mnemos.persistence.sqlite._fetch_all") as fetch_all:
            fetch_all.return_value = []
            await sut.list_journal_entries(
                tx,
                owner_id="alice",
                namespace="ns",
                search="findme",
            )
            sql = str(fetch_all.call_args[0][1])
            assert "LIKE ?" in sql

    async def test_delete_journal_entry_sqlite(self, sut, tx):
        with patch("mnemos.persistence.sqlite._execute_count") as exec_count:
            exec_count.return_value = 1
            result = await sut.delete_journal_entry(
                tx,
                entry_id="e1",
                owner_id="alice",
                namespace="ns",
            )
            assert result is True
            sql = str(exec_count.call_args[0][1])
            assert "UPDATE journal SET deleted_at" in sql
            assert "datetime('now')" in sql


# ── Db2 tests ────────────────────────────────────────────────────────────────


class TestDb2JournalRepository:
    """Db2 — inherits OracleJournalRepository, cursor layer handles translation."""

    @pytest.fixture
    def sut(self):
        from mnemos.persistence.db2 import Db2JournalRepository

        return Db2JournalRepository()

    async def test_db2_journal_repo_is_oracle_subclass(self, sut):
        from mnemos.persistence.oracle import OracleJournalRepository

        assert isinstance(sut, OracleJournalRepository)

    async def test_db2_journal_repo_has_mixin(self, sut):
        from mnemos.persistence.db2 import _Db2OraCompatMixin

        assert isinstance(sut, _Db2OraCompatMixin)
