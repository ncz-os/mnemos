"""Hive Mind repository contracts and SQLite parity helpers."""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import time
import uuid
from typing import Any, Optional, Protocol, runtime_checkable


def _uuid7() -> str:
    try:
        return str(uuid.uuid7())  # type: ignore[attr-defined]
    except AttributeError:
        unix_ms = int(time.time() * 1000) & ((1 << 48) - 1)
        rand_a = random.getrandbits(12)
        rand_b = random.getrandbits(62)
        value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
        return str(uuid.UUID(int=value))


@runtime_checkable
class HiveMindRepository(Protocol):
    async def init(self) -> None: ...

    async def close(self) -> None: ...

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
        plan_cap_usd: float | None,
        subscription_pools: Optional[list[str]],
        host: str,
        session_id: str,
        pid: int | None,
        capabilities: Optional[list[str]],
        version: Optional[str],
        started_at: float,
        last_heartbeat: float,
        metadata: Optional[dict[str, Any]],
    ) -> None: ...

    async def insert_job_queued(self, **kwargs: Any) -> None: ...

    async def insert_job_cache_hit(self, **kwargs: Any) -> None: ...

    async def update_job_routing(self, *, job_id: str, routed_at: float, **kwargs: Any) -> bool: ...

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
    ) -> Optional[dict[str, Any]]: ...


class SqliteHiveMindRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    @staticmethod
    def _json(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, separators=(",", ":"), default=str)

    async def init(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hive_messages (
                  id TEXT PRIMARY KEY,
                  from_urn TEXT NOT NULL,
                  to_urn TEXT,
                  in_reply_to TEXT,
                  topic TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hive_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts REAL NOT NULL,
                  kind TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  agent_urn TEXT
                );
                CREATE TABLE IF NOT EXISTS hive_cache (
                  cache_key TEXT PRIMARY KEY,
                  result_json TEXT NOT NULL,
                  source_job_id TEXT,
                  result_mnemos_id TEXT,
                  hit_count INTEGER NOT NULL DEFAULT 0,
                  cost_saved_usd REAL NOT NULL DEFAULT 0,
                  model TEXT,
                  provider TEXT,
                  cached_at REAL NOT NULL,
                  last_hit_at REAL
                );
                CREATE TABLE IF NOT EXISTS hive_worker_kind_stats (
                  urn TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  success_count INTEGER NOT NULL DEFAULT 0,
                  fail_count INTEGER NOT NULL DEFAULT 0,
                  cancelled_count INTEGER NOT NULL DEFAULT 0,
                  total_tokens_in INTEGER NOT NULL DEFAULT 0,
                  total_tokens_out INTEGER NOT NULL DEFAULT 0,
                  total_cost_usd REAL NOT NULL DEFAULT 0,
                  total_duration_sec REAL NOT NULL DEFAULT 0,
                  last_run REAL,
                  PRIMARY KEY (urn, kind)
                );
                CREATE TABLE IF NOT EXISTS memory_jobs (
                  id TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 0,
                  kind TEXT NOT NULL,
                  description TEXT,
                  submitter_urn TEXT NOT NULL,
                  claimed_by TEXT,
                  parent_job_id TEXT,
                  created_at REAL NOT NULL,
                  started_at REAL,
                  ended_at REAL,
                  result TEXT,
                  eligible_kinds TEXT,
                  project TEXT,
                  tags TEXT,
                  retry_count INTEGER NOT NULL DEFAULT 0,
                  max_retries INTEGER NOT NULL DEFAULT 3,
                  CHECK (status IN ('queued','offered','claimed','running',
                                    'done','failed','cancelled'))
                );
                CREATE INDEX IF NOT EXISTS ix_memory_jobs_queue
                  ON memory_jobs(status, priority DESC, created_at ASC);
                CREATE INDEX IF NOT EXISTS ix_memory_jobs_project
                  ON memory_jobs(project);
                CREATE INDEX IF NOT EXISTS ix_memory_jobs_claimed_by
                  ON memory_jobs(claimed_by);
                CREATE TABLE IF NOT EXISTS agents (
                  urn TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  host TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  pid INTEGER,
                  runtime TEXT,
                  model TEXT,
                  provider TEXT,
                  cost_tier TEXT,
                  autonomy_level TEXT,
                  auth_method TEXT,
                  plan_cap_usd REAL,
                  plan_period_used_usd REAL NOT NULL DEFAULT 0,
                  subscription_pools TEXT,
                  capabilities TEXT,
                  version TEXT,
                  started_at REAL NOT NULL,
                  last_heartbeat REAL NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('online','idle','offline','error')),
                  metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  submitter_urn TEXT NOT NULL,
                  parent_job_id TEXT,
                  kind TEXT NOT NULL,
                  description TEXT,
                  priority INTEGER NOT NULL DEFAULT 0,
                  deadline REAL,
                  required_capabilities TEXT,
                  eligible_kinds TEXT,
                  project TEXT,
                  max_cost_tier TEXT NOT NULL DEFAULT 'A',
                  preferred_providers TEXT,
                  preferred_models TEXT,
                  mnemos_refs TEXT,
                  depends_on TEXT,
                  status TEXT NOT NULL CHECK(status IN ('queued','offered','claimed','running','done','failed','cancelled')),
                  claimed_by TEXT,
                  claimed_at REAL,
                  claimed_runtime TEXT,
                  claimed_model TEXT,
                  claimed_provider TEXT,
                  claimed_cost_tier TEXT,
                  started_at REAL NOT NULL,
                  retry_backoff_until REAL,
                  routed_at REAL,
                  routing_metadata TEXT,
                  ended_at REAL,
                  result TEXT,
                  result_mnemos_id TEXT,
                  tokens_in INTEGER,
                  tokens_out INTEGER,
                  estimated_cost_usd REAL,
                  retry_count INTEGER NOT NULL DEFAULT 0,
                  max_retries INTEGER NOT NULL DEFAULT 2
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_queue
                  ON jobs(status, priority DESC, started_at ASC);
                CREATE TABLE IF NOT EXISTS worker_kind_stats (
                  urn TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  success_count INTEGER NOT NULL DEFAULT 0,
                  fail_count INTEGER NOT NULL DEFAULT 0,
                  cancelled_count INTEGER NOT NULL DEFAULT 0,
                  total_tokens_in INTEGER NOT NULL DEFAULT 0,
                  total_tokens_out INTEGER NOT NULL DEFAULT 0,
                  total_cost_usd REAL NOT NULL DEFAULT 0,
                  total_duration_sec REAL NOT NULL DEFAULT 0,
                  last_run REAL,
                  PRIMARY KEY (urn, kind)
                );
                """
            )
            db.commit()

    async def close(self) -> None:
        return None

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
        plan_cap_usd: float | None,
        subscription_pools: Optional[list[str]],
        host: str,
        session_id: str,
        pid: int | None,
        capabilities: Optional[list[str]],
        version: Optional[str],
        started_at: float,
        last_heartbeat: float,
        metadata: Optional[dict[str, Any]],
    ) -> None:
        await asyncio.to_thread(
            self._insert_agent_sync,
            urn=urn,
            kind=kind,
            runtime=runtime,
            model=model,
            provider=provider,
            cost_tier=cost_tier,
            autonomy_level=autonomy_level,
            auth_method=auth_method,
            plan_cap_usd=plan_cap_usd,
            subscription_pools=subscription_pools,
            host=host,
            session_id=session_id,
            pid=pid,
            capabilities=capabilities,
            version=version,
            started_at=started_at,
            last_heartbeat=last_heartbeat,
            metadata=metadata,
        )

    def _insert_agent_sync(
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
        plan_cap_usd: float | None,
        subscription_pools: Optional[list[str]],
        host: str,
        session_id: str,
        pid: int | None,
        capabilities: Optional[list[str]],
        version: Optional[str],
        started_at: float,
        last_heartbeat: float,
        metadata: Optional[dict[str, Any]],
    ) -> None:
        with sqlite3.connect(self.db_path, timeout=30.0) as db:
            db.execute(
                "INSERT INTO agents "
                "(urn, kind, host, session_id, pid, runtime, model, provider, "
                "cost_tier, autonomy_level, auth_method, plan_cap_usd, "
                "plan_period_used_usd, subscription_pools, capabilities, version, started_at, "
                "last_heartbeat, status, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "COALESCE((SELECT plan_period_used_usd FROM agents WHERE urn=?), 0), "
                "?, ?, ?, ?, ?, 'online', ?) "
                "ON CONFLICT(urn) DO UPDATE SET "
                "kind=excluded.kind, host=excluded.host, session_id=excluded.session_id, "
                "pid=excluded.pid, runtime=excluded.runtime, model=excluded.model, "
                "provider=excluded.provider, cost_tier=excluded.cost_tier, "
                "autonomy_level=excluded.autonomy_level, auth_method=excluded.auth_method, "
                "plan_cap_usd=excluded.plan_cap_usd, subscription_pools=excluded.subscription_pools, "
                "capabilities=excluded.capabilities, "
                "version=excluded.version, last_heartbeat=excluded.last_heartbeat, "
                "status='online', metadata=excluded.metadata",
                (
                    urn,
                    kind,
                    host,
                    session_id,
                    pid,
                    runtime,
                    model,
                    provider,
                    cost_tier,
                    autonomy_level,
                    auth_method,
                    plan_cap_usd,
                    urn,
                    json.dumps(subscription_pools, separators=(",", ":")) if subscription_pools is not None else None,
                    json.dumps(capabilities, separators=(",", ":")) if capabilities is not None else None,
                    version,
                    float(started_at),
                    float(last_heartbeat),
                    json.dumps(metadata, separators=(",", ":"), default=str) if metadata is not None else None,
                ),
            )
            db.commit()

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
        msg_id = _uuid7()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO hive_messages "
                "(id, from_urn, to_urn, in_reply_to, topic, payload, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, from_urn, to_urn, in_reply_to, topic, json.dumps(payload), ts),
            )
            db.commit()
        return msg_id

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
        await asyncio.to_thread(
            self._insert_job_sync,
            job_id=job_id,
            submitter_urn=submitter_urn,
            parent_job_id=parent_job_id,
            kind=kind,
            description=description,
            priority=priority,
            eligible_kinds=eligible_kinds,
            project=project,
            tags=tags,
            max_retries=max_retries,
            created_at=created_at,
        )

    async def insert_job_queued(self, **kwargs: Any) -> None:
        await asyncio.to_thread(self._insert_job_queued_sync, **kwargs)

    def _insert_job_queued_sync(self, **kwargs: Any) -> None:
        started = float(kwargs.get("started_at") or kwargs.get("created_at") or time.time())
        with sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO jobs "
                    "(id, submitter_urn, parent_job_id, kind, description, priority, "
                    "deadline, required_capabilities, eligible_kinds, project, "
                    "max_cost_tier, preferred_providers, preferred_models, mnemos_refs, "
                    "depends_on, status, claimed_by, claimed_at, claimed_runtime, "
                    "claimed_model, claimed_provider, claimed_cost_tier, started_at, "
                    "retry_backoff_until, routed_at, routing_metadata, ended_at, result, result_mnemos_id, "
                    "tokens_in, tokens_out, estimated_cost_usd, retry_count, max_retries) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "'queued', NULL, NULL, NULL, NULL, NULL, NULL, ?, "
                    "NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, 0, ?)",
                    (
                        kwargs["job_id"],
                        kwargs["submitter_urn"],
                        kwargs.get("parent_job_id"),
                        kwargs["kind"],
                        kwargs.get("description"),
                        int(kwargs.get("priority") or 0),
                        kwargs.get("deadline"),
                        self._json(kwargs.get("required_capabilities")),
                        self._json(kwargs.get("eligible_kinds")),
                        kwargs.get("project"),
                        (kwargs.get("max_cost_tier") or "A").upper(),
                        self._json(kwargs.get("preferred_providers")),
                        self._json(kwargs.get("preferred_models")),
                        self._json(kwargs.get("mnemos_refs")),
                        self._json(kwargs.get("depends_on")),
                        started,
                        self._json(kwargs.get("routing_metadata")),
                        int(kwargs.get("max_retries") if kwargs.get("max_retries") is not None else 2),
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

    async def insert_job_cache_hit(self, **kwargs: Any) -> None:
        await asyncio.to_thread(self._insert_job_cache_hit_sync, **kwargs)

    async def update_job_routing(self, *, job_id: str, routed_at: float, **kwargs: Any) -> bool:
        return await asyncio.to_thread(self._update_job_routing_sync, job_id=job_id, routed_at=routed_at, **kwargs)

    def _update_job_routing_sync(self, *, job_id: str, routed_at: float, **kwargs: Any) -> bool:
        requested_max_cost_tier = kwargs.get("max_cost_tier")
        with sqlite3.connect(self.db_path, timeout=30.0) as db:
            existing = db.execute(
                "SELECT max_cost_tier, routing_metadata FROM jobs WHERE id=? AND status='queued' AND claimed_by IS NULL",
                (job_id,),
            ).fetchone()
            if not existing:
                return False
            existing_metadata = self._loads(existing[1], {}) or {}
            if (
                requested_max_cost_tier is not None
                and isinstance(existing_metadata, dict)
                and existing_metadata.get("submitter_max_cost_tier_explicit")
            ):
                order = ["A", "B", "C"]
                current_tier = str(existing[0] or "A").upper()
                requested_tier = str(requested_max_cost_tier).upper()
                try:
                    if order.index(requested_tier) > order.index(current_tier):
                        requested_max_cost_tier = current_tier
                except ValueError:
                    requested_max_cost_tier = current_tier
        fields = ["routed_at=?", "routing_metadata=?"]
        args: list[Any] = [float(routed_at), self._json(kwargs.get("routing_metadata") or {})]
        for column in (
            "required_capabilities",
            "eligible_kinds",
            "preferred_providers",
            "preferred_models",
            "mnemos_refs",
            "depends_on",
        ):
            if column in kwargs and kwargs[column] is not None:
                fields.append(f"{column}=?")
                args.append(self._json(kwargs[column]))
        if requested_max_cost_tier is not None:
            fields.append("max_cost_tier=?")
            args.append(str(requested_max_cost_tier).upper())
        args.append(job_id)
        with sqlite3.connect(self.db_path, timeout=30.0) as db:
            cur = db.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE id=? AND status='queued' AND claimed_by IS NULL",
                args,
            )
            db.commit()
            return cur.rowcount == 1

    def _insert_job_cache_hit_sync(self, **kwargs: Any) -> None:
        started = float(kwargs.get("started_at") or time.time())
        ended = float(kwargs.get("ended_at") or started)
        with sqlite3.connect(self.db_path, timeout=30.0) as db:
            db.execute(
                "INSERT INTO jobs "
                "(id, submitter_urn, parent_job_id, kind, description, priority, "
                "deadline, required_capabilities, eligible_kinds, project, "
                "max_cost_tier, preferred_providers, preferred_models, mnemos_refs, "
                "depends_on, status, claimed_by, claimed_at, claimed_runtime, "
                "claimed_model, claimed_provider, claimed_cost_tier, started_at, "
                "retry_backoff_until, ended_at, result, result_mnemos_id, "
                "tokens_in, tokens_out, estimated_cost_usd, retry_count, max_retries) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, "
                "'done', NULL, NULL, NULL, ?, ?, NULL, ?, NULL, ?, ?, ?, "
                "NULL, NULL, NULL, 0, ?)",
                (
                    kwargs["job_id"],
                    kwargs["submitter_urn"],
                    kwargs.get("parent_job_id"),
                    kwargs["kind"],
                    kwargs.get("description"),
                    int(kwargs.get("priority") or 0),
                    kwargs.get("deadline"),
                    self._json(kwargs.get("required_capabilities")),
                    self._json(kwargs.get("eligible_kinds")),
                    kwargs.get("project"),
                    (kwargs.get("max_cost_tier") or "A").upper(),
                    self._json(kwargs.get("preferred_providers")),
                    self._json(kwargs.get("preferred_models")),
                    self._json(kwargs.get("mnemos_refs")),
                    kwargs.get("model"),
                    kwargs.get("provider"),
                    started,
                    ended,
                    self._json(kwargs.get("result") or {}),
                    kwargs.get("result_mnemos_id"),
                    int(kwargs.get("max_retries") if kwargs.get("max_retries") is not None else 2),
                ),
            )
            db.commit()

    def _insert_job_sync(
        self,
        *,
        job_id: str,
        submitter_urn: str,
        parent_job_id: Optional[str],
        kind: str,
        description: Optional[str],
        priority: int,
        eligible_kinds: Optional[list[str]],
        project: Optional[str],
        tags: Optional[dict[str, Any] | list[str]],
        max_retries: int,
        created_at: Optional[float],
    ) -> None:
        created = float(created_at if created_at is not None else time.time())
        with sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO memory_jobs "
                    "(id, status, priority, kind, description, submitter_urn, "
                    "claimed_by, parent_job_id, created_at, started_at, ended_at, "
                    "result, eligible_kinds, project, tags, retry_count, max_retries) "
                    "VALUES (?, 'queued', ?, ?, ?, ?, NULL, ?, ?, NULL, NULL, "
                    "NULL, ?, ?, ?, 0, ?)",
                    (
                        job_id,
                        int(priority),
                        kind,
                        description,
                        submitter_urn,
                        parent_job_id,
                        created,
                        json.dumps(eligible_kinds, separators=(",", ":")) if eligible_kinds is not None else None,
                        project,
                        json.dumps(tags, separators=(",", ":"), default=str) if tags is not None else None,
                        int(max_retries),
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

    async def claim_next_job(
        self,
        *,
        agent_urn: str,
        agent_kind: str,
        agent_capabilities: Optional[list[str]] = None,
        agent_cost_tier: str = "A",
    ) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(
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
        return await asyncio.to_thread(
            self._find_and_claim_job_sync,
            agent_urn=agent_urn,
            agent_kind=agent_kind,
            agent_caps=set(agent_caps),
            agent_runtime=agent_runtime,
            agent_model=agent_model,
            agent_provider=agent_provider,
            agent_tier=agent_tier,
            cost_tier_order=list(cost_tier_order),
            sub_throttled=sub_throttled,
            now=float(now),
        )

    def _find_and_claim_job_sync(
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
        with sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None) as db:
            if not self._table_exists(db, "jobs"):
                return self._claim_next_job_sync(agent_urn=agent_urn, agent_kind=agent_kind)
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT id, submitter_urn, parent_job_id, kind, description, "
                    "priority, deadline, required_capabilities, eligible_kinds, "
                    "project, max_cost_tier, preferred_providers, preferred_models, "
                    "mnemos_refs, depends_on, status, claimed_by, claimed_at, "
                    "claimed_runtime, claimed_model, claimed_provider, "
                    "claimed_cost_tier, started_at, retry_backoff_until, ended_at, "
                    "result, result_mnemos_id, tokens_in, tokens_out, "
                    "estimated_cost_usd, retry_count, max_retries, routing_metadata "
                    "FROM jobs "
                    "WHERE status='queued' "
                    "AND claimed_by IS NULL "
                    "AND (retry_backoff_until IS NULL OR retry_backoff_until <= ?) "
                    "ORDER BY priority DESC, started_at ASC",
                    (now,),
                ).fetchall()
                for row in rows:
                    if not self._service_job_is_claimable(
                        db,
                        row=row,
                        agent_urn=agent_urn,
                        agent_kind=agent_kind,
                        agent_caps=agent_caps,
                        agent_model=agent_model,
                        agent_provider=agent_provider,
                        agent_tier=agent_tier,
                        cost_tier_order=cost_tier_order,
                        sub_throttled=sub_throttled,
                    ):
                        continue
                    cur = db.execute(
                        "UPDATE jobs SET status='claimed', claimed_by=?, claimed_at=?, "
                        "claimed_runtime=?, claimed_model=?, claimed_provider=?, "
                        "claimed_cost_tier=? "
                        "WHERE id=? AND status='queued' AND claimed_by IS NULL",
                        (
                            agent_urn,
                            now,
                            agent_runtime,
                            agent_model,
                            agent_provider,
                            agent_tier,
                            row[0],
                        ),
                    )
                    if cur.rowcount != 1:
                        continue
                    db.commit()
                    claimed_row = list(row)
                    claimed_row[15] = "claimed"
                    claimed_row[16] = agent_urn
                    claimed_row[17] = now
                    claimed_row[18] = agent_runtime
                    claimed_row[19] = agent_model
                    claimed_row[20] = agent_provider
                    claimed_row[21] = agent_tier
                    claimed = self._service_job_from_row(claimed_row)
                    claimed["claimed_resources"] = {
                        "runtime": agent_runtime,
                        "model": agent_model,
                        "provider": agent_provider,
                        "cost_tier": agent_tier,
                    }
                    return claimed
                db.rollback()
                return None
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _table_exists(db: sqlite3.Connection, table: str) -> bool:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _loads(value: Any, default: Any = None) -> Any:
        if value in (None, ""):
            return default
        return json.loads(value)

    @classmethod
    def _normalized_strings(cls, value: Any) -> set[str]:
        if isinstance(value, str):
            try:
                loaded = cls._loads(value, value)
            except json.JSONDecodeError:
                loaded = value
        else:
            loaded = value
        if not loaded:
            return set()
        if isinstance(loaded, dict):
            items = loaded.keys()
        elif isinstance(loaded, (list, tuple, set)):
            items = loaded
        else:
            items = str(loaded).strip("{}[]").split(",")
        return {
            "".join(ch if ch.isalnum() else "_" for ch in str(item).strip().lower()).strip("_")
            for item in items
            if str(item).strip()
        }

    def _service_job_is_claimable(
        self,
        db: sqlite3.Connection,
        *,
        row: Any,
        agent_urn: str,
        agent_kind: str,
        agent_caps: set[str],
        agent_model: str,
        agent_provider: str,
        agent_tier: str,
        cost_tier_order: list[str],
        sub_throttled: bool,
    ) -> bool:
        required_caps = set(self._loads(row[7], []) or [])
        if required_caps and not required_caps.issubset(agent_caps):
            return False
        eligible_kinds = self._loads(row[8], None)
        if eligible_kinds and agent_kind not in set(eligible_kinds):
            return False
        preferred_providers = self._loads(row[11], []) or []
        if preferred_providers and (agent_provider or "").lower() not in {
            str(provider).lower() for provider in preferred_providers
        }:
            return False
        preferred_models = self._loads(row[12], []) or []
        if preferred_models and (agent_model or "").lower() not in {str(model).lower() for model in preferred_models}:
            return False
        routing_metadata = self._loads(row[32], {}) if len(row) > 32 else {}
        required_pools = self._normalized_strings(
            routing_metadata.get("required_subscription_pools") if isinstance(routing_metadata, dict) else None
        )
        if required_pools:
            agent_pools_row = db.execute(
                "SELECT subscription_pools FROM agents WHERE urn=? AND status IN ('online','idle')",
                (agent_urn,),
            ).fetchone()
            agent_pools = self._normalized_strings(agent_pools_row[0] if agent_pools_row else None)
            if not agent_pools.intersection(required_pools):
                return False
        if sub_throttled and (row[10] or "A").upper() != "A":
            return False
        try:
            agent_tier_idx = cost_tier_order.index((agent_tier or "C").upper())
            max_tier_idx = cost_tier_order.index((row[10] or "A").upper())
        except ValueError:
            return False
        if agent_tier_idx > max_tier_idx:
            return False
        depends_on = self._loads(row[14], []) or []
        if depends_on:
            placeholders = ",".join("?" * len(depends_on))
            done_count = db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE id IN ({placeholders}) AND status='done'",
                tuple(depends_on),
            ).fetchone()[0]
            if int(done_count) != len(depends_on):
                return False
        return True

    def _claim_next_job_sync(self, *, agent_urn: str, agent_kind: str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT id, status, priority, kind, description, submitter_urn, "
                    "claimed_by, parent_job_id, created_at, started_at, ended_at, "
                    "result, eligible_kinds, project, tags, retry_count, max_retries "
                    "FROM memory_jobs "
                    "WHERE status='queued' "
                    "ORDER BY priority DESC, created_at ASC"
                ).fetchall()
                for row in rows:
                    eligible = json.loads(row[12]) if row[12] else None
                    if eligible and agent_kind not in eligible:
                        continue
                    claimed_at = time.time()
                    cur = db.execute(
                        "UPDATE memory_jobs "
                        "SET status='claimed', claimed_by=?, started_at=? "
                        "WHERE id=? AND status='queued'",
                        (agent_urn, claimed_at, row[0]),
                    )
                    if cur.rowcount != 1:
                        continue
                    db.commit()
                    claimed_row = (
                        row[0],
                        "claimed",
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        agent_urn,
                        row[7],
                        row[8],
                        claimed_at,
                        row[10],
                        row[11],
                        row[12],
                        row[13],
                        row[14],
                        row[15],
                        row[16],
                    )
                    return self._job_from_row(claimed_row)
                db.rollback()
                return None
            except Exception:
                db.rollback()
                raise

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
        return await asyncio.to_thread(
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
        params: list[Any] = []
        for name, value in {
            "status": status,
            "kind": kind,
            "project": project,
            "submitter_urn": submitter_urn,
            "claimed_by": claimed_by,
        }.items():
            if value:
                sql += f" AND {name}=?"
                params.append(value)
        sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        params.append(int(limit))
        with sqlite3.connect(self.db_path, timeout=30.0) as db:
            rows = db.execute(sql, params).fetchall()
        return [self._job_from_row(row) for row in rows]

    @staticmethod
    def _job_from_row(row: Any) -> dict[str, Any]:
        return {
            "id": row[0],
            "status": row[1],
            "priority": row[2],
            "kind": row[3],
            "description": row[4],
            "submitter_urn": row[5],
            "claimed_by": row[6],
            "parent_job_id": row[7],
            "created_at": row[8],
            "queued_at": row[8],
            "started_at": row[9],
            "claimed_at": row[9],
            "ended_at": row[10],
            "result": json.loads(row[11]) if row[11] else None,
            "eligible_kinds": json.loads(row[12]) if row[12] else None,
            "project": row[13],
            "tags": json.loads(row[14]) if row[14] else None,
            "retry_count": row[15],
            "max_retries": row[16],
        }

    @classmethod
    def _service_job_from_row(cls, row: Any) -> dict[str, Any]:
        return {
            "id": row[0],
            "submitter_urn": row[1],
            "parent_job_id": row[2],
            "kind": row[3],
            "description": row[4],
            "priority": row[5],
            "deadline": row[6],
            "required_capabilities": cls._loads(row[7], None),
            "eligible_kinds": cls._loads(row[8], None),
            "project": row[9],
            "max_cost_tier": row[10],
            "preferred_providers": cls._loads(row[11], None),
            "preferred_models": cls._loads(row[12], None),
            "mnemos_refs": cls._loads(row[13], None),
            "depends_on": cls._loads(row[14], None),
            "status": row[15],
            "claimed_by": row[16],
            "claimed_at": row[17],
            "claimed_runtime": row[18],
            "claimed_model": row[19],
            "claimed_provider": row[20],
            "claimed_cost_tier": row[21],
            "started_at": row[22],
            "retry_backoff_until": row[23],
            "ended_at": row[24],
            "result": cls._loads(row[25], None),
            "result_mnemos_id": row[26],
            "tokens_in": row[27],
            "tokens_out": row[28],
            "estimated_cost_usd": row[29],
            "retry_count": row[30],
            "max_retries": row[31],
        }

    async def emit_event(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        agent_urn: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        event_ts = float(ts if ts is not None else time.time())
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO hive_events (ts, kind, payload, agent_urn) " "VALUES (?, ?, ?, ?)",
                (
                    event_ts,
                    kind,
                    json.dumps(payload, separators=(",", ":")),
                    agent_urn or payload.get("urn") or payload.get("agent_urn"),
                ),
            )
            db.commit()

    async def cache_get(self, cache_key: str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT result_json, source_job_id, result_mnemos_id, "
                "hit_count, cost_saved_usd, model, provider, cached_at "
                "FROM hive_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if not row:
            return None
        return {
            "result": json.loads(row[0]),
            "source_job_id": row[1],
            "result_mnemos_id": row[2],
            "hit_count": row[3],
            "cost_saved_usd": row[4],
            "model": row[5],
            "provider": row[6],
            "cached_at": row[7],
        }

    async def cache_store(
        self,
        *,
        cache_key: str,
        result_json: Optional[dict[str, Any]] = None,
        source_job_id: Optional[str] = None,
        result_mnemos_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        cached_at: Optional[float] = None,
        result: Optional[dict[str, Any]] = None,
        stored_at: Optional[float] = None,
    ) -> None:
        body = result_json if result_json is not None else result
        cache_ts = float(cached_at if cached_at is not None else stored_at if stored_at is not None else time.time())
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO hive_cache "
                "(cache_key, result_json, source_job_id, result_mnemos_id, "
                "hit_count, cost_saved_usd, model, provider, cached_at, last_hit_at) "
                "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, NULL) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "result_json=excluded.result_json, "
                "source_job_id=excluded.source_job_id, "
                "result_mnemos_id=excluded.result_mnemos_id, "
                "model=excluded.model, provider=excluded.provider, "
                "cached_at=excluded.cached_at",
                (
                    cache_key,
                    json.dumps(body or {}, default=str),
                    source_job_id,
                    result_mnemos_id,
                    model,
                    provider,
                    cache_ts,
                ),
            )
            db.commit()

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
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO hive_worker_kind_stats "
                "(urn, kind, success_count, fail_count, cancelled_count, "
                "total_tokens_in, total_tokens_out, total_cost_usd, "
                "total_duration_sec, last_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(urn, kind) DO UPDATE SET "
                "success_count=success_count+excluded.success_count, "
                "fail_count=fail_count+excluded.fail_count, "
                "cancelled_count=cancelled_count+excluded.cancelled_count, "
                "total_tokens_in=total_tokens_in+excluded.total_tokens_in, "
                "total_tokens_out=total_tokens_out+excluded.total_tokens_out, "
                "total_cost_usd=total_cost_usd+excluded.total_cost_usd, "
                "total_duration_sec=total_duration_sec+excluded.total_duration_sec, "
                "last_run=excluded.last_run",
                (
                    urn,
                    kind,
                    int(success_delta),
                    int(fail_delta),
                    int(cancelled_delta),
                    int(tokens_in),
                    int(tokens_out),
                    float(cost_usd),
                    float(duration_sec),
                    float(last_run),
                ),
            )
            db.commit()


__all__ = ["HiveMindRepository", "SqliteHiveMindRepository"]
