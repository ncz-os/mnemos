"""Run owner-partitioned consolidation through every real backend implementation."""

from datetime import datetime, timedelta, timezone

import hashlib
import uuid

import pytest

from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect
from tests.test_federation_journal import backend, insert, mutate  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("counts,expected", [((2, 1), 0), ((3, 3), 4)])
async def test_live_consolidation_preserves_owner_partitions(backend, counts, expected):  # noqa: F811
    ids = []
    for owner, count in zip(("alice", "bob"), counts):
        for index in range(count):
            mid = f"{owner}-{index}"
            ids.append(mid)
            await insert(backend, mid=mid, namespace="shared", mode=600)
            await mutate(backend, "UPDATE memories SET owner_id = ? WHERE id = ?", owner, mid)
    async with backend.transactional() as tx:
        run_id = await backend.morpheus.begin_run(
            tx,
            triggered_by="api",
            window_hours=24,
            cluster_min_size=3,
            config={"clusters": [{"cluster_id": 0, "member_memory_ids": ids}]},
            namespace=None,
        )
    with pytest.raises(RuntimeError, match="rollback phase"):
        async with backend.transactional() as tx:
            await backend.morpheus.phase_consolidate(tx, run_id=run_id, consolidated_permission_mode=400)
            raise RuntimeError("rollback phase")
    async with backend.transactional() as tx:
        assert (
            await _Ops(tx, transaction_dialect(tx)).scalar(
                "SELECT COUNT(*) FROM memories WHERE consolidated_into IS NOT NULL"
            )
            == 0
        )
    for _ in range(2):
        async with backend.transactional() as tx:
            result = await backend.morpheus.phase_consolidate(tx, run_id=run_id, consolidated_permission_mode=400)
            assert result.memories_consolidated == expected
            cross = await _Ops(tx, transaction_dialect(tx)).fetchall(
                "SELECT m.id FROM memories m JOIN memories c ON c.id=m.consolidated_into "
                "WHERE m.owner_id <> c.owner_id OR m.namespace <> c.namespace"
            )
            assert cross == []


@pytest.mark.asyncio
async def test_live_morpheus_phases_and_rollback_share_transaction(backend):  # noqa: F811
    summary_id = "summary-" + uuid.uuid4().hex
    summary_content = "summary facts " * 500 + "distinct tail"
    await insert(backend, mid="phase-source", namespace="shared", mode=600)
    await mutate(
        backend,
        "UPDATE memories SET created = ?, verbatim_content = content WHERE id = ?",
        datetime.now(timezone.utc) - timedelta(hours=1),
        "phase-source",
    )
    async with backend.transactional() as tx:
        await backend.memories.upsert_memory_embedding(tx, "phase-source", [0.25, 0.5, 0.75])
        repo = backend.morpheus
        run_id = await repo.begin_run(
            tx,
            triggered_by="api",
            window_hours=24,
            cluster_min_size=1,
            config={"clusters": [{"cluster_id": 0, "member_memory_ids": ["phase-source"]}]},
            namespace=None,
        )
        await repo.set_phase(tx, run_id, "cluster")
        await repo.merge_run_config(tx, run_id, patch={"checked": True})
        assert await repo.replay_scan_count(tx, run_id=run_id) == 1
        candidates = await repo.fetch_cluster_candidates(tx, run_id=run_id, max_input_count=10)
        assert [mid for mid, _ in candidates.candidates] == ["phase-source"]
        loaded = await repo.phase_synthesise_load(tx, run_id=run_id)
        assert loaded[0] == 1 and len(loaded[1]) == 1
        await repo.phase_synthesise_store(
            tx,
            memory_id=summary_id,
            content=summary_content,
            category="facts",
            owner_id="alice",
            namespace="shared",
            run_id=run_id,
            source_memory_ids=["phase-source"],
            metadata={},
        )
        batch = await repo.phase_extract_load(tx, run_id=run_id, min_chars=1, max_input_count=10)
        candidate = next(c for c in batch.candidates if c.id == "phase-source")
        failure = await repo.phase_extract_failure(tx, memory_id=candidate.id, max_failures=2, error="test retry")
        assert failure.attempts == 1
        assert await repo.phase_extract_store(
            tx, run_id=run_id, candidate=candidate, triples=[("phase-triple", "Alice", "knows", "facts", 0.9)]
        )
        await repo.finish_run(tx, run_id)
        await repo.rollback_run(tx, run_id, requested_by="test")
        ops = _Ops(tx, transaction_dialect(tx))
        assert await ops.fetchone("SELECT id FROM memories WHERE id = ?", summary_id) is None
        assert await ops.fetchone("SELECT id FROM kg_triples WHERE id = ?", "phase-triple") is None
        row = await ops.fetchone("SELECT triples_extracted_at FROM memories WHERE id = ?", "phase-source")
        assert row["triples_extracted_at"] is None

        audit = await ops.fetchone("SELECT content_hash FROM deletion_log WHERE memory_id = ?", summary_id)
        assert audit["content_hash"] == hashlib.sha256(summary_content.encode()).hexdigest()
