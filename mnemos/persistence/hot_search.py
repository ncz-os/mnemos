"""Shared optional acceleration for persistence-side cosine search."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from types import ModuleType
from typing import Any

from mnemos.core.config import hot_rs_enabled
from mnemos.core.native_accel import load_hot_rs

logger = logging.getLogger(__name__)

_HOT_RS: ModuleType | None = None
_HOT_RS_LOAD_ATTEMPTED = False


def _get_hot_rs() -> ModuleType | None:
    """Load the opt-in accelerator once, on the first fallback search."""
    global _HOT_RS, _HOT_RS_LOAD_ATTEMPTED

    if _HOT_RS is not None:
        return _HOT_RS
    if not _HOT_RS_LOAD_ATTEMPTED:
        _HOT_RS_LOAD_ATTEMPTED = True
        if hot_rs_enabled():
            _HOT_RS = load_hot_rs(logger, "MySQL/MariaDB cosine search")
    return _HOT_RS


def _cosine_distance_python(a: Sequence[float], b: Sequence[float]) -> float:
    """Pure-Python cosine distance (1 - cosine similarity)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


class HotSearchMixin:
    """Shared Rust-accelerated cosine ranking with a Python fallback."""

    def _cosine_rank_rows(
        self,
        query_vec: Sequence[float],
        rows: Sequence[Mapping[str, Any]],
        embedding_key: str,
        *,
        extract_embedding: Callable[[Any], Sequence[float] | None] | None = None,
    ) -> list[float]:
        """Return one cosine distance per row, preserving input row order."""
        extract = extract_embedding or (lambda value: value)
        distances = [1.0] * len(rows)
        valid_indices: list[int] = []
        embeddings: list[Sequence[float]] = []

        for index, row in enumerate(rows):
            try:
                embedding = extract(row.get(embedding_key))
            except (TypeError, ValueError):
                continue
            if embedding:
                valid_indices.append(index)
                embeddings.append(embedding)

        hot_rs = _get_hot_rs()
        if hot_rs is not None and embeddings:
            try:
                batch_cosine = None
                if hasattr(hot_rs, "batch_cosine_similarity"):
                    batch_cosine = hot_rs.batch_cosine_similarity
                elif hasattr(hot_rs, "cosine_batch"):
                    batch_cosine = hot_rs.cosine_batch
                if batch_cosine is not None:
                    similarities = list(batch_cosine(query_vec, embeddings))
                    if len(similarities) != len(embeddings):
                        raise ValueError("native cosine batch returned the wrong number of scores")
                    for index, similarity in zip(valid_indices, similarities):
                        distances[index] = 1.0 - float(similarity)
                    return distances
            except Exception:
                pass

        for index, embedding in zip(valid_indices, embeddings):
            try:
                distances[index] = _cosine_distance_python(query_vec, embedding)
            except (TypeError, ValueError):
                distances[index] = 1.0
        return distances
