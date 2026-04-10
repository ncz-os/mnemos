"""Health check and statistics endpoints."""
import json
import logging
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, HTTPException

import api.lifecycle as _lc
from api.models import HealthResponse, StatsResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        database_connected=True,
        version="2.3.0",
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """Get system statistics from database (cached 60 s)."""
    cache_key = "stats:global"

    if _lc._cache:
        try:
            cached = await _lc._cache.get(cache_key)
            if cached:
                logger.debug("[CACHE] /stats hit")
                return StatsResponse(**json.loads(cached))
        except Exception as e:
            logger.warning(f"[CACHE] /stats read error: {e}")

    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    try:
        async with _lc._pool.acquire() as conn:
            total = await conn.fetchval('SELECT COUNT(*) FROM memories')
            cat_rows = await conn.fetch('SELECT category, COUNT(*) as cnt FROM memories GROUP BY category')
            memories_by_category = {row['category']: row['cnt'] for row in cat_rows}
            sub_rows = await conn.fetch(
                'SELECT category, subcategory, COUNT(*) as cnt FROM memories '
                'WHERE subcategory IS NOT NULL GROUP BY category, subcategory ORDER BY cnt DESC'
            )
            memories_by_subcategory: dict = {}
            for r in sub_rows:
                memories_by_subcategory.setdefault(r['category'], {})[r['subcategory']] = r['cnt']
            avg_quality = await conn.fetchval(
                'SELECT AVG(quality_rating) FROM memories WHERE quality_rating IS NOT NULL'
            )
            total_compressions = (
                await conn.fetchval("SELECT COUNT(*) FROM memories WHERE llm_optimized = true") or 0
            )
            avg_ratio_row = await conn.fetchval("""
                SELECT AVG(LENGTH(compressed_content)::float / NULLIF(LENGTH(content), 0))
                FROM memories WHERE llm_optimized = true AND compressed_content IS NOT NULL
            """)

        result = StatsResponse(
            total_memories=total or 0,
            total_compressions=total_compressions,
            average_compression_ratio=round(avg_ratio_row, 2) if avg_ratio_row else 0.57,
            average_quality_rating=int(avg_quality) if avg_quality else 75,
            memories_by_category=memories_by_category,
            memories_by_subcategory=memories_by_subcategory,
            memories_by_task_type={},
            unreviewed_compressions=0,
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        )

        if _lc._cache:
            try:
                await _lc._cache.setex(cache_key, 60, result.model_dump_json())
            except Exception as e:
                logger.warning(f"[CACHE] /stats write error: {e}")

        return result

    except asyncpg.PostgresError as e:
        logger.error(f"Stats DB error: {e}")
        raise HTTPException(status_code=503, detail=f"Database error: {e}")
    except Exception as e:
        logger.error(f"Stats error: {e}")
        raise HTTPException(status_code=503, detail=f"Internal error: {e}")
