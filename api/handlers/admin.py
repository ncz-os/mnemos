"""MNEMOS v1 admin endpoints — user and API key management (root only)."""
import hashlib
import logging
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException

import api.lifecycle as _lc
from api.auth import UserContext, require_root
from api.models import (
    ApiKeyCreateRequest,
    OAuthIdentity,
    OAuthIdentityListResponse,
    OAuthProviderAdmin,
    OAuthProviderAdminListResponse,
    OAuthProviderCreateRequest,
    OAuthProviderUpdateRequest,
    ApiKeyResponse,
    UserCreateRequest,
    UserResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Users ─────────────────────────────────────────────────────────────────────

@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    request: UserCreateRequest,
    _: UserContext = Depends(require_root),
):
    """Create a new user. id must be unique."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    if request.role not in ("user", "root", "federation"):
        raise HTTPException(
            status_code=422,
            detail="role must be 'user', 'root', or 'federation'",
        )
    async with _lc._pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE id=$1", request.id)
        if existing:
            raise HTTPException(status_code=409, detail=f"User '{request.id}' already exists")
        row = await conn.fetchrow(
            "INSERT INTO users (id, display_name, email, role) "
            "VALUES ($1, $2, $3, $4) RETURNING id, display_name, email, role, created_at",
            request.id, request.display_name, request.email, request.role,
        )
    return UserResponse(
        id=row["id"],
        display_name=row["display_name"],
        email=row["email"],
        role=row["role"],
        created_at=row["created_at"].isoformat(),
    )


@router.get("/users", response_model=List[UserResponse])
async def list_users(_: UserContext = Depends(require_root)):
    """List all users."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, display_name, email, role, created_at FROM users ORDER BY created_at"
        )
    return [
        UserResponse(
            id=r["id"],
            display_name=r["display_name"],
            email=r["email"],
            role=r["role"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


# ── API Keys ──────────────────────────────────────────────────────────────────

@router.post("/users/{user_id}/apikeys", response_model=ApiKeyResponse, status_code=201)
async def create_api_key(
    user_id: str,
    request: ApiKeyCreateRequest,
    _: UserContext = Depends(require_root),
):
    """Generate a new API key for user_id. Raw key is returned once and never stored."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

        key_count = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE user_id=$1 AND NOT revoked", user_id
        )
        if key_count >= 10:
            raise HTTPException(
                status_code=422,
                detail="Maximum of 10 active API keys per user",
            )

        raw_key = secrets.token_hex(32)       # 64 hex chars = 256 bits
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:8]              # shown in listings for identification

        row = await conn.fetchrow(
            "INSERT INTO api_keys (user_id, key_hash, key_prefix, label) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING id, user_id, key_prefix, label, created_at, last_used, revoked",
            user_id, key_hash, key_prefix, request.label,
        )

    logger.info(f"[ADMIN] Created API key prefix={key_prefix} for user={user_id}")
    return ApiKeyResponse(
        id=str(row["id"]),
        user_id=row["user_id"],
        key_prefix=row["key_prefix"],
        label=row["label"],
        created_at=row["created_at"].isoformat(),
        last_used=row["last_used"].isoformat() if row["last_used"] else None,
        revoked=row["revoked"],
        raw_key=raw_key,  # only returned here; never stored, never returned again
    )


@router.get("/users/{user_id}/apikeys", response_model=List[ApiKeyResponse])
async def list_api_keys(
    user_id: str,
    _: UserContext = Depends(require_root),
):
    """List API keys for user_id (no raw key in response)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        rows = await conn.fetch(
            "SELECT id, user_id, key_prefix, label, created_at, last_used, revoked "
            "FROM api_keys WHERE user_id=$1 ORDER BY created_at",
            user_id,
        )
    return [
        ApiKeyResponse(
            id=str(r["id"]),
            user_id=r["user_id"],
            key_prefix=r["key_prefix"],
            label=r["label"],
            created_at=r["created_at"].isoformat(),
            last_used=r["last_used"].isoformat() if r["last_used"] else None,
            revoked=r["revoked"],
        )
        for r in rows
    ]


@router.delete("/apikeys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str,
    _: UserContext = Depends(require_root),
):
    """Revoke an API key by ID (soft-delete: sets revoked=true)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE api_keys SET revoked=true WHERE id=$1::uuid AND NOT revoked",
            key_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="API key not found or already revoked")
    logger.info(f"[ADMIN] Revoked API key id={key_id}")


# ── OAuth provider management (root only) ────────────────────────────────────


def _to_provider_admin(row) -> OAuthProviderAdmin:
    return OAuthProviderAdmin(
        name=row["name"],
        display_name=row["display_name"],
        kind=row["kind"],
        issuer_url=row["issuer_url"],
        client_id=row["client_id"],
        client_secret_set=bool(row["client_secret"]),
        scope=row["scope"],
        authorize_url=row["authorize_url"],
        token_url=row["token_url"],
        userinfo_url=row["userinfo_url"],
        enabled=row["enabled"],
        created=row["created"].isoformat(),
        updated=row["updated"].isoformat(),
    )


@router.post("/oauth/providers", response_model=OAuthProviderAdmin, status_code=201)
async def create_oauth_provider(
    request: OAuthProviderCreateRequest,
    _: UserContext = Depends(require_root),
):
    """Register a new OAuth provider (root only)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    if request.kind not in ("oidc", "oauth2"):
        raise HTTPException(status_code=422, detail="kind must be 'oidc' or 'oauth2'")
    if request.kind == "oidc" and not request.issuer_url:
        raise HTTPException(status_code=422, detail="issuer_url required when kind='oidc'")
    if request.kind == "oauth2" and not (request.authorize_url and request.token_url):
        raise HTTPException(
            status_code=422,
            detail="authorize_url and token_url required when kind='oauth2'",
        )
    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO oauth_providers
              (name, display_name, kind, issuer_url, client_id, client_secret,
               scope, authorize_url, token_url, userinfo_url, enabled)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            request.name, request.display_name, request.kind, request.issuer_url,
            request.client_id, request.client_secret, request.scope,
            request.authorize_url, request.token_url, request.userinfo_url,
            request.enabled,
        )
    return _to_provider_admin(row)


@router.get("/oauth/providers", response_model=OAuthProviderAdminListResponse)
async def list_oauth_providers(_: UserContext = Depends(require_root)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM oauth_providers ORDER BY name")
    items = [_to_provider_admin(r) for r in rows]
    return OAuthProviderAdminListResponse(count=len(items), providers=items)


@router.patch("/oauth/providers/{name}", response_model=OAuthProviderAdmin)
async def update_oauth_provider(
    name: str,
    request: OAuthProviderUpdateRequest,
    _: UserContext = Depends(require_root),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")
    set_clauses = [f"{col}=${i+2}" for i, col in enumerate(updates.keys())]
    set_clauses.append("updated=NOW()")
    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE oauth_providers SET {', '.join(set_clauses)} "
            f"WHERE name=$1 RETURNING *",
            name, *updates.values(),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Provider not found")
    return _to_provider_admin(row)


@router.delete("/oauth/providers/{name}", status_code=204)
async def delete_oauth_provider(
    name: str,
    _: UserContext = Depends(require_root),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM oauth_providers WHERE name=$1", name,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Provider not found")


@router.get("/oauth/identities", response_model=OAuthIdentityListResponse)
async def list_oauth_identities(
    _: UserContext = Depends(require_root),
    user_id: str = None,
):
    """List OAuth identities. Filter by user_id optional."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        if user_id:
            rows = await conn.fetch(
                "SELECT id::text, user_id, provider, external_id, email, "
                "       display_name, last_login_at, created "
                "FROM oauth_identities WHERE user_id=$1 ORDER BY created DESC",
                user_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT id::text, user_id, provider, external_id, email, "
                "       display_name, last_login_at, created "
                "FROM oauth_identities ORDER BY created DESC LIMIT 100",
            )
    items = [
        OAuthIdentity(
            id=r["id"],
            user_id=r["user_id"],
            provider=r["provider"],
            external_id=r["external_id"],
            email=r["email"],
            display_name=r["display_name"],
            last_login_at=r["last_login_at"].isoformat() if r["last_login_at"] else None,
            created=r["created"].isoformat(),
        )
        for r in rows
    ]
    return OAuthIdentityListResponse(count=len(items), identities=items)
