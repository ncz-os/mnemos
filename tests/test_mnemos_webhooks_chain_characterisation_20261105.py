"""Characterisation tests for ``mnemos/webhooks/chain.py``.

``chain.py`` is the pre-item-7 webhook retry-chain helper layer: a handful of
raw-asyncpg statements, the advisory-lock key derivation, and one pure record
accessor. The production runtime no longer reaches these helpers (the ABC
subsumes the state machine), but ``test_webhook_retry_state.py`` still drives
them and the Postgres lock-key derivation must stay byte-for-byte identical to
``mnemos.persistence.postgres._delivery_chain_lock_key`` so old and new leases
mutually exclude each other during a rolling deploy.

These tests PIN TODAY'S behaviour — including the defects — through the
module's public functions. They are not a design proposal: if a value moves
intentionally, the pin moves with it. No real database, network or clock is
touched: every ``conn`` is a fake that records calls, and the NATS publishers
are monkeypatched.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from pathlib import Path

import pytest

from mnemos.webhooks import chain
from mnemos.webhooks import nats_events as webhook_nats_events

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_DELIVERY_ID = "11111111-1111-1111-1111-111111111111"
_LEASE_TOKEN = "22222222-2222-2222-2222-222222222222"
_SUCCESSOR_ID = "33333333-3333-3333-3333-333333333333"


class _FakeConn:
    """Records every call, answers from a small deterministic script.

    ``fetchrow`` answers come from ``row_results``; ``fetchval`` from
    ``val_results``; ``fetch`` from ``fetch_results``; ``execute`` records its
    single argument and returns ``execute_result``. When the script is
    exhausted the remaining answers are ``None`` (or ``[]`` for ``fetch``).
    """

    def __init__(
        self,
        *,
        row_results: list | None = None,
        val_results: list | None = None,
        fetch_results: list | None = None,
        execute_result: str = "UPDATE 0",
    ) -> None:
        self.row_results = list(row_results or [])
        self.val_results = list(val_results or [])
        self.fetch_results = list(fetch_results or [])
        self.execute_result = execute_result
        self.calls: list[tuple] = []

    def _next(self, bucket: list, default):
        return bucket.pop(0) if bucket else default

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._next(self.row_results, None)

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self._next(self.val_results, None)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._next(self.fetch_results, [])

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return self.execute_result


class _SqliteConn:
    """``sqlite3``-home connection — no advisory lock is taken on it."""


_SqliteConn.__module__ = "sqlite3"


class _AiosqliteConn:
    """``aiosqlite``-home connection — also lock-free."""


_AiosqliteConn.__module__ = "aiosqlite"


class _AsyncpgConn(_FakeConn):
    """``asyncpg``-home connection — the only kind that takes the lock."""


_AsyncpgConn.__module__ = "asyncpg.connection"


class _ModelledConn:
    """A connection whose type carries an explicit ``__module__`` attribute."""


class _ModelledSqlite(_ModelledConn):
    pass


_ModelledSqlite.__module__ = "sqlite3.dbapi2"


class _PlainRecord:
    """Only attribute access — no ``__getitem__``."""

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _MappingRecord:
    """``__getitem__`` only — no attributes."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


class _ClassAttributeHolder:
    url = "https://example.test/class-level"


class _SubscriptableMeta(type):
    def __getitem__(cls, key):
        return f"meta-indexed:{key}"


class _MetaclassHolder(metaclass=_SubscriptableMeta):
    url = "https://example.test/meta-class"


class _RaisingRecord:
    """``__getitem__`` that always raises ``KeyError``."""

    def __getitem__(self, key):
        raise KeyError(key)


class _RaisingTypeErrorRecord:
    """``__getitem__`` that always raises ``TypeError``."""

    def __getitem__(self, key):
        raise TypeError(key)


class _Result:
    def __init__(self, response_status=200, response_body="ok", error=None) -> None:
        self.response_status = response_status
        self.response_body = response_body
        self.error = error


def _run(coro):
    return asyncio.run(coro)


