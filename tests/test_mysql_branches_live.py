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


async def test_mysql_memory_branches_roundtrip_head_update_delete() -> None:
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

    pool = await create_mysql_pool(MYSQL_DSN)
    backend = MysqlBackend(pool, SimpleNamespace())
    await backend.open()

    run_id = uuid.uuid4().hex[:12]
    owner_id = f"mysql_branches_live_{run_id}"
    namespace = f"ns_{run_id}"
    memory_id = f"mem_{run_id}"
    version_1 = uuid.uuid4().hex
    version_2 = uuid.uuid4().hex
    commit_1 = uuid.uuid4().hex
    commit_2 = uuid.uuid4().hex
    user = SimpleNamespace(user_id=owner_id, namespace=namespace, role="user")

    try:
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="branch content v1",
                category="test",
                subcategory="branches",
                metadata_json='{"live": true}',
                quality_rating=5,
                owner_id=owner_id,
                namespace=namespace,
                permission_mode=600,
                source_model="unit-model",
                source_provider="unit-provider",
                source_session=None,
                source_agent="unit-agent",
                verbatim_content="verbatim branch content v1",
                created=None,
                updated=None,
            )
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_1,
                memory_id=memory_id,
                version_num=1,
                content="branch content v1",
                category="test",
                subcategory="branches",
                metadata_json='{"live": true}',
                verbatim_content="verbatim branch content v1",
                owner_id=owner_id,
                namespace=namespace,
                permission_mode=600,
                source_model="unit-model",
                source_provider="unit-provider",
                source_session=None,
                source_agent="unit-agent",
                snapshot_at=None,
                snapshot_by=owner_id,
                change_type="create",
                commit_hash=commit_1,
                parent_version_id=None,
                branch="main",
                merge_parents=[],
            )
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_2,
                memory_id=memory_id,
                version_num=2,
                content="branch content v2",
                category="test",
                subcategory="branches",
                metadata_json='{"live": true}',
                verbatim_content="verbatim branch content v2",
                owner_id=owner_id,
                namespace=namespace,
                permission_mode=600,
                source_model="unit-model",
                source_provider="unit-provider",
                source_session=None,
                source_agent="unit-agent",
                snapshot_at=None,
                snapshot_by=owner_id,
                change_type="update",
                commit_hash=commit_2,
                parent_version_id=version_1,
                branch="feature",
                merge_parents=[],
            )

            await backend.memory_branches.upsert_memory_branch_head(
                tx,
                memory_id=memory_id,
                branch="main",
                head_version_id=version_1,
            )

            created = await backend.memory_branches.create_memory_branch(
                tx,
                memory_id,
                "feature",
                commit_1,
                user,
            )
            assert created["success"] is True
            assert created["memory_id"] == memory_id
            assert created["branch"] == "feature"
            assert created["commit_hash"] == commit_1
            assert created["created_by"] == owner_id

            again = await backend.memory_branches.create_memory_branch(
                tx,
                memory_id,
                "feature",
                commit_1,
                user,
            )
            assert again["success"] is True
            assert again["commit_hash"] == commit_1

            await backend.memory_branches.upsert_memory_branch_head(
                tx,
                memory_id=memory_id,
                branch="feature",
                head_version_id=version_2,
            )

            heads = await backend.memory_branches.fetch_memory_branch_heads(tx, [memory_id])
            by_branch = {row["branch"]: row["head_version_id"] for row in heads}
            assert by_branch["main"] == version_1
            assert by_branch["feature"] == version_2

            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT mb.memory_id, mb.name, mb.head_version_id, mv.commit_hash
                      FROM memory_branches mb
                      LEFT JOIN memory_versions mv
                             ON mv.id = mb.head_version_id
                     WHERE mb.memory_id = %s
                       AND mb.name = %s
                    """,
                    (memory_id, "feature"),
                )
                row = await cursor.fetchone()
                assert row == (memory_id, "feature", version_2, commit_2)

            await backend.memory_branches.delete_memory_branches_for_memories(tx, [memory_id])
            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT COUNT(*) FROM memory_branches WHERE memory_id = %s",
                    (memory_id,),
                )
                count_row = await cursor.fetchone()
                assert count_row[0] == 0
    finally:
        try:
            async with backend.transactional() as tx:
                async with tx.conn.cursor() as cursor:
                    await cursor.execute("DELETE FROM memory_branches WHERE memory_id = %s", (memory_id,))
                    await cursor.execute("DELETE FROM memory_versions WHERE owner_id = %s", (owner_id,))
                    await cursor.execute("DELETE FROM memories WHERE owner_id = %s", (owner_id,))
        finally:
            await backend.close()
