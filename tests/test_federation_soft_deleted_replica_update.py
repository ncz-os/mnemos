"""MN-35: a replica this node soft-deleted must not have its source fenced.

``skipped_deleted`` (F-2, ``tests/test_federation_soft_deleted_marker.py``)
made a previously invisible divergence observable: an inbound event for a
replica that was soft-deleted HERE is not applied, the row stays deleted, and
the sync log says how many were skipped. What it did not pin is the ORDER of
the two writes on the journaled path.

``_store_memories`` runs ``federation_journal.accept_event`` *before* it looks
at the replica's lifecycle, and ``accept_event`` is a fence plus a WRITE: it
records the inbound sequence in ``federation_receive_state`` as applied. So a
sync that refuses to apply the event still advances the fence that decides
which sequences are acceptable. The fence is durable and the receiver never
re-reads the tombstone it wrote:

* a replica restored locally AFTER the skip (PERSEPHONE restore, the
  deletion-request ``restore`` phase) is fenced against its source for every
  later event up to and including the one it never received;
* nothing repairs that: the fence has no row state in it, so a peer that is
  behind can only be fixed by a full re-seed.

A skip is a decision by the receiver, so it must be taken BEFORE the fence is
touched. The fence then stays where the replica's applied state actually is,
and a later replay of that sequence applies normally.

These run against real SQLite. The dialect SQL of the six backends is
unaffected: the skip happens in the receiver, before any backend write.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.domain import federation as fed
from mnemos.persistence import sqlite as sqlite_backend

PEER = "pythia"
LOCAL_ID = f"fed:{PEER}:m1"


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    """A real SQLite backend with the journal installed."""
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    from mnemos.persistence.sqlite import SqliteBackend

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    instance = SqliteBackend(tmp_path / "fed-deleted-update.db", settings)
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


def _row(remote_id="m1", *, content="facts", sequence, updated="2020-01-01T00:00:00Z"):
    return {
        "id": remote_id,
        "content": content,
        "category": "facts",
        "namespace": "default",
        "created": updated,
        "updated": updated,
        "federation_sequence": sequence,
    }


async def _apply(backend, *rows, counters=None):
    async with backend.transactional() as tx:
        return await fed._store_memories(backend.federation, tx, PEER, list(rows), counters=counters)


async def _local(backend, local_id=LOCAL_ID):
    async with backend.transactional() as tx:
        return await sqlite_backend._fetch_one(
            tx.conn,
            "SELECT id, content, deleted_at FROM memories WHERE id = ?",
            (local_id,),
        )


async def _fence(backend, remote_id="m1"):
    """The inbound sequence this node has accepted from ``PEER`` for ``remote_id``."""
    async with backend.transactional() as tx:
        return await sqlite_backend._fetch_one(
            tx.conn,
            "SELECT last_sequence FROM federation_receive_state WHERE peer_name = ? AND remote_id = ?",
            (PEER, remote_id),
        )


async def _soft_delete(backend, local_id=LOCAL_ID):
    async with backend.transactional() as tx:
        await sqlite_backend._execute(
            tx.conn,
            "UPDATE memories SET deleted_at = '2020-01-10T00:00:00+00:00' WHERE id = ?",
            (local_id,),
        )


async def _restore(backend, local_id=LOCAL_ID):
    """Undo the local soft-delete — the operator's restore, not the receiver's."""
    async with backend.transactional() as tx:
        await sqlite_backend._execute(
            tx.conn,
            "UPDATE memories SET deleted_at = NULL WHERE id = ?",
            (local_id,),
        )


@pytest.mark.asyncio
async def test_skipped_event_does_not_advance_the_receive_fence(backend):
    """The regression: a refused event must not fence the source.

    Apply version 1 (sequence 1), soft-delete the replica here, then deliver
    version 2 (sequence 2). Sequence 2 is not applied, so it must not be
    recorded as applied either: after the operator restores the row, the very
    same sequence-2 event must still be acceptable and must land its content.

    Before the fix ``accept_event`` wrote ``last_sequence = 2`` and the
    restored replica rejected the replay as already applied — it stays on
    version 1 while its source is on version 2, with no way back but a
    re-seed.
    """
    assert await _apply(backend, _row(content="facts v1", sequence=1)) == (1, 0)

    await _soft_delete(backend)
    counters: dict[str, int] = {}
    assert await _apply(backend, _row(content="facts v2", sequence=2), counters=counters) == (0, 0)
    assert counters.get("skipped_deleted") == 1, f"the skip must be counted, got {counters!r}"

    row = await _local(backend)
    assert row["deleted_at"] is not None, "the receiver must not undelete a locally deleted replica"
    assert row["content"] == "facts v1", "the newer revision must not be written into a deleted replica"

    fence = await _fence(backend)
    assert fence is not None and int(fence["last_sequence"]) == 1, (
        "a skipped event must not be recorded as applied: the fence has to stay where the "
        f"replica's content actually is (1), got {dict(fence) if fence else None}"
    )

    # The operator restores the replica. Its source's pending revision must
    # still be deliverable: the fence never claimed it.
    await _restore(backend)
    counters = {}
    assert await _apply(backend, _row(content="facts v2", sequence=2), counters=counters) == (0, 1)
    assert counters.get("skipped_deleted", 0) == 0, "a restored replica is not a skip"
    row = await _local(backend)
    assert row["deleted_at"] is None
    assert row["content"] == "facts v2", "the restored replica must converge to its source's current revision"


@pytest.mark.asyncio
async def test_skipped_batch_leaves_no_fence_behind_for_its_ids(backend):
    """Every id the batch skipped, not just the first, keeps its old fence.

    The pull loop applies a page as one batch, so a page that mixes a live
    replica with a deleted one is the normal shape. The live row must still be
    fenced and applied; the deleted one must be untouched.
    """
    assert await _apply(
        backend,
        _row("m1", content="one v1", sequence=1),
        _row("m2", content="two v1", sequence=2),
    ) == (2, 0)
    await _soft_delete(backend, f"fed:{PEER}:m2")

    batch = [
        _row("m1", content="one v2", sequence=5, updated="2020-01-02T00:00:00Z"),
        _row("m2", content="two v2", sequence=6, updated="2020-01-02T00:00:00Z"),
    ]
    counters: dict[str, int] = {}
    assert await _apply(backend, *batch, counters=counters) == (0, 1)
    assert counters.get("skipped_deleted") == 1

    live = await _local(backend, f"fed:{PEER}:m1")
    assert live["content"] == "one v2", "the live replica in the same batch must still update"
    assert int((await _fence(backend, "m1"))["last_sequence"]) == 5

    dead = await _local(backend, f"fed:{PEER}:m2")
    assert dead["deleted_at"] is not None
    assert dead["content"] == "two v1"
    assert int((await _fence(backend, "m2"))["last_sequence"]) == 2, (
        "the skipped id's fence must stay at the revision its content actually holds"
    )