class _PublishRecorder:
    """Stand-in for the two NATS publishers ``_insert_successor_delivery`` calls."""

    def __init__(self) -> None:
        self.queued: list[dict] = []
        self.outbox: list[dict] = []
        self.fail_with: Exception | None = None

    async def publish_delivery_queued(self, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.queued.append(kwargs)

    async def publish_webhook_outbox_insert(self, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.outbox.append(kwargs)


@pytest.fixture
def publishers(monkeypatch):
    """Replace both NATS fanout helpers with deterministic recorders."""
    recorder = _PublishRecorder()
    monkeypatch.setattr(webhook_nats_events, "publish_delivery_queued", recorder.publish_delivery_queued)
    monkeypatch.setattr(webhook_nats_events, "publish_webhook_outbox_insert", recorder.publish_webhook_outbox_insert)
    return recorder


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_public_surface_is_the_declared_dunder_all():
    expected = {
        "_load_delivery_for_claim",
        "_insert_successor_delivery",
        "_record_value",
        "_is_sqlite_connection",
        "_lock_delivery_chain",
        "_delivery_chain_lock_key",
        "_has_successor_attempt",
        "_has_live_successor_attempt",
        "_has_succeeded_chain_attempt",
        "_abandon_owned_attempt_after_live_successor",
        "_abandon_current_attempt_after_succeeded_chain_peer",
        "_abandon_owned_attempt_after_succeeded_chain_peer",
        "_find_live_unleased_successor_attempts",
        "_abandon_live_successor_attempt",
    }

    assert set(chain.__all__) == expected
    assert len(chain.__all__) == len(set(chain.__all__))
    for name in chain.__all__:
        assert callable(getattr(chain, name)), name


def test_async_helpers_are_coroutine_functions():
    assert inspect.iscoroutinefunction(chain._load_delivery_for_claim)
    assert inspect.iscoroutinefunction(chain._insert_successor_delivery)
    assert inspect.iscoroutinefunction(chain._lock_delivery_chain)
    assert inspect.iscoroutinefunction(chain._has_successor_attempt)


def test_sync_helpers_are_plain_functions():
    assert not inspect.iscoroutinefunction(chain._record_value)
    assert not inspect.iscoroutinefunction(chain._is_sqlite_connection)
    assert not inspect.iscoroutinefunction(chain._delivery_chain_lock_key)


# ---------------------------------------------------------------------------
# _load_delivery_for_claim
# ---------------------------------------------------------------------------


def test_load_delivery_for_claim_passes_id_and_attempt_cap_four():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    row = _run(chain._load_delivery_for_claim(conn, _DELIVERY_ID))

    assert row == {"id": _DELIVERY_ID}
    kind, sql, args = conn.calls[0]
    assert kind == "fetchrow"
    assert args == (_DELIVERY_ID, 4)
    # The attempt cap is a literal in the SQL as well as a bind parameter.
    assert "$2" in sql
    assert "d.attempt_num <= $2" in sql


def test_load_delivery_for_claim_filter_set_is_pinned():
    conn = _FakeConn(row_results=[None])

    _run(chain._load_delivery_for_claim(conn, _DELIVERY_ID))

    sql = conn.calls[0][1]
    assert "JOIN webhook_subscriptions s ON s.id = d.subscription_id" in sql
    assert "d.scheduled_at <= clock_timestamp()" in sql
    assert "NOT d.superseded" in sql
    assert "d.status IN ('pending', 'retrying')" in sql
    assert "(d.lease_token IS NULL OR d.lease_expires_at < clock_timestamp())" in sql
    assert "d.id = $1::uuid" in sql


def test_load_delivery_for_claim_returns_none_when_no_row_matches():
    conn = _FakeConn(row_results=[None])

    assert _run(chain._load_delivery_for_claim(conn, _DELIVERY_ID)) is None


def test_load_delivery_for_claim_accepts_empty_string_id_verbatim():
    conn = _FakeConn(row_results=[None])

    _run(chain._load_delivery_for_claim(conn, ""))

    assert conn.calls[0][2] == ("", 4)


def test_load_delivery_for_claim_accepts_none_id_verbatim():
    """No client-side validation: a ``None`` id is handed straight to asyncpg."""
    conn = _FakeConn(row_results=[None])

    assert _run(chain._load_delivery_for_claim(conn, None)) is None
    assert conn.calls[0][2] == (None, 4)


def test_load_delivery_for_claim_propagates_driver_errors():
    class _Boom(_FakeConn):
        async def fetchrow(self, sql, *args):
            raise RuntimeError("connection closed")

    with pytest.raises(RuntimeError, match="connection closed"):
        _run(chain._load_delivery_for_claim(_Boom(), _DELIVERY_ID))


# ---------------------------------------------------------------------------
# _record_value
# ---------------------------------------------------------------------------


def test_record_value_reads_a_mapping_by_key():
    assert chain._record_value({"url": "https://example.test/hook"}, "url") == "https://example.test/hook"


def test_record_value_reads_an_object_by_attribute():
    assert chain._record_value(_PlainRecord(owner_id="alice"), "owner_id") == "alice"


def test_record_value_returns_none_for_an_absent_mapping_key():
    assert chain._record_value({"url": "x"}, "namespace") is None


def test_record_value_returns_none_for_an_absent_attribute():
    assert chain._record_value(_PlainRecord(url="x"), "namespace") is None


def test_record_value_preserves_a_none_value():
    assert chain._record_value({"namespace": None}, "namespace") is None


def test_record_value_preserves_falsy_values():
    assert chain._record_value({"namespace": ""}, "namespace") == ""
    assert chain._record_value({"namespace": 0}, "namespace") == 0
    assert chain._record_value({"namespace": False}, "namespace") is False


def test_record_value_swallows_key_error_from_getitem():
    assert chain._record_value(_RaisingRecord(), "url") is None


def test_record_value_swallows_type_error_from_getitem():
    assert chain._record_value(_RaisingTypeErrorRecord(), "url") is None


def test_record_value_on_none_returns_none():
    """A ``None`` record has no ``__getitem__`` and no attributes."""
    assert chain._record_value(None, "url") is None


def test_record_value_on_a_plain_string_uses_getitem():
    """Strings are subscriptable, so they take the ``record[key]`` path.

    A non-index key raises ``TypeError`` inside the ``try`` and is swallowed,
    so a string record quietly yields ``None`` instead of an error.
    """
    assert chain._record_value("ab", 1) == "b"
    assert chain._record_value("ab", "url") is None


def test_record_value_reads_a_class_level_attribute_without_subscripting():
    """A plain class has no ``__getitem__``, so the attribute path is taken."""
    assert hasattr(_ClassAttributeHolder, "__getitem__") is False
    assert chain._record_value(_ClassAttributeHolder, "url") == "https://example.test/class-level"


def test_record_value_isinstance_type_guard_wins_over_a_subscriptable_metaclass():
    """This is what the ``isinstance(record, type)`` guard is for.

    A class whose *metaclass* defines ``__getitem__`` is itself subscriptable,
    so without the guard it would take the mapping path and return the
    metaclass result. The guard forces the attribute lookup instead.
    """
    assert hasattr(_MetaclassHolder, "__getitem__") is True
    assert _MetaclassHolder["url"] == "meta-indexed:url"
    assert chain._record_value(_MetaclassHolder, "url") == "https://example.test/meta-class"


def test_record_value_swallows_attribute_error_from_a_property():
    class _Exploding:
        @property
        def url(self):
            raise AttributeError("no url here")

    assert chain._record_value(_Exploding(), "url") is None


def test_record_value_does_not_swallow_unrelated_exceptions():
    """Only the three documented exception types map to ``None``."""

    class _Exploding:
        @property
        def url(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        chain._record_value(_Exploding(), "url")


def test_record_value_supports_asyncpg_style_mapping_and_attribute_records():
    record = _PlainRecord(url="https://a.test")
    assert chain._record_value(record, "url") == "https://a.test"
    assert chain._record_value({"url": "https://b.test"}, "url") == "https://b.test"


# ---------------------------------------------------------------------------
# _is_sqlite_connection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conn",
    [_SqliteConn(), _AiosqliteConn(), _ModelledSqlite()],
    ids=["sqlite3", "aiosqlite", "sqlite3.dbapi2"],
)
def test_is_sqlite_connection_accepts_the_two_sqlite_module_families(conn):
    assert chain._is_sqlite_connection(conn) is True


@pytest.mark.parametrize(
    "conn",
    [_AsyncpgConn(), _FakeConn(), object()],
    ids=["asyncpg", "local-class", "object"],
)
def test_is_sqlite_connection_rejects_everything_else(conn):
    assert chain._is_sqlite_connection(conn) is False


def test_is_sqlite_connection_is_a_prefix_match_not_an_exact_match():
    """``sqlite3foo`` and ``aiosqlite_extra`` are both treated as SQLite."""

    class _Weird:
        pass

    _Weird.__module__ = "sqlite3foo"
    assert chain._is_sqlite_connection(_Weird()) is True

    class _Weirder:
        pass

    _Weirder.__module__ = "aiosqlite_extra"
    assert chain._is_sqlite_connection(_Weirder()) is True


def test_is_sqlite_connection_is_case_sensitive_on_the_module_name():
    class _Upper:
        pass

    _Upper.__module__ = "Sqlite3"
    assert chain._is_sqlite_connection(_Upper()) is False


def test_is_sqlite_connection_uses_the_type_not_the_instance():
    """An instance attribute named ``__module__`` must not fool the check."""

    conn = _AsyncpgConn()
    conn.__module__ = "sqlite3"  # type: ignore[attr-defined]
    assert chain._is_sqlite_connection(conn) is False


def test_is_sqlite_connection_on_a_class_object_reports_the_metaclass_module():
    """Passing the class rather than an instance inspects ``type``'s module."""
    assert chain._is_sqlite_connection(_FakeConn) is False


# ---------------------------------------------------------------------------
# _delivery_chain_lock_key
# ---------------------------------------------------------------------------

_DELIVERY = {
    "subscription_id": "sub-1",
    "event_type": "memory.created",
    "payload_hash": "abc123",
    "attempt_num": 1,
}


def _expected_key(subscription_id: str, event_type: str, payload_hash: str) -> int:
    digest = hashlib.sha256(f"webhook-chain:{subscription_id}:{event_type}:{payload_hash}".encode("utf-8")).digest()[:8]
    key = int.from_bytes(digest, "big", signed=False)
    return key - 2**64 if key >= 2**63 else key


def test_lock_key_matches_the_documented_sha256_algorithm():
    assert chain._delivery_chain_lock_key(_DELIVERY) == _expected_key("sub-1", "memory.created", "abc123")


def test_lock_key_is_deterministic_across_calls():
    first = chain._delivery_chain_lock_key(_DELIVERY)
    second = chain._delivery_chain_lock_key(dict(_DELIVERY))
    assert first == second


def test_lock_key_is_a_signed_int64():
    key = chain._delivery_chain_lock_key(_DELIVERY)
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63


def test_lock_key_changes_when_any_identity_field_changes():
    base = chain._delivery_chain_lock_key(_DELIVERY)
    for field, value in (
        ("subscription_id", "sub-2"),
        ("event_type", "memory.updated"),
        ("payload_hash", "def456"),
    ):
        assert chain._delivery_chain_lock_key({**_DELIVERY, field: value}) != base, field


def test_lock_key_ignores_fields_outside_the_chain_identity():
    """``attempt_num`` and any extra keys are not part of the identity."""
    base = chain._delivery_chain_lock_key(_DELIVERY)
    assert chain._delivery_chain_lock_key({**_DELIVERY, "attempt_num": 9}) == base
    assert chain._delivery_chain_lock_key({**_DELIVERY, "status": "retrying"}) == base


def test_lock_key_uses_the_documented_namespaced_preimage():
    """The ``webhook-chain:`` prefix is part of the contract."""
    digest = hashlib.sha256("webhook-chain:sub-1:memory.created:abc123".encode("utf-8")).digest()[:8]
    assert chain._delivery_chain_lock_key(_DELIVERY) == int.from_bytes(digest, "big", signed=True)


def test_lock_key_separates_chains_whose_fields_contain_the_separator():
    """Fields are joined with ``:`` unescaped, so a value containing ``:`` is ambiguous in principle.

    Pinned observation: the two plausible boundary-shift collisions below do
    NOT collide today, because sha256 over the shifted preimages differs. The
    ambiguity is a latent hazard for the *textual* preimage (two different
    chains can render the same string only if the field count shifts), not a
    live key collision — so this test records what actually happens.
    """
    left = {"subscription_id": "sub-1", "event_type": "memory.created", "payload_hash": "abc123"}
    right = {"subscription_id": "sub-1:memory.created", "event_type": "abc123", "payload_hash": ""}
    right_preimage = "webhook-chain:sub-1:memory.created:abc123:"
    left_preimage = "webhook-chain:sub-1:memory.created:abc123"
    assert left_preimage + ":" == right_preimage
    assert chain._delivery_chain_lock_key(left) != chain._delivery_chain_lock_key(right)


def test_lock_key_accepts_non_string_field_values_and_stringifies_them():
    key = chain._delivery_chain_lock_key({"subscription_id": 7, "event_type": "e", "payload_hash": "h"})
    assert key == _expected_key("7", "e", "h")


def test_lock_key_missing_field_raises_key_error():
    """There is no defensive default: a missing identity field blows up."""
    with pytest.raises(KeyError, match="subscription_id"):
        chain._delivery_chain_lock_key({"event_type": "e", "payload_hash": "h"})


def test_lock_key_accepts_attribute_style_records():
    """Unlike ``_record_value``, the lock key only subscripts — no attr fallback."""
    record = _PlainRecord(subscription_id="sub-1", event_type="memory.created", payload_hash="abc123")

    with pytest.raises(TypeError):
        chain._delivery_chain_lock_key(record)


def test_lock_key_reuses_an_injected_hashlib_module():
    """The ``_hashlib_mod`` seam exists so callers can pin the digest."""

    class _Stub:
        def __init__(self) -> None:
            self.seen: list[bytes] = []

        def sha256(self, payload: bytes):
            self.seen.append(payload)

            class _Digest:
                @staticmethod
                def digest():
                    return b"\x00" * 8

            return _Digest()

    stub = _Stub()
    assert chain._delivery_chain_lock_key(_DELIVERY, stub) == 0
    assert stub.seen == [b"webhook-chain:sub-1:memory.created:abc123"]


def test_lock_key_maps_high_bit_digests_into_the_negative_int64_range():
    """A digest whose top bit is set must be folded, not truncated."""

    class _Stub:
        @staticmethod
        def sha256(payload: bytes):
            class _Digest:
                @staticmethod
                def digest():
                    return b"\xff" * 8

            return _Digest()

    assert chain._delivery_chain_lock_key(_DELIVERY, _Stub()) == -1


def test_lock_key_maps_a_digest_exactly_at_the_int64_boundary_to_the_negative_floor():
    """``2**63`` (the first value that must be folded) becomes ``-2**63``."""

    class _Stub:
        @staticmethod
        def sha256(payload: bytes):
            class _Digest:
                @staticmethod
                def digest():
                    return (2**63).to_bytes(8, "big")

            return _Digest()

    assert chain._delivery_chain_lock_key(_DELIVERY, _Stub()) == -(2**63)


def test_lock_key_keeps_one_below_the_boundary_positive():
    class _Stub:
        @staticmethod
        def sha256(payload: bytes):
            class _Digest:
                @staticmethod
                def digest():
                    return (2**63 - 1).to_bytes(8, "big")

            return _Digest()

    assert chain._delivery_chain_lock_key(_DELIVERY, _Stub()) == 2**63 - 1


def test_lock_key_returns_zero_for_an_all_zero_digest():
    class _Stub:
        @staticmethod
        def sha256(payload: bytes):
            class _Digest:
                @staticmethod
                def digest():
                    return b"\x00" * 8

            return _Digest()

    assert chain._delivery_chain_lock_key(_DELIVERY, _Stub()) == 0


# ---------------------------------------------------------------------------
# _lock_delivery_chain
# ---------------------------------------------------------------------------


def test_lock_delivery_chain_skips_sqlite_connections():
    conn = _FakeConn()

    assert _run(chain._lock_delivery_chain(_SqliteConn(), _DELIVERY)) is None
    assert conn.calls == []
    assert _run(chain._lock_delivery_chain(_AiosqliteConn(), _DELIVERY)) is None


def test_lock_delivery_chain_takes_the_advisory_lock_on_postgres():
    conn = _AsyncpgConn(execute_result="SELECT 1")

    _run(chain._lock_delivery_chain(conn, _DELIVERY))

    assert len(conn.calls) == 1
    kind, sql, args = conn.calls[0]
    assert kind == "execute"
    assert sql == "SELECT pg_advisory_xact_lock($1)"
    assert args == (chain._delivery_chain_lock_key(_DELIVERY),)


def test_lock_delivery_chain_uses_the_transaction_scoped_variant():
    """Pinned: ``pg_advisory_xact_lock`` (auto-released), never the session form."""
    conn = _AsyncpgConn()

    _run(chain._lock_delivery_chain(conn, _DELIVERY))

    assert "pg_advisory_xact_lock" in conn.calls[0][1]
    assert "pg_advisory_lock" not in conn.calls[0][1].replace("pg_advisory_xact_lock", "")


def test_lock_delivery_chain_propagates_driver_errors():
    class _Boom(_FakeConn):
        async def execute(self, sql, *args):
            raise RuntimeError("deadlock detected")

    with pytest.raises(RuntimeError, match="deadlock detected"):
        _run(chain._lock_delivery_chain(_Boom(), _DELIVERY))


def test_lock_delivery_chain_missing_identity_field_raises_before_executing():
    conn = _FakeConn()

    with pytest.raises(KeyError, match="event_type"):
        _run(chain._lock_delivery_chain(_AsyncpgConn(), {"subscription_id": "s", "payload_hash": "h"}))
    assert conn.calls == []


# ---------------------------------------------------------------------------
# successor / peer existence checks
# ---------------------------------------------------------------------------


def test_has_successor_attempt_binds_the_chain_identity_and_attempt():
    conn = _FakeConn(val_results=[True])

    assert _run(chain._has_successor_attempt(conn, _DELIVERY)) is True
    kind, sql, args = conn.calls[0]
    assert kind == "fetchval"
    assert args == ("sub-1", "memory.created", "abc123", 1)
    assert "newer.attempt_num > $4" in sql


def test_has_successor_attempt_does_not_filter_on_successor_status():
    """Any newer attempt counts, live or dead — that is what distinguishes it
    from ``_has_live_successor_attempt``."""
    conn = _FakeConn(val_results=[True])

    _run(chain._has_successor_attempt(conn, _DELIVERY))

    sql = conn.calls[0][1]
    assert "status IN ('pending', 'retrying')" not in sql
    assert "NOT newer.superseded" not in sql


def test_has_successor_attempt_returns_false_when_the_driver_says_false():
    conn = _FakeConn(val_results=[False])

    assert _run(chain._has_successor_attempt(conn, _DELIVERY)) is False


def test_has_successor_attempt_returns_none_verbatim():
    """No coercion: a ``NULL`` from the driver surfaces as ``None``."""
    conn = _FakeConn(val_results=[None])

    assert _run(chain._has_successor_attempt(conn, _DELIVERY)) is None


def test_has_successor_attempt_missing_field_raises_key_error():
    conn = _FakeConn(val_results=[True])

    with pytest.raises(KeyError, match="payload_hash"):
        _run(chain._has_successor_attempt(conn, {"subscription_id": "s", "event_type": "e"}))


def test_has_live_successor_attempt_binds_the_same_arguments():
    conn = _FakeConn(val_results=[True])

    assert _run(chain._has_live_successor_attempt(conn, _DELIVERY)) is True
    assert conn.calls[0][2] == ("sub-1", "memory.created", "abc123", 1)


def test_has_live_successor_attempt_filters_on_live_status_and_superseded():
    conn = _FakeConn(val_results=[True])

    _run(chain._has_live_successor_attempt(conn, _DELIVERY))

    sql = conn.calls[0][1]
    assert "newer.attempt_num > $4" in sql
    assert "newer.status IN ('pending', 'retrying')" in sql
    assert "NOT newer.superseded" in sql


def test_has_live_successor_attempt_differs_from_the_unfiltered_variant_only_by_the_filter():
    plain = _FakeConn(val_results=[True])
    live = _FakeConn(val_results=[True])

    _run(chain._has_successor_attempt(plain, _DELIVERY))
    _run(chain._has_live_successor_attempt(live, _DELIVERY))

    plain_sql = plain.calls[0][1]
    live_sql = live.calls[0][1]
    stripped = live_sql.replace("AND newer.status IN ('pending', 'retrying')\n", "").replace(
        "AND NOT newer.superseded\n", ""
    )
    assert " ".join(stripped.split()) == " ".join(plain_sql.split())
    assert plain.calls[0][2] == live.calls[0][2]


def test_has_live_successor_attempt_returns_false_and_none_verbatim():
    assert _run(chain._has_live_successor_attempt(_FakeConn(val_results=[False]), _DELIVERY)) is False
    assert _run(chain._has_live_successor_attempt(_FakeConn(val_results=[None]), _DELIVERY)) is None


def test_has_succeeded_chain_attempt_binds_the_delivery_id_last():
    conn = _FakeConn(val_results=[True])

    assert _run(chain._has_succeeded_chain_attempt(conn, _DELIVERY, _DELIVERY_ID)) is True
    kind, sql, args = conn.calls[0]
    assert kind == "fetchval"
    assert args == ("sub-1", "memory.created", "abc123", _DELIVERY_ID)
    assert "peer.status = 'succeeded'" in sql
    assert "peer.id <> $4::uuid" in sql


def test_has_succeeded_chain_attempt_does_not_filter_on_attempt_num():
    """It is a peer check at any attempt, so the delivery's attempt is unused."""
    conn = _FakeConn(val_results=[True])

    _run(chain._has_succeeded_chain_attempt(conn, _DELIVERY, _DELIVERY_ID))

    sql = conn.calls[0][1]
    assert "attempt_num" not in sql
    assert _DELIVERY["attempt_num"] not in conn.calls[0][2]


def test_has_succeeded_chain_attempt_returns_false_and_none_verbatim():
    assert _run(chain._has_succeeded_chain_attempt(_FakeConn(val_results=[False]), _DELIVERY, "d")) is False
    assert _run(chain._has_succeeded_chain_attempt(_FakeConn(val_results=[None]), _DELIVERY, "d")) is None


@pytest.mark.parametrize(
    "helper",
    [chain._has_successor_attempt, chain._has_live_successor_attempt],
)
def test_successor_checks_propagate_driver_errors(helper):
    class _Boom(_FakeConn):
        async def fetchval(self, sql, *args):
            raise ConnectionError("pool closed")

    with pytest.raises(ConnectionError, match="pool closed"):
        _run(helper(_Boom(), _DELIVERY))


# ---------------------------------------------------------------------------
# _insert_successor_delivery
# ---------------------------------------------------------------------------

_SUCCESSOR_SOURCE = {
    "subscription_id": "sub-1",
    "event_type": "memory.created",
    "payload": '{"data":{}}',
    "payload_hash": "abc123",
    "url": "https://example.test/hook",
    "namespace": "alice-ns",
    "owner_id": "alice",
}


def test_insert_successor_returns_the_returned_row_and_binds_every_value(publishers):
    inserted = {"id": _SUCCESSOR_ID}
    conn = _FakeConn(row_results=[inserted])

    row = _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "2026-01-01T00:00:00Z"))

    assert row is inserted
    kind, sql, args = conn.calls[0]
    assert kind == "fetchrow"
    from mnemos.core.webhook_constants import NEW_CODE_WRITER_REVISION

    assert args == (
        "sub-1",
        "memory.created",
        '{"data":{}}',
        "abc123",
        2,
        "2026-01-01T00:00:00Z",
        NEW_CODE_WRITER_REVISION,
    )


