from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_lifespan_shutdown_without_managed_inference_resource(monkeypatch, tmp_path):
    async def run():
        from mnemos.core import config as core_config
        from mnemos.core import lifecycle

        try:
            import mnemos.domain.graeae.engine as graeae_engine
        except ImportError:
            graeae_engine = None

        class FalsyPool:
            def __bool__(self):
                return False

        async def create_pool(**_kwargs):
            return FalsyPool()

        class FakeGraeaeEngine:
            def __init__(self):
                self.closed = False

            async def reload_from_registry(self, _pool):
                return None

            async def close(self):
                self.closed = True

        engine = FakeGraeaeEngine() if graeae_engine is not None else None

        monkeypatch.setattr(lifecycle, "_background_tasks", set())
        monkeypatch.setattr(lifecycle, "_worker_tasks", set())
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())
        monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
        monkeypatch.setenv("MNEMOS_SQLITE_PATH", str(tmp_path / "mnemos.sqlite3"))
        # The lifespan startup guard refuses an unauthenticated API
        # bound off-loopback (documented in
        # ``mnemos.core.network_guard``). This test runs in-process without
        # a real bind, so simulate a loopback bind via the validated-bind
        # env handoff -- the same mechanism ``mnemos serve`` uses.
        monkeypatch.setenv("_MNEMOS_BIND_VALIDATED_HOST", "127.0.0.1")
        core_config.reload_settings()
        monkeypatch.setattr(lifecycle, "_load_config", lambda: {"worker": {"enabled": False}})
        monkeypatch.setattr(lifecycle.asyncpg, "create_pool", create_pool)
        # No aioredis to patch: the lifecycle no longer opens a Redis cache.
        # app.state.cache is unconditionally None, asserted below.
        if graeae_engine is not None:
            monkeypatch.setattr(graeae_engine, "get_graeae_engine", lambda: engine)

        app = SimpleNamespace(state=SimpleNamespace())

        async with lifecycle.lifespan(app):
            assert app.state.pool is lifecycle._pool
            assert app.state.cache is None
            assert lifecycle._worker_status["distillation_worker"] == "disabled"
            assert lifecycle._worker_tasks == set()

        assert lifecycle._cache is None
        if engine is not None:
            assert engine.closed is True

    asyncio.run(run())
