"""Oracle webhook subscription SQL must match the shipped Oracle schema.

The live Oracle integration suite is opt-in.  This always-on guard derives the
subscription columns from the real Oracle migration files and compares the
actual SQL emitted by ``OracleWebhookRepository`` with that exact contract.
It prevents a PostgreSQL-shaped test fixture from masking Oracle column drift.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mnemos.persistence.oracle import OracleWebhookRepository, oracle_webhook_delivery_select_clause


_ROOT = Path(__file__).resolve().parents[1]
_ORACLE_MIGRATIONS = _ROOT / "mnemos" / "db_migrations" / "migrations_oracle"


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


def _subscription_columns_from_migrations() -> set[str]:
    core = (_ORACLE_MIGRATIONS / "0001_core_schema.sql").read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS webhook_subscriptions\s*\((.*?)\n\);",
        core,
        flags=re.DOTALL | re.IGNORECASE,
    )
    assert match is not None
    columns = {
        line.strip().split()[0].lower()
        for line in match.group(1).splitlines()
        if line.strip() and not line.lstrip().upper().startswith("CONSTRAINT ")
    }
    for path in sorted(_ORACLE_MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        columns.update(
            column.lower()
            for column in re.findall(
                r"ALTER TABLE webhook_subscriptions\s+ADD\s*\(\s*([a-z_][a-z0-9_]*)",
                sql,
                flags=re.IGNORECASE,
            )
        )
    return columns


def _delivery_columns_from_migrations() -> set[str]:
    core = (_ORACLE_MIGRATIONS / "0001_core_schema.sql").read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS webhook_deliveries\s*\((.*?)\n\);",
        core,
        flags=re.DOTALL | re.IGNORECASE,
    )
    assert match is not None
    columns = {
        line.strip().split()[0].lower()
        for line in match.group(1).splitlines()
        if line.strip() and not line.lstrip().upper().startswith("CONSTRAINT ")
    }
    for path in sorted(_ORACLE_MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        columns.update(
            column.lower()
            for column in re.findall(
                r"ALTER TABLE webhook_deliveries\s+ADD\s*\(\s*([a-z_][a-z0-9_]*)",
                sql,
                flags=re.IGNORECASE,
            )
        )
    return columns


class _OutVar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def getvalue(self) -> Any:
        return [self._value]


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._var_count = 0
        self.description: tuple[Any, ...] = ()

    def var(self, _type: Any) -> _OutVar:
        self._var_count += 1
        value: Any = "subscription-id"
        if self._var_count == 2:
            value = datetime(2026, 9, 18, tzinfo=timezone.utc)
        return _OutVar(value)

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.calls.append((_normalize_sql(sql), params))

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> None:
        return None

    def close(self) -> None:
        return None


class _RecordingConnection:
    def __init__(self) -> None:
        self.cursors: list[_RecordingCursor] = []

    def cursor(self) -> _RecordingCursor:
        cursor = _RecordingCursor()
        self.cursors.append(cursor)
        return cursor


@pytest.mark.asyncio
async def test_subscription_sql_matches_oracle_migration_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_columns = {
        "id",
        "url",
        "events",
        "secret",
        "description",
        "owner_id",
        "namespace",
        "revoked",
        "revoked_at",
        "created_at",
    }
    assert _subscription_columns_from_migrations() == expected_columns

    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(DB_TYPE_TIMESTAMP_TZ=object()))
    conn = _RecordingConnection()
    tx = SimpleNamespace(conn=conn)
    repo = OracleWebhookRepository()

    await repo.create_subscription(
        tx,
        subscription_id="subscription-id",
        url="https://example.com/hook",
        events=("memory.created",),
        secret="secret",
        description="description",
        owner_id="owner",
        namespace="default",
    )
    await repo.list_subscriptions(
        tx,
        owner_id=None,
        namespace=None,
        include_revoked=True,
        limit=10,
    )
    await repo.get_subscription(
        tx,
        subscription_id="subscription-id",
        owner_id=None,
        namespace=None,
    )

    emitted = [cursor.calls[0][0] for cursor in conn.cursors]
    assert emitted == [
        (
            "INSERT INTO webhook_subscriptions "
            "( id, url, events, secret, description, owner_id, namespace ) "
            "VALUES ( :id, :url, :events, :secret, :description, :owner_id, :namespace ) "
            "RETURNING id, created_at INTO :new_id, :new_created"
        ),
        (
            "SELECT id, url, events, description, owner_id, namespace, "
            "created_at, revoked, revoked_at FROM webhook_subscriptions "
            "ORDER BY created_at DESC FETCH FIRST :limit ROWS ONLY"
        ),
        (
            "SELECT id, url, events, description, owner_id, namespace, "
            "created_at, revoked, revoked_at FROM webhook_subscriptions WHERE id = :id"
        ),
    ]


def test_delivery_select_sql_matches_oracle_migration_columns() -> None:
    columns = _delivery_columns_from_migrations()
    assert columns == {
        "id",
        "subscription_id",
        "event_type",
        "payload",
        "owner_id",
        "namespace",
        "state",
        "attempt_count",
        "next_attempt_at",
        "last_error",
        "created_at",
        "updated_at",
        "payload_hash",
        "attempt_num",
        "status",
        "response_status",
        "response_body",
        "error",
        "scheduled_at",
        "delivered_at",
        "lease_token",
        "lease_expires_at",
        "writer_revision",
        "status_updated_at",
        "superseded",
    }

    select_sql = oracle_webhook_delivery_select_clause()
    delivery_references = set(re.findall(r"\bd\.([a-z_][a-z0-9_]*)", select_sql))
    subscription_references = set(re.findall(r"\bs\.([a-z_][a-z0-9_]*)", select_sql))
    assert delivery_references <= columns
    assert subscription_references <= _subscription_columns_from_migrations()
    assert "d.created_at" in select_sql
    assert "d.created AS created_at" not in select_sql
