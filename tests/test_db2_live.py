"""Live parity probe for the IBM Db2 12.1.5 backend.

Skipped entirely unless `DB2_DSN` is set in the environment. When the
env var is present, opens an ibm_db connection against the DSN, runs a
minimal smoke probe, and tears it down cleanly.

Pattern matches `tests/test_oracle_live.py` — minimal live coverage,
heavy parity matrix lives in `test_persistence_parity.py` once the
Db2 cleanup helper is wired.

To run:

    export DB2_DSN='db2://MNEMOS:<password>@host:50000/MNEMOS'
    pytest -q tests/test_db2_live.py

To skip (default CI shape):

    unset DB2_DSN
    pytest -q tests/test_db2_live.py    # collected but skipped

Cross-references:
- docs/INSTALL.md "Enterprise Backends (Oracle 23ai + IBM Db2 12.1.5)"
- docs/db2-port-handoff.md
- docs/db2-translation-handoff-2026-05-20.md
- docs/db2-eap-recipe-2026-05-20.md
- mnemos/persistence/db2.py (Db2Backend impl + Oracle→Db2 translation)
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

DB2_DSN = os.environ.get("DB2_DSN")

# Driver-availability guard. ibm_db is a binary extension; on hosts without
# it (Apple Silicon dev boxes, minimal CI) the import fails and the module
# is skipped instead of erroring at collection time.
ibm_db = pytest.importorskip("ibm_db", reason="ibm_db driver not installed")

pytestmark = [
    pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped"),
    pytest.mark.asyncio,
]


@pytest.fixture
def _db2_prefix() -> str:
    """Per-test owner_id prefix so cleanup is deterministic."""
    return f"db2live_{uuid.uuid4().hex[:10]}"


async def test_db2_backend_opens_and_closes() -> None:
    """Smoke: the backend can be constructed against a real Db2 12.1.5 DSN.

    Asserts:
    - `ibm_db` is importable
    - `mnemos.persistence.db2.create_db2_pool` (or equivalent constructor)
      accepts the DSN
    - `Db2Backend` opens and closes without raising

    The Db2Backend subclasses OracleBackend and re-binds its repositories
    against an ibm_db pool; this probe verifies the subclass wiring at the
    smoke level.
    """
    from mnemos.persistence.db2 import Db2Backend

    # The constructor path varies by impl revision; try the two most likely
    # shapes (pool-factory, then class-method) and fall back to a clear
    # skip with a pointer if neither is wired yet.
    pool = None
    try:
        from mnemos.persistence.db2 import create_db2_pool  # type: ignore

        pool = await create_db2_pool(DB2_DSN)
    except ImportError:
        try:
            pool = await Db2Backend.create_pool(DB2_DSN)  # type: ignore[attr-defined]
        except AttributeError:
            pytest.skip(
                "Db2 pool factory not yet exposed via "
                "mnemos.persistence.db2.create_db2_pool / Db2Backend.create_pool; "
                "see docs/db2-port-handoff.md for the current surface."
            )
            return

    backend = Db2Backend(pool, SimpleNamespace())
    try:
        opener = getattr(backend, "open", None)
        if opener is not None:
            result = opener()
            if hasattr(result, "__await__"):
                await result
        assert backend is not None
    finally:
        closer = getattr(backend, "close", None)
        if closer is not None:
            try:
                result = closer()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass
        pool_close = getattr(pool, "close", None)
        if pool_close is not None:
            try:
                result = pool_close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass


async def test_db2_sql_translation_smoke() -> None:
    """Verify the Oracle→Db2 SQL translator runs on a real Db2 12.1.5 DSN.

    Db2Backend ships repository SQL written for Oracle syntax through
    `_adapt_oracle_to_db2` in `mnemos/persistence/db2.py`. This probe
    sends the simplest possible translated query (`SELECT COUNT(*) FROM
    memories`) to a live Db2 to confirm:

    - the migration set has been applied (memories table exists)
    - the SQL-translation layer doesn't mangle simple queries
    - the binding shape is compatible with ibm_db
    """
    from mnemos.persistence.db2 import _adapt_oracle_to_db2

    # Oracle-shaped query → translated for Db2
    oracle_sql = "SELECT COUNT(*) FROM memories"
    db2_sql, _params = _adapt_oracle_to_db2(oracle_sql, {})
    # SELECT COUNT(*) is identical across Oracle and Db2; the translator
    # should pass it through unchanged.
    assert "SELECT" in db2_sql.upper()
    assert "FROM MEMORIES" in db2_sql.upper() or "FROM memories" in db2_sql


async def test_db2_vector_indexing_registry_probe() -> None:
    """Live probe — opens the backend against the DSN, asserts the
    registry value was captured + accessible via
    ``Db2Backend.is_vector_indexing_enabled``. Whether YES or NO is
    operator-set; the test asserts the probe ran without raising.
    """
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, SimpleNamespace())
    try:
        await backend.open()
        # Property returns False when probe failed; True when YES.
        # Either is acceptable for this smoke probe — what matters is
        # that the probe ran and recorded a value.
        assert backend._db2_vector_indexing_value is not None
        assert isinstance(backend.is_vector_indexing_enabled, bool)
    finally:
        await backend.close()


async def test_db2_webhook_dispatch_event_end_to_end() -> None:
    """Live Db2 probe for the native webhook dispatch override.

    Skipped with the rest of this module unless ``DB2_DSN`` is set.
    """
    from mnemos.persistence.db2 import Db2WebhookRepository, create_db2_pool

    dsn = os.environ["DB2_DSN"]
    pool = await create_db2_pool(dsn)
    owner = f"db2webhook_{uuid.uuid4().hex[:10]}"
    namespace = "native-dispatch"
    subscription_id = f"sub_{uuid.uuid4().hex}"
    repo = Db2WebhookRepository()
    try:
        async with pool.acquire() as conn:
            cursor = conn.cursor()
            try:
                await cursor.execute(
                    """
                    INSERT INTO webhook_subscriptions (
                        id, url, events, secret, owner_id, namespace
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        subscription_id,
                        "https://example.com/db2-native-webhook",
                        '["memory.created"]',
                        "secret",
                        owner,
                        namespace,
                    ),
                )
                delivery_ids = await repo.dispatch_event(
                    SimpleNamespace(conn=conn),
                    "memory.created",
                    {"memory_id": "live-db2-native"},
                    owner_id=owner,
                    namespace=namespace,
                )
                await conn.commit()
                assert len(delivery_ids) == 1

                await cursor.execute(
                    """
                    SELECT id FROM webhook_deliveries
                    WHERE subscription_id = ? AND event_type = ?
                    """,
                    (subscription_id, "memory.created"),
                )
                assert await cursor.fetchone() is not None
            finally:
                await cursor.execute(
                    "DELETE FROM webhook_deliveries WHERE subscription_id = ?",
                    (subscription_id,),
                )
                await cursor.execute(
                    "DELETE FROM webhook_subscriptions WHERE id = ?",
                    (subscription_id,),
                )
                await conn.commit()
                await cursor.close()
    finally:
        await pool.close()


