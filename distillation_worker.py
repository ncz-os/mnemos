#!/usr/bin/env python3
"""
MNEMOS Background Memory Distillation Worker (v4 - Mistral Optimized)

Configuration (2026-02-19):
  - Mistral 7B Instruct v0.3 on Ollama (configured via OLLAMA_HOST)
  - Dynamic timeouts based on content size
  - Transaction support & attempt tracking
  - Progress monitoring
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
import httpx
import json
import asyncpg

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# Config
# Config — loaded from config.py (single source of truth)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PG_CONFIG as _PG_CONFIG
from config import OLLAMA_HOST as _CFG_OLLAMA_HOST
# Phi-3.5 Mini runs locally on api-host via OpenVINO (port 11435)
# Override with OLLAMA_HOST env var to fall back to inference-server (:11434)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11435")

# Tuning
MODEL = "phi-3.5-mini"  # Phi-3.5 Mini INT4 via OpenVINO on api-host GPU
SIZE_LIMIT_KB = 5
BATCH_SIZE = 5
CHECK_INTERVAL = 30
DISTILL_TIMEOUT_BASE = 45
DISTILL_TIMEOUT_PER_KB = 3
QUALITY_TIMEOUT = 20
MAX_ATTEMPTS = 3

DB_DSN = (
    f"postgresql://{_PG_CONFIG['user']}:{_PG_CONFIG['password']}"
    f"@{_PG_CONFIG['host']}:{_PG_CONFIG['port']}/{_PG_CONFIG['database']}"
)


class LLMDistillationService:
    def __init__(self, base_url: str = OLLAMA_HOST):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=180.0)

    async def distill(self, text: str, content_len: int) -> str:
        """Compress text to ~40% of original length using Mistral"""
        timeout = DISTILL_TIMEOUT_BASE + (content_len / 1024) * DISTILL_TIMEOUT_PER_KB

        prompt = f"""Summarize this text to approximately 40% of original length.
Preserve all critical facts, decisions, and technical details. Remove redundancy.

TEXT:
{text}

SUMMARY:"""

        response = await self.client.post(
            f"{self.base_url}/v1/completions",
            json={
                "model": MODEL,
                "prompt": prompt,
                "temperature": 0.3,
                "top_p": 0.9
            },
            timeout=timeout
        )
        result = response.json()
        return result.get("choices", [{}])[0].get("text", "").strip()

    async def assess_quality(self, original: str, compressed: str) -> float:
        """Score compression quality (0-100)"""
        prompt = f"""Rate this compression quality 0-100.
100 = all critical info preserved. 0 = info lost.

ORIGINAL: {original[:300]}
COMPRESSED: {compressed[:300]}

Score (0-100):"""

        response = await self.client.post(
            f"{self.base_url}/v1/completions",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1}
            },
            timeout=QUALITY_TIMEOUT
        )
        result = response.json()
        try:
            # /v1/completions returns choices[0].text
            raw = result.get("choices", [{}])[0].get("text", "80").strip()
            score = int(''.join(filter(str.isdigit, raw.split()[0])))
            return max(0, min(100, score))
        except Exception:
            return 80


class MemoryDistillationWorker:
    def __init__(self):
        self.db = None
        self.llm = LLMDistillationService()
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
        self.db = await asyncpg.connect(dsn=DB_DSN)
        
        logger.info("✅ Distillation worker started")
        logger.info(f"Model: {MODEL} | Endpoint: {OLLAMA_HOST}")
        logger.info(f"Config: size_limit={SIZE_LIMIT_KB}KB, batch={BATCH_SIZE}, "
                   f"distill_timeout={DISTILL_TIMEOUT_BASE}s+({DISTILL_TIMEOUT_PER_KB}s/KB)")

        while True:
            try:
                await self.process_batch()
                await self.log_stats()
            except Exception as e:
                logger.error(f"Worker error: {e}", exc_info=True)
                try:
                    await self.db.close()
                    self.db = await asyncpg.connect(dsn=DB_DSN)
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

        rows = await self.db.fetch(query, SIZE_LIMIT_KB * 1024, MAX_ATTEMPTS, BATCH_SIZE)

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

        try:
            # Compress
            timeout = DISTILL_TIMEOUT_BASE + (original_len / 1024) * DISTILL_TIMEOUT_PER_KB
            compressed = await asyncio.wait_for(
                self.llm.distill(original_text, original_len),
                timeout=timeout
            )

            if not compressed or len(compressed) < 10:
                logger.warning(f"⚠️  Empty compression for {memory_id[:8]}")
                return

            # Score quality
            quality_score = await asyncio.wait_for(
                self.llm.assess_quality(original_text, compressed),
                timeout=QUALITY_TIMEOUT
            )

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
                metadata = jsonb_set(
                    metadata,
                    '{distillation_success}',
                    'true'
                )
            WHERE id = $3
            """
            await self.db.execute(
                update_query,
                compressed, min(100, int(quality_score)), memory_id
            )

            self.stats["successful"] += 1
            self.stats["total_bytes_saved"] += bytes_saved

            logger.info(
                f"✅ {memory_id[:8]}... | quality {current_quality}→{quality_score:.0f} "
                f"| {original_len}→{compressed_len} chars ({ratio:.2%}) "
                f"| saved {bytes_saved} bytes"
            )

        except asyncio.TimeoutError:
            self.stats["timeouts"] += 1
            logger.warning(f"⏱ Timeout optimizing {memory_id[:8]} ({original_len} chars)")
            await self.increment_attempts(memory_id)

        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"❌ Error optimizing {memory_id[:8]}: {e}")
            await self.increment_attempts(memory_id)

        finally:
            self.stats["processed"] += 1

    async def increment_attempts(self, memory_id: str):
        """Increment distillation_attempts in metadata"""
        try:
            await self.db.execute(
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
            row = await self.db.fetchrow("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN llm_optimized = true THEN 1 END) as optimized,
                    COUNT(CASE WHEN llm_optimized = false THEN 1 END) as pending,
                    AVG(quality_rating) as avg_quality
                FROM memories
            """)
            total, optimized, pending, avg_quality = row['total'], row['optimized'], row['pending'], row['avg_quality']

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
        if worker.db:
            await worker.db.close()


if __name__ == "__main__":
    asyncio.run(main())
