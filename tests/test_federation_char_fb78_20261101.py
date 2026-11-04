"""FB78 characterisation tests: federation parity and journal compaction.

The lane brief asked for new test files only, pinning today's behaviour of
``mnemos/domain/federation/parity.py`` and
``mnemos/persistence/federation_journal.py`` for reconcile, redispatch,
compaction and peer cursor state -- including the error paths:

* the parity report run on a replica that cannot read the source
  (``source_ids=None``): what it claims and what it refuses to claim;
* ``redispatch_memory_ids`` across one and several ``_PUBLISH_BATCH`` (256)
  publish windows;
* the compaction cutoff (``_cutoff`` clamping and the zero-days form);
* peer cursor save/load keyed state.

Everything here runs against the real ``SqliteBackend`` (the shape the
existing ``tests/test_federation_journal_compaction.py`` exercises); rows are
seeded with raw SQL through ``_Ops``, never through HTTP routes. This is a
characterisation suite: assertions record behaviour as it shipped.

One suspected defect was investigated (``save_peer_cursor`` replacing a
peer's cursor row when its ``filter_signature`` changes). It is **not** a
defect: ``federation_peer_cursors`` is ``PRIMARY KEY(peer_id)`` in all six
backends' migrations and the signature is a cache-invalidation guard, so the
overwrite is the contract. The disproved premise survives as a strict
``xfail`` -- it asserts two coexisting cursors per peer, which the schema does
not provide -- and the shipped behaviour is pinned by
``test_a_new_filter_signature_invalidates_the_stored_cursor``.

Surprise noted during authoring (not asserted, just recorded): an INSERT into
``memories`` fires the 0062 journal trigger and appends one pending event, so
an id seeded locally before a reconcile carries one event of its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

STAMP0 = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    monkeypatch.delenv("MNEMOS_FEDERATION_JOURNAL_COMPACTION", raising=False)
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    from mnemos.persistence.sqlite import SqliteBackend

    instance = SqliteBackend(tmp_path / "fb78_char.db", settings)
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def write(backend, sql, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).execute(sql, *params)


async def scalar(backend, sql, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).scalar(sql, *params)


async def seed_peer(backend, peer_id="PEER1", peer_name="ALPHA"):
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO federation_peers(id, name, base_url, auth_token, enabled) VALUES (?,?,?,1,1)",
            peer_id,
            peer_name,
            f"http://{peer_name.lower()}.test",
        )


async def seed_memory(backend, mid, *, namespace="A", category="facts", mode=644, deleted_at=None, content_hash=None):
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO memories(id, content, category, owner_id, namespace, permission_mode, "
            "content_hash, created, updated, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            mid,
            "ordinary facts",
            category,
            "alice",
            namespace,
            mode,
            content_hash if content_hash is not None else "hash-" + mid,
            STAMP0,
            STAMP0,
            deleted_at,
        )


async def seed_replica_row(backend, remote_id, *, peer="ALPHA", content_hash=None, with_embedding=False):
    """A row this node replicated from a peer: the parity page's population."""
    local_id = f"fed:{peer}:{remote_id}"
    await seed_memory(backend, local_id, content_hash=content_hash)
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "UPDATE memories SET federation_source = ? WHERE id = ?",
            peer,
            local_id,
        )
        if with_embedding:
            await ops.execute(
                "INSERT INTO memory_embeddings(memory_id, embedding) VALUES (?, '[0.1,0.2,0.3]')",
                local_id,
            )


# ── parity with an unavailable source ───────────────────────────────────────


