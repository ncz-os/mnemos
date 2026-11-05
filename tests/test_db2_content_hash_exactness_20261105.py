"""The Db2 dedup sweep and backfill must never digest a truncated CLOB.

Measured on Db2 LUW 12.1.5 community: ``HASH_SHA256`` does not exist
(SQL0440N), ``HASH(expr, 2)`` is SHA-256 but rejects a CLOB (SQL-171), and
``CAST(clob AS VARCHAR(32672))`` truncates past 32,672 bytes. A sweep grouping
on a truncated digest merges two distinct long documents and consolidates one
of them away, so the identity has to be ``content_hash`` -- the exact SHA-256
of the newline-normalized body that every write path computes in Python.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DB2_SOURCE = Path(__file__).resolve().parents[1] / "mnemos" / "persistence" / "db2.py"


def _normalized(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


class _BackfillCursor:
    """Serves one SELECT batch, then records the UPDATE parameters."""

    def __init__(self, batch: list[tuple[str, str]]) -> None:
        self._batch = batch
        self.statements: list[tuple[str, Any]] = []
        self.description: Any = None
        self.rowcount = 0

    async def execute(self, sql: Any, params: Any = ()) -> "_BackfillCursor":
        text = " ".join(str(sql).split())
        self.statements.append((text, params))
        upper = text.upper()
        if upper.startswith("SELECT"):
            self.description = [("id",), ("content",)]
            self.rowcount = 0
        else:
            self.description = None
            self.rowcount = 1
        return self

    async def fetchall(self) -> list[tuple[str, str]]:
        return list(self._batch)

    async def fetchone(self) -> Any:
        return None

    async def close(self) -> None:
        return None


class _SweepCursor:
    def __init__(self, calls: list[Any]) -> None:
        self._calls = calls
        self.description: Any = [
            ("owner_id",),
            ("namespace",),
            ("content_hash",),
            ("duplicate_count",),
            ("memory_ids",),
            ("keep_id",),
            ("canonical_id",),
        ]
        self.rowcount = 0

    async def execute(self, sql: Any, params: Any = ()) -> "_SweepCursor":
        self._calls.append((" ".join(str(sql).split()), params))
        return self

    async def fetchall(self) -> list[Any]:
        return []

    async def fetchone(self) -> Any:
        return None

    async def close(self) -> None:
        return None


def test_db2_source_has_no_unavailable_or_truncating_digest() -> None:
    source = DB2_SOURCE.read_text()
    assert "HASH_SHA256" not in source
    # Any SQL digest here can only be HASH(CAST(... AS VARCHAR(32672)), 2): the
    # function rejects a CLOB (SQL-171) and the cast truncates past 32,672
    # bytes, so no SQL digest belongs in this backend at all.
    assert "HEX(HASH(" not in source


@pytest.mark.asyncio
async def test_db2_backfill_hashes_the_whole_body_in_python() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    long_body = "a" * 33000 + "tail-one"
    cursor = _BackfillCursor([("mem-1", long_body)])
    repo = Db2MemoryRepository()
    applied = await repo.backfill_missing_content_hashes(
        SimpleNamespace(conn=SimpleNamespace(cursor=lambda: cursor)),
        apply=True,
        batch_size=10,
    )
    assert applied == 1
    updates = [(sql, params) for sql, params in cursor.statements if sql.upper().startswith("UPDATE")]
    assert len(updates) == 1
    digest = updates[0][1][0]
    assert digest == hashlib.sha256(_normalized(long_body).encode("utf-8")).hexdigest()
    truncated = hashlib.sha256(_normalized(long_body)[:32672].encode("utf-8")).hexdigest()
    assert digest != truncated


@pytest.mark.asyncio
async def test_db2_sweep_groups_by_content_hash_never_by_a_bounded_digest() -> None:
    from mnemos.persistence.db2 import Db2MemoryRepository

    calls: list[Any] = []
    repo = Db2MemoryRepository()
    await repo.find_duplicate_content_groups(
        SimpleNamespace(conn=SimpleNamespace(cursor=lambda: _SweepCursor(calls)))
    )
    assert calls, "the sweep issued no statement"
    sql = " ".join(calls[0][0].upper().split())
    assert "GROUP BY OWNER_ID, NAMESPACE, CONTENT_HASH" in sql
    assert "HASH_SHA256" not in sql
    assert "VARCHAR(32672)" not in sql
    assert "NORMALIZED_CONTENT" not in sql
