#!/usr/bin/env python3
"""
GRAEAE Multi-Provider Consensus Engine
Queries multiple AI providers for reasoning and consensus scoring.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

from graeae.api_keys import get_key

logger = logging.getLogger(__name__)


@dataclass
class ProviderResponse:
    provider: str
    status: str
    response_text: str
    latency_ms: int
    model_id: str
    final_score: float = 0.0


class GraeaeEngine:
    """Multi-provider consensus reasoning engine"""

    def __init__(self):
        # key_name maps to the provider name in ~/.api_keys_master.json llm_providers
        self.providers = {
            "perplexity": {
                "url": "https://api.perplexity.ai/chat/completions",
                "model": "sonar-pro",
                "weight": 0.88,
                "api_type": "openai",
                "key_name": "perplexity",
            },
            "groq": {
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "model": "llama-3.3-70b-versatile",
                "weight": 0.63,
                "api_type": "openai",
                "key_name": "groq",
            },
            "claude-opus": {
                "url": "https://api.anthropic.com/v1/messages",
                "model": "claude-opus-4-6",
                "weight": 0.85,
                "api_type": "anthropic",
                "key_name": "claude-opus",
            },
            "xai": {
                "url": "https://api.x.ai/v1/chat/completions",
                "model": "grok-3",
                "weight": 0.48,
                "api_type": "openai",
                "key_name": "xai",
            },
            "openai": {
                "url": "https://api.openai.com/v1/chat/completions",
                "model": "gpt-5.2",
                "weight": 0.82,
                "api_type": "openai_gpt5",
                "key_name": "openai",
            },
            "gemini": {
                "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent",
                "model": "gemini-3.1-pro-preview",
                "weight": 0.81,
                "api_type": "gemini",
                "key_name": "gemini",
            },
        }
        # Shared client — reused across all provider calls to avoid per-request overhead
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def consult(self, prompt: str, task_type: str = "reasoning", timeout: int = 30) -> Dict:
        """Query all providers in parallel and return all responses."""
        tasks = [
            self._query_provider(name, prompt, task_type, timeout)
            for name in self.providers
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        all_responses: Dict = {}
        for provider_name, response in zip(self.providers.keys(), responses):
            if isinstance(response, Exception):
                all_responses[provider_name] = {
                    "status": "error",
                    "response_text": f"Error: {response}",
                    "latency_ms": 0,
                    "model_id": self.providers[provider_name]["model"],
                    "final_score": 0.0,
                }
            else:
                all_responses[provider_name] = response

        return {"all_responses": all_responses}

    async def _query_provider(
        self, provider_name: str, prompt: str, task_type: str, timeout: int
    ) -> Dict:
        provider = self.providers[provider_name]
        try:
            start = datetime.now(timezone.utc)
            api_type = provider["api_type"]

            if api_type in ("openai", "openai_gpt5"):
                response = await self._query_openai_compatible(provider, prompt, timeout)
            elif api_type == "anthropic":
                response = await self._query_anthropic(provider, prompt, timeout)
            elif api_type == "gemini":
                response = await self._query_gemini(provider, prompt, timeout)
            else:
                return {
                    "status": "error",
                    "response_text": f"Unknown API type: {api_type}",
                    "latency_ms": 0,
                    "model_id": provider["model"],
                    "final_score": 0.0,
                }

            latency = (datetime.now(timezone.utc) - start).total_seconds() * 1000
            response["latency_ms"] = int(latency)
            response["final_score"] = provider["weight"]
            return response
        except Exception as e:
            logger.error(f"Error querying {provider_name}: {e}")
            return {
                "status": "error",
                "response_text": f"Error: {e}",
                "latency_ms": 0,
                "model_id": provider["model"],
                "final_score": 0.0,
            }

    async def _query_openai_compatible(
        self, provider: Dict, prompt: str, timeout: int
    ) -> Dict:
        """Query OpenAI-compatible APIs (Perplexity, Groq, xAI, OpenAI).

        GPT-5 series uses max_completion_tokens; all others use max_tokens.
        """
        api_key = get_key(provider["key_name"])
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        tokens_key = "max_completion_tokens" if provider["api_type"] == "openai_gpt5" else "max_tokens"
        payload = {
            "model": provider["model"],
            "messages": [{"role": "user", "content": prompt}],
            tokens_key: 2000,
            "temperature": 0.7,
        }

        client = await self._get_client()
        resp = await client.post(provider["url"], json=payload, headers=headers, timeout=timeout)

        if resp.status_code != 200:
            return {
                "status": "error",
                "response_text": f"HTTP {resp.status_code}: {resp.text[:500]}",
                "latency_ms": 0,
                "model_id": provider["model"],
            }

        data = resp.json()
        return {
            "status": "success",
            "response_text": data["choices"][0]["message"]["content"],
            "latency_ms": 0,
            "model_id": provider["model"],
        }

    async def _query_anthropic(self, provider: Dict, prompt: str, timeout: int) -> Dict:
        """Query Anthropic Claude API"""
        api_key = get_key(provider["key_name"])
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": provider["model"],
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }

        client = await self._get_client()
        resp = await client.post(provider["url"], json=payload, headers=headers, timeout=timeout)

        if resp.status_code != 200:
            return {
                "status": "error",
                "response_text": f"HTTP {resp.status_code}: {resp.text[:500]}",
                "latency_ms": 0,
                "model_id": provider["model"],
            }

        data = resp.json()
        return {
            "status": "success",
            "response_text": data["content"][0]["text"],
            "latency_ms": 0,
            "model_id": provider["model"],
        }

    async def _query_gemini(self, provider: Dict, prompt: str, timeout: int) -> Dict:
        """Query Google Gemini API"""
        api_key = get_key(provider["key_name"])
        url = provider["url"]  # key sent in header, not query string
        headers = {"x-goog-api-key": api_key}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.7},
        }

        client = await self._get_client()
        resp = await client.post(url, headers=headers, json=payload, timeout=timeout)

        if resp.status_code != 200:
            return {
                "status": "error",
                "response_text": f"HTTP {resp.status_code}: {resp.text[:500]}",
                "latency_ms": 0,
                "model_id": provider["model"],
            }

        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            response_text = parts[0].get("text", "No text in response") if parts else f"No content: {candidates[0]}"
        else:
            response_text = f"No candidates: {data}"

        return {
            "status": "success",
            "response_text": response_text,
            "latency_ms": 0,
            "model_id": provider["model"],
        }


# Module-level singleton
_graeae_engine: Optional[GraeaeEngine] = None


def get_graeae_engine() -> GraeaeEngine:
    global _graeae_engine
    if _graeae_engine is None:
        _graeae_engine = GraeaeEngine()
    return _graeae_engine

