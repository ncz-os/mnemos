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


async def test_mysql_state_roundtrip_update_list_delete() -> None:
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

    pool = await create_mysql_pool(MYSQL_DSN)
    backend = MysqlBackend(pool, SimpleNamespace())
    await backend.open()

    run_id = uuid.uuid4().hex[:12]
    owner_id = f"mysql_state_live_{run_id}"
    namespace = f"ns_{run_id}"
    key = f"k_{run_id}"

    try:
        async with backend.transactional() as tx:
            first = await backend.state_kv.set(tx, key, "v1", owner_id=owner_id, namespace=namespace)
            assert first is not None
            assert first["value"] == "v1"
            assert int(first["version"]) == 1

            got = await backend.state_kv.get(tx, key, owner_id=owner_id, namespace=namespace)
            assert got is not None
            assert got["value"] == "v1"
            assert int(got["version"]) == 1

            second = await backend.state_kv.set(tx, key, "v2", owner_id=owner_id, namespace=namespace)
            assert second is not None
            assert second["value"] == "v2"
            assert int(second["version"]) == int(first["version"]) + 1

            listed = await backend.state_kv.list_namespace(tx, owner_id=owner_id, namespace=namespace)
            assert key in {row["key"] for row in listed}

            deleted = await backend.state_kv.delete(tx, key, owner_id=owner_id, namespace=namespace)
            assert deleted is True

            missing = await backend.state_kv.get(tx, key, owner_id=owner_id, namespace=namespace)
            assert missing is None

            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT deleted_at
                      FROM state
                     WHERE owner_id = %s
                       AND namespace = %s
                       AND `key` = %s
                    """,
                    (owner_id, namespace, key),
                )
                tombstone = await cursor.fetchone()
            assert tombstone is not None
            assert tombstone[0] is not None
    finally:
        try:
            async with backend.transactional() as tx:
                async with tx.conn.cursor() as cursor:
                    await cursor.execute("DELETE FROM state WHERE owner_id = %s", (owner_id,))
        finally:
            await backend.close()
