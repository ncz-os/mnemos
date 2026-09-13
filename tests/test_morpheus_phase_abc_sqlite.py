"""Behavioral contract tests for MORPHEUS item 11c on real SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mnemos.persistence.base import MorpheusExtractCandidate
from mnemos.persistence.sqlite import (
    SqliteBackend,
    SqliteTransaction,
    _execute as _sqlite_execute,
    _fetch_all as _sqlite_fetch_all,
    _fetch_one as _sqlite_fetch_one,
)


async def _seed_memory(
    backend: SqliteBackend,
    tx: SqliteTransaction,
    memory_id: str,
    *,
    created: datetime,
    recall_count: int = 0,
    permission_mode: int = 600,
    verbatim_content: str | None = None,
) -> None:
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=f"content for {memory_id}",
        category="facts",
        subcategory=None,
        metadata_json="{}",
        quality_rating=50,
        owner_id="owner-a",
        namespace="A",
        permission_mode=permission_mode,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=verbatim_content,
        created=created,
        updated=created,
    )
    await _sqlite_execute(
        tx.conn,
        "UPDATE memories SET recall_count = ? WHERE id = ?",
        (recall_count, memory_id),
    )


async def _seed_run(
    tx: SqliteTransaction,
    run_id: str,
    *,
    config: dict,
    cluster_min_size: int = 2,
) -> None:
    await _sqlite_execute(
        tx.conn,
        """
        INSERT INTO morpheus_runs (
            id, triggered_by, started_at, window_started_at, window_ended_at,
            window_hours, cluster_min_size, config, namespace, status
        ) VALUES (?, 'test', CURRENT_TIMESTAMP, ?, ?, 168, ?, ?, 'A', 'running')
        """,
        (
            run_id,
            "2020-01-01 00:00:00",
            "2030-01-01 00:00:00",
            cluster_min_size,
            json.dumps(config),
        ),
    )


@pytest.mark.asyncio
async def test_sqlite_consolidate_is_idempotent_and_rollback_restores_audit_state(
    tmp_path,
):
    backend = SqliteBackend(tmp_path / "consolidate.sqlite3", SimpleNamespace())
    await backend.open()
    run_id = str(uuid4())
    base = datetime(2026, 5, 1, 12, 0, 0)
    ids = [f"mem_{uuid4().hex[:8]}" for _ in range(3)]
    try:
        async with backend.transactional() as tx:
            assert isinstance(tx, SqliteTransaction)
            await _seed_memory(backend, tx, ids[0], created=base, recall_count=1)
            await _seed_memory(
                backend,
                tx,
                ids[1],
                created=base + timedelta(seconds=1),
                recall_count=9,
                permission_mode=640,
            )
            await _seed_memory(
                backend,
                tx,
                ids[2],
                created=base + timedelta(seconds=2),
                recall_count=2,
                permission_mode=644,
            )
            await _seed_run(
                tx,
                run_id,
                config={
                    "clusters": [
                        {"cluster_id": 0, "member_memory_ids": ids},
                    ]
                },
            )
            first = await backend.morpheus.phase_consolidate(tx, run_id=run_id, consolidated_permission_mode=400)
        async with backend.transactional() as tx:
            second = await backend.morpheus.phase_consolidate(tx, run_id=run_id, consolidated_permission_mode=400)
            rows = await _sqlite_fetch_all(
                tx.conn,
                "SELECT id, consolidated_into, permission_mode, metadata "
                "FROM memories WHERE id IN (?, ?, ?) ORDER BY id",
                tuple(ids),
            )

        assert first is not None and second is not None
        assert (first.memories_consolidated, first.clusters_consolidated) == (2, 1)
        assert (second.memories_consolidated, second.clusters_consolidated) == (2, 1)
        by_id = {row["id"]: row for row in rows}
        assert by_id[ids[1]]["consolidated_into"] is None
        for memory_id, old_mode in ((ids[0], 600), (ids[2], 644)):
            assert by_id[memory_id]["consolidated_into"] == ids[1]
            assert by_id[memory_id]["permission_mode"] == 400
            assert json.loads(by_id[memory_id]["metadata"])["pre_consolidate_permission_mode"] == old_mode

        async with backend.transactional() as tx:
            deleted, run_rows = await backend.morpheus.rollback_run(tx, run_id, requested_by="test")
            restored = await _sqlite_fetch_all(
                tx.conn,
                "SELECT id, consolidated_into, permission_mode, metadata, morpheus_run_id "
                "FROM memories WHERE id IN (?, ?, ?)",
                tuple(ids),
            )
        assert (deleted, run_rows) == (0, 1)
        restored_by_id = {row["id"]: row for row in restored}
        assert restored_by_id[ids[0]]["permission_mode"] == 600
        assert restored_by_id[ids[2]]["permission_mode"] == 644
        assert all(row["consolidated_into"] is None for row in restored)
        assert all(row["morpheus_run_id"] is None for row in restored)
        assert all("pre_consolidate_permission_mode" not in json.loads(row["metadata"]) for row in restored)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_synthesise_load_and_store_use_canonical_source_memories(tmp_path):
    backend = SqliteBackend(tmp_path / "synthesise.sqlite3", SimpleNamespace())
    await backend.open()
    run_id = str(uuid4())
    ids = [f"mem_{uuid4().hex[:8]}" for _ in range(2)]
    summary_id = f"mem_{uuid4().hex[:8]}"
    try:
        async with backend.transactional() as tx:
            assert isinstance(tx, SqliteTransaction)
            await _seed_memory(backend, tx, ids[0], created=datetime(2026, 5, 1))
            await _seed_memory(backend, tx, ids[1], created=datetime(2026, 5, 2))
            await _seed_run(
                tx,
                run_id,
                config={
                    "clusters": [
                        {"cluster_id": 7, "member_memory_ids": ids},
                    ]
                },
            )
            clusters = await backend.morpheus.phase_synthesise_load(tx, run_id=run_id)
        assert clusters is not None
        assert len(clusters) == 1
        assert {member.id for member in clusters[0].members} == set(ids)

        async with backend.transactional() as tx:
            await backend.morpheus.phase_synthesise_store(
                tx,
                memory_id=summary_id,
                content="summary",
                category="facts",
                owner_id="owner-a",
                namespace="A",
                run_id=run_id,
                source_memory_ids=ids,
                metadata={"cluster_id": 7},
            )
            row = await _sqlite_fetch_one(
                tx.conn,
                "SELECT provenance, morpheus_run_id, source_memories FROM memories WHERE id = ?",
                (summary_id,),
            )
        assert row is not None
        assert row["provenance"] == "morpheus_local"
        assert row["morpheus_run_id"] == run_id
        assert json.loads(row["source_memories"]) == ids
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_extract_dead_letter_and_atomic_store_contract(tmp_path):
    backend = SqliteBackend(tmp_path / "extract.sqlite3", SimpleNamespace())
    await backend.open()
    run_id = str(uuid4())
    failed_id = f"mem_{uuid4().hex[:8]}"
    stored_id = f"mem_{uuid4().hex[:8]}"
    prose = "A" * 120
    try:
        async with backend.transactional() as tx:
            assert isinstance(tx, SqliteTransaction)
            await _seed_memory(
                backend,
                tx,
                failed_id,
                created=datetime(2026, 5, 1),
                verbatim_content=prose,
            )
            await _seed_memory(
                backend,
                tx,
                stored_id,
                created=datetime(2026, 5, 2),
                verbatim_content=prose,
            )
            await _seed_run(tx, run_id, config={"extract": True})
            batch = await backend.morpheus.phase_extract_load(tx, run_id=run_id, min_chars=80, max_input_count=10)
        assert batch is not None
        assert {candidate.id for candidate in batch.candidates} == {failed_id, stored_id}

        async with backend.transactional() as tx:
            first = await backend.morpheus.phase_extract_failure(tx, memory_id=failed_id, max_failures=2, error="one")
        async with backend.transactional() as tx:
            second = await backend.morpheus.phase_extract_failure(tx, memory_id=failed_id, max_failures=2, error="two")
        assert first is not None and (first.attempts, first.status) == (1, "retryable")
        assert second is not None and (second.attempts, second.status) == (2, "dead_letter")

        candidate = MorpheusExtractCandidate(stored_id, prose, "owner-a", "A")
        triple_id = str(uuid4())
        async with backend.transactional() as tx:
            claimed = await backend.morpheus.phase_extract_store(
                tx,
                run_id=run_id,
                candidate=candidate,
                triples=[(triple_id, "Alice", "owns", "Helios", 0.9)],
            )
        async with backend.transactional() as tx:
            claimed_again = await backend.morpheus.phase_extract_store(
                tx, run_id=run_id, candidate=candidate, triples=[]
            )
            run = await _sqlite_fetch_one(
                tx.conn,
                "SELECT triples_extracted, memories_processed_for_extraction FROM morpheus_runs WHERE id = ?",
                (run_id,),
            )
            triple = await _sqlite_fetch_one(
                tx.conn,
                "SELECT extracted_by_run_id, memory_id FROM kg_triples WHERE id = ?",
                (triple_id,),
            )
            next_batch = await backend.morpheus.phase_extract_load(tx, run_id=run_id, min_chars=80, max_input_count=10)
        assert claimed is True
        assert claimed_again is False
        assert run is not None
        assert (run["triples_extracted"], run["memories_processed_for_extraction"]) == (
            1,
            1,
        )
        assert triple is not None
        assert (triple["extracted_by_run_id"], triple["memory_id"]) == (
            run_id,
            stored_id,
        )
        assert next_batch is not None and next_batch.candidates == ()
    finally:
        await backend.close()