def test_insert_successor_binds_the_new_code_writer_revision(publishers):
    from mnemos.core.webhook_constants import NEW_CODE_WRITER_REVISION

    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])

    _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "2026-01-01T00:00:00Z"))

    assert conn.calls[0][2][-1] == NEW_CODE_WRITER_REVISION
    assert NEW_CODE_WRITER_REVISION == 1


def test_insert_successor_hard_codes_pending_status_and_leaves_scheduling_to_the_caller(publishers):
    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])

    _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 5, "scheduled-at"))

    sql = conn.calls[0][1]
    assert "VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7)" in sql
    assert "RETURNING id" in sql
    assert conn.calls[0][2][4] == 5
    assert conn.calls[0][2][5] == "scheduled-at"


def test_insert_successor_uses_a_partial_conflict_target(publishers):
    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])

    _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "t"))

    sql = conn.calls[0][1]
    assert "ON CONFLICT (subscription_id, event_type, payload_hash, attempt_num)" in sql
    assert "WHERE status IN ('pending', 'retrying') AND NOT superseded" in sql
    assert "DO NOTHING" in sql


def test_insert_successor_publishes_both_events_when_the_insert_wins(publishers):
    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])

    _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "t"))

    assert publishers.queued == [
        {
            "delivery_id": _SUCCESSOR_ID,
            "subscription_id": "sub-1",
            "event_type": "memory.created",
            "url": "https://example.test/hook",
            "payload_hash": "abc123",
            "namespace": "alice-ns",
            "owner_id": "alice",
        }
    ]
    assert publishers.outbox == publishers.queued


