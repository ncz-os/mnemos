#!/usr/bin/env python3
"""
Background distillation worker: compresses memories using LETHE (token + sentence modes) or LLM fallback,
updates embeddings, and maintains compression quality metrics.
"""

import asyncio
import logging
import os
import sys
import httpx
import asyncpg

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Config
# Config — loaded from config.py (single source of truth)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PG_CONFIG as _PG_CONFIG  # noqa: E402
from inference_backend import get_backend  # noqa: E402
try:
    from compression.distillation_engine import DistillationEngine, CompressionStrategy
    from compression.manager import CompressionManager
    _COMPRESSION_AVAILABLE = True
except Exception as _ce:
    logger.warning(f"Local compression unavailable: {_ce}")
    _COMPRESSION_AVAILABLE = False

# Tuning
SIZE_LIMIT_KB = 5
BATCH_SIZE = 5
CHECK_INTERVAL = 30
DISTILL_TIMEOUT_BASE = 45
DISTILL_TIMEOUT_PER_KB = 3
QUALITY_TIMEOUT = 20
MAX_ATTEMPTS = 3

# DB connection kwargs — never build a DSN string with the password embedded
_DB_CONNECT_ARGS = {
    "user":     _PG_CONFIG["user"],
    "password": _PG_CONFIG["password"],
    "database": _PG_CONFIG["database"],
    "host":     _PG_CONFIG["host"],
    "port":     _PG_CONFIG["port"],
}


async def _distill_backend_call(backend, text: str) -> str:
    """Build distillation prompt and call backend.complete()."""
    prompt = (
        "Summarize this text to approximately 40% of original length. "
        "Preserve all critical facts, decisions, and technical details. "
        "Remove redundancy.\n\nTEXT:\n" + text + "\n\nSUMMARY:"
    )
    return await backend.complete(prompt)


