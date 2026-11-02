"""Cross-backend parity for replica lifecycle: tombstones, stats and withdrawal.

FB101 (audit findings F3, P4, P5, S5, F4). The parity table below is the spec;
the tests either run it against a real SQLite database or pin the statement
each enterprise backend sends, because no Oracle / Db2 / MySQL server is
available in this tree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# The parity table. Every backend must agree on these three rows.
# ---------------------------------------------------------------------------

PARITY: dict[str, dict[str, Any]] = {
    "sqlite": {
        "tombstone": "consolidated_into",
        "stats_key": "federation_source",
        "withdrawal_ignores_deleted_at": True,
    },
    "postgres": {
        "tombstone": "consolidated_into",
        "stats_key": "federation_source",
        "withdrawal_ignores_deleted_at": True,
    },
    "mysql": {
        "tombstone": "consolidated_into",
        "stats_key": "federation_source",
        "withdrawal_ignores_deleted_at": True,
    },
    "oracle": {
        "tombstone": "consolidated_into",
        "stats_key": "federation_source",
        "withdrawal_ignores_deleted_at": True,
    },
    "db2": {
        "tombstone": "consolidated_into",
        "stats_key": "federation_source",
        "withdrawal_ignores_deleted_at": True,
    },
}

LOCAL_ID = "fed:peer:r1"


class _Cursor:
    """oracledb / ibm_db_dbi cursor: records every statement it is given."""

    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls
        self.description = None
        self.rowcount = 1

    async def execute(self, sql: Any, params: Any = ()) -> "_Cursor":
        self._calls.append((" ".join(str(sql).split()), params))
        return self

    async def fetchone(self) -> Any:
        return (1, 1, 0, None)

    async def fetchall(self) -> list[Any]:
        return []

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.calls)


class _PgConn:
    """asyncpg connection: one awaitable per call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, sql: Any, *params: Any) -> str:
        self.calls.append((" ".join(str(sql).split()), params))
        return "UPDATE 1"

    async def fetch(self, sql: Any, *params: Any) -> list[Any]:
        self.calls.append((" ".join(str(sql).split()), params))
        return []

    async def fetchrow(self, sql: Any, *params: Any) -> Any:
        self.calls.append((" ".join(str(sql).split()), params))
        return None


async def _delete_sql(backend_name: str) -> str:
    if backend_name == "postgres":
        from mnemos.persistence.postgres import PostgresFederationRepository, PostgresTransaction

        conn = _PgConn()
        await PostgresFederationRepository().delete_federated_memory(
            PostgresTransaction(conn, None), "peer", "r1"
        )
        return conn.calls[0][0]
    conn = _Conn()
    tx = SimpleNamespace(conn=conn)
    if backend_name == "mysql":
        from mnemos.persistence.mysql import MysqlFederationRepository

        await MysqlFederationRepository().delete_federated_memory(tx, "peer", "r1")
    elif backend_name == "oracle":
        from mnemos.persistence.oracle import OracleFederationRepository

        await OracleFederationRepository().delete_federated_memory(tx, "peer", "r1")
    elif backend_name == "db2":
        from mnemos.persistence.db2 import Db2FederationRepository

        await Db2FederationRepository().delete_federated_memory(tx, "peer", "r1")
    else:
        raise AssertionError(f"no fake for {backend_name}")
    return conn.calls[0][0]


async def _tombstone_sql(backend_name: str) -> str:
    kwargs = dict(
        local_id=LOCAL_ID,
        local_canonical_id="canon-1",
        consolidated_at=datetime.now(timezone.utc),
        remote_id="r1",
        canonical_remote_id="r9",
        peer_name="peer",
    )
    if backend_name == "postgres":
        from mnemos.persistence.postgres import PostgresFederationRepository, PostgresTransaction

        conn = _PgConn()
        await PostgresFederationRepository().apply_consolidation_tombstone(
            PostgresTransaction(conn, None), **kwargs
        )
        return "\n".join(sql for sql, _ in conn.calls)
    conn = _Conn()
    tx = SimpleNamespace(conn=conn)
    if backend_name == "mysql":
        from mnemos.persistence.mysql import MysqlFederationRepository

        await MysqlFederationRepository().apply_consolidation_tombstone(tx, **kwargs)
    elif backend_name == "oracle":
        from mnemos.persistence.oracle import OracleFederationRepository

        await OracleFederationRepository().apply_consolidation_tombstone(tx, **kwargs)
    elif backend_name == "db2":
        from mnemos.persistence.db2 import Db2FederationRepository

        await Db2FederationRepository().apply_consolidation_tombstone(tx, **kwargs)
    else:
        raise AssertionError(f"no fake for {backend_name}")
    return "\n".join(sql for sql, _ in conn.calls)


