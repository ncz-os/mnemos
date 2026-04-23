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


def _is_root(user: UserContext) -> bool:
    """Root callers see all rows regardless of namespace — they're
    the operational tier. Everyone else is scoped to their own
    namespace at the app layer, as defense-in-depth against RLS
    being disabled in personal-mode installs."""
    return user.role == "root"


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

    # v3.1.2: app-layer namespace enforcement. When RLS is disabled
    # (personal mode), the memory handlers previously saw every row
    # regardless of the caller's namespace. The filter below scopes
    # non-root callers to their namespace without needing RLS.
    scope_to_ns = not _is_root(user)

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            if category and subcategory:
                if scope_to_ns:
                    rows = await conn.fetch(
                        f"SELECT {_MEMORY_COLS} FROM memories "
                        "WHERE category=$1 AND subcategory=$2 AND namespace=$3 "
                        "ORDER BY created DESC LIMIT $4 OFFSET $5",
                        category, subcategory, user.namespace, limit, offset,
                    )
                    total = await conn.fetchval(
                        "SELECT COUNT(*) FROM memories WHERE category=$1 AND subcategory=$2 AND namespace=$3",
                        category, subcategory, user.namespace,
                    )
                else:
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
                if scope_to_ns:
                    rows = await conn.fetch(
                        f"SELECT {_MEMORY_COLS} FROM memories "
                        "WHERE category=$1 AND namespace=$2 "
                        "ORDER BY created DESC LIMIT $3 OFFSET $4",
                        category, user.namespace, limit, offset,
                    )
                    total = await conn.fetchval(
                        "SELECT COUNT(*) FROM memories WHERE category=$1 AND namespace=$2",
                        category, user.namespace,
                    )
                else:
                    rows = await conn.fetch(
                        f"SELECT {_MEMORY_COLS} FROM memories "
                        "WHERE category=$1 ORDER BY created DESC LIMIT $2 OFFSET $3",
                        category, limit, offset,
                    )
                    total = await conn.fetchval(
                        "SELECT COUNT(*) FROM memories WHERE category=$1", category)
            elif subcategory:
                if scope_to_ns:
                    rows = await conn.fetch(
                        f"SELECT {_MEMORY_COLS} FROM memories "
                        "WHERE subcategory=$1 AND namespace=$2 "
                        "ORDER BY created DESC LIMIT $3 OFFSET $4",
                        subcategory, user.namespace, limit, offset,
                    )
                    total = await conn.fetchval(
                        "SELECT COUNT(*) FROM memories WHERE subcategory=$1 AND namespace=$2",
                        subcategory, user.namespace,
                    )
                else:
                    rows = await conn.fetch(
                        f"SELECT {_MEMORY_COLS} FROM memories "
                        "WHERE subcategory=$1 ORDER BY created DESC LIMIT $2 OFFSET $3",
                        subcategory, limit, offset,
                    )
                    total = await conn.fetchval(
                        "SELECT COUNT(*) FROM memories WHERE subcategory=$1", subcategory)
            else:
                if scope_to_ns:
                    rows = await conn.fetch(
                        f"SELECT {_MEMORY_COLS} FROM memories "
                        "WHERE namespace=$1 ORDER BY created DESC LIMIT $2 OFFSET $3",
                        user.namespace, limit, offset,
                    )
                    total = await conn.fetchval(
                        "SELECT COUNT(*) FROM memories WHERE namespace=$1",
                        user.namespace,
                    )
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
            if _is_root(user):
                row = await conn.fetchrow(
                    f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1", memory_id,
                )
            else:
                # Non-root: 404 on namespace mismatch. Returning 403
                # would leak existence of memories in other namespaces;
                # 404 is uniform with the "not found" response.
                row = await conn.fetchrow(
                    f"SELECT {_MEMORY_COLS} FROM memories "
                    "WHERE id=$1 AND namespace=$2",
                    memory_id, user.namespace,
                )
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")
    return _row_to_memory(row, include_compressed=True)


def _render_content_preview(content: Optional[str], include_content: bool) -> Optional[str]:
    """Full content when the caller asked for it, first-200-chars preview
    otherwise. Returning None stays None — the engine produced no output."""
    if content is None:
        return None
    if include_content:
        return content
    return content if len(content) <= 200 else content[:200] + "…"


