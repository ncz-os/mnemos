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


async def test_mysql_memory_versions_roundtrip_list_get_soft_delete() -> None:
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

    pool = await create_mysql_pool(MYSQL_DSN)
    backend = MysqlBackend(pool, SimpleNamespace())
    await backend.open()

    run_id = uuid.uuid4().hex[:12]
    owner_id = f"mysql_versions_live_{run_id}"
    namespace = f"ns_{run_id}"
    memory_id = f"mem_{run_id}"
    version_id = uuid.uuid4().hex
    commit_hash = uuid.uuid4().hex

    try:
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="versioned content",
                category="test",
                subcategory="versions",
                metadata_json='{"live": true}',
                quality_rating=5,
                owner_id=owner_id,
                namespace=namespace,
                permission_mode=600,
                source_model="unit-model",
                source_provider="unit-provider",
                source_session=None,
                source_agent="unit-agent",
                verbatim_content="verbatim versioned content",
                created=None,
                updated=None,
            )

            status = await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id,
                memory_id=memory_id,
                version_num=1,
                content="versioned content",
                category="test",
                subcategory="versions",
                metadata_json='{"live": true}',
                verbatim_content="verbatim versioned content",
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
                commit_hash=commit_hash,
                parent_version_id=None,
                branch="main",
                merge_parents=[],
            )
            assert status == "INSERT 0 1"

            listed = await backend.memory_versions.fetch_memory_versions_for_export(
                tx,
                memory_ids=[memory_id],
                effective_owner=owner_id,
                effective_ns=namespace,
                hard_limit=10,
            )
            assert [row["id"] for row in listed] == [version_id]
            assert listed[0]["memory_id"] == memory_id
            assert int(listed[0]["version_num"]) == 1
            assert listed[0]["owner_id"] == owner_id
            assert listed[0]["namespace"] == namespace
            assert listed[0]["commit_hash"] == commit_hash

            ids = await backend.memory_versions.fetch_memory_versions_by_ids(tx, [version_id])
            assert ids == [
                {
                    "id": version_id,
                    "memory_id": memory_id,
                    "owner_id": owner_id,
                    "namespace": namespace,
                }
            ]

            got = await backend.memory_versions.fetch_memory_version_by_id(tx, version_id)
            assert got is not None
            assert got["memory_id"] == memory_id
            assert got["owner_id"] == owner_id
            assert got["namespace"] == namespace
            assert int(got["version_num"]) == 1
            assert got["content"] == "versioned content"
            assert got["commit_hash"] == commit_hash
            assert got["branch"] == "main"
            assert got["change_type"] == "create"

            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE memory_versions
                       SET deleted_at = CURRENT_TIMESTAMP(6)
                     WHERE id = %s
                       AND owner_id = %s
                       AND namespace = %s
                    """,
                    (version_id, owner_id, namespace),
                )
            assert await backend.memory_versions.fetch_memory_version_by_id(tx, version_id) is None
            assert await backend.memory_versions.fetch_memory_versions_by_ids(tx, [version_id]) == []
    finally:
        try:
            async with backend.transactional() as tx:
                async with tx.conn.cursor() as cursor:
                    await cursor.execute("DELETE FROM memory_versions WHERE owner_id = %s", (owner_id,))
                    await cursor.execute("DELETE FROM memories WHERE owner_id = %s", (owner_id,))
        finally:
            await backend.close()
