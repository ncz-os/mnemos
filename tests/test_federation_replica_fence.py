"""F-3: local lifecycle workers must not mutate federated (replica) rows.

The Fable federation report (fleet-ops ``docs/design/mnemos-code-health-
decisions-20261031.md``, section F2/F5) found that every *local* mutator --
PERSEPHONE archival, ARTEMIS content-hash dedup, MORPHEUS consolidation and
the deletion sweeps -- selects rows without a ``federation_source`` guard, and
that the receiver's durable sequence fence then makes the damage permanent: a
replayed event with an already-seen sequence is rejected before any write, so
a replica that a local worker soft-deleted or archived can never re-apply the
source's state. On the PROTEUS replica that surfaced as 0.7 % of exportable
rows answering ``404`` on the by-id path.

Two behaviours are pinned here, both behind ``MNEMOS_FEDERATION_REPLICA_FENCE``
(default on, the safe side):

* ``exclude`` -- local sweeps skip replicas (a replica converges only from its
  source, never from a local heuristic);
* ``resurrect`` -- when the same source state IS replayed (recovery, a re-seed,
  a peer that re-numbers its journal), the receiver restores the local
  lifecycle damage instead of fencing the event forever.

SQLite only: the fix is one shared predicate plus one shared repository
method, and each dialect's SQL shape is pinned in
``tests/test_federation_replica_fence_sql.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.domain.federation import _store_memories
from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

PEER = "peer"
LOCAL_ID = f"fed:{PEER}:remote"
FENCE_ENV = "MNEMOS_FEDERATION_REPLICA_FENCE"


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    monkeypatch.delenv(FENCE_ENV, raising=False)
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    from mnemos.persistence.sqlite import SqliteBackend

    instance = SqliteBackend(tmp_path / "replica_fence.db", settings)
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


def needs_zstd(fn):
    """PERSEPHONE archives through zstd; the persephone extra is optional."""
    import functools
    import importlib.util

    if importlib.util.find_spec("zstandard") is not None:
        return fn

    @functools.wraps(fn)
    async def skip(*args, **kwargs):
        pytest.skip("zstandard (persephone extra) not installed")

    return skip


def memory(seq, content="replica facts"):
    return {
        "id": "remote",
        "content": content,
        "category": "facts",
        "namespace": "A",
        "created": "2020-01-01T00:00:00Z",
        "updated": "2020-01-01T00:00:00Z",
        "federation_sequence": seq,
    }


async def replay(backend, event):
    async with backend.transactional() as tx:
        return await _store_memories(backend.federation, tx, PEER, [event])


async def run(backend, statement, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).execute(statement, *params)


async def row(backend):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).fetchone(
            "SELECT id, content, deleted_at, archived_at, consolidated_into, federation_source "
            "FROM memories WHERE id = ?",
            LOCAL_ID,
        )


# ── resurrect: a replayed sequence restores a locally damaged replica ────────


@pytest.mark.asyncio
async def test_replayed_event_restores_a_soft_deleted_replica(backend):
    await replay(backend, memory(1))
    # A local sweep (deletion worker, dedup consolidation) soft-deletes the row;
    # nothing on the receiver records that as a local decision.
    await run(backend, "UPDATE memories SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", LOCAL_ID)
    assert (await row(backend))["deleted_at"] is not None

    # The source sends the same state again: same content, same timestamp, same
    # sequence (a re-seed, a restored backup, or a peer that re-numbers).
    await replay(backend, memory(1))

    restored = await row(backend)
    assert restored["deleted_at"] is None
    assert restored["archived_at"] is None


@pytest.mark.asyncio
async def test_replayed_event_restores_an_archived_replica(backend):
    """PERSEPHONE replaces the body with an archive marker and sets archived_at."""
    await replay(backend, memory(1))
    await run(
        backend,
        "UPDATE memories SET content = ?, archived_at = CURRENT_TIMESTAMP WHERE id = ?",
        f"ARCHIVED:{LOCAL_ID}",
        LOCAL_ID,
    )

    await replay(backend, memory(1))

    restored = await row(backend)
    assert restored["archived_at"] is None
    assert restored["content"] == "replica facts"


@pytest.mark.asyncio
async def test_replayed_event_restores_a_consolidated_replica(backend):
    # ``consolidated_into`` is a FK to a real memory row, so the canonical has
    # to exist: a local row is exactly what MORPHEUS would have pointed at.
    await replay(backend, memory(1))
    await run(
        backend,
        "INSERT INTO memories (id, content, category, owner_id, namespace, permission_mode, "
        "content_hash, created, updated) VALUES ('local_canonical', 'canonical', 'facts', "
        "'alice', 'A', 644, 'canonical-hash', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
    )
    await run(
        backend,
        "UPDATE memories SET consolidated_into = 'local_canonical', deleted_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        LOCAL_ID,
    )

    await replay(backend, memory(1))

    restored = await row(backend)
    assert restored["consolidated_into"] is None
    assert restored["deleted_at"] is None


@pytest.mark.asyncio
async def test_resurrect_skips_a_row_the_replica_already_holds(backend):
    """The fence still rejects a replayed sequence it already applied.

    The repair exists for LOCAL damage. A row that is already in its source
    state must keep the fast fence path: no second write, no second count, and
    no ``updated`` bump that would echo back to the peer.
    """
    await replay(backend, memory(1))
    before = await row(backend)

    new_n, upd_n = await replay(backend, memory(1))

    assert (new_n, upd_n) == (0, 0)
    after = await row(backend)
    assert after["content"] == before["content"]
    assert after["deleted_at"] is None


@pytest.mark.asyncio
async def test_concurrent_replays_repair_once(backend):
    """Two consumers of the same batch must not both resurrect (or double count)."""
    await replay(backend, memory(1))
    await run(backend, "UPDATE memories SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", LOCAL_ID)

    results = await asyncio.gather(replay(backend, memory(1)), replay(backend, memory(1)))

    assert (await row(backend))["deleted_at"] is None
    assert sum(new + upd for new, upd in results) == 1


# ── exclude: local sweeps skip replicas ─────────────────────────────────────


async def _insert_local_and_replica(backend):
    """One locally authored row and one replica row with the same content.

    Both are old and share a content hash, so every local sweep would treat
    them identically without the fence. They also share ``owner_id``: the
    real damage on PROTEUS was a local sweep hitting a replica that happened
    to look like a local duplicate, and a sweep only ever sees rows in the
    same owner scope. (With different owners the dedup GROUP BY never forms a
    group at all, so the fence would not be the thing under test.)
    """
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        for mid, source in (("local_row", None), (LOCAL_ID, PEER)):
            await ops.execute(
                "INSERT INTO memories (id, content, category, owner_id, namespace, "
                "permission_mode, content_hash, created, updated) "
                "VALUES (?, ?, 'facts', ?, 'A', 644, ?, ?, ?)",
                mid,
                "identical replica facts",
                "alice",
                "shared-hash",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
            )
            if source is not None:
                await ops.execute("UPDATE memories SET federation_source = ? WHERE id = ?", source, mid)


@pytest.mark.asyncio
@needs_zstd
async def test_persephone_archival_skips_replicas(backend):
    """The sweep may archive the local row; the replica is the source's.

    An archive marker destroys the body and the source never sends the row
    again (its journal sequence is already fenced), so the replica would be
    lost until a full re-seed.
    """
    from mnemos.persistence.worker_lifecycle import sweep_for_archival

    await _insert_local_and_replica(backend)
    archived = await sweep_for_archival(backend, namespace="A", archive_after_days=30, batch_size=10)

    assert archived == 1
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        replica = await ops.fetchone("SELECT archived_at FROM memories WHERE id = ?", LOCAL_ID)
        local = await ops.fetchone("SELECT archived_at FROM memories WHERE id = 'local_row'")
    assert replica["archived_at"] is None
    assert local["archived_at"] is not None


@pytest.mark.asyncio
@needs_zstd
async def test_persephone_already_archived_replica_is_not_archived_twice(backend):
    """A replica archived by an older build must not be re-archived."""
    from mnemos.persistence.worker_lifecycle import sweep_for_archival

    await _insert_local_and_replica(backend)
    await run(backend, "UPDATE memories SET archived_at = CURRENT_TIMESTAMP WHERE id = ?", LOCAL_ID)
    archived = await sweep_for_archival(backend, namespace="A", archive_after_days=30, batch_size=10)
    assert archived == 1
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        row_ = await ops.fetchone("SELECT content FROM memories WHERE id = ?", LOCAL_ID)
    assert row_["content"] == "identical replica facts"


@pytest.mark.asyncio
async def test_artemis_dedup_groups_exclude_replicas(backend):
    """A replica never appears in a dedup group.

    With the fence on the pair is no longer a duplicate pair at all (one
    local row is not a group), so the correct assertion is absence: ARTEMIS
    must not be handed the replica as a merge candidate.
    """
    await _insert_local_and_replica(backend)
    async with backend.transactional() as tx:
        groups = await backend.memories.find_duplicate_content_groups(tx)
    ids = [str(mid) for group in groups for mid in group["memory_ids"]]
    assert LOCAL_ID not in ids, f"a replica must never be deduped into a local row: {groups}"

    # The local row is still a candidate when it genuinely has a local twin.
    async with backend.transactional() as tx:
        await _Ops(tx, transaction_dialect(tx)).execute(
            "UPDATE memories SET id = 'local_row_2', created = '2021-01-01T00:00:00+00:00' "
            "WHERE id = 'local_row'"
        )
    async with backend.transactional() as tx:
        await _Ops(tx, transaction_dialect(tx)).execute(
            "INSERT INTO memories (id, content, category, owner_id, namespace, "
            "permission_mode, content_hash, created, updated) "
            "VALUES ('local_row', 'identical replica facts', 'facts', 'alice', 'A', 644, "
            "'shared-hash', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
        )
    async with backend.transactional() as tx:
        groups = await backend.memories.find_duplicate_content_groups(tx)
    ids = [str(mid) for group in groups for mid in group["memory_ids"]]
    assert {"local_row", "local_row_2"} <= set(ids)
    assert LOCAL_ID not in ids


@pytest.mark.asyncio
async def test_artemis_consolidate_refuses_a_non_local_canonical(backend):
    """Consolidation needs a local canonical row to point at."""
    await _insert_local_and_replica(backend)
    async with backend.transactional() as tx:
        merged = await backend.memories.consolidate_duplicate_memories(
            tx,
            canonical_id=LOCAL_ID,
            duplicate_ids=["local_row"],
        )
    assert merged == 0
    async with backend.transactional() as tx:
        local = await _Ops(tx, transaction_dialect(tx)).fetchone(
            "SELECT consolidated_into FROM memories WHERE id = 'local_row'"
        )
    assert local["consolidated_into"] is None


@pytest.mark.asyncio
async def test_morpheus_consolidation_skips_replicas(backend):
    """MORPHEUS rewrites permission_mode and metadata: a local, derived decision."""
    await _insert_local_and_replica(backend)
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO morpheus_runs (id, owner_id, namespace, status, config, cluster_min_size) "
            "VALUES ('run1', 'default', 'A', 'running', ?, 2)",
            '{"clusters": [{"cluster_id": "c1", "member_memory_ids": ["local_row", "'
            + LOCAL_ID
            + '"]}]}',
        )
        result = await backend.morpheus.phase_consolidate(
            tx, run_id="run1", consolidated_permission_mode=600
        )
        replica = await ops.fetchone(
            "SELECT consolidated_into, permission_mode FROM memories WHERE id = ?", LOCAL_ID
        )
    assert replica["consolidated_into"] is None
    # The cluster fell below cluster_min_size once the replica was excluded.
    assert result.memories_consolidated == 0


@pytest.mark.asyncio
async def test_morpheus_window_scan_excludes_replicas(backend):
    """CLUSTER and REPLAY share ``eligible_for_morpheus``; both must skip replicas."""
    await _insert_local_and_replica(backend)
    await run(
        backend,
        "UPDATE memories SET embedding = ? WHERE id IN ('local_row', ?)",
        "[0.1,0.2,0.3]",
        LOCAL_ID,
    )
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO morpheus_runs (id, owner_id, namespace, status, config, cluster_min_size, "
            "window_started_at, window_ended_at) VALUES ('run2', 'default', 'A', 'running', '{}', 2, ?, ?)",
            "2019-01-01T00:00:00+00:00",
            "2021-01-01T00:00:00+00:00",
        )
        batch = await backend.morpheus.fetch_cluster_candidates(tx, run_id="run2", max_input_count=10)
        replay_count = await backend.morpheus.replay_scan_count(tx, run_id="run2")
    assert batch is not None
    ids = [str(mid) for mid, _vector in batch.candidates]
    assert "local_row" in ids
    assert LOCAL_ID not in ids, f"a replica must never enter a morpheus cluster: {ids}"
    assert replay_count == 1


# ── the switch ──────────────────────────────────────────────────────────────


def test_fence_values_parse_as_a_bool(monkeypatch):
    from mnemos.core.config import federation_replica_fence_enabled

    monkeypatch.delenv(FENCE_ENV, raising=False)
    assert federation_replica_fence_enabled() is True  # default: the safe side
    for raw, expected in (
        ("0", False),
        ("off", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("on", True),
        ("yes", True),
        ("true", True),
    ):
        monkeypatch.setenv(FENCE_ENV, raw)
        assert federation_replica_fence_enabled() is expected, raw


def test_switch_toggle_changes_the_shared_predicate(monkeypatch):
    from mnemos.core import eligibility

    monkeypatch.setenv(FENCE_ENV, "on")
    assert "federation_source IS NULL" in eligibility.local_sweep_predicate("m")
    monkeypatch.setenv(FENCE_ENV, "off")
    assert "federation_source" not in eligibility.local_sweep_predicate("m")


@pytest.mark.asyncio
async def test_fence_off_restores_the_previous_sweeps(backend, monkeypatch):
    """An operator who opts out gets the pre-fix selection exactly.

    That is the documented fallback: a deployment that deliberately reconciles
    replicas locally must be able to say so, and must get the old behaviour
    back rather than a silent half-fence.
    """
    monkeypatch.setenv(FENCE_ENV, "off")
    await _insert_local_and_replica(backend)
    async with backend.transactional() as tx:
        groups = await backend.memories.find_duplicate_content_groups(tx)
    ids = [str(mid) for group in groups for mid in group["memory_ids"]]
    assert LOCAL_ID in ids
    assert "local_row" in ids
