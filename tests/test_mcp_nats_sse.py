"""MCP HTTP NATS-backed SSE event bridge tests."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types

import pytest

pytestmark = pytest.mark.asyncio


class _FakeMsg:
    def __init__(self, subject: str, payload):
        self.subject = subject
        self.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")


class _FakeSubscription:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.unsubscribed = False
        self.waiting = asyncio.Event()

    async def next_msg(self, timeout=1):
        await asyncio.sleep(0)
        if self.messages:
            item = self.messages.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.waiting.set()
        await asyncio.sleep(60)
        raise asyncio.TimeoutError()

    async def unsubscribe(self):
        self.unsubscribed = True


class _BurstSubscription(_FakeSubscription):
    async def next_msg(self, timeout=1):
        if self.messages:
            item = self.messages.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.waiting.set()
        await asyncio.sleep(60)
        raise asyncio.TimeoutError()


class _FakeJetStream:
    def __init__(self, subscriptions=None):
        self.subscriptions = list(subscriptions or [])
        self.subscribe_calls = []

    async def subscribe(self, subject, **kwargs):
        self.subscribe_calls.append((subject, kwargs))
        if self.subscriptions:
            return self.subscriptions.pop(0)
        return _FakeSubscription()


def _request(subjects: str | None = None, *, principal_id: str = "alice-principal", **params):
    query_params = dict(params)
    if subjects is not None:
        query_params["subjects"] = subjects
    state = types.SimpleNamespace(mnemos_mcp_principal_id=principal_id)
    return types.SimpleNamespace(query_params=query_params, state=state)


def _fresh_http(monkeypatch):
    from mnemos.core import config
    from mnemos.nats import client as nats_client

    monkeypatch.setenv("MNEMOS_MCP_TOKENS", "alice:alice-token")
    config._reset_settings_for_tests()
    monkeypatch.setattr(nats_client, "_jetstream", None)
    sys.modules.pop("mnemos.mcp.http", None)
    return importlib.import_module("mnemos.mcp.http")


def _stub_row_lookup(monkeypatch, http, rows):
    """Replace ``http._fetch_sse_row`` with an in-memory row map.

    The real implementation is now visibility-aware (``memory_id,
    visibility``) and carries a ``cache_negative`` flag for created events;
    tests that only care about the gate logic pass a bare row map and let
    the stub ignore the extra arguments.
    """

    async def _fetch(memory_id, visibility=None, *, cache_negative=True):
        return rows.get(memory_id)

    monkeypatch.setattr(http, "_fetch_sse_row", _fetch)


async def test_sse_non_root_subject_is_derived_from_principal_namespace(monkeypatch):
    http = _fresh_http(monkeypatch)
    http._principal_context_cache["alice-principal"] = http.MCPUserContext(
        user_id="alice", role="user", namespace="alice.ns"
    )
    js = _FakeJetStream()
    monkeypatch.setattr("mnemos.nats.client._jetstream", js)

    response = await http.handle_nats_event_stream(_request("mnemos.memory.>,mnemos.consultation.completed.>"))
    await response.body_iterator.aclose()

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert [call[0] for call in js.subscribe_calls] == ["mnemos.*.*.alice_ns"]


async def test_sse_root_can_use_explicit_subject_filters(monkeypatch):
    http = _fresh_http(monkeypatch)
    http._principal_context_cache["root-principal"] = http.MCPUserContext(user_id="root", role="root", namespace="ops")
    js = _FakeJetStream()
    monkeypatch.setattr("mnemos.nats.client._jetstream", js)

    response = await http.handle_nats_event_stream(
        _request("mnemos.memory.>,mnemos.consultation.completed.>", principal_id="root-principal")
    )
    await response.body_iterator.aclose()

    assert [call[0] for call in js.subscribe_calls] == [
        "mnemos.memory.>",
        "mnemos.consultation.completed.>",
    ]


async def test_nats_message_becomes_sse_frame(monkeypatch):
    http = _fresh_http(monkeypatch)
    _stub_row_lookup(
        monkeypatch,
        http,
        {"mem_1": {"id": "mem_1", "owner_id": "alice", "namespace": "default", "permission_mode": 600}},
    )
    sub = _FakeSubscription(
        [
            _FakeMsg(
                "mnemos.memory.created.default",
                {"memory_id": "mem_1", "source_node": "PYTHIA", "content": "remote note"},
            )
        ]
    )
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))

    response = await http.handle_nats_event_stream(_request())
    frame = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    text = frame.decode("utf-8")
    assert "event: mnemos.memory.created.default\n" in text
    assert "data: " in text
    assert "mem_1" in text
    assert "PYTHIA" in text
    assert "remote note" not in text


async def test_client_disconnect_unsubscribes(monkeypatch):
    http = _fresh_http(monkeypatch)
    _stub_row_lookup(
        monkeypatch,
        http,
        {"mem_1": {"id": "mem_1", "owner_id": "alice", "namespace": "default", "permission_mode": 600}},
    )
    sub = _FakeSubscription([_FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_1"})])
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))

    response = await http.handle_nats_event_stream(_request())
    await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert sub.unsubscribed is True

async def test_sse_drops_memory_message_without_row_id(monkeypatch):
    """Fail-closed: a memory-subject message naming no row cannot be gated, so it is dropped."""
    http = _fresh_http(monkeypatch)
    _stub_row_lookup(monkeypatch, http, {})
    sub = _FakeSubscription(
        [_FakeMsg("mnemos.memory.created.shared_ns", {"namespace": "shared.ns", "category": "note"})]
    )
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))
    http._principal_context_cache["alice-principal"] = http.MCPUserContext(
        user_id="alice", role="user", namespace="shared.ns"
    )

    response = await http.handle_nats_event_stream(_request())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(response.body_iterator), timeout=0.2)
    finally:
        await response.body_iterator.aclose()

    assert sub.unsubscribed is True



async def test_nats_down_returns_503(monkeypatch):
    http = _fresh_http(monkeypatch)
    monkeypatch.setattr("mnemos.nats.client._jetstream", None)

    response = await http.handle_nats_event_stream(_request())

    assert response.status_code == 503
    assert response.body == b"NATS unavailable"


async def test_raw_payload_requires_operator_opt_in(monkeypatch):
    http = _fresh_http(monkeypatch)
    _stub_row_lookup(
        monkeypatch,
        http,
        {"mem_1": {"id": "mem_1", "owner_id": "alice", "namespace": "default", "permission_mode": 600}},
    )
    sub = _FakeSubscription(
        [_FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_1", "content": "remote note"})]
    )
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))
    monkeypatch.setenv("MNEMOS_MCP_NATS_RAW", "true")

    response = await http.handle_nats_event_stream(_request())
    frame = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert "remote note" in frame.decode("utf-8")


async def test_sse_reports_dropped_events_for_lagging_client(monkeypatch):
    http = _fresh_http(monkeypatch)
    monkeypatch.setattr(http, "NATS_SSE_QUEUE_MAXSIZE", 1)
    _stub_row_lookup(
        monkeypatch,
        http,
        {
            f"mem_{i}": {
                "id": f"mem_{i}",
                "owner_id": "alice",
                "namespace": "default",
                "permission_mode": 600,
            }
            for i in range(5)
        },
    )
    sub = _BurstSubscription([_FakeMsg("mnemos.memory.created.default", {"memory_id": f"mem_{i}"}) for i in range(5)])

    stream = http._nats_sse_event_source([sub])
    seen = []
    for _ in range(5):
        frame = await anext(stream)
        seen.append(frame.decode("utf-8"))
        if "event: dropped" in seen[-1]:
            break
    await stream.aclose()

    assert any("event: dropped" in item for item in seen)


# ---------------------------------------------------------------------------
# Namespace -> subject segment encoding.
#
# Invariants:
#
#  1. The encoding is injective: distinct namespaces (`a.b`, `a_b`, `a-b`,
#     `A.B`) map to distinct NATS subjects, so a subscriber pinned to one
#     namespace only receives that namespace's event metadata.
#  2. Readable-but-not-owned rows: `read_visibility_predicate` (and
#     `VisibilityFilter.for_read`) make world/group/ACL-readable rows in the
#     caller's namespace visible to a non-owner. The memory.{created,updated,
#     deleted} payloads carry `memory_id` + `category`, which are delivered
#     only for rows the caller can read through the REST surface.
# ---------------------------------------------------------------------------


def _subscribe_subject(http, monkeypatch, namespace, *, role="user"):
    js = _FakeJetStream()
    monkeypatch.setattr("mnemos.nats.client._jetstream", js)
    http._principal_context_cache["alice-principal"] = http.MCPUserContext(
        user_id="alice", role=role, namespace=namespace
    )
    return js


async def test_row_gate_allows_rowless_non_memory_in_own_namespace(monkeypatch):
    """A row-less message on a non-memory subject is allowed IF (and only IF)
    the namespace gate has pinned it to the caller's own namespace.

    This pins the fail-open-for-control-frames behavior that is already
    namespace-bounded: the namespace gate (``_namespace_gate``) runs first and
    rejects every foreign/wildcard/unknown subject, so a non-memory event that
    reaches the row gate is already in the caller's own namespace. A row-less
    message on a *memory* subject, by contrast, is dropped -- it cannot be
    row-gated and is indistinguishable from a tampered payload.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )
    # Non-memory class in the caller's own namespace: namespace gate passes,
    # no row to gate, so allow (e.g. a namespace-scoped control frame).
    msg_control = _FakeMsg("mnemos.control.created.shared_ns", {"op": "ping"})
    assert await http._nats_sse_row_allowed(msg_control, vis) is True

    # Same non-memory class, but a foreign namespace: namespace gate rejects.
    msg_foreign = _FakeMsg("mnemos.control.created.other_ns", {"op": "ping"})
    assert await http._nats_sse_row_allowed(msg_foreign, vis) is False

    # Memory class with no row id in the own namespace: fail-closed deny.
    msg_memory_norow = _FakeMsg("mnemos.memory.created.shared_ns", {"category": "x"})
    assert await http._nats_sse_row_allowed(msg_memory_norow, vis) is False


