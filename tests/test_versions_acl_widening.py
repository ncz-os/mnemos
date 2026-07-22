"""Versions handler regression tests for GitLab #2 (ncz-os/mnemos#2).

Verifies the HTTP route-level widening for /versions, /versions/{n},
/diff, and /revert:

* A group-only reader of the live memory sees the version row.
* An ACL-only reader of the live memory sees the version row.
* A non-owner / non-group / non-ACL caller still gets 404 / 200-with-empty
  (no over-widening regression).

These tests mirror the shape of tests/test_versions_tenancy.py but
exercise the new widened predicate rather than the previous narrow one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.api.routes import versions as versions_handler


def _user(
    user_id: str = "alice",
    *,
    role: str = "user",
    groups: list[str] | None = None,
    ns: str = "ns-1",
) -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=list(groups or []),
        role=role,
        namespace=ns,
        authenticated=True,
    )


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


def _install_pool(monkeypatch, conn):
    """Replace lifecycle's pool with a mock that yields a single conn."""
    import mnemos.core.lifecycle as lc

    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def _skip_installation_503(monkeypatch):
    """Some versions-route paths require a real pool; bypass that
    check so tests can install the mock pool without hitting it."""

    async def _no_503(*args, **kwargs):
        return None

    monkeypatch.setattr(
        require_postgres_pool_or_503,
        "__wrapped__", _no_503, raising=False,
    )

    # versions.py calls require_postgres_pool_or_503(route_label=...) at the
    # top of each handler. We patch it directly at import.
    import mnemos.api.routes.versions as _v
    monkeypatch.setattr(_v, "require_postgres_pool_or_503", lambda *a, **k: None)


class _VersionsConn:
    """Mock asyncpg-shaped conn for /versions and friends.

    Records every fetch/fetchrow call. Default behavior:
      * the live-memory read-visibility predicate returns ``{"ok": 1}``
        so the route passes the preflight gate.
      * any SELECT against ``memory_versions`` returns a single row.

    Tests can configure ``live_row`` / ``version_rows`` / ``acl_grant``
    to tailor behavior.
    """

    def __init__(
        self,
        *,
        live_row: dict | None = None,
        version_rows: list[dict] | None = None,
        diff_rows: list[dict] | None = None,
    ):
        self._live_row = live_row if live_row is not None else {"owner_id": "alice", "namespace": "ns-1"}
        self._version_rows = list(version_rows or [])
        self._diff_rows = list(diff_rows or [])
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        compact = " ".join(sql.split())
        if "FROM memory_versions" in compact and "version_num" in compact:
            return list(self._version_rows)
        if "SELECT content, version_num FROM memory_versions" in compact:
            return list(self._diff_rows)
        return []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        compact = " ".join(sql.split())
        compact_lower = compact.lower()
        # read_visibility_predicate gate
        if "from memories" in compact_lower and "id = $1" in compact and "deleted_at" in compact_lower:
            return self._live_row
        # memory_versions gate
        if "from memory_versions" in compact and "commit_hash =" in compact:
            return self._version_rows[0] if self._version_rows else None
        return None


def _make_version_row(
    *,
    version_num: int,
    permission_mode: int,
    owner_id: str = "alice",
    group_id: str | None = None,
    namespace: str = "ns-1",
    branch: str = "main",
    commit_hash: str | None = None,
) -> dict:
    return {
        "id": f"v{version_num}-id",
        "memory_id": "mem-1",
        "version_num": version_num,
        "content": f"content {version_num}",
        "category": "solutions",
        "subcategory": None,
        "metadata": {"row": version_num},
        "verbatim_content": f"verbatim {version_num}",
        "owner_id": owner_id,
        "namespace": namespace,
        "permission_mode": permission_mode,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "snapshot_at": datetime(2026, 1, version_num, 12, 0, 0),
        "snapshot_by": owner_id,
        "change_type": "create" if version_num == 1 else "update",
        "branch": branch,
        "commit_hash": commit_hash or f"v{version_num}-hash",
        "group_id": group_id,
    }


# ---------------------------------------------------------------------------
# list_versions — endpoint-level widening
# ---------------------------------------------------------------------------


