"""KGRepository driver-free parity tests — assert each backend emits
the right dialect tokens for the 5 new KGRepository methods.

Uses mock-cursor (asyncpg.Connection / oracledb.AsyncCursor / aiosqlite) to
verify SQL shape without a running DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_asyncpg_conn(
    fetchrow_result: Any = None,
    fetch_result: list | None = None,
    fetchval_result: Any = None,
    execute_result: str = "UPDATE 1",
) -> AsyncMock:
    """Build a mock asyncpg.Connection that captures the last query args."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])
    conn.fetchval = AsyncMock(return_value=fetchval_result)
    conn.execute = AsyncMock(return_value=execute_result)
    return conn


def _make_ora_cursor(
    fetchone_result: Any = None,
    fetchall_result: list | None = None,
    rowcount: int = 1,
    description: list | None = None,
) -> MagicMock:
    """Build a mock oracledb cursor that captures the last query args."""
    cur = MagicMock()
    cur.fetchone = AsyncMock(return_value=fetchone_result)
    cur.fetchall = AsyncMock(return_value=fetchall_result or [])
    cur.rowcount = rowcount
    cur.description = description or [
        ("ID",),
        ("SUBJECT",),
        ("PREDICATE",),
        ("OBJECT",),
        ("SUBJECT_TYPE",),
        ("OBJECT_TYPE",),
        ("VALID_FROM",),
        ("VALID_UNTIL",),
        ("MEMORY_ID",),
        ("CONFIDENCE",),
        ("OWNER_ID",),
        ("NAMESPACE",),
        ("METADATA",),
        ("CREATED",),
        ("DELETED_AT",),
    ]
    cur.execute = AsyncMock()
    cur.close = AsyncMock()
    return cur


def _make_ora_conn(fetchone_result: Any = None, fetchall_result: list | None = None, rowcount: int = 1) -> MagicMock:
    """Build a mock oracledb connection wrapping a mock cursor."""
    cur = _make_ora_cursor(fetchone_result=fetchone_result, fetchall_result=fetchall_result, rowcount=rowcount)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


# ── Postgres tests ───────────────────────────────────────────────────────────


