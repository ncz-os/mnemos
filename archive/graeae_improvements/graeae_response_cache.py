# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
GRAEAE Response Cache - Caches multi-LLM consensus results
Avoids recomputation of expensive consensus rankings and semantic similarity calculations
Typical consensus request: 5-15 seconds → cached response: <1ms
"""

import hashlib
import threading
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta


class GraaeResponseCache:
    """Cache for GRAEAE consensus responses with LRU eviction"""

    def __init__(self, max_cache_size=500, ttl_hours=24):
        """
        Initialize GRAEAE response cache

        Args:
            max_cache_size: Maximum number of cached responses (default 500)
            ttl_hours: Time-to-live for cache entries (default 24 hours)
        """
        self.cache = {}  # query_hash -> cached_response
        self.access_times = {}  # query_hash -> last_access_time
        self.max_size = max_cache_size
        self.ttl = timedelta(hours=ttl_hours)
        self.lock = threading.RLock()
        self.stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0,
            'total_cached_responses': 0,
            'consensus_time_saved_ms': 0  # Track latency savings
        }

    def get_key(self, prompt: str = "", task_type: str = "") -> str:
        """Generate cache key from prompt and task type"""
        key_source = f"{prompt[:500]}.{task_type}"
        return hashlib.sha256(key_source.encode()).hexdigest()[:16]

    def get(self, prompt: str = "", task_type: str = "") -> Optional[Dict[str, Any]]:
        """
        Get cached GRAEAE response

        Args:
            prompt: Original query/prompt
            task_type: Type of task (reasoning, architecture_design, etc.)

        Returns:
            Cached response dict or None if not cached
        """
        with self.lock:
            key = self.get_key(prompt, task_type)

            if key not in self.cache:
                self.stats['misses'] += 1
                return None

            response = self.cache[key]

            # Check if expired
            if datetime.now() - response.get('cached_at', datetime.now()) > self.ttl:
                del self.cache[key]
                del self.access_times[key]
                self.stats['misses'] += 1
                return None

            # Update access time for LRU
            self.access_times[key] = datetime.now()
            self.stats['hits'] += 1
            
            # Track latency savings (approximate: 10 seconds per consensus)
            self.stats['consensus_time_saved_ms'] += 10000

            return response.get('data')

    def set(self, response_data: Dict[str, Any], prompt: str = "", task_type: str = "") -> bool:
        """
        Cache GRAEAE response

        Args:
            response_data: Full response dict from /graeae/consult endpoint
            prompt: Original query/prompt
            task_type: Type of task

        Returns:
            True if successful, False otherwise
        """
        with self.lock:
            key = self.get_key(prompt, task_type)

            if not key:
                return False

            # Evict LRU if cache full
            if len(self.cache) >= self.max_size:
                self._evict_lru()

            self.cache[key] = {
                'data': response_data,
                'cached_at': datetime.now(),
                'prompt_length': len(prompt),
                'task_type': task_type,
                'muse_count': len(response_data.get('muses', [])) if isinstance(response_data, dict) else 0
            }
            self.access_times[key] = datetime.now()
            self.stats['total_cached_responses'] = len(self.cache)
            return True

    def _evict_lru(self):
        """Evict least recently used entry"""
        if not self.access_times:
            return

        lru_key = min(self.access_times.keys(), key=lambda k: self.access_times[k])
        del self.cache[lru_key]
        del self.access_times[lru_key]
        self.stats['evictions'] += 1

    def clear(self):
        """Clear all cached responses"""
        with self.lock:
            self.cache.clear()
            self.access_times.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        with self.lock:
            total = self.stats['hits'] + self.stats['misses']
            hit_rate = self.stats['hits'] / max(total, 1)
            
            # Estimate memory usage (each response ~50KB average)
            estimated_memory_mb = (len(self.cache) * 50) / 1024

            return {
                'size': len(self.cache),
                'hits': self.stats['hits'],
                'misses': self.stats['misses'],
                'hit_rate': hit_rate,
                'evictions': self.stats['evictions'],
                'estimated_memory_mb': round(estimated_memory_mb, 1),
                'consensus_time_saved_seconds': round(self.stats['consensus_time_saved_ms'] / 1000, 1)
            }

    def preload_responses(self, responses_dict: Dict[str, Dict[str, Any]]) -> int:
        """
        Preload cached responses from external source

        Args:
            responses_dict: Dict mapping cache_key -> response_data

        Returns:
            Number of responses loaded
        """
        with self.lock:
            loaded = 0
            for key, response_data in responses_dict.items():
                if len(self.cache) < self.max_size:
                    self.cache[key] = {
                        'data': response_data,
                        'cached_at': datetime.now(),
                        'prompt_length': 0,
                        'task_type': 'preloaded',
                        'muse_count': len(response_data.get('muses', [])) if isinstance(response_data, dict) else 0
                    }
                    self.access_times[key] = datetime.now()
                    loaded += 1

            self.stats['total_cached_responses'] = len(self.cache)
            return loaded
