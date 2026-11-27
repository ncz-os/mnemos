"""Characterisation of ``is_duplicate_memory_error`` (FB561).

The helper is the one place that knows ``insert_memory`` signals a conflict
differently per backend, so its rule is pinned exactly as implemented in
``mnemos/persistence/base.py``:

* the exception's **own class name** decides — ``DuplicateMemoryError`` is
  matched by name, not by ``isinstance`` against this module's class;
* the four driver integrity class names are matched only when the stringified
  message mentions a uniqueness marker (``unique`` / ``duplicate`` /
  ``primary key``, case-insensitive);
* anything else is not a duplicate, and the exception is never mutated.

The helper does **not** walk ``__cause__`` / ``__context__``: a wrapped
duplicate stays the caller's to unwrap. That is pinned here as a divergence
from the batch-38 brief, whose acceptance text assumed the documented rule
covered wrapping.
"""

from __future__ import annotations

import pytest

from mnemos.persistence.base import DuplicateMemoryError, is_duplicate_memory_error


def _named(name: str, message: str = "") -> BaseException:
    """An exception whose ``type(exc).__name__`` is exactly ``name``."""
    return type(name, (Exception,), {})(message)


# ── the real class ───────────────────────────────────────────────────────────


def test_real_duplicate_memory_error_is_detected() -> None:
    assert is_duplicate_memory_error(DuplicateMemoryError("id already exists")) is True


def test_real_duplicate_memory_error_without_a_message_is_detected() -> None:
    assert is_duplicate_memory_error(DuplicateMemoryError()) is True


def test_duplicate_memory_error_is_a_value_error_but_a_plain_value_error_is_not() -> None:
    assert issubclass(DuplicateMemoryError, ValueError)
    assert is_duplicate_memory_error(ValueError("id already exists")) is False


def test_a_subclass_with_a_different_class_name_is_not_detected() -> None:
    """The rule reads ``type(exc).__name__``; a renamed subclass slips through."""

    class SqliteDuplicateMemoryError(DuplicateMemoryError):
        pass

    assert is_duplicate_memory_error(SqliteDuplicateMemoryError("dup")) is False


def test_a_foreign_class_merely_named_duplicate_memory_error_is_detected() -> None:
    """Detection is by name, so an unrelated class with the same name matches."""
    foreign = type("DuplicateMemoryError", (Exception,), {})
    assert foreign is not DuplicateMemoryError
    assert is_duplicate_memory_error(foreign("dup")) is True


def test_a_subclass_keeping_the_same_name_is_detected() -> None:
    clone = type("DuplicateMemoryError", (DuplicateMemoryError,), {})
    assert clone.__name__ == "DuplicateMemoryError"
    assert is_duplicate_memory_error(clone("dup")) is True


# ── the driver integrity class names, with a uniqueness marker ──────────────


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("IntegrityError", 'duplicate key value violates unique constraint "memories_pkey"'),
        ("UniqueViolation", 'duplicate key value violates unique constraint "memories_pkey"'),
        ("DuplicateKeyError", "E11000 duplicate key error collection: mnemos.memories"),
        ("UniqueViolationError", "UNIQUE constraint failed: memories.memory_id"),
        ("IntegrityError", "PRIMARY KEY must be unique"),
        ("IntegrityError", "Unexpected DUPlicate marker"),
    ],
)
def test_integrity_names_with_a_uniqueness_marker_are_detected(name: str, message: str) -> None:
    assert is_duplicate_memory_error(_named(name, message)) is True


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("IntegrityError", "NOT NULL constraint failed: memories.content"),
        ("IntegrityError", "FOREIGN KEY constraint failed"),
        ("IntegrityError", "connection reset by peer"),
        ("IntegrityError", ""),
        ("UniqueViolation", "value too long for type character varying(10)"),
    ],
)
def test_integrity_names_without_a_uniqueness_marker_are_not_detected(name: str, message: str) -> None:
    assert is_duplicate_memory_error(_named(name, message)) is False


def test_an_integrity_class_name_outside_the_four_is_not_detected() -> None:
    """Only the four named driver classes are consulted, message notwithstanding."""
    assert is_duplicate_memory_error(_named("OperationalError", "duplicate key value")) is False


def test_non_exception_objects_are_not_detected() -> None:
    assert is_duplicate_memory_error(RuntimeError("duplicate key")) is False


# ── wrapping: not walked ────────────────────────────────────────────────────


def test_a_wrapped_duplicate_is_not_detected_by_the_helper() -> None:
    """DIVERGENCE: the documented rule covers the argument only, not its cause."""
    inner = DuplicateMemoryError("dup")
    try:
        raise inner
    except DuplicateMemoryError as exc:
        try:
            raise RuntimeError("wrapped by the caller") from exc
        except RuntimeError as outer:
            wrapper = outer

    assert wrapper.__cause__ is inner
    assert is_duplicate_memory_error(wrapper) is False
    assert is_duplicate_memory_error(inner) is True


def test_an_implicit_context_duplicate_is_not_detected() -> None:
    inner = DuplicateMemoryError("dup")
    try:
        raise inner
    except DuplicateMemoryError:
        try:
            raise RuntimeError("raised inside the handler")
        except RuntimeError as outer:
            wrapper = outer

    assert wrapper.__context__ is inner
    assert wrapper.__cause__ is None
    assert is_duplicate_memory_error(wrapper) is False


# ── the check is read-only and idempotent ───────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        DuplicateMemoryError("dup"),
        _named("IntegrityError", "duplicate key value violates unique constraint"),
        _named("IntegrityError", "NOT NULL constraint failed"),
    ],
)
def test_the_check_never_mutates_the_exception(exc: BaseException) -> None:
    exc.__notes__ = ["caller note"]  # type: ignore[attr-defined]
    before = (exc.args, dict(vars(exc)), str(exc), exc.__notes__)

    result = is_duplicate_memory_error(exc)

    after = (exc.args, dict(vars(exc)), str(exc), exc.__notes__)
    assert after == before
    assert set(vars(exc)) == {"__notes__"}
    assert result is is_duplicate_memory_error(exc)