async def test_row_gate_resolves_row_before_allowing(monkeypatch):
    """When a row id is present, the gate resolves it through the repository
    (filtered by the caller's own REST visibility) and applies
    ``allows_row``; an unresolvable row -- invisible, or lookup failure --
    fails closed for created/updated events.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )
    # Stub row lookup: own row readable, other row not, missing id -> None.
    rows = {
        "mem_own": {"namespace": "shared.ns", "owner_id": "u-alice", "permission_mode": 600},
        "mem_other": {"namespace": "shared.ns", "owner_id": "bob", "permission_mode": 600},
    }
    _stub_row_lookup(monkeypatch, http, rows)

    assert await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_own"}), vis) is True
    assert await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_other"}), vis) is False
    # Row missing/invisible for a created event: fail-closed deny.
    assert await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_gone"}), vis) is False
    # A failed lookup (DB error returns None from _fetch_sse_row) is also
    # denied: a missing authorization decision is never an allowed event.
    async def _fail(memory_id, visibility=None, *, cache_negative=True):
        return None

    monkeypatch.setattr(http, "_fetch_sse_row", _fail)
    assert await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_gone"}), vis) is False


async def test_row_gate_delete_requires_prior_allow_for_this_caller(monkeypatch):
    """``memory.deleted`` events are NOT authorized on the namespace gate
    alone: the payload ``memory_id`` is publisher-controlled (NATS federates
    the subject family, so origin is not trustworthy), and the vanished row
    offers no repository check.

    The gate instead consults the bounded per-caller allow-cache -- ids for
    which this caller was already shown a row-gated created/updated frame --
    and drops every uncached id. The row lookup must never run for a delete.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )
    http._sse_delete_allowed_ids.clear()

    calls = []

    async def _fetch(memory_id, visibility=None, *, cache_negative=True):
        calls.append(memory_id)
        return None  # deleted row: the repository returns None by design

    monkeypatch.setattr(http, "_fetch_sse_row", _fetch)

    # Delete for an id this caller was never shown: dropped (no allow entry,
    # and the lookup must never run).
    assert (
        await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.deleted.shared_ns", {"memory_id": "mem_forged"}), vis)
        is False
    )
    assert calls == []

    # Once the caller is allowed a created frame for an id, a delete for that
    # same id is delivered -- still without running the row lookup.
    http._record_sse_delete_allowed("mem_known", vis)
    assert (
        await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.deleted.shared_ns", {"memory_id": "mem_known"}), vis)
        is True
    )
    assert calls == []

    # Same delete event, foreign namespace: namespace gate rejects.
    assert (
        await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.deleted.other_ns", {"memory_id": "mem_known"}), vis)
        is False
    )
    assert calls == []


