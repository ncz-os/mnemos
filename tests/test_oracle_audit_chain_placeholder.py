"""Regression: the pre-sign audit-chain placeholder must not be Oracle-NULL.

``OracleConsultationsRepository.create_consultation_with_audit`` inserts
``graeae_audit_log`` in two steps: first with a placeholder ``chain_hash``
(the real hash needs ``sequence_num``, which doesn't exist until the row
is inserted), then an ``UPDATE`` once the hash is computed. Postgres and
SQLite use ``""`` as that placeholder -- both distinguish an empty string
from ``NULL``. Oracle does not: an empty ``VARCHAR2`` bind is coerced to
``NULL``, and ``graeae_audit_log.chain_hash`` is ``NOT NULL`` (see
``mnemos/db_migrations/migrations_oracle/0002_graeae.sql``), so ``""``
raised ``ORA-01400: cannot insert NULL into ("MNEMOS"."GRAEAE_AUDIT_LOG".
"CHAIN_HASH")`` on every single GRAEAE consultation against real
production Oracle (2026-09-14) -- discovered only after fixing the
separate quota-lock cursor bug (v6.3.8/v6.3.9) let requests reach this
code path at all.

Driver-free: captures the SQL text + bound params the same way
``test_oracle_recency_dialect.py`` does, without a real Oracle connection.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.core import audit_chain
from mnemos.persistence.oracle import OracleConsultationsRepository


class _FakeOracleCursor:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls
        self.description: tuple[tuple[str], ...] = ()

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._calls.append({"sql": sql, "params": params or {}})

    async def fetchone(self) -> tuple[Any, ...] | None:
        sql = self._calls[-1]["sql"]
        # No prior audit-log row: exercise the genesis-hash path.
        if "SELECT id, chain_hash FROM graeae_audit_log" in sql:
            return None
        if "SELECT sequence_num FROM graeae_audit_log" in sql:
            self.description = (("sequence_num",),)
            return (1,)
        return None

    async def close(self) -> None:
        return None


class _FakeOracleConn:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def cursor(self) -> _FakeOracleCursor:
        return _FakeOracleCursor(self._calls)


@pytest.mark.asyncio
async def test_create_consultation_with_audit_never_binds_empty_chain_hash() -> None:
    audit_key = "test-key-not-real"
    calls: list[dict[str, Any]] = []
    tx = SimpleNamespace(conn=_FakeOracleConn(calls))
    repo = OracleConsultationsRepository()

    await repo.create_consultation_with_audit(
        tx,
        prompt="hello",
        task_type="reasoning",
        consensus_response="world",
        consensus_score=0.9,
        winning_muse="claude",
        cost=0.01,
        latency_ms=100,
        mode="auto",
        owner_id="default",
        namespace="default",
        genesis_hash=audit_chain.genesis_hash(key=audit_key),
        audit_key=audit_key,
        memory_ids=[],
    )

    insert_calls = [c for c in calls if c["sql"].startswith("INSERT INTO graeae_audit_log")]
    assert insert_calls, "expected an INSERT into graeae_audit_log"
    bound_chain_hash = insert_calls[0]["params"]["chain_hash"]

    # The real defect: an empty string here becomes NULL on Oracle and
    # violates the NOT NULL constraint (ORA-01400). Assert non-empty AND
    # the exact 64-char sentinel length a real SHA-256 hex digest has, so
    # a future refactor that shortens/empties the placeholder trips this.
    assert bound_chain_hash != ""
    assert len(bound_chain_hash) == 64

    # And the final UPDATE must overwrite it with the real, signed hash
    # (not the placeholder) once sequence_num is known.
    update_calls = [c for c in calls if c["sql"].startswith("UPDATE graeae_audit_log")]
    assert update_calls, "expected the sign-after-insert UPDATE"
    assert update_calls[0]["params"]["chain_hash"] != bound_chain_hash
