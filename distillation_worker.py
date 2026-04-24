#!/usr/bin/env python3
"""
Background distillation worker: compresses memories using LETHE (token + sentence modes) or LLM fallback,
updates embeddings, and maintains compression quality metrics.

Lifecycle supervision lives in `api/lifecycle.py::_run_distillation_worker` —
this class knows how to do the work; that wrapper knows how to keep it alive
(exponential-backoff restart, capped at 5 min). See EVOLUTION.md ADR-02 for
the rationale behind the two-file separation.
"""

import asyncio
import logging
import os
import sys
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
    _COMPRESSION_AVAILABLE = True
except Exception as _ce:
    logger.warning(f"Local compression unavailable: {_ce}")
    _COMPRESSION_AVAILABLE = False

# v3.1 contest path: drains memory_compression_queue via the plugin
# CompressionEngine ABC + run_contest + persist_contest. Runs alongside
# the v3.0 direct-memory-polling path; does not replace it. Operators
# can disable the v3.1 path by setting MNEMOS_CONTEST_ENABLED=false.
try:
    from compression.lethe import LETHEEngine
    from compression.aletheia import ALETHEIAEngine  # deprecated — opt-in only
    from compression.anamnesis import ANAMNESISEngine
    from compression.apollo import APOLLOEngine
    from compression.worker_contest import process_contest_queue
    _CONTEST_AVAILABLE = True
except Exception as _ce:
    logger.warning(f"v3.1 contest path unavailable: {_ce}")
    _CONTEST_AVAILABLE = False

_CONTEST_ENABLED = os.getenv("MNEMOS_CONTEST_ENABLED", "true").lower() == "true"

# ALETHEIA is DEPRECATED. v3.2 tail retirement:
#
# The 2026-04-23 CERBERUS benchmark across 49 PYTHIA memories recorded
# ALETHEIA winning 0 contests. The index-list scoring prompt doesn't
# survive instruction-tuned generalist LLMs (Qwen2.5-Coder-7B on
# TYPHON and gemma-4-E4B-it on CERBERUS both returned whitespace or
# punctuation instead of an index list), and the first-N fallback is
# strictly inferior to LETHE at lower cost.
#
# The niche audit found every case where ALETHEIA might theoretically
# win is already owned by LETHE (same prose-prune shape, free),
# ANAMNESIS (better fact shape), or APOLLO (schema-typed). The going-
# forward stack is LETHE + ANAMNESIS + APOLLO.
#
# Kept behind MNEMOS_ALETHEIA_ENABLED for any operator install that
# had it opted in before retirement, but the engine class now emits a
# DeprecationWarning on construction. v4.0 removes it entirely.
#
# See docs/benchmarks/compression-2026-04-23.md for the measured
# rationale.
_ALETHEIA_ENABLED = os.getenv("MNEMOS_ALETHEIA_ENABLED", "false").lower() == "true"

# Optional minimum-content-length gate for the v3.1 contest path.
# Memories shorter than this value are marked 'failed' with
# error='too_short' BEFORE any engine runs, avoiding the multi-second
# ANAMNESIS GPU round-trip on content that cannot be meaningfully
# compressed (git commit headers, GRAEAE consultation stubs, and
# other short templated blurbs — see the 2026-04-23 CERBERUS
# benchmark for the analysis). Default 0 = no gate (full v3.1 GA
# behavior). Recommended 500 for GPU-constrained installs.
_CONTEST_MIN_CONTENT_LENGTH = int(
    os.getenv("MNEMOS_CONTEST_MIN_CONTENT_LENGTH", "0")
)

# APOLLO joined the default contest in v3.3 S-II. The engine is
# GPU_OPTIONAL (schema fast path is pure regex; LLM fallback uses
# the GPU host when reachable, short-circuits on a closed circuit,
# returns error on parse failure — see compression/apollo.py). The
# env var lets operators disable APOLLO entirely (e.g. while
# benchmarking the LETHE/ANAMNESIS baseline) without editing code.
_APOLLO_ENABLED = os.getenv("MNEMOS_APOLLO_ENABLED", "true").lower() == "true"

# When APOLLO is on but the LLM fallback is unwanted (operators who
# want only the pure schema fast path — no GPU calls from APOLLO),
# flip this off. supports() then falls back to the S-IC shape:
# APOLLO skips non-schema-matching memories entirely.
_APOLLO_LLM_FALLBACK_ENABLED = (
    os.getenv("MNEMOS_APOLLO_LLM_FALLBACK_ENABLED", "true").lower() == "true"
)

