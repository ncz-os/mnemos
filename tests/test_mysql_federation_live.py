from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

MYSQL_DSN = os.environ.get("MYSQL_DSN")

pytestmark = [
    pytest.mark.skipif(not MYSQL_DSN, reason="MYSQL_DSN not set; live probe skipped"),
    pytest.mark.asyncio,
]


async def test_mysql_federation_live_roundtrip() -> None:
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

    pool = await create_mysql_pool(MYSQL_DSN)
    backend = MysqlBackend(pool, SimpleNamespace())
    await backend.open()

    run_id = uuid.uuid4().hex[:12]
    peer_name = f"mysql_fed_live_{run_id}"
    owner_id = f"mysql_fed_owner_{run_id}"
    namespace = f"ns_{run_id}"
    remote_id = f"remote_{run_id}"
    local_id = f"fed:{peer_name}:{remote_id}"
    peer_id: str | None = None
    log_id: str | None = None

    try:
        async with backend.transactional() as tx:
            peer = await backend.federation.create_peer(
                tx,
                name=peer_name,
                base_url=f"https://{peer_name}.example.test",
                auth_token=f"token_{run_id}",
                namespace_filter=[namespace],
                category_filter=["live"],
                enabled=True,
                sync_interval_secs=300,
                compat_mode="strict",
            )
            peer_id = str(peer["id"])

            peers = await backend.federation.list_peers(tx)
            fetched_peer = await backend.federation.get_peer(tx, peer_id)
            assert fetched_peer is not None
            assert fetched_peer["name"] == peer_name
            assert fetched_peer["namespace_filter"] == [namespace]
            assert any(row["id"] == peer_id for row in peers)

            log_id = await backend.federation.create_sync_log(tx, peer_id, "cursor-before")
            await backend.federation.finish_sync_log(
                tx,
                log_id=log_id,
                memories_pulled=1,
                memories_new=1,
                memories_updated=0,
                error=None,
                cursor_after="cursor-after",
            )
            sync_log = await backend.federation.fetch_sync_log(tx, peer_id, 5)
            assert any(row["id"] == log_id and row["cursor_after"] == "cursor-after" for row in sync_log)

            inserted = await backend.federation.insert_federated_memory(
                tx,
                local_id=local_id,
                content="live mysql federated memory",
                category="live",
                subcategory=None,
                metadata_json='{"live": true}',
                verbatim_content="live mysql federated memory",
                quality_rating=4,
                namespace=namespace,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                peer_name=peer_name,
                remote_updated=datetime.now(timezone.utc),
            )
            assert inserted is True
            marker = await backend.federation.fetch_federated_memory_marker(tx, local_id)
            assert marker is not None
            assert marker["federation_remote_updated"] is not None

            deleted = await backend.federation.delete_federated_memory(tx, peer_name, remote_id)
            assert deleted == 1
    finally:
        try:
            async with backend.transactional() as tx:
                async with tx.conn.cursor() as cursor:
                    await cursor.execute(
                        "DELETE FROM memories WHERE id = %s OR owner_id = %s OR namespace = %s",
                        (local_id, owner_id, namespace),
                    )
                    if peer_id is not None:
                        await cursor.execute("DELETE FROM federation_sync_log WHERE peer_id = %s", (peer_id,))
                        await cursor.execute("DELETE FROM federation_peers WHERE id = %s", (peer_id,))
                    if log_id is not None:
                        await cursor.execute("DELETE FROM federation_sync_log WHERE id = %s", (log_id,))
                    await cursor.execute("DELETE FROM federation_peers WHERE name = %s", (peer_name,))
        finally:
            await backend.close()


async def test_mysql_federation_feed_include_embedding_preserves_filter_binds() -> None:
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

    pool = await create_mysql_pool(MYSQL_DSN)
    backend = MysqlBackend(pool, SimpleNamespace())
    await backend.open()

    run_id = uuid.uuid4().hex[:12]
    owner_id = f"mysql_fed_feed_owner_{run_id}"
    namespace = f"feed_ns_{run_id}"
    other_namespace = f"feed_other_ns_{run_id}"
    wanted_id = f"feed_wanted_{run_id}"
    other_id = f"feed_other_{run_id}"

    try:
        async with backend.transactional() as tx:
            async with tx.conn.cursor() as cursor:
                for memory_id, memory_namespace, category in (
                    (wanted_id, namespace, "live"),
                    (other_id, other_namespace, "private"),
                ):
                    await cursor.execute(
                        """
                        INSERT INTO memories (
                            id, content, content_hash, category, subcategory, metadata,
                            quality_rating, verbatim_content, owner_id, namespace,
                            permission_mode, created, updated
                        ) VALUES (
                            %s, %s, SHA2(%s, 256), %s, NULL, %s,
                            3, %s, %s, %s,
                            604, CURRENT_TIMESTAMP(6), CURRENT_TIMESTAMP(6)
                        )
                        """,
                        (
                            memory_id,
                            f"live feed content {memory_id}",
                            f"live feed content {memory_id}",
                            category,
                            "{}",
                            f"live feed content {memory_id}",
                            owner_id,
                            memory_namespace,
                        ),
                    )

            rows = await backend.federation.feed_query(
                tx,
                since_updated=datetime(1970, 1, 1, tzinfo=timezone.utc),
                since_id="",
                namespaces=[namespace],
                categories=["live"],
                limit=10,
                prefer_compressed=False,
                include_embedding=True,
            )

            assert [row["id"] for row in rows] == [wanted_id]
            assert rows[0]["embedding_model"]
            assert rows[0]["embedding_model"] not in {namespace, "live", ""}
    finally:
        try:
            async with backend.transactional() as tx:
                async with tx.conn.cursor() as cursor:
                    await cursor.execute(
                        "DELETE FROM memories WHERE id IN (%s, %s) OR owner_id = %s",
                        (wanted_id, other_id, owner_id),
                    )
        finally:
            await backend.close()
