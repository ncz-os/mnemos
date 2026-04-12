"""
Compression Module: Distillation and quality tracking

Provides:
- extractive token filter: Fast, heuristic-based compression (0.48ms, 57% reduction)
- SENTENCE: Semantic-Anchor Compression (structure-preserving, 50% reduction)
- DistillationEngine: Integrated compression with strategy selection
- QualityAnalyzer: Generates quality manifests
- CompressionManager: Orchestrates compression strategies
"""

from .quality_analyzer import QualityAnalyzer, QualityManifest
from .manager import CompressionManager
from .token_filter import extractive token filter
from .sac import SACCompressor, StructureAnalyzer, CompressionStrategySelector
from .distillation_engine import (
    DistillationEngine,
    CompressionStrategy,
    get_distillation_engine,
    distill,
    get_distillation_stats,
)

__all__ = [
    'QualityAnalyzer',
    'QualityManifest',
    'CompressionManager',
    'extractive token filter',
    'SACCompressor',
    'StructureAnalyzer',
    'CompressionStrategySelector',
    'DistillationEngine',
    'CompressionStrategy',
    'get_distillation_engine',
    'distill',
    'get_distillation_stats',
]
