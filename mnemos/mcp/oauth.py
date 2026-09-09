"""Provider-neutral OAuth 2.1 authorization server for the remote MCP edge.

This module deliberately contains no provider-specific branches.  ``provider``
is an audit claim only; authorization is based on the registered client and
PKCE-bound grant.  The production store is PostgreSQL, while the small
in-memory store is useful for isolated tests and explicit development use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from math import ceil
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

ISSUER_PATH = "/"
ACCESS_TOKEN_SECONDS = 30 * 60
REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
CODE_SECONDS = 5 * 60
ADMIN_SUBJECT = "default"

# Dynamic client registration is deliberately unauthenticated (see
# register_route). /oauth/ is also exempt from BearerAuthMiddleware in
# mcp/http.py, and the standalone MCP Starlette app carries no body-size
# middleware, so these bounds are the ONLY backpressure between an
# unauthenticated caller and unbounded oauth_mcp_clients growth / request
# memory. Open registration is a spec requirement; unbounded is not.
MAX_REGISTRATION_BODY_BYTES = 8 * 1024
MAX_REDIRECT_URIS = 20
MAX_REDIRECT_URI_LENGTH = 2048
REGISTRATION_RATE_LIMIT = 30
REGISTRATION_RATE_WINDOW_SECONDS = 60.0
AUTH_ATTEMPT_RATE_LIMIT = 10
AUTH_ATTEMPT_GLOBAL_RATE_LIMIT = 100
AUTH_ATTEMPT_RATE_WINDOW_SECONDS = 60.0
AUTH_ATTEMPT_BACKOFF_MAX_SECONDS = 60.0
RATE_LIMIT_MAX_CALLERS = 4096
MIN_SIGNING_KEY_BYTES = 32

_OBVIOUS_SIGNING_KEY_PLACEHOLDERS = {
    "admin",
    "changeme",
    "change-me",
    "replace-me",
    "your-signing-key",
    "your-secret-key",
    "secret",
    "password",
    "default",
    "test",
    "example",
    "this-is-a-placeholder-signing-key-do-not-use",
}


def _shortest_repeating_unit(value: str) -> str:
    """Collapse a periodic string to its shortest repeating substring."""
    if len(value) < 2:
        return value
    period = (value + value).find(value, 1)
    return value[:period] if period < len(value) else value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_s256(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _SlidingWindowRateLimiter:
    """Bounded monotonic sliding-window limiter with optional backoff."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        max_callers: int = RATE_LIMIT_MAX_CALLERS,
        backoff_max_seconds: float = 0.0,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_callers = max_callers
        self.backoff_max_seconds = backoff_max_seconds
        self._hits: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._violations: dict[str, int] = {}
        self._last_seen: dict[str, float] = {}

    def check(self, caller: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)`` for *caller*."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self._prune(cutoff, now)
        has_room, capacity_retry_after = self._make_room(caller, now)
        if not has_room:
            return False, capacity_retry_after
        self._last_seen[caller] = now

        blocked_until = self._blocked_until.get(caller, 0.0)
        if blocked_until > now:
            violations = self._violations.get(caller, 0) + 1
            self._violations[caller] = violations
            delay = min(2 ** max(0, violations - 1), self.backoff_max_seconds)
            if delay:
                blocked_until = max(blocked_until, now + delay)
                self._blocked_until[caller] = blocked_until
            return False, max(1, ceil(blocked_until - now))

        hits = self._hits.setdefault(caller, [])
        if len(hits) >= self.limit:
            if self.backoff_max_seconds:
                violations = self._violations.get(caller, 0) + 1
                self._violations[caller] = violations
                delay = min(2 ** max(0, violations - 1), self.backoff_max_seconds)
                self._blocked_until[caller] = now + delay
                return False, max(1, ceil(delay))
            retry_at = hits[0] + self.window_seconds
            return False, max(1, ceil(retry_at - now))

        hits.append(now)
        return True, 0

    def _prune(self, cutoff: float, now: float) -> None:
        for caller in list(self._last_seen):
            recent = [stamp for stamp in self._hits.get(caller, ()) if stamp > cutoff]
            if recent:
                self._hits[caller] = recent
            else:
                self._hits.pop(caller, None)
            if self._blocked_until.get(caller, 0.0) <= now:
                self._blocked_until.pop(caller, None)
            if caller not in self._hits and caller not in self._blocked_until:
                self._violations.pop(caller, None)
                self._last_seen.pop(caller, None)

    def _make_room(self, caller: str, now: float) -> tuple[bool, int]:
        if caller in self._last_seen or len(self._last_seen) < self.max_callers:
            return True, 0
        evictable = [
            known_caller
            for known_caller in self._last_seen
            if self._blocked_until.get(known_caller, 0.0) <= now
        ]
        if not evictable:
            retry_at = min(self._blocked_until.values())
            return False, max(1, ceil(retry_at - now))
        oldest = min(evictable, key=self._last_seen.__getitem__)
        self._hits.pop(oldest, None)
        self._blocked_until.pop(oldest, None)
        self._violations.pop(oldest, None)
        self._last_seen.pop(oldest, None)
        return True, 0


