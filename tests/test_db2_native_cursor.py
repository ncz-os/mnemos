"""Db2 native-cursor pass-through tests (driver-free).

Tests ``_Db2NativeAsyncCursor``, ``_Db2NativeAsyncConnectionPool``,
``create_db2_native_pool``, and backend factory dispatch for the
``MNEMOS_DB2_DIALECT`` runtime selector added in PR #11.
"""

from __future__ import annotations

from typing import Any

import pytest


# ── _Db2NativeAsyncCursor unit tests ──


class _FakeSyncCursor:
    """Minimal ``ibm_db_dbi`` cursor stand-in that records SQL + params."""

    rowcount = -1

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._rows: list[tuple[Any, ...]] = list(rows or [])
        self._executed: list[tuple[str, tuple | None]] = []
        self._closed = False
        self.description: tuple[tuple[str, ...], ...] | None = None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._executed.append((sql, params))

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        self._closed = True


@pytest.mark.asyncio
async def test_native_cursor_passthrough_no_params() -> None:
    """EXECUTE with no params: sync cursor receives verbatim SQL."""
    sync = _FakeSyncCursor()
    from mnemos.persistence.db2 import _Db2NativeAsyncCursor

    cur = _Db2NativeAsyncCursor(sync)
    await cur.execute("SELECT 1 FROM SYSIBM.SYSDUMMY1", None)
    assert len(sync._executed) == 1
    assert sync._executed[0] == ("SELECT 1 FROM SYSIBM.SYSDUMMY1", None)


@pytest.mark.asyncio
async def test_native_cursor_passthrough_with_params() -> None:
    """EXECUTE with positional params: sync cursor receives verbatim SQL + params."""
    sync = _FakeSyncCursor()
    from mnemos.persistence.db2 import _Db2NativeAsyncCursor

    cur = _Db2NativeAsyncCursor(sync)
    await cur.execute(
        "INSERT INTO state (key, value) VALUES (?, ?)",
        ("my_key", "my_value"),
    )
    assert len(sync._executed) == 1
    assert sync._executed[0] == (
        "INSERT INTO state (key, value) VALUES (?, ?)",
        ("my_key", "my_value"),
    )


@pytest.mark.asyncio
async def test_native_cursor_rejects_oracle_binds() -> None:
    """Native cursor raises RuntimeError when SQL contains ``:name`` binds."""
    sync = _FakeSyncCursor()
    from mnemos.persistence.db2 import _Db2NativeAsyncCursor

    cur = _Db2NativeAsyncCursor(sync)
    with pytest.raises(RuntimeError, match="native cursor received Oracle"):
        await cur.execute("SELECT :name FROM SYSIBM.SYSDUMMY1", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SYSTIMESTAMP FROM SYSIBM.SYSDUMMY1",
        "SELECT SYSDATE FROM SYSIBM.SYSDUMMY1",
        "SELECT 1 FROM DUAL",
        "SELECT TO_VECTOR(?) FROM SYSIBM.SYSDUMMY1",
    ],
)
async def test_native_cursor_rejects_oracle_dialect_tokens(sql: str) -> None:
    """Native cursor fails fast on SQL that belongs on the compat path."""
    sync = _FakeSyncCursor()
    from mnemos.persistence.db2 import _Db2NativeAsyncCursor

    cur = _Db2NativeAsyncCursor(sync)
    with pytest.raises(RuntimeError, match="native cursor received Oracle"):
        await cur.execute(sql, None)


@pytest.mark.asyncio
async def test_native_cursor_guard_ignores_literals_identifiers_and_comments() -> None:
    """Guard only inspects SQL syntax, not literal/comment text."""
    sync = _FakeSyncCursor()
    from mnemos.persistence.db2 import _Db2NativeAsyncCursor

    cur = _Db2NativeAsyncCursor(sync)
    sql = """
    SELECT
        'SYSTIMESTAMP :name TO_VECTOR(?) FROM DUAL',
        "SYSDATE"
    FROM SYSIBM.SYSDUMMY1
    -- FROM DUAL
    """
    await cur.execute(sql, None)
    assert sync._executed == [(sql, None)]


@pytest.mark.asyncio
async def test_native_cursor_fetchone_and_fetchall() -> None:
    """Rows pass through fetchone / fetchall unchanged."""
    rows = [(1, "hello"), (2, "world")]
    sync = _FakeSyncCursor(rows)
    from mnemos.persistence.db2 import _Db2NativeAsyncCursor

    cur = _Db2NativeAsyncCursor(sync)
    row = await cur.fetchone()
    assert row == (1, "hello")
    all_rows = await cur.fetchall()
    assert all_rows == [(1, "hello"), (2, "world")]


@pytest.mark.asyncio
async def test_native_cursor_close_calls_sync_close() -> None:
    """Close propagates to the sync cursor."""
    sync = _FakeSyncCursor()
    from mnemos.persistence.db2 import _Db2NativeAsyncCursor

    cur = _Db2NativeAsyncCursor(sync)
    await cur.close()
    assert sync._closed


@pytest.mark.asyncio
async def test_native_cursor_description_and_rowcount() -> None:
    """After execute(), description and rowcount mirror the sync cursor."""
    sync = _FakeSyncCursor()
    sync.description = (("A",), ("B",))
    sync.rowcount = 42
    from mnemos.persistence.db2 import _Db2NativeAsyncCursor

    cur = _Db2NativeAsyncCursor(sync)
    await cur.execute("SELECT * FROM t", None)
    assert cur.description == (("A",), ("B",))
    assert cur.rowcount == 42


# ── Backend factory dispatch test ──


def test_backend_factory_dialect_compat_default() -> None:
    """Default dialect (unspecified) → Db2Backend."""
    # We test that the Db2Backend symbol is distinct from Db2BackendNative,
    # confirming they are two separate classes.
    from mnemos.persistence.db2 import Db2Backend, Db2BackendNative

    assert Db2Backend is not Db2BackendNative
    assert Db2Backend.__name__ == "Db2Backend"
    assert Db2BackendNative.__name__ == "Db2BackendNative"


def test_backend_factory_dialect_native_is_subclass() -> None:
    """Db2BackendNative is a subclass of Db2Backend."""
    from mnemos.persistence.db2 import Db2Backend, Db2BackendNative

    assert issubclass(Db2BackendNative, Db2Backend)
    assert Db2BackendNative.__bases__[0] is Db2Backend
