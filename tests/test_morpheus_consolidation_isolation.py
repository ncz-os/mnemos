import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mnemos.persistence import SqliteBackend
from mnemos.persistence.sqlite import _fetch_all
from tests.test_morpheus_cross_owner_isolation import _seed_memory, _seed_run


@pytest.mark.asyncio
@pytest.mark.parametrize("counts, expected", [((2, 1), 0), ((3, 3), 4)])
async def test_consolidation_partitions_stale_mixed_cluster_and_repeats_idempotently(tmp_path, counts, expected):
    backend = SqliteBackend(tmp_path / "isolation.db", SimpleNamespace())
    await backend.open()
    try:
        run_id = str(uuid4())
        async with backend.transactional() as tx:
            ids = []
            for owner, count in zip(("alice", "bob"), counts):
                for i in range(count):
                    mid = f"{owner}-{i}"
                    ids.append(mid)
                    await _seed_memory(
                        backend,
                        tx,
                        memory_id=mid,
                        owner_id=owner,
                        namespace="shared",
                        content=mid,
                        created=datetime.now(timezone.utc),
                        vector=[1, 0, 0],
                    )
            await _seed_run(tx, run_id, member_ids=ids, cluster_min_size=3, namespace=None)
        for _ in range(2):
            async with backend.transactional() as tx:
                result = await backend.morpheus.phase_consolidate(tx, run_id=run_id, consolidated_permission_mode=400)
                assert result.memories_consolidated == expected
                cross = await _fetch_all(
                    tx.conn,
                    "SELECT m.id FROM memories m JOIN memories c ON c.id=m.consolidated_into "
                    "WHERE m.owner_id != c.owner_id OR m.namespace != c.namespace",
                )
                assert cross == []
    finally:
        await backend.close()


from tests.test_postgres_concurrent_migration_race import fresh_pg_pool  # noqa: E402,F401


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MNEMOS_TEST_DB"), reason="requires disposable PostgreSQL")
@pytest.mark.parametrize("counts, expected", [((2, 1), 0), ((3, 3), 4)])
async def test_postgres_consolidation_partitions_mixed_cluster(fresh_pg_pool, counts, expected):  # noqa: F811
    from mnemos.persistence.postgres import PostgresBackend
    from mnemos.persistence.schema import ensure_postgres_schema

    pool, _ = fresh_pg_pool
    await ensure_postgres_schema(pool)
    backend = PostgresBackend(pool, SimpleNamespace())
    async with backend.transactional() as tx:
        members = []
        for owner, count in zip(("alice", "bob"), counts):
            for index in range(count):
                mid = f"{owner}-{index}"
                members.append(mid)
                await tx.conn.execute(
                    """INSERT INTO memories
                    (id,content,category,owner_id,namespace,permission_mode,created,recall_count)
                    VALUES ($1,$1,'facts',$2,'shared',600,NOW(),0)""",
                    mid,
                    owner,
                )
        run_id = await backend.morpheus.begin_run(
            tx,
            triggered_by="api",
            window_hours=24,
            cluster_min_size=3,
            config={"clusters": [{"cluster_id": 0, "member_memory_ids": members}]},
            namespace=None,
        )
    for _ in range(2):
        async with backend.transactional() as tx:
            result = await backend.morpheus.phase_consolidate(tx, run_id=run_id, consolidated_permission_mode=400)
            assert result.memories_consolidated == expected
            cross = await tx.conn.fetch("""SELECT m.id FROM memories m JOIN memories c ON c.id=m.consolidated_into
                WHERE m.owner_id IS DISTINCT FROM c.owner_id OR m.namespace IS DISTINCT FROM c.namespace""")
            assert cross == []