async def test_delete_allow_cache_caps_per_caller_ids_fifo(monkeypatch):
    """The per-caller allow-set is capped at ``_SSE_DELETE_ALLOW_CACHE_MAX``
    and evicts in INSERTION order (oldest first), deterministically.

    Eviction follows insertion order, never hash order, so the surviving ids
    do not depend on ``PYTHONHASHSEED`` and the cache keeps the bounded
    stale-id window it promises.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )
    http._sse_delete_allowed_ids.clear()
    monkeypatch.setattr(http, "_SSE_DELETE_ALLOW_CACHE_MAX", 4)

    for i in range(6):
        http._record_sse_delete_allowed(f"mem_{i}", vis)

    ids = http._sse_delete_allowed_ids[http._sse_delete_allow_key(vis)][0]
    assert len(ids) == 4
    # Oldest two evicted; newest four retained, in insertion order.
    assert list(ids) == ["mem_2", "mem_3", "mem_4", "mem_5"]
    assert http._sse_delete_allowed("mem_5", vis) is True
    assert http._sse_delete_allowed("mem_0", vis) is False

    # Re-recording an id already in the set must not reorder or grow it.
    http._record_sse_delete_allowed("mem_2", vis)
    assert list(http._sse_delete_allowed_ids[http._sse_delete_allow_key(vis)][0]) == [
        "mem_2",
        "mem_3",
        "mem_4",
        "mem_5",
    ]


async def test_sweep_handles_delete_allow_three_tuple(monkeypatch):
    """``_sweep_expired_row_visibility`` must understand the delete-allow
    map's real 3-tuple ``(ids, expires_at, epoch)`` value.

    A 2-tuple unpack raised ``ValueError`` on the FIRST sweep, so expired
    delete-allow entries were never removed -- and the sweep runs on the hot
    SSE path (cap enforcement, provider-cache TTL expiry), taking the whole
    stream down. Pin both the live and expired shapes.
    """
    http = _fresh_http(monkeypatch)
    http._sse_delete_allowed_ids.clear()

    def _vis(user):
        return http._SSERowVisibility(
            bypass=False,
            user_id=user,
            group_ids=(),
            namespace="shared.ns",
            exclude_namespaces=(),
        )

    live = _vis("u_live")
    expired = _vis("u_expired")
    http._record_sse_delete_allowed("mem_live", live)
    http._record_sse_delete_allowed("mem_expired", expired)

    # Every value is a 3-tuple (ids, expires_at, epoch_at_write).
    for entry in http._sse_delete_allowed_ids.values():
        assert isinstance(entry, tuple) and len(entry) == 3

    # Backdate the "expired" entry past its TTL; leave the live one alone.
    ids, _expires_at, epoch = http._sse_delete_allowed_ids[http._sse_delete_allow_key(expired)]
    http._sse_delete_allowed_ids[http._sse_delete_allow_key(expired)] = (
        ids,
        http._monotonic() - 1.0,
        epoch,
    )

    # Must not raise, and must drop exactly the expired entry.
    http._sweep_expired_row_visibility()

    assert http._sse_delete_allow_key(expired) not in http._sse_delete_allowed_ids
    assert http._sse_delete_allow_key(live) in http._sse_delete_allowed_ids
    assert http._sse_delete_allowed("mem_live", live) is True


async def test_delete_allow_cache_map_is_capped(monkeypatch):
    """The per-caller MAP itself is capped, so per-stream callers cannot grow
    it without bound; eviction drops the oldest callers first.
    """
    http = _fresh_http(monkeypatch)
    http._sse_delete_allowed_ids.clear()
    monkeypatch.setattr(http, "_SSE_DELETE_ALLOW_CACHE_MAX", 2)

    def _vis(user):
        return http._SSERowVisibility(
            bypass=False,
            user_id=user,
            group_ids=(),
            namespace="shared.ns",
            exclude_namespaces=(),
        )

    for user in ("u1", "u2", "u3"):
        http._record_sse_delete_allowed("mem_x", _vis(user))

    assert len(http._sse_delete_allowed_ids) <= 2
    assert http._sse_delete_allow_key(_vis("u1")) not in http._sse_delete_allowed_ids
    assert http._sse_delete_allowed("mem_x", _vis("u3")) is True


async def test_delete_allow_epoch_invalidation(monkeypatch):
    """An epoch advance invalidates the delete-allow entry; the caller must
    be re-authorized before a delete frame is delivered again.

    The epoch is an in-process high-water mark that, in degraded mode, rolls
    forward from a wall-clock bucket every few seconds. Because it never
    reads the shared store, an out-of-band advance (e.g. another process
    picked up the visibility bump) can leave a stale allow-set in memory.
    Stamping the entry with the epoch at write time and comparing it on
    read/write therefore prevents replaying deletes for rows the caller
    can no longer see. A real local visibility bump additionally clears
    the cache via ``principal_cache_invalidate()``.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )
    http._sse_delete_allowed_ids.clear()
    monkeypatch.setattr(http, "_visibility_epoch", lambda: 0)
    http._record_sse_delete_allowed("mem_known", vis)
    assert http._sse_delete_allowed("mem_known", vis) is True

    # The observed epoch rolls (degraded mode: every 5s). The entry was
    # stamped with epoch 0, so it is now stale: deny and drop.
    monkeypatch.setattr(http, "_visibility_epoch", lambda: 5)
    assert http._sse_delete_allowed("mem_known", vis) is False

    # Re-authorization under the new epoch restores the allow-set.
    http._record_sse_delete_allowed("mem_known", vis)
    assert http._sse_delete_allowed("mem_known", vis) is True

    # A REAL visibility bump clears the cache via the epoch listener, so an
    # id authorized before the bump is not replayed after it.
    http.principal_cache_invalidate()
    assert http._sse_delete_allowed("mem_known", vis) is False