@router.get("/memories/{memory_id}/compression-manifests")
async def get_compression_manifests(
    memory_id: str,
    include_content: bool = Query(
        False,
        description=(
            "Return full compressed_content for the winning variant and "
            "every candidate. Default returns a 200-character preview to "
            "keep responses small; flip for deep audit inspection."
        ),
    ),
    user: UserContext = Depends(get_current_user),
):
    """Return the v3.1 compression audit trail for a memory.

    Two sections:
      * `variant`  — the current winning dense form (or null if no contest
                     has produced a winner yet). Pointer into the contest
                     candidate that "won" most recently.
      * `contests` — every historical contest, grouped by contest_id,
                     ordered most recent first. Each contest lists every
                     engine attempt with scoring fields and reject_reason.

    The response shape mirrors the v3.1 compression schema exactly so
    operators can reason about what was tried, what scored how, and why
    each engine was or wasn't picked.
    """
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    async with _lc._pool.acquire() as conn:
        async with _rls_context(conn, user):
            # Enforce memory visibility under RLS before returning manifests.
            # Avoids leaking manifest existence for memories the caller
            # cannot see.
            exists = await conn.fetchval(
                "SELECT 1 FROM memories WHERE id = $1",
                memory_id,
            )
            if not exists:
                raise HTTPException(status_code=404, detail="Memory not found")

            variant_row = await conn.fetchrow(
                """
                SELECT engine_id, engine_version, compressed_content,
                       compressed_tokens, compression_ratio, quality_score,
                       composite_score, scoring_profile, judge_model,
                       selected_at, winner_candidate_id
                FROM memory_compressed_variants
                WHERE memory_id = $1
                """,
                memory_id,
            )

            candidate_rows = await conn.fetch(
                """
                SELECT contest_id, engine_id, engine_version,
                       compressed_content, original_tokens, compressed_tokens,
                       compression_ratio, quality_score, speed_factor,
                       composite_score, scoring_profile, elapsed_ms,
                       judge_model, gpu_used, is_winner, reject_reason,
                       manifest, created
                FROM memory_compression_candidates
                WHERE memory_id = $1
                ORDER BY created ASC, is_winner DESC, engine_id
                """,
                memory_id,
            )

    variant: Optional[dict] = None
    if variant_row is not None:
        variant = {
            "engine_id": variant_row["engine_id"],
            "engine_version": variant_row["engine_version"],
            "compressed_content": _render_content_preview(
                variant_row["compressed_content"], include_content,
            ),
            "compressed_tokens": variant_row["compressed_tokens"],
            "compression_ratio": variant_row["compression_ratio"],
            "quality_score": variant_row["quality_score"],
            "composite_score": variant_row["composite_score"],
            "scoring_profile": variant_row["scoring_profile"],
            "judge_model": variant_row["judge_model"],
            "selected_at": (
                variant_row["selected_at"].isoformat()
                if variant_row["selected_at"] else None
            ),
            "winner_candidate_id": (
                str(variant_row["winner_candidate_id"])
                if variant_row["winner_candidate_id"] else None
            ),
        }

    contests: dict[str, dict] = {}
    for row in candidate_rows:
        cid = str(row["contest_id"])
        bucket = contests.setdefault(cid, {
            "contest_id": cid,
            "started_at": row["created"],
            "candidates": [],
        })
        # earliest created row's timestamp represents the contest start
        if row["created"] < bucket["started_at"]:
            bucket["started_at"] = row["created"]

        manifest_field = row["manifest"]
        if isinstance(manifest_field, str):
            try:
                manifest_field = json.loads(manifest_field)
            except Exception:
                manifest_field = {"_raw": manifest_field}

        bucket["candidates"].append({
            "engine_id": row["engine_id"],
            "engine_version": row["engine_version"],
            "compressed_content": _render_content_preview(
                row["compressed_content"], include_content,
            ),
            "original_tokens": row["original_tokens"],
            "compressed_tokens": row["compressed_tokens"],
            "compression_ratio": row["compression_ratio"],
            "quality_score": row["quality_score"],
            "speed_factor": row["speed_factor"],
            "composite_score": row["composite_score"],
            "scoring_profile": row["scoring_profile"],
            "elapsed_ms": row["elapsed_ms"],
            "judge_model": row["judge_model"],
            "gpu_used": row["gpu_used"],
            "is_winner": row["is_winner"],
            "reject_reason": row["reject_reason"],
            "manifest": manifest_field,
            "created": row["created"].isoformat(),
        })

    contests_list = sorted(
        (
            {**bucket, "started_at": bucket["started_at"].isoformat()}
            for bucket in contests.values()
        ),
        key=lambda c: c["started_at"],
        reverse=True,
    )

    return {
        "memory_id": memory_id,
        "variant": variant,
        "contests": contests_list,
    }


