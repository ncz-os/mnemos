"""MORPHEUS REPLAY ABC coverage (item 11d).

The pre-11d runner executed one Postgres-only COUNT query directly through
``asyncpg.Pool``.  REPLAY has no vector work: this test exercises the count
contract against the real SQLite implementation and separately pins the
runner's dispatch to the backend-owned transactional surface.
"""
from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from mnemos.domain.morpheus import runner
from mnemos.persistence.db2 import Db2MorpheusRepository
from mnemos.persistence.mariadb import MariadbMorpheusRepository
from mnemos.persistence.mysql import MysqlMorpheusRepository
from mnemos.persistence.oracle import OracleMorpheusRepository
from mnemos.persistence.postgres import PostgresMorpheusRepository
from mnemos.persistence.sqlite import (
    SqliteBackend,
    SqliteMorpheusRepository,
    SqliteTransaction,
    _execute as _sqlite_execute,
)


async def _insert_memory(backend: SqliteBackend, tx, memory_id: str, *, namespace: str) -> None:
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=f"body for {memory_id}",
        category="solutions",
        subcategory=None,
        metadata_json="{}",
        quality_rating=50,
        owner_id="test-owner",
        namespace=namespace,
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=None,
        created=None,
        updated=None,
    )


@pytest.mark.asyncio
async def test_sqlite_replay_scan_count_applies_window_provenance_and_namespace(tmp_path):
    """The count retains the original inclusive window/filter contract."""
    backend = SqliteBackend(tmp_path / "morpheus_replay.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        run_id = str(uuid.uuid4())
        ids = {label: f"mem_{label}_{uuid.uuid4().hex[:8]}" for label in range(5)}
        async with backend.transactional() as tx:
            for memory_id in ids.values():
                await _insert_memory(backend, tx, memory_id, namespace="tenant-a")
            assert isinstance(tx, SqliteTransaction)
            await _sqlite_execute(
                tx.conn,
                """
                INSERT INTO morpheus_runs (
                    id, triggered_by, started_at, window_started_at,
                    window_ended_at, window_hours, cluster_min_size,
                    config, namespace, status
                )
                VALUES (?, 'test', CURRENT_TIMESTAMP, ?, ?, 168, 1, '{}', ?, 'running')
                """,
                (run_id, "2025-01-01 00:00:00", "2025-01-02 00:00:00", "tenant-a"),
            )
            # Included at both endpoints: SQLite's BETWEEN matches the
            # original Postgres inclusive-window semantics.
            await _sqlite_execute(
                tx.conn,
                "UPDATE memories SET created = ? WHERE id = ?",
                ("2025-01-01 00:00:00", ids[0]),
            )
            await _sqlite_execute(
                tx.conn,
                "UPDATE memories SET created = ? WHERE id = ?",
                ("2025-01-02 00:00:00", ids[1]),
            )
            await _sqlite_execute(
                tx.conn,
                "UPDATE memories SET created = ?, provenance = 'morpheus_local' WHERE id = ?",
                ("2025-01-01 12:00:00", ids[2]),
            )
            await _sqlite_execute(
                tx.conn,
                "UPDATE memories SET created = ?, morpheus_run_id = ? WHERE id = ?",
                ("2025-01-01 12:00:00", run_id, ids[3]),
            )
            await _sqlite_execute(
                tx.conn,
                "UPDATE memories SET created = ?, namespace = 'tenant-b' WHERE id = ?",
                ("2025-01-01 12:00:00", ids[4]),
            )
            count = await backend.morpheus.replay_scan_count(tx, run_id=run_id)
        assert count == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_phase_replay_dispatches_count_through_abc(monkeypatch):
    """REPLAY must not dereference the backwards-compatible pool argument."""
    calls: list[tuple[str, object]] = []

    class _Morpheus:
        async def replay_scan_count(self, tx, *, run_id):
            calls.append(("scan", (tx, run_id)))
            return 7

        async def update_counters(self, tx, run_id, **kwargs):
            calls.append(("counters", (tx, run_id, kwargs)))

    class _Backend:
        morpheus = _Morpheus()

        @asynccontextmanager
        async def transactional(self):
            yield "transaction-token"

    from mnemos.core import lifecycle

    monkeypatch.setattr(lifecycle, "_persistence_backend", _Backend())
    assert await runner.phase_replay(object(), "run-11d") == 7
    assert calls[0] == ("scan", ("transaction-token", "run-11d"))
    assert calls[1][0] == "counters"
    assert calls[1][1][0:2] == ("transaction-token", "run-11d")
    assert calls[1][1][2]["memories_scanned"] == 7


def test_replay_scan_count_uses_item_11b_dialect_and_inheritance_precedent():
    """Pin all six backend paths without requiring live external engines."""
    postgres = inspect.getsource(PostgresMorpheusRepository.replay_scan_count)
    mysql = inspect.getsource(MysqlMorpheusRepository.replay_scan_count)
    oracle = inspect.getsource(OracleMorpheusRepository.replay_scan_count)

    assert "IS DISTINCT FROM 'morpheus_local'" in postgres
    assert "IS DISTINCT FROM 'morpheus_local'" in inspect.getsource(
        SqliteMorpheusRepository.replay_scan_count
    )
    assert "NOT (m.provenance <=> 'morpheus_local')" in mysql
    assert "NOT (m.provenance = 'morpheus_local' OR m.provenance IS NULL)" in oracle
    assert MariadbMorpheusRepository.replay_scan_count is MysqlMorpheusRepository.replay_scan_count
    assert Db2MorpheusRepository.replay_scan_count is OracleMorpheusRepository.replay_scan_count
