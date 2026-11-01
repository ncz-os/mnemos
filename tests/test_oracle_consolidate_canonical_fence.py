"""F-3 (Oracle): ARTEMIS consolidation must not fold a local row into a replica.

``consolidate_duplicate_memories`` soft-deletes the duplicates of a canonical
row. The duplicates side already carries ``local_sweep_predicate`` so a
replica is never removed by the sweep. The canonical side is the other half:
if the canonical id is itself a federated replica, the local duplicate is
removed in favour of a row whose lifecycle belongs to its source. The next
withdrawal from that source then takes the content away from this node
entirely, because the local copy that would have survived is already gone.

Postgres, SQLite and MySQL refuse that call (an ``EXISTS`` / pre-SELECT on the
canonical id carrying the same fence plus the liveness predicate); SQLite's
behaviour is pinned in ``test_federation_replica_fence.py::
test_artemis_consolidate_refuses_a_non_local_canonical``. These tests hold the
Oracle implementation to the same contract.

No Oracle server is needed: the Oracle repository talks to a DB-API style
connection through ``_call``, so an in-memory SQLite connection stands in for
it. Named ``:dupN`` / ``:canonical_id`` binds are native SQLite syntax; the
only Oracle-specific token, ``SYSTIMESTAMP``, is rewritten to
``CURRENT_TIMESTAMP``. That executes the statement the backend actually
emits, so the assertions are on row state rather than on SQL text.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from mnemos.persistence.oracle import OracleMemoryRepository

FENCE_ENV = "MNEMOS_FEDERATION_REPLICA_FENCE"
REPLICA_ID = "fed:peer:remote"


class _Cursor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._cursor = conn.cursor()

    def execute(self, sql, params=None):
        return self._cursor.execute(sql.replace("SYSTIMESTAMP", "CURRENT_TIMESTAMP"), params or {})

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def close(self) -> None:
        self._cursor.close()


class _Conn:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE memories ("
            " id TEXT PRIMARY KEY, content TEXT, federation_source TEXT,"
            " deleted_at TEXT, archived_at TEXT, consolidated_into TEXT)"
        )

    def cursor(self) -> _Cursor:
        return _Cursor(self.db)

    def insert(self, mid: str, *, source: str | None = None, deleted: bool = False) -> None:
        self.db.execute(
            "INSERT INTO memories (id, content, federation_source, deleted_at) VALUES (?, 'same facts', ?, ?)",
            (mid, source, "2020-01-01" if deleted else None),
        )

    def deleted_at(self, mid: str):
        return self.db.execute("SELECT deleted_at FROM memories WHERE id = ?", (mid,)).fetchone()[0]


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.delenv(FENCE_ENV, raising=False)
    c = _Conn()
    yield c
    c.db.close()


async def _consolidate(conn: _Conn, canonical_id: str, duplicate_ids: list[str]) -> int:
    return await OracleMemoryRepository().consolidate_duplicate_memories(
        SimpleNamespace(conn=conn),
        canonical_id=canonical_id,
        duplicate_ids=duplicate_ids,
    )


@pytest.mark.asyncio
async def test_oracle_consolidate_refuses_a_replica_canonical(conn):
    """The finding: a local duplicate must not be dropped in favour of a replica."""
    conn.insert("local_row")
    conn.insert(REPLICA_ID, source="peer")

    merged = await _consolidate(conn, REPLICA_ID, ["local_row"])

    assert merged == 0
    assert conn.deleted_at("local_row") is None, "local row was folded into a federated replica"
    assert conn.deleted_at(REPLICA_ID) is None


@pytest.mark.asyncio
async def test_oracle_consolidate_refuses_a_deleted_canonical(conn):
    """Postgres's canonical EXISTS also requires a live row; Oracle must match.

    Without it the duplicates are deleted while the row they collapse into is
    already gone, so the content disappears from this node.
    """
    conn.insert("local_canonical", deleted=True)
    conn.insert("local_dup")

    merged = await _consolidate(conn, "local_canonical", ["local_dup"])

    assert merged == 0
    assert conn.deleted_at("local_dup") is None


@pytest.mark.asyncio
async def test_oracle_consolidate_merges_into_a_local_canonical(conn):
    """Positive control: the fence must not stop the ordinary local merge."""
    conn.insert("local_canonical")
    conn.insert("local_dup_1")
    conn.insert("local_dup_2")

    merged = await _consolidate(conn, "local_canonical", ["local_dup_1", "local_dup_2"])

    assert merged == 2
    assert conn.deleted_at("local_dup_1") is not None
    assert conn.deleted_at("local_dup_2") is not None
    assert conn.deleted_at("local_canonical") is None


@pytest.mark.asyncio
async def test_oracle_consolidate_still_skips_a_replica_duplicate(conn):
    """The pre-existing duplicates-side fence keeps working next to the new one."""
    conn.insert("local_canonical")
    conn.insert("local_dup")
    conn.insert(REPLICA_ID, source="peer")

    merged = await _consolidate(conn, "local_canonical", ["local_dup", REPLICA_ID])

    assert merged == 1
    assert conn.deleted_at("local_dup") is not None
    assert conn.deleted_at(REPLICA_ID) is None


@pytest.mark.asyncio
async def test_oracle_consolidate_fence_off_accepts_a_replica_canonical(conn, monkeypatch):
    """Switch off restores the pre-fix selection, as on every other backend."""
    monkeypatch.setenv(FENCE_ENV, "off")
    conn.insert("local_row")
    conn.insert(REPLICA_ID, source="peer")

    merged = await _consolidate(conn, REPLICA_ID, ["local_row"])

    assert merged == 1
    assert conn.deleted_at("local_row") is not None
