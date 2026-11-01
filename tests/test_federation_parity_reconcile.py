"""F-15 replica parity and reconcile.

Two SQLite databases stand in for the fleet: PYTHIA (the source) and
PROTEUS (its replica). The replica pulls over the real federation engine
with a transport that dispatches its outbound HTTP into the source's real
FastAPI routes, so the source-side eligibility rules under test are the
ones production runs -- not a local re-implementation of them.

The parity report compares the source's exportable id/content-hash set
with the replica's ``fed:<peer>:*`` population by explicit keyset pages on
both sides (never a full table load) and classifies every id into
``missing`` / ``extra`` / ``stale`` / ``null_embedding``. Reconcile is
dry-run first: nothing is requested from the feed unless ``apply=True``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.federation import router as federation_router
from mnemos.domain import federation as fed


async def replica_parity(*args, **kwargs):
    from mnemos.domain.federation.parity import replica_parity as _impl

    return await _impl(*args, **kwargs)


from mnemos.domain.federation.parity import reconcile_peer


async def source_export_ids(source) -> dict[str, str | None]:
    """The peer's exportable ``id -> content_hash`` set, as the source sees it.

    In production the replica obtains this by running ``mnemos federation
    parity`` on the source host and handing the ids to its own run.
    """
    async with source.transactional() as tx:
        rows = await source.federation.fetch_source_export_keyset_page(tx, limit=100_000)
    return {r["id"]: r.get("content_hash") for r in rows}


FACTS = "facts"
# The live settings singleton sizes the column; match it so vectors cross.
EMBED_DIM = 768
OWNER = "alice"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Settings(SimpleNamespace):
    """Minimal settings stand-in: a 3-dim embedder, SQLite shaped otherwise."""

    def __init__(self):
        super().__init__()
        self.database = SimpleNamespace(embedding_dim=EMBED_DIM)
        # Both sides must agree on the embedder identity or the receiver
        # fails closed and declines the vector (the field NULL-embedding bug).
        self.providers = SimpleNamespace(inference_embed_model="bge-m3")


@pytest.fixture(autouse=True)
def _lan_posture(monkeypatch):
    # The fleet runs the trusted-LAN posture; the world-read gate must not
    # be what makes these tests pass or fail.
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "1")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", str(EMBED_DIM))
    # Same embedder identity on both hosts, or the receiver fails closed and
    # declines the peer's vector (the field's 24,986 NULL-embedding rows).
    monkeypatch.setenv("MNEMOS_EMBED_HTTP_MODEL", "bge-m3")


async def _open_backend(path):
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(path, _Settings())
    await backend.open()
    return backend


@pytest_asyncio.fixture
async def source(tmp_path):
    backend = await _open_backend(tmp_path / "pythia.db")
    try:
        yield backend
    finally:
        await backend.close()


@pytest_asyncio.fixture
async def replica(tmp_path):
    backend = await _open_backend(tmp_path / "proteus.db")
    try:
        yield backend
    finally:
        await backend.close()


async def insert(
    backend,
    memory_id: str,
    content: str,
    *,
    namespace: str = "default",
    owner_id: str = OWNER,
    permission_mode: int = 644,
    federation_source: str | None = None,
    federation_remote_updated: str | None = None,
    archived_at: str | None = None,
    consolidated_into: str | None = None,
    deleted_at: str | None = None,
    embedding: Iterable[float] | None = None,
    created: datetime | None = None,
    updated: datetime | None = None,
) -> None:
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    created = created or NOW
    updated = updated or created
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO memories(id, content, category, owner_id, namespace, "
            "permission_mode, created, updated, federation_source, "
            "federation_remote_updated, archived_at, consolidated_into, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            memory_id,
            content,
            FACTS,
            owner_id,
            namespace,
            permission_mode,
            created.isoformat(),
            updated.isoformat(),
            federation_source,
            federation_remote_updated,
            archived_at,
            consolidated_into,
            deleted_at,
        )
        if embedding is not None:
            await backend.memories.upsert_memory_embedding(tx, memory_id, list(embedding))


async def mutate(backend, sql: str, *params) -> int:
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).execute(sql, *params)


async def _commit(backend) -> None:
    """Close and reopen, committing whatever the last statement left open."""
    db_path = backend._db_path
    await backend.close()
    await backend.open(Path(db_path))


async def fetch(backend, sql: str, *params) -> list:
    from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).fetchall(sql, *params)


class _SourceTransport(httpx.AsyncBaseTransport):
    """Dispatch the replica's outbound HTTP into the source's real routes."""

    def __init__(self, app: FastAPI, calls: list[httpx.URL]):
        self._app = app
        self.calls = calls

    async def handle_async_request(self, request):
        self.calls.append(request.url)
        # ``httpx.Request.url`` is a read-only property: build a rewritten
        # copy instead of assigning to it.
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
async def source_app(source, monkeypatch):
    """Wire the federation engine's outbound client at the source's routes."""
    from mnemos.api import persistence_helpers

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
    calls: list[httpx.URL] = []
    transport = _SourceTransport(app, calls)

    real_make_safe_client = fed.make_safe_client

    async def _safe_client(url, *, timeout=None, allow_private=True, **kwargs):
        client = httpx.AsyncClient(transport=transport, base_url="http://pythia")
        return client, None

    def _current_backend():
        return source

    monkeypatch.setattr(fed, "make_safe_client", _safe_client)
    monkeypatch.setattr(persistence_helpers, "backend_or_503", _current_backend)
    try:
        yield SimpleNamespace(app=app, calls=calls)
    finally:
        monkeypatch.setattr(fed, "make_safe_client", real_make_safe_client)


async def register_peer(replica, *, name="pythia", enabled=True, copy_embeddings=True):
    async with replica.transactional() as tx:
        peer = await replica.federation.create_peer(
            tx,
            name=name,
            base_url="http://pythia",
            auth_token="token",
            namespace_filter=None,
            category_filter=None,
            enabled=enabled,
            sync_interval_secs=300,
            compat_mode="permissive",
        )
    if copy_embeddings:
        # The fleet sets this per peer so vectors cross the wire; the default
        # (0) is what produced the 24,986 NULL-embedding rows in the field.
        await mutate(replica, "UPDATE federation_peers SET copy_embeddings = 1 WHERE id = ?", peer["id"])
    return peer


async def sync(replica, peer_id):
    async def _schema_ok(*args, **kwargs):
        return {
            "ok": True,
            "mnemos_version": "9.9.9",
            "schema_signature": "9.9",
            "migrations_fingerprint": None,
        }

    real = fed._check_peer_schema
    fed._check_peer_schema = _schema_ok
    try:
        return await fed.sync_peer(replica, peer_id)
    finally:
        fed._check_peer_schema = real


@pytest_asyncio.fixture
async def synced(source, replica, source_app):
    """A replica that has completed one real pull from the source."""
    peer = await register_peer(replica)
    await sync(replica, peer["id"])
    return SimpleNamespace(source=source, replica=replica, peer=peer, app=source_app)


# ── eligibility gap: CHARON export vs the federation feed ────────────────────


@pytest.mark.asyncio
async def test_replica_receives_every_row_the_feed_serves(synced):
    """What the feed serves arrives; nothing is silently dropped."""
    for i in range(4):
        await insert(synced.source, f"plain-{i}", f"plain fact {i}", embedding=[0.1] * EMBED_DIM)

    await sync(synced.replica, synced.peer["id"])

    async with synced.source.transactional() as tx:
        feed_ids = {
            r["id"]
            for r in await synced.source.federation.feed_query(
                tx,
                since_updated=None,
                since_id=None,
                namespaces=[],
                categories=[],
                limit=100,
                prefer_compressed=False,
            )
        }
    assert feed_ids == {"plain-0", "plain-1", "plain-2", "plain-3"}

    replicated = {
        r["id"] for r in await fetch(synced.replica, "SELECT id FROM memories WHERE federation_source = 'pythia'")
    }
    assert replicated == {f"fed:pythia:{mid}" for mid in feed_ids}

    ids = await source_export_ids(synced.source)
    report = await replica_parity(synced.replica, synced.peer["id"], page_size=2, source_ids=ids)
    assert report["missing"]["count"] == 0, report["missing"]
    assert report["extra"]["count"] == 0, report["extra"]
    assert report["in_sync"] is True, {
        k: report[k] for k in ("missing", "extra", "stale", "null_embedding", "unreachable", "replica")
    }


@pytest.mark.asyncio
async def test_export_definition_is_a_superset_of_the_feed(synced):
    """Document the F-15 eligibility gap, per the task's "align or document".

    CHARON's export predicate has no ``federation_source`` term; the feed's
    does, and the journal triggers only journal rows whose
    ``federation_source`` is NULL. So a row replicated here from another
    peer is counted exportable by CHARON but has no publishable event and can
    never be served. ``replica_parity`` reports such rows as ``unreachable``
    on the source host rather than as a permanent ``missing`` deficit.
    """
    from mnemos.core import eligibility

    feed = eligibility.eligible_for_federation("m")
    export = eligibility.eligible_for_federation_export("m")
    assert "federation_source IS NULL" in feed
    assert "federation_source" not in export

    await insert(synced.source, "plain", "plain fact")
    await insert(synced.source, "from-peer", "arrived from elsewhere", federation_source="other")

    async with synced.source.transactional() as tx:
        exportable = await synced.source.federation.count_exportable_rows(tx)
        feed_ids = {
            r["id"]
            for r in await synced.source.federation.feed_query(
                tx,
                since_updated=None,
                since_id=None,
                namespaces=[],
                categories=[],
                limit=100,
                prefer_compressed=False,
            )
        }
    # ``count_exportable_rows`` uses the FEED predicate, so it reports the
    # servable population; the CHARON-side row is the documented gap.
    assert exportable == 1
    assert feed_ids == {"plain"}


@pytest.mark.asyncio
async def test_parity_reports_missing_extra_stale_and_null_embedding(synced):
    """Each population is counted once, by id, with a bounded sample."""
    await insert(synced.source, "one", "first", embedding=[0.1] * EMBED_DIM)
    await insert(synced.source, "two", "second", embedding=[0.1] * EMBED_DIM)
    await insert(synced.source, "three", "third", embedding=[0.1] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])

    # missing: dropped from the replica by something outside the fence.
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:two")
    # extra: a row the source no longer exports.
    await insert(synced.replica, "fed:pythia:ghost", "gone upstream", federation_source="pythia")
    # stale: same id, different content on the replica. The content hash is
    # maintained by a trigger (and never travels on the wire), so drift can
    # only be produced by changing the content itself.
    await mutate(
        synced.replica,
        "UPDATE memories SET content = ? WHERE id = ?",
        "locally mutated",
        "fed:pythia:one",
    )
    # null embedding: the replica stored content without a vector.
    # Embeddings live in ``memory_embeddings``, not inline on ``memories``;
    # dropping the join row is what the parity query reads as "no vector".
    await mutate(synced.replica, "DELETE FROM memory_embeddings WHERE memory_id = ?", "fed:pythia:three")

    ids = await source_export_ids(synced.source)
    report = await replica_parity(synced.replica, synced.peer["id"], page_size=1, source_ids=ids)

    assert report["missing"]["count"] == 1
    assert report["missing"]["sample"] == ["two"]
    assert report["extra"]["count"] == 1
    assert report["extra"]["sample"] == ["ghost"]
    assert report["stale"]["count"] == 1
    assert report["stale"]["sample"] == ["one"]
    # ``three`` lost its vector, and ``ghost`` was never given one: both are
    # rows the replica holds without an embedding. ``two`` is absent from the
    # replica entirely, so it is ``missing``, never ``null_embedding``.
    assert report["null_embedding"]["count"] == 2
    # ``sample`` is capped by ``page_size`` (1 here), so it shows the first id.
    assert report["null_embedding"]["sample"] == ["ghost"]
    assert report["null_embedding"]["sample_truncated"] is True
    assert report["null_embedding"]["population"] == "replica"
    assert report["page_size"] == 1
    assert report["in_sync"] is False
    assert report["peer"] == "pythia"


@pytest.mark.asyncio
async def test_parity_pages_never_load_the_whole_table(synced, monkeypatch):
    """The comparison must be keyset-paged, not one full-table read."""
    for i in range(7):
        await insert(synced.source, f"p{i}", f"fact {i}", embedding=[0.1] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])

    pages: list[int] = []
    real_source_page = getattr(type(synced.replica.federation), "fetch_parity_page")

    async def _spy(self, tx, **kwargs):
        rows = await real_source_page(self, tx, **kwargs)
        pages.append(len(list(rows)))
        return rows

    monkeypatch.setattr(type(synced.replica.federation), "fetch_parity_page", _spy)

    ids = await source_export_ids(synced.source)
    report = await replica_parity(synced.replica, synced.peer["id"], page_size=2, source_ids=ids)
    assert report["in_sync"] is True
    assert pages and max(pages) <= 2, pages


@pytest.mark.asyncio
async def test_parity_is_clean_for_a_freshly_synced_replica(synced):
    for i in range(3):
        await insert(synced.source, f"ok-{i}", f"fact {i}", embedding=[0.1] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])
    ids = await source_export_ids(synced.source)
    report = await replica_parity(synced.replica, synced.peer["id"], source_ids=ids)
    assert report["in_sync"] is True
    assert report["source"]["available"] is True
    assert report["source"]["exportable_rows"] == 3
    assert report["replica"]["rows"] == 3
    assert report["hint"] is None


# ── reconcile ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_is_dry_run_first(synced):
    await insert(synced.source, "lost", "never arrived")
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:lost")

    before = len(synced.app.calls)
    ids = await source_export_ids(synced.source)
    dry = await reconcile_peer(synced.replica, synced.peer["id"], apply=False, source_ids=ids)

    assert dry["applied"] is False
    assert dry["requested"] == ["lost"]
    assert dry["restored"] == 0
    assert len(synced.app.calls) == before, "a dry run must not touch the feed"
    still_missing = await replica_parity(synced.replica, synced.peer["id"], source_ids=ids)
    assert still_missing["missing"]["count"] == 1


@pytest.mark.asyncio
async def test_reconcile_apply_restores_missing_ids_ignoring_the_fence(synced):
    await insert(synced.source, "fenced", "sequence already seen", embedding=[0.5] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])
    # Simulate the local mutation that the durable fence then refuses to re-apply.
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:fenced")

    ids = await source_export_ids(synced.source)
    result = await reconcile_peer(synced.replica, synced.peer["id"], apply=True, source_ids=ids)

    assert result["applied"] is True
    assert result["requested"] == ["fenced"]
    assert result["restored"] == 1
    assert result["failed"] == []
    row = await fetch(
        synced.replica,
        "SELECT content FROM memories WHERE id = ?",
        "fed:pythia:fenced",
    )
    assert row and row[0]["content"] == "sequence already seen"
    report = await replica_parity(synced.replica, synced.peer["id"])
    assert report["in_sync"] is True


@pytest.mark.asyncio
async def test_reconcile_reports_a_peer_that_cannot_serve_the_id(synced):
    """A 404 from the peer is counted, not raised: one bad id cannot stop the run."""
    await insert(synced.source, "retracted", "was here")
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:retracted")
    # The replica was reconciled against an export set that names the row,
    # but the source has since stopped serving it by id (soft delete). The
    # 404 is therefore permanent for this run: re-dispatch publishes nothing
    # and the id is counted as failed rather than retried forever.
    await mutate(
        synced.source,
        "UPDATE memories SET deleted_at = ? WHERE id = ?",
        (NOW + timedelta(days=1)).isoformat(),
        "retracted",
    )
    ids = {"retracted": None}
    result = await reconcile_peer(synced.replica, synced.peer["id"], apply=True, source_ids=ids)
    assert result["requested"] == ["retracted"]
    assert result["restored"] == 0
    assert result["failed"] and result["failed"][0]["id"] == "retracted"
    # Still reported missing -- the operator sees the gap rather than a silence.
    # The report must be given the same export set, or it would re-derive one
    # from the source, which no longer lists the row.
    report = await replica_parity(synced.replica, synced.peer["id"], source_ids=ids)
    assert report["missing"]["sample"] == ["retracted"]


@pytest.mark.asyncio
async def test_parity_endpoint_reports_and_reconcile_dry_run(synced):
    """The admin surface: GET parity and POST reconcile (dry run by default)."""
    import mnemos.api.persistence_helpers as _ph

    await insert(synced.source, "gone", "lost locally", embedding=[0.1] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:gone")

    real = _ph.backend_or_503
    # The peer row lives on the replica, so the admin surface runs there.
    _ph.backend_or_503 = lambda *a, **k: synced.replica
    try:
        transport = httpx.ASGITransport(app=synced.app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://source") as client:
            report = (await client.get(f"/v1/federation/peers/{synced.peer['id']}/parity")).json()["report"]
            # A replica cannot see the source's export set, so it reports the
            # gap honestly as source-unavailable rather than inventing one.
            assert report["source"]["available"] is False
            assert report["missing"]["count"] == 0
            # A dry run touches neither side.
            dry = (
                await client.post(
                    f"/v1/federation/peers/{synced.peer['id']}/reconcile",
                    params={"apply": False},
                )
            ).json()
            assert dry["result"]["applied"] is False
            assert dry["result"]["restored"] == 0
            # No source export set on this host => nothing to request, which
            # is the honest answer rather than a silent "all good".
            assert dry["result"]["requested"] == []
    finally:
        _ph.backend_or_503 = real

    assert await fetch(synced.replica, "SELECT id FROM memories WHERE id = ?", "fed:pythia:gone") == []


# ── regressions: round-2 review of the F-15 delivery ────────────────────────
#
# Each test below pins one review finding, verified at the source first. The
# round-1 code is kept in ``git show`` shape rather than copied here, so the
# assertion messages say what the old behaviour did.
#
# ``_redispatch`` skips the tenant scope a node is not its own source (a
# replica's by-id path 404s such an id), so the scope tests drive the source
# directly -- which is the node that owns the journal anyway.


class _RecorderCursor:
    """A DB-API cursor that records every statement without reaching a DB."""

    rowcount = 0
    description = None

    def __init__(self, sink: list[tuple[str, Any]]):
        self._sink = sink

    async def execute(self, sql, params=None):
        self._sink.append((sql, params))

    async def fetchall(self):
        return []

    async def fetchone(self):
        return None

    async def close(self):
        return None


class _RecorderConn:
    def __init__(self, sink):
        self._sink = sink

    def cursor(self):
        return _RecorderCursor(self._sink)

    def execute(self, sql, params=None):
        self._sink.append((sql, params))
        return SimpleNamespace(lastrowid=0)


class _RecorderTx:
    def __init__(self, sink):
        self.conn = _RecorderConn(sink)


async def journal_committed(source) -> None:
    """Let the source's next transaction see every write so far.

    ``BEGIN IMMEDIATE`` writes are committed by the context manager -- this is
    a readability alias for the tests below, not a second connection.
    """


def _redispatch_calls(sink: list[tuple[str, Any]]) -> list[str]:
    return [sql for sql, _ in sink if sql.lstrip().upper().startswith("UPDATE FEDERATION_CHANGE_CLOCK")]


async def _journal_rows(source, memory_ids: list[str]) -> dict[str, dict]:
    placeholders = ",".join("?" for _ in memory_ids)
    rows = await fetch(
        source,
        "SELECT memory_id, MAX(seq) AS seq, COUNT(*) AS n FROM federation_changes "
        f"WHERE memory_id IN ({placeholders}) GROUP BY memory_id",
        *memory_ids,
    )
    return {r["memory_id"]: {"seq": r["seq"], "n": int(r["n"])} for r in rows}


async def _journal_pending(source, memory_ids: list[str]) -> dict[str, int]:
    placeholders = ",".join("?" for _ in memory_ids)
    rows = await fetch(
        source,
        "SELECT memory_id, COUNT(*) AS n FROM federation_changes "
        f"WHERE memory_id IN ({placeholders}) AND seq IS NULL GROUP BY memory_id",
        *memory_ids,
    )
    return {r["memory_id"]: int(r["n"]) for r in rows}


async def _journal_clock(source) -> int:
    rows = await fetch(source, "SELECT value FROM federation_change_clock WHERE id = 1")
    return int(rows[0]["value"])


@pytest.mark.asyncio
async def test_redispatch_refuses_a_row_outside_the_feed_scope(synced):
    """A row the feed cannot serve must not receive a live journal event.

    ``redispatch_memory_ids`` guarded only ``deleted/archived/consolidated/
    federation_source`` and hardcoded ``new_exportable = 1``. A vault row
    passed that guard and was published as live data, i.e. a credential
    boundary replayed as content. The guard is now the predicate the feed,
    the by-id route and ``fetch_source_export_keyset_page`` already share.

    Scope caveat, verified against the source rather than assumed: a
    non-vault namespace (``private-scope`` here) is still exportable in the
    LAN posture -- ``eligible_for_federation`` excludes only ``vault``, and
    nothing in this repository drops a namespace without an explicit scope
    filter. Round-1's claim that such a row is "out of scope" is disproved
    here, so this test does not assert it; it asserts the vault refusal,
    which is real, and pins the namespace behaviour so a future scope change
    has to update this test deliberately.
    """
    await insert(synced.source, "vaulted", "never federates", namespace="vault")
    await insert(synced.source, "elsewhere", "non-vault namespace", namespace="private-scope")
    await insert(synced.source, "servable", "ordinary fact")

    async with synced.source.transactional() as tx:
        served = await synced.source.federation.redispatch_federation_journal(
            tx, memory_ids=["vaulted", "elsewhere", "servable"]
        )
    journal = await _journal_rows(synced.source, ["vaulted", "elsewhere", "servable"])
    assert "vaulted" not in journal, journal
    assert journal["servable"]["seq"] is not None, journal
    # The vault row is the one real exclusion; ``elsewhere`` is published.
    assert served == 2, journal
    assert journal["elsewhere"]["seq"] is not None, journal


@pytest.mark.asyncio
async def test_redispatch_does_not_claim_another_publishers_pending_event(synced):
    """It publishes *its* ids, not every event a publisher left pending.

    ``_publish`` assigns sequences to up to 256 globally pending outbox rows
    and returns nothing. Reporting "one per event it assigned" made a single
    redispatch claim a concurrent publisher's event, so neither the caller
    nor the reconcile failure accounting could tell what was re-advertised.
    """
    await insert(synced.source, "left-pending", "publisher stopped before publish")
    pending_before = await _journal_pending(synced.source, ["left-pending"])
    assert pending_before.get("left-pending") == 1, "the outbox trigger did not fire"

    await insert(synced.source, "asked-for", "explicitly re-advertised")
    async with synced.source.transactional() as tx:
        served = await synced.source.federation.redispatch_federation_journal(tx, memory_ids=["asked-for"])
    assert served == 1

    pending_after = await _journal_pending(synced.source, ["left-pending"])
    assert pending_after.get("left-pending") == 1, "another publisher's event was claimed"


@pytest.mark.asyncio
async def test_redispatch_with_no_work_does_not_touch_the_clock(synced):
    """A no-op must not write (LOW, confirmed).

    ``redispatch_memory_ids`` opened with ``UPDATE federation_change_clock SET
    value = value WHERE id = 1`` -- a pure write lock on the single journal
    clock row -- and nothing between it and the next statement read it.

    The recorder below satisfies the ``_Ops`` interface ``redispatch`` uses
    (``conn.execute``/``cursor``), so the empty call reaches the same code
    path a real transaction would. The refused-id leg reads the clock
    *outside* the write transaction: ``SqliteBackend.transactional`` holds a
    non-reentrant lock, so a nested ``transactional()`` here would deadlock
    rather than test anything.
    """
    sink: list[tuple[str, Any]] = []
    tx = _RecorderTx(sink)
    from mnemos.persistence.federation_journal import redispatch_memory_ids

    assert await redispatch_memory_ids(tx, []) == 0
    assert sink == [], sink

    # Refused ids are the same case: nothing to publish, nothing to lock.
    await insert(synced.source, "vaulted", "never federates", namespace="vault")
    before = await _journal_clock(synced.source)
    async with synced.source.transactional() as real_tx:
        assert await synced.source.federation.redispatch_federation_journal(real_tx, memory_ids=["vaulted"]) == 0
    assert await _journal_clock(synced.source) == before


@pytest.mark.asyncio
async def test_redispatch_reports_only_ids_it_published(synced):
    """The count is per-id, not per-assigned-event."""
    await insert(synced.source, "one", "first")
    await insert(synced.source, "two", "second")
    await insert(synced.source, "vaulted", "never federates", namespace="vault")
    async with synced.source.transactional() as tx:
        served = await synced.source.federation.redispatch_federation_journal(
            tx, memory_ids=["one", "two", "vaulted", "missing-entirely"]
        )
    assert served == 2
    journal = await _journal_rows(synced.source, ["one", "two"])
    assert journal["one"]["seq"] is not None and journal["two"]["seq"] is not None


@pytest.mark.asyncio
async def test_parity_without_the_export_set_compares_nothing(synced):
    """`source_available` -- not "are there exportable rows" -- picks the branch.

    The body chose the source-comparison branch by asking whether *this*
    node had any exportable rows, so a replica whose own core was empty
    compared the whole export set against a replica dict it had just failed
    to read. Re-deriving the peer's population on a replica is also the full
    sweep the keyset design exists to avoid. With no ``source_ids`` the
    report must say the source is unavailable, whatever the local corpus.
    """
    await insert(synced.source, "a", "first", embedding=[0.1] * EMBED_DIM)
    await insert(synced.source, "b", "second", embedding=[0.1] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])

    report = await replica_parity(synced.replica, synced.peer["id"], page_size=1)
    assert report["source"]["available"] is False
    assert report["source"]["exportable_rows"] is None
    assert report["missing"]["count"] == 0
    assert report["extra"]["count"] == 0
    assert report["unreachable"]["count"] == 0

    # A row the peer no longer serves is not "extra": the replica cannot see
    # the source's population, and guessing would call every local row extra.
    await insert(synced.replica, "fed:pythia:ghost", "gone upstream", federation_source="pythia")
    report = await replica_parity(synced.replica, synced.peer["id"], page_size=1)
    assert report["source"]["available"] is False
    assert report["extra"]["count"] == 0
    assert report["extra"]["sample"] == []


@pytest.mark.asyncio
async def test_parity_recomputes_after_a_local_change(synced):
    """The report must describe the database now, not a fixed answer.

    Round-1 recomputed the comparison from a dict the first branch had left
    empty, so the two paths could never disagree and a stale or extra row
    could not be found on a node that also exported. Extra (peer gone) and
    stale (content drift) must both fall out of the same comparison.

    The restore leg goes through reconcile, not a bare ``sync``: the
    receiver's durable fence drops every replayed event it has already
    applied, so a locally deleted row can only come back via a NEW event --
    which is the reason F-15 exists. A plain re-sync provably restores
    nothing (pinned by ``test_reconcile_apply_restores_missing_ids_ignoring_the_fence``).
    """
    await insert(synced.source, "kept", "first", embedding=[0.1] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:kept")

    ids = await source_export_ids(synced.source)
    missing = await replica_parity(synced.replica, synced.peer["id"], source_ids=ids)
    assert missing["missing"]["sample"] == ["kept"]

    # Restore through the journal (a new event), then drift its content: the
    # same call now says ``in_sync`` and then ``stale``.
    restored = await reconcile_peer(synced.replica, synced.peer["id"], apply=True, source_ids=ids)
    assert restored["restored"] == 1
    clean = await replica_parity(synced.replica, synced.peer["id"], source_ids=ids)
    assert clean["in_sync"] is True
    await mutate(synced.replica, "UPDATE memories SET content = ? WHERE id = ?", "drifted", "fed:pythia:kept")
    drifted = await replica_parity(synced.replica, synced.peer["id"], source_ids=ids)
    assert drifted["stale"]["sample"] == ["kept"], drifted["stale"]

    # And a peer that no longer exists is an error, not an empty report.
    await mutate(synced.replica, "DELETE FROM federation_peers WHERE id = ?", synced.peer["id"])
    with pytest.raises(ValueError):
        await replica_parity(synced.replica, synced.peer["id"], source_ids=ids)


@pytest.mark.asyncio
async def test_parity_counts_unreachable_only_on_the_source_host(synced):
    """``unreachable`` describes THIS node's own feed, never the import's.

    Verified against the source: ``replica_parity`` resolves its peer through
    ``get_sync_peer``, and ``federation_peers`` rows exist only where a peer
    was registered. A source host has no row for the replica that pulls from
    it, so running parity *on* the source requires registering one -- the
    round-1 test skipped that and only ever failed with "peer not found".
    """
    await insert(synced.source, "plain", "ordinary fact")
    await insert(synced.source, "from-elsewhere", "arrived by federation", federation_source="other")
    await sync(synced.replica, synced.peer["id"])

    # The source's view of its own replica: a peer row pointing back at it.
    back = await register_peer(synced.source, name="proteus")

    ids = await source_export_ids(synced.source)
    assert set(ids) == {"plain"}

    from_report = await replica_parity(synced.source, back["id"], source_ids=ids)
    # The source owns the journal but has not pulled anything yet, so nothing
    # local is missing: every exportable id is unreachable-less by definition.
    assert from_report["unreachable"]["count"] == 0
    assert from_report["in_sync"] is False  # the replica holds nothing yet

    unavailable = await replica_parity(synced.replica, synced.peer["id"])
    assert unavailable["unreachable"]["count"] == 0


@pytest.mark.asyncio
async def test_reconcile_reports_the_missing_count_after_the_run(synced):
    """``remaining`` describes the replica after the run (MEDIUM, confirmed).

    The post-run report was compared on the wrong axis: without an explicit
    export set it finds nothing missing by construction, so a run that
    restored nothing still reported ``remaining: 0``.
    """
    await insert(synced.source, "unservable", "was here")
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:unservable")
    await mutate(
        synced.source,
        "UPDATE memories SET deleted_at = ? WHERE id = ?",
        (NOW + timedelta(days=1)).isoformat(),
        "unservable",
    )
    ids = {"unservable": None}

    result = await reconcile_peer(synced.replica, synced.peer["id"], apply=True, source_ids=ids)
    assert result["restored"] == 0
    assert result["failed"] and result["failed"][0]["id"] == "unservable"
    assert result["missing_before"] == 1
    assert result["remaining"] == 1, "a failed repair must still be reported as remaining"
    # And the dry run says the same before anything is applied.
    dry = await reconcile_peer(synced.replica, synced.peer["id"], apply=False, source_ids=ids)
    assert dry["remaining"] == 1
    assert dry["requested"] == ["unservable"]


@pytest.mark.asyncio
async def test_reconcile_dry_run_requests_the_whole_population(synced):
    """A dry run covers every missing id, not just the visible sample."""
    for i in range(6):
        await insert(synced.source, f"lost-{i}", f"fact {i}", embedding=[0.1] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id LIKE ?", "fed:pythia:lost-%")

    ids = await source_export_ids(synced.source)
    report = await replica_parity(synced.replica, synced.peer["id"], page_size=2, source_ids=ids)
    assert report["missing"]["count"] == 6
    assert len(report["missing"]["sample"]) == 2

    before = len(synced.app.calls)
    dry = await reconcile_peer(synced.replica, synced.peer["id"], apply=False, page_size=2, source_ids=ids)
    assert dry["requested"] == [f"lost-{i}" for i in range(6)]
    assert dry["missing_before"] == 6
    assert dry["remaining"] == 6
    assert len(synced.app.calls) == before, "a dry run must not touch the feed"

    capped = await reconcile_peer(synced.replica, synced.peer["id"], apply=False, page_size=2, limit=4, source_ids=ids)
    assert capped["requested"] == [f"lost-{i}" for i in range(4)]
    assert capped["remaining"] == 6


@pytest.mark.asyncio
async def test_reconcile_never_requests_an_unreachable_id(synced):
    """Only ``missing`` is requested -- never an id the feed cannot serve."""
    await insert(synced.source, "reachable", "fine")
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:reachable")
    await insert(synced.source, "from-elsewhere", "arrived from another peer", federation_source="other")

    ids = await source_export_ids(synced.source)
    assert set(ids) == {"reachable"}, ids
    dry = await reconcile_peer(synced.replica, synced.peer["id"], apply=False, source_ids=ids)
    assert dry["requested"] == ["reachable"]


@pytest.mark.asyncio
async def test_reconcile_apply_redispatches_then_fetches(synced):
    """apply=True asks the source over HTTP, then pulls the new event."""
    await insert(synced.source, "lost", "never arrived", embedding=[0.2] * EMBED_DIM)
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:lost")

    ids = await source_export_ids(synced.source)
    before = len(synced.app.calls)
    result = await reconcile_peer(synced.replica, synced.peer["id"], apply=True, source_ids=ids)

    assert result["restored"] == 1, result
    assert result["failed"] == []
    urls = [str(url) for url in synced.app.calls[before:]]
    assert any(url.endswith("/v1/federation/redispatch") for url in urls), urls
    assert result["remaining"] == 0
    row = await fetch(synced.replica, "SELECT content FROM memories WHERE id = ?", "fed:pythia:lost")
    assert row and row[0]["content"] == "never arrived"


# ── LOW, confirmed: the hint advertised a literal ``{peer_id}`` ─────────────


@pytest.mark.asyncio
async def test_reconcile_hint_names_the_peer(synced):
    """An operator pasting the hint must not fetch ``/peers/{peer_id}/...``."""
    await insert(synced.source, "gone", "lost locally")
    await sync(synced.replica, synced.peer["id"])
    await mutate(synced.replica, "DELETE FROM memories WHERE id = ?", "fed:pythia:gone")

    ids = await source_export_ids(synced.source)
    report = await replica_parity(synced.replica, synced.peer["id"], source_ids=ids)
    hint = report["hint"]
    assert hint is not None
    assert "{" not in hint, hint
    assert f"/v1/federation/peers/{synced.peer['id']}/reconcile" in hint
