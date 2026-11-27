"""Characterisation of ``mnemos/db/mcp_repo.py`` (FB1123).

Pins the six MCP SQL helpers as statement and argument contracts: which table
each one reads for root versus non-root callers, where the visibility
placeholders start, and the exact result dictionaries branch creation returns.
Behaviour that looks wrong is pinned and named as a divergence in the summary.
"""

from __future__ import annotations

import pytest

from mnemos.core.auth_context import UserContext
from mnemos.core.visibility import acl_principals
from mnemos.db import mcp_repo


def _alice(groups=None) -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=list(groups or []),
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _compact(sql: str) -> str:
    return " ".join(sql.split())


class _Rec:
    """Records calls; answers from a per-fragment routing function."""

    def __init__(self, *, router=None, fetch_result=None) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.router = router
        self.fetch_result = [] if fetch_result is None else fetch_result

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if self.router is None:
            return None
        return self.router(_compact(sql), args)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.fetch_result

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return None

    def of(self, kind: str) -> list[tuple[str, tuple]]:
        return [(sql, args) for called, sql, args in self.calls if called == kind]

    def only(self, kind: str) -> tuple[str, tuple]:
        matches = self.of(kind)
        assert len(matches) == 1, f"expected one {kind} call, got {self.calls}"
        return matches[0]


class _Txn:
    def __init__(self, events: list[str], name: str = "txn") -> None:
        self.events = events
        self.name = name

    async def __aenter__(self):
        self.events.append(f"{self.name}:begin")
        return None

    async def __aexit__(self, *exc_info):
        self.events.append(f"{self.name}:commit")
        return False


# ── assert_memory_readable ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_readability_is_a_memory_versions_probe() -> None:
    conn = _Rec(router=lambda sql, args: {"?column?": 1})

    await mcp_repo.assert_memory_readable(conn, "mem-1", _root())

    sql, args = conn.only("fetchrow")
    compact = _compact(sql)
    assert compact.startswith("SELECT 1 FROM memory_versions")
    assert "deleted_at IS NULL" in compact
    assert "FROM memories " not in compact
    assert args == ("mem-1",)


@pytest.mark.asyncio
async def test_root_readability_fails_closed_when_no_version_exists() -> None:
    conn = _Rec(router=lambda sql, args: None)

    with pytest.raises(PermissionError, match="Memory not found"):
        await mcp_repo.assert_memory_readable(conn, "mem-1", _root())


@pytest.mark.asyncio
async def test_non_root_readability_is_the_live_row_plus_the_visibility_predicate() -> None:
    conn = _Rec(router=lambda sql, args: {"?column?": 1})

    await mcp_repo.assert_memory_readable(conn, "mem-1", _alice())

    sql, args = conn.only("fetchrow")
    compact = _compact(sql)
    assert compact.startswith("SELECT 1 FROM memories")
    assert "owner_id=$2" in compact
    assert "namespace = $5" in compact
    assert "FROM memory_versions" not in compact
    assert args == ("mem-1", "alice", [], acl_principals("alice", []), "alice-ns")


@pytest.mark.asyncio
async def test_non_root_readability_includes_group_principals() -> None:
    conn = _Rec(router=lambda sql, args: {"?column?": 1})

    await mcp_repo.assert_memory_readable(conn, "mem-1", _alice(["team-a"]))

    _, args = conn.only("fetchrow")
    assert args[2] == ["team-a"]
    assert args[3] == ["user:alice", "group:team-a"]


@pytest.mark.asyncio
async def test_non_root_readability_fails_closed() -> None:
    conn = _Rec(router=lambda sql, args: None)

    with pytest.raises(PermissionError, match="Memory not found"):
        await mcp_repo.assert_memory_readable(conn, "mem-1", _alice())


@pytest.mark.asyncio
async def test_root_and_non_root_check_different_tables() -> None:
    # Root asks "does any version exist?", an owner asks "is the live row
    # readable?". A memory with no versions is therefore invisible to root
    # while its owner still reads it. Pinned as the current split.
    root_conn = _Rec(router=lambda sql, args: None)
    posix_conn = _Rec(router=lambda sql, args: {"?column?": 1})

    with pytest.raises(PermissionError):
        await mcp_repo.assert_memory_readable(root_conn, "mem-1", _root())

    await mcp_repo.assert_memory_readable(posix_conn, "mem-1", _alice())

    assert "memory_versions" in _compact(root_conn.only("fetchrow")[0])
    assert "FROM memories" in _compact(posix_conn.only("fetchrow")[0])


