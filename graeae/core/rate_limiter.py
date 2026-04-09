"""
GRAEAE Feature 5: Rate Limiting & Backpressure
Per-muse limits, queue backpressure, graceful degradation
"""

import os
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from collections import defaultdict

logger = logging.getLogger(__name__)


class RateLimiter:
    """Per-muse rate limiting with exponential backoff"""

    def __init__(
        self,
        muse_id: str,
        requests_per_minute: int = 60,
        burst_size: int = 10,
        backoff_factor: float = 1.5
    ):
        """
        Initialize rate limiter for a muse
        
        Args:
            muse_id: Muse identifier
            requests_per_minute: RPM limit
            burst_size: Allow bursts up to this size
            backoff_factor: Exponential backoff multiplier
        """
        self.muse_id = muse_id
        self.rpm_limit = requests_per_minute
        self.burst_size = burst_size
        self.backoff_factor = backoff_factor

        self.window_size = 60  # seconds
        self.request_times = []
        self.backoff_until = 0.0
        self.backoff_count = 0
        self._lock = threading.RLock()

    def is_allowed(self) -> bool:
        """Check if request is allowed (not rate limited)"""
        with self._lock:
            now = time.time()

            # Check backoff
            if now < self.backoff_until:
                return False

            # Remove old requests outside window
            cutoff = now - self.window_size
            self.request_times = [t for t in self.request_times if t > cutoff]

            # Check rate limit
            if len(self.request_times) >= self.rpm_limit / 60 * self.window_size:
                # Trigger backoff
                backoff_seconds = (self.backoff_factor ** self.backoff_count)
                self.backoff_until = now + backoff_seconds
                self.backoff_count += 1
                logger.warning(f"Rate limit exceeded for {self.muse_id}, backoff {backoff_seconds:.1f}s")
                return False

            # Allow burst
            if len(self.request_times) < self.burst_size:
                self.request_times.append(now)
                return True

            # Burst exceeded — trigger backoff on first violation
            if self.backoff_until == 0.0:
                backoff_seconds = (self.backoff_factor ** self.backoff_count)
                self.backoff_until = now + backoff_seconds
                self.backoff_count += 1
                logger.warning(f"Burst exceeded for {self.muse_id}, backoff {backoff_seconds:.1f}s")
            return False

    def record_request(self):
        """Record a request"""
        with self._lock:
            self.request_times.append(time.time())

    def reset_backoff(self):
        """Reset backoff counter on success"""
        with self._lock:
            self.backoff_count = 0
            self.backoff_until = 0.0

    def get_metrics(self) -> Dict:
        """Get rate limiter metrics"""
        with self._lock:
            now = time.time()
            cutoff = now - self.window_size
            recent_requests = [t for t in self.request_times if t > cutoff]

            return {
                'muse_id': self.muse_id,
                'requests_in_window': len(recent_requests),
                'limit_per_minute': self.rpm_limit,
                'backoff_active': now < self.backoff_until,
                'backoff_seconds_remaining': max(0, self.backoff_until - now),
                'backoff_count': self.backoff_count,
            }


