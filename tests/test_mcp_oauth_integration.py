"""HTTP-level integration tests for the MCP OAuth 2.1 authorization server.

These tests drive the real Starlette ``/oauth/*`` and ``/.well-known/*``
routes mounted by ``mnemos/mcp/http.py``, plus the bearer middleware that
fronts ``/sse``.  The previous review rejected tests that only called
private service helpers — the public HTTP surface is what RFC-7591
clients and ChatGPT/Claude/Gemini connectors actually see, so the test
suite exercises that path end-to-end.

What this covers (positive + negative):

* Metadata endpoints are public and return RFC-8414/9728 shapes.
* Unauthenticated ``/sse`` is rejected with 401 ``WWW-Authenticate``.
* Unknown bearer tokens are rejected.
* Static ``MNEMOS_MCP_TOKEN`` continues to authenticate (Claude path).
* DCR is gated by the registration-secret header — missing/wrong
  secret returns 401, valid secret issues a client_id.
* PKCE enforcement: missing challenge, wrong method, wrong verifier are
  rejected; correct S256 flow issues a JWT access token + refresh.
* JWT signature is verified (attacker-signed token rejected), expired
  JWT rejected.
* Refresh-token rotation issues a fresh access token and revokes the
  prior refresh row.
* Wrong redirect_uri at /token is rejected.
* Empty/None admin passphrase fail-closed (constructor refuses to
  build the service).
"""
from __future__ import annotations

import importlib
import json
import secrets
import sys
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from httpx import ASGITransport, AsyncClient


def _import_fresh(monkeypatch: pytest.MonkeyPatch, *,
                  legacy_token: str = "legacy-shared-bearer-token",
                  signing_key: str = "integration-signing-key-32-bytes-min",
                  admin_passphrase: str = "integration-passphrase",
                  registration_secret: str = "integration-reg-secret",
                  base_url: str = "http://testserver"):
    """Reload ``mnemos.mcp.http`` with the OAuth env fully configured.

    The MCP HTTP module captures the OAuth service lazily, so re-importing
    after setting env is enough for the in-memory service to bind to the
    configured key/passphrase/secret.  ``MNEMOS_MCP_TOKEN`` continues to
    drive the legacy static-bearer path (Claude's connector).
    """
    monkeypatch.setenv("MNEMOS_MCP_TOKEN", legacy_token)
    monkeypatch.setenv("MNEMOS_OAUTH_SIGNING_KEY", signing_key)
    monkeypatch.setenv("MNEMOS_OAUTH_ADMIN_PASSPHRASE", admin_passphrase)
    monkeypatch.setenv("MNEMOS_OAUTH_REGISTRATION_SECRET", registration_secret)
    from mnemos.core import config

    config._reset_settings_for_tests()
    for mod in (
        "mnemos.mcp.http", "mnemos.mcp.oauth", "mnemos.mcp",
    ):
        sys.modules.pop(mod, None)
    return importlib.import_module("mnemos.mcp.http")


@asynccontextmanager
async def _client(http):
    async with AsyncClient(
        transport=ASGITransport(app=http.starlette_app),
        base_url="http://testserver",
    ) as client:
        yield client


def _pkce(verifier: str) -> str:
    from mnemos.mcp.oauth import pkce_s256
    return pkce_s256(verifier)


# ─── Public metadata ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorization_server_metadata_is_public_and_spec_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        response = await client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    body = response.json()
    assert "authorization_endpoint" in body and "/oauth/authorize" in body["authorization_endpoint"]
    assert "token_endpoint" in body and "/oauth/token" in body["token_endpoint"]
    assert "registration_endpoint" in body and "/oauth/register" in body["registration_endpoint"]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["response_types_supported"] == ["code"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]


@pytest.mark.asyncio
async def test_protected_resource_metadata_is_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        response = await client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    body = response.json()
    assert "authorization_servers" in body
    assert body["bearer_methods_supported"] == ["header"]


# ─── Auth gate ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sse_rejects_unauthenticated_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        response = await client.get("/sse")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").lower().startswith("bearer")


