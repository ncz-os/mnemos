"""Durable admission and claiming for the PostgreSQL MORPHEUS API."""

import json
from contextlib import asynccontextmanager
from typing import Any

_ADMISSION_LOCK = 64824721939001
_EXECUTION_LOCK = 64824721939002


class MorpheusQueueFull(RuntimeError):
    pass


class MorpheusQueueUnavailable(RuntimeError):
    pass


class MorpheusExecutionActive(RuntimeError):
    pass


class PostgresMorpheusJobs:
    """Persist jobs in existing run rows; never retry partial runs implicitly.

    Queued rows survive restarts. Claimed runs are not re-claimed after a
    crash: operators inspect/roll back partial work using the existing
    run API. This avoids repeating destructive phases without fencing.
    All queue consumers share a database admission lock and one active slot.
    """

    def __init__(self, backend: Any, *, max_pending: int = 16):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.backend = backend
        self.max_pending = max_pending

    @asynccontextmanager
    async def execution_slot(self):
        """Hold a session lock through claim/execution; process death releases it.

        The reserved connection never participates in phase transactions. A
        competing queue consumer or rollback can only proceed after release.
        """
        self._require_pool_capacity()
        async with self.backend._pool.acquire() as conn:
            acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _EXECUTION_LOCK)
            try:
                yield acquired
            finally:
                if acquired:
                    await conn.execute("SELECT pg_advisory_unlock($1)", _EXECUTION_LOCK)

    def _require_pool_capacity(self):
        if self.backend._pool.get_max_size() < 2:
            raise MorpheusQueueUnavailable(
                "MORPHEUS queue requires a PostgreSQL pool with at least two connections "
                "(one execution-lock slot and one phase transaction slot)"
            )

    async def enqueue(self, *, window_hours: int, cluster_min_size: int, config: dict, namespace: str | None) -> str:
        self._require_pool_capacity()
        async with self.backend.transactional() as tx:
            await tx.conn.execute("SELECT pg_advisory_xact_lock($1)", _ADMISSION_LOCK)
            rows = await tx.conn.fetch(
                "SELECT namespace FROM morpheus_runs WHERE status='running' AND config->>'durable_queue'='true'"
            )
            if len(rows) >= self.max_pending:
                raise MorpheusQueueFull("MORPHEUS queue is full")
            if any(namespace is None or r["namespace"] is None or r["namespace"] == namespace for r in rows):
                raise MorpheusQueueFull("A MORPHEUS run already occupies this namespace")
            run_id = await self.backend.morpheus.begin_run(
                tx,
                triggered_by="api",
                window_hours=window_hours,
                cluster_min_size=cluster_min_size,
                config={**config, "durable_queue": True},
                namespace=namespace,
            )
            await self.backend.morpheus.set_phase(tx, run_id, "queued")
        return run_id

    async def claim(self) -> tuple[str, dict] | None:
        async with self.backend.transactional() as tx:
            await tx.conn.execute("SELECT pg_advisory_xact_lock($1)", _ADMISSION_LOCK)
            active = await tx.conn.fetchval(
                "SELECT count(*) FROM morpheus_runs WHERE status='running' "
                "AND config->>'durable_queue'='true' AND COALESCE(phase,'') <> 'queued'"
            )
            if active:
                return None
            row = await tx.conn.fetchrow(
                "SELECT id,config FROM morpheus_runs WHERE status='running' "
                "AND config->>'durable_queue'='true' AND phase='queued' "
                "ORDER BY started_at,id LIMIT 1 FOR UPDATE"
            )
            if row is None:
                return None
            run_id = str(row["id"])
            await self.backend.morpheus.set_phase(tx, run_id, "claimed")
            config = json.loads(row["config"]) if isinstance(row["config"], str) else dict(row["config"])
        return run_id, config
