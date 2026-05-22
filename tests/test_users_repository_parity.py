"""Driver-free parity tests for UserRepository across all 4 backends.

Mirrors the pattern from test_journal_repository_parity.py /
test_kg_repository_parity.py — mock cursors assert SQL token shape
without driver dependency.
"""

from types import SimpleNamespace
from typing import Any

import pytest


# ── Postgres ──────────────────────────────────────────────────────────────


class _FakePgConn:
    def __init__(self, fetchrow_result: Any = None, fetch_result: Any = None) -> None:
        self.fetchrow_calls: list[dict[str, Any]] = []
        self.fetch_calls: list[dict[str, Any]] = []
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result or []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.fetchrow_calls.append({"sql": sql, "args": args})
        return self._fetchrow_result

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.fetch_calls.append({"sql": sql, "args": args})
        return self._fetch_result


def _make_pg_tx(conn: _FakePgConn) -> Any:
    """Build a PostgresTransaction-shaped fake that satisfies _postgres_tx."""
    from mnemos.persistence.postgres import PostgresTransaction

    tx = PostgresTransaction.__new__(PostgresTransaction)
    tx._conn = conn
    tx._tx = None
    tx._closed = False
    tx._after_commit = []
    return tx


class TestPostgresUserRepository:
    @pytest.mark.asyncio
    async def test_create_user_inserts_with_returning(self) -> None:
        from mnemos.persistence.postgres import PostgresUserRepository

        conn = _FakePgConn(
            fetchrow_result={
                "id": "u1",
                "display_name": "Alice",
                "email": "a@x.com",
                "role": "user",
                "namespace": "default",
                "created_at": "2026-05-22",
            },
        )
        tx = _make_pg_tx(conn)
        repo = PostgresUserRepository()
        result = await repo.create_user(
            tx,
            user_id="u1",
            display_name="Alice",
            email="a@x.com",
            role="user",
            namespace="default",
        )
        assert result["id"] == "u1"
        assert "INSERT INTO users" in conn.fetchrow_calls[0]["sql"]
        assert "RETURNING" in conn.fetchrow_calls[0]["sql"]
        assert "$1" in conn.fetchrow_calls[0]["sql"]

    @pytest.mark.asyncio
    async def test_list_users_orders_by_created_at(self) -> None:
        from mnemos.persistence.postgres import PostgresUserRepository

        conn = _FakePgConn(
            fetch_result=[
                {
                    "id": "u1",
                    "display_name": "A",
                    "email": None,
                    "role": "user",
                    "namespace": "default",
                    "created_at": "2026-01-01",
                },
                {
                    "id": "u2",
                    "display_name": "B",
                    "email": None,
                    "role": "root",
                    "namespace": "default",
                    "created_at": "2026-02-01",
                },
            ]
        )
        tx = _make_pg_tx(conn)
        repo = PostgresUserRepository()
        rows = await repo.list_users(tx)
        assert len(rows) == 2
        assert "ORDER BY created_at" in conn.fetch_calls[0]["sql"]

    @pytest.mark.asyncio
    async def test_get_user_returns_dict_or_none(self) -> None:
        from mnemos.persistence.postgres import PostgresUserRepository

        conn_hit = _FakePgConn(
            fetchrow_result={
                "id": "u1",
                "display_name": "A",
                "email": None,
                "role": "user",
                "namespace": "default",
                "created_at": "2026-01-01",
            }
        )
        repo = PostgresUserRepository()
        hit = await repo.get_user(_make_pg_tx(conn_hit), user_id="u1")
        assert hit and hit["id"] == "u1"

        conn_miss = _FakePgConn(fetchrow_result=None)
        miss = await repo.get_user(_make_pg_tx(conn_miss), user_id="nope")
        assert miss is None


# ── Oracle ────────────────────────────────────────────────────────────────