@pytest.mark.asyncio
async def test_sse_rejects_unknown_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        response = await client.get(
            "/sse", headers={"Authorization": "Bearer not-a-real-token"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_legacy_static_bearer_still_authenticates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy ``MNEMOS_MCP_TOKEN`` path Claude uses must keep working."""
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        # /sse will hang on an open SSE stream; we only need the auth
        # middleware to not 401 before streaming.  /health is unauth but
        # /messages/ is the post-handshake endpoint gated by middleware.
        # A 404/405 from /messages/ still proves auth was accepted.
        response = await client.post(
            "/messages/anything",
            headers={"Authorization": "Bearer legacy-shared-bearer-token"},
        )
    assert response.status_code != 401, (
        "legacy MNEMOS_MCP_TOKEN must still pass the bearer gate"
    )


# ─── Dynamic client registration ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_dcr_rejects_missing_registration_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        response = await client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://client.example/cb"]},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dcr_rejects_wrong_registration_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        response = await client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://client.example/cb"]},
            headers={"X-Mnemos-OAuth-Registration-Secret": "wrong"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dcr_with_admin_secret_issues_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        response = await client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://client.example/cb"],
                  "token_endpoint_auth_method": "none"},
            headers={"X-Mnemos-OAuth-Registration-Secret": "integration-reg-secret"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"].startswith("mnemos_")
    assert body["redirect_uris"] == ["https://client.example/cb"]


# ─── PKCE enforcement ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_without_pkce_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        # First register a client (no PKCE yet — DCR is independent of PKCE).
        reg = (
            await client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://client.example/cb"]},
                headers={"X-Mnemos-OAuth-Registration-Secret": "integration-reg-secret"},
            )
        ).json()
        cid = reg["client_id"]
        # Authorize without code_challenge: must 400.
        response = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code", "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "passphrase": "integration-passphrase",
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_authorize_with_plain_pkce_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        reg = (
            await client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://client.example/cb"]},
                headers={"X-Mnemos-OAuth-Registration-Secret": "integration-reg-secret"},
            )
        ).json()
        cid = reg["client_id"]
        response = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code", "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_challenge": "x", "code_challenge_method": "plain",
                "passphrase": "integration-passphrase",
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_authorize_wrong_redirect_uri_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        reg = (
            await client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://client.example/cb"]},
                headers={"X-Mnemos-OAuth-Registration-Secret": "integration-reg-secret"},
            )
        ).json()
        cid = reg["client_id"]
        verifier = secrets.token_urlsafe(32)
        response = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code", "client_id": cid,
                "redirect_uri": "https://attacker.example/cb",
                "code_challenge": _pkce(verifier),
                "code_challenge_method": "S256",
                "passphrase": "integration-passphrase",
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_authorize_wrong_passphrase_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        reg = (
            await client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://client.example/cb"]},
                headers={"X-Mnemos-OAuth-Registration-Secret": "integration-reg-secret"},
            )
        ).json()
        cid = reg["client_id"]
        verifier = secrets.token_urlsafe(32)
        response = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code", "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_challenge": _pkce(verifier),
                "code_challenge_method": "S256",
                "passphrase": "wrong-passphrase",
            },
        )
    assert response.status_code == 403


# ─── Happy path: full OAuth flow + JWT accepted by /sse gate ─────────────


@pytest.mark.asyncio
async def test_full_oauth_flow_then_jwt_authenticates_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register -> authorize -> token -> present JWT to /sse — proves the
    same JWT issuer that fronts ``/sse`` will also accept the JWT for
    the new OAuth flow.  This is the ChatGPT/Gemini path."""
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        # 1. DCR
        reg = (
            await client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://client.example/cb"]},
                headers={"X-Mnemos-OAuth-Registration-Secret": "integration-reg-secret"},
            )
        ).json()
        cid = reg["client_id"]

        # 2. Authorize (GET with passphrase in query; the form POST path is
        # the user-facing browser flow, this is the ChatGPT CLI path).
        verifier = secrets.token_urlsafe(32)
        response = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code", "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_challenge": _pkce(verifier),
                "code_challenge_method": "S256",
                "state": "xyz",
                "passphrase": "integration-passphrase",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        location = response.headers["location"]
        qs = parse_qs(urlparse(location).query)
        code = qs["code"][0]
        assert qs["state"][0] == "xyz"

        # 3. Exchange code+verifier for tokens.
        token = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code", "code": code,
                "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        token_body = token.json()
        access = token_body["access_token"]
        refresh = token_body["refresh_token"]
        assert token_body["token_type"] == "Bearer"
        assert token_body["scope"] == "mcp"
        assert refresh != access

        # 4. The JWT must be accepted by the bearer middleware that
        # fronts /sse — same gate that accepts the legacy static token.
        # /messages/anything is auth-gated and post-handshake; non-401
        # proves the middleware accepted the JWT.
        response = await client.post(
            "/messages/anything",
            headers={"Authorization": f"Bearer {access}"},
        )
        assert response.status_code != 401, (
            "JWT issued by /oauth/token must pass the bearer gate"
        )

        # 5. Refresh token rotation works (refresh grant + new access).
        refreshed = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token", "client_id": cid,
                "refresh_token": refresh,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        refreshed_body = refreshed.json()
        assert refreshed_body["access_token"] != access
        # Old refresh token must be revoked; reusing it is rejected.
        reuse = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token", "client_id": cid,
                "refresh_token": refresh,
            },
        )
        assert reuse.status_code == 400, "revoked refresh tokens must be rejected"


