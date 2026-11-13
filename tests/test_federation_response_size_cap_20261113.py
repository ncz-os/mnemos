"""Outbound federation pull must bound the peer response body (NG2-MN-004).

The pull path used to read a peer response with ``r.json()``, which makes
httpx buffer the WHOLE body in memory before a single byte is parsed. The
only bound on a peer's answer was ``FEDERATION_HTTP_TIMEOUT`` (time) and
``FEDERATION_BATCH_LIMIT`` (rows the peer is *asked* for) -- neither bounds
the bytes a peer actually sends. A registered peer (or anything that can
answer on its URL after the DNS pin) could therefore return a multi-GB JSON
body and take the worker process down; in the default profile the same
worker also serves the API.

The inbound side already had a bound (``_BodySizeLimitASGI`` in
``mnemos/api/main.py``); the outbound side had none. This suite pins the
outbound bound:

* the response is read as a STREAM with a byte cap, so the process never
  holds more than the cap (plus one chunk) of peer payload in memory;
* an over-cap response is a typed failure
  (``FederationSyncError``) naming the byte count, never a partial apply;
* the cap is settings-driven (``FEDERATION_MAX_RESPONSE_BYTES``) so an
  operator can widen or narrow it without a rebuild.

The fake peer is an ``httpx.MockTransport`` that streams a body larger than
the cap, and records how many bytes it was actually able to push before the
client hung up -- that number is the evidence that the read is bounded
rather than buffered.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mnemos.core import config as core_config
from mnemos.domain import federation


pytestmark = pytest.mark.asyncio


_CAP_ENV = "FEDERATION_MAX_RESPONSE_BYTES"


def _reset_settings(monkeypatch, *, max_response_bytes: int | None = None) -> None:
    """Rebuild the settings singleton around a federation response cap."""
    if max_response_bytes is None:
        monkeypatch.delenv(_CAP_ENV, raising=False)
    else:
        monkeypatch.setenv(_CAP_ENV, str(max_response_bytes))
    monkeypatch.setattr(core_config, "_settings", None)


class _StreamingPeer:
    """A fake peer that streams a body of ``total_bytes`` and counts what it pushed.

    The handler yields the body in chunks, so a client that reads it
    incrementally stops receiving as soon as it hangs up -- the pushed
    count is therefore the proof that the read was bounded.
    """

    def __init__(self, total_bytes: int, *, chunk: int = 64 * 1024):
        self.total_bytes = total_bytes
        self.chunk = chunk
        self.pushed = 0
        self.requests: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        total = self.total_bytes
        chunk_size = self.chunk

        async def body():
            pushed = 0
            while pushed < total:
                size = min(chunk_size, total - pushed)
                pushed += size
                self.pushed = pushed
                yield b"x" * size

        return httpx.Response(200, content=body())


def _install_transport(monkeypatch, handler) -> None:
    """Route the federation module's HTTP through the fake peer."""
    transport = httpx.MockTransport(handler)

    async def _safe_client(url, *, timeout=30.0, allow_private=False):
        return httpx.AsyncClient(transport=transport), object()

    monkeypatch.setattr(federation, "make_safe_client", _safe_client)


class TestPullBatchResponseCap:
    """``_pull_batch`` must not buffer an unbounded peer body."""

    async def test_over_cap_feed_raises_typed_error(self, monkeypatch):
        """An over-cap feed body is a typed failure, not a silent OOM."""
        _reset_settings(monkeypatch, max_response_bytes=4096)
        peer = _StreamingPeer(total_bytes=1_000_000)
        _install_transport(monkeypatch, peer)

        with pytest.raises(federation.FederationSyncError) as exc:
            await federation._pull_batch(
                base_url="https://peer.example",
                auth_token="sek",
                since=None,
                namespace_filter=None,
                category_filter=None,
            )

        message = str(exc.value)
        assert "peer response exceeds" in message
        assert "4096" in message

    async def test_over_cap_feed_never_buffers_the_whole_body(self, monkeypatch):
        """The client hangs up at the cap: the peer cannot push the rest.

        This is the memory bound. A buffered read would consume the entire
        advertised body before parsing anything.
        """
        _reset_settings(monkeypatch, max_response_bytes=4096)
        peer = _StreamingPeer(total_bytes=5_000_000)
        _install_transport(monkeypatch, peer)

        with pytest.raises(federation.FederationSyncError):
            await federation._pull_batch(
                base_url="https://peer.example",
                auth_token="sek",
                since=None,
                namespace_filter=None,
                category_filter=None,
            )

        # The peer got to push only a bounded prefix, not all 5 MB.
        assert peer.pushed <= 4096 + peer.chunk

    async def test_under_cap_feed_still_works(self, monkeypatch):
        """A normal page is unaffected: the cap is a ceiling, not a budget."""
        _reset_settings(monkeypatch, max_response_bytes=1_000_000)
        payload = {
            "memories": [{"id": "mem-1", "content": "hello"}],
            "next_cursor": None,
            "has_more": False,
        }
        body = json.dumps(payload).encode("utf-8")

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        _install_transport(monkeypatch, handler)

        memories, next_cursor, has_more = await federation._pull_batch(
            base_url="https://peer.example",
            auth_token="sek",
            since=None,
            namespace_filter=None,
            category_filter=None,
        )

        assert [m["id"] for m in memories] == ["mem-1"]
        assert next_cursor is None
        assert has_more is False

    async def test_default_cap_is_bounded_and_configurable(self, monkeypatch):
        """The default cap exists, is finite, and the env var overrides it."""
        _reset_settings(monkeypatch)
        default_cap = federation._federation_max_response_bytes()
        assert default_cap > 0
        assert default_cap <= 64 * 1024 * 1024

        _reset_settings(monkeypatch, max_response_bytes=12345)
        assert federation._federation_max_response_bytes() == 12345


