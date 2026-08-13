"""Regression coverage for the post-DB startup hook registry."""
from __future__ import annotations


def test_register_post_db_startup_hook_is_idempotent_by_name(monkeypatch):
    from mnemos.core import lifecycle

    monkeypatch.setattr(lifecycle, "_post_db_startup_hooks", {})

    async def first(_pool, _settings):
        return None

    async def second(_pool, _settings):
        return None

    lifecycle.register_post_db_startup_hook("federation nats consumers", first)
    lifecycle.register_post_db_startup_hook("federation nats consumers", second)

    # Second registration must REPLACE the first under the same name —
    # not append. Otherwise a module reload would queue duplicate
    # federation peer consumers / webhook trigger loops at next startup.
    assert list(lifecycle._post_db_startup_hooks.keys()) == [
        "federation nats consumers"
    ]
    assert lifecycle._post_db_startup_hooks["federation nats consumers"] is second


def test_register_post_db_startup_hook_keeps_distinct_names(monkeypatch):
    from mnemos.core import lifecycle

    monkeypatch.setattr(lifecycle, "_post_db_startup_hooks", {})

    async def federation_hook(_pool, _settings):
        return None

    async def webhook_hook(_pool, _settings):
        return None

    lifecycle.register_post_db_startup_hook("federation nats consumers", federation_hook)
    lifecycle.register_post_db_startup_hook("webhook nats trigger", webhook_hook)

    assert set(lifecycle._post_db_startup_hooks.keys()) == {
        "federation nats consumers",
        "webhook nats trigger",
    }


def test_federation_post_db_hook_threads_queue_group_to_consumer_loop(monkeypatch):
    """v4.2.0a8 round-2: regression guard for codex Finding 1.

    The lifecycle hook registered for federation NATS consumers MUST
    read ``settings.federation.nats_queue_group`` and pass it to each
    ``consumer_loop`` it schedules. Pre-fix the hook bypassed
    ``run_federation_nats_consumer`` and called ``consumer_loop(pool,
    peer)`` with no kwargs, so the env var was silently ignored in
    production startup despite being plumbed everywhere else.
    """
    import asyncio
    from types import SimpleNamespace

    from mnemos.api import lifecycle_hooks
    from mnemos.core import lifecycle
    from mnemos.federation import nats_consumer
    from mnemos.persistence.postgres import PostgresBackend
    from mnemos.workers import federation_memory_nats_consumer

    scheduled_kwargs: list[dict] = []
    scheduled_coros: list = []

    def fake_schedule_worker(coro):
        scheduled_coros.append(coro)
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    def fake_consumer_loop(pool, peer, **kwargs):
        scheduled_kwargs.append({"peer_name": peer.name, **kwargs})

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(lifecycle, "schedule_worker", fake_schedule_worker)
    monkeypatch.setattr(lifecycle, "_persistence_backend", object.__new__(PostgresBackend))
    monkeypatch.setattr(nats_consumer, "consumer_loop", fake_consumer_loop)
    direct_contract_scheduled = []

    async def fake_direct_consumers(pool, *, settings):
        direct_contract_scheduled.append((pool, settings))

    monkeypatch.setattr(federation_memory_nats_consumer, "run_configured_consumers", fake_direct_consumers)
    monkeypatch.setattr(
        nats_consumer,
        "configured_nats_peers",
        lambda settings: [
            nats_consumer.FederationNatsPeer(
                name="pythia",
                nats_url="nats://example:4222",
            )
        ],
    )

    settings = SimpleNamespace(
        nats=SimpleNamespace(node_name="argonas"),
        federation=SimpleNamespace(nats_queue_group="fed_pool", nats_peers=[object()]),
    )

    asyncio.run(lifecycle_hooks._federation_nats_post_db_hook(object(), settings))

    assert len(scheduled_kwargs) == 1
    assert scheduled_kwargs[0]["queue_group"] == "fed_pool", (
        "lifecycle hook must thread MNEMOS_FEDERATION_NATS_QUEUE_GROUP "
        "into consumer_loop or queue groups remain unwired in production"
    )
    assert direct_contract_scheduled == []
    assert len(scheduled_coros) == 2


def test_federation_post_db_hook_skips_direct_contract_on_every_non_postgres_backend(monkeypatch, caplog):
    import asyncio
    from types import SimpleNamespace

    from mnemos.api import lifecycle_hooks
    from mnemos.core import lifecycle
    from mnemos.federation import nats_consumer
    from mnemos.persistence.db2 import Db2Backend
    from mnemos.persistence.mysql import MysqlBackend
    from mnemos.persistence.oracle import OracleBackend
    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.workers import federation_memory_nats_consumer

    scheduled = []

    def fake_schedule_worker(coro):
        scheduled.append(coro)
        coro.close()

    async def fake_direct_consumers(pool, *, settings):
        raise AssertionError("direct consumer must not be scheduled for a non-Postgres backend")

    monkeypatch.setattr(lifecycle, "schedule_worker", fake_schedule_worker)
    monkeypatch.setattr(federation_memory_nats_consumer, "run_configured_consumers", fake_direct_consumers)
    monkeypatch.setattr(nats_consumer, "configured_nats_peers", lambda settings: [])

    settings = SimpleNamespace(
        nats=SimpleNamespace(url="nats://example:4222", node_name="argonas"),
        federation=SimpleNamespace(nats_queue_group="", nats_peers=[object()]),
    )
    for backend_type in (SqliteBackend, OracleBackend, Db2Backend, MysqlBackend):
        monkeypatch.setattr(lifecycle, "_persistence_backend", object.__new__(backend_type))
        asyncio.run(lifecycle_hooks._federation_nats_post_db_hook(object(), settings))

    assert scheduled == []
    for backend_name in ("SqliteBackend", "OracleBackend", "Db2Backend", "MysqlBackend"):
        assert f"unsupported on persistence backend {backend_name}" in caplog.text
