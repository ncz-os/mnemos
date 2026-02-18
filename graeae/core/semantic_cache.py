"""
GRAEAE Feature 4: Semantic Caching Layer
Embeddings-based similarity matching beyond exact-match, 24h window
"""

import os
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Cached response entry"""
    id: int
    query: str
    response: str
    muse_id: str
    embedding: List[float]
    created_at: str
    ttl_seconds: int


class SemanticCache:
    """Embeddings-based cache for query responses"""

    def __init__(self, db_path: Optional[str] = None, ttl_hours: int = 24):
        """
        Initialize semantic cache
        
        Args:
            db_path: Path to cache database
            ttl_hours: Time-to-live for cached entries (default 24h)
        """
        self.db_path = db_path or os.getenv(
            'GRAEAE_CACHE_DB',
            '/var/lib/mnemos/graeae_cache.db'
        )
        self.ttl_seconds = ttl_hours * 3600
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        """Initialize cache database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS semantic_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    response TEXT NOT NULL,
                    muse_id TEXT NOT NULL,
                    query_embedding BLOB NOT NULL,
                    response_embedding BLOB,
                    created_at TIMESTAMP NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    hit_count INTEGER DEFAULT 0,
                    last_hit TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_expires 
                ON semantic_cache(expires_at)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_muse 
                ON semantic_cache(muse_id)
            """)

            conn.commit()
            conn.close()
            logger.info(f"Semantic cache initialized: {self.db_path}")

        except Exception as e:
            logger.error(f"Failed to init semantic cache: {e}")

    def get(
        self,
        query: str,
        query_embedding: List[float],
        muse_id: Optional[str] = None,
        similarity_threshold: float = 0.85
    ) -> Optional[str]:
        """
        Get cached response if similar query exists
        
        Args:
            query: Query text
            query_embedding: Query embedding vector
            muse_id: Filter by specific muse (optional)
            similarity_threshold: Minimum cosine similarity (0-1)
            
        Returns:
            Cached response or None
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                # Get all non-expired entries
                if muse_id:
                    cur.execute("""
                        SELECT id, query_embedding, response FROM semantic_cache
                        WHERE expires_at > datetime('now')
                        AND muse_id = ?
                        ORDER BY hit_count DESC
                    """, (muse_id,))
                else:
                    cur.execute("""
                        SELECT id, query_embedding, response FROM semantic_cache
                        WHERE expires_at > datetime('now')
                        ORDER BY hit_count DESC
                    """)

                rows = cur.fetchall()

                best_match = None
                best_similarity = 0.0

                # Find best matching entry by embedding similarity
                for cache_id, stored_emb_bytes, response in rows:
                    try:
                        stored_emb = json.loads(stored_emb_bytes)
                        similarity = self._cosine_similarity(query_embedding, stored_emb)

                        if similarity > best_similarity:
                            best_similarity = similarity
                            best_match = (cache_id, response)
                    except:
                        continue

                if best_match and best_similarity >= similarity_threshold:
                    # Update hit count and timestamp
                    cache_id, response = best_match
                    cur.execute("""
                        UPDATE semantic_cache
                        SET hit_count = hit_count + 1, last_hit = datetime('now')
                        WHERE id = ?
                    """, (cache_id,))
                    conn.commit()

                conn.close()
                logger.debug(f"Cache hit (similarity={best_similarity:.3f})")
                return response

        except Exception as e:
            logger.error(f"Failed to get from cache: {e}")

        return None

    def put(
        self,
        query: str,
        response: str,
        query_embedding: List[float],
        muse_id: str,
        response_embedding: Optional[List[float]] = None
    ) -> bool:
        """
        Store response in semantic cache
        
        Args:
            query: Query text
            response: Response text
            query_embedding: Query embedding vector
            muse_id: Muse that generated response
            response_embedding: Optional response embedding
            
        Returns:
            True if stored
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                now = datetime.utcnow()
                expires_at = (now + timedelta(seconds=self.ttl_seconds)).isoformat()

                cur.execute("""
                    INSERT INTO semantic_cache
                    (query, response, muse_id, query_embedding, response_embedding, 
                     created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    query,
                    response,
                    muse_id,
                    json.dumps(query_embedding),
                    json.dumps(response_embedding) if response_embedding else None,
                    now.isoformat(),
                    expires_at
                ))

                conn.commit()
                conn.close()
                logger.debug(f"Cached response for muse {muse_id}")
                return True

        except Exception as e:
            logger.error(f"Failed to cache response: {e}")
            return False

    def cleanup_expired(self) -> int:
        """
        Delete expired cache entries
        
        Returns:
            Number of entries deleted
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                cur.execute("""
                    DELETE FROM semantic_cache
                    WHERE expires_at < datetime('now')
                """)

                deleted = cur.rowcount
                conn.commit()
                conn.close()

                if deleted > 0:
                    logger.info(f"Cleaned {deleted} expired cache entries")

                return deleted

        except Exception as e:
            logger.error(f"Failed to cleanup cache: {e}")
            return 0

    def get_stats(self) -> Dict:
        """Get cache statistics"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM semantic_cache")
            total = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*) FROM semantic_cache
                WHERE expires_at > datetime('now')
            """)
            valid = cur.fetchone()[0]

            cur.execute("SELECT SUM(hit_count) FROM semantic_cache")
            total_hits = cur.fetchone()[0] or 0

            conn.close()

            return {
                'total_entries': total,
                'valid_entries': valid,
                'expired_entries': total - valid,
                'total_hits': total_hits,
            }

        except Exception as e:
            logger.error(f"Failed to get cache stats: {e}")
            return {}

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Compute cosine similarity between vectors"""
        if not vec1 or not vec2 or len(vec1) != len(vec2):
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        mag1 = sum(a * a for a in vec1) ** 0.5
        mag2 = sum(b * b for b in vec2) ** 0.5

        if mag1 == 0 or mag2 == 0:
            return 0.0

        return dot_product / (mag1 * mag2)
