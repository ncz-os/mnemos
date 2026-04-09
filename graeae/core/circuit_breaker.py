"""
GRAEAE Feature 2: Muse Failover & Circuit Breaker
Tracks muse failures and auto-disables with 5min cooldown to prevent cascades
"""

import os
import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
from enum import Enum
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"          # Normal operation
    OPEN = "open"              # Disabled, reject requests
    HALF_OPEN = "half_open"    # Testing if recovered


@dataclass
class CircuitMetrics:
    """Metrics for a muse's circuit breaker"""
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[str] = None
    last_success_time: Optional[str] = None
    consecutive_failures: int = 0


class CircuitBreaker:
    """
    Circuit breaker pattern for individual muses
    Prevents cascading failures by disabling failing muses temporarily
    """

    def __init__(
        self,
        muse_id: str,
        failure_threshold: int = 5,
        cooldown_minutes: int = 5,
        success_threshold: int = 2
    ):
        """
        Initialize circuit breaker for a muse
        
        Args:
            muse_id: ID of the muse
            failure_threshold: Number of failures before opening circuit
            cooldown_minutes: Minutes before attempting recovery
            success_threshold: Consecutive successes needed to close circuit
        """
        self.muse_id = muse_id
        self.failure_threshold = failure_threshold
        self.cooldown_minutes = cooldown_minutes
        self.success_threshold = success_threshold

        self.state = CircuitState.CLOSED
        self.metrics = CircuitMetrics()
        self.open_time: Optional[datetime] = None
        self._lock = threading.RLock()

    def record_success(self):
        """Record a successful muse request"""
        with self._lock:
            self.metrics.success_count += 1
            self.metrics.last_success_time = datetime.now(timezone.utc).isoformat()
            self.metrics.consecutive_failures = 0

            # Close circuit if in half-open and threshold met
            if self.state == CircuitState.HALF_OPEN:
                if self.metrics.success_count % self.success_threshold == 0:
                    self.state = CircuitState.CLOSED
                    logger.info(f"Circuit CLOSED for muse {self.muse_id}")

    def record_failure(self, error: str):
        """
        Record a failed muse request
        
        Args:
            error: Error message
        """
        with self._lock:
            self.metrics.failure_count += 1
            self.metrics.last_failure_time = datetime.now(timezone.utc).isoformat()
            self.metrics.consecutive_failures += 1

            logger.warning(
                f"Muse {self.muse_id} failure "
                f"({self.metrics.consecutive_failures}/{self.failure_threshold}): {error}"
            )

            # Open circuit if threshold exceeded
            if self.metrics.consecutive_failures >= self.failure_threshold:
                if self.state != CircuitState.OPEN:
                    self.state = CircuitState.OPEN
                    self.open_time = datetime.now(timezone.utc)
                    logger.error(f"Circuit OPENED for muse {self.muse_id}")

    def is_available(self) -> bool:
        """Check if muse is available for requests"""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.HALF_OPEN:
                return True

            if self.state == CircuitState.OPEN:
                # Check if cooldown expired
                if self.open_time:
                    elapsed = datetime.now(timezone.utc) - self.open_time
                    if elapsed >= timedelta(minutes=self.cooldown_minutes):
                        self.state = CircuitState.HALF_OPEN
                        self.metrics.consecutive_failures = 0
                        logger.info(f"Circuit HALF_OPEN for muse {self.muse_id} (retry)")
                        return True

                return False

            return True

    def get_state(self) -> str:
        """Get current circuit state"""
        return self.state.value

    def get_metrics(self) -> Dict:
        """Get circuit metrics"""
        with self._lock:
            return {
                'muse_id': self.muse_id,
                'state': self.state.value,
                'failures': self.metrics.failure_count,
                'successes': self.metrics.success_count,
                'consecutive_failures': self.metrics.consecutive_failures,
                'last_failure': self.metrics.last_failure_time,
                'last_success': self.metrics.last_success_time,
                'open_since': self.open_time.isoformat() if self.open_time else None,
            }


class CircuitBreakerPool:
    """Manages circuit breakers for multiple muses"""

    def __init__(self, failure_threshold: int = 5, cooldown_minutes: int = 5):
        """
        Initialize circuit breaker pool
        
        Args:
            failure_threshold: Failures to trigger opening
            cooldown_minutes: Recovery cooldown duration
        """
        self.failure_threshold = failure_threshold
        self.cooldown_minutes = cooldown_minutes
        self.breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()

    def get_breaker(self, muse_id: str) -> CircuitBreaker:
        """Get or create circuit breaker for muse"""
        with self._lock:
            if muse_id not in self.breakers:
                self.breakers[muse_id] = CircuitBreaker(
                    muse_id,
                    failure_threshold=self.failure_threshold,
                    cooldown_minutes=self.cooldown_minutes
                )
            return self.breakers[muse_id]

    def is_muse_available(self, muse_id: str) -> bool:
        """Check if muse is available (circuit not open)"""
        breaker = self.get_breaker(muse_id)
        return breaker.is_available()

    def record_success(self, muse_id: str):
        """Record successful request for muse"""
        breaker = self.get_breaker(muse_id)
        breaker.record_success()

    def record_failure(self, muse_id: str, error: str):
        """Record failed request for muse"""
        breaker = self.get_breaker(muse_id)
        breaker.record_failure(error)

    def get_available_muses(self, all_muses: List[str]) -> List[str]:
        """
        Filter list of muses to only those available (circuit not open)
        
        Args:
            all_muses: List of all muse IDs
            
        Returns:
            List of available muses
        """
        available = []
        for muse_id in all_muses:
            if self.is_muse_available(muse_id):
                available.append(muse_id)

        return available

    def get_all_metrics(self) -> List[Dict]:
        """Get metrics for all muses"""
        with self._lock:
            metrics = []
            for breaker in self.breakers.values():
                metrics.append(breaker.get_metrics())
            return metrics

    def get_muse_metrics(self, muse_id: str) -> Optional[Dict]:
        """Get metrics for specific muse"""
        if muse_id in self.breakers:
            return self.breakers[muse_id].get_metrics()
        return None

    def health_report(self) -> Dict:
        """Get overall health report"""
        with self._lock:
            total = len(self.breakers)
            closed = sum(1 for b in self.breakers.values() if b.state == CircuitState.CLOSED)
            open_count = sum(1 for b in self.breakers.values() if b.state == CircuitState.OPEN)
            half_open = total - closed - open_count

            total_failures = sum(b.metrics.failure_count for b in self.breakers.values())
            total_successes = sum(b.metrics.success_count for b in self.breakers.values())

            return {
                'total_muses': total,
                'healthy': closed,
                'degraded': half_open,
                'disabled': open_count,
                'availability_pct': (closed / total * 100) if total > 0 else 0,
                'total_failures': total_failures,
                'total_successes': total_successes,
                'failure_rate': (
                    total_failures / (total_failures + total_successes) * 100
                    if (total_failures + total_successes) > 0 else 0
                ),
            }