async def _stats_sql(backend_name: str) -> str:
    conn = _Conn()
    tx = SimpleNamespace(conn=conn)
    if backend_name == "oracle":
        from mnemos.persistence.oracle import OracleMemoryRepository

        await OracleMemoryRepository().gather_stats(tx)
    elif backend_name == "db2":
        from mnemos.persistence.db2 import Db2MemoryRepository

        await Db2MemoryRepository().gather_stats(tx)
    else:
        raise AssertionError(f"no fake for {backend_name}")
    return "\n".join(sql for sql, _ in conn.calls)


def _assert_no_deleted_at_assignment(sql: str) -> None:
    upper = sql.upper()
    assert "SET DELETED_AT" not in upper
    assert "DELETED_AT =" not in upper


# ---------------------------------------------------------------------------
# F4 · a withdrawal owns the replica, soft-deleted here or not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["postgres", "mysql", "oracle", "db2"])
async def test_withdrawal_statement_ignores_local_soft_delete(backend_name):
    assert PARITY[backend_name]["withdrawal_ignores_deleted_at"] is True
    sql = await _delete_sql(backend_name)
    upper = sql.upper()
    assert "FEDERATION_SOURCE" in upper
    # SQLite deletes the row unconditionally; the others required
    # ``deleted_at IS NULL``, so a source withdrawal left a ghost row.
    assert "DELETED_AT" not in upper


@pytest.mark.asyncio
async def test_sqlite_withdrawal_deletes_a_soft_deleted_replica(backend, monkeypatch):
    from mnemos.persistence.sqlite import _execute, _fetch_one

    async with backend.transactional() as tx:
        await _execute(
            tx.conn,
            "INSERT INTO memories (id, content, owner_id, namespace, federation_source, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (LOCAL_ID, "replica body", "user-1", "default", "peer"),
        )
        deleted = await backend.federation.delete_federated_memory(tx, "peer", "r1")
    assert deleted == 1
    async with backend.transactional() as tx:
        assert await _fetch_one(tx.conn, "SELECT id FROM memories WHERE id = ?", (LOCAL_ID,)) is None


# ---------------------------------------------------------------------------
# F3 · a consolidation tombstone points at its canonical, never deletes the loser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["mysql", "oracle", "db2"])
async def test_tombstone_sets_consolidated_into_and_keeps_the_row(backend_name):
    assert PARITY[backend_name]["tombstone"] == "consolidated_into"
    sql = await _tombstone_sql(backend_name)
    upper = sql.upper()
    assert "CONSOLIDATED_INTO" in upper
    _assert_no_deleted_at_assignment(sql)
    # The live-canonical guard Postgres has: never point at a dead row.
    # MySQL reaches the canonical through a JOIN alias, Oracle/Db2 through an
    # EXISTS subquery, so accept either spelling.
    assert "CANONICAL.DELETED_AT IS NULL" in upper or "C.DELETED_AT IS NULL" in upper