@pytest.mark.asyncio
async def test_parity_with_an_unavailable_source_reports_honestly(backend):
    """A replica that cannot read the source (source_ids=None) must never
    claim the source's population, and must still report its own
    null-embedding rows: the docstring's "found nothing and looked and found
    everything must never print the same"."""
    from mnemos.domain.federation.parity import replica_parity

    await seed_peer(backend)
    await seed_replica_row(backend, "m1")  # no embedding -> null_embedding
    await seed_replica_row(backend, "m2", with_embedding=True)
    await seed_memory(backend, "plain_local")  # exportable here, not the source's business

    report = await replica_parity(backend, "PEER1")

    assert report["source"]["available"] is False
    assert report["source"]["exportable_rows"] is None
    assert report["in_sync"] is False
    assert report["missing"]["count"] == 0
    assert report["extra"]["count"] == 0
    assert report["stale"]["count"] == 0
    assert report["unreachable"]["count"] == 0
    assert report["null_embedding"]["count"] == 1
    assert report["null_embedding"]["population"] == "replica"
    assert report["replica"]["rows"] == 2
    assert report["hint"] is None  # no missing ids, so no re-pull hint


@pytest.mark.asyncio
async def test_zero_replica_rows_without_source_access_is_not_in_sync(backend):
    """Zero rows on the replica is 'missing 0' by construction, not by
    comparison: in_sync must stay False because the source set was never read."""
    from mnemos.domain.federation.parity import replica_parity

    await seed_peer(backend)
    report = await replica_parity(backend, "PEER1")
    assert report["source"]["available"] is False
    assert report["in_sync"] is False
    assert report["replica"]["rows"] == 0


@pytest.mark.asyncio
async def test_parity_with_a_supplied_source_set_classifies_pops(backend):
    """The caller-supplied case: missing / extra / stale classification."""
    from mnemos.domain.federation.parity import replica_parity

    await seed_peer(backend)
    await seed_replica_row(backend, "m1", content_hash="h1", with_embedding=True)
    await seed_replica_row(backend, "m2", content_hash="h2-STALE", with_embedding=True)
    await seed_replica_row(backend, "m5", content_hash="h5")

    source_ids = {"m1": "h1", "m2": "h2", "m3": None, "m4": "h4"}
    report = await replica_parity(backend, "PEER1", source_ids=source_ids)

    assert report["source"]["available"] is True
    assert report["source"]["exportable_rows"] == 4
    assert report["missing"]["count"] == 2
    assert report["missing"]["sample"] == ["m3", "m4"]
    assert report["extra"]["count"] == 1
    assert report["extra"]["sample"] == ["m5"]
    assert report["stale"]["count"] == 1
    assert report["stale"]["sample"] == ["m2"]
    assert report["in_sync"] is False
    # The re-pull hint names the reconcile route.
    assert "reconcile" in report["hint"]


@pytest.mark.asyncio
async def test_in_sync_only_when_source_available_and_everything_matches(backend):
    from mnemos.domain.federation.parity import replica_parity

    await seed_peer(backend)
    await seed_replica_row(backend, "m1", content_hash="h1", with_embedding=True)
    report = await replica_parity(backend, "PEER1", source_ids={"m1": "h1"})
    assert report["in_sync"] is True
    assert report["missing"]["count"] == 0
    assert report["hint"] is None


@pytest.mark.asyncio
async def test_reconcile_dry_run_requests_everything_not_just_the_sample(backend):
    """A population larger than the 50-id sample: the request must cover the
    whole missing population, not the visible slice, and apply=False must
    touch nothing."""
    from mnemos.domain.federation.parity import reconcile_peer

    await seed_peer(backend)
    all_ids = {f"s{i:03d}": f"h{i}" for i in range(60)}
    assert len(all_ids) > 50  # past the report's sample cap

    result = await reconcile_peer(backend, "PEER1", source_ids=all_ids)

    assert result["applied"] is False
    assert result["requested"] == sorted(all_ids)  # all 60, not the 50-id sample
    assert result["restored"] == 0
    assert result["failed"] == []
    assert result["missing_before"] == 60


@pytest.mark.asyncio
async def test_reconcile_apply_with_nothing_missing_makes_no_request(backend):
    """The early return: apply=True with an empty request must not reach the
    redispatch HTTP call (the source peer here is unreachable, so any HTTP
    attempt would raise)."""
    from mnemos.domain.federation.parity import reconcile_peer

    await seed_peer(backend)
    ids = {"m1": "h1", "m2": "h2"}
    await seed_replica_row(backend, "m1", content_hash="h1", with_embedding=True)
    await seed_replica_row(backend, "m2", content_hash="h2", with_embedding=True)

    result = await reconcile_peer(backend, "PEER1", apply=True, source_ids=ids)

    assert result["applied"] is True
    assert result["requested"] == []
    assert result["restored"] == 0
    assert result["failed"] == []


