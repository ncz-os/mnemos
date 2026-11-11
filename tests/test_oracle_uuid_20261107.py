"""Characterisation: the Oracle uuid encoding (FB970)."""

from __future__ import annotations

import uuid

import pytest

from mnemos.persistence.oracle import _raw_to_uuid, _uuid_to_raw

CANONICAL = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
RAW = uuid.UUID(CANONICAL).bytes
ZERO_RAW = uuid.UUID(int=0).bytes


def test_a_uuid_object_becomes_sixteen_raw_bytes():
    raw = _uuid_to_raw(uuid.UUID(CANONICAL))
    assert raw == RAW
    assert len(raw) == 16


def test_a_canonical_string_becomes_raw():
    assert _uuid_to_raw(CANONICAL) == RAW


@pytest.mark.parametrize(
    "text",
    [
        CANONICAL,
        CANONICAL.upper(),
        "{" + CANONICAL + "}",
        "urn:uuid:" + CANONICAL,
        CANONICAL.replace("-", ""),
    ],
)
def test_the_accepted_text_forms_agree(text):
    assert _uuid_to_raw(text) == RAW


def test_sixteen_raw_bytes_pass_through_by_identity():
    assert _uuid_to_raw(RAW) is RAW


def test_none_passes_through_both_ways():
    assert _uuid_to_raw(None) is None
    assert _raw_to_uuid(None) is None


def test_raw_bytes_become_the_canonical_string():
    assert _raw_to_uuid(RAW) == CANONICAL


def test_a_uuid_object_becomes_its_string():
    assert _raw_to_uuid(uuid.UUID(CANONICAL)) == CANONICAL


def test_the_pair_round_trips():
    for value in (uuid.uuid4(), uuid.UUID(int=0), uuid.uuid5(uuid.NAMESPACE_DNS, "x")):
        assert _raw_to_uuid(_uuid_to_raw(value)) == str(value)
        assert uuid.UUID(bytes=_uuid_to_raw(str(value))) == value


def test_upper_case_text_normalises_to_lower():
    assert _raw_to_uuid(_uuid_to_raw(CANONICAL.upper())) == CANONICAL


def test_the_nil_uuid_round_trips():
    assert _uuid_to_raw(uuid.UUID(int=0)) == b"\x00" * 16
    assert _raw_to_uuid(b"\x00" * 16) == "00000000-0000-0000-0000-000000000000"


def test_wrong_width_bytes_are_refused_on_the_way_in():
    for width in (0, 15, 17):
        with pytest.raises(ValueError, match="exactly 16 bytes"):
            _uuid_to_raw(b"\x00" * width)


def test_wrong_width_raw_is_refused_on_the_way_back():
    for width in (0, 15, 17):
        with pytest.raises(ValueError):
            _raw_to_uuid(b"\x00" * width)


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "0", "6ba7b810"])
def test_malformed_text_is_refused(bad):
    with pytest.raises(ValueError):
        _uuid_to_raw(bad)


def test_a_bytearray_is_refused_on_the_way_in_but_accepted_back():
    with pytest.raises(ValueError):
        _uuid_to_raw(bytearray(RAW))
    assert _raw_to_uuid(bytearray(RAW)) == CANONICAL


def test_a_list_of_ints_is_coerced_on_the_way_back():
    assert _raw_to_uuid(list(RAW)) == CANONICAL


def test_a_string_on_the_way_back_raises_type_error():
    with pytest.raises(TypeError):
        _raw_to_uuid(ZERO_RAW.decode("latin-1"))


def test_an_int_is_refused():
    with pytest.raises(ValueError):
        _uuid_to_raw(0)
