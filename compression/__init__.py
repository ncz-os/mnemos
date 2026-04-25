"""
Compression Module

Provides the APOLLO + ARTEMIS stack plus a plugin CompressionEngine
ABC for operator-registered engines, dispatched via the contest
framework.

- APOLLO: schema-aware dense encoding for LLM-to-LLM consumption
  (v3.3 S-IC: PortfolioSchema as the first concrete schema with
  rule-based detection; S-II adds LLM fallback, narration endpoint,
  judge-LLM scoring, decision/person/event schemas).
- ARTEMIS: CPU-only extractive with identifier preservation,
  labeled-block handling, and evidence-based self-scoring. Drives
  the legacy DistillationEngine API after LETHE removal.
- QualityAnalyzer: Quality manifest generation.

History note: LETHE / ALETHEIA / ANAMNESIS were the v3.0–v3.2 stack.
All three were removed in v3.3 after the 2026-04-23 benchmark
(docs/benchmarks/compression-2026-04-23.md). See EVOLUTION.md
"v3.2 tail" for the full settlement.
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
from .apollo import APOLLOEngine
from .apollo_schemas import PortfolioSchema, Schema as APOLLOSchema
from .artemis import ARTEMISEngine
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
    "QualityAnalyzer",
    "QualityManifest",
    # v3.3 going-forward stack: APOLLO (schema-aware) + ARTEMIS (extractive)
    "APOLLOEngine",
    "APOLLOSchema",
    "PortfolioSchema",
    "ARTEMISEngine",
    "DistillationEngine",
    "CompressionStrategy",
    "get_distillation_engine",
    "distill",
    "get_distillation_stats",
]
