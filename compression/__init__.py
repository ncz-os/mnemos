"""
Compression Module: THE MOIRAI (Tier 1-3 compression)

Provides:
- LETHE: Fast CPU compression (Tier 1, 0.5-5ms, 30-57% reduction; token + sentence modes)
- ALETHEIA: GPU token-level compression via a configurable GPU provider (Tier 2, 200-500ms, 70% reduction)
- ANAMNESIS: GPU fact extraction for archival (Tier 3, 500ms-2s, semantic compression)
- CompressionManager: Orchestrates LETHE/ALETHEIA/ANAMNESIS with fallback
- QualityAnalyzer: Quality manifest generation
"""

from .base import (
    BASE_CHUNK_RATIO,
    MIN_CHUNK_RATIO,
    SAFETY_MARGIN,
    SUMMARIZATION_OVERHEAD_TOKENS,
    CompressionEngine,
    CompressionRequest,
    GPUIntent,
    IdentifierPolicy,
)
from .base import CompressionResult as EngineCompressionResult
from .quality_analyzer import QualityAnalyzer, QualityManifest
from .manager import CompressionManager, CompressionResult
from .lethe import LETHE
from .aletheia import ALETHEIA
from .anamnesis import ANAMNESIS
from .distillation_engine import (
    DistillationEngine,
    CompressionStrategy,
    get_distillation_engine,
    distill,
    get_distillation_stats,
)

__all__ = [
    # v3.1 competitive-selection plugin ABC
    "CompressionEngine",
    "CompressionRequest",
    "EngineCompressionResult",
    "GPUIntent",
    "IdentifierPolicy",
    "BASE_CHUNK_RATIO",
    "MIN_CHUNK_RATIO",
    "SAFETY_MARGIN",
    "SUMMARIZATION_OVERHEAD_TOKENS",
    # v3.0 compression surface (still in use until LETHE/ALETHEIA/ANAMNESIS
    # migrate to the ABC)
    "QualityAnalyzer",
    "QualityManifest",
    "CompressionManager",
    "CompressionResult",
    "LETHE",
    "ALETHEIA",
    "ANAMNESIS",
    "DistillationEngine",
    "CompressionStrategy",
    "get_distillation_engine",
    "distill",
    "get_distillation_stats",
]
