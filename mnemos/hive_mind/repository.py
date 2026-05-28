"""Hive Mind repository contracts and SQLite parity helpers."""

from __future__ import annotations

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
