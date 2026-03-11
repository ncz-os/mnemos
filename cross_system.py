"""
Cross-System Features: Health Dashboard, Distributed Tracing, A/B Testing, Batch API
"""

import os
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================================================
# Feature 1: Distributed Tracing (OpenTelemetry)
# ============================================================================

class DistributedTracingIntegration:
    """OpenTelemetry integration for request flow tracking"""

    def __init__(self, service_name: str = "mnemos-graeae"):
        self.service_name = service_name
        self.enabled = os.getenv('OTEL_ENABLED', 'false').lower() == 'true'
        self.traces = {}
        self._lock = threading.RLock()

    def start_trace(self, request_id: str, trace_id: Optional[str] = None) -> str:
        """Start a new trace"""
        import uuid
        trace_id = trace_id or str(uuid.uuid4())
        
        with self._lock:
            self.traces[request_id] = {
                'trace_id': trace_id,
                'start_time': datetime.utcnow().isoformat(),
                'spans': [],
                'service': self.service_name
            }
        
        return trace_id

    def add_span(self, request_id: str, span_name: str, duration_ms: float, attributes: Optional[Dict] = None):
        """Add span to trace"""
        with self._lock:
            if request_id in self.traces:
                span = {
                    'name': span_name,
                    'duration_ms': duration_ms,
                    'timestamp': datetime.utcnow().isoformat(),
                    'attributes': attributes or {}
                }
                self.traces[request_id]['spans'].append(span)

    def get_trace(self, request_id: str) -> Optional[Dict]:
        """Get complete trace"""
        with self._lock:
            return self.traces.get(request_id)

    def export_trace(self, request_id: str) -> str:
        """Export trace as JSON"""
        trace = self.get_trace(request_id)
        if not trace:
            return ""
        
        return json.dumps(trace, indent=2)


# ============================================================================
# Feature 2: Health Dashboard
# ============================================================================