async def test_updated_event_also_skips_negative_cache(monkeypatch):
    """``memory.updated`` races commit/replication lag exactly like
    ``memory.created``: a negative-cached miss would suppress the update for
    a full TTL. The gate must pass ``cache_negative=False`` for both.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )

    seen: dict[str, bool] = {}

    async def _fetch(memory_id, visibility=None, *, cache_negative=True):
        seen[memory_id] = cache_negative
        return None

    monkeypatch.setattr(http, "_fetch_sse_row", _fetch)

    await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "m_created"}), vis)
    await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.updated.shared_ns", {"memory_id": "m_updated"}), vis)

    assert seen["m_created"] is False
    assert seen["m_updated"] is False


async def test_delete_frame_drops_category_but_keeps_id(monkeypatch):
    """A ``memory.deleted`` frame is delivered on the namespace gate alone, so
    the row's category was never visibility-checked; it must not be shipped.

    All three delete publishers (REST, dedup, webhook) include ``category``
    in the event payload, so a caller sharing the namespace would otherwise
    learn the topic of another owner's just-deleted private row.
    """
    http = _fresh_http(monkeypatch)
    frame = http._nats_sse_data(
        _FakeMsg(
            "mnemos.memory.deleted.shared_ns",
            {"memory_id": "mem_gone", "namespace": "shared.ns", "category": "credentials-plan", "source_node": "PYTHIA"},
        )
    )
    payload = json.loads(frame)
    assert payload["memory_id"] == "mem_gone"
    assert payload["namespace"] == "shared.ns"
    assert payload["source_node"] == "PYTHIA"
    assert "category" not in payload, "delete frames must not leak the row category"

    # A non-delete memory event still summarizes its category.
    created = json.loads(
        http._nats_sse_data(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_1", "category": "note"}))
    )
    assert created["category"] == "note"


async def test_delete_frame_drops_category_even_for_wildcard_namespace(monkeypatch):
    """The category strip must hold for EVERY delete subject shape, not just
    the plain ``mnemos.memory.deleted.<ns>`` one.

    The published namespace segment is the encoder output, which
    percent-encodes broker wildcards (``*`` -> ``%2A``). A caller whose own
    namespace IS ``*`` or ``>`` therefore receives ``mnemos.memory.deleted.%2A``
    / ``...%3E`` on its own subscription. The strip compares the subject
    segments (``mnemos.memory.deleted``), never the raw ``*``/``>`` characters,
    and holds for a raw, unencoded delegation subject too.
    """
    http = _fresh_http(monkeypatch)
    payload_body = {"memory_id": "mem_gone", "category": "credentials-plan", "source_node": "PYTHIA"}
    for subject in (
        "mnemos.memory.deleted.%2A",
        "mnemos.memory.deleted.%3E",
        "mnemos.memory.deleted.*",
        "mnemos.memory.deleted.a%20b",
    ):
        frame = http._nats_sse_data(_FakeMsg(subject, dict(payload_body)))
        payload = json.loads(frame)
        assert payload["memory_id"] == "mem_gone", subject
        assert "category" not in payload, f"delete frame on {subject!r} must not leak the row category"

    # Sanity: a sibling CLASS on a memory subject is not a delete and keeps
    # its category (the strip is delete-specific, not a blanket drop).
    kept = json.loads(http._nats_sse_data(_FakeMsg("mnemos.memory.deleted_extra.shared_ns", {"category": "note"})))
    assert kept["category"] == "note"


async def test_explicit_legacy_principal_skips_the_row_gate(monkeypatch):
    """Only the EXPLICIT legacy principal (trusted-network / shared-token
    edge) skips the row gate.

    The legacy flag is set by ``_sse_row_visibility`` only when the MCP edge
    resolved the caller with no REST identity at all -- MCP auth disabled, or
    the single shared MNEMOS_MCP_TOKEN presented. The row gate must skip on
    that flag alone: there is no per-user identity to gate rows against, and
    running the gate would drop every non-world-readable event and silently
    break trusted-network deployments. The namespace gate still applies.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=("vault",),
        legacy_principal=True,
    )

    calls = []

    async def _fetch(memory_id, visibility=None, *, cache_negative=True):
        calls.append(memory_id)
        # A row the anonymous caller "owns" nothing of: an owner-only row.
        return {"id": memory_id, "owner_id": "bob", "namespace": "shared.ns", "permission_mode": 600}

    monkeypatch.setattr(http, "_fetch_sse_row", _fetch)

    # Own-namespace created event: delivered, and the row lookup never runs.
    assert (
        await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_x"}), vis)
        is True
    )
    assert calls == []

    # Foreign namespace is still rejected by the namespace gate.
    assert (
        await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.other_ns", {"memory_id": "mem_x"}), vis)
        is False
    )
    assert calls == []


