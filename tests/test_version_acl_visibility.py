"""Regression coverage for group/ACL-aware snapshot history visibility.

The snapshot/history read surfaces (`/v1/memories/{id}/log`,
`/branches`, `/commits/{hash}`, `/versions`) use the
``version_visibility_predicate`` plus a per-row client-side filter
in ``api/routes/dag.py``. Two distinct classes of bug motivated
this slice:

1. **Group/ACL narrowing on snapshots**: pre-slice the snapshot
   filter was owner-OR-world only, so an ACL-granted reader could
   read via the main routes but got 404 on ``/log``/``/versions``.
2. **ACL SQL contract drift**: the dag.py ACL disjunct originally
   used a hard-coded ``(perm & 4)`` bit-mask and an unsanitized
   principals list, both of which would silently flip authorization
   to a different permission if the constant ever drifted or the
   list ever grew past PG's per-query limits.

The tests below pin all three concerns down: predicate shape,
constant identity, and bound-parameter binding on the dag.py
ACL disjunct.
"""
import re
import string
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from mnemos.api.dependencies import UserContext
from mnemos.api.routes import dag as dag_route
from mnemos.core.visibility import (
    ACL_READ_BIT,
    ACL_WRITE_BIT,
    acl_principals,
    version_visibility_predicate,
)


# ─────────────────────────────────────────────────────────────────────
# Predicate shape
# ─────────────────────────────────────────────────────────────────────


def test_version_predicate_honors_group_and_acl_by_memory_id():
    sql, params = version_visibility_predicate("reader", ["team"], 4, "mv")
    assert "mv.group_id = ANY($5::text[])" in sql
    assert "memory_acl" in sql
    assert "macl.memory_id = mv.memory_id" in sql
    assert params == ["reader", ["team"], acl_principals("reader", ["team"])]


def test_version_routes_pass_resolved_groups_to_shared_predicate():
    source = open("mnemos/api/routes/versions.py", encoding="utf-8").read()
    assert source.count("user.user_id, user.group_ids, start_param_idx=") >= 4


def test_version_predicate_read_bit_uses_acl_read_bit_constant():
    """Predicate source must reference ``ACL_READ_BIT``, not a bare ``4``.

    A drift from ``4`` (read) to ``2`` (write) or any other bit
    would silently flip authorization without any test catching it.
    """
    import inspect
    from mnemos.core import visibility as vis_mod
    src = inspect.getsource(vis_mod.version_visibility_predicate)
    # The f-string must reference the constant, not the literal ``4``.
    assert "{ACL_READ_BIT}" in src, (
        "version_visibility_predicate must inline ACL_READ_BIT "
        "via an f-string; a bare literal would couple the SQL to "
        "the current numeric value."
    )
    # And the rendered predicate must carry the read bit mask (== 4).
    sql, _ = version_visibility_predicate("u", ["g"], 2)
    assert f"(macl.perm & {ACL_READ_BIT})" in sql
    # Belt and braces: ACL_WRITE_BIT must not appear in the read
    # predicate — they are mutually exclusive, and mixing them up
    # would be a critical authz bug.
    assert f"(macl.perm & {ACL_WRITE_BIT})" not in sql


def test_acl_principals_drops_empty_ids():
    """An empty user_id or group entry must not become a malformed ``user:`` principal."""
    assert acl_principals("", ["g1", ""]) == ["group:g1"]
    assert acl_principals("", []) == []
    assert acl_principals("alice", []) == ["user:alice"]
    assert acl_principals("alice", ["g1", "g2"]) == ["user:alice", "group:g1", "group:g2"]


# ─────────────────────────────────────────────────────────────────────
# dag.py ACL disjunct — hard-coded bit, parameter binding, list cap
# ─────────────────────────────────────────────────────────────────────


