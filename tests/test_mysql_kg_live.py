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


async def test_mysql_kg_roundtrip_list_delete() -> None:
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

    pool = await create_mysql_pool(MYSQL_DSN)
    backend = MysqlBackend(pool, SimpleNamespace())
    await backend.open()

    run_id = uuid.uuid4().hex[:12]
    owner_id = f"mysql_kg_live_{run_id}"
    namespace = f"ns_{run_id}"
    memory_id = f"mem_{run_id}"
    triple_id = f"kg_{run_id}"

    try:
        async with backend.transactional() as tx:
            inserted = await backend.kg_triples.insert_kg_triple(
                tx,
                triple_id=triple_id,
                subject="Athena",
                predicate="guides",
                obj="Odysseus",
                subject_type="person",
                object_type="person",
                valid_from=None,
                valid_until=None,
                memory_id=memory_id,
                confidence=0.9,
                created=None,
                owner_id=owner_id,
                namespace=namespace,
            )
            assert inserted == "INSERT 0 1"

            duplicate = await backend.kg_triples.insert_kg_triple(
                tx,
                triple_id=triple_id,
                subject="Athena",
                predicate="guides",
                obj="Odysseus",
                subject_type="person",
                object_type="person",
                valid_from=None,
                valid_until=None,
                memory_id=memory_id,
                confidence=0.9,
                created=None,
                owner_id=owner_id,
                namespace=namespace,
            )
            assert duplicate == "INSERT 0 0"

            fetched = await backend.kg_triples.fetch_kg_triple_by_id(tx, triple_id)
            assert fetched is not None
            assert fetched["subject"] == "Athena"
            assert fetched["predicate"] == "guides"
            assert fetched["object"] == "Odysseus"
            assert fetched["owner_id"] == owner_id
            assert fetched["namespace"] == namespace

            exported = await backend.kg_triples.fetch_kg_triples_for_export(
                tx,
                memory_ids=[memory_id],
                effective_owner=owner_id,
                effective_ns=namespace,
                include_unattached=False,
                hard_limit=10,
            )
            assert [row["id"] for row in exported] == [triple_id]

            listed = await backend.kg_triples.list_kg_triples(
                tx,
                owner_id=owner_id,
                namespace=namespace,
                memory_id=memory_id,
            )
            assert [row["id"] for row in listed] == [triple_id]
            assert listed[0]["subject"] == "Athena"
            assert listed[0]["predicate"] == "guides"
            assert listed[0]["object"] == "Odysseus"
            assert listed[0]["owner_id"] == owner_id
            assert listed[0]["namespace"] == namespace

            deleted = await backend.kg_triples.delete_kg_triple(
                tx,
                triple_id,
                owner_id=owner_id,
                namespace=namespace,
            )
            assert deleted is True

            missing = await backend.kg_triples.fetch_kg_triple_by_id(tx, triple_id)
            assert missing is None

            gone = await backend.kg_triples.list_kg_triples(
                tx,
                owner_id=owner_id,
                namespace=namespace,
                memory_id=memory_id,
            )
            assert gone == []
    finally:
        try:
            async with backend.transactional() as tx:
                async with tx.conn.cursor() as cursor:
                    await cursor.execute("DELETE FROM kg_triples WHERE owner_id = %s", (owner_id,))
        finally:
            await backend.close()
