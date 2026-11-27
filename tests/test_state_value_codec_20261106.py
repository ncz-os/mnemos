"""FB460 — state value encoding for ``mnemos.domain.memory_categorization.state``.

Pins ``_wrap_state_value`` and ``_decode_state_value`` on ``master`` (``22a3f7d8``).

Pinned divergences, not endorsed: nothing raises on malformed input — the decoder
swallows ``JSONDecodeError``/``TypeError`` and returns the raw string, so a corrupt
envelope and a legitimate opaque row look identical at this layer; encoding is
deterministic per value but NOT canonical, since ``json.dumps`` keeps insertion
order and equal dicts built in different orders encode differently; and a JSON
string literal on the non-envelope path decodes to raw text WITH its quotes.

Pure functions, no database, no network.
"""

from __future__ import annotations

import json

import pytest

from mnemos.domain.memory_categorization.state import _decode_state_value, _wrap_state_value

PREFIX = "MNEMOS_SM:v1:"


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1, "b": [1, 2, 3]},
        [1, "two", None, {"three": 3}],
        42,
        -7,
        0,
        3.5,
        True,
        False,
        None,
        "plain text",
        "héllo ☃",
        "",
        {"nested": {"deep": [{"x": None}]}},
        [],
        {},
    ],
)
def test_values_round_trip_exactly(value):
    assert _decode_state_value(_wrap_state_value(value)) == value


def test_the_envelope_is_the_prefix_plus_json():
    assert _wrap_state_value({"a": 1}) == PREFIX + json.dumps({"a": 1})
    assert _wrap_state_value("x") == PREFIX + '"x"'
    assert _wrap_state_value(None) == PREFIX + "null"


def test_encoding_is_deterministic():
    value = {"b": 2, "a": 1}
    assert _wrap_state_value(value) == _wrap_state_value(value) == PREFIX + '{"b": 2, "a": 1}'


def test_a_wrapped_looking_string_is_not_double_decoded():
    """Pinned: the wrapper is applied once, so the inner text stays a string."""
    inner = PREFIX + '{"a": 1}'
    encoded = _wrap_state_value(inner)
    assert encoded == PREFIX + json.dumps(inner)
    assert _decode_state_value(encoded) == inner
    assert isinstance(_decode_state_value(encoded), str)


def test_a_string_that_is_a_json_object_stays_a_string():
    raw = '{"a": 1}'
    assert _decode_state_value(_wrap_state_value(raw)) == raw


def test_none_passes_through():
    assert _decode_state_value(None) is None


@pytest.mark.parametrize("raw", [42, 3.5, True, False, {"a": 1}, [1, 2], b"bytes", object()])
def test_non_strings_pass_through_unchanged(raw):
    assert _decode_state_value(raw) is raw


def test_a_malformed_envelope_returns_raw():
    """Pinned divergence: corrupt data is surfaced verbatim, nothing raises."""
    raw = PREFIX + "{not json"
    assert _decode_state_value(raw) == raw


def test_an_empty_envelope_returns_raw():
    raw = PREFIX
    assert _decode_state_value(raw) == raw


def test_a_non_envelope_non_json_string_returns_raw():
    assert _decode_state_value("legacy opaque text") == "legacy opaque text"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("[1, 2, 3]", [1, 2, 3]),
        ("[]", []),
        ("{}", {}),
        ('{"nested": {"x": [true, null]}}', {"nested": {"x": [True, None]}}),
    ],
)
def test_json_containers_decode(raw, expected):
    assert _decode_state_value(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", "true"),
        ("false", "false"),
        ("null", "null"),
        ("42", "42"),
        ("3.5", "3.5"),
        ('"quoted"', '"quoted"'),
        ('"null"', '"null"'),
    ],
)
def test_json_scalars_return_raw_text(raw, expected):
    """Pinned: only objects and arrays decode; scalars keep their raw bytes."""
    assert _decode_state_value(raw) == expected


def test_a_legacy_null_string_stays_distinct_from_a_missing_row():
    assert _decode_state_value("null") == "null"
    assert _decode_state_value(None) is None


def test_an_envelope_scalar_decodes_to_the_python_value():
    """The envelope path is unambiguous, so scalars DO decode there."""
    assert _decode_state_value(_wrap_state_value(True)) is True
    assert _decode_state_value(_wrap_state_value(42)) == 42
    assert _decode_state_value(_wrap_state_value(None)) is None


def test_equal_dicts_in_different_orders_encode_differently():
    """Pinned divergence: deterministic, not canonical."""
    one = _wrap_state_value({"a": 1, "b": 2})
    two = _wrap_state_value({"b": 2, "a": 1})
    assert one != two
    assert json.loads(one[len(PREFIX) :]) == json.loads(two[len(PREFIX) :])


def test_tuple_collapses_to_list_through_the_envelope():
    assert _decode_state_value(_wrap_state_value((1, 2))) == [1, 2]


def test_integer_dict_keys_become_strings_through_the_envelope():
    assert _decode_state_value(_wrap_state_value({1: "one"})) == {"1": "one"}


def test_a_non_serialisable_value_raises_type_error():
    with pytest.raises(TypeError):
        _wrap_state_value({1, 2, 3})
