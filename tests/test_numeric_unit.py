"""Unit tests for numeric coercion."""

import math
from decimal import Decimal

import pytest

from mnemos.core.numeric import safe_float


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0.0),
        (0, 0.0),
        (-7, -7.0),
        (True, 1.0),
        (2.5, 2.5),
        (Decimal("3.125"), 3.125),
        ("  -1.25e2  ", -125.0),
        (b"4.5", 4.5),
    ],
)
def test_safe_float_converts_values(value, expected):
    result = safe_float(value)
    assert type(result) is float
    assert result == expected


@pytest.mark.parametrize("value", ["", "not a number", "1,2", object(), [], {}])
def test_safe_float_returns_zero_for_invalid_values(value):
    result = safe_float(value)
    assert type(result) is float
    assert result == 0.0


def test_safe_float_preserves_negative_zero():
    assert math.copysign(1.0, safe_float("-0.0")) == -1.0


def test_safe_float_preserves_nonfinite_values():
    assert math.isnan(safe_float("nan"))
    assert safe_float("inf") == math.inf
    assert safe_float(Decimal("-Infinity")) == -math.inf


def test_safe_float_uses_float_protocol():
    class Floatable:
        def __float__(self):
            return 6.25

    assert safe_float(Floatable()) == 6.25


@pytest.mark.parametrize("error", [TypeError, ValueError])
def test_safe_float_handles_conversion_errors(error):
    class InvalidFloat:
        def __float__(self):
            raise error("invalid")

    assert safe_float(InvalidFloat()) == 0.0


def test_safe_float_does_not_hide_other_errors():
    class BrokenFloat:
        def __float__(self):
            raise RuntimeError("broken")

    with pytest.raises(RuntimeError, match="broken"):
        safe_float(BrokenFloat())

    with pytest.raises(OverflowError):
        safe_float(10**400)
