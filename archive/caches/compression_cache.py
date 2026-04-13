# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

"""
Compression Response Cache
Caches compressed responses to avoid recomputation
"""

import hashlib
import threading
from typing import Optional, Dict, Any
from datetime import datetime, timedelta


class CompressionCache:
    """Cache for compression results"""

    def __init__(self, max_cache_size: int = 2500, ttl_hours: float = 24) -> None:
        self.cache: Dict[str, Any] = {}  # query_hash -> compressed_response
        self.access_times: Dict[str, datetime] = {}  # query_hash -> last_access_time
        self.max_size = max_cache_size
        self.ttl = timedelta(hours=ttl_hours)
        self.lock = threading.RLock()
        self.stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0
        }

    def get_key(self, query: str, limit: int = 0) -> str:
        """Generate cache key from query"""
        cache_str = f"{query}:{limit}"
        return hashlib.sha256(cache_str.encode()).hexdigest()

    def get(self, query: str, limit: int = 0) -> Optional[Dict[str, Any]]:
        """Get cached compression result"""
        with self.lock:
            key = self.get_key(query, limit)

            if key not in self.cache:
                self.stats['misses'] += 1
                return None

            entry = self.cache[key]

            # Check if expired
            if datetime.now() - entry['created'] > self.ttl:
                del self.cache[key]
                del self.access_times[key]
                self.stats['misses'] += 1
                return None

            # Update access time
            self.access_times[key] = datetime.now()
            self.stats['hits'] += 1
            return entry['data']

    def set(self, query: str, result: Dict[str, Any], limit: int = 0) -> bool:
        """Cache compression result"""
        with self.lock:
            key = self.get_key(query, limit)

            # Evict LRU if cache full
            if len(self.cache) >= self.max_size:
                self._evict_lru()

            self.cache[key] = {
                'data': result,
                'created': datetime.now()
            }
            self.access_times[key] = datetime.now()
            return True

    def _evict_lru(self) -> None:
        """Evict least recently used entry"""
        if not self.access_times:
            return

        lru_key = min(self.access_times.keys(), key=lambda k: self.access_times[k])
        if lru_key not in self.cache:
            return
        del self.cache[lru_key]
        del self.access_times[lru_key]
        self.stats['evictions'] += 1

    def clear(self) -> None:
        """Clear all cached entries"""
        with self.lock:
            self.cache.clear()
            self.access_times.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        with self.lock:
            total = self.stats['hits'] + self.stats['misses']
            hit_rate = self.stats['hits'] / max(total, 1)

            return {
                'size': len(self.cache),
                'hits': self.stats['hits'],
                'misses': self.stats['misses'],
                'hit_rate': hit_rate,
                'evictions': self.stats['evictions']
            }
