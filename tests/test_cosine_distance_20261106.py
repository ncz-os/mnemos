"""Characterisation of the pure-Python cosine distance (FB555, tests only).

Pins ``_cosine_distance_python`` in ``mnemos/persistence/hot_search.py``:
the 1 - cosine-similarity definition, the zero-norm guard at ``1e-9``, the
documented maximum of 2.0, and the unvalidated length mismatch (dot truncates
through ``zip`` while the norms do not).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from mnemos.persistence.hot_search import _cosine_distance_python


def _reference(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


def test_identical_vectors_have_zero_distance() -> None:
    assert _cosine_distance_python([3.0, 4.0], [3.0, 4.0]) == pytest.approx(0.0, abs=1e-12)


def test_orthogonal_vectors_have_distance_one() -> None:
    assert _cosine_distance_python([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0, abs=1e-12)


def test_opposite_vectors_reach_the_documented_maximum() -> None:
    # Cosine distance spans [0, 2]; there is no clamp to 1.
    assert _cosine_distance_python([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(2.0, abs=1e-12)


def test_zero_vector_returns_one_not_nan() -> None:
    result = _cosine_distance_python([0.0, 0.0], [1.0, 2.0])

    assert result == 1.0
    assert not math.isnan(result)


def test_both_zero_vectors_return_one() -> None:
    assert _cosine_distance_python([0.0, 0.0], [0.0, 0.0]) == 1.0


def test_norm_below_the_threshold_returns_one() -> None:
    assert _cosine_distance_python([1e-10, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine_distance_python([0.0, 1e-10], [0.0, 1.0]) == 1.0


def test_norm_just_above_the_threshold_still_computes() -> None:
    # 1e-8 is not < 1e-9, so the guard does not fire and identical tiny vectors give 0.
    assert _cosine_distance_python([1e-8, 0.0], [1e-8, 0.0]) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]),
        ([0.5, -0.25, 0.75], [-0.1, 0.2, -0.3]),
        ([1, 2, 3], [3, 2, 1]),
    ],
)
def test_matches_a_reference_implementation(a: list[float], b: list[float]) -> None:
    assert _cosine_distance_python(a, b) == pytest.approx(_reference(a, b), abs=1e-12)


def test_is_symmetric_for_equal_lengths() -> None:
    a = [0.3, -1.2, 2.5, 0.0]
    b = [1.1, 0.4, -0.7, 3.0]

    assert _cosine_distance_python(a, b) == pytest.approx(_cosine_distance_python(b, a), abs=1e-12)


def test_mismatched_lengths_are_not_validated() -> None:
    # dot truncates to the shorter sequence via zip; both norms use the full
    # sequences, so the value differs from a like-for-like comparison.
    result = _cosine_distance_python([1.0, 0.0], [1.0, 0.0, 1.0])

    assert result == pytest.approx(1.0 - 1.0 / math.sqrt(2.0), abs=1e-12)
    assert result != pytest.approx(0.0, abs=1e-9)


def test_empty_sequences_return_one() -> None:
    assert _cosine_distance_python([], []) == 1.0
    assert _cosine_distance_python([1.0], []) == 1.0
    assert _cosine_distance_python([], [1.0]) == 1.0


def test_integer_inputs_are_accepted_and_return_float() -> None:
    result = _cosine_distance_python([1, 2], [2, 4])

    assert isinstance(result, float)
    assert result == pytest.approx(0.0, abs=1e-12)


def test_inputs_are_not_mutated() -> None:
    a = [1.0, 2.0]
    b = [3.0, 4.0]

    _cosine_distance_python(a, b)

    assert a == [1.0, 2.0]
    assert b == [3.0, 4.0]