async def test_unresolved_identity_fails_closed_not_legacy(monkeypatch):
    """A non-root caller whose identity was not resolved is NOT legacy.

    ``role=admin``/``monitor`` with ``user_id=None`` (and a token user whose
    OAuth lookup failed) resolves to ``user_id=""`` with ``bypass=False`` and
    ``legacy_principal=False``. The row gate must apply to them, never skip
    it on an empty ``user_id`` -- otherwise they receive every memory event
    of the namespace, including ids and categories of other owners'
    permission_mode=600 rows.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=("vault",),
    )

    calls = []

    async def _fetch(memory_id, visibility=None, *, cache_negative=True):
        calls.append(memory_id)
        # An owner-only row in the shared namespace, owned by someone else.
        return {"id": memory_id, "owner_id": "bob", "namespace": "shared.ns", "permission_mode": 600}

    monkeypatch.setattr(http, "_fetch_sse_row", _fetch)

    # Own-namespace created event: the gate runs (lookup happens) and denies,
    # because the caller has no owner/world/group claim on the row.
    assert (
        await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_x"}), vis)
        is False
    )
    assert calls == ["mem_x"]

    # A world-readable row in the shared namespace IS delivered for this
    # caller -- the gate applies, it does not bypass.
    async def _fetch_world(memory_id, visibility=None, *, cache_negative=True):
        calls.append(memory_id)
        return {"id": memory_id, "owner_id": "bob", "namespace": "shared.ns", "permission_mode": 644}

    monkeypatch.setattr(http, "_fetch_sse_row", _fetch_world)
    calls.clear()
    assert (
        await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_pub"}), vis)
        is True
    )
    assert calls == ["mem_pub"]


async def test_created_event_delivers_for_legacy_principal_end_to_end(monkeypatch):
    """The pre-branch end-to-end path: a created event on the caller's own
    namespace reaches the stream for a legacy (anonymous) principal.

    Pins delivery for the trusted-network deployment class: the row gate
    passes events for a legacy principal.
    """
    http = _fresh_http(monkeypatch)
    _stub_row_lookup(
        monkeypatch,
        http,
        {"mem_1": {"id": "mem_1", "owner_id": "someone-else", "namespace": "default", "permission_mode": 600}},
    )
    sub = _FakeSubscription([_FakeMsg("mnemos.memory.created.default", {"memory_id": "mem_1", "source_node": "PYTHIA"})])
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))

    response = await http.handle_nats_event_stream(_request())
    try:
        frame = await asyncio.wait_for(anext(response.body_iterator), timeout=1)
    finally:
        await response.body_iterator.aclose()

    assert "mem_1" in frame.decode("utf-8")


async def test_row_gate_only_applies_to_memory_class(monkeypatch):
    """The row gate must not run for non-memory classes, even when the payload
    carries an ``id`` (or ``memory_id``) field.

    Any generic ``id`` on a consultation/control frame is not a memory-row
    reference; looking it up in the memories table misses and wrongly drops
    the event. Non-memory classes have no row to gate, so they are allowed
    once the namespace gate has pinned them to the caller's own namespace.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )

    called = []

    async def _fetch(memory_id, visibility=None, *, cache_negative=True):
        called.append(memory_id)
        return None

    monkeypatch.setattr(http, "_fetch_sse_row", _fetch)

    # Non-memory class with an ``id`` field in the own namespace: allowed,
    # and the lookup must never have run.
    assert await http._nats_sse_row_allowed(_FakeMsg("mnemos.consultation.completed.shared_ns", {"id": "some-id"}), vis) is True
    assert called == []

    # Non-memory class carrying ``memory_id`` in the own namespace: still
    # not row-gated.
    assert await http._nats_sse_row_allowed(_FakeMsg("mnemos.control.created.shared_ns", {"memory_id": "mem_x"}), vis) is True
    assert called == []

    # Foreign namespace with an ``id``: namespace gate rejects.
    assert await http._nats_sse_row_allowed(_FakeMsg("mnemos.consultation.completed.other_ns", {"id": "some-id"}), vis) is False

    # Memory class in the own namespace with a missing row id: fail-closed
    # deny (unchanged contract).
    assert await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"category": "x"}), vis) is False
    assert called == []


async def test_namespace_segments_are_injective(monkeypatch):
    """`a.b` and `a_b` must not share a subject; the mapping must be injective."""
    http = _fresh_http(monkeypatch)
    dotted = http._safe_namespace("a.b")
    underscored = http._safe_namespace("a_b")
    assert dotted != underscored
    # The two namespaces must never meet in the middle: encoding is
    # injective, so distinct inputs yield distinct segments.
    assert dotted != underscored
    assert "." not in dotted
    # A single subject segment: no '.' means no wildcard/segment splitting.
    assert "." not in dotted and "." not in underscored
    # Collation must not merge distinct namespaces either.
    assert http._safe_namespace("a-b") not in {dotted, underscored}
    assert http._safe_namespace("alice") == "alice"
    # The safe-encode form is stable for segments that contain no reserved
    # characters at all (e.g. "alice"), but not for ones containing '_'
    # (which percent-encodes to %5F). We only need injectivity here.
    assert http._safe_namespace("alice") == "alice"


async def test_nats_sse_is_delete_requires_exactly_four_segments(monkeypatch):
    """The delete-subject check must reject malformed namespace segments.

    It is a SHAPE check, not a fixed-point check: the published namespace
    segment is ``_safe_namespace``'s output, and a real dotted/underscored
    namespace (``shared.ns`` -> ``shared_ns``) is not a fixed point of that
    encoder. Requiring one rejected the common case and disabled the
    category-stripping branch for real tenants.
    """
    http = _fresh_http(monkeypatch)
    # Valid: exactly four segments, non-empty namespace segment.
    assert http._nats_sse_is_delete("mnemos.memory.deleted.alice") is True
    # Dotted namespace publishes as its encoded form (``shared.ns`` ->
    # ``shared_ns``); both the encoded form and an already-encoded
    # ``%5F`` form are valid delete targets.
    assert http._nats_sse_is_delete("mnemos.memory.deleted.alice_ns") is True
    assert http._nats_sse_is_delete("mnemos.memory.deleted.shared_ns") is True
    assert http._nats_sse_is_delete("mnemos.memory.deleted.a_b") is True
    assert http._nats_sse_is_delete("mnemos.memory.deleted.a%5Fb") is True
    # Missing namespace (three segments).
    assert http._nats_sse_is_delete("mnemos.memory.deleted") is False
    # Empty namespace.
    assert http._nats_sse_is_delete("mnemos.memory.deleted.") is False
    # Extra trailing segments.
    assert http._nats_sse_is_delete("mnemos.memory.deleted.alice.extra") is False
    # Wrong prefix.
    assert http._nats_sse_is_delete("mnemos.memory.deleted2.alice") is False
    assert http._nats_sse_is_delete("other.memory.deleted.alice") is False
    # Non-string subject.
    assert http._nats_sse_is_delete(None) is False
    assert http._nats_sse_is_delete(123) is False
    # Namespace containing a dot (four segments become five after split).
    assert http._nats_sse_is_delete("mnemos.memory.deleted.a.b") is False
    # Wildcard/whitespace characters are PERCENT-ENCODED in the published
    # segment (``*`` -> ``%2A``), so a caller whose own namespace is ``*``
    # legitimately receives ``mnemos.memory.deleted.%2A``. The shape helper
    # must not reject it.
    assert http._nats_sse_is_delete("mnemos.memory.deleted.%2A") is True
    assert http._nats_sse_is_delete("mnemos.memory.deleted.%3E") is True
    assert http._nats_sse_is_delete("mnemos.memory.deleted.a%20b") is True


