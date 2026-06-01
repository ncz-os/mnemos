"""Oracle backend for the Hive Mind repository contract.

This module deliberately does not call ``oracledb.init_oracle_client()``.
Callers can opt into thin or thick python-oracledb mode before constructing
``OracleHiveMindRepository``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
import time
import uuid
from typing import Any, Optional

from mnemos.hive_mind.repository import HiveMindRepository


UTC = dt.timezone.utc


def _json_dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), default=str)


def _json_loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if hasattr(value, "read"):
        value = value.read()
    if value in ("", b""):
        return default
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


def _uuid_to_raw(value: Optional[str | bytes]) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, bytes):
        if len(value) != 16:
            raise ValueError("RAW UUID values must be exactly 16 bytes")
        return value
    return uuid.UUID(str(value)).bytes


def _raw_to_uuid(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(uuid.UUID(bytes=bytes(value)))


def _ts_to_dt(value: Optional[float | int | dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)
    return dt.datetime.fromtimestamp(float(value), UTC).replace(tzinfo=None)


def _ts_to_tstz(value: Optional[float | int | dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return dt.datetime.fromtimestamp(float(value), UTC)


def _dt_to_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    return float(value)


def _read_lob(value: Any) -> Any:
    if hasattr(value, "read"):
        return value.read()
    return value


def _uuid7_raw() -> bytes:
    try:
        return uuid.uuid7().bytes  # type: ignore[attr-defined]
    except AttributeError:
        unix_ms = int(time.time() * 1000) & ((1 << 48) - 1)
        rand_a = random.getrandbits(12)
        rand_b = random.getrandbits(62)
        value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
        return uuid.UUID(int=value).bytes


class OracleHiveMindRepository(HiveMindRepository):
    """python-oracledb implementation of the Phase-2 Hive Mind contract."""

    def __init__(
        self,
        *,
        user: str,
        password: str,
        dsn: str,
        min_pool: int = 1,
        max_pool: int = 4,
        increment: int = 1,
        pool: Any = None,
    ) -> None:
        if pool is not None:
            self._pool = pool
            return
        import oracledb

        self._pool = oracledb.create_pool(
            user=user,
            password=password,
            dsn=dsn,
            min=min_pool,
            max=max_pool,
            increment=increment,
        )

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    @staticmethod
    def _execute(conn: Any, sql: str, params: Optional[dict[str, Any]] = None) -> Any:
        cur = conn.cursor()
        cur.execute(sql, params or {})
        return cur

    async def init(self) -> None:
        # TODO(Phase-2): migration application is handled by fleet-ops.
        raise NotImplementedError("OracleHiveMindRepository.init is deferred to migration/fleet-ops.")

    async def close(self) -> None:
        await self._run(self._pool.close)

    # ---------- agents ----------

    async def register_agent(
        self,
        *,
        urn: str,
        kind: str,
        host: str,
        runtime: str | None = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        cost_tier: Optional[str] = None,
        auth_method: Optional[str] = None,
        autonomy_level: Optional[str] = None,
        pid: Optional[int] = None,
        capabilities: Optional[list[str]] = None,
        version: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        await self._run(
            self._register_agent_sync,
            urn=urn,
            kind=kind,
            host=host,
            capabilities=capabilities,
        )

    async def insert_agent(
        self,
        *,
        urn: str,
        kind: str,
        runtime: str,
        model: str,
        provider: str,
        cost_tier: str,
        autonomy_level: str,
        auth_method: str,
        plan_cap_usd: float,
        subscription_pools: Optional[list[str]],
        host: str,
        session_id: str,
        pid: Optional[int],
        capabilities: Optional[list[str]],
        version: Optional[str],
        started_at: float,
        last_heartbeat: float,
        metadata: Optional[dict[str, Any]],
    ) -> None:
        await self._run(
            self._register_agent_sync,
            urn=urn,
            kind=kind,
            host=host,
            capabilities=capabilities,
            last_heartbeat=_ts_to_dt(last_heartbeat),
        )

    def _register_agent_sync(
        self,
        *,
        urn: str,
        kind: str,
        host: str,
        capabilities: Optional[list[str]],
        last_heartbeat: Optional[dt.datetime] = None,
    ) -> None:
        with self._pool.acquire() as conn:
            self._execute(
                conn,
                """
                MERGE INTO memory_agents dst
                USING (
                  SELECT :agent_urn AS agent_urn,
                         :kind AS kind,
                         :host AS host,
                         :last_heartbeat AS last_heartbeat,
                         :capabilities AS capabilities
                  FROM dual
                ) src
                ON (dst.agent_urn = src.agent_urn)
                WHEN MATCHED THEN UPDATE SET
                  dst.kind = src.kind,
                  dst.host = src.host,
                  dst.last_heartbeat = COALESCE(src.last_heartbeat, SYSTIMESTAMP),
                  dst.capabilities = src.capabilities
                WHEN NOT MATCHED THEN INSERT (
                  agent_urn, kind, host, registered_at, last_heartbeat, capabilities
                ) VALUES (
                  src.agent_urn, src.kind, src.host, SYSTIMESTAMP,
                  COALESCE(src.last_heartbeat, SYSTIMESTAMP), src.capabilities
                )
                """,
                {
                    "agent_urn": urn,
                    "kind": kind,
                    "host": host,
                    "last_heartbeat": last_heartbeat,
                    "capabilities": _json_dumps(capabilities),
                },
            )
            conn.commit()

    async def heartbeat(self, *, urn: str, ts: Optional[float] = None) -> bool:
        return await self._run(self._heartbeat_sync, urn, _ts_to_dt(ts))

    async def heartbeat_agent(self, *, urn: str, ts: float) -> bool:
        return await self.heartbeat(urn=urn, ts=ts)

    def _heartbeat_sync(self, urn: str, heartbeat_at: Optional[dt.datetime]) -> bool:
        with self._pool.acquire() as conn:
            cur = self._execute(
                conn,
                """
                UPDATE memory_agents
                SET last_heartbeat = COALESCE(:last_heartbeat, SYSTIMESTAMP)
                WHERE agent_urn = :agent_urn
                """,
                {"agent_urn": urn, "last_heartbeat": heartbeat_at},
            )
            conn.commit()
            return cur.rowcount > 0

    async def get_agent(self, urn: str) -> Optional[dict[str, Any]]:
        return await self._run(self._get_agent_sync, urn)

    def _get_agent_sync(self, urn: str) -> Optional[dict[str, Any]]:
        with self._pool.acquire() as conn:
            cur = self._execute(
                conn,
                """
                SELECT agent_urn, kind, host, registered_at, last_heartbeat,
                       capabilities
                FROM memory_agents
                WHERE agent_urn = :agent_urn
                """,
                {"agent_urn": urn},
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "urn": row[0],
                "kind": row[1],
                "host": row[2],
                "registered_at": _dt_to_ts(row[3]),
                "last_heartbeat": _dt_to_ts(row[4]),
                "capabilities": _json_loads(row[5], []),
            }

    async def list_agents(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        host: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_agents_sync,
            kind=kind,
            host=host,
            limit=limit,
        )

    def _list_agents_sync(
        self,
        *,
        kind: Optional[str],
        host: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT agent_urn, kind, host, registered_at, last_heartbeat, capabilities " "FROM memory_agents WHERE 1=1"
        )
        params: dict[str, Any] = {"limit": int(limit)}
        if kind:
            sql += " AND kind = :kind"
            params["kind"] = kind
        if host:
            sql += " AND host = :host"
            params["host"] = host
        sql += " ORDER BY last_heartbeat DESC FETCH FIRST :limit ROWS ONLY"
        with self._pool.acquire() as conn:
            cur = self._execute(conn, sql, params)
            return [
                {
                    "urn": r[0],
                    "kind": r[1],
                    "host": r[2],
                    "registered_at": _dt_to_ts(r[3]),
                    "last_heartbeat": _dt_to_ts(r[4]),
                    "capabilities": _json_loads(r[5], []),
                }
                for r in cur.fetchall()
            ]

    # ---------- jobs ----------

    async def create_job(
        self,
        *,
        job_id: str,
        submitter_urn: str,
        parent_job_id: Optional[str],
        kind: str,
        description: Optional[str],
        priority: int,
        deadline: Optional[float],
        required_capabilities: Optional[list[str]],
        eligible_kinds: Optional[list[str]],
        project: Optional[str],
        max_cost_tier: str,
        preferred_providers: Optional[list[str]],
        preferred_models: Optional[list[str]],
        mnemos_refs: Optional[list[str]],
        depends_on: Optional[list[str]],
        max_retries: int,
        started_at: float,
    ) -> None:
        await self.insert_job(
            job_id=job_id,
            submitter_urn=submitter_urn,
            parent_job_id=parent_job_id,
            kind=kind,
            description=description,
            priority=priority,
            eligible_kinds=eligible_kinds,
            project=project,
            tags={"mnemos_refs": mnemos_refs, "depends_on": depends_on},
            max_retries=max_retries,
            created_at=started_at,
        )

    async def insert_job_queued(self, **kwargs: Any) -> None:
        await self.create_job(**kwargs)

    async def insert_job(
        self,
        *,
        job_id: str,
        submitter_urn: str,
        parent_job_id: Optional[str],
        kind: str,
        description: Optional[str],
        priority: int = 0,
        eligible_kinds: Optional[list[str]] = None,
        project: Optional[str] = None,
        tags: Optional[dict[str, Any] | list[str]] = None,
        max_retries: int = 3,
        created_at: Optional[float] = None,
    ) -> None:
        await self._run(
            self._insert_job_sync,
            job_id=job_id,
            submitter_urn=submitter_urn,
            parent_job_id=parent_job_id,
            kind=kind,
            description=description,
            priority=priority,
            status="queued",
            eligible_kinds=eligible_kinds,
            project=project,
            tags=tags,
            max_retries=max_retries,
            created_at=_ts_to_dt(created_at),
            started_at=None,
            ended_at=None,
            result=None,
            claimed_by=None,
        )

    async def insert_job_cache_hit(
        self,
        *,
        job_id: str,
        submitter_urn: str,
        parent_job_id: Optional[str],
        kind: str,
        description: Optional[str],
        priority: int,
        deadline: Optional[float] = None,
        required_capabilities: Optional[list[str]] = None,
        eligible_kinds: Optional[list[str]] = None,
        project: Optional[str] = None,
        max_cost_tier: str = "A",
        preferred_providers: Optional[list[str]] = None,
        preferred_models: Optional[list[str]] = None,
        mnemos_refs: Optional[list[str]] = None,
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
        result: Optional[dict[str, Any]] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        result_mnemos_id: Optional[str] = None,
    ) -> None:
        tags = {
            "max_cost_tier": max_cost_tier,
            "preferred_providers": preferred_providers,
            "preferred_models": preferred_models,
            "mnemos_refs": mnemos_refs,
            "provider": provider,
            "model": model,
            "result_mnemos_id": result_mnemos_id,
        }
        await self._run(
            self._insert_job_sync,
            job_id=job_id,
            submitter_urn=submitter_urn,
            parent_job_id=parent_job_id,
            kind=kind,
            description=description,
            priority=priority,
            status="done",
            eligible_kinds=eligible_kinds,
            project=project,
            tags=tags,
            max_retries=0,
            created_at=_ts_to_dt(started_at),
            started_at=_ts_to_dt(started_at),
            ended_at=_ts_to_dt(ended_at),
            result=result,
            claimed_by=None,
        )

    def _insert_job_sync(
        self,
        *,
        job_id: str,
        submitter_urn: str,
        parent_job_id: Optional[str],
        kind: str,
        description: Optional[str],
        priority: int,
        status: str,
        eligible_kinds: Optional[list[str]],
        project: Optional[str],
        tags: Any,
        max_retries: int,
        created_at: Optional[dt.datetime],
        started_at: Optional[dt.datetime],
        ended_at: Optional[dt.datetime],
        result: Optional[dict[str, Any]],
        claimed_by: Optional[str],
    ) -> None:
        with self._pool.acquire() as conn:
            self._execute(
                conn,
                """
                INSERT INTO memory_jobs (
                  id, status, priority, kind, description, submitter_urn,
                  claimed_by, parent_job_id, created_at, started_at, ended_at,
                  result, eligible_kinds, project, tags, retry_count, max_retries
                ) VALUES (
                  :id, :status, :priority, :kind, :description, :submitter_urn,
                  :claimed_by, :parent_job_id, COALESCE(:created_at, SYSTIMESTAMP),
                  :started_at, :ended_at, :result, :eligible_kinds, :project,
                  :tags, 0, :max_retries
                )
                """,
                {
                    "id": _uuid_to_raw(job_id),
                    "status": status,
                    "priority": int(priority),
                    "kind": kind,
                    "description": description,
                    "submitter_urn": submitter_urn,
                    "claimed_by": claimed_by,
                    "parent_job_id": _uuid_to_raw(parent_job_id),
                    "created_at": created_at,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "result": _json_dumps(result),
                    "eligible_kinds": _json_dumps(eligible_kinds),
                    "project": project,
                    "tags": _json_dumps(tags),
                    "max_retries": int(max_retries),
                },
            )
            conn.commit()

    async def claim_next_job(
        self,
        *,
        agent_urn: str,
        agent_kind: str,
        agent_capabilities: Optional[list[str]] = None,
        agent_cost_tier: str = "A",
    ) -> Optional[dict[str, Any]]:
        return await self._run(
            self._claim_next_job_sync,
            agent_urn=agent_urn,
            agent_kind=agent_kind,
        )

    async def find_and_claim_job(
        self,
        *,
        agent_urn: str,
        agent_kind: str,
        agent_caps: set[str],
        agent_runtime: str,
        agent_model: str,
        agent_provider: str,
        agent_tier: str,
        cost_tier_order: list[str],
        sub_throttled: bool,
        now: float,
    ) -> Optional[dict[str, Any]]:
        claimed = await self.claim_next_job(
            agent_urn=agent_urn,
            agent_kind=agent_kind,
            agent_capabilities=list(agent_caps),
            agent_cost_tier=agent_tier,
        )
        if claimed:
            claimed["claimed_resources"] = {
                "runtime": agent_runtime,
                "model": agent_model,
                "provider": agent_provider,
                "cost_tier": agent_tier,
            }
        return claimed

    def _claim_next_job_sync(
        self,
        *,
        agent_urn: str,
        agent_kind: str,
    ) -> Optional[dict[str, Any]]:
        with self._pool.acquire() as conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, kind, description, priority, submitter_urn,
                           parent_job_id, created_at, eligible_kinds, project, tags
                    FROM memory_jobs
                    WHERE status = 'queued'
                    ORDER BY priority DESC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    """
                )
                for row in cur:
                    eligible = _json_loads(row[7], None)
                    if eligible and agent_kind not in eligible:
                        continue
                    job_id = row[0]
                    update_cur = self._execute(
                        conn,
                        """
                        UPDATE memory_jobs
                        SET status = 'claimed',
                            claimed_by = :claimed_by,
                            started_at = SYSTIMESTAMP
                        WHERE id = :id
                          AND status = 'queued'
                        """,
                        {"claimed_by": agent_urn, "id": job_id},
                    )
                    if update_cur.rowcount != 1:
                        continue
                    conn.commit()
                    return self._job_from_row(
                        (
                            row[0],
                            "claimed",
                            row[3],
                            row[1],
                            row[2],
                            row[4],
                            agent_urn,
                            row[5],
                            row[6],
                            dt.datetime.now(),
                            None,
                            None,
                            row[7],
                            row[8],
                            row[9],
                            0,
                            3,
                        )
                    )
                conn.rollback()
                return None
            except Exception:
                conn.rollback()
                raise

    async def update_job(
        self,
        *,
        job_id: str,
        status: str,
        result: Optional[dict[str, Any]] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        result_mnemos_id: Optional[str] = None,
        ended_at: Optional[float] = None,
        estimated_cost_usd: Optional[float] = None,
    ) -> None:
        await self._run(
            self._update_job_sync,
            job_id=job_id,
            status=status,
            result=result,
            ended_at=_ts_to_dt(ended_at),
        )

    def _update_job_sync(
        self,
        *,
        job_id: str,
        status: str,
        result: Optional[dict[str, Any]],
        ended_at: Optional[dt.datetime],
    ) -> None:
        with self._pool.acquire() as conn:
            cur = self._execute(
                conn,
                """
                UPDATE memory_jobs
                SET status = :status,
                    result = COALESCE(:result, result),
                    ended_at = CASE
                      WHEN :ended_at IS NOT NULL THEN :ended_at
                      WHEN :status IN ('done', 'failed', 'cancelled') THEN SYSTIMESTAMP
                      ELSE ended_at
                    END
                WHERE id = :id
                """,
                {
                    "id": _uuid_to_raw(job_id),
                    "status": status,
                    "result": _json_dumps(result),
                    "ended_at": ended_at,
                },
            )
            conn.commit()
            if cur.rowcount == 0:
                raise KeyError(f"job not found: {job_id}")

    async def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return await self._run(self._get_job_sync, job_id)

    def _get_job_sync(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._pool.acquire() as conn:
            cur = self._execute(
                conn,
                """
                SELECT id, status, priority, kind, description, submitter_urn,
                       claimed_by, parent_job_id, created_at, started_at,
                       ended_at, result, eligible_kinds, project, tags,
                       retry_count, max_retries
                FROM memory_jobs
                WHERE id = :id
                """,
                {"id": _uuid_to_raw(job_id)},
            )
            row = cur.fetchone()
            return self._job_from_row(row) if row else None

    async def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        project: Optional[str] = None,
        submitter_urn: Optional[str] = None,
        claimed_by: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_jobs_sync,
            status=status,
            kind=kind,
            project=project,
            submitter_urn=submitter_urn,
            claimed_by=claimed_by,
            limit=limit,
        )

    def _list_jobs_sync(
        self,
        *,
        status: Optional[str],
        kind: Optional[str],
        project: Optional[str],
        submitter_urn: Optional[str],
        claimed_by: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, status, priority, kind, description, submitter_urn, "
            "claimed_by, parent_job_id, created_at, started_at, ended_at, result, "
            "eligible_kinds, project, tags, retry_count, max_retries "
            "FROM memory_jobs WHERE 1=1"
        )
        params: dict[str, Any] = {"limit": int(limit)}
        for name, value in {
            "status": status,
            "kind": kind,
            "project": project,
            "submitter_urn": submitter_urn,
            "claimed_by": claimed_by,
        }.items():
            if value:
                sql += f" AND {name} = :{name}"
                params[name] = value
        sql += " ORDER BY priority DESC, created_at DESC FETCH FIRST :limit ROWS ONLY"
        with self._pool.acquire() as conn:
            cur = self._execute(conn, sql, params)
            return [self._job_from_row(r) for r in cur.fetchall()]

    def _job_from_row(self, row: Any) -> dict[str, Any]:
        return {
            "id": _raw_to_uuid(row[0]),
            "status": row[1],
            "priority": row[2],
            "kind": row[3],
            "description": row[4],
            "submitter_urn": row[5],
            "claimed_by": row[6],
            "parent_job_id": _raw_to_uuid(row[7]),
            "created_at": _dt_to_ts(row[8]),
            "queued_at": _dt_to_ts(row[8]),
            "started_at": _dt_to_ts(row[9]),
            "claimed_at": _dt_to_ts(row[9]),
            "ended_at": _dt_to_ts(row[10]),
            "result": _json_loads(row[11], None),
            "eligible_kinds": _json_loads(row[12], None),
            "project": row[13],
            "tags": _json_loads(row[14], None),
            "retry_count": row[15],
            "max_retries": row[16],
        }

    # ---------- cache + stats ----------

    async def get_cache(self, cache_key: str) -> Optional[Any]:
        return await self._run(self._get_cache_sync, cache_key)

    async def cache_lookup(self, cache_key: str) -> Optional[dict[str, Any]]:
        value = await self.get_cache(cache_key)
        return value if isinstance(value, dict) else {"value": value} if value is not None else None

    def _get_cache_sync(self, cache_key: str) -> Optional[Any]:
        with self._pool.acquire() as conn:
            cur = self._execute(
                conn,
                """
                SELECT value
                FROM memory_hive_cache
                WHERE cache_key = :cache_key
                  AND (expires_at IS NULL OR expires_at > SYSTIMESTAMP)
                """,
                {"cache_key": cache_key},
            )
            row = cur.fetchone()
            if not row:
                return None
            raw = _read_lob(row[0])
            try:
                return _json_loads(raw, None)
            except json.JSONDecodeError:
                return raw

    async def set_cache(
        self,
        *,
        cache_key: str,
        value: Any,
        expires_at: Optional[float | dt.datetime] = None,
    ) -> None:
        await self._run(
            self._set_cache_sync,
            cache_key=cache_key,
            value=value,
            expires_at=_ts_to_dt(expires_at),
        )

    async def cache_get(self, cache_key: str) -> Optional[dict[str, Any]]:
        return await self._run(self._cache_get_sync, cache_key)

    async def cache_store(
        self,
        *,
        cache_key: str,
        source_job_id: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
        result_json: Optional[dict[str, Any]] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        result_mnemos_id: Optional[str] = None,
        stored_at: Optional[float] = None,
        cached_at: Optional[float] = None,
    ) -> None:
        await self._run(
            self._cache_store_sync,
            cache_key=cache_key,
            result_json=result_json if result_json is not None else result,
            source_job_id=source_job_id,
            result_mnemos_id=result_mnemos_id,
            model=model,
            provider=provider,
            cached_at=cached_at if cached_at is not None else stored_at,
        )

    def _cache_get_sync(self, cache_key: str) -> Optional[dict[str, Any]]:
        with self._pool.acquire() as conn:
            cur = self._execute(
                conn,
                """
                SELECT result_json, source_job_id, result_mnemos_id,
                       hit_count, cost_saved_usd, model, provider, cached_at
                FROM hive_cache
                WHERE cache_key = :cache_key
                """,
                {"cache_key": cache_key},
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "result": _json_loads(row[0], None),
                "source_job_id": row[1],
                "result_mnemos_id": row[2],
                "hit_count": row[3],
                "cost_saved_usd": float(row[4] or 0),
                "model": row[5],
                "provider": row[6],
                "cached_at": _dt_to_ts(row[7]) if isinstance(row[7], dt.datetime) else row[7],
            }

    def _cache_store_sync(
        self,
        *,
        cache_key: str,
        result_json: Optional[dict[str, Any]],
        source_job_id: Optional[str],
        result_mnemos_id: Optional[str],
        model: Optional[str],
        provider: Optional[str],
        cached_at: Optional[float],
    ) -> None:
        with self._pool.acquire() as conn:
            self._execute(
                conn,
                """
                MERGE INTO hive_cache dst
                USING (
                  SELECT :cache_key AS cache_key,
                         :result_json AS result_json,
                         :source_job_id AS source_job_id,
                         :result_mnemos_id AS result_mnemos_id,
                         :model AS model,
                         :provider AS provider,
                         :cached_at AS cached_at
                  FROM dual
                ) src
                ON (dst.cache_key = src.cache_key)
                WHEN MATCHED THEN UPDATE SET
                  dst.result_json = src.result_json,
                  dst.source_job_id = src.source_job_id,
                  dst.result_mnemos_id = src.result_mnemos_id,
                  dst.model = src.model,
                  dst.provider = src.provider,
                  dst.cached_at = src.cached_at
                WHEN NOT MATCHED THEN INSERT (
                  cache_key, result_json, source_job_id, result_mnemos_id,
                  hit_count, cost_saved_usd, model, provider, cached_at, last_hit_at
                ) VALUES (
                  src.cache_key, src.result_json, src.source_job_id,
                  src.result_mnemos_id, 0, 0, src.model, src.provider,
                  src.cached_at, NULL
                )
                """,
                {
                    "cache_key": cache_key,
                    "result_json": _json_dumps(result_json or {}),
                    "source_job_id": source_job_id,
                    "result_mnemos_id": result_mnemos_id,
                    "model": model,
                    "provider": provider,
                    "cached_at": float(cached_at if cached_at is not None else time.time()),
                },
            )
            conn.commit()

    def _set_cache_sync(
        self,
        *,
        cache_key: str,
        value: Any,
        expires_at: Optional[dt.datetime],
    ) -> None:
        with self._pool.acquire() as conn:
            self._execute(
                conn,
                """
                MERGE INTO memory_hive_cache dst
                USING (
                  SELECT :cache_key AS cache_key,
                         :value AS value,
                         :expires_at AS expires_at
                  FROM dual
                ) src
                ON (dst.cache_key = src.cache_key)
                WHEN MATCHED THEN UPDATE SET
                  dst.value = src.value,
                  dst.expires_at = src.expires_at
                WHEN NOT MATCHED THEN INSERT (
                  cache_key, value, expires_at, created_at
                ) VALUES (
                  src.cache_key, src.value, src.expires_at, SYSTIMESTAMP
                )
                """,
                {
                    "cache_key": cache_key,
                    "value": value if isinstance(value, str) else _json_dumps(value),
                    "expires_at": expires_at,
                },
            )
            conn.commit()

    async def increment_kind_stat(
        self,
        *,
        kind: str,
        status: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        duration_sec: float = 0.0,
    ) -> None:
        await self._run(
            self._increment_kind_stat_sync,
            kind=kind,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            duration_sec=duration_sec,
        )

    def _increment_kind_stat_sync(
        self,
        *,
        kind: str,
        status: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        duration_sec: float,
    ) -> None:
        counts = {
            "done": (1, 0, 0),
            "success": (1, 0, 0),
            "failed": (0, 1, 0),
            "fail": (0, 1, 0),
            "cancelled": (0, 0, 1),
            "canceled": (0, 0, 1),
        }
        success_count, fail_count, cancelled_count = counts.get(status, (0, 0, 0))
        with self._pool.acquire() as conn:
            self._execute(
                conn,
                """
                MERGE INTO memory_worker_kind_stats dst
                USING (
                  SELECT :kind AS kind,
                         :success_count AS success_count,
                         :fail_count AS fail_count,
                         :cancelled_count AS cancelled_count,
                         :tokens_in AS total_tokens_in,
                         :tokens_out AS total_tokens_out,
                         :cost_usd AS total_cost_usd,
                         :duration_sec AS total_duration_sec
                  FROM dual
                ) src
                ON (dst.kind = src.kind)
                WHEN MATCHED THEN UPDATE SET
                  dst.success_count = dst.success_count + src.success_count,
                  dst.fail_count = dst.fail_count + src.fail_count,
                  dst.cancelled_count = dst.cancelled_count + src.cancelled_count,
                  dst.total_tokens_in = dst.total_tokens_in + src.total_tokens_in,
                  dst.total_tokens_out = dst.total_tokens_out + src.total_tokens_out,
                  dst.total_cost_usd = dst.total_cost_usd + src.total_cost_usd,
                  dst.total_duration_sec = dst.total_duration_sec + src.total_duration_sec,
                  dst.last_run = SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT (
                  kind, success_count, fail_count, cancelled_count,
                  total_tokens_in, total_tokens_out, total_cost_usd,
                  total_duration_sec, last_run
                ) VALUES (
                  src.kind, src.success_count, src.fail_count, src.cancelled_count,
                  src.total_tokens_in, src.total_tokens_out, src.total_cost_usd,
                  src.total_duration_sec, SYSTIMESTAMP
                )
                """,
                {
                    "kind": kind,
                    "success_count": success_count,
                    "fail_count": fail_count,
                    "cancelled_count": cancelled_count,
                    "tokens_in": int(tokens_in),
                    "tokens_out": int(tokens_out),
                    "cost_usd": float(cost_usd),
                    "duration_sec": float(duration_sec),
                },
            )
            conn.commit()

    # ---------- messages + events ----------

    async def insert_message(
        self,
        *,
        from_urn: str,
        to_urn: Optional[str],
        in_reply_to: Optional[str],
        topic: str,
        payload: dict[str, Any],
        ts: float,
    ) -> str:
        msg_id = _uuid7_raw()
        await self._run(
            self._insert_message_sync,
            msg_id=msg_id,
            from_urn=from_urn,
            to_urn=to_urn,
            in_reply_to=in_reply_to,
            topic=topic,
            payload=payload,
            ts=ts,
        )
        return _raw_to_uuid(msg_id) or ""

    async def post_message(self, **kwargs: Any) -> None:
        await self.insert_message(
            from_urn=kwargs["from_urn"],
            to_urn=kwargs.get("to_urn"),
            in_reply_to=kwargs.get("in_reply_to"),
            topic=kwargs["topic"],
            payload=kwargs["payload"],
            ts=kwargs["ts"],
        )

    def _insert_message_sync(
        self,
        *,
        msg_id: bytes,
        from_urn: str,
        to_urn: Optional[str],
        in_reply_to: Optional[str],
        topic: str,
        payload: dict[str, Any],
        ts: float,
    ) -> None:
        with self._pool.acquire() as conn:
            self._execute(
                conn,
                """
                INSERT INTO hive_messages (
                  id, from_urn, to_urn, in_reply_to, topic, payload, ts
                ) VALUES (
                  :id, :from_urn, :to_urn, :in_reply_to, :topic, :payload, :ts
                )
                """,
                {
                    "id": msg_id,
                    "from_urn": from_urn,
                    "to_urn": to_urn,
                    "in_reply_to": _uuid_to_raw(in_reply_to) if in_reply_to else None,
                    "topic": topic,
                    "payload": _json_dumps(payload),
                    "ts": float(ts),
                },
            )
            conn.commit()

    async def list_messages(
        self,
        *,
        to_urn: Optional[str] = None,
        topic: Optional[str] = None,
        since_ts: Optional[float] = None,
        limit: int = 100,
        **_legacy: Any,
    ) -> list[dict[str, Any]]:
        """Recent messages from hive_messages (option B: reuse agent_bus-owned table)."""
        return await self._run(
            self._list_messages_sync,
            to_urn=to_urn,
            topic=topic,
            since_ts=since_ts,
            limit=int(limit),
        )

    def _list_messages_sync(
        self,
        *,
        to_urn: Optional[str],
        topic: Optional[str],
        since_ts: Optional[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id, from_urn, to_urn, in_reply_to, topic, payload, ts " "FROM hive_messages WHERE 1=1"
        params: dict[str, Any] = {}
        if to_urn is not None:
            sql += " AND to_urn = :to_urn"
            params["to_urn"] = to_urn
        if topic is not None:
            sql += " AND topic = :topic"
            params["topic"] = topic
        if since_ts is not None:
            sql += " AND ts >= :since_ts"
            params["since_ts"] = float(since_ts)
        sql += " ORDER BY ts DESC FETCH FIRST :lim ROWS ONLY"
        params["lim"] = max(1, min(int(limit), 1000))
        with self._pool.acquire() as conn:
            cur = self._execute(conn, sql, params)
            rows = cur.fetchall() if hasattr(cur, "fetchall") else []
            out: list[dict[str, Any]] = []
            for r in rows:
                # hive_messages.id + in_reply_to are VARCHAR2(64) UUIDs (not RAW).
                rid = str(r[0]) if r and r[0] is not None else ""
                payload_raw = r[5]
                try:
                    payload = (
                        payload_raw
                        if isinstance(payload_raw, (dict, list))
                        else (_json_loads(payload_raw) if payload_raw else {})
                    )
                except Exception:
                    payload = {"_raw": str(payload_raw)}
                out.append(
                    {
                        "id": rid,
                        "from_urn": r[1],
                        "to_urn": r[2],
                        "in_reply_to": (str(r[3]) if r[3] is not None else None),
                        "topic": r[4],
                        "payload": payload,
                        "ts": float(r[6]) if r[6] is not None else 0.0,
                    }
                )
            return out

    async def emit_event(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        agent_urn: Optional[str] = None,
        ts: Optional[float | dt.datetime] = None,
    ) -> None:
        event_ts = ts if ts is not None else time.time()
        await self._run(
            self._emit_event_sync,
            kind=kind,
            payload=payload,
            agent_urn=agent_urn or payload.get("urn") or payload.get("agent_urn"),
            ts=_ts_to_tstz(event_ts),
            ts_epoch=_dt_to_ts(event_ts) if isinstance(event_ts, dt.datetime) else float(event_ts),
        )

    def _emit_event_sync(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        agent_urn: Optional[str],
        ts: Optional[dt.datetime],
        ts_epoch: float,
    ) -> None:
        with self._pool.acquire() as conn:
            params = {
                "ts": ts,
                "kind": kind,
                "payload": _json_dumps(payload),
                "agent_urn": agent_urn,
            }
            sql = """
            INSERT INTO hive_events (ts, kind, payload, agent_urn)
            VALUES (:ts, :kind, :payload, :agent_urn)
            """
            try:
                self._execute(conn, sql, params)
            except Exception as exc:
                if "ORA-00932" not in str(exc) or "NUMBER" not in str(exc):
                    raise
                params["ts"] = ts_epoch
                self._execute(conn, sql, params)
            conn.commit()

    async def tail_events(
        self,
        *,
        kind: Optional[str] = None,
        agent_urn: Optional[str] = None,
        since_ts: Optional[float] = None,
        limit: int = 100,
        **_legacy: Any,
    ) -> list[dict[str, Any]]:
        """Recent events from hive_events (option B: reuse agent_bus-owned table)."""
        return await self._run(
            self._tail_events_sync,
            kind=kind,
            agent_urn=agent_urn,
            since_ts=since_ts,
            limit=int(limit),
        )

    def _tail_events_sync(
        self,
        *,
        kind: Optional[str],
        agent_urn: Optional[str],
        since_ts: Optional[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id, ts, kind, payload, agent_urn FROM hive_events WHERE 1=1"
        params: dict[str, Any] = {}
        if kind is not None:
            sql += " AND kind = :kind"
            params["kind"] = kind
        if agent_urn is not None:
            sql += " AND agent_urn = :agent_urn"
            params["agent_urn"] = agent_urn
        if since_ts is not None:
            sql += " AND ts >= :since_ts"
            params["since_ts"] = float(since_ts)
        sql += " ORDER BY ts DESC, id DESC FETCH FIRST :lim ROWS ONLY"
        params["lim"] = max(1, min(int(limit), 5000))
        with self._pool.acquire() as conn:
            cur = self._execute(conn, sql, params)
            rows = cur.fetchall() if hasattr(cur, "fetchall") else []
            out: list[dict[str, Any]] = []
            for r in rows:
                payload_raw = r[3]
                try:
                    payload = (
                        payload_raw
                        if isinstance(payload_raw, (dict, list))
                        else (_json_loads(payload_raw) if payload_raw else {})
                    )
                except Exception:
                    payload = {"_raw": str(payload_raw)}
                out.append(
                    {
                        "id": int(r[0]) if r[0] is not None else 0,
                        "ts": float(r[1]) if r[1] is not None else 0.0,
                        "kind": r[2],
                        "payload": payload,
                        "agent_urn": r[4],
                    }
                )
            return out

    async def cache_record_hit(
        self,
        *,
        cache_key: str,
        delta: int = 1,
        **_legacy: Any,
    ) -> None:
        """Increment hits counter for cache_key. Best-effort ALTER TABLE adds the
        hits column on first call (idempotent via ORA-01430 swallow); subsequent
        calls UPDATE only. Failures are silent so cache reads stay usable on
        snapshots that disallow ALTER — schema owners should land hits via
        proper migration."""
        await self._run(
            self._cache_record_hit_sync,
            cache_key=cache_key,
            delta=int(delta),
        )

    def _cache_record_hit_sync(
        self,
        *,
        cache_key: str,
        delta: int,
    ) -> None:
        with self._pool.acquire() as conn:
            try:
                self._execute(
                    conn,
                    "ALTER TABLE memory_hive_cache ADD (hits NUMBER DEFAULT 0 NOT NULL)",
                    None,
                )
                conn.commit()
            except Exception as exc:
                msg = str(exc)
                if "ORA-01430" not in msg and "ORA-00955" not in msg:
                    return
            try:
                self._execute(
                    conn,
                    "UPDATE memory_hive_cache SET hits = NVL(hits,0) + :delta " "WHERE cache_key = :ck",
                    {"delta": int(delta), "ck": cache_key},
                )
                conn.commit()
            except Exception:
                pass

    async def stats_costs(
        self,
        *,
        since_ts: Optional[float] = None,
        limit: int = 100,
        **_legacy: Any,
    ) -> dict[str, Any]:
        """Aggregate cost/token totals from memory_worker_kind_stats. Returns
        {total, by_agent, by_kind}. since_ts filter on last_seen_at."""
        return await self._run(
            self._stats_costs_sync,
            since_ts=since_ts,
            limit=int(limit),
        )

    def _stats_costs_sync(
        self,
        *,
        since_ts: Optional[float],
        limit: int,
    ) -> dict[str, Any]:
        where = ""
        params: dict[str, Any] = {}
        if since_ts is not None:
            where = " WHERE last_seen_at >= :since_ts"
            params["since_ts"] = float(since_ts)
        with self._pool.acquire() as conn:
            cur = self._execute(
                conn,
                "SELECT NVL(SUM(tokens_in),0), NVL(SUM(tokens_out),0), "
                "NVL(SUM(est_cost_usd),0), COUNT(DISTINCT agent_urn), "
                "COUNT(DISTINCT kind) FROM memory_worker_kind_stats" + where,
                params,
            )
            tr = cur.fetchone() if hasattr(cur, "fetchone") else None
            total = {
                "tokens_in": int(tr[0]) if tr else 0,
                "tokens_out": int(tr[1]) if tr else 0,
                "est_cost_usd": float(tr[2]) if tr else 0.0,
                "distinct_agents": int(tr[3]) if tr else 0,
                "distinct_kinds": int(tr[4]) if tr else 0,
            }
            lim = max(1, min(int(limit), 1000))
            params_lim = dict(params)
            params_lim["lim"] = lim
            cur = self._execute(
                conn,
                "SELECT agent_urn, SUM(tokens_in), SUM(tokens_out), "
                "SUM(est_cost_usd) FROM memory_worker_kind_stats" + where + " GROUP BY agent_urn ORDER BY 4 DESC "
                "FETCH FIRST :lim ROWS ONLY",
                params_lim,
            )
            by_agent = [
                {
                    "agent_urn": r[0],
                    "tokens_in": int(r[1]) if r[1] is not None else 0,
                    "tokens_out": int(r[2]) if r[2] is not None else 0,
                    "est_cost_usd": float(r[3]) if r[3] is not None else 0.0,
                }
                for r in (cur.fetchall() if hasattr(cur, "fetchall") else [])
            ]
            cur = self._execute(
                conn,
                "SELECT kind, SUM(tokens_in), SUM(tokens_out), "
                "SUM(est_cost_usd) FROM memory_worker_kind_stats" + where + " GROUP BY kind ORDER BY 4 DESC "
                "FETCH FIRST :lim ROWS ONLY",
                params_lim,
            )
            by_kind = [
                {
                    "kind": r[0],
                    "tokens_in": int(r[1]) if r[1] is not None else 0,
                    "tokens_out": int(r[2]) if r[2] is not None else 0,
                    "est_cost_usd": float(r[3]) if r[3] is not None else 0.0,
                }
                for r in (cur.fetchall() if hasattr(cur, "fetchall") else [])
            ]
            return {"total": total, "by_agent": by_agent, "by_kind": by_kind}

    async def record_worker_kind_stats(
        self,
        *,
        urn: str,
        kind: str,
        success_delta: int,
        fail_delta: int,
        cancelled_delta: int,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        duration_sec: float,
        last_run: float,
    ) -> None:
        await self._run(
            self._record_worker_kind_stats_sync,
            urn=urn,
            kind=kind,
            success_delta=success_delta,
            fail_delta=fail_delta,
            cancelled_delta=cancelled_delta,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            duration_sec=duration_sec,
            last_run=last_run,
        )

    def _record_worker_kind_stats_sync(
        self,
        *,
        urn: str,
        kind: str,
        success_delta: int,
        fail_delta: int,
        cancelled_delta: int,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        duration_sec: float,
        last_run: float,
    ) -> None:
        with self._pool.acquire() as conn:
            self._execute(
                conn,
                """
                MERGE INTO hive_worker_kind_stats dst
                USING (
                  SELECT :urn AS urn,
                         :kind AS kind,
                         :success_delta AS success_count,
                         :fail_delta AS fail_count,
                         :cancelled_delta AS cancelled_count,
                         :tokens_in AS total_tokens_in,
                         :tokens_out AS total_tokens_out,
                         :cost_usd AS total_cost_usd,
                         :duration_sec AS total_duration_sec,
                         :last_run AS last_run
                  FROM dual
                ) src
                ON (dst.urn = src.urn AND dst.kind = src.kind)
                WHEN MATCHED THEN UPDATE SET
                  dst.success_count = dst.success_count + src.success_count,
                  dst.fail_count = dst.fail_count + src.fail_count,
                  dst.cancelled_count = dst.cancelled_count + src.cancelled_count,
                  dst.total_tokens_in = dst.total_tokens_in + src.total_tokens_in,
                  dst.total_tokens_out = dst.total_tokens_out + src.total_tokens_out,
                  dst.total_cost_usd = dst.total_cost_usd + src.total_cost_usd,
                  dst.total_duration_sec = dst.total_duration_sec + src.total_duration_sec,
                  dst.last_run = src.last_run
                WHEN NOT MATCHED THEN INSERT (
                  urn, kind, success_count, fail_count, cancelled_count,
                  total_tokens_in, total_tokens_out, total_cost_usd,
                  total_duration_sec, last_run
                ) VALUES (
                  src.urn, src.kind, src.success_count, src.fail_count,
                  src.cancelled_count, src.total_tokens_in, src.total_tokens_out,
                  src.total_cost_usd, src.total_duration_sec, src.last_run
                )
                """,
                {
                    "urn": urn,
                    "kind": kind,
                    "success_delta": int(success_delta),
                    "fail_delta": int(fail_delta),
                    "cancelled_delta": int(cancelled_delta),
                    "tokens_in": int(tokens_in),
                    "tokens_out": int(tokens_out),
                    "cost_usd": float(cost_usd),
                    "duration_sec": float(duration_sec),
                    "last_run": float(last_run),
                },
            )
            conn.commit()

    async def stats_workers(
        self,
        *,
        kind: Optional[str] = None,
        since_ts: Optional[float] = None,
        limit: int = 100,
        **_legacy: Any,
    ) -> list[dict[str, Any]]:
        """List rows from memory_worker_kind_stats sorted by last_seen_at DESC,
        est_cost_usd DESC. Optional kind/since_ts filters."""
        return await self._run(
            self._stats_workers_sync,
            kind=kind,
            since_ts=since_ts,
            limit=int(limit),
        )

    def _stats_workers_sync(
        self,
        *,
        kind: Optional[str],
        since_ts: Optional[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT agent_urn, kind, claims, completions, failures, "
            "tokens_in, tokens_out, est_cost_usd, last_seen_at "
            "FROM memory_worker_kind_stats WHERE 1=1"
        )
        params: dict[str, Any] = {}
        if kind is not None:
            sql += " AND kind = :kind"
            params["kind"] = kind
        if since_ts is not None:
            sql += " AND last_seen_at >= :since_ts"
            params["since_ts"] = float(since_ts)
        sql += " ORDER BY last_seen_at DESC, est_cost_usd DESC " "FETCH FIRST :lim ROWS ONLY"
        params["lim"] = max(1, min(int(limit), 1000))
        with self._pool.acquire() as conn:
            cur = self._execute(conn, sql, params)
            rows = cur.fetchall() if hasattr(cur, "fetchall") else []
            return [
                {
                    "agent_urn": r[0],
                    "kind": r[1],
                    "claims": int(r[2]) if r[2] is not None else 0,
                    "completions": int(r[3]) if r[3] is not None else 0,
                    "failures": int(r[4]) if r[4] is not None else 0,
                    "tokens_in": int(r[5]) if r[5] is not None else 0,
                    "tokens_out": int(r[6]) if r[6] is not None else 0,
                    "est_cost_usd": float(r[7]) if r[7] is not None else 0.0,
                    "last_seen_at": float(r[8]) if r[8] is not None else 0.0,
                }
                for r in rows
            ]


__all__ = ["OracleHiveMindRepository"]
