"""Regression test for F01 (adbeeb63) — MORPHEUS cross-owner privacy isolation.

The pre-F01 ``phase_synthesise`` runner combined cluster members from
multiple owners into one summary memory and assigned the synthesis to the
*majority* owner (mode ``600``). A reviewer found this leaked minority
owners' private content to the majority owner via the new
``morpheus_local`` summary — a real cross-tenant privacy bug, not a
privilege escalation.

This file pins the F01 contract end-to-end against the real SQLite
backend and the real ``phase_synthesise`` runner:

1. **Reviewer's exact reproduction** — three private memories with the
   same embedding vector, two owned by Alice, one owned by Bob, each
   with a greppable marker string. The runner must produce syntheses
   that never combine Bob's marker with Alice's content.
2. **Same-namespace, different-owner isolation** — same content under
   two distinct owners in one cluster must split into per-owner
   sub-clusters.
3. **Different-namespace isolation** — memories under two distinct
   namespaces must split even when the owner is the same.

A root-only HTTP trigger does NOT close this bug (the disclosure is
between two non-root owners). The fix lives in ``phase_synthesise``;
these tests guard the runtime contract from regressing.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mnemos.core import lifecycle as _lifecycle
from mnemos.domain.morpheus.runner import phase_synthesise
from mnemos.persistence.sqlite import (
    SqliteBackend,
    SqliteTransaction,
    _execute as _sqlite_execute,
    _fetch_all as _sqlite_fetch_all,
)


# Distinct, greppable markers so the test can assert "X's marker
# never appears in Y's synthesis content".
ALICE_MARKER = "ALICE_CONFIDENTIAL_MARKER_QQQ"
BOB_MARKER = "BOB_CONFIDENTIAL_MARKER_XYZ"
CAROL_MARKER = "CAROL_CONFIDENTIAL_MARKER_AAA"
DAVE_MARKER = "DAVE_CONFIDENTIAL_MARKER_BBB"

# Identical embedding vectors across all three reviewers' memories — this
# is what triggers ``phase_cluster`` to place them in a single cluster
# (cosine similarity = 1.0, well above the 0.85 threshold).
_IDENTICAL_VECTOR = [1.0, 0.0, 0.0]


async def _seed_memory(
    backend: SqliteBackend,
    tx: SqliteTransaction,
    *,
    memory_id: str,
    owner_id: str,
    namespace: str,
    content: str,
    created: datetime,
    vector: list[float],
) -> None:
    """Insert one private memory row directly via the SqliteBackend repo.

    Uses ``permission_mode=600`` (private to the owner) to mirror the
    reviewer's reproduction. The embedding is written as JSON text, the
    same shape ``SqliteMorpheusRepository.fetch_cluster_candidates``
    parses — so a ``phase_cluster`` pass on this dataset would in fact
    pick up all rows.
    """
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=content,
        category="facts",
        subcategory=None,
        metadata_json="{}",
        quality_rating=50,
        owner_id=owner_id,
        namespace=namespace,
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=content,
        created=created,
        updated=created,
    )
    await _sqlite_execute(
        tx.conn,
        "UPDATE memories SET embedding = ? WHERE id = ?",
        (json.dumps(vector), memory_id),
    )


async def _seed_run(
    tx: SqliteTransaction,
    run_id: str,
    *,
    member_ids: list[str],
    cluster_min_size: int = 2,
    namespace: str | None = "A",
) -> None:
    """Write a ``morpheus_runs`` row whose ``config.clusters`` lists all
    members in one cluster — mimicking what ``phase_cluster`` would have
    produced if it had grouped purely by embedding similarity without
    F01's owner partitioning."""
    await _sqlite_execute(
        tx.conn,
        """
        INSERT INTO morpheus_runs (
            id, triggered_by, started_at, window_started_at, window_ended_at,
            window_hours, cluster_min_size, config, namespace, status
        ) VALUES (?, 'test', CURRENT_TIMESTAMP, ?, ?, 168, ?, ?, ?, 'running')
        """,
        (
            run_id,
            "2020-01-01 00:00:00",
            "2030-01-01 00:00:00",
            cluster_min_size,
            json.dumps({
                "clusters": [
                    {"cluster_id": 0, "member_memory_ids": member_ids},
                ]
            }),
            namespace,
        ),
    )


