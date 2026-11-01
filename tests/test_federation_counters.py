"""Federation counters, cursor bootstrap and embedding-gap regressions (F-1, F-2, F-5).

Three field-visible defects from the Fable federation report share one root:
the federation receiver is correct for a healthy static corpus and silent
for everything else.

F-1 — ``total_pulled`` counted WIRE EVENTS, not applied rows.  A journal
replay emits one row per event, so 25 memories with 5 updates produced 30
wire rows; the receiver added ``len(batch)`` before the durable fence ran,
so ``federation_peers.total_pulled`` grew by 30 while only 25 rows were
ever applied.  At a 300 s poll interval that inflation is what made the
PROTEUS replica report 4.4 M pulled against 25 k rows.  ``total_pulled``
must count APPLIED rows (``new_n + upd_n``), the wire count must be visible
separately as ``events_received``, and a manual ``trigger_sync`` must not be
able to overlap the background worker on the same peer.

F-2 — A sync that starts without a journal cursor silently replays the
whole journal from sequence 0 on every poll.  That is the mechanism behind
the 4.4 M: the receiver had no ``federation_peer_cursors`` row, so the
timestamp cursor was re-encoded with an empty lower id, the source read it
as "no sequence" and replayed every event.  The sync must WARN loudly and
the receiver must bootstrap the cursor from the first successful page.

F-5 — Every reason the embedding copy declined a peer vector logged at
DEBUG, so a replica whose 24 986 rows all had NULL embeddings looked
healthy.  The skips must be WARNINGs, counted per sync, and the resulting
gap must be visible per peer in ``/v1/federation/status`` as
``rows_without_embedding``.

The tests here run against real SQLite (the backend the report reproduced
on); the other backends' SQL is covered by the static parity test.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.domain import federation as fed


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    """A real SQLite backend with the journal installed."""
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    from mnemos.persistence.sqlite import SqliteBackend

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    instance = SqliteBackend(tmp_path / "fed-counters.db", settings)
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def _make_peer(backend, name="pythia", **kwargs):
    """Register a peer and return its row."""
    async with backend.transactional() as tx:
        return await backend.federation.create_peer(
            tx,
            name=name,
            base_url=kwargs.get("base_url", "https://pythia.example"),
            auth_token=kwargs.get("auth_token", "token-abc"),
            namespace_filter=None,
            category_filter=None,
            enabled=True,
            sync_interval_secs=300,
            compat_mode="permissive",
        )


async def _peer_row(backend, peer_id):
    async with backend.transactional() as tx:
        return await backend.federation.get_peer(tx, peer_id)


async def _sync_log(backend, peer_id):
    async with backend.transactional() as tx:
        return await backend.federation.fetch_sync_log(tx, peer_id, 50)


def _wire_row(remote_id, *, content="facts", sequence=1, updated="2020-01-01T00:00:00Z"):
    """One inbound federation wire item, shaped like /v1/federation/feed."""
    return {
        "id": remote_id,
        "content": content,
        "category": "facts",
        "namespace": "default",
        "created": updated,
        "updated": updated,
        "federation_sequence": sequence,
    }


class _FakeFeedClient:
    """Minimal stand-in for the HTTP client that pulls /v1/federation/feed."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.requests.append(dict(params or {}))
        index = min(len(self.requests) - 1, len(self._batches) - 1)
        body = self._batches[index]
        return SimpleNamespace(
            status_code=200,
            json=lambda: body,
            raise_for_status=lambda: None,
        )


def _install_pull(monkeypatch, batches, *, calls=None):
    """Wire ``_pull_batch`` to serve canned pages and record the calls."""
    client = _FakeFeedClient(batches)
    state = {"index": 0}

    async def fake_pull(base_url, auth_token, since, namespace_filter, category_filter, **kwargs):
        if calls is not None:
            calls.append(since)
        index = min(state["index"], len(batches) - 1)
        state["index"] += 1
        body = batches[index]
        next_cursor = body.get("next_cursor")
        if next_cursor:
            next_cursor = fed._decode_feed_cursor(next_cursor)
        return body.get("memories", []), next_cursor, bool(body.get("has_more"))

    async def fake_client(*args, **kwargs):
        return client, object()

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
    return client


# ── F-1: total_pulled counts applied rows, not wire events ───────────────────


