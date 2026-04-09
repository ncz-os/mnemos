"""
GraeaeClient: HTTP client for Graeae multi-LLM consensus service

Provides:
- Consultation with multi-model consensus
- Mode selection (local, external, auto)
- Fallback handling
- Result storage integration
"""

import logging
import asyncio
import aiohttp
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class ConsultationResult:
    """Result from Graeae consultation"""

    consensus_response: str
    consensus_score: float  # 0-100
    winning_muse: str
    winning_latency_ms: int
    cost: float
    mode: str
    task_type: str
    all_responses: Dict[str, Any]
    timestamp: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'consensus_response': self.consensus_response,
            'consensus_score': self.consensus_score,
            'winning_muse': self.winning_muse,
            'winning_latency_ms': self.winning_latency_ms,
            'cost': self.cost,
            'mode': self.mode,
            'task_type': self.task_type,
            'all_responses': self.all_responses,
            'timestamp': self.timestamp,
        }


class GraeaeClient:
    """Client for Graeae multi-LLM consensus service"""

    def __init__(self, graeae_url: str = "http://192.0.2.67:5001",
                 fallback_on_error: bool = True,
                 timeout_seconds: int = 30,
                 memory_store=None):
        """Initialize Graeae client

        Args:
            graeae_url: URL to Graeae service
            fallback_on_error: Use fallback response on error
            timeout_seconds: Request timeout
            memory_store: MemoryStore for saving consultations
        """
        self.graeae_url = graeae_url
        self.fallback_on_error = fallback_on_error
        self.timeout_seconds = timeout_seconds
        self.memory_store = memory_store
        self._health_checked = False

    async def check_health(self) -> bool:
        """Check if Graeae service is healthy

        Returns:
            True if healthy, False otherwise
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.graeae_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    self._health_checked = (resp.status == 200)
                    logger.debug(f"Graeae health check: {resp.status}")
                    return self._health_checked
        except Exception as e:
            logger.error(f"Graeae health check failed: {e}")
            self._health_checked = False
            return False

    async def consult(self,
                     prompt: str,
                     task_type: str = 'reasoning',
                     context: Optional[str] = None,
                     mode: str = 'auto',
                     muses: Optional[List[str]] = None) -> Optional[ConsultationResult]:
        """Consult Graeae for multi-LLM consensus

        Args:
            prompt: The question/prompt
            task_type: Type of task (reasoning, code, architecture, etc)
            context: Optional context for the prompt
            mode: 'local' ($0), 'external' ($0.04), 'auto' (adaptive)
            muses: Optional list of specific muses to query

        Returns:
            ConsultationResult or None if failed and no fallback
        """
        logger.info(f"Consulting Graeae: {task_type} (mode: {mode})")

        request_body = {
            'prompt': prompt,
            'task_type': task_type,
            'mode': mode,
        }

        if context:
            request_body['context'] = context

        if muses:
            request_body['muses'] = muses

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.graeae_url}/graeae/consult",
                    json=request_body,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.debug(f"Graeae response: {data['winning_muse']}")

                        result = ConsultationResult(
                            consensus_response=data.get('consensus_response', ''),
                            consensus_score=data.get('consensus_score', 0),
                            winning_muse=data.get('winning_muse', 'unknown'),
                            winning_latency_ms=data.get('winning_latency_ms', 0),
                            cost=data.get('cost', 0),
                            mode=data.get('mode', mode),
                            task_type=task_type,
                            all_responses=data.get('all_responses', {}),
                            timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        )

                        # Save to memory store if available
                        if self.memory_store:
                            await self._save_consultation(result, prompt, context)

                        return result
                    else:
                        logger.error(f"Graeae error: {resp.status}")
                        return await self._handle_error(
                            prompt, task_type, context
                        )

        except asyncio.TimeoutError:
            logger.error(f"Graeae timeout after {self.timeout_seconds}s")
            return await self._handle_error(prompt, task_type, context)
        except Exception as e:
            logger.error(f"Graeae error: {e}", exc_info=True)
            return await self._handle_error(prompt, task_type, context)

    async def _handle_error(self, prompt: str, task_type: str,
                          context: Optional[str]) -> Optional[ConsultationResult]:
        """Handle Graeae error with fallback

        Args:
            prompt: Original prompt
            task_type: Task type
            context: Original context

        Returns:
            Fallback ConsultationResult or None
        """
        if not self.fallback_on_error:
            return None

        logger.debug("Using fallback response")

        # Import here to avoid circular imports
        from .fallbacks import get_fallback

        fallback = get_fallback(task_type)
        if not fallback:
            logger.error("No fallback response available")
            return None

        return ConsultationResult(
            consensus_response=fallback['response'],
            consensus_score=0,  # Fallback has no consensus
            winning_muse='fallback',
            winning_latency_ms=0,
            cost=0,
            mode='fallback',
            task_type=task_type,
            all_responses={},
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        )

    async def _save_consultation(self, result: ConsultationResult,
                                prompt: str, context: Optional[str]) -> None:
        """Save consultation to memory store

        Args:
            result: ConsultationResult
            prompt: Original prompt
            context: Optional context
        """
        try:
            if hasattr(self.memory_store, 'save_consultation'):
                await self.memory_store.save_consultation(
                    prompt=prompt,
                    task_type=result.task_type,
                    context_uncompressed=context or '',
                    context_compressed='',  # Will be compressed by memory_store
                    consensus_response=result.consensus_response,
                    consensus_score=result.consensus_score / 100.0,  # Convert to 0-1
                    winning_muse=result.winning_muse,
                    cost=result.cost,
                    latency_ms=result.winning_latency_ms,
                )
                logger.debug("Saved consultation to memory store")
        except Exception as e:
            logger.error(f"Error saving consultation: {e}")

    async def get_stats(self) -> Optional[Dict[str, Any]]:
        """Get Graeae statistics

        Returns:
            Stats dict or None
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.graeae_url}/stats",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception as e:
            logger.error(f"Error getting Graeae stats: {e}")

        return None

    async def batch_consult(self,
                           prompts: List[Dict[str, str]]) -> List[Optional[ConsultationResult]]:
        """Consult on multiple prompts in parallel

        Args:
            prompts: List of dicts with 'prompt', 'task_type', optional 'context'

        Returns:
            List of ConsultationResult or None
        """
        logger.debug(f"Batch consulting on {len(prompts)} prompts")

        tasks = [
            self.consult(
                prompt=p['prompt'],
                task_type=p.get('task_type', 'reasoning'),
                context=p.get('context'),
            )
            for p in prompts
        ]

        return await asyncio.gather(*tasks)