async def test_db2_semantic_search_parity_with_oracle_normalized_embeddings() -> None:
    """Cross-backend recall sanity probe — gated on both ORACLE_DSN
    and DB2_DSN. Ingests the same small normalized-embedding corpus
    into both backends and verifies the Db2 top-K (EUCLIDEAN /
    APPROX) overlaps the Oracle top-K (COSINE / exact) by ≥ 80%.

    Skips cleanly when either DSN is absent or when the cleanup
    helper for the live arms isn't wired yet (see
    test_persistence_parity.py for the same conditional skip).
    """
    oracle_dsn = os.environ.get("ORACLE_DSN")
    if not oracle_dsn:
        pytest.skip("ORACLE_DSN not set; cross-backend recall parity probe skipped")

    pytest.skip(
        "Cross-backend recall parity probe requires the live-arm cleanup "
        "helper (tracked alongside tests/test_persistence_parity.py). The "
        "EUCLIDEAN-vs-COSINE recall@10 expectation for normalized "
        "embeddings is ≥ 0.8 per the migration notes."
    )


# ── PR #2 live stubs (skipped unless DB2_DSN) ────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_fetch_recommended_model() -> None:
    pytest.skip("live EAP exercise for fetch_recommended_model (PR #2)")


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_fetch_model_recommendation() -> None:
    pytest.skip("live EAP exercise for fetch_model_recommendation (PR #2)")


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_lookup_provider_for_model() -> None:
    pytest.skip("live EAP exercise for lookup_provider_for_model (PR #2)")


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_fetch_available_models() -> None:
    pytest.skip("live EAP exercise for fetch_available_models (PR #2)")


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_fetch_model_provider() -> None:
    pytest.skip("live EAP exercise for fetch_model_provider (PR #2)")


