"""POST/GET/DELETE /admin/.../apikeys must work without a Postgres pool.

The three admin api_keys endpoints used to open with
``require_postgres_pool_or_503`` and then run raw asyncpg SQL. On the
SQLite edge profile ``lifecycle._pool`` is deliberately ``None``, so
every one of them returned:

    503 "endpoint requires Postgres backend"

even though ``lookup_api_key`` — the read half of the same table — had
been backend-neutral since v6.3. An operator could authenticate with an
API key but had no supported way to mint one.

These tests drive the route functions against a real SQLite backend with
``lifecycle._pool`` pinned to ``None``, which is exactly the condition
that used to 503. They also pin the behaviours the Postgres path already
had — 404 on an unknown user, 422 past ten active keys, the raw key
returned once — so making the write neutral did not quietly change the
contract.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

import mnemos.core.lifecycle as _lc
from mnemos.api.routes.admin import create_api_key, list_api_keys, revoke_api_key
from mnemos.domain.models import ApiKeyCreateRequest


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    """A live SQLite backend installed as the process persistence backend.

    ``_pool`` is forced to None: that is the edge-profile condition the
    old ``require_postgres_pool_or_503`` gate turned into a 503.
    """
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "admin.db", SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))
    await backend.open()
    monkeypatch.setattr(_lc, "_persistence_backend", backend)
    monkeypatch.setattr(_lc, "_pool", None)
    try:
        yield backend
    finally:
        await backend.close()


def _root():
    return MagicMock(role="root")


@pytest.mark.asyncio
async def test_create_api_key_on_sqlite_returns_a_usable_key(sqlite_backend):
    """The regression: creating a key on a non-Postgres backend must work,
    and the key it hands back must authenticate."""
    response = await create_api_key("default", ApiKeyCreateRequest(label="edge-key"), _=_root())

    assert response.user_id == "default"
    assert response.label == "edge-key"
    assert response.revoked is False
    assert response.raw_key and len(response.raw_key) == 64, "raw key is returned exactly once, on creation"
    assert response.key_prefix == response.raw_key[:8], "the listing prefix must match the issued key"
    assert isinstance(response.created_at, str) and response.created_at

    async with sqlite_backend.transactional() as tx:
        resolved = await sqlite_backend.oauth.lookup_api_key(
            tx, hashlib.sha256(response.raw_key.encode()).hexdigest()
        )
    assert resolved is not None, "the key the admin route just issued must authenticate"
    assert resolved["user_id"] == "default"


@pytest.mark.asyncio
async def test_default_user_is_seeded_on_sqlite(sqlite_backend):
    """``default`` must exist on a fresh edge database.

    Postgres has seeded this row since migrations_v1_multiuser.sql. SQLite
    did not, so api_keys.user_id had nothing to join against and every
    minted key failed the INNER JOIN in lookup_api_key.
    """
    async with sqlite_backend.transactional() as tx:
        assert await sqlite_backend.oauth.user_exists(tx, "default") is True


@pytest.mark.asyncio
async def test_unknown_user_still_404s(sqlite_backend):
    with pytest.raises(HTTPException) as exc:
        await create_api_key("nobody", ApiKeyCreateRequest(label=None), _=_root())
    assert exc.value.status_code == 404
    assert "nobody" in exc.value.detail


@pytest.mark.asyncio
async def test_ten_key_cap_still_422s(sqlite_backend):
    for i in range(10):
        await create_api_key("default", ApiKeyCreateRequest(label=f"k{i}"), _=_root())

    with pytest.raises(HTTPException) as exc:
        await create_api_key("default", ApiKeyCreateRequest(label="eleventh"), _=_root())
    assert exc.value.status_code == 422
    assert "10 active API keys" in exc.value.detail

    # Revoking one frees a slot — the cap counts active keys, not rows.
    listed = await list_api_keys("default", _=_root())
    await revoke_api_key(listed[0].id, _=_root())
    freed = await create_api_key("default", ApiKeyCreateRequest(label="eleventh"), _=_root())
    assert freed.label == "eleventh"


@pytest.mark.asyncio
async def test_list_and_revoke_round_trip(sqlite_backend):
    created = await create_api_key("default", ApiKeyCreateRequest(label="listed"), _=_root())

    listed = await list_api_keys("default", _=_root())
    assert [k.id for k in listed] == [created.id]
    assert listed[0].raw_key is None, "a listing must never echo the secret"
    assert listed[0].key_prefix == created.key_prefix

    await revoke_api_key(created.id, _=_root())

    with pytest.raises(HTTPException) as exc:
        await revoke_api_key(created.id, _=_root())
    assert exc.value.status_code == 404, "re-revoking an already-revoked key is a 404"

    with pytest.raises(HTTPException) as exc:
        await revoke_api_key("not-a-real-id", _=_root())
    assert exc.value.status_code == 404, "an unknown key id is a 404, not a 500"

    after = await list_api_keys("default", _=_root())
    assert after[0].revoked is True, "a revoked key stays listed and flagged"


@pytest.mark.asyncio
async def test_list_unknown_user_404s(sqlite_backend):
    with pytest.raises(HTTPException) as exc:
        await list_api_keys("nobody", _=_root())
    assert exc.value.status_code == 404


def test_apikey_routes_no_longer_take_the_postgres_gate():
    """Source-level guard: re-adding the pool gate to these three routes
    would silently re-break every non-Postgres backend, and the runtime
    tests above only catch it if the fixture still pins _pool to None.

    Parsed with ast rather than grepped so the prose in the routes' own
    docstrings — which names the gate it used to take — cannot satisfy or
    trip the assertion.
    """
    import ast
    import inspect
    import textwrap

    from mnemos.api.routes import admin

    for fn in (admin.create_api_key, admin.list_api_keys, admin.revoke_api_key):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "require_postgres_pool_or_503" not in called, (
            f"{fn.__name__} re-acquired the Postgres-only gate; the api_keys surface "
            f"is backend-neutral and must stay that way"
        )
        assert "require_oauth_backend" in called, (
            f"{fn.__name__} must resolve its backend through require_oauth_backend"
        )
