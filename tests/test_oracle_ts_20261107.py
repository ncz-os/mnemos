"""Characterisation: the Oracle timestamp conversion (FB973)."""

from __future__ import annotations

import datetime as dt

import pytest

from mnemos.persistence.oracle import _ts_for_oracle

UTC = dt.timezone.utc


def test_none_passes_through():
    assert _ts_for_oracle(None) is None


def test_datetimes_pass_through_by_identity():
    aware = dt.datetime(2026, 11, 7, 10, 11, 12, tzinfo=UTC)
    naive = dt.datetime(2026, 11, 7, 10, 11, 12)
    assert _ts_for_oracle(aware) is aware
    assert _ts_for_oracle(naive) is naive


def test_a_trailing_z_becomes_utc():
    value = _ts_for_oracle("2026-11-07T10:11:12Z")
    assert value == dt.datetime(2026, 11, 7, 10, 11, 12, tzinfo=UTC)
    assert value.utcoffset() == dt.timedelta(0)


def test_the_z_suffix_renders_as_an_offset():
    assert _ts_for_oracle("2026-11-07T10:11:12Z").isoformat() == "2026-11-07T10:11:12+00:00"


def test_explicit_offsets_are_preserved():
    value = _ts_for_oracle("2026-11-07T10:11:12+02:00")
    offset = dt.timezone(dt.timedelta(hours=2))
    assert value == dt.datetime(2026, 11, 7, 10, 11, 12, tzinfo=offset)
    assert value.utcoffset() == dt.timedelta(hours=2)


def test_microseconds_are_preserved():
    assert _ts_for_oracle("2026-11-07T10:11:12.123456").microsecond == 123456
    assert _ts_for_oracle("2026-11-07T10:11:12.123").microsecond == 123000


def test_extra_precision_is_truncated():
    assert _ts_for_oracle("2026-11-07T10:11:12.123456789Z").microsecond == 123456


def test_a_naive_string_stays_naive():
    value = _ts_for_oracle("2026-11-07T10:11:12")
    assert value.tzinfo is None
    assert value == dt.datetime(2026, 11, 7, 10, 11, 12)


def test_a_space_separator_is_accepted():
    assert _ts_for_oracle("2026-11-07 10:11:12") == dt.datetime(2026, 11, 7, 10, 11, 12)


def test_the_basic_date_form_is_accepted():
    assert _ts_for_oracle("20261107") == dt.datetime(2026, 11, 7, 0, 0)


def test_a_date_object_widens_to_midnight():
    value = _ts_for_oracle(dt.date(2026, 11, 7))
    assert value == dt.datetime(2026, 11, 7, 0, 0)
    assert value.tzinfo is None


@pytest.mark.parametrize(
    "value",
    ["nope", "2026-13-01T00:00:00", "2026-11-07T10:11:12z", "2026-11-07T10:11:12Z "],
)
def test_unparseable_text_is_returned_unchanged(value):
    assert _ts_for_oracle(value) is value


def test_epoch_numbers_are_not_converted():
    assert _ts_for_oracle(1700000000) == 1700000000
    float_value = 1700000000.5
    assert _ts_for_oracle(float_value) is float_value


def test_bytes_are_not_converted():
    raw = b"2026-11-07T10:11:12Z"
    assert _ts_for_oracle(raw) is raw


def test_a_lowercase_z_is_not_normalised():
    value = "2026-11-07T10:11:12z"
    assert _ts_for_oracle(value) is value
