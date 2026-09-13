from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mnemos.persistence.db2 import Db2MemoryRepository
from mnemos.persistence.mariadb import MariadbMemoryRepository
from mnemos.persistence.mysql import MysqlMemoryRepository
from mnemos.persistence.oracle import OracleMemoryRepository
from mnemos.persistence.postgres import PostgresMemoryRepository, PostgresTransaction
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope


class _SharedTags:
    def __init__(self) -> None:
        self.tags: set[str] = set()
        self.parent_lock = asyncio.Lock()
        self.unlocked_deletes = 0
        self.both_unlocked_deletes = asyncio.Event()


class _ConcurrentAsyncpgConnection:
    def __init__(self, shared: _SharedTags) -> None:
        self.shared = shared
        self.holds_parent_lock = False

    async def fetchrow(self, sql: str, *args: object):
        assert "FROM memories" in sql
        assert "FOR UPDATE" in sql
        await self.shared.parent_lock.acquire()
        self.holds_parent_lock = True
        return {"id": args[0]}

    async def execute(self, sql: str, *args: object) -> str:
        assert sql.startswith("DELETE FROM memory_tags")
        self.shared.tags.clear()
        if not self.holds_parent_lock:
            self.shared.unlocked_deletes += 1
            if self.shared.unlocked_deletes == 2:
                self.shared.both_unlocked_deletes.set()
            await self.shared.both_unlocked_deletes.wait()
        return "DELETE 0"

    async def executemany(self, sql: str, args) -> None:
        assert sql.startswith("INSERT INTO memory_tags")
        for _memory_id, tag in args:
            self.shared.tags.add(tag)
            await asyncio.sleep(0)

    def release_parent_lock(self) -> None:
        if self.holds_parent_lock:
            self.holds_parent_lock = False
            self.shared.parent_lock.release()


def test_postgres_concurrent_tag_replacements_leave_one_complete_set():
    async def _go() -> None:
        shared = _SharedTags()
        repo = PostgresMemoryRepository()
        tags_a = [f"a-{i}" for i in range(32)]
        tags_b = [f"b-{i}" for i in range(32)]

        async def replace(tags: list[str]) -> None:
            conn = _ConcurrentAsyncpgConnection(shared)
            tx = PostgresTransaction(conn, None)
            try:
                await repo.replace_memory_tags(tx, "mem-race", tags)
            finally:
                conn.release_parent_lock()

        await asyncio.gather(replace(tags_a), replace(tags_b))
        assert len(shared.tags) == 32
        assert frozenset(shared.tags) in {frozenset(tags_a), frozenset(tags_b)}

    asyncio.run(_go())


def _alice_visibility() -> VisibilityFilter:
    return VisibilityFilter(
        scope=VisibilityScope.OWN_ONLY,
        user_id="alice",
        group_ids=(),
        namespace="team",
    )


class _ContextCursor:
    def __init__(self, *, admitted: bool) -> None:
        self.admitted = admitted
        self.calls: list[tuple[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))

    async def executemany(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return ("mem-1",) if self.admitted else None


class _ContextConnection:
    def __init__(self, *, admitted: bool) -> None:
        self.cursor_value = _ContextCursor(admitted=admitted)

    def cursor(self):
        return self.cursor_value


class _DirectCursor(_ContextCursor):
    async def close(self):
        return None


class _DirectConnection:
    def __init__(self, *, admitted: bool) -> None:
        self.cursor_value = _DirectCursor(admitted=admitted)

    def cursor(self):
        return self.cursor_value


def test_mysql_and_mariadb_tag_replacement_lock_before_delete():
    async def _go() -> None:
        conn = _ContextConnection(admitted=True)
        replaced = await MysqlMemoryRepository().replace_memory_tags(
            SimpleNamespace(conn=conn),
            "mem-1",
            ["one", "two"],
            visibility=_alice_visibility(),
        )
        assert replaced is True
        assert "SELECT m.id FROM memories m" in conn.cursor_value.calls[0][0]
        assert conn.cursor_value.calls[0][0].endswith("FOR UPDATE")
        assert conn.cursor_value.calls[0][1] == ("mem-1", "alice", "team")
        assert conn.cursor_value.calls[1][0].startswith("DELETE FROM memory_tags")
        assert MariadbMemoryRepository.replace_memory_tags is MysqlMemoryRepository.replace_memory_tags

    asyncio.run(_go())


def test_oracle_and_db2_tag_replacement_lock_before_delete():
    async def _go() -> None:
        oracle_conn = _DirectConnection(admitted=True)
        db2_conn = _DirectConnection(admitted=True)
        assert await OracleMemoryRepository().replace_memory_tags(
            SimpleNamespace(conn=oracle_conn),
            "mem-1",
            ["one"],
            visibility=_alice_visibility(),
        )
        assert await Db2MemoryRepository().replace_memory_tags(
            SimpleNamespace(conn=db2_conn),
            "mem-1",
            ["one"],
            visibility=_alice_visibility(),
        )

        oracle_lock, oracle_params = oracle_conn.cursor_value.calls[0]
        db2_lock, db2_params = db2_conn.cursor_value.calls[0]
        assert oracle_lock.endswith("FOR UPDATE")
        assert oracle_params == {
            "memory_id": "mem-1",
            "vis_owner": "alice",
            "vis_ns": "team",
        }
        assert db2_lock.endswith("FOR UPDATE")
        assert db2_params == ("mem-1", "alice", "team")
        assert oracle_conn.cursor_value.calls[1][0].startswith("DELETE FROM memory_tags")
        assert db2_conn.cursor_value.calls[1][0].startswith("DELETE FROM memory_tags")

    asyncio.run(_go())


def test_tag_replacement_does_not_delete_when_parent_lock_denies_visibility():
    async def _go() -> None:
        mysql_conn = _ContextConnection(admitted=False)
        oracle_conn = _DirectConnection(admitted=False)
        assert not await MysqlMemoryRepository().replace_memory_tags(
            SimpleNamespace(conn=mysql_conn),
            "mem-1",
            ["forbidden"],
            visibility=_alice_visibility(),
        )
        assert not await OracleMemoryRepository().replace_memory_tags(
            SimpleNamespace(conn=oracle_conn),
            "mem-1",
            ["forbidden"],
            visibility=_alice_visibility(),
        )
        assert len(mysql_conn.cursor_value.calls) == 1
        assert len(oracle_conn.cursor_value.calls) == 1

    asyncio.run(_go())