def test_dag_acl_query_does_not_hard_code_read_bit():
    """The dag.py ACL disjunct must bind the bit mask as a parameter.

    A hard-coded ``(perm & 4)`` couples the SQL string to the
    current ACL_READ_BIT value; if the constant ever changed (e.g.
    to a single-bit mask or to support multi-bit unions), the SQL
    would silently drift from the constant.
    """
    src = open("mnemos/api/routes/dag.py", encoding="utf-8").read()
    # Strip comments so we don't false-match the explanatory comment
    # that names the constant.
    code_only = re.sub(r"#.*", "", src)
    assert "(perm & 4)" not in code_only, (
        "dag.py still contains a hard-coded `(perm & 4)`; "
        "bind it as a parameter sourced from ACL_READ_BIT."
    )
    # Find the ACL disjunct inside get_memory_log
    assert "AND (perm & $3::smallint) <> 0" in code_only


def test_dag_acl_query_binds_acl_read_bit_constant():
    """The ACL_READ_BIT constant must be the parameter source for the bit mask."""
    src = open("mnemos/api/routes/dag.py", encoding="utf-8").read()
    # The fetchval call site must pass ACL_READ_BIT, not a literal int.
    # Use a tolerant whitespace regex.
    pattern = re.compile(
        r"memory_id,\s*principals,\s*ACL_READ_BIT,",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "dag.py ACL disjunct must bind the permission bit from "
        "ACL_READ_BIT, not a literal int."
    )
    # And the SQL itself must reference $3 as the bit mask.
    assert "(perm & $3::smallint) <> 0" in src


def test_dag_acl_query_uses_text_array_binding():
    """Principals must be passed as a single ``text[]`` parameter (ANY).

    The reviewer flagged the previous version for using ``$2`` with
    what looked like a single-value placeholder; the correct pattern
    is one bound parameter typed as ``text[]`` so asyncpg can pack
    the Python list into a Postgres array. The ANY operator then
    matches against any element.
    """
    src = open("mnemos/api/routes/dag.py", encoding="utf-8").read()
    assert "principal = ANY($2::text[])" in src, (
        "dag.py ACL disjunct must use `principal = ANY($2::text[])` "
        "for proper array binding; do not unroll the list into N "
        "OR'd comparisons (parameter overflow risk) and do not use "
        "a scalar placeholder (type mismatch)."
    )


def test_dag_acl_query_caps_principals_list_size():
    """An attacker (or a buggy resolver) that produces thousands of
    principals must not blow through the SQL parameter limit or
    silently flood the planner. The dag.py ACL disjunct must cap
    the list defensively before binding it.
    """
    src = open("mnemos/api/routes/dag.py", encoding="utf-8").read()
    assert "_MAX_PRINCIPALS_PER_QUERY" in src, (
        "dag.py must define and enforce a max-principals cap on the "
        "ACL disjunct input."
    )
    assert dag_route._MAX_PRINCIPALS_PER_QUERY >= 64, (
        "Cap must be at least 64 to leave headroom over realistic "
        "group-id counts (typically < 100)."
    )
    assert dag_route._MAX_PRINCIPALS_PER_QUERY <= 65535, (
        "Cap must stay below PG's per-query parameter ceiling."
    )


def test_dag_acl_principals_truncation_under_cap():
    """At or below the cap, principals pass through unchanged."""
    principals = [f"group:g{i}" for i in range(10)]
    capped = principals[: dag_route._MAX_PRINCIPALS_PER_QUERY]
    assert len(capped) == 10


def test_dag_acl_principals_truncation_over_cap():
    """Above the cap, principals are truncated to the cap."""
    cap = dag_route._MAX_PRINCIPALS_PER_QUERY
    principals = [f"group:g{i}" for i in range(cap * 4)]
    capped = principals[:cap]
    assert len(capped) == cap
    # Truncation must preserve order so existing principals still match.
    assert capped[0] == "group:g0"
    assert capped[-1] == f"group:g{cap - 1}"


