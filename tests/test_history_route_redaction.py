"""M2 (code-health decisions 2026-10-31): history routes honour redaction.

``get_memory`` has masked credential spans since the 2026-06-13
release block (``_should_redact_secrets`` + ``row_to_memory``), but the
*history* routes were never routed through the same gate:

* ``GET /v1/memories/{id}/versions/{n}`` — ``_row_to_version`` returned
  the raw snapshot content.
* ``GET /v1/memories/{id}/diff`` — diffed two raw snapshots.
* ``GET /v1/memories/{id}/log`` and ``GET /v1/memories/{id}/commits/{h}``
  — returned raw commit content.
* the MCP ``checkout_memory`` / ``diff_memory_commits`` tools — returned
  the rows from the same repositories.
* ``revert_memory``'s response — returned the live row unframed.
* ``PATCH /v1/memories/{id}`` — returned the live row unframed.

So a non-root caller who could GET a memory and see ``[REDACTED]`` could
walk the version log and read the same credential in plaintext. The vault
namespace exclusion still hides vaulted rows (they 404 / are filtered);
this is the incidental-span + vaulted-miss backstop applied to history.

Contract pinned here: every history read passes through
``_should_redact_secrets`` (+ ``_should_frame_data``); **no** route grants
a plaintext history read. ``include_secrets`` is root-only and, as on
``GET /v1/memories/{id}``, only ever unlocks a vault-namespace row.

The fixture secret (``hunter2`` in a password clause) is the classifier's
REDACT tier — an incidental span that does NOT move the row to the vault —
so the masking is observable through the ordinary visibility predicate.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import mnemos.api.routes.memories as memories_route
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import dag as dag_handler
from mnemos.api.routes import versions as versions_handler
from mnemos.domain.models import MemoryUpdateRequest

SECRET = "hunter2"
SECRET_TEXT = "the guest wifi password is hunter2 for the lab network"
MASKED_TEXT = "the guest wifi password is [REDACTED] for the lab network"
VAULT_TEXT = "INFRASTRUCTURE CREDENTIALS: TYPHON root login password is DenylistSelfTest2NotReal99"


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin",
        group_ids=[],
        role="root",
        namespace="alice-ns",
        authenticated=True,
    )


def _version_row(**overrides) -> dict:
    row = {
        "id": "v1-id",
        "memory_id": "mem-1",
        "version_num": 1,
        "content": SECRET_TEXT,
        "category": "solutions",
        "subcategory": None,
        "metadata": {"note": "v1"},
        "verbatim_content": SECRET_TEXT,
        "owner_id": "alice",
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "snapshot_at": datetime(2026, 1, 1, 12, 0, 0),
        "snapshot_by": "alice",
        "change_type": "create",
        "branch": "main",
    }
    row.update(overrides)
    return row


def _memory_row(**overrides) -> dict:
    row = {
        "id": "mem-1",
        "content": SECRET_TEXT,
        "category": "solutions",
        "subcategory": None,
        "created": datetime(2026, 1, 1, 12, 0, 0),
        "updated": datetime(2026, 1, 1, 12, 0, 0),
        "metadata": {"note": "v1"},
        "quality_rating": 80,
        "compressed_content": None,
        "verbatim_content": SECRET_TEXT,
        "owner_id": "alice",
        "group_id": None,
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }
    row.update(overrides)
    return row


def _dag_version(**overrides) -> dict:
    row = _version_row(
        id="v1",
        version_num=1,
        group_id=None,
        commit_hash="c1",
        parent_version_id=None,
        parent_commit_hash=None,
        depth=1,
    )
    row.update(overrides)
    return row


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


def _install_pool(monkeypatch, conn):
    """Point the versions routes at a fake backend facade (NG2-MN-018).

    ``list_versions`` / ``get_version`` resolve the persistence backend
    and call ``VersionRepository`` inside ``backend.transactional()``.
    The fake backend serves the REAL Postgres repositories over the
    stub asyncpg connection, so the SQL under test is the shipped SQL —
    only the transport is fake.
    """
    import mnemos.core.lifecycle as lc
    from mnemos.persistence.postgres import (
        PostgresMemoryRepository,
        PostgresTransaction,
        PostgresVersionRepository,
    )

    tx = PostgresTransaction(conn, MagicMock())

    class _Ctx:
        async def __aenter__(self):
            return tx

        async def __aexit__(self, *_args):
            return False

    class _VersionsBackend:
        """Minimal facade exposing only what the ABC-routed history reads use.

        Deliberately NOT a MagicMock: ``require_postgres_pool_or_503``
        treats any backend with a truthy ``pool`` attribute as pool-backed,
        which would hijack the still-raw revert/dag routes. A plain
        object has no ``pool`` attribute, so those routes fall through to
        the installed ``_pool`` exactly as before.
        """

        def __init__(self):
            self.memories = PostgresMemoryRepository()
            self.memory_versions = PostgresVersionRepository()

        def transactional(self):
            return _Ctx()

    monkeypatch.setattr(lc, "_persistence_backend", _VersionsBackend())
    # revert / dag routes still acquire the raw pool (their slices
    # are pending); keep it installed so those tests exercise them.
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


class _FakeVersionsTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeVersionsBackend:
    """Minimal backend double for the ABC-routed versions handlers.

    list_versions/get_version go through ``backend.transactional()`` +
    ``backend.memories.assert_memory_readable`` +
    ``backend.memory_versions.list/fetch`` — never the raw pool — so the
    double serves pre-filtered rows instead of SQL text.
    """

    def __init__(self, *, version_row=None, version_rows=None):
        self._version_row = version_row
        self._version_rows = (
            version_rows if version_rows is not None else ([version_row] if version_row is not None else [])
        )
        self.memories = self
        self.memory_versions = self
        self.memory_branches = self

    def transactional(self, **_kwargs):
        return _FakeVersionsTx()

    async def assert_memory_readable(self, tx, memory_id, user):
        return None

    async def list_memory_versions(self, tx, memory_id, branch, user):
        return list(self._version_rows)

    async def get_memory_version(self, tx, memory_id, version_num, branch, visibility):
        return self._version_row

    async def fetch_memory_branch_heads(self, tx, memory_ids, **_kwargs):
        if self._version_rows:
            return [{"memory_id": memory_ids[0], "branch": "main"}]
        return []


def _install_versions_backend(monkeypatch, *, version_row=None, version_rows=None):
    import mnemos.core.lifecycle as lc

    monkeypatch.setattr(
        lc,
        "_persistence_backend",
        _FakeVersionsBackend(version_row=version_row, version_rows=version_rows),
    )


# ── versions.py: get_version ────────────────────────────────────────────────


class _VersionConn:
    """Stub connection for the versions routes (no live Postgres)."""

    def __init__(self, *, version_row=None, diff_rows=None):
        self.version_row = version_row
        self.diff_rows = diff_rows or []
        self.fetchrow_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("SELECT 1 FROM memories WHERE id = $1"):
            return {"ok": 1}
        if compact.startswith("SELECT 1 FROM memory_versions"):
            return {"ok": 1}
        if "FROM memory_versions WHERE memory_id = $1 AND version_num = $2" in compact:
            return self.version_row
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if "SELECT version_num, snapshot_at, snapshot_by, change_type, content, branch" in compact:
            return self.diff_rows
        if "SELECT version_num, content FROM memory_versions" in compact:
            if "permission_mode % 10" in compact:
                # NG2-MN-018 diff slice: repository path carries the
                # per-snapshot visibility predicate + namespace pin
                # (memory_id=$1, nums=$2, branch=$3, vis..., ns=last).
                kept = []
                for r in self.diff_rows:
                    row = {"memory_id": "mem-1", "owner_id": "alice",
                           "namespace": "alice-ns", "permission_mode": 600,
                           **r}
                    namespace = args[-1]
                    caller = args[3]
                    if row.get("namespace") != namespace:
                        continue
                    if row.get("owner_id") == caller:
                        kept.append(r)
                        continue
                    if (row.get("permission_mode") or 0) % 10 >= 4:
                        kept.append(r)
                return kept
            return self.diff_rows
        raise AssertionError(f"unexpected fetch SQL: {sql}")


def test_non_root_get_version_masks_secret_spans(monkeypatch):
    _install_versions_backend(monkeypatch, version_row=_version_row())

    version = asyncio.run(versions_handler.get_version("mem-1", 1, user=_alice()))

    assert SECRET not in version.content
    # Content may be wrapped in the untrusted-data frame; the masked body
    # must still be the only version of the text returned.
    assert MASKED_TEXT in version.content
    assert version.verbatim_content is not None
    assert SECRET not in version.verbatim_content


def test_root_get_version_is_also_masked(monkeypatch):
    """No history route grants a plaintext read — not even to root."""
    _install_versions_backend(monkeypatch, version_row=_version_row())

    version = asyncio.run(versions_handler.get_version("mem-1", 1, user=_root()))

    assert SECRET not in version.content
    assert MASKED_TEXT in version.content


def test_vault_namespace_version_is_not_found(monkeypatch):
    """A vault row is outside the visibility predicate: 404, never content."""
    _install_versions_backend(monkeypatch, version_row=None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(versions_handler.get_version("mem-1", 1, user=_alice()))
    assert exc.value.status_code == 404
    assert "DenylistSelfTest2NotReal99" not in str(exc.value.detail)


def test_list_versions_preview_is_masked(monkeypatch):
    """The 120-char content preview must not leak a short credential."""
    _install_versions_backend(
        monkeypatch,
        version_rows=[
            {
                "version_num": 1,
                "content": SECRET_TEXT,
                "snapshot_at": datetime(2026, 1, 1, 12, 0, 0),
                "snapshot_by": "alice",
                "change_type": "create",
                "branch": "main",
            }
        ],
    )

    summaries = asyncio.run(versions_handler.list_versions("mem-1", user=_alice()))

    assert len(summaries) == 1
    assert SECRET not in summaries[0].content_preview
    assert "[REDACTED]" in summaries[0].content_preview


# ── versions.py: diff ───────────────────────────────────────────────────────


def test_non_root_diff_masks_both_sides(monkeypatch):
    conn = _VersionConn(
        diff_rows=[
            {"version_num": 1, "content": SECRET_TEXT},
            {"version_num": 2, "content": "the guest wifi password is hunter2-rotated for the lab"},
        ]
    )
    _install_pool(monkeypatch, conn)

    diff = asyncio.run(versions_handler.diff_versions("mem-1", from_version=1, to_version=2, user=_alice()))

    assert SECRET not in diff.diff
    assert "[REDACTED]" in diff.diff


def test_root_diff_is_also_masked(monkeypatch):
    conn = _VersionConn(
        diff_rows=[
            {"version_num": 1, "content": SECRET_TEXT},
            {"version_num": 2, "content": "clean second version"},
        ]
    )
    _install_pool(monkeypatch, conn)

    diff = asyncio.run(versions_handler.diff_versions("mem-1", from_version=1, to_version=2, user=_root()))

    assert SECRET not in diff.diff


# ── versions.py: revert response ────────────────────────────────────────────


def test_revert_response_is_masked(monkeypatch):
    """``revert_memory`` returns the live row: it must be masked."""

    class _Agnostic:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    class _RevertConn(_VersionConn):
        def transaction(self):
            return _Agnostic()

        async def fetchval(self, sql: str, *args):
            compact = " ".join(sql.split())
            if compact.startswith("SELECT COALESCE(MAX(version_num), 0) + 1"):
                return 2
            if compact.startswith("INSERT INTO memory_versions"):
                return "new-version-id"
            if compact.startswith("SELECT head_version_id FROM memory_branches"):
                return "target-head-id"
            raise AssertionError(f"unexpected fetchval SQL: {sql}")

        async def execute(self, sql: str, *args):
            compact = " ".join(sql.split())
            if compact.startswith("SELECT pg_advisory_xact_lock"):
                return "SELECT 1"
            if compact.startswith("UPDATE memory_branches SET head_version_id = $1"):
                return "UPDATE 1"
            raise AssertionError(f"unexpected execute SQL: {sql}")

        async def fetchrow(self, sql: str, *args):
            compact = " ".join(sql.split())
            self.fetchrow_calls.append((sql, args))
            if compact.startswith("SELECT 1 FROM memories WHERE id = $1"):
                return {"ok": 1}
            if "FROM memory_versions WHERE memory_id = $1 AND version_num = $2 AND branch = $3" in compact:
                return self.version_row
            if compact.startswith("SELECT head_version_id FROM memory_branches"):
                return {"head_version_id": "target-head-id"}
            if compact.startswith("SELECT 1 FROM memory_versions WHERE id = $1"):
                return {"ok": 1}
            if compact.startswith("SELECT id, owner_id, namespace, permission_mode FROM memory_versions"):
                return {
                    "id": "target-head-id",
                    "owner_id": "alice",
                    "namespace": "alice-ns",
                    "permission_mode": 600,
                }
            if compact.startswith("SELECT ") and "FROM memories WHERE id=$1" in compact:
                return _memory_row()
            raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    _install_pool(monkeypatch, _RevertConn(version_row=_version_row()))

    item = asyncio.run(versions_handler.revert_memory("mem-1", 1, user=_alice()))

    assert SECRET not in item.content
    assert "[REDACTED]" in item.content


# ── dag.py: log + commit ────────────────────────────────────────────────────


class _DagConn:
    def __init__(self, *, commit_row=None, log_rows=None):
        self.commit_row = commit_row
        self.log_rows = log_rows or []

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT 1 FROM memories WHERE id = $1"):
            return {"ok": 1}
        if "mv.commit_hash, mv.version_num" in compact:
            # get_commit: root and non-root variants share this select list.
            return self.commit_row
        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
            return {"owner_id": "alice", "namespace": "alice-ns"}
        if "FROM memory_acl" in compact:
            return None
        if "mv.commit_hash, mv.version_num, mv.branch, mv.content" in compact:
            return self.commit_row
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if "WITH RECURSIVE commit_walk AS" in compact:
            return self.log_rows
        raise AssertionError(f"unexpected fetch SQL: {sql}")


def _commit_row() -> dict:
    return {
        "commit_hash": "c1",
        "version_num": 1,
        "branch": "main",
        "content": SECRET_TEXT,
        "category": "solutions",
        "subcategory": None,
        "snapshot_at": datetime(2026, 1, 1, 12, 0, 0),
        "snapshot_by": "alice",
        "change_type": "create",
        "parent_hash": None,
    }


def test_non_root_dag_log_masks_commit_content(monkeypatch):
    _install_pool(monkeypatch, _DagConn(log_rows=[_dag_version()]))

    commits = asyncio.run(dag_handler.get_memory_log("mem-1", branch="main", user=_alice()))

    assert len(commits) == 1
    assert SECRET not in commits[0].content
    assert "[REDACTED]" in commits[0].content


def test_root_dag_log_is_also_masked(monkeypatch):
    _install_pool(monkeypatch, _DagConn(log_rows=[_dag_version()]))

    commits = asyncio.run(dag_handler.get_memory_log("mem-1", branch="main", user=_root()))

    assert SECRET not in commits[0].content


def test_non_root_dag_commit_masks_content(monkeypatch):
    _install_pool(monkeypatch, _DagConn(commit_row=_commit_row()))

    commit = asyncio.run(dag_handler.get_commit("mem-1", "c1", user=_alice()))

    assert SECRET not in commit.content
    assert "[REDACTED]" in commit.content


def test_root_dag_commit_is_also_masked(monkeypatch):
    _install_pool(monkeypatch, _DagConn(commit_row=_commit_row()))

    commit = asyncio.run(dag_handler.get_commit("mem-1", "c1", user=_root()))

    assert SECRET not in commit.content


def test_vaulted_history_content_is_masked_not_returned(monkeypatch):
    """A vaulted-miss (row still in a default namespace) is masked on history."""
    _install_pool(monkeypatch, _DagConn(log_rows=[_dag_version(content=VAULT_TEXT)]))

    commits = asyncio.run(dag_handler.get_memory_log("mem-1", branch="main", user=_alice()))

    assert "DenylistSelfTest2NotReal99" not in commits[0].content
    assert "[REDACTED]" in commits[0].content


# ── MCP dag tools ───────────────────────────────────────────────────────────


class _Agnostic:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_mcp_checkout_memory_masks_content(monkeypatch):
    from mnemos.mcp.tools import dag as mcp_dag

    class _Backend:
        def __init__(self):
            self.memories = SimpleNamespace(
                assert_memory_readable=self._readable,
                fetch_checkout_commit=self._checkout,
            )

        async def _readable(self, *_args, **_kwargs):
            return None

        async def _checkout(self, _tx, _memory_id, _commit_hash, _user):
            return {
                "commit_hash": "c1",
                "version_num": 1,
                "branch": "main",
                "content": SECRET_TEXT,
                "category": "solutions",
                "subcategory": None,
                "snapshot_at": datetime(2026, 1, 1, 12, 0, 0),
                "snapshot_by": "alice",
                "change_type": "create",
            }

        def transactional(self):
            return _Agnostic()

    monkeypatch.setattr(mcp_dag, "_get_persistence_backend", lambda: _Backend())

    result = await mcp_dag.tool_checkout_memory("mem-1", "c1", user=_alice())

    assert result["success"] is True
    assert SECRET not in result["content"]
    assert "[REDACTED]" in result["content"]


@pytest.mark.asyncio
async def test_mcp_diff_memory_commits_masks_content(monkeypatch):
    from mnemos.mcp.tools import dag as mcp_dag

    class _Backend:
        def __init__(self):
            self.memories = SimpleNamespace(
                assert_memory_readable=self._readable,
                fetch_diff_commit_pair=self._pair,
            )

        async def _readable(self, *_args, **_kwargs):
            return None

        async def _pair(self, _tx, _memory_id, _a, _b, _user):
            return (
                {"content": SECRET_TEXT, "version_num": 1},
                {"content": "clean second version", "version_num": 2},
            )

        def transactional(self):
            return _Agnostic()

    monkeypatch.setattr(mcp_dag, "_get_persistence_backend", lambda: _Backend())

    result = await mcp_dag.tool_diff_memory_commits("mem-1", "c1", "c2", user=_alice())

    assert result["success"] is True
    assert SECRET not in result["diff"]
    assert "[REDACTED]" in result["diff"]


# ── memories.py: PATCH response ─────────────────────────────────────────────


def test_update_memory_response_is_masked(monkeypatch):
    """The PATCH response returns the live row: mask its spans too."""
    stored: dict = {}

    class _Tx:
        pass

    class _Txn(_Agnostic):
        async def __aenter__(self):
            return _Tx()

    class _Backend:
        def __init__(self):
            self.memories = SimpleNamespace(update_memory=self._update)

        async def _update(self, _tx, _memory_id, *, visibility, fields):
            stored["fields"] = fields
            return _memory_row()

        def transactional(self):
            return _Txn()

    async def _noop(*_args, **_kwargs):
        return None

    async def _passthrough(*_args, **_kwargs):
        return _memory_row()

    monkeypatch.setattr(memories_route, "_backend_or_503", lambda: _Backend())
    monkeypatch.setattr(memories_route, "_maybe_set_pg_rls", _noop)
    monkeypatch.setattr(memories_route, "_attach_memory_tags_one", _passthrough)
    monkeypatch.setattr(memories_route, "_write_memory_mutation_audit_entry", _noop)
    monkeypatch.setattr(memories_route, "_invalidate_caches_after_mutation", _noop)
    monkeypatch.setattr(memories_route, "_publish_nats_with_timeout", _noop)

    item = asyncio.run(
        memories_route.update_memory(
            "mem-1",
            MemoryUpdateRequest(category="facts"),
            user=_alice(),
        )
    )

    assert SECRET not in item.content
    assert "[REDACTED]" in item.content
