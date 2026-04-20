"""Memory CRUD, search, and rehydration endpoints."""
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from api.lifecycle import (
    _MEMORY_COLS,
    _fts_fetch,
    _get_cache_key,
    _get_embedding,
    _row_to_memory,
    _vector_search,
    COMPRESSION_ITEM_THRESHOLD,
    COMPRESSION_RESULT_SET_THRESHOLD,
)
from api.models import (
    BulkCreateRequest,
    BulkCreateResponse,
    MemoryCreateRequest,
    MemoryItem,
    MemoryListResponse,
    MemorySearchRequest,
    MemoryUpdateRequest,
    RehydrationRequest,
    RehydrationResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["memories"])


@asynccontextmanager
async def _rls_context(conn, user: UserContext):
    """Set PostgreSQL session variables for RLS when auth is active."""
    if _lc._rls_enabled and user.authenticated:
        async with conn.transaction():
            await conn.execute(
                "SET LOCAL mnemos.current_user_id = $1", user.user_id
            )
            await conn.execute(
                "SET LOCAL mnemos.current_role = $1", user.role
            )
            yield conn
    else:
        yield conn


async def _persist_compression(memory_id: str, compressed_text: str) -> None:
    """Persist on-the-fly compression back to DB. Guard: only updates if compressed_content IS NULL."""
    if not _lc._pool:
        return
    try:
        async with _lc._pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET compressed_content=$1, llm_optimized=true "
                "WHERE id=$2 AND compressed_content IS NULL",
                compressed_text, memory_id,
            )
        logger.debug(f"[PHASE2] Persisted compression for {memory_id[:8]}")
    except Exception as e:
        logger.warning(f"[PHASE2] Failed to persist compression for {memory_id}: {e}")


@router.get("/memories", response_model=MemoryListResponse)
async def list_memories(
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            if category and subcategory:
                rows = await conn.fetch(
                    f"SELECT {_MEMORY_COLS} FROM memories "
                    "WHERE category=$1 AND subcategory=$2 ORDER BY created DESC LIMIT $3 OFFSET $4",
                    category, subcategory, limit, offset,
                )
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE category=$1 AND subcategory=$2",
                    category, subcategory,
                )
            elif category:
                rows = await conn.fetch(
                    f"SELECT {_MEMORY_COLS} FROM memories "
                    "WHERE category=$1 ORDER BY created DESC LIMIT $2 OFFSET $3",
                    category, limit, offset,
                )
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE category=$1", category)
            elif subcategory:
                rows = await conn.fetch(
                    f"SELECT {_MEMORY_COLS} FROM memories "
                    "WHERE subcategory=$1 ORDER BY created DESC LIMIT $2 OFFSET $3",
                    subcategory, limit, offset,
                )
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE subcategory=$1", subcategory)
            else:
                rows = await conn.fetch(
                    f"SELECT {_MEMORY_COLS} FROM memories ORDER BY created DESC LIMIT $1 OFFSET $2",
                    limit, offset,
                )
                total = await conn.fetchval("SELECT COUNT(*) FROM memories")
    return MemoryListResponse(count=total, memories=[_row_to_memory(r) for r in rows])