@router.post("/memories/search", response_model=MemoryListResponse)
async def search_memories(
    request: MemorySearchRequest,
    user: UserContext = Depends(get_current_user),
):
    """Search memories with optional compression of large result sets (cached 5 min)."""
    request_limit = min(request.limit, 500)  # server-side cap regardless of model field
    # Cache key MUST include user.user_id and namespace: when RLS is enabled,
    # different users get different row sets for the same query — caching
    # without scoping was an RLS bypass.
    cache_key = _get_cache_key(
        "search",
        user.user_id, user.namespace,
        request.query, request_limit,
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
                f"on-the-fly compression deferred to Phase 8A"
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

    # Only root may create a memory attributed to a different owner or namespace
    # than the caller. Previously any user could set request.owner_id and
    # ghost-write memories under someone else's identity.
    if request.owner_id and request.owner_id != user.user_id and user.role != "root":
        raise HTTPException(status_code=403, detail="owner_id override requires root")
    if request.namespace and request.namespace != user.namespace and user.role != "root":
        raise HTTPException(status_code=403, detail="namespace override requires root")
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
                # (trigger trg_memory_version_insert inserts version 1 automatically,
                # computing commit_hash + branch; no explicit handler INSERT needed)
                row = await conn.fetchrow(
                    f"SELECT {_MEMORY_COLS} FROM memories WHERE id=$1", mem_id,
                )
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
            # Invalidate per-user search caches on mutation. Keys are
            # namespaced "mnemos:search:*" so SCAN MATCH is bounded to our
            # entries and safe against shared Redis.
            try:
                async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                    await _lc._cache.delete(_k)
            except Exception:
                pass
        except Exception:
            pass
    try:
        from api.webhook_dispatcher import dispatch as _dispatch_webhook
        async with _lc._pool.acquire() as _wh_conn:
            await _dispatch_webhook(_wh_conn, "memory.created", {
                "memory_id": mem_id,
                "category": request.category,
                "subcategory": request.subcategory,
                "content": request.content,
                "owner_id": owner_id,
                "namespace": namespace,
            }, owner_id=owner_id, namespace=namespace)
    except Exception:
        logger.warning("webhook dispatch failed for memory.created %s", mem_id, exc_info=True)
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
                    if mem.owner_id and mem.owner_id != user.user_id and user.role != "root":
                        errors.append(f"[{i}] owner_id override requires root")
                        continue
                    if mem.namespace and mem.namespace != user.namespace and user.role != "root":
                        errors.append(f"[{i}] namespace override requires root")
                        continue
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
            # Invalidate per-user search caches on mutation. Keys are
            # namespaced "mnemos:search:*" so SCAN MATCH is bounded to our
            # entries and safe against shared Redis.
            try:
                async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                    await _lc._cache.delete(_k)
            except Exception:
                pass
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
                # Defense-in-depth: don't rely exclusively on RLS. Explicitly
                # check ownership so that an install with rls_enabled=false
                # still prevents cross-user edits.
                if user.role == "root":
                    row = await conn.fetchrow(
                        "SELECT id FROM memories WHERE id=$1", memory_id,
                    )
                else:
                    row = await conn.fetchrow(
                        "SELECT id FROM memories WHERE id=$1 AND owner_id=$2",
                        memory_id, user.user_id,
                    )
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
            # Invalidate per-user search caches on mutation. Keys are
            # namespaced "mnemos:search:*" so SCAN MATCH is bounded to our
            # entries and safe against shared Redis.
            try:
                async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                    await _lc._cache.delete(_k)
            except Exception:
                pass
        except Exception:
            pass
    try:
        from api.webhook_dispatcher import dispatch as _dispatch_webhook
        async with _lc._pool.acquire() as _wh_conn:
            await _dispatch_webhook(_wh_conn, "memory.updated", {
                "memory_id": memory_id,
                "category": row["category"],
                "subcategory": row["subcategory"],
                "content": row["content"],
                "owner_id": row["owner_id"],
                "namespace": row["namespace"],
            }, owner_id=row["owner_id"], namespace=row["namespace"])
    except Exception:
        logger.warning("webhook dispatch failed for memory.updated %s", memory_id, exc_info=True)
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
            if user.role == "root":
                result = await conn.execute(
                    "DELETE FROM memories WHERE id = $1", memory_id,
                )
            else:
                result = await conn.execute(
                    "DELETE FROM memories WHERE id = $1 AND owner_id = $2",
                    memory_id, user.user_id,
                )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    if _lc._cache:
        try:
            await _lc._cache.delete("stats:global")
            # Invalidate per-user search caches on mutation. Keys are
            # namespaced "mnemos:search:*" so SCAN MATCH is bounded to our
            # entries and safe against shared Redis.
            try:
                async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
                    await _lc._cache.delete(_k)
            except Exception:
                pass
        except Exception:
            pass
    try:
        from api.webhook_dispatcher import dispatch as _dispatch_webhook
        async with _lc._pool.acquire() as _wh_conn:
            await _dispatch_webhook(_wh_conn, "memory.deleted", {
                "memory_id": memory_id,
                "owner_id": user.user_id,
                "namespace": user.namespace,
            }, owner_id=user.user_id, namespace=user.namespace)
    except Exception:
        logger.warning("webhook dispatch failed for memory.deleted %s", memory_id, exc_info=True)


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
    # Gateway prioritizes memory injection compression (critical path)
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
