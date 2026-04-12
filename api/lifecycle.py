"""Shared globals, lifespan, and DB/cache helpers for MNEMOS API."""
import hashlib
import json
import logging
import os
import sys
import tomllib
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import httpx
from fastapi import HTTPException
import redis.asyncio as aioredis

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import PG_CONFIG
from external_inference_provider import ExternalInferenceProvider
from graeae.engine import get_graeae_engine  # noqa: F401 — re-exported for handlers

from .models import MemoryItem

logger = logging.getLogger(__name__)

# Compression thresholds
COMPRESSION_RESULT_SET_THRESHOLD = 50 * 1024   # 50 KB
COMPRESSION_ITEM_THRESHOLD = 5 * 1024           # 5 KB per item

# Background task registry — prevents dangling tasks at shutdown
_background_tasks: set = set()


def _schedule_background(coro) -> None:
    """Schedule a fire-and-forget coroutine with lifecycle tracking.

    Unlike asyncio.create_task(), tasks created here are tracked in
    _background_tasks so the lifespan teardown can await them before
    closing the DB pool.
    """
    import asyncio as _asyncio
    task = _asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

# DB config sourced from config.PG_CONFIG (env > config.toml > defaults)

# Embedding config (for vector search, MOD-02)
_EMBED_HOST = os.getenv('OLLAMA_EMBED_HOST', 'http://localhost:11434')
_EMBED_MODEL = os.getenv('OLLAMA_EMBED_MODEL', 'nomic-embed-text')
_EMBED_TIMEOUT = float(os.getenv('OLLAMA_EMBED_TIMEOUT', '10'))

# ── Singleton globals ────────────────────────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None
_cache: Optional[aioredis.Redis] = None
_inference_provider: Optional[ExternalInferenceProvider] = None
_rls_enabled: bool = False   # set from config at startup; read by handlers


def get_inference_provider() -> ExternalInferenceProvider:
    global _inference_provider
    if _inference_provider is None:
        _inference_provider = ExternalInferenceProvider()
    return _inference_provider


def _load_config() -> dict:
    """Load config.toml from standard locations. Returns empty dict if not found."""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.toml"),
        "/etc/mnemos/config.toml",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    return tomllib.load(f)
            except Exception as e:
                logger.warning(f"Failed to parse {path}: {e}")
    return {}


# ── App lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan: initialize and teardown DB pool, Redis, and inference provider."""
    global _pool, _cache, _rls_enabled
    logger.info("Starting MNEMOS API Server v2.3.0 (hierarchy + knowledge graph + MCP)")

    config = _load_config()

    try:
        _pool = await asyncpg.create_pool(
            user=PG_CONFIG['user'],
            password=PG_CONFIG['password'],
            database=PG_CONFIG['database'],
            host=PG_CONFIG['host'],
            port=PG_CONFIG['port'],
            min_size=PG_CONFIG['pool_min_size'],
            max_size=PG_CONFIG['pool_max_size'],
        )
        app.state.pool = _pool   # auth.py reads this via request.app.state.pool
        logger.info(
            f"asyncpg connection pool initialized "
            f"(min={PG_CONFIG['pool_min_size']}, max={PG_CONFIG['pool_max_size']})"
        )
    except Exception as e:
        logger.error(f"Failed to create DB pool: {e}")
        raise

    # Configure auth (personal profile: auth.enabled=false → no-op beyond singleton)
    from api.auth import configure_auth
    configure_auth(config.get("auth", {}))

    # RLS enforcement flag
    _rls_enabled = config.get("multiuser", {}).get("rls_enabled", False)
    if _rls_enabled:
        logger.info("Row Level Security: ENABLED (team/enterprise profile)")
    else:
        logger.info("Row Level Security: DISABLED (personal profile)")

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
        logger.info("ExternalInferenceProvider: inference-server llama-server CONNECTED")
    else:
        logger.warning("ExternalInferenceProvider: inference-server llama-server UNREACHABLE - compression disabled")

    yield

    if _background_tasks:
        logger.info(f"Draining {len(_background_tasks)} background task(s)…")
        import asyncio as _asyncio
        await _asyncio.gather(*list(_background_tasks), return_exceptions=True)

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
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


async def _get_db():
    """Acquire a connection from the pool."""
    global _pool
    if not _pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    return _pool.acquire()


