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
import httpx
import pytest_asyncio

from mnemos.domain.federation.repair import _repair_order, repair_fenced_replicas

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
    # The canonical the local consolidation points at must exist: the schema
    # constrains ``consolidated_into REFERENCES memories(id)``.
    await _replica_row(replica, "canon")
    await _fence(replica, "canon")
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

    assert sorted(c["remote_id"] for c in report["candidates"]) == [
        "gone",
        "merged",
        "old",
    ], report["candidates"]
    assert report["repaired"] == 0
    assert report["skipped_withdrawn"] == 0
    assert report["skipped_consolidated"] == 0
    # Dry run touches nothing: rows, fence and archive blob all still there.
    rows = await _all(replica, "SELECT id FROM memories WHERE federation_source = 'pythia'")
    assert len(rows) == 4
    fences = await _all(replica, "SELECT remote_id FROM federation_receive_state WHERE peer_name = 'pythia'")
    assert len(fences) == 4
    blob = await _one(replica, "SELECT id FROM memory_archive WHERE id = ?", "fed:pythia:old")
    assert blob is not None


@pytest.mark.asyncio
async def test_apply_deletes_rows_blob_fence_and_cursors(replica):
    peer = await _peer(replica)
    await _damage_three(replica)
    await _exec(
        replica,
        "INSERT INTO federation_peer_cursors(peer_id, cursor_value, filter_signature) VALUES (?, ?, ?)",
        peer["id"],
        "journal:9",
        "sig",
    )

    report = await repair_fenced_replicas(replica, peer["id"], dry_run=False)

    assert report["repaired"] == 3
    assert report["skipped_withdrawn"] == 0
    assert report["skipped_consolidated"] == 0
    rows = await _all(replica, "SELECT id FROM memories WHERE federation_source = 'pythia'")
    # The healthy canonical stays; every damaged row is gone.
    assert [r["id"] for r in rows] == ["fed:pythia:canon"]
    blob = await _one(replica, "SELECT id FROM memory_archive WHERE id = ?", "fed:pythia:old")
    assert blob is None
    fences = await _all(replica, "SELECT remote_id FROM federation_receive_state WHERE peer_name = 'pythia'")
    # Only damaged rows lose their fence; the healthy canonical keeps its own.
    assert [f["remote_id"] for f in fences] == ["canon"]
    cursors = await _all(replica, "SELECT peer_id FROM federation_peer_cursors WHERE peer_id = ?", peer["id"])
    assert cursors == []


@pytest.mark.asyncio
async def test_apply_skips_withdrawn_and_peer_consolidated_rows(replica):
    peer = await _peer(replica)
    # The canonical the peer-consolidated twin points at must exist.
    await _replica_row(replica, "canon")
    await _fence(replica, "canon")
    # Source withdrew this one: the fence says so, so it must never come back.
    await _replica_row(replica, "revoked", deleted_at=NOW.isoformat())
    await _fence(replica, "revoked", withdrawn=1)
    # The PEER consolidated this one (tombstone carried the marker): not local damage.
    await _replica_row(
        replica,
        "twin",
        consolidated_into="fed:pythia:canon",
        metadata=json.dumps({"federation_consolidation": {"remote_id": "twin", "peer": "pythia"}}),
    )
    await _fence(replica, "twin")
    # Genuine local damage alongside, to prove the run still repairs the rest.
    await _replica_row(replica, "gone", deleted_at=NOW.isoformat())
    await _fence(replica, "gone")

    report = await repair_fenced_replicas(replica, peer["id"], dry_run=False)

    assert report["repaired"] == 1
    assert report["skipped_withdrawn"] == 1
    assert report["skipped_consolidated"] == 1
    remaining = {r["id"] for r in await _all(replica, "SELECT id FROM memories WHERE federation_source = 'pythia'")}
    assert remaining == {"fed:pythia:revoked", "fed:pythia:twin", "fed:pythia:canon"}


# ── end-to-end: the next ordinary sync re-pulls what the repair cleared ──────


class _SourceTransport(httpx.AsyncBaseTransport):
    """Dispatch the replica's outbound HTTP into the source's real routes."""

    def __init__(self, app, calls):
        self._app = app
        self.calls = calls

    async def handle_async_request(self, request):
        rewritten = httpx.Request(
            method=request.method,
            url=request.url.copy_with(scheme="http", host="pythia", port=None),
            headers=request.headers,
            content=request.content,
            extensions=request.extensions,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app), base_url="http://pythia") as inner:
            return await inner.send(rewritten)


