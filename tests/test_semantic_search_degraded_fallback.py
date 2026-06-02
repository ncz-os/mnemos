from __future__ import annotations

from types import SimpleNamespace

import pytest

import mnemos.core.lifecycle as lifecycle
from mnemos.api.dependencies import UserContext
from mnemos.api.routes import memories as memories_handler
from mnemos.domain.models import MemorySearchRequest
from mnemos.persistence.sqlite import SqliteBackend
from mnemos.persistence.visibility import VisibilityFilter

pytestmark = pytest.mark.asyncio


def _alice() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


async def _insert_memory(backend: SqliteBackend, *, memory_id: str, content: str) -> None:
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=memory_id,
            content=content,
            category="facts",
            subcategory=None,
            metadata_json="{}",
            quality_rating=75,
            owner_id="alice",
            namespace="alice-ns",
            permission_mode=600,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=content,
            created=None,
            updated=None,
        )


async def test_semantic_search_falls_back_to_exact_text_on_dim_mismatch(tmp_path, monkeypatch):
    backend = SqliteBackend(
        tmp_path / "semantic-degraded.sqlite3",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=3)),
    )
    await backend.open()
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_cache", None)

    async def wrong_dim_embedding(_text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr(memories_handler, "_get_embedding", wrong_dim_embedding)
    content = "semantic degradation needle exact content"
    memory_id = "mem_semantic_degraded"
    try:
        await _insert_memory(backend, memory_id=memory_id, content=content)
        response = await memories_handler.search_memories(
            MemorySearchRequest(query=content, semantic=True, limit=5),
            user=_alice(),
        )
    finally:
        await backend.close()

    assert response.count == 1
    assert response.memories[0].id == memory_id


async def test_sqlite_fts_zero_rows_falls_back_to_exact_content_like(tmp_path):
    backend = SqliteBackend(tmp_path / "stale-fts.sqlite3", SimpleNamespace())
    await backend.open()
    content = "stale fts needle exact content"
    memory_id = "mem_stale_fts"
    try:
        await _insert_memory(backend, memory_id=memory_id, content=content)
        async with backend.transactional() as tx:
            await tx.conn.execute("DELETE FROM memories_fts")
            rows = await backend.memories.fts_search(
                tx,
                query=content,
                limit=5,
                visibility=VisibilityFilter.for_read(
                    _alice(),
                    namespace="alice-ns",
                ),
            )
    finally:
        await backend.close()

    assert [row["id"] for row in rows] == [memory_id]
