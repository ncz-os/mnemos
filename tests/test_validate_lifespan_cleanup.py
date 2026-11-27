import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["body", "startup", "close"])
async def test_release_lifespan_resources(monkeypatch, failure):
    from mnemos.core import lifecycle as lc
    from mnemos.core.config import get_settings

    monkeypatch.setenv("_MNEMOS_BIND_VALIDATED_HOST", "127.0.0.1")
    backend = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("close") if failure == "close" else None),
        capabilities=set(),
        oauth=None,
    )
    cache = SimpleNamespace(aclose=AsyncMock())
    hook = AsyncMock()
    for name in ["_worker_tasks", "_background_tasks", "_delivery_attempt_tasks"]:
        monkeypatch.setattr(lc, name, set())
    monkeypatch.setattr(lc, "_lifespan_worker_factories", {})
    monkeypatch.setattr(lc, "_lifespan_cleanup_hooks", {"cleanup": hook})
    monkeypatch.setattr(lc, "_auth_configurer", None)
    monkeypatch.setattr(lc, "_load_config", lambda: {"worker": {"enabled": False}})
    monkeypatch.setattr(lc, "_select_persistence_backend", lambda settings: "sqlite")
    monkeypatch.setattr(lc, "_build_sqlite_backend", AsyncMock(return_value=backend))
    monkeypatch.setattr(lc, "_log_and_validate_backend_capabilities", lambda *a: None)
    monkeypatch.setattr(lc, "_log_federation_startup_guidance", AsyncMock())
    monkeypatch.setattr(
        "mnemos.persistence.base.backend_supported_layers", lambda backend: set(get_settings().layers.active_layers)
    )
    monkeypatch.setattr("mnemos.nats.connect_nats", AsyncMock())
    tasks = []

    async def startup(*args):
        lc._cache = cache
        task = asyncio.create_task(asyncio.Event().wait())
        tasks.append(task)
        lc._worker_tasks.add(task)
        if failure == "startup":
            raise RuntimeError("startup")

    monkeypatch.setattr(lc, "_run_post_db_startup_hooks", startup)
    try:
        try:
            async with lc.lifespan(SimpleNamespace(state=SimpleNamespace())):
                if failure == "body":
                    raise RuntimeError("body")
        except RuntimeError as exc:
            assert str(exc) == failure
        backend.close.assert_awaited_once()
        cache.aclose.assert_awaited_once()
        hook.assert_awaited_once()
        assert all(task.done() for task in tasks)
        assert lc._persistence_backend is None and lc._pool is None and lc._cache is None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
