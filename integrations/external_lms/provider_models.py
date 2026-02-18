"""
ProviderModels: Query external LLM provider APIs for available models

Fetches and caches model listings from:
- OpenAI
- Google
- Together AI
- Groq
- Perplexity
- xAI
"""

import logging
import asyncio
import aiohttp
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from functools import lru_cache

logger = logging.getLogger(__name__)


class ProviderModels:
    """Query and manage LLM provider models"""

    # Provider API endpoints
    PROVIDER_ENDPOINTS = {
        'openai': {
            'url': 'https://api.openai.com/v1/models',
            'auth': 'bearer',
        },
        'groq': {
            'url': 'https://api.groq.com/openai/v1/models',
            'auth': 'bearer',
        },
        'together_ai': {
            'url': 'https://api.together.xyz/models',
            'auth': 'bearer',
        },
        'perplexity': {
            'url': 'https://api.perplexity.ai/models',
            'auth': 'bearer',
        },
    }

    def __init__(self, api_keys: Optional[Dict[str, str]] = None):
        """Initialize provider models

        Args:
            api_keys: Dict of provider names to API keys
        """
        self.api_keys = api_keys or {}
        self._cache: Dict[str, tuple] = {}  # (models, timestamp)
        self._cache_ttl_minutes = 60  # Cache for 1 hour

    async def get_provider_models(self, provider: str,
                                 force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Get models from provider

        Args:
            provider: Provider name (openai, groq, together_ai, etc)
            force_refresh: Bypass cache and refresh

        Returns:
            List of model dicts
        """
        logger.debug(f"Getting models from {provider}")

        # Check cache
        if provider in self._cache and not force_refresh:
            models, timestamp = self._cache[provider]
            if datetime.utcnow() - timestamp < timedelta(minutes=self._cache_ttl_minutes):
                logger.debug(f"Using cached models for {provider}")
                return models

        # Query provider API
        models = await self._query_provider(provider)

        if models:
            # Cache results
            self._cache[provider] = (models, datetime.utcnow())
            logger.debug(f"Cached {len(models)} models from {provider}")

        return models

    async def _query_provider(self, provider: str) -> List[Dict[str, Any]]:
        """Query provider API for models

        Args:
            provider: Provider name

        Returns:
            List of model dicts
        """
        if provider not in self.PROVIDER_ENDPOINTS:
            logger.warning(f"Unknown provider: {provider}")
            return []

        endpoint_config = self.PROVIDER_ENDPOINTS[provider]
        url = endpoint_config['url']
        api_key = self.api_keys.get(provider)

        if not api_key:
            logger.debug(f"No API key for {provider}, skipping")
            return []

        try:
            headers = {}
            if endpoint_config['auth'] == 'bearer':
                headers['Authorization'] = f'Bearer {api_key}'

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        # Parse provider-specific response format
                        if provider == 'openai':
                            models = data.get('data', [])
                        elif provider in ['groq', 'together_ai']:
                            models = data.get('data', []) or data
                        elif provider == 'perplexity':
                            models = data.get('models', [])
                        else:
                            models = data if isinstance(data, list) else []

                        logger.debug(f"Found {len(models)} models from {provider}")
                        return models
                    else:
                        logger.error(f"Provider API error ({provider}): {resp.status}")
                        return []

        except asyncio.TimeoutError:
            logger.error(f"Timeout querying {provider}")
            return []
        except Exception as e:
            logger.error(f"Error querying {provider}: {e}", exc_info=True)
            return []

    async def get_all_provider_models(self,
                                     force_refresh: bool = False) -> Dict[str, List[Dict]]:
        """Get models from all providers in parallel

        Args:
            force_refresh: Bypass cache

        Returns:
            Dict of provider → models
        """
        logger.debug("Getting models from all providers")

        tasks = {
            provider: self.get_provider_models(provider, force_refresh)
            for provider in self.PROVIDER_ENDPOINTS.keys()
        }

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        provider_models = {}
        for provider, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"Error getting models from {provider}: {result}")
                provider_models[provider] = []
            else:
                provider_models[provider] = result

        return provider_models

    def extract_model_names(self, provider: str,
                          models: List[Dict]) -> List[str]:
        """Extract model names from provider response

        Args:
            provider: Provider name
            models: Models list from provider

        Returns:
            List of model name strings
        """
        names = []

        for model in models:
            if provider == 'openai':
                # OpenAI uses 'id' field
                if 'id' in model:
                    names.append(model['id'])
            elif provider in ['groq', 'together_ai']:
                # Groq/Together use 'id' or 'name'
                if 'id' in model:
                    names.append(model['id'])
                elif 'name' in model:
                    names.append(model['name'])
            elif provider == 'perplexity':
                # Perplexity uses 'name' or 'model'
                if 'name' in model:
                    names.append(model['name'])
                elif 'model' in model:
                    names.append(model['model'])
            else:
                # Generic fallback
                if isinstance(model, str):
                    names.append(model)
                elif isinstance(model, dict):
                    if 'id' in model:
                        names.append(model['id'])
                    elif 'name' in model:
                        names.append(model['name'])

        return names

    def clear_cache(self, provider: Optional[str] = None) -> None:
        """Clear cached models

        Args:
            provider: Specific provider to clear, or None for all
        """
        if provider:
            self._cache.pop(provider, None)
            logger.debug(f"Cleared cache for {provider}")
        else:
            self._cache.clear()
            logger.debug("Cleared all caches")

    def get_cache_status(self) -> Dict[str, Any]:
        """Get cache status

        Returns:
            Dict with cache info
        """
        status = {}
        now = datetime.utcnow()

        for provider, (models, timestamp) in self._cache.items():
            age = (now - timestamp).total_seconds() / 60  # Minutes
            status[provider] = {
                'cached': True,
                'model_count': len(models),
                'age_minutes': round(age, 1),
                'expired': age > self._cache_ttl_minutes,
            }

        return status


# Global instance for convenience
_global_provider_models = None


async def get_provider_models(provider: str,
                             api_keys: Optional[Dict[str, str]] = None) -> List[Dict]:
    """Get models from provider (convenience function)

    Args:
        provider: Provider name
        api_keys: API keys dict

    Returns:
        List of models
    """
    global _global_provider_models

    if _global_provider_models is None:
        _global_provider_models = ProviderModels(api_keys)
    elif api_keys:
        _global_provider_models.api_keys.update(api_keys)

    return await _global_provider_models.get_provider_models(provider)


async def refresh_provider_models(api_keys: Optional[Dict[str, str]] = None) -> Dict[str, List[Dict]]:
    """Refresh models from all providers (convenience function)

    Args:
        api_keys: API keys dict

    Returns:
        Dict of provider → models
    """
    global _global_provider_models

    if _global_provider_models is None:
        _global_provider_models = ProviderModels(api_keys)
    elif api_keys:
        _global_provider_models.api_keys.update(api_keys)

    return await _global_provider_models.get_all_provider_models(force_refresh=True)