@pytest.mark.asyncio
async def test_sqlite_tombstone_refuses_a_dead_canonical(backend):
    from mnemos.persistence.sqlite import _execute, _fetch_one

    now = datetime.now(timezone.utc)
    async with backend.transactional() as tx:
        await _execute(
            tx.conn,
            "INSERT INTO memories (id, content, owner_id, namespace, deleted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("canon-1", "canonical body", "user-1", "default", now),
        )
        await _execute(
            tx.conn,
            "INSERT INTO memories (id, content, owner_id, namespace) VALUES (?, ?, ?, ?)",
            ("loser-1", "loser body", "user-1", "default"),
        )
        applied = await backend.federation.apply_consolidation_tombstone(
            tx,
            local_id="loser-1",
            local_canonical_id="canon-1",
            consolidated_at=now,
            remote_id="r1",
            canonical_remote_id="r9",
            peer_name="peer",
        )
        assert applied is False
        row = await _fetch_one(
            tx.conn, "SELECT consolidated_into, deleted_at FROM memories WHERE id = ?", ("loser-1",)
        )
    assert row["consolidated_into"] is None
    # Postgres and MySQL leave the live loser alone when the canonical is gone;
    # pointing it at a soft-deleted row hid it from reads with nothing live.
    assert row["deleted_at"] is None


@pytest.mark.asyncio
async def test_sqlite_tombstone_ignores_an_already_deleted_loser(backend):
    from mnemos.persistence.sqlite import _execute, _fetch_one

    now = datetime.now(timezone.utc)
    async with backend.transactional() as tx:
        await _execute(
            tx.conn,
            "INSERT INTO memories (id, content, owner_id, namespace) VALUES (?, ?, ?, ?)",
            ("canon-1", "canonical body", "user-1", "default"),
        )
        await _execute(
            tx.conn,
            "INSERT INTO memories (id, content, owner_id, namespace, deleted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("loser-1", "loser body", "user-1", "default", now),
        )
        applied = await backend.federation.apply_consolidation_tombstone(
            tx,
            local_id="loser-1",
            local_canonical_id="canon-1",
            consolidated_at=now,
            remote_id="r1",
            canonical_remote_id="r9",
            peer_name="peer",
        )
        assert applied is False
        row = await _fetch_one(
            tx.conn, "SELECT consolidated_into FROM memories WHERE id = ?", ("loser-1",)
        )
    assert row["consolidated_into"] is None


# ---------------------------------------------------------------------------
# P4 · federated memories are counted by the replica column, not metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_name", ["oracle", "db2"])
async def test_stats_count_replicas_by_federation_source(backend_name):
    assert PARITY[backend_name]["stats_key"] == "federation_source"
    sql = await _stats_sql(backend_name)
    upper = sql.upper()
    assert "FEDERATION_SOURCE IS NULL" in upper
    assert "FEDERATION_SOURCE IS NOT NULL" in upper
    # Nothing writes this key: replicas carry ``federation_source`` and
    # ``federation_remote_id``, so the count was permanently zero.
    assert "FEDERATION_ORIGIN" not in upper


# ---------------------------------------------------------------------------
# S5 · stats count live rows only, like every other backend
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_FEDERATION_REPLICA_FENCE", "1")
    from mnemos.core import config as core_config

    core_config.reload_settings()
    from mnemos.persistence.sqlite import SqliteBackend

    instance = SqliteBackend(
        tmp_path / "replica-lifecycle.db",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=3)),
    )
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_sqlite_stats_exclude_soft_deleted_rows(backend):
    from mnemos.persistence.sqlite import _execute

    now = datetime.now(timezone.utc)
    async with backend.transactional() as tx:
        await _execute(
            tx.conn,
            "INSERT INTO memories (id, content, owner_id, namespace, category, quality_rating) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("live-1", "live body", "user-1", "default", "facts", 3),
        )
        await _execute(
            tx.conn,
            "INSERT INTO memories (id, content, owner_id, namespace, category, quality_rating, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("gone-1", "deleted body", "user-1", "default", "facts", 1, now),
        )
        await _execute(
            tx.conn,
            "INSERT INTO memories (id, content, owner_id, namespace, category, quality_rating) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (LOCAL_ID, "replica body", "user-1", "default", "facts", 3),
        )
        await _execute(
            tx.conn, "UPDATE memories SET federation_source = ? WHERE id = ?", ("peer", LOCAL_ID)
        )
        stats = await backend.memories.gather_stats(tx)

    assert stats.total_memories == 2
    assert stats.native_memories == 1
    assert stats.federated_memories == 1
    assert stats.memories_by_category == {"facts": 2}
    assert stats.avg_quality_rating == pytest.approx(3.0)