def test_insert_successor_stringifies_the_returned_id(publishers):
    class _Uuid:
        def __str__(self):
            return _SUCCESSOR_ID

    conn = _FakeConn(row_results=[{"id": _Uuid()}])

    _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "t"))

    assert publishers.queued[0]["delivery_id"] == _SUCCESSOR_ID


def test_insert_successor_publishes_nothing_when_the_conflict_returns_no_row(publishers):
    conn = _FakeConn(row_results=[None])

    assert _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "t")) is None
    assert publishers.queued == []
    assert publishers.outbox == []


def test_insert_successor_defaults_a_missing_url_to_empty_string(publishers):
    source = {k: v for k, v in _SUCCESSOR_SOURCE.items() if k != "url"}
    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])

    _run(chain._insert_successor_delivery(conn, source, 2, "t"))

    assert publishers.queued[0]["url"] == ""
    assert publishers.outbox[0]["url"] == ""


def test_insert_successor_defaults_missing_namespace_and_owner_to_none(publishers):
    source = {k: v for k, v in _SUCCESSOR_SOURCE.items() if k not in ("namespace", "owner_id")}
    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])

    _run(chain._insert_successor_delivery(conn, source, 2, "t"))

    assert publishers.queued[0]["namespace"] is None
    assert publishers.queued[0]["owner_id"] is None