async def test_sse_subject_does_not_collide_across_namespaces(monkeypatch):
    http = _fresh_http(monkeypatch)
    subjects = set()
    for namespace in ("a.b", "a_b"):
        js = _subscribe_subject(http, monkeypatch, namespace)
        response = await http.handle_nats_event_stream(_request())
        await response.body_iterator.aclose()
        ((subject, _kwargs),) = js.subscribe_calls
        # mnemos.<class>.<action>.<namespace>, namespace being ONE token.
        assert subject.startswith("mnemos.*.*."), subject
        assert subject.count(".") == 3, subject
        subjects.add(subject)
    assert len(subjects) == 2


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_namespace_gate_compares_the_real_published_subject_shape(monkeypatch):
    """The gate must actually run on ``mnemos.<class>.<action>.<namespace>``.

    That shape has THREE dots, so the namespace is the LAST segment and the
    comparison runs on it, never on a tail that still contains dots. This pins
    the behaviour on the exact shape ``_parse_nats_sse_subjects`` builds.
    """
    http = _fresh_http(monkeypatch)
    visibility = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="alice",
        exclude_namespaces=(),
    )

    def _msg(subject):
        return types.SimpleNamespace(subject=subject)

    # Own namespace, and the encoded form of a namespace with a dot.
    assert http._namespace_gate(_msg("mnemos.memory.created.alice"), visibility) is True
    # ``a.b`` encodes to ``a_b``; an ``alice`` stream must not accept it.
    assert http._namespace_gate(_msg("mnemos.memory.created.a_b"), visibility) is False
    # A different namespace is rejected -- the whole point of the gate.
    assert http._namespace_gate(_msg("mnemos.memory.created.bob"), visibility) is False
    # Wildcarded subjects can never carry a valid encoded namespace token,
    # so they fail closed here. Operators bypass this gate entirely via
    # ``visibility.bypass``, which is the only path that subscribes with
    # wildcard filters.
    assert http._namespace_gate(_msg("mnemos.*.*.bob"), visibility) is False
    # A wildcard in the class/action slots with a foreign namespace still fails.
    assert http._namespace_gate(_msg("mnemos.memory.*.bob"), visibility) is False
    # Malformed shapes this bridge does not publish are rejected, not deferred.
    assert http._namespace_gate(_msg("mnemos.memory.created"), visibility) is False
    assert http._namespace_gate(_msg("other.memory.created.alice"), visibility) is False


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_namespace_gate_accepts_the_encoded_form_of_a_dotted_namespace(monkeypatch):
    """A stream pinned to ``a.b`` matches its own encoded segment only."""
    http = _fresh_http(monkeypatch)
    visibility = http._SSERowVisibility(
        bypass=False,
        user_id="u",
        group_ids=(),
        namespace="a.b",
        exclude_namespaces=(),
    )
    encoded = http._safe_namespace("a.b")
    assert http._namespace_gate(types.SimpleNamespace(subject=f"mnemos.memory.created.{encoded}"), visibility) is True
    # The other half of the old collision must not be accepted.
    assert (
        http._namespace_gate(
            types.SimpleNamespace(subject=f"mnemos.memory.created.{http._safe_namespace('a_b')}"),
            visibility,
        )
        is False
    )


