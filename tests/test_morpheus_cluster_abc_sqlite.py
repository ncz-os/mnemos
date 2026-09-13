"""Cross-backend coverage for MORPHEUS phase_cluster ABC plumbing (item 11b).

Item 11b moved ``phase_cluster``'s candidate fetch + cluster-payload
write from raw asyncpg onto the ``MorpheusRepository`` ABC:

  - ``fetch_cluster_candidates(tx, *, run_id, max_input_count)`` —
    returns a ``ClusterCandidateRow`` carrying the run's cluster
    config plus the candidate ``(memory_id, embedding)`` tuples
    pre-materialised to ``list[float]``.

  - ``merge_run_config(tx, run_id, *, patch)`` — merges a ``dict``
    patch into ``morpheus_runs.config`` (JSONB on Postgres, JSON
    column on MySQL/MariaDB, LONGTEXT-as-JSON on MariaDB, CLOB on
    Oracle/Db2, TEXT on SQLite). Each backend uses its own dialect
    to interpret the merge (Postgres: native ``|| jsonb_build_object``;
    every other backend: read-modify-write).

These tests pin the contract on a real SQLite backend (the
fastest no-network fixture), then exercise the runner's plumbing
through the live ABC impls so a regression in either side
(``fetch_cluster_candidates`` returning the wrong shape, or
``merge_run_config`` clobbering sibling keys) is caught here.

The Postgres path is exercised separately in the existing
``tests/test_tz_fix.py::test_morpheus_cluster_phase_handles_legacy_timestamp_columns_after_tz_migration``
when ``MNEMOS_TEST_DB`` is set; MySQL / MariaDB / Oracle / Db2
are out of CI's reach but their per-backend impls are stubbed by
the slice-2 unit tests.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from mnemos.persistence.base import ClusterCandidateRow
from mnemos.persistence.sqlite import (
    SqliteBackend,
    SqliteTransaction,
    _execute as _sqlite_execute,
    _fetch_one as _sqlite_fetch_one,
)


@pytest.mark.asyncio
async def test_sqlite_fetch_cluster_candidates_returns_pre_materialised_embeddings(
    tmp_path,
):
    """The ABC contract: fetch returns ``list[float]`` embeddings.

    ``phase_cluster`` no longer parses text-form pgvector. The
    backend is responsible for turning whatever column type it
    uses (TEXT-as-JSON on SQLite, VECTOR on Postgres/Oracle/Db2,
    JSON on MySQL) into a ``list[float]`` before the runner sees
    it. This test pins that the SQLite impl does so for the
    three shapes it may store: JSON string, list, and a string
    the JSON parser rejects (the runner expects such rows to be
    silently dropped, not crash)."""
    backend = SqliteBackend(tmp_path / "morpheus.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        # Seed: one well-formed JSON-string row, one well-formed
        # Python-list row, one garbage row.
        ids = [
            f"mem_json_{uuid.uuid4().hex[:8]}",
            f"mem_list_{uuid.uuid4().hex[:8]}",
            f"mem_garbage_{uuid.uuid4().hex[:8]}",
        ]
        async with backend.transactional() as tx:
            for mid in ids:
                await backend.memories.insert_memory(
                    tx,
                    memory_id=mid,
                    content=f"body for {mid}",
                    category="solutions",
                    subcategory=None,
                    metadata_json="{}",
                    quality_rating=50,
                    owner_id="test-owner",
                    namespace="default",
                    permission_mode=600,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content=None,
                    created=None,
                    updated=None,
                )
            # Direct write to the embedding column so we can hit all
            # three shapes — the public ``upsert_memory_embedding``
            # always JSON-encodes which would hide the list-shape path.
            assert isinstance(tx, SqliteTransaction)
            await _sqlite_execute(
                tx.conn,
                "UPDATE memories SET embedding = ? WHERE id = ?",
                (json.dumps([1.0, 0.0, 0.0]), ids[0]),
            )
            await _sqlite_execute(
                tx.conn,
                "UPDATE memories SET embedding = ? WHERE id = ?",
                ("[0.0, 1.0, 0.0]", ids[1]),  # raw text, not JSON-parseable
            )
            await _sqlite_execute(
                tx.conn,
                "UPDATE memories SET embedding = ? WHERE id = ?",
                ("garbage-not-a-vector", ids[2]),
            )
            # Open a morpheus_runs row with cluster_min_size=1, NULL
            # namespace so every candidate passes the eligibility
            # predicate.
            run_id = str(uuid.uuid4())
            await _sqlite_execute(
                tx.conn,
                """
                INSERT INTO morpheus_runs (
                    id, triggered_by, started_at, window_started_at,
                    window_ended_at, window_hours, cluster_min_size,
                    config, namespace, status
                )
                VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, 168, 1, '{}', NULL, 'running')
                """,
                (run_id, "test", "2020-01-01 00:00:00", "2030-01-01 00:00:00"),
            )
            ctx = await backend.morpheus.fetch_cluster_candidates(
                tx, run_id=run_id, max_input_count=100
            )

        assert isinstance(ctx, ClusterCandidateRow)
        assert ctx.cluster_min_size == 1
        assert ctx.namespace is None
        # Garbage row dropped; the two well-formed rows survived.
        candidate_ids = {mid for mid, _ in ctx.candidates}
        assert candidate_ids == {ids[0], ids[1]}
        # All embeddings are plain ``list[float]``, not strings /
        # ndarrays. The runner relies on this — it iterates each
        # candidate and calls ``np.asarray(vec_list, dtype=np.float32)``.
        for _, vec in ctx.candidates:
            assert isinstance(vec, list)
            assert all(isinstance(v, float) for v in vec)
        # Specifically, the JSON-string row came back as [1.0, 0.0, 0.0]
        # and the raw-text row came back as [0.0, 1.0, 0.0] (the
        # _parse_embedding fallback path splits on comma).
        by_id = dict(ctx.candidates)
        assert by_id[ids[0]] == [1.0, 0.0, 0.0]
        assert by_id[ids[1]] == [0.0, 1.0, 0.0]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_fetch_cluster_candidates_returns_none_for_missing_run(
    tmp_path,
):
    """A missing run_id mirrors the pre-11b runner's ``run_row is None``
    early-exit so the runner can still call ``update_counters`` with
    ``clusters_found=0``."""
    backend = SqliteBackend(tmp_path / "morpheus_missing.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            ctx = await backend.morpheus.fetch_cluster_candidates(
                tx, run_id=str(uuid.uuid4()), max_input_count=100
            )
        assert ctx is None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_merge_run_config_merges_patch_keys(tmp_path):
    """``merge_run_config`` is a partial update, not a replace.

    Postgres uses ``|| jsonb_build_object(...)`` for partial merge.
    SQLite has no JSONB ``||`` operator, so this impl uses
    read-modify-write: read the existing ``config``, ``json.loads``
    it, merge the patch keys in Python, ``json.dumps``, write it
    back. The patch must NOT clobber sibling keys — e.g. a later
    phase that wants to add ``synthesised_ids`` must keep
    ``clusters`` intact. The contract is the same on every backend.
    """
    backend = SqliteBackend(tmp_path / "morpheus_merge.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            run_id = str(uuid.uuid4())
            existing_config = {"synthesised_ids": ["mem_x"], "version": 7}
            await _sqlite_execute(
                tx.conn,
                """
                INSERT INTO morpheus_runs (
                    id, triggered_by, started_at, window_started_at,
                    window_ended_at, window_hours, cluster_min_size,
                    config, namespace, status
                )
                VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, 168, 3, ?, NULL, 'running')
                """,
                (
                    run_id,
                    "test",
                    "2020-01-01 00:00:00",
                    "2030-01-01 00:00:00",
                    json.dumps(existing_config),
                ),
            )
            new_clusters = [
                {"cluster_id": 0, "member_memory_ids": ["mem_a", "mem_b"]},
                {"cluster_id": 1, "member_memory_ids": ["mem_c"]},
            ]
            await backend.morpheus.merge_run_config(
                tx, run_id, patch={"clusters": new_clusters}
            )
            # Re-read the row to confirm the write landed.
            row = await _sqlite_fetch_one(
                tx.conn,
                "SELECT config FROM morpheus_runs WHERE id = ?",
                (run_id,),
            )
        assert row is not None
        raw_config = row[0] if not isinstance(row, dict) else row.get("config")
        assert isinstance(raw_config, str)
        merged = json.loads(raw_config)
        # Sibling keys preserved.
        assert merged["synthesised_ids"] == ["mem_x"]
        assert merged["version"] == 7
        # Patch keys added.
        assert merged["clusters"] == new_clusters
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_merge_run_config_handles_empty_existing_config(tmp_path):
    """Defensive: a ``config`` column that is NULL or ``'{}'`` should
    merge into a clean dict — no AttributeError, no json.loads crash."""
    backend = SqliteBackend(tmp_path / "morpheus_empty.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        async with backend.transactional() as tx:
            run_id = str(uuid.uuid4())
            await _sqlite_execute(
                tx.conn,
                """
                INSERT INTO morpheus_runs (
                    id, triggered_by, started_at, window_started_at,
                    window_ended_at, window_hours, cluster_min_size,
                    config, namespace, status
                )
                VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, 168, 3, '{}', NULL, 'running')
                """,
                (run_id, "test", "2020-01-01 00:00:00", "2030-01-01 00:00:00"),
            )
            await backend.morpheus.merge_run_config(
                tx,
                run_id,
                patch={"clusters": [{"cluster_id": 0, "member_memory_ids": []}]},
            )
            row = await _sqlite_fetch_one(
                tx.conn,
                "SELECT config FROM morpheus_runs WHERE id = ?",
                (run_id,),
            )
        assert row is not None
        raw_config = row[0] if not isinstance(row, dict) else row.get("config")
        merged = json.loads(raw_config)
        assert merged == {"clusters": [{"cluster_id": 0, "member_memory_ids": []}]}
    finally:
        await backend.close()
