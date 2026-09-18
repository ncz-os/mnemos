"""Timestamp parsing and formatting helpers for MPF."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


class MalformedTimestampError(ValueError):
    """Raised when a non-empty timestamp string fails to parse.

    Import callers treat this as a per-record failure: an envelope
    that supplies a malformed timestamp (rather than omitting it)
    is reporting corrupted lifecycle data, and silently substituting
    NOW() would mask the corruption. Persistence treats omitted
    timestamps with COALESCE(now), which is the correct absent-field
    behaviour; non-empty-but-malformed timestamps must NOT take that
    path.
    """


def _iso(value) -> Optional[str]:
    """Render a DB timestamp value as an RFC 3339 / ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an MPF timestamp string.

    Distinguishes three states:
    - ``None`` / empty string: timestamp was omitted. Returns None so
      callers can apply the COALESCE(now) absent-field semantics.
    - non-empty, well-formed ISO 8601 string: returns the parsed
      datetime (may be naive or aware).
    - non-empty, malformed string: raises ``MalformedTimestampError``
      so the caller fails the affected record/sidecar rather than
      silently substituting NOW().
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedTimestampError(f"timestamp must be an ISO 8601 string, got {type(value).__name__}")
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise MalformedTimestampError(f"timestamp {value!r} is not a valid ISO 8601 string: {exc}") from exc


def _parse_iso_naive(value: Optional[str]) -> Optional[datetime]:
    """Parse a timestamp and return a UTC-aware value for DB writes.

    The helper name is retained for import-call compatibility from the
    pre-v5.0.3 TIMESTAMP schema. Postgres lifecycle columns are now
    TIMESTAMPTZ and asyncpg expects aware datetime values.

    Re-raises ``MalformedTimestampError`` for non-empty-but-malformed
    inputs (see ``_parse_iso`` docstring); only omits timestamps
    (None / empty string) silently fall back to COALESCE(now).
    """
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _same_instant(db_value, envelope_value) -> bool:
    """Compare a STORED timestamp against an envelope-derived one.

    Necessary because backends do not agree on the Python type they hand
    back for a lifecycle column:

    * Postgres (asyncpg) decodes timestamptz to an aware ``datetime``.
    * SQLite stores ISO-8601 TEXT and returns a ``str``.
    * Oracle/Db2 return naive ``datetime`` values.

    The importer's conflict check compares a stored value against
    ``_parse_iso_naive(envelope)``, which is always an aware ``datetime``. A
    direct ``!=`` therefore reports "differ" for EVERY row on SQLite -- the
    str/datetime comparison can never be equal, whatever the actual instant --
    so an idempotent re-import was recorded as a hard failure on that backend.

    Both sides are normalised to an aware UTC ``datetime`` and compared as
    instants. A value that cannot be parsed falls back to strict equality
    rather than guessing, so genuinely corrupt data still surfaces as a
    mismatch instead of being waved through.
    """
    if db_value is None or envelope_value is None:
        return db_value == envelope_value
    try:
        left = _parse_iso_naive(db_value) if isinstance(db_value, str) else db_value
        right = _parse_iso_naive(envelope_value) if isinstance(envelope_value, str) else envelope_value
    except MalformedTimestampError:
        return db_value == envelope_value
    if not isinstance(left, datetime) or not isinstance(right, datetime):
        return db_value == envelope_value
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left == right
