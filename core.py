"""
MNEMOS Core Module
Shared utilities: database, embeddings, tier selection, memory store, sync, cleanup
"""

import json
import os
import sys
import time
import glob
import threading
import httpx
# DB drivers: asyncpg (api/ path) + psycopg v3 (this file + distillation_worker)
import psycopg
import uuid
from datetime import datetime, timezone
from psycopg.rows import dict_row
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor, as_completed
import config

# Request ID context for tracing
request_id_context: ContextVar[str] = ContextVar('request_id', default='')

# ============================================================================
# Database Manager
# ============================================================================

# Named constant: max chars sent to embedding model
EMBED_CONTENT_MAX_CHARS = 2000

class DatabaseManager:
    """PostgreSQL connection management and utilities"""

    def __init__(self):
        self.config = config.PG_CONFIG

    def get_connection(self):
        """Get a new database connection"""
        try:
            return psycopg.connect(**self.config, row_factory=dict_row)
        except Exception as e:
            print(f"[DB] Connection error: {e}", file=sys.stderr, flush=True)
            raise

    def execute_query(self, sql, params=None, fetch=None):
        """Execute query with error handling"""
        try:
            with psycopg.connect(**self.config, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params or ())
                    if fetch == 'one':
                        return cur.fetchone()
                    elif fetch == 'all':
                        return cur.fetchall()
                    else:
                        conn.commit()
                        return None
        except Exception as e:
            print(f"[DB] Query error: {e}", file=sys.stderr, flush=True)
            raise

    def init_schema(self):
        """Verify schema exists"""
        try:
            with psycopg.connect(**self.config) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM memories")
                    count = cur.fetchone()[0]
            print(f"[DB] Connected ({count} memories)", file=sys.stderr, flush=True)
            return True
        except Exception as e:
            print(f"[DB] Failed: {e}", file=sys.stderr, flush=True)
            return False

# ============================================================================
# Embedding Service (Parallel Batch Processing)
# ============================================================================

