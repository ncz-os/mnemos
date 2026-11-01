"""Pin the rendered MySQL ``consolidate_duplicate_memories`` UPDATE.

The duplicates-side replica fence is spliced into the UPDATE through an
f-string (``{duplicate_guard}``, alongside ``{placeholders}``). A dropped
``f`` prefix would still be valid Python and would ship the literal braces to
the server, so the statement the repository actually executes is captured and
checked for both a fenced and an unfenced run. No MySQL server is needed: a
recording cursor stands in for aiomysql.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnemos.persistence.mysql import MysqlMemoryRepository

FENCE_ENV = "MNEMOS_FEDERATION_REPLICA_FENCE"


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.rowcount = 2

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))

    async def fetchone(self):
        return (1,)  # the canonical guard SELECT finds a live local row


class _Conn:
    def __init__(self) -> None:
        self.cursor_obj = _RecordingCursor()

    def cursor(self):
        return self.cursor_obj


async def _render_update(monkeypatch, fence: str) -> tuple[str, tuple]:
    monkeypatch.setenv(FENCE_ENV, fence)
    conn = _Conn()
    merged = await MysqlMemoryRepository().consolidate_duplicate_memories(
        SimpleNamespace(conn=conn),
        canonical_id="canon",
        duplicate_ids=["dup1", "dup2"],
    )
    assert merged == 2
    updates = [stmt for stmt in conn.cursor_obj.statements if "UPDATE memories" in stmt[0]]
    assert len(updates) == 1, conn.cursor_obj.statements
    return updates[0]


def _collapse(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.asyncio
async def test_fenced_update_interpolates_the_duplicate_guard(monkeypatch):
    sql, params = await _render_update(monkeypatch, "on")

    assert "{" not in sql and "}" not in sql, f"uninterpolated f-string field in: {sql}"
    collapsed = _collapse(sql)
    assert "WHERE id IN (%s, %s)" in collapsed
    assert collapsed.endswith("AND consolidated_into IS NULL AND federation_source IS NULL")
    assert collapsed.count("%s") == len(params) == 4
    assert params == ("canon", "dup1", "dup2", "canon")


@pytest.mark.asyncio
async def test_unfenced_update_renders_no_guard(monkeypatch):
    sql, params = await _render_update(monkeypatch, "off")

    assert "{" not in sql and "}" not in sql, f"uninterpolated f-string field in: {sql}"
    collapsed = _collapse(sql)
    assert "WHERE id IN (%s, %s)" in collapsed
    assert "federation_source" not in collapsed
    assert collapsed.endswith("AND consolidated_into IS NULL")
    assert collapsed.count("%s") == len(params) == 4