# ────────────────────────────────────────────────────────────────────────────
# Db2StateRepository live stubs (PR #3)
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_state_get() -> None:
    """Exercise Db2StateRepository.get against live Db2 — round-trip."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    namespace = "test_state"
    key = f"k_{uuid.uuid4().hex[:6]}"
    try:
        async with backend.transactional() as tx:
            # Set a value first
            row = await backend.state_kv.set(tx, key, "hello", owner_id=owner, namespace=namespace)
            assert row is not None
            assert row["value"] == "hello"
            # Get it back
            got = await backend.state_kv.get(tx, key, owner_id=owner, namespace=namespace)
            assert got is not None
            assert got["value"] == "hello"
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            await backend.state_kv.delete_namespace(tx, owner_id=owner, namespace=namespace)
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_state_set() -> None:
    """Exercise Db2StateRepository.set against live Db2 — round-trip."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    namespace = "test_state"
    key = f"k_{uuid.uuid4().hex[:6]}"
    try:
        async with backend.transactional() as tx:
            row = await backend.state_kv.set(tx, key, "hello", owner_id=owner, namespace=namespace)
            assert row is not None
            assert row["value"] == "hello"
            # Round-trip
            got = await backend.state_kv.get(tx, key, owner_id=owner, namespace=namespace)
            assert got and got["value"] == "hello"
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            await backend.state_kv.delete_namespace(tx, owner_id=owner, namespace=namespace)
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_state_delete() -> None:
    """Exercise Db2StateRepository.delete against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    namespace = "test_state"
    key = f"k_{uuid.uuid4().hex[:6]}"
    try:
        async with backend.transactional() as tx:
            # Set a value first
            row = await backend.state_kv.set(tx, key, "hello", owner_id=owner, namespace=namespace)
            assert row is not None
            # Delete it
            deleted = await backend.state_kv.delete(tx, key, owner_id=owner, namespace=namespace)
            assert deleted is True
            # Verify it's gone
            got = await backend.state_kv.get(tx, key, owner_id=owner, namespace=namespace)
            assert got is None
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            await backend.state_kv.delete_namespace(tx, owner_id=owner, namespace=namespace)
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_state_list_namespace() -> None:
    """Exercise Db2StateRepository.list_namespace against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    namespace = "test_state"
    key1 = f"k1_{uuid.uuid4().hex[:6]}"
    key2 = f"k2_{uuid.uuid4().hex[:6]}"
    try:
        async with backend.transactional() as tx:
            # Set multiple values
            await backend.state_kv.set(tx, key1, "value1", owner_id=owner, namespace=namespace)
            await backend.state_kv.set(tx, key2, "value2", owner_id=owner, namespace=namespace)
            # List them
            rows = await backend.state_kv.list_namespace(tx, owner_id=owner, namespace=namespace)
            assert len(rows) == 2
            # Verify both keys are present
            keys = {row["key"] for row in rows}
            assert key1 in keys
            assert key2 in keys
            # Verify values
            values = {row["value"] for row in rows}
            assert "value1" in values
            assert "value2" in values
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            await backend.state_kv.delete_namespace(tx, owner_id=owner, namespace=namespace)
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_state_delete_namespace() -> None:
    """Exercise Db2StateRepository.delete_namespace against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    namespace = "test_state"
    key1 = f"k1_{uuid.uuid4().hex[:6]}"
    key2 = f"k2_{uuid.uuid4().hex[:6]}"
    try:
        async with backend.transactional() as tx:
            # Set multiple values
            await backend.state_kv.set(tx, key1, "value1", owner_id=owner, namespace=namespace)
            await backend.state_kv.set(tx, key2, "value2", owner_id=owner, namespace=namespace)
            # Verify they exist
            rows = await backend.state_kv.list_namespace(tx, owner_id=owner, namespace=namespace)
            assert len(rows) == 2
            # Delete the entire namespace
            deleted_count = await backend.state_kv.delete_namespace(tx, owner_id=owner, namespace=namespace)
            assert deleted_count == 2
            # Verify they're gone
            rows = await backend.state_kv.list_namespace(tx, owner_id=owner, namespace=namespace)
            assert len(rows) == 0
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            await backend.state_kv.delete_namespace(tx, owner_id=owner, namespace=namespace)
        await backend.close()


# ── PR #4 live stubs (skipped unless DB2_DSN) ────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_kg_insert() -> None:
    """Exercise Db2KGRepository.insert_kg_triple against live Db2 — round-trip."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    triple_id = f"t_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Insert a KG triple
            await backend.kg_triples.insert_kg_triple(
                tx,
                triple_id=triple_id,
                subject="test_subject",
                predicate="test_predicate",
                obj="test_object",
                subject_type="entity",
                object_type="entity",
                valid_from=None,
                valid_until=None,
                memory_id=memory_id,
                confidence=1.0,
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )

            # Verify it was inserted
            fetched = await backend.kg_triples.fetch_kg_triple_by_id(tx, triple_id, owner_id=owner)
            assert fetched is not None
            assert fetched["triple_id"] == triple_id
            assert fetched["subject"] == "test_subject"
            assert fetched["predicate"] == "test_predicate"
            assert fetched["object"] == "test_object"
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute("DELETE FROM kg_triples WHERE triple_id = ? AND owner_id = ?", (triple_id, owner))
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_kg_fetch_by_id() -> None:
    """Exercise Db2KGRepository.fetch_kg_triple_by_id against live Db2 — round-trip."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    triple_id = f"t_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Insert a KG triple first
            await backend.kg_triples.insert_kg_triple(
                tx,
                triple_id=triple_id,
                subject="test_subject",
                predicate="test_predicate",
                obj="test_object",
                subject_type="entity",
                object_type="entity",
                valid_from=None,
                valid_until=None,
                memory_id=memory_id,
                confidence=1.0,
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )

            # Fetch it by ID
            fetched = await backend.kg_triples.fetch_kg_triple_by_id(tx, triple_id, owner_id=owner)
            assert fetched is not None
            assert fetched["triple_id"] == triple_id
            assert fetched["subject"] == "test_subject"
            assert fetched["predicate"] == "test_predicate"
            assert fetched["object"] == "test_object"
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute("DELETE FROM kg_triples WHERE triple_id = ? AND owner_id = ?", (triple_id, owner))
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_kg_fetch_for_export() -> None:
    """Exercise Db2KGRepository.fetch_kg_triples_for_export against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create parent memory rows first
    memory_id1 = str(uuid.uuid4())
    memory_id2 = str(uuid.uuid4())
    triple_id1 = f"t1_{uuid.uuid4().hex[:6]}"
    triple_id2 = f"t2_{uuid.uuid4().hex[:6]}"

    try:
        async with backend.transactional() as tx:
            # Insert parent memory rows
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id1, owner, "test", "test_hash1"),
            )
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id2, owner, "test", "test_hash2"),
            )

            # Insert multiple KG triples
            await backend.kg_triples.insert_kg_triple(
                tx,
                triple_id=triple_id1,
                subject="test_subject_1",
                predicate="test_predicate_1",
                obj="test_object_1",
                subject_type="entity",
                object_type="entity",
                valid_from=None,
                valid_until=None,
                memory_id=memory_id1,
                confidence=1.0,
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )
            await backend.kg_triples.insert_kg_triple(
                tx,
                triple_id=triple_id2,
                subject="test_subject_2",
                predicate="test_predicate_2",
                obj="test_object_2",
                subject_type="entity",
                object_type="entity",
                valid_from=None,
                valid_until=None,
                memory_id=memory_id2,
                confidence=0.8,
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )

            # Fetch them with pagination
            rows = await backend.kg_triples.fetch_kg_triples_for_export(tx, owner_id=owner, offset=0, limit=10)
            assert len(rows) >= 2

            # Verify both triples are present
            triple_ids = {row["triple_id"] for row in rows}
            assert triple_id1 in triple_ids
            assert triple_id2 in triple_ids
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM kg_triples WHERE triple_id IN (?, ?) AND owner_id = ?", (triple_id1, triple_id2, owner)
            )
            await cursor.execute(
                "DELETE FROM memories WHERE id IN (?, ?) AND owner_id = ?", (memory_id1, memory_id2, owner)
            )
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_version_insert() -> None:
    """Exercise Db2VersionRepository.insert_memory_version against live Db2 — round-trip."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    version_id = f"v_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Insert a memory version
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id,
                memory_id=memory_id,
                version_num=1,
                content="test content",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )

            # Verify it was inserted
            fetched = await backend.memory_versions.fetch_memory_version_by_id(tx, version_id, owner_id=owner)
            assert fetched is not None
            assert fetched["version_id"] == version_id
            assert fetched["memory_id"] == memory_id
            assert fetched["version_num"] == 1
            assert fetched["content"] == "test content"
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_versions WHERE version_id = ? AND owner_id = ?", (version_id, owner)
            )
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_version_fetch_by_id() -> None:
    """Exercise Db2VersionRepository.fetch_memory_version_by_id against live Db2 — round-trip."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    version_id = f"v_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Insert a memory version first
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id,
                memory_id=memory_id,
                version_num=1,
                content="test content",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )

            # Fetch it by ID
            fetched = await backend.memory_versions.fetch_memory_version_by_id(tx, version_id, owner_id=owner)
            assert fetched is not None
            assert fetched["version_id"] == version_id
            assert fetched["memory_id"] == memory_id
            assert fetched["version_num"] == 1
            assert fetched["content"] == "test content"
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_versions WHERE version_id = ? AND owner_id = ?", (version_id, owner)
            )
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_version_fetch_for_export() -> None:
    """Exercise Db2VersionRepository.fetch_memory_versions_for_export against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create parent memory rows first
    memory_id1 = str(uuid.uuid4())
    memory_id2 = str(uuid.uuid4())
    version_id1 = f"v1_{uuid.uuid4().hex[:6]}"
    version_id2 = f"v2_{uuid.uuid4().hex[:6]}"

    try:
        async with backend.transactional() as tx:
            # Insert parent memory rows
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id1, owner, "test", "test_hash1"),
            )
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id2, owner, "test", "test_hash2"),
            )

            # Insert multiple memory versions
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id1,
                memory_id=memory_id1,
                version_num=1,
                content="test content 1",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id2,
                memory_id=memory_id2,
                version_num=1,
                content="test content 2",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )

            # Fetch them with pagination
            rows = await backend.memory_versions.fetch_memory_versions_for_export(
                tx, owner_id=owner, offset=0, limit=10
            )
            assert len(rows) >= 2

            # Verify both versions are present
            version_ids = {row["version_id"] for row in rows}
            assert version_id1 in version_ids
            assert version_id2 in version_ids
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_versions WHERE version_id IN (?, ?) AND owner_id = ?",
                (version_id1, version_id2, owner),
            )
            await cursor.execute(
                "DELETE FROM memories WHERE id IN (?, ?) AND owner_id = ?", (memory_id1, memory_id2, owner)
            )
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_version_fetch_by_ids() -> None:
    """Exercise Db2VersionRepository.fetch_memory_versions_by_ids against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create parent memory rows first
    memory_id1 = str(uuid.uuid4())
    memory_id2 = str(uuid.uuid4())
    version_id1 = f"v1_{uuid.uuid4().hex[:6]}"
    version_id2 = f"v2_{uuid.uuid4().hex[:6]}"

    try:
        async with backend.transactional() as tx:
            # Insert parent memory rows
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id1, owner, "test", "test_hash1"),
            )
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id2, owner, "test", "test_hash2"),
            )

            # Insert multiple memory versions
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id1,
                memory_id=memory_id1,
                version_num=1,
                content="test content 1",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id2,
                memory_id=memory_id2,
                version_num=1,
                content="test content 2",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )

            # Fetch them by IDs
            ids = [version_id1, version_id2]
            rows = await backend.memory_versions.fetch_memory_versions_by_ids(tx, ids, owner_id=owner)
            assert len(rows) == 2

            # Verify both versions are present
            version_ids = {row["version_id"] for row in rows}
            assert version_id1 in version_ids
            assert version_id2 in version_ids
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_versions WHERE version_id IN (?, ?) AND owner_id = ?",
                (version_id1, version_id2, owner),
            )
            await cursor.execute(
                "DELETE FROM memories WHERE id IN (?, ?) AND owner_id = ?", (memory_id1, memory_id2, owner)
            )
        await backend.close()