@pytest.mark.asyncio
async def test_token_with_wrong_verifier_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        reg = (
            await client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://client.example/cb"]},
                headers={"X-Mnemos-OAuth-Registration-Secret": "integration-reg-secret"},
            )
        ).json()
        cid = reg["client_id"]
        verifier = secrets.token_urlsafe(32)
        response = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code", "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_challenge": _pkce(verifier),
                "code_challenge_method": "S256",
                "passphrase": "integration-passphrase",
            },
            follow_redirects=False,
        )
        code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
        bad = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code", "code": code,
                "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_verifier": "WRONG-VERIFIER-1234567890",
            },
        )
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_token_with_wrong_redirect_uri_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _import_fresh(monkeypatch)
    async with _client(http) as client:
        reg = (
            await client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://client.example/cb"]},
                headers={"X-Mnemos-OAuth-Registration-Secret": "integration-reg-secret"},
            )
        ).json()
        cid = reg["client_id"]
        verifier = secrets.token_urlsafe(32)
        response = await client.get(
            "/oauth/authorize",
            params={
                "response_type": "code", "client_id": cid,
                "redirect_uri": "https://client.example/cb",
                "code_challenge": _pkce(verifier),
                "code_challenge_method": "S256",
                "passphrase": "integration-passphrase",
            },
            follow_redirects=False,
        )
        code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
        bad = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code", "code": code,
                "client_id": cid,
                "redirect_uri": "https://attacker.example/cb",
                "code_verifier": verifier,
            },
        )
    assert bad.status_code == 400


# ─── JWT negative tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attacker_signed_jwt_is_rejected_by_sse_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JWT signed with the wrong key must NOT pass the bearer middleware."""
    # The middleware uses the configured signing key for verification.
    # An attacker cannot sign with that key.  We construct a JWT signed
    # with an attacker-controlled key and confirm /sse 401s on it.
    http = _import_fresh(monkeypatch)
    forged = jwt.encode(
        {"sub": "default", "aud": "mnemos-mcp", "iss": "http://testserver/",
         "client_id": "evil", "scope": "mcp", "jti": "x",
         "iat": 0, "exp": 9_999_999_999},
        "attacker-key", algorithm="HS256",
    )
    async with _client(http) as client:
        response = await client.post(
            "/messages/anything",
            headers={"Authorization": f"Bearer {forged}"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_jwt_is_rejected_by_sse_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired JWT must NOT pass the bearer middleware (exp is verified)."""
    http = _import_fresh(monkeypatch)
    from mnemos.mcp.oauth import InMemoryOAuthStore, OAuthService
    # Mint an already-expired JWT using the configured signing key so the
    # signature is valid for the server but the exp claim is in the past.
    service = OAuthService(
        base_url="http://testserver",
        signing_key="integration-signing-key-32-bytes-min",
        store=InMemoryOAuthStore(),
        registration_secret="integration-reg-secret",
        admin_passphrase="integration-passphrase",
    )
    expired = service.issue_access_token(client_id="x", lifetime=-5)
    async with _client(http) as client:
        response = await client.post(
            "/messages/anything",
            headers={"Authorization": f"Bearer {expired}"},
        )
    assert response.status_code == 401


# ─── Fail-closed configuration ────────────────────────────────────────────


def test_oauth_service_rejects_empty_passphrase() -> None:
    """An empty passphrase would let ``passphrase=`` pass hmac.compare_digest;
    the constructor refuses to build such a service."""
    from mnemos.mcp.oauth import InMemoryOAuthStore, OAuthService
    with pytest.raises(ValueError):
        OAuthService(
            base_url="http://testserver", signing_key="k" * 32,
            store=InMemoryOAuthStore(), registration_secret="r",
            admin_passphrase="",
        )


def test_oauth_service_rejects_none_passphrase() -> None:
    """A None passphrase must not silently pass auth; constructor refuses."""
    from mnemos.mcp.oauth import InMemoryOAuthStore, OAuthService
    with pytest.raises(ValueError):
        OAuthService(
            base_url="http://testserver", signing_key="k" * 32,
            store=InMemoryOAuthStore(), registration_secret="r",
            admin_passphrase=None,  # type: ignore[arg-type]
        )


def test_oauth_service_rejects_missing_signing_key() -> None:
    from mnemos.mcp.oauth import InMemoryOAuthStore, OAuthService
    with pytest.raises(ValueError):
        OAuthService(
            base_url="http://testserver", signing_key="",
            store=InMemoryOAuthStore(), registration_secret="r",
            admin_passphrase="p",
        )
