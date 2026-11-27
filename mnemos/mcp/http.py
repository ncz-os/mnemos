#!/usr/bin/env python3
"""MNEMOS MCP HTTP/SSE server — for ChatGPT Pro Developer Mode + any
remote MCP client that needs an HTTPS URL instead of a stdio process.

Reuses the same `Server("mnemos")` instance as mcp_server.py. Tool
definitions come from mnemos/mcp/tools's canonical registry; the only
difference is the transport: stdio framing vs SSE-over-HTTP framing.
Tool surface is identical, so a memory written from Claude Desktop
(stdio) is queryable from ChatGPT Pro (SSE) and vice versa.

Auth: bearer token, when enabled. MNEMOS_MCP_AUTH_ENABLED switches it
explicitly; unset, it follows the REST auth setting (MNEMOS_AUTH_ENABLED,
else the profile default: ``server`` on, ``edge``/``personal``/``dev``
off). When enabled, the connector caller MUST send
  Authorization: Bearer <token>
on the SSE handshake. Prefer per-user token issuance with
MNEMOS_MCP_TOKENS=user:api_key[,user:api_key]. Legacy
MNEMOS_MCP_TOKEN remains supported for single-user deployments but
logs a warning because every client shares the same backend identity.
When disabled, no token is required or read and every caller is served
as the legacy single-token principal (trusted networks only).

Transport security: TLS terminated upstream (Cloudflare Tunnel,
Tailscale Funnel, Caddy/nginx). This process listens on a local
HTTP port; the public URL is opaque to it.

Run:
  MNEMOS_MCP_TOKENS=user1:<mnemos-api-key-1>,user2:<mnemos-api-key-2>  \
  MNEMOS_BASE=http://localhost:5002  \
  python3 mcp_http_server.py --host 127.0.0.1 --port 5004
"""

from __future__ import annotations
import httpx

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import sys
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qs, quote
from uuid import UUID, uuid4

import uvicorn
import jwt
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

try:
    from starlette.responses import StreamingResponse
except ImportError:  # pragma: no cover - exercised only by lightweight test stubs.
    StreamingResponse = None  # type: ignore[assignment]
from starlette.routing import BaseRoute, Match, Mount, NoMatchFound, Route

from mnemos.core.config import (
    get_settings,
    mcp_max_initial_body_bytes_env,
    mcp_max_principal_sessions_env,
    mcp_max_sessions_env,
    mcp_nats_raw_enabled,
    mcp_session_idle_seconds_env,
    mcp_session_startup_seconds_env,
)
from mnemos.core.roles import OPERATOR_ROLES
from mnemos.core.visibility import ACL_READ_BIT as READ_BIT
from mnemos.mcp.oauth import (
    authorize_post_route,
    authorize_route,
    get_oauth_service,
    metadata_authorization,
    metadata_resource,
    register_route,
    token_route,
)

# Reuse the exact same Server instance + tool registrations from
# the stdio entry point. Importing for the side effect of having
# tools registered against `app`.
from mnemos.mcp.stdio import app  # noqa: F401 (used by handle_sse below)
from mnemos.mcp.tools import (
    TOOL_REGISTRY,
    reset_mcp_backend_context,
    set_mcp_backend_context,
)
from mnemos.persistence.base import OAuthPersistence
from mnemos.persistence.nats_events import safe_subject_segment
from mnemos.persistence.visibility import VisibilityScope

# stderr logging — matches mcp_server.py convention so log shipping
# from container stdout/stderr stays consistent.
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [%(levelname)s] mcp_http: %(message)s")
logger = logging.getLogger(__name__)


HTTP_TOOL_REGISTRY = TOOL_REGISTRY
# #181: removed `DEFAULT_NATS_SSE_SUBJECT = "mnemos.*.*.default"` —
# defined but never referenced. SSE subject defaults are derived
# per-principal from auth context, not a global string constant.
NATS_SSE_PATH = "/mcp/events/stream"
NATS_SSE_QUEUE_MAXSIZE = 256
NATS_SSE_MAX_CONSECUTIVE_DROPS = 1000


@dataclass(frozen=True)
class MCPClientPrincipal:
    user_id: str | None
    api_key: str | None


@dataclass(frozen=True)
class MCPUserContext:
    user_id: str | None
    role: str
    namespace: str


MCP_AUTH_DISABLED_EXPLICIT_WARNING = (
    "MCP HTTP/SSE auth DISABLED by MNEMOS_MCP_AUTH_ENABLED=false (trusted network only)"
)


def _mcp_auth_enabled() -> bool:
    return bool(get_settings().mcp.auth_enabled)


def _mcp_auth_disabled_warning() -> str:
    settings = get_settings()
    if "auth_enabled" in settings.explicit_fields("mcp"):
        return MCP_AUTH_DISABLED_EXPLICIT_WARNING
    return (
        "MCP HTTP/SSE auth DISABLED by default "
        f"(profile={settings.profile}, REST auth off; "
        "set MNEMOS_MCP_AUTH_ENABLED=true to require tokens) (trusted network only)"
    )


def _trusted_network_principal() -> MCPClientPrincipal:
    """The principal every caller gets when MCP bearer auth is disabled.

    Identical to the legacy single MNEMOS_MCP_TOKEN principal: no MCP user,
    the backend MNEMOS_API_KEY (if any) as the identity, so namespace and
    role resolve exactly as they do in legacy single-token mode.
    """
    return MCPClientPrincipal(
        user_id=None,
        api_key=get_settings().server.api_key.strip() or None,
    )


def _fatal_auth_config(message: str) -> None:
    sys.stderr.write(message)
    sys.exit(2)


def _load_token_principals() -> dict[str, MCPClientPrincipal]:
    """Load bearer-token principals for the HTTP MCP edge.

    Preferred format:
      MNEMOS_MCP_TOKENS=user1:api_key1,user2:api_key2

    If the MCP bearer token must differ from the backend API key:
      MNEMOS_MCP_TOKENS=user1:mcp_token1:api_key1
    """
    settings = get_settings()
    if not _mcp_auth_enabled():
        # Auth off: tokens are neither required nor parsed (a malformed map
        # cannot FATAL), and none of their values are ever logged.
        logger.warning(_mcp_auth_disabled_warning())
        if settings.mcp.tokens.strip() or settings.mcp.token.strip():
            logger.info("MNEMOS_MCP_TOKEN/MNEMOS_MCP_TOKENS are set but ignored while MCP auth is disabled")
        return {}
    raw_map = settings.mcp.tokens.strip()
    if raw_map:
        principals: dict[str, MCPClientPrincipal] = {}
        for item in raw_map.split(","):
            item = item.strip()
            if not item:
                continue
            parts = [part.strip() for part in item.split(":", 2)]
            if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
                _fatal_auth_config(
                    "FATAL: MNEMOS_MCP_TOKENS entries must be user_id:token or user_id:mcp_token:api_key.\n"
                )
            user_id = parts[0]
            token = parts[1]
            api_key = parts[2] if len(parts) == 3 and parts[2] else token
            if token in principals:
                _fatal_auth_config(
                    "FATAL: duplicate bearer token in MNEMOS_MCP_TOKENS. Each MCP client token must be unique.\n"
                )
            principals[token] = MCPClientPrincipal(user_id=user_id, api_key=api_key)
        if not principals:
            _fatal_auth_config("FATAL: MNEMOS_MCP_TOKENS was set but empty.\n")
        logger.info("Configured %d per-user MCP HTTP bearer token(s)", len(principals))
        return principals

    # Required bearer token. We refuse to start without one because
    # this edge exposes full memory write access.
    tok = settings.mcp.token.strip()
    if not tok:
        oauth = settings.oauth
        oauth_ready = bool(oauth.issuer.strip() and oauth.admin_passphrase.strip())
        if oauth_ready:
            logger.info("MCP HTTP bearer authentication configured for OAuth-only mode")
            return {}
        _fatal_auth_config(
            "FATAL: configure MNEMOS_MCP_TOKEN/MNEMOS_MCP_TOKENS or a complete\n"
            "MCP OAuth setup (MNEMOS_OAUTH_ISSUER,\n"
            "MNEMOS_OAUTH_ADMIN_PASSPHRASE, and MNEMOS_DATABASE_DSN).\n"
        )
    logger.warning(
        "WARNING: MCP HTTP/SSE is using one shared MNEMOS_MCP_TOKEN. "
        "All accepted clients will share the backend MNEMOS_API_KEY "
        "identity. Set MNEMOS_MCP_TOKENS=user:api_key,... for per-user "
        "token issuance and backend tenancy."
    )
    return {tok: _trusted_network_principal()}


MCP_AUTH_ENABLED = _mcp_auth_enabled()
TOKEN_PRINCIPALS = _load_token_principals()


def _match_principal(presented: str) -> MCPClientPrincipal | None:
    """Constant-time bearer-token match (F4, 2026-06-28).

    A plain ``TOKEN_PRINCIPALS.get(presented)`` compares dict keys with
    short-circuiting equality, leaking token bytes through response timing —
    inconsistent with the ``hmac.compare_digest`` checks elsewhere
    (``mcp_audit.py``, ``audit/crypto.py``, the hashed HTTP-API path). Compare
    against every configured token with ``hmac.compare_digest`` and do **not**
    early-exit on a hit, so neither the match position nor which token matched
    is observable via timing.
    """
    matched: MCPClientPrincipal | None = None
    for token, candidate in TOKEN_PRINCIPALS.items():
        if hmac.compare_digest(presented, token):
            matched = candidate
    return matched


def _verify_presented_token(presented: str) -> MCPClientPrincipal | None:
    """Accept a configured static bearer or a valid OAuth-issued JWT.

    Static-token matching retains the constant-time comparison used by the
    current 6.1 line. OAuth JWTs are signature-, issuer-, audience-, expiry-,
    subject-, and scope-validated by :class:`OAuthService`.
    """
    static = _match_principal(presented)
    if static is not None:
        return static
    try:
        service = get_oauth_service()
    except RuntimeError:
        return None
    try:
        claims = service.validate_access_token(presented)
    except jwt.PyJWTError:
        return None
    user_id = claims.get("sub")
    api_key = get_settings().server.api_key.strip() or None
    return MCPClientPrincipal(user_id=user_id, api_key=api_key)