def test_insert_successor_reads_url_namespace_and_owner_off_a_mapping_record(publishers):
    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])

    _run(chain._insert_successor_delivery(conn, dict(_SUCCESSOR_SOURCE), 2, "t"))

    assert publishers.queued[0]["url"] == "https://example.test/hook"


def test_insert_successor_does_not_tolerate_a_record_without_the_payload_fields(publishers):
    """The INSERT arguments are read with plain ``[]``, so a missing key raises."""
    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])
    source = {k: v for k, v in _SUCCESSOR_SOURCE.items() if k != "payload"}

    with pytest.raises(KeyError, match="payload"):
        _run(chain._insert_successor_delivery(conn, source, 2, "t"))
    assert publishers.queued == []


def test_insert_successor_treats_a_falsy_row_as_a_won_race(publishers):
    """Pinned: the guard is ``row is not None``, so a falsy row still publishes.

    ``None`` is the only "lost the race" signal; any other value is treated as
    an inserted row and immediately subscripted for its ``id``. A driver that
    returned ``0``/``""``/``[]`` would therefore fail here rather than being
    silently mistaken for a loss — the failure mode is a loud ``TypeError``.
    """
    conn = _FakeConn(row_results=[0])

    with pytest.raises(TypeError):
        _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "t"))
    assert publishers.queued == []


