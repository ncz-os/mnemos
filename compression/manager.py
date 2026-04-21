"""
Compression Manager - Orchestrates THE MOIRAI compression tiers

Tier 1: LETHE (CPU, real-time)
Tier 2: ALETHEIA (GPU, batch offline)
Tier 3: ANAMNESIS (GPU, archival fact extraction)
Tier 4: None (full archive)
"""

import logging
from typing import Dict, Optional

from .lethe import LETHE
from .aletheia import ALETHEIA
from .anamnesis import ANAMNESIS
from .quality_analyzer import QualityAnalyzer

logger = logging.getLogger(__name__)


class CompressionResult(Dict):
    """Flexible result container for all compression tiers."""

    def __init__(self, **kwargs):
        """Store compression result as dict."""
        super().__init__(**kwargs)
        self.compressed_text = kwargs.get("compressed_text", "")
        self.original_tokens = kwargs.get("original_tokens", 0)
        self.compressed_tokens = kwargs.get("compressed_tokens", 0)
        self.compression_ratio = kwargs.get("compression_ratio", 1.0)
        self.quality_rating = kwargs.get("quality_rating", 100)
        self.quality_summary = kwargs.get("quality_summary", {})
        self.compression_manifest = kwargs.get("compression_manifest", {})
        self.method = kwargs.get("method", "none")


