"""Characterisation tests for ``mnemos/persistence/nats_events.py``.

Target (213 lines, 1 test reference before this file — the single
reference being ``tests/integration_nats/test_v5_2_nats_substrate.py``,
which is gated behind ``MNEMOS_NATS_TEST_URL`` and therefore does not run
in the default suite). These tests pin the module's CURRENT behaviour —
normal, edge and error cases — using temp dirs and fakes only: no
network, no live broker, no live database.

Module surface as of ``master`` 782ed312 (branch
``hv/ty-mn-mnemos-persistence-nats-events-20261105``):

Public
    ``WEBHOOKS_OUTBOX_SUBJECT_PREFIX`` / ``FEDERATION_MEMORY_SUBJECT_PREFIX``
    / ``FEDERATION_MEMORY_SCHEMA_VERSION``  — module constants.
    ``webhooks_nats_enabled()``             — gate for the webhook fanout.
    ``federation_nats_enabled()``           — gate for the federation fanout.
    ``safe_subject_segment()``              — NATS subject sanitiser.
    ``webhook_outbox_subject()``            — subject builder.
    ``publish_webhook_outbox_insert()``     — best-effort outbox fanout.
    ``federation_memory_subject()``         — subject builder.
    ``is_federation_memory_publishable()``  — publish gate (row predicate).
    ``federation_memory_upsert_event()``    — row -> event dict.
    ``publish_federation_memory_upsert_event()`` — best-effort publisher.

Private (still pinned — callers and tests reach them)
    ``_publish_event()``        — timeout-guarded ``publish_event`` wrapper.
    ``_safe_event_type()``      — dotted event-type sanitiser.
    ``_row_value()``            — Mapping/sequence row accessor.
    ``_json_object()``          — metadata -> dict normaliser.
    ``_wire_datetime()``        — datetime -> wire-format string.

Behaviour that looks like a defect is pinned ``xfail(strict=True)`` with
an inline comment and listed in the delivery report; no production code is
changed by this file. Three xfails assert the OPPOSITE of the current
behaviour (they encode the desired value and FAIL-to-xpass today); when
the defect is fixed they XPASS strictly, which is the signal to flip them
to ordinary assertions.
All tests are synchronous (``asyncio.run`` drives the coroutine paths), so
the module carries NO global ``pytest.mark.asyncio`` — that avoids the
pytest-asyncio "marked but not an async function" warnings a file-wide
mark would emit on the sync tests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

import pytest

from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.persistence import nats_events as ne


# ---------------------------------------------------------------------------
# Shared helpers / fakes
# ---------------------------------------------------------------------------


def _refresh_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the autouse settings-cache fixture re-read this test's env.

    ``tests/conftest.py::isolate_settings_singleton`` rebinds
    ``mnemos.core.config._settings`` to ``None`` at test teardown, which
    would otherwise record ``None`` as the undo value and leave a
    test-derived singleton installed for the NEXT test (see that
    fixture's docstring). Every test in this file that changes
    ``MNEMOS_*`` env before calling into the module undoes with the same
    rebinding, so the two fixtures compose deterministically regardless
    of suite order.
    """
    from mnemos.core import config

    monkeypatch.setattr(config, "_settings", None)


def _set_nats_publish_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setenv("MNEMOS_NATS_PUBLISH_TIMEOUT", str(seconds))
    _refresh_settings(monkeypatch)


def _set_service_flag(monkeypatch: pytest.MonkeyPatch, env_name: str, value: str) -> None:
    monkeypatch.setenv(env_name, value)
    _refresh_settings(monkeypatch)