def test_insert_successor_publishes_for_an_empty_mapping_row_and_fails_on_the_id(publishers):
    conn = _FakeConn(row_results=[{}])

    with pytest.raises(KeyError, match="id"):
        _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "t"))
    assert publishers.queued == []


def test_insert_successor_propagates_publish_failures_after_the_insert(publishers):
    """DEFECT-ish: the row is already committed but the event fanout is fatal.

    The publisher is documented as best-effort ("the Postgres outbox remains
    authoritative"), yet an exception here propagates to the caller after the
    successor row exists, so the retry chain is durable while the caller sees a
    failure. Pinned as current behaviour, not as a fix.
    """
    publishers.fail_with = RuntimeError("nats unavailable")
    conn = _FakeConn(row_results=[{"id": _SUCCESSOR_ID}])

    with pytest.raises(RuntimeError, match="nats unavailable"):
        _run(chain._insert_successor_delivery(conn, _SUCCESSOR_SOURCE, 2, "t"))
    assert len(conn.calls) == 1


def test_insert_successor_propagates_insert_failures_without_publishing(publishers):
    class _Boom(_FakeConn):
        async def fetchrow(self, sql, *args):
            raise RuntimeError("unique violation")

    with pytest.raises(RuntimeError, match="unique violation"):
        _run(chain._insert_successor_delivery(_Boom(), _SUCCESSOR_SOURCE, 2, "t"))
    assert publishers.queued == []


# ---------------------------------------------------------------------------
# _abandon_owned_attempt_after_live_successor
# ---------------------------------------------------------------------------


def test_abandon_after_live_successor_returns_the_updated_row():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    row = _run(chain._abandon_owned_attempt_after_live_successor(conn, _DELIVERY_ID, _LEASE_TOKEN))

    assert row == {"id": _DELIVERY_ID}
    kind, sql, args = conn.calls[0]
    assert kind == "fetchrow"
    assert args == (_DELIVERY_ID, _LEASE_TOKEN)


def test_abandon_after_live_successor_marks_abandoned_superseded_and_clears_the_lease():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    _run(chain._abandon_owned_attempt_after_live_successor(conn, _DELIVERY_ID, _LEASE_TOKEN))

    sql = conn.calls[0][1]
    assert "status='abandoned'" in sql
    assert "superseded=TRUE" in sql
    assert "lease_token=NULL" in sql
    assert "lease_expires_at=NULL" in sql
    assert "RETURNING id" in sql


def test_abandon_after_live_successor_guards_on_an_unexpired_lease():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    _run(chain._abandon_owned_attempt_after_live_successor(conn, _DELIVERY_ID, _LEASE_TOKEN))

    sql = conn.calls[0][1]
    assert "lease_token=$2::uuid" in sql
    assert "lease_expires_at > clock_timestamp()" in sql
    assert "status IN ('pending', 'retrying')" in sql
    assert "NOT superseded" in sql


def test_abandon_after_live_successor_returns_none_when_the_lease_lapsed():
    conn = _FakeConn(row_results=[None])

    assert _run(chain._abandon_owned_attempt_after_live_successor(conn, _DELIVERY_ID, _LEASE_TOKEN)) is None


def test_abandon_after_live_successor_does_stamp_status_updated_at():
    """Pinned: this variant stamps ``status_updated_at`` like the repair SQL."""
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    _run(chain._abandon_owned_attempt_after_live_successor(conn, _DELIVERY_ID, _LEASE_TOKEN))

    assert "status_updated_at=clock_timestamp()" in conn.calls[0][1]


def test_abandon_after_live_successor_accepts_empty_string_ids_verbatim():
    conn = _FakeConn(row_results=[None])

    _run(chain._abandon_owned_attempt_after_live_successor(conn, "", ""))

    assert conn.calls[0][2] == ("", "")


def test_abandon_after_live_successor_propagates_driver_errors():
    class _Boom(_FakeConn):
        async def fetchrow(self, sql, *args):
            raise TimeoutError("lease store unavailable")

    with pytest.raises(TimeoutError, match="lease store unavailable"):
        _run(chain._abandon_owned_attempt_after_live_successor(_Boom(), _DELIVERY_ID, _LEASE_TOKEN))


# ---------------------------------------------------------------------------
# _abandon_current_attempt_after_succeeded_chain_peer
# ---------------------------------------------------------------------------


def test_abandon_current_attempt_executes_an_update_and_returns_none():
    conn = _FakeConn(execute_result="UPDATE 1")

    assert _run(chain._abandon_current_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID)) is None
    kind, _sql, args = conn.calls[0]
    assert kind == "execute"
    assert args == (_DELIVERY_ID,)


