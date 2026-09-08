"""Provider-neutral OAuth 2.1 authorization server for the remote MCP edge.

This module deliberately contains no provider-specific branches.  ``provider``
is an audit claim only; authorization is based on the registered client and
PKCE-bound grant.  The production store is PostgreSQL, while the small
in-memory store is useful for isolated tests and explicit development use.
"""
from __future__ import annotations

import base64
import json
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from mnemos.core.config import get_settings

ISSUER_PATH = "/"
ACCESS_TOKEN_SECONDS = 30 * 60
REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
CODE_SECONDS = 5 * 60
ADMIN_SUBJECT = "default"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_s256(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class InMemoryOAuthStore:
    """A test/development store with the same operations as the PG store."""

    def __init__(self) -> None:
        self.clients: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.signing_key: str | None = None

    async def get_signing_key(self) -> str | None:
        return self.signing_key

    async def save_client(self, row: dict[str, Any]) -> None:
        self.clients[row["client_id"]] = row

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self.clients.get(client_id)

    async def save_code(self, row: dict[str, Any]) -> None:
        self.codes[row["code"]] = row

    async def consume_code(self, code: str) -> dict[str, Any] | None:
        row = self.codes.get(code)
        if not row or row["used_at"] is not None or row["expires_at"] <= _now():
            return None
        row["used_at"] = _now()
        return row

    async def save_token(self, row: dict[str, Any]) -> None:
        self.tokens[row["jti"]] = row

    async def get_refresh(self, token_hash: str) -> dict[str, Any] | None:
        for row in self.tokens.values():
            if row["refresh_token_hash"] == token_hash:
                return row
        return None

    async def revoke_token(self, jti: str) -> None:
        row = self.tokens.get(jti)
        if row:
            row["revoked_at"] = _now()


class PostgresOAuthStore:
    """Parameterized asyncpg persistence for the MCP authorization server."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def get_signing_key(self) -> str | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT signing_key FROM oauth_mcp_signing_keys WHERE key_id = $1",
                "default",
            )
            return row["signing_key"] if row else None

    async def save_client(self, row: dict[str, Any]) -> None:
        redirect_uris = row["redirect_uris"]
        if isinstance(redirect_uris, list):
            redirect_uris = json.dumps(redirect_uris)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO oauth_mcp_clients
                   (client_id, client_secret, redirect_uris, token_endpoint_auth_method)
                   VALUES ($1, $2, $3, $4)""",
                row["client_id"], row.get("client_secret"), redirect_uris,
                row["token_endpoint_auth_method"],
            )

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT client_id, client_secret, redirect_uris,
                          token_endpoint_auth_method
                   FROM oauth_mcp_clients WHERE client_id = $1""", client_id,
            )
            return dict(row) if row else None

    async def save_code(self, row: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO oauth_mcp_authorization_codes
                   (code, client_id, code_challenge, code_challenge_method,
                    redirect_uri, expires_at)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                row["code"], row["client_id"], row["code_challenge"],
                row["code_challenge_method"], row["redirect_uri"], row["expires_at"],
            )

    async def consume_code(self, code: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE oauth_mcp_authorization_codes
                   SET used_at = NOW()
                   WHERE code = $1 AND used_at IS NULL AND expires_at > NOW()
                   RETURNING code, client_id, code_challenge, code_challenge_method,
                             redirect_uri, expires_at""", code,
            )
            return dict(row) if row else None

    async def save_token(self, row: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO oauth_mcp_tokens
                   (jti, refresh_token_hash, client_id, expires_at)
                   VALUES ($1, $2, $3, $4)""",
                row["jti"], row["refresh_token_hash"], row["client_id"], row["expires_at"],
            )

    async def get_refresh(self, token_hash: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT jti, refresh_token_hash, client_id, expires_at, revoked_at
                   FROM oauth_mcp_tokens
                   WHERE refresh_token_hash = $1 AND revoked_at IS NULL""", token_hash,
            )
            return dict(row) if row else None

    async def revoke_token(self, jti: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE oauth_mcp_tokens SET revoked_at = NOW() WHERE jti = $1", jti,
            )

    async def save_signing_key(self, *, key_id: str, signing_key: str) -> None:
        """Persist a signing key. Idempotent on (key_id); the first
        writer wins via ``ON CONFLICT DO NOTHING`` so concurrent
        first-boots don't clobber each other's key."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO oauth_mcp_signing_keys (key_id, signing_key)
                   VALUES ($1, $2)
                   ON CONFLICT (key_id) DO NOTHING""",
                key_id, signing_key,
            )


