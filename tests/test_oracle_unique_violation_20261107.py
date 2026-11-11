"""Characterisation: Oracle unique-violation detection (FB969)."""

from __future__ import annotations

import pytest

from mnemos.persistence.oracle import _is_unique_violation

ORA = "ORA-00001: unique constraint (MNEMOS.PK_MEMORIES) violated"


class DbError(Exception):
    """Shape of a driver error: the code lives on the error object."""

    def __init__(self, message: str, *, code: object = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def wrapped(code: object) -> Exception:
    return Exception(DbError("driver error", code=code))


def test_the_oracle_code_is_detected():
    assert _is_unique_violation(Exception(ORA)) is True
    assert _is_unique_violation(Exception(f"dpi-1080: {ORA}")) is True


def test_a_code_of_one_on_the_argument_is_detected():
    assert _is_unique_violation(wrapped(1)) is True


def test_the_db2_forms_are_detected():
    assert _is_unique_violation(Exception("SQLSTATE=23505")) is True
    assert _is_unique_violation(Exception("[IBM][CLI Driver] SQL0803N")) is True
    assert _is_unique_violation(Exception("SQLCODE=-803")) is True


def test_a_generic_exception_is_not_a_violation():
    assert _is_unique_violation(Exception("connection reset")) is False
    assert _is_unique_violation(ValueError("ORA-")) is False


def test_an_exception_without_args_is_not_a_violation():
    assert _is_unique_violation(Exception()) is False


def test_matching_is_case_sensitive():
    assert _is_unique_violation(Exception("ora-00001")) is False
    assert _is_unique_violation(Exception("sqlstate=23505")) is False


def test_matching_is_a_substring():
    assert _is_unique_violation(Exception(f"prefix {ORA} suffix")) is True
    assert _is_unique_violation(Exception("ORA-000010")) is True


def test_a_bare_number_is_not_enough():
    assert _is_unique_violation(Exception("23505")) is False
    assert _is_unique_violation(Exception("SQLSTATE = 23505")) is False
    assert _is_unique_violation(Exception("SQLCODE= -803")) is False


def test_sqlite_and_postgres_unique_errors_are_not_detected():
    assert _is_unique_violation(Exception("UNIQUE constraint failed: memories.id")) is False
    assert _is_unique_violation(Exception("duplicate key value violates unique constraint")) is False


def test_a_string_code_is_not_a_code():
    assert _is_unique_violation(wrapped("1")) is False


def test_float_and_bool_codes_of_one_count():
    assert _is_unique_violation(wrapped(1.0)) is True
    assert _is_unique_violation(wrapped(True)) is True


def test_a_code_on_the_exception_itself_is_ignored():
    err = DbError("driver error", code=1)
    assert getattr(err, "code", None) == 1
    assert _is_unique_violation(err) is False


def test_a_mapping_argument_is_not_honoured():
    assert _is_unique_violation(Exception({"code": 1})) is False


def test_a_chained_cause_is_not_followed():
    outer = Exception("wrapped")
    try:
        raise outer from Exception(ORA)
    except Exception as caught:
        assert _is_unique_violation(caught) is False


def test_a_wrapped_message_is_detected():
    assert _is_unique_violation(Exception(f"cause: {ORA}")) is True


def test_the_verdict_is_a_bool():
    assert _is_unique_violation(Exception(ORA)) is True
    assert _is_unique_violation(Exception("nope")) is False


@pytest.mark.parametrize("message", [ORA, "SQLSTATE=23505", "nope"])
def test_the_check_does_not_mutate_the_exception(message):
    inner = DbError(message, code=1)
    exc = Exception(inner)
    before = (exc.args, str(exc), dict(exc.__dict__), exc.__cause__, exc.__context__)
    _is_unique_violation(exc)
    after = (exc.args, str(exc), dict(exc.__dict__), exc.__cause__, exc.__context__)
    assert before == after
    assert exc.args[0] is inner
    assert getattr(inner, "code", None) == 1


def test_the_check_is_pure_across_calls():
    exc = Exception(ORA)
    assert [_is_unique_violation(exc) for _ in range(3)] == [True, True, True]
