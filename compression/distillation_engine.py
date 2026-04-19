"""
Distillation Engine: Integrated compression with intelligent strategy selection

Now uses LETHE (THE MOIRAI Tier 1) which combines:
- Token mode (extractive token filter-style): Fast heuristic compression
- Sentence mode (SENTENCE-style): Structure-preserving compression
- Auto mode: Intelligent strategy selection
- Performance monitoring
"""

import logging
import time
from typing import Dict, Optional
from enum import Enum

from .lethe import LETHE

logger = logging.getLogger(__name__)


class CompressionStrategy(Enum):
    """Compression strategy options (backward compat)"""
    TOKEN = "token"           # Token mode (was extractive token filter), ~57% reduction
    SENTENCE = "sentence"         # Sentence mode (was SENTENCE), ~50% reduction
    AUTO = "auto"            # Auto-select based on structure


class DistillationEngine:
    """Integrated distillation/compression engine using LETHE"""

    def __init__(self, default_ratio: float = 0.45):
        """Initialize distillation engine

        Args:
            default_ratio: Default compression ratio
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

        # Initialize LETHE (auto mode)
        self.lethe = LETHE(mode="auto")

    def distill(
        self,
        text: str,
        strategy: CompressionStrategy = CompressionStrategy.AUTO,
        ratio: Optional[float] = None,
        task_type: Optional[str] = None,
    ) -> Dict:
        """Distill/compress text using LETHE

        Args:
            text: Text to compress
            strategy: Compression strategy (token, sentence, or auto)
            ratio: Target compression ratio (overrides default)
            task_type: Task type for ratio selection

        Returns:
            Compression result dict
        """
        start_time = time.time()

        # Coerce string strategy to enum
        if isinstance(strategy, str):
            try:
                strategy = CompressionStrategy(strategy)
            except ValueError:
                strategy = CompressionStrategy.AUTO

        # Use provided ratio or default
        target_ratio = ratio or self._get_ratio_for_task(task_type)

        # Map strategy to LETHE mode
        if strategy == CompressionStrategy.AUTO:
            lethe_mode = "auto"
        elif strategy == CompressionStrategy.SENTENCE:
            lethe_mode = "sentence"
            self.stats["sentence_mode_compressions"] += 1
        else:  # TOKEN
            lethe_mode = "token"
            self.stats["token_mode_compressions"] += 1

        # Compress using LETHE
        result = self.lethe.compress(text, target_ratio=target_ratio, mode=lethe_mode)

        # Record metrics
        end_time = time.time()
        elapsed_ms = (end_time - start_time) * 1000

        self.stats["total_compressions"] += 1
        self.stats["total_input_tokens"] += result["original_tokens"]
        self.stats["total_output_tokens"] += result["compressed_tokens"]
        self.stats["total_time_ms"] += elapsed_ms

        # Add metadata (normalize output for backward compat)
        strategy_value = result.get("mode", lethe_mode)
        result["strategy_used"] = strategy_value
        if "compressed_text" in result and "compressed" not in result:
            result["compressed"] = result["compressed_text"]
        result["compression_time_ms"] = round(elapsed_ms, 2)

        logger.debug(
            f"Distilled {result['original_tokens']} tokens → "
            f"{result['compressed_tokens']} ({result['compression_ratio']:.2%}) "
            f"using {strategy_value} in {elapsed_ms:.2f}ms"
        )

        return result

    def batch_distill(
        self,
        texts: list,
        strategy: CompressionStrategy = CompressionStrategy.AUTO,
        ratio: Optional[float] = None,
    ) -> list:
        """Distill multiple texts

        Args:
            texts: List of texts to compress
            strategy: Compression strategy
            ratio: Target compression ratio

        Returns:
            List of compression results
        """
        results = []
        for text in texts:
            result = self.distill(text, strategy, ratio)
            results.append(result)
        return results

    def _get_ratio_for_task(self, task_type: Optional[str]) -> float:
        """Get compression ratio for task type

        Args:
            task_type: Type of task

        Returns:
            Target compression ratio
        """
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
        """Get compression statistics

        Returns:
            Statistics dict
        """
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
        """Reset statistics"""
        self.stats = {
            "total_compressions": 0,
            "token_mode_compressions": 0,
            "sentence_mode_compressions": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_time_ms": 0.0,
        }
        logger.info("Distillation statistics reset")


# Global instance
_engine = None


def get_distillation_engine() -> DistillationEngine:
    """Get global distillation engine instance

    Returns:
        DistillationEngine instance
    """
    global _engine
    if _engine is None:
        _engine = DistillationEngine()
    return _engine


def distill(text: str,
           strategy: str = "auto",
           ratio: Optional[float] = None,
           task_type: Optional[str] = None) -> Dict:
    """Convenience function to distill text

    Args:
        text: Text to compress
        strategy: 'hyco', 'sac', or 'auto'
        ratio: Target compression ratio
        task_type: Task type for ratio selection

    Returns:
        Compression result
    """
    engine = get_distillation_engine()

    # Convert string strategy to enum
    strategy_enum = {
        'hyco': CompressionStrategy.TOKEN,
        'sac': CompressionStrategy.SENTENCE,
        'auto': CompressionStrategy.AUTO,
    }.get(strategy, CompressionStrategy.AUTO)

    return engine.distill(text, strategy_enum, ratio, task_type)


def get_distillation_stats() -> Dict:
    """Get distillation statistics

    Returns:
        Statistics dict
    """
    engine = get_distillation_engine()
    return engine.get_stats()