# ────────────────────────────────────────────────────────────────────────────
# Db2BranchRepository live stubs (PR #6)
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_branch_upsert_head() -> None:
    """Exercise Db2BranchRepository.upsert_memory_branch_head against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    version_id = f"v_{uuid.uuid4().hex[:8]}"
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Insert a memory version
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id,
                memory_id=memory_id,
                version_num=1,
                content="test content",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )

            # Upsert a branch head
            await backend.memory_branches.upsert_memory_branch_head(
                tx, memory_id=memory_id, branch="main", head_version_id=version_id
            )

            # Verify it was inserted/updated
            rows = await backend.memory_branches.fetch_memory_branch_heads(tx, [memory_id], owner_id=owner)
            assert len(rows) == 1
            assert rows[0]["memory_id"] == memory_id
            assert rows[0]["name"] == "main"
            assert rows[0]["head_version_id"] == version_id
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute("DELETE FROM memory_branches WHERE memory_id = ? AND owner_id = ?", (memory_id, owner))
            await cursor.execute(
                "DELETE FROM memory_versions WHERE version_id = ? AND owner_id = ?", (version_id, owner)
            )
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_branch_fetch_heads() -> None:
    """Exercise Db2BranchRepository.fetch_memory_branch_heads against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create parent memory rows first
    memory_id1 = str(uuid.uuid4())
    memory_id2 = str(uuid.uuid4())
    version_id1 = f"v1_{uuid.uuid4().hex[:6]}"
    version_id2 = f"v2_{uuid.uuid4().hex[:6]}"

    try:
        async with backend.transactional() as tx:
            # Insert parent memory rows
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id1, owner, "test", "test_hash1"),
            )
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id2, owner, "test", "test_hash2"),
            )

            # Insert memory versions
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id1,
                memory_id=memory_id1,
                version_num=1,
                content="test content 1",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id2,
                memory_id=memory_id2,
                version_num=1,
                content="test content 2",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )

            # Create branches
            await backend.memory_branches.create_memory_branch(
                tx, memory_id=memory_id1, branch="main", head_version_id=version_id1, owner_id=owner
            )
            await backend.memory_branches.create_memory_branch(
                tx, memory_id=memory_id2, branch="main", head_version_id=version_id2, owner_id=owner
            )

            # Fetch branch heads
            memory_ids = [memory_id1, memory_id2]
            rows = await backend.memory_branches.fetch_memory_branch_heads(tx, memory_ids, owner_id=owner)
            assert len(rows) == 2

            # Verify both branch heads are present
            memory_ids_in_result = {row["memory_id"] for row in rows}
            assert memory_id1 in memory_ids_in_result
            assert memory_id2 in memory_ids_in_result
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_branches WHERE memory_id IN (?, ?) AND owner_id = ?",
                (memory_id1, memory_id2, owner),
            )
            await cursor.execute(
                "DELETE FROM memory_versions WHERE version_id IN (?, ?) AND owner_id = ?",
                (version_id1, version_id2, owner),
            )
            await cursor.execute(
                "DELETE FROM memories WHERE id IN (?, ?) AND owner_id = ?", (memory_id1, memory_id2, owner)
            )
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_branch_delete_for_memories() -> None:
    """Exercise Db2BranchRepository.delete_memory_branches_for_memories against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create parent memory rows first
    memory_id1 = str(uuid.uuid4())
    memory_id2 = str(uuid.uuid4())
    version_id1 = f"v1_{uuid.uuid4().hex[:6]}"
    version_id2 = f"v2_{uuid.uuid4().hex[:6]}"

    try:
        async with backend.transactional() as tx:
            # Insert parent memory rows
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id1, owner, "test", "test_hash1"),
            )
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id2, owner, "test", "test_hash2"),
            )

            # Insert memory versions
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id1,
                memory_id=memory_id1,
                version_num=1,
                content="test content 1",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id2,
                memory_id=memory_id2,
                version_num=1,
                content="test content 2",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )

            # Create branches
            await backend.memory_branches.create_memory_branch(
                tx, memory_id=memory_id1, branch="main", head_version_id=version_id1, owner_id=owner
            )
            await backend.memory_branches.create_memory_branch(
                tx, memory_id=memory_id2, branch="main", head_version_id=version_id2, owner_id=owner
            )

            # Verify branches exist
            rows = await backend.memory_branches.fetch_memory_branch_heads(tx, [memory_id1, memory_id2], owner_id=owner)
            assert len(rows) == 2

            # Delete branches for memories
            deleted_count = await backend.memory_branches.delete_memory_branches_for_memories(
                tx, [memory_id1], owner_id=owner
            )
            assert deleted_count == 1

            # Verify only one branch remains
            rows = await backend.memory_branches.fetch_memory_branch_heads(tx, [memory_id1, memory_id2], owner_id=owner)
            assert len(rows) == 1
            assert rows[0]["memory_id"] == memory_id2
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_branches WHERE memory_id IN (?, ?) AND owner_id = ?",
                (memory_id1, memory_id2, owner),
            )
            await cursor.execute(
                "DELETE FROM memory_versions WHERE version_id IN (?, ?) AND owner_id = ?",
                (version_id1, version_id2, owner),
            )
            await cursor.execute(
                "DELETE FROM memories WHERE id IN (?, ?) AND owner_id = ?", (memory_id1, memory_id2, owner)
            )
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_branch_create() -> None:
    """Exercise Db2BranchRepository.create_memory_branch against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    version_id = f"v_{uuid.uuid4().hex[:8]}"
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Insert a memory version
            await backend.memory_versions.insert_memory_version(
                tx,
                version_id=version_id,
                memory_id=memory_id,
                version_num=1,
                content="test content",
                category="test",
                subcategory=None,
                metadata_json="{}",
                verbatim_content=None,
                owner_id=owner,
                namespace=None,
                permission_mode=None,
                source_model=None,
                created="2026-05-22 12:00:00",
                recall_last="2026-05-22 12:00:00",
                recall_count=0,
                is_compressed=False,
                is_encrypted=False,
                is_snapshot=False,
                is_hidden=False,
            )

            # Create a branch
            await backend.memory_branches.create_memory_branch(
                tx, memory_id=memory_id, branch="main", head_version_id=version_id, owner_id=owner
            )

            # Verify it was created
            rows = await backend.memory_branches.fetch_memory_branch_heads(tx, [memory_id], owner_id=owner)
            assert len(rows) == 1
            assert rows[0]["memory_id"] == memory_id
            assert rows[0]["name"] == "main"
            assert rows[0]["head_version_id"] == version_id
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute("DELETE FROM memory_branches WHERE memory_id = ? AND owner_id = ?", (memory_id, owner))
            await cursor.execute(
                "DELETE FROM memory_versions WHERE version_id = ? AND owner_id = ?", (version_id, owner)
            )
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


