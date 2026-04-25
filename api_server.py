"""MNEMOS API Server v3.0.0 — unified service with consultations + providers + OpenAI-compat gateway."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.rate_limit import (
    limiter,
    SlowAPIMiddleware,
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
)

from api.lifecycle import lifespan
from api.handlers.health import router as health_router
from api.handlers.consultations import router as consultations_router
from api.handlers.providers import router as providers_router
from api.handlers.memories import router as memories_router
from api.handlers.ingest import router as ingest_router
from api.handlers.kg import router as kg_router
from api.handlers.portability import router as portability_router
from api.handlers.admin import router as admin_router
from api.handlers.versions import router as versions_router
from api.handlers.journal import router as journal_router
from api.handlers.state import router as state_router
from api.handlers.entities import router as entities_router
from api.handlers.openai_compat import router as openai_compat_router
from api.handlers.sessions import router as sessions_router
from api.handlers.dag import router as dag_router
from api.handlers.webhooks import router as webhooks_router
from api.handlers.oauth import router as oauth_router
from api.handlers.federation import router as federation_router
from api.handlers.narrate import router as narrate_router

try:
    from api.handlers.document_import import router as document_import_router
    _document_import_available = True
except ImportError:
    _document_import_available = False
    document_import_router = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

# v3.2 observability foundation: request-ID correlation across logs +
# response headers. Must run BEFORE any handler emits log records so
# each line is tagged with [req:<id>] from the first one.
from api.observability import (  # noqa: E402
    PrometheusMiddleware,
    RequestIDMiddleware,
    TracingMiddleware,
    install_log_correlation,
    install_structured_logging,
    install_tracing,
    metrics_router,
)
install_log_correlation()
install_tracing()  # no-op unless opentelemetry is installed
# Structured JSON logs are OPT-IN via env var — enabling changes
# every log line's shape and would break operators whose log
# parsers expect the default format. Without `structlog` installed,
# or without the env flag set, the standard formatter (with
# [req:<id>]) is used.
if os.getenv("MNEMOS_STRUCTURED_LOGS", "").lower() in ("1", "true", "yes"):
    install_structured_logging()

from _version import __version__ as _MNEMOS_VERSION  # noqa: E402

app = FastAPI(title="MNEMOS API", version=_MNEMOS_VERSION, description="Unified service: GRAEAE consultations + MNEMOS memory + multi-provider inference gateway", lifespan=lifespan)

# ── Request body size limit (SEC-04) ──────────────────────────────────────────
# Default 5 MB. Override via MAX_BODY_BYTES env var.
# Implemented as a pure ASGI middleware (not BaseHTTPMiddleware) so we can
# reject oversized bodies as they stream in, including requests that use
# Transfer-Encoding: chunked and omit Content-Length. The previous
# BaseHTTPMiddleware version only inspected Content-Length and was bypassed
# by chunked uploads, which Starlette then buffered into memory unbounded.
_MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(5 * 1024 * 1024)))


class _BodySizeLimitASGI:
    """Reject HTTP requests whose body exceeds MAX_BODY_BYTES.

    Works for both Content-Length-declared and chunked uploads: we intercept
    `http.request` messages as they stream past and short-circuit with 413
    as soon as the running byte count exceeds the limit.
    """
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PATCH", "PUT"):
            await self.app(scope, receive, send)
            return

        # Fast-path: trust a declared Content-Length.
        headers = dict(scope.get("headers") or [])
        cl_bytes = headers.get(b"content-length")
        if cl_bytes is not None:
            try:
                if int(cl_bytes) > self.max_bytes:
                    await self._send_413(send)
                    return
            except ValueError:
                pass  # malformed CL, fall through to streaming check

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body") or b""
                received += len(body)
                if received > self.max_bytes:
                    # Drain any remaining body so the client doesn't hang,
                    # then signal the app via a closed channel.
                    while message.get("more_body"):
                        message = await receive()
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._send_413(send)

    async def _send_413(self, send):
        msg = f'{{"detail":"Request body exceeds {self.max_bytes // 1024 // 1024} MB limit"}}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(msg)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": msg.encode("utf-8"),
        })


class _BodyTooLarge(Exception):
    """Internal signal used by _BodySizeLimitASGI to short-circuit."""


# ── Middleware stack (LIFO: last add_middleware = outermost on the wire) ───
#
# Desired evaluation order on an incoming request (outer → inner):
#
#   RequestIDMiddleware      bind request_id ContextVar BEFORE anything logs
#     CORSMiddleware         preflight + CORS headers on every response
#       SessionMiddleware    authlib OAuth-state cookie for /oauth/*
#         SlowAPIMiddleware  rate-limit rejections tagged with request_id
#           TracingMiddleware  span reads current_request_id() into attrs
#             PrometheusMiddleware  histogram tagged
#               _BodySizeLimitASGI  413 for oversized bodies (innermost)
#                 <handler>
#
# Codex v3.2 re-audit found that the earlier version added
# RequestIDMiddleware BEFORE SlowAPI / Session / CORS, which under LIFO
# makes it INNER to all three — so a 429 from the rate limiter, a CORS
# rejection, or an OAuth session decode would log with no request_id.
# Fix: add RequestIDMiddleware LAST so it's truly outermost.

app.add_middleware(_BodySizeLimitASGI, max_bytes=_MAX_BODY_BYTES)
app.add_middleware(PrometheusMiddleware)
app.add_middleware(TracingMiddleware)

# Rate limiting (opt-in via RATE_LIMIT_ENABLED=true — see api/rate_limit.py)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Starlette SessionMiddleware — required by authlib for OAuth state (PKCE verifier,
# CSRF nonce) carried across the authorize -> callback redirect. This cookie is
# DIFFERENT from the application session cookie set after successful login.
#
# IMPORTANT: set MNEMOS_SESSION_SECRET to a stable value in production. When
# unset we generate a random one at startup, which invalidates any in-flight
# OAuth login on every server restart (the 10-min redirect roundtrip breaks).
import os as _os
import secrets as _secrets
from starlette.middleware.sessions import SessionMiddleware as _SessionMiddleware
_oauth_state_secret = _os.environ.get('MNEMOS_SESSION_SECRET')
if not _oauth_state_secret:
    logging.getLogger(__name__).warning(
        "MNEMOS_SESSION_SECRET is not set — generating a random key for this "
        "process. In-flight OAuth logins will break on restart. Set a stable "
        "value in your environment for production."
    )
    _oauth_state_secret = _secrets.token_urlsafe(48)
app.add_middleware(
    _SessionMiddleware,
    secret_key=_oauth_state_secret,
    session_cookie='mnemos_oauth_state',
    max_age=600,  # 10 minutes — just for the redirect roundtrip
    same_site='lax',
    https_only=False,  # set MNEMOS_SESSION_HTTPS_ONLY=1 to harden in prod
)

# CORS: set CORS_ORIGINS env var to restrict in production (comma-separated list).
# Defaults to "*" for local dev. Example: CORS_ORIGINS=https://app.example.com
_cors_origins_raw = os.getenv("CORS_ORIGINS", "http://localhost,http://127.0.0.1,http://127.0.0.1:5002,http://localhost:5002")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=_cors_origins != ["*"],
)

# RequestIDMiddleware MUST be the final add_middleware call so it ends up
# outermost under Starlette LIFO. See the stack diagram above.
app.add_middleware(RequestIDMiddleware)

app.include_router(health_router)
app.include_router(metrics_router)  # v3.2 observability: Prometheus /metrics
app.include_router(consultations_router)  # v3.0.0: Unified /v1/consultations (GRAEAE reasoning)
app.include_router(providers_router)  # v3.0.0: Unified /v1/providers (model routing)
app.include_router(openai_compat_router)  # Phase 0: OpenAI-compatible gateway
app.include_router(sessions_router)  # Phase 0: Session management for stateful chat
app.include_router(dag_router)  # Phase 3: DAG versioning (git-like)
app.include_router(webhooks_router)  # v3.0.0: Outbound webhook subscriptions
app.include_router(oauth_router)  # v3.0.0: OAuth/OIDC browser login
app.include_router(federation_router)  # v3.0.0: Cross-instance memory federation
app.include_router(memories_router)
app.include_router(narrate_router)  # v3.3 S-II: APOLLO dense-form narration
app.include_router(ingest_router)
app.include_router(kg_router)
app.include_router(portability_router)  # v3.2: /v1/export + /v1/import (MPF v0.1)
app.include_router(admin_router)
app.include_router(versions_router)
app.include_router(journal_router)
app.include_router(state_router)
app.include_router(entities_router)

# Document import (Docling) — optional, requires docling extra
if _document_import_available:
    app.include_router(document_import_router)

if __name__ == "__main__":
    import uvicorn
    # workers=1 is required: GRAEAE circuit breakers, rate limiters, and semaphores
    # are in-process state. Multiple workers each get their own copy and will not
    # share limits. Use MNEMOS_PORT env var to override (default: 5002).
    port = int(os.getenv("MNEMOS_PORT", "5002"))
    host = os.getenv("MNEMOS_BIND", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, workers=1)