# Stale-running sweep threshold (v3.1.1). Queue rows stuck in 'running'
# longer than this are reclaimed at the top of each batch — reset to
# 'pending' (if attempts < max) or marked 'failed' (if attempts >= max).
# Covers the rare case where a worker crashed after dequeue but before
# any terminal status was recorded (both the contest-transaction commit
# AND the fresh-connection fallback mark-failed failed — pool exhausted,
# SIGKILL, etc.). Default 600s is safe for typical runs that finish in
# seconds. Set to 0 to disable the sweep entirely.
def _parse_stale_threshold_secs() -> int:
    raw = os.getenv("MNEMOS_CONTEST_STALE_THRESHOLD_SECS", "600")
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "MNEMOS_CONTEST_STALE_THRESHOLD_SECS=%r is not an integer; "
            "disabling stale-running sweep.", raw,
        )
        return 0
    if value < 0:
        logger.warning(
            "MNEMOS_CONTEST_STALE_THRESHOLD_SECS=%d is negative; "
            "disabling stale-running sweep.", value,
        )
        return 0
    return value


_CONTEST_STALE_THRESHOLD_SECS = _parse_stale_threshold_secs()

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
        # v3.1 contest engines — populated in start() once config is loaded
        self._contest_engines = []
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

        # Construct v3.1 contest engines if available. Each engine is
        # lazy about creating HTTP clients — construction itself is
        # cheap and doesn't touch the network, so we always build the
        # enabled set and let the gpu_guard handle endpoint
        # unavailability at runtime.
        if _CONTEST_AVAILABLE and _CONTEST_ENABLED:
            # Going-forward stack: LETHE + ANAMNESIS + APOLLO.
            # ALETHEIA is retired — kept opt-in behind
            # MNEMOS_ALETHEIA_ENABLED=true for operators who had it
            # enabled before retirement; emits a DeprecationWarning on
            # construction. v4.0 removes it entirely.
            self._contest_engines = [LETHEEngine()]
            if _ALETHEIA_ENABLED:
                self._contest_engines.append(ALETHEIAEngine())
            self._contest_engines.append(ANAMNESISEngine())
            if _APOLLO_ENABLED:
                self._contest_engines.append(
                    APOLLOEngine(
                        enable_llm_fallback=_APOLLO_LLM_FALLBACK_ENABLED,
                    )
                )
            engine_ids = [e.id for e in self._contest_engines]
            logger.info(
                "[OK] contest path enabled (engines: %s)",
                ", ".join(engine_ids),
            )
            if _ALETHEIA_ENABLED:
                logger.warning(
                    "ALETHEIA is deprecated and retired from the "
                    "default stack. You have MNEMOS_ALETHEIA_ENABLED=true "
                    "set; v4.0 will remove the engine entirely. See "
                    "docs/benchmarks/compression-2026-04-23.md."
                )
            if _APOLLO_ENABLED and not _APOLLO_LLM_FALLBACK_ENABLED:
                logger.info(
                    "APOLLO registered with LLM fallback DISABLED — "
                    "engine runs only on schema-matching memories. "
                    "Flip MNEMOS_APOLLO_LLM_FALLBACK_ENABLED=true to "
                    "cover all memories."
                )
        else:
            logger.info(
                "v3.1 contest path disabled (available=%s, enabled=%s)",
                _CONTEST_AVAILABLE, _CONTEST_ENABLED,
            )

        logger.info("[OK] Distillation worker started")
        logger.info(f"Backend: {self.llm.__class__.__name__}")
        logger.info(f"Config: size_limit={SIZE_LIMIT_KB}KB, batch={BATCH_SIZE}, "
                   f"distill_timeout={DISTILL_TIMEOUT_BASE}s+({DISTILL_TIMEOUT_PER_KB}s/KB)")

        while True:
            try:
                # v3.0 direct-memory-polling path (backward compat)
                await self.process_batch()
                # v3.1 queue-driven contest path (runs alongside, failure-isolated)
                await self.process_contest_queue_batch()
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

    async def process_contest_queue_batch(self):
        """Drain up to BATCH_SIZE rows from memory_compression_queue via
        the v3.1 contest path. No-op if contest engines aren't configured.

        Failures here do not propagate — the contest path is additive
        to v3.0 behavior and must not kill the worker loop if something
        goes wrong. Errors are logged and the next loop iteration tries
        again.
        """
        if not self._contest_engines:
            return
        try:
            counts = await process_contest_queue(
                self.db_pool,
                self._contest_engines,
                batch_size=BATCH_SIZE,
                max_attempts=MAX_ATTEMPTS,
                min_content_length=_CONTEST_MIN_CONTENT_LENGTH,
                stale_threshold_secs=_CONTEST_STALE_THRESHOLD_SECS,
            )
            if counts:
                logger.info("contest queue drain: %s", counts)
        except Exception as e:
            logger.error("contest queue drain error: %s", e, exc_info=True)

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
                    if strategy_used == "sentence":
                        compression_method = "lethe-sentence"
                    elif strategy_used == "token":
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
