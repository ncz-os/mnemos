"""
GRAEAE Feature 3: Response Quality Scoring
Automated QA metrics for relevance, coherence, toxicity per-muse
"""

import os
import json
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class QualityScore:
    """Quality metrics for a response"""
    relevance: float        # 0-1: How relevant to query
    coherence: float        # 0-1: How coherent/logical
    toxicity: float         # 0-1: How toxic/harmful
    completeness: float     # 0-1: How complete
    accuracy: Optional[float] = None  # 0-1: Factual accuracy (optional)


class ResponseQualityScorer:
    """
    Automated quality scoring for muse responses
    Tracks per-muse metrics for ranking and A/B testing
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize quality scorer
        
        Args:
            db_path: Path to metrics database
        """
        self.db_path = db_path or os.getenv(
            'GRAEAE_METRICS_DB',
            '/var/lib/mnemos/graeae_metrics.db'
        )
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        """Initialize metrics database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # Quality scores table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quality_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    muse_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    response TEXT,
                    relevance REAL,
                    coherence REAL,
                    toxicity REAL,
                    completeness REAL,
                    accuracy REAL,
                    overall_score REAL,
                    timestamp TIMESTAMP NOT NULL,
                    user_feedback REAL,
                    
                    FOREIGN KEY (muse_id) REFERENCES muses(id)
                )
            """)

            # Muse performance aggregates
            cur.execute("""
                CREATE TABLE IF NOT EXISTS muse_metrics (
                    muse_id TEXT PRIMARY KEY,
                    response_count INTEGER DEFAULT 0,
                    avg_relevance REAL,
                    avg_coherence REAL,
                    avg_toxicity REAL,
                    avg_completeness REAL,
                    avg_accuracy REAL,
                    avg_overall REAL,
                    last_updated TIMESTAMP,
                    
                    FOREIGN KEY (muse_id) REFERENCES muses(id)
                )
            """)

            # Indices
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_quality_muse 
                ON quality_scores(muse_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_quality_timestamp 
                ON quality_scores(timestamp DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_quality_request 
                ON quality_scores(request_id)
            """)

            conn.commit()
            conn.close()
            logger.info(f"Quality scorer schema initialized: {self.db_path}")

        except Exception as e:
            logger.error(f"Failed to init quality scorer schema: {e}")

    def compute_quality(
        self,
        query: str,
        response: str,
        query_embedding: Optional[List[float]] = None,
        response_embedding: Optional[List[float]] = None
    ) -> QualityScore:
        """
        Compute quality metrics for a response
        
        Args:
            query: Original query
            response: Muse response
            query_embedding: Query embedding vector
            response_embedding: Response embedding vector
            
        Returns:
            QualityScore object
        """
        score = QualityScore(
            relevance=self._compute_relevance(query, response, query_embedding, response_embedding),
            coherence=self._compute_coherence(response),
            toxicity=self._compute_toxicity(response),
            completeness=self._compute_completeness(response),
        )

        return score

    def _compute_relevance(
        self,
        query: str,
        response: str,
        query_emb: Optional[List[float]] = None,
        response_emb: Optional[List[float]] = None
    ) -> float:
        """
        Compute relevance score (0-1)
        Measures how well response answers the query
        """
        # Basic keyword matching
        query_words = set(query.lower().split())
        response_text = response.lower()
        
        matching_words = sum(1 for w in query_words if w in response_text)
        keyword_score = matching_words / len(query_words) if query_words else 0

        # If embeddings available, use cosine similarity
        if query_emb and response_emb:
            cosine_sim = self._cosine_similarity(query_emb, response_emb)
            # Weighted combination: 60% embedding, 40% keywords
            return (cosine_sim * 0.6 + keyword_score * 0.4)

        # Fallback: keyword matching
        return min(keyword_score, 1.0)

    def _compute_coherence(self, response: str) -> float:
        """
        Compute coherence score (0-1)
        Measures logical flow and structure
        """
        # Simple heuristics
        lines = response.split('\n')
        paragraphs = [l for l in lines if l.strip()]

        if not paragraphs:
            return 0.0

        # Prefer responses with multiple sentences
        sentence_count = response.count('.') + response.count('!') + response.count('?')
        sentence_score = min(sentence_count / 5, 1.0)  # Prefer 5+ sentences

        # Prefer responses with good length
        word_count = len(response.split())
        length_score = min(word_count / 50, 1.0)  # Prefer 50+ words

        # Check for repetition (lower score for repetitive)
        words = response.lower().split()
        if words:
            unique_ratio = len(set(words)) / len(words)
            repetition_score = unique_ratio
        else:
            repetition_score = 0.0

        # Weighted combination
        coherence = (sentence_score * 0.3 + length_score * 0.3 + repetition_score * 0.4)
        return min(coherence, 1.0)

    def _compute_toxicity(self, response: str) -> float:
        """
        Compute toxicity score (0-1, where 0 = clean, 1 = very toxic)
        Detects harmful, offensive, or inappropriate language
        """
        # Simple keyword-based detection
        toxic_keywords = {
            'hate', 'kill', 'attack', 'destroy', 'harm', 'abuse',
            'racist', 'sexist', 'slur', 'offensive', 'inappropriate'
        }

        response_lower = response.lower()
        toxic_matches = sum(1 for keyword in toxic_keywords if keyword in response_lower)

        # Normalize to 0-1
        toxicity = min(toxic_matches / len(toxic_keywords), 1.0)

        # Also check for excessive caps (often aggressive)
        caps_ratio = sum(1 for c in response if c.isupper()) / max(len(response), 1)
        if caps_ratio > 0.5:
            toxicity = min(toxicity + 0.2, 1.0)

        return toxicity

    def _compute_completeness(self, response: str) -> float:
        """
        Compute completeness score (0-1)
        Measures if response fully addresses the query
        """
        # Simple heuristics based on response characteristics
        word_count = len(response.split())
        
        # Longer responses tend to be more complete
        length_score = min(word_count / 200, 1.0)  # Prefer 200+ words

        # Check for conclusion phrases
        conclusion_phrases = ['in conclusion', 'to summarize', 'in summary', 'finally', 'therefore']
        has_conclusion = any(phrase in response.lower() for phrase in conclusion_phrases)
        conclusion_score = 1.0 if has_conclusion else 0.7

        # Check for specific content indicators
        indicators = response.count(':') + response.count('-') + response.count('•')
        indicator_score = min(indicators / 3, 1.0)

        completeness = (length_score * 0.4 + conclusion_score * 0.3 + indicator_score * 0.3)
        return min(completeness, 1.0)

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Compute cosine similarity between two vectors"""
        if not vec1 or not vec2 or len(vec1) != len(vec2):
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        mag1 = sum(a * a for a in vec1) ** 0.5
        mag2 = sum(b * b for b in vec2) ** 0.5

        if mag1 == 0 or mag2 == 0:
            return 0.0

        return dot_product / (mag1 * mag2)

    def record_score(
        self,
        muse_id: str,
        request_id: str,
        query: str,
        response: str,
        score: QualityScore,
        user_feedback: Optional[float] = None
    ) -> bool:
        """
        Record quality score for a response
        
        Args:
            muse_id: Muse that generated response
            request_id: Request ID
            query: Original query
            response: Generated response
            score: QualityScore object
            user_feedback: Optional user rating (0-1)
            
        Returns:
            True if recorded
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()

                overall = (
                    score.relevance * 0.35 +
                    score.coherence * 0.35 +
                    (1.0 - score.toxicity) * 0.20 +
                    score.completeness * 0.10
                )

                now = datetime.utcnow().isoformat()

                cur.execute("""
                    INSERT INTO quality_scores
                    (muse_id, request_id, query, response, 
                     relevance, coherence, toxicity, completeness, accuracy,
                     overall_score, timestamp, user_feedback)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    muse_id, request_id, query, response,
                    score.relevance, score.coherence, score.toxicity, score.completeness,
                    score.accuracy, overall, now, user_feedback
                ))

                conn.commit()
                conn.close()

                # Update aggregates
                self._update_muse_metrics(muse_id)

                logger.debug(f"Recorded quality score for {muse_id}: {overall:.2f}")
                return True

        except Exception as e:
            logger.error(f"Failed to record quality score: {e}")
            return False

    def _update_muse_metrics(self, muse_id: str):
        """Update aggregate metrics for muse"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # Compute averages from recent scores
            cur.execute("""
                SELECT 
                    COUNT(*),
                    AVG(relevance),
                    AVG(coherence),
                    AVG(toxicity),
                    AVG(completeness),
                    AVG(accuracy),
                    AVG(overall_score)
                FROM quality_scores
                WHERE muse_id = ?
                AND timestamp > datetime('now', '-7 days')
            """, (muse_id,))

            row = cur.fetchone()
            count, avg_rel, avg_coh, avg_tox, avg_comp, avg_acc, avg_overall = row

            if count > 0:
                # Insert or update aggregate
                cur.execute("""
                    INSERT INTO muse_metrics 
                    (muse_id, response_count, avg_relevance, avg_coherence, 
                     avg_toxicity, avg_completeness, avg_accuracy, avg_overall, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(muse_id) DO UPDATE SET
                        response_count = excluded.response_count,
                        avg_relevance = excluded.avg_relevance,
                        avg_coherence = excluded.avg_coherence,
                        avg_toxicity = excluded.avg_toxicity,
                        avg_completeness = excluded.avg_completeness,
                        avg_accuracy = excluded.avg_accuracy,
                        avg_overall = excluded.avg_overall,
                        last_updated = excluded.last_updated
                """, (
                    muse_id, count, avg_rel, avg_coh, avg_tox, avg_comp, avg_acc, avg_overall,
                    datetime.utcnow().isoformat()
                ))

                conn.commit()

            conn.close()

        except Exception as e:
            logger.error(f"Failed to update muse metrics: {e}")

    def get_muse_metrics(self, muse_id: str) -> Optional[Dict]:
        """Get quality metrics for a muse"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("""
                SELECT * FROM muse_metrics WHERE muse_id = ?
            """, (muse_id,))

            row = cur.fetchone()
            conn.close()

            if not row:
                return None

            return {
                'muse_id': row[0],
                'response_count': row[1],
                'avg_relevance': row[2],
                'avg_coherence': row[3],
                'avg_toxicity': row[4],
                'avg_completeness': row[5],
                'avg_accuracy': row[6],
                'avg_overall': row[7],
                'last_updated': row[8],
            }

        except Exception as e:
            logger.error(f"Failed to get muse metrics: {e}")
            return None

    def get_best_muses(self, count: int = 5, min_samples: int = 10) -> List[Dict]:
        """
        Get top-performing muses by overall quality
        
        Args:
            count: Number of muses to return
            min_samples: Minimum responses required
            
        Returns:
            List of muse metrics sorted by quality
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("""
                SELECT * FROM muse_metrics
                WHERE response_count >= ?
                ORDER BY avg_overall DESC
                LIMIT ?
            """, (min_samples, count))

            rows = cur.fetchall()
            conn.close()

            results = []
            for row in rows:
                results.append({
                    'muse_id': row[0],
                    'response_count': row[1],
                    'avg_relevance': row[2],
                    'avg_coherence': row[3],
                    'avg_toxicity': row[4],
                    'avg_completeness': row[5],
                    'avg_accuracy': row[6],
                    'avg_overall': row[7],
                })

            return results

        except Exception as e:
            logger.error(f"Failed to get best muses: {e}")
            return []
