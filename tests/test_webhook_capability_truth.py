"""Fail-closed capability checks for the Postgres-era webhook worker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_postgres_claims_end_to_end_webhook_delivery() -> None:
    from mnemos.persistence.base import WEBHOOKS_CAPABILITY, capability_details_for_backend
    from mnemos.persistence.postgres import PostgresBackend

    backend = object.__new__(PostgresBackend)

    assert backend.supports_webhooks is True
    assert WEBHOOKS_CAPABILITY in capability_details_for_backend(backend)


def test_capability_details_hide_webhooks_for_unknown_backend() -> None:
    from mnemos.persistence.base import WEBHOOKS_CAPABILITY, capability_details_for_backend

    backend = SimpleNamespace(
        capability_details={"memory_crud", WEBHOOKS_CAPABILITY},
    )

    assert capability_details_for_backend(backend) == {"memory_crud"}


def test_sqlite_does_not_claim_webhook_delivery() -> None:
    from mnemos.persistence.base import (
        BackendCapabilityMissing,
        WEBHOOKS_CAPABILITY,
        capability_details_for_backend,
    )
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(":memory:", SimpleNamespace())

    assert backend.supports_webhooks is False
    assert WEBHOOKS_CAPABILITY not in capability_details_for_backend(backend)
    with pytest.raises(BackendCapabilityMissing, match="webhooks"):
        _ = backend.webhooks


@pytest.mark.parametrize(
    "module_name,backend_name",
    [
        ("mnemos.persistence.oracle", "OracleBackend"),
        ("mnemos.persistence.db2", "Db2Backend"),
    ],
)
def test_enterprise_non_postgres_backends_do_not_claim_webhook_delivery(
    module_name: str,
    backend_name: str,
) -> None:
    import importlib

    from mnemos.persistence.base import (
        BackendCapabilityMissing,
        WEBHOOKS_CAPABILITY,
        capability_details_for_backend,
    )

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.skip(f"optional backend driver unavailable: {exc.name}")
    backend_type = getattr(module, backend_name)
    backend = object.__new__(backend_type)

    assert backend.supports_webhooks is False
    assert WEBHOOKS_CAPABILITY not in capability_details_for_backend(backend)
    with pytest.raises(BackendCapabilityMissing, match="webhooks"):
        _ = backend.webhooks


@pytest.mark.parametrize(
    "module_name,backend_name",
    [
        ("mnemos.persistence.mysql", "MysqlBackend"),
        ("mnemos.persistence.mariadb", "MariadbBackend"),
    ],
)
def test_mysql_family_webhook_accessor_fails_loud(module_name: str, backend_name: str) -> None:
    import importlib

    from mnemos.persistence.base import (
        BackendCapabilityMissing,
        WEBHOOKS_CAPABILITY,
        capability_details_for_backend,
    )

    module = importlib.import_module(module_name)
    backend = object.__new__(getattr(module, backend_name))

    assert backend.supports_webhooks is False
    assert WEBHOOKS_CAPABILITY not in capability_details_for_backend(backend)
    with pytest.raises(BackendCapabilityMissing, match="webhooks"):
        _ = backend.webhooks


@pytest.mark.asyncio
async def test_legacy_dispatch_without_postgres_pool_fails_loud(monkeypatch) -> None:
    from mnemos.core import lifecycle
    from mnemos.persistence.base import BackendCapabilityMissing
    from mnemos.webhooks.dispatcher import dispatch

    backend = SimpleNamespace()
    monkeypatch.setattr(lifecycle, "_pool", None)
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)

    with pytest.raises(BackendCapabilityMissing, match="webhooks") as exc:
        await dispatch("memory.created", {"memory_id": "mem_test"})
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_delivery_attempt_without_supported_handle_fails_loud(monkeypatch) -> None:
    from mnemos.core import lifecycle
    from mnemos.webhooks.sender import _attempt_delivery

    monkeypatch.setattr(lifecycle, "_pool", None)

    with pytest.raises(RuntimeError, match="cannot run without a supported persistence handle"):
        await _attempt_delivery("delivery_test")


@pytest.mark.asyncio
@pytest.mark.parametrize("nats_url", ["", "nats://example:4222"])
async def test_webhook_nats_trigger_refuses_unsupported_backend(monkeypatch, caplog, nats_url) -> None:
    from mnemos.api import lifecycle_hooks
    from mnemos.core import lifecycle

    backend = SimpleNamespace(supports_webhooks=False)
    scheduled: list[object] = []
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle_hooks, "service_enabled", lambda *_args: True)
    monkeypatch.setattr(lifecycle, "schedule_worker", scheduled.append)

    settings = SimpleNamespace(nats=SimpleNamespace(url=nats_url))
    await lifecycle_hooks._webhook_nats_post_db_hook(backend, settings)

    assert scheduled == []
    assert "webhook delivery unavailable" in caplog.text
    assert "webhook workers will not start" in caplog.text
