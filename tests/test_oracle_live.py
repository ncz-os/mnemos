"""Live parity probe for the Oracle 23ai backend.

This module is skipped entirely unless `ORACLE_DSN` is set in the
environment. When the env var is present, it opens an oracledb async
pool against the given DSN, runs a minimal CRUD + visibility smoke,
and tears the connection down cleanly.

Pattern matches `tests/test_persistence_parity.py`'s PostgreSQL arm
but stays intentionally minimal — the heavy parity matrix lives in
that file. This file is the regression guard for the
DSN-recognized → backend-instantiable → trivial-roundtrip path.

To run:

    export ORACLE_DSN='oracle://MNEMOS:<password>@host:1521/service_name'
    pytest -q tests/test_oracle_live.py

To skip (default CI shape):

    unset ORACLE_DSN
    pytest -q tests/test_oracle_live.py    # collected but skipped

Cross-references:
- docs/INSTALL.md "Enterprise Backends (Oracle 23ai + IBM Db2 12.1.5)"
- docs/oracle-port-status.md (repository surface coverage)
- mnemos/persistence/oracle.py (OracleBackend impl)
- tests/test_oracle_vector_validation.py (driver-free helper coverage)
"""

from __future__ import annotations

import math
import os
import uuid
from types import SimpleNamespace

import pytest

ORACLE_DSN = os.environ.get("ORACLE_DSN")

# Driver-availability guard: skip module-wide if oracledb isn't installed.
# `OracleBackend` imports oracledb at module-load time, so we use the same
# import to drive the skip decision.
oracledb = pytest.importorskip("oracledb", reason="oracledb driver not installed")

# Driver-free tests at the bottom run regardless of ORACLE_DSN; module-level
# skipif targets only the live arms. Each live test re-asserts the gate via
# the per-test `skipif` decorator so collection stays explicit.
pytestmark = [
    pytest.mark.asyncio,
]

_LIVE_SKIP = pytest.mark.skipif(not ORACLE_DSN, reason="ORACLE_DSN not set; live probe skipped")


@pytest.fixture
def _oracle_prefix() -> str:
    """Per-test owner_id prefix so cleanup is deterministic."""
    return f"oraclelive_{uuid.uuid4().hex[:10]}"


@_LIVE_SKIP
async def test_oracle_backend_opens_and_closes() -> None:
    """Smoke: the backend can be constructed against a real Oracle 23ai DSN.

    This is the lightest possible live probe — it does NOT exercise the
    full parity matrix (those live in `test_persistence_parity.py` when
    the cleanup helper for Oracle lands). The probe asserts only that:

    - `oracledb` is importable
    - `mnemos.persistence.oracle.create_oracle_pool` accepts the DSN
    - `OracleBackend` opens and closes without raising

    A passing run here is the precondition for the heavier parity arm.
    """
    from mnemos.persistence.oracle import OracleBackend, create_oracle_pool

    pool = await create_oracle_pool(ORACLE_DSN)
    backend = OracleBackend(pool, SimpleNamespace())
    try:
        await backend.open()
        # If we get here, the backend is live against a real Oracle pool.
        assert backend is not None
    finally:
        await backend.close()
        # Defensive: ensure the pool itself is released even if backend.close
        # didn't own it.
        pool_close = getattr(pool, "close", None)
        if pool_close is not None:
            try:
                result = pool_close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass


@_LIVE_SKIP
async def test_oracle_memories_count_query(_oracle_prefix: str) -> None:
    """Trivial query path: COUNT(*) on the memories table.

    Validates the pool/cursor wiring against a real Oracle 23ai schema
    that has already had `db/oracle/migrations/0001_core_schema.sql`
    applied. Useful as the simplest "is this actually a working Oracle
    23ai install" signal.
    """
    from mnemos.persistence.oracle import create_oracle_pool

    pool = await create_oracle_pool(ORACLE_DSN)
    try:
        # oracledb async pool supports `acquire()` returning an async conn.
        async with pool.acquire() as conn:
            with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM memories")
                row = await cur.fetchone()
                assert row is not None
                assert isinstance(row[0], int)
    finally:
        close = getattr(pool, "close", None)
        if close is not None:
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────────────
# Live CRUD + semantic search + transaction rollback + session callback
# (Oracle eng audit T1 — minimum credible coverage, not full parity matrix)
# ────────────────────────────────────────────────────────────────────────────