# ── redispatch across the _PUBLISH_BATCH (256) window ───────────────────────


@pytest.mark.asyncio
async def test_redispatch_drains_every_256_window(backend):
    """300 ids past the one-batch publish limit: the drain loop must sequence
    every redispatch event (two _publish batches: 256 + 44) and leave nothing
    pending for the eligible ids. The ineligible ids contribute no events at
    all."""
    from mnemos.persistence import federation_journal as journal

    eligible = [f"m{i}" for i in range(300)]
    skips = [f"skip{i}" for i in range(3)]
    for i, mid in enumerate(eligible):
        await seed_memory(backend, mid, content_hash=f"h{i}")
    await seed_memory(backend, "skip0", namespace="vault")  # vault boundary
    await seed_memory(backend, "skip1", deleted_at=STAMP0)  # soft-deleted
    await seed_memory(backend, "skip2", deleted_at=STAMP0)

    n = None
    async with backend.transactional() as tx:
        n = await journal.redispatch_memory_ids(tx, eligible + skips)

    assert n == 300  # only eligible rows re-advertised
    # per-id: exactly one trigger event + one redispatch event, all published
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        per_id = await ops.fetchone(
            "SELECT COUNT(*) AS total, SUM(seq IS NOT NULL) AS published FROM federation_changes WHERE memory_id = 'm0'"
        )
    assert per_id["total"] == 2
    assert per_id["published"] == 2
    due = await scalar(
        backend,
        "SELECT COUNT(*) FROM federation_changes e "
        "JOIN memories m ON m.id = e.memory_id WHERE m.id LIKE 'm%' AND e.seq IS NULL",
    )
    assert due == 0, "some redispatched event was left unpublished past the 256 limit"
    # Every one of the 600 events (300 trigger + 300 redispatch) is sequenced,
    # so the clock covers them all. This value is what discriminates the drain
    # loop: a single _publish call sequences 256 and leaves the clock at 256,
    # and the "due == 0" assertion above is the independent check that nothing
    # past the first batch stayed pending.
    clock = await scalar(backend, "SELECT value FROM federation_change_clock WHERE id = 1")
    assert clock == 600, clock
    max_seq = await scalar(backend, "SELECT MAX(seq) FROM federation_changes WHERE memory_id LIKE 'm%'")
    assert max_seq == clock


@pytest.mark.asyncio
async def test_redispatch_with_nothing_eligible_touches_nothing(backend):
    """All-vault request: early return 0, no clock bump, no events written."""
    from mnemos.persistence import federation_journal as journal

    await seed_memory(backend, "v1", namespace="vault")
    await seed_memory(backend, "v2", namespace="vault")
    clock_before = await scalar(backend, "SELECT value FROM federation_change_clock WHERE id = 1")

    async with backend.transactional() as tx:
        n = await journal.redispatch_memory_ids(tx, ["v1", "v2"])

    assert n == 0
    assert await scalar(backend, "SELECT value FROM federation_change_clock WHERE id = 1") == clock_before
    # Observed on this base: a vault INSERT does not journal at all (the 0062
    # trigger rejects it outright), and redispatch adds no event either.
    assert await scalar(backend, "SELECT COUNT(*) FROM federation_changes WHERE memory_id LIKE 'v%'") == 0


@pytest.mark.asyncio
async def test_get_feed_memory_for_an_unknown_id_is_none(backend):
    """Error path: a by-id feed lookup for a memory the journal never saw."""
    from mnemos.persistence import federation_journal as journal

    async with backend.transactional() as tx:
        row = await journal.get_feed_memory(backend, tx, "no-such-memory", namespaces=[], categories=[])
    assert row is None


# ── compaction cutoff handling ──────────────────────────────────────────────


