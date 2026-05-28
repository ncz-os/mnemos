from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext, get_current_user, require_root
from mnemos.api.routes import (
    document_import,
    entities,
    morpheus,
    sessions,
    state,
)
from mnemos.persistence.sqlite import SqliteBackend


def _user() -> UserContext:
    return UserContext(user_id="u", group_ids=[], role="root", namespace="default", authenticated=True)


@pytest.fixture
def edge_client(monkeypatch, tmp_path):
    backend = SqliteBackend(tmp_path / "edge.sqlite3", SimpleNamespace())
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_pool", None)
    monkeypatch.setattr(lifecycle, "_pool_manager", None)

    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(entities.router)
    app.include_router(state.router)
    app.include_router(morpheus.router)
    app.include_router(document_import.router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[require_root] = _user
    return TestClient(app)


def test_edge_profile_postgres_only_routes_return_503(edge_client):
    cases = [
        ("get", "/entities", {}),
        ("get", "/v1/morpheus/runs", {}),
    ]
    for method, path, kwargs in cases:
        resp = getattr(edge_client, method)(path, **kwargs)
        assert resp.status_code == 503
        # Detail wording differs across routes — all hit
        # ``_require_postgres_backend`` which produces the
        # "requires a Postgres backend" message.
        assert "Postgres backend" in resp.text


def test_edge_profile_state_route_uses_sqlite_backend(edge_client):
    resp = edge_client.put("/state/edge-key", json={"value": {"ok": True}})
    assert resp.status_code == 200, resp.text

    resp = edge_client.get("/state/edge-key")
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == '{"ok": true}'


def test_edge_profile_documents_import_no_longer_requires_postgres(edge_client, monkeypatch):
    """Document import is backend-lifted; SQLite profiles should no
    longer fail at the Postgres-only route gate."""
    monkeypatch.setattr(document_import, "DOCLING_AVAILABLE", False)
    resp = edge_client.post(
        "/v1/documents/import",
        files={"file": ("doc.txt", b"hello", "text/plain")},
        data={"project_tag": "mnemos"},
    )
    assert resp.status_code == 501
    assert "Postgres backend" not in resp.text


def test_edge_profile_documents_batch_import_no_longer_requires_postgres(edge_client, monkeypatch):
    monkeypatch.setattr(document_import, "DOCLING_AVAILABLE", False)
    resp = edge_client.post(
        "/v1/documents/batch-import",
        files=[
            ("files", ("a.txt", b"hello", "text/plain")),
            ("files", ("b.txt", b"world", "text/plain")),
        ],
        data={"project_tag": "mnemos"},
    )
    assert resp.status_code == 207
    assert "Postgres backend" not in resp.text
    assert {entry["status_code"] for entry in resp.json()} == {501}