class RateLimiterPool:
    """Manages rate limiters for multiple muses"""

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize rate limiter pool
        
        Args:
            db_path: Path to persistence database
        """
        self.db_path = db_path or os.getenv(
            'GRAEAE_LIMITS_DB',
            '/var/lib/mnemos/graeae_limits.db'
        )
        self.limiters: Dict[str, RateLimiter] = {}
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        """Initialize limits database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS muse_limits (
                    muse_id TEXT PRIMARY KEY,
                    requests_per_minute INTEGER,
                    burst_size INTEGER,
                    enforced BOOLEAN DEFAULT 1
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    muse_id TEXT,
                    event_type TEXT,
                    timestamp TIMESTAMP,
                    details TEXT
                )
            """)

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to init limits schema: {e}")

    def get_limiter(
        self,
        muse_id: str,
        requests_per_minute: int = 60,
        burst_size: int = 3
    ) -> RateLimiter:
        """Get or create limiter for muse"""
        with self._lock:
            if muse_id not in self.limiters:
                self.limiters[muse_id] = RateLimiter(
                    muse_id,
                    requests_per_minute,
                    burst_size
                )
            return self.limiters[muse_id]

    def is_allowed(self, muse_id: str) -> bool:
        """Check if muse can accept request"""
        limiter = self.get_limiter(muse_id)
        allowed = limiter.is_allowed()

        if not allowed:
            self._log_event(muse_id, 'rate_limited', None)

        return allowed

    def record_request(self, muse_id: str):
        """Record successful request for muse"""
        limiter = self.get_limiter(muse_id)
        limiter.record_request()
        limiter.reset_backoff()  # Success resets backoff

    def get_all_metrics(self) -> Dict:
        """Get metrics for all muses"""
        with self._lock:
            metrics = {}
            for muse_id, limiter in self.limiters.items():
                metrics[muse_id] = limiter.get_metrics()
            return metrics

    def get_queue_backpressure(self, queue_size: int, queue_limit: int) -> bool:
        """
        Determine if queue backpressure should be applied
        Returns True if queue is filling up and we should slow down
        
        Args:
            queue_size: Current queue depth
            queue_limit: Max queue capacity
            
        Returns:
            True if backpressure should be applied
        """
        usage_pct = (queue_size / queue_limit) * 100

        if usage_pct > 90:
            # Critical - reject new requests
            logger.warning(f"Critical backpressure: {queue_size}/{queue_limit} items")
            return True

        if usage_pct > 75:
            # Warn - rate limit more aggressively
            logger.warning(f"High backpressure: {queue_size}/{queue_limit} items")

        return False

    def _log_event(self, muse_id: str, event_type: str, details: Optional[str]):
        """Log a rate limiting event"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            now = datetime.now(timezone.utc).isoformat()

            cur.execute("""
                INSERT INTO rate_limit_events (muse_id, event_type, timestamp, details)
                VALUES (?, ?, ?, ?)
            """, (muse_id, event_type, now, details))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to log event: {e}")


class QueueBackpressure:
    """Manages queue backpressure and graceful degradation"""

    def __init__(self, max_queue_size: int = 10000):
        """
        Initialize backpressure controller
        
        Args:
            max_queue_size: Maximum queue size before rejecting
        """
        self.max_queue_size = max_queue_size
        self.degradation_level = 0  # 0-3 for graceful degradation
        self._lock = threading.RLock()

    def update_queue_depth(self, current_size: int):
        """Update queue depth and adjust degradation"""
        with self._lock:
            usage_pct = (current_size / self.max_queue_size) * 100

            if usage_pct > 90:
                self.degradation_level = 3  # Reject non-essential, cache-only
                logger.warning(f"Severe backpressure: {usage_pct:.1f}%")
            elif usage_pct > 75:
                self.degradation_level = 2  # Cache-prefer, reduce batch size
                logger.warning(f"High backpressure: {usage_pct:.1f}%")
            elif usage_pct > 50:
                self.degradation_level = 1  # Cache-prefer
            else:
                self.degradation_level = 0  # Normal

    def should_accept_request(self, request_type: str = 'normal') -> bool:
        """
        Determine if request should be accepted
        
        Args:
            request_type: 'cache' (cached response), 'batch', or 'normal'
            
        Returns:
            True if request should be accepted
        """
        if self.degradation_level == 0:
            return True

        if self.degradation_level == 1:
            return request_type in ['cache', 'normal']

        if self.degradation_level == 2:
            return request_type in ['cache']

        # Level 3 - reject all except cache
        return False

    def get_batch_size(self, default_size: int = 100) -> int:
        """Get recommended batch size based on backpressure"""
        if self.degradation_level <= 1:
            return default_size
        elif self.degradation_level == 2:
            return max(10, default_size // 2)
        else:
            return 1  # Process one at a time

    def get_status(self) -> Dict:
        """Get backpressure status"""
        return {
            'degradation_level': self.degradation_level,
            'max_queue_size': self.max_queue_size,
            'accepting_normal': self.degradation_level < 3,
            'accepting_batch': self.degradation_level < 2,
            'cache_preferred': self.degradation_level > 0,
        }
