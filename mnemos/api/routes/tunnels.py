"""MNEMOS admin endpoints for public tunnels (root only, opt-in).

Implements the REST contract that ``scripts/mnemos_tunnel_setup.py`` has
been written against since v5.0.1:

* ``POST   /admin/tunnels/start``  — open a tunnel, return ``{url, token}``
* ``GET    /admin/tunnels/status`` — is one running, and where
* ``DELETE /admin/tunnels/stop``   — tear it down

Auth posture. These routes take ``Depends(require_root)``, the same
dependency every other ``/admin/*`` route in this package uses
(``admin.py``, ``admin_decay.py``) — there is no second auth mechanism
here. On top of that they are gated behind ``MNEMOS_TUNNELS_ENABLED``,
which defaults to false, because this is the one admin route whose effect
is "make this instance reachable from the public internet". Root auth
alone would mean a leaked root API key is sufficient to expose the whole
node; requiring a host-level flag means the operator running the daemon
has to have agreed to it too.

Token. The ``token`` returned alongside the URL is NOT minted by a new
token system. It is, in order of preference, an OAuth 2.1 access token
from the MCP edge's existing :class:`~mnemos.mcp.oauth.OAuthService`, or
the already-configured static ``MNEMOS_MCP_TOKEN``. Those are exactly the
two credentials ``mnemos/mcp/http.py``'s ``BearerAuthMiddleware`` accepts,
so the token handed to the connector is one the tunnel's target will
actually honour. Anything else would look like it worked and 401 on first
use.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mnemos.api.dependencies import UserContext, require_root
from mnemos.core.config import get_settings
from mnemos.tunnels import (
    BACKEND_NAMES,
    DEFAULT_BACKEND,
    TunnelAuthError,
    TunnelBinaryMissingError,
    TunnelBridge,
    TunnelError,
    get_bridge_class,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/tunnels", tags=["admin", "tunnels"])

#: Lifetime of an issued OAuth tunnel token. The MCP edge's interactive
#: default (30 min) is wrong here: a connector registered in ChatGPT is
#: expected to keep working for a session's worth of use, and re-pasting a
#: token every half hour is the friction this whole helper exists to
#: remove. Still bounded — a tunnel token is not a permanent credential.
TUNNEL_TOKEN_SECONDS = 12 * 60 * 60

#: Default target: the MCP HTTP/SSE edge, matching the script's contract.
DEFAULT_TARGET_PORT = 5004

# One tunnel per process. The REST contract carries no tunnel id
# (``/stop`` takes no argument), so a second concurrent tunnel would be
# unaddressable and un-stoppable through this API.
_active_bridge: Optional[TunnelBridge] = None
_lock = asyncio.Lock()


# ── models ───────────────────────────────────────────────────────────────


class TunnelStartRequest(BaseModel):
    backend: Literal["cloudflare", "ngrok"] = Field(
        DEFAULT_BACKEND,
        description="Tunnel provider. 'cloudflare' needs no account; 'ngrok' needs an authtoken.",
    )
    authtoken: Optional[str] = Field(
        None,
        description=(
            "Provider credential. Required for ngrok unless the host has already run "
            "`ngrok config add-authtoken`. Ignored for cloudflare quick tunnels, which "
            "are anonymous."
        ),
    )
    target_port: int = Field(
        DEFAULT_TARGET_PORT,
        ge=1,
        le=65535,
        description="Local port to publish. Always bound to loopback on this host.",
    )


class TunnelStartResponse(BaseModel):
    url: str
    token: str
    token_source: Literal["oauth", "static"]
    backend: str
    target_port: int
    pid: int
    started_at: str
    expires_in: Optional[int] = Field(
        None,
        description="Seconds until the token expires; null for a static token, which does not.",
    )


class TunnelStatusResponse(BaseModel):
    running: bool
    backend: Optional[str] = None
    url: Optional[str] = None
    target_port: Optional[int] = None
    pid: Optional[int] = None
    started_at: Optional[str] = None


class TunnelStopResponse(BaseModel):
    stopped: bool = Field(description="True if a tunnel was running and has been torn down.")
    backend: Optional[str] = None


# ── helpers ──────────────────────────────────────────────────────────────


def _require_enabled() -> None:
    """403 unless the operator opted this host in. See module docstring."""
    if not get_settings().mcp.tunnels_enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Tunnel management is disabled on this host. These routes publish a "
                "local MNEMOS port to the public internet, so they require a host-level "
                "opt-in in addition to root auth. Set MNEMOS_TUNNELS_ENABLED=true (or "
                "`tunnels_enabled = true` under [mcp] in config.toml) and restart MNEMOS."
            ),
        )


def _oauth_service():
    """Return the MCP edge's OAuth service, or raise if unconfigured.

    A named seam rather than an inline import: ``mnemos.mcp.oauth`` is
    imported lazily (it is not needed unless a tunnel is being opened,
    and `mnemos.api` importing `mnemos.mcp` eagerly would couple startup
    to the MCP edge), and having one function own that import keeps the
    dependency explicit and substitutable instead of buried in a try
    block.
    """
    from mnemos.mcp.oauth import get_oauth_service

    return get_oauth_service()


def _issue_tunnel_token() -> tuple[str, str, Optional[int]]:
    """Return ``(token, source, expires_in)`` for the tunnel session.

    Preference order matches what the MCP edge actually accepts:

    1. An OAuth 2.1 access token from the existing MCP authorization
       server. Scoped, expiring, and validated by the same
       ``OAuthService.validate_access_token`` the SSE handshake uses.
    2. The configured static ``MNEMOS_MCP_TOKEN``, surfaced rather than
       minted — this is the single-user path the connector docs describe.

    ``MNEMOS_MCP_TOKENS`` (the per-user map) is deliberately NOT used as a
    fallback: picking one operator's token out of a multi-user map and
    handing it to a connector would silently grant that user's identity to
    whoever pasted the URL.
    """
    settings = get_settings()

    try:
        service = _oauth_service()
    except Exception as exc:  # RuntimeError when OAuth isn't configured
        logger.info("[TUNNEL] OAuth token issuance unavailable (%s); trying static token", exc)
    else:
        token = service.issue_access_token(
            client_id="mnemos-tunnel",
            provider="mnemos-tunnel",
            lifetime=TUNNEL_TOKEN_SECONDS,
        )
        return token, "oauth", TUNNEL_TOKEN_SECONDS

    static = settings.mcp.token.strip()
    if static:
        return static, "static", None

    raise HTTPException(
        status_code=503,
        detail=(
            "No bearer credential is available to hand the connector. Configure the MCP "
            "edge with either MNEMOS_MCP_TOKEN (single-user static bearer) or a complete "
            "OAuth setup (MNEMOS_OAUTH_ISSUER + MNEMOS_OAUTH_SIGNING_KEY, or "
            "MNEMOS_DATABASE_DSN so the persistence backend can supply the signing key), "
            "then retry. MNEMOS_MCP_TOKENS alone is not used here: handing out one entry "
            "of a per-user token map would grant that user's identity to the connector."
        ),
    )


def _http_error_for(exc: TunnelError) -> HTTPException:
    """Map a bridge failure to a status code with an actionable message."""
    if isinstance(exc, TunnelBinaryMissingError):
        # 503: the capability is genuinely unavailable on this host, which
        # matches how the rest of the admin surface reports a missing
        # dependency (see persistence_helpers.require_postgres_pool_or_503).
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, TunnelAuthError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


def _reset_active_for_tests() -> None:
    """Drop the module-level tunnel handle. Test-support only."""
    global _active_bridge
    _active_bridge = None


# ── routes ───────────────────────────────────────────────────────────────


@router.post("/start", response_model=TunnelStartResponse)
async def start_tunnel(
    request: TunnelStartRequest,
    user: UserContext = Depends(require_root),
) -> TunnelStartResponse:
    """Open a public tunnel to a local port and return connector credentials."""
    global _active_bridge
    _require_enabled()

    # Validate the request before minting anything, then resolve the
    # credential before spawning anything. A token issued for a request
    # that then 422s is a live credential nobody asked for; a tunnel opened
    # before we know we have a token exposes the port for nothing.
    try:
        bridge_cls = get_bridge_class(request.backend)
    except KeyError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown tunnel backend {request.backend!r}; expected one of {list(BACKEND_NAMES)}.",
        )

    token, token_source, expires_in = _issue_tunnel_token()

    async with _lock:
        if _active_bridge is not None and _active_bridge.status() is not None:
            existing = _active_bridge.status()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A {existing.backend} tunnel is already running at {existing.url} "
                    f"(port {existing.target_port}). Stop it first: "
                    f"DELETE /admin/tunnels/stop"
                ),
            )

        bridge = bridge_cls()
        try:
            info = await bridge.start(
                target_port=request.target_port,
                authtoken=request.authtoken,
            )
        except TunnelError as exc:
            logger.warning("[TUNNEL] start failed (%s): %s", request.backend, exc)
            raise _http_error_for(exc)
        _active_bridge = bridge

    logger.warning(
        "[TUNNEL] %s published local port %d at %s by root user %s — this instance "
        "is now reachable from the public internet",
        info.backend,
        info.target_port,
        info.url,
        user.user_id,
    )
    return TunnelStartResponse(
        url=info.url,
        token=token,
        token_source=token_source,
        backend=info.backend,
        target_port=info.target_port,
        pid=info.pid,
        started_at=info.started_at,
        expires_in=expires_in,
    )


@router.get("/status", response_model=TunnelStatusResponse)
async def tunnel_status(
    _: UserContext = Depends(require_root),
) -> TunnelStatusResponse:
    """Report the live tunnel, if any.

    ``TunnelBridge.status()`` re-checks the child process rather than
    trusting the stored record, so an agent that died on its own is
    reported as not-running instead of as a working tunnel.
    """
    _require_enabled()
    if _active_bridge is None:
        return TunnelStatusResponse(running=False)
    info = _active_bridge.status()
    if info is None:
        return TunnelStatusResponse(running=False)
    return TunnelStatusResponse(
        running=True,
        backend=info.backend,
        url=info.url,
        target_port=info.target_port,
        pid=info.pid,
        started_at=info.started_at,
    )


@router.delete("/stop", response_model=TunnelStopResponse)
async def stop_tunnel(
    _: UserContext = Depends(require_root),
) -> TunnelStopResponse:
    """Tear down the running tunnel. Idempotent."""
    global _active_bridge
    _require_enabled()
    async with _lock:
        if _active_bridge is None:
            return TunnelStopResponse(stopped=False)
        bridge = _active_bridge
        backend = bridge.name
        stopped = await bridge.stop()
        _active_bridge = None
    if stopped:
        logger.info("[TUNNEL] %s tunnel stopped", backend)
    return TunnelStopResponse(stopped=stopped, backend=backend if stopped else None)


async def shutdown_active_tunnel() -> None:
    """Kill any running tunnel. Called from the app lifespan on shutdown.

    Without this, stopping MNEMOS leaves the vendor agent running and the
    public URL live, pointed at a port that is no longer serving — the
    instance looks reachable and is not, and the operator has no handle on
    the orphan.
    """
    global _active_bridge
    # Take the lock like the routes do. Lifespan teardown normally runs
    # after in-flight requests drain, so this rarely contends — but `_lock`
    # is what makes "at most one tunnel" true, and a mutation path that
    # skips it is a hole in that invariant rather than an optimisation.
    async with _lock:
        bridge = _active_bridge
        _active_bridge = None
    if bridge is None:
        return
    try:
        if await bridge.stop():
            logger.info("[TUNNEL] %s tunnel closed on shutdown", bridge.name)
    except Exception as exc:  # pragma: no cover - shutdown best-effort
        logger.warning("[TUNNEL] failed to close %s tunnel on shutdown: %s", bridge.name, exc)
