from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace

import pytest

from mnemos.persistence import SqliteBackend
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


async def _insert_memory(
    backend: SqliteBackend,
    tx,
    *,
    memory_id: str,
    content: str,
    updated: datetime,
) -> None:
    await backend.memories.insert_memory(
        tx,
        memory_id=memory_id,
        content=content,
        category="solutions",
        subcategory=None,
        metadata_json='{"source":"sqlite-recency-test"}',
        quality_rating=75,
        owner_id="sqlite-recency-owner",
        namespace="default",
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content=content,
        created=updated,
        updated=updated,
    )


def _embedding_with_similarity(similarity: float) -> list[float]:
    return [similarity, math.sqrt(1.0 - similarity * similarity), 0.0]


@pytest.mark.asyncio
async def test_sqlite_semantic_search_boosts_recent_candidates(tmp_path):
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "recency.sqlite3", settings)
    await backend.open()
    visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )
    now = datetime.now(timezone.utc)
    old = "sqlite-recency-old"
    newer = "sqlite-recency-newer"
    fillers = [f"sqlite-recency-filler-{idx}" for idx in range(3)]

    try:
        async with backend.transactional() as tx:
            await _insert_memory(
                backend,
                tx,
                memory_id=old,
                content="old exact match",
                updated=now - timedelta(days=45),
            )
            await _insert_memory(
                backend,
                tx,
                memory_id=newer,
                content="newer near match",
                updated=now,
            )
            for idx, memory_id in enumerate(fillers):
                await _insert_memory(
                    backend,
                    tx,
                    memory_id=memory_id,
                    content=f"filler {idx}",
                    updated=now - timedelta(days=idx),
                )

            await backend.memories.upsert_memory_embedding(tx, old, [1.0, 0.0, 0.0])
            await backend.memories.upsert_memory_embedding(tx, newer, [0.995, 0.1, 0.0])
            await backend.memories.upsert_memory_embedding(tx, fillers[0], [0.8, 0.6, 0.0])
            await backend.memories.upsert_memory_embedding(tx, fillers[1], [0.7, 0.7, 0.0])
            await backend.memories.upsert_memory_embedding(tx, fillers[2], [0.6, 0.8, 0.0])

            unboosted = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=2,
                visibility=visibility,
                boost_recency=False,
            )
            boosted = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=2,
                visibility=visibility,
                boost_recency=True,
                recency_weight=0.2,
            )
    finally:
        await backend.close()

    assert len(unboosted) <= 2
    assert len(boosted) <= 2
    assert unboosted[0]["id"] == old
    assert boosted[0]["id"] == newer
    assert [row["id"] for row in boosted].index(newer) < [row["id"] for row in boosted].index(old)


@pytest.mark.asyncio
async def test_sqlite_semantic_search_invalid_updated_falls_back_to_created(tmp_path):
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "recency-invalid-updated.sqlite3", settings)
    await backend.open()
    visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )
    now = datetime.now(timezone.utc)
    corrupt = "sqlite-recency-corrupt-updated"
    fresh = "sqlite-recency-valid-fresh"

    try:
        async with backend.transactional() as tx:
            await _insert_memory(
                backend,
                tx,
                memory_id=corrupt,
                content="corrupt timestamp raw winner",
                updated=now - timedelta(days=365),
            )
            await _insert_memory(
                backend,
                tx,
                memory_id=fresh,
                content="fresh valid boosted winner",
                updated=now,
            )
            await tx.conn.execute(
                "UPDATE memories SET updated = ? WHERE id = ?",
                ("not-a-date", corrupt),
            )
            await backend.memories.upsert_memory_embedding(tx, corrupt, [1.0, 0.0, 0.0])
            await backend.memories.upsert_memory_embedding(
                tx,
                fresh,
                _embedding_with_similarity(0.9),
            )

            boosted = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=2,
                visibility=visibility,
                boost_recency=True,
                recency_weight=0.2,
            )
    finally:
        await backend.close()

    assert [row["id"] for row in boosted] == [fresh, corrupt]


@pytest.mark.asyncio
async def test_sqlite_semantic_search_recency_reranks_only_candidate_window(tmp_path):
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=3))
    backend = SqliteBackend(tmp_path / "recency-candidate-window.sqlite3", settings)
    await backend.open()
    visibility = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
    )
    now = datetime.now(timezone.utc)
    stale_ids = [f"sqlite-recency-window-stale-{idx}" for idx in range(4)]
    fresh = "sqlite-recency-window-fresh-beyond-bound"

    try:
        async with backend.transactional() as tx:
            for memory_id in stale_ids:
                await _insert_memory(
                    backend,
                    tx,
                    memory_id=memory_id,
                    content=f"{memory_id} raw candidate",
                    updated=now - timedelta(days=365),
                )
            await _insert_memory(
                backend,
                tx,
                memory_id=fresh,
                content="fresh candidate beyond bounded window",
                updated=now,
            )

            for memory_id, similarity in zip(stale_ids, (1.0, 0.99, 0.98, 0.97), strict=True):
                await backend.memories.upsert_memory_embedding(
                    tx,
                    memory_id,
                    _embedding_with_similarity(similarity),
                )
            await backend.memories.upsert_memory_embedding(
                tx,
                fresh,
                _embedding_with_similarity(0.96),
            )

            boosted = await backend.memories.semantic_search(
                tx,
                embedding=[1.0, 0.0, 0.0],
                limit=1,
                visibility=visibility,
                boost_recency=True,
                recency_weight=1.0,
            )
    finally:
        await backend.close()

    assert [row["id"] for row in boosted] == [stale_ids[0]]