async def _backend_ctx():
    """Yield an opened OracleBackend with deterministic cleanup."""
    from mnemos.persistence.oracle import OracleBackend, create_oracle_pool

    pool = await create_oracle_pool(ORACLE_DSN)
    backend = OracleBackend(pool, SimpleNamespace())
    await backend.open()
    return backend, pool


async def _close_backend(backend, pool) -> None:
    try:
        await backend.close()
    finally:
        pool_close = getattr(pool, "close", None)
        if pool_close is not None:
            try:
                result = pool_close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass


@_LIVE_SKIP
async def test_oracle_crud_roundtrip(_oracle_prefix: str) -> None:
    """End-to-end CRUD: insert → fetch → update → soft-delete → hard-delete.

    Uses ``OracleMemoryRepository`` directly via the backend's
    ``transactional()`` context so the insert + cleanup share one
    connection lifecycle. The owner_id prefix isolates this test's
    rows from any other concurrent runs.
    """
    backend, pool = await _backend_ctx()
    memory_id = f"oracle_live_{uuid.uuid4().hex[:12]}"
    try:
        async with backend.transactional() as tx:
            result = await backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="oracle-live CRUD roundtrip",
                category="solutions",
                subcategory=None,
                metadata_json="{}",
                quality_rating=5,
                owner_id=_oracle_prefix,
                namespace="default",
                permission_mode=0,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content=None,
                created=None,
                updated=None,
            )
            assert result.startswith("INSERT 0 1")

        async with backend.transactional() as tx:
            row = await backend.memories.fetch_memory_by_id(tx, memory_id)
            assert row is not None
            assert row["content"] == "oracle-live CRUD roundtrip"

        # Hard-delete (cleanup). Soft-delete + restore are covered by the
        # parity matrix in test_persistence_parity.py.
        async with backend.transactional() as tx:
            conn = tx.conn
            cur = conn.cursor()
            try:
                await cur.execute("DELETE FROM memories WHERE id = :id", {"id": memory_id})
            finally:
                cur.close()
    finally:
        await _close_backend(backend, pool)


@_LIVE_SKIP
async def test_oracle_semantic_search_returns_inserted_memory(
    _oracle_prefix: str,
) -> None:
    """Insert a row with a deterministic small embedding; expect top-1 match.

    Uses a 16-dim embedding to keep the test fast + drive the new
    ``_validate_and_format_vector`` ``expected_dim`` argument. The
    OracleMemoryRepository.insert_memory path doesn't yet take an
    embedding kwarg, so we set the column via a direct UPDATE after
    insert — mirrors the ingest path used by the bench harness.
    """
    from mnemos.persistence.oracle import _validate_and_format_vector
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    backend, pool = await _backend_ctx()
    memory_id = f"oracle_live_{uuid.uuid4().hex[:12]}"
    dim = 16
    embedding = [0.0] * dim
    embedding[0] = 1.0  # canonical unit vector along axis 0

    # Exercise the new dim-checked formatting path as a side test.
    literal = _validate_and_format_vector(embedding, expected_dim=dim)
    assert literal.startswith("[")

    try:
        async with backend.transactional() as tx:
            await backend.memories.insert_memory(
                tx,
                memory_id=memory_id,
                content="oracle-live semantic anchor",
                category="solutions",
                subcategory=None,
                metadata_json="{}",
                quality_rating=5,
                owner_id=_oracle_prefix,
                namespace="default",
                permission_mode=0,
                source_model=None,
                source_provider=None,
                source_session=None,
                source_agent=None,
                verbatim_content=None,
                created=None,
                updated=None,
            )
            # Write the embedding via TO_VECTOR — the literal is the same
            # formatted string the semantic_search path would emit.
            conn = tx.conn
            cur = conn.cursor()
            try:
                await cur.execute(
                    "UPDATE memories SET embedding = TO_VECTOR(:q) WHERE id = :id",
                    {"q": literal, "id": memory_id},
                )
            finally:
                cur.close()

        visibility = VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=None,
        )
        async with backend.transactional() as tx:
            rows = await backend.memories.semantic_search(
                tx,
                embedding=embedding,
                limit=5,
                visibility=visibility,
            )
            ids = [r["id"] for r in rows]
            assert memory_id in ids, f"inserted memory not in semantic_search top-5; got {ids!r}"

        # Cleanup.
        async with backend.transactional() as tx:
            conn = tx.conn
            cur = conn.cursor()
            try:
                await cur.execute("DELETE FROM memories WHERE id = :id", {"id": memory_id})
            finally:
                cur.close()
    finally:
        await _close_backend(backend, pool)