@pytest.fixture
def wired_sqlite_backend(tmp_path, monkeypatch):
    """Open a real SqliteBackend, wire it into the lifecycle global, and
    yield it. Cleanup closes the backend and clears the global."""
    backend = SqliteBackend(tmp_path / "f01.sqlite3", SimpleNamespace())
    monkeypatch.setattr(_lifecycle, "_persistence_backend", backend)
    yield backend
    # The lifecycle global will be reset by monkeypatch teardown, but
    # we still need to close the Sqlite connection explicitly.
    try:
        import asyncio as _asyncio

        _asyncio.get_event_loop()
    except RuntimeError:
        pass


@pytest.mark.asyncio
async def test_reviewers_exact_reproduction_alice_synthesis_excludes_bob_marker(
    tmp_path,
    monkeypatch,
):
    """Exact reproduction of the F01 finding.

    Setup (per the reviewer): three private memories with identical
    embedding vectors, two owned by Alice, one owned by Bob, each with a
    distinct greppable marker. The ``morpheus_runs.config`` carries all
    three in a single cluster — i.e. as ``phase_cluster`` would have
    persisted them pre-F01 (it groups purely by cosine similarity).

    Pre-F01: a single synthesis is created, owned by Alice (the majority),
    containing all three contents — Bob's marker leaks.

    Post-F01: the cluster is partitioned into per-(owner, namespace)
    sub-clusters. Alice's synthesis contains Alice's two markers; Bob's
    synthesis (if any) contains only Bob's marker. No synthesis
    contains markers from more than one owner.
    """
    backend = SqliteBackend(tmp_path / "f01_repro.sqlite3", SimpleNamespace())
    monkeypatch.setattr(_lifecycle, "_persistence_backend", backend)
    await backend.open()
    try:
        run_id = str(uuid4())
        base = datetime(2026, 9, 1, 12, 0, 0)
        alice_a = f"mem_alice_a_{uuid4().hex[:8]}"
        alice_b = f"mem_alice_b_{uuid4().hex[:8]}"
        bob_a = f"mem_bob_a_{uuid4().hex[:8]}"
        member_ids = [alice_a, alice_b, bob_a]

        async with backend.transactional() as tx:
            assert isinstance(tx, SqliteTransaction)
            # Alice's two private memories.
            await _seed_memory(
                backend, tx,
                memory_id=alice_a, owner_id="alice", namespace="A",
                content=f"Alice note one — {ALICE_MARKER}",
                created=base,
                vector=_IDENTICAL_VECTOR,
            )
            await _seed_memory(
                backend, tx,
                memory_id=alice_b, owner_id="alice", namespace="A",
                content=f"Alice note two — {ALICE_MARKER}",
                created=base,
                vector=_IDENTICAL_VECTOR,
            )
            # Bob's private memory — same vector, distinct owner/namespace.
            await _seed_memory(
                backend, tx,
                memory_id=bob_a, owner_id="bob", namespace="A",
                content=f"Bob secret — {BOB_MARKER}",
                created=base,
                vector=_IDENTICAL_VECTOR,
            )
            await _seed_run(tx, run_id, member_ids=member_ids, cluster_min_size=2, namespace="A")

        # Run the real ``phase_synthesise`` against the real backend.
        n_summaries = await phase_synthesise(_DummyPool(), run_id)

        # Exactly one synthesis per non-empty partition — Alice's two
        # memories form one sub-cluster; Bob's single memory forms
        # another (the cluster_min_size was already satisfied at CLUSTER
        # time and is not re-applied post-partition; see F01 fix
        # comment in phase_synthesise).
        assert n_summaries >= 1

        async with backend.transactional() as tx:
            summaries = await _sqlite_fetch_all(
                tx.conn,
                "SELECT id, owner_id, namespace, content, source_memories "
                "FROM memories "
                "WHERE provenance = 'morpheus_local' AND morpheus_run_id = ?",
                (run_id,),
            )

        # No synthesis may contain markers from more than one owner.
        for summary in summaries:
            content = str(summary["content"] or "")
            owner = summary["owner_id"]
            # The synthesis owned by Alice must contain Alice's markers
            # and must NOT contain Bob's marker (the F01 disclosure).
            if owner == "alice":
                assert ALICE_MARKER in content, (
                    f"Alice's synthesis must contain her own marker; got {content!r}"
                )
                assert BOB_MARKER not in content, (
                    "F01 REGRESSION: Alice's synthesis leaked Bob's marker — "
                    f"{BOB_MARKER!r} appeared in Alice-owned summary {summary['id']}: "
                    f"{content!r}"
                )
            elif owner == "bob":
                assert BOB_MARKER in content, (
                    f"Bob's synthesis must contain his own marker; got {content!r}"
                )
                assert ALICE_MARKER not in content, (
                    "F01 REGRESSION: Bob's synthesis leaked Alice's marker — "
                    f"{ALICE_MARKER!r} appeared in Bob-owned summary {summary['id']}"
                )
            else:
                pytest.fail(
                    f"Unexpected synthesis owner {owner!r} — partition should "
                    "only produce Alice- or Bob-owned summaries"
                )

        # Stronger invariant: every synthesis's owner must equal the
        # owner of EVERY member in its ``source_memories`` list.
        for summary in summaries:
            sources = json.loads(summary["source_memories"] or "[]")
            assert sources, (
                f"Synthesis {summary['id']} has empty source_memories; "
                "phase_synthesise must never emit a summary with no inputs"
            )
            async with backend.transactional() as tx:
                rows = await _sqlite_fetch_all(
                    tx.conn,
                    "SELECT id, owner_id, namespace FROM memories WHERE id IN ({})".format(
                        ",".join("?" for _ in sources)
                    ),
                    tuple(sources),
                )
            owners = {row["owner_id"] for row in rows}
            namespaces = {row["namespace"] for row in rows}
            assert len(owners) == 1, (
                f"F01 REGRESSION: synthesis {summary['id']} (owned by "
                f"{summary['owner_id']!r}) was fed by sources from multiple "
                f"owners: {sorted(owners)}"
            )
            assert len(namespaces) == 1, (
                f"F01 REGRESSION: synthesis {summary['id']} (namespace "
                f"{summary['namespace']!r}) was fed by sources from multiple "
                f"namespaces: {sorted(namespaces)}"
            )
            assert owners == {summary["owner_id"]}, (
                f"F01 REGRESSION: synthesis owner {summary['owner_id']!r} "
                f"does not match source owner set {sorted(owners)}"
            )
            assert namespaces == {summary["namespace"]}, (
                f"F01 REGRESSION: synthesis namespace {summary['namespace']!r} "
                f"does not match source namespace set {sorted(namespaces)}"
            )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_same_namespace_different_owners_split_into_per_owner_subclusters(
    tmp_path,
    monkeypatch,
):
    """Two private memories, same namespace, DIFFERENT owners, identical
    embeddings. They cluster together. F01 must split them into two
    per-owner sub-clusters, producing two syntheses, each owned by its
    own owner with no cross-owner content.
    """
    backend = SqliteBackend(tmp_path / "f01_same_ns.sqlite3", SimpleNamespace())
    monkeypatch.setattr(_lifecycle, "_persistence_backend", backend)
    await backend.open()
    try:
        run_id = str(uuid4())
        base = datetime(2026, 9, 1, 12, 0, 0)
        alice = f"mem_alice_{uuid4().hex[:8]}"
        bob = f"mem_bob_{uuid4().hex[:8]}"

        async with backend.transactional() as tx:
            assert isinstance(tx, SqliteTransaction)
            await _seed_memory(
                backend, tx, memory_id=alice,
                owner_id="alice", namespace="A",
                content=f"Alice's private project note — {ALICE_MARKER}",
                created=base,
                vector=_IDENTICAL_VECTOR,
            )
            await _seed_memory(
                backend, tx, memory_id=bob,
                owner_id="bob", namespace="A",
                content=f"Bob's private project note — {BOB_MARKER}",
                created=base,
                vector=_IDENTICAL_VECTOR,
            )
            await _seed_run(tx, run_id, member_ids=[alice, bob], cluster_min_size=2, namespace="A")

        n_summaries = await phase_synthesise(_DummyPool(), run_id)
        assert n_summaries >= 1

        async with backend.transactional() as tx:
            summaries = await _sqlite_fetch_all(
                tx.conn,
                "SELECT id, owner_id, namespace, content "
                "FROM memories "
                "WHERE provenance = 'morpheus_local' AND morpheus_run_id = ?",
                (run_id,),
            )

        owners = {summary["owner_id"] for summary in summaries}
        assert owners == {"alice", "bob"}, (
            f"F01 REGRESSION: same-namespace/different-owner cluster was not "
            f"split per-owner — owners seen: {sorted(owners)}"
        )

        for summary in summaries:
            content = str(summary["content"] or "")
            if summary["owner_id"] == "alice":
                assert ALICE_MARKER in content
                assert BOB_MARKER not in content, (
                    f"Alice-owned summary leaked Bob's marker: {content!r}"
                )
            else:
                assert BOB_MARKER in content
                assert ALICE_MARKER not in content, (
                    f"Bob-owned summary leaked Alice's marker: {content!r}"
                )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_different_namespaces_split_into_per_namespace_subclusters(
    tmp_path,
    monkeypatch,
):
    """Same owner, two distinct namespaces — the cluster must still split.

    The fix partitions on (owner_id, namespace), not just owner_id,
    because the namespace is also an access boundary. Two memories from
    the same owner in different namespaces must not be merged into one
    synthesis (the owner could legitimately have different permission
    envelopes per namespace).
    """
    backend = SqliteBackend(tmp_path / "f01_diff_ns.sqlite3", SimpleNamespace())
    monkeypatch.setattr(_lifecycle, "_persistence_backend", backend)
    await backend.open()
    try:
        run_id = str(uuid4())
        base = datetime(2026, 9, 1, 12, 0, 0)
        alice_a = f"mem_alice_A_{uuid4().hex[:8]}"
        alice_b = f"mem_alice_B_{uuid4().hex[:8]}"

        async with backend.transactional() as tx:
            assert isinstance(tx, SqliteTransaction)
            await _seed_memory(
                backend, tx, memory_id=alice_a,
                owner_id="alice", namespace="A",
                content=f"Alice in namespace A — {ALICE_MARKER}",
                created=base,
                vector=_IDENTICAL_VECTOR,
            )
            await _seed_memory(
                backend, tx, memory_id=alice_b,
                owner_id="alice", namespace="B",
                content=f"Alice in namespace B — {CAROL_MARKER}",
                created=base,
                vector=_IDENTICAL_VECTOR,
            )
            # Both share owner_id="alice" but different namespaces.
            await _seed_run(
                tx, run_id, member_ids=[alice_a, alice_b],
                cluster_min_size=2, namespace=None,
            )

        n_summaries = await phase_synthesise(_DummyPool(), run_id)
        assert n_summaries >= 1

        async with backend.transactional() as tx:
            summaries = await _sqlite_fetch_all(
                tx.conn,
                "SELECT id, owner_id, namespace, content "
                "FROM memories "
                "WHERE provenance = 'morpheus_local' AND morpheus_run_id = ?",
                (run_id,),
            )

        # Each summary must live in exactly one namespace, and that
        # namespace must match the namespace of every one of its
        # source memories.
        for summary in summaries:
            content = str(summary["content"] or "")
            sources = json.loads(
                (
                    await _sqlite_fetch_all(
                        tx.conn,
                        "SELECT source_memories FROM memories WHERE id = ?",
                        (summary["id"],),
                    )
                )[0]["source_memories"] or "[]"
            )
            async with backend.transactional() as tx2:
                rows = await _sqlite_fetch_all(
                    tx2.conn,
                    "SELECT namespace FROM memories WHERE id IN ({})".format(
                        ",".join("?" for _ in sources)
                    ),
                    tuple(sources),
                )
            namespaces = {row["namespace"] for row in rows}
            assert namespaces == {summary["namespace"]}, (
                f"F01 REGRESSION: synthesis {summary['id']} mixed namespaces — "
                f"summary namespace {summary['namespace']!r} vs source "
                f"namespaces {sorted(namespaces)}"
            )

        # Strongest check: ALICE_MARKER and CAROL_MARKER must NOT appear
        # in the same synthesis (they came from different namespaces).
        for summary in summaries:
            content = str(summary["content"] or "")
            has_alice = ALICE_MARKER in content
            has_carol = CAROL_MARKER in content
            assert not (has_alice and has_carol), (
                "F01 REGRESSION: same-owner/different-namespace memories "
                "ended up in the same synthesis — namespaces were not "
                f"used as a partition key: {content!r}"
            )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_single_owner_cluster_unchanged_by_partition(
    tmp_path,
    monkeypatch,
):
    """Negative control: when a cluster contains memories from only one
    owner in one namespace, the partition produces the same single
    synthesis the pre-F01 code would have. No behavior change for the
    non-leaking case.
    """
    backend = SqliteBackend(tmp_path / "f01_single.sqlite3", SimpleNamespace())
    monkeypatch.setattr(_lifecycle, "_persistence_backend", backend)
    await backend.open()
    try:
        run_id = str(uuid4())
        base = datetime(2026, 9, 1, 12, 0, 0)
        a = f"mem_alice_a_{uuid4().hex[:8]}"
        b = f"mem_alice_b_{uuid4().hex[:8]}"

        async with backend.transactional() as tx:
            assert isinstance(tx, SqliteTransaction)
            await _seed_memory(
                backend, tx, memory_id=a, owner_id="alice", namespace="A",
                content=f"First Alice note — {ALICE_MARKER}",
                created=base, vector=_IDENTICAL_VECTOR,
            )
            await _seed_memory(
                backend, tx, memory_id=b, owner_id="alice", namespace="A",
                content=f"Second Alice note — {ALICE_MARKER}",
                created=base, vector=_IDENTICAL_VECTOR,
            )
            await _seed_run(tx, run_id, member_ids=[a, b], cluster_min_size=2, namespace="A")

        n_summaries = await phase_synthesise(_DummyPool(), run_id)

        async with backend.transactional() as tx:
            summaries = await _sqlite_fetch_all(
                tx.conn,
                "SELECT owner_id, namespace, content "
                "FROM memories "
                "WHERE provenance = 'morpheus_local' AND morpheus_run_id = ?",
                (run_id,),
            )

        # Exactly one synthesis (one partition, since both members share
        # owner+namespace), owned by Alice.
        assert n_summaries == 1
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary["owner_id"] == "alice"
        assert summary["namespace"] == "A"
        assert ALICE_MARKER in str(summary["content"] or "")
        assert BOB_MARKER not in str(summary["content"] or "")
    finally:
        await backend.close()


class _DummyPool:
    """Stand-in pool for ``phase_synthesise(pool, run_id)``.

    The runner's post-11c signature keeps ``pool`` as a backwards-
    compatible parameter; ``phase_synthesise`` ignores it (``_ = pool``)
    and routes everything through ``backend.morpheus.*``. The tests in
    this file exercise that path on a real SqliteBackend, so the pool
    only needs to exist — it is never dereferenced.
    """

    def acquire(self, *_args, **_kwargs):
        raise AssertionError(
            "phase_synthesise must route through backend.morpheus; "
            "the pool parameter is a backwards-compat no-op after item 11b"
        )