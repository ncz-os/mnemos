"""Request-ID middleware + log correlation (v3.2 observability).

Assigns a unique ID to every incoming request, binds it to a
ContextVar for the request's lifetime, emits it in the X-Request-ID
response header, and injects it into every log record produced during
the request via a logging.Filter.

Foundation commit: Prometheus /metrics, OpenTelemetry tracing, and
structlog integration follow in their own commits and reuse the
`current_request_id()` accessor defined here.

## Usage

    from api.observability import RequestIDMiddleware, current_request_id

    app.add_middleware(RequestIDMiddleware)

Any code path reached during a request can call
`current_request_id()` to get the correlating UUID. Outside a request
context, it returns None.

## Callers bringing their own correlation ID

If the inbound request carries an `X-Request-ID` header, we honor it
verbatim so correlation survives across a load balancer / gateway
hop. Validation is minimal — we strip to 128 ASCII printables and
cap length at 128 chars, defensive against an adversary trying to
inject control characters or blow up log lines. If the inbound header
is absent or fails validation, we generate a fresh UUID4 hex.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# The header name operators see and can override from upstream proxies.
REQUEST_ID_HEADER = "X-Request-ID"

# Max length we accept for an incoming correlation ID. Anything longer
# is discarded and replaced with a fresh UUID — keeps log lines
# bounded even when a caller passes an absurdly long header.
_MAX_INBOUND_LENGTH = 128

# Only ASCII printables (no whitespace / no control chars). Prevents
# an adversary from smuggling newlines into logs via X-Request-ID.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


# ContextVar is task-local — asyncio tasks spawned from within a
# request inherit the bound value. Good enough for FastAPI's default
# "per-request is one task" shape.
_request_id_ctx: ContextVar[Optional[str]] = ContextVar("mnemos_request_id", default=None)


def current_request_id() -> Optional[str]:
    """Return the request_id bound to the current async context, or
    None if we're outside a request (e.g. module import, background
    worker tick)."""
    return _request_id_ctx.get()


def _validate_inbound(value: str) -> Optional[str]:
    """Accept the inbound ID only if it's reasonably shaped.
    Returns the sanitized value or None to indicate "generate fresh"."""
    if not value:
        return None
    value = value.strip()
    if not value or len(value) > _MAX_INBOUND_LENGTH:
        return None
    if not _SAFE_ID_RE.match(value):
        return None
    return value


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that binds a request ID for the lifetime
    of every HTTP request.

    Preferred ID source order:
      1. Inbound X-Request-ID header (validated)
      2. Fresh uuid4().hex

    The bound value is:
      * stored in the `_request_id_ctx` ContextVar — visible to any
        code in the request's task tree via `current_request_id()`
      * echoed to the client in the X-Request-ID response header
      * picked up by `_RequestIDLogFilter` for every log record
        produced during the request

    We intentionally do NOT stash the ID on request.state. Starlette's
    BaseHTTPMiddleware creates a distinct Request object for its
    dispatch, so a state setter here would not propagate to the
    handler's Request. The ContextVar is the single source of truth;
    handlers that need the ID should call `current_request_id()`.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = _validate_inbound(inbound) or uuid.uuid4().hex

        token = _request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _request_id_ctx.reset(token)

        # Echo the ID so the client can cite it when reporting an issue.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class _RequestIDLogFilter(logging.Filter):
    """Attaches the current request_id (or '-' outside a request) to
    every log record as `record.request_id`. Configure the root
    formatter to print `%(request_id)s` to surface it in log lines.

    Attached by `install_log_correlation()` to the root logger — every
    handler already in place picks up the filter automatically
    because logging.Filter walks up the hierarchy.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


def install_log_correlation(
    fmt: Optional[str] = None,
    *,
    replace_root_formatter: bool = True,
) -> None:
    """Attach `_RequestIDLogFilter` to the root logger and optionally
    replace the root handler's formatter with one that prints
    request_id. Call once at startup, before any handlers fire.

    `fmt` overrides the default format string. Default matches the
    pre-v3.2 MNEMOS format plus a [req:<id>] segment so existing log
    parsers stay compatible.
    """
    if fmt is None:
        fmt = (
            "%(asctime)s [%(levelname)s] [req:%(request_id)s] "
            "%(name)s: %(message)s"
        )

    root = logging.getLogger()
    filt = _RequestIDLogFilter()
    # Only install once — idempotent on repeated calls (dev-server reloads).
    if not any(isinstance(f, _RequestIDLogFilter) for f in root.filters):
        root.addFilter(filt)

    if replace_root_formatter:
        formatter = logging.Formatter(fmt)
        for handler in root.handlers:
            handler.addFilter(filt)
            handler.setFormatter(formatter)


__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIDMiddleware",
    "current_request_id",
    "install_log_correlation",
]
