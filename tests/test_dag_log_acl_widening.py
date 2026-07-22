"""Post-walk ACL widening for the DAG /log endpoint (GitLab #2 ncz-os/mnemos#2).

The widened version_visibility_predicate composes a memory_acl EXISTS
disjunct at SQL level, but the /log endpoint composes a recursive CTE
that historically couldn't take that disjunct cleanly. The DAG route
handles widening by:

  1. calling ``_assert_memory_readable`` (which uses
     ``read_visibility_predicate`` — already ACL-aware post-merge),
  2. running the recursive CTE without the ACL disjunct for the bulk
     fetch, then
  3. applying a CLIENT-SIDE filter that mirrors the snapshot predicate
     MINUS ACL and PLUS a one-shot memory_acl lookup that widens read
     to every snapshot of an ACL-granted memory.

These tests pin step 3: when the preflight gate says the caller has an
ACL grant (mocked here), ALL snapshots of the memory that pass the
namespace/owner/world/group predicate are kept in the result. Without
the grant, the client-side filter reverts to the pre-#2 owner/world
plus the new group branch.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import dag as dag_handler


def _user(
    user_id: str = "alice",
    *,
    role: str = "user",
    groups: list[str] | None = None,
    ns: str = "alice-ns",
) -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=list(groups or []),
        role=role,
        namespace=ns,
        authenticated=True,
    )


def _version(
    *,
    version_id: str,
    commit_hash: str,
    version_num: int,
    parent_version_id: str | None,
    parent_commit_hash: str | None,
    owner_id: str,
    permission_mode: int,
    group_id: str | None = None,
):
    return {
        "id": version_id,
        "commit_hash": commit_hash,
        "parent_version_id": parent_version_id,
        "parent_commit_hash": parent_commit_hash,
        "version_num": version_num,
        "branch": "main",
        "content": f"content {version_num}",
        "category": "solutions",
        "subcategory": None,
        "snapshot_at": datetime(2026, 1, version_num, 12, 0, 0),
        "snapshot_by": owner_id,
        "change_type": "create" if parent_version_id is None else "update",
        "owner_id": owner_id,
        "namespace": "alice-ns",
        "permission_mode": permission_mode,
        "group_id": group_id,
    }


class _Conn:
    """Mock conn that records every SQL call. The post-walk filter
    sequence against ``/log`` is:

      * ``SELECT owner_id, namespace FROM memories WHERE id = $1`` —
        live-memory writable check.
      * the recursive CTE ``WITH RECURSIVE commit_walk AS ...`` —
        full history (root bypasses).
      * one ``SELECT 1 FROM memory_acl macl WHERE memory_id = $1`` —
        the post-#2 widening lookup. Mock returns None (no grant)
        OR a row (grant present).

    ``acl_grant`` controls whether the widening lookup returns a row.
    """

    def __init__(
        self,
        *,
        rows: list[dict],
        acl_grant: bool = False,
    ):
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self._rows = rows
        self._acl_grant = acl_grant

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        compact = " ".join(sql.split())
        compact_lower = compact.lower()
        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
            return {"owner_id": "alice", "namespace": "alice-ns"}
        # read_visibility_predicate SQL — preflight gate.
        # The actual SQL "AND deleted_at IS NULL" is uppercase. Stretchy:
        # any SELECT ... FROM memories WHERE id = $1 ... we accept because
        # the regression-test doesn't care about the exact predicate shape,
        # only about whether the route reached the post-walk filter step.
        if (
            "from memories where id = $1" in compact_lower
            and "deleted_at" in compact_lower
        ):
            return {"ok": 1}
        if "from memory_acl macl" in compact_lower and "macl.memory_id = $1" in compact:
            return {"ok": 1} if self._acl_grant else None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        compact = " ".join(sql.split())
        if "WITH RECURSIVE commit_walk AS" not in compact:
            raise AssertionError(f"unexpected fetch SQL: {sql}")
        return list(self._rows)


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


def _install(monkeypatch, conn):
    import mnemos.core.lifecycle as lc

    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def test_post_walk_acl_grant_widens_private_snapshots(monkeypatch):
    """Without ACL grant, private snapshots are dropped client-side.
    With ACL grant, the post-walk filter accepts every surviving
    snapshot of that memory regardless of permission_mode.
    """
    rows = [
        _version(
            version_id="v1", commit_hash="v1-hash", version_num=1,
            parent_version_id=None, parent_commit_hash=None,
            owner_id="alice", permission_mode=600,
        ),
        _version(
            version_id="v2", commit_hash="v2-hash", version_num=2,
            parent_version_id="v1", parent_commit_hash="v1-hash",
            owner_id="alice", permission_mode=600,
        ),
    ]
    conn = _Conn(rows=rows, acl_grant=True)
    _install(monkeypatch, conn)

    # bob has no group_id match but DOES have an ACL grant on mem-1.
    bob = _user(user_id="bob", groups=[])
    commits = asyncio.run(
        dag_handler.get_memory_log("mem-1", branch="main", user=bob)
    )
    # Both snapshots are visible because the ACL grant widens.
    assert {c.commit_hash for c in commits} == {"v1-hash", "v2-hash"}


def test_post_walk_no_acl_grant_keeps_pre_acl_behavior(monkeypatch):
    """Without the ACL grant, the client-side filter falls back to
    owner/world/group only — exactly the pre-#2 behavior. bob is not
    the owner and the rows are world-unreadable (mode 600), so the
    result is an empty list of commits (the CTE returned rows but
    the post-walk filter dropped them all)."""
    rows = [
        _version(
            version_id="v1", commit_hash="v1-hash", version_num=1,
            parent_version_id=None, parent_commit_hash=None,
            owner_id="alice", permission_mode=600,
        ),
    ]
    conn = _Conn(rows=rows, acl_grant=False)
    _install(monkeypatch, conn)

    bob = _user(user_id="bob", groups=[])
    commits = asyncio.run(
        dag_handler.get_memory_log("mem-1", branch="main", user=bob)
    )
    assert commits == [], "bob without grant must see an empty /log"


def test_post_walk_group_match_accepts_world_unreadable_snapshot(monkeypatch):
    """A caller in a matching group_id sees a group-readable snapshot
    (mode >= 40x). The widened predicate's group branch fires against
    snapshot rows carrying group_id from the migration's backfill."""
    rows = [
        _version(
            version_id="v1", commit_hash="v1-hash", version_num=1,
            parent_version_id=None, parent_commit_hash=None,
            owner_id="alice", permission_mode=640,  # group readable
            group_id="team-x",
        ),
    ]
    conn = _Conn(rows=rows, acl_grant=False)
    _install(monkeypatch, conn)

    bob = _user(user_id="bob", groups=["team-x"])
    commits = asyncio.run(
        dag_handler.get_memory_log("mem-1", branch="main", user=bob)
    )
    assert {c.commit_hash for c in commits} == {"v1-hash"}


def test_post_walk_group_mismatch_drops_snapshot(monkeypatch):
    """A caller in a non-matching group_id is filtered out — pre-#2
    behavior preserved for the no-ACL / wrong-group case."""
    rows = [
        _version(
            version_id="v1", commit_hash="v1-hash", version_num=1,
            parent_version_id=None, parent_commit_hash=None,
            owner_id="alice", permission_mode=640,  # group readable
            group_id="team-x",
        ),
    ]
    conn = _Conn(rows=rows, acl_grant=False)
    _install(monkeypatch, conn)

    bob = _user(user_id="bob", groups=["team-y"])
    commits = asyncio.run(
        dag_handler.get_memory_log("mem-1", branch="main", user=bob)
    )
    assert commits == [], "bob in non-matching group must see an empty /log"
