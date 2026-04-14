"""Journal API: POST /journal, GET /journal, DELETE /journal/{entry_id}"""
import json
import logging
from datetime import date
from typing import Optional, List

import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["journal"])


class JournalCreateRequest(BaseModel):
    topic: str
    content: str
    date: Optional[str] = None   # ISO date string; defaults to CURRENT_DATE if omitted
    metadata: Optional[dict] = None


class JournalEntry(BaseModel):
    id: str
    entry_date: str
    topic: Optional[str]
    content: Optional[str]
    metadata: Optional[dict]
    created: str


@router.post("/journal", status_code=201)
async def create_journal_entry(
    req: JournalCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        entry_id = str(uuid.uuid4())
        entry_date = req.date or "CURRENT_DATE"
        async with _lc._pool.acquire() as conn:
            if req.date:
                row = await conn.fetchrow(
                    '''INSERT INTO journal (id, entry_date, topic, content, metadata)
                       VALUES ($1, $2::date, $3, $4, $5::jsonb)
                       RETURNING id, entry_date::text, topic, content, metadata, created::text''',
                    entry_id, req.date, req.topic, req.content,
                    json.dumps(req.metadata or {}),
                )
            else:
                row = await conn.fetchrow(
                    '''INSERT INTO journal (id, entry_date, topic, content, metadata)
                       VALUES ($1, CURRENT_DATE, $2, $3, $4::jsonb)
                       RETURNING id, entry_date::text, topic, content, metadata, created::text''',
                    entry_id, req.topic, req.content,
                    json.dumps(req.metadata or {}),
                )
        return dict(row)
    except Exception as e:
        logger.error(f"Error creating journal entry: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/journal")
async def list_journal_entries(
    topic: Optional[str] = None,
    date_str: Optional[str] = Query(None, alias="date"),
    search: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        async with _lc._pool.acquire() as conn:
            if date_str:
                rows = await conn.fetch(
                    '''SELECT id, entry_date::text, topic, content, metadata, created::text
                       FROM journal WHERE entry_date = $1 ORDER BY created DESC LIMIT $2''',
                    date_str, limit
                )
            elif topic:
                rows = await conn.fetch(
                    '''SELECT id, entry_date::text, topic, content, metadata, created::text
                       FROM journal WHERE topic = $1 ORDER BY created DESC LIMIT $2''',
                    topic, limit
                )
            elif search:
                rows = await conn.fetch(
                    '''SELECT id, entry_date::text, topic, content, metadata, created::text
                       FROM journal WHERE content ILIKE $1 OR topic ILIKE $1
                       ORDER BY created DESC LIMIT $2''',
                    f'%{search}%', limit
                )
            else:
                rows = await conn.fetch(
                    '''SELECT id, entry_date::text, topic, content, metadata, created::text
                       FROM journal ORDER BY created DESC LIMIT $1''',
                    limit
                )
        return {"entries": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        logger.error(f"Error listing journal entries: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/journal/{entry_id}", status_code=204)
async def delete_journal_entry(
    entry_id: str,
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        result = await conn.execute('DELETE FROM journal WHERE id = $1', entry_id)
    if result == 'DELETE 0':
        raise HTTPException(status_code=404, detail="Entry not found")