class TestPostgresKGRepository:
    """Postgres dialect token tests — mock asyncpg.Connection."""

    @pytest.fixture
    def sut(self):
        from mnemos.persistence.postgres import PostgresKGRepository

        return PostgresKGRepository()

    @pytest.fixture
    def tx(self):
        from mnemos.persistence.postgres import PostgresTransaction

        conn = _make_asyncpg_conn(
            fetchrow_result={
                "id": "kg_abc",
                "subject": "s",
                "predicate": "p",
                "object": "o",
                "subject_type": None,
                "object_type": None,
                "valid_from": "2025-01-01",
                "valid_until": None,
                "memory_id": "mem1",
                "confidence": 0.9,
                "owner_id": "alice",
                "namespace": "ns",
                "metadata": '{"k":"v"}',
                "created": "2025-01-01",
                "deleted_at": None,
            },
            fetchval_result=5,
        )
        raw_tx = AsyncMock()
        return PostgresTransaction(conn, raw_tx)

    async def test_list_kg_triples_root(self, sut, tx):
        total, rows = await sut.list_kg_triples(
            tx,
            is_root=True,
            owner_id=None,
            namespace=None,
            filters={},
            limit=50,
            offset=0,
        )
        assert total == 5
        assert "$1" in str(tx.conn.fetch.call_args[0][0])
        assert "ORDER BY k.created DESC" in str(tx.conn.fetch.call_args[0][0])

    async def test_list_kg_triples_tenant(self, sut, tx):
        total, rows = await sut.list_kg_triples(
            tx,
            is_root=False,
            owner_id="alice",
            namespace="ns",
            filters={},
            limit=50,
            offset=0,
        )
        call_sql = str(tx.conn.fetch.call_args[0][0])
        assert "k.owner_id=$1" in call_sql
        assert "k.namespace=$2" in call_sql

    async def test_list_kg_triples_with_filters(self, sut, tx):
        total, rows = await sut.list_kg_triples(
            tx,
            is_root=True,
            owner_id=None,
            namespace=None,
            filters={
                "subject": "S",
                "predicate": "P",
                "object": "O",
                "since": datetime(2025, 1, 1, tzinfo=timezone.utc),
            },
            limit=10,
            offset=5,
        )
        call_sql = str(tx.conn.fetch.call_args[0][0])
        assert "k.subject=" in call_sql
        assert "k.predicate=" in call_sql
        assert "k.object=" in call_sql
        assert "k.created >=" in call_sql

    async def test_get_kg_timeline_root(self, sut, tx):
        await sut.get_kg_timeline(
            tx,
            subject="S1",
            is_root=True,
            owner_id=None,
            namespace=None,
            limit=10,
        )
        assert tx.conn.fetch.called
        sql = str(tx.conn.fetch.call_args[0][0])
        assert "valid_from ASC" in sql

    async def test_get_kg_timeline_tenant(self, sut, tx):
        await sut.get_kg_timeline(
            tx,
            subject="S1",
            is_root=False,
            owner_id="alice",
            namespace="ns",
            limit=10,
        )
        sql = str(tx.conn.fetch.call_args[0][0])
        assert "owner_id=" in sql

    async def test_update_kg_triple_root(self, sut, tx):
        result = await sut.update_kg_triple(
            tx,
            triple_id="kg_abc",
            updates={"confidence": 0.95},
            is_root=True,
            owner_id=None,
            namespace=None,
        )
        assert result is not None
        sql = str(tx.conn.fetchrow.call_args[0][0])
        assert "RETURNING" in sql

    async def test_update_kg_triple_tenant(self, sut, tx):
        await sut.update_kg_triple(
            tx,
            triple_id="kg_abc",
            updates={"confidence": 0.95},
            is_root=False,
            owner_id="alice",
            namespace="ns",
        )
        sql = str(tx.conn.fetchrow.call_args[0][0])
        assert "owner_id=" in sql

    async def test_update_kg_triple_empty_updates(self, sut, tx):
        result = await sut.update_kg_triple(
            tx,
            triple_id="kg_abc",
            updates={"subject": "x"},
            is_root=True,
            owner_id=None,
            namespace=None,
        )
        assert result is None

    async def test_update_kg_triple_not_found(self, sut, tx):
        tx.conn.fetchrow = AsyncMock(return_value=None)
        result = await sut.update_kg_triple(
            tx,
            triple_id="missing",
            updates={"confidence": 0.5},
            is_root=True,
            owner_id=None,
            namespace=None,
        )
        assert result is None

    async def test_delete_kg_triple_root(self, sut, tx):
        result = await sut.delete_kg_triple(
            tx,
            triple_id="kg_abc",
            is_root=True,
            owner_id=None,
            namespace=None,
        )
        assert result is True
        sql = str(tx.conn.execute.call_args[0][0])
        assert "UPDATE kg_triples SET deleted_at = NOW()" in sql
        assert "deleted_at IS NULL" in sql

    async def test_delete_kg_triple_tenant(self, sut, tx):
        await sut.delete_kg_triple(
            tx,
            triple_id="kg_abc",
            is_root=False,
            owner_id="alice",
            namespace="ns",
        )
        sql = str(tx.conn.execute.call_args[0][0])
        assert "owner_id=$2" in sql

    async def test_delete_kg_triple_not_found(self, sut, tx):
        tx.conn.execute = AsyncMock(return_value="UPDATE 0")
        result = await sut.delete_kg_triple(
            tx,
            triple_id="missing",
            is_root=True,
            owner_id=None,
            namespace=None,
        )
        assert result is False

    async def test_check_memory_ownership_found(self, sut, tx):
        result = await sut.check_memory_ownership(
            tx,
            memory_id="mem1",
            owner_id="alice",
            namespace="ns",
        )
        assert result is True
        sql = str(tx.conn.fetchrow.call_args[0][0])
        assert "SELECT 1 FROM memories" in sql
        assert "$1" in sql

    async def test_check_memory_ownership_not_found(self, sut, tx):
        tx.conn.fetchrow = AsyncMock(return_value=None)
        result = await sut.check_memory_ownership(
            tx,
            memory_id="missing",
            owner_id="bob",
            namespace="ns",
        )
        assert result is False


# ── Oracle tests ─────────────────────────────────────────────────────────────


class TestOracleKGRepository:
    """Oracle dialect token tests — mock oracledb cursor."""

    @pytest.fixture
    def sut(self):
        from mnemos.persistence.oracle import OracleKGRepository

        return OracleKGRepository()

    @pytest.fixture
    def tx(self):
        from mnemos.persistence.oracle import _OracleTransaction

        conn = _make_ora_conn(fetchone_result=(5,), rowcount=1)
        return _OracleTransaction(conn)

    async def test_list_kg_triples_oracle(self, sut, tx):
        total, rows = await sut.list_kg_triples(
            tx,
            is_root=True,
            owner_id=None,
            namespace=None,
            filters={},
            limit=10,
            offset=0,
        )
        assert total == 5

    async def test_list_kg_triples_oracle_with_filters(self, sut, tx):
        total, rows = await sut.list_kg_triples(
            tx,
            is_root=False,
            owner_id="alice",
            namespace="ns",
            filters={"subject": "S"},
            limit=10,
            offset=0,
        )
        assert isinstance(total, int)

    async def test_get_kg_timeline_oracle(self, sut, tx):
        rows = await sut.get_kg_timeline(
            tx,
            subject="S",
            is_root=True,
            owner_id=None,
            namespace=None,
            limit=10,
        )
        assert isinstance(rows, list)

    async def test_update_kg_triple_oracle(self, sut, tx):
        result = await sut.update_kg_triple(
            tx,
            triple_id="kg_abc",
            updates={"confidence": 0.9},
            is_root=True,
            owner_id=None,
            namespace=None,
        )
        assert result is not None

    async def test_delete_kg_triple_oracle(self, sut, tx):
        result = await sut.delete_kg_triple(
            tx,
            triple_id="kg_abc",
            is_root=True,
            owner_id=None,
            namespace=None,
        )
        assert result is True

    async def test_check_memory_ownership_oracle(self, sut, tx):
        result = await sut.check_memory_ownership(
            tx,
            memory_id="mem1",
            owner_id="alice",
            namespace="ns",
        )
        assert result is True