@pytest.mark.asyncio
async def test_total_pulled_counts_applied_rows_not_wire_events(backend, monkeypatch):
    """A replay of 25 rows + 5 updates applies 25 rows and pulls 25, not 30.

    The journal replays one wire row per EVENT, so the same memory arrives
    once per mutation.  Counting wire rows inflated the counter by the
    number of duplicate events — the 4.4 M-against-25 k symptom.
    """
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [_wire_row("m1", sequence=1)],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:1"),
            "has_more": True,
        },
        {
            "memories": [_wire_row("m1", content="facts v2", sequence=2)],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-03T00:00:00Z"), "journal:2"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)

    pulled, new, updated = await fed.sync_peer(backend, peer["id"])

    # Two wire events were received, and two rows were applied: one
    # insert, one update.  The APPLIED count is 2, and the wire count is
    # visible separately as events_received.
    assert (pulled, new, updated) == (2, 1, 1)

    row = await _peer_row(backend, peer["id"])
    assert row["total_pulled"] == 2

    log = await _sync_log(backend, peer["id"])
    assert len(log) == 1
    assert log[0]["memories_pulled"] == 2
    assert log[0]["events_received"] == 2


@pytest.mark.asyncio
async def test_sync_log_records_events_received_separately_from_pulled(backend, monkeypatch):
    """``events_received`` counts the wire; ``memories_pulled`` counts applied."""
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [
                _wire_row("m1", sequence=1),
                _wire_row("m2", sequence=2),
            ],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:2"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)

    await fed.sync_peer(backend, peer["id"])

    log = await _sync_log(backend, peer["id"])
    assert len(log) == 1
    entry = log[0]
    assert entry["events_received"] == 2
    assert entry["memories_pulled"] == 2
    assert entry["memories_new"] == 2


@pytest.mark.asyncio
async def test_replaying_a_batch_twice_grows_total_pulled_once(backend, monkeypatch):
    """The regression the task names: replay a batch twice, counter grows once.

    The second pull re-sends the same events.  The durable sequence fence
    rejects them, so no row is applied and ``total_pulled`` must not move.
    Before the fix the counter grew by ``len(batch)`` on every replay.
    """
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [_wire_row("m1", sequence=1)],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:1"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)

    await fed.sync_peer(backend, peer["id"])
    first = await _peer_row(backend, peer["id"])
    assert first["total_pulled"] == 1

    # Replay the identical batch: the fence rejects every event.
    await fed.sync_peer(backend, peer["id"])
    second = await _peer_row(backend, peer["id"])
    assert second["total_pulled"] == 1, "replay must not grow total_pulled"

    log = await _sync_log(backend, peer["id"])
    assert len(log) == 2
    # The second run saw the wire event but applied nothing.
    assert log[-1]["events_received"] == 1
    assert log[-1]["memories_pulled"] == 0
    assert log[-1]["memories_new"] == 0


@pytest.mark.asyncio
async def test_concurrent_syncs_on_one_peer_do_not_overlap(backend, monkeypatch):
    """A manual trigger and the background worker must not run concurrently.

    Without a per-peer lock both pull the same pages and both apply the
    same rows, so the applied-row count (and the audit trail) doubled.
    """
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [_wire_row("m1", sequence=1)],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:1"),
            "has_more": False,
        },
    ]
    in_flight = 0
    max_in_flight = 0

    async def slow_pull(base_url, auth_token, since, namespace_filter, category_filter, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.05)
            body = batches[0]
            return body["memories"], fed._decode_feed_cursor(body["next_cursor"]), False
        finally:
            in_flight -= 1

    async def fake_client(*args, **kwargs):
        return _FakeFeedClient(batches), object()

    async def schema_ok(*args, **kwargs):
        return {
            "ok": True,
            "mnemos_version": "7.0.0",
            "schema_signature": "7.0",
            "migrations_fingerprint": None,
        }

    monkeypatch.setattr(fed, "_check_peer_schema", schema_ok)
    monkeypatch.setattr(fed, "make_safe_client", fake_client)
    monkeypatch.setattr(fed, "_pull_batch", slow_pull)

    await asyncio.gather(
        fed.sync_peer(backend, peer["id"]),
        fed.sync_peer(backend, peer["id"]),
    )

    assert max_in_flight == 1, "two syncs for one peer overlapped"
    row = await _peer_row(backend, peer["id"])
    assert row["total_pulled"] == 1


