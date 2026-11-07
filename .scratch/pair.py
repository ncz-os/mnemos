"""Shared source+replica SQLite pair harness for F2 investigation."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", "1")
os.environ.setdefault("MNEMOS_EMBEDDING_DIM", "768")

import httpx
from fastapi import FastAPI

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.federation import router as federation_router
from mnemos.domain import federation as fed
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FACTS = "facts"
EMBED_DIM = 768
OWNER = "alice"


class _Settings(SimpleNamespace):
    def __init__(self):
        super().__init__()
        self.database = SimpleNamespace(embedding_dim=EMBED_DIM)
        self.providers = SimpleNamespace(inference_embed_model="bge-m3")


async def open_backend(path):
    backend = SqliteBackend(path, _Settings())
    await backend.open()
    return backend


async def insert(backend, memory_id, content=None, *, namespace="default", owner_id=OWNER,
                 permission_mode=644, federation_source=None, federation_remote_updated=None,
                 archived_at=None, consolidated_into=None, deleted_at=None,
                 created=None, updated=None):
    created = created or NOW
    updated = updated or created
    async with backend.transactional() as tx:
        ops = _Ops(tx, transaction_dialect(tx))
        await ops.execute(
            "INSERT INTO memories(id, content, category, owner_id, namespace, permission_mode, "
            "created, updated, federation_source, federation_remote_updated, archived_at, "
            "consolidated_into, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            memory_id, content or f"c-{memory_id}", FACTS, owner_id, namespace, permission_mode,
            created.isoformat(), updated.isoformat(), federation_source, federation_remote_updated,
            archived_at, consolidated_into, deleted_at,
        )


async def mutate(backend, sql, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).execute(sql, *params)


async def fetch(backend, sql, *params):
    async with backend.transactional() as tx:
        return await _Ops(tx, transaction_dialect(tx)).fetchall(sql, *params)


class _SourceTransport(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self._app = app
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(request.url)
        rewritten = httpx.Request(
            method=request.method,
            url=request.url.copy_with(scheme="http", host="pythia", port=None),
            headers=request.headers,
            content=request.content,
            extensions=request.extensions,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app), base_url="http://pythia") as inner:
            return await inner.send(rewritten)


def build_source_app(source, monkeypatch):
    from mnemos.api import persistence_helpers

    async def _current_user():
        return UserContext(user_id="federation-bot", group_ids=[], role="root", namespace=None, authenticated=True)

    app = FastAPI()
    app.include_router(federation_router)
    app.dependency_overrides[get_current_user] = _current_user
    transport = _SourceTransport(app)

    async def _safe_client(url, *, timeout=None, allow_private=True, **kwargs):
        return httpx.AsyncClient(transport=transport, base_url="http://pythia"), None

    monkeypatch.setattr(fed, "make_safe_client", _safe_client)
    monkeypatch.setattr(persistence_helpers, "backend_or_503", lambda *a, **k: source)
    return app


async def register_peer(replica, *, name="pythia", copy_embeddings=True):
    async with replica.transactional() as tx:
        peer = await replica.federation.create_peer(
            tx, name=name, base_url="http://pythia", auth_token="token",
            namespace_filter=None, category_filter=None, enabled=True,
            sync_interval_secs=300, compat_mode="permissive",
        )
    if copy_embeddings:
        await mutate(replica, "UPDATE federation_peers SET copy_embeddings = 1 WHERE id = ?", peer["id"])
    return peer


async def sync(replica, peer_id):
    async def _schema_ok(*a, **k):
        return {"ok": True, "mnemos_version": "9.9.9", "schema_signature": "9.9", "migrations_fingerprint": None}
    real = fed._check_peer_schema
    fed._check_peer_schema = _schema_ok
    try:
        return await fed.sync_peer(replica, peer_id)
    finally:
        fed._check_peer_schema = real


async def feed_served_ids(backend, *, namespaces=(), categories=(), limit=100):
    """Every id the live feed serves, following the real journal cursor."""
    served = {}
    seq = 0
    guard = 0
    while True:
        guard += 1
        if guard > 5000:
            raise RuntimeError("feed cursor did not terminate")
        async with backend.transactional() as tx:
            rows = await backend.federation.feed_query(
                tx, since_updated=None, since_id=f"journal:{seq}", namespaces=list(namespaces),
                categories=list(categories), limit=limit, prefer_compressed=False,
            )
        if not rows:
            break
        for row in rows:
            served[str(row["id"])] = row.get("type")
        last = rows[-1]
        cid = last.get("cursor_id")
        nxt = int(cid[len("journal:"):]) if cid and cid.startswith("journal:") else None
        if len(rows) < limit:
            break
        if nxt is None or nxt <= seq:
            break
        seq = nxt
    return served


async def replica_ids(replica, peer="pythia"):
    rows = await fetch(replica, "SELECT id FROM memories WHERE federation_source = ?", peer)
    return {r["id"][len(f"fed:{peer}:"):] for r in rows}
