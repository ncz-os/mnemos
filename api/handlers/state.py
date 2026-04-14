"""State API: GET/PUT/DELETE /state/{key}, GET /state"""
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["state"])


class StateSetRequest(BaseModel):
    value: Any


@router.get("/state")
async def list_state_keys(user: UserContext = Depends(get_current_user)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        async with _lc._pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT key, updated::text, version FROM state ORDER BY key'
            )
        return {"keys": [dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Error listing state keys: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/state/{key}")
async def get_state(key: str, user: UserContext = Depends(get_current_user)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        async with _lc._pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT key, value, updated::text, version FROM state WHERE key = $1', key
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
                '''INSERT INTO state (key, value, updated)
                   VALUES ($1, $2::jsonb, NOW())
                   ON CONFLICT (key) DO UPDATE
                   SET value = $2::jsonb, updated = NOW(), version = state.version + 1
                   RETURNING key, value, updated::text, version''',
                key, json.dumps(req.value)
            )
        return dict(row)
    except Exception as e:
        logger.error(f"Error setting state key '{key}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/state/{key}", status_code=204)
async def delete_state(key: str, user: UserContext = Depends(get_current_user)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        result = await conn.execute('DELETE FROM state WHERE key = $1', key)
    if result == 'DELETE 0':
        raise HTTPException(status_code=404, detail=f"State key '{key}' not found")