def _caller_key(request: Request, *, fallback: str = "unknown") -> str:
    client = getattr(request, "client", None)
    if client and client.host:
        return f"ip:{client.host}"
    return fallback


def _rate_limited_response(retry_after: int) -> JSONResponse:
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": "too many authorization attempts"},
        status_code=429,
        headers={"Retry-After": str(max(1, retry_after))},
    )


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

    async def rotate_refresh(self, token_hash: str, client_id: str, successor: dict[str, Any]) -> str:
        """Atomically rotate a refresh token and invalidate its family on replay."""
        for row in self.tokens.values():
            if row["refresh_token_hash"] != token_hash or row["client_id"] != client_id:
                continue
            if row.get("revoked_at") is not None:
                family_id = row["family_id"]
                now = _now()
                for member in self.tokens.values():
                    if member["family_id"] == family_id and member.get("revoked_at") is None:
                        member["revoked_at"] = now
                return "reused"
            if row["expires_at"] <= _now():
                return "invalid"
            row["revoked_at"] = _now()
            row["replaced_by_jti"] = successor["jti"]
            successor["family_id"] = row["family_id"]
            successor["parent_jti"] = row["jti"]
            self.tokens[successor["jti"]] = successor
            return "rotated"
        return "invalid"


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
                row["client_id"],
                row.get("client_secret"),
                redirect_uris,
                row["token_endpoint_auth_method"],
            )

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT client_id, client_secret, redirect_uris,
                          token_endpoint_auth_method
                   FROM oauth_mcp_clients WHERE client_id = $1""",
                client_id,
            )
            if not row:
                return None
            client = dict(row)
            # asyncpg's default json/jsonb codec returns text. Decode it here
            # so authorization performs exact list membership, not substring
            # membership against a serialized JSON string.
            redirect_uris = client.get("redirect_uris")
            if isinstance(redirect_uris, str):
                redirect_uris = json.loads(redirect_uris)
            if not isinstance(redirect_uris, list) or not all(isinstance(uri, str) for uri in redirect_uris):
                raise ValueError("stored OAuth client redirect_uris is invalid")
            client["redirect_uris"] = redirect_uris
            return client

    async def save_code(self, row: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO oauth_mcp_authorization_codes
                   (code, client_id, code_challenge, code_challenge_method,
                    redirect_uri, expires_at)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                row["code"],
                row["client_id"],
                row["code_challenge"],
                row["code_challenge_method"],
                row["redirect_uri"],
                row["expires_at"],
            )

    async def consume_code(self, code: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE oauth_mcp_authorization_codes
                   SET used_at = NOW()
                   WHERE code = $1 AND used_at IS NULL AND expires_at > NOW()
                   RETURNING code, client_id, code_challenge, code_challenge_method,
                             redirect_uri, expires_at""",
                code,
            )
            return dict(row) if row else None

    async def save_token(self, row: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO oauth_mcp_tokens
                   (jti, refresh_token_hash, client_id, family_id, parent_jti,
                    expires_at)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                row["jti"],
                row["refresh_token_hash"],
                row["client_id"],
                row["family_id"],
                row.get("parent_jti"),
                row["expires_at"],
            )

    async def rotate_refresh(self, token_hash: str, client_id: str, successor: dict[str, Any]) -> str:
        """Consume and replace a refresh token in one transaction.

        A second use of an already-rotated token is evidence of replay. In that
        case every still-active refresh token in the family is revoked, including
        the successor minted by the first request.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                presented = await conn.fetchrow(
                    """SELECT jti, family_id
                       FROM oauth_mcp_tokens
                       WHERE refresh_token_hash = $1 AND client_id = $2""",
                    token_hash,
                    client_id,
                )
                if not presented:
                    return "invalid"

                # Serialize every member on the immutable root row. Locking
                # only the presented token is insufficient: stale-ancestor
                # replay could otherwise race current-token rotation and miss
                # its newly inserted successor at READ COMMITTED isolation.
                root = await conn.fetchrow(
                    """SELECT jti FROM oauth_mcp_tokens
                       WHERE jti = $1 FOR UPDATE""",
                    presented["family_id"],
                )
                if not root:
                    return "invalid"

                current = await conn.fetchrow(
                    """UPDATE oauth_mcp_tokens
                       SET revoked_at = NOW()
                       WHERE refresh_token_hash = $1 AND client_id = $2
                         AND revoked_at IS NULL AND expires_at > NOW()
                       RETURNING jti, family_id""",
                    token_hash,
                    client_id,
                )
                if current:
                    successor["family_id"] = current["family_id"]
                    successor["parent_jti"] = current["jti"]
                    await conn.execute(
                        """INSERT INTO oauth_mcp_tokens
                           (jti, refresh_token_hash, client_id, family_id,
                            parent_jti, expires_at)
                           VALUES ($1, $2, $3, $4, $5, $6)""",
                        successor["jti"],
                        successor["refresh_token_hash"],
                        successor["client_id"],
                        successor["family_id"],
                        successor["parent_jti"],
                        successor["expires_at"],
                    )
                    await conn.execute(
                        """UPDATE oauth_mcp_tokens SET replaced_by_jti = $1
                           WHERE jti = $2""",
                        successor["jti"],
                        current["jti"],
                    )
                    return "rotated"

                replay = await conn.fetchrow(
                    """SELECT family_id, revoked_at, expires_at
                       FROM oauth_mcp_tokens
                       WHERE refresh_token_hash = $1 AND client_id = $2""",
                    token_hash,
                    client_id,
                )
                if replay and replay["revoked_at"] is not None:
                    await conn.execute(
                        """UPDATE oauth_mcp_tokens SET revoked_at = COALESCE(revoked_at, NOW())
                           WHERE family_id = $1""",
                        replay["family_id"],
                    )
                    return "reused"
                return "invalid"

    async def save_signing_key(self, *, key_id: str, signing_key: str) -> None:
        """Persist a signing key. Idempotent on (key_id); the first
        writer wins via ``ON CONFLICT DO NOTHING`` so concurrent
        first-boots don't clobber each other's key."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO oauth_mcp_signing_keys (key_id, signing_key)
                   VALUES ($1, $2)
                   ON CONFLICT (key_id) DO NOTHING""",
                key_id,
                signing_key,
            )


class OAuthService:
    def __init__(
        self, *, base_url: str, signing_key: str, store: Any, registration_secret: str, admin_passphrase: str
    ) -> None:
        if not isinstance(signing_key, str):
            raise ValueError("OAuth signing key must be a string")
        if not signing_key or signing_key != signing_key.strip():
            raise ValueError("OAuth signing key must not be empty or padded with whitespace")
        if len(signing_key.encode("utf-8")) < MIN_SIGNING_KEY_BYTES:
            raise ValueError(f"OAuth signing key must be at least {MIN_SIGNING_KEY_BYTES} bytes for HS256")
        normalized_key = signing_key.casefold()
        collapsed_key = _shortest_repeating_unit(normalized_key)
        if collapsed_key != normalized_key or collapsed_key in _OBVIOUS_SIGNING_KEY_PLACEHOLDERS:
            raise ValueError("OAuth signing key must not be periodic or an obvious placeholder")
        # Fail-closed: an empty/None passphrase would let `passphrase=`
        # pass hmac.compare_digest (empty vs empty) and None would raise.
        if not admin_passphrase:
            raise ValueError(
                "OAuth admin passphrase must be a non-empty string; configure MNEMOS_OAUTH_ADMIN_PASSPHRASE."
            )
        self.base_url = base_url.rstrip("/") + "/"
        self.signing_key = signing_key
        self.store = store
        self.registration_secret = registration_secret
        self.admin_passphrase = admin_passphrase
        self._registration_limiter = _SlidingWindowRateLimiter(
            limit=REGISTRATION_RATE_LIMIT,
            window_seconds=REGISTRATION_RATE_WINDOW_SECONDS,
        )
        # One shared bucket covers both passphrase authorization and token
        # exchange, preventing attackers from alternating endpoints.
        self._authorization_limiter = _SlidingWindowRateLimiter(
            limit=AUTH_ATTEMPT_RATE_LIMIT,
            window_seconds=AUTH_ATTEMPT_RATE_WINDOW_SECONDS,
            backoff_max_seconds=AUTH_ATTEMPT_BACKOFF_MAX_SECONDS,
        )
        # The administrator passphrase is one system-wide credential, so a
        # second bucket also caps attempts spread across many caller identities.
        self._authorization_global_limiter = _SlidingWindowRateLimiter(
            limit=AUTH_ATTEMPT_GLOBAL_RATE_LIMIT,
            window_seconds=AUTH_ATTEMPT_RATE_WINDOW_SECONDS,
            max_callers=1,
        )

    def check_registration_rate_limit(self, caller: str) -> bool:
        """Sliding-window limiter for the unauthenticated DCR endpoint.

        Returns True when the caller may register. Windows that have fully
        aged out are dropped on every call so the bookkeeping map cannot
        itself become the unbounded structure it is guarding against.
        """
        allowed, _retry_after = self._registration_limiter.check(caller)
        return allowed

    def check_authorization_rate_limit(self, caller: str) -> tuple[bool, int]:
        """Throttle authorize and token attempts globally and per caller."""
        globally_allowed, retry_after = self._authorization_global_limiter.check("admin-auth")
        if not globally_allowed:
            return False, retry_after
        return self._authorization_limiter.check(caller)

    def issue_access_token(
        self,
        *,
        client_id: str,
        provider: str = "unknown",
        now: datetime | None = None,
        lifetime: int = ACCESS_TOKEN_SECONDS,
    ) -> str:
        current = now or _now()
        claims = {
            "sub": ADMIN_SUBJECT,
            "aud": "mnemos-mcp",
            "iss": self.base_url,
            "client_id": client_id,
            "provider": provider,
            "scope": "mcp",
            "jti": secrets.token_urlsafe(24),
            "iat": current,
            "exp": current + timedelta(seconds=lifetime),
        }
        return jwt.encode(claims, self.signing_key, algorithm="HS256")

    def validate_access_token(self, token: str) -> dict[str, Any]:
        # PyJWT verifies the HS256 signature and exp claim here; do not use
        # decode(..., verify_signature=False), even for metadata/audit.
        claims = jwt.decode(
            token,
            self.signing_key,
            algorithms=["HS256"],
            audience="mnemos-mcp",
            issuer=self.base_url,
            options={"require": ["sub", "aud", "iss", "client_id", "jti", "exp"]},
        )
        if claims["sub"] != ADMIN_SUBJECT or claims.get("scope") != "mcp":
            raise jwt.InvalidTokenError("invalid MCP subject or scope")
        return claims

    async def register(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise TypeError("client metadata must be a JSON object")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
            raise ValueError("redirect_uris must be a non-empty array")
        if len(uris) > MAX_REDIRECT_URIS:
            raise ValueError(f"redirect_uris must contain at most {MAX_REDIRECT_URIS} entries")
        if any(len(u) > MAX_REDIRECT_URI_LENGTH for u in uris):
            raise ValueError(f"each redirect_uri must be at most {MAX_REDIRECT_URI_LENGTH} characters")
        for uri in uris:
            parts = urlsplit(uri)
            if parts.fragment or not parts.scheme:
                raise ValueError("redirect_uris must be absolute and must not contain fragments")
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("redirect_uris must use HTTP or HTTPS and include a host")
            if parts.scheme == "http" and parts.hostname not in {
                "127.0.0.1",
                "::1",
                "localhost",
            }:
                raise ValueError("HTTP redirect_uris are allowed only for loopback clients")
            if parts.username is not None or parts.password is not None:
                raise ValueError("redirect_uris must not contain user information")
        auth_method = body.get("token_endpoint_auth_method", "none")
        if auth_method not in {"none", "client_secret_post", "client_secret_basic"}:
            raise ValueError("unsupported token_endpoint_auth_method")
        client_id = "mnemos_" + secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32) if auth_method != "none" else None
        await self.store.save_client(
            {
                "client_id": client_id,
                "client_secret": secret,
                "redirect_uris": uris,
                "token_endpoint_auth_method": auth_method,
            }
        )
        response = {
            "client_id": client_id,
            "client_id_issued_at": int(_now().timestamp()),
            "redirect_uris": uris,
            "token_endpoint_auth_method": auth_method,
        }
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
                {
                    "error": "invalid_request",
                    "error_description": "passphrase must be sent in the POST body, not the query string",
                },
                status_code=400,
            )
        client_id, redirect_uri = q.get("client_id", ""), q.get("redirect_uri", "")
        client = await self.store.get_client(client_id)
        challenge = q.get("code_challenge", "")
        if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not challenge:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "PKCE S256 is required"}, status_code=400
            )
        if not client or redirect_uri not in client["redirect_uris"]:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "unknown client or redirect_uri"}, status_code=400
            )
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
        allowed, retry_after = self.check_authorization_rate_limit(_caller_key(request))
        if not allowed:
            return _rate_limited_response(retry_after)
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
                {
                    "error": "invalid_request",
                    "error_description": "passphrase must be sent in the POST body, not the query string",
                },
                status_code=400,
            )
        if supplied is None or not hmac.compare_digest(str(supplied), self.admin_passphrase):
            return HTMLResponse("authorization denied", status_code=403)
        client_id = data.get("client_id", "")
        redirect_uri = data.get("redirect_uri", "")
        challenge = data.get("code_challenge", "")
        if data.get("response_type") != "code" or data.get("code_challenge_method") != "S256" or not challenge:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "PKCE S256 is required"},
                status_code=400,
            )
        client = await self.store.get_client(client_id)
        if not client or redirect_uri not in client["redirect_uris"]:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "unknown client or redirect_uri"},
                status_code=400,
            )
        request2 = type("QueryRequest", (), {"query_params": data})()
        return await self._grant_code(request2.query_params, client)

    async def _grant_code(self, q: Any, client: dict[str, Any]):
        code = secrets.token_urlsafe(32)
        await self.store.save_code(
            {
                "code": code,
                "client_id": client["client_id"],
                "code_challenge": q.get("code_challenge"),
                "code_challenge_method": "S256",
                "redirect_uri": q.get("redirect_uri"),
                "expires_at": _now() + timedelta(seconds=CODE_SECONDS),
                "used_at": None,
            }
        )
        params = {"code": code}
        if q.get("state") is not None:
            params["state"] = q.get("state")
        redirect_uri = q.get("redirect_uri")
        parts = urlsplit(redirect_uri)
        encoded = urlencode(params)
        query = f"{parts.query}&{encoded}" if parts.query else encoded
        target = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
        return RedirectResponse(target, status_code=303)

    async def token(self, form: dict[str, str], *, caller: str | None = None) -> JSONResponse:
        caller_key = caller or f"client:{form.get('client_id', '') or 'unknown'}"
        allowed, retry_after = self.check_authorization_rate_limit(caller_key)
        if not allowed:
            return _rate_limited_response(retry_after)
        grant = form.get("grant_type")
        client_id = form.get("client_id", "")
        client = await self.store.get_client(client_id)
        if not client:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        auth_method = client.get("token_endpoint_auth_method")
        if auth_method in ("client_secret_post", "client_secret_basic") and not hmac.compare_digest(
            form.get("client_secret", ""), client.get("client_secret", "")
        ):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if grant == "authorization_code":
            row = await self.store.consume_code(form.get("code", ""))
            verifier = form.get("code_verifier", "")
            if (
                not row
                or row["client_id"] != client_id
                or row["redirect_uri"] != form.get("redirect_uri")
                or not verifier
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            try:
                valid = hmac.compare_digest(pkce_s256(verifier), row["code_challenge"])
            except (UnicodeEncodeError, ValueError):
                valid = False
            if not valid:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return await self._tokens(client_id)
        if grant == "refresh_token":
            refresh, successor = self._new_refresh_token(client_id)
            result = await self.store.rotate_refresh(
                hash_refresh_token(form.get("refresh_token", "")),
                client_id,
                successor,
            )
            if result != "rotated":
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return self._token_response(client_id, refresh)
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def _tokens(self, client_id: str) -> JSONResponse:
        refresh, row = self._new_refresh_token(client_id)
        row["family_id"] = row["jti"]
        await self.store.save_token(row)
        return self._token_response(client_id, refresh)

    def _new_refresh_token(self, client_id: str) -> tuple[str, dict[str, Any]]:
        refresh = secrets.token_urlsafe(48)
        return refresh, {
            "jti": secrets.token_urlsafe(24),
            "refresh_token_hash": hash_refresh_token(refresh),
            "client_id": client_id,
            "expires_at": _now() + timedelta(seconds=REFRESH_TOKEN_SECONDS),
            "revoked_at": None,
            "family_id": None,
            "parent_jti": None,
            "replaced_by_jti": None,
        }

    def _token_response(self, client_id: str, refresh: str) -> JSONResponse:
        access = self.issue_access_token(client_id=client_id)
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_SECONDS,
                "refresh_token": refresh,
                "scope": "mcp",
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )


def _html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


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
        issuer = settings.oauth.issuer.strip()
        if not issuer:
            raise RuntimeError("MNEMOS_OAUTH_ISSUER is not configured")
        if not key:
            raise RuntimeError(
                "MNEMOS_OAUTH_SIGNING_KEY is not configured and no service "
                "with a persistent signing key has been installed via "
                "set_oauth_service(). Set MNEMOS_OAUTH_SIGNING_KEY, or "
                "start the server with MNEMOS_OAUTH_DATABASE_URL so the "
                "Postgres-backed store can provide it."
            )
        _service = OAuthService(
            base_url=issuer,
            signing_key=key,
            store=InMemoryOAuthStore(),
            registration_secret=settings.oauth.registration_secret,
            admin_passphrase=settings.oauth.admin_passphrase,
        )
    return _service


async def metadata_authorization(_request: Request):
    service = get_oauth_service()
    return JSONResponse(
        {
            "issuer": service.base_url,
            "authorization_endpoint": service.base_url.rstrip("/") + "/oauth/authorize",
            "token_endpoint": service.base_url.rstrip("/") + "/oauth/token",
            "registration_endpoint": service.base_url.rstrip("/") + "/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp"],
        }
    )


async def metadata_resource(_request: Request):
    service = get_oauth_service()
    return JSONResponse(
        {
            "resource": service.base_url.rstrip("/"),
            "authorization_servers": [service.base_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
        }
    )


async def register_route(request: Request):
    # RFC 7591 dynamic client registration is intentionally OPEN: a client_id
    # by itself grants no access to any tool or memory. The real security
    # boundary is /oauth/authorize (passphrase-gated). Standard OAuth clients
    # (ChatGPT included) call this endpoint automatically with no way to
    # supply an out-of-band secret, so gating registration itself breaks
    # every spec-compliant client's auto-discovery flow.
    #
    # Open, however, is not the same as unbounded. Nothing else rate-limits or
    # size-limits this endpoint, and every accepted registration is persisted
    # forever, so the body/cardinality/rate bounds below are what keep an
    # unauthenticated caller from exhausting request memory or growing
    # oauth_mcp_clients without limit.
    service = get_oauth_service()
    caller = request.client.host if request.client else "unknown"
    if not service.check_registration_rate_limit(caller):
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "registration rate limit exceeded"},
            status_code=429,
        )
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = -1
        if declared_size < 0:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": "invalid Content-Length"},
                status_code=400,
            )
        if declared_size > MAX_REGISTRATION_BODY_BYTES:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": "client metadata exceeds the maximum size"},
                status_code=413,
            )
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_REGISTRATION_BODY_BYTES:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": "client metadata exceeds the maximum size"},
                status_code=413,
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": str(exc)},
            status_code=400,
        )
    try:
        return JSONResponse(await service.register(payload), status_code=201)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": str(exc)}, status_code=400)


async def authorize_route(request: Request):
    return await get_oauth_service().authorize(request)


async def authorize_post_route(request: Request):
    return await get_oauth_service().authorize_post(request)


async def token_route(request: Request):
    try:
        form = {str(k): str(v) for k, v in (await request.form()).items()}
        # RFC 6749 5.2.1: client_secret_basic presents credentials via the
        # Authorization header, not the form body -- decode it if present;
        # it takes precedence over any form-supplied client_id/secret.
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("basic "):
            import base64

            try:
                decoded = base64.b64decode(authz[6:].strip()).decode("utf-8")
                basic_id, _, basic_secret = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            form["client_id"] = basic_id
            form["client_secret"] = basic_secret
        caller = _caller_key(request, fallback=f"client:{form.get('client_id', '') or 'unknown'}")
        return await get_oauth_service().token(form, caller=caller)
    except RuntimeError:
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
