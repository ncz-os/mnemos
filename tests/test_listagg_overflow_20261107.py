"""Characterisation: db2 LISTAGG overflow detection (FB990)."""

from __future__ import annotations

import pytest

from mnemos.persistence.db2 import _is_listagg_overflow


def test_the_documented_sqlcode():
    assert _is_listagg_overflow(Exception("SQL0137N The aggregated list is too long")) is True


def test_the_sqlstate():
    assert _is_listagg_overflow(Exception("SQLSTATE=54006")) is True


def test_the_signed_sqlcode():
    assert _is_listagg_overflow(Exception("SQLCODE=-137")) is True


def test_the_legacy_code():
    assert _is_listagg_overflow(Exception("SQL0349N")) is True


def test_the_resource_code():
    assert _is_listagg_overflow(Exception("57011")) is True


def test_matching_is_case_insensitive():
    assert _is_listagg_overflow(Exception("sql0137n")) is True


def test_a_generic_exception_is_not_an_overflow():
    assert _is_listagg_overflow(Exception("boom")) is False


def test_an_empty_exception_is_not_an_overflow():
    assert _is_listagg_overflow(Exception()) is False


def test_none_is_not_an_overflow():
    assert _is_listagg_overflow(None) is False


def test_the_result_is_a_bool():
    assert _is_listagg_overflow(Exception("SQL0137N")) is True
    assert _is_listagg_overflow(Exception("x")) is False


def test_a_chained_overflow_is_not_detected():
    inner = Exception("SQLCODE=-137")
    outer = RuntimeError("db2 call failed")
    outer.__cause__ = inner
    assert _is_listagg_overflow(outer) is False


def test_a_near_miss_substring_is_matched():
    assert _is_listagg_overflow(Exception("code -1371")) is True
    assert _is_listagg_overflow(Exception("value 540060")) is True
    assert _is_listagg_overflow(Exception("1570112")) is True


def test_a_bytes_payload_is_matched_through_its_repr():
    assert _is_listagg_overflow(Exception(b"SQL0137N")) is True


def test_a_raising_str_propagates():
    class Bad(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no text")

    with pytest.raises(RuntimeError):
        _is_listagg_overflow(Bad())


def test_the_check_is_pure_and_deterministic():
    exc = Exception("SQL0137N")
    assert _is_listagg_overflow(exc) is True
    assert _is_listagg_overflow(exc) is True
