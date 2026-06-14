"""Db2-native migration DDL syntax probes.

Driver-free, DB-free regex assertions that verify
``db/migrations_db2/0001_core_schema.sql`` has been rewritten to use
Db2-native types with zero Oracle-compatibility-alias tokens remaining.

The port is described in ``docs/native-db2-port-plan.md`` §2.3 and
executed in PR #10. This test file is the guardrail: it catches any
regression that reintroduces Oracle-compat tokens into the native
migration path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.db2_apply_migration import _split_statements

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _REPO_ROOT / "db" / "migrations_db2" / "0001_core_schema.sql"
_BACKFILL_FIXTURES = (
    ("0002_graeae.sql", 9),
    ("0010_hive_mind.sql", 1),
    ("0011_hive_mind_extended_columns.sql", 1),
    ("0012_pantheon_routing_audit.sql", 5),
    ("0041_knemon_tier_assignments.sql", 3),
    ("0042_knemon_baseline_tables.sql", 7),
)


def _read() -> str:
    return _FIXTURE.read_text()


def test_no_varchar2_tokens() -> None:
    """No ``VARCHAR2`` should survive the rewrite — only ``VARCHAR``."""
    sql = _read()
    assert "VARCHAR2" not in sql, "VARCHAR2 found in native migration"


def test_no_number_tokens() -> None:
    """No ``NUMBER`` (Oracle generic numeric) should survive.

    Only Db2-native type names — DECIMAL, BIGINT, INTEGER, SMALLINT — are
    expected.
    """
    sql = _read()
    # Match NUMBER as a free-standing word (not part of another word)
    matches = [m for m in re.finditer(r"\bNUMBER\b", sql) if not sql[m.start() - 1 : m.end()].startswith(".")]
    assert not matches, f"NUMBER found in native migration at positions: {[m.start() for m in matches]}"


def test_no_nvl_function_tokens() -> None:
    """No ``NVL(`` (Oracle coalesce alias) should remain — only ``COALESCE``."""
    sql = _read()
    assert "NVL(" not in sql, "NVL( found in native migration"


def test_no_sysdate_tokens() -> None:
    """No ``SYSDATE`` should remain — only ``CURRENT DATE`` or ``CURRENT TIMESTAMP``."""
    sql = _read()
    assert "SYSDATE" not in sql, "SYSDATE found in native migration"


def test_no_systimestamp_tokens() -> None:
    """No ``SYSTIMESTAMP`` should remain — only ``CURRENT TIMESTAMP``."""
    sql = _read()
    assert "SYSTIMESTAMP" not in sql, "SYSTIMESTAMP found in native migration"


def test_no_from_dual_tokens() -> None:
    """No ``FROM DUAL`` should remain — only ``FROM SYSIBM.SYSDUMMY1``."""
    sql = _read()
    assert "FROM DUAL" not in sql, "FROM DUAL found in native migration"


def test_header_mentions_db2_native_no_ora_compat() -> None:
    """Header comment must declare native Db2, no ORA-compat required."""
    sql = _read()
    lines = sql.splitlines()[:20]
    header = "\n".join(lines)
    header_lower = header.lower()
    assert "native db2" in header_lower, "Header must mention 'native Db2'"
    assert "no ora-compat" in header_lower, "Header must mention 'no ORA-compat'"


@pytest.mark.parametrize(("basename", "expected_count"), _BACKFILL_FIXTURES)
def test_backfill_migrations_parse_with_db2_runner_splitter(basename: str, expected_count: int) -> None:
    """The Db2 runner splits on @, so backfill migrations must use @ terminators."""
    path = _REPO_ROOT / "db" / "migrations_db2" / basename
    sql = path.read_text()
    percent_terminated = [
        (line_no, line)
        for line_no, line in enumerate(sql.splitlines(), 1)
        if line.strip() == "%" or line.rstrip().endswith("%")
    ]

    assert "--#SET TERMINATOR %" not in sql
    assert not percent_terminated
    statements = _split_statements(sql)
    assert len(statements) == expected_count
    assert all(not stmt.rstrip().endswith("@") for stmt in statements)