# ── fetch_memory_log ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_log_walk_is_unscoped_and_takes_three_arguments() -> None:
    conn = _Rec(fetch_result=[("hash-1",)])

    rows = await mcp_repo.fetch_memory_log(conn, "mem-1", "main", 10, _root())

    sql, args = conn.only("fetch")
    compact = _compact(sql)
    assert args == ("mem-1", "main", 10)
    assert "owner_id=" not in compact
    assert "macl.principal" not in compact
    assert "cw.depth < $3" in compact
    assert "LIMIT $3" in compact
    assert rows == [("hash-1",)]


@pytest.mark.asyncio
async def test_the_walk_pins_depth_and_the_same_memory_predicate() -> None:
    conn = _Rec()

    await mcp_repo.fetch_memory_log(conn, "mem-1", "main", 5, _root())

    sql, _ = conn.only("fetch")
    compact = _compact(sql)
    assert "WITH RECURSIVE commit_walk AS" in compact
    assert "mb.name = $2" in compact
    assert "mb.head_version_id = mv.id" in compact
    assert "mv.id = cw.parent_version_id" in compact
    assert "mv.memory_id = cw.memory_id" in compact
    assert "ORDER BY depth ASC" in compact


@pytest.mark.asyncio
async def test_non_root_log_walk_scopes_both_halves_and_appends_namespace() -> None:
    conn = _Rec()

    await mcp_repo.fetch_memory_log(conn, "mem-1", "main", 10, _alice())

    sql, args = conn.only("fetch")
    compact = _compact(sql)
    assert "mv.owner_id=$4" in compact
    assert "mv.group_id = ANY($5::text[])" in compact
    assert "macl.principal = ANY($6::text[])" in compact
    assert "mv.namespace = $7" in compact
    assert "AND mv.namespace = $7" in compact
    assert args == (
        "mem-1",
        "main",
        10,
        "alice",
        [],
        acl_principals("alice", []),
        "alice-ns",
    )
    # The scope predicate appears in the anchor and in the recursive term.
    assert compact.count("mv.namespace = $7") == 2


@pytest.mark.asyncio
async def test_log_returns_a_list_copy_of_the_rows() -> None:
    conn = _Rec(fetch_result=(("a",), ("b",)))

    rows = await mcp_repo.fetch_memory_log(conn, "mem-1", "main", 10, _root())

    assert rows == [("a",), ("b",)]
    assert isinstance(rows, list)


# ── create_memory_branch ────────────────────────────────────────────────────


class _BranchConn:
    def __init__(
        self,
        *,
        live=True,
        start=True,
        main=True,
        insert="new",
        existing="absent",
    ) -> None:
        self.events: list[str] = []
        self.calls: list[tuple[str, str, tuple]] = []
        self.live = live
        self.start = start
        self.main = main
        self.insert = insert
        self.existing = existing

    def transaction(self):
        return _Txn(self.events)

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        compact = _compact(sql)

        if compact.startswith("SELECT 1 FROM memories") and "FOR SHARE" in compact:
            self.events.append("lock")
            return {"?column?": 1} if self.live else None

        if "FROM memory_versions" in compact and "commit_hash = $2" in compact:
            self.events.append("start-by-commit")
            return {"id": "v-from", "commit_hash": args[1]} if self.start else None

        if "FROM memory_versions mv" in compact and "mb.name = 'main'" in compact:
            self.events.append("start-main")
            return {"id": "v-main", "commit_hash": "main-hash"} if self.main else None

        if compact.startswith("INSERT INTO memory_branches"):
            self.events.append("insert")
            if self.insert == "new":
                return {"head_version_id": args[2]}
            return None

        if compact.startswith("SELECT mb.head_version_id, mv.commit_hash"):
            self.events.append("existing")
            return None if self.existing == "absent" else self.existing

        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    def only(self, kind: str) -> tuple[str, tuple]:
        matches = [(sql, args) for called, sql, args in self.calls if called == kind]
        assert len(matches) == 1, f"expected one {kind} call, got {self.calls}"
        return matches[0]


@pytest.mark.asyncio
async def test_branch_creation_answers_not_found_without_touching_the_start() -> None:
    conn = _BranchConn(live=False)

    result = await mcp_repo.create_memory_branch(conn, "mem-1", "feature", None, _alice())

    assert result == {"success": False, "error": "Memory not found"}
    assert conn.events == ["txn:begin", "lock", "txn:commit"]


@pytest.mark.asyncio
async def test_branch_creation_answers_commit_not_found_for_an_unreadable_commit() -> None:
    conn = _BranchConn(start=False)

    result = await mcp_repo.create_memory_branch(conn, "mem-1", "feature", "abc", _alice())

    assert result == {"success": False, "error": "Commit not found"}
    assert conn.events == ["txn:begin", "lock", "start-by-commit", "txn:commit"]


