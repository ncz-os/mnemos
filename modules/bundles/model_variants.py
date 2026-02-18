"""
ModelVariants: Provider model listings and selection

Maps providers to their available models with capabilities.
Supports querying models by:
- Provider
- Capability (reasoning, coding, multimodal)
- Cost tier
- Latency requirements
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ModelInfo:
    """Information about a specific model"""

    name: str
    provider: str
    capabilities: List[str]  # reasoning, coding, multimodal, web_search
    cost_tier: str           # free, budget, standard, premium, custom
    latency_ms: int          # Expected latency
    description: str

    def to_dict(self) -> Dict:
        return {
            'name': self.name,
            'provider': self.provider,
            'capabilities': self.capabilities,
            'cost_tier': self.cost_tier,
            'latency_ms': self.latency_ms,
            'description': self.description,
        }


# Provider model listings
PROVIDER_MODELS = {
    'openai': {
        'gpt-5.2': ModelInfo(
            name='gpt-5.2',
            provider='openai',
            capabilities=['reasoning', 'coding', 'multimodal'],
            cost_tier='premium',
            latency_ms=3000,
            description='Latest OpenAI frontier model',
        ),
        'gpt-5.2-fast': ModelInfo(
            name='gpt-5.2-fast',
            provider='openai',
            capabilities=['reasoning', 'coding'],
            cost_tier='standard',
            latency_ms=1500,
            description='Fast mode variant (lower cost, lower latency)',
        ),
        'gpt-5.2-thinking': ModelInfo(
            name='gpt-5.2-thinking',
            provider='openai',
            capabilities=['reasoning', 'complex_analysis'],
            cost_tier='premium',
            latency_ms=5000,
            description='Thinking mode for deep analysis',
        ),
    },
    'google': {
        'gemini-3-pro': ModelInfo(
            name='gemini-3-pro',
            provider='google',
            capabilities=['reasoning', 'multimodal', 'coding'],
            cost_tier='premium',
            latency_ms=3000,
            description='Latest Gemini 3.0 Pro',
        ),
        'gemini-3-flash': ModelInfo(
            name='gemini-3-flash',
            provider='google',
            capabilities=['reasoning', 'coding'],
            cost_tier='budget',
            latency_ms=1000,
            description='Fast Gemini model (unlimited rate)',
        ),
        'gemini-2.5-flash': ModelInfo(
            name='gemini-2.5-flash',
            provider='google',
            capabilities=['reasoning', 'coding', 'multimodal'],
            cost_tier='budget',
            latency_ms=800,
            description='Gemini 2.5 Flash variant',
        ),
    },
    'xai_grok': {
        'grok-4': ModelInfo(
            name='grok-4',
            provider='xai_grok',
            capabilities=['reasoning', 'real_time', 'code_specialist'],
            cost_tier='premium',
            latency_ms=2500,
            description='Latest Grok-4 with real-time context',
        ),
        'grok-4-code': ModelInfo(
            name='grok-4-code',
            provider='xai_grok',
            capabilities=['coding', 'algorithms', 'optimization'],
            cost_tier='standard',
            latency_ms=2000,
            description='Grok-4 code specialist variant',
        ),
        'grok-4-reasoning': ModelInfo(
            name='grok-4-reasoning',
            provider='xai_grok',
            capabilities=['reasoning', 'analysis', 'real_time'],
            cost_tier='premium',
            latency_ms=3000,
            description='Grok-4 for deep reasoning',
        ),
    },
    'groq': {
        'llama-3.3-70b': ModelInfo(
            name='llama-3.3-70b',
            provider='groq',
            capabilities=['reasoning', 'coding'],
            cost_tier='free',
            latency_ms=800,
            description='Groq Llama 3.3 70B (free tier, fastest)',
        ),
        'llama-2-70b': ModelInfo(
            name='llama-2-70b',
            provider='groq',
            capabilities=['reasoning', 'coding'],
            cost_tier='free',
            latency_ms=900,
            description='Groq Llama 2 70B',
        ),
        'mixtral-8x7b': ModelInfo(
            name='mixtral-8x7b',
            provider='groq',
            capabilities=['reasoning', 'coding'],
            cost_tier='free',
            latency_ms=700,
            description='Groq Mixtral MoE model',
        ),
    },
    'together_ai': {
        'llama-4-405b': ModelInfo(
            name='llama-4-405b',
            provider='together_ai',
            capabilities=['reasoning', 'coding', 'multimodal'],
            cost_tier='budget',
            latency_ms=2000,
            description='Together AI Llama 4 405B (current gen)',
        ),
        'llama-3-70b': ModelInfo(
            name='llama-3-70b',
            provider='together_ai',
            capabilities=['reasoning', 'coding'],
            cost_tier='budget',
            latency_ms=1500,
            description='Together AI Llama 3 70B',
        ),
        'mistral-7b': ModelInfo(
            name='mistral-7b',
            provider='together_ai',
            capabilities=['reasoning', 'coding'],
            cost_tier='free',
            latency_ms=800,
            description='Mistral 7B (lightweight)',
        ),
    },
    'perplexity': {
        'sonar-pro': ModelInfo(
            name='sonar-pro',
            provider='perplexity',
            capabilities=['reasoning', 'web_search', 'citations'],
            cost_tier='standard',
            latency_ms=3000,
            description='Perplexity with web search capability',
        ),
        'sonar-online': ModelInfo(
            name='sonar-online',
            provider='perplexity',
            capabilities=['reasoning', 'web_search', 'real_time'],
            cost_tier='standard',
            latency_ms=3500,
            description='Perplexity online/real-time variant',
        ),
    },
    'local': {
        'mistral-7b-instruct': ModelInfo(
            name='mistral-7b-instruct',
            provider='local',
            capabilities=['reasoning', 'coding'],
            cost_tier='free',
            latency_ms=3000,
            description='Local VLLM Mistral 7B',
        ),
        'deepseek-r1': ModelInfo(
            name='deepseek-r1',
            provider='local',
            capabilities=['reasoning', 'coding', 'math'],
            cost_tier='free',
            latency_ms=4000,
            description='Local Ollama DeepSeek R1',
        ),
    },
}


class ModelVariants:
    """Query and select models by criteria"""

    @staticmethod
    def get_model(provider: str, model_name: str) -> Optional[ModelInfo]:
        """Get specific model

        Args:
            provider: Provider name
            model_name: Model name

        Returns:
            ModelInfo or None
        """
        return PROVIDER_MODELS.get(provider, {}).get(model_name)

    @staticmethod
    def list_provider_models(provider: str) -> List[ModelInfo]:
        """List all models from provider

        Args:
            provider: Provider name

        Returns:
            List of ModelInfo objects
        """
        return list(PROVIDER_MODELS.get(provider, {}).values())

    @staticmethod
    def find_by_capability(capability: str) -> List[ModelInfo]:
        """Find models with capability

        Args:
            capability: Capability name (reasoning, coding, multimodal, etc)

        Returns:
            List of ModelInfo objects
        """
        results = []
        for provider_models in PROVIDER_MODELS.values():
            for model_info in provider_models.values():
                if capability in model_info.capabilities:
                    results.append(model_info)
        return results

    @staticmethod
    def find_by_cost_tier(cost_tier: str) -> List[ModelInfo]:
        """Find models by cost tier

        Args:
            cost_tier: 'free', 'budget', 'standard', 'premium'

        Returns:
            List of ModelInfo objects
        """
        results = []
        for provider_models in PROVIDER_MODELS.values():
            for model_info in provider_models.values():
                if model_info.cost_tier == cost_tier:
                    results.append(model_info)
        return results

    @staticmethod
    def find_fastest(capability: Optional[str] = None) -> List[ModelInfo]:
        """Find fastest models

        Args:
            capability: Filter by capability (optional)

        Returns:
            List of ModelInfo objects sorted by latency (fastest first)
        """
        if capability:
            models = ModelVariants.find_by_capability(capability)
        else:
            models = []
            for provider_models in PROVIDER_MODELS.values():
                models.extend(provider_models.values())

        return sorted(models, key=lambda m: m.latency_ms)

    @staticmethod
    def find_cheapest(capability: Optional[str] = None) -> List[ModelInfo]:
        """Find cheapest models

        Args:
            capability: Filter by capability (optional)

        Returns:
            List of ModelInfo sorted by cost tier
        """
        cost_order = {'free': 0, 'budget': 1, 'standard': 2, 'premium': 3, 'custom': 4}

        if capability:
            models = ModelVariants.find_by_capability(capability)
        else:
            models = []
            for provider_models in PROVIDER_MODELS.values():
                models.extend(provider_models.values())

        return sorted(models, key=lambda m: cost_order.get(m.cost_tier, 5))