class _FakeOraCursor:
    def __init__(self, fetchone_result: Any = None, fetchall_result: Any = None) -> None:
        self.execute_calls: list[dict[str, Any]] = []
        self._fetchone_result = fetchone_result
        self._fetchall_result = fetchall_result or []
        self.description = [
            ("id",),
            ("display_name",),
            ("email",),
            ("role",),
            ("namespace",),
            ("created_at",),
        ]
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.execute_calls.append({"sql": sql, "params": params})

    def fetchone(self) -> Any:
        return self._fetchone_result

    def fetchall(self) -> Any:
        return self._fetchall_result

    def close(self) -> None:
        pass


class _FakeOraConn:
    def __init__(self, cursor: _FakeOraCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeOraCursor:
        return self._cursor


class TestOracleUserRepository:
    @pytest.mark.asyncio
    async def test_create_user_uses_named_binds(self) -> None:
        from mnemos.persistence.oracle import OracleUserRepository

        cur = _FakeOraCursor(fetchone_result=("u1", "A", None, "user", "default", "2026-01-01"))
        tx = SimpleNamespace(conn=_FakeOraConn(cur))
        repo = OracleUserRepository()
        result = await repo.create_user(
            tx,
            user_id="u1",
            display_name="A",
            email=None,
            role="user",
            namespace="default",
        )
        # First execute is INSERT, second is SELECT-after-INSERT
        assert len(cur.execute_calls) == 2
        assert "INSERT INTO users" in cur.execute_calls[0]["sql"]
        assert ":id" in cur.execute_calls[0]["sql"]
        assert "$1" not in cur.execute_calls[0]["sql"]
        assert result["id"] == "u1"

    @pytest.mark.asyncio
    async def test_get_user_returns_none_for_missing(self) -> None:
        from mnemos.persistence.oracle import OracleUserRepository

        cur = _FakeOraCursor(fetchone_result=None)
        tx = SimpleNamespace(conn=_FakeOraConn(cur))
        repo = OracleUserRepository()
        result = await repo.get_user(tx, user_id="nope")
        assert result is None
        assert "SELECT" in cur.execute_calls[0]["sql"]
        assert "WHERE id = :id" in cur.execute_calls[0]["sql"]


# ── SQLite ────────────────────────────────────────────────────────────────


class TestSqliteUserRepository:
    @pytest.mark.asyncio
    async def test_create_user_uses_positional_binds(self) -> None:
        from mnemos.persistence.sqlite import SqliteUserRepository

        # SqliteUserRepository uses self._conn(tx) which calls
        # _sqlite_tx(tx).conn — patch via the _conn method.
        repo = SqliteUserRepository()
        captured: list[tuple[str, Any]] = []

        async def fake_fetch_one(conn: Any, sql: str, args: Any) -> Any:
            captured.append((sql, args))
            return {
                "id": "u1",
                "display_name": "A",
                "email": None,
                "role": "user",
                "namespace": "default",
                "created_at": "2026-01-01",
            }

        import mnemos.persistence.sqlite as sqlite_mod

        original = sqlite_mod._fetch_one
        sqlite_mod._fetch_one = fake_fetch_one
        repo._conn = lambda tx: object()  # type: ignore[assignment]
        try:
            result = await repo.create_user(
                SimpleNamespace(),
                user_id="u1",
                display_name="A",
                email=None,
                role="user",
                namespace="default",
            )
        finally:
            sqlite_mod._fetch_one = original

        assert result["id"] == "u1"
        assert "INSERT INTO users" in captured[0][0]
        assert "VALUES (?, ?, ?, ?, ?)" in captured[0][0]
        assert "RETURNING" in captured[0][0]


# ── Db2 ───────────────────────────────────────────────────────────────────


class TestDb2UserRepository:
    def test_db2_inherits_oracle_user_repo(self) -> None:
        from mnemos.persistence.db2 import Db2UserRepository
        from mnemos.persistence.oracle import OracleUserRepository

        assert issubclass(Db2UserRepository, OracleUserRepository)

    def test_db2_has_compat_mixin(self) -> None:
        from mnemos.persistence.db2 import Db2UserRepository, _Db2OraCompatMixin

        assert _Db2OraCompatMixin in Db2UserRepository.__mro__
