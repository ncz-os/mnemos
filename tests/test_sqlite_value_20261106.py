"""Characterisation of ``_sqlite_value`` (FB567).

The helper is the SQLite parameter adapter in ``mnemos/persistence/sqlite.py``
and is four lines long:

    def _sqlite_value(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        return value

Pinned as implemented:

* the special case is ``datetime`` → ``.isoformat()`` (naive stays naive,
  an offset is preserved verbatim, microseconds are kept), selected by
  ``isinstance``, so a ``datetime`` subclass is converted too — which is what
  this repo's frozen-clock fixtures hand to a backend;
* every other value — ``bool``, ``None``, ``int``, ``float``, ``str``,
  ``bytes``, ``date``, ``Decimal``, ``dict``/``list``, ``uuid.UUID``, an
  arbitrary object — is returned unchanged, with its type and identity intact;
* consequently a ``bool`` does not become text (``True`` stays ``True``), and
  nothing raises: there is no "unsupported type" error, so the batch-38
  premise that unsupported types raise does not hold. A value SQLite cannot
  bind fails later, in the driver, not here.

Both the pass-through breadth and the missing unsupported-type error are
recorded as ``DIVERGENCE:`` pins in the landing summary for this branch.
"""

from __future__ import annotations

from array import array
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mnemos.persistence.sqlite import _sqlite_value


# ── datetime: the one special case ──────────────────────────────────────────


def test_a_naive_datetime_becomes_its_isoformat() -> None:
    assert _sqlite_value(datetime(2026, 11, 6, 12, 34, 56)) == "2026-11-06T12:34:56"


def test_a_utc_datetime_keeps_the_plus_zero_offset() -> None:
    value = datetime(2026, 11, 6, 12, 34, 56, tzinfo=timezone.utc)

    assert _sqlite_value(value) == "2026-11-06T12:34:56+00:00"
    assert _sqlite_value(value).endswith("+00:00")


def test_a_non_utc_offset_is_preserved_not_converted() -> None:
    kolkata = timezone(timedelta(hours=5, minutes=30))

    value = datetime(2026, 11, 6, 12, 34, 56, tzinfo=kolkata)

    assert _sqlite_value(value) == "2026-11-06T12:34:56+05:30"


def test_a_datetime_subclass_is_converted_too() -> None:
    """``isinstance`` dispatch, not exact-type: a frozen clock converts.

    The repo's frozen-clock fixtures (``_FrozenDatetime`` in
    ``tests/test_kronos.py`` and ``tests/test_retention_policy_20261106.py``)
    hand a ``datetime`` subclass to the backend, so a helper that only
    accepted ``type(value) is datetime`` would bind the subclass object
    itself. A ``date`` subclass is still not a ``datetime`` and still passes
    through.
    """

    class FrozenClock(datetime):
        pass

    class FrozenDay(date):
        pass

    frozen = FrozenClock(2026, 11, 6, 12, 34, 56)
    day = FrozenDay(2026, 11, 6)

    assert _sqlite_value(frozen) == "2026-11-06T12:34:56"
    assert type(_sqlite_value(frozen)) is str
    assert _sqlite_value(day) is day


def test_microseconds_are_preserved() -> None:
    value = datetime(2026, 11, 6, 12, 34, 56, 123456)

    assert _sqlite_value(value) == "2026-11-06T12:34:56.123456"


def test_the_datetime_is_not_mutated() -> None:
    value = datetime(2026, 11, 6, 12, 34, 56, tzinfo=timezone.utc)
    before = (value.year, value.month, value.day, value.tzinfo)

    assert _sqlite_value(value) == "2026-11-06T12:34:56+00:00"
    assert (value.year, value.month, value.day, value.tzinfo) == before


# ── pass-through: type and identity are preserved ───────────────────────────


def test_true_and_false_stay_booleans_not_text() -> None:
    assert _sqlite_value(True) is True
    assert _sqlite_value(False) is False
    assert type(_sqlite_value(True)) is bool


def test_none_passes_through() -> None:
    assert _sqlite_value(None) is None


