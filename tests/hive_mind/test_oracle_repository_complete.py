from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from typing import Any

import pytest

from mnemos.hive_mind.oracle_repository import OracleHiveMindRepository
from mnemos.hive_mind.repository import SqliteHiveMindRepository


class _FakeCursor:
    def __init__(self, conn: "_FakeConnection") -> None:
        self.conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.conn.statements.append((sql, params or {}))
        p = params or {}
        sql_norm = " ".join(sql.lower().split())
        if sql_norm.startswith("insert into hive_messages"):
            self.conn.messages.append(
                {
                    "id": p["id"],
                    "from_urn": p["from_urn"],
                    "to_urn": p["to_urn"],
                    "in_reply_to": p["in_reply_to"],
                    "topic": p["topic"],
                    "payload": json.loads(p["payload"]),
                    "ts": p["ts"],
                }
            )
        elif sql_norm.startswith("insert into hive_events"):
            self.conn.events.append(
                {
                    "ts": p["ts"],
                    "kind": p["kind"],
                    "payload": json.loads(p["payload"]),
                    "agent_urn": p["agent_urn"],
                }
            )
        elif "from hive_cache" in sql_norm:
            cache = self.conn.cache.get(p["cache_key"])
            if cache:
                self._rows = [
                    (
                        json.dumps(cache["result"]),
                        cache["source_job_id"],
                        cache["result_mnemos_id"],
                        cache["hit_count"],
                        cache["cost_saved_usd"],
                        cache["model"],
                        cache["provider"],
                        cache["cached_at"],
                    )
                ]
        elif sql_norm.startswith("merge into hive_cache"):
            existing = self.conn.cache.get(p["cache_key"], {})
            self.conn.cache[p["cache_key"]] = {
                "result": json.loads(p["result_json"]),
                "source_job_id": p["source_job_id"],
                "result_mnemos_id": p["result_mnemos_id"],
                "hit_count": existing.get("hit_count", 0),
                "cost_saved_usd": existing.get("cost_saved_usd", 0),
                "model": p["model"],
                "provider": p["provider"],
                "cached_at": p["cached_at"],
            }
        elif sql_norm.startswith("merge into hive_worker_kind_stats"):
            key = (p["urn"], p["kind"])
            row = self.conn.worker_stats.setdefault(
                key,
                {
                    "success_count": 0,
                    "fail_count": 0,
                    "cancelled_count": 0,
                    "total_tokens_in": 0,
                    "total_tokens_out": 0,
                    "total_cost_usd": 0.0,
                    "total_duration_sec": 0.0,
                    "last_run": None,
                },
            )
            row["success_count"] += p["success_delta"]
            row["fail_count"] += p["fail_delta"]
            row["cancelled_count"] += p["cancelled_delta"]
            row["total_tokens_in"] += p["tokens_in"]
            row["total_tokens_out"] += p["tokens_out"]
            row["total_cost_usd"] += p["cost_usd"]
            row["total_duration_sec"] += p["duration_sec"]
            row["last_run"] = p["last_run"]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.cache: dict[str, dict[str, Any]] = {}
        self.worker_stats: dict[tuple[str, str], dict[str, Any]] = {}
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn

    def acquire(self) -> _FakeConnection:
        return self.conn


