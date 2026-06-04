from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

MYSQL_DSN = os.environ.get("MYSQL_DSN")

pytestmark = [
    pytest.mark.skipif(not MYSQL_DSN, reason="MYSQL_DSN not set; live probe skipped"),
    pytest.mark.asyncio,
]


async def test_mysql_compression_variant_candidate_export_stats_roundtrip() -> None:
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

    pool = await create_mysql_pool(MYSQL_DSN)
    backend = MysqlBackend(pool, SimpleNamespace())
    await backend.open()

    run_id = uuid.uuid4().hex[:12]
    owner_id = f"mysql_compression_live_{run_id}"
    namespace = f"ns_{run_id}"
    memory_id = f"mem_{run_id}"
    candidate_id = uuid.uuid4().hex
    contest_id = uuid.uuid4().hex

    try:
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="This is a live MySQL compression source memory with enough text to compress.",
                category="test",
                subcategory="compression",
                metadata_json='{"live": true}',
                quality_rating=5,
                owner_id=owner_id,
                namespace=namespace,
                permission_mode=600,
                source_model="unit-model",
                source_provider="unit-provider",
                source_session=None,
                source_agent="unit-agent",
                verbatim_content="verbatim live MySQL compression source memory",
                created=None,
                updated=None,
            )

            assert not await backend.compression.compression_candidate_exists(
                tx,
                candidate_id=candidate_id,
                memory_id=memory_id,
                owner_id=owner_id,
            )

            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO memory_compression_candidates (
                        id, memory_id, owner_id, contest_id, engine_id,
                        is_winner, reject_reason
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        FALSE, 'inferior'
                    )
                    """,
                    (candidate_id, memory_id, owner_id, contest_id, "engine"),
                )

            assert await backend.compression.compression_candidate_exists(
                tx,
                candidate_id=candidate_id,
                memory_id=memory_id,
                owner_id=owner_id,
            )

            await backend.compression.insert_compressed_variant(
                tx,
                memory_id=memory_id,
                owner_id=owner_id,
                winner_candidate_id=candidate_id,
                engine_id="engine",
                engine_version="1",
                compressed_content="short mysql compression variant",
                compressed_tokens=4,
                compression_ratio=0.5,
                quality_score=None,
                composite_score=0.8,
                scoring_profile="balanced",
                judge_model="judge",
                selected_at=None,
            )

            row = await backend.compression.fetch_compressed_variant_by_memory_id(tx, memory_id)
            exported = await backend.compression.fetch_compressed_variants_for_export(
                tx,
                memory_ids=[memory_id],
                effective_owner=owner_id,
                hard_limit=10,
            )
            stats = await backend.compression.gather_stats(tx)

            assert row is not None
            assert row["owner_id"] == owner_id
            assert row["winner_candidate_id"] == candidate_id
            assert row["compressed_content"] == "short mysql compression variant"
            assert exported and exported[0]["memory_id"] == memory_id
            assert exported[0]["compressed_content"] == "short mysql compression variant"
            assert stats.total_compressions >= 1
            assert stats.unreviewed_compressions >= 1
    finally:
        try:
            async with backend.transactional() as tx:
                async with tx.conn.cursor() as cursor:
                    await cursor.execute("DELETE FROM memory_compressed_variants WHERE owner_id = %s", (owner_id,))
                    await cursor.execute("DELETE FROM memory_compression_candidates WHERE owner_id = %s", (owner_id,))
                    await cursor.execute(
                        "DELETE FROM memories WHERE owner_id = %s AND namespace = %s", (owner_id, namespace)
                    )
        finally:
            await backend.close()
