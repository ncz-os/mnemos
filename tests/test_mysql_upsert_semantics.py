from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mnemos.persistence.mysql import MysqlMemoryRepository


pytestmark = pytest.mark.asyncio


class _AsyncCursorContext:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self._cursor

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _tx_for_cursor(cursor):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_AsyncCursorContext(cursor))
    return SimpleNamespace(conn=conn)


async def test_mysql_insert_duplicate_id_leaves_existing_memory_unchanged():
    repo = MysqlMemoryRepository()
    cursor = MagicMock()
    rows: dict[str, dict[str, object]] = {}
    next_fetchone: tuple[object, ...] | None = None

    async def execute(sql, params=()):
        nonlocal next_fetchone
        compact_sql = " ".join(str(sql).split())
        sql_lower = compact_sql.lower()

        if sql_lower.startswith("insert into memories"):
            assert "on duplicate key update id = id" in sql_lower

            memory_id = params[0]
            incoming = {
                "id": memory_id,
                "content": params[1],
                "category": params[3],
                "subcategory": params[4],
                "metadata": params[5],
                "quality_rating": params[6],
                "verbatim_content": params[7],
                "owner_id": params[8],
                "namespace": params[9],
                "permission_mode": params[10],
                "source_model": params[11],
                "source_provider": params[12],
                "source_session": params[13],
                "source_agent": params[14],
                "created": params[15],
                "updated": params[16],
                "compressed_content": None,
                "group_id": None,
                "archived_at": None,
                "deleted_at": None,
            }
            if memory_id not in rows:
                rows[memory_id] = incoming
                cursor.rowcount = 1
            else:
                # Model the regression this test guards: COALESCE/VALUES
                # duplicate handling would have overwritten tenant columns.
                if "owner_id = coalesce(values(owner_id), owner_id)" in sql_lower:
                    rows[memory_id]["owner_id"] = incoming["owner_id"]
                if "namespace = coalesce(values(namespace), namespace)" in sql_lower:
                    rows[memory_id]["namespace"] = incoming["namespace"]
                cursor.rowcount = 0
            return

        if sql_lower.startswith("select id, content"):
            row = rows.get(params[0])
            cursor.description = (
                ("id",),
                ("owner_id",),
                ("namespace",),
            )
            next_fetchone = None if row is None else (row["id"], row["owner_id"], row["namespace"])
            return

        raise AssertionError(f"unexpected SQL: {compact_sql}")

    async def fetchone():
        return next_fetchone

    cursor.execute = AsyncMock(side_effect=execute)
    cursor.fetchone = AsyncMock(side_effect=fetchone)
    tx = _tx_for_cursor(cursor)

    kwargs = dict(
        memory_id="mem_same",
        content="original",
        category="facts",
        subcategory=None,
        metadata_json="{}",
        quality_rating=3,
        owner_id="alice",
        namespace="ns1",
        permission_mode=600,
        source_model=None,
        source_provider=None,
        source_session=None,
        source_agent=None,
        verbatim_content="original",
        created=None,
        updated=None,
    )
    await repo.insert_memory(tx, **kwargs)
    await repo.insert_memory(
        tx,
        **{
            **kwargs,
            "content": "poisoned",
            "owner_id": "mallory",
            "namespace": "ns2",
            "verbatim_content": "poisoned",
        },
    )

    row = await repo.fetch_memory_by_id(tx, "mem_same")

    assert row is not None
    assert row["owner_id"] == "alice"
    assert row["namespace"] == "ns1"
