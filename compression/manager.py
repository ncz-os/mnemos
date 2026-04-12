"""
Compression Manager
Orchestrates compression strategies (extractive token filter, SENTENCE) and quality analysis
"""

import logging
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ._token_filter_impl import CompressionResult

from .token_filter import extractive token filter
from .quality_analyzer import QualityAnalyzer

logger = logging.getLogger(__name__)


class CompressionManager:
    """
    Manages compression operations across the system.

    Supports multiple compression strategies:
    - extractive token filter (Hybrid Compression with Online Learning) - fast, heuristic-based
    - SENTENCE (Semantic-Anchor Compression) - structure-preserving

    Task-type specific compression ratios:
    - reasoning: 0.45 (keep 45%)
    - code_generation: 0.30 (keep 30%)
    - architecture_design: 0.50 (keep 50%)
    """

    # Compression enabled per task type
    COMPRESS_TASK_TYPES = {
        'reasoning',
        'code_generation',
        'architecture_design',
        'api_design',
        'data_modeling',
        'debugging',
        'refactoring'
    }

    # Task-specific compression ratios
    TASK_COMPRESSION_RATIOS = {
        'reasoning': 0.45,
        'code_generation': 0.30,
        'architecture_design': 0.50,
        'api_design': 0.40,
        'data_modeling': 0.45,
        'debugging': 0.35,
        'refactoring': 0.40,
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
        Initialize compression manager

        Args:
            config: Configuration dict with keys:
                - default_strategy: 'token_filter' or 'sac'
                - enabled: bool
                - storage.enabled: bool
                - storage.ratios: dict of task_type -> ratio
                - rehydration.enabled: bool
                - rehydration.tier_ratios: dict of tier -> ratio
                - quality.enabled: bool
                - quality.analyzer: 'semantic' or 'heuristic'
        """
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)

        # Initialize compression strategies
        self.token_filter = extractive token filter(
            aggressive=self.config.get('aggressive', True),
            min_length=self.config.get('min_length', 5)
        )

        # Initialize quality analyzer
        semantic_analysis = self.config.get('quality', {}).get(
            'analyzer', 'heuristic'
        ) == 'semantic'
        self.quality_analyzer = QualityAnalyzer(
            enable_semantic_analysis=semantic_analysis
        )

        # Load custom ratios if provided
        self.task_ratios = self.TASK_COMPRESSION_RATIOS.copy()
        if 'storage' in self.config and 'ratios' in self.config['storage']:
            self.task_ratios.update(self.config['storage']['ratios'])

        self.tier_ratios = self.TIER_COMPRESSION_RATIOS.copy()
        if 'rehydration' in self.config and 'tier_ratios' in self.config['rehydration']:
            self.tier_ratios.update(self.config['rehydration']['tier_ratios'])

        logger.info("✓ CompressionManager initialized")
        logger.info(f"  Default strategy: {self.config.get('default_strategy', 'token_filter')}")
        logger.info(f"  Semantic analysis: {'enabled' if semantic_analysis else 'disabled'}")

    def should_compress(self, task_type: str) -> bool:
        """Check if compression is enabled for this task type"""
        if not self.enabled:
            return False

        if task_type not in self.COMPRESS_TASK_TYPES:
            return False

        return self.config.get('storage', {}).get('enabled', True)

    async def compress(
        self,
        text: str,
        task_type: str,
        target_ratio: Optional[float] = None,
        method: Optional[str] = None
    ) -> 'CompressionResult':
        """
        Compress text using configured strategy.

        Args:
            text: Text to compress
            task_type: Type of task (for ratio selection)
            target_ratio: Override compression ratio (0.0-1.0)
            method: Override compression method ('token_filter', 'sac')

        Returns:
            CompressionResult with compressed text and quality metrics
        """
        if not self.enabled:
            # Return uncompressed
            from ._token_filter_impl import CompressionResult
            return CompressionResult(
                compressed_text=text,
                original_tokens=len(text.split()),
                compressed_tokens=len(text.split()),
                compression_ratio=1.0,
                quality_rating=100,
                quality_summary={'note': 'Compression disabled'},
                compression_manifest={'note': 'Compression disabled'},
                method='none'
            )

        # Determine target ratio
        if target_ratio is None:
            target_ratio = self.task_ratios.get(task_type, 0.4)

        # Determine compression method
        if method is None:
            method = self.config.get('default_strategy', 'token_filter')

        # Execute compression
        try:
            if method == 'token_filter':
                result = await self._compress_token_filter(text, task_type, target_ratio)
            else:
                # Fallback to extractive token filter
                result = await self._compress_token_filter(text, task_type, target_ratio)

            logger.debug(
                f"✓ Compressed {len(text.split())} tokens → "
                f"{len(result.compressed_text.split())} tokens "
                f"({target_ratio:.0%})"
            )

            return result

        except Exception as e:
            logger.error(f"Compression failed: {e}, returning original")
            # Fallback: return original
            from ._token_filter_impl import CompressionResult
            return CompressionResult(
                compressed_text=text,
                original_tokens=len(text.split()),
                compressed_tokens=len(text.split()),
                compression_ratio=1.0,
                quality_rating=100,
                quality_summary={'error': str(e)},
                compression_manifest={'error': str(e)},
                method='none'
            )

    async def _compress_token_filter(
        self,
        text: str,
        task_type: str,
        target_ratio: float
    ) -> 'CompressionResult':
        """Compress using extractive token filter algorithm"""
        result = self.token_filter.compress(text, target_ratio=target_ratio)

        # Analyze quality
        quality_manifest = await self.quality_analyzer.analyze(
            original=text,
            compressed=result['compressed_text'],
            task_type=task_type,
            method='token_filter',
            source='compression_manager'
        )

        # Convert to CompressionResult
        from ._token_filter_impl import CompressionResult
        return CompressionResult(
            compressed_text=result['compressed_text'],
            original_tokens=result['original_tokens'],
            compressed_tokens=result['compressed_tokens'],
            compression_ratio=result['compression_ratio'],
            quality_rating=quality_manifest.quality_rating,
            quality_summary=quality_manifest.quality_summary,
            compression_manifest=quality_manifest.compression_manifest,
            method='token_filter'
        )

    async def decompress(self, compressed: str) -> str:
        """
        Decompress text (currently a no-op, as extractive token filter is lossy).

        In future, could support reversible compression methods.
        """
        # extractive token filter is lossy - decompression returns best-effort reconstruction
        # For now, just return the text as-is
        return compressed

    def get_tier_ratio(self, tier: int) -> float:
        """Get compression ratio for a specific tier"""
        return self.tier_ratios.get(tier, 0.5)

    def get_task_ratio(self, task_type: str) -> float:
        """Get default compression ratio for a task type"""
        return self.task_ratios.get(task_type, 0.4)

    def get_stats(self) -> Dict:
        """Get compression statistics"""
        return {
            'enabled': self.enabled,
            'default_strategy': self.config.get('default_strategy', 'token_filter'),
            'task_ratios': self.task_ratios,
            'tier_ratios': self.tier_ratios,
            'semantic_analysis': self.config.get('quality', {}).get('analyzer') == 'semantic',
        }
