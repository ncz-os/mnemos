"""Memory-tag API, filtering, and backend-schema parity tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_route
from mnemos.domain.models import (
    MAX_TAGS_PER_REQUEST,
    MemoryCreateRequest,
    MemoryListRequest,
    MemorySearchRequest,
    MemoryUpdateRequest,
)
from mnemos.mcp.tools import memory as mcp_memory
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


def _root() -> UserContext:
    return UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )


def _user(user_id: str) -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role="user",
        namespace="team",
        authenticated=True,
    )


async def _insert(backend: SqliteBackend, memory_id: str, owner_id: str, content: str, tags: list[str]) -> None:
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=content,
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=75,
            owner_id=owner_id,
            namespace="team",
            permission_mode=700,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=content,
            created=None,
            updated=None,
        )
        await backend.memories.replace_memory_tags(tx, memory_id, tags)


def test_create_and_tag_only_update_replace_complete_set(tmp_path, monkeypatch):
    async def _go() -> None:
        backend = SqliteBackend(tmp_path / "tags-route.db", SimpleNamespace())
        await backend.open()
        monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

        async def _no_embedding(_content):
            return None

        async def _no_publish(*_args, **_kwargs):
            return None

        monkeypatch.setattr(memories_route, "_get_embedding", _no_embedding)
        monkeypatch.setattr(memories_route, "_publish_nats_with_timeout", _no_publish)
        try:
            from fastapi import Response

            created = await memories_route.create_memory(
                MemoryCreateRequest(
                    content="tag route creation",
                    tags=["project-b", "project-a"],
                ),
                Response(),
                user=_root(),
            )
            assert created.tags == ["project-a", "project-b"]
            async with backend.transactional() as tx:
                versions_before = await backend.memory_versions.fetch_memory_versions_for_export(
                    tx,
                    memory_ids=[created.id],
                    effective_owner=None,
                    effective_ns=None,
                    hard_limit=10,
                )

            replaced = await memories_route.update_memory(
                created.id,
                MemoryUpdateRequest(tags=["project-c"]),
                user=_root(),
            )
            assert replaced.tags == ["project-c"]

            cleared = await memories_route.update_memory(
                created.id,
                MemoryUpdateRequest(tags=[]),
                user=_root(),
            )
            assert cleared.tags == []
            async with backend.transactional() as tx:
                versions_after = await backend.memory_versions.fetch_memory_versions_for_export(
                    tx,
                    memory_ids=[created.id],
                    effective_owner=None,
                    effective_ns=None,
                    hard_limit=10,
                )
            assert len(versions_after) == len(versions_before)
        finally:
            await backend.close()

    asyncio.run(_go())


def test_sqlite_tag_filters_use_any_semantics_and_preserve_visibility(tmp_path):
    async def _go() -> None:
        backend = SqliteBackend(tmp_path / "tags-filter.db", SimpleNamespace())
        await backend.open()
        try:
            await _insert(backend, "mem_alpha", "alice", "tagfilter alpha", ["project-a", "shared"])
            await _insert(backend, "mem_beta", "alice", "tagfilter beta", ["project-b"])
            await _insert(backend, "mem_private", "bob", "tagfilter private", ["project-a"])

            alice_visibility = VisibilityFilter.for_read(_user("alice"), namespace="team")
            async with backend.transactional() as tx:
                one, total_one = await backend.memories.list_memories(
                    tx,
                    visibility=alice_visibility,
                    tags=["project-a"],
                )
                many, total_many = await backend.memories.list_memories(
                    tx,
                    visibility=alice_visibility,
                    tags=["project-a", "project-b"],
                )
                none, total_none = await backend.memories.list_memories(
                    tx,
                    visibility=alice_visibility,
                    tags=["missing"],
                )
                searched = await backend.memories.fts_search(
                    tx,
                    query="tagfilter",
                    limit=20,
                    visibility=alice_visibility,
                    tags=["project-b", "missing"],
                )

            assert {row["id"] for row in one} == {"mem_alpha"}
            assert total_one == 1
            assert {row["id"] for row in many} == {"mem_alpha", "mem_beta"}
            assert total_many == 2
            assert none == []
            assert total_none == 0
            assert {row["id"] for row in searched} == {"mem_beta"}
        finally:
            await backend.close()

    asyncio.run(_go())


def test_tag_count_cap_is_enforced_on_create_update_and_search():
    too_many = [f"project-{i}" for i in range(MAX_TAGS_PER_REQUEST + 1)]
    for model, kwargs in (
        (MemoryCreateRequest, {"content": "x", "tags": too_many}),
        (MemoryUpdateRequest, {"tags": too_many}),
        (MemoryListRequest, {"tags": too_many}),
        (MemorySearchRequest, {"query": "x", "tags": too_many}),
    ):
        with pytest.raises(ValidationError):
            model(**kwargs)


def test_multibyte_tags_honor_the_255_character_api_contract():
    tag = "é" * 255
    assert len(tag) == 255
    assert len(tag.encode("utf-8")) == 510
    for model, kwargs in (
        (MemoryCreateRequest, {"content": "x", "tags": [tag]}),
        (MemoryUpdateRequest, {"tags": [tag]}),
        (MemoryListRequest, {"tags": [tag]}),
        (MemorySearchRequest, {"query": "x", "tags": [tag]}),
    ):
        assert model(**kwargs).tags == [tag]


def test_tag_only_patch_rechecks_authorization_at_the_locked_write(monkeypatch):
    class _Repo:
        def __init__(self) -> None:
            self.tags = ["before"]
            self.visibility = None

        async def get_memory(self, _tx, _memory_id, **_kwargs):
            return {
                "id": "mem-race",
                "content": "payload",
                "category": "facts",
                "subcategory": None,
                "created": datetime.now(timezone.utc),
                "updated": datetime.now(timezone.utc),
                "metadata": {},
                "owner_id": "alice",
                "namespace": "team",
                "permission_mode": 700,
            }

        async def replace_memory_tags(
            self,
            _tx,
            _memory_id,
            tags,
            *,
            visibility=None,
        ):
            self.visibility = visibility
            # Simulate an ownership transfer to bob committing immediately
            # before alice reaches the parent-row lock.
            if visibility is not None:
                return False
            self.tags = list(tags)
            return True

        async def fetch_memory_tags(self, _tx, memory_ids):
            return {memory_id: list(self.tags) for memory_id in memory_ids}

    class _Backend:
        def __init__(self) -> None:
            self.memories = _Repo()

        @asynccontextmanager
        async def transactional(self):
            yield SimpleNamespace(conn=None)

    async def _go() -> None:
        backend = _Backend()
        monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
        with pytest.raises(HTTPException) as exc_info:
            await memories_route.update_memory(
                "mem-race",
                MemoryUpdateRequest(tags=["after"]),
                user=_user("alice"),
            )
        assert exc_info.value.status_code == 404
        assert backend.memories.tags == ["before"]
        assert backend.memories.visibility.scope is VisibilityScope.OWN_ONLY
        assert backend.memories.visibility.user_id == "alice"
        assert backend.memories.visibility.namespace == "team"

    asyncio.run(_go())


def test_mcp_tools_forward_tags_to_shared_http_surface(monkeypatch):
    calls: list[tuple[str, object]] = []

    async def _post(path, body, method="POST"):
        calls.append((path, body))
        return {"ok": True}

    async def _get(path, params=None):
        calls.append((path, params))
        return {"ok": True}

    monkeypatch.setattr(mcp_memory, "_rest_post", _post)
    monkeypatch.setattr(mcp_memory, "_rest_get", _get)

    async def _go() -> None:
        await mcp_memory.tool_create_memory("x", tags=["project-a"])
        await mcp_memory.tool_update_memory("mem_1_a", tags=[])
        await mcp_memory.tool_search_memories("x", tags=["project-a", "project-b"])
        await mcp_memory.tool_list_memories(tags=["project-b"])

    asyncio.run(_go())
    assert calls[0][1]["tags"] == ["project-a"]
    assert calls[1][1]["tags"] == []
    assert calls[2][1]["tags"] == ["project-a", "project-b"]
    assert calls[3][1]["tags"] == ["project-b"]


@pytest.mark.parametrize(
    "backend_dir",
    [
        "migrations",
        "migrations_oracle",
        "migrations_db2",
        "migrations_mysql",
        "migrations_mariadb",
        "migrations_sqlite",
    ],
)
def test_memory_tags_migration_parity(backend_dir):
    root = Path(__file__).resolve().parents[1]
    sql = (root / "mnemos" / "db_migrations" / backend_dir / "0054_memory_tags.sql").read_text()
    lowered = sql.lower()
    assert "memory_tags" in lowered
    assert "memory_id" in lowered
    assert "tag" in lowered
    assert "added_at" in lowered
    assert "primary key (memory_id, tag)" in lowered
    assert "on delete cascade" in lowered
    assert "idx_memory_tags_tag" in lowered


def test_oracle_and_db2_tag_columns_use_character_length_units():
    root = Path(__file__).resolve().parents[1] / "mnemos" / "db_migrations"
    oracle = (root / "migrations_oracle" / "0054_memory_tags.sql").read_text()
    db2 = (root / "migrations_db2" / "0054_memory_tags.sql").read_text()

    assert "VARCHAR2(255 CHAR)" in oracle
    assert "VARCHAR(255 CODEUNITS32)" in db2
