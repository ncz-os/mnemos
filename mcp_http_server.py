#!/usr/bin/env python3
"""MNEMOS MCP HTTP/SSE server — for ChatGPT Pro Developer Mode + any
remote MCP client that needs an HTTPS URL instead of a stdio process.

Reuses the same `Server("mnemos")` instance + every tool definition from
mcp_server.py. The only difference is the transport: stdio framing vs
SSE-over-HTTP framing. Tool surface is identical, so a memory written
from Claude Desktop (stdio) is queryable from ChatGPT Pro (SSE) and
vice versa.

Auth: bearer token. The connector caller MUST send
  Authorization: Bearer <MNEMOS_MCP_TOKEN>
on the SSE handshake. Tokens are configured via env var; rotate by
restarting the daemon. Future iterations may add per-user OAuth +
audit-trail attribution; v1 is a single shared token because that's
what ChatGPT Developer Mode's "Custom connector → bearer auth" UX
asks for and we don't want to add ceremony before validating fit.

Transport security: TLS terminated upstream (Cloudflare Tunnel,
Tailscale Funnel, Caddy/nginx). This process listens on a local
HTTP port; the public URL is opaque to it.

Run:
  MNEMOS_MCP_TOKEN=<token>  \
  MNEMOS_BASE=http://localhost:5002  \
  MNEMOS_API_KEY=<mnemos-bearer>  \
  python3 mcp_http_server.py --host 127.0.0.1 --port 5004
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from mcp.server.sse import SseServerTransport

# Reuse the exact same Server instance + tool registrations from
# the stdio entry point. Importing for the side effect of having
# tools registered against `app`.
from mcp_server import app  # noqa: F401 (used by handle_sse below)

# stderr logging — matches mcp_server.py convention so log shipping
# from container stdout/stderr stays consistent.
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] mcp_http: %(message)s")
logger = logging.getLogger(__name__)


def _required_token() -> str:
    """Required bearer token. We refuse to start without one — opening
    an MCP server with full memory write access on the public internet
    behind no auth is exactly the configuration that gets a project
    on the front page of HN for the wrong reason."""
    tok = os.getenv("MNEMOS_MCP_TOKEN", "").strip()
    if not tok:
        sys.stderr.write(
            "FATAL: MNEMOS_MCP_TOKEN must be set. Refusing to expose the\n"
            "MCP server without bearer auth. Generate a token (e.g. via\n"
            "`openssl rand -hex 32`), set it in the environment, and\n"
            "configure the same token in the connector caller.\n"
        )
        sys.exit(2)
    return tok


REQUIRED_TOKEN = _required_token()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate `Authorization: Bearer <token>` on every request before
    the SSE handshake or the POST-message endpoint sees it. Reject
    everything else with 401 + a `WWW-Authenticate` header so the
    client knows what scheme to use."""

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnemos-mcp"'},
            )
        presented = auth.split(" ", 1)[1].strip()
        if presented != REQUIRED_TOKEN:
            return JSONResponse(
                {"error": "invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnemos-mcp"'},
            )
        return await call_next(request)


sse = SseServerTransport("/messages/")


async def handle_sse(request):
    """Open an SSE stream and pump MCP frames over it. The transport
    object owns the bidirectional plumbing; we just hand it the
    stream pair the ASGI runtime gave us."""
    # Starlette exposes the underlying ASGI send via a private attr on
    # request; the SDK examples accept this trade for now.
    async with sse.connect_sse(
        request.scope, request.receive, request._send,
    ) as streams:
        read_stream, write_stream = streams
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


async def healthz(_request):
    """Readiness probe. Skips bearer auth so deployment infra
    (cloudflared, k8s) can confirm the process is up without
    needing to share the token."""
    return PlainTextResponse("ok")


starlette_app = Starlette(
    routes=[
        Route("/healthz", endpoint=healthz),
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
)


def main() -> None:
    p = argparse.ArgumentParser(description="MNEMOS MCP HTTP/SSE server")
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address (default: 127.0.0.1; use 0.0.0.0 if "
                        "running behind a tunnel/proxy that shares the box)")
    p.add_argument("--port", type=int, default=5004,
                   help="Listen port (default: 5004 — alongside MNEMOS API "
                        "on 5002, GRAEAE on 5002, federation on 5002)")
    args = p.parse_args()

    logger.info("MNEMOS MCP HTTP/SSE listening on %s:%d", args.host, args.port)
    logger.info("Bearer token configured (length=%d)", len(REQUIRED_TOKEN))
    logger.info("MNEMOS backend: %s", os.getenv("MNEMOS_BASE",
                                                 "http://localhost:5002"))
    uvicorn.run(starlette_app, host=args.host, port=args.port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