@pytest.mark.asyncio
async def test_branch_creation_answers_main_branch_not_found_without_main() -> None:
    conn = _BranchConn(main=False)

    result = await mcp_repo.create_memory_branch(conn, "mem-1", "feature", None, _alice())

    assert result == {"success": False, "error": "main branch not found"}
    assert conn.events == ["txn:begin", "lock", "start-main", "txn:commit"]


@pytest.mark.asyncio
async def test_branch_creation_inserts_at_the_start_and_reports_it() -> None:
    conn = _BranchConn()

    result = await mcp_repo.create_memory_branch(conn, "mem-1", "feature", None, _alice())

    assert result == {
        "success": True,
        "memory_id": "mem-1",
        "branch": "feature",
        "commit_hash": "main-hash",
        "created_by": "alice",
    }
    insert_sql, insert_args = [
        (sql, args) for called, sql, args in conn.calls if "INSERT INTO memory_branches" in _compact(sql)
    ][0]
    assert "ON CONFLICT (memory_id, name) DO NOTHING" in _compact(insert_sql)
    assert insert_args == ("mem-1", "feature", "v-main", "alice")
    assert conn.events == ["txn:begin", "lock", "start-main", "insert", "txn:commit"]


@pytest.mark.asyncio
async def test_branch_creation_locks_the_live_row_owner_scoped_for_non_root() -> None:
    conn = _BranchConn()

    await mcp_repo.create_memory_branch(conn, "mem-1", "feature", None, _alice())

    lock_sql = [sql for called, sql, _ in conn.calls if "FOR SHARE" in _compact(sql)][0]
    compact = _compact(lock_sql)
    assert "owner_id = $2 AND namespace = $3" in compact


@pytest.mark.asyncio
async def test_branch_creation_locks_the_live_row_unscoped_for_root() -> None:
    conn = _BranchConn()

    await mcp_repo.create_memory_branch(conn, "mem-1", "feature", None, _root())

    lock_sql = [sql for called, sql, _ in conn.calls if "FOR SHARE" in _compact(sql)][0]
    compact = _compact(lock_sql)
    assert "owner_id" not in compact
    assert "namespace" not in compact


@pytest.mark.asyncio
async def test_branch_creation_start_by_commit_is_namespace_pinned_for_non_root() -> None:
    conn = _BranchConn()

    await mcp_repo.create_memory_branch(conn, "mem-1", "feature", "abc", _alice())

    start_sql, start_args = [(sql, args) for called, sql, args in conn.calls if "commit_hash = $2" in _compact(sql)][0]
    compact = _compact(start_sql)
    assert "namespace = $6" in compact
    assert "owner_id=$3" in compact
    assert start_args == ("mem-1", "abc", "alice", [], acl_principals("alice", []), "alice-ns")


@pytest.mark.asyncio
async def test_duplicate_branch_at_the_same_head_is_idempotent() -> None:
    conn = _BranchConn(
        insert="duplicate",
        existing={"head_version_id": "v-main", "commit_hash": "main-hash"},
    )

    result = await mcp_repo.create_memory_branch(conn, "mem-1", "feature", None, _alice())

    assert result == {
        "success": True,
        "memory_id": "mem-1",
        "branch": "feature",
        "commit_hash": "main-hash",
        "created_by": "alice",
        "idempotent": True,
    }
    assert "existing" in conn.events


@pytest.mark.asyncio
async def test_duplicate_branch_without_a_from_commit_is_idempotent_even_at_another_head() -> None:
    # With no from_commit the caller only said "make this branch exist", so a
    # different existing head is reported as success rather than a conflict.
    conn = _BranchConn(
        insert="duplicate",
        existing={"head_version_id": "v-other", "commit_hash": "other-hash"},
    )

    result = await mcp_repo.create_memory_branch(conn, "mem-1", "feature", None, _alice())

    assert result["success"] is True
    assert result["idempotent"] is True
    assert result["commit_hash"] == "other-hash"


@pytest.mark.asyncio
async def test_duplicate_branch_at_a_different_head_is_refused() -> None:
    conn = _BranchConn(
        insert="duplicate",
        existing={"head_version_id": "v-other", "commit_hash": "other-hash"},
    )

    result = await mcp_repo.create_memory_branch(conn, "mem-1", "feature", "abc", _alice())

    assert result == {
        "success": False,
        "error": "branch already exists at a different head; refusing to silently move it",
    }


@pytest.mark.asyncio
async def test_duplicate_branch_with_an_invisible_head_asks_for_reconciliation() -> None:
    conn = _BranchConn(insert="duplicate", existing="absent")

    result = await mcp_repo.create_memory_branch(conn, "mem-1", "feature", None, _alice())

    assert result == {
        "success": False,
        "error": (
            "branch exists but its head is not visible or points at a foreign memory version; reconciliation required"
        ),
    }


