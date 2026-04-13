# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
Dual-Layer Cache for MNEMOS
L1: Python dict in-memory (ultra-fast, <1ms)
L2: PostgreSQL persistent (durable, survives restarts)

This replaces Redis with a more reliable architecture:
- PostgreSQL is the source of truth
- Python cache is the working set
- Auto-recovery on restart (syncs from PostgreSQL)
"""

import json
import threading
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from typing import Optional, Dict, Any


class DualLayerCache:
    """
    Two-tier cache: L1 (Memory) + L2 (PostgreSQL)

    Pattern:
    1. Read: Check L1 (memory) → if miss, check L2 (PostgreSQL) → populate L1
    2. Write: Write to L2 (PostgreSQL) → immediately available in L1
    3. Startup: Sync all from L2 to L1 (~1 second for 5,676 items)
    """

    def __init__(self, pg_config: Dict[str, str], max_memory_size: int = 10000):
        """
        Initialize dual-layer cache

        Args:
            pg_config: PostgreSQL connection config (dbname, user, host, port)
            max_memory_size: Max items in L1 memory (for LRU eviction if needed)
        """
        self.memory_cache = {}  # L1: Python dict (ultra-fast)
        self.pg_config = pg_config  # L2: PostgreSQL connection
        self.max_memory_size = max_memory_size
        self.lock = threading.RLock()  # Thread-safe operations
        self.access_times = {}  # Track access times for LRU
        self.stats = {
            'l1_hits': 0,
            'l1_misses': 0,
            'l2_hits': 0,
            'l2_misses': 0,
        }

    def _get_connection(self):
        """Get PostgreSQL connection"""
        return psycopg2.connect(**self.pg_config)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Get value from cache
        L1: Memory (ultra-fast)
        L2: PostgreSQL (durable)

        Args:
            key: Memory ID (e.g., "mem_123")

        Returns:
            Memory dict or None if not found
        """
        with self.lock:
            # L1: Check memory cache first
            if key in self.memory_cache:
                self.access_times[key] = time.time()  # Update LRU
                self.stats['l1_hits'] += 1
                return self.memory_cache[key]

            # L1 miss - check L2 (PostgreSQL)
            self.stats['l1_misses'] += 1
            value = self._fetch_from_db(key)

            if value:
                # L2 hit - populate L1 for future access
                self._evict_lru_if_needed()  # Make room if cache full
                self.memory_cache[key] = value
                self.access_times[key] = time.time()
                self.stats['l2_hits'] += 1
                return value
            else:
                # L2 miss - not found
                self.stats['l2_misses'] += 1
                return None

    def set(self, key: str, value: Dict[str, Any]) -> bool:
        """
        Set value in cache
        Writes to L2 (PostgreSQL) immediately, updates L1 cache

        Args:
            key: Memory ID
            value: Memory dict with id, content, category, etc.

        Returns:
            True if successful, False otherwise
        """
        with self.lock:
            try:
                # L2: Write to PostgreSQL (source of truth)
                self._write_to_db(key, value)

                # L1: Update memory cache
                self._evict_lru_if_needed()
                self.memory_cache[key] = value
                self.access_times[key] = time.time()

                return True
            except Exception as e:
                print(f"[CACHE] Error writing {key}: {e}", flush=True)
                return False

    def delete(self, key: str) -> bool:
        """Delete value from cache"""
        with self.lock:
            try:
                # L2: Delete from PostgreSQL
                self._delete_from_db(key)

                # L1: Remove from memory
                if key in self.memory_cache:
                    del self.memory_cache[key]
                if key in self.access_times:
                    del self.access_times[key]

                return True
            except Exception as e:
                print(f"[CACHE] Error deleting {key}: {e}", flush=True)
                return False

    def sync_on_startup(self) -> int:
        """
        Sync all memories from PostgreSQL to Python cache on startup

        Returns:
            Number of memories loaded
        """
        with self.lock:
            try:
                conn = self._get_connection()
                cur = conn.cursor(cursor_factory=RealDictCursor)

                # Get ALL memories from PostgreSQL
                cur.execute("""
                    SELECT id, content, category, created, updated, metadata
                    FROM memories
                    ORDER BY created DESC
                    LIMIT 10000
                """)

                loaded_count = 0
                for row in cur.fetchall():
                    memory_id = row['id']
                    self.memory_cache[memory_id] = {
                        'id': memory_id,
                        'text': row['content'],
                        'category': row['category'],
                        'created_at': row['created'].isoformat() if row['created'] else None,
                        'timestamp': row['updated'].timestamp() if row['updated'] else None,
                        'tags': [],
                        'metadata': row['metadata'] or {}
                    }
                    self.access_times[memory_id] = time.time()
                    loaded_count += 1

                cur.close()
                conn.close()

                print(f"[CACHE] ✓ Synced {loaded_count} memories from PostgreSQL to L1 cache", flush=True)
                return loaded_count

            except Exception as e:
                print(f"[CACHE] ✗ Sync failed: {e}", flush=True)
                return 0

    def _fetch_from_db(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Fetch single memory from PostgreSQL"""
        try:
            conn = self._get_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)

            cur.execute("""
                SELECT id, content, category, created, updated, metadata
                FROM memories
                WHERE id = %s
            """, (memory_id,))

            row = cur.fetchone()
            cur.close()
            conn.close()

            if row:
                return {
                    'id': row['id'],
                    'text': row['content'],
                    'category': row['category'],
                    'created_at': row['created'].isoformat() if row['created'] else None,
                    'timestamp': row['updated'].timestamp() if row['updated'] else None,
                    'tags': [],
                    'metadata': row['metadata'] or {}
                }
            return None

        except Exception as e:
            print(f"[CACHE] Error fetching {memory_id}: {e}", flush=True)
            return None

    def _write_to_db(self, memory_id: str, value: Dict[str, Any]):
        """Write memory to PostgreSQL"""
        conn = self._get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO memories (id, content, category, metadata, created, updated)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET
                content = EXCLUDED.content,
                category = EXCLUDED.category,
                metadata = EXCLUDED.metadata,
                updated = NOW()
        """, (
            memory_id,
            value.get('text', ''),
            value.get('category', 'uncategorized'),
            json.dumps(value.get('metadata', {}))
        ))

        conn.commit()
        cur.close()
        conn.close()

    def _delete_from_db(self, memory_id: str):
        """Delete memory from PostgreSQL"""
        conn = self._get_connection()
        cur = conn.cursor()

        cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))

        conn.commit()
        cur.close()
        conn.close()

    def _evict_lru_if_needed(self):
        """Evict least-recently-used item if cache is full"""
        if len(self.memory_cache) >= self.max_memory_size:
            # Find least recently accessed key
            if self.access_times:
                lru_key = min(self.access_times.keys(), key=lambda k: self.access_times[k])
                del self.memory_cache[lru_key]
                del self.access_times[lru_key]
                print(f"[CACHE] LRU evicted {lru_key}", flush=True)

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        with self.lock:
            total_requests = sum([
                self.stats['l1_hits'],
                self.stats['l1_misses'],
                self.stats['l2_hits'],
                self.stats['l2_misses']
            ])

            return {
                'l1_size': len(self.memory_cache),
                'l1_hits': self.stats['l1_hits'],
                'l1_hit_rate': self.stats['l1_hits'] / max(total_requests, 1),
                'l1_misses': self.stats['l1_misses'],
                'l2_hits': self.stats['l2_hits'],
                'l2_misses': self.stats['l2_misses'],
                'combined_hit_rate': (self.stats['l1_hits'] + self.stats['l2_hits']) / max(total_requests, 1)
            }

    def clear_stats(self):
        """Reset cache statistics"""
        with self.lock:
            self.stats = {
                'l1_hits': 0,
                'l1_misses': 0,
                'l2_hits': 0,
                'l2_misses': 0,
            }
