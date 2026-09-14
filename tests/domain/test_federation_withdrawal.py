"""F07 — HTTP federation feed must emit withdrawal/tombstone events for rows
that leave the live feed (soft-delete, archive, permission-narrowing offsite,
or manual vault move).

The bug: prior to F07, when a row's authorized source fetch became empty
(deleted, archived, or made private while MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE=0),
the HTTP feed simply stopped returning that id. A polling replica kept the
now-invalid copy forever because nothing ever told it to drop it. NATS
already handled the equivalent transition correctly via the
``memory.deleted`` subject → ``delete_federated_memory`` path; the HTTP
counterpart is what this fix wires up.

Three real-SqliteBackend scenarios in this file:

  1. ``test_feed_emits_withdrawal_when_row_is_soft_deleted_offsite`` —
     reproduce the reviewer's case: insert a public (644) row,
     confirm the feed returns it; soft-delete; confirm the next feed
     call returns a ``FederationWithdrawalEvent`` (not a MemoryItem, not
     silent absence) for that id.

  2. ``test_feed_emits_withdrawal_when_permission_mode_narrows_offsite``
     — same posture (offsite, world-read gate on), but the row is
     narrowed from 644 to 600 instead of being deleted. Withdrawal
     emitted for the same reason: the row left the exportable set.

  3. ``test_receiver_applies_withdrawal_and_drops_local_copy`` — drive
     ``_store_memories`` with a withdrawal event against a row that was
     previously imported via ``insert_federated_memory`` (so the local
     row exists with federation_source = peer). Assert the row is gone
     afterward, and that a no-op withdrawal (idempotent, missing row)
     does not raise.

A parity check (assertion 4) confirms the HTTP-withdrawal event and the
NATS hard-delete convey the same information: same remote id, same
"this row no longer exists on the source" semantics. NATS path is
exercised against the same SQLite backend by calling
``backend.federation.delete_federated_memory`` (the same call NATS uses
via ``nats_consumer.delete_federated_memory``) and asserting the row
count drop is identical.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _offsite_feed_scope(monkeypatch):
    """Pin the OFFSITE posture so the world-read gate applies.

    F07 specifically targets MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE=0:
    in the trusted-LAN posture (the default), permission-mode narrowing
    is meaningless for federation and only deletes/archives trigger
    withdrawals. The reviewer's reproduction explicitly disabled
    private export, which is what we're testing here.
    """
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")


def _build_settings(tmp_path):
    """Minimal settings stub with embedding_dim so SqliteBackend opens."""
    return SimpleNamespace(
        database=SimpleNamespace(embedding_dim=3),
        server=SimpleNamespace(session_secret="test-secret-for-f07"),
        providers=SimpleNamespace(inference_embed_model=""),
    )


async def _install_in_lifecycle(backend, monkeypatch):
    """Make ``require_federation_backend()`` return this SqliteBackend.

    The federation route reads ``mnemos.core.lifecycle._persistence_backend``
    via the helpers module; tests that exercise the real route handler
    need that slot populated. We don't touch _pool (Postgres-shaped)
    or anything else — just the one attribute the federation route
    actually consumes.
    """
    import mnemos.core.lifecycle as _lc

    monkeypatch.setattr(_lc, "_persistence_backend", backend)


async def _insert_memory(
    backend,
    tx,
    *,
    memory_id: str,
    content: str,
    updated: datetime,
    permission_mode: int = 644,
    namespace: str = "default",
):
    """Insert a local memory the way a normal create_memory call would."""
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=content,
        category="facts",
        subcategory=None,
        metadata_json='{"source":"f07-withdrawal-test"}',
        quality_rating=75,
        owner_id="f07-owner",
        namespace=namespace,
        permission_mode=permission_mode,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=content,
        created=updated,
        updated=updated,
    )


@asynccontextmanager
async def _sqlite_backend(tmp_path):
    """Open a fresh real SQLite backend for one test."""
    from mnemos.persistence import SqliteBackend

    backend = SqliteBackend(tmp_path / "f07-withdrawal.sqlite3", _build_settings(tmp_path))
    await backend.open()
    try:
        yield backend
    finally:
        await backend.close()


def _all_event_types(memories):
    """Return the per-item `type` string, or None for a MemoryItem."""
    out: list[str | None] = []
    for m in memories:
        t = getattr(m, "type", None)
        if t is None:
            # Plain MemoryItem (no .type attribute set on it).
            out.append(None)
        else:
            out.append(t)
    return out


@pytest.mark.asyncio
async def test_feed_emits_withdrawal_when_row_is_soft_deleted_offsite(tmp_path, monkeypatch):
    """Reviewer's reproduction: offsite feed, insert public row, soft-delete,
    next feed call returns a withdrawal event for that id (not silent absence).
    """
    from mnemos.api.routes import federation as handler

    async with _sqlite_backend(tmp_path) as backend:
        await _install_in_lifecycle(backend, monkeypatch)
        now = datetime.now(timezone.utc)
        # Step 1: insert a world-readable (644) row. With the offsite gate,
        # 644 passes (644 % 10 == 4 >= 4).
        async with backend.transactional() as tx:
            await _insert_memory(
                backend,
                tx,
                memory_id="f07-pub-1",
                content="public row that will be deleted",
                updated=now,
                permission_mode=644,
            )

        # Step 2: first feed call — the row IS in the live feed.
        async with backend.transactional() as tx:
            rows = await backend.federation.feed_query(
                tx,
                since_updated=None,
                since_id=None,
                namespaces=[],
                categories=[],
                limit=10,
                prefer_compressed=False,
            )
        assert [r["id"] for r in rows] == ["f07-pub-1"], (
            "setup sanity: public 644 row must be in the live feed under "
            "the offsite posture"
        )
        assert rows[0]["type"] is None  # plain MemoryItem

        # Step 3: soft-delete the row. Use a direct UPDATE because the
        # public soft_delete_memory path requires a VisibilityFilter; this
        # is the same end state (deleted_at IS NOT NULL, updated bumped).
        async with backend.transactional() as tx:
            from mnemos.persistence.sqlite import _execute
            await _execute(
                tx.conn,
                "UPDATE memories SET deleted_at = CURRENT_TIMESTAMP, "
                "updated = CURRENT_TIMESTAMP WHERE id = ?",
                ("f07-pub-1",),
            )

        # Step 4: next feed call — must surface a withdrawal, NOT just
        # silently drop the row.
        response = await handler.federation_feed(
            None, None,
            since=None, namespace=None, category=None, limit=10,
            prefer_compressed=False, copy_embeddings=False,
        )
        event_types = _all_event_types(response.memories)
        ids = [m.id for m in response.memories]
        assert "f07-pub-1" in ids, (
            "the deleted row's id must still appear, but as a "
            "FederationWithdrawalEvent — not be silently absent"
        )
        withdrawal = next(
            m for m in response.memories
            if m.id == "f07-pub-1"
            and getattr(m, "type", None) == "withdrawal"
        )
        assert withdrawal.type == "withdrawal"
        assert withdrawal.namespace == "default"
        assert withdrawal.withdrawn_at  # ISO timestamp populated
        assert withdrawal.reason == "ineligible"
        # MemoryItem variant must NOT have leaked through for the deleted row.
        assert None not in event_types or len([t for t in event_types if t is None]) == 0, (
            "the deleted row must NOT be emitted as a live MemoryItem "
            f"(got event_types={event_types})"
        )


@pytest.mark.asyncio
async def test_feed_emits_withdrawal_when_permission_mode_narrows_offsite(tmp_path, monkeypatch):
    """The other reviewer's reproduction: row is narrowed from world-readable
    to owner-only while offsite export is on. The row was federated; now it
    isn't. The HTTP feed must say so.
    """
    from mnemos.api.routes import federation as handler

    async with _sqlite_backend(tmp_path) as backend:
        await _install_in_lifecycle(backend, monkeypatch)
        now = datetime.now(timezone.utc)
        async with backend.transactional() as tx:
            await _insert_memory(
                backend,
                tx,
                memory_id="f07-narrow-1",
                content="about to be made private",
                updated=now,
                permission_mode=644,
            )

        # Sanity: the row is in the live feed.
        async with backend.transactional() as tx:
            rows = await backend.federation.feed_query(
                tx,
                since_updated=None,
                since_id=None,
                namespaces=[],
                categories=[],
                limit=10,
                prefer_compressed=False,
            )
        assert [r["id"] for r in rows] == ["f07-narrow-1"]

        # Now narrow permission_mode from 644 to 600 (owner-only).
        # 600 % 10 == 0, which fails the world-read gate.
        async with backend.transactional() as tx:
            from mnemos.persistence.sqlite import _execute
            await _execute(
                tx.conn,
                "UPDATE memories SET permission_mode = 600, "
                "updated = CURRENT_TIMESTAMP WHERE id = ?",
                ("f07-narrow-1",),
            )

        response = await handler.federation_feed(
            None, None,
            since=None, namespace=None, category=None, limit=10,
            prefer_compressed=False, copy_embeddings=False,
        )
        ids = [m.id for m in response.memories]
        assert "f07-narrow-1" in ids
        withdrawal = next(
            m for m in response.memories
            if m.id == "f07-narrow-1"
            and getattr(m, "type", None) == "withdrawal"
        )
        assert withdrawal.type == "withdrawal"
        # Crucial: not delivered as a live MemoryItem.
        assert not any(
            m.id == "f07-narrow-1" and getattr(m, "type", None) is None
            for m in response.memories
        ), "narrowed row must not appear as a live MemoryItem"


@pytest.mark.asyncio
async def test_receiver_applies_withdrawal_and_drops_local_copy(tmp_path):
    """End-to-end: an HTTP-feed withdrawal event actually removes the row
    that was previously imported from the same peer.

    This proves the signal is *actionable*, not just present in the wire
    format — i.e. a polling replica that receives and applies the event
    no longer carries the now-stale copy.
    """
    from mnemos.domain.federation import _store_memories

    async with _sqlite_backend(tmp_path) as backend:
        # Step 1: simulate that the receiver had previously pulled this
        # row from the same peer (i.e. insert it as a federated row).
        remote_id = "f07-receiver-1"
        now = datetime.now(timezone.utc)
        async with backend.transactional() as tx:
            await backend.federation.insert_federated_memory(
                tx,
                local_id=f"fed:peer-a:{remote_id}",
                content="imported from peer-a",
                category="facts",
                subcategory=None,
                metadata_json='{"federation_remote_id":"f07-receiver-1"}',
                verbatim_content="imported from peer-a",
                quality_rating=75,
                namespace="default",
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                peer_name="peer-a",
                remote_updated=now,
            )

        async with backend.transactional() as tx:
            from mnemos.persistence.sqlite import _fetch_all
            rows = await _fetch_all(
                tx.conn,
                "SELECT id, federation_source FROM memories "
                "WHERE federation_source = 'peer-a'",
            )
        assert [r["id"] for r in rows] == [f"fed:peer-a:{remote_id}"], (
            "setup sanity: the federated row must exist before withdrawal"
        )

        # Step 2: drive _store_memories with a withdrawal event arriving
        # on the HTTP feed for that same remote_id.
        withdrawal_event = {
            "type": "withdrawal",
            "id": remote_id,
            "namespace": "default",
            "withdrawn_at": now.isoformat(),
            "reason": "ineligible",
        }
        async with backend.transactional() as tx:
            new_n, upd_n = await _store_memories(
                backend.federation,
                tx,
                "peer-a",
                [withdrawal_event],
            )
        # The withdrawal isn't an "update" of an existing row — it's a
        # hard delete. Both counters should be 0; the row is gone.
        assert new_n == 0
        assert upd_n == 0

        # Step 3: confirm the local federated copy is gone.
        async with backend.transactional() as tx:
            from mnemos.persistence.sqlite import _fetch_all
            rows = await _fetch_all(
                tx.conn,
                "SELECT id FROM memories WHERE federation_source = 'peer-a'",
            )
        assert rows == [], (
            "the receiver must drop the federated copy on receipt of the "
            "withdrawal event — the whole point of F07"
        )

        # Step 4: idempotency. Apply the same withdrawal again — must not
        # raise. Missing rows are not errors.
        async with backend.transactional() as tx:
            await _store_memories(
                backend.federation,
                tx,
                "peer-a",
                [withdrawal_event],
            )


@pytest.mark.asyncio
async def test_http_withdrawal_is_equivalent_to_nats_hard_delete(tmp_path):
    """Parity check: the HTTP-withdrawal event and the NATS path's hard-delete
    delete the same federated row, with the same end-state and the same
    information ("this id is gone").

    A receiver watching EITHER transport converges to the same state.
    """
    from mnemos.domain.federation import _apply_withdrawal

    async with _sqlite_backend(tmp_path) as backend:
        now = datetime.now(timezone.utc)
        # Two separate "peers" — one delivered via HTTP, one via NATS.
        # Same remote_id, different peer names so each side has its own
        # federated row to delete.
        for peer in ("peer-http", "peer-nats"):
            async with backend.transactional() as tx:
                await backend.federation.insert_federated_memory(
                    tx,
                    local_id=f"fed:{peer}:parity-1",
                    content=f"imported from {peer}",
                    category="facts",
                    subcategory=None,
                    metadata_json='{"federation_remote_id":"parity-1"}',
                    verbatim_content=f"imported from {peer}",
                    quality_rating=75,
                    namespace="default",
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    peer_name=peer,
                    remote_updated=now,
                )

        # Pre-state: both rows present.
        async with backend.transactional() as tx:
            from mnemos.persistence.sqlite import _fetch_all
            rows = await _fetch_all(tx.conn, "SELECT id FROM memories ORDER BY id")
        assert {r["id"] for r in rows} == {
            "fed:peer-http:parity-1",
            "fed:peer-nats:parity-1",
        }

        # HTTP path: drive _apply_withdrawal on the HTTP-side row.
        async with backend.transactional() as tx:
            http_deleted = await _apply_withdrawal(
                backend.federation,
                tx,
                "peer-http",
                {
                    "type": "withdrawal",
                    "id": "parity-1",
                    "namespace": "default",
                    "withdrawn_at": now.isoformat(),
                    "reason": "ineligible",
                },
            )
        assert http_deleted == 1

        # NATS path: drive backend.federation.delete_federated_memory
        # directly — this is exactly what nats_consumer.delete_federated_memory
        # calls on a memory.deleted subject.
        async with backend.transactional() as tx:
            nats_deleted = await backend.federation.delete_federated_memory(
                tx, "peer-nats", "parity-1"
            )
        assert nats_deleted == 1

        # End-state: both rows are gone, both transports converge.
        async with backend.transactional() as tx:
            from mnemos.persistence.sqlite import _fetch_all
            rows = await _fetch_all(tx.conn, "SELECT id FROM memories")
        assert rows == [], (
            "after both transports have applied their respective signal "
            "(HTTP withdrawal, NATS delete), the federated copies must "
            f"all be gone — found {rows}"
        )