# ── SQLite tests ─────────────────────────────────────────────────────────────


class TestSqliteKGRepository:
    """SQLite dialect token tests — mock aiosqlite.Connection."""

    @pytest.fixture
    def sut(self):
        from mnemos.persistence.sqlite import SqliteKGRepository

        return SqliteKGRepository()

    @pytest.fixture
    def tx(self):
        from mnemos.persistence.sqlite import SqliteTransaction

        conn = AsyncMock()
        conn.execute = AsyncMock()
        return SqliteTransaction(conn)

    async def test_list_kg_triples_sqlite(self, sut, tx):
        with (
            patch("mnemos.persistence.sqlite._fetch_all") as fetch_all,
            patch("mnemos.persistence.sqlite._fetch_one") as fetch_one,
        ):
            fetch_all.return_value = []
            fetch_one.return_value = [5]
            total, rows = await sut.list_kg_triples(
                tx,
                is_root=True,
                owner_id=None,
                namespace=None,
                filters={},
                limit=10,
                offset=0,
            )
            assert total == 5
            sql = str(fetch_all.call_args[0][1])
            assert "OFFSET ?" in sql

    async def test_list_kg_triples_sqlite_tenant(self, sut, tx):
        with (
            patch("mnemos.persistence.sqlite._fetch_all") as fetch_all,
            patch("mnemos.persistence.sqlite._fetch_one") as fetch_one,
        ):
            fetch_all.return_value = []
            fetch_one.return_value = [3]
            await sut.list_kg_triples(
                tx,
                is_root=False,
                owner_id="alice",
                namespace="ns",
                filters={},
                limit=10,
                offset=0,
            )
            sql = str(fetch_all.call_args[0][1])
            assert "owner_id = ?" in sql
            assert "namespace = ?" in sql

    async def test_get_kg_timeline_sqlite(self, sut, tx):
        with patch("mnemos.persistence.sqlite._fetch_all") as fetch_all:
            fetch_all.return_value = []
            await sut.get_kg_timeline(
                tx,
                subject="S",
                is_root=True,
                owner_id=None,
                namespace=None,
                limit=10,
            )
            sql = str(fetch_all.call_args[0][1])
            assert "valid_from ASC" in sql

    async def test_update_kg_triple_sqlite(self, sut, tx):
        with (
            patch("mnemos.persistence.sqlite._execute") as _exec,
            patch("mnemos.persistence.sqlite._fetch_one") as fetch_one,
        ):
            fetch_one.return_value = {"id": "kg_abc", "subject": "s"}
            result = await sut.update_kg_triple(
                tx,
                triple_id="kg_abc",
                updates={"confidence": 0.9},
                is_root=True,
                owner_id=None,
                namespace=None,
            )
            assert result is not None

    async def test_delete_kg_triple_sqlite(self, sut, tx):
        with patch("mnemos.persistence.sqlite._execute_count") as exec_count:
            exec_count.return_value = 1
            result = await sut.delete_kg_triple(
                tx,
                triple_id="kg_abc",
                is_root=True,
                owner_id=None,
                namespace=None,
            )
            assert result is True
            sql = str(exec_count.call_args[0][1])
            assert "datetime('now')" in sql

    async def test_check_memory_ownership_sqlite(self, sut, tx):
        with patch("mnemos.persistence.sqlite._fetch_one") as fetch_one:
            fetch_one.return_value = [1]
            result = await sut.check_memory_ownership(
                tx,
                memory_id="mem1",
                owner_id="alice",
                namespace="ns",
            )
            assert result is True


# ── Db2 tests ────────────────────────────────────────────────────────────────


class TestDb2KGRepository:
    """Db2 — inherits OracleKGRepository, cursor layer handles translation."""

    @pytest.fixture
    def sut(self):
        from mnemos.persistence.db2 import Db2KGRepository

        return Db2KGRepository()

    async def test_db2_kg_repo_is_oracle_subclass(self, sut):
        from mnemos.persistence.oracle import OracleKGRepository

        assert isinstance(sut, OracleKGRepository)

    async def test_db2_kg_repo_has_mixin(self, sut):
        from mnemos.persistence.db2 import _Db2OraCompatMixin

        assert isinstance(sut, _Db2OraCompatMixin)

    async def test_db2_kg_repo_inherits_five_new_methods(self, sut):
        """Db2KGRepository has no body — it inherits all 5 new methods
        from OracleKGRepository via _Db2OraCompatMixin translation."""
        assert hasattr(sut, "list_kg_triples")
        assert hasattr(sut, "get_kg_timeline")
        assert hasattr(sut, "update_kg_triple")
        assert hasattr(sut, "delete_kg_triple")
        assert hasattr(sut, "check_memory_ownership")
