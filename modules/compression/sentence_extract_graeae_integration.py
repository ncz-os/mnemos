#!/usr/bin/env python3
"""
SENTENCE (Semantic-Anchor Compression) Integration for GRAEAE

This module provides integration between the SENTENCE compression engine
and the GRAEAE multi-LLM consensus routing system.

SENTENCE excels at preserving semantic structure and is excellent for:
- Reasoning tasks (preserves logical flow)
- Architecture tasks (preserves design components)
- Analysis tasks (preserves analytical structure)
"""

from typing import Dict, Optional, Any
from datetime import datetime
import json
from pathlib import Path

# Assumes sac_implementation is available
try:
    from sac_implementation import SACCompressor, CompressionResult
    SAC_AVAILABLE = True
except ImportError:
    SAC_AVAILABLE = False
    print("[WARNING] SENTENCE implementation not available. Install sac_implementation.py")


class SACCompressionAdapter:
    """
    Adapter to integrate SENTENCE compression with GRAEAE consensus engine.

    SENTENCE is optimized for structure-preserving compression and excels at:
    - Reasoning tasks (preserves chain-of-thought)
    - Architecture discussions (preserves design components)
    - Analysis (preserves logical structure)

    Features:
    - Semantic anchor detection and preservation
    - Graph-based token importance
    - Task-type specific compression ratios
    - Fallback to no compression on failure
    - Comprehensive metrics tracking
    """

    # Task-specific compression ratios (optimized for structure preservation)
    TASK_SPECIFIC_RATIOS = {
        'reasoning': 0.45,              # Keep 45% (preserve chain-of-thought)
        'code_generation': 0.30,        # Keep 30% (structure less critical)
        'architecture_design': 0.50,    # Keep 50% (preserve design components)
        'analysis': 0.45,               # Keep 45% (preserve analytical structure)
        'qa': 0.30,                     # Keep 30% (less structure needed)
        'default': 0.35                 # Keep 35% default
    }

    # Minimum tokens to preserve (safety threshold)
    MIN_TOKENS_TO_PRESERVE = 40

    def __init__(self,
                 enable_metrics: bool = True,
                 metrics_file: Optional[str] = None):
        """
        Initialize SENTENCE compression adapter for GRAEAE.

        Args:
            enable_metrics: Whether to track compression metrics
            metrics_file: File to store compression metrics
        """
        self.enable_metrics = enable_metrics
        self.metrics_file = metrics_file or "/tmp/sac_metrics.json"

        # Initialize SENTENCE compressor
        if SAC_AVAILABLE:
            self.compressor = SACCompressor()
            self.available = True
        else:
            self.compressor = None
            self.available = False

        # Metrics tracking
        self.metrics = {
            'total_compressions': 0,
            'total_tokens_input': 0,
            'total_tokens_output': 0,
            'total_anchors_detected': 0,
            'total_anchors_preserved': 0,
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
                    for key in ['total_compressions', 'total_tokens_input',
                                'total_tokens_output', 'total_anchors_detected',
                                'total_anchors_preserved']:
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
        Get task-specific compression target ratio for SENTENCE.

        SENTENCE uses slightly higher ratios than token-filter² to preserve structure.

        Args:
            task_type: Type of task

        Returns:
            Target compression ratio
        """
        return self.TASK_SPECIFIC_RATIOS.get(task_type, self.TASK_SPECIFIC_RATIOS['default'])

    def compress_consensus_response(self,
                                   text: str,
                                   task_type: str = 'reasoning',
                                   target_ratio: Optional[float] = None,
                                   enforce_minimum: bool = True) -> Dict[str, Any]:
        """
        Compress a consensus response using SENTENCE.

        Args:
            text: Full consensus response text
            task_type: Type of task (for task-specific compression)
            target_ratio: Override default target ratio
            enforce_minimum: Enforce minimum token threshold

        Returns:
            {
                'original': original_text,
                'compressed': compressed_text,
                'original_tokens': original token count,
                'compressed_tokens': compressed token count,
                'compression_ratio': ratio achieved,
                'compression_percentage': percentage reduction,
                'num_anchors': number of semantic anchors detected,
                'anchors_preserved': number of anchors in output,
                'method': 'sac' or 'fallback',
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

        # If not enough tokens, return original
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
                # Fallback: return original
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

            # Apply SENTENCE compression
            compression_result = self.compressor.compress(
                text,
                target_ratio=target_ratio
            )

            # Apply minimum enforcement
            final_tokens = compression_result.compressed_tokens
            if enforce_minimum and final_tokens < self.MIN_TOKENS_TO_PRESERVE:
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
                'method': 'sac',
                'num_anchors': compression_result.num_anchors,
                'anchors_preserved': compression_result.anchors_preserved,
                'path_segments': compression_result.path_segments_preserved,
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
            error_msg = f"SENTENCE compression failed: {str(e)}"
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
        self.metrics['total_anchors_detected'] += result.get('num_anchors', 0)
        self.metrics['total_anchors_preserved'] += result.get('anchors_preserved', 0)
        self.metrics['compression_ratios'].append(result['compression_ratio'])

        # Task-specific tracking
        task_type = result['task_type']
        if task_type not in self.metrics['compressions_by_task']:
            self.metrics['compressions_by_task'][task_type] = {
                'count': 0,
                'total_input': 0,
                'total_output': 0,
                'total_anchors': 0
            }

        self.metrics['compressions_by_task'][task_type]['count'] += 1
        self.metrics['compressions_by_task'][task_type]['total_input'] += result['original_tokens']
        self.metrics['compressions_by_task'][task_type]['total_output'] += result['compressed_tokens']
        self.metrics['compressions_by_task'][task_type]['total_anchors'] += result.get('num_anchors', 0)

        # Save periodically
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
            'total_anchors_detected': self.metrics['total_anchors_detected'],
            'total_anchors_preserved': self.metrics['total_anchors_preserved'],
            'anchor_preservation_rate': (
                self.metrics['total_anchors_preserved'] / max(self.metrics['total_anchors_detected'], 1)
            ),
            'errors_count': len(self.metrics['errors']),
            'by_task_type': self.metrics['compressions_by_task']
        }

    def get_compressor_stats(self) -> Dict[str, Any]:
        """Get SENTENCE compressor internal statistics."""
        if not self.available or not self.compressor:
            return {'status': 'unavailable'}

        return self.compressor.get_stats()


# Example usage and CLI
if __name__ == "__main__":
    print("=" * 80)
    print("SENTENCE Integration for GRAEAE Consensus Engine")
    print("=" * 80)

    # Initialize adapter
    print("\n[INIT] Initializing SENTENCE adapter for GRAEAE...")
    adapter = SACCompressionAdapter()

    # Sample consensus response
    sample_consensus = """
    Based on semantic analysis, the proposed microservices architecture involves
    several critical components. First, API Gateway with Kong or Envoy provides
    load balancing and rate limiting. Second, Core Services include User Service,
    Product Service, and Order Service, each running independently. Third,
    Message Queue using RabbitMQ or Kafka enables asynchronous communication.
    Fourth, Data Layer includes PostgreSQL for relational data and MongoDB for
    document storage. Fifth, Caching layer with Redis reduces database load.
    The consensus recommends container orchestration using Kubernetes for
    automated scaling and management. Service discovery using Consul or etcd
    simplifies service location in dynamic environments.
    """

    print(f"\nOriginal consensus response:")
    print(f"  Length: {len(sample_consensus)} characters")
    print(f"  Tokens: {len(sample_consensus.split())} tokens")

    # Test compression for different task types
    print("\n[TEST] Compressing for different task types:")
    task_types = ['reasoning', 'architecture_design', 'analysis', 'code_generation']

    for task_type in task_types:
        result = adapter.compress_consensus_response(
            sample_consensus,
            task_type=task_type
        )

        target_ratio = adapter.get_target_ratio(task_type)
        print(f"\n  Task type: {task_type}")
        print(f"    Target ratio: {target_ratio:.0%}")
        print(f"    Tokens: {result['original_tokens']} → {result['compressed_tokens']}")
        print(f"    Anchors: {result.get('num_anchors', 0)} detected, {result.get('anchors_preserved', 0)} preserved")
        print(f"    Compression: {result['compression_percentage']:.1f}%")
        print(f"    Status: {result['status']}")

    # Show metrics
    print(f"\n[METRICS] Compression Summary:")
    metrics = adapter.get_metrics_summary()
    for key, value in metrics.items():
        if isinstance(value, float):
            if 'ratio' in key or 'percentage' in key or 'rate' in key:
                print(f"  {key}: {value:.3f}")
            else:
                print(f"  {key}: {value:.2f}")
        elif isinstance(value, dict):
            print(f"  {key}: {len(value)} task types")
        else:
            print(f"  {key}: {value}")

    print("\n[READY] SENTENCE integration ready for GRAEAE deployment")