@pytest.mark.asyncio
async def test_messages_events_cache_and_worker_stats_match_sqlite(tmp_path) -> None:
    sqlite_repo = SqliteHiveMindRepository(str(tmp_path / "hive.sqlite3"))
    await sqlite_repo.init()
    oracle_conn = _FakeConnection()
    oracle_repo = OracleHiveMindRepository(user="u", password="p", dsn="d", pool=_FakePool(oracle_conn))

    payload = {"text": "hello", "n": 7}
    ts = 1_779_999_123.25
    sqlite_msg_id = await sqlite_repo.insert_message(
        from_urn="urn:agent:codex:host:1",
        to_urn="urn:agent:worker:host:2",
        in_reply_to=None,
        topic="topic.a",
        payload=payload,
        ts=ts,
    )
    oracle_msg_id = await oracle_repo.insert_message(
        from_urn="urn:agent:codex:host:1",
        to_urn="urn:agent:worker:host:2",
        in_reply_to=None,
        topic="topic.a",
        payload=payload,
        ts=ts,
    )

    uuid.UUID(sqlite_msg_id)
    assert len(uuid.UUID(oracle_msg_id).bytes) == 16
    assert oracle_conn.messages[0] == {
        "id": uuid.UUID(oracle_msg_id).bytes,
        "from_urn": "urn:agent:codex:host:1",
        "to_urn": "urn:agent:worker:host:2",
        "in_reply_to": None,
        "topic": "topic.a",
        "payload": payload,
        "ts": ts,
    }

    await sqlite_repo.emit_event(kind="message", payload={"urn": "urn:agent:codex:host:1"}, ts=ts)
    await oracle_repo.emit_event(kind="message", payload={"urn": "urn:agent:codex:host:1"}, ts=ts)
    assert oracle_conn.events[0]["kind"] == "message"
    assert oracle_conn.events[0]["agent_urn"] == "urn:agent:codex:host:1"
    assert oracle_conn.events[0]["ts"] == dt.datetime.fromtimestamp(ts, dt.timezone.utc)

    result = {"ok": True, "items": [1, 2, 3]}
    await sqlite_repo.cache_store(
        cache_key="cache-key",
        result_json=result,
        source_job_id="job-1",
        result_mnemos_id="mem-1",
        model="gpt-5.5",
        provider="openai",
        cached_at=ts,
    )
    await oracle_repo.cache_store(
        cache_key="cache-key",
        result_json=result,
        source_job_id="job-1",
        result_mnemos_id="mem-1",
        model="gpt-5.5",
        provider="openai",
        cached_at=ts,
    )
    assert await oracle_repo.cache_get("cache-key") == await sqlite_repo.cache_get("cache-key")

    await sqlite_repo.record_worker_kind_stats(
        urn="urn:agent:worker:host:2",
        kind="code-edit",
        success_delta=1,
        fail_delta=2,
        cancelled_delta=3,
        tokens_in=100,
        tokens_out=40,
        cost_usd=0.125,
        duration_sec=9.5,
        last_run=ts,
    )
    await sqlite_repo.record_worker_kind_stats(
        urn="urn:agent:worker:host:2",
        kind="code-edit",
        success_delta=2,
        fail_delta=0,
        cancelled_delta=1,
        tokens_in=50,
        tokens_out=10,
        cost_usd=0.025,
        duration_sec=0.5,
        last_run=ts + 1,
    )
    await oracle_repo.record_worker_kind_stats(
        urn="urn:agent:worker:host:2",
        kind="code-edit",
        success_delta=1,
        fail_delta=2,
        cancelled_delta=3,
        tokens_in=100,
        tokens_out=40,
        cost_usd=0.125,
        duration_sec=9.5,
        last_run=ts,
    )
    await oracle_repo.record_worker_kind_stats(
        urn="urn:agent:worker:host:2",
        kind="code-edit",
        success_delta=2,
        fail_delta=0,
        cancelled_delta=1,
        tokens_in=50,
        tokens_out=10,
        cost_usd=0.025,
        duration_sec=0.5,
        last_run=ts + 1,
    )

    with sqlite3.connect(str(tmp_path / "hive.sqlite3")) as db:
        sqlite_row = db.execute(
            "SELECT success_count, fail_count, cancelled_count, total_tokens_in, "
            "total_tokens_out, total_cost_usd, total_duration_sec, last_run "
            "FROM hive_worker_kind_stats WHERE urn=? AND kind=?",
            ("urn:agent:worker:host:2", "code-edit"),
        ).fetchone()
    oracle_row = oracle_conn.worker_stats[("urn:agent:worker:host:2", "code-edit")]
    assert sqlite_row == (
        oracle_row["success_count"],
        oracle_row["fail_count"],
        oracle_row["cancelled_count"],
        oracle_row["total_tokens_in"],
        oracle_row["total_tokens_out"],
        oracle_row["total_cost_usd"],
        oracle_row["total_duration_sec"],
        oracle_row["last_run"],
    )

    sql_text = "\n".join(sql for sql, _ in oracle_conn.statements)
    assert "MERGE INTO hive_cache" in sql_text
    assert "MERGE INTO hive_worker_kind_stats" in sql_text
    assert "hive_events" in sql_text
