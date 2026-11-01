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
from types import SimpleNamespace

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
