"""MN-24 / D-MN5: repair rows fenced before the replica fence existed.

A replica row damaged by a LOCAL lifecycle sweep (archival, dedup,
consolidation, deletion) *before* the replica fence shipped can never heal:
the receiver's durable sequence fence drops the source's replay before any
write. ``repair_fenced_replicas`` is the one-off cleanup: dry run by default,
the operator reads the list and applies it once. Apply deletes the damaged
local copy (plus its archive blob), clears the row's
``federation_receive_state`` fence and the peer's journal cursors, so the next
ordinary sync replays and re-pulls the row through the normal write path.

Rows the source actually withdrew (``federation_receive_state.withdrawn``)
must NOT come back, and a consolidation the PEER itself published (metadata
``federation_consolidation`` marker) must not be mistaken for local damage.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.domain.federation.repair import repair_fenced_replicas

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
EMBED_DIM = 3


class _Settings(SimpleNamespace):
    def __init__(self):
        super().__init__()
        self.database = SimpleNamespace(embedding_dim=EMBED_DIM)
        self.providers = SimpleNamespace(inference_embed_model="bge-m3")


@pytest_asyncio.fixture
async def replica(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "1")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", str(EMBED_DIM))
    monkeypatch.setenv("MNEMOS_EMBED_HTTP_MODEL", "bge-m3")
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "repair.db", _Settings())
    await backend.open()
    try:
        yield backend
    finally:
        await backend.close()


async def _exec(backend, sql: str, *params) -> None:
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    async with backend.transactional() as tx:
        await _Ops(tx, transaction_dialect(tx)).execute(sql, *params)


async def _one(backend, sql: str, *params):
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).fetchone(sql, *params)


async def _all(backend, sql: str, *params) -> list:
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).fetchall(sql, *params)


async def _peer(replica):
    async with replica.transactional() as tx:
        peer = await replica.federation.create_peer(
            tx,
            name="pythia",
            base_url="http://pythia",
            auth_token="token",
            namespace_filter=None,
            category_filter=None,
            enabled=True,
            sync_interval_secs=300,
            compat_mode="permissive",
        )
    return peer


async def _replica_row(
    replica,
    remote_id: str,
    *,
    deleted_at=None,
    archived_at=None,
    consolidated_into=None,
    metadata: str = "{}",
) -> str:
    local_id = f"fed:pythia:{remote_id}"
    await _exec(
        replica,
        "INSERT INTO memories(id, content, category, owner_id, namespace, permission_mode, "
        "created, updated, federation_source, federation_remote_updated, metadata, "
        "deleted_at, archived_at, consolidated_into) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        local_id,
        "replica facts" if archived_at is None else f"ARCHIVED:{local_id}",
        "facts",
        "alice",
        "default",
        644,
        NOW.isoformat(),
        NOW.isoformat(),
        "pythia",
        NOW.isoformat(),
        metadata,
        deleted_at,
        archived_at,
        consolidated_into,
    )
    return local_id


async def _fence(replica, remote_id: str, *, withdrawn: int = 0) -> None:
    await _exec(
        replica,
        "INSERT INTO federation_receive_state(peer_name, remote_id, last_sequence, "
        "remote_updated, withdrawn) VALUES (?, ?, ?, ?, ?)",
        "pythia",
        remote_id,
        7,
        NOW.isoformat(),
        withdrawn,
    )


async def _damage_three(replica) -> None:
    """One soft-deleted, one archived, one locally consolidated replica row."""
    await _replica_row(replica, "gone", deleted_at=NOW.isoformat())
    await _fence(replica, "gone")
    archived = await _replica_row(replica, "old", archived_at=NOW.isoformat())
    await _exec(
        replica,
        "INSERT INTO memory_archive(id, archived_by, compressed_content, "
        "compression_algo, original_size_bytes, compressed_size_bytes, schema_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        archived,
        "system:persephone",
        b"blob",
        "zstd",
        4,
        4,
        1,
    )
    await _fence(replica, "old")
    await _replica_row(replica, "merged", consolidated_into="fed:pythia:canon")
    await _fence(replica, "merged")


@pytest.mark.asyncio
async def test_dry_run_lists_candidates_and_changes_nothing(replica):
    peer = await _peer(replica)
    await _damage_three(replica)

    report = await repair_fenced_replicas(replica, peer["id"], dry_run=True)

    assert sorted(report["candidates"]) == ["merged", "gone", "old"] or sorted(
        c["remote_id"] if isinstance(c, dict) else c for c in report["candidates"]
    ) == ["gone", "merged", "old"]
    assert report["repaired"] == 0
    assert report["skipped_withdrawn"] == 0
    assert report["skipped_consolidated"] == 0
    # Dry run touches nothing: rows, fence and archive blob all still there.
    rows = await _all(
        replica, "SELECT id FROM memories WHERE federation_source = 'pythia'"
    )
    assert len(rows) == 3
    fences = await _all(
        replica, "SELECT remote_id FROM federation_receive_state WHERE peer_name = 'pythia'"
    )
    assert len(fences) == 3
    blob = await _one(replica, "SELECT id FROM memory_archive WHERE id = ?", "fed:pythia:old")
    assert blob is not None


@pytest.mark.asyncio
async def test_apply_deletes_rows_blob_fence_and_cursors(replica):
    peer = await _peer(replica)
    await _damage_three(replica)
    await _exec(
        replica,
        "INSERT INTO federation_peer_cursors(peer_id, cursor_value, filter_signature) "
        "VALUES (?, ?, ?)",
        peer["id"],
        "journal:9",
        "sig",
    )

    report = await repair_fenced_replicas(replica, peer["id"], dry_run=False)

    assert report["repaired"] == 3
    assert report["skipped_withdrawn"] == 0
    assert report["skipped_consolidated"] == 0
    rows = await _all(
        replica, "SELECT id FROM memories WHERE federation_source = 'pythia'"
    )
    assert rows == []
    blob = await _one(replica, "SELECT id FROM memory_archive WHERE id = ?", "fed:pythia:old")
    assert blob is None
    fences = await _all(
        replica, "SELECT remote_id FROM federation_receive_state WHERE peer_name = 'pythia'"
    )
    assert fences == []
    cursors = await _all(
        replica, "SELECT peer_id FROM federation_peer_cursors WHERE peer_id = ?", peer["id"]
    )
    assert cursors == []


@pytest.mark.asyncio
async def test_apply_skips_withdrawn_and_peer_consolidated_rows(replica):
    peer = await _peer(replica)
    # Source withdrew this one: the fence says so, so it must never come back.
    await _replica_row(replica, "revoked", deleted_at=NOW.isoformat())
    await _fence(replica, "revoked", withdrawn=1)
    # The PEER consolidated this one (tombstone carried the marker): not local damage.
    await _replica_row(
        replica,
        "twin",
        consolidated_into="fed:pythia:canon",
        metadata=json.dumps(
            {"federation_consolidation": {"remote_id": "twin", "peer": "pythia"}}
        ),
    )
    await _fence(replica, "twin")
    # Genuine local damage alongside, to prove the run still repairs the rest.
    await _replica_row(replica, "gone", deleted_at=NOW.isoformat())
    await _fence(replica, "gone")

    report = await repair_fenced_replicas(replica, peer["id"], dry_run=False)

    assert report["repaired"] == 1
    assert report["skipped_withdrawn"] == 1
    assert report["skipped_consolidated"] == 1
    remaining = {
        r["id"]
        for r in await _all(replica, "SELECT id FROM memories WHERE federation_source = 'pythia'")
    }
    assert remaining == {"fed:pythia:revoked", "fed:pythia:twin"}
