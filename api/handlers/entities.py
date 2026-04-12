"""Entities API: CRUD for tracked entities (people, projects, concepts)."""
import logging
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["entities"])

ENTITY_TYPES = ['person', 'project', 'concept', 'document', 'decision', 'event']


class EntityCreateRequest(BaseModel):
    entity_type: str
    name: str
    description: Optional[str] = None
    metadata: Optional[dict] = None


class EntityUpdateRequest(BaseModel):
    description: Optional[str] = None
    metadata: Optional[dict] = None


class EntityLinkRequest(BaseModel):
    related_id: str


@router.post("/entities", status_code=201)
async def create_entity(
    req: EntityCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    if req.entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=400, detail=f"entity_type must be one of: {ENTITY_TYPES}")
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        import uuid
        entity_id = str(uuid.uuid4())
        async with _lc._pool.acquire() as conn:
            row = await conn.fetchrow(
                '''INSERT INTO entities (id, entity_type, name, description, metadata)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (entity_type, name) DO UPDATE
                   SET description = COALESCE($4, entities.description),
                       updated = NOW()
                   RETURNING id::text, entity_type, name, description, metadata, created::text, updated::text''',
                entity_id, req.entity_type, req.name, req.description, req.metadata or {}
            )
        return dict(row)
    except Exception as e:
        logger.error(f"Error creating entity: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entities")
async def list_entities(
    entity_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        async with _lc._pool.acquire() as conn:
            if entity_type and search:
                rows = await conn.fetch(
                    '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                       FROM entities WHERE entity_type=$1 AND name ILIKE $2
                       ORDER BY name LIMIT $3''',
                    entity_type, f'%{search}%', limit
                )
            elif entity_type:
                rows = await conn.fetch(
                    '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                       FROM entities WHERE entity_type=$1 ORDER BY name LIMIT $2''',
                    entity_type, limit
                )
            elif search:
                rows = await conn.fetch(
                    '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                       FROM entities WHERE name ILIKE $1 OR description ILIKE $1
                       ORDER BY name LIMIT $2''',
                    f'%{search}%', limit
                )
            else:
                rows = await conn.fetch(
                    '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                       FROM entities ORDER BY entity_type, name LIMIT $1''',
                    limit
                )
        return {"entities": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entities/{entity_id}")
async def get_entity(entity_id: str, user: UserContext = Depends(get_current_user)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT id::text, entity_type, name, description, metadata,
                      related_entities, created::text, updated::text
               FROM entities WHERE id = $1''',
            entity_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Entity not found")
    return dict(row)


@router.patch("/entities/{entity_id}")
async def update_entity(
    entity_id: str,
    req: EntityUpdateRequest,
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    updates = {}
    if req.description is not None:
        updates['description'] = req.description
    if req.metadata is not None:
        updates['metadata'] = req.metadata
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        async with _lc._pool.acquire() as conn:
            if 'description' in updates and 'metadata' in updates:
                row = await conn.fetchrow(
                    '''UPDATE entities SET description=$1, metadata=$2, updated=NOW()
                       WHERE id=$3
                       RETURNING id::text, entity_type, name, description, metadata, created::text, updated::text''',
                    updates['description'], updates['metadata'], entity_id
                )
            elif 'description' in updates:
                row = await conn.fetchrow(
                    '''UPDATE entities SET description=$1, updated=NOW() WHERE id=$2
                       RETURNING id::text, entity_type, name, description, metadata, created::text, updated::text''',
                    updates['description'], entity_id
                )
            else:
                row = await conn.fetchrow(
                    '''UPDATE entities SET metadata=$1, updated=NOW() WHERE id=$2
                       RETURNING id::text, entity_type, name, description, metadata, created::text, updated::text''',
                    updates['metadata'], entity_id
                )
        if not row:
            raise HTTPException(status_code=404, detail="Entity not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/entities/{entity_id}/link", status_code=200)
async def link_entities(
    entity_id: str,
    req: EntityLinkRequest,
    user: UserContext = Depends(get_current_user),
):
    """Link two entities bidirectionally via related_entities UUID[] array."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        async with _lc._pool.acquire() as conn:
            # Verify both exist
            count = await conn.fetchval(
                'SELECT COUNT(*) FROM entities WHERE id = ANY($1::uuid[])',
                [entity_id, req.related_id]
            )
            if count < 2:
                raise HTTPException(status_code=404, detail="One or both entities not found")
            # Link A->B
            await conn.execute(
                '''UPDATE entities
                   SET related_entities = array_append(
                       COALESCE(related_entities, ARRAY[]::uuid[]), $2::uuid
                   ), updated = NOW()
                   WHERE id = $1
                   AND NOT ($2::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[])))''',
                entity_id, req.related_id
            )
            # Link B->A
            await conn.execute(
                '''UPDATE entities
                   SET related_entities = array_append(
                       COALESCE(related_entities, ARRAY[]::uuid[]), $2::uuid
                   ), updated = NOW()
                   WHERE id = $1
                   AND NOT ($2::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[])))''',
                req.related_id, entity_id
            )
        return {"status": "linked", "entity_id": entity_id, "related_id": req.related_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/entities/{entity_id}", status_code=204)
async def delete_entity(entity_id: str, user: UserContext = Depends(get_current_user)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    try:
        async with _lc._pool.acquire() as conn:
            # Remove from other entities' arrays
            await conn.execute(
                '''UPDATE entities
                   SET related_entities = array_remove(related_entities, $1::uuid)
                   WHERE $1::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[]))''',
                entity_id
            )
            result = await conn.execute('DELETE FROM entities WHERE id = $1', entity_id)
        if result == 'DELETE 0':
            raise HTTPException(status_code=404, detail="Entity not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entities/{entity_id}/related")
async def get_related_entities(entity_id: str, user: UserContext = Depends(get_current_user)):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        entity = await conn.fetchrow('SELECT related_entities FROM entities WHERE id = $1', entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    related_ids = entity['related_entities'] or []
    if not related_ids:
        return {"related": []}
    async with _lc._pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
               FROM entities WHERE id = ANY($1::uuid[])''',
            related_ids
        )
    return {"related": [dict(r) for r in rows]}
