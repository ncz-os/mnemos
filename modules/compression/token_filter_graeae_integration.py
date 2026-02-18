#!/usr/bin/env python3
"""
token-filter² Integration for GRAEAE Consensus Engine

This module provides integration between the token-filter² compression engine
and the GRAEAE multi-LLM consensus routing system.

Usage:
    from token_filter_graeae_integration import token-filterCompressionAdapter

    adapter = token-filterCompressionAdapter()
    compressed_result = adapter.compress_consensus_response(
        consensus_text,
        task_type="reasoning",
        target_ratio=0.3
    )
"""

from typing import Dict, Optional, Any
from datetime import datetime
import json
import hashlib
from pathlib import Path

# Assumes token_filter_implementation is available
try:
    from token_filter_implementation import token-filterCompressor, CompressionResult
    HYCO2_AVAILABLE = True
except ImportError:
    HYCO2_AVAILABLE = False
    print("[WARNING] token-filter² implementation not available. Install token_filter_implementation.py")


class token-filterCompressionAdapter:
    """
    Adapter to integrate token-filter² compression with GRAEAE consensus engine.

    Features:
    - Task-type specific compression ratios
    - Consensus quality preservation
    - Metrics tracking and reporting
    - Fallback to no compression if issues arise
    """

    # Task-specific compression ratios (strategy D from semantic loss mitigation)
    TASK_SPECIFIC_RATIOS = {
        'reasoning': 0.4,           # Keep 40% for reasoning (needs chain-of-thought)
        'code_generation': 0.35,    # Keep 35% for code (concise)
        'architecture_design': 0.45, # Keep 45% for design (needs context)
        'analysis': 0.40,           # Keep 40% for analysis
        'qa': 0.35,                 # Keep 35% for Q&A (short answers)
        'default': 0.30             # Keep 30% by default
    }

    # Minimum tokens to preserve (strategy C from semantic loss mitigation)
    MIN_TOKENS_TO_PRESERVE = 50

    def __init__(self,
                 embedding_model: str = "all-MiniLM-L6-v2",
                 enable_metrics: bool = True,
                 metrics_file: Optional[str] = None):
        """
        Initialize token-filter² compression adapter for GRAEAE.

        Args:
            embedding_model: SentenceTransformer model to use
            enable_metrics: Whether to track compression metrics
            metrics_file: File to store compression metrics (default: /tmp/token_filter_metrics.json)
        """
        self.embedding_model = embedding_model
        self.enable_metrics = enable_metrics
        self.metrics_file = metrics_file or "/tmp/token_filter_metrics.json"

        # Initialize token-filter² compressor
        if HYCO2_AVAILABLE:
            self.compressor = token-filterCompressor(embedding_model=embedding_model)
            self.available = True
        else:
            self.compressor = None
            self.available = False

        # Metrics tracking
        self.metrics = {
            'total_compressions': 0,
            'total_tokens_input': 0,
            'total_tokens_output': 0,
            'compressions_by_task': {},
            'compression_ratios': [],
            'errors': []
        }

        self._load_metrics()

    def _load_metrics(self):
        """Load metrics from file if available."""
        if self.enable_metrics and Path(self.metrics_file).exists():
            try:
                with open(self.metrics_file, 'r') as f:
                    saved_metrics = json.load(f)
                    # Merge with existing metrics
                    for key in ['total_compressions', 'total_tokens_input', 'total_tokens_output']:
                        if key in saved_metrics:
                            self.metrics[key] = saved_metrics.get(key, 0)
            except Exception as e:
                print(f"[WARNING] Failed to load metrics: {e}")

    def _save_metrics(self):
        """Save metrics to file."""
        if self.enable_metrics:
            try:
                with open(self.metrics_file, 'w') as f:
                    json.dump(self.metrics, f, indent=2, default=str)
            except Exception as e:
                print(f"[WARNING] Failed to save metrics: {e}")

    def get_target_ratio(self, task_type: str) -> float:
        """
        Get task-specific compression target ratio.

        Args:
            task_type: Type of task (reasoning, code_generation, etc.)

        Returns:
            Target compression ratio (0.3 = keep 30% of tokens)
        """
        return self.TASK_SPECIFIC_RATIOS.get(task_type, self.TASK_SPECIFIC_RATIOS['default'])

    def compress_consensus_response(self,
                                   text: str,
                                   task_type: str = 'reasoning',
                                   target_ratio: Optional[float] = None,
                                   enforce_minimum: bool = True) -> Dict[str, Any]:
        """
        Compress a consensus response from GRAEAE.

        Args:
            text: Full consensus response text to compress
            task_type: Type of task (for task-specific compression)
            target_ratio: Override default target ratio (optional)
            enforce_minimum: Enforce minimum token threshold

        Returns:
            {
                'original': original_text,
                'compressed': compressed_text,
                'original_tokens': original token count,
                'compressed_tokens': compressed token count,
                'compression_ratio': ratio achieved,
                'compression_percentage': percentage reduction,
                'method': 'token_filter' or 'fallback',
                'task_type': task_type,
                'timestamp': compression timestamp,
                'status': 'success' or 'failed'
            }
        """
        timestamp = datetime.now().isoformat()

        # Use task-specific ratio if not provided
        if target_ratio is None:
            target_ratio = self.get_target_ratio(task_type)

        # Track compression
        original_tokens = len(text.split())

        # If not enough tokens to compress, return original
        if original_tokens < self.MIN_TOKENS_TO_PRESERVE:
            result = {
                'original': text,
                'compressed': text,
                'original_tokens': original_tokens,
                'compressed_tokens': original_tokens,
                'compression_ratio': 1.0,
                'compression_percentage': 0,
                'method': 'no_compression',
                'reason': 'below_minimum_threshold',
                'task_type': task_type,
                'timestamp': timestamp,
                'status': 'success'
            }
            return result

        try:
            if not self.available or not self.compressor:
                # Fallback: return original if token-filter² not available
                return {
                    'original': text,
                    'compressed': text,
                    'original_tokens': original_tokens,
                    'compressed_tokens': original_tokens,
                    'compression_ratio': 1.0,
                    'compression_percentage': 0,
                    'method': 'fallback_unavailable',
                    'task_type': task_type,
                    'timestamp': timestamp,
                    'status': 'warning'
                }

            # Apply token-filter² compression
            compression_result = self.compressor.compress(
                text,
                target_ratio=target_ratio
            )

            # Apply minimum token enforcement
            final_tokens = compression_result.compressed_tokens
            if enforce_minimum and final_tokens < self.MIN_TOKENS_TO_PRESERVE:
                # If below minimum after compression, use less aggressive ratio
                new_ratio = min(1.0, self.MIN_TOKENS_TO_PRESERVE / max(original_tokens, 1))
                compression_result = self.compressor.compress(
                    text,
                    target_ratio=new_ratio
                )

            # Build result
            result = {
                'original': compression_result.original_text,
                'compressed': compression_result.compressed_text,
                'original_tokens': compression_result.original_tokens,
                'compressed_tokens': compression_result.compressed_tokens,
                'compression_ratio': compression_result.compression_ratio,
                'compression_percentage': (1 - compression_result.compression_ratio) * 100,
                'method': 'token_filter',
                'stage1_tokens': compression_result.stage1_tokens,
                'stage2_tokens': compression_result.stage2_tokens,
                'task_type': task_type,
                'target_ratio': target_ratio,
                'timestamp': timestamp,
                'status': 'success'
            }

            # Update metrics
            self._update_metrics(result)

            return result

        except Exception as e:
            # Error handling with fallback
            error_msg = f"token-filter² compression failed: {str(e)}"
            print(f"[ERROR] {error_msg}")

            self.metrics['errors'].append({
                'timestamp': timestamp,
                'error': error_msg,
                'task_type': task_type
            })

            return {
                'original': text,
                'compressed': text,
                'original_tokens': original_tokens,
                'compressed_tokens': original_tokens,
                'compression_ratio': 1.0,
                'compression_percentage': 0,
                'method': 'fallback_error',
                'error': error_msg,
                'task_type': task_type,
                'timestamp': timestamp,
                'status': 'failed'
            }

    def _update_metrics(self, result: Dict[str, Any]):
        """Update compression metrics."""
        if not self.enable_metrics:
            return

        self.metrics['total_compressions'] += 1
        self.metrics['total_tokens_input'] += result['original_tokens']
        self.metrics['total_tokens_output'] += result['compressed_tokens']
        self.metrics['compression_ratios'].append(result['compression_ratio'])

        # Task-specific tracking
        task_type = result['task_type']
        if task_type not in self.metrics['compressions_by_task']:
            self.metrics['compressions_by_task'][task_type] = {
                'count': 0,
                'total_input': 0,
                'total_output': 0
            }

        self.metrics['compressions_by_task'][task_type]['count'] += 1
        self.metrics['compressions_by_task'][task_type]['total_input'] += result['original_tokens']
        self.metrics['compressions_by_task'][task_type]['total_output'] += result['compressed_tokens']

        # Save metrics periodically
        if self.metrics['total_compressions'] % 10 == 0:
            self._save_metrics()

    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get compression metrics summary."""
        total_input = self.metrics['total_tokens_input']
        total_output = self.metrics['total_tokens_output']
        avg_ratio = total_output / max(total_input, 1)
        avg_compression_pct = (1 - avg_ratio) * 100

        return {
            'status': 'available' if self.available else 'unavailable',
            'total_compressions': self.metrics['total_compressions'],
            'total_tokens_input': total_input,
            'total_tokens_output': total_output,
            'average_compression_ratio': avg_ratio,
            'average_compression_percentage': avg_compression_pct,
            'compression_ratios_min': min(self.metrics['compression_ratios']) if self.metrics['compression_ratios'] else 0,
            'compression_ratios_max': max(self.metrics['compression_ratios']) if self.metrics['compression_ratios'] else 0,
            'errors_count': len(self.metrics['errors']),
            'by_task_type': self.metrics['compressions_by_task'],
            'embedding_model': self.embedding_model
        }

    def get_compressor_stats(self) -> Dict[str, Any]:
        """Get token-filter² compressor internal statistics."""
        if not self.available or not self.compressor:
            return {'status': 'unavailable'}

        return self.compressor.get_stats()


# Example usage and CLI
if __name__ == "__main__":
    print("=" * 80)
    print("token-filter² Integration for GRAEAE Consensus Engine")
    print("=" * 80)

    # Initialize adapter
    print("\n[INIT] Initializing token-filter² adapter for GRAEAE...")
    adapter = token-filterCompressionAdapter()

    if not adapter.available:
        print("[WARNING] token-filter² not available, running in demonstration mode")

    # Sample GRAEAE consensus response (simulated)
    sample_consensus = """
    Based on the multi-LLM consensus analysis, the recommended architecture for scaling
    the MNEMOS inference system involves several key components. First, implement a
    load balancer using HAProxy or Nginx to distribute incoming requests across multiple
    backend servers. Second, containerize the MNEMOS application using Docker to ensure
    consistent deployment across all inference nodes. Third, use Kubernetes (k8s) for
    orchestration to manage container scaling, health checks, and automatic failover.

    The consensus models (GPT-4, Claude 3, Gemini) all agreed that the system should
    implement horizontal scaling rather than vertical scaling for cost efficiency. The
    estimated latency for the consensus process is 15-20 seconds per request, which is
    acceptable for most reasoning tasks. However, for real-time applications requiring
    lower latency, implementing a caching layer with Redis would provide significant
    improvements, reducing response time to 2-3 seconds for cached queries.

    Security considerations include implementing rate limiting, API authentication using
    JWT tokens, and encrypting all data in transit using TLS 1.3. The consensus model
    strongly recommends against exposing the inference API directly to the internet without
    proper API gateway protection.
    """

    print(f"\nOriginal consensus response:")
    print(f"  Length: {len(sample_consensus)} characters")
    print(f"  Tokens: {len(sample_consensus.split())} tokens")

    # Test compression for different task types
    print("\n[TEST] Compressing for different task types:")
    task_types = ['reasoning', 'architecture_design', 'code_generation', 'qa']

    for task_type in task_types:
        result = adapter.compress_consensus_response(
            sample_consensus,
            task_type=task_type
        )

        target_ratio = adapter.get_target_ratio(task_type)
        print(f"\n  Task type: {task_type}")
        print(f"    Target ratio: {target_ratio:.0%}")
        print(f"    Result tokens: {result['original_tokens']} → {result['compressed_tokens']}")
        print(f"    Compression: {result['compression_percentage']:.1f}%")
        print(f"    Status: {result['status']}")

    # Show metrics
    print(f"\n[METRICS] Compression Summary:")
    metrics = adapter.get_metrics_summary()
    for key, value in metrics.items():
        if isinstance(value, float):
            if 'ratio' in key or 'percentage' in key:
                print(f"  {key}: {value:.3f}")
            else:
                print(f"  {key}: {value:.2f}")
        elif isinstance(value, dict):
            print(f"  {key}: {len(value)} task types")
        else:
            print(f"  {key}: {value}")

    print("\n[READY] token-filter² integration ready for GRAEAE deployment")
