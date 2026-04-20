"""
Compression Module: THE MOIRAI (Tier 1-3 compression)

Provides:
- LETHE: Fast CPU compression (Tier 1, 0.5-5ms, 30-57% reduction)
- ALETHEIA: GPU token-level compression (Tier 2, 200-500ms, 70% reduction) via configurable GPU provider
- ANAMNESIS: GPU fact extraction for archival (Tier 3, 500ms-2s, semantic compression)
- CompressionManager: Orchestrates LETHE/ALETHEIA/ANAMNESIS with fallback
- QualityAnalyzer: Quality manifest generation
"""

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
