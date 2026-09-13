from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mnemos.persistence import worker_lifecycle


class _AsyncpgOnlyConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append(("execute", " ".join(sql.split()), args))
        return "UPDATE 2"

    async def fetchrow(self, sql: str, *args: object):
        self.calls.append(("fetchrow", " ".join(sql.split()), args))
        return None

    async def fetch(self, sql: str, *args: object):
        self.calls.append(("fetch", " ".join(sql.split()), args))
        return [{"id": "m1"}]


class PostgresBackend:
    def __init__(self) -> None:
        self.conn = _AsyncpgOnlyConnection()
        self.transactions = 0

    @asynccontextmanager
    async def transactional(self):
        self.transactions += 1
        yield SimpleNamespace(conn=self.conn)


def test_postgres_ops_use_asyncpg_paramstyle_and_variadic_calls():
    async def _go() -> None:
        conn = _AsyncpgOnlyConnection()
        ops = worker_lifecycle._Ops(SimpleNamespace(conn=conn), "postgres")

        assert ops.sql("a = ? AND (? IS NULL OR b = ?)") == (
            "a = $1 AND ($2::text IS NULL OR b = $3)"
        )
        assert await ops.execute("UPDATE t SET a = ? WHERE b = ?", 1, 2) == 2
        assert await ops.fetchone("SELECT * FROM t WHERE a = ?", 1) is None
        assert await ops.fetchall("SELECT * FROM t WHERE a = ?", 1) == [{"id": "m1"}]

        assert conn.calls == [
            ("execute", "UPDATE t SET a = $1 WHERE b = $2", (1, 2)),
            ("fetchrow", "SELECT * FROM t WHERE a = $1", (1,)),
            ("fetch", "SELECT * FROM t WHERE a = $1", (1,)),
        ]

    asyncio.run(_go())


def test_both_postgres_deletion_workers_reach_locked_asyncpg_claims():
    async def _go() -> None:
        soft = PostgresBackend()
        hard = PostgresBackend()

        assert (
            await worker_lifecycle.process_one_deletion_request(
                soft,
                verify_attempts=1,
                restore_days=30,
            )
            is None
        )
        assert await worker_lifecycle.process_one_hard_deletion_request(hard) is None

        assert soft.transactions == hard.transactions == 1
        soft_call = soft.conn.calls[0]
        hard_call = hard.conn.calls[0]
        assert soft_call[0] == hard_call[0] == "fetchrow"
        assert soft_call[1].endswith("LIMIT 1 FOR UPDATE SKIP LOCKED")
        assert hard_call[1].endswith("LIMIT 1 FOR UPDATE SKIP LOCKED")
        assert soft_call[2] == ()
        assert len(hard_call[2]) == 1

    asyncio.run(_go())


class _HardDeleteAsyncpgConnection(_AsyncpgOnlyConnection):
    def __init__(self) -> None:
        super().__init__()
        self.claimed = False

    async def execute(self, sql: str, *args: object) -> str:
        normalized = " ".join(sql.split())
        self.calls.append(("execute", normalized, args))
        if normalized.startswith("INSERT INTO"):
            return "INSERT 0 1"
        if normalized.startswith("DELETE FROM"):
            return "DELETE 1"
        if normalized.startswith("UPDATE"):
            return "UPDATE 1"
        return "SET"

    async def fetchrow(self, sql: str, *args: object):
        normalized = " ".join(sql.split())
        self.calls.append(("fetchrow", normalized, args))
        if normalized.startswith("SELECT * FROM deletion_requests") and not self.claimed:
            self.claimed = True
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            return {
                "id": "request-1",
                "target_user_id": "alice",
                "target_namespace": "team",
                "requested_by": "admin",
                "requested_at": yesterday,
                "status": "soft_deleted",
                "soft_deleted_at": yesterday,
                "restore_by": yesterday,
            }
        if normalized.startswith("SELECT COUNT(*)"):
            return {"count": 0}
        return None

    async def fetch(self, sql: str, *args: object):
        normalized = " ".join(sql.split())
        self.calls.append(("fetch", normalized, args))
        if normalized.startswith("SELECT id, content, owner_id, namespace FROM memories"):
            return [
                {
                    "id": "memory-1",
                    "content": "erase me",
                    "owner_id": "alice",
                    "namespace": "team",
                }
            ]
        return []


def test_postgres_hard_delete_preserves_native_trigger_and_array_conventions():
    async def _go() -> None:
        backend = PostgresBackend()
        backend.conn = _HardDeleteAsyncpgConnection()
        result = await worker_lifecycle.process_one_hard_deletion_request(backend)

        assert result is not None
        assert result["status"] == "hard_deleted"
        statements = [call[1] for call in backend.conn.calls]
        suppress_at = statements.index(
            "SET LOCAL mnemos.suppress_version_snapshot = '1'"
        )
        memory_delete_at = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith("DELETE FROM memories")
        )
        assert suppress_at < memory_delete_at
        log_call = next(
            call
            for call in backend.conn.calls
            if call[1].startswith("INSERT INTO deletion_log")
        )
        assert isinstance(log_call[2][0], uuid.UUID)
        assert log_call[2][-1] == ["deletion_request_worker", "request-1"]
        assert "$2::text IS NULL" in next(
            statement
            for statement in statements
            if statement.startswith("SELECT id, content, owner_id, namespace")
        )

    asyncio.run(_go())
