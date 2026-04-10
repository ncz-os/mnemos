"""Knowledge Graph triple endpoints."""
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import api.lifecycle as _lc
from api.models import KGTriple, KGTripleCreate, KGTripleListResponse, KGTripleUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kg", tags=["knowledge-graph"])


def _row_to_triple(row) -> KGTriple:
    return KGTriple(
        id=row['id'],
        subject=row['subject'],
        predicate=row['predicate'],
        object=row['object'],
        subject_type=row.get('subject_type'),
        object_type=row.get('object_type'),
        valid_from=row['valid_from'].isoformat() if row['valid_from'] else '',
        valid_until=row['valid_until'].isoformat() if row.get('valid_until') else None,
        memory_id=row.get('memory_id'),
        confidence=row['confidence'],
        created=row['created'].isoformat() if row['created'] else '',
    )


@router.post("/triples", response_model=KGTriple, status_code=201)
async def create_triple(req: KGTripleCreate):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
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

    async with _lc._pool.acquire() as conn:
        if req.memory_id:
            exists = await conn.fetchval("SELECT 1 FROM memories WHERE id=$1", req.memory_id)
            if not exists:
                raise HTTPException(status_code=404, detail=f"memory_id {req.memory_id} not found")

        await conn.execute(
            "INSERT INTO kg_triples "
            "(id, subject, predicate, object, subject_type, object_type, "
            " valid_from, valid_until, memory_id, confidence) "
            "VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, NOW()), $8, $9, $10)",
            triple_id, req.subject, req.predicate, req.object,
            req.subject_type, req.object_type,
            valid_from, valid_until, req.memory_id, req.confidence,
        )
        row = await conn.fetchrow("SELECT * FROM kg_triples WHERE id=$1", triple_id)

    return _row_to_triple(row)


@router.get("/triples", response_model=KGTripleListResponse)
async def list_triples(
    subject: Optional[str] = Query(None),
    predicate: Optional[str] = Query(None),
    object: Optional[str] = Query(None),
    subject_type: Optional[str] = Query(None),
    object_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    conditions = []
    filter_params = []
    idx = 1
    for col, val in [
        ("subject", subject), ("predicate", predicate), ("object", object),
        ("subject_type", subject_type), ("object_type", object_type),
    ]:
        if val is not None:
            conditions.append(f"{col}=${idx}")
            filter_params.append(val)
            idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM kg_triples {where} ORDER BY created DESC "
            f"LIMIT ${idx} OFFSET ${idx + 1}",
            *filter_params, limit, offset,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM kg_triples {where}",
            *filter_params,
        )

    return KGTripleListResponse(count=total, triples=[_row_to_triple(r) for r in rows])


@router.get("/timeline/{subject}", response_model=KGTripleListResponse)
async def get_timeline(subject: str, limit: int = Query(100, ge=1, le=1000)):
    """Get all triples for a subject ordered by valid_from (chronological history)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM kg_triples WHERE subject=$1 ORDER BY valid_from ASC LIMIT $2",
            subject, limit,
        )
    return KGTripleListResponse(count=len(rows), triples=[_row_to_triple(r) for r in rows])


@router.patch("/triples/{triple_id}", response_model=KGTriple)
async def update_triple(triple_id: str, req: KGTripleUpdate):
    """Partially update a KG triple."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
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
    set_clauses = [f"{col}=${i+2}" for i, col in enumerate(updates.keys())]
    async with _lc._pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM kg_triples WHERE id=$1", triple_id)
        if not exists:
            raise HTTPException(status_code=404, detail=f"Triple {triple_id} not found")
        await conn.execute(
            f"UPDATE kg_triples SET {', '.join(set_clauses)} WHERE id=$1",
            triple_id, *list(updates.values()),
        )
        row = await conn.fetchrow("SELECT * FROM kg_triples WHERE id=$1", triple_id)
    return _row_to_triple(row)


@router.delete("/triples/{triple_id}", status_code=204)
async def delete_triple(triple_id: str):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        result = await conn.execute("DELETE FROM kg_triples WHERE id=$1", triple_id)
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail=f"Triple {triple_id} not found")
