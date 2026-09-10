"""Ownership and startup-failure regression coverage for the MCP backend."""

from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys

import pytest
from starlette.applications import Starlette

from tests.oauth_backend_helpers import oauth_database as oauth_database
from tests.test_mcp_oauth_integration import mcp_http_app as mcp_http_app


@pytest.mark.asyncio
async def test_mcp_borrows_lifecycle_backend_without_another_pool(mcp_http_app, monkeypatch):
    from mnemos.core import lifecycle

    backend = mcp_http_app.service.store.backend
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    async def unexpected_factory(*_args):
        pytest.fail("an existing lifecycle backend must be reused")

    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", unexpected_factory)
    app = Starlette()
    async with mcp_http_app.http._mcp_http_lifespan(app):
        assert app.state.persistence_backend is backend
        assert mcp_http_app.http.get_oauth_service().store.backend is backend
    assert await backend.ping(), "the borrower must not close the lifecycle's backend"


@pytest.mark.asyncio
async def test_owned_backend_closes_when_oauth_initialization_fails(mcp_http_app, monkeypatch):
    from mnemos.core import lifecycle

    backend = mcp_http_app.service.store.backend
    closed = []
    real_close = backend.close

    async def close():
        closed.append(True)
        await real_close()

    async def factory(_settings):
        return "test", backend

    monkeypatch.setattr(backend, "close", close)
    monkeypatch.setattr(lifecycle, "_persistence_backend", None)
    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", factory)
    settings = SimpleNamespace(
        oauth=SimpleNamespace(
            database_url="",
            issuer="http://testserver",
            signing_key="short",
            registration_secret="",
            admin_passphrase="passphrase",
        )
    )
    monkeypatch.setattr(mcp_http_app.http, "get_settings", lambda: settings)
    app = Starlette()
    with pytest.raises(ValueError, match="at least"):
        async with mcp_http_app.http._mcp_http_lifespan(app):
            pytest.fail("invalid key must abort startup")
    assert closed == [True]
    assert not hasattr(app.state, "persistence_backend")


def test_sqlite_only_standalone_startup_and_http_flow_without_socket(tmp_path):
    """Real standalone app startup and HTTP routes; no socket/SDK stubs.

    This supplements, but does not replace, the real network/SSE CLI test.
    A fresh interpreter prevents pytest's settings/module stubs masking wiring.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MNEMOS_", "PG_")) and key not in {"ORACLE_DSN", "DB2_DSN"}
    }
    env.update(
        MNEMOS_DATABASE_DSN=f"sqlite:///{tmp_path / 'standalone.db'}",
        MNEMOS_CONFIG_PATH=str(tmp_path / "absent.toml"),
        MNEMOS_OAUTH_ISSUER="http://testserver",
        MNEMOS_OAUTH_ADMIN_PASSPHRASE="test-only-standalone-passphrase",
    )
    script = """
import asyncio
import secrets
from urllib.parse import parse_qs, urlparse
from httpx import ASGITransport, AsyncClient
from mnemos.mcp.http import starlette_app, _mcp_http_lifespan, get_oauth_service
from mnemos.mcp.oauth import pkce_s256

async def main():
    async with _mcp_http_lifespan(starlette_app):
        first_key = get_oauth_service().signing_key
        assert len(first_key) >= 32
        assert type(starlette_app.state.persistence_backend).__name__ == "SqliteBackend"
        async with AsyncClient(transport=ASGITransport(app=starlette_app), base_url="http://testserver") as client:
            assert (await client.get("/.well-known/oauth-authorization-server")).status_code == 200
            registration = await client.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1/cb"]})
            assert registration.status_code == 201, registration.text
            client_id = registration.json()["client_id"]
            verifier = secrets.token_urlsafe(32)
            approval = await client.post("/oauth/authorize", data={
                "response_type": "code", "client_id": client_id, "redirect_uri": "http://127.0.0.1/cb",
                "code_challenge": pkce_s256(verifier), "code_challenge_method": "S256",
                "passphrase": "test-only-standalone-passphrase",
            })
            assert approval.status_code == 303, approval.text
            code = parse_qs(urlparse(approval.headers["location"]).query)["code"][0]
            token = await client.post("/oauth/token", data={
                "grant_type": "authorization_code", "client_id": client_id, "code": code,
                "code_verifier": verifier, "redirect_uri": "http://127.0.0.1/cb",
            })
            assert token.status_code == 200, token.text
            tokens = token.json()
            assert get_oauth_service().validate_access_token(tokens["access_token"])["client_id"] == client_id
    assert not hasattr(starlette_app.state, "persistence_backend")
    async with _mcp_http_lifespan(starlette_app):
        assert get_oauth_service().signing_key == first_key
        async with AsyncClient(transport=ASGITransport(app=starlette_app), base_url="http://testserver") as client:
            refreshed = await client.post("/oauth/token", data={
                "grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"],
            })
            assert refreshed.status_code == 200, refreshed.text

asyncio.run(main())
print("SQLite-only standalone startup, HTTP DCR/PKCE/token, reopen/refresh: PASS")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reopen/refresh: PASS" in result.stdout
