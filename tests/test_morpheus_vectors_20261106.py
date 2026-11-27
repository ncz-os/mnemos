"""MORPHEUS cosine similarity and vector conversion (FB433, tests only).

Pins the per-pair scoring helper and the ndarray-to-JSON conversion the
clustering path uses. Divergences from the brief are pinned and reported;
source unchanged.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from mnemos.domain.morpheus.runner import _cosine_similarity, _vector_to_float_list


def test_identical_vectors_have_similarity_one() -> None:
    vector = np.array([1.0, 2.0, 3.0])
    assert _cosine_similarity(vector, vector) == pytest.approx(1.0)


def test_orthogonal_vectors_have_similarity_zero() -> None:
    assert _cosine_similarity(np.array([1.0, 0.0]), np.array([0.0, 1.0])) == pytest.approx(0.0)


def test_opposite_vectors_are_negative_one() -> None:
    """DIVERGENCE pinned, not endorsed: similarity ranges over [-1, 1], so an
    opposite embedding answers -1.0, not 0.0."""
    assert _cosine_similarity(np.array([1.0, 2.0]), np.array([-1.0, -2.0])) == pytest.approx(-1.0)


def test_similarity_is_scale_invariant() -> None:
    assert _cosine_similarity(np.array([1.0, 2.0]), np.array([2.0, 4.0])) == pytest.approx(1.0)


def test_a_known_forty_five_degree_value() -> None:
    expected = 1.0 / math.sqrt(2.0)
    assert _cosine_similarity(np.array([1.0, 0.0]), np.array([1.0, 1.0])) == pytest.approx(expected)


def test_similarity_is_symmetric() -> None:
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([3.0, 2.0, 1.0])
    assert _cosine_similarity(a, b) == pytest.approx(_cosine_similarity(b, a))


@pytest.mark.parametrize(
    "a,b",
    [
        (np.zeros(3), np.array([1.0, 2.0, 3.0])),
        (np.array([1.0, 2.0, 3.0]), np.zeros(3)),
        (np.zeros(3), np.zeros(3)),
    ],
    ids=["left-zero", "right-zero", "both-zero"],
)
def test_a_zero_vector_answers_zero_and_not_nan(a: np.ndarray, b: np.ndarray) -> None:
    value = _cosine_similarity(a, b)
    assert value == 0.0
    assert not math.isnan(value)


def test_float32_and_float64_agree() -> None:
    a32 = np.array([0.5, 0.25, 0.125], dtype=np.float32)
    b32 = np.array([0.125, 0.25, 0.5], dtype=np.float32)
    assert _cosine_similarity(a32, b32) == pytest.approx(
        _cosine_similarity(a32.astype(np.float64), b32.astype(np.float64)), rel=1e-6
    )


def test_mismatched_dimensions_raise_instead_of_scoring() -> None:
    """DIVERGENCE pinned, not endorsed: the docstring only promises 0.0 for a
    zero norm; a length mismatch escapes as ValueError from the dot product."""
    with pytest.raises(ValueError):
        _cosine_similarity(np.array([1.0, 2.0]), np.array([1.0, 2.0, 3.0]))


def test_conversion_returns_plain_floats_of_the_same_length() -> None:
    vector = np.array([0.1, 1.0, -2.5], dtype=np.float32)
    converted = _vector_to_float_list(vector)
    assert len(converted) == len(vector)
    assert all(type(value) is float for value in converted)
    assert json.loads(json.dumps(converted)) == converted


def test_float32_precision_is_preserved_as_a_python_float() -> None:
    converted = _vector_to_float_list(np.array([np.float32(0.1)]))
    assert type(converted[0]) is float
    assert converted[0] == pytest.approx(0.1, abs=1e-6)


def test_an_empty_vector_converts_to_an_empty_list() -> None:
    assert _vector_to_float_list(np.array([], dtype=np.float32)) == []


def test_any_iterable_is_accepted_despite_the_ndarray_annotation() -> None:
    """DIVERGENCE pinned, not endorsed: the annotation says ndarray, but the
    comprehension takes any iterable of floats."""
    assert _vector_to_float_list([1, 2.5]) == [1.0, 2.5]
    assert _vector_to_float_list((0.5, 0.25)) == [0.5, 0.25]


def test_a_non_numeric_element_raises_value_error() -> None:
    """Pinned as measured: a string element reaches `float()` and raises
    ValueError (not TypeError) from the comprehension."""
    with pytest.raises(ValueError):
        _vector_to_float_list(np.array(["a"]))


def test_conversion_is_deterministic() -> None:
    vector = np.array([0.3, -0.7, 1.1])
    assert _vector_to_float_list(vector) == _vector_to_float_list(vector)
