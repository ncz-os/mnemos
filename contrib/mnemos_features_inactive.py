"""
MNEMOS: All 6 Features in Single Module
1. Memory Decay & TTL
2. Duplicate Detection  
3. Memory Importance Scoring
4. Incremental Backups
5. Knowledge Graph Integration
6. Privacy/Retention Policies
"""

import os
import json
import sqlite3
import logging
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any, Tuple
from enum import Enum
import psycopg2

logger = logging.getLogger(__name__)


# ============================================================================
# Feature 1: Memory Decay & TTL
# ============================================================================

class MemoryDecayEngine:
    """Auto-expire memories based on age/access frequency"""

    def __init__(self, db_config: Dict[str, Any]):
        self.db_config = db_config

    def apply_decay(self) -> Tuple[int, int]:
        """
        Apply decay to memories, archiving old ones
        Returns: (archived_count, deleted_count)
        """
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            # Archive old memories (>1 year, low access)
            cutoff_date = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=365)).isoformat()

            cur.execute("""
                UPDATE memories
                SET archived = TRUE, archived_at = CURRENT_TIMESTAMP
                WHERE created_at < %s
                AND access_count < 5
                AND archived = FALSE
            """, (cutoff_date,))

            archived = cur.rowcount

            # Delete very old archived (>2 years)
            very_old_date = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=730)).isoformat()

            cur.execute("""
                DELETE FROM memories
                WHERE archived_at < %s
                AND archived = TRUE
            """, (very_old_date,))

            deleted = cur.rowcount
            conn.commit()
            cur.close()
            conn.close()

            logger.info(f"Decay applied: {archived} archived, {deleted} deleted")
            return archived, deleted

        except Exception as e:
            logger.error(f"Failed to apply memory decay: {e}")
            return 0, 0

    def compute_ttl(self, memory_id: int) -> Optional[timedelta]:
        """Compute remaining TTL for memory"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            cur.execute("""
                SELECT created_at, access_count, archived
                FROM memories WHERE id = %s
            """, (memory_id,))

            row = cur.fetchone()
            cur.close()
            conn.close()

            if not row:
                return None

            created, access_count, archived = row

            if archived:
                # Archived memories expire in 1 year
                expiry = created + timedelta(days=730)
            else:
                # Active memory TTL based on access frequency
                # Frequent: 5 years, rare: 1 year
                if access_count > 100:
                    expiry = created + timedelta(days=365*5)
                elif access_count > 50:
                    expiry = created + timedelta(days=365*3)
                else:
                    expiry = created + timedelta(days=365)

            remaining = expiry - datetime.now(timezone.utc).replace(tzinfo=None)
            return remaining if remaining.total_seconds() > 0 else None

        except Exception as e:
            logger.error(f"Failed to compute TTL: {e}")
            return None


# ============================================================================
# Feature 2: Duplicate Detection
# ============================================================================

class DeduplicationEngine:
    """Semantic deduplication, merge near-identical entries"""

    def __init__(self, db_config: Dict[str, Any], similarity_threshold: float = 0.85):
        self.db_config = db_config
        self.threshold = similarity_threshold

    def find_duplicates(self, user_id: str) -> List[Tuple[int, int, float]]:
        """
        Find potential duplicate memory pairs
        Returns: [(id1, id2, similarity), ...]
        """
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            # Get all memories for user
            cur.execute("""
                SELECT id, content, embedding FROM memories
                WHERE user_id = %s AND archived = FALSE
                ORDER BY id
            """, (user_id,))

            memories = cur.fetchall()
            cur.close()
            conn.close()

            duplicates = []
            for i, mem1 in enumerate(memories):
                for mem2 in memories[i+1:]:
                    id1, content1, emb1 = mem1
                    id2, content2, emb2 = mem2

                    # Exact match
                    if content1 == content2:
                        duplicates.append((id1, id2, 1.0))
                        continue

                    # Semantic similarity (if embeddings available)
                    if emb1 and emb2:
                        sim = self._cosine_similarity(emb1, emb2)
                        if sim >= self.threshold:
                            duplicates.append((id1, id2, sim))

            return duplicates

        except Exception as e:
            logger.error(f"Failed to find duplicates: {e}")
            return []

    def merge_duplicates(self, keep_id: int, merge_id: int) -> bool:
        """Merge two memories, keeping one and removing the other"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            # Update keep_id with merged metadata
            cur.execute("""
                SELECT content, metadata FROM memories WHERE id = %s
            """, (merge_id,))

            merge_row = cur.fetchone()
            merge_content, merge_meta = merge_row

            cur.execute("""
                SELECT metadata FROM memories WHERE id = %s
            """, (keep_id,))

            keep_meta = cur.fetchone()[0]

            # Merge metadata
            merged_meta = {**(keep_meta or {}), **(merge_meta or {})}

            cur.execute("""
                UPDATE memories
                SET metadata = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (json.dumps(merged_meta), keep_id))

            # Delete duplicate
            cur.execute("DELETE FROM memories WHERE id = %s", (merge_id,))

            conn.commit()
            cur.close()
            conn.close()

            logger.info(f"Merged memories {keep_id} and {merge_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to merge duplicates: {e}")
            return False

    def _cosine_similarity(self, vec1, vec2) -> float:
        """Compute cosine similarity"""
        if not vec1 or not vec2:
            return 0.0
        
        try:
            emb1 = json.loads(vec1) if isinstance(vec1, str) else vec1
            emb2 = json.loads(vec2) if isinstance(vec2, str) else vec2

            dot = sum(a*b for a,b in zip(emb1, emb2))
            mag1 = sum(a*a for a in emb1) ** 0.5
            mag2 = sum(b*b for b in emb2) ** 0.5

            return dot / (mag1 * mag2) if mag1 * mag2 > 0 else 0.0
        except Exception:
            return 0.0


# ============================================================================
# Feature 3: Memory Importance Scoring
# ============================================================================

class ImportanceScorer:
    """Auto-score memories by usage count + recency"""

    def __init__(self, db_config: Dict[str, Any]):
        self.db_config = db_config

    def compute_importance(self, memory_id: int) -> float:
        """
        Compute importance score (0-1)
        Based on: usage count, recency, embedding quality
        """
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            cur.execute("""
                SELECT access_count, created_at, updated_at, embedding
                FROM memories WHERE id = %s
            """, (memory_id,))

            row = cur.fetchone()
            cur.close()
            conn.close()

            if not row:
                return 0.0

            access_count, created, updated, embedding = row

            # Usage score (0-1): capped at 100 accesses
            usage_score = min(access_count / 100, 1.0)

            # Recency score (0-1): age affects importance
            age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - updated).days
            if age_days < 7:
                recency_score = 1.0
            elif age_days < 30:
                recency_score = 0.8
            elif age_days < 90:
                recency_score = 0.5
            else:
                recency_score = 0.2

            # Embedding quality score
            embedding_score = 1.0 if embedding else 0.5

            # Weighted combination
            importance = (
                usage_score * 0.4 +
                recency_score * 0.4 +
                embedding_score * 0.2
            )

            return min(importance, 1.0)

        except Exception as e:
            logger.error(f"Failed to compute importance: {e}")
            return 0.0

    def rank_memories(self, user_id: str, limit: int = 100) -> List[Dict]:
        """Get memories ranked by importance"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            cur.execute("""
                SELECT id, content, access_count, updated_at
                FROM memories
                WHERE user_id = %s AND archived = FALSE
                ORDER BY (
                    LEAST(access_count, 100) / 100.0 * 0.4 +
                    CASE 
                        WHEN AGE(updated_at) < '7 days'::interval THEN 0.4
                        WHEN AGE(updated_at) < '30 days'::interval THEN 0.32
                        WHEN AGE(updated_at) < '90 days'::interval THEN 0.2
                        ELSE 0.08
                    END * 0.4 +
                    0.1
                ) DESC
                LIMIT %s
            """, (user_id, limit))

            results = []
            for row_id, content, access_count, updated in cur.fetchall():
                importance = self.compute_importance(row_id)
                results.append({
                    'id': row_id,
                    'content': content,
                    'importance': importance,
                    'access_count': access_count,
                    'last_accessed': updated.isoformat()
                })

            cur.close()
            conn.close()
            return results

        except Exception as e:
            logger.error(f"Failed to rank memories: {e}")
            return []


# ============================================================================
# Feature 4: Incremental Backups
# ============================================================================

class IncrementalBackupManager:
    """Delta backups hourly/daily, faster restore"""

    def __init__(self, db_config: Dict[str, Any], backup_dir: str):
        self.db_config = db_config
        self.backup_dir = backup_dir
        os.makedirs(backup_dir, exist_ok=True)

    def create_full_backup(self) -> str:
        """Create full backup of all memories"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            cur.execute("""
                SELECT id, user_id, content, metadata, embedding, created_at
                FROM memories WHERE archived = FALSE
            """)

            memories = cur.fetchall()
            cur.close()
            conn.close()

            timestamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            backup_file = os.path.join(self.backup_dir, f"full_backup_{timestamp}.jsonl")

            with open(backup_file, 'w') as f:
                for mem_id, user_id, content, meta, emb, created in memories:
                    record = {
                        'id': mem_id,
                        'user_id': user_id,
                        'content': content,
                        'metadata': meta,
                        'embedding': emb,
                        'created_at': created.isoformat()
                    }
                    f.write(json.dumps(record) + '\n')

            logger.info(f"Full backup created: {backup_file}")
            return backup_file

        except Exception as e:
            logger.error(f"Failed to create full backup: {e}")
            return ""

    def create_delta_backup(self, since_timestamp: Optional[str] = None) -> str:
        """Create incremental backup of recent changes"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            if since_timestamp:
                cur.execute("""
                    SELECT id, user_id, content, metadata, embedding, updated_at
                    FROM memories
                    WHERE updated_at > %s
                """, (since_timestamp,))
            else:
                # Last 24h
                since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
                cur.execute("""
                    SELECT id, user_id, content, metadata, embedding, updated_at
                    FROM memories
                    WHERE updated_at > %s
                """, (since,))

            memories = cur.fetchall()
            cur.close()
            conn.close()

            timestamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            backup_file = os.path.join(self.backup_dir, f"delta_backup_{timestamp}.jsonl")

            with open(backup_file, 'w') as f:
                for mem_id, user_id, content, meta, emb, updated in memories:
                    record = {
                        'id': mem_id,
                        'user_id': user_id,
                        'content': content,
                        'metadata': meta,
                        'embedding': emb,
                        'updated_at': updated.isoformat()
                    }
                    f.write(json.dumps(record) + '\n')

            logger.info(f"Delta backup created: {backup_file} ({len(memories)} items)")
            return backup_file

        except Exception as e:
            logger.error(f"Failed to create delta backup: {e}")
            return ""

    def restore_from_backup(self, backup_file: str) -> int:
        """Restore memories from backup file"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            restored_count = 0
            with open(backup_file, 'r') as f:
                for line in f:
                    record = json.loads(line)
                    
                    cur.execute("""
                        INSERT INTO memories 
                        (user_id, content, metadata, embedding, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            content = excluded.content,
                            metadata = excluded.metadata
                    """, (
                        record['user_id'],
                        record['content'],
                        json.dumps(record['metadata']),
                        record['embedding'],
                        record['created_at']
                    ))
                    restored_count += 1

            conn.commit()
            cur.close()
            conn.close()

            logger.info(f"Restored {restored_count} memories from {backup_file}")
            return restored_count

        except Exception as e:
            logger.error(f"Failed to restore backup: {e}")
            return 0


# ============================================================================
# Feature 5: Knowledge Graph Integration
# ============================================================================

class KnowledgeGraphBuilder:
    """Entity links, topic graphs, related memory discovery"""

    def __init__(self, db_config: Dict[str, Any]):
        self.db_config = db_config
        self._init_schema()

    def _init_schema(self):
        """Initialize knowledge graph tables"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    entity_type VARCHAR(50),
                    description TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS memory_entities (
                    memory_id INT REFERENCES memories(id),
                    entity_id INT REFERENCES entities(id),
                    PRIMARY KEY (memory_id, entity_id)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS memory_relationships (
                    memory_id1 INT REFERENCES memories(id),
                    memory_id2 INT REFERENCES memories(id),
                    relationship_type VARCHAR(100),
                    strength REAL,
                    PRIMARY KEY (memory_id1, memory_id2)
                )
            """)

            conn.commit()
            cur.close()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to init knowledge graph schema: {e}")

    def extract_entities(self, content: str) -> List[str]:
        """Extract named entities from memory content"""
        # Simple heuristic entity extraction
        entities = []
        
        # Look for proper nouns (capitalized words)
        words = content.split()
        for word in words:
            if word and word[0].isupper() and len(word) > 2:
                entities.append(word.strip('.,!?'))
        
        return list(set(entities))

    def link_memory_entities(self, memory_id: int, content: str) -> int:
        """Extract and link entities for a memory"""
        try:
            entities = self.extract_entities(content)
            
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            linked_count = 0
            for entity_name in entities:
                # Insert or get entity
                cur.execute("""
                    INSERT INTO entities (name, entity_type)
                    VALUES (%s, %s)
                    ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                    RETURNING id
                """, (entity_name, 'GENERIC'))

                entity_id = cur.fetchone()[0]

                # Link to memory
                cur.execute("""
                    INSERT INTO memory_entities (memory_id, entity_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (memory_id, entity_id))

                linked_count += 1

            conn.commit()
            cur.close()
            conn.close()

            return linked_count

        except Exception as e:
            logger.error(f"Failed to link entities: {e}")
            return 0

    def find_related_memories(self, memory_id: int, limit: int = 10) -> List[Dict]:
        """Find memories related via entity links"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            # Find memories sharing entities
            cur.execute("""
                SELECT DISTINCT m2.id, m2.content, COUNT(*) as shared_entities
                FROM memories m1
                JOIN memory_entities me1 ON m1.id = me1.memory_id
                JOIN memory_entities me2 ON me1.entity_id = me2.entity_id
                JOIN memories m2 ON me2.memory_id = m2.id
                WHERE m1.id = %s AND m2.id != m1.id
                GROUP BY m2.id, m2.content
                ORDER BY shared_entities DESC
                LIMIT %s
            """, (memory_id, limit))

            results = []
            for mem_id, content, shared in cur.fetchall():
                results.append({
                    'id': mem_id,
                    'content': content,
                    'shared_entities': shared
                })

            cur.close()
            conn.close()
            return results

        except Exception as e:
            logger.error(f"Failed to find related memories: {e}")
            return []


# ============================================================================
# Feature 6: Privacy/Retention Policies
# ============================================================================

class DataClassification(Enum):
    """Data sensitivity levels"""
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"  # PII, secrets


class PrivacyPolicyManager:
    """GDPR deletion, data classification, redaction"""

    def __init__(self, db_config: Dict[str, Any]):
        self.db_config = db_config
        self.pii_patterns = [
            r'\b\d{3}-\d{2}-\d{4}\b',  # SSN
            r'\b\d{16}\b',  # Credit card
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',  # Email
        ]

    def classify_memory(self, content: str) -> DataClassification:
        """Classify memory based on content"""
        import re
        
        content_lower = content.lower()
        
        # Check for sensitive patterns
        for pattern in self.pii_patterns:
            if re.search(pattern, content):
                return DataClassification.RESTRICTED
        
        # Check for keywords
        restricted_keywords = ['password', 'secret', 'key', 'token', 'api_key']
        if any(kw in content_lower for kw in restricted_keywords):
            return DataClassification.RESTRICTED
        
        confidential_keywords = ['confidential', 'private', 'internal']
        if any(kw in content_lower for kw in confidential_keywords):
            return DataClassification.CONFIDENTIAL
        
        return DataClassification.PUBLIC

    def redact_pii(self, content: str) -> str:
        """Redact PII from content"""
        import re
        
        redacted = content
        
        # Redact emails
        redacted = re.sub(
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            '[EMAIL_REDACTED]',
            redacted
        )
        
        # Redact phone
        redacted = re.sub(
            r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b',
            '[PHONE_REDACTED]',
            redacted
        )
        
        # Redact SSN
        redacted = re.sub(
            r'\b\d{3}-\d{2}-\d{4}\b',
            '[SSN_REDACTED]',
            redacted
        )
        
        return redacted

    def delete_user_data(self, user_id: str) -> int:
        """Delete all data for user (GDPR right to be forgotten)"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            # Delete related data first
            cur.execute("""
                DELETE FROM memory_entities
                WHERE memory_id IN (SELECT id FROM memories WHERE user_id = %s)
            """, (user_id,))

            # Delete memories
            cur.execute("""
                DELETE FROM memories WHERE user_id = %s
            """, (user_id,))

            deleted = cur.rowcount
            conn.commit()
            cur.close()
            conn.close()

            logger.info(f"Deleted all data for user {user_id}: {deleted} memories")
            return deleted

        except Exception as e:
            logger.error(f"Failed to delete user data: {e}")
            return 0

    def apply_retention_policy(self, days: int = 365) -> int:
        """Delete memories older than retention period"""
        try:
            conn = psycopg2.connect(**self.db_config)
            cur = conn.cursor()

            cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()

            cur.execute("""
                DELETE FROM memories
                WHERE created_at < %s
                AND classified_as = %s
            """, (cutoff, DataClassification.PUBLIC.value))

            deleted = cur.rowcount
            conn.commit()
            cur.close()
            conn.close()

            logger.info(f"Applied retention policy: {deleted} old memories deleted")
            return deleted

        except Exception as e:
            logger.error(f"Failed to apply retention policy: {e}")
            return 0