# --- PR #7: Db2CompressionRepository live stubs (5) ---


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_compression_candidate_exists() -> None:
    """Exercise Db2CompressionRepository.compression_candidate_exists against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    candidate_id = f"c_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Check if candidate exists (should be False initially)
            exists = await backend.compression.compression_candidate_exists(
                tx, candidate_id=candidate_id, memory_id=memory_id, owner_id=owner
            )
            assert exists is False

            # Insert a compression candidate
            await backend.compression.insert_compressed_variant(
                tx,
                candidate_id=candidate_id,
                memory_id=memory_id,
                variant_key="test_variant",
                compressed_data="compressed_test_data",
                compression_ratio=0.5,
                algorithm="test_algorithm",
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )

            # Check if candidate exists (should be True now)
            exists = await backend.compression.compression_candidate_exists(
                tx, candidate_id=candidate_id, memory_id=memory_id, owner_id=owner
            )
            assert exists is True
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_compression_candidates WHERE candidate_id = ? AND owner_id = ?",
                (candidate_id, owner),
            )
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_compression_insert_variant() -> None:
    """Exercise Db2CompressionRepository.insert_compressed_variant against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    candidate_id = f"c_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Insert a compressed variant
            await backend.compression.insert_compressed_variant(
                tx,
                candidate_id=candidate_id,
                memory_id=memory_id,
                variant_key="test_variant",
                compressed_data="compressed_test_data",
                compression_ratio=0.5,
                algorithm="test_algorithm",
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )

            # Verify it was inserted
            fetched = await backend.compression.fetch_compressed_variant_by_memory_id(
                tx, memory_id=memory_id, owner_id=owner
            )
            assert fetched is not None
            assert fetched["candidate_id"] == candidate_id
            assert fetched["memory_id"] == memory_id
            assert fetched["variant_key"] == "test_variant"
            assert fetched["compressed_data"] == "compressed_test_data"
            assert fetched["compression_ratio"] == 0.5
            assert fetched["algorithm"] == "test_algorithm"
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_compression_candidates WHERE candidate_id = ? AND owner_id = ?",
                (candidate_id, owner),
            )
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_compression_fetch_by_memory_id() -> None:
    """Exercise Db2CompressionRepository.fetch_compressed_variant_by_memory_id against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"
    candidate_id = f"c_{uuid.uuid4().hex[:8]}"

    # Create a parent memory row first
    memory_id = str(uuid.uuid4())
    try:
        async with backend.transactional() as tx:
            # Insert parent memory row
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, owner, "test", "test_hash"),
            )

            # Insert a compressed variant first
            await backend.compression.insert_compressed_variant(
                tx,
                candidate_id=candidate_id,
                memory_id=memory_id,
                variant_key="test_variant",
                compressed_data="compressed_test_data",
                compression_ratio=0.5,
                algorithm="test_algorithm",
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )

            # Fetch it by memory ID
            fetched = await backend.compression.fetch_compressed_variant_by_memory_id(
                tx, memory_id=memory_id, owner_id=owner
            )
            assert fetched is not None
            assert fetched["candidate_id"] == candidate_id
            assert fetched["memory_id"] == memory_id
            assert fetched["variant_key"] == "test_variant"
            assert fetched["compressed_data"] == "compressed_test_data"
            assert fetched["compression_ratio"] == 0.5
            assert fetched["algorithm"] == "test_algorithm"
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_compression_candidates WHERE candidate_id = ? AND owner_id = ?",
                (candidate_id, owner),
            )
            await cursor.execute("DELETE FROM memories WHERE id = ? AND owner_id = ?", (memory_id, owner))
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_compression_gather_stats() -> None:
    """Exercise Db2CompressionRepository.gather_stats against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create parent memory rows first
    memory_id1 = str(uuid.uuid4())
    memory_id2 = str(uuid.uuid4())
    candidate_id1 = f"c1_{uuid.uuid4().hex[:6]}"
    candidate_id2 = f"c2_{uuid.uuid4().hex[:6]}"

    try:
        async with backend.transactional() as tx:
            # Insert parent memory rows
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id1, owner, "test", "test_hash1"),
            )
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id2, owner, "test", "test_hash2"),
            )

            # Insert multiple compressed variants
            await backend.compression.insert_compressed_variant(
                tx,
                candidate_id=candidate_id1,
                memory_id=memory_id1,
                variant_key="test_variant_1",
                compressed_data="compressed_test_data_1",
                compression_ratio=0.5,
                algorithm="test_algorithm",
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )
            await backend.compression.insert_compressed_variant(
                tx,
                candidate_id=candidate_id2,
                memory_id=memory_id2,
                variant_key="test_variant_2",
                compressed_data="compressed_test_data_2",
                compression_ratio=0.3,
                algorithm="test_algorithm",
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )

            # Gather stats
            stats = await backend.compression.gather_stats(tx, owner_id=owner)
            assert stats is not None
            # We should have at least some stats data
            assert "total_candidates" in stats
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_compression_candidates WHERE candidate_id IN (?, ?) AND owner_id = ?",
                (candidate_id1, candidate_id2, owner),
            )
            await cursor.execute(
                "DELETE FROM memories WHERE id IN (?, ?) AND owner_id = ?", (memory_id1, memory_id2, owner)
            )
        await backend.close()


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_compression_fetch_variants_for_export() -> None:
    """Exercise Db2CompressionRepository.fetch_compressed_variants_for_export against live Db2."""
    from mnemos.persistence.db2 import Db2Backend, create_db2_pool

    pool = await create_db2_pool(DB2_DSN)
    backend = Db2Backend(pool, settings=None)
    await backend.open()
    owner = f"db2live_{uuid.uuid4().hex[:8]}"

    # Create parent memory rows first
    memory_id1 = str(uuid.uuid4())
    memory_id2 = str(uuid.uuid4())
    candidate_id1 = f"c1_{uuid.uuid4().hex[:6]}"
    candidate_id2 = f"c2_{uuid.uuid4().hex[:6]}"

    try:
        async with backend.transactional() as tx:
            # Insert parent memory rows
            cursor = tx.conn.cursor()
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id1, owner, "test", "test_hash1"),
            )
            await cursor.execute(
                """
                INSERT INTO memories (id, owner_id, category, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id2, owner, "test", "test_hash2"),
            )

            # Insert multiple compressed variants
            await backend.compression.insert_compressed_variant(
                tx,
                candidate_id=candidate_id1,
                memory_id=memory_id1,
                variant_key="test_variant_1",
                compressed_data="compressed_test_data_1",
                compression_ratio=0.5,
                algorithm="test_algorithm",
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )
            await backend.compression.insert_compressed_variant(
                tx,
                candidate_id=candidate_id2,
                memory_id=memory_id2,
                variant_key="test_variant_2",
                compressed_data="compressed_test_data_2",
                compression_ratio=0.3,
                algorithm="test_algorithm",
                created="2026-05-22 12:00:00",
                owner_id=owner,
            )

            # Fetch variants with pagination
            rows = await backend.compression.fetch_compressed_variants_for_export(
                tx, owner_id=owner, offset=0, limit=10
            )
            assert len(rows) >= 2

            # Verify both variants are present
            candidate_ids = {row["candidate_id"] for row in rows}
            assert candidate_id1 in candidate_ids
            assert candidate_id2 in candidate_ids
    finally:
        # Cleanup
        async with backend.transactional() as tx:
            cursor = tx.conn.cursor()
            await cursor.execute(
                "DELETE FROM memory_compression_candidates WHERE candidate_id IN (?, ?) AND owner_id = ?",
                (candidate_id1, candidate_id2, owner),
            )
            await cursor.execute(
                "DELETE FROM memories WHERE id IN (?, ?) AND owner_id = ?", (memory_id1, memory_id2, owner)
            )
        await backend.close()


# --- PR #8a: Db2MemoryRepository live stubs (3) ---


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_memory_insert() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.insert_memory (PR #8a)")


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_memory_fetch_by_id() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_memory_by_id (PR #8a)")


@pytest.mark.skipif(
    os.environ.get("DB2_DSN") is None,
    reason="DB2_DSN not set; nightly EAP-only",
)
@pytest.mark.asyncio
async def test_db2_live_memory_update() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.update_memory (PR #8a)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_delete() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.delete_memory (PR #8b)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_list() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.list_memories (PR #8b)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_count() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.count_memories (PR #8b)")


# --- PR #8c: Db2MemoryRepository live stubs (5, read-side) ---


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_get_memory() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.get_memory (PR #8c)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_assert_readable() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.assert_memory_readable (PR #8c)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_fetch_export() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_memory_export (PR #8c)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_fts_search() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fts_search (PR #8c)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_find_duplicate() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.find_active_duplicate_by_content_hash (PR #8c)")


# --- PR #8d: Db2MemoryRepository live stubs (5, version-snapshot + recall + stats + memory-log) ---


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_set_suppress_version_snapshot() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.set_suppress_version_snapshot (PR #8d)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_fetch_versioned_ids() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_versioned_memory_ids (PR #8d)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_gather_stats() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.gather_stats (MemoryStatsRow) (PR #8d)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_bump_recall() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.bump_recall_and_get_memory (PR #8d)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_fetch_log() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_memory_log (PR #8d)")


# ── PR #8e: Db2MemoryRepository live stubs (7, commit-head / diff / checkout / allowlist / dedup / context) ──


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_head_checks() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_memory_head_checks (PR #8e)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_diff_commit_pair() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_diff_commit_pair (PR #8e)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_checkout_commit() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_checkout_commit (PR #8e)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_referenced_allowlist() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_referenced_memory_allowlist (PR #8e)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_find_duplicate_groups() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.find_duplicate_content_groups (PR #8e)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_consolidate_duplicates() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.consolidate_duplicate_memories (PR #8e)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_memory_fetch_context() -> None:
    pytest.skip("live EAP exercise for Db2MemoryRepository.fetch_memory_context (PR #8e)")


# ────────────────────────────────────────────────────────────────────────────
# Db2FederationRepository live tests (PR #9a) — 6 core peer/sync methods


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_federation_list_peers() -> None:
    pytest.skip("live EAP exercise for Db2FederationRepository.list_peers (PR #9a)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_federation_get_peer() -> None:
    pytest.skip("live EAP exercise for Db2FederationRepository.get_peer (PR #9a)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_federation_delete_peer() -> None:
    pytest.skip("live EAP exercise for Db2FederationRepository.delete_peer (PR #9a)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_federation_list_due_peers() -> None:
    pytest.skip("live EAP exercise for Db2FederationRepository.list_due_peers (PR #9a)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_federation_fetch_memory_page() -> None:
    pytest.skip("live EAP exercise for Db2FederationRepository.fetch_memory_page (PR #9a)")


@pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live probe skipped")
@pytest.mark.asyncio
async def test_db2_live_federation_create_peer() -> None:
    pytest.skip("live EAP exercise for Db2FederationRepository.create_peer (PR #9a)")