def _principal_id(principal: MCPClientPrincipal) -> str:
    """Return the stable caller identity used to bind SSE sessions."""
    api_key_fingerprint = hashlib.sha256((principal.api_key or "").encode()).hexdigest()
    if principal.user_id:
        return f"user:{principal.user_id}:api:{api_key_fingerprint}"
    return f"legacy-token:api:{api_key_fingerprint}"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate `Authorization: Bearer <token>` on every request before
    the SSE handshake or the POST-message endpoint sees it. Reject
    everything else with 401 + a `WWW-Authenticate` header so the
    client knows what scheme to use.

    With MCP auth disabled (MNEMOS_MCP_AUTH_ENABLED=false, or the profile
    default) every request is attached to the legacy single-token
    principal and any Authorization header is ignored, so clients still
    configured with an old token keep working."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in {"/health", "/healthz"} or path.startswith("/.well-known/") or path.startswith("/oauth/"):
            return await call_next(request)
        if not MCP_AUTH_ENABLED:
            principal = _trusted_network_principal()
            request.state.mnemos_mcp_principal = principal
            request.state.mnemos_mcp_principal_id = _principal_id(principal)
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnemos-mcp"'},
            )
        presented = auth.split(" ", 1)[1].strip()
        principal = _verify_presented_token(presented)
        if principal is None:
            return JSONResponse(
                {"error": "invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnemos-mcp"'},
            )
        request.state.mnemos_mcp_principal = principal
        request.state.mnemos_mcp_principal_id = _principal_id(principal)
        return await call_next(request)


sse = SseServerTransport("/messages/")
_sse_session_principals: dict[str, str] = {}
_sse_session_bind_lock = asyncio.Lock()
_sse_tasks: set[asyncio.Task] = set()


class _RawASGIRoute(BaseRoute):
    """Route an ASGI transport without Starlette trying to add a response.

    The MCP SDK owns the HTTP response for both SSE and Streamable HTTP.  A
    normal ``Route`` expects its endpoint to return a Response after the SDK
    has written one, which used to turn a clean SSE disconnect into a noisy
    ``TypeError: 'NoneType' object is not callable``.
    """

    def __init__(self, path: str, app, methods: set[str]) -> None:
        self.path = path
        self.app = app
        self.methods = methods

    def matches(self, scope):
        if scope["type"] != "http" or scope["path"] != self.path:
            return Match.NONE, {}
        if scope["method"] not in self.methods:
            return Match.PARTIAL, {}
        return Match.FULL, {}

    def url_path_for(self, name: str, /, **path_params):
        raise NoMatchFound(name, path_params)

    async def handle(self, scope, receive, send) -> None:
        await self.app(scope, receive, send)


@dataclass
class _StreamableSession:
    transport: StreamableHTTPServerTransport
    principal_id: str
    principal: MCPClientPrincipal
    context: MCPUserContext
    ready: asyncio.Future[None]
    task: asyncio.Task[None] | None = None
    last_activity: float = 0.0
    active_requests: int = 0


class _StreamableHTTPMCP:
    """Per-principal session manager for the preferred MCP ``/mcp`` route."""

    def __init__(self) -> None:
        self._sessions: dict[str, _StreamableSession] = {}
        self._lock = asyncio.Lock()
        self.max_sessions = mcp_max_sessions_env()
        self.max_principal_sessions = mcp_max_principal_sessions_env()
        self.max_body_bytes = mcp_max_initial_body_bytes_env()
        self.idle_seconds = mcp_session_idle_seconds_env()
        self.startup_seconds = mcp_session_startup_seconds_env()
        self._reaper = None
        self._closing = False

    async def _stop_sessions(self, sessions):
        tasks = [session.task for session in sessions if session.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _expire_idle(self):
        async with self._lock:
            expired = [key for key, session in self._sessions.items()
                       if not session.active_requests and time.monotonic() - session.last_activity >= self.idle_seconds]
            sessions = [self._sessions.pop(key) for key in expired]
        await self._stop_sessions(sessions)

    async def _reap(self):
        while True:
            await asyncio.sleep(min(self.idle_seconds, 30))
            await self._expire_idle()

    async def shutdown(self):
        self._closing = True
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await self._stop_sessions(sessions)

    async def _run_session(self, session_id: str, session: _StreamableSession) -> None:
        context_tokens = set_mcp_backend_context(
            api_key=session.principal.api_key,
            user_id=session.context.user_id,
            role=session.context.role,
            namespace=session.context.namespace,
        )
        try:
            async with session.transport.connect() as streams:
                if not session.ready.done():
                    session.ready.set_result(None)
                read_stream, write_stream = streams
                await app.run(read_stream, write_stream, app.create_initialization_options())
        except BaseException as exc:
            if not session.ready.done():
                session.ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                logger.exception("Streamable HTTP MCP session %s failed", session_id)
        finally:
            reset_mcp_backend_context(context_tokens)
            async with self._lock:
                if self._sessions.get(session_id) is session:
                    self._sessions.pop(session_id, None)

    async def _new_session(self, request) -> _StreamableSession | PlainTextResponse:
        principal = getattr(request.state, "mnemos_mcp_principal", None)
        principal_id = getattr(request.state, "mnemos_mcp_principal_id", None)
        if principal is None or principal_id is None:
            return PlainTextResponse("unauthorized", status_code=403)
        try:
            context = await _resolve_mcp_user_context(request)
        except PermissionError:
            return PlainTextResponse("unauthorized", status_code=403)

        await self._expire_idle()
        async with self._lock:
            if self._closing:
                return PlainTextResponse("MCP session unavailable", status_code=503)
            if len(self._sessions) >= self.max_sessions or sum(
                item.principal_id == principal_id for item in self._sessions.values()
            ) >= self.max_principal_sessions:
                return PlainTextResponse("MCP session limit reached", status_code=429)
            session_id = uuid4().hex
            ready = asyncio.get_running_loop().create_future()
            transport = StreamableHTTPServerTransport(session_id)
            session = _StreamableSession(transport, principal_id, principal, context, ready,
                                         last_activity=time.monotonic())
            self._sessions[session_id] = session
            session.task = asyncio.create_task(self._run_session(session_id, session))
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap())
        try:
            await asyncio.wait_for(asyncio.shield(ready), self.startup_seconds)
        except BaseException:
            self._sessions.pop(session_id, None)
            await self._stop_sessions([session])
            if ready.done() and not ready.cancelled():
                ready.exception()
            raise
        return session

    async def __call__(self, scope, receive, send) -> None:
        from starlette.requests import Request

        request = Request(scope, receive, send)
        session_id = request.headers.get("mcp-session-id")
        if session_id:
            async with self._lock:
                session = self._sessions.get(session_id)
            if session is None or (session.task is not None and session.task.done()):
                async with self._lock:
                    self._sessions.pop(session_id, None)
                response = PlainTextResponse("session expired or never existed", status_code=404)
                await response(scope, receive, send)
                return
            if getattr(request.state, "mnemos_mcp_principal_id", None) != session.principal_id:
                response = PlainTextResponse("session does not belong to caller", status_code=403)
                await response(scope, receive, send)
                return
        else:
            if scope["method"] != "POST":
                response = PlainTextResponse("MCP session id is required", status_code=400)
                await response(scope, receive, send)
                return
            body_buffer = bytearray()
            async for chunk in request.stream():
                if len(body_buffer) + len(chunk) > self.max_body_bytes:
                    await PlainTextResponse("initialize body exceeds limit", status_code=413)(scope, receive, send)
                    return
                body_buffer.extend(chunk)
            body = bytes(body_buffer)
            try:
                initial_request = json.loads(body)
            except json.JSONDecodeError:
                initial_request = None
            if not isinstance(initial_request, dict) or initial_request.get("method") != "initialize":
                response = PlainTextResponse("initialize is required to create an MCP session", status_code=400)
                await response(scope, receive, send)
                return

            # The bounded stream consumed the original ASGI receive callable.
            # Replay its exact bytes so the SDK still owns JSON-RPC parsing and
            # response framing below.  Refusing non-initialize requests before
            # allocating a transport also prevents session-map growth from
            # malformed first requests.
            sent_body = False
            original_receive = receive

            async def replay_receive():
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await original_receive()

            receive = replay_receive
            try:
                session = await self._new_session(request)
            except Exception:
                logger.exception("Could not start Streamable HTTP MCP session")
                response = PlainTextResponse("MCP session unavailable", status_code=503)
                await response(scope, receive, send)
                return
            if isinstance(session, PlainTextResponse):
                await session(scope, receive, send)
                return

        # An open GET is the server-to-client notification stream and can stay
        # open indefinitely; counting it as an active request made the idle
        # reaper skip the session forever and exhausted the per-principal cap.
        # Only client-driven requests keep a session alive; a client that
        # holds a silent stream past the idle window re-initialises on 404.
        is_stream = scope["method"] == "GET"
        if not is_stream:
            session.active_requests += 1
        session.last_activity = time.monotonic()
        try:
            await session.transport.handle_request(scope, receive, send)
        finally:
            if not is_stream:
                session.active_requests -= 1
            session.last_activity = time.monotonic()


streamable_mcp = _StreamableHTTPMCP()


def _session_id_key(session_id) -> str:
    if isinstance(session_id, UUID):
        return session_id.hex
    session_id_text = str(session_id)
    try:
        return UUID(session_id_text).hex
    except ValueError:
        return session_id_text


class AmbiguousSessionIdError(ValueError):
    """Raised when a request supplies conflicting session identifiers."""


def _extract_query_session_id(scope) -> str | None:
    query_string = scope.get("query_string", b"")
    if isinstance(query_string, str):
        query_string = query_string.encode()
    query_params = parse_qs(query_string.decode("latin-1"), keep_blank_values=True)
    has_session_id = "session_id" in query_params
    has_session_id_camel = "sessionId" in query_params
    if has_session_id and has_session_id_camel:
        raise AmbiguousSessionIdError("ambiguous session id")
    if has_session_id:
        values = query_params["session_id"]
    elif has_session_id_camel:
        values = query_params["sessionId"]
    else:
        return None
    if len(values) > 1:
        raise AmbiguousSessionIdError("ambiguous session id")
    raw_session_id = values[0]
    if raw_session_id:
        return _session_id_key(raw_session_id)
    return None


def _extract_path_session_id(scope) -> str | None:
    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :]
    messages_prefix = "/messages/"
    if path.startswith(messages_prefix) and len(path) > len(messages_prefix):
        return _session_id_key(path[len(messages_prefix) :].strip("/"))
    if root_path.rstrip("/") == "/messages" and path.strip("/"):
        return _session_id_key(path.strip("/"))
    return None


