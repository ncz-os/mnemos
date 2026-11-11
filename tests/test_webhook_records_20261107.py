"""FB1008: ``_sqlite_webhook_subscription`` / ``_sqlite_webhook_delivery`` mapping.

Each mapper must produce its documented frozen record with every documented field, use the
documented default for an absent optional field, and leave the source row untouched.

DIVERGENCE (brief premise vs code): ``description`` is only read when the row is a ``dict``,
so the ``sqlite3.Row`` the repository queries actually return always maps ``description`` to
``None`` even when the column is present. Invalid ``events`` JSON also degrades to an empty
tuple instead of raising, so a malformed value is indistinguishable from "no events".
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timezone

import pytest

from mnemos.persistence.base import WebhookDeliveryRecord, WebhookSubscriptionRecord
from mnemos.persistence.sqlite import (
    _sqlite_webhook_delivery,
    _sqlite_webhook_subscription,
)

UTC = timezone.utc
CREATED = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def _sub_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "sub-1",
        "url": "https://example.test/hook",
        "events": '["memory.created","memory.deleted"]',
        "description": "primary hook",
        "owner_id": "alice",
        "namespace": "alice-ns",
        "created": "2026-03-04T05:06:07Z",
        "revoked": 0,
        "revoked_at": None,
    }
    row.update(overrides)
    return row


def _del_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "del-1",
        "subscription_id": "sub-1",
        "event_type": "memory.created",
        "payload": '{"memory_id":"m1"}',
        "payload_hash": "abc123",
        "attempt_num": 1,
        "status": "pending",
        "response_status": None,
        "response_body": None,
        "error": None,
        "scheduled_at": "2026-03-04T05:06:07Z",
        "delivered_at": None,
        "created": "2026-03-04T05:06:07Z",
        "status_updated_at": "2026-03-04T05:06:07+00:00",
        "superseded": 0,
        "lease_token": None,
        "lease_expires_at": None,
        "writer_revision": 1,
    }
    row.update(overrides)
    return row


def test_subscription_maps_every_documented_field() -> None:
    record = _sqlite_webhook_subscription(_sub_row())

    assert type(record) is WebhookSubscriptionRecord
    assert record == WebhookSubscriptionRecord(
        id="sub-1",
        url="https://example.test/hook",
        events=("memory.created", "memory.deleted"),
        description="primary hook",
        owner_id="alice",
        namespace="alice-ns",
        created=CREATED,
        revoked=False,
        revoked_at=None,
    )


def test_subscription_timestamps_are_aware_utc() -> None:
    record = _sqlite_webhook_subscription(_sub_row(revoked=1, revoked_at="2026-03-05T00:00:00Z"))

    assert record.created.tzinfo is UTC
    assert record.revoked_at == datetime(2026, 3, 5, tzinfo=UTC)
    assert record.revoked_at is not None and record.revoked_at.tzinfo is UTC


def test_subscription_revoked_flag_becomes_a_bool() -> None:
    assert _sqlite_webhook_subscription(_sub_row(revoked=1)).revoked is True
    assert _sqlite_webhook_subscription(_sub_row(revoked=0)).revoked is False
    assert _sqlite_webhook_subscription(_sub_row(revoked=7)).revoked is True


@pytest.mark.parametrize("events", [None, "", "{}", '"just-a-string"', "not json"])
def test_subscription_bad_or_absent_events_become_an_empty_tuple(events: object) -> None:
    record = _sqlite_webhook_subscription(_sub_row(events=events))

    assert record.events == ()
    assert isinstance(record.events, tuple)


def test_subscription_events_elements_are_stringified() -> None:
    record = _sqlite_webhook_subscription(_sub_row(events='["a", 2, {"b": 3}]'))

    assert record.events == ("a", "2", "{'b': 3}")


def test_subscription_missing_description_defaults_to_none() -> None:
    row = _sub_row()
    del row["description"]

    assert _sqlite_webhook_subscription(row).description is None


def test_subscription_row_without_description_key_is_not_given_one() -> None:
    row = _sub_row()
    del row["description"]

    _sqlite_webhook_subscription(row)

    assert "description" not in row


def test_subscription_from_a_sqlite_row_drops_description() -> None:
    columns = list(_sub_row())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT ? AS " + ", ? AS ".join(columns),
        [_sub_row()[name] for name in columns],
    )
    row = cursor.fetchone()

    record = _sqlite_webhook_subscription(row)

    assert row["description"] == "primary hook"
    assert record.description is None  # DIVERGENCE: the dict-only guard drops it


def test_subscription_id_is_coerced_to_text() -> None:
    assert _sqlite_webhook_subscription(_sub_row(id=7)).id == "7"


def test_subscription_mapper_does_not_mutate_the_row() -> None:
    row = _sub_row()
    snapshot = copy.deepcopy(row)

    _sqlite_webhook_subscription(row)

    assert row == snapshot


def test_delivery_maps_every_documented_field() -> None:
    record = _sqlite_webhook_delivery(_del_row())

    assert type(record) is WebhookDeliveryRecord
    assert record == WebhookDeliveryRecord(
        id="del-1",
        subscription_id="sub-1",
        event_type="memory.created",
        payload='{"memory_id":"m1"}',
        payload_hash="abc123",
        attempt_num=1,
        status="pending",
        response_status=None,
        response_body=None,
        error=None,
        scheduled_at=CREATED,
        delivered_at=None,
        created=CREATED,
        status_updated_at=CREATED,
        superseded=False,
        lease_token=None,
        lease_expires_at=None,
        writer_revision=1,
    )


def test_delivery_optional_fields_are_none_and_superseded_is_bool() -> None:
    record = _sqlite_webhook_delivery(_del_row(superseded=1))

    assert record.response_status is None
    assert record.response_body is None
    assert record.error is None
    assert record.delivered_at is None
    assert record.lease_token is None
    assert record.lease_expires_at is None
    assert record.superseded is True


def test_delivery_populated_optional_fields_are_mapped() -> None:
    record = _sqlite_webhook_delivery(
        _del_row(
            response_status=500,
            response_body="boom",
            error="http 500",
            delivered_at="2026-03-04T06:00:00Z",
            lease_token=42,
            lease_expires_at="2026-03-04T05:10:00Z",
            attempt_num=3,
            writer_revision=9,
        )
    )

    assert record.response_status == 500
    assert record.response_body == "boom"
    assert record.error == "http 500"
    assert record.delivered_at == datetime(2026, 3, 4, 6, tzinfo=UTC)
    assert record.lease_token == "42"
    assert record.lease_expires_at == datetime(2026, 3, 4, 5, 10, tzinfo=UTC)
    assert record.attempt_num == 3
    assert record.writer_revision == 9


@pytest.mark.parametrize("revision", [0, None])
def test_delivery_writer_revision_falsy_becomes_zero(revision: object) -> None:
    assert _sqlite_webhook_delivery(_del_row(writer_revision=revision)).writer_revision == 0


@pytest.mark.parametrize("status", ["pending", "retrying", "succeeded", "abandoned"])
def test_delivery_accepts_each_documented_status(status: str) -> None:
    assert _sqlite_webhook_delivery(_del_row(status=status)).status == status


@pytest.mark.parametrize("status", ["queued", "", "SUCCEEDED", None])
def test_delivery_rejects_an_unknown_status(status: object) -> None:
    with pytest.raises(ValueError, match="unexpected webhook_deliveries status"):
        _sqlite_webhook_delivery(_del_row(status=status))


def test_delivery_mapper_does_not_mutate_the_row() -> None:
    row = _del_row()
    snapshot = copy.deepcopy(row)

    _sqlite_webhook_delivery(row)

    assert row == snapshot


def test_delivery_maps_from_a_sqlite_row() -> None:
    data = _del_row()
    columns = list(data)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        "SELECT " + ", ".join(f"? AS {name}" for name in columns),
        [data[name] for name in columns],
    )
    row = cursor.fetchone()

    record = _sqlite_webhook_delivery(row)

    assert record.id == "del-1"
    assert record.attempt_num == 1
    assert record.scheduled_at == CREATED
