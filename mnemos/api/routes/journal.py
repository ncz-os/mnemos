"""Journal API: POST /journal, GET /journal, DELETE /journal/{entry_id}

Per-owner, per-namespace journal. Each entry is scoped to the creating user's
`user_id` and `namespace`; root can target another owner/namespace via
`?owner_id=` and `?namespace=`.
"""

import logging
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import backend_or_503
from mnemos.core.security import scope_namespace, scope_owner

logger = logging.getLogger(__name__)
router = APIRouter(tags=["journal"])


class JournalCreateRequest(BaseModel):
    topic: str
    content: str
    date: Optional[str] = None  # ISO date string; defaults to CURRENT_DATE if omitted
    metadata: Optional[dict] = None


# #192: removed `JournalEntry` Pydantic response model — declared
# but never used as a `response_model=` annotation. Journal routes
# return raw dict / list payloads directly.


@router.post("/journal", status_code=201)
async def create_journal_entry(
    req: JournalCreateRequest,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None, description="Admin-only: write on behalf of another owner"),
    namespace: Optional[str] = Query(None, description="Admin-only: write into another namespace"),
):
    backend = backend_or_503()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        entry_id = str(uuid.uuid4())
        entry_date = None
        if req.date:
            try:
                entry_date = date.fromisoformat(req.date)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date format; expected YYYY-MM-DD")
        async with backend.transactional() as tx:
            row = await backend.create_journal_entry(
                tx,
                entry_id=entry_id,
                owner_id=target_owner,
                namespace=target_ns,
                entry_date=entry_date,
                topic=req.topic,
                content=req.content,
                metadata=req.metadata,
            )
        return row
    except HTTPException:
        raise
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
    owner_id: Optional[str] = Query(None),
    namespace: Optional[str] = Query(None),
):
    backend = backend_or_503()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        parsed_date = None
        if date_str:
            try:
                parsed_date = date.fromisoformat(date_str)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date format; expected YYYY-MM-DD")
        async with backend.transactional() as tx:
            rows = await backend.list_journal_entries(
                tx,
                owner_id=target_owner,
                namespace=target_ns,
                entry_date=parsed_date,
                topic=topic,
                search=search,
                limit=limit,
            )
        return {"entries": [dict(r) for r in rows], "count": len(rows)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing journal entries: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/journal/{entry_id}", status_code=204)
async def delete_journal_entry(
    entry_id: str,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None),
    namespace: Optional[str] = Query(None),
):
    backend = backend_or_503()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    async with backend.transactional() as tx:
        deleted = await backend.delete_journal_entry(
            tx,
            entry_id=entry_id,
            owner_id=target_owner,
            namespace=target_ns,
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Entry not found")