class MemoryDistillationWorker:
    def __init__(self):
        self.db_pool = None   # asyncpg Pool — set in start()
        self.llm = get_backend()
        # Local compression engine (LETHE: token + sentence modes, no external calls)
        self._compression_engine = DistillationEngine() if _COMPRESSION_AVAILABLE else None
        self.stats = {
            "processed": 0,
            "successful": 0,
            "timeouts": 0,
            "errors": 0,
            "total_bytes_saved": 0
        }

    async def start(self):
        """Start background worker"""
        logger.info(f"Connecting to DB: {_PG_CONFIG['host']}:{_PG_CONFIG['port']}/{_PG_CONFIG['database']}")
        self.db_pool = await asyncpg.create_pool(
            min_size=1, max_size=3, command_timeout=60, **_DB_CONNECT_ARGS
        )
        
        logger.info("[OK] Distillation worker started")
        logger.info(f"Backend: {self.llm.__class__.__name__}")
        logger.info(f"Config: size_limit={SIZE_LIMIT_KB}KB, batch={BATCH_SIZE}, "
                   f"distill_timeout={DISTILL_TIMEOUT_BASE}s+({DISTILL_TIMEOUT_PER_KB}s/KB)")

        while True:
            try:
                await self.process_batch()
                await self.log_stats()
            except Exception as e:
                logger.error(f"Worker error: {e}", exc_info=True)
                try:
                    await self.db_pool.close()
                    self.db_pool = await asyncpg.create_pool(
            min_size=1, max_size=3, command_timeout=60, **_DB_CONNECT_ARGS
        )
                except Exception as re:
                    logger.error(f"DB reconnect failed: {re}")

            await asyncio.sleep(CHECK_INTERVAL)

    async def process_batch(self):
        """Process batch of unoptimized memories"""
        query = """
        SELECT id, content, quality_rating, LENGTH(content) as len,
               COALESCE((metadata->>'distillation_attempts')::int, 0) as attempts
        FROM memories
        WHERE llm_optimized = false
          AND content IS NOT NULL
          AND LENGTH(content) > 100
          AND LENGTH(content) <= $1
          AND COALESCE((metadata->>'distillation_attempts')::int, 0) < $2
        ORDER BY LENGTH(content) DESC, created DESC
        LIMIT $3
        """

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(query, SIZE_LIMIT_KB * 1024, MAX_ATTEMPTS, BATCH_SIZE)

        if not rows:
            logger.debug("No memories pending optimization")
            return

        memories = [
            {
                'id': row['id'],
                'content': row['content'],
                'quality_rating': row['quality_rating'] or 75,
                'len': row['len'],
                'attempts': row['attempts'] or 0
            }
            for row in rows
        ]

        logger.info(f"Processing batch of {len(memories)} memories")

        for memory in memories:
            try:
                await self.optimize_memory(memory)
            except Exception as e:
                logger.error(f"Error processing {memory['id'][:8]}: {e}")
                await self.increment_attempts(memory['id'])

    async def optimize_memory(self, memory: dict):
        """Compress and score a single memory"""
        memory_id = memory["id"]
        original_text = memory["content"]
        original_len = memory["len"]
        current_quality = memory["quality_rating"]

        content_len = len(original_text)
        if content_len > SIZE_LIMIT_KB * 1024:
            logger.debug(f"Skipping {memory_id[:8]}: content {content_len}b exceeds {SIZE_LIMIT_KB}KB limit")
            return

        try:
            compression_method = None
            compressed = None
            quality_score = None

            # --- Primary path: local LETHE compression (no external calls) ---
            if _COMPRESSION_AVAILABLE and self._compression_engine is not None:
                try:
                    result = self._compression_engine.distill(original_text, strategy=CompressionStrategy.AUTO)
                    compressed_candidate = result.get("compressed_text") or result.get("compressed", "")
                    candidate_quality = float(result.get("quality_score", 0) or 0) * 100  # 0-1 -> 0-100
                    strategy_used = result.get("strategy_used", "token")
                    # strategy_used is 'token', 'sentence', or the engine's internal label;
                    # record as 'lethe-<mode>' for clarity in the compression log.
                    if strategy_used in ("sentence", "sac"):
                        compression_method = "lethe-sentence"
                    elif strategy_used in ("token", "hyco", "token_filter"):
                        compression_method = "lethe-token"
                    else:
                        compression_method = f"lethe-{strategy_used}"
                    if compressed_candidate and len(compressed_candidate) >= 10 and candidate_quality >= 60:
                        compressed = compressed_candidate
                        quality_score = candidate_quality
                        logger.debug(f"Local compression ({compression_method}) quality={quality_score:.1f}")
                    else:
                        logger.debug(
                            f"Local compression quality too low ({candidate_quality:.1f}), falling back to LLM"
                        )
                except Exception as ce:
                    logger.warning(f"Local compression failed for {memory_id[:8]}: {ce}, falling back to LLM")

            # --- Fallback: ExternalInferenceProvider / LLM-assisted compression ---
            if compressed is None:
                timeout = DISTILL_TIMEOUT_BASE + (original_len / 1024) * DISTILL_TIMEOUT_PER_KB
                compressed = await asyncio.wait_for(
                    _distill_backend_call(self.llm, original_text),
                    timeout=timeout
                )
                if compressed:
                    quality_score = await asyncio.wait_for(
                        self.llm.evaluate_quality(original_text, compressed),
                        timeout=QUALITY_TIMEOUT
                    )
                compression_method = "external"

            if not compressed or len(compressed) < 10:
                logger.warning(f"[WARN]  Empty compression for {memory_id[:8]}")
                return

            compressed_len = len(compressed)
            ratio = compressed_len / max(original_len, 1)
            bytes_saved = original_len - compressed_len

            # Persist
            update_query = """
            UPDATE memories
            SET compressed_content = $1,
                quality_rating = $2,
                llm_optimized = true,
                optimized_at = NOW(),
                compression_method = $4,
                metadata = jsonb_set(
                    metadata,
                    '{distillation_success}',
                    'true'
                )
            WHERE id = $3
            """
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    update_query,
                    compressed, min(100, int(quality_score or 75)), memory_id, compression_method
                )

            self.stats["successful"] += 1
            self.stats["total_bytes_saved"] += bytes_saved

            logger.info(
                f"✅ {memory_id[:8]}... [{compression_method}] | quality {current_quality}→{quality_score:.0f} "
                f"| {original_len}→{compressed_len} chars ({ratio:.2%}) "
                f"| saved {bytes_saved} bytes"
            )

        except asyncio.TimeoutError:
            self.stats["timeouts"] += 1
            logger.warning(f"[TIMER] Timeout optimizing {memory_id[:8]} ({original_len} chars)")
            await self.increment_attempts(memory_id)

        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"[ERROR] Error optimizing {memory_id[:8]}: {e}")
            await self.increment_attempts(memory_id)

        finally:
            self.stats["processed"] += 1

    async def increment_attempts(self, memory_id: str):
        """Increment distillation_attempts in metadata"""
        try:
            async with self.db_pool.acquire() as conn:
              await conn.execute(
                """
                UPDATE memories
                SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'),
                    '{distillation_attempts}',
                    to_jsonb(COALESCE((metadata->>'distillation_attempts')::int, 0) + 1)
                )
                WHERE id = $1
                """,
                memory_id
              )
        except Exception as e:
            logger.error(f"Could not increment attempts for {memory_id}: {e}")

    async def log_stats(self):
        """Log current progress"""
        try:
            async with self.db_pool.acquire() as conn:
              row = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN llm_optimized = true THEN 1 END) as optimized,
                    COUNT(CASE WHEN llm_optimized = false THEN 1 END) as pending,
                    AVG(quality_rating) as avg_quality
                FROM memories
              """)
            total, optimized = row['total'], row['optimized']

            if self.stats["processed"] > 0:
                logger.info(
                    f"📊 Progress: {optimized}/{total} optimized ({100*optimized/max(total,1):.1f}%) | "
                    f"Session: {self.stats['successful']} success, {self.stats['timeouts']} timeouts, "
                    f"{self.stats['errors']} errors | "
                    f"Saved: {self.stats['total_bytes_saved']/1024:.1f}KB"
                )
        except Exception as e:
            logger.debug(f"Could not log stats: {e}")


async def main():
    worker = MemoryDistillationWorker()
    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("Worker shutting down gracefully")
    finally:
        if worker.db_pool:
            await worker.db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
