"""Edge cases of the bounded peer read (NG2-MN-004 follow-up).

The first cut of the outbound cap read the body as a stream but returned the
response OBJECT while still inside ``async with client.stream(...)``. httpx
closes the response on exit, so every caller's ``response.raise_for_status()``
raised whichever error the closed object produced instead of the peer's real
status; a peer answering 500 to a feed pull was reported through a response
that no longer describes the request. These tests pin the caller contract:

* an HTTP status is reported as an ``httpx.HTTPStatusError`` carrying that
  status, including when the error body is HTML rather than JSON;
* the status is decided before the body is used, so a caller can never act on
  a ``None`` body;
* a 2xx that is not JSON is a peer defect, not a crash in the reconcile.
"""

from __future__ import annotations

import httpx
import pytest

from mnemos.domain import federation


pytestmark = pytest.mark.asyncio


def _install(monkeypatch, handler, *, max_response_bytes: int = 64 * 1024 * 1024) -> None:
    """Route the federation module's HTTP through a fake peer at a fixed cap."""
    monkeypatch.setenv("FEDERATION_MAX_RESPONSE_BYTES", str(max_response_bytes))
    from mnemos.core import config as core_config

    monkeypatch.setattr(core_config, "_settings", None)

    transport = httpx.MockTransport(handler)

    async def _safe_client(url, *, timeout=None, allow_private=False, **_kwargs):
        return httpx.AsyncClient(transport=transport), object()

    monkeypatch.setattr(federation, "make_safe_client", _safe_client)