@pytest_asyncio.fixture
async def live_pair(tmp_path: Path, monkeypatch):
    """Two real SQLite backends: PYTHIA (source) and PROTEUS (replica)."""
    import httpx
    from fastapi import FastAPI

    from mnemos.api import persistence_helpers
    from mnemos.api.routes.federation import router as federation_router
    from mnemos.api.dependencies import UserContext, get_current_user
    from mnemos.domain import federation as fed

    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "1")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", str(EMBED_DIM))
    monkeypatch.setenv("MNEMOS_EMBED_HTTP_MODEL", "bge-m3")
    from mnemos.persistence.sqlite import SqliteBackend

    source = SqliteBackend(tmp_path / "pythia.db", _Settings())
    await source.open()
    replica = SqliteBackend(tmp_path / "proteus.db", _Settings())
    await replica.open()
    from types import SimpleNamespace as _NS

    async def _current_user():
        return UserContext(
            user_id="federation-bot",
            group_ids=[],
            role="root",
            namespace=None,
            authenticated=True,
        )

    app = FastAPI()
    app.include_router(federation_router)
    app.dependency_overrides[get_current_user] = _current_user
    transport = _SourceTransport(app, [])

    real_make_safe_client = fed.make_safe_client

    async def _safe_client(url, *, timeout=None, allow_private=True, **kwargs):
        client = httpx.AsyncClient(transport=transport, base_url="http://pythia")
        return client, None

    def _current_backend():
        return source

    monkeypatch.setattr(fed, "make_safe_client", _safe_client)
    monkeypatch.setattr(persistence_helpers, "backend_or_503", _current_backend)

    async def _schema_ok(*args, **kwargs):
        return {
            "ok": True,
            "mnemos_version": "9.9.9",
            "schema_signature": "9.9",
            "migrations_fingerprint": None,
        }

    real_schema = fed._check_peer_schema
    fed._check_peer_schema = _schema_ok
    try:
        yield _NS(source=source, replica=replica)
    finally:
        monkeypatch.setattr(fed, "make_safe_client", real_make_safe_client)
        fed._check_peer_schema = real_schema
        await replica.close()
        await source.close()


@pytest.mark.asyncio
async def test_next_sync_repulls_repaired_rows(live_pair):
    """Apply clears fence + cursors, so the next ordinary sync restores the rows."""
    from mnemos.domain import federation as fed

    source, replica = live_pair.source, live_pair.replica
    async with source.transactional() as tx:
        for rid, text in (("one", "first fact"), ("two", "second fact")):
            await tx.conn.execute(
                "INSERT INTO memories(id, content, category, owner_id, namespace, "
                "permission_mode, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, text, "facts", "alice", "default", 644, NOW.isoformat(), NOW.isoformat()),
            )
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
    await fed.sync_peer(replica, peer["id"])
    got = await _all(replica, "SELECT id FROM memories WHERE federation_source = 'pythia'")
    assert {r["id"] for r in got} == {"fed:pythia:one", "fed:pythia:two"}

    # Pre-fence damage, the way an old build left it: archived + soft-deleted.
    await _exec(
        replica,
        "UPDATE memories SET archived_at = ?, content = ? WHERE id = ?",
        NOW.isoformat(),
        "ARCHIVED:fed:pythia:one",
        "fed:pythia:one",
    )
    await _exec(
        replica,
        "UPDATE memories SET deleted_at = ? WHERE id = ?",
        NOW.isoformat(),
        "fed:pythia:two",
    )
    dry = await repair_fenced_replicas(replica, peer["id"], dry_run=True)
    assert sorted(c["remote_id"] for c in dry["candidates"]) == ["one", "two"]
    applied = await repair_fenced_replicas(replica, peer["id"], dry_run=False)
    assert applied["repaired"] == 2
    assert applied["failed"] == []

    await fed.sync_peer(replica, peer["id"])
    rows = await _all(
        replica,
        "SELECT id, content, deleted_at, archived_at FROM memories WHERE federation_source = 'pythia' ORDER BY id",
    )
    assert [(r["id"], r["content"]) for r in rows] == [
        ("fed:pythia:one", "first fact"),
        ("fed:pythia:two", "second fact"),
    ]
    assert all(r["deleted_at"] is None and r["archived_at"] is None for r in rows)


@pytest.mark.asyncio
async def test_apply_repairs_candidates_past_the_first_page(replica):
    """The apply re-read pages to the candidate: >page_size damage all heals.

    Withdrawn rows sort first and are never deleted, so a first-page-only
    re-read would stall on them and silently skip every damaged row behind
    them (no counter records it).
    """
    peer = await _peer(replica)
    for i in range(3):
        await _replica_row(replica, f"aaa-kept-{i}", deleted_at=NOW.isoformat())
        await _fence(replica, f"aaa-kept-{i}", withdrawn=1)
    for i in range(3):
        await _replica_row(replica, f"zzz-lost-{i}", deleted_at=NOW.isoformat())
        await _fence(replica, f"zzz-lost-{i}")

    dry = await repair_fenced_replicas(replica, peer["id"], dry_run=True, page_size=2)
    assert len(dry["candidates"]) == 6
    applied = await repair_fenced_replicas(replica, peer["id"], dry_run=False, page_size=2)

    assert applied["repaired"] == 3
    assert applied["skipped_withdrawn"] == 3
    assert applied["failed"] == []
    remaining = await _all(replica, "SELECT id FROM memories WHERE federation_source = 'pythia'")
    assert sorted(r["id"] for r in remaining) == [f"fed:pythia:aaa-kept-{i}" for i in range(3)]


