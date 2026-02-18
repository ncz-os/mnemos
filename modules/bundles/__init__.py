"""
Consultation Bundles: Task-specific model variant selection

Provides:
- BundleDefinition: Bundle configuration
- ModelVariants: Provider → model mappings
- BundleRouter: Task type → Bundle selector
"""

from .bundle_definitions import BundleDefinition, BUNDLES
from .model_variants import ModelVariants, PROVIDER_MODELS
from .task_routing import BundleRouter

__all__ = [
    'BundleDefinition',
    'BUNDLES',
    'ModelVariants',
    'PROVIDER_MODELS',
    'BundleRouter',
]