class EmbeddingService:
    """Ollama embedding integration with parallel batch processing"""

    def __init__(self):
        self.url = config.OLLAMA_EMBED_URL
        self.model = config.OLLAMA_EMBED_MODEL
        self.timeout = config.OLLAMA_EMBED_TIMEOUT

    def embed(self, text):
        """Get embedding for text"""
        if not text or not str(text).strip():
            return None

        try:
            response = httpx.post(
                self.url,
                json={'model': self.model, 'prompt': str(text)[:EMBED_CONTENT_MAX_CHARS]},
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return response.json().get('embedding')
        except Exception as e:
            print(f"[EMBED] Error: {e}", file=sys.stderr, flush=True)

        return None

    def embed_batch(self, texts, batch_size=None, max_workers=None):
        """Get embeddings for multiple texts using parallel processing

        Args:
            texts: List of text strings to embed
            batch_size: Size of sub-batches (default from config)
            max_workers: Number of parallel workers (default from config)

        Returns:
            List of embeddings (None for failed items)
        """
        batch_size = batch_size or getattr(config, 'EMBEDDING_BATCH_SIZE', 500)
        max_workers = max_workers or getattr(config, 'MAX_EMBEDDING_WORKERS', 4)

        total = len(texts)
        if total == 0:
            return []

        start_time = time.time()
        results = [None] * total  # Pre-allocate results list
        completed_count = [0]  # Use list for mutable counter in closure

        # Create batches with original indices
        batches = []
        for i in range(0, total, batch_size):
            batch_items = [(idx, texts[idx]) for idx in range(i, min(i + batch_size, total))]
            batches.append(batch_items)

        def process_batch(batch_items):
            """Process a single batch of items"""
            batch_results = []
            for idx, text in batch_items:
                embedding = self.embed(text)
                batch_results.append((idx, embedding))
            return batch_results

        print(f"[EMBED] Starting parallel embedding: {total} items, "
              f"{len(batches)} batches, {max_workers} workers",
              file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all batches
            futures = {
                executor.submit(process_batch, batch): i
                for i, batch in enumerate(batches)
            }

            # Process results as they complete
            for future in as_completed(futures):
                try:
                    batch_results = future.result()
                    for idx, embedding in batch_results:
                        results[idx] = embedding
                        completed_count[0] += 1

                    # Progress logging every 100 items or on completion
                    if completed_count[0] % 100 == 0 or completed_count[0] == total:
                        elapsed = time.time() - start_time
                        rate = completed_count[0] / elapsed if elapsed > 0 else 0
                        remaining = (total - completed_count[0]) / rate if rate > 0 else 0

                        print(f"[EMBED] Progress: {completed_count[0]}/{total} "
                              f"({100*completed_count[0]/total:.1f}%) "
                              f"Rate: {rate:.1f}/s ETA: {remaining:.0f}s",
                              file=sys.stderr, flush=True)

                except Exception as e:
                    print(f"[EMBED] Batch error: {e}", file=sys.stderr, flush=True)

        elapsed = time.time() - start_time
        success_count = sum(1 for r in results if r is not None)

        print(f"[EMBED] Completed: {success_count}/{total} embeddings "
              f"in {elapsed:.1f}s ({success_count/elapsed:.1f}/s)",
              file=sys.stderr, flush=True)

        return results

    def embed_batch_with_ids(self, items, batch_size=None, max_workers=None):
        """Embed items with IDs for database updates

        Args:
            items: List of (id, content) tuples
            batch_size: Size of sub-batches
            max_workers: Number of parallel workers

        Returns:
            List of (id, embedding) tuples for successful embeddings
        """
        batch_size = batch_size or getattr(config, 'EMBEDDING_BATCH_SIZE', 500)
        max_workers = max_workers or getattr(config, 'MAX_EMBEDDING_WORKERS', 4)

        total = len(items)
        if total == 0:
            return []

        start_time = time.time()
        results = []
        results_lock = threading.Lock()
        completed_count = [0]

        # Create batches
        batches = [items[i:i+batch_size] for i in range(0, total, batch_size)]

        def process_batch(batch_items):
            """Process a batch and return (id, embedding) tuples"""
            batch_results = []
            for mem_id, content in batch_items:
                if content and str(content).strip():
                    embedding = self.embed(str(content)[:EMBED_CONTENT_MAX_CHARS])
                    if embedding:
                        batch_results.append((mem_id, embedding))
            return batch_results

        print(f"[EMBED] Starting parallel embedding: {total} items, "
              f"{len(batches)} batches, {max_workers} workers",
              file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_batch, batch): i
                for i, batch in enumerate(batches)
            }

            for future in as_completed(futures):
                try:
                    batch_results = future.result()
                    with results_lock:
                        results.extend(batch_results)
                        completed_count[0] += len(batch_results)

                    # Progress logging
                    elapsed = time.time() - start_time
                    rate = completed_count[0] / elapsed if elapsed > 0 else 0

                    if completed_count[0] % 100 == 0 or completed_count[0] >= total:
                        print(f"[EMBED] Progress: {completed_count[0]}/{total} "
                              f"({100*completed_count[0]/total:.1f}%) "
                              f"Rate: {rate:.1f}/s",
                              file=sys.stderr, flush=True)

                except Exception as e:
                    print(f"[EMBED] Batch error: {e}", file=sys.stderr, flush=True)

        elapsed = time.time() - start_time
        print(f"[EMBED] Completed: {len(results)}/{total} embeddings "
              f"in {elapsed:.1f}s ({len(results)/elapsed:.1f}/s)",
              file=sys.stderr, flush=True)

        return results

# ============================================================================
# Tier Selector
# ============================================================================

class TierSelector:
    """Task-based tier selection for context retrieval"""

    def detect_task_type(self, query_text):
        """Detect task type from query"""
        if not query_text:
            return 'general'

        query_lower = query_text.lower()

        # Check keywords
        for task_type, keywords in config.TASK_TYPE_KEYWORDS.items():
            if task_type == 'complex':
                continue
            if any(word in query_lower for word in keywords):
                return task_type

        # Long queries are complex
        if len(query_text.split()) > 20:
            return 'complex'

        return 'general'

    def select_tiers(self, task_type):
        """Get tier categories for task type"""
        return config.TIER_SELECTION.get(task_type, config.TIER_SELECTION['general'])

# ============================================================================
# Memory Store
# ============================================================================

class MemoryStore:
    """Memory CRUD operations and search"""

    def __init__(self):
        self.db = DatabaseManager()
        self.embedding = EmbeddingService()
        self.tier_selector = TierSelector()
        self.last_cleanup = time.time()

    def create_memory(self, content, category='facts', memory_id=None):
        """Create new memory"""
        if not content or not str(content).strip():
            raise ValueError("Memory content cannot be empty")

        # Generate ID if not provided
        if not memory_id:
            memory_id = f"mem_{int(time.time() * 1000)}"

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        clean_content = str(content).replace('\x00', '')

        # Generate embedding
        embedding = self.embedding.embed(clean_content)

        # Write to PostgreSQL
        conn = self.db.get_connection()
        cur = conn.cursor()

        try:
            cur.execute("""
                INSERT INTO memories (id, content, category, embedding, created, updated)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    content = EXCLUDED.content,
                    category = EXCLUDED.category,
                    embedding = EXCLUDED.embedding,
                    updated = EXCLUDED.updated
            """, (memory_id, clean_content, category, embedding, now, now))

            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

        return {
            'id': memory_id,
            'content': clean_content,
            'category': category,
            'created': now,
            'updated': now
        }

    def get_memory(self, memory_id):
        """Get single memory by ID"""
        conn = self.db.get_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT id, content, category, created, updated
                FROM memories WHERE id = %s
            """, (memory_id,))
            result = cur.fetchone()
        finally:
            cur.close()
            conn.close()

        return dict(result) if result else None

    def update_memory(self, memory_id, content=None, category=None):
        """Update existing memory"""
        existing = self.get_memory(memory_id)
        if not existing:
            raise ValueError(f"Memory {memory_id} not found")

        new_content = content or existing['content']
        new_category = category or existing['category']
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        # Regenerate embedding if content changed
        embedding = None
        if content and content != existing['content']:
            embedding = self.embedding.embed(new_content)

        conn = self.db.get_connection()
        cur = conn.cursor()

        if embedding:
            cur.execute("""
                UPDATE memories
                SET content = %s, category = %s, embedding = %s, updated = %s
                WHERE id = %s
            """, (new_content, new_category, embedding, now, memory_id))
        else:
            cur.execute("""
                UPDATE memories
                SET content = %s, category = %s, updated = %s
                WHERE id = %s
            """, (new_content, new_category, now, memory_id))

        conn.commit()
        cur.close()
        conn.close()

        return self.get_memory(memory_id)

    def delete_memory(self, memory_id):
        """Delete memory by ID"""
        conn = self.db.get_connection()
        cur = conn.cursor()

        cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
        conn.commit()
        cur.close()
        conn.close()

        return memory_id

    def list_memories(self, limit=100, offset=0):
        """List all memories with pagination"""
        conn = self.db.get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT id, content, category, created, updated
            FROM memories
            ORDER BY created DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))

        results = cur.fetchall()
        cur.close()
        conn.close()

        return [dict(r) for r in results]

    def search(self, query_text, limit=10, category=None):
        """Vector search with tier selection"""
        # Get embedding for query
        query_embedding = self.embedding.embed(query_text)
        if not query_embedding:
            # Fallback to keyword search
            return self._keyword_search(query_text, limit, category)

        # Detect task type and select tiers
        task_type = self.tier_selector.detect_task_type(query_text)
        selected_tiers = self.tier_selector.select_tiers(task_type)

        conn = self.db.get_connection()
        cur = conn.cursor()
        try:
            # Set optimal IVFFlat probes for lists=79 index (30 = 100% recall, 356 queries/sec)
            cur.execute("SET ivfflat.probes = 30")
            # Search across selected tiers
            # Use ANY(%s::text[]) for parameterized tier filtering (no SQL injection risk)
            cur.execute("""
                SELECT
                    id,
                    content,
                    category,
                    created,
                    1 - (embedding <=> %s::vector) as similarity
                FROM memories
                WHERE embedding IS NOT NULL AND (category = ANY(%s::text[]) OR category = 'documentation')
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (query_embedding, list(selected_tiers), query_embedding, limit))
            results = [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

        return {
            'results': results,
            'task_type': task_type,
            'selected_tiers': selected_tiers,
            'query': query_text,
            'method': 'vector'
        }

    def _keyword_search(self, query_text, limit=10, category=None):
        """Fallback keyword search"""
        conn = self.db.get_connection()
        cur = conn.cursor()

        pattern = f'%{query_text}%'

        if category:
            cur.execute("""
                SELECT id, content, category, created
                FROM memories
                WHERE (content ILIKE %s OR category ILIKE %s) AND category = %s
                ORDER BY created DESC
                LIMIT %s
            """, (pattern, pattern, category, limit))
        else:
            cur.execute("""
                SELECT id, content, category, created
                FROM memories
                WHERE content ILIKE %s OR category ILIKE %s
                ORDER BY created DESC
                LIMIT %s
            """, (pattern, pattern, limit))

        results = cur.fetchall()
        cur.close()
        conn.close()

        return {
            'results': [dict(r) for r in results],
            'query': query_text,
            'method': 'keyword'
        }

# ============================================================================
# Sync Service
# ============================================================================

class SyncService:
    """JSON shard synchronization"""

    def __init__(self):
        self.shard_dir = config.SHARD_DIR
        self.db = DatabaseManager()

    def sync_json_shards(self):
        """Sync JSON shards to PostgreSQL (single connection per run, no N+1)"""
        try:
            os.makedirs(self.shard_dir, exist_ok=True)
            shard_files = sorted(glob.glob(os.path.join(self.shard_dir, 'shard_*.json')))
            synced = 0

            conn = self.db.get_connection()
            cur = conn.cursor()
            try:
                for shard_file in shard_files:
                    try:
                        with open(shard_file, 'r') as f:
                            data = json.load(f)
                            if not isinstance(data, list):
                                data = [data]

                            for mem in data:
                                mem_id = mem.get('id')
                                if not mem_id:
                                    continue

                                cur.execute('SELECT id FROM memories WHERE id = %s', (mem_id,))
                                if not cur.fetchone():
                                    cur.execute("""
                                        INSERT INTO memories (id, content, category, created, updated)
                                        VALUES (%s, %s, %s, %s, %s)
                                    """, (
                                        mem_id,
                                        mem.get('content', ''),
                                        mem.get('category', 'facts'),
                                        mem.get('created', ''),
                                        mem.get('updated', mem.get('created', ''))
                                    ))
                                    synced += 1

                    except Exception as e:
                        print(f"[SYNC] Error processing {shard_file}: {e}", file=sys.stderr, flush=True)
                        continue

                conn.commit()
            finally:
                cur.close()
                conn.close()

            print(f"[SYNC] Synced {synced} from JSON shards", file=sys.stderr, flush=True)
            return synced
        except Exception as e:
            print(f"[SYNC] Failed: {e}", file=sys.stderr, flush=True)
            return 0

# ============================================================================
# Embedding Generator (Parallel Processing)
# ============================================================================

class EmbeddingGenerator:
    """Background embedding generation with parallel processing"""

    def __init__(self):
        self.db = DatabaseManager()
        self.embedding = EmbeddingService()
        self.running = False
        self._reindex_in_progress = False

    def generate_missing(self, limit=None):
        """Generate embeddings for memories without them using parallel processing"""
        try:
            batch_size = limit or config.EMBEDDING_BATCH_SIZE

            conn = self.db.get_connection()
            cur = conn.cursor()

            cur.execute("""
                SELECT id, content FROM memories
                WHERE embedding IS NULL
                LIMIT %s
            """, (batch_size,))

            missing = cur.fetchall()
            cur.close()
            conn.close()

            if not missing:
                return 0

            # Use parallel batch processing
            results = self.embedding.embed_batch_with_ids(missing)

            if not results:
                return 0

            # Bulk update embeddings in database
            generated = 0
            conn = self.db.get_connection()
            cur = conn.cursor()

            for mem_id, embedding in results:
                try:
                    cur.execute("""
                        UPDATE memories
                        SET embedding = %s, updated = NOW()
                        WHERE id = %s
                    """, (embedding, mem_id))
                    generated += 1
                except Exception as e:
                    print(f"[EMBED] Update error for {mem_id}: {e}", file=sys.stderr, flush=True)

            conn.commit()
            cur.close()
            conn.close()

            if generated > 0:
                print(f"[EMBED] Generated {generated} embeddings (parallel)", file=sys.stderr, flush=True)

            return generated
        except Exception as e:
            print(f"[EMBED] Error: {e}", file=sys.stderr, flush=True)
            return 0

    def reindex_all(self, force=False):
        """Reindex all memories with fresh embeddings

        Args:
            force: If True, regenerate even if embedding exists

        Returns:
            dict with reindex statistics
        """
        if self._reindex_in_progress:
            return {'error': 'Reindex already in progress', 'status': 'busy'}

        self._reindex_in_progress = True
        start_time = time.time()

        try:
            conn = self.db.get_connection()
            cur = conn.cursor()

            if force:
                # Get all memories
                cur.execute("SELECT id, content FROM memories")
            else:
                # Get only memories without embeddings
                cur.execute("SELECT id, content FROM memories WHERE embedding IS NULL")

            all_memories = cur.fetchall()
            cur.close()
            conn.close()

            total = len(all_memories)
            if total == 0:
                self._reindex_in_progress = False
                return {'message': 'No memories to reindex', 'count': 0, 'status': 'complete'}

            print(f"[REINDEX] Starting reindex of {total} memories...", file=sys.stderr, flush=True)

            # Process in parallel batches
            results = self.embedding.embed_batch_with_ids(all_memories)

            # Bulk update
            generated = 0
            conn = self.db.get_connection()
            cur = conn.cursor()

            for mem_id, embedding in results:
                try:
                    cur.execute("""
                        UPDATE memories
                        SET embedding = %s, updated = NOW()
                        WHERE id = %s
                    """, (embedding, mem_id))
                    generated += 1
                except Exception as e:
                    print(f"[REINDEX] Update error for {mem_id}: {e}", file=sys.stderr, flush=True)

            conn.commit()
            cur.close()
            conn.close()

            elapsed = time.time() - start_time
            rate = generated / elapsed if elapsed > 0 else 0

            print(f"[REINDEX] Complete: {generated}/{total} embeddings in {elapsed:.1f}s ({rate:.1f}/s)",
                  file=sys.stderr, flush=True)

            self._reindex_in_progress = False

            return {
                'status': 'complete',
                'total': total,
                'generated': generated,
                'failed': total - generated,
                'elapsed_seconds': round(elapsed, 2),
                'rate_per_second': round(rate, 2)
            }

        except Exception as e:
            self._reindex_in_progress = False
            print(f"[REINDEX] Error: {e}", file=sys.stderr, flush=True)
            return {'error': str(e), 'status': 'failed'}

    def start(self):
        """Start background embedding thread"""
        def loop():
            while self.running:
                try:
                    self.generate_missing()
                    time.sleep(config.EMBEDDING_CHECK_INTERVAL)
                except Exception as e:
                    print(f"[EMBED] Loop error: {e}", file=sys.stderr, flush=True)
                    time.sleep(60)

        self.running = True
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        print(f"[EMBED] Background generator started (parallel mode)", file=sys.stderr, flush=True)

    def stop(self):
        """Stop background embedding thread"""
        self.running = False

# ============================================================================
# Memory Maintenance
# ============================================================================

class MemoryMaintenance:
    """Memory cleanup and optimization"""

    def __init__(self):
        self.db = DatabaseManager()

    def cleanup_expired(self):
        """Remove memories older than TTL"""
        try:
            ttl_seconds = config.MEMORY_TTL.total_seconds()
            cutoff_time = datetime.now(timezone.utc).replace(tzinfo=None) - config.MEMORY_TTL

            conn = self.db.get_connection()
            cur = conn.cursor()

            cur.execute("""
                DELETE FROM memories
                WHERE created < %s
            """, (cutoff_time.isoformat(),))

            deleted = cur.rowcount
            conn.commit()
            cur.close()
            conn.close()

            if deleted > 0:
                print(f"[CLEANUP] Deleted {deleted} expired memories", file=sys.stderr, flush=True)

            return deleted
        except Exception as e:
            print(f"[CLEANUP] Error: {e}", file=sys.stderr, flush=True)
            return 0

    def run(self):
        """Run all maintenance tasks"""
        deleted = self.cleanup_expired()
        return deleted

# ============================================================================
# Request Tracer
# ============================================================================

class RequestTracer:
    """Request ID tracking and timing"""

    @staticmethod
    def new_id():
        """Generate new request ID"""
        rid = str(uuid.uuid4())[:8]
        request_id_context.set(rid)
        return rid

    @staticmethod
    def get_id():
        """Get current request ID"""
        return request_id_context.get()

    @staticmethod
    def log(message, level='INFO'):
        """Log message with request ID"""
        rid = RequestTracer.get_id()
        timestamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime('%H:%M:%S')
        print(f"[{timestamp}] [{rid}] {message}", file=sys.stderr, flush=True)

# ============================================================================
# Initialization
# ============================================================================

def init_mnemos():
    """Initialize MNEMOS on startup"""
    print(f"[MNEMOS] Starting {config.API_VERSION}...", file=sys.stderr, flush=True)

    # Verify database
    db = DatabaseManager()
    if not db.init_schema():
        print(f"[MNEMOS] Database initialization failed", file=sys.stderr, flush=True)
        return False

    print(f"[MNEMOS] Core initialized", file=sys.stderr, flush=True)
    return True
