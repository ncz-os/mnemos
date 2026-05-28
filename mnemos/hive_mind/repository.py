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


class SqliteHiveMindRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

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
                """
            )
            db.commit()

    async def close(self) -> None:
        return None

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
        await self.insert_job(**kwargs)

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