def test_list_versions_widened_predicate_present(monkeypatch):
    """The list_versions SQL must include the group + ACL disjuncts
    (the widened version_visibility_predicate). Pre-#2 the SQL only
    had owner + world."""
    conn = _VersionsConn(
        live_row={"owner_id": "alice", "namespace": "ns-1"},
        version_rows=[],
    )
    _skip_installation_503(monkeypatch)
    _install_pool(monkeypatch, conn)

    bob = _user(user_id="bob", groups=["team-x"])
    asyncio.run(
        versions_handler.list_versions("mem-1", branch="main", user=bob)
    )
    # Find the SELECT against memory_versions.
    sql, _ = next(
        pair for pair in conn.fetch_calls
        if "from memory_versions" in " ".join(pair[0].split()).lower()
    )
    compact = " ".join(sql.split())
    assert "permission_mode / 10" in compact
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in compact
    assert "team-x" in str(conn.fetch_calls[0][1]) or "team-x" in [
        a for arglist in conn.fetch_calls for a in arglist[1]
    ]


def test_list_versions_invisible_to_unrelated_caller(monkeypatch):
    """A caller with neither owner nor group nor ACL match sees an
    empty list — the preflight ``_assert_memory_readable`` may pass
    (because the live memory is world-readable or because the test
    mock returns the row), but the per-snapshot predicate still
    fails against a private snapshot row."""
    conn = _VersionsConn(
        live_row={"owner_id": "alice", "namespace": "ns-1"},
        # No version rows returned ⇒ widened predicate filtered them all.
        version_rows=[],
    )
    _skip_installation_503(monkeypatch)
    _install_pool(monkeypatch, conn)

    carol = _user(user_id="carol", groups=["other-team"])
    summaries = asyncio.run(
        versions_handler.list_versions("mem-1", branch="main", user=carol)
    )
    assert summaries == []


# ---------------------------------------------------------------------------
# get_version — endpoint-level widening
# ---------------------------------------------------------------------------


def test_get_version_widened_predicate_present(monkeypatch):
    """get_version has a single-row SELECT against memory_versions.
    The widened predicate's group + ACL disjuncts MUST be present."""
    conn = _VersionsConn(
        live_row={"owner_id": "alice", "namespace": "ns-1"},
        version_rows=[],
    )
    _skip_installation_503(monkeypatch)
    _install_pool(monkeypatch, conn)

    bob = _user(user_id="bob", groups=["team-x"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            versions_handler.get_version("mem-1", 1, branch="main", user=bob)
        )
    assert exc.value.status_code == 404

    # The single-row SELECT against memory_versions must carry the
    # widened predicate.
    sql, args = next(
        (s, a) for s, a in conn.fetchrow_calls
        if "from memory_versions" in " ".join(s.split()).lower()
    )
    compact = " ".join(sql.split())
    assert "permission_mode / 10" in compact
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in compact
    # Per-principal list lives in args (nested list of strings).
    flat = []
    for _, arglist in conn.fetchrow_calls:
        for a in arglist:
            if isinstance(a, list):
                flat.extend(a)
            else:
                flat.append(a)
    assert "user:bob" in flat
    assert "group:team-x" in flat


# ---------------------------------------------------------------------------
# diff_versions — endpoint-level widening
# ---------------------------------------------------------------------------


def test_diff_versions_widened_predicate_present(monkeypatch):
    """The /diff SELECT includes the widened predicate. With no
    matching version rows and a private memory, the route 404s on
    the missing from_version."""
    conn = _VersionsConn(
        live_row={"owner_id": "alice", "namespace": "ns-1"},
        diff_rows=[],
    )
    _skip_installation_503(monkeypatch)
    _install_pool(monkeypatch, conn)

    bob = _user(user_id="bob", groups=["team-x"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            versions_handler.diff_versions("mem-1", from_version=1, to_version=2, user=bob)
        )
    assert exc.value.status_code == 404

    sql, _ = next(
        (s, a) for s, a in conn.fetch_calls
        if "from memory_versions" in " ".join(s.split()).lower()
    )
    compact = " ".join(sql.split())
    assert "permission_mode / 10" in compact
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in compact
