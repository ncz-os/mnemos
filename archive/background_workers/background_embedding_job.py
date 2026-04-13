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

import asyncio
import sys
import os
import time
import logging
import json
import httpx
import asyncpg

# Config — loaded from config.py (single source of truth)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from config import PG_CONFIG, OLLAMA_EMBED_URL  # noqa: E402

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [EMBED-BG] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

# DB connection kwargs — never build a DSN string with the password embedded
_DB_CONNECT_ARGS = {
    "user":     PG_CONFIG["user"],
    "password": PG_CONFIG["password"],
    "database": PG_CONFIG["database"],
    "host":     PG_CONFIG["host"],
    "port":     PG_CONFIG["port"],
}

# Phase 3: Inference backend for pre-compression
_INFERENCE_BASE = os.getenv('INFERENCE_BACKEND_URL', 'http://localhost:8000')
CERBERUS_COMPLETIONS_URL = _INFERENCE_BASE + '/v1/chat/completions'
CERBERUS_HEALTH_URL = _INFERENCE_BASE + '/v1/models'

# Phase 3: Compression configuration
COMPRESSION_THRESHOLD_BYTES = 10 * 1024    # 10KB - compress if content > 10KB
COMPRESSION_TARGET_RATIO = 0.40            # Compress to 40% of original
MAX_PROMPT_CHARS = 6000                    # Max chars to send to vLLM
MAX_GENERATION_TOKENS = 600               # Max tokens to generate
CERBERUS_TIMEOUT = 60                     # Timeout for CERBERUS requests
CERBERUS_RECHECK_INTERVAL = 300          # Re-check CERBERUS every 5 minutes


