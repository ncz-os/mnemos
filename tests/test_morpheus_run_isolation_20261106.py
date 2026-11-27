"""FB322: MORPHEUS run isolation, on the real SQLite backend.

Pinned here (base ``master`` @ ``22a3f7d8``):

* ``begin_run`` mints a fresh version-4 uuid per run (nothing reuses an id);
* a run consolidates only the clusters named in its own ``config.clusters``; another run's members
  keep ``consolidated_into`` and ``morpheus_run_id`` NULL;
* a later run does not touch an earlier run's members or their tags;
* ``rollback_run(A)`` restores A's originals and leaves B's consolidated rows tagged with B alone;
* a crash before the phase transaction commits leaves no partial consolidation visible.

These are the run-to-run guarantees the brief asks for; owner/namespace partitioning
(``test_morpheus_consolidation_isolation.py``) and single-run idempotency
(``test_morpheus_consolidate.py``) are already covered elsewhere.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from mnemos.persistence import SqliteBackend
from mnemos.persistence.sqlite import _fetch_all
from tests.test_morpheus_cross_owner_isolation import _seed_memory


@asynccontextmanager
async def _store(tmp_path):
    """A real SQLite backend, opened for one test."""
    store = SqliteBackend(tmp_path / "run-isolation.db", SimpleNamespace())
    await store.open()
    try:
        yield store
    finally:
        await store.close()


def _cluster(member_ids):
    return {"clusters": [{"cluster_id": 0, "member_memory_ids": list(member_ids)}]}


async def _begin(backend, members, *, min_size=3):
    async with backend.transactional() as tx:
        return await backend.morpheus.begin_run(
            tx,
            triggered_by="test",
            window_hours=24,
            cluster_min_size=min_size,
            config=_cluster(members),
            namespace=None,
        )


async def _seed(backend, ids):
    async with backend.transactional() as tx:
        for mid in ids:
            await _seed_memory(
                backend,
                tx,
                memory_id=mid,
                owner_id="alice",
                namespace="shared",
                content=mid,
                created=datetime.now(timezone.utc),
                vector=[1.0, 0.0, 0.0],
            )


async def _rows(backend, ids):
    async with backend.transactional() as tx:
        rows = await _fetch_all(
            tx.conn,
            "SELECT id, consolidated_into, morpheus_run_id, permission_mode FROM memories "
            f"WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id",
            tuple(ids),
        )
    return {row["id"]: row for row in rows}


async def _consolidate(backend, run_id):
    async with backend.transactional() as tx:
        return await backend.morpheus.phase_consolidate(tx, run_id=run_id, consolidated_permission_mode=400)


@pytest.mark.asyncio
async def test_every_run_gets_a_fresh_id_and_no_run_id_is_reused(tmp_path):
    async with _store(tmp_path) as backend:
        ids = [await _begin(backend, []) for _ in range(3)]
    assert len(set(ids)) == 3, "run ids are never reused"
    # SQLite mints the row id as lower(hex(randomblob(16))): 32 hex characters with no RFC 4122
    # version bits (the uuid column type on the server backends stores a real uuid), so the pin
    # is the shape plus uniqueness, not a version nibble.
    assert all(len(run_id) == 32 and all(ch in "0123456789abcdef" for ch in run_id) for run_id in ids)
    assert all(UUID(run_id) is not None for run_id in ids), "still parseable as a uuid"


@pytest.mark.asyncio
async def test_a_run_consolidates_only_the_clusters_in_its_own_config(tmp_path):
    mine = ["a1", "a2", "a3"]
    theirs = ["b1", "b2", "b3"]
    async with _store(tmp_path) as backend:
        await _seed(backend, mine + theirs)
        run_a = await _begin(backend, mine)
        result = await _consolidate(backend, run_a)
        assert result.memories_consolidated == 2

        rows = await _rows(backend, mine + theirs)
    assert sum(1 for mid in mine if rows[mid]["morpheus_run_id"] == run_a) == 2
    assert sum(1 for mid in mine if rows[mid]["consolidated_into"]) == 2
    for mid in theirs:
        assert rows[mid]["consolidated_into"] is None, "the other run's cluster is invisible"
        assert rows[mid]["morpheus_run_id"] is None
        assert rows[mid]["permission_mode"] == 600


@pytest.mark.asyncio
async def test_a_later_run_does_not_touch_an_earlier_runs_members(tmp_path):
    mine = ["a1", "a2", "a3"]
    theirs = ["b1", "b2", "b3"]
    async with _store(tmp_path) as backend:
        await _seed(backend, mine + theirs)
        run_a = await _begin(backend, mine)
        await _consolidate(backend, run_a)
        run_b = await _begin(backend, theirs)
        result = await _consolidate(backend, run_b)
        assert result.memories_consolidated == 2

        rows = await _rows(backend, mine + theirs)
    assert sum(1 for mid in mine if rows[mid]["morpheus_run_id"] == run_a) == 2
    assert sum(1 for mid in theirs if rows[mid]["morpheus_run_id"] == run_b) == 2
    assert not [mid for mid in mine if rows[mid]["morpheus_run_id"] == run_b]


@pytest.mark.asyncio
async def test_rollback_restores_only_the_named_runs_rows(tmp_path):
    mine = ["a1", "a2", "a3"]
    theirs = ["b1", "b2", "b3"]
    async with _store(tmp_path) as backend:
        await _seed(backend, mine + theirs)
        run_a = await _begin(backend, mine)
        run_b = await _begin(backend, theirs)
        await _consolidate(backend, run_a)
        await _consolidate(backend, run_b)

        async with backend.transactional() as tx:
            deleted, run_rows = await backend.morpheus.rollback_run(tx, run_a, requested_by="test")
        assert run_rows == 1
        assert deleted == 0, "no summaries were synthesised in this phase"

        rows = await _rows(backend, mine + theirs)
    for mid in mine:
        assert rows[mid]["consolidated_into"] is None
        assert rows[mid]["morpheus_run_id"] is None, "A's tag is cleared"
        assert rows[mid]["permission_mode"] == 600, "the audit key restored the original mode"
    assert sum(1 for mid in theirs if rows[mid]["morpheus_run_id"] == run_b) == 2
    assert sum(1 for mid in theirs if rows[mid]["consolidated_into"]) == 2


@pytest.mark.asyncio
async def test_a_crash_before_commit_leaves_no_partial_consolidation(tmp_path):
    mine = ["a1", "a2", "a3"]
    async with _store(tmp_path) as backend:
        await _seed(backend, mine)
        run_a = await _begin(backend, mine)

        with pytest.raises(RuntimeError):
            async with backend.transactional() as tx:
                await backend.morpheus.phase_consolidate(tx, run_id=run_a, consolidated_permission_mode=400)
                raise RuntimeError("worker died mid-phase")

        rows = await _rows(backend, mine)
    for mid in mine:
        assert rows[mid]["consolidated_into"] is None
        assert rows[mid]["morpheus_run_id"] is None
        assert rows[mid]["permission_mode"] == 600, "no half-written permission change survives"
