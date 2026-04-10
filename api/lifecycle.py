"""Shared globals, lifespan, and DB/cache helpers for MNEMOS API."""
import hashlib
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import HTTPException
import redis.asyncio as aioredis

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from external_inference_provider import ExternalInferenceProvider
from graeae_providers import get_graeae_engine  # noqa: F401 — re-exported for handlers

from .models import MemoryItem

logger = logging.getLogger(__name__)

# Compression thresholds
COMPRESSION_RESULT_SET_THRESHOLD = 50 * 1024   # 50 KB
COMPRESSION_ITEM_THRESHOLD = 5 * 1024           # 5 KB per item

# DB config from environment (mirrors config.py defaults)
PG_PASSWORD = os.getenv('PG_PASSWORD', 'mnemos_secure_password')
PG_USER = os.getenv('PG_USER', 'mnemos_user')
PG_DATABASE = os.getenv('PG_DATABASE', 'mnemos')
PG_HOST = os.getenv('PG_HOST', 'localhost')

# ── Singleton globals ────────────────────────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None
_cache: Optional[aioredis.Redis] = None
_inference_provider: Optional[ExternalInferenceProvider] = None


def get_inference_provider() -> ExternalInferenceProvider:
    global _inference_provider
    if _inference_provider is None:
        _inference_provider = ExternalInferenceProvider()
    return _inference_provider


# ── App lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan: initialize and teardown DB pool, Redis, and inference provider."""
    global _pool, _cache
    logger.info("Starting MNEMOS API Server v2.3.0 (hierarchy + knowledge graph + MCP)")

    try:
        _pool = await asyncpg.create_pool(
            user=PG_USER, password=PG_PASSWORD,
            database=PG_DATABASE, host=PG_HOST,
            min_size=5, max_size=20,
        )
        logger.info("asyncpg connection pool initialized (min=5, max=20)")
    except Exception as e:
        logger.error(f"Failed to create DB pool: {e}")
        raise

    try:
        _cache = aioredis.from_url("redis://localhost:6379", decode_responses=True)
        await _cache.ping()
        app.state.cache = _cache
        logger.info("Redis cache connected (localhost:6379)")
    except Exception as e:
        logger.warning(f"Redis unavailable, caching disabled: {e}")
        _cache = None
        app.state.cache = None

    provider = get_inference_provider()
    healthy = await provider.health_check()
    if healthy:
        logger.info("ExternalInferenceProvider: CERBERUS llama-server CONNECTED")
    else:
        logger.warning("ExternalInferenceProvider: CERBERUS llama-server UNREACHABLE - compression disabled")

    yield

    if _pool:
        await _pool.close()
        logger.info("DB pool closed")
    if _cache:
        await _cache.aclose()
        logger.info("Redis cache closed")
    await provider.close()
    logger.info("Shutting down MNEMOS API Server")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_cache_key(prefix: str, *args) -> str:
    """Generate a stable cache key from prefix and arguments."""
    raw = prefix + ":" + ":".join(str(a) for a in args)
    return hashlib.md5(raw.encode()).hexdigest()


async def _get_db():
    """Acquire a connection from the pool."""
    global _pool
    if not _pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    return _pool.acquire()


_MEMORY_COLS = (
    "id, content, category, subcategory, created, updated, "
    "metadata, quality_rating, compressed_content"
)


def _row_to_memory(row, include_compressed: bool = False) -> MemoryItem:
    raw_meta = row.get('metadata')
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except Exception:
            raw_meta = None
    elif not isinstance(raw_meta, dict):
        raw_meta = None
    return MemoryItem(
        id=row['id'],
        content=row['content'][:2000],
        category=row['category'],
        subcategory=row.get('subcategory'),
        created=row['created'].isoformat() if row['created'] else '',
        updated=row['updated'].isoformat() if row.get('updated') else None,
        metadata=raw_meta if raw_meta else None,
        quality_rating=row.get('quality_rating'),
        compressed_content=row.get('compressed_content') if include_compressed else None,
    )


async def _fts_fetch(conn, query: str, limit: int,
                     category=None,
                     subcategory=None,
                     select_cols=None):
    """FTS search with ILIKE fallback. Shared by /memories/search and /memories/rehydrate."""
    if select_cols is None:
        select_cols = _MEMORY_COLS
    query_tsv = " & ".join(query.split())
    rank_col = "ts_rank(to_tsvector('english', content), to_tsquery('english', $1)) as rank"
    try:
        if category and subcategory:
            return await conn.fetch(
                f"SELECT {select_cols}, {rank_col} FROM memories "
                "WHERE to_tsvector('english', content) @@ to_tsquery('english', $1) "
                "AND category=$3 AND subcategory=$4 ORDER BY rank DESC LIMIT $2",
                query_tsv, limit, category, subcategory,
            )
        elif category:
            return await conn.fetch(
                f"SELECT {select_cols}, {rank_col} FROM memories "
                "WHERE to_tsvector('english', content) @@ to_tsquery('english', $1) "
                "AND category=$3 ORDER BY rank DESC LIMIT $2",
                query_tsv, limit, category,
            )
        elif subcategory:
            return await conn.fetch(
                f"SELECT {select_cols}, {rank_col} FROM memories "
                "WHERE to_tsvector('english', content) @@ to_tsquery('english', $1) "
                "AND subcategory=$3 ORDER BY rank DESC LIMIT $2",
                query_tsv, limit, subcategory,
            )
        else:
            return await conn.fetch(
                f"SELECT {select_cols}, {rank_col} FROM memories "
                "WHERE to_tsvector('english', content) @@ to_tsquery('english', $1) "
                "ORDER BY rank DESC LIMIT $2",
                query_tsv, limit,
            )
    except Exception:
        logger.warning(f"[FTS] falling back to ILIKE for: {query[:50]!r}")
        like_q = f"%{query}%"
        try:
            if category and subcategory:
                return await conn.fetch(
                    f"SELECT {select_cols} FROM memories "
                    "WHERE content ILIKE $1 AND category=$3 AND subcategory=$4 "
                    "ORDER BY created DESC LIMIT $2",
                    like_q, limit, category, subcategory,
                )
            elif category:
                return await conn.fetch(
                    f"SELECT {select_cols} FROM memories "
                    "WHERE content ILIKE $1 AND category=$3 ORDER BY created DESC LIMIT $2",
                    like_q, limit, category,
                )
            elif subcategory:
                return await conn.fetch(
                    f"SELECT {select_cols} FROM memories "
                    "WHERE content ILIKE $1 AND subcategory=$3 ORDER BY created DESC LIMIT $2",
                    like_q, limit, subcategory,
                )
            else:
                return await conn.fetch(
                    f"SELECT {select_cols} FROM memories "
                    "WHERE content ILIKE $1 ORDER BY created DESC LIMIT $2",
                    like_q, limit,
                )
        except Exception as e2:
            logger.error(f"[FTS] Both FTS and ILIKE failed: {e2}")
            return []
