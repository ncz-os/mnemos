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

_FIXTURE = Path(__file__).resolve().parent.parent / "db" / "migrations_db2" / "0001_core_schema.sql"


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
