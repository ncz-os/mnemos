from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,chain_enabled", [(False, True), (True, False), (True, True)])
async def test_audit_sealer_lifecycle(monkeypatch, enabled, chain_enabled):
    from mnemos.api import lifecycle_hooks
    from mnemos.core import config, lifecycle
    from mnemos.core.services import resolve_profile_services
    from mnemos.workers import audit_sealer

    resolution = resolve_profile_services(
        profile="edge", managed=True, env={"MNEMOS_AUDIT_SEALER_ENABLED": str(enabled)}
    )
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(services=SimpleNamespace(resolution=resolution)))
    monkeypatch.setattr(audit_sealer, "audit_chain_enabled", lambda: chain_enabled)
    backend = SimpleNamespace(audit_chain=object())
    monkeypatch.setattr(lifecycle, "get_persistence_backend", lambda: backend)
    monkeypatch.setattr(lifecycle, "_worker_status", {})
    instances = []

    class FakeSealer:
        def __init__(self, actual_backend):
            assert actual_backend is backend
            self.stopped = False
            instances.append(self)

        async def run_forever(self):
            await asyncio.Event().wait()

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(audit_sealer, "AuditSealer", FakeSealer)
    coro = lifecycle_hooks._audit_sealer_worker(None)
    if not (enabled and chain_enabled):
        assert coro is None
        assert not instances
        assert lifecycle._worker_status["audit_sealer_worker"] == "disabled"
        return
    task = asyncio.create_task(coro)
    await asyncio.sleep(0)
    assert lifecycle._worker_status["audit_sealer_worker"] == "healthy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instances[0].stopped
    assert lifecycle._worker_status["audit_sealer_worker"] == "idle"


@pytest.mark.parametrize("required", [False, True])
def test_audit_sealer_missing_key(monkeypatch, caplog, required):
    from mnemos.api import lifecycle_hooks
    from mnemos.core import config, lifecycle
    from mnemos.workers import audit_sealer

    monkeypatch.setattr(lifecycle_hooks, "service_enabled", lambda *_: True)
    monkeypatch.setattr(config, "get_settings", lambda: None)
    monkeypatch.setattr(config, "audit_chain_key_required", lambda: required)
    monkeypatch.setattr(audit_sealer, "audit_chain_enabled", lambda: True)
    monkeypatch.setattr(lifecycle, "get_persistence_backend", lambda: SimpleNamespace(audit_chain=object()))
    monkeypatch.setattr(lifecycle, "_worker_status", {})

    def missing_key(_backend):
        raise ValueError("missing root key")

    monkeypatch.setattr(audit_sealer, "AuditSealer", missing_key)
    if required:
        with pytest.raises(ValueError, match="missing root key"):
            lifecycle_hooks._audit_sealer_worker(None)
    else:
        assert lifecycle_hooks._audit_sealer_worker(None) is None
    assert lifecycle._worker_status["audit_sealer_worker"] == "unavailable"
    assert "missing root key" in caplog.text


def test_audit_sealer_unsupported_backend(monkeypatch, caplog):
    from mnemos.api import lifecycle_hooks
    from mnemos.core import config, lifecycle
    from mnemos.workers import audit_sealer

    monkeypatch.setattr(lifecycle_hooks, "service_enabled", lambda *_: True)
    monkeypatch.setattr(config, "get_settings", lambda: None)
    monkeypatch.setattr(audit_sealer, "audit_chain_enabled", lambda: True)
    monkeypatch.setattr(lifecycle, "get_persistence_backend", lambda: SimpleNamespace(audit_chain=None))
    monkeypatch.setattr(lifecycle, "_worker_status", {})
    monkeypatch.setattr(audit_sealer, "AuditSealer", lambda _: pytest.fail("sealer must not start"))

    assert lifecycle_hooks._audit_sealer_worker(None) is None
    assert lifecycle._worker_status["audit_sealer_worker"] == "unavailable"
    assert "no audit chain repository" in caplog.text


@pytest.mark.parametrize("audit_enabled", [False, True])
def test_lifespan_shutdown_without_managed_inference_resource(monkeypatch, tmp_path, audit_enabled):
    async def run():
        from mnemos.api import lifecycle_hooks
        from mnemos.core import config as core_config
        from mnemos.core import lifecycle
        from mnemos.workers import audit_sealer

        sealers = []

        class FakeSealer:
            def __init__(self, backend):
                self.backend = backend
                self.stopped = False
                sealers.append(self)

            async def run_forever(self):
                await asyncio.Event().wait()

            def stop(self):
                self.stopped = True

        monkeypatch.setattr(audit_sealer, "AuditSealer", FakeSealer)
        monkeypatch.setattr(audit_sealer, "audit_chain_enabled", lambda: True)
        monkeypatch.setattr(lifecycle, "_lifespan_worker_factories", {})
        lifecycle.register_lifespan_worker(
            "audit_sealer_worker", lifecycle_hooks._audit_sealer_worker, honor_worker_enabled=True
        )

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

        engine = None
        monkeypatch.setattr(lifecycle, "_lifespan_cleanup_hooks", {})

        monkeypatch.setattr(lifecycle, "_background_tasks", set())
        monkeypatch.setattr(lifecycle, "_worker_tasks", set())
        monkeypatch.setattr(lifecycle, "_delivery_attempt_tasks", set())
        monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
        monkeypatch.setenv("MNEMOS_SQLITE_PATH", str(tmp_path / "mnemos.sqlite3"))
        monkeypatch.setenv("MNEMOS_AUDIT_SEALER_ENABLED", str(audit_enabled))
        # The lifespan startup guard refuses an unauthenticated API
        # bound off-loopback (documented in
        # ``mnemos.core.network_guard``). This test runs in-process without
        # a real bind, so simulate a loopback bind via the validated-bind
        # env handoff -- the same mechanism ``mnemos serve`` uses.
        monkeypatch.setenv("_MNEMOS_BIND_VALIDATED_HOST", "127.0.0.1")
        core_config.reload_settings()
        monkeypatch.setattr(lifecycle, "_load_config", lambda: {"worker": {"enabled": True}})
        monkeypatch.setattr(lifecycle.asyncpg, "create_pool", create_pool)
        # No aioredis to patch: the lifecycle no longer opens a Redis cache.
        # app.state.cache is unconditionally None, asserted below.
        if graeae_engine is not None:
            monkeypatch.setattr(graeae_engine, "GraeaeEngine", FakeGraeaeEngine)
            monkeypatch.setattr(graeae_engine, "_graeae_engine", None)
            engine = graeae_engine.get_graeae_engine()

        app = SimpleNamespace(state=SimpleNamespace())

        async with lifecycle.lifespan(app):
            assert app.state.pool is lifecycle._pool
            assert app.state.cache is None
            assert lifecycle._worker_status["audit_sealer_worker"] == ("healthy" if audit_enabled else "disabled")
            assert len(sealers) == int(audit_enabled)
            if audit_enabled:
                assert sealers[0].backend is lifecycle.get_persistence_backend()
                assert not sealers[0].stopped
                assert len(lifecycle._worker_tasks) == 2
            else:
                assert len(lifecycle._worker_tasks) == 1

        assert lifecycle._cache is None
        if audit_enabled:
            assert sealers[0].stopped
            assert lifecycle._worker_status["audit_sealer_worker"] == "idle"
        if engine is not None:
            assert engine.closed is True

    asyncio.run(run())
