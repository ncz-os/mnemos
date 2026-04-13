# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

"""
Embedding Cache for MNEMOS - Nomic Embeddings
Caches 768-dimensional vectors to avoid recomputation
"""

import hashlib
import threading
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta


class EmbeddingCache:
    """Cache for embedding results with LRU eviction"""

    def __init__(self, max_cache_size: int = 1000, ttl_hours: Optional[float] = None) -> None:
        """
        Initialize embedding cache

        Args:
            max_cache_size: Maximum number of embeddings to keep (default 1000)
            ttl_hours: Time-to-live for cache entries (None = no expiration)
        """
        self.cache: Dict[str, Any] = {}  # memory_id or text_hash -> embedding vector (768-dim)
        self.access_times: Dict[str, datetime] = {}  # memory_id -> last_access_time
        self.max_size = max_cache_size
        self.ttl = timedelta(hours=ttl_hours) if ttl_hours else None
        self.lock = threading.RLock()
        self.stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0,
            'total_cached_vectors': 0
        }

    def get_key(self, memory_id: str = "", text: str = "") -> str:
        """Generate cache key from memory_id or text content"""
        if memory_id:
            return memory_id
        elif text:
            # For arbitrary text (not in database), hash the content
            return hashlib.sha256(text.encode()).hexdigest()[:32]
        else:
            return ""

    def get(self, memory_id: str = "", text: str = "") -> Optional[List[float]]:
        """
        Get cached embedding vector

        Args:
            memory_id: Memory ID (preferred)
            text: Text content (fallback for non-DB text)

        Returns:
            768-dimensional embedding vector or None if not cached
        """
        with self.lock:
            key = self.get_key(memory_id, text)

            if not key:
                return None

            if key not in self.cache:
                self.stats['misses'] += 1
                return None

            embedding = self.cache[key]

            # Check if expired — missing 'created' always expires
            if self.ttl and datetime.now() - embedding.get('created', datetime.min) > self.ttl:
                del self.cache[key]
                del self.access_times[key]
                self.stats['misses'] += 1
                return None

            # Update access time for LRU
            self.access_times[key] = datetime.now()
            self.stats['hits'] += 1
            return embedding['vector']

    def set(self, embedding_vector: List[float], memory_id: str = "", text: str = "") -> bool:
        """
        Cache embedding vector

        Args:
            embedding_vector: 768-dimensional embedding vector
            memory_id: Memory ID (preferred)
            text: Text content (fallback for non-DB text)

        Returns:
            True if successful, False otherwise
        """
        with self.lock:
            key = self.get_key(memory_id, text)

            if not key:
                return False

            # Evict LRU if cache full
            if len(self.cache) >= self.max_size:
                self._evict_lru()

            self.cache[key] = {
                'vector': embedding_vector,
                'created': datetime.now(),
                'memory_id': memory_id,
                'text_hash': text[:50] if text else ""  # Preview
            }
            self.access_times[key] = datetime.now()
            self.stats['total_cached_vectors'] = len(self.cache)
            return True

    def _evict_lru(self) -> None:
        """Evict least recently used entry"""
        if not self.access_times:
            return

        lru_key = min(self.access_times.keys(), key=lambda k: self.access_times[k])
        del self.cache[lru_key]
        del self.access_times[lru_key]
        self.stats['evictions'] += 1

    def clear(self) -> None:
        """Clear all cached embeddings"""
        with self.lock:
            self.cache.clear()
            self.access_times.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        with self.lock:
            total = self.stats['hits'] + self.stats['misses']
            hit_rate = self.stats['hits'] / max(total, 1)

            # Correct MB calculation: 768-dim float32 vectors = 768 * 4 bytes each
            estimated_memory_mb = (len(self.cache) * 768 * 4) / 1_048_576

            return {
                'size': len(self.cache),
                'hits': self.stats['hits'],
                'misses': self.stats['misses'],
                'hit_rate': hit_rate,
                'evictions': self.stats['evictions'],
                'estimated_memory_mb': round(estimated_memory_mb, 1)
            }

    def preload_embeddings(self, embeddings_dict: Dict[str, List[float]]) -> int:
        """
        Preload embeddings from external source
        Useful for warming cache with top memories

        Args:
            embeddings_dict: Dict mapping memory_id -> embedding_vector

        Returns:
            Number of embeddings loaded
        """
        with self.lock:
            loaded = 0
            for memory_id, vector in embeddings_dict.items():
                if len(self.cache) < self.max_size:
                    self.cache[memory_id] = {
                        'vector': vector,
                        'created': datetime.now(),
                        'memory_id': memory_id,
                        'text_hash': ""
                    }
                    self.access_times[memory_id] = datetime.now()
                    loaded += 1

            self.stats['total_cached_vectors'] = len(self.cache)
            return loaded
