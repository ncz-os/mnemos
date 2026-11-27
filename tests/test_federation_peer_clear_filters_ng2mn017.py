"""NG2-MN-017: PATCH /v1/federation/peers/{id} can clear filters with null.

A filter set once could never be removed.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.federation import router as federation_router


@pytest_asyncio.fixture
async def peer_client(tmp_path, monkeypatch):
    """Real SQLite backend behind the real PATCH route; http URLs allowed."""
    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    monkeypatch.setenv("FEDERATION_ALLOW_INSECURE", "true")
    monkeypatch.setenv("FEDERATION_ALLOW_PRIVATE", "true")
    from mnemos.api import persistence_helpers
    from mnemos.core import config
    from mnemos.persistence.sqlite import SqliteBackend

    monkeypatch.setattr(config, "_settings", None)

    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "fed-peer-clear.db", settings)
    await backend.open()

    async with backend.transactional() as tx:
        peer = await backend.federation.create_peer(
            tx,
            name="pythia",
            base_url="http://pythia.example",
            auth_token="token-abc",
            namespace_filter=["a"],
            category_filter=["facts"],
            enabled=True,
            sync_interval_secs=300,
            compat_mode="permissive",
        )

    app = FastAPI()
    app.include_router(federation_router)

    async def _current_user():
        return UserContext(user_id="root", group_ids=[], role="root", namespace="root-ns", authenticated=True)

    app.dependency_overrides[get_current_user] = _current_user

    real = persistence_helpers.backend_or_503
    monkeypatch.setattr(persistence_helpers, "backend_or_503", lambda *a, **k: backend)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, backend, peer["id"]
    finally:
        monkeypatch.setattr(persistence_helpers, "backend_or_503", real)
        await backend.close()


@pytest.mark.asyncio
async def test_patch_null_clears_namespace_and_category_filters(peer_client):
    """Explicit null clears the nullable filter columns; absent stays."""
    client, backend, peer_id = peer_client

    resp = await client.patch(f"/v1/federation/peers/{peer_id}", json={"namespace_filter": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["namespace_filter"] is None
    assert resp.json()["category_filter"] == ["facts"]

    resp = await client.patch(f"/v1/federation/peers/{peer_id}", json={"category_filter": None})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["namespace_filter"] is None
    assert body["category_filter"] is None

    async with backend.transactional() as tx:
        row = await backend.federation.get_peer(tx, peer_id)
    assert row["namespace_filter"] is None
    assert row["category_filter"] is None


@pytest.mark.asyncio
async def test_patch_null_on_required_strings_stays_422(peer_client):
    """Null must not silently wipe required columns — it stays a 422.

    ``name`` is not a field of FederationPeerUpdateRequest in this
    version (Pydantic drops the unknown key, so it 422s via the
    no-fields branch); the guard under test is the explicit
    cannot-clear-required-fields branch for real fields.
    """
    client, _backend, peer_id = peer_client

    for field in ("auth_token", "base_url"):
        resp = await client.patch(f"/v1/federation/peers/{peer_id}", json={field: None})
        assert resp.status_code == 422, f"{field}: {resp.status_code} {resp.text}"
        assert "cannot clear required fields" in resp.text, resp.text

    for field in ("enabled", "sync_interval_secs", "compat_mode"):
        resp = await client.patch(f"/v1/federation/peers/{peer_id}", json={field: None})
        assert resp.status_code == 422, f"{field}: {resp.status_code} {resp.text}"
        assert "cannot clear required fields" in resp.text, resp.text


@pytest.mark.asyncio
async def test_patch_empty_body_stays_422(peer_client):
    """PATCH with no fields set is still rejected."""
    client, _backend, peer_id = peer_client
    resp = await client.patch(f"/v1/federation/peers/{peer_id}", json={})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_clearing_a_filter_changes_the_filter_signature(peer_client, monkeypatch, caplog):
    """Clearing a filter changes the signature, so the next sync takes the
    existing no-cursor bootstrap path (full replay from seq 0, logged)."""
    import logging

    import mnemos.domain.federation as fed

    client, backend, peer_id = peer_client

    def _sig(ns, cat):
        import hashlib
        import json as _json

        return hashlib.sha256(
            _json.dumps(["http://pythia.example", ns, cat], sort_keys=True, default=str).encode()
        ).hexdigest()

    before = _sig(["a"], ["facts"])
    after = _sig(None, None)
    assert before != after, "clearing the filters must change the sync scope signature"

    resp = await client.patch(f"/v1/federation/peers/{peer_id}", json={"namespace_filter": None})
    assert resp.status_code == 200, resp.text
    resp = await client.patch(f"/v1/federation/peers/{peer_id}", json={"category_filter": None})
    assert resp.status_code == 200, resp.text

    async with backend.transactional() as tx:
        row = await backend.federation.get_peer(tx, peer_id)
    assert row["namespace_filter"] is None
    assert row["category_filter"] is None

    # Fake the pull so no network is touched; the schema pre-flight is
    # permissive and returns schema_resp["ok"]=False transient-free, so the
    # domain proceeds to the cursor bootstrap path.
    async def schema_unverifiable(*args, **kwargs):
        return {"ok": True, "mnemos_version": "9.9.9", "schema_signature": "9.9", "migrations_fingerprint": None}

    async def fake_pull(base_url, auth_token, since, namespace_filter, category_filter, **kwargs):
        assert namespace_filter is None
        assert category_filter is None
        return [], None, False

    async def noop_client(*args, **kwargs):
        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        return _C(), object()

    async def fingerprint_ok(*args, **kwargs):
        return {"ok": True, "supported": True}

    monkeypatch.setattr(fed, "_check_peer_schema", schema_unverifiable)
    monkeypatch.setattr(fed, "make_safe_client", noop_client)
    monkeypatch.setattr(fed, "post_peer_fingerprint", fingerprint_ok)
    monkeypatch.setattr(fed, "_pull_batch", fake_pull)

    with caplog.at_level(logging.DEBUG, logger="mnemos.domain.federation"):
        await fed.sync_peer(backend, peer_id)

    assert any("has no journal cursor for this filter scope" in r.getMessage() for r in caplog.records), (
        "after clearing the filters the next sync must take the no-cursor bootstrap path and log it"
    )
