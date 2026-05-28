"""Optional native vector search helpers.

The Rust extension is an accelerator only. This module keeps a pure-Python
reference path available for hosts that have not built ``mnemos_native_search``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

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


def pure_python_similarity_dot_normalized(
    query: "np.ndarray",
    candidates: "np.ndarray",
) -> "np.ndarray":
    import numpy as np

    query_array = np.asarray(query, dtype=np.float64)
    candidate_array = np.asarray(candidates, dtype=np.float64)
    if query_array.ndim != 1 or candidate_array.ndim != 2:
        raise ValueError("query must be 1-D and candidates must be 2-D")
    if candidate_array.shape[1] != query_array.shape[0]:
        return np.zeros(candidate_array.shape[0], dtype=np.float64)

    query_norm = float(np.linalg.norm(query_array))
    if query_norm == 0.0:
        return np.zeros(candidate_array.shape[0], dtype=np.float64)

    candidate_norms = np.linalg.norm(candidate_array, axis=1)
    denominator = candidate_norms * query_norm
    scores = np.zeros(candidate_array.shape[0], dtype=np.float64)
    np.divide(candidate_array @ query_array, denominator, out=scores, where=denominator != 0.0)
    return scores


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


def similarity_dot_normalized(
    query: "np.ndarray",
    candidates: "np.ndarray",
) -> "np.ndarray":
    if _NATIVE_SEARCH is not None:
        try:
            return _NATIVE_SEARCH.similarity_dot_normalized(query, candidates)
        except Exception:
            pass
    return pure_python_similarity_dot_normalized(query, candidates)


cosine = cosine_similarity
cosine_batch = batch_cosine_similarity