@pytest.mark.parametrize("value", [0, 1, -1, 42, 0.0, 1.5, -2.25])
def test_numbers_pass_through_unchanged(value: object) -> None:
    assert _sqlite_value(value) == value
    assert type(_sqlite_value(value)) is type(value)


def test_text_passes_through_unchanged() -> None:
    assert _sqlite_value("2026-11-06T00:00:00") == "2026-11-06T00:00:00"
    assert _sqlite_value("") == ""


def test_bytes_pass_through_unchanged() -> None:
    raw = b"\x00\x01binary"

    assert _sqlite_value(raw) is raw


def test_a_date_is_not_a_datetime_and_passes_through() -> None:
    """``date`` is not ``datetime``, so it is not isoformatted here."""
    value = date(2026, 11, 6)

    assert _sqlite_value(value) is value
    assert _sqlite_value(value) == date(2026, 11, 6)


def test_a_decimal_passes_through_unchanged() -> None:
    value = Decimal("1.2300")

    assert _sqlite_value(value) is value


def test_a_uuid_passes_through_unconverted() -> None:
    """``uuid.UUID`` is not special-cased: it is neither converted on the way
    in nor assumed to arrive as a string. The driver adapts it."""
    value = uuid.UUID("8f14e45f-ea0f-4e8b-9c1a-0c2b3d4e5f60")

    assert _sqlite_value(value) is value


def test_containers_pass_through_by_identity() -> None:
    mapping = {"a": 1}
    sequence = [1, "two", None]
    # A distinct empty list rather than the ``[]`` constant: CPython keeps one
    # per code site, so an ``is`` check against a literal empty list could
    # never fail. ``list(())`` builds a different object, so a helper that
    # rebuilt the argument (``list(value)``) fails this line while one that
    # mutated it in place fails ``test_an_empty_value_passes_through_unchanged``.
    empty = list(())
    before = (dict(mapping), list(sequence))

    assert _sqlite_value(mapping) is mapping
    assert _sqlite_value(sequence) is sequence
    assert _sqlite_value(empty) is empty
    # Identity alone does not see a helper that edited the container in place
    # before handing it back.
    assert (mapping, sequence) == before


def test_an_arbitrary_object_passes_through() -> None:
    sentinel = object()

    assert _sqlite_value(sentinel) is sentinel


def test_an_array_passes_through_by_identity() -> None:
    """``array.array`` is not a ``datetime``, so it is handed to the driver as-is."""
    value = array("i", [1, 2, 3])

    assert _sqlite_value(value) is value


def test_an_empty_value_passes_through_unchanged() -> None:
    empty_bytes = b""

    assert _sqlite_value("") == ""
    assert _sqlite_value(empty_bytes) is empty_bytes
    assert _sqlite_value([]) == []


def test_a_cached_integer_keeps_its_identity() -> None:
    """Small ints are the values the driver most often receives already-boxed.

    CPython caches ``-5..256``, so for those the helper returning "the same
    int" is what a caller sees; a helper that boxed every int into a new
    object would still compare equal but could break an ``is`` check a caller
    relies on. This is a breadth pin, not an independent mutant-killer — the
    rebuild mutants are caught by ``test_numbers_pass_through_unchanged``
    (which asserts ``type`` as well as value).
    """
    value = 7 + 0

    assert _sqlite_value(value) is value


# ── no documented unsupported-type error ───────────────────────────────────


def test_an_unbindable_value_does_not_raise_here() -> None:
    """DIVERGENCE: the brief expected unsupported types to raise; nothing raises."""
    unbound = {1, 2, 3}  # sqlite3 cannot bind a set

    assert _sqlite_value(unbound) is unbound


@pytest.mark.parametrize("value", [datetime(2026, 11, 6), date(2026, 11, 6), None, True, Decimal("2.5")])
def test_the_helper_is_a_fixpoint_on_everything_it_has_already_converted(value: object) -> None:
    once = _sqlite_value(value)
    twice = _sqlite_value(once)

    if isinstance(value, datetime):
        # A datetime is converted exactly once; the string is then returned as-is.
        assert isinstance(once, str)
        assert twice is once
    else:
        assert twice is once
