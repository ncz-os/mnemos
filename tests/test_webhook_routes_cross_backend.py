"""Route-level webhook CRUD parity across every persistence facade."""

from __future__ import annotations

import dataclasses
import importlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext
from mnemos.domain.models import WebhookCreateRequest
from mnemos.persistence.base import (
    WebhookDeliveryRecord,
    WebhookRepository,
    WebhookSubscriptionRecord,
)


_BACKENDS = (
    ("mnemos.persistence.postgres", "PostgresBackend", "_webhooks"),
    ("mnemos.persistence.sqlite", "SqliteBackend", "_webhooks"),
    ("mnemos.persistence.mysql", "MysqlBackend", "_webhooks_repo"),
    ("mnemos.persistence.mariadb", "MariadbBackend", "_webhooks_repo"),
    ("mnemos.persistence.oracle", "OracleBackend", "_webhooks_repo"),
    ("mnemos.persistence.db2", "Db2Backend", "_webhooks_repo"),
)


def _user() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="alice-ns",
        authenticated=True,
    )


class _RecordingWebhookRepository:
    """In-memory repository spy used through each real backend facade."""

    def __init__(self) -> None:
        self.subscriptions: dict[str, WebhookSubscriptionRecord] = {}
        self.deliveries: dict[str, list[WebhookDeliveryRecord]] = {}
        self.calls: list[str] = []

    @staticmethod
    def _visible(row, owner_id, namespace) -> bool:
        return owner_id is None or (
            row.owner_id == owner_id and row.namespace == namespace
        )

    async def create_subscription(self, _tx, **kwargs):
        self.calls.append("create_subscription")
        row = WebhookSubscriptionRecord(
            id=kwargs["subscription_id"],
            url=kwargs["url"],
            events=tuple(kwargs["events"]),
            description=kwargs["description"],
            owner_id=kwargs["owner_id"],
            namespace=kwargs["namespace"],
            created=datetime.now(timezone.utc),
            revoked=False,
            revoked_at=None,
        )
        self.subscriptions[row.id] = row
        return row

    async def list_subscriptions(self, _tx, **kwargs):
        self.calls.append("list_subscriptions")
        rows = [
            row
            for row in self.subscriptions.values()
            if self._visible(row, kwargs["owner_id"], kwargs["namespace"])
            and (kwargs["include_revoked"] or not row.revoked)
        ]
        return rows[: kwargs["limit"]]

    async def get_subscription(self, _tx, **kwargs):
        self.calls.append("get_subscription")
        row = self.subscriptions.get(kwargs["subscription_id"])
        if row is None or not self._visible(
            row,
            kwargs["owner_id"],
            kwargs["namespace"],
        ):
            return None
        return row

    async def revoke_subscription(self, _tx, **kwargs):
        self.calls.append("revoke_subscription")
        row = self.subscriptions.get(kwargs["subscription_id"])
        if (
            row is None
            or row.revoked
            or not self._visible(row, kwargs["owner_id"], kwargs["namespace"])
        ):
            return False
        self.subscriptions[row.id] = dataclasses.replace(
            row,
            revoked=True,
            revoked_at=datetime.now(timezone.utc),
        )
        return True

    async def list_deliveries(self, _tx, **kwargs):
        self.calls.append("list_deliveries")
        row = self.subscriptions.get(kwargs["subscription_id"])
        if row is None or not self._visible(
            row,
            kwargs["owner_id"],
            kwargs["namespace"],
        ):
            return []
        return self.deliveries.get(row.id, [])[: kwargs["limit"]]