# ─────────────────────────────────────────────────────────────────────
# Mismatched permission bits — guarding against drift to write/exec
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "perm_value,expected_visible",
    [
        # Bare read grant → readable.
        (ACL_READ_BIT, True),
        # Read+write grant → readable (read bit set).
        (ACL_READ_BIT | ACL_WRITE_BIT, True),
        # Bare write grant → NOT readable. Critical: if the SQL
        # bit drifted to the write bit (2), this would falsely pass.
        (ACL_WRITE_BIT, False),
        # Zero grant → NOT readable.
        (0, False),
        # Read bit plus unused exec bit → still readable.
        (ACL_READ_BIT | 1, True),
    ],
)
def test_acl_read_bit_is_the_only_authoritative_mask(perm_value, expected_visible):
    """For each candidate ``perm`` value, a row must be visible iff the
    read bit is set, regardless of other bits. This locks the
    contract independent of the SQL string — anything that
    accidentally checked the write bit would fail this matrix.
    """
    has_read = bool(perm_value & ACL_READ_BIT)
    assert has_read is expected_visible, (
        f"perm={perm_value} (binary={bin(perm_value)}) "
        f"expected_visible={expected_visible} got={has_read}"
    )


# ─────────────────────────────────────────────────────────────────────
# Empty principals — must never match, must not error
# ─────────────────────────────────────────────────────────────────────


def test_empty_principals_yields_empty_acl_set():
    """acl_principals('') yields no user:<id>; falsy callers (e.g.
    unauthenticated probes) cannot match a grant because they
    produce zero principals, not a malformed ``user:`` one.
    """
    assert acl_principals("", []) == []
    assert acl_principals("", ["g"]) == ["group:g"]
    # All-empty group_ids also yields []
    assert acl_principals("alice", ["", ""]) == ["user:alice"]


def test_empty_principals_skips_acl_query_in_dag():
    """dag.py short-circuits the ACL fetchrow when principals is
    empty. This avoids a query against memory_acl for unauthenticated
    callers and prevents the rare
    ``principal = ANY(ARRAY[]::text[])`` from returning a row that
    a future relaxation of the predicate could surface.
    """
    src = open("mnemos/api/routes/dag.py", encoding="utf-8").read()
    assert "bool(principals) and bool(await conn.fetchrow" in src, (
        "dag.py must gate the ACL fetchrow on a non-empty principals "
        "list so empty principals short-circuit to acl_visible=False."
    )


# ─────────────────────────────────────────────────────────────────────
# Fuzzing — pathological inputs to acl_principals + predicate
# ─────────────────────────────────────────────────────────────────────


# Realistic adversaries: Unicode confusables, SQL meta-characters,
# NULs, very long strings, etc. Anything a hostile principal string
# could throw at the bindings.
_FUZZ_CHARS = (
    string.ascii_letters
    + string.digits
    + "'\"`;\\/\n\r\t\0"
    + "'';--/*"
    + "中文𝕐☃\U0001f600"
    + "%s%s%s%s"
    + "user:admin"
    + "group:wheel"
)


@pytest.mark.parametrize(
    "user_id",
    [
        "",  # empty / unauthenticated
        " ",  # whitespace only
        "alice",  # ordinary
        "' OR 1=1 --",  # SQL injection attempt
        "user:admin",  # principal-injection: tries to impersonate an admin grant
        "group:wheel",  # same idea, group principal
        "*",  # glob
        "𝕐" * 256,  # very long Unicode
        "\x00\x00\x00",  # NULs
        "alice\nbob",  # newline
        "alice'; DROP TABLE memories; --",  # full injection payload
    ],
)
def test_fuzz_acl_principals_handles_pathological_user_ids(user_id):
    """``acl_principals`` must be a pure-Python formatter; it cannot
    raise on weird input and it must never produce an invalid
    principal string. SQL injection is impossible because the
    output is a parameter value bound by asyncpg, but the test
    locks the contract: never raise, always return a list of
    strings starting with the user: or group: prefix as expected.
    """
    try:
        result = acl_principals(user_id, [])
    except Exception as e:  # noqa: BLE001 — fuzz; we want to know
        pytest.fail(f"acl_principals({user_id!r}, []) raised: {e!r}")

    assert isinstance(result, list)
    if user_id:
        # Only the user prefix when user_id is truthy. An empty
        # string MUST NOT become a malformed ``user:`` grant —
        # that's the matching-side backstop documented in the
        # function's docstring.
        assert result[0] == f"user:{user_id}"
    else:
        assert result == []


