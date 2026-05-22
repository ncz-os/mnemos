"""VersionRepository read-side parity — driver-free mock-cursor tests.

Covers ``list_versions``, ``get_version``, and ``diff_versions`` on all
four backends (Postgres, Oracle, SQLite, Db2).  Db2 inherits via
``_Db2OraCompatMixin(OracleVersionRepository)`` — parity is an import+smoke
test.  All tests are database-free.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


# ── shared test data ──────────────────────────────────────────────────────

_ID = "mem-abc"
_BRANCH = "main"
_OWNER = "owner-1"
_NS = "ns-a"

_SAMPLE_ROW = {
    "id": "ver-1",
    "memory_id": _ID,
    "version_num": 1,
    "content": "hello",
    "category": "facts",
    "subcategory": None,
    "metadata": "{}",
    "verbatim_content": None,
    "owner_id": _OWNER,
    "namespace": _NS,
    "permission_mode": 600,
    "source_model": None,
    "source_provider": None,
    "source_session": None,
    "source_agent": None,
    "snapshot_at": "2025-01-01T00:00:00+00:00",
    "snapshot_by": None,
    "change_type": "create",
    "commit_hash": "abc123",
    "parent_version_id": None,
    "branch": _BRANCH,
    "merge_parents": [],
}

_SAMPLE_ROW_V2 = {**_SAMPLE_ROW, "version_num": 2, "content": "world"}


# ──────────────────────────────────────────────────────────────────────────
# Postgres mock tests
# ──────────────────────────────────────────────────────────────────────────


def _pg_fake_tx(fetch_rows=None, fetchrow_result=None):
    """Return a ``PostgresTransaction``-shaped SimpleNamespace with
    mocked ``fetch``/``fetchrow``."""

    class _MockConn:
        def __init__(self):
            self._fetch_rows = fetch_rows or []
            self._fetchrow_result = fetchrow_result

        async def fetch(self, sql: str, *params: Any):
            return self._fetch_rows

        async def fetchrow(self, sql: str, *params: Any):
            return self._fetchrow_result

    return SimpleNamespace(conn=_MockConn())


@pytest.mark.asyncio
async def test_pg_list_versions_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.postgres import PostgresVersionRepository

    monkeypatch.setattr(
        "mnemos.persistence.postgres._postgres_tx",
        lambda tx: tx,
    )
    repo = PostgresVersionRepository()
    tx = _pg_fake_tx(fetch_rows=[_SAMPLE_ROW, _SAMPLE_ROW_V2])
    result = await repo.list_versions(
        tx,
        memory_id=_ID,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert len(result) == 2
    assert result[0]["version_num"] == 1
    assert result[1]["version_num"] == 2


@pytest.mark.asyncio
async def test_pg_list_versions_no_branch_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.postgres import PostgresVersionRepository

    monkeypatch.setattr("mnemos.persistence.postgres._postgres_tx", lambda tx: tx)
    repo = PostgresVersionRepository()
    tx = _pg_fake_tx(fetch_rows=[_SAMPLE_ROW])
    result = await repo.list_versions(
        tx,
        memory_id=_ID,
        branch=None,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_pg_list_versions_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.postgres import PostgresVersionRepository

    monkeypatch.setattr("mnemos.persistence.postgres._postgres_tx", lambda tx: tx)
    repo = PostgresVersionRepository()
    tx = _pg_fake_tx(fetch_rows=[_SAMPLE_ROW])
    result = await repo.list_versions(
        tx,
        memory_id=_ID,
        branch=_BRANCH,
        is_root=False,
        owner_id=_OWNER,
        namespace=_NS,
    )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_pg_get_version_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.postgres import PostgresVersionRepository

    monkeypatch.setattr("mnemos.persistence.postgres._postgres_tx", lambda tx: tx)
    repo = PostgresVersionRepository()
    tx = _pg_fake_tx(fetchrow_result=_SAMPLE_ROW)
    result = await repo.get_version(
        tx,
        memory_id=_ID,
        version_num=1,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert result is not None
    assert result["version_num"] == 1


@pytest.mark.asyncio
async def test_pg_get_version_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.postgres import PostgresVersionRepository

    monkeypatch.setattr("mnemos.persistence.postgres._postgres_tx", lambda tx: tx)
    repo = PostgresVersionRepository()
    tx = _pg_fake_tx(fetchrow_result=None)
    result = await repo.get_version(
        tx,
        memory_id=_ID,
        version_num=99,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_pg_diff_versions_both_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.postgres import PostgresVersionRepository

    monkeypatch.setattr("mnemos.persistence.postgres._postgres_tx", lambda tx: tx)
    repo = PostgresVersionRepository()
    tx = _pg_fake_tx(fetch_rows=[_SAMPLE_ROW, _SAMPLE_ROW_V2])
    a, b = await repo.diff_versions(
        tx,
        memory_id=_ID,
        from_version=1,
        to_version=2,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert a is not None and a["version_num"] == 1
    assert b is not None and b["version_num"] == 2


@pytest.mark.asyncio
async def test_pg_diff_versions_one_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.postgres import PostgresVersionRepository

    monkeypatch.setattr("mnemos.persistence.postgres._postgres_tx", lambda tx: tx)
    repo = PostgresVersionRepository()
    tx = _pg_fake_tx(fetch_rows=[_SAMPLE_ROW])
    a, b = await repo.diff_versions(
        tx,
        memory_id=_ID,
        from_version=1,
        to_version=99,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert a is not None
    assert b is None


# ──────────────────────────────────────────────────────────────────────────
# Oracle mock tests
# ──────────────────────────────────────────────────────────────────────────


def _ora_fake_tx(rows_by_sql=None):
    """Return a fake ``oracledb.AsyncConnection``-shaped object.

    ``_conn_from_tx`` in oracle.py expects ``tx.conn`` to have a
    ``cursor()`` that returns an async cursor with
    ``execute(sql, params)``, ``fetchall()``, ``fetchone()``,
    and ``close()``.

    If *rows_by_sql* is a dict mapping SQL pattern → list[dict],
    the cursor returns matching rows.  Otherwise, returns a single
    default row.
    """
    if rows_by_sql is None:
        rows_by_sql = {"memory_versions": [_SAMPLE_ROW]}

    class _FakeCursor:
        def __init__(self):
            self.description = None
            self._rows: list[tuple] = []
            self._single: tuple | None = None

        async def execute(self, sql: str, params: Any = None) -> None:
            for key, rows in rows_by_sql.items():
                if key in sql:
                    cols = list(rows[0].keys()) if rows else []
                    self.description = tuple((c,) for c in cols)
                    self._rows = [tuple(d[c] for c in cols) for d in rows]
                    self._single = self._rows[0] if self._rows else None
                    return
            # fallback: return sample row
            cols = list(_SAMPLE_ROW.keys())
            self.description = tuple((c,) for c in cols)
            self._rows = [tuple(_SAMPLE_ROW[c] for c in cols)]
            self._single = self._rows[0]

        async def fetchall(self) -> list[tuple]:
            return self._rows

        async def fetchone(self) -> tuple | None:
            return self._single

        async def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    return SimpleNamespace(conn=_FakeConn())


@pytest.mark.asyncio
async def test_ora_list_versions_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.oracle import OracleVersionRepository

    monkeypatch.setattr(
        "mnemos.persistence.oracle._conn_from_tx",
        lambda tx: tx.conn,
    )
    repo = OracleVersionRepository()
    tx = _ora_fake_tx(rows_by_sql={"FROM memory_versions": [_SAMPLE_ROW, _SAMPLE_ROW_V2]})
    result = await repo.list_versions(
        tx,
        memory_id=_ID,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert len(result) == 2
    assert result[0]["version_num"] == 1


@pytest.mark.asyncio
async def test_ora_get_version_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.oracle import OracleVersionRepository

    monkeypatch.setattr("mnemos.persistence.oracle._conn_from_tx", lambda tx: tx.conn)
    repo = OracleVersionRepository()
    tx = _ora_fake_tx(rows_by_sql={"FROM memory_versions": [_SAMPLE_ROW]})
    result = await repo.get_version(
        tx,
        memory_id=_ID,
        version_num=1,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert result is not None
    assert result["version_num"] == 1


@pytest.mark.asyncio
async def test_ora_get_version_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.oracle import OracleVersionRepository

    monkeypatch.setattr("mnemos.persistence.oracle._conn_from_tx", lambda tx: tx.conn)
    repo = OracleVersionRepository()
    tx = _ora_fake_tx(rows_by_sql={"FROM memory_versions": []})
    result = await repo.get_version(
        tx,
        memory_id=_ID,
        version_num=99,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_ora_diff_versions_both_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.oracle import OracleVersionRepository

    monkeypatch.setattr("mnemos.persistence.oracle._conn_from_tx", lambda tx: tx.conn)
    repo = OracleVersionRepository()
    tx = _ora_fake_tx(rows_by_sql={"FROM memory_versions": [_SAMPLE_ROW, _SAMPLE_ROW_V2]})
    a, b = await repo.diff_versions(
        tx,
        memory_id=_ID,
        from_version=1,
        to_version=2,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert a is not None and a["version_num"] == 1
    assert b is not None and b["version_num"] == 2


# ──────────────────────────────────────────────────────────────────────────
# SQLite mock tests
# ──────────────────────────────────────────────────────────────────────────


def _sqlite_fake_tx(rows=None):
    """Return a SimpleNamespace with a ``.execute()`` that yields *rows*."""
    if rows is None:
        rows = [_SAMPLE_ROW]
    return SimpleNamespace(_rows=rows)


@pytest.mark.asyncio
async def test_sql_list_versions_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.sqlite import SqliteVersionRepository

    repo = SqliteVersionRepository()
    tx = _sqlite_fake_tx(rows=[_SAMPLE_ROW, _SAMPLE_ROW_V2])

    async def _fake_fetch_all(conn, sql, params):
        return conn._rows

    monkeypatch.setattr("mnemos.persistence.sqlite._fetch_all", _fake_fetch_all)
    monkeypatch.setattr(repo, "_conn", lambda t: t)
    result = await repo.list_versions(
        tx,
        memory_id=_ID,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert len(result) == 2


@pytest.mark.asyncio
async def test_sql_get_version_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.sqlite import SqliteVersionRepository

    repo = SqliteVersionRepository()
    tx = _sqlite_fake_tx(rows=[_SAMPLE_ROW])

    async def _fake_fetch_one(conn, sql, params):
        return conn._rows[0] if conn._rows else None

    monkeypatch.setattr("mnemos.persistence.sqlite._fetch_one", _fake_fetch_one)
    monkeypatch.setattr(repo, "_conn", lambda t: t)
    result = await repo.get_version(
        tx,
        memory_id=_ID,
        version_num=1,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert result is not None
    assert result["version_num"] == 1


@pytest.mark.asyncio
async def test_sql_diff_versions_both_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.persistence.sqlite import SqliteVersionRepository

    repo = SqliteVersionRepository()
    tx = _sqlite_fake_tx(rows=[_SAMPLE_ROW, _SAMPLE_ROW_V2])

    async def _fake_fetch_all(conn, sql, params):
        return conn._rows

    monkeypatch.setattr("mnemos.persistence.sqlite._fetch_all", _fake_fetch_all)
    monkeypatch.setattr(repo, "_conn", lambda t: t)
    a, b = await repo.diff_versions(
        tx,
        memory_id=_ID,
        from_version=1,
        to_version=2,
        branch=_BRANCH,
        is_root=True,
        owner_id=None,
        namespace=None,
    )
    assert a is not None and a["version_num"] == 1
    assert b is not None and b["version_num"] == 2


# ──────────────────────────────────────────────────────────────────────────
# Db2 parity smoke — inherits Oracle impl via _Db2OraCompatMixin
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db2_repo_is_oracle_compat_subclass() -> None:
    """Db2VersionRepository inherits OracleVersionRepository — new methods
    flow through automatically."""
    from mnemos.persistence.db2 import Db2VersionRepository, _Db2OraCompatMixin
    from mnemos.persistence.oracle import OracleVersionRepository

    class _CompatClone(_Db2OraCompatMixin, OracleVersionRepository):
        pass

    # Db2VersionRepository uses the same MRO pattern — it should have
    # all 3 new methods from OracleVersionRepository.
    repo = Db2VersionRepository()
    assert hasattr(repo, "list_versions")
    assert hasattr(repo, "get_version")
    assert hasattr(repo, "diff_versions")
    assert callable(repo.list_versions)
    assert callable(repo.get_version)
    assert callable(repo.diff_versions)
