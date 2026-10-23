"""F-06 slice 2: MCP DAG/models tools route through the persistence ABC.

The governing ABC-first rule (``docs/FEATURES_TO_BE_DONE.md`` §0b) makes raw
``_lc._pool`` access outside ``mnemos/persistence/`` a defect. These tests
run the real ``SqliteBackend`` (in-memory) and pin that the MCP DAG and model
tools:

* resolve their transaction from the backend facade
  (``backend.transactional()``), and
* call the ABC repositories (``memories.fetch_memory_log`` /
  ``memories.fetch_diff_commit_pair`` / ``memories.fetch_checkout_commit`` /
  ``memory_branches.create_memory_branch`` /
  ``consultations_audit.fetch_recommended_model``) instead of executing raw
  SQL against ``_lc._pool.acquire()``.

Against the pre-F-06-slice-2 tree, the same tests fail: the tools read
``_lc._pool`` (which is ``None`` on the SQLite profile) and return
``{"success": False, "error": "Database unavailable"}`` without ever
touching the backend.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from mnemos.core.auth_context import UserContext
from mnemos.core.config import _reset_settings_for_tests
from mnemos.mcp.tools import dag as mcp_dag
from mnemos.mcp.tools import models as mcp_models
from mnemos.persistence.sqlite import SqliteBackend, _execute, _fetch_one

pytestmark = pytest.mark.asyncio

ROOT = UserContext(user_id="root", group_ids=[], role="root", namespace="root", authenticated=True)


def _install_backend_for_tools(monkeypatch, backend: SqliteBackend) -> None:
    """Point the MCP tools' backend resolution at the SQLite facade.

    The tools resolve the backend via
    ``mnemos.core.lifecycle.get_persistence_backend`` (imported into each
    tool module's namespace), so patch the *module-local* reference.
    """
    monkeypatch.setattr(mcp_dag, "_get_persistence_backend", lambda: backend)
    monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: backend)


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    """An open, schema-ready SQLite backend.

    The backend's own migration chain (``open()``) seeds the canonical
    ``memories`` / ``memory_versions`` / ``memory_branches`` schema — no
    hand-rolled DDL, so the tests exercise exactly the table shapes the ABC
    implementations run against.
    """
    _reset_settings_for_tests()
    from mnemos.core.config import get_settings

    backend = SqliteBackend(str(tmp_path / "mcp_tools_test.db"), get_settings())
    await backend.open()

    conn = backend._conn
    await _execute(
        conn,
        "INSERT INTO memories (id, content, owner_id, namespace, permission_mode) "
        "VALUES ('m1', 'seed', 'root', 'root', 7)",
    )
    # SQLite memory_versions.id is a TEXT primary key (migrations.sql) —
    # seed explicit ids rather than relying on rowid.
    await _execute(
        conn,
        "INSERT INTO memory_versions "
        "(id, memory_id, commit_hash, version_num, branch, content, change_type, "
        " snapshot_at, snapshot_by, owner_id, namespace, permission_mode) "
        "VALUES ('v1', 'm1', 'abc123', 1, 'main', 'hello v1', 'create', "
        " '2026-05-02T00:00:00+00:00', 'root', 'root', 'root', 7)",
    )
    await _execute(
        conn,
        "INSERT INTO memory_versions "
        "(id, memory_id, commit_hash, parent_version_id, version_num, branch, content, "
        " change_type, snapshot_at, snapshot_by, owner_id, namespace, permission_mode) "
        "VALUES ('v2', 'm1', 'def456', 'v1', 2, 'main', 'hello v2', 'update', "
        " '2026-05-03T00:00:00+00:00', 'root', 'root', 'root', 7)",
    )
    head = await _fetch_one(
        conn,
        "SELECT id FROM memory_versions WHERE memory_id = 'm1' AND version_num = 2",
    )
    await _execute(
        conn,
        "INSERT INTO memory_branches (memory_id, name, head_version_id, created_by) "
        "VALUES ('m1', 'main', ?, 'root')",
        (head["id"],),
    )
    await conn.commit()
    yield backend
    await backend.close()


class _FakeAssertMemoryReadable:
    """Counts ``assert_memory_readable`` calls; never blocks."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, UserContext]] = []


def _fake_assert_memory_readable(repo, fake: "_FakeAssertMemoryReadable"):
    """Bind the fake as a real method on the SQLite repository instance.

    ``monkeypatch.setattr(instance, name, fake)`` on an instance would be
    called *unbound* (no ``self``); the closure below is a real function
    bound to the repository, so it is invoked exactly like the production
    method: ``repo.assert_memory_readable(tx, memory_id, user)``.
    """
    async def _bound(self_, tx, memory_id: str, user: UserContext) -> None:
        fake.calls.append((self_, memory_id, user))

    return _bound.__get__(repo)


class _NoRestFallback:
    async def __call__(self, *args, **kwargs):
        raise AssertionError("REST fallback must not be used when a backend is active")


async def test_tool_log_memory_routes_through_persistence_abc(sqlite_backend, monkeypatch):
    _install_backend_for_tools(monkeypatch, sqlite_backend)
    fake_assert = _FakeAssertMemoryReadable()
    # The read gate resolves ``memories.assert_memory_readable`` on the
    # transaction-owning facade, so patch it on the real SQLite repository.
    monkeypatch.setattr(
        sqlite_backend.memories, "assert_memory_readable",
        _fake_assert_memory_readable(sqlite_backend.memories, fake_assert),
    )

    result = await mcp_dag.tool_log_memory("m1", user=ROOT)

    assert result["success"] is True, result
    assert result["memory_id"] == "m1"
    assert result["count"] == 2
    hashes = {c["hash"] for c in result["commits"]}
    assert hashes == {"abc123", "def456"}
    # The read-assert ran through the ABC on the backend's own repository,
    # not a hand-rolled conn.
    assert fake_assert.calls == [(sqlite_backend.memories, "m1", ROOT)]


async def test_tool_branch_memory_routes_through_persistence_abc(sqlite_backend, monkeypatch):
    _install_backend_for_tools(monkeypatch, sqlite_backend)

    result = await mcp_dag.tool_branch_memory("m1", name="dev", user=ROOT)

    assert result["success"] is True, result
    assert result["branch"] == "dev"
    assert result["commit_hash"] == "def456"
    # Re-creating an existing branch at the same HEAD must not fail (the
    # Postgres ABC flags it ``idempotent``; the SQLite ABC returns the plain
    # success shape — both are honest, neither refuses).
    again = await mcp_dag.tool_branch_memory("m1", name="dev", user=ROOT)
    assert again["success"] is True, again
    assert again["branch"] == "dev", again
    assert again["commit_hash"] == "def456", again


async def test_tool_diff_memory_commits_routes_through_persistence_abc(sqlite_backend, monkeypatch):
    _install_backend_for_tools(monkeypatch, sqlite_backend)
    fake_assert = _FakeAssertMemoryReadable()
    monkeypatch.setattr(
        sqlite_backend.memories, "assert_memory_readable",
        _fake_assert_memory_readable(sqlite_backend.memories, fake_assert),
    )

    result = await mcp_dag.tool_diff_memory_commits("m1", "abc123", "def456", user=ROOT)

    assert result["success"] is True, result
    assert "+hello v2" in result["diff"]
    assert fake_assert.calls == [(sqlite_backend.memories, "m1", ROOT)]


async def test_tool_checkout_memory_routes_through_persistence_abc(sqlite_backend, monkeypatch):
    _install_backend_for_tools(monkeypatch, sqlite_backend)
    fake_assert = _FakeAssertMemoryReadable()
    monkeypatch.setattr(
        sqlite_backend.memories, "assert_memory_readable",
        _fake_assert_memory_readable(sqlite_backend.memories, fake_assert),
    )

    result = await mcp_dag.tool_checkout_memory("m1", "abc123", user=ROOT)

    assert result["success"] is True, result
    assert result["commit"]["hash"] == "abc123"
    assert result["commit"]["version"] == 1
    assert result["content"] == "hello v1"
    assert fake_assert.calls == [(sqlite_backend.memories, "m1", ROOT)]


async def test_tool_recommend_model_routes_through_persistence_abc(sqlite_backend, monkeypatch):
    _install_backend_for_tools(monkeypatch, sqlite_backend)
    # No REST fallback should fire: a backend exists and the tool must use it.
    monkeypatch.setattr(mcp_models, "_rest_get", _NoRestFallback())

    result = await mcp_models.tool_recommend_model("code_generation", user=ROOT)

    # The SQLite model_registry table is empty in this fixture, so the ABC
    # reports no priced model. The tool surfaces the honest "no models"
    # outcome — not "Database unavailable" and not a REST fallback.
    assert result["success"] is False, result
    assert result["error"] == "No models available"