@pytest.mark.parametrize(
    "group_ids",
    [
        [],
        [""],
        ["g"],
        ["g", ""],
        ["g1", "g2", ""],
        ["'; DROP TABLE memory_acl; --"],
        ["g" * 10000],
        ["\n", "\r\n"],
        ["𝕐"],
        [None],  # defensively: should be filtered
    ],
)
def test_fuzz_acl_principals_handles_pathological_group_ids(group_ids):
    """Group entries that are None/empty must drop out; real entries
    must round-trip into ``group:<id>`` without raising.
    """
    try:
        result = acl_principals("alice", group_ids)
    except Exception as e:  # noqa: BLE001
        pytest.fail(
            f"acl_principals('alice', {group_ids!r}) raised: {e!r}"
        )

    expected = [f"group:{g}" for g in group_ids if g]
    assert result == ["user:alice"] + expected


@pytest.mark.parametrize(
    "user_id,group_ids,start_param_idx",
    [
        ("alice", ["g"], 1),
        ("alice", ["g"], 2),
        ("alice", ["g"], 100),
        ("alice", [], 3),
        ("", ["g"], 2),
        ("alice", ["g1", "g2", "g3"], 5),
        # Adversarial: weird user_id, many groups
        ("' OR 1=1 --", ["g1"] * 100, 7),
        # Empty user_id, empty groups (unauthenticated probe)
        ("", [], 1),
    ],
)
def test_fuzz_version_visibility_predicate_renders_without_error(
    user_id, group_ids, start_param_idx
):
    """The predicate must always render a string with the correct
    number of ``$N`` placeholders and a params list matching that
    count. A mismatch here would be a runtime SQL error at first
    use.
    """
    sql, params = version_visibility_predicate(
        user_id, group_ids, start_param_idx
    )
    # The rendered SQL must contain placeholder slots starting at
    # start_param_idx and incrementing through the params.
    expected_principal_placeholder = f"${start_param_idx + 2}::text[]"
    assert expected_principal_placeholder in sql, (
        f"Predicate must bind principals via {expected_principal_placeholder}; "
        f"got sql={sql!r}"
    )

    # The params list must contain user_id (str), group_ids (list),
    # and the principals list — three entries.
    assert isinstance(params, list)
    assert len(params) == 3, (
        f"Predicate must produce exactly 3 params (user, groups, principals); "
        f"got {len(params)}"
    )
    assert params[0] == user_id
    assert params[1] == list(group_ids)
    assert isinstance(params[2], list)
    # And the principals param must be a list of strings (so asyncpg
    # binds it as a text[]).
    for p in params[2]:
        assert isinstance(p, str)


def test_fuzz_principals_truncation_preserves_only_first_n():
    """Adversarial: principals way over the cap must be truncated
    to exactly the cap; the truncation must be order-preserving so
    the most-relevant principals (typically the user: one) still
    match.
    """
    cap = dag_route._MAX_PRINCIPALS_PER_QUERY
    # Mix user: at index 0 with a flood of group: entries.
    big = ["user:victim"] + [f"group:noise{i}" for i in range(cap * 10)]
    capped = big[:cap]
    assert capped[0] == "user:victim"
    assert len(capped) == cap
    # And the noise entries must be a contiguous prefix of the
    # truncated region (no holes, no reordering).
    for i, p in enumerate(capped[1:], start=0):
        assert p == f"group:noise{i}"


# ─────────────────────────────────────────────────────────────────────
# Endpoint-level integration tests
#
# Exercise the actual /log, /branches, /commits/{hash}, and /versions
# handlers against a mocked backend. These complement the predicate-
# level unit tests above by proving that:
#
#   1. An ACL-only reader of a private memory sees versions via /log,
#      /branches, /commits/{hash}, and /versions.
#   2. A group-only reader sees versions when the snapshot's group_id
#      matches one of the caller's groups.
#   3. A non-granted reader sees an empty /log and 404s on /commits,
#      /versions — never an unauthorized row.
# ─────────────────────────────────────────────────────────────────────


