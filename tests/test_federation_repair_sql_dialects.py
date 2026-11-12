"""MN-24: the repair statements each backend ships, per dialect.

No live server is required: recording fakes capture the rendered SQL and
binds so a dialect that uses the wrong placeholder style, the wrong
substring function, or a non-portable row limiter fails here rather than in
production. The live proof (the repair actually heals rows and the next sync
re-pulls them) stays with the SQLite round-trip in
``tests/test_federation_repair_fenced_replicas.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnemos.persistence.mysql import MysqlFederationRepository
from mnemos.persistence.oracle import OracleFederationRepository
from mnemos.persistence.postgres import PostgresFederationRepository


class _RecordingAsyncpgConn:
    """asyncpg style: fetch/execute take SQL + *params."""

    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.statements.append((" ".join(sql.split()), tuple(params)))
        return []

    async def execute(self, sql, *params):
        self.statements.append((" ".join(sql.split()), tuple(params)))
        return "DELETE 0"


class _RecordingCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self.description = []

    async def execute(self, sql, params=()):
        self._conn.statements.append((" ".join(sql.split()), params))
        return self

    async def fetchall(self):
        return []

    async def fetchone(self):
        return None

    async def close(self):
        return None


class _RecordingDbApiConn:
    """DB-API style: cursor() for the MySQL/Oracle drivers."""

    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []

    def cursor(self):
        return _RecordingCursor(self)


class _RecordingMysqlCursor(_RecordingCursor):
    """aiomysql style: ``async with conn.cursor()``."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _RecordingMysqlConn(_RecordingDbApiConn):
    def cursor(self):
        return _RecordingMysqlCursor(self)


def _pg_tx():
    from mnemos.persistence.postgres import PostgresTransaction

    class _NoopRaw:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    return PostgresTransaction(_RecordingAsyncpgConn(), _NoopRaw())


def _tx(conn):
    return SimpleNamespace(conn=conn)


def _repo_tx(dialect: str):
    """Fresh repo + tx per test: statements never leak between cases."""
    if dialect == "postgres":
        return PostgresFederationRepository(), _pg_tx()
    if dialect == "mysql":
        return MysqlFederationRepository(), _tx(_RecordingMysqlConn())
    return OracleFederationRepository(), _tx(_RecordingDbApiConn())


def _statements(dialect: str, tx):
    return tx._conn.statements if dialect == "postgres" else tx.conn.statements


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["postgres", "mysql", "oracle"])
async def test_fenced_scan_selects_only_damaged_replicas_of_the_peer(dialect):
    repo, tx = _repo_tx(dialect)
    await repo.fetch_fenced_damaged_replicas(tx, peer_name="pythia", after_id="fed:pythia:a")
    [(sql, params)] = _statements(dialect, tx)
    assert "m.federation_source" in sql
    assert "m.deleted_at IS NOT NULL" in sql
    assert "m.archived_at IS NOT NULL" in sql
    assert "m.consolidated_into IS NOT NULL" in sql
    assert "federation_receive_state" in sql
    assert "ORDER BY m.id ASC" in sql
    bound = list(params.values()) if isinstance(params, dict) else list(params)
    assert "pythia" in bound
    assert "fed:pythia:a" in bound


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["postgres", "mysql", "oracle"])
async def test_fenced_scan_uses_the_dialect_row_limiter(dialect):
    repo, tx = _repo_tx(dialect)
    await repo.fetch_fenced_damaged_replicas(tx, peer_name="pythia")
    [(sql, _)] = _statements(dialect, tx)
    if dialect == "oracle":
        assert "FETCH FIRST" in sql
        assert "LIMIT" not in sql
    else:
        assert "LIMIT" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["postgres", "mysql", "oracle"])
async def test_repair_writes_delete_fence_cursors_and_archive_blob(dialect):
    repo, tx = _repo_tx(dialect)
    await repo.clear_federation_receive_fence(tx, peer_name="pythia", remote_id="old")
    await repo.clear_federation_peer_cursors(tx, peer_id="peer-id")
    await repo.delete_federated_archive_blob(tx, local_id="fed:pythia:old")
    statements = _statements(dialect, tx)
    assert len(statements) == 3
    assert statements[0][0].startswith("DELETE FROM federation_receive_state")
    assert statements[1][0].startswith("DELETE FROM federation_peer_cursors")
    assert statements[2][0].startswith("DELETE FROM memory_archive")

    def _bound(params):
        return list(params.values()) if isinstance(params, dict) else list(params)

    assert "old" in _bound(statements[0][1])
    assert "peer-id" in _bound(statements[1][1])
    assert "fed:pythia:old" in _bound(statements[2][1])
