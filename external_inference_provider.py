#!/usr/bin/env python3
"""
Unified External Inference Provider
Handles all compression and context preparation via inference-server llama.cpp

Used for:
  - Memory distillation (compress large memories to 20-30% size)
  - Response compression (compress LLM outputs before storage)
  - Context preparation (compress context for Claude injection)
  - Embedding pre-processing (compress before embedding)

Configuration:
  - Endpoint: <EXTERNAL_INFERENCE_ENDPOINT> (configure via env var)
  - Model: Llama-2-7B-Chat-Q4_K_M.gguf
  - Context window: 3072 tokens
  - Max safe prompt tokens: ~2000 (leaves room for generation)
"""

import httpx
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# llama-server configuration
MAX_CONTEXT_TOKENS = 3072     # Llama-2-7B-Chat context window
MAX_PROMPT_CHARS = 6000       # ~1500 tokens for the prompt (safe margin)
MAX_GENERATION_TOKENS = 800   # Max tokens to generate
MIN_GENERATION_TOKENS = 10    # Minimum meaningful generation


class ExternalInferenceProvider:
    """Unified interface for external LLM inference on inference-server llama.cpp"""

    def __init__(self,
                 endpoint: Optional[str] = None,
                 model: Optional[str] = None,
                 timeout: float = 90.0):
        self.endpoint = endpoint or os.getenv(
            "EXTERNAL_INFERENCE_ENDPOINT",
            "http://localhost:8000"
        )
        self.model = model or os.getenv(
            "EXTERNAL_INFERENCE_MODEL",
            "Llama-2-7B-Chat-Q4_K_M.gguf"
        )
        self.timeout = timeout
        self.client = httpx.AsyncClient(timeout=timeout)

    def _truncate_for_context(self, text: str, max_chars: int = MAX_PROMPT_CHARS) -> str:
        """Truncate text to fit within context window"""
        if len(text) <= max_chars:
            return text
        # Truncate and add indicator
        return text[:max_chars] + "\n[TRUNCATED]"

    def _safe_max_tokens(self, text: str, target_ratio: float) -> int:
        """Calculate safe max_tokens that won't exceed context window"""
        # Estimate tokens in input (~4 chars per token)
        input_tokens = len(text) / 4
        # Target output tokens
        target_output = int(input_tokens * target_ratio)
        # Cap to safe generation limit
        return max(MIN_GENERATION_TOKENS, min(target_output, MAX_GENERATION_TOKENS))

    async def compress(self,
                      text: str,
                      target_ratio: float = 0.25,
                      min_quality: int = 75) -> Dict:
        """Compress text to target ratio (25% of original by default)"""
        if not text or len(text.strip()) == 0:
            return {
                'original': text,
                'compressed': text,
                'original_length': len(text),
                'compressed_length': len(text),
                'ratio': 1.0,
                'quality_score': 100,
                'tokens_saved': 0,
                'latency_ms': 0,
                'success': True,
                'error': None
            }

        original_len = len(text)

        try:
            # Truncate input to fit safely within context window
            truncated_text = self._truncate_for_context(text, MAX_PROMPT_CHARS)
            max_tokens = self._safe_max_tokens(truncated_text, target_ratio)

            prompt = f"""Summarize this text to approximately {int(target_ratio * 100)}% of original length.
Preserve all critical facts, decisions, and technical details. Remove redundancy.

TEXT:
{truncated_text}

SUMMARY:"""

            response = await self.client.post(
                f"{self.endpoint}/v1/completions",
                json={
                    "prompt": prompt,
                    "temperature": 0.3,
                    "top_p": 0.9,
                    "max_tokens": max_tokens
                }
            )

            if response.status_code != 200:
                logger.warning(f"[EIP] Compression HTTP {response.status_code}: {response.text[:200]}")
                return {
                    'original': text,
                    'compressed': text,
                    'original_length': original_len,
                    'compressed_length': original_len,
                    'ratio': 1.0,
                    'quality_score': 0,
                    'tokens_saved': 0,
                    'latency_ms': response.elapsed.total_seconds() * 1000 if response.elapsed else 0,
                    'success': False,
                    'error': f'HTTP {response.status_code}'
                }

            result = response.json()
            compressed_text = result.get('choices', [{}])[0].get('text', '').strip()

            if not compressed_text:
                return {
                    'original': text,
                    'compressed': text,
                    'original_length': original_len,
                    'compressed_length': original_len,
                    'ratio': 1.0,
                    'quality_score': 0,
                    'tokens_saved': 0,
                    'latency_ms': 0,
                    'success': False,
                    'error': 'Empty response from model'
                }

            quality_score = await self.evaluate_quality(text[:600], compressed_text[:600])

            compressed_len = len(compressed_text)
            actual_ratio = compressed_len / max(original_len, 1)
            success = quality_score >= min_quality

            latency = 0
            if hasattr(response, 'elapsed') and response.elapsed:
                latency = response.elapsed.total_seconds() * 1000

            return {
                'original': text,
                'compressed': compressed_text,
                'original_length': original_len,
                'compressed_length': compressed_len,
                'ratio': actual_ratio,
                'quality_score': quality_score,
                'tokens_saved': max(0, original_len - compressed_len),
                'latency_ms': latency,
                'success': success,
                'error': None if success else f'Quality {quality_score} < {min_quality}'
            }

        except Exception as e:
            logger.error(f"[EIP] Compression error: {e}")
            return {
                'original': text,
                'compressed': text,
                'original_length': original_len,
                'compressed_length': original_len,
                'ratio': 1.0,
                'quality_score': 0,
                'tokens_saved': 0,
                'latency_ms': 0,
                'success': False,
                'error': str(e)
            }

    async def evaluate_quality(self, original: str, compressed: str) -> int:
        """Score compression quality (0-100)"""
        if not compressed or len(compressed) == 0:
            return 0

        try:
            # Use short excerpts to avoid context overflow
            orig_excerpt = original[:300]
            comp_excerpt = compressed[:300]

            prompt = f"""Rate this compression quality 0-100.
100 = all critical info preserved.
0 = info lost or incoherent.

ORIGINAL: {orig_excerpt}
COMPRESSED: {comp_excerpt}

Score (0-100):"""

            response = await self.client.post(
                f"{self.endpoint}/v1/completions",
                json={
                    "prompt": prompt,
                    "temperature": 0.1,
                    "max_tokens": 3
                },
                timeout=20.0
            )

            if response.status_code != 200:
                logger.warning(f"[EIP] Quality eval HTTP {response.status_code}")
                return 75

            result = response.json()
            response_text = result.get('choices', [{}])[0].get('text', '75').strip()
            # Extract first integer from response
            digits = ''.join(c for c in response_text.split()[0] if c.isdigit()) if response_text.split() else '75'
            if not digits:
                return 75
            score = int(digits)
            return min(100, max(0, score))

        except Exception as e:
            logger.warning(f"[EIP] Quality eval error: {e}")
            return 75

    async def prepare_context(self,
                             context: str,
                             max_tokens: int = 4000) -> Dict:
        """Prepare context for Claude injection, auto-compressing to fit token budget"""
        if not context:
            return {
                'context_for_injection': '',
                'original_tokens': 0,
                'tokens_used': 0,
                'ratio': 1.0,
                'quality_score': 100,
                'success': True,
                'error': None
            }

        estimated_tokens = len(context) / 4

        if estimated_tokens <= max_tokens:
            return {
                'context_for_injection': context,
                'original_tokens': int(estimated_tokens),
                'tokens_used': int(estimated_tokens),
                'ratio': 1.0,
                'quality_score': 100,
                'success': True,
                'error': None
            }

        target_ratio = max(0.1, min(0.9, max_tokens / estimated_tokens * 0.9))

        compression_result = await self.compress(
            context,
            target_ratio=target_ratio,
            min_quality=70
        )

        if not compression_result['success']:
            # Return truncated original if compression fails
            # Truncate to approximate token budget
            char_budget = max_tokens * 4
            truncated = context[:char_budget]
            return {
                'context_for_injection': truncated,
                'original_tokens': int(estimated_tokens),
                'tokens_used': int(len(truncated) / 4),
                'ratio': len(truncated) / max(len(context), 1),
                'quality_score': 100,
                'success': True,
                'error': f'Compression failed ({compression_result["error"]}), using truncated original'
            }

        compressed_tokens = int(compression_result['compressed_length'] / 4)

        return {
            'context_for_injection': compression_result['compressed'],
            'original_tokens': int(estimated_tokens),
            'tokens_used': compressed_tokens,
            'ratio': compression_result['ratio'],
            'quality_score': compression_result['quality_score'],
            'success': True,
            'error': None
        }

    async def health_check(self) -> bool:
        """Verify inference-server llama-server is responding"""
        try:
            response = await self.client.get(
                f"{self.endpoint}/health",
                timeout=5.0
            )
            if response.status_code == 200:
                data = response.json()
                return data.get('status') == 'ok'
            return False
        except Exception as e:
            logger.error(f"[EIP] Health check failed: {e}")
            return False

    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()