def test_peer_lock_is_usable_from_a_second_event_loop():
    """The per-peer lock must not leak across event loops.

    An ``asyncio.Lock`` binds to the loop that first awaits it under
    contention. A process that serves several loops over its lifetime — a
    CLI ``asyncio.run`` per command, a test that opens a fresh loop per
    case, the lifespan worker next to a one-shot command — would raise
    "is bound to a different event loop" on the second loop if the lock
    were shared across loops. The lock is therefore keyed by (loop, peer).

    The test drives several SEPARATE ``asyncio.run`` calls, i.e. several
    real event loops in one process, and CONTENDS inside each one — the
    contention is what makes the lock bind to the running loop, which is
    the state that breaks a lock shared across loops.
    """
    peer_id = "peer-lock-probe"

    async def contend() -> None:
        lock = fed._peer_sync_lock(peer_id)

        async def worker(index: int) -> int:
            async with lock:
                await asyncio.sleep(0.02)
                return index

        await asyncio.gather(worker(0), worker(1))

    # First loop: contends, so the lock binds to this loop.
    asyncio.run(contend())
    # Second loop: contends again. With a lock shared across loops this
    # raises "is bound to a different event loop".
    asyncio.run(contend())

    # Third loop: uncontended, i.e. the common single-sync path.
    async def single() -> None:
        async with fed._peer_sync_lock(peer_id):
            pass

    asyncio.run(single())

    # A second peer must get its own lock, and a second loop must not
    # share the first loop's lock object.
    async def check_distinct() -> None:
        other = fed._peer_sync_lock("peer-lock-probe-other")
        assert other is not fed._peer_sync_lock(peer_id)

    asyncio.run(check_distinct())


# ── F-2: no journal cursor → WARN, and bootstrap the cursor ──────────────────