def _set_include_private(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE", value)
    _refresh_settings(monkeypatch)


class _RecordedPublish:
    """Callable installed in place of ``nats_events.nats_publisher.publish_event``.

    Records every call as ``(subject, payload, msg_id)`` so a test can pin
    exactly what the module would have put on the wire. ``delay`` makes a
    call outlive the module's publish timeout; ``error`` makes it raise.
    """

    def __init__(self, *, delay: float = 0.0, error: BaseException | None = None):
        self.calls: list[tuple[str, dict, str | None]] = []
        self.delay = delay
        self.error = error

    async def __call__(self, subject, payload, *, msg_id=None):
        self.calls.append((subject, dict(payload), msg_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error

    @property
    def subjects(self) -> list[str]:
        return [call[0] for call in self.calls]

    @property
    def msg_ids(self) -> list[str | None]:
        return [call[2] for call in self.calls]


def _recorder(monkeypatch: pytest.MonkeyPatch, recorder: _RecordedPublish) -> _RecordedPublish:
    monkeypatch.setattr(ne.nats_publisher, "publish_event", recorder)
    return recorder


def _run(coro):
    """Drive a module coroutine on a private event loop (sync tests only)."""
    return asyncio.run(coro)


def _row(**overrides) -> dict:
    """A publishable local memory row, mirroring ``_FEDERATION_NATS_MEMORY_ROW_COLS``."""
    row = {
        "id": "mem_001",
        "content": "hello",
        "verbatim_content": "hello",
        "category": "facts",
        "subcategory": None,
        "metadata": {"k": "v"},
        "quality_rating": 88,
        "owner_id": "alice",
        "namespace": "default",
        "permission_mode": 644,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": "test-agent",
        "created": datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        "updated": datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        "federation_source": None,
        "deleted_at": None,
        "archived_at": None,
        "consolidated_into": None,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


def test_subject_prefixes_and_schema_version_are_pinned():
    assert ne.WEBHOOKS_OUTBOX_SUBJECT_PREFIX == "mnemos.webhooks.outbox"
    assert ne.FEDERATION_MEMORY_SUBJECT_PREFIX == "mnemos.federation.memory"
    assert ne.FEDERATION_MEMORY_SCHEMA_VERSION == "1"


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------


def test_webhooks_nats_enabled_is_false_by_default(monkeypatch):
    monkeypatch.delenv("MNEMOS_NATS_WEBHOOKS_ENABLED", raising=False)
    _refresh_settings(monkeypatch)
    assert ne.webhooks_nats_enabled() is False


def test_webhooks_nats_enabled_reads_env_flag(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_WEBHOOKS_ENABLED", "true")
    assert ne.webhooks_nats_enabled() is True


def test_federation_nats_enabled_is_false_by_default(monkeypatch):
    monkeypatch.delenv("MNEMOS_NATS_FEDERATION_ENABLED", raising=False)
    _refresh_settings(monkeypatch)
    assert ne.federation_nats_enabled() is False


def test_federation_nats_enabled_reads_env_flag(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_FEDERATION_ENABLED", "true")
    assert ne.federation_nats_enabled() is True


# ---------------------------------------------------------------------------
# safe_subject_segment — normal / edge / error
# ---------------------------------------------------------------------------


def test_safe_subject_segment_keeps_safe_characters():
    assert ne.safe_subject_segment("alice-01_NS") == "alice-01_NS"


def test_safe_subject_segment_replaces_unsafe_runs_with_single_underscore():
    # Dotted, spaced and symbol runs each collapse to ONE underscore.
    assert ne.safe_subject_segment("team.ns one/two") == "team_ns_one_two"


def test_safe_subject_segment_strips_edge_underscores():
    # Interior sanitiser output keeps interior underscores, but leading /
    # trailing sanitiser output is stripped: ".alpha." -> "alpha".
    assert ne.safe_subject_segment(".alpha.") == "alpha"
    # "...///" sanitises to "___" then strips to "" -> the default wins.
    assert ne.safe_subject_segment("///") == "default"


def test_safe_subject_segment_none_falls_back_to_default():
    assert ne.safe_subject_segment(None) == "default"


def test_safe_subject_segment_explicit_default_is_used_when_sanitised_value_is_empty():
    assert ne.safe_subject_segment("!!!", default="event") == "event"


def test_safe_subject_segment_stringifies_arbitrary_values():
    # int / bool / type inputs are str()-ed first, then sanitised: the
    # space in "<class 'object'>" collapses to "_" and the surrounding
    # non-word runs are stripped by the .strip("_") tail.
    assert ne.safe_subject_segment(42) == "42"
    assert ne.safe_subject_segment(False) == "False"
    assert ne.safe_subject_segment(object) == "class_object"


# ---------------------------------------------------------------------------
# _safe_event_type / webhook_outbox_subject / federation_memory_subject
# ---------------------------------------------------------------------------


def test_safe_event_type_keeps_dotted_segments():
    assert ne._safe_event_type("memory.created") == "memory.created"


def test_safe_event_type_sanitises_each_segment():
    assert ne._safe_event_type("memory.created v2") == "memory.created_v2"


def test_safe_event_type_drops_empty_segments_and_falls_back_to_event():
    # ".memory..created." -> empty segments are filtered out entirely...
    assert ne._safe_event_type(".memory..created.") == "memory.created"
    # ...and an all-empty event type falls back to "event".
    assert ne._safe_event_type("") == "event"
    assert ne._safe_event_type("..") == "event"


def test_webhook_outbox_subject_joins_prefix_tenant_and_event_type():
    subject = ne.webhook_outbox_subject(tenant="alice", event_type="memory.created")
    assert subject == "mnemos.webhooks.outbox.alice.memory.created"


def test_webhook_outbox_subject_sanitises_tenant_and_event_type():
    subject = ne.webhook_outbox_subject(tenant="a b/c", event_type="memory.created v2")
    assert subject == "mnemos.webhooks.outbox.a_b_c.memory.created_v2"


def test_webhook_outbox_subject_none_tenant_uses_default_segment():
    subject = ne.webhook_outbox_subject(tenant=None, event_type="memory.created")
    assert subject == "mnemos.webhooks.outbox.default.memory.created"


def test_federation_memory_subject_sanitises_namespace():
    assert ne.federation_memory_subject("team ns") == "mnemos.federation.memory.team_ns"
    assert ne.federation_memory_subject(None) == "mnemos.federation.memory.default"
    assert ne.federation_memory_subject("///") == "mnemos.federation.memory.default"


# ---------------------------------------------------------------------------
# _publish_event — timeout wrapper
# ---------------------------------------------------------------------------


def test_publish_event_passes_subject_payload_and_msg_id_through(monkeypatch):
    recorder = _recorder(monkeypatch, _RecordedPublish())
    _set_nats_publish_timeout(monkeypatch, 1.0)

    _run(ne._publish_event("mnemos.webhooks.outbox.t.e", {"k": "v"}, msg_id="msg-1"))

    assert recorder.calls == [("mnemos.webhooks.outbox.t.e", {"k": "v"}, "msg-1")]


def test_publish_event_timeout_is_swallowed_and_logged(monkeypatch, caplog):
    # A hung publisher must not block the caller: the module catches
    # asyncio.TimeoutError and logs a warning instead of propagating.
    _recorder(monkeypatch, _RecordedPublish(delay=1.0))
    _set_nats_publish_timeout(monkeypatch, 0.01)

    with caplog.at_level(logging.WARNING, logger="mnemos.persistence.nats_events"):
        _run(ne._publish_event("mnemos.subject.under.test", {"k": "v"}, msg_id="msg-t"))

    warning = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning) == 1
    assert "NATS publish timed out" in warning[0].getMessage()
    assert "0.010" in warning[0].getMessage()  # "%.3fs" of the configured timeout
    assert "mnemos.subject.under.test" in warning[0].getMessage()


def test_publish_event_propagates_non_timeout_errors(monkeypatch):
    # Only asyncio.TimeoutError is swallowed. Any other exception from the
    # publisher escapes _publish_event — this is the CURRENT behaviour
    # (publish_event itself never raises, so the branch is unreachable in
    # production, but the contract is pinned here).
    recorder = _RecordedPublish(error=ValueError("boom"))
    _recorder(monkeypatch, recorder)
    _set_nats_publish_timeout(monkeypatch, 1.0)

    with pytest.raises(ValueError, match="boom"):
        _run(ne._publish_event("mnemos.subject.under.test", {"k": "v"}, msg_id="msg-e"))


# ---------------------------------------------------------------------------
# publish_webhook_outbox_insert
# ---------------------------------------------------------------------------


def test_publish_webhook_outbox_insert_noop_when_disabled(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_WEBHOOKS_ENABLED", "false")
    recorder = _recorder(monkeypatch, _RecordedPublish())

    _run(
        ne.publish_webhook_outbox_insert(
            delivery_id="d1",
            subscription_id="s1",
            event_type="memory.created",
            url="https://hooks.example.test/x",
            payload_hash="abc123",
            namespace="default",
            owner_id="alice",
        )
    )

    assert recorder.calls == []


def test_publish_webhook_outbox_insert_fanout_shape(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_WEBHOOKS_ENABLED", "true")
    recorder = _recorder(monkeypatch, _RecordedPublish())
    monkeypatch.setattr(ne.nats_client, "get_node_name", lambda: "node-a")

    _run(
        ne.publish_webhook_outbox_insert(
            delivery_id="11111111-1111-4111-8111-111111111111",
            subscription_id="22222222-2222-4222-8222-222222222222",
            event_type="memory.created",
            url="https://hooks.example.test/x",
            payload_hash="abc123",
            namespace="default",
            owner_id="alice",
        )
    )

    assert recorder.subjects == ["mnemos.webhooks.outbox.alice.memory.created"]
    assert recorder.msg_ids == ["webhooks.outbox.11111111-1111-4111-8111-111111111111"]
    _subject, payload, _msg_id = recorder.calls[0]
    assert payload == {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "delivery_id": "11111111-1111-4111-8111-111111111111",
        "subscription_id": "22222222-2222-4222-8222-222222222222",
        "event_type": "memory.created",
        "url": "https://hooks.example.test/x",
        "payload_hash": "abc123",
        "namespace": "default",
        "tenant": "alice",
        "owner_id": "alice",
        "outbox_table": "webhook_deliveries",
        "source_node": "node-a",
    }
    # The webhook outbox consumer's payload validator requires these
    # exact keys (mnemos/workers/webhooks_dispatch_nats_consumer.py).
    assert set(payload) >= {"event_id", "delivery_id", "event_type", "payload_hash", "outbox_table"}
    assert payload["outbox_table"] == "webhook_deliveries"


def test_publish_webhook_outbox_insert_stringifies_non_string_ids(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_WEBHOOKS_ENABLED", "true")
    recorder = _recorder(monkeypatch, _RecordedPublish())
    monkeypatch.setattr(ne.nats_client, "get_node_name", lambda: "node-a")

    _run(
        ne.publish_webhook_outbox_insert(
            delivery_id=7,
            subscription_id=9,
            event_type="memory.created",
            url="https://hooks.example.test/x",
            payload_hash="abc123",
            namespace=None,
            owner_id=None,
        )
    )

    payload = recorder.calls[0][1]
    assert payload["event_id"] == "7"
    assert payload["delivery_id"] == "7"
    assert payload["subscription_id"] == "9"
    # None tenant still produces a well-formed subject segment.
    assert recorder.subjects == ["mnemos.webhooks.outbox.default.memory.created"]


# ---------------------------------------------------------------------------
# _row_value / _json_object / _wire_datetime
# ---------------------------------------------------------------------------


def test_row_value_reads_mapping_keys_with_default():
    assert ne._row_value({"a": 1}, "a") == 1
    assert ne._row_value({"a": 1}, "b") is None
    assert ne._row_value({"a": 1}, "b", default="d") == "d"
    assert ne._row_value({}, "a", default=0) == 0


def test_row_value_reads_sequence_rows_by_index():
    # Sequence (non-Mapping) rows are indexed by ``row[key]``: a numeric
    # key in range is returned positionally — this is how an asyncpg-style
    # sequence row whose keys line up with column order is read.
    seq_row = ("id-value", "content-value")
    assert ne._row_value(seq_row, 0) == "id-value"
    assert ne._row_value(seq_row, 1) == "content-value"
    # A non-numeric key raises TypeError on ``row[key]`` -> default.
    assert ne._row_value(seq_row, "id", default="fallback") == "fallback"
    assert ne._row_value(seq_row, "missing") is None
    # A numeric key past the end raises IndexError -> default.
    assert ne._row_value(seq_row, 99, default="fallback") == "fallback"
    # ``row`` without ``__getitem__`` at all (an attribute-style record)
    # raises TypeError -> default: attribute fallback is NOT attempted.
    class _AttrOnly:
        def __init__(self):
            self.id = "attr-id"

    assert ne._row_value(_AttrOnly(), "id") is None
    assert ne._row_value(_AttrOnly(), "id", default="d") == "d"


def test_json_object_normalises_metadata_shapes():
    assert ne._json_object({"a": 1}) == {"a": 1}
    assert ne._json_object('{"a": 1}') == {"a": 1}
    # Invalid JSON -> {} (never raises).
    assert ne._json_object("{not json") == {}
    # Valid JSON that is NOT an object -> {} (the isinstance dict guard).
    assert ne._json_object("[1, 2]") == {}
    assert ne._json_object("42") == {}
    assert ne._json_object(None) == {}
    assert ne._json_object(42) == {}


def test_wire_datetime_converts_to_utc_zulu():
    # Naive datetimes are assumed UTC.
    naive = datetime(2026, 5, 4, 12, 0, 0)
    assert ne._wire_datetime(naive) == "2026-05-04T12:00:00Z"
    # Aware datetimes are converted to UTC.
    offset = datetime(2026, 5, 4, 8, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert ne._wire_datetime(offset) == "2026-05-04T12:00:00Z"


def test_wire_datetime_edge_cases():
    assert ne._wire_datetime(None) is None
    # Non-datetime values fall through to str().
    assert ne._wire_datetime("2026-05-04T12:00:00Z") == "2026-05-04T12:00:00Z"
    assert ne._wire_datetime(1735689600) == "1735689600"


# ---------------------------------------------------------------------------
# is_federation_memory_publishable
# ---------------------------------------------------------------------------


def test_publishable_row_passes_gate(monkeypatch):
    _set_include_private(monkeypatch, "0")  # exercise the world-read branch
    assert ne.is_federation_memory_publishable(_row()) is True


def test_replica_rows_are_never_publishable(monkeypatch):
    _set_include_private(monkeypatch, "1")
    # Any non-None federation_source marks the row as a replica copy.
    assert ne.is_federation_memory_publishable(_row(federation_source="peer-b")) is False


def test_lifecycle_gated_rows_are_never_publishable(monkeypatch):
    _set_include_private(monkeypatch, "1")
    assert ne.is_federation_memory_publishable(_row(deleted_at="2026-01-01")) is False
    assert ne.is_federation_memory_publishable(_row(archived_at="2026-01-01")) is False
    assert ne.is_federation_memory_publishable(_row(consolidated_into="mem_other")) is False


def test_vault_namespace_is_never_publishable_even_for_trusted_feed(monkeypatch):
    # Credential boundary: the vault is excluded regardless of the
    # include-private posture — mirrors the SQL feed_query exclusion.
    _set_include_private(monkeypatch, "1")
    assert ne.is_federation_memory_publishable(_row(namespace=VAULT_NAMESPACE)) is False
    # ...and also when the gate would otherwise run the world-read check.
    _set_include_private(monkeypatch, "0")
    assert ne.is_federation_memory_publishable(_row(namespace=VAULT_NAMESPACE, permission_mode=644)) is False


def test_world_read_gate_reads_only_the_world_digit(monkeypatch):
    _set_include_private(monkeypatch, "0")  # world-read gate active
    # The gate is exactly ``permission_mode % 10 >= 4``: the UNITS digit
    # decides, alone. 644/664/646 clear it (units >= 4)...
    assert ne.is_federation_memory_publishable(_row(permission_mode=644)) is True
    assert ne.is_federation_memory_publishable(_row(permission_mode=664)) is True
    assert ne.is_federation_memory_publishable(_row(permission_mode=646)) is True
    # ...600, 440, 640 and 660 all have units digit 0 -> all refused,
    # whatever their tens digit says. 640/660 carry the tens digit as
    # the group/owner side (tens=6), so under that reading they are
    # group-readable and world-denied: refusing them is RIGHT, but only
    # because their units digit happens to be 0 — the tens digit never
    # participated in the verdict (strict xfails below pin that).
    assert ne.is_federation_memory_publishable(_row(permission_mode=600)) is False
    assert ne.is_federation_memory_publishable(_row(permission_mode=440)) is False
    assert ne.is_federation_memory_publishable(_row(permission_mode=640)) is False
    assert ne.is_federation_memory_publishable(_row(permission_mode=660)) is False


def test_world_read_gate_rejects_non_numeric_permission_mode(monkeypatch):
    _set_include_private(monkeypatch, "0")
    assert ne.is_federation_memory_publishable(_row(permission_mode="not-a-number")) is False
    assert ne.is_federation_memory_publishable(_row(permission_mode=None)) is False


def test_include_private_gate_bypasses_world_read_check(monkeypatch):
    _set_include_private(monkeypatch, "1")  # trusted-LAN posture (default)
    assert ne.is_federation_memory_publishable(_row(permission_mode=600)) is True
    assert ne.is_federation_memory_publishable(_row(permission_mode=644)) is True
    # The lifecycle / replica / vault guards still apply with include-private on.
    assert ne.is_federation_memory_publishable(_row(permission_mode=600, federation_source="peer")) is False


def test_include_private_unrecognised_value_falls_back_to_include(monkeypatch, caplog):
    # A typo ("flase") must NOT silently switch the gate off: the
    # accessor warns and falls back to the documented default (include).
    _set_include_private(monkeypatch, "flase")
    with caplog.at_level(logging.WARNING, logger="mnemos.core.config"):
        assert ne.is_federation_memory_publishable(_row(permission_mode=600)) is True
    assert "MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE" in caplog.text
    assert "not a recognised value" in caplog.text


def test_publishable_gate_accepts_non_mapping_rows(monkeypatch):
    _set_include_private(monkeypatch, "1")

    class _AttrRow:
        federation_source = None
        deleted_at = None
        archived_at = None
        consolidated_into = None
        namespace = "default"
        permission_mode = 644

    # An attribute-style record (no __getitem__): every gate read falls
    # back to None -> passes the lifecycle/namespace guards...
    assert ne.is_federation_memory_publishable(_AttrRow()) is True
    # ...and a sequence row reads positionally: every gated key indexes
    # out of range -> default None -> gate passes -> include-private on.
    assert ne.is_federation_memory_publishable(("only-one-value",)) is True


# ---------------------------------------------------------------------------
# federation_memory_upsert_event
# ---------------------------------------------------------------------------


def test_upsert_event_builds_full_payload(monkeypatch):
    _set_include_private(monkeypatch, "0")
    monkeypatch.setattr(ne.nats_client, "get_node_name", lambda: "node-a")

    event = ne.federation_memory_upsert_event(_row())

    assert event == {
        "schema_version": "1",
        "event_id": "federation.memory.upsert.mem_001.2026-05-04T12:00:00Z",
        "event_type": "memory.upsert",
        "memory_id": "mem_001",
        "id": "mem_001",
        "content": "hello",
        "verbatim_content": "hello",
        "category": "facts",
        "subcategory": None,
        "metadata": {"k": "v"},
        "quality_rating": 88,
        "owner_id": "alice",
        "namespace": "default",
        "permission_mode": 644,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": "test-agent",
        "created": "2026-05-04T12:00:00Z",
        "updated": "2026-05-04T12:00:00Z",
        "source_node": "node-a",
    }


def test_upsert_event_returns_none_for_unpublishable_rows(monkeypatch):
    _set_include_private(monkeypatch, "1")
    assert ne.federation_memory_upsert_event(_row(federation_source="peer-b")) is None
    assert ne.federation_memory_upsert_event(_row(deleted_at="2026-01-01")) is None
    assert ne.federation_memory_upsert_event(_row(namespace=VAULT_NAMESPACE)) is None


def test_upsert_event_returns_none_without_memory_id(monkeypatch):
    _set_include_private(monkeypatch, "1")
    # ``if not memory_id`` — missing, empty and zero-ish ids are all refused.
    assert ne.federation_memory_upsert_event(_row(id=None)) is None
    assert ne.federation_memory_upsert_event(_row(id="")) is None
    assert ne.federation_memory_upsert_event({"namespace": "default", "permission_mode": 644}) is None


def test_upsert_event_event_id_falls_back_to_unknown_without_updated(monkeypatch):
    _set_include_private(monkeypatch, "1")
    event = ne.federation_memory_upsert_event(_row(updated=None, created=None))
    assert event is not None
    assert event["event_id"] == "federation.memory.upsert.mem_001.unknown"
    assert event["updated"] is None
    assert event["created"] is None


def test_upsert_event_applies_documented_defaults(monkeypatch):
    _set_include_private(monkeypatch, "1")
    row = _row(content="", verbatim_content=None, category=None, quality_rating=0, namespace=None)
    event = ne.federation_memory_upsert_event(row)
    assert event is not None
    # ``or`` defaults: empty/None content falls back to "", category to
    # "federation", namespace to "default"... but quality_rating=0 is
    # ALSO truthily coerced to 75 (the strict xfail at the bottom pins
    # that collapse as a suspected defect).
    assert event["content"] == ""
    assert event["verbatim_content"] == ""
    assert event["category"] == "federation"
    assert event["namespace"] == "default"
    assert event["quality_rating"] == 75


def test_upsert_event_stringifies_integer_memory_id(monkeypatch):
    _set_include_private(monkeypatch, "1")
    event = ne.federation_memory_upsert_event(_row(id=42))
    assert event is not None
    assert event["memory_id"] == "42"
    assert event["id"] == "42"
    assert event["event_id"] == "federation.memory.upsert.42.2026-05-04T12:00:00Z"


def test_upsert_event_metadata_normalisation(monkeypatch):
    _set_include_private(monkeypatch, "1")
    # JSON-string metadata is decoded...
    from_string = ne.federation_memory_upsert_event(_row(metadata='{"a": 1}'))
    assert from_string is not None and from_string["metadata"] == {"a": 1}
    # ...non-object metadata falls back to {}.
    for bad in ("[1, 2]", "{not json", None, 42):
        event = ne.federation_memory_upsert_event(_row(metadata=bad))
        assert event is not None, bad
        assert event["metadata"] == {}, bad


def test_upsert_event_wire_datetime_edge_values(monkeypatch):
    _set_include_private(monkeypatch, "1")
    # Non-datetime updated/created pass through str() unchanged and the
    # event_id embeds that literal string.
    event = ne.federation_memory_upsert_event(_row(updated="not-a-date", created=1735689600))
    assert event is not None
    assert event["updated"] == "not-a-date"
    assert event["created"] == "1735689600"
    assert event["event_id"] == "federation.memory.upsert.mem_001.not-a-date"


def test_upsert_event_naive_datetimes_are_utc(monkeypatch):
    _set_include_private(monkeypatch, "1")
    row = _row(created=datetime(2026, 1, 2, 3, 4, 5), updated=datetime(2026, 1, 2, 3, 4, 5))
    event = ne.federation_memory_upsert_event(row)
    assert event is not None
    assert event["created"] == "2026-01-02T03:04:05Z"
    assert event["updated"] == "2026-01-02T03:04:05Z"


# ---------------------------------------------------------------------------
# publish_federation_memory_upsert_event
# ---------------------------------------------------------------------------


def test_publish_federation_noop_when_disabled(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_FEDERATION_ENABLED", "false")
    recorder = _recorder(monkeypatch, _RecordedPublish())

    _run(ne.publish_federation_memory_upsert_event({"namespace": "default", "event_id": "e1"}))

    assert recorder.calls == []


def test_publish_federation_subject_and_msg_id(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_FEDERATION_ENABLED", "true")
    recorder = _recorder(monkeypatch, _RecordedPublish())

    _run(
        ne.publish_federation_memory_upsert_event(
            {"namespace": "team ns", "event_id": "e1", "memory_id": "m1", "content": "c"}
        )
    )

    assert recorder.subjects == ["mnemos.federation.memory.team_ns"]
    assert recorder.msg_ids == ["e1"]
    payload = recorder.calls[0][1]
    assert payload["content"] == "c"  # the whole event dict is forwarded as-is


def test_publish_federation_msg_id_falls_back_to_memory_id_then_empty(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_FEDERATION_ENABLED", "true")
    recorder = _recorder(monkeypatch, _RecordedPublish())

    _run(ne.publish_federation_memory_upsert_event({"namespace": "default", "memory_id": "m2"}))
    _run(ne.publish_federation_memory_upsert_event({"namespace": "default"}))

    # ``event_id or memory_id or ""`` — a missing event_id falls back to
    # the memory_id, and a missing both yields an EMPTY msg_id (which the
    # publisher treats as "no dedup header").
    assert recorder.msg_ids == ["m2", ""]


def test_publish_federation_namespace_defaults_to_default(monkeypatch):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_FEDERATION_ENABLED", "true")
    recorder = _recorder(monkeypatch, _RecordedPublish())

    _run(ne.publish_federation_memory_upsert_event({"namespace": None, "event_id": "e"}))

    assert recorder.subjects == ["mnemos.federation.memory.default"]


def test_publish_federation_timeout_is_swallowed(monkeypatch, caplog):
    _set_service_flag(monkeypatch, "MNEMOS_NATS_FEDERATION_ENABLED", "true")
    # Side-effect install only: the module reads the patched publisher off
    # nats_publisher, so the helper's return value is not needed here.
    _recorder(monkeypatch, _RecordedPublish(delay=1.0))
    _set_nats_publish_timeout(monkeypatch, 0.01)

    with caplog.at_level(logging.WARNING, logger="mnemos.persistence.nats_events"):
        _run(ne.publish_federation_memory_upsert_event({"namespace": "default", "event_id": "e"}))

    assert "NATS publish timed out" in caplog.text


# ---------------------------------------------------------------------------
# Suspected defects — pinned strict xfail
#
# Each body asserts the DESIRED behaviour, so today it fails -> xfail;
# when the production defect is fixed the test XPASSes strictly, which is
# the signal to convert it into an ordinary assertion. None of these fix
# production code — they only pin the current (wrong) value.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT 1 (NATS-EVENTS-CHAR-1): quality_rating=0 is truthily coerced to the "
        "75 default by `_row_value(row, 'quality_rating') or 75`. A legitimate "
        "zero-rated memory is re-published as 75 to federation peers."
    ),
)
def test_xfail_upsert_event_zero_quality_rating_is_not_defaulted(monkeypatch):
    _set_include_private(monkeypatch, "1")
    event = ne.federation_memory_upsert_event(_row(quality_rating=0))
    assert event is not None
    # Desired: a stored 0 is forwarded verbatim. Current: `or 75` fires.
    assert event["quality_rating"] == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT 2 (NATS-EVENTS-CHAR-2): the world-read gate computes only "
        "`permission_mode % 10 >= 4` — the TENS (group/owner) digit is never "
        "read. Under the offsite posture "
        "(`MNEMOS_FEDERATION_FEED_INCLUDE_PRIVATE=0`) every verdict is a "
        "function of the units digit alone: 640 and 660 (both tens=6 "
        "group+owner) receive identical answers today (both refused on "
        "units=0) because their units digits match, not because the gate "
        "compared their group/owner side. The gate cannot distinguish "
        "'world-readable' from 'group-readable' at all — it inspects "
        "exactly one digit."
    ),
)
def test_xfail_world_read_gate_requires_a_world_bit_not_a_group_bit(monkeypatch):
    _set_include_private(monkeypatch, "0")  # offsite posture: world-read only
    # Desired: under the offsite posture the gate must read the tens
    # (group/owner) digit alongside the units digit, so rows that differ
    # only in their tens digit are never decided by the units digit
    # alone. Reading the mode as group|world|owner in descending digit
    # order (the only layout the gate's `units >= 4` test corresponds
    # to): 640/660 are group+owner but world-denied (units=0), 646/664
    # are group+owner and world-readable (units>=4), 460/464 are
    # group-only (tens=4) with world on the units side — see the 2b
    # xfail for the companion case that separates the readings.
    # Today the gate computes `mode % 10 >= 4` alone: every verdict a
    # function of the units digit, the tens digit never consulted.
    #
    # The XPASS trigger: asserted with today's values throughout, but
    # the 464 and 660 lines are the two a tens-reading fix must answer
    # differently than the units digit does — 464 (group-only, units=4)
    # passes today and must fall under the offsite posture once the
    # tens=4 group bit is read; 660 (group+owner, units=0) is refused
    # today and must be admitted once the tens=6 side is read. Flipping
    # EITHER line XPASSes this strictly. The other four are invariants
    # under every single-digit reading (646/664 keep world+group+owner,
    # 640/460 keep world absent), so a fix must not touch them.
    assert ne.is_federation_memory_publishable(_row(permission_mode=646)) is True
    assert ne.is_federation_memory_publishable(_row(permission_mode=664)) is True
    assert ne.is_federation_memory_publishable(_row(permission_mode=640)) is False
    assert ne.is_federation_memory_publishable(_row(permission_mode=460)) is False
    assert ne.is_federation_memory_publishable(_row(permission_mode=660)) is False
    assert ne.is_federation_memory_publishable(_row(permission_mode=464)) is True


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT 2b (NATS-EVENTS-CHAR-2): the live half of DEFECT 2 — under "
        "the offsite posture the gate forwards a row on its UNITS digit "
        "regardless of the TENS (group/owner) digit: 460 (tens=4 group, "
        "units=0) is refused while 464 (tens=4 group, units=4) is passed "
        "— the SAME group-only row, opposite verdicts, purely because the "
        "units digit differs. The gate never consults the tens digit, so "
        "'group-readable' and 'owner/world-readable' rows receive the "
        "answer their units digit dictates."
    ),
)
def test_xfail_world_read_gate_rejects_group_only_rows(monkeypatch):
    _set_include_private(monkeypatch, "0")  # offsite posture: world-read only
    # Desired: a group-readable, world-denied row must be refused under
    # the offsite posture, and the refusal must come from reading the
    # tens (group/owner) digit — not from the units digit happening to
    # be 0. Today the gate computes `mode % 10 >= 4` alone: 440 and 460
    # (tens=4 group, units=0) refused because units=0; 464 (tens=4
    # group, units=4) PASSED — the same group-only rows refused or
    # forwarded purely on their units digits.
    #
    # The XPASS trigger is 460: asserted False (today's value) so a fix
    # that reads the tens digit AND still refuses the group-only 460
    # under the offsite posture keeps it False... so instead trigger on
    # 440 vs 464: same group-only side (tens=4), opposite verdicts
    # today. A fix that reads the tens digit must answer them from the
    # SAME digit — flipping 464 to False (or 440 to True) XPASSes.
    # 640 stays asserted False as the invariant no reading can flip.
    assert ne.is_federation_memory_publishable(_row(permission_mode=440)) is False
    assert ne.is_federation_memory_publishable(_row(permission_mode=464)) is True
    assert ne.is_federation_memory_publishable(_row(permission_mode=640)) is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT 3 (NATS-EVENTS-CHAR-3): empty-string content and missing "
        "content are indistinguishable in the event payload — `'' or ''` is "
        "`''`, so the `or ''` default never fires for an empty value, and a "
        "row whose content is genuinely empty is re-published to federation "
        "peers with the exact same `content` field as a row that had none. "
        "No marker distinguishes the two on the wire."
    ),
)
def test_xfail_upsert_event_empty_content_is_reported_as_missing(monkeypatch):
    _set_include_private(monkeypatch, "1")
    empty = ne.federation_memory_upsert_event(_row(content="", verbatim_content=""))
    missing = ne.federation_memory_upsert_event(_row(content=None, verbatim_content=None))
    assert empty is not None and missing is not None
    # Desired: the two rows are distinguishable in the payload (distinct
    # content values, or an explicit flag). Current: both carry "" —
    # byte-identical content fields — so an empty row and a contentless
    # row are the same event to a peer.
    assert empty["content"] != missing["content"]
