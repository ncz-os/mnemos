"""Listing / paging / FTS audit fixes (2026-11-03).

Covers the cloud audit findings that were still open on master after
FB135 (soft-delete filters) and FB137 (list_memories id tiebreaker):

- F2  SQLite FTS MATCH receives raw user text (operators and column
      filters live, malformed quotes kill the FTS arm); the search request
      has no query length cap, so a 200k-char query reaches the LIKE
      fallback and raises "LIKE or GLOB pattern too complex" (500).
- F3  SQLite fetch_provider_history / fetch_history order by second-
      resolution timestamps with a random-hex id tiebreak, so turns that
      land in the same second come back shuffled. Insertion order (rowid)
      is the tiebreak now.
- F5  MemorySearchRequest.limit accepts 0 and negatives (-1 is unbounded
      on SQLite).
- F6  Postgres fts_search runs the ILIKE fallback in the transaction the
      failed FTS statement aborted, so the fallback dies with
      InFailedSQLTransactionError instead of returning rows.
- F7  LIKE / ILIKE fallbacks treat % and _ in the user query as wildcards.
- F8  ORDER BYs without a unique tiebreaker (fts rank, LIKE fallback,
      kg triples, webhook lists, consultations, session history).

Live arm: SQLite. Postgres and MySQL are exercised through recording
fakes (no servers here); the Postgres fake models the server rule that
matters for F6: after a statement error every further statement in the
same transaction fails until a ROLLBACK (TO SAVEPOINT). Real Postgres and
MySQL execution of the changed SQL stays unproven in this file.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mnemos.domain.models import MemorySearchRequest
from mnemos.persistence import mysql as mysql_mod
from mnemos.persistence import postgres as postgres_mod
from mnemos.persistence import sqlite as sqlite_mod
from mnemos.persistence.mysql import (
    MysqlConsultationAuditRepository,
    MysqlKGRepository,
    MysqlMemoryRepository,
    MysqlWebhookRepository,
)
from mnemos.persistence.postgres import (
    PostgresMemoryRepository,
    PostgresSessionsRepository,
    PostgresTransaction,
    PostgresWebhookRepository,
)
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

_TS = "2026-11-03 10:00:00"


def _vis() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )


def _collapsed(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    from types import SimpleNamespace as _NS

    monkeypatch.setenv("MNEMOS_EMBEDDING_DIM", "3")
    settings = _NS(database=_NS(embedding_dim=3))
    instance = SqliteBackend(tmp_path / "listing_fts.db", settings)
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def _seed(backend, content: str, *, memory_id: str | None = None, ts: Any = None) -> str:
    mid = memory_id or f"lf_{uuid.uuid4().hex[:12]}"
    stamp = ts if ts is not None else datetime.now(timezone.utc)
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=mid,
            content=content,
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=80,
            owner_id="lf-user",
            namespace="lf-ns",
            permission_mode=600,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=content,
            embedding=None,
            created=stamp,
            updated=stamp,
        )
    return mid


async def _fts_ids(backend, query: str, limit: int = 50) -> list[str]:
    async with backend.transactional() as tx:
        rows = await backend.memories.fts_search(tx, query=query, limit=limit, visibility=_vis())
    return [row["id"] for row in rows]


# ─────────────────────────── F2: SQLite FTS MATCH sanitising ────────────────


@pytest.mark.asyncio
async def test_sqlite_fts_or_operator_is_literal(sqlite_backend):
    await _seed(sqlite_backend, "alpha apple orchard")
    gamma = await _seed(sqlite_backend, "gamma grape vineyard")
    ids = await _fts_ids(sqlite_backend, "alpha OR gamma")
    assert gamma not in ids


@pytest.mark.asyncio
async def test_sqlite_fts_column_filter_is_literal(sqlite_backend):
    gamma = await _seed(sqlite_backend, "gamma grape vineyard")
    ids = await _fts_ids(sqlite_backend, "content:gamma")
    assert gamma not in ids


@pytest.mark.asyncio
async def test_sqlite_fts_unbalanced_quote_still_matches_token(sqlite_backend):
    alpha = await _seed(sqlite_backend, "alpha apple orchard")
    ids = await _fts_ids(sqlite_backend, 'alpha"')
    assert ids == [alpha]


def test_search_request_rejects_oversized_query():
    with pytest.raises(ValidationError):
        MemorySearchRequest(query="x" * 200_000)
    # The cap is generous: ordinary long queries still validate.
    assert len(MemorySearchRequest(query="y" * 10_000).query) == 10_000


def test_oversized_query_is_422_not_500():
    app = FastAPI()

    @app.post("/search")
    async def _search(request: MemorySearchRequest) -> dict[str, int]:
        return {"n": len(request.query)}

    client = TestClient(app)
    assert client.post("/search", json={"query": "z" * 200_000}).status_code == 422
    assert client.post("/search", json={"query": "ok"}).status_code == 200


# ─────────────────────────── F5: limit >= 1 ─────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, -500])
def test_search_request_limit_must_be_positive(bad):
    with pytest.raises(ValidationError):
        MemorySearchRequest(query="q", limit=bad)


def test_search_request_limit_upper_bound_unchanged():
    # The route clamps to the profile cap / 500; the model must not start
    # rejecting large limits.
    assert MemorySearchRequest(query="q", limit=100_000).limit == 100_000
    assert MemorySearchRequest(query="q").limit == 10


# ─────────────────────────── F7: LIKE wildcards are literal ─────────────────


@pytest.mark.asyncio
async def test_sqlite_like_fallback_percent_is_literal(sqlite_backend):
    pct = await _seed(sqlite_backend, "discount 100% off today")
    await _seed(sqlite_backend, "plain words only")
    assert await _fts_ids(sqlite_backend, "%") == [pct]


@pytest.mark.asyncio
async def test_sqlite_like_fallback_underscore_is_literal(sqlite_backend):
    snake = await _seed(sqlite_backend, "variable snake_case name")
    await _seed(sqlite_backend, "plain words only")
    assert await _fts_ids(sqlite_backend, "_") == [snake]


@pytest.mark.asyncio
async def test_sqlite_like_fallback_backslash_is_literal(sqlite_backend):
    win = await _seed(sqlite_backend, r"path C:\temp\x here")
    await _seed(sqlite_backend, "plain words only")
    assert await _fts_ids(sqlite_backend, "\\") == [win]


# ─────────────────────────── F8: SQLite FTS / LIKE tiebreak ─────────────────


@pytest.mark.asyncio
async def test_sqlite_fts_rank_ties_break_on_id(sqlite_backend):
    ids = [await _seed(sqlite_backend, "tiebreak token body", ts=_TS) for _ in range(8)]
    got = await _fts_ids(sqlite_backend, "tiebreak")
    assert sorted(got) == sorted(ids)
    assert got == sorted(ids)


@pytest.mark.asyncio
async def test_sqlite_like_fallback_ties_break_on_id(sqlite_backend):
    ids = [await _seed(sqlite_backend, "tiebreak token body", ts=_TS) for _ in range(8)]
    # "ebreak tok" is no FTS token, so the LIKE fallback answers.
    got = await _fts_ids(sqlite_backend, "ebreak tok")
    assert got == sorted(ids)


# ─────────────────────────── F3: SQLite session ordering ────────────────────


async def _session_with_turns(backend, turns: int) -> tuple[str, list[str]]:
    async with backend.transactional() as tx:
        session = await backend.sessions.create_session(
            tx, user_id="lf-user", namespace="lf-ns", model="m", initial_context="SYSTEM-0"
        )
    session_id = session["id"]
    contents: list[str] = []
    for i in range(turns):
        content = f"turn-{i:02d}"
        contents.append(content)
        async with backend.transactional() as tx:
            await backend.sessions.add_message(
                tx,
                session_id=session_id,
                role="user" if i % 2 == 0 else "assistant",
                content=content,
                model="m",
            )
    # Every turn lands in the same CURRENT_TIMESTAMP second in production
    # when the provider answers fast; pin that worst case.
    async with backend.transactional() as tx:
        await sqlite_mod._execute(
            backend.memories._conn(tx),
            "UPDATE session_messages SET timestamp = ? WHERE session_id = ?",
            (_TS, session_id),
        )
    return session_id, contents


@pytest.mark.asyncio
async def test_sqlite_provider_history_keeps_insertion_order(sqlite_backend):
    session_id, contents = await _session_with_turns(sqlite_backend, 24)
    async with sqlite_backend.transactional() as tx:
        rows = await sqlite_backend.sessions.fetch_provider_history(tx, session_id)
    assert [row["content"] for row in rows] == ["SYSTEM-0", *contents[-10:]]


@pytest.mark.asyncio
async def test_sqlite_history_pages_in_insertion_order(sqlite_backend):
    session_id, contents = await _session_with_turns(sqlite_backend, 24)
    seen: list[str] = []
    async with sqlite_backend.transactional() as tx:
        for offset in range(0, 25, 7):
            rows, total = await sqlite_backend.sessions.fetch_history(tx, session_id, 7, offset)
            assert total == 25
            seen.extend(row["content"] for row in rows)
    assert seen == ["SYSTEM-0", *contents]


# ─────────────────────────── F8: SQLite emitted SQL (no seed path) ──────────


@pytest.mark.asyncio
async def test_sqlite_other_listings_carry_id_tiebreak(sqlite_backend, monkeypatch):
    calls: list[str] = []

    async def _spy(conn: Any, sql: str, params: Any = ()) -> list[Any]:
        calls.append(_collapsed(sql))
        return []

    monkeypatch.setattr(sqlite_mod, "_fetch_all", _spy)
    async with sqlite_backend.transactional() as tx:
        await sqlite_backend.kg_triples.search_triples(tx, "x")
        await sqlite_backend.webhooks.fetch_deliveries(tx)
        await sqlite_backend.webhooks.fetch_deliveries(tx, "sub")
        await sqlite_backend.webhooks.list_deliveries(tx, subscription_id="sub", owner_id=None, namespace=None, limit=5)
        await sqlite_backend.webhooks.list_subscriptions(
            tx, owner_id=None, namespace=None, include_revoked=True, limit=5
        )
        await sqlite_backend.list_consultations(tx)
    assert len(calls) == 6
    assert calls[0].endswith("ORDER BY valid_from ASC, created ASC, id ASC LIMIT ?")
    assert calls[1].endswith("ORDER BY created_at ASC, id ASC")
    assert calls[2].endswith("ORDER BY created_at ASC, id ASC")
    assert "d.created_at DESC, d.id ASC LIMIT ?" in calls[3]
    assert "created_at DESC, id ASC LIMIT ?" in calls[4]
    assert calls[5].endswith("ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?")


# ─────────────────────────── F6/F7/F8: Postgres fts_search ──────────────────


class _PgAbortingConn:
    """asyncpg stand-in that enforces Postgres' aborted-transaction rule.

    A statement matching ``fail_on`` raises; afterwards every statement
    fails with InFailedSQLTransactionError until the innermost savepoint
    opened before the failure is rolled back.
    """

    def __init__(self, fail_on: str | None, rows: list[Any]) -> None:
        self.fail_on = fail_on
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.aborted = False
        self.savepoints = 0

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        if self.aborted:
            raise asyncpg.exceptions.InFailedSQLTransactionError(
                "current transaction is aborted, commands ignored until end of transaction block"
            )
        self.calls.append((_collapsed(sql), args))
        if self.fail_on and self.fail_on in sql:
            self.aborted = True
            raise asyncpg.exceptions.UndefinedFunctionError("text search configuration missing")
        if "ILIKE" in sql:
            return list(self.rows)
        return []

    def transaction(self, **_: Any) -> Any:
        conn = self

        class _Nested:
            async def start(self) -> None:
                conn.savepoints += 1

            async def commit(self) -> None:
                conn.savepoints -= 1

            async def rollback(self) -> None:
                conn.savepoints -= 1
                conn.aborted = False

            async def __aenter__(self) -> "_Nested":
                await self.start()
                return self

            async def __aexit__(self, exc_type: Any, *_: Any) -> bool:
                if exc_type is None:
                    await self.commit()
                else:
                    await self.rollback()
                return False

        return _Nested()


@pytest.mark.asyncio
async def test_postgres_fts_failure_falls_back_outside_aborted_tx():
    conn = _PgAbortingConn(fail_on="plainto_tsquery", rows=[{"id": "fallback-row"}])
    tx = PostgresTransaction(conn, SimpleNamespace())
    rows = await PostgresMemoryRepository().fts_search(tx, query="anything", limit=5, visibility=_vis())
    assert rows == [{"id": "fallback-row"}]
    assert conn.savepoints == 0


@pytest.mark.asyncio
async def test_postgres_fts_and_ilike_sql_shape():
    conn = _PgAbortingConn(fail_on=None, rows=[])
    tx = PostgresTransaction(conn, SimpleNamespace())
    await PostgresMemoryRepository().fts_search(tx, query="100%_\\x", limit=5, visibility=_vis())
    fts_sql, _ = conn.calls[0]
    ilike_sql, ilike_args = conn.calls[1]
    assert fts_sql.endswith("ORDER BY rank DESC, id ASC LIMIT $2")
    assert "content ILIKE $1 ESCAPE '\\'" in ilike_sql
    assert ilike_sql.endswith("ORDER BY created DESC, id ASC LIMIT $2")
    assert ilike_args[0] == "%100\\%\\_\\\\x%"
    assert conn.savepoints == 0


# ─────────────────────────── F8: Postgres sessions / webhooks ───────────────


class _PgSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append(_collapsed(sql))
        return []

    async def fetchval(self, sql: str, *args: Any) -> int:
        self.calls.append(_collapsed(sql))
        return 0


@pytest.mark.asyncio
async def test_postgres_history_and_webhook_lists_carry_id_tiebreak(monkeypatch):
    spy = _PgSpy()
    monkeypatch.setattr(postgres_mod, "_postgres_tx", lambda tx: SimpleNamespace(conn=spy))
    await PostgresSessionsRepository().fetch_history(None, "s", 10, 0)
    hooks = PostgresWebhookRepository()
    await hooks.fetch_deliveries(None)
    await hooks.fetch_deliveries(None, "sub")
    await hooks.list_deliveries(None, subscription_id="sub", owner_id=None, namespace=None, limit=5)
    await hooks.list_subscriptions(None, owner_id=None, namespace=None, include_revoked=True, limit=5)
    assert "ORDER BY timestamp ASC, id ASC LIMIT $2 OFFSET $3" in spy.calls[0]
    assert spy.calls[2].endswith("ORDER BY created ASC, id ASC")
    assert spy.calls[3].endswith("ORDER BY created ASC, id ASC")
    assert "ORDER BY d.created DESC, d.id ASC LIMIT" in spy.calls[4]
    assert "ORDER BY created DESC, id ASC LIMIT" in spy.calls[5]


# ─────────────────────────── F8: MySQL recording fakes ──────────────────────


class _MysqlCursorSpy:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.description = (("X",),)

    async def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append(_collapsed(sql))

    async def fetchone(self) -> tuple[int]:
        return (0,)

    async def fetchall(self) -> list[Any]:
        return []


class _MysqlConnSpy:
    def __init__(self, calls: list[str]) -> None:
        self._cursor = _MysqlCursorSpy(calls)

    def cursor(self, *a: Any, **k: Any) -> Any:
        cursor = self._cursor

        class _CM:
            async def __aenter__(self) -> _MysqlCursorSpy:
                return cursor

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _CM()


@pytest.mark.asyncio
async def test_mysql_listings_carry_id_tiebreak():
    calls: list[str] = []
    tx = mysql_mod._MysqlTransaction(_MysqlConnSpy(calls))
    await MysqlMemoryRepository().fts_search(tx, query="q", limit=5, visibility=_vis())
    await MysqlKGRepository().search_triples(tx, "x")
    hooks = MysqlWebhookRepository()
    await hooks.fetch_deliveries(tx)
    await hooks.list_deliveries(tx, subscription_id="sub", owner_id=None, namespace=None, limit=5)
    await hooks.list_subscriptions(tx, owner_id=None, namespace=None, include_revoked=True, limit=5)
    await MysqlConsultationAuditRepository().fetch_consultations(tx, limit=5, offset=0)
    joined = "\n".join(calls)
    assert "ORDER BY rank_score DESC, m.id ASC" in joined
    assert "ORDER BY valid_from ASC, created ASC, id ASC LIMIT %s" in joined
    assert "ORDER BY created ASC, id ASC" in joined
    assert "ORDER BY d.created DESC, d.id ASC LIMIT %s" in joined
    assert "ORDER BY created DESC, id ASC LIMIT %s" in joined
    assert "ORDER BY created DESC, id ASC LIMIT %s OFFSET %s" in joined


@pytest.mark.asyncio
async def test_sqlite_fts_non_string_query_takes_like_rescue(sqlite_backend):
    # The pre-escape LIKE path formatted f"%{query}%"; a non-str query must
    # still reach the LIKE rescue instead of raising AttributeError.
    assert await _fts_ids(sqlite_backend, None) == []