class CompressionManager:
    """
    Manages THE MOIRAI compression stack (LETHE/ALETHEIA/ANAMNESIS).

    Task-type specific compression ratios:
    - reasoning: 0.45 (keep 45%)
    - code_generation: 0.30 (keep 30%)
    - architecture_design: 0.50 (keep 50%)
    - api_design: 0.40 (keep 40%)
    - data_modeling: 0.45 (keep 45%)
    - debugging: 0.35 (keep 35%)
    - refactoring: 0.40 (keep 40%)
    """

    # Compression enabled per task type
    COMPRESS_TASK_TYPES = {
        "reasoning",
        "code_generation",
        "architecture_design",
        "api_design",
        "data_modeling",
        "debugging",
        "refactoring",
    }

    # Task-specific compression ratios
    TASK_COMPRESSION_RATIOS = {
        "reasoning": 0.45,
        "code_generation": 0.30,
        "architecture_design": 0.50,
        "api_design": 0.40,
        "data_modeling": 0.45,
        "debugging": 0.35,
        "refactoring": 0.40,
    }

    # Tier strategy mapping (new, MOIRAI-based)
    TIER_STRATEGY_MAP = {
        1: "lethe",       # Real-time CPU
        2: "aletheia",    # Offline GPU (LLMLingua-2)
        3: "anamnesis",   # Archival fact extraction
        4: "none",        # Full archive
    }

    # Tier-specific compression ratios (for rehydration)
    TIER_COMPRESSION_RATIOS = {
        1: 0.20,  # Tier 1: aggressive (20% tokens, 80% reduction)
        2: 0.35,  # Tier 2: moderate (35% tokens, 65% reduction)
        3: 0.50,  # Tier 3: light (50% tokens, 50% reduction)
        4: 1.00,  # Tier 4: none (100% tokens, full archive)
    }

    def __init__(self, config: Dict = None):
        """
        Initialize compression manager.

        Args:
            config: Configuration dict with keys:
                - default_strategy: 'lethe', 'aletheia', 'anamnesis', or 'auto'
                - enabled: bool
                - storage.enabled: bool
                - storage.ratios: dict of task_type -> ratio
                - rehydration.enabled: bool
                - rehydration.tier_ratios: dict of tier -> ratio
                - quality.enabled: bool
                - quality.analyzer: 'semantic' or 'heuristic'
        """
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)

        # Initialize LETHE (Tier 1 CPU)
        self.lethe = LETHE(
            mode="auto",
            aggressive=self.config.get("aggressive", True),
            min_length=self.config.get("min_length", 5),
        )

        # Initialize ALETHEIA (Tier 2 GPU) — lazy load
        self.aletheia = None

        # Initialize ANAMNESIS (Tier 3 GPU) — lazy load
        self.anamnesis = None

        # Initialize quality analyzer
        semantic_analysis = (
            self.config.get("quality", {}).get("analyzer", "heuristic") == "semantic"
        )
        self.quality_analyzer = QualityAnalyzer(
            enable_semantic_analysis=semantic_analysis
        )

        # Load custom ratios if provided
        self.task_ratios = self.TASK_COMPRESSION_RATIOS.copy()
        if "storage" in self.config and "ratios" in self.config["storage"]:
            self.task_ratios.update(self.config["storage"]["ratios"])

        self.tier_ratios = self.TIER_COMPRESSION_RATIOS.copy()
        if "rehydration" in self.config and "tier_ratios" in self.config["rehydration"]:
            self.tier_ratios.update(self.config["rehydration"]["tier_ratios"])

        logger.info("✓ CompressionManager initialized (LETHE/ALETHEIA/ANAMNESIS)")
        logger.info(f"  Default strategy: {self.config.get('default_strategy', 'lethe')}")
        logger.info(f"  Semantic analysis: {'enabled' if semantic_analysis else 'disabled'}")

    def should_compress(self, task_type: str) -> bool:
        """Check if compression is enabled for this task type."""
        if not self.enabled:
            return False

        if task_type not in self.COMPRESS_TASK_TYPES:
            return False

        return self.config.get("storage", {}).get("enabled", True)

    async def compress(
        self,
        text: str,
        task_type: str,
        target_ratio: Optional[float] = None,
        method: Optional[str] = None,
        tier: int = 1,
    ) -> CompressionResult:
        """
        Compress text using configured strategy.

        Args:
            text: Text to compress
            task_type: Type of task (for ratio selection)
            target_ratio: Override compression ratio (0.0-1.0)
            method: Override compression method ('lethe', 'aletheia', 'anamnesis')
            tier: Tier override (1-4)

        Returns:
            CompressionResult with compressed text and quality metrics
        """
        if not self.enabled:
            return CompressionResult(
                compressed_text=text,
                original_tokens=len(text.split()),
                compressed_tokens=len(text.split()),
                compression_ratio=1.0,
                quality_rating=100,
                quality_summary={"note": "Compression disabled"},
                compression_manifest={"note": "Compression disabled"},
                method="none",
            )

        # Determine target ratio
        if target_ratio is None:
            target_ratio = self.task_ratios.get(task_type, 0.4)

        # Determine compression method
        if method is None:
            method = self.TIER_STRATEGY_MAP.get(tier, "lethe")

        # Execute compression
        try:
            if method in ("lethe", "token_filter"):  # token_filter alias for backward compat
                result = await self._compress_lethe(text, task_type, target_ratio)
            elif method == "aletheia":
                result = await self._compress_aletheia(text, task_type, target_ratio)
            elif method == "anamnesis":
                result = await self._compress_anamnesis(text, task_type)
            else:
                result = await self._compress_lethe(text, task_type, target_ratio)

            logger.debug(
                f"✓ Compressed {result.original_tokens} tokens → "
                f"{result.compressed_tokens} tokens ({target_ratio:.0%})"
            )

            return result

        except Exception as e:
            logger.error(f"Compression failed: {e}, returning original")
            return CompressionResult(
                compressed_text=text,
                original_tokens=len(text.split()),
                compressed_tokens=len(text.split()),
                compression_ratio=1.0,
                quality_rating=100,
                quality_summary={"error": str(e)},
                compression_manifest={"error": str(e)},
                method="none",
            )

    async def _compress_lethe(
        self, text: str, task_type: str, target_ratio: float
    ) -> CompressionResult:
        """Compress using LETHE (Tier 1 CPU)."""
        result = self.lethe.compress(text, target_ratio=target_ratio)

        # Analyze quality (optional)
        quality_manifest = await self.quality_analyzer.analyze(
            original=text,
            compressed=result["compressed_text"],
            task_type=task_type,
            method="lethe",
            source="compression_manager",
        )

        return CompressionResult(
            compressed_text=result["compressed_text"],
            original_tokens=result["original_tokens"],
            compressed_tokens=result["compressed_tokens"],
            compression_ratio=result["compression_ratio"],
            quality_rating=quality_manifest.quality_rating,
            quality_summary=quality_manifest.quality_summary,
            compression_manifest=quality_manifest.compression_manifest,
            method="lethe",
        )

    async def _compress_aletheia(
        self, text: str, task_type: str, target_ratio: float
    ) -> CompressionResult:
        """Compress using ALETHEIA (Tier 2 GPU via the configured GPU host)."""
        if self.aletheia is None:
            self.aletheia = ALETHEIA()

        result = await self.aletheia.compress(text, target_ratio=target_ratio)

        if result.get("error"):
            logger.warning(f"[ALETHEIA] {result['error']}, falling back to LETHE")
            return await self._compress_lethe(text, task_type, target_ratio)

        return CompressionResult(
            compressed_text=result["compressed_text"],
            original_tokens=result["original_tokens"],
            compressed_tokens=result["compressed_tokens"],
            compression_ratio=result["compression_ratio"],
            quality_rating=int(result.get("quality_score", 95) * 100),
            quality_summary={"quality_score": result.get("quality_score")},
            compression_manifest={
                "method": result.get("method"),
                "timestamp": result.get("timestamp"),
            },
            method="aletheia",
        )

    async def _compress_anamnesis(
        self, text: str, task_type: str
    ) -> CompressionResult:
        """Extract facts using ANAMNESIS (Tier 3 GPU via the configured GPU host)."""
        if self.anamnesis is None:
            self.anamnesis = ANAMNESIS()

        result = await self.anamnesis.extract_facts(text, memory_id="", category=task_type)

        if result.get("error"):
            logger.warning(f"[ANAMNESIS] {result['error']}, skipping fact extraction")

        # ANAMNESIS returns facts, not compressed text
        # For now, return original text with fact metadata
        return CompressionResult(
            compressed_text=text,  # Full text preserved
            original_tokens=len(text.split()),
            compressed_tokens=len(text.split()),
            compression_ratio=1.0,
            quality_rating=100,
            quality_summary={"facts": result.get("facts", [])},
            compression_manifest={
                "extraction_method": result.get("extraction_method"),
                "entities": result.get("entities", []),
                "concepts": result.get("concepts", []),
            },
            method="anamnesis",
        )

    async def decompress(self, compressed: str) -> str:
        """
        Decompress text (lossy compression — returns as-is).

        LETHE and ALETHEIA are lossy; ANAMNESIS preserves full text.
        """
        return compressed

    def get_tier_ratio(self, tier: int) -> float:
        """Get compression ratio for a specific tier."""
        return self.tier_ratios.get(tier, 0.5)

    def get_task_ratio(self, task_type: str) -> float:
        """Get default compression ratio for a task type."""
        return self.task_ratios.get(task_type, 0.4)

    def get_stats(self) -> Dict:
        """Get compression statistics."""
        return {
            "enabled": self.enabled,
            "default_strategy": self.config.get("default_strategy", "lethe"),
            "task_ratios": self.task_ratios,
            "tier_ratios": self.tier_ratios,
            "tier_strategy_map": self.TIER_STRATEGY_MAP,
            "semantic_analysis": self.config.get("quality", {}).get("analyzer")
            == "semantic",
        }

    async def close(self) -> None:
        """Clean up GPU resources."""
        if self.aletheia:
            await self.aletheia.close()
        if self.anamnesis:
            await self.anamnesis.close()
