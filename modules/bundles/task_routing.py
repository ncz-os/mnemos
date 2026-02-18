"""
BundleRouter: Task type → Bundle selection

Routes tasks to appropriate bundles based on:
- Task type
- Complexity
- Available providers
- Cost constraints
"""

import logging
from typing import Dict, List, Optional
from .bundle_definitions import BundleDefinition, get_bundle, BUNDLES
from .model_variants import ModelVariants, ModelInfo

logger = logging.getLogger(__name__)


class BundleRouter:
    """Routes tasks to consultation bundles"""

    def __init__(self, config: Optional[Dict] = None):
        """Initialize bundle router

        Args:
            config: Configuration dict
        """
        self.config = config or {}
        self.bundles = self.config.get('bundles', {}).get('model_variants', {})

    def select_bundle(self, task_type: str) -> Optional[BundleDefinition]:
        """Select bundle for task type

        Args:
            task_type: Type of task (code_generation, architecture_design, etc)

        Returns:
            BundleDefinition or None if not found
        """
        logger.debug(f"Selecting bundle for task: {task_type}")

        bundle = get_bundle(task_type)
        if not bundle:
            logger.warning(f"No bundle found for task type: {task_type}")
            return None

        logger.debug(f"Selected bundle: {bundle.bundle_type}")
        return bundle

    def select_models(self, task_type: str) -> Dict[str, str]:
        """Get model selection for task type

        Args:
            task_type: Type of task

        Returns:
            Dict of provider → model name
        """
        bundle = self.select_bundle(task_type)
        if not bundle:
            # Default fallback bundle
            logger.debug("Using default reasoning bundle")
            bundle = BUNDLES.get('reasoning')

        if bundle:
            return bundle.models
        return {}

    def get_model_variant(self, provider: str, task_type: str) -> Optional[str]:
        """Get specific model variant for provider and task type

        Args:
            provider: Provider name
            task_type: Task type

        Returns:
            Model name or None
        """
        bundle = self.select_bundle(task_type)
        if bundle and provider in bundle.models:
            return bundle.models[provider]

        # Fallback: get fastest model from provider
        models = ModelVariants.list_provider_models(provider)
        if models:
            models_sorted = sorted(models, key=lambda m: m.latency_ms)
            return models_sorted[0].name

        return None

    def select_primary_model(self, task_type: str) -> Optional[ModelInfo]:
        """Select primary model for task type

        Args:
            task_type: Task type

        Returns:
            ModelInfo or None
        """
        bundle = self.select_bundle(task_type)
        if not bundle:
            return None

        # Prefer OpenAI, then Google, then Groq
        for provider in ['openai', 'google', 'xai_grok', 'groq']:
            if provider in bundle.models:
                model_name = bundle.models[provider]
                model_info = ModelVariants.get_model(provider, model_name)
                if model_info:
                    logger.debug(f"Selected primary: {provider}/{model_name}")
                    return model_info

        return None

    def select_secondary_model(self, task_type: str) -> Optional[ModelInfo]:
        """Select secondary model for task type

        Args:
            task_type: Task type

        Returns:
            ModelInfo or None
        """
        bundle = self.select_bundle(task_type)
        if not bundle:
            return None

        # Get second provider from bundle
        for provider in list(bundle.models.keys())[1:]:
            model_name = bundle.models[provider]
            model_info = ModelVariants.get_model(provider, model_name)
            if model_info:
                logger.debug(f"Selected secondary: {provider}/{model_name}")
                return model_info

        return None

    def select_by_cost(self, task_type: str, cost_constraint: str = 'budget') -> Dict[str, str]:
        """Select models respecting cost constraint

        Args:
            task_type: Task type
            cost_constraint: 'free', 'budget', 'standard', 'premium'

        Returns:
            Dict of provider → model name
        """
        logger.debug(f"Selecting models for {task_type} with cost constraint: {cost_constraint}")

        bundle = self.select_bundle(task_type)
        if not bundle:
            return {}

        result = {}
        cost_priority = ['free', 'budget', 'standard', 'premium']
        max_cost_idx = cost_priority.index(cost_constraint) if cost_constraint in cost_priority else 2

        for provider, model_name in bundle.models.items():
            model_info = ModelVariants.get_model(provider, model_name)
            if model_info:
                cost_idx = cost_priority.index(model_info.cost_tier) if model_info.cost_tier in cost_priority else 2

                if cost_idx <= max_cost_idx:
                    result[provider] = model_name

        if not result:
            # Fallback to cheapest available
            logger.debug("No models within cost constraint, using cheapest available")
            cheapest = ModelVariants.find_cheapest()
            if cheapest:
                result[cheapest[0].provider] = cheapest[0].name

        return result

    def select_by_latency(self, task_type: str, max_latency_ms: int = 3000) -> Dict[str, str]:
        """Select models respecting latency constraint

        Args:
            task_type: Task type
            max_latency_ms: Maximum acceptable latency

        Returns:
            Dict of provider → model name
        """
        logger.debug(f"Selecting models for {task_type} with max latency: {max_latency_ms}ms")

        bundle = self.select_bundle(task_type)
        if not bundle:
            return {}

        result = {}

        for provider, model_name in bundle.models.items():
            model_info = ModelVariants.get_model(provider, model_name)
            if model_info and model_info.latency_ms <= max_latency_ms:
                result[provider] = model_name

        if not result:
            # Fallback to fastest available
            logger.debug("No models within latency constraint, using fastest available")
            fastest = ModelVariants.find_fastest()
            if fastest:
                result[fastest[0].provider] = fastest[0].name

        return result

    def recommend_bundle(self, task_description: str) -> Dict:
        """Get bundle recommendation for task description

        Args:
            task_description: Description of task

        Returns:
            Dict with bundle info and recommendations
        """
        # Simple keyword matching
        desc_lower = task_description.lower()

        task_type = 'reasoning'  # Default

        if any(word in desc_lower for word in ['code', 'implement', 'write', 'function']):
            task_type = 'code_generation'
        elif any(word in desc_lower for word in ['architect', 'design', 'microservice', 'system']):
            task_type = 'architecture_design'
        elif any(word in desc_lower for word in ['api', 'endpoint', 'rest', 'graphql']):
            task_type = 'api_design'
        elif any(word in desc_lower for word in ['database', 'schema', 'model', 'data']):
            task_type = 'data_modeling'
        elif any(word in desc_lower for word in ['debug', 'error', 'issue', 'fix']):
            task_type = 'debugging'
        elif any(word in desc_lower for word in ['refactor', 'optimize', 'improve']):
            task_type = 'refactoring'
        elif any(word in desc_lower for word in ['research', 'find', 'lookup', 'search']):
            task_type = 'research'

        bundle = self.select_bundle(task_type)
        if not bundle:
            bundle = BUNDLES.get('reasoning')

        return {
            'detected_task_type': task_type,
            'bundle': bundle.to_dict() if bundle else None,
            'models': self.select_models(task_type),
            'primary_model': self.select_primary_model(task_type).to_dict() if self.select_primary_model(task_type) else None,
            'secondary_model': self.select_secondary_model(task_type).to_dict() if self.select_secondary_model(task_type) else None,
        }
