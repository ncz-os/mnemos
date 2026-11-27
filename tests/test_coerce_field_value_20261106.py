"""Characterisation of ``_coerce_field_value`` (FB570).

The helper projects a stored model-registry observation onto the column's
Python type, using ``_MODEL_FIELD_TYPES``:

    def _coerce_field_value(field: str, value: str | None) -> Any:
        if value is None: return None
        kind = _MODEL_FIELD_TYPES.get(field)
        if kind == "int":   return int(float(value))
        if kind == "float": return float(value)
        return value

Pinned as implemented:

* ``None`` (known-unknown) stays ``None`` for every field, including numeric
  ones — it never becomes ``0``;
* the integer fields are ``context_window``, ``max_output_tokens`` and
  ``arena_rank``; they go through **float first**, so ``"128000.9"`` truncates
  to ``128000``, scientific notation is accepted, and an integer beyond float's
  53-bit precision **loses digits** (pinned);
* the float fields are ``arena_score`` and ``graeae_weight``; ``"2"`` comes back
  as ``2.0``, and ``"nan"``/``"inf"`` are accepted by ``float`` (the int path
  rejects them: ``int(float("nan"))`` is ``ValueError`` and ``int(float("inf"))``
  is ``OverflowError``);
* the text fields (``display_name``, ``family``, ``alias_of``) and any unknown
  field return the raw value untouched, so ``"007"`` keeps its leading zeros;
* an unparseable number **refuses by raising** ``ValueError`` (from ``float``) —
  it does not fall back to ``None``;
* the function does not type-check its argument: a non-string is passed to
  ``float`` for numeric fields and returned unchanged for text/unknown fields.

The precision-loss and raise-don't-return-None behaviours are pinned by the
tests below.
"""

from __future__ import annotations

import math

import pytest

from mnemos.persistence.sqlite import _MODEL_FIELD_TYPES, _coerce_field_value

INT_FIELDS = ("context_window", "max_output_tokens", "arena_rank")
FLOAT_FIELDS = ("arena_score", "graeae_weight")
TEXT_FIELDS = ("display_name", "family", "alias_of")


# ── the mapping itself ─────────────────────────────────────────────────────


def test_the_documented_field_map_is_exact() -> None:
    # Intentional characterisation pin: the plan scopes FB570 to these eight
    # fields, so widening the map must update this test deliberately.
    assert _MODEL_FIELD_TYPES == {
        "display_name": "text",
        "family": "text",
        "context_window": "int",
        "max_output_tokens": "int",
        "arena_score": "float",
        "arena_rank": "int",
        "graeae_weight": "float",
        "alias_of": "text",
    }


# ── None stays None ────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", INT_FIELDS + FLOAT_FIELDS + TEXT_FIELDS + ("unknown_field",))
def test_none_stays_none_for_every_field(field: str) -> None:
    assert _coerce_field_value(field, None) is None


# ── int fields ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", INT_FIELDS)
def test_int_fields_return_ints(field: str) -> None:
    coerced = _coerce_field_value(field, "128000")

    assert coerced == 128000
    assert type(coerced) is int


def test_an_int_field_truncates_through_float() -> None:
    assert _coerce_field_value("context_window", "128000.9") == 128000
    assert _coerce_field_value("context_window", "-1.9") == -1


def test_an_int_field_accepts_scientific_notation_and_padding() -> None:
    assert _coerce_field_value("context_window", "1e3") == 1000
    assert _coerce_field_value("context_window", "  42  ") == 42


def test_an_int_field_loses_precision_beyond_float_exactness() -> None:
    """DIVERGENCE-adjacent: ``int(float(...))`` cannot keep 53-bit+ integers."""
    assert _coerce_field_value("context_window", "9007199254740993") == 9007199254740992


@pytest.mark.parametrize("text", ["", "abc", "12abc"])
def test_an_int_field_refuses_unparseable_text(text: str) -> None:
    """DIVERGENCE: the refusal is a raised ``ValueError``, not a ``None`` result."""
    with pytest.raises(ValueError):
        _coerce_field_value("context_window", text)


def test_an_int_field_refuses_nan_as_a_value_error() -> None:
    """``float("nan")`` parses; ``int(nan)`` then raises ``ValueError``."""
    with pytest.raises(ValueError):
        _coerce_field_value("context_window", "nan")


def test_an_int_field_overflows_on_infinity() -> None:
    with pytest.raises(OverflowError):
        _coerce_field_value("context_window", "inf")


# ── float fields ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", FLOAT_FIELDS)
def test_float_fields_return_floats(field: str) -> None:
    coerced = _coerce_field_value(field, "2")

    assert coerced == 2.0
    assert type(coerced) is float


def test_a_float_field_keeps_fractional_values() -> None:
    assert _coerce_field_value("arena_score", "1.25") == 1.25
    assert _coerce_field_value("arena_score", "-0.5") == -0.5


def test_a_float_field_accepts_non_finite_text() -> None:
    assert math.isnan(_coerce_field_value("arena_score", "nan"))
    assert math.isinf(_coerce_field_value("graeae_weight", "inf"))


@pytest.mark.parametrize("text", ["", "abc", "1,5"])
def test_a_float_field_refuses_unparseable_text(text: str) -> None:
    with pytest.raises(ValueError):
        _coerce_field_value("arena_score", text)


# ── text and unknown fields: raw passthrough ───────────────────────────────


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_text_fields_are_not_coerced(field: str) -> None:
    assert _coerce_field_value(field, "007") == "007"
    assert _coerce_field_value(field, "1e3") == "1e3"
    assert _coerce_field_value(field, "") == ""


def test_an_unknown_field_returns_the_raw_value() -> None:
    assert _coerce_field_value("unknown_field", "123") == "123"
    assert _coerce_field_value("benchmark_score", "not-a-number") == "not-a-number"


# ── the argument is not type-checked ───────────────────────────────────────


def test_a_non_string_is_handled_by_the_numeric_paths() -> None:
    coerced = _coerce_field_value("context_window", 42)  # type: ignore[arg-type]
    assert coerced == 42
    assert type(coerced) is int

    coerced = _coerce_field_value("arena_score", 2)  # type: ignore[arg-type]
    assert coerced == 2.0
    assert type(coerced) is float

    coerced = _coerce_field_value("context_window", True)  # type: ignore[arg-type]
    assert coerced == 1
    assert type(coerced) is int


def test_a_non_string_passes_through_text_fields_unchanged() -> None:
    sentinel = object()

    assert _coerce_field_value("display_name", sentinel) is sentinel  # type: ignore[arg-type]