@pytest.mark.asyncio
async def test_sync_without_journal_cursor_warns_and_bootstraps_cursor(backend, monkeypatch, caplog):
    """A legacy (timestamp) cursor must warn loudly and be replaced.

    Without a ``federation_peer_cursors`` row the receiver sends a
    timestamp cursor, the source reads it as "no sequence" and replays
    every journal event — on every poll.  The sync must say so, and the
    first successful page must bootstrap a real journal cursor.
    """
    peer = await _make_peer(backend)
    # A legacy timestamp cursor on the peer row (what a pre-journal
    # receiver ends up with: a bare timestamp, not a journal cursor).
    async with backend.transactional() as tx:
        await backend.federation.record_sync_success(tx, peer["id"], "2020-01-01T00:00:00Z", 0)

    batches = [
        {
            "memories": [_wire_row("m1", sequence=1)],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:1"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)

    with caplog.at_level(logging.WARNING, logger="mnemos.domain.federation"):
        await fed.sync_peer(backend, peer["id"])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("full replay" in message and "seq 0" in message for message in warnings), (
        f"expected a full-replay warning, got: {warnings}"
    )

    # The cursor row now exists and carries a journal cursor.
    from mnemos.persistence import federation_journal as journal

    async with backend.transactional() as tx:
        saved = await journal.load_peer_cursor(tx, peer["id"], _filter_signature(peer))
    assert saved is not None
    assert fed._decode_feed_cursor(saved).memory_id.startswith("journal:")


@pytest.mark.asyncio
async def test_replay_warning_reports_the_number_of_events(backend, monkeypatch, caplog):
    """The warning must say HOW MANY events were replayed, not just that it happened.

    The task names the shape explicitly: 'full replay from seq 0, N
    events'. An operator reading only the warning needs the magnitude of
    the replay to judge whether the poll interval is tolerable.
    """
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [
                _wire_row("m1", sequence=1),
                _wire_row("m2", sequence=2),
                _wire_row("m3", sequence=3),
            ],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:3"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)

    with caplog.at_level(logging.WARNING, logger="mnemos.domain.federation"):
        await fed.sync_peer(backend, peer["id"])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    replay = [m for m in warnings if "full replay" in m]
    assert replay, f"expected a full-replay warning, got: {warnings}"
    # The count of replayed events must appear in the warning.
    assert any("3 events" in message for message in replay), (
        f"the replay warning must report the event count, got: {replay}"
    )
    assert any("seq 0" in message for message in replay), f"the replay warning must name seq 0, got: {replay}"


def _filter_signature(peer):
    """The signature sync_peer computes for a peer's filter scope."""
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            [peer["base_url"], peer["namespace_filter"], peer["category_filter"]],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


@pytest.mark.asyncio
async def test_second_sync_after_bootstrap_does_not_warn_about_replay(backend, monkeypatch, caplog):
    """The replay warning fires once, then stops once the cursor exists.

    The bootstrap is the fix: the receiver must carry a journal cursor
    from the FIRST successful page, so the next poll resumes instead of
    replaying the whole journal.  The first sync therefore warns (it
    really was a full replay from seq 0) and the second must not.
    """
    peer = await _make_peer(backend)
    batches = [
        {
            "memories": [_wire_row("m1", sequence=1)],
            "next_cursor": fed._encode_feed_cursor(fed._parse_cursor_timestamp("2020-01-02T00:00:00Z"), "journal:1"),
            "has_more": False,
        },
    ]
    _install_pull(monkeypatch, batches)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mnemos.domain.federation"):
        await fed.sync_peer(backend, peer["id"])

    first_warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("full replay" in message for message in first_warnings), (
        f"the first sync had no journal cursor and must say so: {first_warnings}"
    )

    # The cursor row exists and carries a journal cursor.
    from mnemos.persistence import federation_journal as journal

    async with backend.transactional() as tx:
        saved = await journal.load_peer_cursor(tx, peer["id"], _filter_signature(peer))
    assert saved is not None, "the first successful page must bootstrap the peer cursor"
    assert fed._decode_feed_cursor(saved).memory_id.startswith("journal:")

    # Only the SECOND sync's warnings are under test.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mnemos.domain.federation"):
        await fed.sync_peer(backend, peer["id"])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("full replay" in message for message in warnings), (
        f"unexpected replay warning after bootstrap: {warnings}"
    )


# ── F-5: embed skips are loud, counted, and visible in /status ───────────────


@pytest.mark.asyncio
async def test_embed_skip_is_logged_at_warning_and_counted(backend, monkeypatch, caplog):
    """A declined peer vector must not be a DEBUG line."""
    peer = await _make_peer(backend)
    # A peer vector whose model does not match the local one is declined.
    row = _wire_row("m1", sequence=1)
    row["embedding"] = "AAAA"
    row["embedding_model"] = "some-other-model"
    row["embedding_dim"] = 3
    batches = [{"memories": [row], "next_cursor": None, "has_more": False}]
    _install_pull(monkeypatch, batches)

    # Resolve the local embedder so the skip is the MODEL branch, not the
    # "local embedder unresolved" branch.
    monkeypatch.setattr(fed, "_local_embed_fingerprint", lambda: ("local-model", 3), raising=False)

    with caplog.at_level(logging.WARNING, logger="mnemos.domain.federation"):
        await fed.sync_peer(backend, peer["id"])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("[federation/embed] skip" in message for message in warnings), (
        f"expected an embed skip warning, got: {warnings}"
    )

    log = await _sync_log(backend, peer["id"])
    assert log[0]["rows_without_embedding"] == 1


@pytest.mark.asyncio
async def test_status_exposes_rows_without_embedding_per_peer(backend):
    """``/v1/federation/status`` must show the embedding gap per peer."""
    from mnemos.api.routes import federation as routes

    peer = await _make_peer(backend)
    # A federated row without an embedding.
    async with backend.transactional() as tx:
        await backend.federation.insert_federated_memory(
            tx,
            local_id=f"fed:{peer['name']}:m1",
            content="no vector here",
            category="facts",
            subcategory=None,
            metadata_json="{}",
            verbatim_content="no vector here",
            quality_rating=75,
            namespace="default",
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            peer_name=peer["name"],
            remote_updated="2020-01-01T00:00:00Z",
        )

    async with backend.transactional() as tx:
        rows = await backend.federation.list_peers(tx)
        counts = await backend.federation.count_federated_rows_without_embedding_per_peer(tx)
    assert counts.get(peer["name"]) == 1

    # The route maps the count onto the peer payload.
    payload = routes._to_peer(rows[0], rows_without_embedding=counts.get(peer["name"], 0))
    assert payload.rows_without_embedding == 1