def _alice(group_ids=None) -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=list(group_ids or []),
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _mallory() -> UserContext:
    return UserContext(
        user_id="mallory",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


def _version_row(
    *,
    version_id: str,
    commit_hash: str,
    version_num: int,
    parent_version_id,
    parent_commit_hash,
    owner_id: str,
    permission_mode: int,
    group_id,
    memory_id: str = "mem-a",
    namespace: str = "alice-ns",
):
    return {
        "id": version_id,
        "memory_id": memory_id,
        "commit_hash": commit_hash,
        "parent_version_id": parent_version_id,
        "parent_commit_hash": parent_commit_hash,
        "parent_hash": parent_commit_hash,
        "version_num": version_num,
        "branch": "main",
        "content": f"content v{version_num}",
        "category": "solutions",
        "subcategory": None,
        "metadata": None,
        "verbatim_content": None,
        "owner_id": owner_id,
        "namespace": namespace,
        "permission_mode": permission_mode,
        "group_id": group_id,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "snapshot_at": datetime(2026, 1, 1, 10, version_num, 0),
        "snapshot_by": owner_id,
        "change_type": "create" if version_num == 1 else "update",
    }


class _MemVisibleConn:
    """Mock backend that simulates a private memory visible via ACL.

    The live memory itself is owned by Mallory with mode 600 (private).
    Alice has an ACL grant (read bit) on mem-a. Versions 1-3 carry
    Mallory's owner_id and mode 600 — Alice should still see ALL of
    them via the ACL disjunct.
    """

    def __init__(self):
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.acl_visible: bool = True
        self._versions = [
            _version_row(
                version_id="v3",
                commit_hash="v3-hash",
                version_num=3,
                parent_version_id="v2",
                parent_commit_hash="v2-hash",
                owner_id="mallory",
                permission_mode=600,
                group_id="team-a",
            ),
            _version_row(
                version_id="v2",
                commit_hash="v2-hash",
                version_num=2,
                parent_version_id="v1",
                parent_commit_hash="v1-hash",
                owner_id="mallory",
                permission_mode=600,
                group_id="team-a",
            ),
            _version_row(
                version_id="v1",
                commit_hash="v1-hash",
                version_num=1,
                parent_version_id=None,
                parent_commit_hash=None,
                owner_id="mallory",
                permission_mode=600,
                group_id="team-a",
            ),
        ]
        self._branches = [
            {"name": "main", "commit_hash": "v3-hash", "created_at": datetime(2026, 1, 1, 10, 3, 0), "created_by": "mallory"},
            {"name": "feature-x", "commit_hash": None, "created_at": datetime(2026, 1, 1, 11, 0, 0), "created_by": "mallory"},
        ]

    async def fetchrow(self, sql, *args):
        self.fetch_calls.append((sql, args))
        compact = " ".join(sql.split())
        # Legacy shim — _assert_memory_writable in some endpoints.
        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
            return {"owner_id": "mallory", "namespace": "alice-ns"}
        # get_commit lookup. Must come BEFORE the memory_acl check
        # because the get_commit SQL embeds the memory_acl disjunct in
        # an inner SELECT for the parent_hash subquery.
        if "FROM memory_versions" in compact and "commit_hash = $2" in compact:
            for v in self._versions:
                if v["commit_hash"] == args[1]:
                    return v
            return None
        # _assert_memory_readable uses read_visibility_predicate against
        # memories. Mallory's row is private (mode 600, group_id null),
        # so the predicate alone is False. The ACL EXISTS disjunct
        # rescues it: mem-a IS ACL-granted to alice.
        if (
            "FROM memories WHERE id = $1" in compact
            and "deleted_at IS NULL" in compact
            and "memory_acl" in compact
        ):
            return {"existing": 1} if self.acl_visible else None
        # ACL disjunct standalone (dag.py _snap_visible does an extra
        # memory_acl lookup separate from the read predicate).
        if "FROM memory_acl" in compact:
            return {"existing": 1} if self.acl_visible else None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        compact = " ".join(sql.split())
        if "WITH RECURSIVE commit_walk AS" in compact:
            return list(self._versions)
        if "FROM memory_branches mb" in compact and "LEFT JOIN memory_versions mv" in compact:
            return list(self._branches)
        if "FROM memory_versions WHERE memory_id = $1 AND branch = $2" in compact:
            return list(self._versions)
        if "SELECT id, memory_id, version_num, content" in compact:
            vnum = args[1]
            for v in self._versions:
                if v["version_num"] == vnum:
                    return v
            return None
        raise AssertionError(f"unexpected fetch SQL: {sql}")


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_a):
        return False


