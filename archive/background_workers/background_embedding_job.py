# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
Background Embedding Job for MNEMOS - Phase 3: Pre-Compression
Processes memories with NULL embeddings asynchronously.

Phase 3 Enhancement:
  - Pre-compresses content > 10KB before sending to embeddings API
  - Target: 40% compression ratio
  - Stores both original content and compressed_content in DB
  - Logs compression metrics
  - Falls back to original content if compression fails
"""

import psycopg2
import requests
import sys
import time
import threading
import logging
import json
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [EMBED-BG] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

# PostgreSQL configuration
PG_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'database': 'mnemos',
    'user': 'mnemos_user',
    'password': 'mnemos_secure_pass'
}

# Ollama configuration (for embeddings on PYTHIA local Ollama)
OLLAMA_EMBED_URL = 'http://localhost:11434/api/embeddings'

# Phase 3: CERBERUS llama-server for pre-compression
CERBERUS_COMPLETIONS_URL = 'http://192.0.2.96:8000/v1/completions'
CERBERUS_HEALTH_URL = 'http://192.0.2.96:8000/health'

# Phase 3: Compression configuration
COMPRESSION_THRESHOLD_BYTES = 10 * 1024    # 10KB - compress if content > 10KB
COMPRESSION_TARGET_RATIO = 0.40            # Compress to 40% of original
MAX_PROMPT_CHARS = 6000                    # Max chars to send to llama-server
MAX_GENERATION_TOKENS = 600               # Max tokens to generate
CERBERUS_TIMEOUT = 60                     # Timeout for CERBERUS requests


def check_cerberus_health() -> bool:
    """Check if CERBERUS llama-server is available"""
    try:
        resp = requests.get(CERBERUS_HEALTH_URL, timeout=5)
        return resp.status_code == 200 and resp.json().get('status') == 'ok'
    except Exception as e:
        logger.debug(f"[PHASE3] CERBERUS health check failed: {e}")
        return False


def compress_content_sync(text: str, target_ratio: float = COMPRESSION_TARGET_RATIO) -> dict:
    """
    Phase 3: Synchronously compress text using CERBERUS llama-server.

    Returns dict with:
      - compressed: compressed text (or original on failure)
      - ratio: compression ratio achieved
      - success: bool
      - error: error message if failed
    """
    original_len = len(text)

    # Truncate to fit within context window safely
    truncated = text[:MAX_PROMPT_CHARS]

    # Calculate safe max_tokens
    target_output = int((len(truncated) / 4) * target_ratio)
    max_tokens = max(10, min(target_output, MAX_GENERATION_TOKENS))

    prompt = f"""Summarize this text to approximately {int(target_ratio * 100)}% of original length.
Preserve all critical facts, decisions, and technical details. Remove redundancy.

TEXT:
{truncated}

