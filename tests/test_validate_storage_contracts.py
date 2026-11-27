from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mnemos.core.secret_detection import redact_content, redact_field_with_stored
from mnemos.domain.deletion_fence import assert_no_active_deletion
from mnemos.persistence.db2 import Db2Backend, _Db2AsyncConnectionPool
from mnemos.persistence.oracle import OracleBackend


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
@pytest.mark.parametrize("stored", [False, True])
def test_redact_complete_private_key(kind, stored):
    header = f"-----BEGIN {kind}PRIVATE KEY-----"
    text = f"before\n{header}\nYWJjZGVm\nZ2hpamts\n-----END {kind}PRIVATE KEY-----\nafter"
    if stored:
        result = redact_field_with_stored(text, {"secret_redact_spans": [[7, 7 + len(header)]]}, "content")
    else:
        result = redact_content(text)
    assert "YWJjZGVm" not in result
    assert "Z2hpamts" not in result
    assert result.startswith("before\n") and result.endswith("\nafter")


def test_redact_incomplete_private_key():
    result = redact_content("before\n-----BEGIN PRIVATE KEY-----\nYWJjZGVm")
    assert "YWJjZGVm" not in result
    assert result.startswith("before\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError()])
async def test_require_available_deletion_fence(monkeypatch, error):
    monkeypatch.setattr("mnemos.persistence.worker_lifecycle.active_deletion_for_scope", AsyncMock(side_effect=error))
    with pytest.raises(Exception) as raised:
        await assert_no_active_deletion(object(), owner_id="alice", namespace="notes")
    assert raised.value.status_code == 503
    assert raised.value.headers["Retry-After"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [None, "read_committed", "repeatable_read", "serializable"])
async def test_validate_readonly_transaction(level):
    statements = []
    cursor = SimpleNamespace(
        execute=AsyncMock(side_effect=lambda sql, *_args: statements.append(sql)),
        fetchone=AsyncMock(return_value=("CS",)),
        close=AsyncMock(),
    )
    conn = SimpleNamespace(cursor=lambda: cursor, commit=AsyncMock(), rollback=AsyncMock())

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = SimpleNamespace(acquire=acquire)
    backend = OracleBackend(pool=pool, settings=SimpleNamespace())
    async with backend.transactional(isolation=level, readonly=True):
        assert statements == ["SET TRANSACTION READ ONLY"]
    backend = Db2Backend(pool=pool, settings=SimpleNamespace(database=SimpleNamespace(db2_dialect="compat")))
    async with backend.transactional(isolation=level, readonly=True):
        pass
    if level is None:
        assert statements == ["SET TRANSACTION READ ONLY"]
    else:
        db2_level = {
            "read_committed": "CS",
            "repeatable_read": "RR",
            "serializable": "RR",
        }[level]
        assert statements[-2:] == [f"SET CURRENT ISOLATION = {db2_level}", "SET CURRENT ISOLATION = CS"]


@pytest.mark.asyncio
@pytest.mark.parametrize("close_pool", [False, True])
async def test_release_pending_pool_open(close_pool):
    import asyncio

    pool = _Db2AsyncConnectionPool({}, max_size=1, acquire_timeout=0.2)
    started, ready = asyncio.Event(), asyncio.Event()

    class Connection:
        close = AsyncMock()

    conn = Connection()

    async def opening():
        started.set()
        await ready.wait()
        return conn

    pool._open = opening

    async def checkout():
        async with pool.acquire():
            return True

    task = asyncio.create_task(checkout())
    await started.wait()
    if close_pool:
        await pool.close()
    else:
        task.cancel()
    ready.set()
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await task
    for _ in range(10):
        await asyncio.sleep(0)
    assert pool._reserved == 0
    assert not pool._in_use
    conn.close.assert_awaited_once()
    if not close_pool:
        async with pool.acquire():
            pass
        await pool.close()