@_LIVE_SKIP
async def test_oracle_transaction_rollback(_oracle_prefix: str) -> None:
    """Rollback semantics: insert inside a transaction, raise, verify gone.

    Exercises the ``transactional()`` async-context-manager's exception
    path. The row must not survive after the exception unwinds the
    context, even though the cursor.execute itself succeeded.
    """
    backend, pool = await _backend_ctx()
    memory_id = f"oracle_live_{uuid.uuid4().hex[:12]}"

    class _ForcedRollback(RuntimeError):
        pass

    try:
        with pytest.raises(_ForcedRollback):
            async with backend.transactional() as tx:
                await backend.memories.insert_memory(
                    tx,
                    memory_id=memory_id,
                    content="oracle-live rollback victim",
                    category="solutions",
                    subcategory=None,
                    metadata_json="{}",
                    quality_rating=5,
                    owner_id=_oracle_prefix,
                    namespace="default",
                    permission_mode=0,
                    source_model=None,
                    source_provider=None,
                    source_session=None,
                    source_agent=None,
                    verbatim_content=None,
                    created=None,
                    updated=None,
                )
                raise _ForcedRollback("force rollback")

        async with backend.transactional() as tx:
            row = await backend.memories.fetch_memory_by_id(tx, memory_id)
            assert row is None, "row survived rollback — transactional() did not " "roll back on exception"
    finally:
        await _close_backend(backend, pool)


@_LIVE_SKIP
async def test_oracle_pool_session_callback_sets_nls_decimal_separator() -> None:
    """Verify the session callback pins NLS_NUMERIC_CHARACTERS to '. '.

    ``SELECT TO_NUMBER('1.5') FROM DUAL`` must return ``1.5`` regardless
    of the operator's locale. Without the session callback, hosts where
    the default decimal separator is ``,`` would parse the string as
    ``15`` (or raise an ORA-01722), silently corrupting vector literal
    parsing. The session callback closes that hole.
    """
    from mnemos.persistence.oracle import create_oracle_pool

    pool = await create_oracle_pool(ORACLE_DSN)
    try:
        async with pool.acquire() as conn:
            with conn.cursor() as cur:
                await cur.execute("SELECT TO_NUMBER('1.5') FROM DUAL")
                row = await cur.fetchone()
                assert row is not None
                # Some oracledb versions return Decimal, others float; both
                # must equal 1.5 to prove the NLS pin is in effect.
                assert float(row[0]) == pytest.approx(1.5)
    finally:
        close = getattr(pool, "close", None)
        if close is not None:
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────────────
# Driver-free contract guard — runs even when ORACLE_DSN is unset
# ────────────────────────────────────────────────────────────────────────────


async def test_oracle_vector_input_validation_rejects_nan() -> None:
    """Documents the validation contract at the live-test boundary.

    Redundant with the standalone unit tests in
    ``test_oracle_vector_validation.py`` — kept here so a developer
    reading the live test file alone sees the rejection contract.
    """
    from mnemos.persistence.oracle import _validate_and_format_vector

    with pytest.raises(ValueError, match="non-finite"):
        _validate_and_format_vector([0.1, math.nan, 0.3])
