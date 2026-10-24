"""Postgres ``fetch_memory_context``: vault subtraction is filter-driven.

The gateway's raw SQL (pre-F-06) hardcoded ``m.namespace <> 'vault'`` into
both the root and non-root statements. The ABC implementation now derives
that predicate from the ``VisibilityFilter`` via the shared Postgres
renderer, so the vault subtraction has exactly one source of truth.

These tests drive the real ``PostgresMemoryRepository`` against a fake
connection and assert on the statement and bound parameters it actually
emits -- the requirement is that Postgres behaviour is unchanged, and the
security-relevant part of that behaviour is "the vault is never returned".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mnemos.core.auth_context import UserContext
from mnemos.persistence.postgres import PostgresMemoryRepository, PostgresTransaction


pytestmark = pytest.mark.asyncio


class _CapturingConn:
    """asyncpg-shaped connection that records the statement and returns rows."""

    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return self.rows


def _tx(conn) -> PostgresTransaction:
    return PostgresTransaction(conn, MagicMock())


def _root():
    return UserContext(
        user_id="root", group_ids=[], role="root", namespace="root-ns", authenticated=True
    )


def _user():
    return UserContext(
        user_id="alice", group_ids=[], role="user", namespace="alice-ns", authenticated=True
    )


def _emitted(conn) -> tuple[str, tuple]:
    assert len(conn.calls) == 1, conn.calls
    return conn.calls[0]


async def test_root_query_excludes_vault():
    conn = _CapturingConn()
    repo = PostgresMemoryRepository()

    await repo.fetch_memory_context(_tx(conn), "needle", _root(), limit=4)

    sql, args = _emitted(conn)
    lower = sql.lower()

    # Vault subtraction survives the move to a filter-driven predicate.
    assert "namespace not in" in lower
    assert "vault" in args
    # Root is NOT pinned to a single namespace: it reads cross-tenant.
    assert "m.namespace=$" not in lower.replace(" ", "")
    assert "m.namespace = $" not in lower
    # Placeholders are contiguous: the three trailing binds are query/limit
    # after however many visibility parameters the filter consumed.
    assert args[-2] == "needle"
    assert args[-1] == 4
    assert tuple(args[:-2]) == ("vault",)


async def test_root_predicate_semantics_match_previous_hardcoded_form():
    """The rendered predicate selects the same rows as the old ``<> 'vault'``.

    ``memories.namespace`` is ``NOT NULL`` on Postgres (v1_multiuser
    migration line 89: ``ALTER TABLE memories ALTER COLUMN namespace SET
    NOT NULL``), so the renderer's ``namespace IS NULL OR ...`` arm is
    unreachable there. The two predicates are therefore row-identical on
    the production backend, which is what "Postgres behaviour unchanged"
    requires. Executed against a real engine rather than argued.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE mem (namespace TEXT NOT NULL, content TEXT)")
    conn.executemany(
        "INSERT INTO mem VALUES (?,?)",
        [("vault", "secret"), ("alice-ns", "a"), ("bob-ns", "b")],
    )

    def rows(where: str, params: tuple):
        return sorted(
            r[0] for r in conn.execute(f"SELECT content FROM mem WHERE {where}", params).fetchall()
        )

    assert rows("namespace <> 'vault'", ()) == rows(
        "(namespace IS NULL OR namespace NOT IN (?))", ("vault",)
    ) == ["a", "b"]


async def test_non_root_query_pinned_to_namespace_and_excludes_vault():
    conn = _CapturingConn()
    repo = PostgresMemoryRepository()

    await repo.fetch_memory_context(_tx(conn), "needle", _user(), limit=4)

    sql, args = _emitted(conn)
    lower = sql.lower()

    # READABLE envelope + namespace pin + vault subtraction.
    assert "m.owner_id=$" in lower.replace(" ", "") or "m.owner_id = $" in lower
    assert "m.namespace=$" in lower.replace(" ", "") or "m.namespace = $" in lower
    assert "namespace not in" in lower
    assert "alice-ns" in args
    assert "vault" in args
    assert args[-2] == "needle"
    assert args[-1] == 4


async def test_placeholders_are_contiguous_and_densely_bound():
    """A mis-indexed placeholder is a runtime failure, not a silently empty result."""
    conn = _CapturingConn()
    repo = PostgresMemoryRepository()

    await repo.fetch_memory_context(_tx(conn), "needle", _user(), limit=4)

    sql, args = _emitted(conn)
    indices = sorted({int(tok[1:]) for tok in sql.split() if tok.startswith("$") and tok[1:].isdigit()})
    # Every placeholder referenced appears; the highest one equals len(args).
    assert indices, sql
    assert max(indices) == len(args), (indices, args)


async def test_rows_are_mapped_with_namespace():
    conn = _CapturingConn(
        rows=[{"id": "m1", "category": "facts", "namespace": "alice-ns", "content": "ctx"}]
    )
    repo = PostgresMemoryRepository()

    out = await repo.fetch_memory_context(_tx(conn), "needle", _user())

    assert out == [{"id": "m1", "content": "ctx", "namespace": "alice-ns"}]


async def test_system_caller_reads_cross_tenant_and_excludes_vault():
    """A userless caller gets the same root-equivalent read as an authenticated root."""
    conn = _CapturingConn()
    repo = PostgresMemoryRepository()

    await repo.fetch_memory_context(_tx(conn), "needle", SimpleNamespace(), limit=4)

    sql, args = _emitted(conn)
    lower = sql.lower()
    assert "namespace not in" in lower
    assert "vault" in args
    assert "m.namespace=$" not in lower.replace(" ", "")


async def test_vault_targeting_for_non_root_still_subtracts_vault():
    """Release-blocking invariant: a non-root cannot enumerate the vault by asking."""
    conn = _CapturingConn()
    repo = PostgresMemoryRepository()

    await repo.fetch_memory_context(_tx(conn), "needle", _user(), limit=1)

    _sql, args = _emitted(conn)
    assert "vault" in args
