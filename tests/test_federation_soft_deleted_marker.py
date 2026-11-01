"""A replica soft-deleted locally must not vanish from the sync accounting.

Report finding 2 (M-plan `mnemos-code-health-decisions-20261031.md`, verified
against current master): every backend except SQLite filtered ``deleted_at``
out of ``fetch_federated_memory_marker`` (Postgres
``mnemos/persistence/postgres.py:5976-5991``, and the same clause in MySQL,
Oracle and Db2). SQLite's marker lookup had no such filter
(``mnemos/persistence/sqlite.py:6768``), so the two families behaved
differently:

* **SQLite** — the marker returned the soft-deleted row, the receiver took
  the "existing" branch, ``update_federated_memory_if_newer`` matched
  ``deleted_at IS NULL`` and therefore changed 0 rows, and the event was
  silently counted as neither new nor updated.
* **Postgres / MySQL / Oracle / Db2** — the marker returned ``None``, the
  receiver treated the replica as absent, ``insert_federated_memory``'s
  ``ON CONFLICT (id) DO NOTHING`` wrote nothing because the row *is* there,
  and the re-read after the losing insert also returned ``None``.

Either way the row stayed deleted and the sync reported a clean run: no
counter anywhere said "N events targeted rows this node has soft-deleted".

The fix makes the marker lookup return the deleted row (with its lifecycle
state) on all five dialects, and has the receiver count the skip as
``skipped_deleted`` — in the per-sync accounting, in ``federation_sync_log``,
and in one capped WARNING per sync. It deliberately does NOT undelete: a
replica's deleted lifecycle on this node is a policy decision, not
something a sync gets to overrule (see the separate repair job).

These tests run against real SQLite; ``tests/test_federation_marker_postgres_live.py``
runs the same scenario against a live Postgres when ``MNEMOS_TEST_DB`` is
set, and ``tests/test_federation_counter_backend_parity.py`` pins the SQL of
the other four dialects.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.domain import federation as fed


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    """A real SQLite backend with the journal installed."""
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    from mnemos.persistence.sqlite import SqliteBackend

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    instance = SqliteBackend(tmp_path / "fed-soft-deleted.db", settings)
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def _make_peer(backend, name="pythia"):
    async with backend.transactional() as tx:
        return await backend.federation.create_peer(
            tx,
            name=name,
            base_url="https://pythia.example",
            auth_token="token-abc",
            namespace_filter=None,
            category_filter=None,
            enabled=True,
            sync_interval_secs=300,
            compat_mode="permissive",
        )


def _wire_row(remote_id, *, content="facts", sequence=1, updated="2020-01-01T00:00:00Z"):
    return {
        "id": remote_id,
        "content": content,
        "category": "facts",
        "namespace": "default",
        "created": updated,
        "updated": updated,
        "federation_sequence": sequence,
    }


def _install_pull(monkeypatch, batches):
    """Serve one canned page per sync call from ``batches``."""
    state = {"index": 0}

    async def fake_pull(base_url, auth_token, since, namespace_filter, category_filter, **kwargs):
        index = min(state["index"], len(batches) - 1)
        state["index"] += 1
        body = batches[index]
        next_cursor = body.get("next_cursor")
        if next_cursor:
            next_cursor = fed._decode_feed_cursor(next_cursor)
        return body.get("memories", []), next_cursor, bool(body.get("has_more"))

    class _FakeFeedClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def fake_client(*args, **kwargs):
        return _FakeFeedClient(), object()

    async def schema_ok(*args, **kwargs):
        return {
            "ok": True,
            "mnemos_version": "7.0.0",
            "schema_signature": "7.0",
            "migrations_fingerprint": None,
        }

    monkeypatch.setattr(fed, "_check_peer_schema", schema_ok)
    monkeypatch.setattr(fed, "make_safe_client", fake_client)
    monkeypatch.setattr(fed, "_pull_batch", fake_pull)


def _page(rows, *, cursor="journal:9", has_more=False):
    return {
        "memories": rows,
        "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-05T00:00:00Z"), cursor),
        "has_more": has_more,
    }


async def _locals(backend, local_id):
    """The memory row as the database actually holds it, deleted or not."""
    from mnemos.persistence import sqlite as sqlite_backend

    async with backend.transactional() as tx:
        return await sqlite_backend._fetch_one(
            tx.conn,
            "SELECT id, content, deleted_at, federation_remote_updated FROM memories WHERE id = ?",
            (local_id,),
        )


async def _soft_delete(backend, local_id):
    """Soft-delete a replica the way a local lifecycle sweep would."""
    from mnemos.persistence import sqlite as sqlite_backend

    async with backend.transactional() as tx:
        await sqlite_backend._execute(
            tx.conn,
            "UPDATE memories SET deleted_at = '2020-01-10T00:00:00+00:00' WHERE id = ?",
            (local_id,),
        )


async def _sync_log(backend, peer_id):
    async with backend.transactional() as tx:
        return await backend.federation.fetch_sync_log(tx, peer_id, 50)


async def _peer_row(backend, peer_id):
    async with backend.transactional() as tx:
        return await backend.federation.get_peer(tx, peer_id)


# ── the regression ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_log_counts_a_locally_soft_deleted_replica(backend, monkeypatch):
    """Pull, soft-delete locally, apply a newer source revision.

    The row must stay deleted (no auto-undelete) AND the sync must say it
    skipped one deleted replica, in the sync log and in the returned
    counters. Before the fix this event was applied to nothing and reported
    as a clean sync with ``memories_pulled == 0`` and no explanation.
    """
    peer = await _make_peer(backend)
    local_id = "fed:pythia:m1"
    _install_pull(
        monkeypatch,
        [
            _page([_wire_row("m1", content="facts v1", sequence=1)]),
            _page([_wire_row("m1", content="facts v2", sequence=2, updated="2020-01-02T00:00:00Z")], cursor="journal:12"),
        ],
    )

    pulled, new, updated = await fed.sync_peer(backend, peer["id"])
    assert (pulled, new, updated) == (1, 1, 0)

    row = await _locals(backend, local_id)
    assert row["deleted_at"] is None, "the first sync must land a live replica"

    # A LOCAL operator/policy deletes the replica on this node.
    await _soft_delete(backend, local_id)

    # A newer source revision arrives for the same remote id.
    pulled, new, updated = await fed.sync_peer(backend, peer["id"])
    # Nothing was applied — the row is still soft-deleted and the receiver
    # does not get to undelete it.
    assert (pulled, new, updated) == (0, 0, 0), "a deleted replica must not be written back into the feed count"

    row = await _locals(backend, local_id)
    assert row["deleted_at"] is not None, "the receiver must not auto-undelete a locally deleted replica"
    assert row["content"] == "facts v1", "the newer source revision must not be written into a deleted replica"

    log = await _sync_log(backend, peer["id"])
    assert len(log) == 2
    assert log[0]["skipped_deleted"] == 0, "the first sync applied a live row; nothing was skipped"
    assert log[1]["skipped_deleted"] == 1, (
        "the second sync targeted a locally soft-deleted replica and must count it — "
        f"got skipped_deleted={log[1]['skipped_deleted']!r}"
    )
    # The skipped event is still a WIRE event, so the wire count sees it.
    assert log[1]["events_received"] == 1
    assert log[1]["memories_pulled"] == 0


@pytest.mark.asyncio
async def test_locally_soft_deleted_replica_is_logged_once_at_warning(backend, monkeypatch, caplog):
    """The skip must be visible in the log, capped at one line per sync."""
    peer = await _make_peer(backend)
    ids = [f"m{i}" for i in range(1, 6)]
    first_page = _page([_wire_row(rid, sequence=index + 1) for index, rid in enumerate(ids)])
    second_page = _page(
        [_wire_row(rid, content="v2", sequence=index + 10, updated="2020-01-02T00:00:00Z") for index, rid in enumerate(ids)],
        cursor="journal:20",
    )
    _install_pull(monkeypatch, [first_page, second_page])

    await fed.sync_peer(backend, peer["id"])
    for rid in ids:
        await _soft_delete(backend, f"fed:pythia:{rid}")

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mnemos.domain.federation"):
        await fed.sync_peer(backend, peer["id"])

    log = await _sync_log(backend, peer["id"])
    assert log[-1]["skipped_deleted"] == 5

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "deleted" in record.getMessage()
    ]
    assert len(lines) == 1, f"expected exactly one capped summary line, got {len(lines)}: {lines}"
    assert "5" in lines[0], f"the summary must carry the count, got: {lines[0]!r}"


@pytest.mark.asyncio
async def test_deleted_marker_reports_the_lifecycle_state(backend, monkeypatch):
    """The marker lookup must not hide a soft-deleted replica from the receiver.

    This is the mechanism, tested directly: the receiver's existence probe
    is how it decides "insert" vs "update". Hiding the row makes it try an
    insert that the primary key refuses, and the event disappears.
    """
    peer = await _make_peer(backend)
    _install_pull(monkeypatch, [_page([_wire_row("m1", sequence=1)])])
    await fed.sync_peer(backend, peer["id"])

    local_id = "fed:pythia:m1"
    await _soft_delete(backend, local_id)

    async with backend.transactional() as tx:
        marker = await backend.federation.fetch_federated_memory_marker(tx, local_id)
    assert marker is not None, "a soft-deleted replica is still a row; the marker lookup must return it"
    assert marker["deleted_at"] is not None, "the marker must carry the deleted state so the receiver can tell"


@pytest.mark.asyncio
async def test_live_replica_still_updates_after_a_deleted_sibling(backend, monkeypatch):
    """Counting the skip must not disturb the ordinary update path."""
    peer = await _make_peer(backend)
    _install_pull(
        monkeypatch,
        [
            _page([_wire_row("m1", content="v1", sequence=1), _wire_row("m2", content="v1", sequence=2)]),
            _page(
                [
                    _wire_row("m1", content="v2", sequence=10, updated="2020-01-02T00:00:00Z"),
                    _wire_row("m2", content="v2", sequence=11, updated="2020-01-02T00:00:00Z"),
                ],
                cursor="journal:20",
            ),
        ],
    )
    await fed.sync_peer(backend, peer["id"])
    await _soft_delete(backend, "fed:pythia:m1")

    pulled, new, updated = await fed.sync_peer(backend, peer["id"])
    assert (pulled, new, updated) == (1, 0, 1)

    log = await _sync_log(backend, peer["id"])
    assert log[-1]["skipped_deleted"] == 1
    assert log[-1]["memories_updated"] == 1, "the live replica must still update"

    live = await _locals(backend, "fed:pythia:m2")
    assert live["content"] == "v2"

    peer_row = await _peer_row(backend, peer["id"])
    assert peer_row["total_pulled"] == 3, "applied rows only: 2 from the first sync + 1 update"


@pytest.mark.asyncio
async def test_skipped_deleted_accumulates_across_pages_in_one_sync(backend, monkeypatch):
    """Pin: the per-sync skip count sums EVERY page, not just the last one.

    ``_store_memories`` reports each batch's delta through a shared
    ``counters`` dict that it overwrites on every call, so the pull loop has
    to settle the delta into a running total per page
    (``mnemos/domain/federation.py`` in ``_sync_peer_locked``). This drives
    two pages in a single sync with UNEQUAL skip counts (1 on the first,
    2 on the second) and asserts the sync log records the sum, 3.

    Unequal page sizes matter: with equal deltas a "read the dict once at
    the end" implementation lands on the same number by coincidence and the
    test would pass against a broken loop. Verified: this reads 1 + 2 = 3,
    and a variant with 2 skips on page 1 and 0 on page 2 records 2 (the
    per-page reads), not 0.
    """
    peer = await _make_peer(backend)
    ids = [f"m{i}" for i in range(1, 5)]
    _install_pull(
        monkeypatch,
        [
            _page([_wire_row(rid, sequence=index + 1) for index, rid in enumerate(ids)], cursor="journal:4"),
            # Second sync: TWO pages in one pull, with UNEQUAL skip counts
            # (one deleted replica on page 1, two on page 2).
            _page(
                [_wire_row("m1", content="v2", sequence=10, updated="2020-01-02T00:00:00Z")],
                cursor="journal:10",
                has_more=True,
            ),
            _page(
                [
                    _wire_row("m2", content="v2", sequence=11, updated="2020-01-02T00:00:00Z"),
                    _wire_row("m3", content="v2", sequence=12, updated="2020-01-02T00:00:00Z"),
                ],
                cursor="journal:12",
                has_more=False,
            ),
        ],
    )

    await fed.sync_peer(backend, peer["id"])
    for rid in ("m1", "m2", "m3"):
        await _soft_delete(backend, f"fed:pythia:{rid}")

    pulled, new, updated = await fed.sync_peer(backend, peer["id"])
    assert (pulled, new, updated) == (0, 0, 0), "every event targets a soft-deleted replica; none applies"

    log = await _sync_log(backend, peer["id"])
    assert log[0]["skipped_deleted"] == 0, "the bootstrap sync applied live rows; nothing was skipped"
    assert log[-1]["skipped_deleted"] == 3, (
        "skips must accumulate across pages within one sync (1 + 2 = 3); "
        f"got skipped_deleted={log[-1]['skipped_deleted']!r}"
    )
    assert log[-1]["events_received"] == 3, "both pages' events are wire events"
