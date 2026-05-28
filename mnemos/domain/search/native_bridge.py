"""Optional native vector search helpers.

The Rust extension is an accelerator only. This module keeps a pure-Python
reference path available for hosts that have not built ``mnemos_native_search``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

try:
    import mnemos_native_search as _NATIVE_SEARCH  # type: ignore[import-not-found]
except ImportError:
    _NATIVE_SEARCH = None

NATIVE_AVAILABLE = _NATIVE_SEARCH is not None


def _native_ready(value: Any) -> bool:
    return isinstance(value, Sequence) or hasattr(value, "__array_interface__")


def _to_float_list(vector: Iterable[Any]) -> list[float]:
    return [float(value) for value in vector]


def pure_python_cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(left * right for left, right in zip(a, b))
    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def pure_python_batch_cosine_similarity(
    query: Sequence[float],
    corpus: Sequence[Sequence[float]],
) -> list[float]:
    return [pure_python_cosine_similarity(query, candidate) for candidate in corpus]


def cosine_similarity(a: Iterable[Any], b: Iterable[Any]) -> float:
    if _NATIVE_SEARCH is not None:
        if _native_ready(a) and _native_ready(b):
            try:
                return float(_NATIVE_SEARCH.cosine_similarity(a, b))
            except Exception:
                pass
        else:
            left = _to_float_list(a)
            right = _to_float_list(b)
            try:
                return float(_NATIVE_SEARCH.cosine_similarity(left, right))
            except Exception:
                return pure_python_cosine_similarity(left, right)
    left = _to_float_list(a)
    right = _to_float_list(b)
    return pure_python_cosine_similarity(left, right)


def batch_cosine_similarity(
    query: Iterable[Any],
    corpus: Iterable[Iterable[Any]],
) -> list[float]:
    if _NATIVE_SEARCH is not None:
        if _native_ready(query) and _native_ready(corpus):
            try:
                return [float(score) for score in _NATIVE_SEARCH.batch_cosine_similarity(query, corpus)]
            except Exception:
                pass
        else:
            query_values = _to_float_list(query)
            corpus_values = [_to_float_list(candidate) for candidate in corpus]
            try:
                return [float(score) for score in _NATIVE_SEARCH.batch_cosine_similarity(query_values, corpus_values)]
            except Exception:
                return pure_python_batch_cosine_similarity(query_values, corpus_values)
    query_values = _to_float_list(query)
    corpus_values = [_to_float_list(candidate) for candidate in corpus]
    return pure_python_batch_cosine_similarity(query_values, corpus_values)


cosine = cosine_similarity
cosine_batch = batch_cosine_similarity