def test_allows_row_uses_acl_read_bit_and_missing_group_is_denied(monkeypatch):
    """Direct contract: world/group bits use ``READ_BIT`` (= 4).

    A group-readable row that carries no ``group_id`` has no membership to
    check against -- it must fall through to the safe ``False`` deny, and is
    never treated as world-readable.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=("grp-1",),
        namespace="shared.ns",
        exclude_namespaces=(),
    )
    # World-readable (ones digit >= 4, i.e. READ_BIT).
    assert vis.allows_row({"namespace": "shared.ns", "permission_mode": 644}) is True
    # Group-readable (tens digit >= 4) with a matching group_id.
    assert vis.allows_row({"namespace": "shared.ns", "permission_mode": 640, "group_id": "grp-1"}) is True
    # Group-readable (tens digit >= 4) with the WRONG group_id.
    assert vis.allows_row({"namespace": "shared.ns", "permission_mode": 640, "group_id": "grp-2"}) is False
    # Group-readable (tens digit >= 4) but group_id is MISSING -> safe deny.
    assert vis.allows_row({"namespace": "shared.ns", "permission_mode": 640, "group_id": None}) is False
    # Owner-only (600): neither world nor group readable.
    assert vis.allows_row({"namespace": "shared.ns", "permission_mode": 600, "group_id": "grp-1"}) is False
    # Namespace mismatch always denies, even when the bits would allow.
    assert vis.allows_row({"namespace": "other.ns", "permission_mode": 644}) is False


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_permission_mode_is_decimal_encoded_octal_not_bitmask(monkeypatch):
    """Lock the representation contract against a silent storage change.

    ``permission_mode`` is stored as a DECIMAL integer whose digits are octal
    (``755`` means octal 755; ``600`` means octal 600). It is NOT a Unix
    bitmask (``0o755`` == 493). If the persistence layer ever switches to
    storing ``0o755`` (493), the digit-extraction math in
    ``allows_row`` (``% 10``, ``(// 10) % 10``) -- which mirrors the REST
    predicate in ``mnemos/core/visibility.py`` -- will silently mis-extract
    bits. This test pins the current contract.
    """
    from mnemos.api.read_policy import _validate_permission_mode

    # The validator accepts the decimal-encoded octal representation.
    assert _validate_permission_mode(755) == 755
    assert _validate_permission_mode(640) == 640
    assert _validate_permission_mode(600) == 600
    # The validator rejects values that would not be valid octal digits.
    with pytest.raises(Exception):
        _validate_permission_mode(493)  # 0o755 as a bitmask; digit '9' is not octal

    # allows_row must use the digit-extraction math, not bitmask operators.
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False, user_id="u-alice", group_ids=(),
        namespace="ns", exclude_namespaces=(),
    )
    # 644: world digit is 4 (>= READ_BIT) -> world-readable, allow.
    assert vis.allows_row({"namespace": "ns", "permission_mode": 644}) is True
    # 493 (= 0o755 as a bitmask): world digit is 3 (< READ_BIT) -> deny,
    # proving the gate is NOT interpreting the value as a bitmask.
    assert vis.allows_row({"namespace": "ns", "permission_mode": 493}) is False


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_visibility_epoch_is_monotonic_int_and_survives_failure(monkeypatch):
    """``_visibility_epoch`` always returns ``int`` and never regresses.

    The high-water mark advances via ``max`` with the previous value. If the
    epoch source is broken (import or call fails), the function keeps the
    last successful value -- it does NOT reset to ``0`` or return ``None``.
    On the very first failure (no prior success) it returns ``0``, which is
    still an ``int`` and satisfies the ``-> int`` signature.
    """
    import mnemos.core.lifecycle as lifecycle
    http = _fresh_http(monkeypatch)

    # Normal advance.
    monkeypatch.setattr(lifecycle, "visibility_epoch_cached", lambda: 5)
    assert http._visibility_epoch() == 5
    assert isinstance(http._visibility_epoch(), int)

    # A broken source keeps the last known value (5); it does not fall back to 0.
    def _raise():
        raise RuntimeError("epoch store is down")

    monkeypatch.setattr(lifecycle, "visibility_epoch_cached", _raise)
    assert http._visibility_epoch() == 5
    assert isinstance(http._visibility_epoch(), int)

    # Once recovered, it advances past the old high-water mark.
    monkeypatch.setattr(lifecycle, "visibility_epoch_cached", lambda: 7)
    assert http._visibility_epoch() == 7


async def test_sse_row_gate_cache_is_bounded_and_epoch_scoped(monkeypatch):
    """``_fetch_sse_row`` caches per (id, caller, epoch) for both positive
    and negative results -- no per-event query storm, and a visibility bump
    never replays a stale decision.

    Only the persistence backend is faked; the real cache, the epoch key,
    and the visibility-filtered ABC path (``get_memory``) are exercised.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )

    calls = {"n": 0}

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _MemRepo:
        async def get_memory(self, tx, memory_id, *, visibility=None):
            calls["n"] += 1
            if memory_id == "mem_1":
                return {"id": "mem_1", "namespace": "shared.ns", "owner_id": "alice"}
            return None

    class _Backend:
        def __init__(self):
            self.memories = _MemRepo()

        def transactional(self):
            return _Tx()

    import mnemos.core.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "get_persistence_backend", lambda: _Backend())
    monkeypatch.setattr(http, "_visibility_epoch", lambda: 1)
    http._sse_row_cache.clear()

    first = await http._fetch_sse_row("mem_1", vis)
    second = await http._fetch_sse_row("mem_1", vis)
    assert first == second == {"id": "mem_1", "namespace": "shared.ns", "owner_id": "alice"}
    assert calls["n"] == 1, "second call must hit the cache"

    # Negative result (row missing/deleted/invisible) is cached too.
    assert await http._fetch_sse_row("mem_gone", vis) is None
    assert await http._fetch_sse_row("mem_gone", vis) is None
    assert calls["n"] == 2

    # Epoch advance = different cache key; the envelope cache and the row
    # cache are invalidated together by the same epoch listener.
    monkeypatch.setattr(http, "_visibility_epoch", lambda: 2)
    assert await http._fetch_sse_row("mem_gone", vis) is None
    assert calls["n"] == 3


