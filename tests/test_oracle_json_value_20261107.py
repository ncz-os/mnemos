"""Characterisation: the oracle JSON value decoder (FB995)."""

from __future__ import annotations

import pytest

from mnemos.persistence.oracle import _json_value


def test_none_and_empty_yield_the_default():
    assert _json_value(None) is None
    assert _json_value("") is None
    assert _json_value("", "fallback") == "fallback"


def test_a_mapping_is_returned_unchanged():
    value = {"a": 1}
    assert _json_value(value) is value


def test_a_list_is_returned_unchanged():
    value = [1, 2]
    assert _json_value(value) is value


def test_json_object_and_list_text_are_parsed():
    assert _json_value('{"a":1}') == {"a": 1}
    assert _json_value("[1,2]") == [1, 2]


def test_bytes_are_decoded_then_parsed():
    assert _json_value(b'["a"]') == ["a"]


def test_a_json_string_scalar_returns_a_str():
    assert _json_value('"hi"') == "hi"


def test_json_numbers_and_booleans_are_typed():
    assert _json_value("5") == 5
    assert isinstance(_json_value("5"), int)
    assert _json_value("true") is True


def test_a_python_number_round_trips_through_str():
    assert _json_value(5) == 5
    assert _json_value(1.5) == 1.5


def test_false_collapses_to_the_default_while_zero_survives():
    assert _json_value(False) is None
    assert _json_value(0) == 0


def test_unparsable_text_yields_the_default():
    assert _json_value("hello") is None
    assert _json_value("hello", "none") == "none"


def test_a_json_null_is_the_default():
    assert _json_value("null") is None


def test_invalid_utf8_raises_instead_of_defaulting():
    with pytest.raises(UnicodeDecodeError):
        _json_value(b"\xff")


def test_nested_structures_are_preserved():
    assert _json_value('{"a":[1,2]}') == {"a": [1, 2]}


def test_the_default_object_is_returned_by_identity():
    sentinel = object()
    assert _json_value("hello", sentinel) is sentinel


def test_the_decoder_is_deterministic():
    assert _json_value('{"a":1}') == _json_value('{"a":1}')
