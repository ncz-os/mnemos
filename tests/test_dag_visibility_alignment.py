from __future__ import annotations

import pytest
from fastapi import HTTPException

from mnemos.api.dependencies import UserContext
from mnemos.api.routes.dag import _assert_memory_readable, _assert_memory_writable


pytestmark = pytest.mark.asyncio


class _Conn:
    async def fetchrow(self, sql: str, *_args):
        if "group_id = ANY" in sql:
            return {"ok": 1}
        if "owner_id, namespace" in sql:
            return {"owner_id": "other", "namespace": "default"}
        return {"ok": 1}


def _group_reader() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=["team"],
        role="user",
        namespace="default",
        authenticated=True,
    )


async def test_dag_read_allows_group_visible_memory():
    await _assert_memory_readable(_Conn(), "mem-1", _group_reader())


async def test_dag_write_stays_strict_owner_scoped():
    with pytest.raises(HTTPException) as exc:
        await _assert_memory_writable(_Conn(), "mem-1", _group_reader())
    assert exc.value.status_code == 404


async def test_dag_read_honors_acl_grant():
    """A reader visible only via a ``memory_acl`` grant must not 404 from DAG
    endpoints — the read predicate must carry the ACL EXISTS disjunct, the same
    one the main read path uses. Regression: the hand-rolled DAG predicate used
    to omit it, so an ACL-granted reader got a partial share (readable via main
    routes, 404 via DAG)."""
    from mnemos.core.visibility import acl_principals

    captured: dict = {}

    class _AclOnlyConn:
        async def fetchrow(self, sql: str, *args):
            captured["sql"] = sql
            captured["args"] = args
            # Memory is private (no owner/world/group match); only the ACL
            # disjunct can make it visible.
            if "EXISTS (SELECT 1 FROM memory_acl" in sql:
                return {"ok": 1}
            return None

    await _assert_memory_readable(_AclOnlyConn(), "mem-acl", _group_reader())
    assert "EXISTS (SELECT 1 FROM memory_acl" in captured["sql"]
    assert acl_principals("alice", ["team"]) in captured["args"]
