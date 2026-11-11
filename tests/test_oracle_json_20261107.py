"""Characterisation: the Oracle json codecs (FB972)."""

from __future__ import annotations

import math

import pytest

from mnemos.persistence.oracle import _json_text, _json_value


def test_text_is_compact_json():
    assert _json_text({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'
    assert _json_text([1, 2]) == "[1,2]"
    assert _json_text("x") == '"x"'
    assert " " not in _json_text({"a": 1})


def test_text_keeps_insertion_order():
    assert _json_text({"b": 1, "a": 2}) == '{"b":1,"a":2}'


def test_text_of_none_is_the_default():
    assert _json_text(None) == "null"
    assert _json_text(None, []) == "[]"
    assert _json_text(None, {"k": None}) == '{"k":null}'


def test_text_of_an_empty_string_is_not_the_default():
    assert _json_text("") == '""'


def test_text_escapes_non_ascii():
    assert _json_text("héllo") == '"h\\u00e9llo"'
    assert _json_text("héllo").isascii()


def test_tuples_become_arrays():
    assert _json_text((1, 2)) == "[1,2]"


def test_unserialisable_values_raise():
    with pytest.raises(TypeError):
        _json_text({1, 2})
    with pytest.raises(TypeError):
        _json_text(object())


def test_value_parses_scalars_and_containers():
    assert _json_value('{"a":1}') == {"a": 1}
    assert _json_value("[1,2]") == [1, 2]
    assert _json_value("123") == 123
    assert _json_value("null") is None
    assert _json_value("false") is False


def test_value_of_none_or_empty_is_the_default():
    assert _json_value(None) is None
    assert _json_value("", "fallback") == "fallback"
    assert _json_value(None, "fallback") == "fallback"


def test_value_passes_containers_through_by_identity():
    mapping = {"a": 1}
    sequence = [1, 2]
    assert _json_value(mapping) is mapping
    assert _json_value(sequence) is sequence


def test_value_decodes_bytes():
    assert _json_value(b'{"a":1}') == {"a": 1}


def test_value_of_malformed_text_is_the_default():
    for bad in ("{", "not json", "undefined", "'single'"):
        assert _json_value(bad, "fallback") == "fallback"


def test_value_accepts_the_python_non_finite_literals():
    assert math.isnan(_json_value("NaN"))
    assert _json_value("Infinity") == float("inf")
    assert _json_value("-Infinity") == float("-inf")


def test_value_coerces_other_values_through_str():
    assert _json_value(123) == 123
    assert _json_value("true") is True
    assert _json_value(True) is None
    assert _json_value(False, "fallback") == "fallback"


def test_value_of_a_bytearray_is_the_default():
    assert _json_value(bytearray(b'{"a":1}'), "fallback") == "fallback"


def test_the_pair_round_trips_compact_text():
    text = '{"a":1,"b":[1,2]}'
    assert _json_text(_json_value(text)) == text


def test_re_encoding_drops_whitespace():
    assert _json_value('{"a": 1}') == {"a": 1}
    assert _json_text(_json_value('{"a": 1}')) == '{"a":1}'


def test_the_sqlite_sibling_agrees_on_values():
    sqlite = pytest.importorskip("mnemos.persistence.sqlite")
    for raw in ('{"a":1}', "[1,2]", "null", "", "not json", 123):
        assert sqlite._json_value(raw) == _json_value(raw)


def test_the_sqlite_sibling_encodes_text_differently():
    sqlite = pytest.importorskip("mnemos.persistence.sqlite")
    assert _json_text({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'
    assert sqlite._json_text({"a": 1, "b": [1, 2]}) == '{"a": 1, "b": [1, 2]}'
    assert _json_text("null") == '"null"'
    assert sqlite._json_text("null") == "null"
