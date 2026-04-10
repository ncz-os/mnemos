"""
Routing: Graeae integration and fallback handling

Provides:
- GraeaeClient: HTTP client for Graeae service
- Fallback bundles: Embedded responses when Graeae unavailable
- Consultation result storage
"""

from .fallbacks import get_fallback, list_fallbacks

__all__ = [
    'GraeaeClient',
    'ConsultationResult',
    'get_fallback',
    'list_fallbacks',
]
