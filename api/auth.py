"""MNEMOS v1 authentication — API key bearer tokens, personal profile bypass."""
import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import List

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer

logger = logging.getLogger(__name__)

_auth_enabled: bool = False
_default_namespace: str = "default"
_personal_user_id: str = "default"

PERSONAL_SINGLETON: "UserContext" = None   # set by configure_auth()

_bearer = HTTPBearer(auto_error=False)


@dataclass
class UserContext:
    user_id: str
    group_ids: List[str]
    role: str          # "user" | "root"
    namespace: str
    authenticated: bool


def configure_auth(config: dict) -> None:
    """Called once at startup from lifecycle lifespan."""
    global _auth_enabled, _default_namespace, _personal_user_id, PERSONAL_SINGLETON
    _auth_enabled = config.get("enabled", False)
    _default_namespace = config.get("default_namespace", "default")
    _personal_user_id = config.get("personal_user_id", "default")
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


async def _update_last_used(pool, key_id: str) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE api_keys SET last_used=NOW() WHERE id=$1", key_id
            )
    except Exception as e:
        logger.warning(f"[AUTH] Failed to update last_used for key {key_id}: {e}")


async def get_current_user(
    request: Request,
    credentials=Depends(_bearer),
) -> UserContext:
    """FastAPI dependency — returns UserContext for every authenticated route."""
    if not _auth_enabled:
        return PERSONAL_SINGLETON

    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    raw_key = credentials.credentials
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ak.id, ak.user_id, ak.revoked, u.role "
            "FROM api_keys ak JOIN users u ON u.id = ak.user_id "
            "WHERE ak.key_hash = $1",
            key_hash,
        )

    if row is None or row["revoked"]:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    asyncio.create_task(_update_last_used(pool, str(row["id"])))

    async with pool.acquire() as conn:
        group_rows = await conn.fetch(
            "SELECT group_id FROM user_groups WHERE user_id = $1", row["user_id"]
        )

    return UserContext(
        user_id=row["user_id"],
        group_ids=[r["group_id"] for r in group_rows],
        role=row["role"],
        namespace=_default_namespace,
        authenticated=True,
    )


async def require_root(user: UserContext = Depends(get_current_user)) -> UserContext:
    """FastAPI dependency — raises 403 if caller is not root."""
    if user.role != "root":
        raise HTTPException(status_code=403, detail="Root access required")
    return user
