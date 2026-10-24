"""Contract tests for the userless / root-equivalent read filter.

The ``fetch_memory_context`` read paths on Db2 and Oracle used to hand-build
a ``ROOT_BYPASS`` ``VisibilityFilter`` for callers with no ``UserContext``,
right next to the ``for_read`` call used for an authenticated root user.
Two constructions for one concept drift: the hand-built one had to remember
``exclude_namespaces`` on its own. These tests pin the shared factories
(``for_system`` / ``for_caller``) so the two root-like paths stay identical.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mnemos.core.auth_context import UserContext
from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.persistence.visibility import (
    DEFAULT_EXCLUDED_NAMESPACES,
    VisibilityFilter,
    VisibilityScope,
)


def _user(**overrides):
    base = dict(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
        session_id=None,
    )
    base.update(overrides)
    return UserContext(**base)


# ── for_system ─────────────────────────────────────────────────────────────


def test_for_system_is_cross_tenant_and_subtracts_vault():
    vis = VisibilityFilter.for_system()

    assert vis.scope is VisibilityScope.ROOT_BYPASS
    assert vis.user_id is None
    assert vis.group_ids == ()
    assert vis.namespace is None
    assert vis.exclude_namespaces == DEFAULT_EXCLUDED_NAMESPACES
    assert VAULT_NAMESPACE in vis.exclude_namespaces


def test_for_system_matches_for_read_for_root_user():
    """The invariant the hand-built block kept violating: root-like == root."""
    system = VisibilityFilter.for_system()
    root = VisibilityFilter.for_read(_user(role="root"), namespace=None)

    assert system.scope is root.scope
    assert system.user_id == root.user_id
    assert system.group_ids == root.group_ids
    assert system.namespace == root.namespace
    assert system.exclude_namespaces == root.exclude_namespaces


def test_for_system_discovery_opt_in_still_returns_vault_rows():
    vis = VisibilityFilter.for_system(include_vault=True)
    assert vis.scope is VisibilityScope.ROOT_BYPASS
    assert vis.exclude_namespaces == ()


# ── for_caller ─────────────────────────────────────────────────────────────


def test_for_caller_userless_uses_system_filter():
    # No ``user_id`` attribute at all -> system caller.
    vis = VisibilityFilter.for_caller(SimpleNamespace(role="root"))

    assert vis.scope is VisibilityScope.ROOT_BYPASS
    assert vis.exclude_namespaces == DEFAULT_EXCLUDED_NAMESPACES
    # Presence of ``user_id`` (even empty) means "a user": for_read semantics.
    vis_user = VisibilityFilter.for_caller(_user(user_id="", namespace="ns"))
    assert vis_user.scope is VisibilityScope.READABLE
    assert vis_user.namespace == "ns"


def test_for_caller_root_ignores_namespace_pin():
    vis = VisibilityFilter.for_caller(_user(role="root", namespace="root-ns"))

    assert vis.scope is VisibilityScope.ROOT_BYPASS
    assert vis.namespace is None
    assert vis.exclude_namespaces == DEFAULT_EXCLUDED_NAMESPACES


def test_for_caller_non_root_pinned_to_own_namespace():
    vis = VisibilityFilter.for_caller(_user(user_id="bob", namespace="bob-ns"))

    assert vis.scope is VisibilityScope.READABLE
    assert vis.user_id == "bob"
    assert vis.namespace == "bob-ns"


def test_for_caller_non_root_missing_namespace_falls_back_to_default():
    """Duck-typed callers omit ``namespace``; must not raise (regression)."""
    vis = VisibilityFilter.for_caller(SimpleNamespace(user_id="bob", group_ids=[], role="user"))

    assert vis.scope is VisibilityScope.READABLE
    assert vis.namespace == "default"


def test_for_caller_matches_for_read_for_authenticated_user():
    user = _user()
    assert VisibilityFilter.for_caller(user) == VisibilityFilter.for_read(user, namespace="alice-ns")


# ── backend delegation: Db2 + Oracle use the factory ───────────────────────


@pytest.mark.parametrize("module_name", ["mnemos.persistence.db2", "mnemos.persistence.oracle"])
def test_fetch_memory_context_delegates_to_for_caller(monkeypatch, module_name):
    """Both backends route the caller decision through one factory."""
    import asyncio
    import importlib

    module = importlib.import_module(module_name)
    repo_cls = module.Db2MemoryRepository if "db2" in module_name else module.OracleMemoryRepository
    repo = repo_cls()

    from mnemos.core import lifecycle

    async def _embed(_query):
        return [0.5] * 384

    monkeypatch.setattr(lifecycle, "_get_embedding", _embed)

    captured: dict[str, object] = {}

    async def _semantic_search(tx, *, embedding, limit, visibility):
        captured["visibility"] = visibility
        return []

    monkeypatch.setattr(repo, "semantic_search", _semantic_search)

    # Userless caller: previously hand-built, now via ``for_system``.
    asyncio.run(repo.fetch_memory_context(SimpleNamespace(), "q", SimpleNamespace(), limit=2))
    vis = captured["visibility"]
    assert vis.scope is VisibilityScope.ROOT_BYPASS
    assert vis.exclude_namespaces == DEFAULT_EXCLUDED_NAMESPACES

    # Real user: still ``for_read`` semantics.
    asyncio.run(repo.fetch_memory_context(SimpleNamespace(), "q", _user(), limit=2))
    vis = captured["visibility"]
    assert vis.scope is VisibilityScope.READABLE
    assert vis.namespace == "alice-ns"


# ── SQLite renderer: never returns a dangling boolean fragment ─────────────


@pytest.mark.parametrize("caller", ["root", "user", "system"])
def test_sqlite_renderer_returns_complete_expression_or_empty(caller):
    """``_render_sqlite_visibility`` must be safe to join with ``AND``.

    ``fetch_memory_context`` builds its WHERE clause from parts, so the
    renderer's contract is "empty string, or a complete boolean
    expression" -- never a fragment with a trailing/leading operator.
    """
    from mnemos.persistence.sqlite import _render_sqlite_visibility

    callers = {
        "root": _user(role="root"),
        "user": _user(),
        "system": SimpleNamespace(),
    }
    params: list[object] = []
    clause = _render_sqlite_visibility(
        VisibilityFilter.for_caller(callers[caller]), params, table_alias="m"
    )

    assert isinstance(clause, str)
    if clause:
        assert not clause.strip().upper().endswith(" AND")
        assert not clause.strip().upper().startswith("AND ")
        assert clause.count("?") == len(params)
    # Every shape subtracts the vault.
    assert "vault" in params


def test_sqlite_fetch_memory_context_sql_is_wellformed_for_every_caller(monkeypatch):
    """The composed statement always has a predicate after WHERE."""
    import asyncio

    from mnemos.core import lifecycle
    from mnemos.persistence.sqlite import SqliteMemoryRepository

    async def _embed(_q):
        return [0.1] * 3

    monkeypatch.setattr(lifecycle, "_get_embedding", _embed)
    seen: dict[str, object] = {}

    async def _fetch_all(conn, sql, params):
        seen["sql"] = " ".join(sql.split())
        return []

    monkeypatch.setattr("mnemos.persistence.sqlite._fetch_all", _fetch_all)

    repo = SqliteMemoryRepository.__new__(SqliteMemoryRepository)
    repo._conn = lambda tx: None  # type: ignore[method-assign]

    for caller in (_user(role="root"), _user(), SimpleNamespace()):
        seen.clear()
        asyncio.run(repo.fetch_memory_context(None, "needle", caller, limit=2))
        sql = str(seen["sql"]).lower()
        assert "where" in sql
        after_where = sql.split("where", 1)[1].strip()
        assert after_where, sql
        assert not after_where.startswith("and ")
