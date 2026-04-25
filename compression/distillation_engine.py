"""
Distillation Engine: ARTEMIS-backed compression with the legacy
DistillationEngine + distill() API surface preserved for callers.

Was LETHE-backed in v3.0–v3.2; LETHE was removed in v3.3 (cleanup
of the legacy compression stack — see EVOLUTION.md "v3.2 tail").
ARTEMIS is the going-forward CPU-only extractive engine with
identifier preservation and structure-aware label detection.

API shape preserved for distillation_worker.py and any external
caller that constructs a DistillationEngine and calls distill():
  - distill(text, ...) returns a dict with the same keys
    (compressed_text, original_tokens, compressed_tokens,
    compression_ratio, quality_score, strategy_used,
    compression_time_ms).
  - Strategy enum kept as a no-op API field (ARTEMIS has no
    token/sentence/auto distinction); accepted for backwards
    compatibility, ignored at the engine level.
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Dict, Optional

from .artemis import ARTEMISEngine
from .base import CompressionRequest

logger = logging.getLogger(__name__)


class CompressionStrategy(Enum):
    """Compression strategy. Vestigial post-LETHE-removal — ARTEMIS
    has a single extractive path. Kept so callers passing a strategy
    don't break; the value is recorded in the result for
    observability but does not change the algorithm."""
    TOKEN = "token"
    SENTENCE = "sentence"
    AUTO = "auto"


class DistillationEngine:
    """Integrated distillation/compression engine, ARTEMIS-backed."""

    def __init__(self, default_ratio: float = 0.45):
        """Initialize distillation engine.

        Args:
            default_ratio: Default compression ratio.
        """
        self.default_ratio = default_ratio
        self.stats = {
            "total_compressions": 0,
            "token_mode_compressions": 0,
            "sentence_mode_compressions": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_time_ms": 0.0,
        }
        self._engine = ARTEMISEngine()

    async def distill_async(
        self,
        text: str,
        strategy: CompressionStrategy = CompressionStrategy.AUTO,
        ratio: Optional[float] = None,
        task_type: Optional[str] = None,
    ) -> Dict:
        """Distill/compress text using ARTEMIS — async path.

        Use this from any async caller (the distillation worker, FastAPI
        handlers, anywhere already inside an event loop). The sync
        distill() method spawns a fresh loop via asyncio.run() and
        therefore raises RuntimeError when called from inside a running
        loop. distill_async() is the correct seam for those callers.
        """
        start_time = time.time()

        if isinstance(strategy, str):
            try:
                strategy = CompressionStrategy(strategy)
            except ValueError:
                strategy = CompressionStrategy.AUTO

        target_ratio = ratio if ratio is not None else self._get_ratio_for_task(task_type)

        # Maintain stat counters at the strategy granularity callers
        # expect, even though ARTEMIS treats them as the same path.
        if strategy == CompressionStrategy.SENTENCE:
            self.stats["sentence_mode_compressions"] += 1
        elif strategy == CompressionStrategy.TOKEN:
            self.stats["token_mode_compressions"] += 1

        request = CompressionRequest(
            memory_id=task_type or "distillation",
            content=text,
            task_type=task_type,
            target_ratio=target_ratio,
        )

        artemis_result = await self._engine.compress(request)

        elapsed_ms = (time.time() - start_time) * 1000
        original_tokens = artemis_result.original_tokens
        compressed_tokens = artemis_result.compressed_tokens or 0
        compression_ratio = (
            artemis_result.compression_ratio
            if artemis_result.compression_ratio is not None
            else 1.0
        )

        self.stats["total_compressions"] += 1
        self.stats["total_input_tokens"] += original_tokens
        self.stats["total_output_tokens"] += compressed_tokens
        self.stats["total_time_ms"] += elapsed_ms

        result: Dict = {
            "compressed_text": artemis_result.compressed_content or text,
            "compressed": artemis_result.compressed_content or text,
            "original_tokens": original_tokens,
            "compressed_tokens": compressed_tokens,
            "compression_ratio": compression_ratio,
            "quality_score": artemis_result.quality_score,
            "strategy_used": strategy.value,
            "compression_time_ms": round(elapsed_ms, 2),
            "engine": "artemis",
        }
        if artemis_result.error:
            result["error"] = artemis_result.error

        logger.debug(
            f"Distilled {original_tokens} tokens → "
            f"{compressed_tokens} ({compression_ratio:.2%}) "
            f"using artemis in {elapsed_ms:.2f}ms"
        )

        return result

    def distill(
        self,
        text: str,
        strategy: CompressionStrategy = CompressionStrategy.AUTO,
        ratio: Optional[float] = None,
        task_type: Optional[str] = None,
    ) -> Dict:
        """Distill/compress text using ARTEMIS — sync path.

        Spawns its own event loop. **Cannot be called from inside an
        existing running loop** — raises RuntimeError. Async callers
        must use distill_async().

        Args:
            text: Text to compress.
            strategy: Compression strategy (vestigial — recorded in
                output for observability, ignored at the engine level).
            ratio: Target compression ratio (overrides default).
            task_type: Task type for ratio selection.

        Returns:
            Compression result dict — same shape as the historical
            LETHE-backed return value.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Expected: no running loop, sync path is safe.
            pass
        else:
            raise RuntimeError(
                "DistillationEngine.distill() cannot be called from "
                "inside a running event loop. Use distill_async() "
                "instead. (Pre-fix this was the silent regression that "
                "made the worker always fall through to the LLM "
                "fallback.)"
            )
        return asyncio.run(
            self.distill_async(text, strategy, ratio, task_type)
        )

    def batch_distill(
        self,
        texts: list,
        strategy: CompressionStrategy = CompressionStrategy.AUTO,
        ratio: Optional[float] = None,
    ) -> list:
        """Distill multiple texts."""
        return [self.distill(text, strategy, ratio) for text in texts]

    def _get_ratio_for_task(self, task_type: Optional[str]) -> float:
        ratios = {
            "reasoning": 0.45,
            "code_generation": 0.30,
            "architecture_design": 0.50,
            "api_design": 0.40,
            "data_modeling": 0.45,
            "debugging": 0.35,
            "refactoring": 0.40,
            "research": 0.40,
        }
        return ratios.get(task_type, self.default_ratio)

    def get_stats(self) -> Dict:
        total_tokens = self.stats["total_input_tokens"]
        if total_tokens == 0:
            avg_ratio = 1.0
            avg_time = 0.0
        else:
            avg_ratio = self.stats["total_output_tokens"] / total_tokens
            avg_time = self.stats["total_time_ms"] / max(
                self.stats["total_compressions"], 1
            )
        return {
            "total_compressions": self.stats["total_compressions"],
            "token_mode_compressions": self.stats["token_mode_compressions"],
            "sentence_mode_compressions": self.stats["sentence_mode_compressions"],
            "total_input_tokens": self.stats["total_input_tokens"],
            "total_output_tokens": self.stats["total_output_tokens"],
            "average_ratio": round(avg_ratio, 4),
            "average_time_ms": round(avg_time, 2),
            "total_time_ms": round(self.stats["total_time_ms"], 2),
            "compression_efficiency": round((1 - avg_ratio) * 100, 2),
        }

    def reset_stats(self) -> None:
        self.stats = {
            "total_compressions": 0,
            "token_mode_compressions": 0,
            "sentence_mode_compressions": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_time_ms": 0.0,
        }
        logger.info("Distillation statistics reset")


_engine: Optional[DistillationEngine] = None


def get_distillation_engine() -> DistillationEngine:
    """Get global distillation engine instance."""
    global _engine
    if _engine is None:
        _engine = DistillationEngine()
    return _engine


def distill(
    text: str,
    strategy: str = "auto",
    ratio: Optional[float] = None,
    task_type: Optional[str] = None,
) -> Dict:
    """Convenience function to distill text."""
    engine = get_distillation_engine()
    strategy_enum = {
        "token": CompressionStrategy.TOKEN,
        "sentence": CompressionStrategy.SENTENCE,
        "auto": CompressionStrategy.AUTO,
    }.get(strategy, CompressionStrategy.AUTO)
    return engine.distill(text, strategy_enum, ratio, task_type)


def get_distillation_stats() -> Dict:
    """Get distillation statistics."""
    return get_distillation_engine().get_stats()