async def test_feed_http_error_keeps_the_peer_status_code(monkeypatch):
    """A 500 from the feed is an HTTPStatusError carrying the peer's status."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"detail": "peer says no"}')

    _install(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await federation._pull_batch(
            base_url="https://peer.example",
            auth_token="sek",
            since=None,
            namespace_filter=None,
            category_filter=None,
        )

    assert exc.value.response.status_code == 500


async def test_by_id_http_error_keeps_the_peer_status_code(monkeypatch):
    """The by-id path reports the peer's 500 identically."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"detail": "peer says no"}')

    _install(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await federation.pull_memory_by_id(
            base_url="https://peer.example",
            auth_token="sek",
            memory_id="mem-1",
            namespace_filter=None,
            category_filter=None,
        )

    assert exc.value.response.status_code == 500


async def test_a_non_json_error_body_still_reports_the_status(monkeypatch):
    """An error body is not parsed at all, so its shape cannot mask the status.

    A proxy error page in front of the peer is a 5xx with HTML in it; the
    caller must see the status, never a JSON decode failure from a body it
    was told it does not need.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>bad gateway</html>")

    _install(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await federation._pull_batch(
            base_url="https://peer.example",
            auth_token="sek",
            since=None,
            namespace_filter=None,
            category_filter=None,
        )

    assert exc.value.response.status_code == 500


async def test_by_id_404_and_401_keep_their_meaning(monkeypatch):
    """404 is "not exportable" (empty list); 401 is an auth failure."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"detail": "gone"}')

    _install(monkeypatch, handler)
    assert (
        await federation.pull_memory_by_id(
            base_url="https://peer.example",
            auth_token="sek",
            memory_id="mem-1",
            namespace_filter=None,
            category_filter=None,
        )
        == []
    )

    async def unauth(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"detail": "no"}')

    _install(monkeypatch, unauth)
    with pytest.raises(RuntimeError, match="401"):
        await federation.pull_memory_by_id(
            base_url="https://peer.example",
            auth_token="sek",
            memory_id="mem-1",
            namespace_filter=None,
            category_filter=None,
        )


async def test_schema_server_error_is_still_transient(monkeypatch):
    """A 5xx /schema answer is transient even when the body is not read."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"<html>unavailable</html>")

    _install(monkeypatch, handler)

    result = await federation._check_peer_schema(
        base_url="https://peer.example",
        auth_token="sek",
        name="peer-a",
    )

    assert result["ok"] is False
    assert result["transient"] is True
    assert "503" in result["reason"]


async def test_redispatch_survives_a_non_json_success_body(monkeypatch):
    """A 200 that is not JSON is a peer defect, not a crash in the reconcile."""
    from mnemos.domain.federation import parity

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>proxy error page</html>")

    _install(monkeypatch, handler)

    assert await parity._redispatch_on_source("https://peer.example", "sek", ["mem-1"]) == 0


async def test_redispatch_over_cap_body_is_one_clear_failure(monkeypatch):
    """An over-cap redispatch answer is a RuntimeError naming the cap."""
    from mnemos.domain.federation import parity

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b" " * 200_000 + b"}")

    _install(monkeypatch, handler, max_response_bytes=4096)

    with pytest.raises(RuntimeError, match="peer response exceeds 4096 bytes"):
        await parity._redispatch_on_source("https://peer.example", "sek", ["mem-1"])


async def test_over_cap_schema_body_counts_toward_the_cap_while_streaming(monkeypatch):
    """The schema preflight stops reading at the cap, it does not buffer first."""
    pushed = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            while pushed["n"] < 5_000_000:
                pushed["n"] += 64 * 1024
                yield b"x" * (64 * 1024)

        return httpx.Response(200, content=body())

    _install(monkeypatch, handler, max_response_bytes=4096)

    result = await federation._check_peer_schema(
        base_url="https://peer.example",
        auth_token="sek",
        name="peer-a",
    )

    assert result["transient"] is True
    assert pushed["n"] <= 4096 + 64 * 1024


async def test_fingerprint_exchange_body_is_capped(monkeypatch):
    """The embedder-fingerprint POST runs on the same tick and is capped too.

    Peers that pre-date the endpoint answer 404/405 with no body to read;
    a 412 carries ``detail``. Neither may be a place to buffer an unbounded
    peer body, and an over-cap answer is unverifiable rather than fatal.
    """
    pushed = {"n": 0}

    async def too_big(request: httpx.Request) -> httpx.Response:
        async def body():
            while pushed["n"] < 5_000_000:
                pushed["n"] += 64 * 1024
                yield b"x" * (64 * 1024)

        return httpx.Response(412, headers={"content-length": str(5_000_000)}, content=body())

    _install(monkeypatch, too_big, max_response_bytes=4096)
    verdict = await federation.post_peer_fingerprint(
        "https://peer.example",
        "sek",
        {"embed_model": "m", "embed_dim": 768},
    )

    assert verdict["ok"] is False
    assert verdict["supported"] is True
    assert "peer response exceeds 4096 bytes" in verdict["reason"]
    # The declared length is refused before the body: not one chunk was read.
    assert pushed["n"] == 0

    async def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, content=b'{"detail": "width mismatch"}')

    _install(monkeypatch, rejected)
    verdict = await federation.post_peer_fingerprint(
        "https://peer.example",
        "sek",
        {"embed_model": "m", "embed_dim": 768},
    )
    assert verdict == {"ok": False, "supported": True, "status": 412, "reason": "width mismatch"}


async def test_fingerprint_exchange_keeps_its_status_meanings(monkeypatch):
    """404/405 are "unsupported", other errors are unverifiable, 2xx is ok."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _install(monkeypatch, handler)
    assert await federation.post_peer_fingerprint("https://peer.example", "sek", {}) == {
        "ok": True,
        "supported": False,
        "status": 404,
    }

    async def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>boom</html>")

    _install(monkeypatch, broken)
    verdict = await federation.post_peer_fingerprint("https://peer.example", "sek", {})
    assert verdict["ok"] is False
    assert verdict["status"] == 500

    async def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"accepted": true}')

    _install(monkeypatch, ok)
    verdict = await federation.post_peer_fingerprint("https://peer.example", "sek", {})
    assert verdict["ok"] is True
    assert verdict["supported"] is True
    assert verdict["body"] == {"accepted": True}