def _extract_session_id_from_scope(scope) -> str | None:
    query_session_id = _extract_query_session_id(scope)
    path_session_id = _extract_path_session_id(scope)
    if query_session_id and path_session_id and query_session_id != path_session_id:
        raise AmbiguousSessionIdError("ambiguous session id")
    return query_session_id or path_session_id


def _scope_state(scope) -> dict:
    state = scope.get("state")
    if isinstance(state, dict):
        return state
    return {}


def _authenticated_principal_id_from_scope(scope) -> str | None:
    state = _scope_state(scope)
    principal_id = state.get("mnemos_mcp_principal_id")
    if principal_id:
        return principal_id
    principal = state.get("mnemos_mcp_principal")
    if principal is not None:
        return _principal_id(principal)
    return None


@asynccontextmanager
async def _bound_sse_connection(request, principal_id: str):
    session_key = None
    transport_context = sse.connect_sse(
        request.scope,
        request.receive,
        request._send,
    )
    async with _sse_session_bind_lock:
        before_sessions = set(getattr(sse, "_read_stream_writers", {}))
        streams = await transport_context.__aenter__()
        after_sessions = set(getattr(sse, "_read_stream_writers", {}))
        new_sessions = after_sessions - before_sessions
        if len(new_sessions) != 1:
            await transport_context.__aexit__(None, None, None)
            raise RuntimeError("Could not identify newly-created MCP SSE session for principal binding")
        session_key = _session_id_key(new_sessions.pop())
        _sse_session_principals[session_key] = principal_id

    try:
        yield streams
    except BaseException as exc:
        _sse_session_principals.pop(session_key, None)
        await transport_context.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        _sse_session_principals.pop(session_key, None)
        await transport_context.__aexit__(None, None, None)


async def handle_post_message(scope, receive, send) -> None:
    try:
        session_id = _extract_session_id_from_scope(scope)
    except AmbiguousSessionIdError:
        response = PlainTextResponse("ambiguous session id", status_code=400)
        return await response(scope, receive, send)
    if session_id is None:
        response = PlainTextResponse("session_id is required", status_code=400)
        return await response(scope, receive, send)

    owner_principal_id = _sse_session_principals.get(session_id)
    if owner_principal_id is None:
        response = PlainTextResponse("session expired or never existed", status_code=404)
        return await response(scope, receive, send)

    caller_principal_id = _authenticated_principal_id_from_scope(scope)
    if caller_principal_id != owner_principal_id:
        response = PlainTextResponse("session does not belong to caller", status_code=403)
        return await response(scope, receive, send)

    forwarded_scope = dict(scope)
    forwarded_scope["query_string"] = f"session_id={quote(session_id, safe='')}".encode()

    return await sse.handle_post_message(forwarded_scope, receive, send)


async def handle_sse(request):
    """Open an SSE stream and pump MCP frames over it. The transport
    object owns the bidirectional plumbing; we just hand it the
    stream pair the ASGI runtime gave us."""
    # Starlette exposes the underlying ASGI send via a private attr on
    # request; the SDK examples accept this trade for now.
    principal = getattr(request.state, "mnemos_mcp_principal", None)
    principal_id = getattr(request.state, "mnemos_mcp_principal_id", None)
    if principal_id is None and principal is not None:
        principal_id = _principal_id(principal)
    try:
        principal_context = await _resolve_mcp_user_context(request)
    except PermissionError:
        # Revoked/expired key: refuse the stream rather than opening it with a
        # context synthesised from the caller's own claimed identity.
        return PlainTextResponse("unauthorized", status_code=403)
    context_tokens = set_mcp_backend_context(
        api_key=principal.api_key if principal else None,
        user_id=principal_context.user_id,
        role=principal_context.role,
        namespace=principal_context.namespace,
    )
    task = asyncio.current_task()
    if streamable_mcp._closing:
        reset_mcp_backend_context(context_tokens)
        return PlainTextResponse("MCP session unavailable", status_code=503)
    _sse_tasks.add(task)
    try:
        async with _bound_sse_connection(request, principal_id or "unknown") as streams:
            read_stream, write_stream = streams
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    finally:
        _sse_tasks.discard(task)
        reset_mcp_backend_context(context_tokens)


async def handle_sse_asgi(scope, receive, send) -> None:
    """ASGI wrapper for legacy SSE.

    ``SseServerTransport`` writes its response directly.  Keeping that work
    out of a regular Starlette ``Route`` prevents Starlette from attempting a
    second response after the client disconnects.
    """
    from starlette.requests import Request

    response = await handle_sse(Request(scope, receive, send))
    if response is not None:
        await response(scope, receive, send)


# MCP principal context cache (entry → resolved MCPUserContext +
# monotonic-time expiry). Until #203 this was an unbounded dict
# that NEVER expired — operator role / namespace changes were
# hidden for the process lifetime. Bounded LRU + TTL closes
# both gaps without per-request httpx round-trips.
_principal_context_cache: dict[str, tuple[MCPUserContext, float]] = {}

# Generation stamp for the principal-context cache.
#
# MCPUserContext carries `role` and `namespace` -- authorization inputs. The
# cache held them for _PRINCIPAL_CACHE_TTL_SECONDS with NOTHING invalidating it
# on an ACL change, so narrowing a principal's role left MCP honouring the old
# one for up to five minutes. That is the same revocation-freshness leak the
# search cache was fixed for (ncz-os/mnemos#1), on a more sensitive value.
#
# The visibility epoch already advances on every visibility-narrowing mutation.
# Reading it here is sync-safe: it is only a cached integer refreshed by the
# async path, and a stale-but-lower generation can only cause an extra miss,
# never a stale hit.
_principal_cache_generation: int = 0


def _register_epoch_listener() -> None:
    """Subscribe to visibility-epoch bumps, if core is importable.

    Registration is pull-based so core never has to import this module (which
    exits at import when MCP auth is enabled and no token is configured).
    """
    try:
        from mnemos.core.lifecycle import register_visibility_epoch_listener

        register_visibility_epoch_listener(principal_cache_invalidate)
    except Exception:
        pass


def principal_cache_invalidate(generation: int | None = None) -> None:
    """Drop every cached principal context.

    Called when the visibility epoch advances. Cheap and unconditional: the
    cache is capped and repopulates on the next request, and being wrong in the
    direction of "recompute" is the only safe direction for an authz input.
    """
    global _principal_cache_generation
    if generation is not None:
        if generation == _principal_cache_generation:
            return
        _principal_cache_generation = generation
    _principal_context_cache.clear()
    # The SSE row-visibility envelopes are derived from the same authz inputs
    # (role / namespace / group membership), so they narrow together.
    _reset_row_visibility_cache()


_PRINCIPAL_CACHE_TTL_SECONDS = 300.0  # 5 min — short enough that
# role/namespace changes propagate within a sane window; long
# enough that high-rps SSE callers don't re-hit /auth/oauth/me.
_PRINCIPAL_CACHE_MAX = 1024  # cap per-process. Beyond this the
# half-oldest entries are evicted; principal-id churn (e.g. token
# rotation in CI) won't grow the dict without bound.


def _principal_cache_get(principal_id: str) -> MCPUserContext | None:
    """Return cached context if fresh, else None (also evict
    stale entries on read so the cache shrinks lazily).

    Tests historically assigned bare MCPUserContext values to
    ``_principal_context_cache[key]``; tolerate that shape by
    treating a non-tuple entry as "never expires" — the
    `_principal_cache_set` helper writes the tuple shape, but
    direct-assigned bare values continue to work in tests.
    """
    entry = _principal_context_cache.get(principal_id)
    if entry is None:
        return None
    if isinstance(entry, tuple):
        ctx, expires_at = entry
        if _monotonic() >= expires_at:
            _principal_context_cache.pop(principal_id, None)
            # The expiration sweeps below run from the hot authz read path.
            _sweep_expired_row_visibility()
            return None
        return ctx
    # Backward-compat: bare MCPUserContext (test helper / direct
    # assign). Treat as never-expires.
    return entry  # type: ignore[return-value]