def _delivery(subscription_id: str) -> WebhookDeliveryRecord:
    now = datetime.now(timezone.utc)
    return WebhookDeliveryRecord(
        id="delivery-1",
        subscription_id=subscription_id,
        event_type="memory.created",
        payload="{}",
        payload_hash="0" * 64,
        attempt_num=1,
        status="pending",
        response_status=None,
        response_body=None,
        error=None,
        scheduled_at=now,
        delivered_at=None,
        created=now,
        status_updated_at=now,
        superseded=False,
        lease_token=None,
        lease_expires_at=None,
        writer_revision=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name,backend_name,repo_attr", _BACKENDS)
async def test_all_five_routes_use_each_backend_facade(
    module_name,
    backend_name,
    repo_attr,
    monkeypatch,
):
    """Drive every route through each concrete facade's ``webhooks`` accessor."""
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.skip(f"optional backend driver unavailable: {exc.name}")
    backend_type = getattr(module, backend_name)
    backend = object.__new__(backend_type)
    repository = _RecordingWebhookRepository()
    setattr(backend, repo_attr, repository)

    @asynccontextmanager
    async def _transactional(_self, **_kwargs):
        yield SimpleNamespace(conn=None)

    monkeypatch.setattr(backend_type, "transactional", _transactional)
    monkeypatch.setattr(_lc, "_persistence_backend", backend)
    monkeypatch.setattr(_lc, "_pool", None)
    monkeypatch.setattr(_lc, "_rls_enabled", False)

    from mnemos.api.routes import webhooks as routes
    import mnemos.nats
    import mnemos.nats.client

    monkeypatch.setattr(routes, "_validate_url", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(mnemos.nats, "publish_event", publish)
    monkeypatch.setattr(mnemos.nats.client, "get_node_name", lambda: "test-node")

    created = await routes.create_webhook(
        WebhookCreateRequest(
            url="https://example.com/hook",
            events=["memory.created"],
            namespace="alice-ns",
        ),
        user=_user(),
    )
    listed = await routes.list_webhooks(user=_user(), include_revoked=False)
    fetched = await routes.get_webhook(created.id, user=_user())

    repository.deliveries[created.id] = [_delivery(created.id)]
    deliveries = await routes.list_deliveries(created.id, user=_user(), limit=50)
    await routes.revoke_webhook(created.id, user=_user())

    assert created.secret
    assert listed.count == 1
    assert fetched.id == created.id
    assert deliveries.count == 1
    assert repository.subscriptions[created.id].revoked is True
    assert set(repository.calls) >= {
        "create_subscription",
        "list_subscriptions",
        "get_subscription",
        "list_deliveries",
        "revoke_subscription",
    }
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_five_routes_round_trip_on_live_sqlite(tmp_path, monkeypatch):
    """Use the real SQLite schema, transaction manager, and repository methods."""
    from mnemos.api.routes import webhooks as routes
    from mnemos.persistence.sqlite import SqliteBackend
    import mnemos.nats
    import mnemos.nats.client

    backend = SqliteBackend(
        tmp_path / "webhook-routes.db",
        SimpleNamespace(database=SimpleNamespace(embedding_dim=3)),
    )
    await backend.open()
    monkeypatch.setattr(_lc, "_persistence_backend", backend)
    monkeypatch.setattr(_lc, "_pool", None)
    monkeypatch.setattr(_lc, "_rls_enabled", False)
    monkeypatch.setattr(routes, "_validate_url", AsyncMock())
    monkeypatch.setattr(mnemos.nats, "publish_event", AsyncMock())
    monkeypatch.setattr(mnemos.nats.client, "get_node_name", lambda: "test-node")
    try:
        created = await routes.create_webhook(
            WebhookCreateRequest(
                url="https://example.com/sqlite-hook",
                events=["memory.created"],
            ),
            user=_user(),
        )
        listed = await routes.list_webhooks(user=_user(), include_revoked=False)
        fetched = await routes.get_webhook(created.id, user=_user())

        async with backend.transactional() as tx:
            await backend.webhooks.dispatch_event(
                tx,
                "memory.created",
                {"memory_id": "mem_test"},
                owner_id="alice",
                namespace="alice-ns",
            )
        deliveries = await routes.list_deliveries(created.id, user=_user(), limit=50)
        await routes.revoke_webhook(created.id, user=_user())
        active = await routes.list_webhooks(user=_user(), include_revoked=False)
        all_rows = await routes.list_webhooks(user=_user(), include_revoked=True)

        assert listed.count == 1
        assert fetched.id == created.id
        assert deliveries.count == 1
        assert active.count == 0
        assert all_rows.count == 1
        assert all_rows.webhooks[0].revoked is True
    finally:
        await backend.close()


def test_every_concrete_repository_implements_route_contract():
    implementations = (
        ("mnemos.persistence.postgres", "PostgresWebhookRepository"),
        ("mnemos.persistence.sqlite", "SqliteWebhookRepository"),
        ("mnemos.persistence.mysql", "MysqlWebhookRepository"),
        ("mnemos.persistence.mariadb", "MariadbWebhookRepository"),
        ("mnemos.persistence.oracle", "OracleWebhookRepository"),
        ("mnemos.persistence.db2", "Db2WebhookRepository"),
    )
    methods = (
        "create_subscription",
        "list_subscriptions",
        "get_subscription",
        "revoke_subscription",
        "list_deliveries",
    )
    for module_name, class_name in implementations:
        try:
            repository_type = getattr(importlib.import_module(module_name), class_name)
        except ModuleNotFoundError as exc:
            pytest.skip(f"optional backend driver unavailable: {exc.name}")
        for method in methods:
            assert getattr(repository_type, method) is not getattr(
                WebhookRepository,
                method,
            )


def test_routes_cannot_reacquire_postgres_pool_gate():
    import ast
    import inspect
    import textwrap

    from mnemos.api.routes import webhooks as routes

    for handler in (
        routes.create_webhook,
        routes.list_webhooks,
        routes.get_webhook,
        routes.revoke_webhook,
        routes.list_deliveries,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "require_postgres_pool_or_503" not in calls
        assert "require_webhooks_backend" in calls