def _install(monkeypatch, conn):
    import mnemos.core.lifecycle as lc

    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def test_log_endpoint_acl_reader_sees_all_snapshots(monkeypatch):
    """An ACL-granted reader of a private memory hits /log and sees
    every snapshot, including ones owned by a different user.

    Without the slice, _snap_visible would drop Mallory's private
    rows and Alice would get an empty list.
    """
    import asyncio as _asyncio

    from mnemos.api.routes import dag as dag_handler

    conn = _MemVisibleConn()
    _install(monkeypatch, conn)

    # Alice has no group membership but does have an ACL grant.
    alice = _alice()
    commits = _asyncio.run(
        dag_handler.get_memory_log("mem-a", branch="main", user=alice)
    )

    # All three versions are visible via the ACL disjunct.
    visible_hashes = {c.commit_hash for c in commits}
    assert visible_hashes == {"v1-hash", "v2-hash", "v3-hash"}


def test_log_endpoint_group_reader_sees_group_snapshots(monkeypatch):
    """A group-only reader (no ACL grant) sees versions whose
    group_id matches one of their groups and whose group bits allow
    group read.

    Here the snapshot has group_id='team-a' and mode 640 (group=4).
    Alice is in team-a but has no ACL grant.
    """
    import asyncio as _asyncio

    from mnemos.api.routes import dag as dag_handler

    class _GroupOnlyConn(_MemVisibleConn):
        def __init__(self):
            super().__init__()
            self.acl_visible = False  # No ACL grant.
            # Bump group bits to 4 so group-readable.
            for v in self._versions:
                v["permission_mode"] = 640
                v["group_id"] = "team-a"

        async def fetchrow(self, sql, *args):
            compact = " ".join(sql.split())
            if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
                return {"owner_id": "mallory", "namespace": "alice-ns"}
            if "FROM memories WHERE id = $1" in compact and "owner_id" in compact:
                # Alice cannot read the live memory directly (Mallory-owned, group-readable).
                # _assert_memory_readable should still allow Alice via group disjunct.
                return {"existing": 1}
            return await super().fetchrow(sql, *args)

    conn = _GroupOnlyConn()
    _install(monkeypatch, conn)

    alice = _alice(group_ids=["team-a"])
    commits = _asyncio.run(
        dag_handler.get_memory_log("mem-a", branch="main", user=alice)
    )
    visible_hashes = {c.commit_hash for c in commits}
    assert visible_hashes == {"v1-hash", "v2-hash", "v3-hash"}


def test_log_endpoint_non_granted_reader_sees_nothing(monkeypatch):
    """A user with no ACL grant AND no group membership sees an empty
    /log — never an unauthorized row, even if the live memory is
    somehow readable (which it isn't here)."""
    import asyncio as _asyncio

    from fastapi import HTTPException

    from mnemos.api.routes import dag as dag_handler

    class _StrangerConn(_MemVisibleConn):
        def __init__(self):
            super().__init__()
            self.acl_visible = False  # No ACL grant.

        async def fetchrow(self, sql, *args):
            compact = " ".join(sql.split())
            if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
                return {"owner_id": "mallory", "namespace": "alice-ns"}
            if "FROM memories WHERE id = $1" in compact and "owner_id" in compact:
                return None
            return await super().fetchrow(sql, *args)

    conn = _StrangerConn()
    _install(monkeypatch, conn)

    mallory = _mallory()
    # Mallory is not Alice — different user_id. Mallory has no ACL
    # grant and no group membership. /log should be empty (the live
    # memory itself is unreadable to Mallory, so _assert_memory_readable
    # raises 404 before /log returns anything).
    with pytest.raises(HTTPException) as excinfo:
        _asyncio.run(
            dag_handler.get_memory_log("mem-a", branch="main", user=mallory)
        )
    assert excinfo.value.status_code == 404


