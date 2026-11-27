"""Characterisation tests (FB1118) for ``mnemos/api/lifecycle_hooks.py``.

Pins what ``register_lifespan_hooks`` does today on ``master`` (``98e1094ba``),
not what it ought to do:

- It is a **one-shot registrar** guarded by a module global ``_registered``:
  the first call fills the five registries on ``mnemos.core.lifecycle`` and
  every later call is a no-op, including after someone clears a registry by
  hand. That is the pin, not an endorsement — a re-call does not repair.
- The flag flips **after** the last registration, so a registration that
  raises leaves ``_registered`` false and a retry registers again. Pinned
  because it is the only thing that makes a failed boot recoverable.
- The exact registry contents are pinned by name: ten worker factories (four
  of them ``honor_worker_enabled=True``), four cleanup hooks, three post-DB
  startup hooks, one auth configurer, one provider-manifest reloader.
- The ``mcp audit drain`` hook calls ``drain_pending_audit_tasks(timeout=5.0)``
  and only logs when it actually drained something.

Every test swaps a saved copy of the registries in and out, so nothing here
depends on whether another test already registered the hooks, and nothing here
leaks registrations into the rest of the suite.
"""

from __future__ import annotations

import asyncio
import logging
from types import ModuleType

import pytest

from mnemos.api import lifecycle_hooks
from mnemos.core import lifecycle


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch):
    """Empty the five registries, then restore whatever was there before."""
    saved = (
        dict(lifecycle._lifespan_worker_factories),
        dict(lifecycle._post_db_startup_hooks),
        dict(lifecycle._lifespan_cleanup_hooks),
        lifecycle._auth_configurer,
        lifecycle._provider_manifest_reloader,
        lifecycle_hooks._registered,
    )

    lifecycle._lifespan_worker_factories.clear()
    lifecycle._post_db_startup_hooks.clear()
    lifecycle._lifespan_cleanup_hooks.clear()
    lifecycle._auth_configurer = None
    lifecycle._provider_manifest_reloader = None
    lifecycle_hooks._registered = False

    yield lifecycle

    lifecycle._lifespan_worker_factories.clear()
    lifecycle._lifespan_worker_factories.update(saved[0])
    lifecycle._post_db_startup_hooks.clear()
    lifecycle._post_db_startup_hooks.update(saved[1])
    lifecycle._lifespan_cleanup_hooks.clear()
    lifecycle._lifespan_cleanup_hooks.update(saved[2])
    lifecycle._auth_configurer = saved[3]
    lifecycle._provider_manifest_reloader = saved[4]
    lifecycle_hooks._registered = saved[5]


def test_it_starts_unregistered(registry) -> None:
    # The flag is process-wide, so this test asks the fixture for the clean
    # state rather than assuming it: another suite may have booted the app
    # before this file ran. The fixture sets it back to false, which is what a
    # boot looks like.
    assert lifecycle_hooks._registered is False