def _principal_cache_set(principal_id: str, context: MCPUserContext) -> None:
    """Write the tuple-shaped entry with TTL; cap size."""
    if len(_principal_context_cache) >= _PRINCIPAL_CACHE_MAX and principal_id not in _principal_context_cache:
        # Evict the half-oldest entries by expiry time so a single
        # eviction storm doesn't repeat on every subsequent set.
        # Bare-context entries (test direct-assign) sort to the
        # bottom — `float("inf")` keeps them past any tuple entry.
        def _expiry_key(item: tuple[str, Any]) -> float:
            v = item[1]
            return v[1] if isinstance(v, tuple) else float("inf")

        items = sorted(_principal_context_cache.items(), key=_expiry_key)
        for k, _ in items[: max(1, len(items) // 2)]:
            _principal_context_cache.pop(k, None)
    _principal_context_cache[principal_id] = (
        context,
        _monotonic() + _PRINCIPAL_CACHE_TTL_SECONDS,
    )


# Indirection so tests can monkeypatch the clock.
def _monotonic() -> float:
    import time

    return time.monotonic()


def _query_value(request, name: str, default: str) -> str:
    params = getattr(request, "query_params", {})
    value = params.get(name, default) if params is not None else default
    return value or default


def _safe_subject_token(value: str, *, label: str) -> str:
    if value == "*":
        return value
    if not value.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"invalid NATS {label}: {value}")
    return value


def _safe_namespace(namespace: str | None) -> str:
    """Namespace token for a NATS subject segment (CF-35/MN-17).

    Routes through the shared encoder (:func:`safe_subject_segment`) so
    distinct namespaces always map to distinct subjects; a stream only
    receives its own namespace's events. The mapping is injective
    (``.`` -> ``_``, ``_`` -> ``%5F``, ``%`` -> ``%25``); the established
    ``alice.ns`` -> ``alice_ns`` shape is unchanged.
    """
    return safe_subject_segment(namespace)


def _is_operator_context(context: MCPUserContext) -> bool:
    return context.role in OPERATOR_ROLES


async def _resolve_mcp_user_context(request) -> MCPUserContext:
    principal = getattr(getattr(request, "state", None), "mnemos_mcp_principal", None)
    principal_id = getattr(getattr(request, "state", None), "mnemos_mcp_principal_id", None)
    if principal_id:
        cached = _principal_cache_get(principal_id)
        if cached is not None:
            return cached

    if principal is not None and principal.api_key:
        try:
            base = get_settings().server.base.rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{base}/auth/oauth/me",
                    headers={"Authorization": f"Bearer {principal.api_key}"},
                )
            response.raise_for_status()
            body = response.json()
            context = MCPUserContext(
                user_id=body.get("user_id") or principal.user_id,
                role=body.get("role") or "user",
                namespace=body.get("namespace") or principal.user_id or "default",
            )
            if principal_id:
                _principal_cache_set(principal_id, context)
            return context
        except httpx.HTTPStatusError as exc:
            # FAIL CLOSED on an authentication verdict. A 401/403 from
            # /auth/oauth/me means this key is revoked, expired or not
            # entitled. Falling through to the permissive context below would
            # mint an authenticated MCPUserContext from the caller's own
            # claimed identity, so a revoked API key would keep working
            # indefinitely -- _match_principal only compares the static token
            # and never consults revocation.
            status = exc.response.status_code if exc.response is not None else None
            if status in (401, 403):
                logger.warning("MCP principal context rejected by auth service (HTTP %s); denying", status)
                raise PermissionError("MCP principal is not authorized") from exc
            logger.warning("MCP principal context lookup failed with HTTP %s: %s", status, exc)
        except Exception as exc:
            # Transport/timeout failures are NOT an authorization verdict, so
            # the degraded context below is still appropriate for them.
            logger.warning("MCP NATS SSE principal context lookup failed: %s", exc)

    context = MCPUserContext(
        user_id=getattr(principal, "user_id", None),
        role="user",
        namespace=getattr(principal, "user_id", None) or "default",
    )
    if principal_id:
        _principal_cache_set(principal_id, context)
    return context


def _parse_nats_sse_subjects(request, context: MCPUserContext) -> list[str]:
    if _is_operator_context(context):
        raw = request.query_params.get("subjects") if hasattr(request, "query_params") else None
        if raw:
            subjects = [part.strip() for part in raw.split(",") if part.strip()]
            invalid = [
                subject
                for subject in subjects
                if not subject.startswith("mnemos.") or any(ch.isspace() for ch in subject)
            ]
            if invalid:
                raise ValueError(f"invalid NATS subject filter: {invalid[0]}")
            if subjects:
                return subjects

    event_class = _safe_subject_token(_query_value(request, "event_class", "*"), label="event_class")
    event_action = _safe_subject_token(_query_value(request, "event_action", "*"), label="event_action")
    return [f"mnemos.{event_class}.{event_action}.{_safe_namespace(context.namespace)}"]


def _sse_frame(event: str, data: str) -> bytes:
    lines = [f"event: {event}"]
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _nats_msg_data(msg: Any) -> str:
    data = getattr(msg, "data", b"")
    if isinstance(data, str):
        return data
    return bytes(data).decode("utf-8", errors="replace")


def _nats_sse_raw_enabled() -> bool:
    return mcp_nats_raw_enabled()


def _nats_sse_is_delete(subject: Any) -> bool:
    """Whether ``subject`` is a well-formed ``mnemos.memory.deleted.<ns>`` subject.

    A pure SHAPE helper: the subject must be exactly four segments
    (``mnemos.memory.deleted.<ns>``) with a non-empty namespace segment,
    matching the namespace gate precisely. A subject without a namespace
    (``mnemos.memory.deleted``), with an empty namespace, or with extra
    trailing segments is not a delete event this bridge publishes.

    The namespace segment is NOT required to be a fixed point of the encoder:
    ``safe_subject_segment`` maps ``.`` -> ``_`` and ``_`` -> ``%5F``, so a
    dotted/underscored namespace never re-encodes to itself. Requiring a
    fixed point rejected the common case (``shared.ns`` publishes as
    ``shared_ns``). It is also NOT required to be free of ``*``/``>``/
    whitespace: the encoder percent-encodes those in the published segment
    (``*`` -> ``%2A``, ``a b`` -> ``a%20b``), so a caller whose own namespace
    IS ``*`` legitimately publishes ``mnemos.memory.deleted.%2A``.

    This helper deliberately carries no security decision. The category
    strip in ``_nats_sse_data`` holds for *every* memory-class delete frame
    the bridge emits, including raw/unencoded subjects and wildcard
    namespaces, so it compares the subject segments
    (``mnemos.memory.deleted``) and never consults this helper.
    """
    segments = str(subject or "").split(".")
    if len(segments) != 4 or segments[0] != "mnemos" or segments[1] != "memory" or segments[2] != "deleted":
        return False
    return bool(segments[3])


def _nats_sse_is_delete_subject(subject: Any) -> bool:
    """Whether ``subject`` addresses the memory-delete CLASS (``mnemos.memory.deleted.*``).

    This is a prefix/segment test only -- it says an event concerns the
    deletion of a memory row, not that its subject is well-formed. It is the
    predicate ``_nats_sse_data`` uses to decide that a frame is a delete
    notification and must therefore drop the row ``category``: a delete is
    authorized against the bounded allow-cache, never the (vanished) row, so
    the category was never visibility-checked and shipping it would disclose
    the topic of another owner's private row. Using a prefix test (rather
    than a shape test on the exact four-segment subject) means the strip can
    never be defeated by an unusual but still-delete-shaped subject such as a
    percent-encoded wildcard namespace (``mnemos.memory.deleted.%2A``) or a
    raw/unencoded one.
    """
    segments = str(subject or "").split(".")
    return len(segments) >= 3 and segments[0] == "mnemos" and segments[1] == "memory" and segments[2] == "deleted"


def _nats_sse_data(msg: Any) -> str:
    raw = _nats_msg_data(msg)
    if _nats_sse_raw_enabled():
        return raw
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    summary = {
        "subject": getattr(msg, "subject", "mnemos.event"),
        "memory_id": payload.get("memory_id") or payload.get("id"),
        "namespace": payload.get("namespace"),
        "category": payload.get("category"),
        "source_node": payload.get("source_node"),
    }
    if _nats_sse_is_delete_subject(getattr(msg, "subject", "")):
        # A delete event is authorized against the bounded allow-cache, not
        # against the row (which is gone, so the visibility-filtered lookup
        # returns ``None``). The row's ``category`` was therefore never
        # visibility-checked for this frame: the publisher includes it, but
        # shipping it would disclose the topic of another owner's
        # (now-deleted) private row to callers sharing the namespace. The
        # delete notification only needs the id + namespace to invalidate
        # caches, so the category is dropped.
        summary["category"] = None
    return json.dumps({k: v for k, v in summary.items() if v is not None})


