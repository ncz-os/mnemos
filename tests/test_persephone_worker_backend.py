import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mnemos.core import lifecycle
from mnemos.persistence import SqliteBackend
from mnemos.workers import persephone_archival_worker as worker


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("worker failed"), asyncio.CancelledError()])
async def test_main_passes_open_backend_and_always_closes_it(tmp_path, monkeypatch, failure):
    backend = SqliteBackend(tmp_path / "worker.db", SimpleNamespace())
    await backend.open()
    factory = AsyncMock(return_value=("sqlite", backend))
    monkeypatch.setattr(lifecycle, "build_configured_persistence_backend", factory)

    async def run(handle):
        assert handle is backend
        assert backend._conn is not None
        if failure is not None:
            raise failure

    monkeypatch.setattr(worker, "persephone_archival_worker_loop", run)
    if failure is None:
        await worker.main()
    else:
        with pytest.raises(type(failure)):
            await worker.main()
    factory.assert_awaited_once_with()
    assert backend._conn is None
