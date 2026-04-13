# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

"""
Dual-Layer Cache for MNEMOS
L1: Python dict in-memory (ultra-fast, <1ms)
L2: PostgreSQL persistent via asyncpg (durable, survives restarts)

This replaces Redis with a more reliable architecture:
- PostgreSQL is the source of truth
- Python cache is the working set
- Auto-recovery on restart (warm from PostgreSQL)
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

import asyncpg

logger = logging.getLogger(__name__)


class DualLayerCache:
    """
    Two-tier cache: L1 (Memory) + L2 (PostgreSQL via asyncpg)

    Pattern:
    1. Read: Check L1 (memory) → if miss, check L2 (PostgreSQL) → populate L1
    2. Write: Write to L2 (PostgreSQL) → immediately available in L1
    3. Startup: warm_from_db() loads all from L2 to L1 (~1 second for 5,676 items)
    """

    def __init__(self, pool: asyncpg.Pool, max_memory_size: int = 10_000) -> None:
        """
        Initialize dual-layer cache.

        Args:
            pool: asyncpg connection pool (from api.lifecycle._pool)
            max_memory_size: Max items in L1 memory (LRU eviction when full)
        """
        self.pool = pool
        self.max_memory_size = max_memory_size
        self.memory_cache: Dict[str, Dict[str, Any]] = {}  # L1: Python dict
        self.lock = asyncio.Lock()
        self.access_times: Dict[str, float] = {}
        self.stats = {
            'l1_hits': 0,
            'l1_misses': 0,
            'l2_hits': 0,
            'l2_misses': 0,
        }

    async def warm_from_db(self) -> int:
        """
        Load memories from PostgreSQL into L1 cache on startup.

        Returns:
            Number of memories loaded
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, content, category, tags, metadata, created, updated
                    FROM memories
                    ORDER BY created DESC
                    LIMIT 10000
                """)

            loaded_count = 0
            now = time.time()
            async with self.lock:
                for row in rows:
                    memory_id = row['id']
                    self.memory_cache[memory_id] = self._row_to_dict(row)
                    self.access_times[memory_id] = now
                    loaded_count += 1

            logger.info("Warmed %d memories from PostgreSQL into L1 cache", loaded_count)
            return loaded_count

        except Exception as exc:
            logger.error("Cache warm_from_db failed: %s", exc)
            return 0

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Get value from cache (L1 first, then L2).

        Args:
            key: Memory ID (e.g., "mem_123")

        Returns:
            Memory dict or None if not found
        """
        async with self.lock:
            if key in self.memory_cache:
                self.access_times[key] = time.time()
                self.stats['l1_hits'] += 1
                return self.memory_cache[key]
            self.stats['l1_misses'] += 1

        # L1 miss — fetch from DB without holding the lock during I/O
        value = await self._fetch_from_db(key)

        if value is not None:
            async with self.lock:
                await self._evict_lru_if_needed()
                self.memory_cache[key] = value
                self.access_times[key] = time.time()
                self.stats['l2_hits'] += 1
            return value

        async with self.lock:
            self.stats['l2_misses'] += 1
        return None

    async def set(self, key: str, value: Dict[str, Any]) -> bool:
        """
        Set value in cache (write to L2, update L1).

        Args:
            key: Memory ID
            value: Memory dict with id, content, category, etc.

        Returns:
            True if successful, False otherwise
        """
        try:
            await self._write_to_db(key, value)
            async with self.lock:
                await self._evict_lru_if_needed()
                self.memory_cache[key] = value
                self.access_times[key] = time.time()
            return True
        except Exception as exc:
            logger.warning("Cache set failed for key %s: %s", key, exc)
            return False

    async def delete(self, key: str) -> bool:
        """Delete value from both L1 and L2."""
        try:
            await self._delete_from_db(key)
            async with self.lock:
                self.memory_cache.pop(key, None)
                self.access_times.pop(key, None)
            return True
        except Exception as exc:
            logger.warning("Cache delete failed for key %s: %s", key, exc)
            return False

    async def _fetch_from_db(self, key: str) -> Optional[Dict[str, Any]]:
        """Fetch a single memory from PostgreSQL."""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT id, content, category, tags, metadata, created, updated
                    FROM memories
                    WHERE id = $1
                """, key)
            if row:
                return self._row_to_dict(row)
            return None
        except Exception as exc:
            logger.warning("Cache _fetch_from_db failed for key %s: %s", key, exc)
            return None

    async def _write_to_db(self, key: str, value: Dict[str, Any]) -> None:
        """Write a memory to PostgreSQL."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO memories (id, content, category, tags, metadata, created, updated)
                VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET
                    content   = EXCLUDED.content,
                    category  = EXCLUDED.category,
                    tags      = EXCLUDED.tags,
                    metadata  = EXCLUDED.metadata,
                    updated   = NOW()
            """,
                key,
                value.get('content', value.get('text', '')),
                value.get('category', 'uncategorized'),
                value.get('tags') or [],
                json.dumps(value.get('metadata', {})),
            )

    async def _delete_from_db(self, key: str) -> None:
        """Delete a memory from PostgreSQL."""
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM memories WHERE id = $1", key)

    async def _evict_lru_if_needed(self) -> None:
        """Evict least-recently-used item if L1 cache is full. Must be called under self.lock."""
        if len(self.memory_cache) >= self.max_memory_size and self.access_times:
            lru_key = min(self.access_times, key=lambda k: self.access_times[k])
            self.memory_cache.pop(lru_key, None)
            self.access_times.pop(lru_key, None)
            logger.debug("LRU evicted cache key %s", lru_key)
            # Best-effort background delete from DB (don't await in hot path)
            asyncio.create_task(self._delete_from_db(lru_key))

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        """Convert an asyncpg Record to a plain dict."""
        return {
            'id': row['id'],
            'content': row['content'],
            'category': row['category'],
            'tags': row.get('tags') or [],
            'metadata': row['metadata'] or {},
            'created': row['created'].isoformat() if row['created'] else None,
            'updated': row['updated'].isoformat() if row['updated'] else None,
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total_requests = sum(self.stats.values())
        return {
            'l1_size': len(self.memory_cache),
            'l1_hits': self.stats['l1_hits'],
            'l1_hit_rate': self.stats['l1_hits'] / max(total_requests, 1),
            'l1_misses': self.stats['l1_misses'],
            'l2_hits': self.stats['l2_hits'],
            'l2_misses': self.stats['l2_misses'],
            'combined_hit_rate': (
                (self.stats['l1_hits'] + self.stats['l2_hits']) / max(total_requests, 1)
            ),
        }

    def clear_stats(self) -> None:
        """Reset cache statistics."""
        self.stats = {
            'l1_hits': 0,
            'l1_misses': 0,
            'l2_hits': 0,
            'l2_misses': 0,
        }
