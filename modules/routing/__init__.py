"""
Routing: Graeae integration and fallback handling

Provides:
- Fallback responses: Embedded responses when GRAEAE is unavailable
"""

from .fallbacks import get_fallback, list_fallbacks

__all__ = [
    'get_fallback',
    'list_fallbacks',
]