@dataclass(frozen=True)
class _SSERowVisibility:
    """Which rows the caller of one SSE stream may see event metadata for.

    Built from the same ``VisibilityFilter`` the REST read paths use, so the
    bridge cannot be wider than ``GET /v1/memories/{id}``.
    """

    bypass: bool
    user_id: str | None
    group_ids: tuple[str, ...]
    namespace: str
    exclude_namespaces: tuple[str, ...]
    # True ONLY for the trusted-network / shared-MNEMOS_MCP_TOKEN edge: MCP
    # auth is disabled (or the caller presented the single shared token), so
    # the principal has no per-user REST identity of its own. This is an
    # explicit auth-state flag, not an inference from ``user_id`` being
    # empty -- a non-root caller with an unresolved identity (role=admin/
    # monitor, user_id=None) carries user_id="" but is NOT legacy, and must
    # fail closed on every row gate.
    legacy_principal: bool = False

    def allows_row(self, row: Mapping[str, Any] | None) -> bool:
        """True when the caller may read ``row``.

        Mirrors ``visibility.read_visibility_predicate`` for the row
        disjuncts that need nothing but the row itself: same namespace,
        owner, federation-replicated, world-readable and (where the row
        carries a single ``group_id``) group-readable.

        The per-principal ACL disjunct needs ``memory_acl`` and is
        deliberately NOT evaluated here -- applying this gate can therefore
        only ever narrow the bridge, never widen it.
        """
        if row is None:
            return False
        row_ns = row.get("namespace")
        if row_ns in self.exclude_namespaces:
            return False
        if self.bypass:
            return True
        if row_ns != self.namespace:
            return False
        if self.user_id is not None and row.get("owner_id") == self.user_id:
            return True
        if row.get("federation_source") is not None:
            return True
        try:
            permission_mode = int(row.get("permission_mode") or 0)
        except (TypeError, ValueError):
            return False
        # Permission modes are stored as DECIMAL integers whose digits are
        # octal (e.g. 755 means "octal 755" = owner rwx, group r-x, world r-x;
        # 600 = owner rw only). This is NOT a Unix bitmask (0o755 == 493).
        # The validator ``_validate_permission_mode`` (mnemos/api/read_policy.py)
        # enforces 0-777 with every digit in "01234567". The REST read path
        # (mnemos/core/visibility.py ``read_visibility_predicate``) extracts
        # world/group bits with exactly these digit operations:
        #   world:  (permission_mode % 10) >= 4
        #   group:  ((permission_mode // 10) % 10) >= 4 AND group_id IS NOT NULL
        # and compares against the constant 4. We mirror that extraction here
        # and compare against READ_BIT (== 4, imported from
        # mnemos.core.visibility as ACL_READ_BIT) so group/world-readable rows
        # match here exactly when they match the REST visibility predicate.
        # Do NOT switch to bitmask operators (e.g. ``& 0o004``): that would
        # diverge from the REST path and desync the bridge from ``GET
        # /v1/memories/{id}``.
        if permission_mode % 10 >= READ_BIT:
            return True
        group_id = row.get("group_id")
        if (permission_mode // 10) % 10 >= READ_BIT and group_id is not None:
            return group_id in self.group_ids
        return False


def principal_api_key_is_shared_token() -> bool:
    """Whether the legacy single-token principal is in use.

    True when MCP auth is enabled and the token map holds exactly one
    entry mapped to ``_trusted_network_principal`` -- i.e. the operator
    configured a single shared ``MNEMOS_MCP_TOKEN`` rather than the
    per-user ``MNEMOS_MCP_TOKENS=user:token`` map. A caller that presented
    that token has no per-user REST identity (the shared token is not a
    valid backend API key), so it rides the legacy principal exactly as
    the auth-disabled edge does.

    This is an explicit auth-config signal, not an inference from the
    caller's resolved identity: a non-root context with ``user_id=None``
    (role=admin/monitor, or a token user whose OAuth lookup failed) is
    still NOT legacy even when this returns True.
    """
    return bool(TOKEN_PRINCIPALS) and all(
        principal is _trusted_network_principal() for principal in TOKEN_PRINCIPALS.values()
    )


def _sse_row_visibility(context: MCPUserContext) -> _SSERowVisibility:
    """Resolve the caller's row-visibility envelope for the SSE bridge.

    ``MCPUserContext`` carries only ``user_id``/``role``/``namespace`` -- the
    MCP auth path never resolves group membership, so ``group_ids`` is empty
    here by construction, not by omission. That makes the bridge strictly
    NARROWER than the REST read path (which knows the caller's groups): a row
    readable solely through group membership will not surface its metadata
    here. Narrow is the safe direction for an authorization input, so this is
    deliberate rather than a defect -- widening it would require the MCP auth
    path to start resolving groups, which is a separate change.
    """
    from mnemos.core.auth_context import UserContext
    from mnemos.persistence.visibility import VisibilityFilter

    user = UserContext(
        user_id=context.user_id or "",
        group_ids=[],
        role=context.role,
        namespace=context.namespace or "default",
        authenticated=bool(context.user_id),
    )
    visibility = VisibilityFilter.for_read(user, namespace=user.namespace)
    # Legacy principal = an explicit auth-state fact, never an empty-id
    # inference. It is true ONLY when the MCP edge resolved this caller with
    # no REST identity at all -- MCP auth disabled (every caller rides the
    # shared backend identity) or the single shared MNEMOS_MCP_TOKEN
    # presented with MCP auth enabled. A non-root context whose user_id is
    # empty for a different reason (role=admin/monitor with user_id=None, or
    # a token user whose OAuth lookup failed) is NOT legacy: the row gate
    # must apply to them, not skip it.
    legacy_principal = (
        not context.user_id
        and not (visibility.scope == VisibilityScope.ROOT_BYPASS)
        and (not _mcp_auth_enabled() or principal_api_key_is_shared_token())
    )
    return _SSERowVisibility(
        bypass=visibility.scope == VisibilityScope.ROOT_BYPASS,
        user_id=visibility.user_id,
        group_ids=tuple(visibility.group_ids or ()),
        namespace=visibility.namespace or "default",
        exclude_namespaces=tuple(visibility.exclude_namespaces or ()),
        legacy_principal=legacy_principal,
    )


_ROW_VISIBILITY_CACHE_TTL_SECONDS = 60.0
_row_visibility_cache: dict[tuple[Any, ...], tuple[_SSERowVisibility, float]] = {}

_SSE_ROW_CACHE_TTL_SECONDS = 60.0
_SSE_ROW_CACHE_MAX = 1024
# Bounded allow-cache for delete frames (review finding 2). A ``memory.deleted``
# event carries only an id the publisher claims; the row gate never sees that
# row (the visibility-filtered lookup returns None for a deleted row on
# purpose). The delete frame is therefore authorised by an allow-set: per
# (caller, namespace) we record the ids for which a created/updated frame was
# ALREADY row-gated and allowed for THIS caller, and ship a delete only for an
# id in that cache. Ids of rows never delivered to the caller are not shipped.
#
# The allow key is ``(caller, namespace)`` and does NOT embed the visibility
# epoch, because the epoch is an in-process, lazily-refreshed high-water mark
# that never reads the shared store, and in degraded mode it advances from a
# wall-clock bucket every few seconds -- keying the cache on it would expire
# every entry far inside the TTL and silently stop delete frames for
# still-readable rows. Instead each map entry carries the epoch observed when
# the entry was (re)written. On read, an entry whose stored epoch differs from
# the current epoch is dropped and the id denied: a visibility bump that was
# observed out-of-band (e.g. another process picked it up via
# ``visibility_epoch_cached``) can therefore never keep serving deletes for
# ids authorized under the old visibility. On write, the entry is always
# stamped with the current epoch. The TTL below and
# ``principal_cache_invalidate()`` (fired on every local visibility bump,
# which clears this cache outright) provide the remaining bounds. Both
# directions are safe: a dropped entry can only delay a delete notification,
# never widen it, because an entry exists only for an id this caller was
# already allowed a row-gated frame for.
#
# Both levels use ``collections.OrderedDict`` (guaranteed insertion order,
# unlike ``set``/``frozenset`` iteration which is CPython implementation detail):
#   * the map is bounded by ``_SSE_DELETE_ALLOW_CACHE_MAX`` via surgical
#     ``popitem(last=False)`` -- no ``clear()``/``update()`` gap, so a concurrent
#     reader never sees an empty cache between eviction steps;
#   * each per-caller id set is an ``OrderedDict`` keyed by id (empty values),
#     capped at the same constant, evicting the OLDEST ids deterministically.
_SSE_DELETE_ALLOW_CACHE_TTL_SECONDS = 300.0
_SSE_DELETE_ALLOW_CACHE_MAX = 1024
# Map value is ``(ids, expires_at, epoch_at_write)``: the per-caller id set,
# its TTL expiry, and the visibility epoch observed when the entry was
# (re)written. The epoch is never part of the key (see the block comment
# above), but it is carried with every entry so an out-of-band epoch advance
# can invalidate it on the next read or write.
_sse_delete_allowed_ids: OrderedDict[tuple[Any, ...], tuple["OrderedDict[str, None]", float, int]] = OrderedDict()
# The visibility-filtered repository lookup in the row gate runs inline in the
# SSE generator loop. A hung backend must not stall every stream, so the call
# is bounded.
_SSE_ROW_LOOKUP_TIMEOUT_SECONDS = 2.0
_sse_row_cache: dict[tuple[Any, ...], tuple[Mapping[str, Any] | None, float]] = {}
_sse_row_cache_lock = asyncio.Lock()


# Indirection so tests can drive the invalidation path without a live epoch
# store. ``None`` = "no broad invalidation seen yet".
_visibility_epoch_seen: int | None = None


def _visibility_epoch() -> int:
    """Best-effort read of the shared visibility epoch.

    Reached lazily (never at import) so importing this module does not open a
    NATS connection. Any failure returns a non-advancing value: a broken epoch
    source must not silently widen anything, because the TTL below still
    bounds staleness. Reads/writes of ``_visibility_epoch_seen`` are plain
    loads/stores of one immutable int -- atomic under the GIL -- and the
    value only ever advances (``max`` with the previous high-water mark),
    so concurrent SSE tasks can at worst recompute a visibility envelope
    one extra time, never read a widened one.
    """
    global _visibility_epoch_seen
    try:
        from mnemos.core.lifecycle import visibility_epoch_cached

        _visibility_epoch_seen = max(_visibility_epoch_seen or 0, int(visibility_epoch_cached()))
    except Exception:
        # visibility_epoch_cached is a synchronous in-process high-water
        # mark with no I/O, so this path fires only when core itself is
        # unimportable -- nothing actionable, and the TTL still bounds
        # staleness on the cached value.
        pass
    return _visibility_epoch_seen or 0


def _reset_row_visibility_cache() -> None:
    _row_visibility_cache.clear()
    _sse_row_cache.clear()
    _sse_delete_allowed_ids.clear()


def _enforce_sse_row_cache_limit() -> None:
    """Evict oldest entries if the SSE row cache exceeds the max size.

    The cache is bounded to prevent unbounded growth. We sweep expired
    entries first, then evict the oldest half if still over limit.
    """
    if len(_sse_row_cache) <= _SSE_ROW_CACHE_MAX:
        return
    _sweep_expired_row_visibility()
    if len(_sse_row_cache) <= _SSE_ROW_CACHE_MAX:
        return
    # Evict oldest entries by expiration time using a single pass.
    # This avoids sorting the whole dict on every insertion.
    # We keep the newest _SSE_ROW_CACHE_MAX entries.
    # Build a list of (expires_at, key) for the current items.
    # For small max (1024) this is acceptable and avoids O(N log N) sort.
    # Use heapq.nsmallest for O(N log K) where K is max.
    import heapq
    items = list(_sse_row_cache.items())
    # Keep the newest entries: those with largest expires_at.
    # nsmallest on negative expires_at gives largest expires_at.
    keep = heapq.nlargest(_SSE_ROW_CACHE_MAX, items, key=lambda kv: kv[1][1])
    # Rebuild cache from kept items.
    new_cache = {k: v for k, v in keep}
    _sse_row_cache.clear()
    _sse_row_cache.update(new_cache)


def _sweep_expired_row_visibility() -> None:
    """Drop expired entries from the bounded row-visibility cache.

    The snapshot-then-pop pattern is deliberate: iterating ``list(...)``
    avoids mutating during iteration, and ``pop(key, None)`` tolerates a
    concurrent task having already evicted or replaced the key. Dict
    operations are individually atomic under the GIL, so the worst case
    under concurrent SSE tasks is a redundant recompute or a re-insert of
    a just-swept key -- both safe directions for a TTL cache.
    """
    now = _monotonic()
    for key, (_visibility, expires_at) in list(_row_visibility_cache.items()):
        if now >= expires_at:
            _row_visibility_cache.pop(key, None)
    for key, (_row, expires_at) in list(_sse_row_cache.items()):
        if now >= expires_at:
            _sse_row_cache.pop(key, None)
    # The delete-allow map stores a 3-tuple ``(ids, expires_at, epoch)``; the
    # epoch is read-validated in ``_sse_delete_allowed``, so only the TTL is
    # relevant to the sweep here.
    for key, (_ids, expires_at, _epoch) in list(_sse_delete_allowed_ids.items()):
        if now >= expires_at:
            _sse_delete_allowed_ids.pop(key, None)


def _sse_delete_allow_key(visibility: "_SSERowVisibility") -> tuple[Any, ...]:
    """Cache key for the delete allow-set: the caller and their namespace.

    Deliberately epoch-free -- see the block comment above
    ``_SSE_DELETE_ALLOW_CACHE_TTL_SECONDS``.
    """
    return (
        visibility.user_id,
        visibility.namespace,
    )


def _sse_delete_allowed(memory_id: str, visibility: "_SSERowVisibility | None") -> bool:
    """Whether this caller was previously allowed a frame for ``memory_id``.

    Consulted ONLY for ``memory.deleted`` frames (see the row gate): a delete
    must not carry an id the caller was never shown, because the publisher
    controls that field and the vanished row offers no authorization check.
    """
    if visibility is None:
        return False
    key = _sse_delete_allow_key(visibility)
    entry = _sse_delete_allowed_ids.get(key)
    now = _monotonic()
    if entry is None:
        return False
    if now >= entry[1]:
        # TTL expired: drop it so the map cannot hold stale allow-sets for
        # departed callers, then deny (fail closed).
        _sse_delete_allowed_ids.pop(key, None)
        return False
    if entry[2] != _visibility_epoch():
        # Epoch advanced since the entry was written: the allow-set may grant
        # access to rows the caller can no longer see, so deny and drop.
        _sse_delete_allowed_ids.pop(key, None)
        return False
    return memory_id in entry[0]


def _record_sse_delete_allowed(memory_id: str, visibility: "_SSERowVisibility | None") -> None:
    """Remember that ``memory_id`` was row-gated and allowed for this caller.

    The entry is per (caller, namespace) and carries the visibility epoch at
    write time. If the epoch advances before the TTL expires, the entry is
    treated as stale: the caller's visibility may have narrowed, so the
    allow-set is replaced rather than reused. Both dimensions are bounded —
    the TTL and ``principal_cache_invalidate()`` clears this cache on epoch
    change — with surgical (never emptying) eviction:

    * **Per-caller id set** -- an insertion-ordered ``OrderedDict`` keyed by
      id. When the cap is hit the OLDEST id (``popitem(last=False)``) is
      dropped deterministically; the bridge only ever narrows.
    * **Per-caller map** -- an insertion-ordered ``OrderedDict``. On every
      write expired entries are swept; if the map is still over
      ``_SSE_DELETE_ALLOW_CACHE_MAX`` the oldest callers
      (``popitem(last=False)``) are dropped one by one. Each ``popitem`` is
      individually atomic under the GIL, so a concurrent reader never sees
      the map emptied between steps.
    """
    if visibility is None:
        return
    key = _sse_delete_allow_key(visibility)
    now = _monotonic()
    epoch = _visibility_epoch()
    entry = _sse_delete_allowed_ids.get(key)
    if entry is None or now >= entry[1] or entry[2] != epoch:
        # Expired, absent, or stale (epoch advanced since write): start a
        # fresh allow-set stamped with the current epoch.
        ids: OrderedDict[str, None] = OrderedDict()
        ids[memory_id] = None
    else:
        ids = entry[0]
        if memory_id not in ids:
            ids[memory_id] = None
            while len(ids) > _SSE_DELETE_ALLOW_CACHE_MAX:
                # Deterministic LRU eviction: drop the oldest id first.
                ids.popitem(last=False)
    _sse_delete_allowed_ids[key] = (
        ids,
        now + _SSE_DELETE_ALLOW_CACHE_TTL_SECONDS,
        epoch,
    )
    _sse_delete_allowed_ids.move_to_end(key)
    if len(_sse_delete_allowed_ids) > _SSE_DELETE_ALLOW_CACHE_MAX:
        # Bound the per-caller map itself: sweep expired entries first, then
        # drop the oldest remaining callers one at a time (narrow, never empty).
        _sweep_expired_row_visibility()
        while len(_sse_delete_allowed_ids) > _SSE_DELETE_ALLOW_CACHE_MAX:
            _sse_delete_allowed_ids.popitem(last=False)


def _cached_row_visibility(context: MCPUserContext) -> _SSERowVisibility:
    key = (
        context.user_id,
        context.role,
        context.namespace,
        _visibility_epoch(),
    )
    cached = _row_visibility_cache.get(key)
    if cached is not None and _monotonic() < cached[1]:
        return cached[0]
    visibility = _sse_row_visibility(context)
    if len(_row_visibility_cache) >= _PRINCIPAL_CACHE_MAX:
        # Evict only what has actually expired first; a full clear would drop
        # every live entry and stampede the authz path on the next event.
        _sweep_expired_row_visibility()
        if len(_row_visibility_cache) >= _PRINCIPAL_CACHE_MAX:
            _row_visibility_cache.clear()
    _row_visibility_cache[key] = (visibility, _monotonic() + _ROW_VISIBILITY_CACHE_TTL_SECONDS)
    return visibility


async def _fetch_sse_row(
    memory_id: str,
    visibility: _SSERowVisibility | None = None,
    *,
    cache_negative: bool = True,
) -> Mapping[str, Any] | None:
    """Load the row a NATS event refers to, or ``None``.

    Read through the repository ABC (never a raw pool handle). Any failure
    returns ``None``: a missing decision is a missing event, which is the
    only safe direction for an authorization input.

    The persistence backend is imported locally (not at module level) to
    avoid a circular import: ``mnemos.core.lifecycle`` pulls in persistence
    facades that, in turn, import this module's visibility helpers. The call
    itself is cheap -- ``get_persistence_backend()`` returns a cached singleton
    -- and this function is invoked only for messages that pass the namespace
    gate and carry a row id, so it is already bounded by the row-visibility
    cache upstream.
    """
    # Snapshot epoch once to avoid race between get and set.
    epoch = _visibility_epoch()
    user_id = visibility.user_id if visibility else None
    namespace = visibility.namespace if visibility else None
    key = (memory_id, user_id, namespace, epoch)
    now = _monotonic()

    # Fast path: check cache under lock to avoid duplicate DB work.
    async with _sse_row_cache_lock:
        cached = _sse_row_cache.get(key)
        if cached is not None and now < cached[1]:
            return cached[0]
        # Ensure we don't exceed max before inserting.
        _enforce_sse_row_cache_limit()

    try:
        from mnemos.core.lifecycle import get_persistence_backend

        backend = get_persistence_backend()
    except Exception:
        return None
    try:
        async with backend.transactional() as tx:
            if visibility is not None:
                # Use the visibility-filtered ABC path so the row is only
                # returned when the caller may read it. This collapses
                # "deleted" and "invisible" to None, which is the safe
                # direction for the SSE bridge.
                from mnemos.core.auth_context import UserContext
                from mnemos.persistence.visibility import VisibilityFilter

                user = UserContext(
                    user_id=visibility.user_id or "",
                    group_ids=visibility.group_ids,
                    role="user",
                    namespace=visibility.namespace,
                    authenticated=bool(visibility.user_id),
                )
                vfilter = VisibilityFilter.for_read(user, namespace=visibility.namespace)
                row = await backend.memories.get_memory(tx, memory_id, visibility=vfilter)
            else:
                row = await backend.memories.fetch_memory_by_id(tx, memory_id)
    except Exception as exc:
        logger.warning("NATS SSE row lookup failed for %s: %s", memory_id, exc)
        return None
    if row is None:
        # Negative caching is skipped for write events (``created`` and
        # ``updated``): a row that was just written may not be visible to
        # ``get_memory`` yet (commit/replication lag), and caching ``None``
        # would suppress every subsequent event for that id for up to the
        # full TTL even after the row exists. Only a class that is not
        # racing a write keeps the negative entry.
        if cache_negative:
            async with _sse_row_cache_lock:
                _sse_row_cache[key] = (None, now + _SSE_ROW_CACHE_TTL_SECONDS)
                _enforce_sse_row_cache_limit()
        return None
    if isinstance(row, Mapping):
        result = row
    else:
        try:
            result = dict(row)
        except (TypeError, ValueError):
            result = None
    async with _sse_row_cache_lock:
        _sse_row_cache[key] = (result, now + _SSE_ROW_CACHE_TTL_SECONDS)
        _enforce_sse_row_cache_limit()
    return result


def _nats_sse_event_payload(msg: Any) -> dict[str, Any]:
    """The JSON object a message body carries, or ``{}``."""
    try:
        payload = json.loads(_nats_msg_data(msg))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _namespace_gate(msg: Any, visibility: _SSERowVisibility) -> bool:
    """Whether the subject an event arrived on belongs to the caller.

    The namespace encoding is injective, so ``a.b`` and ``a_b`` map to
    different segments and the comparison below cannot be satisfied by two
    different namespaces.

    The subject an event arrives on is ``mnemos.<class>.<action>.<namespace>``
    (four segments, three dots), so the namespace is the LAST segment. Only
    subjects with exactly that shape naming the caller's own namespace pass;
    anything else is rejected. In particular a subject carrying ``*`` or ``>``
    can never carry a valid encoded namespace token (``_safe_subject_token``
    rejects them outside the operator path, and operators bypass this gate
    via the short-circuit above), so wildcarded subjects fail closed here
    rather than passing through to the row gate on a guess. Unknown shapes
    this bridge does not publish are likewise rejected, not deferred.
    """
    if visibility.bypass:
        return True
    subject = str(getattr(msg, "subject", "") or "")
    segments = subject.split(".")
    if len(segments) != 4 or segments[0] != "mnemos":
        return False
    return segments[3] == _safe_namespace(visibility.namespace)


async def _nats_sse_row_allowed(msg: Any, visibility: _SSERowVisibility) -> bool:
    """Whether this message may be delivered to the stream's caller.

    Two gates, both required:

    1. **Namespace gate** (:func:`_namespace_gate`). The subject
       the message arrived on must name the caller's own namespace, so
       ``a.b`` no longer shares a subject with ``a_b``.
    2. **Row gate.** Memory events summarize row ids + categories. Those are
       metadata about rows that may simply be *readable* (world/group/ACL)
       rather than owned, so each event's row is resolved through the same
       visibility predicate as the REST read path before anything is sent.

    Fail-closed: a memory-class message that names no row cannot be row-gated,
    so it is dropped. The row gate applies ONLY to the memory class and ONLY
    to the ``memory_id`` payload key -- a generic ``id`` field on any other
    class (consultation frames, control frames) is not a memory-row reference
    and must never be looked up in the memories table. Non-memory classes
    have no row to gate, so they are allowed once the namespace gate has
    pinned them to the caller's own namespace (e.g. namespace-scoped control
    frames).

    Two callers are exempt from the row gate, both for the same reason: there
    is no per-row authorization decision to make.

    * **Operators** (``visibility.bypass``) -- cross-tenant reads are the point
      of the role.
    * **The legacy shared-token principal** (``visibility.legacy_principal``) --
      an EXPLICIT flag set only by the trusted-network / shared-token edge:
      MCP auth is disabled, or the caller presented the single shared
      ``MNEMOS_MCP_TOKEN`` (which is not a valid REST API key), so the caller
      shares one backend identity and reads exactly what that identity reads
      over REST. Gating rows here would drop every non-world-readable event
      and silently regress trusted-network deployments. The namespace gate
      still applies.

      This flag is NEVER inferred from ``user_id`` being empty: a non-root
      caller with an unresolved identity (``role=admin``/``monitor`` with
      ``user_id=None``, or a token user whose OAuth lookup failed) carries
      ``user_id=""`` but ``legacy_principal=False``, and must fail closed
      through the row gate below rather than skipping it.

    Deletion notifications flow for ids the caller already knows: the payload
    ``memory_id`` on a delete is publisher-controlled and the vanished row
    offers no repository check, so per-row authorization comes from the
    bounded allow-cache -- the id must have been row-gated and allowed for
    this caller on a prior ``created``/``updated`` frame. Uncached delete ids
    fail closed. Because that decision never consults the row, the shipped
    frame must not carry row metadata beyond the id + namespace the caller
    already has; ``_nats_sse_data`` drops the category for delete frames
    accordingly.
    """
    if not _namespace_gate(msg, visibility):
        return False
    if visibility.bypass:
        return True
    if visibility.legacy_principal:
        # Explicit trusted-network / shared-token principal (see flag docs on
        # ``_SSERowVisibility``): the caller has no per-user REST identity --
        # MCP auth is disabled or it presented the single shared
        # MNEMOS_MCP_TOKEN -- so there is no per-row decision to make, and
        # every caller already shares the one backend identity that this
        # bridge can read over REST. The row gate is skipped ONLY on that
        # explicit flag, never on an empty ``user_id``: a non-root caller
        # whose identity was not resolved (e.g. role=admin/monitor with
        # user_id=None) falls through to the gate below and fails closed.
        return True

    event_class = str(getattr(msg, "subject", "") or "").split(".")[1:2]
    payload = _nats_sse_event_payload(msg)
    # The row gate is class-specific: only memory-class events reference a
    # row in the memories table, and only via the ``memory_id`` key. Applying
    # the lookup to every event class (or accepting a generic ``id`` key)
    # would miss and drop non-memory payloads that carry an unrelated ``id``.
    if event_class != ["memory"]:
        return True
    memory_id = payload.get("memory_id")
    if not memory_id:
        # Row-less message on a memory subject: nothing to gate on, so drop.
        # All current memory publishers (created/updated/deleted) carry the
        # id; the fail-closed direction for an authorization input is to
        # deny.
        return False

    memory_id = str(memory_id)
    action = str(getattr(msg, "subject", "") or "").split(".")[2:3] or [""]
    if action[0] == "deleted":
        # Deleted rows cannot be read back: the visibility-filtered lookup
        # returns None for them by design (it collapses "deleted" and
        # "invisible" on purpose). But the payload ``memory_id`` is
        # publisher-controlled -- any publisher can emit an arbitrary id on a
        # shared namespace subject (NATS federates the family, so origin is
        # not trustworthy). A frame for an id this caller was never shown
        # must not be delivered: it would disclose the id (and the
        # fact of deletion) to co-tenants who cannot read the row over REST.
        #
        # The namespace gate above already pins the subject to the caller's
        # own namespace. Per-row authorization for a delete therefore comes
        # from the bounded allow-cache: the id must have been row-gated and
        # allowed for THIS caller on a prior created/updated frame
        # (``_record_sse_delete_allowed``). Uncached ids fail closed; this is
        # the fail-closed direction for an authorization input, so a missing
        # allow entry -- stale epoch, first stream, or forged id -- is a drop,
        # never a hang or a delivery. The shipped frame still carries only
        # the id + namespace (``_nats_sse_data`` drops the category).
        return _sse_delete_allowed(memory_id, visibility)

    # Bounded lookup: a hung backend must not stall the stream. A timeout
    # (or any failure inside ``_fetch_sse_row``) yields None, which the
    # fail-closed check below turns into a drop -- never a hang.
    #
    # A miss for a ``created`` OR ``updated`` event is a write that just
    # happened, so it races read-replica/commit lag the same way: the row may
    # not be visible yet. Negative-caching that miss would suppress every
    # subsequent event for the id for a full TTL even after the write lands,
    # so it is skipped for both mutation classes.
    try:
        row = await asyncio.wait_for(
            _fetch_sse_row(
                memory_id,
                visibility,
                cache_negative=action[0] not in ("created", "updated"),
            ),
            timeout=_SSE_ROW_LOOKUP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        row = None
    if row is None:
        # No such row: either it was deleted, or it is invisible to this
        # caller -- the visibility-filtered repository lookup collapses both to
        # ``None`` on purpose, so the bridge cannot distinguish them. The
        # row gate below already withholds the ids and categories of other
        # users' rows whenever the row IS resolvable, and the events whose
        # row has vanished carry nothing beyond the namespace (already
        # gated) plus the vanished id. Returning False here fails closed:
        # a missing authorization decision is never an allowed event.
        return False
    if not visibility.allows_row(row):
        return False
    # This caller was just allowed a row-gated frame for ``memory_id``.
    # Remember it so a later ``memory.deleted`` event for the same row can be
    # authorized against the bounded allow-cache instead of shipping an
    # id the caller was never shown (see the ``deleted`` branch above).
    _record_sse_delete_allowed(memory_id, visibility)
    return True


async def _subscribe_nats_sse_subject(js: Any, subject: str) -> Any:
    """Subscribe an MCP SSE bridge to a NATS subject.

    LIVE-ONLY telemetry contract. Both branches deliberately give
    up JetStream durability/replay semantics:

      * Core-NATS path (``js._nc.subscribe``) — used when the
        underlying connection exposes the core NATS handle. Pure
        live pub/sub: messages published while no SSE client is
        connected are dropped by the broker, no replay window, no
        delivery acks.
      * JetStream path — uses ``DeliverPolicy.NEW`` +
        ``AckPolicy.NONE``: each subscriber starts at the live
        edge, does NOT replay the 30-day stream backlog, and
        does NOT ack messages back to the broker. So even though
        this branch goes through JetStream API, the SSE bridge
        is configured to behave like a live-only feed.

    Operator expectations: ``GET /sse?subjects=...`` is a
    real-time telemetry stream for SSE clients that are
    connected RIGHT NOW. It is NOT a replay-able audit log; for
    historical reads use the HTTP REST surface (``GET /v1/
    memories/...``, ``GET /v1/federation/feed``, etc.) which
    runs through the visibility-gated repository path.

    Codex round-10 of the v4.2-NATS corpus review surfaced the
    expectation gap: a docs-aware operator might assume a
    NATS-backed SSE over MNEMOS events implied JetStream
    semantics. This docstring + ``docs/NATS_OPERATIONS.md``
    "MCP event bridge" section make the live-only contract
    explicit.
    """
    nc = getattr(js, "_nc", None)
    subscribe = getattr(nc, "subscribe", None)
    if subscribe is not None:
        return await subscribe(subject)

    try:
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy  # type: ignore

        config = ConsumerConfig(
            deliver_policy=DeliverPolicy.NEW,
            ack_policy=AckPolicy.NONE,
        )
    except ImportError:
        config = None
    return await js.subscribe(subject, config=config)


def _is_nats_sse_timeout(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return exc.__class__.__name__ == "TimeoutError" and exc.__class__.__module__.startswith("nats")


async def _unsubscribe_nats_sse(sub: Any) -> None:
    unsubscribe = getattr(sub, "unsubscribe", None)
    if unsubscribe is None:
        return
    result = unsubscribe()
    if hasattr(result, "__await__"):
        await result


async def _nats_sse_event_source(
    subscriptions: list[Any],
    visibility: _SSERowVisibility | None = None,
):
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=NATS_SSE_QUEUE_MAXSIZE)
    dropped_total = 0

    def enqueue(kind: str, item: Any) -> int:
        nonlocal dropped_total
        try:
            queue.put_nowait((kind, item))
            return dropped_total
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            dropped_total += 1
            queue.put_nowait((kind, item))
            return dropped_total

    async def pump(sub: Any) -> None:
        while True:
            try:
                msg = await sub.next_msg(timeout=1)
                dropped = enqueue("message", msg)
                if dropped:
                    enqueue("dropped", dropped)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_nats_sse_timeout(exc):
                    continue
                enqueue("error", exc)
                return

    tasks = [asyncio.create_task(pump(sub)) for sub in subscriptions]
    consecutive_drops = 0
    try:
        while True:
            kind, item = await queue.get()
            if kind == "error":
                logger.warning("NATS SSE stream closing after subscription error: %s", item)
                yield _sse_frame(
                    "error",
                    json.dumps({"error": "nats_connection_lost", "detail": str(item)}),
                )
                return
            if kind == "dropped":
                consecutive_drops += 1
                yield _sse_frame("dropped", json.dumps({"count": item}))
                if consecutive_drops >= NATS_SSE_MAX_CONSECUTIVE_DROPS:
                    yield _sse_frame("error", json.dumps({"reason": "lagging"}))
                    return
                continue
            consecutive_drops = 0
            if visibility is not None and not await _nats_sse_row_allowed(item, visibility):
                continue
            yield _sse_frame(getattr(item, "subject", "mnemos.event"), _nats_sse_data(item))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(_unsubscribe_nats_sse(sub) for sub in subscriptions),
            return_exceptions=True,
        )


async def handle_nats_event_stream(request):
    """Expose MNEMOS bus events as a thin NATS-to-SSE bridge.

    This is intentionally separate from `/sse`, which is the MCP SDK's
    bidirectional protocol transport.
    """
    if StreamingResponse is None:
        return PlainTextResponse("streaming responses unavailable", status_code=503)

    try:
        try:
            context = await _resolve_mcp_user_context(request)
        except PermissionError:
            return PlainTextResponse("unauthorized", status_code=403)
        subjects = _parse_nats_sse_subjects(request, context)
        # One envelope for the whole stream: operators are exempt from both
        # gates (explicit subject filters + cross-tenant reads are the point
        # of the operator role), everyone else is bounded by the same
        # VisibilityFilter the REST read paths render.
        visibility = None if _is_operator_context(context) else _cached_row_visibility(context)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)

    from mnemos.nats.client import get_jetstream

    js = get_jetstream()
    if js is None:
        return PlainTextResponse("NATS unavailable", status_code=503)

    subscriptions: list[Any] = []
    try:
        for subject in subjects:
            subscriptions.append(await _subscribe_nats_sse_subject(js, subject))
    except Exception as exc:
        await asyncio.gather(
            *(_unsubscribe_nats_sse(sub) for sub in subscriptions),
            return_exceptions=True,
        )
        logger.warning("NATS SSE subscription failed for subjects=%s: %s", subjects, exc)
        return PlainTextResponse("NATS unavailable", status_code=503)

    return StreamingResponse(
        _nats_sse_event_source(subscriptions, visibility),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def healthz(_request):
    """Readiness probe. Skips bearer auth so deployment infra
    (cloudflared, k8s) can confirm the process is up without
    needing to share the token."""
    return PlainTextResponse("ok")


async def _drain_audit_tasks_on_shutdown() -> None:
    """Round-3 residual #2 of #146 (#149): drain in-flight MCP audit
    persist tasks before the loop closes.

    Without this, the SSE bridge can deliver tool results and the
    Starlette teardown can cancel outstanding fire-and-forget audit
    tasks before the HTTP POST completes.
    """
    from mnemos.mcp.tools._security import drain_pending_audit_tasks

    try:
        drained = await drain_pending_audit_tasks(timeout=5.0)
        if drained:
            logger.info(
                "drained %d pending mcp_audit_log persist task(s) on shutdown",
                drained,
            )
    except Exception:
        # Drain failures must NOT propagate through Starlette
        # shutdown; the underlying logger entry is the always-on
        # surface and the dropped row is recoverable from logs.
        logger.exception("mcp_audit drain on http shutdown failed")


#: The backend this process registered as the lifecycle backend, so shutdown
#: only clears a registration it made.
_registered_ledger_backend = None


def _budget_cap_configured(settings) -> bool:
    knemon = getattr(settings, "knemon", None)
    if knemon is None:
        return False
    try:
        weekly = float(getattr(knemon, "weekly_budget_cap_usd", 0) or 0)
    except (TypeError, ValueError):
        weekly = 0.0
    parse = getattr(knemon, "parsed_provider_budget_caps_usd", None)
    provider_caps = parse() if callable(parse) else {}
    return weekly > 0 or bool(provider_caps)


async def _register_budget_ledger(_app: Starlette, settings, reusable_backend):
    """Register persistence for standalone tools and the shared budget ledger."""
    global _registered_ledger_backend
    from mnemos.core import lifecycle

    if lifecycle._persistence_backend is not None:
        return None
    backend = reusable_backend or getattr(_app.state, "persistence_backend", None)
    opened = None
    if backend is None:
        _backend_type, backend = await lifecycle.build_configured_persistence_backend(settings)
        opened = backend
    lifecycle._persistence_backend = backend
    _registered_ledger_backend = backend
    logger.info("KNEMON budget ledger for MCP GRAEAE wired to %s.", type(backend).__name__)
    return opened


def _unregister_budget_ledger() -> None:
    global _registered_ledger_backend
    from mnemos.core import lifecycle

    if _registered_ledger_backend is not None and lifecycle._persistence_backend is _registered_ledger_backend:
        lifecycle._persistence_backend = None
    _registered_ledger_backend = None


@asynccontextmanager
async def _mcp_http_lifespan(_app: Starlette):
    """Use the node's shared backend for durable OAuth state.

    Embedded apps borrow an existing backend; standalone MCP apps open it
    through the same factory as the REST lifecycle. Only owned backends are
    closed here. OAuth initialization failures abort startup before serving.
    """
    from mnemos.mcp import oauth as oauth_module

    owned_backend = None
    previous_oauth_service = oauth_module._service
    registered_oauth_service = None
    attached_backend = None
    registered_backend = None
    settings = get_settings()
    streamable_mcp._closing = False
    try:
        if settings.oauth.database_url.strip():
            raise RuntimeError(
                "MNEMOS_OAUTH_DATABASE_URL has been removed. Configure "
                "MNEMOS_DATABASE_DSN for the node's shared persistence backend "
                "and unset MNEMOS_OAUTH_DATABASE_URL."
            )
        if settings.oauth.issuer.strip():
            from mnemos.core.lifecycle import build_configured_persistence_backend, get_persistence_backend

            backend = getattr(_app.state, "persistence_backend", None)
            if backend is None:
                try:
                    backend = get_persistence_backend()
                except HTTPException as exc:
                    if exc.status_code != 503:
                        raise
                    _backend_type, backend = await build_configured_persistence_backend(settings)
                    owned_backend = backend
            if not isinstance(backend, OAuthPersistence):
                raise RuntimeError("Configured persistence backend does not support OAuthPersistence")
            if getattr(_app.state, "persistence_backend", None) is None:
                attached_backend = backend
                _app.state.persistence_backend = backend
            store = oauth_module.PersistenceOAuthStore(backend)
            signing_key = settings.oauth.signing_key or await store.get_signing_key()
            if not signing_key:
                await store.save_signing_key(key_id="default", signing_key=secrets.token_urlsafe(32))
                signing_key = await store.get_signing_key()
                if not signing_key:
                    raise RuntimeError("failed to persist the MCP OAuth signing key")
                logger.info("OAuth signing key persisted in the configured backend on first boot.")
            service = oauth_module.OAuthService(
                base_url=settings.oauth.issuer,
                signing_key=signing_key,
                store=store,
                registration_secret=settings.oauth.registration_secret,
                admin_passphrase=settings.oauth.admin_passphrase,
            )
            oauth_module.set_oauth_service(service)
            registered_oauth_service = service
            logger.info("OAuth store wired to %s (MNEMOS_DATABASE_DSN).", type(backend).__name__)
        from mnemos.core import lifecycle

        prior_backend = lifecycle._persistence_backend
        ledger_backend = await _register_budget_ledger(_app, settings, owned_backend)
        if prior_backend is None:
            registered_backend = lifecycle._persistence_backend
        if ledger_backend is not None and owned_backend is None:
            owned_backend = ledger_backend
        yield
    finally:
        await streamable_mcp.shutdown()
        sse_tasks = list(_sse_tasks)
        for task in sse_tasks:
            task.cancel()
        if sse_tasks:
            await asyncio.gather(*sse_tasks, return_exceptions=True)
        await _drain_audit_tasks_on_shutdown()
        if registered_backend is not None and _registered_ledger_backend is registered_backend:
            _unregister_budget_ledger()
        if registered_oauth_service is not None and oauth_module._service is registered_oauth_service:
            oauth_module.set_oauth_service(previous_oauth_service)
        try:
            if owned_backend is not None:
                await owned_backend.close()
        finally:
            if attached_backend is not None and getattr(_app.state, "persistence_backend", None) is attached_backend:
                del _app.state.persistence_backend


starlette_app = Starlette(
    routes=[
        Route("/health", endpoint=healthz),
        Route("/healthz", endpoint=healthz),
        Route("/.well-known/oauth-authorization-server", endpoint=metadata_authorization),
        Route("/.well-known/oauth-protected-resource", endpoint=metadata_resource),
        Route("/oauth/authorize", endpoint=authorize_route, methods=["GET"]),
        Route("/oauth/authorize", endpoint=authorize_post_route, methods=["POST"]),
        Route("/oauth/token", endpoint=token_route, methods=["POST"]),
        Route("/oauth/register", endpoint=register_route, methods=["POST"]),
        _RawASGIRoute("/mcp", streamable_mcp, {"POST", "GET", "DELETE"}),
        _RawASGIRoute("/sse", handle_sse_asgi, {"GET"}),
        Route(NATS_SSE_PATH, endpoint=handle_nats_event_stream),
        Mount("/messages/", app=handle_post_message),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
    lifespan=_mcp_http_lifespan,
)


def main() -> None:
    p = argparse.ArgumentParser(description="MNEMOS MCP HTTP/SSE server")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 if running behind a tunnel/proxy that shares the box)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=5004,
        help="Listen port (default: 5004 — alongside MNEMOS API on 5002, GRAEAE on 5002, federation on 5002)",
    )
    args = p.parse_args()

    logger.info("MNEMOS MCP HTTP/SSE listening on %s:%d", args.host, args.port)
    if MCP_AUTH_ENABLED:
        logger.info("Bearer principals configured (count=%d)", len(TOKEN_PRINCIPALS))
    else:
        logger.info("Bearer auth disabled; all callers share the legacy single-token identity")
    logger.info("MNEMOS backend: %s", get_settings().server.base)
    uvicorn.run(starlette_app, host=args.host, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()


_register_epoch_listener()
