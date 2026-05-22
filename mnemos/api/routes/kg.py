"""Knowledge Graph triple endpoints.

v3.1.2 tenancy contract (Tier 3):

  * Every triple carries owner_id + namespace (added to kg_triples in
    migrations_v3_1_2_kg_tenancy.sql).
  * create_triple stamps the authenticated caller's user_id as
    owner_id and their namespace from UserContext.
  * Read endpoints (list, timeline) filter by the caller's owner_id
    so users only see their own triples. Root role bypasses the
    filter for operational access.
  * update and delete verify the caller owns the target row before
    mutating; non-owners get 404 (not 403 — the row is invisible
    to them per the read contract).

Migrated to backend-neutral persistence (v6.0-rc Oracle route migration RA-3).
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import maybe_set_pg_rls as _maybe_set_pg_rls
from mnemos.api.routes._postgres_only import _backend_or_503
from mnemos.core.security import is_root
from mnemos.domain.models import KGTriple, KGTripleCreate, KGTripleListResponse, KGTripleUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/kg", tags=["knowledge-graph"])


def _row_to_triple(row) -> KGTriple:
    return KGTriple(
        id=row["id"],
        subject=row["subject"],
        predicate=row["predicate"],
        object=row["object"],
        subject_type=row.get("subject_type"),
        object_type=row.get("object_type"),
        valid_from=row["valid_from"].isoformat() if row["valid_from"] else "",
        valid_until=row["valid_until"].isoformat() if row.get("valid_until") else None,
        memory_id=row.get("memory_id"),
        confidence=row["confidence"],
        created=row["created"].isoformat() if row["created"] else "",
    )


@router.post("/triples", response_model=KGTriple, status_code=201)
async def create_triple(req: KGTripleCreate, user: UserContext = Depends(get_current_user)):
    backend = _backend_or_503()
    triple_id = f"kg_{uuid.uuid4().hex[:12]}"

    valid_from = None
    if req.valid_from:
        try:
            valid_from = datetime.fromisoformat(req.valid_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="valid_from must be ISO8601")

    valid_until = None
    if req.valid_until:
        try:
            valid_until = datetime.fromisoformat(req.valid_until)
        except ValueError:
            raise HTTPException(status_code=422, detail="valid_until must be ISO8601")

    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            if req.memory_id and not is_root(user):
                # Cross-tenant memory_id references are rejected: a triple's
                # memory_id must point at a memory the caller can see.
                ok = await backend.kg_triples.check_memory_ownership(
                    tx,
                    memory_id=req.memory_id,
                    owner_id=user.user_id,
                    namespace=user.namespace,
                )
                if not ok:
                    raise HTTPException(status_code=404, detail=f"memory_id {req.memory_id} not found")
            await backend.kg_triples.insert_kg_triple(
                tx,
                triple_id=triple_id,
                subject=req.subject,
                predicate=req.predicate,
                obj=req.object,
                subject_type=req.subject_type,
                object_type=req.object_type,
                valid_from=valid_from,
                valid_until=valid_until,
                memory_id=req.memory_id,
                confidence=req.confidence,
                created=None,
                owner_id=user.user_id,
                namespace=user.namespace,
            )
            row = await backend.kg_triples.fetch_kg_triple_by_id(tx, triple_id)
        return _row_to_triple(row)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to create KG triple")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/triples", response_model=KGTripleListResponse)
async def list_triples(
    subject: Optional[str] = Query(None),
    predicate: Optional[str] = Query(None),
    object: Optional[str] = Query(None),
    subject_type: Optional[str] = Query(None),
    object_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user),
):
    backend = _backend_or_503()
    filters: dict = {}
    if subject is not None:
        filters["subject"] = subject
    if predicate is not None:
        filters["predicate"] = predicate
    if object is not None:
        filters["object"] = object
    if subject_type is not None:
        filters["subject_type"] = subject_type
    if object_type is not None:
        filters["object_type"] = object_type
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            total, rows = await backend.kg_triples.list_kg_triples(
                tx,
                filters=filters,
                is_root=is_root(user),
                owner_id=user.user_id,
                namespace=user.namespace,
                limit=limit,
                offset=offset,
            )
        return KGTripleListResponse(count=total, triples=[_row_to_triple(r) for r in rows])
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list KG triples")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/timeline/{subject}", response_model=KGTripleListResponse)
async def get_timeline(
    subject: str, limit: int = Query(100, ge=1, le=1000), user: UserContext = Depends(get_current_user)
):
    """Get all triples for a subject ordered by valid_from (chronological history)."""
    backend = _backend_or_503()
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            rows = await backend.kg_triples.get_kg_timeline(
                tx,
                subject=subject,
                is_root=is_root(user),
                owner_id=user.user_id,
                namespace=user.namespace,
                limit=limit,
            )
        return KGTripleListResponse(count=len(rows), triples=[_row_to_triple(r) for r in rows])
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to get KG timeline")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/triples/{triple_id}", response_model=KGTriple)
async def update_triple(triple_id: str, req: KGTripleUpdate, user: UserContext = Depends(get_current_user)):
    """Partially update a KG triple. Non-owners see 404 to avoid
    leaking existence of triples they don't own."""
    backend = _backend_or_503()
    updates: dict = {}
    for field in ("subject", "predicate", "object", "subject_type", "object_type", "confidence"):
        val = getattr(req, field)
        if val is not None:
            updates[field] = val
    if req.valid_until is not None:
        try:
            updates["valid_until"] = datetime.fromisoformat(req.valid_until)
        except ValueError:
            raise HTTPException(status_code=422, detail="valid_until must be ISO8601")
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            row = await backend.kg_triples.update_kg_triple(
                tx,
                triple_id=triple_id,
                updates=updates,
                is_root=is_root(user),
                owner_id=user.user_id,
                namespace=user.namespace,
            )
        if row is None:
            raise HTTPException(status_code=404, detail=f"Triple {triple_id} not found")
        return _row_to_triple(row)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to update KG triple")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/triples/{triple_id}", status_code=204)
async def delete_triple(triple_id: str, user: UserContext = Depends(get_current_user)):
    backend = _backend_or_503()
    try:
        async with backend.transactional() as tx:
            await _maybe_set_pg_rls(tx, user)
            deleted = await backend.kg_triples.delete_kg_triple(
                tx,
                triple_id=triple_id,
                is_root=is_root(user),
                owner_id=user.user_id,
                namespace=user.namespace,
            )
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Triple {triple_id} not found")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to delete KG triple")
        raise HTTPException(status_code=500, detail="Internal server error")