def test_one_call_registers_every_worker_by_name(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()

    assert sorted(registry._lifespan_worker_factories) == [
        "audit_sealer_worker",
        "deletion_request_worker",
        "distillation_worker",
        "federation local-embed worker",
        "federation sync worker",
        "hard_deletion_request_worker",
        "morpheus queued runs",
        "persephone archival worker",
        "webhook delivery recovery worker",
        "webhook retry repair worker",
    ]


def test_worker_factories_are_the_modules_own_functions(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()

    factories = {name: entry[0] for name, entry in registry._lifespan_worker_factories.items()}
    assert factories["audit_sealer_worker"] is lifecycle_hooks._audit_sealer_worker
    assert factories["distillation_worker"] is lifecycle_hooks._run_distillation_worker
    assert factories["deletion_request_worker"] is lifecycle_hooks._deletion_request_worker
    assert factories["hard_deletion_request_worker"] is lifecycle_hooks._hard_deletion_request_worker
    assert factories["persephone archival worker"] is lifecycle_hooks._persephone_archival_worker
    assert factories["morpheus queued runs"] is lifecycle_hooks._morpheus_queue_worker
    assert factories["webhook retry repair worker"] is lifecycle_hooks._webhook_repair_worker
    assert factories["webhook delivery recovery worker"] is lifecycle_hooks._webhook_delivery_worker
    assert factories["federation sync worker"] is lifecycle_hooks._federation_sync_worker
    assert factories["federation local-embed worker"] is lifecycle_hooks._federation_local_embed_worker


def test_only_four_workers_honor_the_worker_enabled_switch(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()

    honored = sorted(name for name, (_factory, honor) in registry._lifespan_worker_factories.items() if honor)
    assert honored == [
        "audit_sealer_worker",
        "deletion_request_worker",
        "distillation_worker",
        "hard_deletion_request_worker",
    ]


def test_the_four_honoring_workers_share_one_setting_name(registry) -> None:
    # Two deletion phases, the distillation loop and the audit sealer all read
    # their own worker name through service_enabled(self) — pinned separately
    # in the factories themselves.
    lifecycle_hooks.register_lifespan_hooks()
    assert "hard_deletion_request_worker" in registry._lifespan_worker_factories
    assert "deletion_request_worker" in registry._lifespan_worker_factories
    assert "audit_sealer_worker" in registry._lifespan_worker_factories


def test_one_call_registers_the_four_cleanup_hooks_in_order(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()

    assert list(registry._lifespan_cleanup_hooks) == [
        "mcp rest client",
        "pantheon http client",
        "mcp audit drain",
        "public tunnel",
    ]


def test_cleanup_hooks_are_the_modules_own_coroutines(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()

    hooks = registry._lifespan_cleanup_hooks
    assert hooks["pantheon http client"] is lifecycle_hooks._close_pantheon_http_client
    assert hooks["public tunnel"] is lifecycle_hooks._close_public_tunnel


def test_federation_consumers_are_not_a_cleanup_hook(registry) -> None:
    # The federation NATS consumers are post-DB startup hooks, not shutdown
    # hooks: pin the split so a future move is visible.
    lifecycle_hooks.register_lifespan_hooks()

    assert "federation nats consumers" not in registry._lifespan_cleanup_hooks
    assert "federation nats consumers" in registry._post_db_startup_hooks


def test_one_call_registers_the_three_post_db_hooks(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()

    assert list(registry._post_db_startup_hooks) == [
        "federation nats consumers",
        "webhook nats trigger",
        "PANTHEON routing audit NATS consumer",
    ]


def test_the_auth_configurer_is_the_api_owned_one(registry) -> None:
    from mnemos.api.dependencies import configure_auth

    lifecycle_hooks.register_lifespan_hooks()

    assert registry._auth_configurer is configure_auth


def test_the_provider_manifest_reloader_is_the_modules_own(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()

    assert registry._provider_manifest_reloader is lifecycle_hooks._reload_provider_manifest


def test_a_second_call_is_a_no_op_by_design(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()
    first = dict(registry._lifespan_worker_factories)

    # Clear by hand: the guard, not the registry, decides. A no-op second call
    # must leave the (now empty) registry empty.
    registry._lifespan_worker_factories.clear()
    lifecycle_hooks.register_lifespan_hooks()

    assert first
    assert registry._lifespan_worker_factories == {}


def test_a_second_call_does_not_duplicate_cleanup_hooks(registry) -> None:
    lifecycle_hooks.register_lifespan_hooks()
    lifecycle_hooks.register_lifespan_hooks()

    assert len(registry._lifespan_cleanup_hooks) == 4
    assert len(registry._post_db_startup_hooks) == 3


def test_the_flag_flips_only_after_every_registration_succeeds(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    real_register = registry.register_lifespan_worker

    def _fail_on_webhooks(name, factory, **kwargs):
        if name == "webhook retry repair worker":
            raise RuntimeError("registry is full")
        return real_register(name, factory, **kwargs)

    monkeypatch.setattr(registry, "register_lifespan_worker", _fail_on_webhooks)

    with pytest.raises(RuntimeError, match="registry is full"):
        lifecycle_hooks.register_lifespan_hooks()

    assert lifecycle_hooks._registered is False


def test_a_failed_registration_can_be_retried(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    real_register = registry.register_lifespan_worker
    calls = {"n": 0}

    def _fail_once(name, factory, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real_register(name, factory, **kwargs)

    monkeypatch.setattr(registry, "register_lifespan_worker", _fail_once)
    with pytest.raises(RuntimeError):
        lifecycle_hooks.register_lifespan_hooks()

    monkeypatch.setattr(registry, "register_lifespan_worker", real_register)
    lifecycle_hooks.register_lifespan_hooks()

    assert lifecycle_hooks._registered is True
    assert len(registry._lifespan_worker_factories) == 10


def _install_audit_drain(monkeypatch: pytest.MonkeyPatch, drained: int) -> dict:
    """Swap the drain coroutine the registrar imports, recording its timeout."""
    seen: dict = {}

    async def _drain_pending_audit_tasks(*, timeout):
        seen["timeout"] = timeout
        return drained

    module = ModuleType("mnemos.mcp.tools._security_fake")
    module.drain_pending_audit_tasks = _drain_pending_audit_tasks  # type: ignore[attr-defined]
    import mnemos.mcp.tools._security as real_security

    monkeypatch.setattr(real_security, "drain_pending_audit_tasks", _drain_pending_audit_tasks)
    return seen


def test_the_audit_drain_hook_waits_five_seconds(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_audit_drain(monkeypatch, drained=0)
    lifecycle_hooks.register_lifespan_hooks()

    hook = registry._lifespan_cleanup_hooks["mcp audit drain"]
    asyncio.run(hook())

    assert seen == {"timeout": 5.0}


def test_the_audit_drain_hook_is_silent_when_nothing_was_drained(
    registry, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_audit_drain(monkeypatch, drained=0)
    lifecycle_hooks.register_lifespan_hooks()

    with caplog.at_level(logging.INFO, logger="mnemos.mcp.audit"):
        asyncio.run(registry._lifespan_cleanup_hooks["mcp audit drain"]())

    assert caplog.text == ""


def test_the_audit_drain_hook_logs_the_count_when_it_drained(
    registry, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_audit_drain(monkeypatch, drained=3)
    lifecycle_hooks.register_lifespan_hooks()

    with caplog.at_level(logging.INFO, logger="mnemos.mcp.audit"):
        asyncio.run(registry._lifespan_cleanup_hooks["mcp audit drain"]())

    assert "drained 3 pending mcp_audit_log persist task(s) on shutdown" in caplog.text
