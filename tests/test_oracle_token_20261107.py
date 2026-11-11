"""Characterisation: the Oracle token encoding (FB971)."""

from __future__ import annotations

import pytest

from mnemos.persistence.oracle import _raw_token, _raw_token_text

HEX = "ab" * 32
RAW = bytes.fromhex(HEX)
PLAIN = "pass" * 8
PLAIN_RAW = PLAIN.encode("utf-8")


def test_raw_bytes_pass_through_by_identity():
    assert _raw_token(RAW) is RAW
    assert len(_raw_token(RAW)) == 32


def test_sixty_four_hex_characters_become_thirty_two_bytes():
    assert _raw_token(HEX) == RAW
    assert _raw_token(HEX.upper()) == RAW


def test_the_default_length_is_thirty_two():
    assert len(_raw_token(HEX)) == 32
    assert len(_raw_token(PLAIN)) == 32


def test_a_plain_token_is_stored_as_its_text():
    assert _raw_token(PLAIN) == PLAIN_RAW
    assert PLAIN.encode("utf-8") in _raw_token(PLAIN)


def test_a_wrong_width_is_refused():
    for value in (b"x" * 31, b"x" * 33, "x" * 63, "z" * 64):
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            _raw_token(value)


def test_a_custom_length_is_honoured():
    assert len(_raw_token("ab" * 16, length=16)) == 16
    with pytest.raises(ValueError, match="exactly 16 bytes"):
        _raw_token(b"x" * 32, length=16)


def test_a_thirty_two_character_hex_string_decodes_to_sixteen_and_is_refused():
    sixteen = "ab" * 16
    assert len(bytes.fromhex(sixteen)) == 16
    with pytest.raises(ValueError):
        _raw_token(sixteen)


def test_none_is_not_special_cased_on_the_way_in():
    with pytest.raises(ValueError):
        _raw_token(None)
    assert _raw_token_text(None) is None


def test_the_text_form_is_lowercase_hex():
    text = _raw_token_text(RAW)
    assert text == HEX
    assert len(text) == 64
    assert text == text.lower()


def test_the_pair_round_trips():
    assert _raw_token(_raw_token_text(RAW)) == RAW


def test_the_encoding_is_reversible():
    raw = _raw_token(PLAIN)
    assert raw == PLAIN_RAW
    assert bytes.fromhex(_raw_token_text(raw)) == PLAIN_RAW


def test_fromhex_ignores_spaces():
    spaced = " ".join(HEX[index : index + 2] for index in range(0, 64, 2))
    assert len(spaced) == 95
    assert _raw_token(spaced) == RAW


def test_bytearray_and_list_are_accepted_on_the_way_back():
    assert _raw_token_text(bytearray(RAW)) == HEX
    assert _raw_token_text(list(RAW)) == HEX


def test_a_string_on_the_way_back_raises_type_error():
    with pytest.raises(TypeError):
        _raw_token_text(PLAIN)


def test_the_raw_value_is_thirty_two_bytes_for_every_accepted_form():
    for value in (RAW, HEX, HEX.upper(), PLAIN):
        assert len(_raw_token(value)) == 32