async def check_cerberus_health() -> bool:
    """Check if CERBERUS vLLM is available"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(CERBERUS_HEALTH_URL)
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"[PHASE3] CERBERUS health check failed: {e}")
        return False


async def compress_content_async(text: str, target_ratio: float = COMPRESSION_TARGET_RATIO) -> dict:
    """
    Phase 3: Asynchronously compress text using CERBERUS vLLM.

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
        async with httpx.AsyncClient(timeout=CERBERUS_TIMEOUT) as client:
            resp = await client.post(
                CERBERUS_COMPLETIONS_URL,
                json={
                    "model": "gemma4-e4b-fp8",
                    "messages": [{"role": "user", "content": f"Compress: {prompt}"}],
                    "temperature": 0.3,
                    "top_p": 0.9,
                    "max_tokens": max_tokens
                }
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

        compressed = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '').strip()

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
        self.db_pool = None
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
        # Phase 3: CERBERUS availability caching with timestamp
        self._cerberus_available: bool | None = None
        self._cerberus_last_check: float = 0

    async def _is_cerberus_available(self) -> bool:
        """Cache CERBERUS availability check; re-check every 5 minutes"""
        now = time.monotonic()
        if self._cerberus_available is None or (now - self._cerberus_last_check) >= CERBERUS_RECHECK_INTERVAL:
            self._cerberus_available = await check_cerberus_health()
            self._cerberus_last_check = now
            if self._cerberus_available:
                logger.info("[PHASE3] CERBERUS vLLM available for pre-compression")
            else:
                logger.warning("[PHASE3] CERBERUS unavailable - embedding pre-compression disabled")
        return self._cerberus_available

    async def get_embedding(self, text: str, retry: int = 0):
        """Generate embedding via Ollama with retries"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    OLLAMA_EMBED_URL,
                    json={"model": "nomic-embed-text", "prompt": text}
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

        except httpx.ConnectError as e:
            logger.warning(f"Ollama connection failed: {e}")
            self.stats['ollama_failures'] += 1

            # Retry with backoff
            if retry < self.max_retries:
                wait_time = 5 * (retry + 1)
                logger.info(f"Retrying in {wait_time}s (attempt {retry+1}/{self.max_retries})")
                await asyncio.sleep(wait_time)
                self.stats['retried'] += 1
                return await self.get_embedding(text, retry=retry + 1)
            return None

        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return None

    async def process_batch(self) -> bool:
        """Process one batch of memories with NULL embeddings"""
        try:
            async with self.db_pool.acquire() as conn:
                memories = await conn.fetch("""
                    SELECT id, content, LENGTH(content) as content_len
                    FROM memories
                    WHERE embedding IS NULL
                    ORDER BY created DESC
                    LIMIT $1
                """, self.batch_size)

                if not memories:
                    logger.debug("No memories with NULL embeddings")
                    return False

                logger.info(f"Processing batch of {len(memories)} memories")

                # Phase 3: Check CERBERUS availability once per batch
                cerberus_available = await self._is_cerberus_available()

                processed_count = 0
                failed_count = 0

                for row in memories:
                    memory_id = row['id']
                    content = row['content']
                    content_len = row['content_len']
                    try:
                        # Phase 3: Pre-compress if content > threshold and CERBERUS available
                        text_for_embedding = content
                        compressed_content = None
                        compression_applied = False

                        if cerberus_available and content_len > COMPRESSION_THRESHOLD_BYTES:
                            logger.info(
                                f"[PHASE3] Pre-compressing {memory_id[:8]} "
                                f"({content_len} bytes > {COMPRESSION_THRESHOLD_BYTES} threshold)"
                            )

                            compress_result = await compress_content_async(content, COMPRESSION_TARGET_RATIO)

                            if compress_result['success']:
                                compressed_content = compress_result['compressed']
                                text_for_embedding = compressed_content
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
                                logger.warning(
                                    f"[PHASE3] Compression failed for {memory_id[:8]}: "
                                    f"{compress_result['error']} - using original"
                                )
                                # Re-check CERBERUS availability on repeated failures
                                if self.stats['compression_failures'] % 5 == 0:
                                    self._cerberus_available = None
                                    self._cerberus_last_check = 0

                        # Get embedding (from compressed content if available, else original)
                        embedding = await self.get_embedding(text_for_embedding)

                        if embedding:
                            # Update memory with embedding and optionally compressed_content
                            if compression_applied and compressed_content:
                                await conn.execute("""
                                    UPDATE memories
                                    SET embedding = $1,
                                        compressed_content = $2,
                                        updated = DEFAULT,
                                        metadata = jsonb_set(
                                            COALESCE(metadata, '{}'),
                                            '{embedding_pre_compressed}',
                                            'true'
                                        )
                                    WHERE id = $3
                                """, embedding, compressed_content, memory_id)
                            else:
                                await conn.execute("""
                                    UPDATE memories
                                    SET embedding = $1, updated = DEFAULT
                                    WHERE id = $2
                                """, embedding, memory_id)

                            processed_count += 1
                            if compression_applied:
                                logger.info(
                                    f"[PHASE3] Updated {memory_id[:8]} with "
                                    f"{len(embedding)}-dim embedding (pre-compressed)"
                                )
                            else:
                                logger.debug(f"Updated {memory_id[:8]} with {len(embedding)}-dim embedding")
                        else:
                            failed_count += 1
                            logger.warning(f"Failed to get embedding for {memory_id}")

                    except Exception as e:
                        failed_count += 1
                        logger.error(f"Error processing {memory_id}: {e}")

            self.stats['processed'] += processed_count
            self.stats['failed'] += failed_count

            logger.info(
                f"Batch complete: {processed_count} processed, {failed_count} failed | "
                f"Phase3: {self.stats['compressed']} compressed, "
                f"{self.stats['bytes_saved']} bytes saved total"
            )
            return True

        except Exception as e:
            logger.error(f"Batch processing error: {e}")
            return False

    async def run_background(self):
        """Main background job loop"""
        logger.info("Background embedding job started (Phase 3: Pre-compression enabled)")

        while self.running:
            try:
                has_work = await self.process_batch()

                if has_work:
                    logger.info(f"Sleeping for {self.sleep_interval}s before next batch")
                else:
                    logger.info(f"No pending embeddings. Sleeping for {self.sleep_interval}s")

                await asyncio.sleep(self.sleep_interval)

            except Exception as e:
                logger.error(f"Unexpected error in background loop: {e}")
                await asyncio.sleep(self.sleep_interval)

        logger.info("Background embedding job stopped")
        logger.info(f"Final stats: {self.stats}")

    async def start(self):
        """Start the background job"""
        if self.running:
            logger.warning("Background job already running")
            return

        logger.info(f"Connecting to DB: {PG_CONFIG['host']}:{PG_CONFIG['port']}/{PG_CONFIG['database']}")
        self.db_pool = await asyncpg.create_pool(
            min_size=1, max_size=3, command_timeout=60, **_DB_CONNECT_ARGS
        )

        self.running = True
        await self.run_background()

    async def stop(self):
        """Stop the background job"""
        self.running = False
        if self.db_pool:
            await self.db_pool.close()
        logger.info("Background job stopped")


async def main():
    job = BackgroundEmbeddingJob(
        batch_size=20,
        sleep_interval=30,
        max_retries=3
    )
    try:
        await job.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await job.stop()
        logger.info(f"Final stats: {job.stats}")


if __name__ == '__main__':
    asyncio.run(main())