def test_compaction_cutoff_clamps_days_to_one_and_is_utc_aware():
    from mnemos.persistence.federation_journal import _cutoff

    before = datetime.now(timezone.utc)
    for days in (0, -3, 1):
        cutoff = _cutoff(days)
        after = datetime.now(timezone.utc)
        assert cutoff.tzinfo is timezone.utc
        assert before - timedelta(days=1) <= cutoff <= after - timedelta(days=1) + timedelta(seconds=1), days
    # A FLOOR, not a ceiling: ``max(1, int(days))`` pins every value at or
    # below one day to exactly one, and lets larger windows through unchanged.
    # Both halves matter, and asserting them is what makes this test able to
    # fail -- a `_cutoff` returning a NAIVE datetime, or one that forwarded
    # ``days`` unclamped, satisfies a bare "two recent calls agree" check
    # while breaking the guard (0 would mean "now", racing live writers;
    # 2 would mean a 2-day-old event is still a candidate).
    for days, window in ((1000, 1000), (2, 2)):
        before = datetime.now(timezone.utc)
        cutoff = _cutoff(days)
        after = datetime.now(timezone.utc)
        assert cutoff.tzinfo is timezone.utc
        assert before - timedelta(days=window) <= cutoff <= after - timedelta(days=window) + timedelta(seconds=1), days


@pytest.mark.asyncio
async def test_compact_with_zero_days_uses_one_day_and_keeps_the_newest(backend):
    """``compact(older_than_days=0)`` clamps to a 1-day cutoff: superseded
    events aged 3 days go, the 2-hour-old successor stays (it is neither a
    compactable candidate nor a compactable successor). Also proves the
    clamp matters: days=0 cannot mean 'now', which would race live writers."""
    from mnemos.persistence import federation_journal as journal

    await seed_memory(backend, "cmp")
    for n in range(3):
        await write(backend, "UPDATE memories SET content = ? WHERE id = 'cmp'", f"rev{n}")
    async with backend.transactional() as tx:
        await backend.federation.feed_query(
            tx, since_updated=None, since_id=None, namespaces=[], categories=[], limit=100, prefer_compressed=False
        )
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await write(
        backend,
        "UPDATE federation_changes SET changed_at = ? "
        "WHERE seq < (SELECT MAX(seq) FROM federation_changes WHERE memory_id = 'cmp')",
        old,
    )
    await write(
        backend,
        "UPDATE federation_changes SET changed_at = ? "
        "WHERE seq = (SELECT MAX(seq) FROM federation_changes WHERE memory_id = 'cmp')",
        recent,
    )

    async with backend.transactional() as tx:
        removed = await journal.compact(tx, older_than_days=0)

    # Four events existed; the two oldest are CompactCandidates (three days
    # old, superseded) and go. rev1 stays because its only successor, rev2,
    # is 2 hours old -- NOT a compactable successor -- so retiring rev1 would
    # leave a consumer without any published event older than the cutoff.
    assert removed == 2
    rows = await scalar(backend, "SELECT COUNT(*) FROM federation_changes WHERE memory_id = 'cmp'")
    assert rows == 2
    survivor_age_days = await scalar(
        backend,
        "SELECT julianday('now') - julianday(MAX(changed_at)) FROM federation_changes WHERE memory_id = 'cmp'",
    )
    assert 0 <= survivor_age_days <= 1, "the newest event (2 h old) must be a survivor"


# ── peer cursors: keyed state and the FINDING ───────────────────────────────


@pytest.mark.asyncio
async def test_peer_cursor_roundtrip_updates_in_place_and_scopes_alone(backend):
    """Characterised today-behaviour: one cursor row per peer (the schema's
    ``PRIMARY KEY(peer_id)``), read back only under the matching
    ``filter_signature``; an unknown peer or an unknown signature reads None;
    saving the same peer again overwrites its row in place."""
    from mnemos.persistence import federation_journal as journal

    async with backend.transactional() as tx:
        await journal.save_peer_cursor(tx, "P1", "journal:5", "sig-a")
    async with backend.transactional() as tx:
        assert await journal.load_peer_cursor(tx, "P1", "sig-a") == "journal:5"
        assert await journal.load_peer_cursor(tx, "P1", "sig-b") is None
        assert await journal.load_peer_cursor(tx, "P2", "sig-a") is None

    async with backend.transactional() as tx:
        await journal.save_peer_cursor(tx, "P1", "journal:6", "sig-a")

    async with backend.transactional() as tx:
        assert await journal.load_peer_cursor(tx, "P1", "sig-a") == "journal:6"
    assert await scalar(backend, "SELECT COUNT(*) FROM federation_peer_cursors WHERE peer_id = 'P1'") == 1


