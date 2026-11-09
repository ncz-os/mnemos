"""Unit tests for scripts/oracle_to_postgres_copy.py value conversion (no database needed)."""

import datetime as dt
import decimal
import importlib.util
import json
import uuid
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "oracle_to_postgres_copy.py"
spec = importlib.util.spec_from_file_location("oracle_to_postgres_copy", SCRIPT)
copy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy)


def test_uuid_from_raw16_hex_and_hand_written_ids():
    raw = uuid.uuid4()
    conv = copy.converter("uuid", "RAW")
    assert conv(raw.bytes) == raw
    assert conv(raw.hex) == raw
    # a hand-written id in a uuid column: deterministic, so a rerun maps it to the same value
    assert (
        conv("claude-opus-4-8") == conv("claude-opus-4-8") == uuid.uuid5(uuid.NAMESPACE_URL, "ora2pg:claude-opus-4-8")
    )
    assert conv(None) is None


def test_json_columns_take_documents_text_and_keep_non_json_as_a_json_string():
    conv = copy.converter("jsonb", "JSON")
    assert json.loads(conv({"a": [1, 2]})) == {"a": [1, 2]}
    assert conv('{"b": true}') == '{"b": true}'
    assert json.loads(conv("not json")) == "not json"
    assert conv(None) is None


def test_timestamptz_from_epoch_and_naive_utc():
    conv = copy.converter("timestamp with time zone", "NUMBER")
    assert conv(0) == dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    naive = dt.datetime(2026, 11, 9, 15, 0, 0)
    assert conv(naive) == naive.replace(tzinfo=dt.timezone.utc)


def test_timestamp_without_zone_drops_the_offset_after_normalising_to_utc():
    conv = copy.converter("timestamp without time zone", "TIMESTAMP")
    aware = dt.datetime(2026, 11, 9, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert conv(aware) == dt.datetime(2026, 11, 9, 15, 0)


def test_numeric_boolean_and_text_conversions():
    assert copy.converter("bigint", "NUMBER")(decimal.Decimal("42")) == 42
    assert copy.converter("double precision", "NUMBER")(decimal.Decimal("1.5")) == 1.5
    assert copy.converter("numeric", "NUMBER")(1.25) == decimal.Decimal("1.25")
    b = copy.converter("boolean", "NUMBER")
    assert (b(1), b(0), b("Y"), b("false")) == (True, False, True, False)
    t = copy.converter("text", "RAW")
    assert t(b"\x01\xff") == "01ff"
    assert copy.converter("text", "CLOB")({"k": 1}) == '{"k": 1}'


def test_array_columns_accept_lists_and_json_arrays():
    conv = copy.converter("ARRAY", "CLOB")
    assert conv(["a", 1]) == ["a", "1"]
    assert conv('["x", "y"]') == ["x", "y"]
    assert conv("solo") == ["solo"]


def test_timestamp_without_zone_normalises_an_iso_string_with_an_offset():
    conv = copy.converter("timestamp without time zone", "VARCHAR2")
    assert conv("2026-11-09T10:00:00-05:00") == dt.datetime(2026, 11, 9, 15, 0)
    assert conv("2026-11-09T15:00:00") == dt.datetime(2026, 11, 9, 15, 0)


def test_uuid_from_non_uuid_bytes_uses_the_same_deterministic_fallback_as_text():
    conv = copy.converter("uuid", "RAW")
    assert conv(b"claude-opus-4-8") == conv("claude-opus-4-8")


def test_identifiers_are_quoted_with_embedded_quotes_doubled():
    assert copy.qi("memories") == '"memories"'
    assert copy.qi('we"ird') == '"we""ird"'