class TestByIdResponseCap:
    """``pull_memory_by_id`` must not buffer an unbounded peer body either."""

    async def test_over_cap_by_id_raises_typed_error(self, monkeypatch):
        _reset_settings(monkeypatch, max_response_bytes=4096)
        peer = _StreamingPeer(total_bytes=1_000_000)
        _install_transport(monkeypatch, peer)

        with pytest.raises(federation.FederationSyncError) as exc:
            await federation.pull_memory_by_id(
                base_url="https://peer.example",
                auth_token="sek",
                memory_id="mem-1",
                namespace_filter=None,
                category_filter=None,
            )

        assert "peer response exceeds" in str(exc.value)
        assert peer.pushed <= 4096 + peer.chunk


class TestSchemaResponseCap:
    """``_check_peer_schema`` must not buffer an unbounded peer body either."""

    async def test_over_cap_schema_is_transient_not_durable(self, monkeypatch):
        """An over-cap schema body is a transient failure of the peer.

        Durable (transient=False) would burn the peer's whole sync
        interval on a body the peer could trivially have kept small.
        """
        _reset_settings(monkeypatch, max_response_bytes=4096)
        peer = _StreamingPeer(total_bytes=1_000_000)
        _install_transport(monkeypatch, peer)

        result = await federation._check_peer_schema(
            base_url="https://peer.example",
            auth_token="sek",
            name="peer-a",
        )

        assert result["ok"] is False
        assert result["transient"] is True
        assert "peer response exceeds" in result["reason"]
        assert peer.pushed <= 4096 + peer.chunk


class TestSyncPeerOverCap:
    """An over-cap peer must be recorded as a transient sync failure."""

    async def test_sync_records_transient_failure_with_peer_name(self, monkeypatch):
        """``last_error`` names the peer and the cap; no rows are applied."""
        from tests.test_federation_replica_features import FakeReplicaBackend

        _reset_settings(monkeypatch, max_response_bytes=4096)
        peer = _StreamingPeer(total_bytes=1_000_000)
        _install_transport(monkeypatch, peer)

        backend = FakeReplicaBackend()
        monkeypatch.setattr(
            federation,
            "_check_peer_schema",
            lambda *a, **k: _async_schema_ok(),
        )
        monkeypatch.setattr(
            federation,
            "post_peer_fingerprint",
            lambda *a, **k: _async_fingerprint_ok(),
        )

        with pytest.raises(federation.FederationSyncError):
            await federation.sync_peer(backend, backend.federation.peer["id"])

        last_error = backend.federation.peer["last_error"]
        assert last_error is not None
        assert "peer response exceeds" in last_error
        assert "peer" in last_error
        # Nothing was applied: an over-cap body is never a partial apply.
        assert backend.federation.rows == {}
        assert backend.federation.peer["consecutive_failures"] == 1


async def _async_schema_ok() -> dict[str, Any]:
    """A peer /schema answer that passes this node's strict preflight.

    The migrations fingerprint must be this tree's real one: a same-major
    peer that does not expose it is a strict abort long before the pull
    path runs, and the test would then pass for the wrong reason.
    """
    from mnemos._version import __version__
    from mnemos.domain.federation import _local_migrations_fingerprint

    parts = __version__.split(".")
    return {
        "ok": True,
        "mnemos_version": __version__,
        "schema_signature": f"{parts[0]}.{parts[1]}",
        "migrations_fingerprint": _local_migrations_fingerprint(),
    }


async def _async_fingerprint_ok() -> dict[str, Any]:
    return {"ok": True, "supported": True}
