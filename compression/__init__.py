"""
Compression Module

Provides the LETHE / ANAMNESIS / APOLLO stack plus a plugin
CompressionEngine ABC for operator-registered engines.

- LETHE: Fast CPU extractive compression (0.5-5ms, 30-57% reduction;
  token + sentence modes).
- ANAMNESIS: GPU-optional LLM fact extraction for prose that doesn't
  fit a known schema (500ms-2s, semantic compression).
- APOLLO: schema-aware dense encoding for LLM-to-LLM consumption
  (v3.3 S-IC: PortfolioSchema as the first concrete schema with
  rule-based detection; S-II adds LLM fallback, narration endpoint,
  judge-LLM scoring, decision/person/event schemas).
- ALETHEIA: DEPRECATED. GPU token-level importance scoring from the
  v3.1 stack; lost every contest in the 2026-04-23 benchmark
  (docs/benchmarks/compression-2026-04-23.md). Kept importable for
  operators who had it opted-in, but removed from the default
  contest in distillation_worker.py. Scheduled for v4.0 removal.
- CompressionManager: legacy v3.0 orchestrator, still used for
  MNEMOS_COMPRESSION_MODE='off|lethe|anamnesis|auto'.
- QualityAnalyzer: Quality manifest generation.
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
from .contest import (
    BUILT_IN_PROFILES,
    ContestCandidate,
    ContestOutcome,
    ScoringProfile,
    load_scoring_profile,
    run_contest,
)
from .contest_store import persist_contest
from .quality_analyzer import QualityAnalyzer, QualityManifest
from .manager import CompressionManager, CompressionResult
from .lethe import LETHE, LETHEEngine
from .aletheia import ALETHEIA, ALETHEIAEngine
from .anamnesis import ANAMNESIS, ANAMNESISEngine
from .apollo import APOLLOEngine
from .apollo_schemas import PortfolioSchema, Schema as APOLLOSchema
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
    # v3.1 competitive-selection orchestrator
    "ScoringProfile",
    "BUILT_IN_PROFILES",
    "load_scoring_profile",
    "ContestCandidate",
    "ContestOutcome",
    "run_contest",
    "persist_contest",
    # v3.0 compression surface (still in use until LETHE/ANAMNESIS
    # migrate to the ABC; APOLLO lands on the ABC directly)
    "QualityAnalyzer",
    "QualityManifest",
    "CompressionManager",
    "CompressionResult",
    "LETHE",
    "LETHEEngine",
    # ALETHEIA: deprecated — kept importable for operators who had
    # MNEMOS_ALETHEIA_ENABLED=true before the retirement commit;
    # v4.0 removes.
    "ALETHEIA",
    "ALETHEIAEngine",
    "ANAMNESIS",
    "ANAMNESISEngine",
    # v3.3 S-IC: APOLLO — schema-aware dense encoding
    "APOLLOEngine",
    "APOLLOSchema",
    "PortfolioSchema",
    "DistillationEngine",
    "CompressionStrategy",
    "get_distillation_engine",
    "distill",
    "get_distillation_stats",
]