async def test_sse_row_gate_skips_negative_cache_for_created_events(monkeypatch):
    """``cache_negative=False`` (used for ``memory.created``) must not cache a
    momentary miss: a row inserted just before the event may not be visible
    to ``get_memory`` yet, and a cached ``None`` would suppress every
    subsequent event for that id for up to the full TTL.
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )

    calls = {"n": 0}

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _MemRepo:
        async def get_memory(self, tx, memory_id, *, visibility=None):
            calls["n"] += 1
            return None

    class _Backend:
        def __init__(self):
            self.memories = _MemRepo()

        def transactional(self):
            return _Tx()

    import mnemos.core.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "get_persistence_backend", lambda: _Backend())
    monkeypatch.setattr(http, "_visibility_epoch", lambda: 1)
    http._sse_row_cache.clear()

    # Two misses with negative caching disabled: the backend is hit twice,
    # so nothing was cached.
    assert await http._fetch_sse_row("mem_lag", vis, cache_negative=False) is None
    assert await http._fetch_sse_row("mem_lag", vis, cache_negative=False) is None
    assert calls["n"] == 2
    key = ("mem_lag", "alice", "shared.ns", 1)
    assert key not in http._sse_row_cache

    # Default (cache_negative=True): the miss IS cached, so the second
    # lookup hits the cache and does not reach the backend.
    assert await http._fetch_sse_row("mem_gone", vis) is None
    assert await http._fetch_sse_row("mem_gone", vis) is None
    assert calls["n"] == 3
    assert ("mem_gone", "alice", "shared.ns", 1) in http._sse_row_cache


async def test_sse_does_not_deliver_other_users_rows_in_namespace(monkeypatch):
    """Non-owner rows that are merely readable must not leak id/category."""
    http = _fresh_http(monkeypatch)
    _stub_row_lookup(
        monkeypatch,
        http,
        {
            "mem_other_user": {
                "id": "mem_other_user",
                "owner_id": "bob",
                "namespace": "shared.ns",
                # owner-only: alice shares the namespace but cannot read it.
                "permission_mode": 600,
            }
        },
    )
    sub = _FakeSubscription(
        [
            _FakeMsg(
                "mnemos.memory.created.shared_ns",
                {
                    "memory_id": "mem_other_user",
                    "namespace": "shared.ns",
                    "category": "credentials-plan",
                    "source_node": "PYTHIA",
                },
            )
        ]
    )
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))
    http._principal_context_cache["alice-principal"] = http.MCPUserContext(
        user_id="alice", role="user", namespace="shared.ns"
    )

    response = await http.handle_nats_event_stream(_request())
    try:
        with pytest.raises(TimeoutError):
            # The only message on the stream is the other user's row: nothing
            # may be delivered, so the next frame must time out.
            await asyncio.wait_for(anext(response.body_iterator), timeout=0.2)
    finally:
        await response.body_iterator.aclose()

    assert sub.unsubscribed is True


async def test_sse_delivers_own_and_readable_rows(monkeypatch):
    """The row gate narrows; it must not blank out the caller's own traffic."""
    http = _fresh_http(monkeypatch)
    _stub_row_lookup(
        monkeypatch,
        http,
        {
            "mem_mine": {"id": "mem_mine", "owner_id": "alice", "namespace": "shared.ns", "permission_mode": 600},
            "mem_world": {"id": "mem_world", "owner_id": "bob", "namespace": "shared.ns", "permission_mode": 604},
            "mem_vault": {"id": "mem_vault", "owner_id": "alice", "namespace": "vault", "permission_mode": 600},
        },
    )
    sub = _FakeSubscription(
        [
            _FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_mine"}),
            _FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_world"}),
            _FakeMsg("mnemos.memory.created.vault", {"memory_id": "mem_vault"}),
        ]
    )
    monkeypatch.setattr("mnemos.nats.client._jetstream", _FakeJetStream([sub]))
    http._principal_context_cache["alice-principal"] = http.MCPUserContext(
        user_id="alice", role="user", namespace="shared.ns"
    )

    response = await http.handle_nats_event_stream(_request())
    frames = []
    try:
        for _ in range(2):
            frames.append((await asyncio.wait_for(anext(response.body_iterator), timeout=1)).decode("utf-8"))
        # The third queued message (`mem_vault`) sits behind a namespace gate
        # that is not the caller's own; it must be dropped. Attempt to read a
        # third frame -- if the gate had wrongly allowed it, `mem_vault` would
        # be delivered instead of timing out.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(response.body_iterator), timeout=0.3)
    finally:
        await response.body_iterator.aclose()

    delivered = " ".join(frames)
    assert "mem_mine" in frames[0]
    assert "mem_world" in frames[1]
    # A namespace gate that is not the caller's own can never deliver, even
    # when the row itself would be readable.
    assert "mem_vault" not in delivered


async def test_sse_row_visibility_cache_invalidates_on_epoch(monkeypatch):
    http = _fresh_http(monkeypatch)
    context = http.MCPUserContext(user_id="alice", role="user", namespace="shared.ns")
    monkeypatch.setattr(http, "_visibility_epoch", lambda: 1)
    first = http._cached_row_visibility(context)
    assert http._cached_row_visibility(context) is first

    monkeypatch.setattr(http, "_visibility_epoch", lambda: 2)
    assert http._cached_row_visibility(context) is not first

    http.principal_cache_invalidate()
    assert http._row_visibility_cache == {}


async def test_row_gate_bounds_slow_backend_lookup(monkeypatch):
    """A hung persistence lookup is bounded by ``asyncio.wait_for`` in the
    gate, so one slow backend call cannot stall every SSE stream. The
    timeout produces a missing authorization decision, which fails closed
    (drop).
    """
    http = _fresh_http(monkeypatch)
    vis = http._SSERowVisibility(
        bypass=False,
        user_id="u-alice",
        group_ids=(),
        namespace="shared.ns",
        exclude_namespaces=(),
    )

    async def _hang(memory_id, visibility=None, *, cache_negative=True):
        await asyncio.sleep(60)  # simulates a stuck backend
        return None

    monkeypatch.setattr(http, "_fetch_sse_row", _hang)
    monkeypatch.setattr(http, "_SSE_ROW_LOOKUP_TIMEOUT_SECONDS", 0.05)

    # The gate must deny quickly (timeout -> None -> fail-closed) rather
    # than hang for the full 60s.
    import time as _time

    started = _time.monotonic()
    allowed = await http._nats_sse_row_allowed(_FakeMsg("mnemos.memory.created.shared_ns", {"memory_id": "mem_slow"}), vis)
    elapsed = _time.monotonic() - started
    assert allowed is False
    assert elapsed < 5, f"lookup was not bounded: took {elapsed:.2f}s"


async def test_sse_operator_is_exempt_from_the_row_gate(monkeypatch):
    """Root/operator contexts skip the row-visibility envelope entirely.

    ``handle_nats_event_stream`` resolves one ``_SSERowVisibility`` per stream
    (line 1473) unless the caller is an operator. This test installs a
    tracking flag on ``_cached_row_visibility``: if the gate runs, the flag
    flips and the stream fails. The stream must stay alive and subscribe to
    the wildcard subject the operator requested.
    """
    http = _fresh_http(monkeypatch)
    state = {"called": False}

    def _should_not_be_called(_ctx):
        state["called"] = True
        pytest.fail("operators bypass _cached_row_visibility")

    monkeypatch.setattr(http, "_cached_row_visibility", _should_not_be_called)

    js = _FakeJetStream()
    monkeypatch.setattr("mnemos.nats.client._jetstream", js)
    http._principal_context_cache["root-principal"] = http.MCPUserContext(user_id="root", role="root", namespace="ops")
    response = await http.handle_nats_event_stream(_request("mnemos.memory.>", principal_id="root-principal"))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(response.body_iterator), timeout=0.2)
    finally:
        await response.body_iterator.aclose()

    # The visibility resolver must never have run for an operator.
    assert state["called"] is False
    # The operator's explicit wildcard subscription was honored.
    assert [call[0] for call in js.subscribe_calls] == ["mnemos.memory.>"]



