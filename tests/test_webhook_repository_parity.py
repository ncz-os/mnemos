"""Driver-free parity tests for WebhookRepository 5-method expansion (RA-0b).

Each backend's methods are exercised with mocked cursor/connection objects.
No real database connection is opened. Coverage: ~25 tests across all 4 backends.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mnemos.persistence.base import Transaction, WebhookRepository
from mnemos.persistence.postgres import PostgresWebhookRepository
from mnemos.persistence.oracle import OracleWebhookRepository
from mnemos.persistence.sqlite import SqliteWebhookRepository
from mnemos.persistence.db2 import Db2WebhookRepository

# ── Test data ─────────────────────────────────────────────────────────────────

NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
WEBHOOK_ID = "aaaaaaaa-1111-4aaa-aaaa-aaaaaaaaaaaa"
OWNER_ID = "owner-1"
NAMESPACE = "test-ns"
URL = "https://example.com/webhook"
EVENTS = ["memory.created", "consultation.completed"]
SECRET = "secret-token"
DESCRIPTION = "Test webhook"


def _mock_tx() -> Transaction:
    """Create a Transaction stub (no-op commit/rollback)."""
    tx = MagicMock(spec=Transaction)
    tx.commit = AsyncMock()
    tx.rollback = AsyncMock()
    return tx


# ──────────────────────────────────────────────────────────────────────────────
# PostgresWebhookRepository (driver-free, mock-asyncpg-cursor)
# ──────────────────────────────────────────────────────────────────────────────


def _make_pg_row_dict(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {
        "id": WEBHOOK_ID,
        "url": URL,
        "events": list(EVENTS),
        "description": DESCRIPTION,
        "owner_id": OWNER_ID,
        "namespace": NAMESPACE,
        "created": NOW,
        "revoked": False,
    }
    if overrides:
        base.update(overrides)
    return base


def _make_pg_delivery_row_dict(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {
        "id": str(uuid.uuid4()),
        "subscription_id": WEBHOOK_ID,
        "event_type": "memory.created",
        "attempt_num": 1,
        "status": "pending",
        "superseded": False,
        "response_status": None,
        "response_body": None,
        "error": None,
        "scheduled_at": NOW,
        "delivered_at": None,
        "created": NOW,
    }
    if overrides:
        base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_postgres_create_webhook_subscription() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    expected = _make_pg_row_dict()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=expected)
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        result = await repo.create_webhook_subscription(
            tx,
            url=URL,
            events=EVENTS,
            secret=SECRET,
            description=DESCRIPTION,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
        )
    assert result["id"] == WEBHOOK_ID
    assert result["url"] == URL
    assert result["description"] == DESCRIPTION
    assert result["revoked"] is False
    mock_conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_postgres_list_webhooks_root_include_revoked() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    expected = [_make_pg_row_dict()]
    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(return_value=expected)
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        result = await repo.list_webhooks(
            tx,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=True,
            include_revoked=True,
        )
    assert len(result) == 1
    assert result[0]["id"] == WEBHOOK_ID
    called_sql = mock_conn.fetch.call_args[0][0]
    assert "NOT revoked" not in called_sql


@pytest.mark.asyncio
async def test_postgres_list_webhooks_user_active_only() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    expected = [_make_pg_row_dict()]
    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(return_value=expected)
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        result = await repo.list_webhooks(
            tx,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=False,
            include_revoked=False,
        )
    assert len(result) == 1
    called_sql = mock_conn.fetch.call_args[0][0]
    assert "NOT revoked" in called_sql
    assert "owner_id = $1" in called_sql


@pytest.mark.asyncio
async def test_postgres_get_webhook_root_found() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    expected = _make_pg_row_dict()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=expected)
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        result = await repo.get_webhook(
            tx,
            webhook_id=WEBHOOK_ID,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=True,
        )
    assert result is not None
    assert result["id"] == WEBHOOK_ID
    called_sql = mock_conn.fetchrow.call_args[0][0]
    assert ":uuid" in called_sql


@pytest.mark.asyncio
async def test_postgres_get_webhook_user_not_found() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        result = await repo.get_webhook(
            tx,
            webhook_id="nonexistent",
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=False,
        )
    assert result is None


@pytest.mark.asyncio
async def test_postgres_revoke_webhook_user_success() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value={"id": WEBHOOK_ID})
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        result = await repo.revoke_webhook(
            tx,
            webhook_id=WEBHOOK_ID,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=False,
        )
    assert result == WEBHOOK_ID
    called_sql = mock_conn.fetchrow.call_args[0][0]
    assert "SET revoked = TRUE" in called_sql
    assert "AND NOT revoked" in called_sql


@pytest.mark.asyncio
async def test_postgres_revoke_webhook_not_found() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        result = await repo.revoke_webhook(
            tx,
            webhook_id="nonexistent",
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=True,
        )
    assert result is None


@pytest.mark.asyncio
async def test_postgres_list_deliveries_sub_exists() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value={"id": WEBHOOK_ID})
    delivery = _make_pg_delivery_row_dict()
    mock_conn.fetch = AsyncMock(return_value=[delivery])
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        sub_exists, rows = await repo.list_deliveries(
            tx,
            webhook_id=WEBHOOK_ID,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=False,
            limit=50,
        )
    assert sub_exists is True
    assert len(rows) == 1
    assert rows[0]["subscription_id"] == WEBHOOK_ID


@pytest.mark.asyncio
async def test_postgres_list_deliveries_sub_not_found() -> None:
    repo = PostgresWebhookRepository()
    tx = _mock_tx()
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    with patch("mnemos.persistence.postgres._postgres_tx", return_value=MagicMock(conn=mock_conn)):
        sub_exists, rows = await repo.list_deliveries(
            tx,
            webhook_id="nonexistent",
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=True,
            limit=50,
        )
    assert sub_exists is False
    assert rows == []


# ──────────────────────────────────────────────────────────────────────────────
# OracleWebhookRepository (driver-free, mock-oracledb-cursor)
# ──────────────────────────────────────────────────────────────────────────────


def _make_ora_row_from_cols(cursor: MagicMock, values: dict[str, Any]) -> MagicMock:
    """Build a mock cursor with .description and .fetchone for _row_to_dict."""
    row = MagicMock()
    row.__iter__.return_value = iter(values.values())
    return row


@pytest.mark.asyncio
async def test_oracle_create_webhook_subscription() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone = AsyncMock(
        return_value=_make_ora_row_from_cols(
            mock_cursor,
            {
                "id": WEBHOOK_ID,
                "url": URL,
                "events": json.dumps(EVENTS),
                "description": DESCRIPTION,
                "owner_id": OWNER_ID,
                "namespace": NAMESPACE,
                "created": NOW,
                "revoked": 0,
            },
        )
    )
    mock_cursor.description = [
        ("ID",),
        ("URL",),
        ("EVENTS",),
        ("DESCRIPTION",),
        ("OWNER_ID",),
        ("NAMESPACE",),
        ("CREATED",),
        ("REVOKED",),
    ]

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        result = await repo.create_webhook_subscription(
            tx,
            url=URL,
            events=EVENTS,
            secret=SECRET,
            description=DESCRIPTION,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
        )
    assert result is not None
    # Oracle stores events as CLOB JSON string; _materialize reads it
    # Verify the INSERT was called
    assert mock_cursor.execute.call_count >= 1


@pytest.mark.asyncio
async def test_oracle_list_webhooks_root_all() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchall = AsyncMock(
        return_value=[
            _make_ora_row_from_cols(
                mock_cursor,
                {
                    "id": WEBHOOK_ID,
                    "url": URL,
                    "events": json.dumps(EVENTS),
                    "description": DESCRIPTION,
                    "owner_id": OWNER_ID,
                    "namespace": NAMESPACE,
                    "created": NOW,
                    "revoked": 0,
                    "revoked_at": None,
                },
            )
        ]
    )
    mock_cursor.description = [
        ("ID",),
        ("URL",),
        ("EVENTS",),
        ("DESCRIPTION",),
        ("OWNER_ID",),
        ("NAMESPACE",),
        ("CREATED",),
        ("REVOKED",),
        ("REVOKED_AT",),
    ]

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        result = await repo.list_webhooks(
            tx,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=True,
            include_revoked=True,
        )
    assert len(result) == 1
    # Root + include_revoked → no "revoked = 0" clause
    called_sql = mock_cursor.execute.call_args[0][0]
    assert "revoked = 0" not in called_sql


@pytest.mark.asyncio
async def test_oracle_list_webhooks_user_active() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchall = AsyncMock(return_value=[])
    mock_cursor.description = [
        ("ID",),
        ("URL",),
        ("EVENTS",),
        ("DESCRIPTION",),
        ("OWNER_ID",),
        ("NAMESPACE",),
        ("CREATED",),
        ("REVOKED",),
        ("REVOKED_AT",),
    ]

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        await repo.list_webhooks(
            tx,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=False,
            include_revoked=False,
        )
    called_sql = mock_cursor.execute.call_args[0][0]
    assert "revoked = 0" in called_sql
    assert "owner_id = :owner_id" in called_sql


@pytest.mark.asyncio
async def test_oracle_get_webhook_user_found() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone = AsyncMock(
        return_value=_make_ora_row_from_cols(
            mock_cursor,
            {
                "id": WEBHOOK_ID,
                "url": URL,
                "events": json.dumps(EVENTS),
                "description": DESCRIPTION,
                "owner_id": OWNER_ID,
                "namespace": NAMESPACE,
                "created": NOW,
                "revoked": 0,
                "revoked_at": None,
            },
        )
    )
    mock_cursor.description = [
        ("ID",),
        ("URL",),
        ("EVENTS",),
        ("DESCRIPTION",),
        ("OWNER_ID",),
        ("NAMESPACE",),
        ("CREATED",),
        ("REVOKED",),
        ("REVOKED_AT",),
    ]

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        result = await repo.get_webhook(
            tx,
            webhook_id=WEBHOOK_ID,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=False,
        )
    assert result is not None
    assert result["id"] == WEBHOOK_ID


@pytest.mark.asyncio
async def test_oracle_get_webhook_not_found() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=None)
    mock_cursor.description = []

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        result = await repo.get_webhook(
            tx,
            webhook_id="nonexistent",
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=True,
        )
    assert result is None


@pytest.mark.asyncio
async def test_oracle_revoke_webhook_root_success() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.rowcount = 1

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        result = await repo.revoke_webhook(
            tx,
            webhook_id=WEBHOOK_ID,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=True,
        )
    assert result == WEBHOOK_ID
    called_sql = mock_cursor.execute.call_args[0][0]
    assert "revoked = 1" in called_sql
    assert "SYSTIMESTAMP" in called_sql


@pytest.mark.asyncio
async def test_oracle_revoke_webhook_none_affected() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.rowcount = 0

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        result = await repo.revoke_webhook(
            tx,
            webhook_id="nonexistent",
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=False,
        )
    assert result is None


@pytest.mark.asyncio
async def test_oracle_list_deliveries_sub_exists() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    # First fetchone for sub check
    mock_cursor.fetchone = AsyncMock(return_value=["some-id"])
    # fetchall for deliveries
    delivery_row = _make_ora_row_from_cols(
        mock_cursor,
        {
            "id": str(uuid.uuid4()),
            "subscription_id": WEBHOOK_ID,
            "event_type": "memory.created",
            "attempt_num": 1,
            "status": "pending",
            "superseded": None,
            "response_status": None,
            "response_body": None,
            "error": None,
            "scheduled_at": NOW,
            "delivered_at": None,
            "created": NOW,
        },
    )
    mock_cursor.fetchall = AsyncMock(return_value=[delivery_row])
    mock_cursor.description = [
        ("ID",),
        ("SUBSCRIPTION_ID",),
        ("EVENT_TYPE",),
        ("ATTEMPT_NUM",),
        ("STATUS",),
        ("SUPERSEDED",),
        ("RESPONSE_STATUS",),
        ("RESPONSE_BODY",),
        ("ERROR",),
        ("SCHEDULED_AT",),
        ("DELIVERED_AT",),
        ("CREATED",),
    ]

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        sub_exists, rows = await repo.list_deliveries(
            tx,
            webhook_id=WEBHOOK_ID,
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=False,
            limit=50,
        )
    assert sub_exists is True
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_oracle_list_deliveries_sub_not_found() -> None:
    repo = OracleWebhookRepository()
    tx = _mock_tx()

    mock_cursor = MagicMock()
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=None)

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    with patch("mnemos.persistence.oracle._conn_from_tx", return_value=mock_conn):
        sub_exists, rows = await repo.list_deliveries(
            tx,
            webhook_id="nonexistent",
            owner_id=OWNER_ID,
            namespace=NAMESPACE,
            is_root=True,
            limit=50,
        )
    assert sub_exists is False
    assert rows == []


# ──────────────────────────────────────────────────────────────────────────────
# SqliteWebhookRepository (driver-free, mock-aiosqlite-cursor via _conn)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_create_webhook_subscription() -> None:
    repo = SqliteWebhookRepository()
    tx = _mock_tx()
    expected = {
        "id": WEBHOOK_ID,
        "url": URL,
        "events": json.dumps(EVENTS),
        "description": DESCRIPTION,
        "owner_id": OWNER_ID,
        "namespace": NAMESPACE,
        "created": "2026-05-22 12:00:00",
        "revoked": 0,
    }

    mock_conn = MagicMock()
    with patch("mnemos.persistence.sqlite._sqlite_tx", return_value=MagicMock(conn=mock_conn)):
        with patch("mnemos.persistence.sqlite._fetch_one", return_value=expected) as mock_fetch:
            result = await repo.create_webhook_subscription(
                tx,
                url=URL,
                events=EVENTS,
                secret=SECRET,
                description=DESCRIPTION,
                owner_id=OWNER_ID,
                namespace=NAMESPACE,
            )
    assert result["id"] == WEBHOOK_ID
    assert result["description"] == DESCRIPTION
    called_sql = mock_fetch.call_args[0][1]
    assert "INSERT INTO webhook_subscriptions" in called_sql
    assert "RETURNING" in called_sql


@pytest.mark.asyncio
async def test_sqlite_list_webhooks_user_active() -> None:
    repo = SqliteWebhookRepository()
    tx = _mock_tx()
    expected = [
        {
            "id": WEBHOOK_ID,
            "url": URL,
            "events": json.dumps(EVENTS),
            "description": DESCRIPTION,
            "owner_id": OWNER_ID,
            "namespace": NAMESPACE,
            "created": "2026-05-22 12:00:00",
            "revoked": 0,
            "revoked_at": None,
        }
    ]

    mock_conn = MagicMock()
    with patch("mnemos.persistence.sqlite._sqlite_tx", return_value=MagicMock(conn=mock_conn)):
        with patch("mnemos.persistence.sqlite._fetch_all", return_value=expected) as mock_fetch:
            result = await repo.list_webhooks(
                tx,
                owner_id=OWNER_ID,
                namespace=NAMESPACE,
                is_root=False,
                include_revoked=False,
            )
    assert len(result) == 1
    called_sql = mock_fetch.call_args[0][1]
    assert "revoked = 0" in called_sql


@pytest.mark.asyncio
async def test_sqlite_get_webhook_root_found() -> None:
    repo = SqliteWebhookRepository()
    tx = _mock_tx()
    expected = {
        "id": WEBHOOK_ID,
        "url": URL,
        "events": json.dumps(EVENTS),
        "description": DESCRIPTION,
        "owner_id": OWNER_ID,
        "namespace": NAMESPACE,
        "created": "2026-05-22 12:00:00",
        "revoked": 0,
        "revoked_at": None,
    }

    mock_conn = MagicMock()
    with patch("mnemos.persistence.sqlite._sqlite_tx", return_value=MagicMock(conn=mock_conn)):
        with patch("mnemos.persistence.sqlite._fetch_one", return_value=expected):
            result = await repo.get_webhook(
                tx,
                webhook_id=WEBHOOK_ID,
                owner_id=OWNER_ID,
                namespace=NAMESPACE,
                is_root=True,
            )
    assert result is not None
    assert result["id"] == WEBHOOK_ID


@pytest.mark.asyncio
async def test_sqlite_get_webhook_not_found() -> None:
    repo = SqliteWebhookRepository()
    tx = _mock_tx()

    mock_conn = MagicMock()
    with patch("mnemos.persistence.sqlite._sqlite_tx", return_value=MagicMock(conn=mock_conn)):
        with patch("mnemos.persistence.sqlite._fetch_one", return_value=None):
            result = await repo.get_webhook(
                tx,
                webhook_id="nonexistent",
                owner_id=OWNER_ID,
                namespace=NAMESPACE,
                is_root=False,
            )
    assert result is None


@pytest.mark.asyncio
async def test_sqlite_revoke_webhook_user_success() -> None:
    repo = SqliteWebhookRepository()
    tx = _mock_tx()
    expected = {"id": WEBHOOK_ID}

    mock_conn = MagicMock()
    with patch("mnemos.persistence.sqlite._sqlite_tx", return_value=MagicMock(conn=mock_conn)):
        with patch("mnemos.persistence.sqlite._fetch_one", return_value=expected) as mock_fetch:
            result = await repo.revoke_webhook(
                tx,
                webhook_id=WEBHOOK_ID,
                owner_id=OWNER_ID,
                namespace=NAMESPACE,
                is_root=False,
            )
    assert result == WEBHOOK_ID
    called_sql = mock_fetch.call_args[0][1]
    assert "revoked = 1" in called_sql
    assert "CURRENT_TIMESTAMP" in called_sql


@pytest.mark.asyncio
async def test_sqlite_revoke_webhook_none_affected() -> None:
    repo = SqliteWebhookRepository()
    tx = _mock_tx()

    mock_conn = MagicMock()
    with patch("mnemos.persistence.sqlite._sqlite_tx", return_value=MagicMock(conn=mock_conn)):
        with patch("mnemos.persistence.sqlite._fetch_one", return_value=None):
            result = await repo.revoke_webhook(
                tx,
                webhook_id="nonexistent",
                owner_id=OWNER_ID,
                namespace=NAMESPACE,
                is_root=True,
            )
    assert result is None


@pytest.mark.asyncio
async def test_sqlite_list_deliveries_sub_exists() -> None:
    repo = SqliteWebhookRepository()
    tx = _mock_tx()
    sub = {"id": WEBHOOK_ID}
    deliveries = [{"id": str(uuid.uuid4()), "subscription_id": WEBHOOK_ID}]

    mock_conn = MagicMock()
    with patch("mnemos.persistence.sqlite._sqlite_tx", return_value=MagicMock(conn=mock_conn)):
        with patch("mnemos.persistence.sqlite._fetch_one", return_value=sub):
            with patch("mnemos.persistence.sqlite._fetch_all", return_value=deliveries):
                sub_exists, rows = await repo.list_deliveries(
                    tx,
                    webhook_id=WEBHOOK_ID,
                    owner_id=OWNER_ID,
                    namespace=NAMESPACE,
                    is_root=False,
                    limit=50,
                )
    assert sub_exists is True
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_sqlite_list_deliveries_sub_not_found() -> None:
    repo = SqliteWebhookRepository()
    tx = _mock_tx()

    mock_conn = MagicMock()
    with patch("mnemos.persistence.sqlite._sqlite_tx", return_value=MagicMock(conn=mock_conn)):
        with patch("mnemos.persistence.sqlite._fetch_one", return_value=None):
            sub_exists, rows = await repo.list_deliveries(
                tx,
                webhook_id="nonexistent",
                owner_id=OWNER_ID,
                namespace=NAMESPACE,
                is_root=True,
                limit=50,
            )
    assert sub_exists is False
    assert rows == []


# ──────────────────────────────────────────────────────────────────────────────
# Db2WebhookRepository — inherits all 5 methods from OracleWebhookRepository
# ──────────────────────────────────────────────────────────────────────────────


def test_db2_webhook_repo_inherits_all_abstract_methods() -> None:
    """Verify Db2WebhookRepository has all 5 new methods via Oracle inheritance."""
    repo = Db2WebhookRepository()
    assert hasattr(repo, "create_webhook_subscription")
    assert hasattr(repo, "list_webhooks")
    assert hasattr(repo, "get_webhook")
    assert hasattr(repo, "revoke_webhook")
    assert hasattr(repo, "list_deliveries")
    assert hasattr(repo, "dispatch_event")
    # Verify they are callable (not abstract placeholders)
    assert callable(repo.create_webhook_subscription)
    assert callable(repo.list_webhooks)
    assert callable(repo.get_webhook)
    assert callable(repo.revoke_webhook)
    assert callable(repo.list_deliveries)


def test_all_backend_repos_are_subclass_of_webhook_repo() -> None:
    """Webhook repository type hierarchy check."""
    assert issubclass(PostgresWebhookRepository, WebhookRepository)
    assert issubclass(OracleWebhookRepository, WebhookRepository)
    assert issubclass(SqliteWebhookRepository, WebhookRepository)
    assert issubclass(Db2WebhookRepository, WebhookRepository)
    # Db2 also inherits Oracle
    assert issubclass(Db2WebhookRepository, OracleWebhookRepository)