class HealthMetrics:
    """Collects health metrics for dashboard"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or '/var/lib/mnemos/health.db'
        self._init_schema()

    def _init_schema(self):
        """Initialize health metrics database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS health_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    metric_type TEXT,
                    tags TEXT
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_health_timestamp 
                ON health_metrics(timestamp DESC)
            """)

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to init health schema: {e}")

    def record_metric(self, name: str, value: float, metric_type: str = 'gauge', tags: Optional[Dict] = None):
        """Record a health metric"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            now = datetime.utcnow().isoformat()
            tags_json = json.dumps(tags or {})

            cur.execute("""
                INSERT INTO health_metrics 
                (timestamp, metric_name, metric_value, metric_type, tags)
                VALUES (?, ?, ?, ?, ?)
            """, (now, name, value, metric_type, tags_json))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to record metric: {e}")

    def get_current_health(self) -> Dict[str, Any]:
        """Get current system health snapshot"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # Get latest metric for each metric_name
            cur.execute("""
                SELECT DISTINCT metric_name
                FROM health_metrics
                WHERE timestamp > datetime('now', '-1 hour')
            """)

            metrics = {}
            for (metric_name,) in cur.fetchall():
                cur.execute("""
                    SELECT metric_value, timestamp
                    FROM health_metrics
                    WHERE metric_name = ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                """, (metric_name,))

                row = cur.fetchone()
                if row:
                    value, timestamp = row
                    metrics[metric_name] = {
                        'value': value,
                        'timestamp': timestamp
                    }

            conn.close()

            # Compute health status
            status = "healthy"
            if metrics.get('queue_depth', {}).get('value', 0) > 8000:
                status = "degraded"
            if metrics.get('error_rate', {}).get('value', 0) > 0.1:
                status = "critical"

            return {
                'status': status,
                'timestamp': datetime.utcnow().isoformat(),
                'metrics': metrics
            }

        except Exception as e:
            logger.error(f"Failed to get health: {e}")
            return {}


class DashboardAPI:
    """API for health dashboard"""

    def __init__(self, health_metrics: HealthMetrics):
        self.health = health_metrics

    def get_system_status(self) -> Dict:
        """Get overall system status"""
        return self.health.get_current_health()

    def get_muse_health(self, circuit_breaker_pool) -> Dict:
        """Get health for all muses"""
        return circuit_breaker_pool.health_report()

    def get_queue_status(self, queue_obj) -> Dict:
        """Get queue statistics"""
        return queue_obj.get_queue_status()

    def render_dashboard_json(self, circuit_pool=None, queue_obj=None) -> str:
        """Render dashboard as JSON for frontend"""
        dashboard = {
            'system': self.get_system_status(),
            'muses': self.get_muse_health(circuit_pool) if circuit_pool else {},
            'queue': self.get_queue_status(queue_obj) if queue_obj else {},
            'timestamp': datetime.utcnow().isoformat()
        }

        return json.dumps(dashboard, indent=2)


# ============================================================================
# Feature 3: A/B Testing Framework
# ============================================================================

class ABTestVariant(Enum):
    """Test variants"""
    CONTROL = "control"
    VARIANT_A = "variant_a"
    VARIANT_B = "variant_b"


class ABTestingFramework:
    """Compare routing strategies, measure quality vs latency"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or '/var/lib/mnemos/ab_tests.db'
        self._init_schema()

    def _init_schema(self):
        """Initialize A/B testing database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ab_test_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    assigned_at TIMESTAMP NOT NULL,
                    
                    UNIQUE (test_name, user_id)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ab_test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_name TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    quality_score REAL,
                    latency_ms REAL,
                    user_satisfied BOOLEAN,
                    timestamp TIMESTAMP NOT NULL
                )
            """)

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to init A/B test schema: {e}")

    def assign_variant(self, test_name: str, user_id: str) -> ABTestVariant:
        """Assign user to test variant"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # Check existing assignment
            cur.execute("""
                SELECT variant FROM ab_test_assignments
                WHERE test_name = ? AND user_id = ?
            """, (test_name, user_id))

            existing = cur.fetchone()
            if existing:
                return ABTestVariant(existing[0])

            # Assign new variant (round-robin for simplicity)
            import hashlib
            hash_val = int(hashlib.md5(f"{user_id}{test_name}".encode()).hexdigest(), 16)
            variant_idx = hash_val % 3

            variants = [ABTestVariant.CONTROL, ABTestVariant.VARIANT_A, ABTestVariant.VARIANT_B]
            variant = variants[variant_idx]

            now = datetime.utcnow().isoformat()
            cur.execute("""
                INSERT INTO ab_test_assignments (test_name, user_id, variant, assigned_at)
                VALUES (?, ?, ?, ?)
            """, (test_name, user_id, variant.value, now))

            conn.commit()
            conn.close()

            return variant

        except Exception as e:
            logger.error(f"Failed to assign variant: {e}")
            return ABTestVariant.CONTROL

    def record_result(
        self,
        test_name: str,
        variant: ABTestVariant,
        request_id: str,
        quality_score: float,
        latency_ms: float,
        user_satisfied: Optional[bool] = None
    ):
        """Record A/B test result"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            now = datetime.utcnow().isoformat()

            cur.execute("""
                INSERT INTO ab_test_results
                (test_name, variant, request_id, quality_score, latency_ms, 
                 user_satisfied, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                test_name, variant.value, request_id, quality_score,
                latency_ms, user_satisfied, now
            ))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to record test result: {e}")

    def get_test_results(self, test_name: str, days: int = 7) -> Dict:
        """Analyze A/B test results"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

            cur.execute("""
                SELECT variant, COUNT(*), AVG(quality_score), AVG(latency_ms)
                FROM ab_test_results
                WHERE test_name = ? AND timestamp > ?
                GROUP BY variant
            """, (test_name, cutoff))

            results = {}
            for variant, count, avg_quality, avg_latency in cur.fetchall():
                results[variant] = {
                    'samples': count,
                    'avg_quality': avg_quality,
                    'avg_latency': avg_latency
                }

            conn.close()
            return results

        except Exception as e:
            logger.error(f"Failed to get test results: {e}")
            return {}


# ============================================================================
# Feature 4: Batch Operations API
# ============================================================================

class BatchJob:
    """Represents a batch job"""

    def __init__(self, job_id: str, queries: List[str], muse_ids: Optional[List[str]] = None):
        self.job_id = job_id
        self.queries = queries
        self.muse_ids = muse_ids or []
        self.results = []
        self.status = 'pending'  # pending, processing, completed
        self.created_at = datetime.utcnow().isoformat()


class BatchOperationsAPI:
    """Submit 100s of queries, async results"""

    def __init__(self, queue_obj, db_path: Optional[str] = None):
        self.queue = queue_obj
        self.db_path = db_path or '/var/lib/mnemos/batch_jobs.db'
        self.jobs = {}  # In-memory cache
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        """Initialize batch database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS batch_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    total_queries INTEGER,
                    completed_queries INTEGER DEFAULT 0,
                    created_at TIMESTAMP NOT NULL,
                    completed_at TIMESTAMP,
                    results TEXT
                )
            """)

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to init batch schema: {e}")

    def submit_batch(self, queries: List[str], muse_ids: Optional[List[str]] = None) -> str:
        """
        Submit batch of queries for async processing
        
        Returns:
            Job ID for tracking
        """
        import uuid
        job_id = str(uuid.uuid4())

        with self._lock:
            job = BatchJob(job_id, queries, muse_ids)
            self.jobs[job_id] = job

            # Queue all queries
            for idx, query in enumerate(queries):
                req_id = f"{job_id}-{idx}"
                muse_id = muse_ids[idx % len(muse_ids)] if muse_ids else "default"
                self.queue.enqueue(req_id, muse_id, query, {'batch_job_id': job_id})

            # Persist to database
            try:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                now = datetime.utcnow().isoformat()
                cur.execute("""
                    INSERT INTO batch_jobs (job_id, status, total_queries, created_at)
                    VALUES (?, ?, ?, ?)
                """, (job_id, 'pending', len(queries), now))

                conn.commit()
                conn.close()

            except Exception as e:
                logger.error(f"Failed to persist batch job: {e}")

            logger.info(f"Batch job {job_id} submitted: {len(queries)} queries")
            return job_id

    def get_batch_status(self, job_id: str) -> Dict:
        """Get batch job status"""
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return {}

            return {
                'job_id': job_id,
                'status': job.status,
                'total': len(job.queries),
                'completed': len(job.results),
                'progress_pct': (len(job.results) / len(job.queries) * 100) if job.queries else 0,
                'created_at': job.created_at
            }

    def record_batch_result(self, job_id: str, query_idx: int, result: str):
        """Record result for a query in batch"""
        with self._lock:
            if job_id in self.jobs:
                job = self.jobs[job_id]
                job.results.append({
                    'index': query_idx,
                    'result': result,
                    'timestamp': datetime.utcnow().isoformat()
                })

                # Update database
                try:
                    conn = sqlite3.connect(self.db_path)
                    cur = conn.cursor()

                    cur.execute("""
                        UPDATE batch_jobs
                        SET completed_queries = ?
                        WHERE job_id = ?
                    """, (len(job.results), job_id))

                    if len(job.results) == len(job.queries):
                        cur.execute("""
                            UPDATE batch_jobs
                            SET status = 'completed', completed_at = ?
                            WHERE job_id = ?
                        """, (datetime.utcnow().isoformat(), job_id))
                        job.status = 'completed'

                    conn.commit()
                    conn.close()

                except Exception as e:
                    logger.error(f"Failed to update batch: {e}")

    def get_batch_results(self, job_id: str) -> List[Dict]:
        """Get results for completed batch"""
        with self._lock:
            if job_id in self.jobs:
                return self.jobs[job_id].results
            return []
