"""NG2-MN-020: read-only MCP memory-version tools.

Regression coverage for ``list_memory_versions`` /
``get_memory_version``:

* ``tools/list`` includes both tools (they fail on the old code with
  ``Unknown tool`` because the registry had no such entries).
* Direct-DB mode on the SQLite backend returns the branch history
  oldest-first and the single snapshot by version number.
* Cross-namespace callers get an empty / not-found result (the same
  visibility gate as the REST routes).
* Vault rows are redacted, never plaintext.
* The unauthenticated transport (``user=None``) proxies through the
  same REST routes the UI uses, so direct-DB and REST-backed mode
  behave identically.

Against the pre-fix tree these tests fail: the TOOLS registry has no
``list_memory_versions`` / ``get_memory_version`` entries and the
``MemoryRepository`` ABC has no ``fetch_memory_versions`` /
``fetch_memory_version`` methods.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from mnemos.core.auth_context import UserContext
from mnemos.core.config import _reset_settings_for_tests
from mnemos.mcp import tools as mcp_tools
from mnemos.mcp.tools import dag as mcp_dag
from mnemos.persistence.sqlite import SqliteBackend, _execute, _fetch_one

pytestmark = pytest.mark.asyncio

ROOT = UserContext(user_id="root", group_ids=[], role="root", namespace="root", authenticated=True)
ALICE = UserContext(
    user_id="alice", group_ids=[], role="user", namespace="alice-ns", authenticated=True
)
BOB = UserContext(
    user_id="bob", group_ids=[], role="user", namespace="bob-ns", authenticated=True
)


def _install_backend_for_tools(monkeypatch, backend: SqliteBackend) -> None:
    monkeypatch.setattr(mcp_dag, "_get_persistence_backend", lambda: backend)


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    _reset_settings_for_tests()
    from mnemos.core.config import get_settings

    backend = SqliteBackend(str(tmp_path / "ng2_mn020_test.db"), get_settings())
    await backend.open()

    conn = backend._conn
    await _execute(
        conn,
        "INSERT INTO memories (id, content, owner_id, namespace, permission_mode) "
        "VALUES ('m1', 'seed', 'alice', 'alice-ns', 7)",
    )
    await _execute(
        conn,
        "INSERT INTO memory_versions "
        "(id, memory_id, commit_hash, version_num, branch, content, change_type, "
        " snapshot_at, snapshot_by, owner_id, namespace, permission_mode) "
        "VALUES ('v1', 'm1', 'aaa111', 1, 'main', 'hello v1', 'create', "
        " '2026-05-02T00:00:00+00:00', 'alice', 'alice', 'alice-ns', 7)",
    )
    await _execute(
        conn,
        "INSERT INTO memory_versions "
        "(id, memory_id, commit_hash, parent_version_id, version_num, branch, content, "
        " change_type, snapshot_at, snapshot_by, owner_id, namespace, permission_mode) "
        "VALUES ('v2', 'm1', 'bbb222', 'v1', 2, 'main', 'hello v2', 'update', "
        " '2026-05-03T00:00:00+00:00', 'alice', 'alice', 'alice-ns', 7)",
    )
    head = await _fetch_one(
        conn,
        "SELECT id FROM memory_versions WHERE memory_id = 'm1' AND version_num = 2",
    )
    await _execute(
        conn,
        "INSERT INTO memory_branches (memory_id, name, head_version_id, created_by) "
        "VALUES ('m1', 'main', ?, 'alice')",
        (head["id"],),
    )
    await conn.commit()
    yield backend
    await backend.close()


async def test_registry_lists_version_tools():
    assert "list_memory_versions" in mcp_tools.TOOL_REGISTRY
    assert "get_memory_version" in mcp_tools.TOOL_REGISTRY
    assert "list_memory_versions" in mcp_tools._TOOL_ORDER
    assert "get_memory_version" in mcp_tools._TOOL_ORDER


async def test_list_memory_versions_direct_db(sqlite_backend, monkeypatch):
    _install_backend_for_tools(monkeypatch, sqlite_backend)
    result = await mcp_dag.tool_list_memory_versions("m1", user=ALICE)
    assert result["success"] is True, result
    assert result["memory_id"] == "m1"
    assert result["branch"] == "main"
    assert result["count"] == 2
    assert [v["version"] for v in result["versions"]] == [1, 2]


async def test_get_memory_version_direct_db(sqlite_backend, monkeypatch):
    _install_backend_for_tools(monkeypatch, sqlite_backend)
    result = await mcp_dag.tool_get_memory_version("m1", 1, user=ALICE)
    assert result["success"] is True, result
    assert result["version"]["version"] == 1
    assert result["content"] == "hello v1"


async def test_version_tools_cross_namespace_invisible(sqlite_backend, monkeypatch):
    """A caller in another namespace sees nothing (404-style, not 403)."""
    _install_backend_for_tools(monkeypatch, sqlite_backend)
    listed = await mcp_dag.tool_list_memory_versions("m1", user=BOB)
    assert listed["success"] is False, listed
    single = await mcp_dag.tool_get_memory_version("m1", 1, user=BOB)
    assert single["success"] is False, single


async def test_get_memory_version_redacts_vault(sqlite_backend, monkeypatch):
    """Vault rows come back masked, never plaintext."""
    conn = sqlite_backend._conn
    await _execute(
        conn,
        "INSERT INTO memories (id, content, owner_id, namespace, permission_mode) "
        "VALUES ('mv1', 'vault seed', 'root', 'vault', 7)",
    )
    secret = "token sk-test-1234567890abcdef"
    await _execute(
        conn,
        "INSERT INTO memory_versions "
        "(id, memory_id, commit_hash, version_num, branch, content, change_type, "
        " snapshot_at, snapshot_by, owner_id, namespace, permission_mode) "
        "VALUES ('vv1', 'mv1', 'ccc333', 1, 'main', ?, 'create', "
        " '2026-05-04T00:00:00+00:00', 'root', 'root', 'vault', 7)",
        (secret,),
    )
    head = await _fetch_one(
        conn,
        "SELECT id FROM memory_versions WHERE memory_id = 'mv1' AND version_num = 1",
    )
    await _execute(
        conn,
        "INSERT INTO memory_branches (memory_id, name, head_version_id, created_by) "
        "VALUES ('mv1', 'main', ?, 'root')",
        (head["id"],),
    )
    await conn.commit()

    _install_backend_for_tools(monkeypatch, sqlite_backend)
    result = await mcp_dag.tool_get_memory_version("mv1", 1, user=ROOT)
    assert result["success"] is True, result
    assert "sk-test-1234567890abcdef" not in result["content"]
    assert "[REDACTED]" in result["content"]


async def test_version_tools_rest_backed_parity(monkeypatch):
    """Unauthenticated transport proxies the same REST routes as the UI."""
    _install_backend_for_tools(monkeypatch, None)
    seen: dict[str, object] = {}

    async def _fake_rest_get(path, params=None):
        seen["path"] = path
        seen["params"] = params
        if path.endswith("/versions"):
            return [
                {
                    "version_num": 1,
                    "snapshot_at": "2026-05-02T00:00:00+00:00",
                    "snapshot_by": "alice",
                    "change_type": "create",
                    "content_preview": "hello v1",
                    "branch": "main",
                }
            ]
        return {
            "version_num": 1,
            "branch": "main",
            "change_type": "create",
            "category": "facts",
            "subcategory": None,
            "snapshot_at": "2026-05-02T00:00:00+00:00",
            "snapshot_by": "alice",
            "content": "hello v1",
        }

    monkeypatch.setattr(mcp_dag, "_rest_get", _fake_rest_get)

    listed = await mcp_dag.tool_list_memory_versions("m1", user=None)
    assert listed["success"] is True, listed
    assert seen["path"] == "/v1/memories/m1/versions"
    assert seen["params"] == {"branch": "main"}
    assert listed["count"] == 1
    assert listed["versions"][0]["version"] == 1

    single = await mcp_dag.tool_get_memory_version("m1", 1, user=None)
    assert single["success"] is True, single
    assert seen["path"] == "/v1/memories/m1/versions/1"
    assert single["version"]["version"] == 1
    assert single["content"] == "hello v1"


async def test_version_tools_execute_through_dispatcher(sqlite_backend, monkeypatch):
    """Both tools dispatch via execute_tool (tools/list parity)."""
    _install_backend_for_tools(monkeypatch, sqlite_backend)
    listed = await mcp_tools.execute_tool(
        "list_memory_versions", {"memory_id": "m1"}, user=ALICE
    )
    assert listed["success"] is True, listed
    assert listed["count"] == 2
    single = await mcp_tools.execute_tool(
        "get_memory_version", {"memory_id": "m1", "version_num": 2}, user=ALICE
    )
    assert single["success"] is True, single
    assert single["content"] == "hello v2"


async def test_get_memory_version_string_version_num_returns_error_envelope(
    sqlite_backend, monkeypatch
):
    """A loose client sending version_num as a string gets a clean error, not an exception."""
    from mnemos.mcp.tools import dag as mcp_dag2

    monkeypatch.setattr(mcp_dag2, "_get_persistence_backend", lambda: sqlite_backend)
    result = await mcp_dag2.tool_get_memory_version("m1", "1", user=ALICE)  # type: ignore[arg-type]
    assert result == {"success": False, "error": "version_num must be an integer"}


async def test_list_memory_versions_group_readable(sqlite_backend, monkeypatch):
    """Group-readable snapshots surface for group members (same predicate as REST)."""
    conn = sqlite_backend._conn
    await _execute(
        conn,
        "INSERT INTO memories (id, content, owner_id, namespace, permission_mode, group_id) "
        "VALUES ('g1', 'seed', 'alice', 'alice-ns', 640, 'team-a')",
    )
    await _execute(
        conn,
        "INSERT INTO memory_versions "
        "(id, memory_id, commit_hash, version_num, branch, content, change_type, "
        " snapshot_at, snapshot_by, owner_id, namespace, permission_mode, group_id) "
        "VALUES ('gv1', 'g1', 'ggg111', 1, 'main', 'group hello', 'create', "
        " '2026-05-02T00:00:00+00:00', 'alice', 'alice', 'alice-ns', 640, 'team-a')",
    )
    head = await _fetch_one(
        conn,
        "SELECT id FROM memory_versions WHERE memory_id = 'g1' AND version_num = 1",
    )
    await _execute(
        conn,
        "INSERT INTO memory_branches (memory_id, name, head_version_id, created_by) "
        "VALUES ('g1', 'main', ?, 'alice')",
        (head["id"],),
    )
    await conn.commit()

    _install_backend_for_tools(monkeypatch, sqlite_backend)
    carol = UserContext(
        user_id="carol", group_ids=["team-a"], role="user",
        namespace="alice-ns", authenticated=True,
    )
    result = await mcp_dag.tool_list_memory_versions("g1", user=carol)
    assert result["success"] is True, result
    assert result["count"] == 1

    single = await mcp_dag.tool_get_memory_version("g1", 1, user=carol)
    assert single["success"] is True, single
    assert single["content"] == "group hello"
