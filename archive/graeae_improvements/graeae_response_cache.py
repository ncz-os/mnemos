# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
ConsensusCache - Caches full multi-provider consensus results for GRAEAE.

This is a consensus-level cache: it stores the complete all_responses dict
returned by GraeaeEngine.consult(), keyed on a SHA-256 hash of the full prompt
and task_type. It is distinct from the per-provider ResponseCache in
graeae/_cache.py, which caches individual provider responses.

Typical consensus request: 5-15 seconds -> cached response: <1ms
"""

import hashlib
import threading
import time
import json
from typing import Optional, Dict, Any
from datetime import timedelta


class ConsensusCache:
    """Cache for GRAEAE full consensus responses with LRU eviction.

    Distinguishes from the per-provider ResponseCache in graeae/_cache.py.
    Use this to avoid re-running expensive multi-provider fan-outs for
    identical (prompt, task_type) pairs within the TTL window.
    """

    def __init__(self, max_cache_size: int = 500, ttl_hours: int = 24) -> None:
        """
        Initialize consensus response cache.

        Args:
            max_cache_size: Maximum number of cached responses (default 500)
            ttl_hours: Time-to-live for cache entries in hours (default 24)
        """
        self.cache: Dict[str, Dict] = {}        # key -> cached_entry
        self.access_times: Dict[str, float] = {}  # key -> monotonic time of last access
        self.max_size = max_cache_size
        self.ttl_seconds = ttl_hours * 3600
        self.lock = threading.RLock()
        self.stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0,
            'total_cached_responses': 0,
            'consensus_time_saved_ms': 0,
        }

    def _make_key(self, prompt: str, task_type: str) -> str:
        """Generate a stable cache key from the full prompt and task type."""
        return hashlib.sha256(f"{prompt}\x00{task_type}".encode()).hexdigest()

    def get(self, prompt: str = "", task_type: str = "") -> Optional[Dict[str, Any]]:
        """
        Get cached consensus response.

        Args:
            prompt: Original query/prompt
            task_type: Type of task (reasoning, architecture_design, etc.)

        Returns:
            Cached response dict or None if not cached / expired
        """
        with self.lock:
            key = self._make_key(prompt, task_type)

            if key not in self.cache:
                self.stats['misses'] += 1
                return None

            entry = self.cache[key]

            # TTL check using monotonic clock
            age = time.monotonic() - entry['cached_at']
            if age > self.ttl_seconds:
                del self.cache[key]
                del self.access_times[key]
                self.stats['misses'] += 1
                return None

            # Update access time for LRU
            self.access_times[key] = time.monotonic()
            self.stats['hits'] += 1

            # Accumulate actual saved latency
            self.stats['consensus_time_saved_ms'] += entry.get('latency_ms', 0)

            return entry.get('data')

    def set(self, prompt: str = "", task_type: str = "",
            response_data: Dict[str, Any] = None, latency_ms: int = 0) -> bool:
        """
        Cache a consensus response.

        Args:
            prompt: Original query/prompt
            task_type: Type of task
            response_data: Full response dict from GraeaeEngine.consult()
            latency_ms: Actual latency of the consensus call being cached

        Returns:
            True if successful
        """
        if response_data is None:
            response_data = {}

        with self.lock:
            key = self._make_key(prompt, task_type)

            # Evict LRU if cache full
            if len(self.cache) >= self.max_size:
                self._evict_lru()

            now = time.monotonic()
            self.cache[key] = {
                'data': response_data,
                'cached_at': now,
                'prompt_length': len(prompt),
                'task_type': task_type,
                'provider_count': len(response_data.get('all_responses', {})) if isinstance(response_data, dict) else 0,
                'latency_ms': latency_ms,
            }
            self.access_times[key] = now
            self.stats['total_cached_responses'] = len(self.cache)
            return True

    def _evict_lru(self) -> None:
        """Evict least recently used entry."""
        if not self.access_times:
            return

        lru_key = min(self.access_times.keys(), key=lambda k: self.access_times[k])
        del self.cache[lru_key]
        del self.access_times[lru_key]
        self.stats['evictions'] += 1

    def clear(self) -> None:
        """Clear all cached responses and reset counters."""
        with self.lock:
            self.cache.clear()
            self.access_times.clear()
            self.stats['total_cached_responses'] = 0

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
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
                'consensus_time_saved_seconds': round(self.stats['consensus_time_saved_ms'] / 1000, 1),
            }

    def preload_responses(self, responses_dict: Dict[str, Dict[str, Any]]) -> int:
        """
        Preload cached responses from external source.

        Args:
            responses_dict: Dict mapping cache_key -> response_data

        Returns:
            Number of responses loaded
        """
        with self.lock:
            loaded = 0
            for key, response_data in responses_dict.items():
                if len(self.cache) < self.max_size:
                    now = time.monotonic()
                    self.cache[key] = {
                        'data': response_data,
                        'cached_at': now,
                        'prompt_length': 0,
                        'task_type': 'preloaded',
                        'provider_count': len(response_data.get('all_responses', {})) if isinstance(response_data, dict) else 0,
                        'latency_ms': 0,
                    }
                    self.access_times[key] = now
                    loaded += 1

            self.stats['total_cached_responses'] = len(self.cache)
            return loaded