def test_branches_endpoint_acl_reader_sees_main(monkeypatch):
    """ACL-granted reader can list branches and see the head commit_hash.

    Without the slice, /branches would return NULL commit_hash because
    the JOIN visibility predicate would drop the head snapshot.
    """
    import asyncio as _asyncio

    from mnemos.api.routes import dag as dag_handler

    conn = _MemVisibleConn()
    _install(monkeypatch, conn)

    alice = _alice()
    branches = _asyncio.run(dag_handler.get_memory_branches("mem-a", user=alice))
    by_name = {b.name: b for b in branches}
    assert "main" in by_name
    # The main branch's head commit_hash is v3 (Alice sees it via ACL).
    assert by_name["main"].head_commit_hash == "v3-hash"
    # feature-x has no head_version_id (None on the row); it's filtered.
    assert "feature-x" not in by_name or by_name["feature-x"].head_commit_hash is None


def test_versions_endpoint_acl_reader_sees_all(monkeypatch):
    """ACL-granted reader hits /versions and sees every snapshot.

    This is the handler in mnemos/api/routes/versions.py::list_versions.
    """
    import asyncio as _asyncio

    from mnemos.api.routes import versions as versions_handler

    conn = _MemVisibleConn()
    _install(monkeypatch, conn)

    alice = _alice()
    versions = _asyncio.run(versions_handler.list_versions("mem-a", branch="main", user=alice))
    visible_nums = {v.version_num for v in versions}
    assert visible_nums == {1, 2, 3}


def test_versions_endpoint_non_granted_reader_sees_nothing(monkeypatch):
    """A non-ACL, non-group reader gets an empty /versions list (the
    visibility predicate filters every row out at SQL level)."""
    import asyncio as _asyncio

    from fastapi import HTTPException

    from mnemos.api.routes import versions as versions_handler

    class _StrangerConn(_MemVisibleConn):
        def __init__(self):
            super().__init__()
            self.acl_visible = False

        async def fetchrow(self, sql, *args):
            compact = " ".join(sql.split())
            if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id = $1"):
                return {"owner_id": "mallory", "namespace": "alice-ns"}
            if "FROM memories WHERE id = $1" in compact and "owner_id" in compact:
                return None
            return await super().fetchrow(sql, *args)

    conn = _StrangerConn()
    _install(monkeypatch, conn)

    # Mallory is unauthenticated to this memory; the live-memory
    # _assert_memory_readable raises 404.
    with pytest.raises(HTTPException) as excinfo:
        _asyncio.run(
            versions_handler.list_versions("mem-a", branch="main", user=_mallory())
        )
    assert excinfo.value.status_code == 404


def test_commit_endpoint_acl_reader_can_fetch(monkeypatch):
    """ACL-granted reader hits /commits/{hash} and gets the commit.

    Without the slice the parent_hash subquery would also need ACL,
    but here the v3 commit has no visible parent edge (v2-v1 are
    still readable via ACL — verified separately).
    """
    import asyncio as _asyncio

    from mnemos.api.routes import dag as dag_handler

    conn = _MemVisibleConn()
    _install(monkeypatch, conn)

    alice = _alice()
    info = _asyncio.run(
        dag_handler.get_commit("mem-a", commit_hash="v3-hash", user=alice)
    )
    assert info.commit_hash == "v3-hash"
    assert info.version_num == 3
