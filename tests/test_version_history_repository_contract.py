"""Contract suite for NG2-MN-018: VersionRepository history reads.

``VersionRepository`` historically exposed only export/insert/fetch-by-id,
so ``GET .../versions`` and ``GET .../versions/{n}`` hand-rolled Postgres
SQL in the route (503 on every other backend). This suite pins the new
ABC surface:

* ``list_memory_versions(tx, memory_id, branch, visibility)`` returns
  version summaries ordered by ``version_num`` ASC;
* ``get_memory_version(tx, memory_id, version_num, branch, visibility)``
  returns the row or ``None``;
* both honour the per-snapshot visibility predicate: a snapshot that was
  private at v1 stays hidden to a non-owner even after a later public
  flip of the live memory (v2 world-readable).

The fake connection below emulates the Postgres predicate in Python so
the suite runs with no live database: it applies owner/world/group/ACL
filtering exactly when the emitted SQL carries the visibility clause,
and returns every row when it does not (the root path). A row is only
returned when the SQL the repository generated authorizes it.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.persistence.base import VersionRepository
from mnemos.persistence.postgres import (
    PostgresMemoryRepository,
    PostgresTransaction,
    PostgresVersionRepository,
)
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _alice() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="alice-ns", authenticated=True,
    )


def _bob() -> UserContext:
    return UserContext(
        user_id="bob", group_ids=[], role="user",
        namespace="alice-ns", authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


def _row(version_num: int, *, owner_id: str = "alice", permission_mode: int = 600) -> dict:
    return {
        "id": f"v{version_num}-id",
        "memory_id": "mem-1",
        "version_num": version_num,
        "content": f"content v{version_num}",
        "category": "solutions",
        "subcategory": None,
        "metadata": {"note": f"v{version_num}"},
        "verbatim_content": f"content v{version_num}",
        "owner_id": owner_id,
        "namespace": "alice-ns",
        "permission_mode": permission_mode,
        "group_id": None,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "snapshot_at": datetime(2026, 1, 1, 12, version_num, 0),
        "snapshot_by": owner_id,
        "change_type": "create" if version_num == 1 else "update",
        "branch": "main",
    }


def _readable(user: UserContext) -> VisibilityFilter:
    assert user.role != "root"
    return VisibilityFilter(
        scope=VisibilityScope.READABLE,
        user_id=user.user_id,
        group_ids=tuple(user.group_ids),
        namespace=user.namespace,
    )


def _root_bypass() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )


class _FakePGConn:
    """Minimal asyncpg-shaped double that enforces the emitted predicate.

    Candidate rows are fixed; the double evaluates owner/world/group/ACL
    visibility in Python against the caller identity carried in the query
    args — but ONLY when the SQL actually contains the visibility clause.
    SQL without the clause (the root path) returns every candidate, so a
    repository that forgot the predicate would leak the private snapshot
    and fail the suite.
    """

    def __init__(self, rows: list[dict], *, acl: set[tuple[str, str]] | None = None):
        self._rows = rows
        self._acl = acl or set()
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    @staticmethod
    def _caller_of(args: tuple, prefix: int) -> tuple[str, list, str]:
        caller = args[prefix]
        groups = list(args[prefix + 1])
        namespace = args[-1]
        return caller, groups, namespace

    def _visible(self, row: dict, caller: str, groups: list, namespace: str) -> bool:
        if row.get("namespace") != namespace:
            return False
        if row.get("owner_id") == caller:
            return True
        mode = row.get("permission_mode") or 0
        if (mode % 10) >= 4:
            return True
        if ((mode // 10) % 10) >= 4 and row.get("group_id") in groups:
            return True
        return (row.get("memory_id"), caller) in self._acl

    def _apply(self, sql: str, args: tuple, candidates: list[dict]) -> list[dict]:
        if "permission_mode % 10" not in sql:
            return sorted(candidates, key=lambda r: r["version_num"])
        if "version_num = ANY(" in sql:
            prefix = 3  # memory_id=$1, nums=$2, branch=$3, vis@$4...
        else:
            prefix = 3 if "version_num = $2" in sql else 2
        caller, groups, namespace = self._caller_of(args, prefix)
        kept = [r for r in candidates if self._visible(r, caller, groups, namespace)]
        return sorted(kept, key=lambda r: r["version_num"])

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        if "version_num = ANY(" in sql:
            candidates = [
                r for r in self._rows
                if r["memory_id"] == args[0] and r["version_num"] in args[1] and r.get("branch") == args[2]
            ]
            return self._apply(sql, args, candidates)
        candidates = [r for r in self._rows if r["memory_id"] == args[0] and r.get("branch") == args[1]]
        return self._apply(sql, args, candidates)

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        if sql.strip().startswith("SELECT 1 FROM memories"):
            return {"ok": 1} if args[0] == "mem-1" else None
        if sql.strip().startswith("SELECT 1 FROM memory_versions"):
            return {"ok": 1} if any(r["memory_id"] == args[0] for r in self._rows) else None
        candidates = [
            r for r in self._rows
            if r["memory_id"] == args[0] and r["version_num"] == args[1] and r.get("branch") == args[2]
        ]
        kept = self._apply(sql, args, candidates)
        return kept[0] if kept else None


def _tx(conn: _FakePGConn) -> PostgresTransaction:
    return PostgresTransaction(conn, MagicMock())


def _seed() -> list[dict]:
    # v1 was snapshotted while the memory was private (600); v2 after the
    # flip to world-readable (644). Bob was never the owner.
    return [_row(1, permission_mode=600), _row(2, permission_mode=644)]


def test_abc_exposes_history_reads() -> None:
    assert "list_memory_versions" in VersionRepository.__abstractmethods__
    assert "get_memory_version" in VersionRepository.__abstractmethods__
    assert "fetch_version_contents" in VersionRepository.__abstractmethods__
    assert hasattr(PostgresVersionRepository(), "list_memory_versions")
    assert hasattr(PostgresVersionRepository(), "get_memory_version")
    assert hasattr(PostgresVersionRepository(), "fetch_version_contents")


def test_list_returns_ordered_summaries() -> None:
    import asyncio

    conn = _FakePGConn(_seed())
    rows = asyncio.run(
        PostgresVersionRepository().list_memory_versions(_tx(conn), "mem-1", "main", _readable(_alice()))
    )
    assert [r["version_num"] for r in rows] == [1, 2]
    sql = conn.fetch_calls[-1][0]
    assert "ORDER BY version_num ASC" in sql
    assert "memory_id = $1 AND branch = $2" in sql


def test_private_snapshot_stays_hidden_after_public_flip() -> None:
    import asyncio

    repo = PostgresVersionRepository()
    # Bob sees only the world-readable v2, never the private v1.
    conn = _FakePGConn(_seed())
    rows = asyncio.run(repo.list_memory_versions(_tx(conn), "mem-1", "main", _readable(_bob())))
    assert [r["version_num"] for r in rows] == [2]

    # Per-version read agrees: v1 is None for Bob, the row for Alice.
    assert asyncio.run(repo.get_memory_version(_tx(conn), "mem-1", 1, "main", _readable(_bob()))) is None
    v2 = asyncio.run(repo.get_memory_version(_tx(conn), "mem-1", 2, "main", _readable(_bob())))
    assert v2 is not None and v2["version_num"] == 2
    v1 = asyncio.run(repo.get_memory_version(_tx(conn), "mem-1", 1, "main", _readable(_alice())))
    assert v1 is not None and v1["version_num"] == 1


def test_root_sees_every_snapshot() -> None:
    import asyncio

    repo = PostgresVersionRepository()
    conn = _FakePGConn(_seed())
    rows = asyncio.run(repo.list_memory_versions(_tx(conn), "mem-1", "main", _root_bypass()))
    assert [r["version_num"] for r in rows] == [1, 2]
    v1 = asyncio.run(repo.get_memory_version(_tx(conn), "mem-1", 1, "main", _root_bypass()))
    assert v1 is not None and v1["version_num"] == 1


def test_get_missing_version_returns_none() -> None:
    import asyncio

    conn = _FakePGConn(_seed())
    assert (
        asyncio.run(
            PostgresVersionRepository().get_memory_version(
                _tx(conn), "mem-1", 99, "main", _readable(_alice())
            )
        )
        is None
    )


def test_fetch_contents_honours_per_snapshot_visibility() -> None:
    """The diff query omits snapshots the caller could not read directly.

    Bob (never the owner) gets only the world-readable v2, so the route
    404s instead of diffing text he must never see. Alice (owner) and
    root get both sides.
    """
    import asyncio

    repo = PostgresVersionRepository()
    conn = _FakePGConn(_seed())
    bobs = asyncio.run(repo.fetch_version_contents(_tx(conn), "mem-1", [1, 2], "main", _readable(_bob())))
    assert [r["version_num"] for r in bobs] == [2]

    conn = _FakePGConn(_seed())
    alices = asyncio.run(repo.fetch_version_contents(_tx(conn), "mem-1", [1, 2], "main", _readable(_alice())))
    assert [r["version_num"] for r in alices] == [1, 2]

    conn = _FakePGConn(_seed())
    roots = asyncio.run(repo.fetch_version_contents(_tx(conn), "mem-1", [1, 2], "main", _root_bypass()))
    assert [r["version_num"] for r in roots] == [1, 2]
    sql = conn.fetch_calls[-1][0]
    assert "version_num = ANY($2::int[])" in sql


def test_assert_readable_still_gates_live_memory() -> None:
    import asyncio

    conn = _FakePGConn(_seed())
    asyncio.run(PostgresMemoryRepository().assert_memory_readable(_tx(conn), "mem-1", _alice()))
    with pytest.raises(PermissionError):
        asyncio.run(PostgresMemoryRepository().assert_memory_readable(_tx(conn), "nope", _alice()))
