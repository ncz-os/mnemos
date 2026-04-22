"""State API: GET/PUT/DELETE /state/{key}, GET /state

Per-owner KV store. All operations are scoped to the caller's `user.user_id`;
keys from one owner are invisible to another. Root can read/write any owner's
keys by passing an `?owner_id=` query parameter on GET/DELETE.
"""
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["state"])


class StateSetRequest(BaseModel):
    value: Any


def _scope_owner(user: UserContext, override: Optional[str]) -> str:
    """Return the owner_id to query. Only root can override."""
    if override and override != user.user_id:
        if user.role != "root":
            raise HTTPException(status_code=403, detail="owner_id override requires root")
        return override
    return user.user_id


@router.get("/state")
async def list_state_keys(
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None, description="Admin-only: target another owner"),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_owner = _scope_owner(user, owner_id)
    try:
        async with _lc._pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT key, updated::text, version FROM state '
                'WHERE owner_id = $1 ORDER BY key',
                target_owner,
            )
        return {"keys": [dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Error listing state keys: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/state/{key}")
async def get_state(
    key: str,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_owner = _scope_owner(user, owner_id)
    try:
        async with _lc._pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT key, value, updated::text, version FROM state '
                'WHERE owner_id = $1 AND key = $2',
                target_owner, key,
            )
        if not row:
            raise HTTPException(status_code=404, detail=f"State key '{key}' not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting state key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/state/{key}", status_code=200)
async def set_state(
    key: str,
    req: StateSetRequest,
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        async with _lc._pool.acquire() as conn:
            row = await conn.fetchrow(
                '''INSERT INTO state (owner_id, key, value, updated)
                   VALUES ($1, $2, $3::jsonb, NOW())
                   ON CONFLICT (owner_id, key) DO UPDATE
                   SET value = $3::jsonb, updated = NOW(), version = state.version + 1
                   RETURNING key, value, updated::text, version''',
                user.user_id, key, json.dumps(req.value),
            )
        return dict(row)
    except Exception as e:
        logger.error(f"Error setting state key '{key}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/state/{key}", status_code=204)
async def delete_state(
    key: str,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_owner = _scope_owner(user, owner_id)
    async with _lc._pool.acquire() as conn:
        result = await conn.execute(
            'DELETE FROM state WHERE owner_id = $1 AND key = $2',
            target_owner, key,
        )
    if result == 'DELETE 0':
        raise HTTPException(status_code=404, detail=f"State key '{key}' not found")
