"""Concurrent federation sync triggers must block, not 409 (MN-25).

Two operators (or an operator plus the background worker) firing
``POST /v1/federation/peers/{id}/sync`` for the same peer at the same
time must SERIALIZE on the per-peer sync lock: the second request waits
for the first pass to finish and then performs its own pass and reports
its own result. The defect (research ``cf2-mn-sync-trigger-409``) is a
route-level busy/overlap guard that turns "a sync is already running"
into a 409 — an operator-visible conflict that leaves the second sync
unperformed. A route-level guard is also weaker than the lock: it
covers only route-to-route overlaps, not the documented route-vs-worker
case (the manual trigger route and the background worker both call
``sync_peer`` for the same peer).

The domain-layer lock itself is covered by
``test_federation_counters.py::test_concurrent_syncs_on_one_peer_do_not_overlap``;
this test covers the API contract: the trigger route hands the peer id
straight to ``sync_peer`` and lets the shared lock serialize the pass —
it never rejects a concurrent trigger.

Runs against a real SQLite backend through the in-process ASGI app,
the same rig ``test_federation_parity_reconcile.py`` uses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from mnemos.domain import federation as fed


@pytest_asyncio.fixture
async def trigger_app(tmp_path, monkeypatch):
    """Wire the sync-trigger route to a real SQLite backend and a fake feed.

    Yields (client, peer_id, hooks). ``hooks['gate']`` (an
    ``asyncio.Event``, set by default) gates the feed pull, and
    ``hooks['pull_started']`` signals that a pull is in flight — the test
    uses them to hold a sync mid-pull and prove a second trigger blocks
    until it finishes.
    """
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "0")
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")

    import mnemos.core.lifecycle as lc
    from mnemos.api.dependencies import UserContext, get_current_user
    from mnemos.api.main import app
    from mnemos.persistence.sqlite import SqliteBackend

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "fed-sync-trigger.db", settings)
    await backend.open()

    async with backend.transactional() as tx:
        peer = await backend.federation.create_peer(
            tx,
            name="pythia",
            base_url="https://pythia.example",
            auth_token="token-abc",
            namespace_filter=None,
            category_filter=None,
            enabled=True,
            sync_interval_secs=300,
            compat_mode="permissive",
        )

    hooks = {"gate": asyncio.Event(), "pull_started": asyncio.Event()}
    hooks["gate"].set()

    wire_row = {
        "id": "m1",
        "content": "facts",
        "category": "facts",
        "namespace": "default",
        "created": "2020-01-01T00:00:00Z",
        "updated": "2020-01-01T00:00:00Z",
        "federation_sequence": 1,
    }

    async def fake_pull(base_url, auth_token, since, namespace_filter, category_filter, **kwargs):
        hooks["pull_started"].set()
        await hooks["gate"].wait()
        return [dict(wire_row)], None, False

    async def schema_ok(*args, **kwargs):
        return {
            "ok": True,
            "mnemos_version": "7.0.0",
            "schema_signature": "7.0",
            "migrations_fingerprint": None,
        }

    async def fingerprint_ok(*args, **kwargs):
        return {"ok": True, "supported": True}

    class _NoopClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def noop_client(*args, **kwargs):
        # The pull is faked; the real HTTP client (with its SSRF/DNS
        # validation) must never touch the network.
        return _NoopClient(), object()

    monkeypatch.setattr(fed, "make_safe_client", noop_client)
    monkeypatch.setattr(fed, "_check_peer_schema", schema_ok)
    monkeypatch.setattr(fed, "post_peer_fingerprint", fingerprint_ok)
    monkeypatch.setattr(fed, "_pull_batch", fake_pull)
    monkeypatch.setattr(lc, "_persistence_backend", backend)

    async def root_user():
        return UserContext(
            user_id="root",
            group_ids=[],
            role="root",
            namespace="root-ns",
            authenticated=True,
        )

    app.dependency_overrides[get_current_user] = root_user
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30.0) as client:
            yield client, peer["id"], hooks
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        await backend.close()


@pytest.mark.asyncio
async def test_sync_trigger_blocks_while_a_sync_is_in_flight(trigger_app, monkeypatch):
    """A trigger arriving while a sync of the same peer is in flight
    must wait for it to finish, then complete its own pass with 200 —
    never 409.
    """
    client, peer_id, hooks = trigger_app

    # The route resolves the sync entry point as ``_fed.sync_peer`` on the
    # shared module object, so a wrapper here sees every call the route
    # makes. A route-level busy guard would skip the call and 409 instead.
    calls = []
    real_sync_peer = fed.sync_peer

    async def tracking_sync_peer(backend, peer_id, *a, **kw):
        calls.append(peer_id)
        return await real_sync_peer(backend, peer_id, *a, **kw)

    monkeypatch.setattr(fed, "sync_peer", tracking_sync_peer)

    hooks["pull_started"] = asyncio.Event()
    hooks["gate"] = asyncio.Event()  # closed: the in-flight sync is gated in the pull

    # First trigger: begins a sync and parks inside the (slow) pull.
    first = asyncio.create_task(client.post(f"/v1/federation/peers/{peer_id}/sync"))
    await asyncio.wait_for(hooks["pull_started"].wait(), timeout=5)

    # Second trigger while the first is mid-sync.
    second = asyncio.create_task(client.post(f"/v1/federation/peers/{peer_id}/sync"))
    await asyncio.sleep(0.1)

    # Let the first pass finish; the second must then run and finish too.
    hooks["gate"].set()
    first_resp = await asyncio.wait_for(first, timeout=15)
    second_resp = await asyncio.wait_for(second, timeout=15)

    assert len(calls) == 2, (
        "each trigger must hand the peer id to sync_peer; the per-peer lock "
        "serializes the passes (a route-level guard would skip the call)"
    )
    assert first_resp.status_code == 200, first_resp.text
    assert second_resp.status_code == 200, (
        "concurrent sync trigger must block, then succeed — got " f"{second_resp.status_code}: {second_resp.text}"
    )
    assert second_resp.status_code != 409, "overlap must never surface as 409"
    # The first pass applied the one wire row; the second pass had an
    # empty feed (fake serves one page) and must report a clean pass.
    assert first_resp.json()["pulled"] == 1
    assert second_resp.json()["pulled"] == 0
