"""``_Ops`` must hand callers materialised LOBs, never the driver handle (FB99).

The python-oracledb async driver returns CLOB columns as ``AsyncLOB``, whose
``repr()`` is a memory address. The lifecycle worker serialises rows straight
into the archive and hashes them for the deletion log, so a handle reaching a
caller destroys the only copy of the text. There is no Oracle driver in this
tree, so the fakes below model the two documented ``read()`` shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mnemos.persistence.worker_lifecycle import _Ops

# ``archive_format`` imports zstandard at module scope and the persephone extra
# is optional, so gate the payload assertions on the probe.
pytest.importorskip(
    "zstandard",
    reason="persephone subsystem not installed; pip install mnemos-core[persephone]",
)

from mnemos.persistence.archive_format import _archive_payload


class _AsyncLOB:
    """python-oracledb async CLOB: ``read()`` is a coroutine."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def read(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return "<oracledb.AsyncLOB object at 0x10b4c7d90>"


class _SyncLOB:
    """python-oracledb sync CLOB: ``read()`` returns the text directly."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return "<oracledb.LOB object at 0x10b4c7d90>"


class _Cursor:
    def __init__(self, rows, columns):
        self._rows = rows
        self.description = [(name,) for name in columns]
        self.rowcount = 0

    async def execute(self, sql, params=()):
        return self

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)

    async def close(self):
        return None


class _Conn:
    def __init__(self, rows, columns):
        self._rows = rows
        self._columns = columns

    def cursor(self):
        return _Cursor(self._rows, self._columns)

    async def execute(self, sql, params=()):
        # SQLite's path has no cursors: the connection's execute() returns one.
        return await _Cursor(self._rows, self._columns).execute(sql, params)


COLUMNS = ("id", "content", "metadata", "created")


def _enterprise_ops(rows, dialect="oracle"):
    return _Ops(SimpleNamespace(conn=_Conn(rows, COLUMNS)), dialect)


@pytest.mark.asyncio
async def test_fetchone_materializes_async_lobs_before_archiving():
    created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    ops = _enterprise_ops(
        [("m1", _AsyncLOB("the only copy of this memory"), _AsyncLOB('{"k": 1}'), created)]
    )

    row = await ops.fetchone("SELECT * FROM memories WHERE id = ?", "m1")

    assert row["content"] == "the only copy of this memory"
    assert row["metadata"] == '{"k": 1}'
    assert row["created"] == created
    payload = _archive_payload(row)
    assert payload["memory"]["content"] == "the only copy of this memory"
    assert payload["memory"]["metadata"] == {"k": 1}
    assert "AsyncLOB" not in str(payload)


@pytest.mark.asyncio
async def test_fetchone_materializes_sync_driver_lobs():
    ops = _enterprise_ops([("m2", _SyncLOB("payload"), _SyncLOB("{}"), None)])

    row = await ops.fetchone("SELECT * FROM memories WHERE id = ?", "m2")

    assert row["content"] == "payload"
    assert row["metadata"] == "{}"
    assert row["created"] is None


@pytest.mark.asyncio
async def test_fetchall_materializes_every_lob_in_every_row():
    ops = _enterprise_ops(
        [
            ("m1", _AsyncLOB("first"), _AsyncLOB('{"a": 1}'), None),
            ("m2", _AsyncLOB("second"), _AsyncLOB('{"a": 2}'), None),
        ]
    )

    rows = await ops.fetchall("SELECT * FROM memories LIMIT ?", 2)

    assert [row["content"] for row in rows] == ["first", "second"]
    assert [row["metadata"] for row in rows] == ['{"a": 1}', '{"a": 2}']


@pytest.mark.asyncio
async def test_db2_rows_are_materialized_too():
    ops = _enterprise_ops([("m1", _AsyncLOB("db2 payload"), _AsyncLOB("{}"), None)], dialect="db2")

    row = await ops.fetchone("SELECT * FROM memories WHERE id = ?", "m1")

    assert row["content"] == "db2 payload"


@pytest.mark.asyncio
async def test_sqlite_and_postgres_rows_are_returned_unchanged():
    created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    lite = _enterprise_ops([("m1", "plain text", "{}", created)], dialect="sqlite")
    assert (await lite.fetchone("SELECT * FROM memories WHERE id = ?", "m1")) == {
        "id": "m1",
        "content": "plain text",
        "metadata": "{}",
        "created": created,
    }

    class _PostgresConn:
        async def fetchrow(self, sql, *params):
            return {"id": "m1", "content": "plain text", "metadata": "{}", "created": created}

        async def fetch(self, sql, *params):
            return [{"id": "m1", "content": "plain text", "metadata": "{}", "created": created}]

    pg = _Ops(SimpleNamespace(conn=_PostgresConn()), "postgres")
    assert (await pg.fetchone("SELECT * FROM memories WHERE id = ?", "m1"))["content"] == "plain text"
    assert (await pg.fetchall("SELECT * FROM memories"))[0]["created"] == created
