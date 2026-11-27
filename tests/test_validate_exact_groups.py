import sqlite3
from types import SimpleNamespace

import pytest
from mnemos.persistence.sqlite import SqliteMemoryRepository
from mnemos.persistence.postgres import PostgresMemoryRepository
from mnemos.persistence.oracle import OracleMemoryRepository
from mnemos.persistence.db2 import Db2MemoryRepository
from mnemos.persistence.mysql import MysqlMemoryRepository
from mnemos.persistence.mariadb import MariadbMemoryRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content", "owner", "namespace", "keeper", "none"])
async def test_validate_exact_group_before_mutation(change, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, content_hash TEXT,
      owner_id TEXT, namespace TEXT, deleted_at TEXT, archived_at TEXT, consolidated_into TEXT,
      consolidated_at TEXT, updated TEXT, federation_source TEXT, metadata TEXT);
      INSERT INTO memories (id, content, content_hash, owner_id, namespace) VALUES
      ('a', 'note', 'hash', 'alice', 'notes'), ('b', 'note', 'hash', 'alice', 'notes');""")
    if change == "content":
        conn.execute("UPDATE memories SET content='revision' WHERE id='b'")
    elif change == "owner":
        conn.execute("UPDATE memories SET owner_id='bob' WHERE id='b'")
    elif change == "namespace":
        conn.execute("UPDATE memories SET namespace='other' WHERE id='b'")
    elif change == "keeper":
        conn.execute("UPDATE memories SET deleted_at=CURRENT_TIMESTAMP WHERE id='a'")
    tx = SimpleNamespace(conn=conn)
    monkeypatch.setattr("mnemos.persistence.worker_lifecycle.transaction_dialect", lambda tx: "sqlite")
    try:
        repo = object.__new__(SqliteMemoryRepository)
        result = await repo.mutate_exact_duplicates(
            tx,
            canonical_id="a",
            duplicate_ids=["b"],
            owner_id="alice",
            namespace="notes",
            content_hash="hash",
            consolidate=False,
        )
        assert len(result) == (1 if change == "none" else 0)
        assert (conn.execute("SELECT deleted_at FROM memories WHERE id='b'").fetchone()[0] is not None) == (
            change == "none"
        )
        again = await repo.mutate_exact_duplicates(
            tx,
            canonical_id="b",
            duplicate_ids=["a"],
            owner_id="alice",
            namespace="notes",
            content_hash="hash",
            consolidate=True,
        )
        assert again == []
        if change == "none":
            assert conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    "repo",
    [
        SqliteMemoryRepository,
        PostgresMemoryRepository,
        OracleMemoryRepository,
        Db2MemoryRepository,
        MysqlMemoryRepository,
        MariadbMemoryRepository,
    ],
)
def test_expose_exact_group_operation(repo):
    assert callable(getattr(repo, "mutate_exact_duplicates", None))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "consolidate"])
@pytest.mark.parametrize("change", ["content", "owner", "namespace", "keeper"])
async def test_sweep_preserves_revised_group(operation, change, monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from mnemos.persistence import sqlite as sq
    from mnemos.domain import memory_dedup, artemis_dedup

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sq._dict_factory
    conn.executescript("""CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, content_hash TEXT,
      owner_id TEXT, namespace TEXT, deleted_at TEXT, archived_at TEXT, consolidated_into TEXT,
      consolidated_at TEXT, updated TEXT, federation_source TEXT, metadata TEXT, category TEXT, subcategory TEXT);
      INSERT INTO memories (id, content, content_hash, owner_id, namespace) VALUES
      ('a', 'note', 'hash', 'alice', 'notes'), ('b', 'note', 'hash', 'alice', 'notes');""")
    repo = object.__new__(sq.SqliteMemoryRepository)

    async def discover(tx, **kwargs):
        if change == "keeper":
            conn.execute("UPDATE memories SET deleted_at=CURRENT_TIMESTAMP WHERE id='a'")
        else:
            column = {"content": "content", "owner": "owner_id", "namespace": "namespace"}[change]
            conn.execute(f"UPDATE memories SET {column}='revised' WHERE id='b'")
        return [
            dict(owner_id="alice", namespace="notes", content_hash="hash", memory_ids=["a", "b"], duplicate_count=2)
        ]

    repo.find_duplicate_content_groups = discover

    @asynccontextmanager
    async def transactional():
        yield sq.SqliteTransaction(conn)

    backend = SimpleNamespace(memories=repo, transactional=transactional)
    audit = AsyncMock()
    events = AsyncMock(return_value=([], []))
    monkeypatch.setattr(memory_dedup, "_maybe_write_delete_audit", audit)
    monkeypatch.setattr(memory_dedup, "_dispatch_delete_side_effects", events)
    try:
        if operation == "delete":
            report = await memory_dedup.dedup_exact_content(backend, apply=True)
            assert report["deleted_count"] == 0
        else:
            report = await artemis_dedup.sweep_duplicate_content(backend, auto_merge=True)
            assert report["merged_count"] == 0
        assert conn.execute("SELECT deleted_at FROM memories WHERE id='b'").fetchone()["deleted_at"] is None
        audit.assert_not_awaited()
        events.assert_not_awaited()
    finally:
        conn.close()