def test_abandon_current_attempt_reports_nothing_even_when_zero_rows_match():
    """Pinned: the row count is discarded, so callers cannot tell 0 from 1."""
    conn = _FakeConn(execute_result="UPDATE 0")

    assert _run(chain._abandon_current_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID)) is None


def test_abandon_current_attempt_only_takes_unleased_or_expired_attempts():
    conn = _FakeConn()

    _run(chain._abandon_current_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID))

    sql = conn.calls[0][1]
    assert "(lease_token IS NULL OR lease_expires_at < clock_timestamp())" in sql
    assert "status IN ('pending', 'retrying')" in sql
    assert "NOT superseded" in sql
    assert "status_updated_at=clock_timestamp()" in sql


def test_abandon_current_attempt_takes_no_lease_token():
    """The signature has no lease argument — it cannot prove ownership."""
    conn = _FakeConn()

    _run(chain._abandon_current_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID))

    assert "lease_token=$2::uuid" not in conn.calls[0][1]
    assert len(conn.calls[0][2]) == 1


# ---------------------------------------------------------------------------
# _abandon_owned_attempt_after_succeeded_chain_peer
# ---------------------------------------------------------------------------

_RESULT = _Result(response_status=200, response_body='{"ok":true}', error=None)


def test_abandon_after_peer_with_unexpired_lease_stamps_the_result():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    row = _run(chain._abandon_owned_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID, _LEASE_TOKEN, _RESULT))

    assert row == {"id": _DELIVERY_ID}
    kind, sql, args = conn.calls[0]
    assert kind == "fetchrow"
    assert args == (_DELIVERY_ID, _LEASE_TOKEN, 200, '{"ok":true}', None)
    assert "response_status=$3" in sql
    assert "response_body=$4" in sql
    assert "error=$5" in sql


def test_abandon_after_peer_with_unexpired_lease_defaults_to_requiring_the_lease():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    _run(chain._abandon_owned_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID, _LEASE_TOKEN, _RESULT))

    assert "lease_expires_at >= clock_timestamp()" in conn.calls[0][1]


def test_abandon_after_peer_without_lease_requirement_drops_the_expiry_guard():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    _run(
        chain._abandon_owned_attempt_after_succeeded_chain_peer(
            conn, _DELIVERY_ID, _LEASE_TOKEN, _RESULT, require_unexpired_lease=False
        )
    )

    sql = conn.calls[0][1]
    assert "lease_expires_at >= clock_timestamp()" not in sql
    # The column is still cleared, just no longer used as a WHERE guard.
    assert "lease_expires_at=NULL" in sql
    assert "lease_token=$2::uuid" in sql
    assert "status IN ('pending', 'retrying')" in sql
    assert "NOT superseded" in sql


def test_abandon_after_peer_still_clears_the_lease_columns_in_the_lenient_variant():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    _run(
        chain._abandon_owned_attempt_after_succeeded_chain_peer(
            conn, _DELIVERY_ID, _LEASE_TOKEN, _RESULT, require_unexpired_lease=False
        )
    )

    sql = conn.calls[0][1]
    assert "lease_token=NULL" in sql
    assert "lease_expires_at=NULL" in sql


def test_abandon_after_peer_does_not_stamp_status_updated_at():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    _run(chain._abandon_owned_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID, _LEASE_TOKEN, _RESULT))

    assert "status_updated_at" not in conn.calls[0][1]


def test_abandon_after_peer_requires_an_object_with_the_three_result_attributes():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    with pytest.raises(AttributeError, match="response_status"):
        _run(chain._abandon_owned_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID, _LEASE_TOKEN, object()))


def test_abandon_after_peer_reads_result_attributes_by_plain_access():
    """A mapping with the same keys is NOT accepted — the helper uses attributes."""
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])

    with pytest.raises(AttributeError):
        _run(
            chain._abandon_owned_attempt_after_succeeded_chain_peer(
                conn,
                _DELIVERY_ID,
                _LEASE_TOKEN,
                {"response_status": 200, "response_body": "ok", "error": None},
            )
        )


def test_abandon_after_peer_preserves_a_non_none_error():
    conn = _FakeConn(row_results=[{"id": _DELIVERY_ID}])
    result = _Result(response_status=502, response_body="bad gateway", error="upstream 502")

    _run(chain._abandon_owned_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID, _LEASE_TOKEN, result))

    assert conn.calls[0][2] == (_DELIVERY_ID, _LEASE_TOKEN, 502, "bad gateway", "upstream 502")


def test_abandon_after_peer_returns_none_when_no_row_is_updated():
    conn = _FakeConn(row_results=[None])

    assert (
        _run(chain._abandon_owned_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID, _LEASE_TOKEN, _RESULT)) is None
    )


@pytest.mark.parametrize("require_unexpired_lease", [True, False])
def test_abandon_after_peer_propagates_driver_errors(require_unexpired_lease):
    class _Boom(_FakeConn):
        async def fetchrow(self, sql, *args):
            raise RuntimeError("row is locked")

    with pytest.raises(RuntimeError, match="row is locked"):
        _run(
            chain._abandon_owned_attempt_after_succeeded_chain_peer(
                _Boom(),
                _DELIVERY_ID,
                _LEASE_TOKEN,
                _RESULT,
                require_unexpired_lease=require_unexpired_lease,
            )
        )


# ---------------------------------------------------------------------------
# _find_live_unleased_successor_attempts
# ---------------------------------------------------------------------------


def test_find_live_unleased_successors_returns_the_rows_verbatim():
    rows = [{"id": _SUCCESSOR_ID}, {"id": "another"}]
    conn = _FakeConn(fetch_results=[rows])

    assert _run(chain._find_live_unleased_successor_attempts(conn, _DELIVERY)) == rows
    kind, _sql, args = conn.calls[0]
    assert kind == "fetch"
    assert args == ("sub-1", "memory.created", "abc123", 1)


def test_find_live_unleased_successors_returns_the_empty_list_when_none_match():
    conn = _FakeConn(fetch_results=[[]])

    assert _run(chain._find_live_unleased_successor_attempts(conn, _DELIVERY)) == []


