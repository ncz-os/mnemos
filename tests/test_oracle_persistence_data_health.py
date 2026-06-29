from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from mnemos.persistence.oracle import (
    OracleMemoryRepository,
    _content_hash,
    _render_visibility,
)
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


class _Cursor:
    def __init__(self, *, rowcount: int = 1):
        self.rowcount = rowcount
        self.executions: list[tuple[str, dict]] = []
        self.description = [
            ("ID",),
            ("CONTENT",),
            ("CATEGORY",),
            ("SUBCATEGORY",),
            ("METADATA",),
            ("QUALITY_RATING",),
            ("COMPRESSED_CONTENT",),
            ("VERBATIM_CONTENT",),
            ("OWNER_ID",),
            ("NAMESPACE",),
            ("PERMISSION_MODE",),
            ("SOURCE_MODEL",),
            ("SOURCE_PROVIDER",),
            ("SOURCE_SESSION",),
            ("SOURCE_AGENT",),
            ("GROUP_ID",),
            ("CREATED",),
            ("UPDATED",),
            ("ARCHIVED_AT",),
            ("DELETED_AT",),
        ]

    def execute(self, sql, params=None):
        self.executions.append((sql, dict(params or {})))

    def fetchone(self):
        return (
            "mem-1",
            "new body",
            "notes",
            None,
            "{}",
            75,
            None,
            "new body",
            "alice",
            "default",
            600,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_oracle_read_visibility_matches_v1_multiuser_shape():
    visibility = VisibilityFilter(
        scope=VisibilityScope.READABLE,
        user_id="alice",
        group_ids=("team-a", "team-b"),
        namespace="alice-ns",
    )

    clause, params = _render_visibility(visibility, table_alias="m", param_prefix="v")

    assert "m.owner_id = :v_owner" in clause
    assert "m.federation_source IS NOT NULL" in clause
    assert "MOD(NVL(m.permission_mode, 0), 10) >= 4" in clause
    assert "m.group_id IN (:v_group0,:v_group1)" in clause
    assert "EXISTS (SELECT 1 FROM memory_acl macl" in clause
    assert "macl.principal IN (:v_acl0,:v_acl1,:v_acl2)" in clause
    assert "m.namespace = :v_ns" in clause
    assert "namespace = 'world'" not in clause
    assert params == {
        "v_owner": "alice",
        "v_ns": "alice-ns",
        "v_group0": "team-a",
        "v_group1": "team-b",
        "v_acl0": "user:alice",
        "v_acl1": "group:team-a",
        "v_acl2": "group:team-b",
    }


@pytest.mark.asyncio
async def test_oracle_insert_memory_persists_normalized_content_hash():
    cursor = _Cursor()
    repo = OracleMemoryRepository()

    result = await repo.insert_memory(
        SimpleNamespace(conn=_Conn(cursor)),
        memory_id="mem-1",
        content="line1\r\nline2",
        category="notes",
        subcategory=None,
        metadata_json="{}",
        quality_rating=75,
        owner_id="alice",
        namespace="default",
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=None,
        created=None,
        updated=None,
    )

    assert result == "INSERT 0 1"
    _, params = cursor.executions[0]
    assert params["content_hash"] == hashlib.sha256(b"line1\nline2").hexdigest()
    assert params["content_hash"] == _content_hash("line1\r\nline2")


@pytest.mark.asyncio
async def test_oracle_update_memory_refreshes_content_hash_atomically():
    cursor = _Cursor()
    repo = OracleMemoryRepository()
    visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )

    row = await repo.update_memory(
        SimpleNamespace(conn=_Conn(cursor)),
        "mem-1",
        visibility=visibility,
        fields={"content": "new body"},
    )

    assert row is not None
    update_sql, update_params = cursor.executions[0]
    assert "content_hash = :f_content_hash" in update_sql
    assert update_params["f_content_hash"] == _content_hash("new body")
