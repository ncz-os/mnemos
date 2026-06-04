from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