def test_find_live_unleased_successors_returns_none_verbatim():
    """No list coercion on the driver's answer."""
    conn = _FakeConn(fetch_results=[None])

    assert _run(chain._find_live_unleased_successor_attempts(conn, _DELIVERY)) is None


def test_find_live_unleased_successors_filters_and_orders_by_attempt():
    conn = _FakeConn(fetch_results=[[]])

    _run(chain._find_live_unleased_successor_attempts(conn, _DELIVERY))

    sql = conn.calls[0][1]
    assert "newer.attempt_num > $4" in sql
    assert "newer.status IN ('pending', 'retrying')" in sql
    assert "NOT newer.superseded" in sql
    assert "(newer.lease_token IS NULL OR newer.lease_expires_at < clock_timestamp())" in sql
    assert "ORDER BY newer.attempt_num ASC" in sql


def test_find_live_unleased_successors_selects_only_the_id():
    conn = _FakeConn(fetch_results=[[]])

    _run(chain._find_live_unleased_successor_attempts(conn, _DELIVERY))

    assert "SELECT newer.id" in conn.calls[0][1]


def test_find_live_unleased_successors_missing_field_raises_key_error():
    conn = _FakeConn(fetch_results=[[]])

    with pytest.raises(KeyError, match="attempt_num"):
        _run(
            chain._find_live_unleased_successor_attempts(
                conn, {"subscription_id": "s", "event_type": "e", "payload_hash": "h"}
            )
        )


def test_find_live_unleased_successors_propagates_driver_errors():
    class _Boom(_FakeConn):
        async def fetch(self, sql, *args):
            raise RuntimeError("read replica down")

    with pytest.raises(RuntimeError, match="read replica down"):
        _run(chain._find_live_unleased_successor_attempts(_Boom(), _DELIVERY))


# ---------------------------------------------------------------------------
# _abandon_live_successor_attempt
# ---------------------------------------------------------------------------


def test_abandon_live_successor_executes_an_update_and_returns_none():
    conn = _FakeConn(execute_result="UPDATE 1")

    assert _run(chain._abandon_live_successor_attempt(conn, _SUCCESSOR_ID)) is None
    kind, _sql, args = conn.calls[0]
    assert kind == "execute"
    assert args == (_SUCCESSOR_ID,)


def test_abandon_live_successor_targets_only_free_or_expired_attempts():
    conn = _FakeConn()

    _run(chain._abandon_live_successor_attempt(conn, _SUCCESSOR_ID))

    sql = conn.calls[0][1]
    assert "(lease_token IS NULL OR lease_expires_at < clock_timestamp())" in sql
    assert "status IN ('pending', 'retrying')" in sql
    assert "NOT superseded" in sql


def test_abandon_live_successor_marks_the_row_abandoned_and_superseded():
    conn = _FakeConn()

    _run(chain._abandon_live_successor_attempt(conn, _SUCCESSOR_ID))

    sql = conn.calls[0][1]
    assert "status='abandoned'" in sql
    assert "superseded=TRUE" in sql
    assert "status_updated_at=clock_timestamp()" in sql


def test_abandon_live_successor_takes_no_lease_token():
    conn = _FakeConn()

    _run(chain._abandon_live_successor_attempt(conn, _SUCCESSOR_ID))

    assert len(conn.calls[0][2]) == 1


def test_abandon_live_successor_with_empty_id_still_executes():
    """No client-side validation of the id."""
    conn = _FakeConn()

    _run(chain._abandon_live_successor_attempt(conn, ""))

    assert conn.calls[0][2] == ("",)


def test_abandon_live_successor_propagates_driver_errors():
    class _Boom(_FakeConn):
        async def execute(self, sql, *args):
            raise RuntimeError("deadlock detected")

    with pytest.raises(RuntimeError, match="deadlock detected"):
        _run(chain._abandon_live_successor_attempt(_Boom(), _SUCCESSOR_ID))


# ---------------------------------------------------------------------------
# cross-cutting invariants
# ---------------------------------------------------------------------------


def test_every_abandon_update_uses_clock_timestamp_not_now():
    """``now()`` is transaction-start time; these statements need the real clock."""
    conn = _FakeConn(row_results=[{"id": "x"}, {"id": "x"}], execute_result="UPDATE 1")

    _run(chain._abandon_owned_attempt_after_live_successor(conn, _DELIVERY_ID, _LEASE_TOKEN))
    _run(chain._abandon_current_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID))
    _run(chain._abandon_owned_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID, _LEASE_TOKEN, _RESULT))
    _run(chain._abandon_live_successor_attempt(conn, _SUCCESSOR_ID))

    for kind, sql, _args in conn.calls:
        assert "now()" not in sql, kind
        assert "clock_timestamp()" in sql or kind == "fetchrow"


def test_every_abandon_update_ignores_already_superseded_rows():
    conn = _FakeConn(row_results=[{"id": "x"}, {"id": "x"}], execute_result="UPDATE 1")

    _run(chain._abandon_owned_attempt_after_live_successor(conn, _DELIVERY_ID, _LEASE_TOKEN))
    _run(chain._abandon_current_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID))
    _run(chain._abandon_owned_attempt_after_succeeded_chain_peer(conn, _DELIVERY_ID, _LEASE_TOKEN, _RESULT))
    _run(chain._abandon_live_successor_attempt(conn, _SUCCESSOR_ID))

    for _kind, sql, _args in conn.calls:
        assert "NOT superseded" in sql


def test_no_helper_commits_or_rolls_back_on_its_own():
    """Every statement runs inside the caller's transaction."""
    conn = _FakeConn(row_results=[{"id": "x"}], execute_result="UPDATE 1")

    _run(chain._load_delivery_for_claim(conn, _DELIVERY_ID))
    _run(chain._abandon_live_successor_attempt(conn, _SUCCESSOR_ID))

    for _kind, sql, _args in conn.calls:
        assert "COMMIT" not in sql.upper()
        assert "ROLLBACK" not in sql.upper()


def test_helpers_never_touch_the_pool_directly():
    """The ABC-first rule: no ``_lc._pool`` access anywhere in this module."""
    source = Path(chain.__file__).read_text()
    assert "_lc._pool" not in source
    assert Path(chain.__file__).name == "chain.py"