@router.get("/memories/{memory_id}", response_model=MemoryItem)
async def get_memory(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            row = await conn.fetchrow(
                f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1", memory_id,
            )
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")
    return _row_to_memory(row, include_compressed=True)


@router.post("/memories/search", response_model=MemoryListResponse)
async def search_memories(
    request: MemorySearchRequest,
    user: UserContext = Depends(get_current_user),
):
    """Search memories with optional compression of large result sets (cached 5 min)."""
    request_limit = min(request.limit, 500)  # server-side cap regardless of model field
    cache_key = _get_cache_key(
        "search", request.query, request_limit,
        request.category or "", request.subcategory or "",
        "semantic" if request.semantic else "fts",
        request.source_provider or "", request.source_model or "",
        request.source_agent or "", request.namespace or "",
    )

    if _lc._cache and not request.include_compressed:
        try:
            cached = await _lc._cache.get(cache_key)
            if cached:
                logger.debug(f"[CACHE] /memories/search hit for '{request.query[:30]}'")
                return MemoryListResponse(**json.loads(cached))
        except Exception as e:
            logger.warning(f"[CACHE] search read error: {e}")

    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            _prov = dict(
                source_provider=request.source_provider,
                source_model=request.source_model,
                source_agent=request.source_agent,
                namespace=request.namespace,
            )
            if request.semantic:
                embedding = await _get_embedding(request.query)
                if not embedding:
                    logger.warning("[VECTOR] Embedding failed, falling back to FTS")
                    rows = await _fts_fetch(
                        conn, request.query, request_limit,
                        request.category, request.subcategory,
                        **_prov,
                    )
                else:
                    logger.info(f"[VECTOR] Semantic search: {len(embedding)}-dim vector")
                    rows = await _vector_search(
                        conn, embedding, request_limit,
                        request.category, request.subcategory,
                        **_prov,
                    )
            else:
                rows = await _fts_fetch(
                    conn, request.query, request_limit,
                    request.category, request.subcategory,
                    **_prov,
                )

    memories = [_row_to_memory(r, include_compressed=request.include_compressed) for r in rows]
    compression_applied = False
    compression_metadata = {}
    total_size = sum(len(m.content) for m in memories)

    if total_size > COMPRESSION_RESULT_SET_THRESHOLD:
        backend = _lc.get_inference_backend()
        backend_healthy = await backend.health_check()
        if backend_healthy:
            # Phase 2 complete: compression stack available (LETHE/ALETHEIA/ANAMNESIS)
            # On-the-fly search result compression deferred to Phase 8A (batch optimization)
            # Gateway uses LETHE compression for memory injection (critical path)
            logger.debug(
                f"[COMPRESSION] Result set {total_size} bytes > threshold; "
                f"on-the-fly compression deferred to Phase 8A (see MNEMOS_v24_IMPLEMENTATION_NOTES.md)"
            )
        else:
            logger.warning("[PHASE2] distillation backend unavailable, skipping compression")

    response = MemoryListResponse(
        count=len(memories),
        memories=memories,
        compression_applied=compression_applied,
        compression_metadata=compression_metadata if compression_applied else None,
    )

    if _lc._cache and not request.include_compressed and not compression_applied:
        try:
            await _lc._cache.setex(cache_key, 300, response.model_dump_json())
        except Exception as e:
            logger.warning(f"[CACHE] search write error: {e}")

    return response


@router.post("/memories", response_model=MemoryItem, status_code=201)
async def create_memory(
    request: MemoryCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    if not request.content or not request.content.strip():
        raise HTTPException(status_code=422, detail="Memory content cannot be empty")
    mem_id = f"mem_{uuid.uuid4().hex[:12]}"
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    owner_id = request.owner_id or user.user_id
    namespace = request.namespace or user.namespace

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            async with conn.transaction():
                meta = json.dumps(request.metadata or {"source": request.source})
                verbatim = request.verbatim_content if request.verbatim_content is not None else request.content
                await conn.execute(
                    "INSERT INTO memories "
                    "(id, content, category, subcategory, metadata, quality_rating, verbatim_content, "
                    "owner_id, namespace, permission_mode, "
                    "source_model, source_provider, source_session, source_agent) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, $9, $10, $11, $12, $13)",
                    mem_id, request.content, request.category, request.subcategory, meta, verbatim,
                    owner_id, namespace, 600,
                    request.source_model, request.source_provider,
                    request.source_session, request.source_agent,
                )
                await conn.execute(
                    "INSERT INTO memory_versions "
                    "(memory_id, version_num, content, category, subcategory, metadata, verbatim_content, "
                    "owner_id, namespace, permission_mode, "
                    "source_model, source_provider, source_session, source_agent, "
                    "snapshot_by, change_type) "
                    "VALUES ($1, 1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12, $13, $14, 'create') "
                    "ON CONFLICT (memory_id, version_num) DO NOTHING",
                    mem_id, request.content, request.category, request.subcategory, meta, verbatim,
                    owner_id, namespace, 600,
                    request.source_model, request.source_provider,
                    request.source_session, request.source_agent,
                    user.user_id,
                )
                row = await conn.fetchrow(
                    f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1", mem_id,
                )
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
        except Exception:
            pass
    return _row_to_memory(row)


@router.post("/memories/bulk", response_model=BulkCreateResponse, status_code=201)
async def bulk_create_memories(
    request: BulkCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create multiple memories in one request. Per-item errors are collected, not raised."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    import time as _time
    created_ids: list[str] = []
    errors: list[str] = []
    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            for i, mem in enumerate(request.memories):
                if not mem.content.strip():
                    errors.append(f"[{i}] content is empty")
                    continue
                try:
                    mid = f"mem_{int(_time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
                    verbatim = mem.verbatim_content if mem.verbatim_content is not None else mem.content
                    owner_id = mem.owner_id or user.user_id
                    namespace = mem.namespace or user.namespace
                    await conn.execute(
                        "INSERT INTO memories "
                        "(id, content, category, subcategory, metadata, quality_rating, verbatim_content, "
                        "owner_id, namespace, permission_mode, "
                        "source_model, source_provider, source_session, source_agent) "
                        "VALUES ($1, $2, $3, $4, $5::jsonb, 75, $6, $7, $8, $9, $10, $11, $12, $13)",
                        mid, mem.content, mem.category, mem.subcategory,
                        json.dumps(mem.metadata or {}), verbatim,
                        owner_id, namespace, 600,
                        mem.source_model, mem.source_provider,
                        mem.source_session, mem.source_agent,
                    )
                    created_ids.append(mid)
                except Exception as e:
                    errors.append(f"[{i}] {e}")
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
        except Exception:
            pass
    return BulkCreateResponse(created=len(created_ids), memory_ids=created_ids, errors=errors)


@router.patch("/memories/{memory_id}", response_model=MemoryItem)
async def update_memory(
    memory_id: str,
    request: MemoryUpdateRequest,
    user: UserContext = Depends(get_current_user),
):
    """Partially update a memory (content, category, subcategory, metadata)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    updates = {}
    if request.content is not None:
        if not request.content.strip():
            raise HTTPException(status_code=422, detail="Memory content cannot be empty")
        updates["content"] = request.content
    if request.category is not None:
        updates["category"] = request.category
    if request.subcategory is not None:
        updates["subcategory"] = request.subcategory
    if request.metadata is not None:
        updates["metadata"] = json.dumps(request.metadata)
    if request.verbatim_content is not None:
        updates["verbatim_content"] = request.verbatim_content
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    set_clauses = [f"{col}=${i+2}" for i, col in enumerate(updates.keys())]
    set_clauses.append("updated=NOW()")
    values = list(updates.values())

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            async with conn.transaction():
                row = await conn.fetchrow("SELECT id FROM memories WHERE id=$1", memory_id)
                if not row:
                    raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
                # Calculate next version BEFORE the UPDATE so trigger and app agree on the version number.
                # On instances with the pg trigger, the trigger fires during UPDATE and writes next_ver;
                # ON CONFLICT DO NOTHING ensures we skip cleanly. On instances without the trigger,
                # our INSERT writes the version.
                next_ver = await conn.fetchval(
                    "SELECT COALESCE(MAX(version_num), 0) + 1 FROM memory_versions WHERE memory_id = $1",
                    memory_id,
                )
                await conn.execute(
                    f"UPDATE memories SET {', '.join(set_clauses)} WHERE id=$1",
                    memory_id, *values,
                )
                row = await conn.fetchrow(
                    f"SELECT {_lc._MEMORY_COLS} FROM memories WHERE id=$1", memory_id,
                )
                # Snapshot the post-update state into version history
                await conn.execute(
                    "INSERT INTO memory_versions "
                    "(memory_id, version_num, content, category, subcategory, metadata, verbatim_content, "
                    "owner_id, namespace, permission_mode, "
                    "source_model, source_provider, source_session, source_agent, "
                    "snapshot_by, change_type) "
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12, $13, $14, $15, 'update') "
                    "ON CONFLICT (memory_id, version_num) DO NOTHING",
                    memory_id, next_ver, row["content"], row["category"], row["subcategory"],
                    json.dumps(row["metadata"] or {}),
                    row["verbatim_content"], row["owner_id"], row["namespace"], row["permission_mode"],
                    row["source_model"], row["source_provider"], row["source_session"], row["source_agent"],
                    user.user_id,
                )
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
        except Exception:
            pass
    return _lc._row_to_memory(row)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Delete a memory by ID."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            result = await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
        except Exception:
            pass


@router.post("/memories/rehydrate", response_model=RehydrationResponse)
async def rehydrate_memories(
    request: RehydrationRequest,
    user: UserContext = Depends(get_current_user),
):
    """Return memories optimized for Claude context injection (Phase 5)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            rows = await _fts_fetch(
                conn, request.query, request.limit, request.category,
                select_cols="id, content, category, created, compressed_content, quality_rating",
            )
    if not rows:
        return RehydrationResponse(
            context="", tokens_used=0, original_tokens=0,
            compression_ratio=1.0, quality_score=100,
            memories_included=0, compression_applied=False,
        )
    context_parts = []
    for row in rows:
        effective_content = row['compressed_content'] if row['compressed_content'] else row['content']
        created_str = row['created'].strftime('%Y-%m-%d') if row['created'] else 'unknown'
        context_parts.append(f"[{row['category']} / {created_str}]\n{effective_content[:2000]}")
    combined_context = "\n\n---\n\n".join(context_parts)
    original_tokens = int(len(combined_context) / 4)

    # Phase 2 complete: compression available (LETHE/ALETHEIA/ANAMNESIS)
    # Rehydration compression deferred to Phase 8A (batch optimization)
    # Gateway prioritizes memory injection compression (critical path v2.4.0)
    tokens_used = min(original_tokens, request.budget_tokens) if request.budget_tokens else original_tokens
    compression_applied = False  # Phase 8A: integrate LETHE for large context budgets

    logger.info(
        f"[REHYDRATE] query='{request.query[:30]}' | memories={len(rows)} | "
        f"original_tokens={original_tokens} | tokens_used={tokens_used} | "
        f"compression_applied={compression_applied}"
    )
    return RehydrationResponse(
        context=combined_context[:request.budget_tokens * 4] if request.budget_tokens else combined_context,
        tokens_used=tokens_used,
        original_tokens=original_tokens,
        compression_ratio=1.0,
        quality_score=100,
        memories_included=len(rows),
        compression_applied=compression_applied,
    )