class OAuthService:
    def __init__(self, *, base_url: str, signing_key: str, store: Any,
                 registration_secret: str, admin_passphrase: str) -> None:
        if not signing_key:
            raise ValueError("OAuth signing key is not configured")
        # Fail-closed: an empty/None passphrase would let `passphrase=`
        # pass hmac.compare_digest (empty vs empty) and None would raise.
        if not admin_passphrase:
            raise ValueError(
                "OAuth admin passphrase must be a non-empty string; "
                "configure MNEMOS_OAUTH_ADMIN_PASSPHRASE."
            )
        self.base_url = base_url.rstrip("/") + "/"
        self.signing_key = signing_key
        self.store = store
        self.registration_secret = registration_secret
        self.admin_passphrase = admin_passphrase

    def issue_access_token(self, *, client_id: str, provider: str = "unknown",
                           now: datetime | None = None, lifetime: int = ACCESS_TOKEN_SECONDS) -> str:
        current = now or _now()
        claims = {
            "sub": ADMIN_SUBJECT, "aud": "mnemos-mcp", "iss": self.base_url,
            "client_id": client_id, "provider": provider, "scope": "mcp",
            "jti": secrets.token_urlsafe(24), "iat": current,
            "exp": current + timedelta(seconds=lifetime),
        }
        return jwt.encode(claims, self.signing_key, algorithm="HS256")

    def validate_access_token(self, token: str) -> dict[str, Any]:
        # PyJWT verifies the HS256 signature and exp claim here; do not use
        # decode(..., verify_signature=False), even for metadata/audit.
        claims = jwt.decode(
            token, self.signing_key, algorithms=["HS256"], audience="mnemos-mcp",
            issuer=self.base_url, options={"require": ["sub", "aud", "iss", "client_id", "jti", "exp"]},
        )
        if claims["sub"] != ADMIN_SUBJECT or claims.get("scope") != "mcp":
            raise jwt.InvalidTokenError("invalid MCP subject or scope")
        return claims

    async def register(self, body: dict[str, Any]) -> dict[str, Any]:
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
            raise ValueError("redirect_uris must be a non-empty array")
        auth_method = body.get("token_endpoint_auth_method", "none")
        if auth_method not in {"none", "client_secret_post"}:
            raise ValueError("unsupported token_endpoint_auth_method")
        client_id = "mnemos_" + secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32) if auth_method != "none" else None
        await self.store.save_client({
            "client_id": client_id, "client_secret": secret, "redirect_uris": uris,
            "token_endpoint_auth_method": auth_method,
        })
        response = {"client_id": client_id, "client_id_issued_at": int(_now().timestamp()),
                    "redirect_uris": uris, "token_endpoint_auth_method": auth_method}
        if secret:
            response["client_secret"] = secret
        return response

    async def authorize(self, request: Request):
        """Step 1 of the OAuth 2.1 authorization code flow (with PKCE).

        The admin passphrase MUST be supplied via the POST body — never the
        query string. Query strings are logged by access logs, reverse
        proxies (ngrok, Cloudflare), and browser history, so accepting the
        passphrase there would leak it to anyone who can read those logs.

        GET renders the approval form (no passphrase required to render the
        page). The browser submits the form via POST with the passphrase in
        the body. ``authorize_post`` is the only entry that consumes the
        passphrase and grants a code.
        """
        q = request.query_params
        # Defense in depth: refuse any GET that carries a passphrase in the
        # URL, even by accident. Real clients only POST it.
        if "passphrase" in q:
            return JSONResponse(
                {"error": "invalid_request",
                 "error_description": "passphrase must be sent in the POST body, not the query string"},
                status_code=400,
            )
        client_id, redirect_uri = q.get("client_id", ""), q.get("redirect_uri", "")
        client = await self.store.get_client(client_id)
        challenge = q.get("code_challenge", "")
        if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not challenge:
            return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 is required"}, status_code=400)
        if not client or redirect_uri not in client["redirect_uris"]:
            return JSONResponse({"error": "invalid_request", "error_description": "unknown client or redirect_uri"}, status_code=400)
        # Render the approval page; the browser submits via POST.
        return HTMLResponse(
            "<h1>Authorize MNEMOS MCP</h1><form method='post'>"
            "<label>Admin passphrase <input name='passphrase' type='password' autofocus></label>"
            + "".join(f"<input type='hidden' name='{_html(k)}' value='{_html(v)}'>" for k, v in q.multi_items())
            + "<button type='submit'>Approve</button></form>"
        )

    async def authorize_post(self, request: Request):
        """Step 2 of the flow: validate the form-posted passphrase and
        issue an authorization code. Passphrase comes from the POST body
        only; a query-string passphrase is rejected by ``authorize`` so
        it can never reach this code path via the GET route."""
        form = await request.form()
        # ``request.form()`` returns a Starlette ``FormData`` (multi-dict
        # wrapper) with an ``.items()`` method.  Keep passphrase in
        # memory only; never put it in logs or redirect URLs.
        data = {str(k): str(v) for k, v in form.items()}
        supplied = data.pop("passphrase", None)
        # Defense in depth: if the form also carries a passphrase query
        # parameter (it shouldn't), reject.  This catches any path that
        # bypasses the GET guard above.
        if "passphrase" in request.query_params:
            return JSONResponse(
                {"error": "invalid_request",
                 "error_description": "passphrase must be sent in the POST body, not the query string"},
                status_code=400,
            )
        if supplied is None or not hmac.compare_digest(str(supplied), self.admin_passphrase):
            return HTMLResponse("authorization denied", status_code=403)
        client_id = data.get("client_id", "")
        redirect_uri = data.get("redirect_uri", "")
        challenge = data.get("code_challenge", "")
        if data.get("response_type") != "code" \
                or data.get("code_challenge_method") != "S256" or not challenge:
            return JSONResponse(
                {"error": "invalid_request",
                 "error_description": "PKCE S256 is required"},
                status_code=400,
            )
        client = await self.store.get_client(client_id)
        if not client or redirect_uri not in client["redirect_uris"]:
            return JSONResponse(
                {"error": "invalid_request",
                 "error_description": "unknown client or redirect_uri"},
                status_code=400,
            )
        request2 = type("QueryRequest", (), {"query_params": data})()
        return await self._grant_code(request2.query_params, client)

    async def _grant_code(self, q: Any, client: dict[str, Any]):
        code = secrets.token_urlsafe(32)
        await self.store.save_code({"code": code, "client_id": client["client_id"],
            "code_challenge": q.get("code_challenge"), "code_challenge_method": "S256",
            "redirect_uri": q.get("redirect_uri"), "expires_at": _now() + timedelta(seconds=CODE_SECONDS),
            "used_at": None})
        params = {"code": code}
        if q.get("state") is not None:
            params["state"] = q.get("state")
        return RedirectResponse(q.get("redirect_uri") + "?" + urlencode(params), status_code=303)

    async def token(self, form: dict[str, str]) -> JSONResponse:
        grant = form.get("grant_type")
        client_id = form.get("client_id", "")
        client = await self.store.get_client(client_id)
        if not client:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if client.get("token_endpoint_auth_method") == "client_secret_post" and not hmac.compare_digest(form.get("client_secret", ""), client.get("client_secret", "")):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if grant == "authorization_code":
            row = await self.store.consume_code(form.get("code", ""))
            verifier = form.get("code_verifier", "")
            if not row or row["client_id"] != client_id or row["redirect_uri"] != form.get("redirect_uri") or not verifier:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            try:
                valid = hmac.compare_digest(pkce_s256(verifier), row["code_challenge"])
            except (UnicodeEncodeError, ValueError):
                valid = False
            if not valid:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return await self._tokens(client_id)
        if grant == "refresh_token":
            row = await self.store.get_refresh(hash_refresh_token(form.get("refresh_token", "")))
            if not row or row["expires_at"] <= _now() or row.get("revoked_at") is not None or row["client_id"] != client_id:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            await self.store.revoke_token(row["jti"])
            return await self._tokens(client_id)
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def _tokens(self, client_id: str) -> JSONResponse:
        access = self.issue_access_token(client_id=client_id)
        refresh = secrets.token_urlsafe(48)
        expires = _now() + timedelta(seconds=REFRESH_TOKEN_SECONDS)
        await self.store.save_token({"jti": secrets.token_urlsafe(24), "refresh_token_hash": hash_refresh_token(refresh),
                                     "client_id": client_id, "expires_at": expires, "revoked_at": None})
        return JSONResponse({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TOKEN_SECONDS,
                             "refresh_token": refresh, "scope": "mcp"})


