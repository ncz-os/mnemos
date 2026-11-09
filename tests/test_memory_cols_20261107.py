"""Characterisation: the sqlite memory column projection (FB979)."""

from __future__ import annotations

import re
import sqlite3


from mnemos.persistence.sqlite import _MEMORY_COLS as MEMORY_COLS
from mnemos.persistence.sqlite import _sqlite_memory_cols

SOURCE = [raw.strip() for raw in MEMORY_COLS.split(",")]
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REWRITTEN = {"compressed_content", "created", "updated"}


def expected(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    out: list[str] = []
    for name in SOURCE:
        if name == "compressed_content":
            out.append("NULL AS compressed_content")
        elif name in {"created", "updated"}:
            out.append(f"replace(datetime({prefix}{name}), ' ', 'T') AS {name}")
        else:
            out.append(f"{prefix}{name}")
    return ", ".join(out)


def memory_table() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memories (" + ", ".join(f'"{name}" TEXT' for name in SOURCE) + ")")
    return conn


def test_the_source_list_is_plain_identifiers():
    assert SOURCE
    assert "" not in SOURCE
    assert all(IDENT.match(name) for name in SOURCE)
    assert "compressed_content" in SOURCE
    assert {"created", "updated"} <= set(SOURCE)


def test_the_projection_is_a_string():
    assert isinstance(_sqlite_memory_cols(), str)
    assert _sqlite_memory_cols().strip() == _sqlite_memory_cols()


def test_the_projection_is_exactly_the_documented_list():
    assert _sqlite_memory_cols() == expected()


def test_an_alias_prefixes_columns_and_datetime_arguments():
    assert _sqlite_memory_cols("m") == expected("m")


def test_compressed_content_is_a_literal_null():
    sql = _sqlite_memory_cols()
    assert sql.count("compressed_content") == 1
    assert "NULL AS compressed_content" in sql
    assert "m.compressed_content" not in _sqlite_memory_cols("m")


def test_timestamps_are_rewritten_once_each():
    sql = _sqlite_memory_cols()
    assert sql.count("datetime(") == 2
    assert sql.count("replace(") == 2
    assert "replace(datetime(created), ' ', 'T') AS created" in sql
    assert "replace(datetime(updated), ' ', 'T') AS updated" in sql


def test_every_other_column_passes_through_once():
    plain = _sqlite_memory_cols()
    aliased = _sqlite_memory_cols("m")
    for name in SOURCE:
        if name in REWRITTEN:
            continue
        token = re.escape(name)
        assert re.search(rf"(?:^|, ){token}(?:$|, )", plain)
        assert re.search(rf"(?:^|, )m\.{token}(?:$|, )", aliased)


def test_an_empty_alias_matches_no_alias():
    assert _sqlite_memory_cols("") == _sqlite_memory_cols()
    assert ".." not in _sqlite_memory_cols("m")


def test_the_projection_is_deterministic():
    assert _sqlite_memory_cols("m") == _sqlite_memory_cols("m")


def test_the_list_is_valid_sql_without_an_alias():
    conn = memory_table()
    try:
        cursor = conn.execute(f"SELECT {_sqlite_memory_cols()} FROM memories LIMIT 0")
        assert [column[0] for column in cursor.description] == SOURCE
    finally:
        conn.close()


def test_the_list_is_valid_sql_with_an_alias():
    conn = memory_table()
    try:
        cursor = conn.execute(f"SELECT {_sqlite_memory_cols('m')} FROM memories m LIMIT 0")
        assert [column[0] for column in cursor.description] == SOURCE
    finally:
        conn.close()


def test_a_stored_timestamp_renders_iso_shaped():
    conn = memory_table()
    try:
        conn.execute("INSERT INTO memories (created, updated) VALUES ('2026-01-01 12:34:56', '2026-01-01 12:34:56')")
        row = conn.execute(f"SELECT {_sqlite_memory_cols()} FROM memories").fetchone()
        assert row[SOURCE.index("created")] == "2026-01-01T12:34:56"
        assert row[SOURCE.index("updated")] == "2026-01-01T12:34:56"
        assert row[SOURCE.index("compressed_content")] is None
    finally:
        conn.close()


def test_an_unparseable_stored_timestamp_renders_null():
    conn = memory_table()
    try:
        conn.execute("INSERT INTO memories (created) VALUES ('not a date')")
        row = conn.execute(f"SELECT {_sqlite_memory_cols()} FROM memories").fetchone()
        assert row[SOURCE.index("created")] is None
    finally:
        conn.close()


def test_a_fractional_offset_timestamp_loses_both():
    conn = memory_table()
    try:
        conn.execute("INSERT INTO memories (created) VALUES ('2026-01-01 12:34:56.789+02:00')")
        row = conn.execute(f"SELECT {_sqlite_memory_cols()} FROM memories").fetchone()
        value = row[SOURCE.index("created")]
        assert value.startswith("2026-01-01T")
        assert ".789" not in value
        assert "+02:00" not in value
        assert value in {"2026-01-01T10:34:56", "2026-01-01T12:34:56"}
    finally:
        conn.close()
