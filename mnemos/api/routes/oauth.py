"""OAuth / OIDC endpoints — user-facing login flow.

Mounts under /auth/oauth/*. These endpoints do NOT require authentication:
they establish it. Admin-side provider management is in api/handlers/oauth_admin.py.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import require_oauth_backend
from mnemos.core import oauth as _oauth
from mnemos.persistence.base import OAuthPersistence
from mnemos.domain.models import (
    OAuthIdentity,
    OAuthLogoutResponse,
    OAuthMeResponse,
    OAuthProviderListResponse,
    OAuthProviderPublic,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


# ── Public provider list (no auth) ────────────────────────────────────────────


@router.get("/providers", response_model=OAuthProviderListResponse)
async def list_providers_public():
    """List enabled providers for a login UI. No secrets returned."""
    backend = require_oauth_backend()
    async with backend.transactional() as tx:
        rows = await backend.oauth.list_enabled_providers(tx)
    providers = [
        OAuthProviderPublic(
            name=r["name"],
            display_name=r["display_name"],
            kind=r["kind"],
            enabled=r["enabled"],
        )
        for r in rows
    ]
    return OAuthProviderListResponse(count=len(providers), providers=providers)


# ── Login + callback ──────────────────────────────────────────────────────────


async def _load_provider(name: str):
    """Fetch an enabled provider row, else 404."""
    backend = require_oauth_backend()
    async with backend.transactional() as tx:
        row = await backend.oauth.get_provider(tx, name)
    if not row or not row["enabled"]:
        raise HTTPException(status_code=404, detail=f"OAuth provider '{name}' not found or disabled")
    return row


@router.get("/{provider}/login")
async def oauth_login(provider: str, request: Request):
    """Start an OAuth authorization-code flow. Redirects to the provider."""
    provider_row = await _load_provider(provider)
    redirect_uri = str(request.url_for("oauth_callback", provider=provider))
    try:
        return await _oauth.start_login(request, provider_row, redirect_uri)
    except Exception as e:
        logger.exception("oauth login start failed for provider=%s", provider)
        raise HTTPException(status_code=502, detail=f"OAuth provider error: {e}")


@router.get("/{provider}/callback", name="oauth_callback")
async def oauth_callback(provider: str, request: Request):
    """Provider redirect target. Exchanges code, provisions user, sets cookie."""
    provider_row = await _load_provider(provider)

    try:
        client = await _oauth.build_client(provider_row)
        token = await client.authorize_access_token(request)
        claims = {}
        if "id_token" in token:
            try:
                claims = dict(token.get("userinfo") or await client.parse_id_token(request, token))
            except Exception:
                pass
        if not claims:
            try:
                claims = dict(await client.userinfo(token=token))
            except Exception as e:
                logger.warning("userinfo fetch failed for %s: %s", provider_row["name"], e)
                claims = {}
        external_id = _oauth._extract_external_id(provider_row["name"], claims)
        if not external_id:
            raise ValueError(
                f"provider {provider_row['name']} returned no usable external id in claims: " f"{list(claims.keys())}"
            )
        session_id = secrets.token_urlsafe(48)
        expires = datetime.now(timezone.utc) + _oauth.SESSION_TTL
        user_agent = request.headers.get("user-agent", "")[:500]
        ip = request.client.host if request.client else None
        backend = require_oauth_backend()
        async with backend.transactional() as tx:
            user_id, identity_id = await backend.oauth.provision_or_link_user(
                tx,
                provider=provider_row["name"],
                external_id=external_id,
                claims=claims,
            )
            await backend.oauth.create_session(
                tx,
                session_id=session_id,
                user_id=user_id,
                identity_id=identity_id,
                expires_at=expires,
                user_agent=user_agent,
                ip_address=ip,
            )
    except Exception as e:
        logger.exception("oauth callback failed for provider=%s", provider)
        raise HTTPException(status_code=502, detail=f"OAuth callback error: {e}")

    # Where to send the browser now.
    post_login_redirect = request.query_params.get("next") or "/"
    # Open-redirect defense: only allow local absolute paths. Reject:
    #   - anything not starting with "/" (absolute URLs, javascript:, etc.)
    #   - protocol-relative targets like "//evil.com" (browsers treat as URL)
    #   - backslash variants "/\evil.com" (some browsers normalize)
    if (
        not post_login_redirect.startswith("/")
        or post_login_redirect.startswith("//")
        or post_login_redirect.startswith("/\\")
    ):
        post_login_redirect = "/"

    # Determine whether to set the Secure flag. Behind a TLS-terminating proxy
    # `request.url.scheme` is "http" even when the client is on HTTPS — trust
    # X-Forwarded-Proto when OAUTH_TRUST_PROXY is set (and the proxy is
    # configured to rewrite the header).
    from mnemos.core.config import get_settings

    settings = get_settings()
    _trust_proxy = settings.oauth.trust_proxy
    _xfp = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    is_https = request.url.scheme == "https" or (_trust_proxy and _xfp == "https") or settings.server.session_https_only
    response: RedirectResponse = RedirectResponse(url=post_login_redirect, status_code=303)
    response.set_cookie(
        key=_oauth.SESSION_COOKIE_NAME,
        value=session_id,
        max_age=_oauth.SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )
    logger.info(
        "oauth: session created user_id=%s provider=%s identity=%s",
        user_id,
        provider,
        identity_id,
    )
    return response


# ── Logout ────────────────────────────────────────────────────────────────────


@router.post("/logout", response_model=OAuthLogoutResponse)
async def oauth_logout(
    request: Request,
    all_devices: bool = False,
    user: UserContext = Depends(get_current_user),
):
    """Invalidate the current session cookie (or all sessions for the user)."""
    backend = require_oauth_backend()
    sessions_revoked = 0
    async with backend.transactional() as tx:
        if all_devices:
            sessions_revoked = await backend.oauth.revoke_all_sessions(tx, user.user_id)
        else:
            cookie_session = request.cookies.get(_oauth.SESSION_COOKIE_NAME)
            if cookie_session:
                ok = await backend.oauth.revoke_session(tx, cookie_session)
                sessions_revoked = 1 if ok else 0

    response = JSONResponse(content={"logged_out": True, "sessions_revoked": sessions_revoked})
    response.delete_cookie(_oauth.SESSION_COOKIE_NAME, path="/")
    return response


# ── Me ────────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=OAuthMeResponse)
async def oauth_me(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    """Who am I? Works with either auth method."""
    identity: Optional[OAuthIdentity] = None

    # If authenticated via session cookie, hydrate the most-recent identity.
    cookie_session = request.cookies.get(_oauth.SESSION_COOKIE_NAME)
    auth_method = "personal" if not user.authenticated else "api_key"

    if cookie_session:
        backend = _lc._persistence_backend
        if backend is not None and isinstance(backend, OAuthPersistence):
            async with backend.transactional() as tx:
                ident = await backend.oauth.get_identity_for_session(tx, cookie_session)
            if ident:
                auth_method = "session"
                identity = OAuthIdentity(
                    id=str(ident["id"]),
                    user_id=ident["user_id"],
                    provider=ident["provider"],
                    external_id=ident["external_id"],
                    email=ident["email"],
                    display_name=ident["display_name"],
                    last_login_at=ident["last_login_at"].isoformat() if ident["last_login_at"] else None,
                    created=ident["created"].isoformat()
                    if hasattr(ident["created"], "isoformat")
                    else str(ident["created"]),
                )

    return OAuthMeResponse(
        user_id=user.user_id,
        role=user.role,
        namespace=user.namespace,
        authenticated=user.authenticated,
        auth_method=auth_method,
        identity=identity,
    )