_MEMORY_COLS = (
    "id, content, category, subcategory, created, updated, "
    "metadata, quality_rating, compressed_content, verbatim_content, "
    "owner_id, group_id, namespace, permission_mode, "
    "source_model, source_provider, source_session, source_agent"
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
        content=row['content'],
        category=row['category'],
        subcategory=row.get('subcategory'),
        created=row['created'].isoformat() if row['created'] else '',
        updated=row['updated'].isoformat() if row.get('updated') else None,
        metadata=raw_meta if raw_meta else None,
        quality_rating=row.get('quality_rating'),
        compressed_content=row.get('compressed_content') if include_compressed else None,
        verbatim_content=row.get('verbatim_content'),
        owner_id=row.get('owner_id'),
        group_id=row.get('group_id'),
        namespace=row.get('namespace'),
        permission_mode=row.get('permission_mode'),
        source_model=row.get('source_model'),
        source_provider=row.get('source_provider'),
        source_session=row.get('source_session'),
        source_agent=row.get('source_agent'),
    )


async def _get_embedding(text: str) -> list:
    """Get embedding vector from nomic-embed-text on inference-server. Returns [] on failure."""
    try:
        async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT) as client:
            r = await client.post(
                f"{_EMBED_HOST}/api/embeddings",
                json={"model": _EMBED_MODEL, "prompt": text[:2000]},
            )
            r.raise_for_status()
            return r.json().get("embedding", [])
    except Exception as e:
        logger.warning(f"[EMBED] Failed to get embedding: {e}")
        return []


async def _vector_search(conn, embedding: list, limit: int,
                         category=None, subcategory=None,
                         select_cols=None) -> list:
    """pgvector cosine similarity search. Returns rows ordered by similarity desc.

    The vector is always $1 — used in both the SELECT similarity expression and
    the ORDER BY clause.  Passing it as a parameter (not interpolated into the
    query string) eliminates any injection risk from a poisoned embedding response.
    """
    if select_cols is None:
        select_cols = _MEMORY_COLS
    # float() cast guards against non-numeric values in the embedding response
    vec_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"
    # $1 is the vector — referenced in SELECT and ORDER BY, never interpolated
    sim_col = "1 - (embedding <=> $1::vector) AS similarity"
    base = f"SELECT {select_cols}, {sim_col} FROM memories WHERE embedding IS NOT NULL"
    try:
        if category and subcategory:
            return await conn.fetch(
                f"{base} AND category=$2 AND subcategory=$3 "
                "ORDER BY embedding <=> $1::vector LIMIT $4",
                vec_str, category, subcategory, limit,
            )
        elif category:
            return await conn.fetch(
                f"{base} AND category=$2 ORDER BY embedding <=> $1::vector LIMIT $3",
                vec_str, category, limit,
            )
        elif subcategory:
            return await conn.fetch(
                f"{base} AND subcategory=$2 ORDER BY embedding <=> $1::vector LIMIT $3",
                vec_str, subcategory, limit,
            )
        else:
            return await conn.fetch(
                f"{base} ORDER BY embedding <=> $1::vector LIMIT $2",
                vec_str, limit,
            )
    except Exception as e:
        logger.error(f"[VECTOR] pgvector search failed: {e}")
        return []


async def _fts_fetch(conn, query: str, limit: int,
                     category=None,
                     subcategory=None,
                     select_cols=None):
    """FTS search with ILIKE fallback. Shared by /memories/search and /memories/rehydrate.

    Uses plainto_tsquery (not to_tsquery) so user input is treated as plain text —
    tsquery operators like |, !, & are not interpreted.  This prevents tsquery
    operator injection while preserving full-text search quality.
    """
    if select_cols is None:
        select_cols = _MEMORY_COLS
    # Pass the raw query directly — plainto_tsquery handles tokenisation safely
    clean_query = query.strip()
    rank_col = "ts_rank(to_tsvector('english', content), plainto_tsquery('english', $1)) as rank"
    try:
        if category and subcategory:
            return await conn.fetch(
                f"SELECT {select_cols}, {rank_col} FROM memories "
                "WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $1) "
                "AND category=$3 AND subcategory=$4 ORDER BY rank DESC LIMIT $2",
                clean_query, limit, category, subcategory,
            )
        elif category:
            return await conn.fetch(
                f"SELECT {select_cols}, {rank_col} FROM memories "
                "WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $1) "
                "AND category=$3 ORDER BY rank DESC LIMIT $2",
                clean_query, limit, category,
            )
        elif subcategory:
            return await conn.fetch(
                f"SELECT {select_cols}, {rank_col} FROM memories "
                "WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $1) "
                "AND subcategory=$3 ORDER BY rank DESC LIMIT $2",
                clean_query, limit, subcategory,
            )
        else:
            return await conn.fetch(
                f"SELECT {select_cols}, {rank_col} FROM memories "
                "WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $1) "
                "ORDER BY rank DESC LIMIT $2",
                clean_query, limit,
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