@pytest.mark.asyncio
async def test_apply_deletes_a_damaged_row_that_another_replica_points_at(replica):
    """A damaged replica that is the TARGET of a consolidation pointer deletes.

    ``memories.consolidated_into REFERENCES memories(id)`` (and SQLite runs
    with ``PRAGMA foreign_keys=ON``), so deleting a row that another row
    still points at fails with a FOREIGN KEY error. The repair collects
    per-row errors instead of raising, so the failure was invisible: the
    row stayed damaged and the run reported it in ``failed``.

    The scenario: ``aaa-target`` is damaged (soft-deleted) and ``zzz-loser``
    points at it. Candidates are repaired in ``remote_id`` order, so the
    TARGET is deleted first and hits the FK -- exactly the order that
    breaks. The repair must release the pointer first so the DELETE
    succeeds and BOTH rows heal.
    """
    peer = await _peer(replica)
    await _replica_row(replica, "aaa-target", deleted_at=NOW.isoformat())
    await _fence(replica, "aaa-target")
    await _replica_row(replica, "zzz-loser", consolidated_into="fed:pythia:aaa-target")
    await _fence(replica, "zzz-loser")

    report = await repair_fenced_replicas(replica, peer["id"], dry_run=False)

    # The FK blocker must not turn into a swallowed failure.
    assert report["failed"] == [], report["failed"]
    assert report["repaired"] == 2, report
    remaining = await _all(replica, "SELECT id FROM memories WHERE federation_source = 'pythia'")
    assert remaining == []


# ── the delete order itself ─────────────────────────────────────────────────


def _cand(remote_id: str, *, points_at: str | None = None, peer: str = "pythia") -> dict:
    return {
        "peer": peer,
        "remote_id": remote_id,
        "local_id": f"fed:{peer}:{remote_id}",
        "consolidated_into": f"fed:{peer}:{points_at}" if points_at else None,
    }


def test_repair_order_deletes_a_referrer_before_its_target():
    """The referrer goes first: the target's DELETE is not FK-blocked."""
    ordered = _repair_order([_cand("aaa-target"), _cand("zzz-loser", points_at="aaa-target")])
    assert [c["remote_id"] for c in ordered] == ["zzz-loser", "aaa-target"]


def test_repair_order_deletes_a_referrer_first_even_when_it_sorts_later():
    """The dependency wins over the stable order."""
    ordered = _repair_order([_cand("aaa-target"), _cand("zzz-loser", points_at="aaa-target")])
    pos = {c["remote_id"]: i for i, c in enumerate(ordered)}
    assert pos["zzz-loser"] < pos["aaa-target"]


def test_repair_order_keeps_the_stable_order_without_dependencies():
    """No dependency -> the stable (peer, remote_id) order is kept."""
    ordered = _repair_order([_cand("b"), _cand("a"), _cand("c")])
    assert [c["remote_id"] for c in ordered] == ["a", "b", "c"]


def test_repair_order_orders_a_chain_of_three():
    """c -> b -> a: the order must be c, b, a."""
    ordered = _repair_order(
        [_cand("a"), _cand("b", points_at="a"), _cand("c", points_at="b")]
    )
    assert [c["remote_id"] for c in ordered] == ["c", "b", "a"]


def test_repair_order_breaks_a_cycle_instead_of_hanging():
    """Two rows pointing at each other: no valid order exists, but the
    repair must still finish (the FK release breaks the cycle)."""
    ordered = _repair_order([_cand("a", points_at="b"), _cand("b", points_at="a")])
    assert sorted(c["remote_id"] for c in ordered) == ["a", "b"]


def test_repair_order_ignores_a_pointer_to_a_row_outside_the_candidates():
    """A pointer at a row that is not a candidate (a LOCAL row, say)
    cannot be ordered -- it is handled by the FK release instead."""
    ordered = _repair_order([_cand("a"), _cand("b", points_at="not-a-candidate")])
    assert [c["remote_id"] for c in ordered] == ["a", "b"]


def test_repair_order_scales_linearly_with_the_candidate_count():
    """The order must not be quadratic: the CLI allows 5000 per page."""
    import time

    candidates = [_cand(f"r{i:06d}") for i in range(4000)]
    start = time.perf_counter()
    ordered = _repair_order(candidates)
    elapsed = time.perf_counter() - start
    assert len(ordered) == 4000
    # A quadratic sort-and-remove takes ~1.5s here; the linear one is <0.1s.
    assert elapsed < 0.5, f"repair order took {elapsed:.3f}s for 4000 candidates"