def _html(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#x27;"))


_service: OAuthService | None = None


def set_oauth_service(service: OAuthService | None) -> None:
    global _service
    _service = service


def get_oauth_service() -> OAuthService:
    """Return the process-wide OAuth service singleton.

    The first caller resolves the signing key from, in priority order:

      1. ``MNEMOS_OAUTH_SIGNING_KEY`` env / config (explicit operator override).
      2. The persistent store (only ``PostgresOAuthStore`` exposes
         ``get_signing_key``); on a cold start against Postgres with an
         empty ``oauth_mcp_signing_keys`` table, the startup path
         generates a fresh 32-byte key and persists it.
      3. Otherwise a missing signing key is a hard error — we will not
         silently fall back to a generated ephemeral key, because that
         would invalidate every previously-issued refresh token on the
         next restart.

    The store passed in is whatever the caller installed via
    ``set_oauth_service()`` at startup; if no service has been installed
    yet, an in-memory store is used so import-time callers and the
    bare ``mcp_http_app`` test fixture continue to work without
    Postgres. Production deploys that want persistence set
    ``MNEMOS_OAUTH_DATABASE_URL``; ``mcp/http.py``'s lifespan then
    builds an asyncpg pool, a ``PostgresOAuthStore``, and calls
    ``set_oauth_service`` before any request lands.
    """
    global _service
    if _service is None:
        from mnemos.core.config import get_settings  # local to keep this module import-light

        settings = get_settings()
        key = settings.oauth.signing_key
        if not key:
            raise RuntimeError(
                "MNEMOS_OAUTH_SIGNING_KEY is not configured and no service "
                "with a persistent signing key has been installed via "
                "set_oauth_service(). Set MNEMOS_OAUTH_SIGNING_KEY, or "
                "start the server with MNEMOS_OAUTH_DATABASE_URL so the "
                "Postgres-backed store can provide it."
            )
        _service = OAuthService(base_url=(settings.oauth.issuer or settings.server.base), signing_key=key,
            store=InMemoryOAuthStore(), registration_secret=settings.oauth.registration_secret,
            admin_passphrase=settings.oauth.admin_passphrase)
    return _service


async def metadata_authorization(_request: Request):
    service = get_oauth_service()
    return JSONResponse({"issuer": service.base_url, "authorization_endpoint": service.base_url.rstrip("/") + "/oauth/authorize",
        "token_endpoint": service.base_url.rstrip("/") + "/oauth/token", "registration_endpoint": service.base_url.rstrip("/") + "/oauth/register",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"], "scopes_supported": ["mcp"],
    })


async def metadata_resource(_request: Request):
    service = get_oauth_service()
    return JSONResponse({"resource": service.base_url.rstrip("/"), "authorization_servers": [service.base_url],
                         "bearer_methods_supported": ["header"], "scopes_supported": ["mcp"]})


async def register_route(request: Request):
    service = get_oauth_service()
    presented = request.headers.get("x-mnemos-oauth-registration-secret", "")
    if not service.registration_secret or not hmac.compare_digest(presented, service.registration_secret):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return JSONResponse(await service.register(await request.json()), status_code=201)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": str(exc)}, status_code=400)


async def authorize_route(request: Request):
    return await get_oauth_service().authorize(request)


async def authorize_post_route(request: Request):
    return await get_oauth_service().authorize_post(request)


async def token_route(request: Request):
    try:
        form = {str(k): str(v) for k, v in (await request.form()).items()}
        return await get_oauth_service().token(form)
    except RuntimeError:
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
