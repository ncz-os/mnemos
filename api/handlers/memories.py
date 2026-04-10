"""Memory CRUD, search, and rehydration endpoints."""
import asyncio
import json
import logging
import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException

import api.lifecycle as _lc
from api.lifecycle import (
    _MEMORY_COLS,
    _fts_fetch,
    _get_cache_key,
    _row_to_memory,
    COMPRESSION_ITEM_THRESHOLD,
    COMPRESSION_RESULT_SET_THRESHOLD,
)
from api.models import (
    MemoryCreateRequest,
    MemoryItem,
    MemoryListResponse,
    MemorySearchRequest,
    RehydrationRequest,
    RehydrationResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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
    limit: int = 20,
    offset: int = 0,
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
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
async def get_memory(memory_id: str):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1", memory_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        return _row_to_memory(row, include_compressed=True)


@router.post("/memories/search", response_model=MemoryListResponse)
async def search_memories(request: MemorySearchRequest):
    """Search memories with optional compression of large result sets (cached 5 min)."""
    cache_key = _get_cache_key(
        "search", request.query, request.limit,
        request.category or "", request.subcategory or "",
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
        rows = await _fts_fetch(
            conn, request.query, request.limit,
            request.category, request.subcategory,
        )

    memories = [_row_to_memory(r, include_compressed=request.include_compressed) for r in rows]
    compression_applied = False
    compression_metadata = {}
    total_size = sum(len(m.content) for m in memories)

    if total_size > COMPRESSION_RESULT_SET_THRESHOLD:
        provider = _lc.get_inference_provider()
        cerberus_healthy = await provider.health_check()
        if cerberus_healthy:
            logger.info(f"[PHASE2] Result set {total_size} bytes > threshold, applying compression")
            compressed_count = 0
            total_original = total_size
            total_compressed = 0
            quality_scores = []
            for memory in memories:
                item_size = len(memory.content)
                if item_size > COMPRESSION_ITEM_THRESHOLD and not memory.compressed_content:
                    result = await provider.compress(memory.content, target_ratio=0.35, min_quality=70)
                    if result['success']:
                        memory.compressed_content = result['compressed']
                        quality_scores.append(result['quality_score'])
                        total_compressed += result['compressed_length']
                        compressed_count += 1
                        logger.info(
                            f"[PHASE2] Compressed {memory.id[:8]}: "
                            f"{item_size} -> {result['compressed_length']} chars"
                        )
                        asyncio.create_task(_persist_compression(memory.id, result['compressed']))
                    else:
                        total_compressed += item_size
                        logger.warning(f"[PHASE2] Compression failed for {memory.id[:8]}: {result['error']}")
                else:
                    total_compressed += item_size
            if compressed_count > 0:
                compression_applied = True
                avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0
                compression_metadata = {
                    'items_compressed': compressed_count,
                    'total_items': len(memories),
                    'original_bytes': total_original,
                    'compressed_bytes': total_compressed,
                    'compression_ratio': round(total_compressed / max(total_original, 1), 3),
                    'average_quality_score': round(avg_quality, 1),
                    'threshold_triggered': COMPRESSION_RESULT_SET_THRESHOLD,
                }
        else:
            logger.warning("[PHASE2] CERBERUS unavailable, skipping compression")

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


@router.post("/memories", response_model=MemoryItem)
async def create_memory(request: MemoryCreateRequest):
    if not request.content or not request.content.strip():
        raise HTTPException(status_code=422, detail="Memory content cannot be empty")
    mem_id = f"mem_{uuid.uuid4().hex[:12]}"
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        meta = json.dumps(request.metadata or {"source": request.source})
        await conn.execute(
            "INSERT INTO memories (id, content, category, subcategory, metadata, quality_rating) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, 75)",
            mem_id, request.content, request.category, request.subcategory, meta,
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


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: str):
    """Delete a memory by ID."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
        result = await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
        except Exception:
            pass


@router.post("/memories/rehydrate", response_model=RehydrationResponse)
async def rehydrate_memories(request: RehydrationRequest):
    """Return memories optimized for Claude context injection (Phase 5)."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    async with _lc._pool.acquire() as conn:
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
    provider = _lc.get_inference_provider()
    result = await provider.prepare_context(combined_context, max_tokens=request.budget_tokens)
    compression_applied = result['ratio'] < 0.99
    logger.info(
        f"[REHYDRATE] query='{request.query[:30]}' | memories={len(rows)} | "
        f"original_tokens={original_tokens} | tokens_used={result['tokens_used']} | "
        f"ratio={result['ratio']:.2%} | quality={result['quality_score']} | "
        f"compressed={compression_applied}"
    )
    return RehydrationResponse(
        context=result['context_for_injection'],
        tokens_used=result['tokens_used'],
        original_tokens=original_tokens,
        compression_ratio=round(result['ratio'], 3),
        quality_score=result['quality_score'],
        memories_included=len(rows),
        compression_applied=compression_applied,
    )
