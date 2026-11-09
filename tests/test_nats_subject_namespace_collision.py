"""MN-17 / CF-35: NATS subject namespace tokens must be collision-free.

A NATS subject segment must not contain ``.``, ``*`` or ``>``, so a
namespace used as the final subject token has to be escaped. The pre-fix
code escaped it by mapping ``.`` -> ``_``, which collides with namespaces
that genuinely contain an underscore::

    "a.b" -> "a_b"
    "a_b" -> "a_b"

Two distinct tenants then share one NATS subject, so an SSE bridge,
federation consumer or webhook trigger scoped to one tenant also receives
the other tenant's events. The fix keeps the established
``.`` -> ``_`` shape but escapes the underscore itself (and the escape
character ``%``, and the NATS wildcards), so the mapping is injective and
the ordinary ``alice.ns`` -> ``alice_ns`` form operators already have in
their dashboards is unchanged.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


# ── the shared encoder ───────────────────────────────────────────────────────


def test_safe_subject_segment_distinguishes_dot_from_underscore():
    from mnemos.persistence.nats_events import safe_subject_segment

    assert safe_subject_segment("a.b") != safe_subject_segment("a_b")


def test_safe_subject_segment_escapes_percent_so_encoding_is_injective():
    from mnemos.persistence.nats_events import safe_subject_segment

    # A namespace that literally contains the escape marker's spelling must
    # not be able to impersonate an encoded namespace either.
    assert safe_subject_segment("a%5Fb") != safe_subject_segment("a_b")


def test_safe_subject_segment_keeps_the_established_subject_shape():
    from mnemos.persistence.nats_events import safe_subject_segment

    # The one-escape rule predates this fix and is what operators already
    # have in dashboards and `mnemos subscribe` filters, so a dotted
    # namespace must keep mapping to its underscored form.
    assert safe_subject_segment("alice.ns") == "alice_ns"
    assert safe_subject_segment("default") == "default"
    assert safe_subject_segment(None) == "default"
    assert safe_subject_segment("") == "default"


def test_safe_subject_segment_never_emits_a_wildcard_or_a_dot():
    from mnemos.persistence.nats_events import safe_subject_segment

    for raw in ("*", ">", "a.*.b", "a.b", "alice.ns"):
        safe = safe_subject_segment(raw)
        assert "." not in safe
        assert "*" not in safe
        assert ">" not in safe


def test_every_namespace_sink_uses_the_shared_encoder():
    """One encoder, no private copies of the ``.`` -> ``_`` mapping."""
    mcp_safe_namespace = _load_mcp_http()._safe_namespace
    from mnemos.nats.webhook_events import safe_namespace as webhook_safe_namespace
    from mnemos.persistence.nats_events import safe_subject_segment

    for fn in (mcp_safe_namespace, webhook_safe_namespace):
        assert fn("a.b") == safe_subject_segment("a.b")
        assert fn("a_b") == safe_subject_segment("a_b")
        assert fn("a.b") != fn("a_b")


@pytest.mark.asyncio
async def test_memory_dedup_deleted_intent_does_not_collide(monkeypatch):
    from mnemos.domain import memory_dedup

    class _Backend:
        supports_webhooks = False

    async def _deleted_intent(row):
        _, intents = await memory_dedup._dispatch_delete_side_effects(_Backend(), object(), row)
        return intents[0][0]

    dot = await _deleted_intent({"id": "m1", "namespace": "a.b", "category": "facts"})
    underscore = await _deleted_intent({"id": "m2", "namespace": "a_b", "category": "facts"})

    assert dot != underscore
    assert "." not in dot.rsplit(".", 1)[1]


# ── the SSE bridge (the reported symptom) ────────────────────────────────────


def _load_mcp_http():
    """Import ``mnemos.mcp.http`` with a stub ``mnemos.mcp.stdio``.

    The real stdio module needs the live ``mcp`` SDK server API; this test
    only exercises the pure subject-encoding helpers, so the single
    ``app`` symbol ``http.py`` imports from stdio is stubbed.
    """
    stub = types.ModuleType("mnemos.mcp.stdio")
    stub.app = object()
    original = sys.modules.get("mnemos.mcp.stdio")
    sys.modules["mnemos.mcp.stdio"] = stub
    try:
        return importlib.import_module("mnemos.mcp.http")
    finally:
        if original is not None:
            sys.modules["mnemos.mcp.stdio"] = original
        else:
            sys.modules.pop("mnemos.mcp.stdio", None)


def test_sse_subject_for_dotted_namespace_is_not_flattened():
    http = _load_mcp_http()
    request = types.SimpleNamespace(query_params={}, state=types.SimpleNamespace())

    dotted = http._parse_nats_sse_subjects(request, http.MCPUserContext(user_id="alice", role="user", namespace="a.b"))
    underscored = http._parse_nats_sse_subjects(
        request, http.MCPUserContext(user_id="bob", role="user", namespace="a_b")
    )

    assert dotted != underscored
    assert dotted[0].startswith("mnemos.*.*.")
    assert "." not in dotted[0].rsplit(".", 1)[1]


@pytest.mark.asyncio
async def test_webhook_delivery_subject_does_not_collide(monkeypatch):
    from mnemos.nats import webhook_events as ne

    published = []

    async def _capture(subject, payload, msg_id=None):
        published.append(subject)

    monkeypatch.setattr(ne.nats_bus, "publish_event", _capture)
    monkeypatch.setattr(ne.nats_client, "get_node_name", lambda: "node-1")

    async def _queue(namespace):
        await ne.publish_delivery_queued(
            delivery_id=f"d-{namespace}",
            subscription_id="s-1",
            event_type="memory.created",
            url="https://hooks.example.com/mnemos",
            payload_hash="deadbeef",
            namespace=namespace,
            owner_id="alice",
        )

    await _queue("a.b")
    await _queue("a_b")

    assert len(published) == 2
    assert published[0] != published[1]
