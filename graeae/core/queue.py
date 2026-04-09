"""
GRAEAE Feature 1: Request Persistence & Resumability
SQLite-backed queue for crash-safe, resumable request handling
Allows recovery from failures without losing request state
"""

import os
import json
import sqlite3
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


class RequestStatus(Enum):
    """Request lifecycle states"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    ABANDONED = "abandoned"


@dataclass
class QueuedRequest:
    """Represents a queued request"""
    id: int
    request_id: str
    muse_id: str
    query: str
    metadata: Dict[str, Any]
    status: RequestStatus
    created_at: str
    attempted_at: Optional[str]
    completed_at: Optional[str]
    error_message: Optional[str]
    retry_count: int
    max_retries: int


class PersistentQueue:
    """SQLite-backed persistent queue for GRAEAE requests"""

    def __init__(self, db_path: Optional[str] = None, max_retries: int = 3):
        """
        Initialize persistent queue
        
        Args:
            db_path: Path to SQLite database
            max_retries: Maximum retry attempts before abandoning
        """
        self.db_path = db_path or os.getenv(
            'GRAEAE_QUEUE_DB',
            '/var/lib/mnemos/graeae_queue.db'
        )
        self.max_retries = max_retries
        
        # Ensure directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Lock for thread safety
        self._lock = threading.RLock()
        
        self._init_schema()

    def _init_schema(self):
        """Initialize SQLite schema for request queue"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Create request queue table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS request_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT UNIQUE NOT NULL,
                    muse_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    attempted_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER NOT NULL,
                    
                    CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'retrying', 'abandoned'))
                )
            """)

            # Create indices for efficiency
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_status 
                ON request_queue(status)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_muse 
                ON request_queue(muse_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_created 
                ON request_queue(created_at DESC)
            """)

            # Create recovery log for audit
            cur.execute("""
                CREATE TABLE IF NOT EXISTS recovery_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    details TEXT
                )
            """)

            conn.commit()
            conn.close()
            logger.info(f"Queue schema initialized: {self.db_path}")

        except Exception as e:
            logger.error(f"Failed to init queue schema: {e}")
            raise

    def enqueue(
        self,
        request_id: str,
        muse_id: str,
        query: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Add a new request to the queue
        
        Args:
            request_id: Unique request identifier
            muse_id: Target muse ID
            query: Query/prompt text
            metadata: Additional request context
            
        Returns:
            True if successfully enqueued
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                now = datetime.utcnow().isoformat()
                metadata_json = json.dumps(metadata or {})

                cur.execute("""
                    INSERT INTO request_queue
                    (request_id, muse_id, query, metadata, status, created_at, max_retries)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    request_id,
                    muse_id,
                    query,
                    metadata_json,
                    RequestStatus.PENDING.value,
                    now,
                    self.max_retries
                ))

                conn.commit()
                conn.close()

                logger.debug(f"Enqueued request {request_id} for muse {muse_id}")
                return True

        except sqlite3.IntegrityError:
            logger.warning(f"Request {request_id} already exists in queue")
            return False
        except Exception as e:
            logger.error(f"Failed to enqueue request: {e}")
            return False

    def dequeue(self, muse_id: Optional[str] = None) -> Optional[QueuedRequest]:
        """
        Get next pending request from queue
        
        Args:
            muse_id: Filter by specific muse (optional)
            
        Returns:
            Next queued request or None
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                # Find pending or retrying request
                if muse_id:
                    cur.execute("""
                        SELECT * FROM request_queue
                        WHERE (status = 'pending' OR status = 'retrying')
                        AND muse_id = ?
                        ORDER BY created_at ASC
                        LIMIT 1
                    """, (muse_id,))
                else:
                    cur.execute("""
                        SELECT * FROM request_queue
                        WHERE (status = 'pending' OR status = 'retrying')
                        ORDER BY created_at ASC
                        LIMIT 1
                    """)

                row = cur.fetchone()
                if not row:
                    return None

                # Update status to processing
                request_id = row['request_id']
                now = datetime.utcnow().isoformat()

                cur.execute("""
                    UPDATE request_queue
                    SET status = 'processing', attempted_at = ?
                    WHERE request_id = ?
                """, (now, request_id))

                conn.commit()
                conn.close()

                return QueuedRequest(
                    id=row['id'],
                    request_id=row['request_id'],
                    muse_id=row['muse_id'],
                    query=row['query'],
                    metadata=json.loads(row['metadata']),
                    status=RequestStatus.PROCESSING,
                    created_at=row['created_at'],
                    attempted_at=now,
                    completed_at=row['completed_at'],
                    error_message=row['error_message'],
                    retry_count=row['retry_count'],
                    max_retries=row['max_retries']
                )

        except Exception as e:
            logger.error(f"Failed to dequeue request: {e}")
            return None

    def mark_completed(self, request_id: str) -> bool:
        """Mark request as successfully completed"""
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                now = datetime.utcnow().isoformat()

                cur.execute("""
                    UPDATE request_queue
                    SET status = 'completed', completed_at = ?
                    WHERE request_id = ?
                """, (now, request_id))

                affected = cur.rowcount
                conn.commit()
                conn.close()

                if affected > 0:
                    self._log_recovery(request_id, 'completed', 'Request succeeded')
                    logger.debug(f"Marked {request_id} as completed")

                return affected > 0

        except Exception as e:
            logger.error(f"Failed to mark completed: {e}")
            return False

    def mark_failed(self, request_id: str, error: str) -> bool:
        """
        Mark request as failed and schedule retry if possible
        
        Args:
            request_id: Request ID
            error: Error message
            
        Returns:
            True if retry scheduled, False if abandoned
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                # Get current retry count
                cur.execute(
                    "SELECT retry_count, max_retries FROM request_queue WHERE request_id = ?",
                    (request_id,)
                )
                row = cur.fetchone()

                if not row:
                    return False

                retry_count, max_retries = row
                new_retry_count = retry_count + 1

                if new_retry_count < max_retries:
                    # Schedule retry
                    new_status = RequestStatus.RETRYING.value
                    self._log_recovery(
                        request_id,
                        'retrying',
                        f"Retry {new_retry_count}/{max_retries}: {error}"
                    )
                else:
                    # Abandon request
                    new_status = RequestStatus.ABANDONED.value
                    self._log_recovery(
                        request_id,
                        'abandoned',
                        f"Max retries exceeded: {error}"
                    )

                cur.execute("""
                    UPDATE request_queue
                    SET status = ?, error_message = ?, retry_count = ?
                    WHERE request_id = ?
                """, (new_status, error[:500], new_retry_count, request_id))

                conn.commit()
                conn.close()

                logger.warning(f"Failed request {request_id} -> {new_status}: {error}")
                return new_retry_count < max_retries

        except Exception as e:
            logger.error(f"Failed to mark failed: {e}")
            return False

    def get_queue_status(self, muse_id: Optional[str] = None) -> Dict[str, int]:
        """
        Get queue statistics
        
        Args:
            muse_id: Filter by muse (optional)
            
        Returns:
            Dictionary of status counts
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                if muse_id:
                    where_clause = "WHERE muse_id = ?"
                    params = (muse_id,)
                else:
                    where_clause = ""
                    params = ()

                status_counts = {}
                for status in RequestStatus:
                    query = f"""
                        SELECT COUNT(*) FROM request_queue
                        WHERE status = ?
                        {where_clause}
                    """
                    cur.execute(query, (status.value,) + params)
                    count = cur.fetchone()[0]
                    status_counts[status.value] = count

                conn.close()
                return status_counts

        except Exception as e:
            logger.error(f"Failed to get queue status: {e}")
            return {}

    def recover_stuck_requests(self, timeout_minutes: int = 30) -> int:
        """
        Recover requests stuck in PROCESSING state
        Marks them for retry if they're older than timeout
        
        Args:
            timeout_minutes: Consider processing request stuck after this many minutes
            
        Returns:
            Number of requests recovered
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                cutoff_time = (datetime.utcnow() - timedelta(minutes=timeout_minutes)).isoformat()

                cur.execute("""
                    SELECT request_id FROM request_queue
                    WHERE status = 'processing'
                    AND attempted_at < ?
                """, (cutoff_time,))

                stuck_requests = [row[0] for row in cur.fetchall()]

                for req_id in stuck_requests:
                    cur.execute("""
                        UPDATE request_queue
                        SET status = 'retrying'
                        WHERE request_id = ?
                    """, (req_id,))
                    self._log_recovery(
                        req_id,
                        'recovery',
                        f'Recovered from stuck processing state'
                    )

                conn.commit()
                conn.close()

                if stuck_requests:
                    logger.warning(f"Recovered {len(stuck_requests)} stuck requests")

                return len(stuck_requests)

        except Exception as e:
            logger.error(f"Failed to recover stuck requests: {e}")
            return 0

    def cleanup_old_requests(self, days: int = 30) -> int:
        """
        Clean up old completed/abandoned requests
        
        Args:
            days: Delete requests older than N days
            
        Returns:
            Number of requests deleted
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                cutoff_time = (datetime.utcnow() - timedelta(days=days)).isoformat()

                cur.execute("""
                    DELETE FROM request_queue
                    WHERE (status = 'completed' OR status = 'abandoned')
                    AND created_at < ?
                """, (cutoff_time,))

                deleted = cur.rowcount
                conn.commit()
                conn.close()

                logger.info(f"Cleaned up {deleted} old requests (>{days}d)")
                return deleted

        except Exception as e:
            logger.error(f"Failed to cleanup old requests: {e}")
            return 0

    def _log_recovery(self, request_id: str, action: str, details: str):
        """Log recovery action for audit trail"""
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                now = datetime.utcnow().isoformat()

                cur.execute("""
                    INSERT INTO recovery_log (request_id, action, timestamp, details)
                    VALUES (?, ?, ?, ?)
                """, (request_id, action, now, details))

                conn.commit()
                conn.close()

        except Exception as e:
            logger.error(f"Failed to log recovery: {e}")
