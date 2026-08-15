"""FastAPI auth dependencies (Bearer + session-cookie).

Live home of the `get_current_user` dependency consumed by the route
modules. Existing Bearer flow preserved; session-cookie flow added
as a secondary path. Auth-disabled mode unchanged.

Auth lookups (API key + browser session) go through the backend-neutral
``OAuthRepository`` exposed by ``request.app.state.persistence_backend.oauth``
so the same path works on every supported backend (Postgres, SQLite,
Oracle, Db2). Backends that do not implement OAuthPersistence return a
clear 503 with the missing-capability name, instead of pretending the
credential check ran.

(Originally split out from a pre-v4 `api/auth.py` module that no
longer exists.)
"""
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer

from mnemos.core.auth_context import UserContext

logger = logging.getLogger(__name__)

_auth_enabled: bool = False
_default_namespace: str = "default"
_personal_user_id: str = "default"

PERSONAL_SINGLETON: "Optional[UserContext]" = None   # set by configure_auth(); None before startup

_bearer = HTTPBearer(auto_error=False)


def configure_auth(config: dict | None = None) -> None:
    """Called once at startup from lifecycle lifespan."""
    from mnemos.core.config import get_settings

    global _auth_enabled, _default_namespace, _personal_user_id, PERSONAL_SINGLETON
    settings = get_settings().auth
    if config is None:
        config = {}
        _auth_enabled = settings.enabled
    else:
        _auth_enabled = config.get("enabled", settings.enabled)
    _default_namespace = config.get("default_namespace", settings.default_namespace)
    _personal_user_id = config.get("personal_user_id", settings.personal_user_id)
    PERSONAL_SINGLETON = UserContext(
        user_id=_personal_user_id,
        group_ids=[],
        role="root",
        namespace=_default_namespace,
        authenticated=False,
    )
    logger.info(
        f"Auth configured: enabled={_auth_enabled}, "
        f"namespace={_default_namespace}, personal_user={_personal_user_id}"
    )


def _auth_backend(request: Request):
    """Return ``(oauth_repo, backend)`` from the active persistence backend.

    Backend-neutral authentication goes through ``OAuthRepository`` so the
    same call shape works on Postgres, SQLite, Oracle, and Db2. Backends
    that do not implement ``OAuthPersistence`` (MySQL/MariaDB today, since
    those backends do not carry the ``api_keys`` / ``oauth_sessions``
    tables) return a 503 with a clear message rather than a misleading
    pool-availability error.
    """
    backend = getattr(request.app.state, "persistence_backend", None)
    if backend is None:
        raise HTTPException(
            status_code=503, detail="Persistence backend not available"
        )
    if not getattr(backend, "_supports_oauth_persistence", False):
        raise HTTPException(
            status_code=503,
            detail=(
                "Authentication requires an OAuth-capable persistence backend; "
                "this backend does not implement OAuthPersistence."
            ),
        )
    return backend.oauth, backend


async def _touch_api_key(backend, oauth, key_id) -> None:
    """Bump ``api_keys.last_used`` via the backend-neutral OAuth repository."""
    try:
        async with backend.transactional() as tx:
            await oauth.touch_api_key(tx, key_id)
    except Exception as e:
        logger.warning(f"[AUTH] Failed to update last_used for key {key_id}: {e}")


async def get_current_user(
    request: Request,
    credentials=Depends(_bearer),
) -> UserContext:
    """Auth dependency — Bearer token first, session cookie second.

    Credential lookups go through the backend-neutral OAuth repository
    exposed by the active persistence backend, so the same code path serves
    Postgres, SQLite, Oracle, and Db2.
    """
    if not _auth_enabled:
        if PERSONAL_SINGLETON is None:
            raise HTTPException(status_code=503, detail="Auth not yet configured — startup incomplete")
        return PERSONAL_SINGLETON

    oauth, backend = _auth_backend(request)

    # 1. API key (Bearer) — existing behaviour.
    if credentials is not None:
        raw_key = credentials.credentials
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        async with backend.transactional() as tx:
            row = await oauth.lookup_api_key(tx, key_hash)
        if row is None or row.get("revoked"):
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")

        from mnemos.core.lifecycle import _schedule_background

        _schedule_background(_touch_api_key(backend, oauth, row["id"]))

        return UserContext(
            user_id=row["user_id"],
            group_ids=list(row.get("group_ids") or []),
            role=row["role"],
            namespace=row["namespace"],
            authenticated=True,
        )

    # 2. Session cookie — v3.0.0+ path (only checked when no Bearer).
    from mnemos.core.oauth import SESSION_COOKIE_NAME

    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value:
        now = datetime.now(timezone.utc)
        async with backend.transactional() as tx:
            resolved = await oauth.resolve_active_session(tx, cookie_value, now=now)
        if resolved is not None:
            context = UserContext(
                user_id=resolved["user_id"],
                group_ids=[],
                role="user",
                namespace=_default_namespace,
                authenticated=True,
            )
            context.session_id = cookie_value
            return context

    # 3. No credentials.
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_root(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency — raises 403 if caller is not root."""
    if user.role != "root":
        raise HTTPException(status_code=403, detail="Root access required")
    return user