# ── fetch_diff_commit_pair ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_diff_reads_both_commits_with_the_same_unscoped_sql() -> None:
    seen = []

    def router(sql, args):
        seen.append(args)
        return {"content": args[1], "version_num": 1}

    conn = _Rec(router=router)

    a, b = await mcp_repo.fetch_diff_commit_pair(conn, "mem-1", "aaa", "bbb", _root())

    assert a == {"content": "aaa", "version_num": 1}
    assert b == {"content": "bbb", "version_num": 1}
    assert seen == [("mem-1", "aaa"), ("mem-1", "bbb")]
    sqls = [sql for called, sql, _ in conn.calls]
    assert len(set(sqls)) == 1
    assert "owner_id=" not in _compact(sqls[0])
    assert "SELECT content, version_num" in _compact(sqls[0])


@pytest.mark.asyncio
async def test_non_root_diff_applies_the_visibility_predicate_to_both_reads() -> None:
    conn = _Rec(router=lambda sql, args: None)

    await mcp_repo.fetch_diff_commit_pair(conn, "mem-1", "aaa", "bbb", _alice())

    calls = conn.of("fetchrow")
    assert len(calls) == 2
    for sql, args in calls:
        compact = _compact(sql)
        assert "owner_id=$3" in compact
        assert "namespace = $6" in compact
        assert args == ("mem-1", args[1], "alice", [], acl_principals("alice", []), "alice-ns")
    assert [args[1] for _, args in calls] == ["aaa", "bbb"]


@pytest.mark.asyncio
async def test_diff_returns_none_for_missing_commits_in_place() -> None:
    conn = _Rec(router=lambda sql, args: {"content": args[1]} if args[1] == "aaa" else None)

    a, b = await mcp_repo.fetch_diff_commit_pair(conn, "mem-1", "aaa", "missing", _root())

    assert a == {"content": "aaa"}
    assert b is None


# ── fetch_checkout_commit ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_checkout_selects_the_nine_columns_unscoped() -> None:
    conn = _Rec(router=lambda sql, args: {"commit_hash": args[1]})

    row = await mcp_repo.fetch_checkout_commit(conn, "mem-1", "abc", _root())

    sql, args = conn.only("fetchrow")
    compact = _compact(sql)
    for column in (
        "commit_hash",
        "version_num",
        "branch",
        "category",
        "subcategory",
        "content",
        "change_type",
        "snapshot_at",
        "snapshot_by",
    ):
        assert column in compact
    assert "deleted_at IS NULL" in compact
    assert "owner_id=" not in compact
    assert args == ("mem-1", "abc")
    assert row == {"commit_hash": "abc"}


@pytest.mark.asyncio
async def test_non_root_checkout_is_namespace_pinned() -> None:
    conn = _Rec(router=lambda sql, args: None)

    assert await mcp_repo.fetch_checkout_commit(conn, "mem-1", "abc", _alice()) is None

    sql, args = conn.only("fetchrow")
    compact = _compact(sql)
    assert "owner_id=$3" in compact
    assert "namespace = $6" in compact
    assert args == ("mem-1", "abc", "alice", [], acl_principals("alice", []), "alice-ns")


# ── fetch_recommended_model ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recommendation_reads_only_available_undeprecated_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Rec(fetch_result=[])

    await mcp_repo.fetch_recommended_model(conn, "reasoning", 5.0, 0.8)

    sql, args = conn.only("fetch")
    compact = _compact(sql)
    assert "FROM model_registry" in compact
    assert "WHERE available = true" in compact
    assert "AND deprecated = false" in compact
    assert "COALESCE(graeae_weight, 0) AS graeae_weight" in compact
    assert "context_window" in compact
    assert args == ()


@pytest.mark.asyncio
async def test_recommendation_delegates_to_the_scorer_with_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def _choose(rows, task_type, cost_budget, quality_floor):
        captured["rows"] = rows
        captured["task_type"] = task_type
        captured["cost_budget"] = cost_budget
        captured["quality_floor"] = quality_floor
        return {"provider": "openai", "model_id": "m"}, ["no cheap model"]

    monkeypatch.setattr("mnemos.core.recommendation.choose_recommended_model", _choose)
    rows = ({"model_id": "a"}, {"model_id": "b"})
    conn = _Rec(fetch_result=rows)

    model, notes = await mcp_repo.fetch_recommended_model(conn, "reasoning", 5.0, 0.8)

    assert model == {"provider": "openai", "model_id": "m"}
    assert notes == ["no cheap model"]
    assert captured["rows"] == [{"model_id": "a"}, {"model_id": "b"}]
    assert isinstance(captured["rows"], list)
    assert captured["task_type"] == "reasoning"
    assert captured["cost_budget"] == 5.0
    assert captured["quality_floor"] == 0.8