@pytest.mark.xfail(strict=True, reason="FB78-FINDING-1 (disproved: single cursor per peer is the design)")
@pytest.mark.asyncio
async def test_a_second_filter_signature_replaces_the_peer_cursor(backend):
    """FB78-FINDING-1, as originally filed, called this a defect: "saving a
    cursor under a second ``filter_signature`` destroys the first". Verified
    against source, the claim does not hold and the finding is withdrawn.

    ``federation_peer_cursors`` is keyed on ``peer_id`` ALONE --
    ``PRIMARY KEY(peer_id)``, identically in the migrations for all six
    backends (``mnemos/db_migrations/migrations*/0062_federation_journal.sql``) -- so one cursor
    per peer is the schema's own contract, not an accident of
    ``save_peer_cursor``. ``filter_signature`` is a cache-INVALIDATION guard,
    not half of a compound key: ``mnemos/domain/federation.py`` derives it
    from the peer's configured ``base_url``/``namespace_filter``/
    ``category_filter``, and ``load_peer_cursor`` returns ``None`` when the
    stored signature no longer matches, so a peer whose filters changed
    replays from sequence 0 instead of resuming at a cursor that describes a
    different scope. Overwriting the row is what makes that guard fire.

    This body is retained as a strict xfail because it asserts the OPPOSITE
    of the shipped contract (two coexisting cursors per peer). It is here to
    document the disproved premise: if the schema ever gains a compound key,
    this test starts passing and strict mode fails loudly, which is the
    signal to revisit the finding rather than leave a stale claim in the
    suite.
    """
    from mnemos.persistence import federation_journal as journal

    async with backend.transactional() as tx:
        await journal.save_peer_cursor(tx, "P1", "journal:5", "sig-a")
    async with backend.transactional() as tx:
        await journal.save_peer_cursor(tx, "P1", "journal:9", "sig-b")

    async with backend.transactional() as tx:
        assert await journal.load_peer_cursor(tx, "P1", "sig-a") == "journal:5"
        assert await journal.load_peer_cursor(tx, "P1", "sig-b") == "journal:9"
    assert await scalar(backend, "SELECT COUNT(*) FROM federation_peer_cursors WHERE peer_id = 'P1'") == 2


@pytest.mark.asyncio
async def test_a_new_filter_signature_invalidates_the_stored_cursor(backend):
    """The other half of FB78-FINDING-1, pinned as the shipped behaviour: one
    cursor row per peer (schema ``PRIMARY KEY(peer_id)``), and a save under a
    new signature replaces it, so the OLD signature reads ``None`` and the
    next sync replays from sequence 0 rather than resuming a cursor that
    describes filters the peer no longer has."""
    from mnemos.persistence import federation_journal as journal

    async with backend.transactional() as tx:
        await journal.save_peer_cursor(tx, "P1", "journal:5", "sig-a")
    async with backend.transactional() as tx:
        await journal.save_peer_cursor(tx, "P1", "journal:9", "sig-b")

    async with backend.transactional() as tx:
        # The stale scope is invalidated, not merely stale-readable.
        assert await journal.load_peer_cursor(tx, "P1", "sig-a") is None
        assert await journal.load_peer_cursor(tx, "P1", "sig-b") == "journal:9"
    # Exactly one row per peer, regardless of how many signatures it saw.
    assert await scalar(backend, "SELECT COUNT(*) FROM federation_peer_cursors WHERE peer_id = 'P1'") == 1
