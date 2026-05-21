"""Driver-free unit tests for ``_validate_and_format_vector``.

These tests cover the Oracle eng O5 finding: every site that builds a
``TO_VECTOR`` literal must reject empty / NaN / Inf / dim-cap-exceeded
inputs before the literal hits the cursor. The helper lives in
:mod:`mnemos.persistence.oracle` and is also imported by
:mod:`mnemos.persistence.db2` so both vector-search code paths share
one rejection contract.

This file deliberately avoids importing ``oracledb`` — the helper is
pure-Python and runs in any test environment.
"""

from __future__ import annotations

import math
import os
from contextlib import contextmanager

import pytest

from mnemos.persistence.oracle import _validate_and_format_vector


@contextmanager
def _env(name: str, value: str | None):
    """Temporarily set / unset an env var, restoring the prior state."""
    prior = os.environ.get(name)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if prior is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


def test_empty_embedding_raises_value_error() -> None:
    with pytest.raises(ValueError, match="empty"):
        _validate_and_format_vector([])


def test_none_embedding_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _validate_and_format_vector(None)  # type: ignore[arg-type]


def test_nan_element_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        _validate_and_format_vector([0.1, math.nan, 0.3])


def test_positive_inf_element_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        _validate_and_format_vector([0.1, math.inf, 0.3])


def test_negative_inf_element_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        _validate_and_format_vector([0.1, -math.inf, 0.3])


def test_mismatched_dim_raises_value_error() -> None:
    with pytest.raises(ValueError, match="dimensionality mismatch"):
        _validate_and_format_vector([0.1, 0.2, 0.3], expected_dim=4)


def test_matched_dim_returns_literal() -> None:
    literal = _validate_and_format_vector([0.1, 0.2, 0.3], expected_dim=3)
    assert literal == "[0.1000000,0.2000000,0.3000000]"


def test_exceeds_dim_cap_raises_value_error() -> None:
    # Tighten the cap so we can prove the gate without allocating a
    # 4097-float embedding.
    with _env("MNEMOS_VECTOR_DIM_MAX", "4"):
        with pytest.raises(ValueError, match="exceeds MNEMOS_VECTOR_DIM_MAX"):
            _validate_and_format_vector([0.1, 0.2, 0.3, 0.4, 0.5])


def test_at_dim_cap_returns_literal() -> None:
    with _env("MNEMOS_VECTOR_DIM_MAX", "3"):
        literal = _validate_and_format_vector([0.1, 0.2, 0.3])
        assert literal == "[0.1000000,0.2000000,0.3000000]"


def test_valid_embedding_returns_expected_format() -> None:
    literal = _validate_and_format_vector([1.0, -0.5, 0.25])
    assert literal.startswith("[")
    assert literal.endswith("]")
    assert literal == "[1.0000000,-0.5000000,0.2500000]"


def test_non_float_convertible_element_raises_value_error() -> None:
    with pytest.raises(ValueError, match="not float-convertible"):
        _validate_and_format_vector([0.1, "not-a-number", 0.3])  # type: ignore[list-item]


def test_unparsable_env_var_falls_back_to_default() -> None:
    # Garbage env var must not crash — falls back to default cap (4096).
    # Build a 5-element embedding that is well under the default cap.
    with _env("MNEMOS_VECTOR_DIM_MAX", "not-a-number"):
        literal = _validate_and_format_vector([0.1, 0.2, 0.3, 0.4, 0.5])
        assert literal.startswith("[")


def test_negative_env_var_falls_back_to_default() -> None:
    # Defensive: zero / negative caps would deny every call; the helper
    # falls back to the default cap rather than blocking the backend.
    with _env("MNEMOS_VECTOR_DIM_MAX", "-1"):
        literal = _validate_and_format_vector([0.1, 0.2, 0.3])
        assert literal.startswith("[")
