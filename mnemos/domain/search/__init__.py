"""v6.2 M-2.2.3 retrieval-profile dispatcher.

Public surface:

    from mnemos.domain.search import (
        SearchProfile,
        resolve_profile,
        get_reranker,
    )

`SearchProfile` enumerates the three pipelines (fast / balanced / deep).
`resolve_profile(raw)` validates the caller value and returns the enum
(or raises ValueError for unknown values; route handler maps that to
400). `get_reranker()` returns a cached singleton `Reranker` HTTP client
keyed on env config.
"""

from __future__ import annotations

from .profile import SearchProfile, resolve_profile
from .reranker import Reranker, get_reranker

__all__ = ["SearchProfile", "resolve_profile", "Reranker", "get_reranker"]