SUMMARY:"""

    try:
        resp = requests.post(
            CERBERUS_COMPLETIONS_URL,
            json={
                "prompt": prompt,
                "temperature": 0.3,
                "top_p": 0.9,
                "max_tokens": max_tokens
            },
            timeout=CERBERUS_TIMEOUT
        )

        if resp.status_code != 200:
            return {
                'compressed': text,
                'original_length': original_len,
                'compressed_length': original_len,
                'ratio': 1.0,
                'success': False,
                'error': f'HTTP {resp.status_code}'
            }

        compressed = resp.json().get('choices', [{}])[0].get('text', '').strip()

        if not compressed or len(compressed) < 10:
            return {
                'compressed': text,
                'original_length': original_len,
                'compressed_length': original_len,
                'ratio': 1.0,
                'success': False,
                'error': 'Empty response from CERBERUS'
            }

        compressed_len = len(compressed)
        actual_ratio = compressed_len / max(original_len, 1)

        return {
            'compressed': compressed,
            'original_length': original_len,
            'compressed_length': compressed_len,
            'ratio': actual_ratio,
            'success': True,
            'error': None
        }

    except Exception as e:
        return {
            'compressed': text,
            'original_length': original_len,
            'compressed_length': original_len,
            'ratio': 1.0,
            'success': False,
            'error': str(e)
        }


class BackgroundEmbeddingJob:
    """Background job to process NULL embeddings with Phase 3 pre-compression"""

    def __init__(self, batch_size=20, sleep_interval=30, max_retries=3):
        """
        Initialize background job

        Args:
            batch_size: Number of memories to process per batch
            sleep_interval: Seconds to sleep between batches
            max_retries: Maximum retries per embedding failure
        """
        self.batch_size = batch_size
        self.sleep_interval = sleep_interval
        self.max_retries = max_retries
        self.running = False
        self.thread = None
        self.stats = {
            'processed': 0,
            'failed': 0,
            'retried': 0,
            'ollama_failures': 0,
            # Phase 3 stats
            'compressed': 0,
            'compression_failures': 0,
            'bytes_saved': 0,
        }
        # Phase 3: Check CERBERUS availability once at startup
        self._cerberus_available = None

    def _is_cerberus_available(self) -> bool:
        """Cache CERBERUS availability check (re-check periodically)"""
        if self._cerberus_available is None:
            self._cerberus_available = check_cerberus_health()
            if self._cerberus_available:
                logger.info("[PHASE3] CERBERUS llama-server available for pre-compression")
            else:
                logger.warning("[PHASE3] CERBERUS unavailable - embedding pre-compression disabled")
        return self._cerberus_available

    def get_embedding(self, text, retry=0):
        """Generate embedding via Ollama with retries"""
        try:
            response = requests.post(
                OLLAMA_EMBED_URL,
                json={"model": "nomic-embed-text", "prompt": text},
                timeout=10
            )

            if response.status_code == 200:
                embedding = response.json().get('embedding', [])
                if embedding:
                    return embedding
                else:
                    logger.warning("Empty embedding from Ollama")
                    return None
            else:
                logger.warning(f"Ollama returned {response.status_code}")
                return None

        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Ollama connection failed: {e}")
            self.stats['ollama_failures'] += 1

            # Retry with backoff
            if retry < self.max_retries:
                wait_time = 5 * (retry + 1)
                logger.info(f"Retrying in {wait_time}s (attempt {retry+1}/{self.max_retries})")
                time.sleep(wait_time)
                self.stats['retried'] += 1
                return self.get_embedding(text, retry=retry+1)
            return None

        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return None

    def process_batch(self):
        """Process one batch of memories with NULL embeddings"""
        try:
            conn = psycopg2.connect(**PG_CONFIG)
            cur = conn.cursor()

            # Get memories without embeddings
            cur.execute("""
                SELECT id, content, LENGTH(content) as content_len
                FROM memories
                WHERE embedding IS NULL
                ORDER BY created DESC
                LIMIT %s
            """, (self.batch_size,))

            memories = cur.fetchall()

            if not memories:
                logger.debug("No memories with NULL embeddings")
                cur.close()
                conn.close()
                return False

            logger.info(f"Processing batch of {len(memories)} memories")

            # Phase 3: Check CERBERUS availability once per batch
            cerberus_available = self._is_cerberus_available()

            processed_count = 0
            failed_count = 0

            for memory_id, content, content_len in memories:
                try:
                    # Phase 3: Pre-compress if content > threshold and CERBERUS available
                    text_for_embedding = content
                    compressed_content = None
                    compression_applied = False

                    if cerberus_available and content_len > COMPRESSION_THRESHOLD_BYTES:
                        logger.info(f"[PHASE3] Pre-compressing {memory_id[:8]} ({content_len} bytes > {COMPRESSION_THRESHOLD_BYTES} threshold)")

                        compress_result = compress_content_sync(content, COMPRESSION_TARGET_RATIO)

                        if compress_result['success']:
                            compressed_content = compress_result['compressed']
                            text_for_embedding = compressed_content  # Embed the compressed version
                            compression_applied = True
                            bytes_saved = compress_result['original_length'] - compress_result['compressed_length']
                            self.stats['compressed'] += 1
                            self.stats['bytes_saved'] += bytes_saved
                            logger.info(
                                f"[PHASE3] Compressed {memory_id[:8]}: "
                                f"{compress_result['original_length']} -> {compress_result['compressed_length']} chars "
                                f"(ratio={compress_result['ratio']:.2%}, saved {bytes_saved} bytes)"
                            )
                        else:
                            self.stats['compression_failures'] += 1
                            logger.warning(f"[PHASE3] Compression failed for {memory_id[:8]}: {compress_result['error']} - using original")
                            # Re-check CERBERUS availability on repeated failures
                            if self.stats['compression_failures'] % 5 == 0:
                                self._cerberus_available = None

                    # Get embedding (from compressed content if available, else original)
                    embedding = self.get_embedding(text_for_embedding)

                    if embedding:
                        # Update memory with embedding and optionally compressed_content
                        if compression_applied and compressed_content:
                            cur.execute("""
                                UPDATE memories
                                SET embedding = %s,
                                    compressed_content = %s,
                                    updated = %s,
                                    metadata = jsonb_set(
                                        COALESCE(metadata, '{}'),
                                        '{embedding_pre_compressed}',
                                        'true'
                                    )
                                WHERE id = %s
                            """, (
                                str(embedding),  # psycopg2 needs list as string for vector type
                                compressed_content,
                                datetime.now().isoformat(),
                                memory_id
                            ))
                        else:
                            cur.execute("""
                                UPDATE memories
                                SET embedding = %s, updated = %s
                                WHERE id = %s
                            """, (str(embedding), datetime.now().isoformat(), memory_id))

                        conn.commit()
                        processed_count += 1
                        if compression_applied:
                            logger.info(f"[PHASE3] Updated {memory_id[:8]} with {len(embedding)}-dim embedding (pre-compressed)")
                        else:
                            logger.debug(f"Updated {memory_id[:8]} with {len(embedding)}-dim embedding")
                    else:
                        failed_count += 1
                        logger.warning(f"Failed to get embedding for {memory_id}")

                except Exception as e:
                    failed_count += 1
                    logger.error(f"Error processing {memory_id}: {e}")
                    try:
                        conn.rollback()
                    except Exception as rb_err:
                        logger.warning(f"Rollback failed: {rb_err}")

            cur.close()
            conn.close()

            self.stats['processed'] += processed_count
            self.stats['failed'] += failed_count

            logger.info(
                f"Batch complete: {processed_count} processed, {failed_count} failed | "
                f"Phase3: {self.stats['compressed']} compressed, "
                f"{self.stats['bytes_saved']} bytes saved total"
            )
            return len(memories) > 0

        except Exception as e:
            logger.error(f"Batch processing error: {e}")
            return False

    def run_background(self):
        """Main background job loop"""
        logger.info("Background embedding job started (Phase 3: Pre-compression enabled)")

        while self.running:
            try:
                has_work = self.process_batch()

                if has_work:
                    logger.info(f"Sleeping for {self.sleep_interval}s before next batch")
                else:
                    logger.info(f"No pending embeddings. Sleeping for {self.sleep_interval}s")
                    # Re-check CERBERUS periodically when idle
                    self._cerberus_available = None

                time.sleep(self.sleep_interval)

            except Exception as e:
                logger.error(f"Unexpected error in background loop: {e}")
                time.sleep(self.sleep_interval)

        logger.info("Background embedding job stopped")
        logger.info(f"Final stats: {self.stats}")

    def start(self):
        """Start the background job"""
        if self.running:
            logger.warning("Background job already running")
            return

        self.running = True
        self.thread = threading.Thread(target=self.run_background, daemon=True)
        self.thread.start()
        logger.info("Background job thread started")

    def stop(self):
        """Stop the background job"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=10)
        logger.info("Background job stopped")


# Global job instance
background_job = BackgroundEmbeddingJob(
    batch_size=20,       # Process 20 memories at a time
    sleep_interval=30,   # Check every 30 seconds
    max_retries=3        # Retry failed embeddings up to 3 times
)

if __name__ == '__main__':
    # Manual testing
    job = BackgroundEmbeddingJob()
    job.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        job.stop()
        logger.info(f"Final stats: {job.stats}")
