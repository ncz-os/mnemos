from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mnemos.persistence.db2 import Db2Backend


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_restore_db2_session_isolation(cancelled):
    import asyncio

    levels = ["CS"]

    class Cursor:
        async def execute(self, sql):
            if sql.startswith("SET CURRENT ISOLATION"):
                levels[0] = sql.rsplit(" ", 1)[-1]

        async def fetchone(self):
            return (levels[0],)

        async def close(self):
            pass

    conn = SimpleNamespace(cursor=Cursor, commit=AsyncMock(), rollback=AsyncMock())

    @asynccontextmanager
    async def acquire():
        yield conn

    backend = Db2Backend(
        pool=SimpleNamespace(acquire=acquire), settings=SimpleNamespace(database=SimpleNamespace(db2_dialect="compat"))
    )
    try:
        async with backend.transactional(isolation="serializable"):
            assert levels[0] == "RR"
            if cancelled:
                raise asyncio.CancelledError()
    except asyncio.CancelledError:
        pass
    async with backend.transactional():
        assert levels[0] == "CS"
