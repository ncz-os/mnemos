from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from fastapi import HTTPException

from mnemos.core import net_validation, safe_http


class FakeStream:
    def __init__(self, response):
        self.response = response
        self.writes = []

    async def read(self, max_bytes, timeout=None):
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        pass

    def get_extra_info(self, info):
        return None


@pytest.fixture
def network(monkeypatch):
    stream = FakeStream(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
    backend = httpcore.AsyncNetworkBackend()
    backend.connect_tcp = AsyncMock(return_value=stream)
    backend.connect_unix_socket = AsyncMock(return_value=stream)
    monkeypatch.setattr(safe_http, "AutoBackend", lambda: backend)
    return backend, stream


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_host", "expected_host"),
    [("peer.example", "203.0.113.7"), ("PEER.EXAMPLE.", "203.0.113.7"), ("other.example", "other.example")],
)
async def test_transport_pins_only_matching_hostname(network, request_host, expected_host):
    backend, stream = network
    options = [(1, 2, 3)]
    transport = safe_http.PinnedDNSAsyncHTTPTransport(
        hostname="PEER.EXAMPLE.",
        resolved_ip="203.0.113.7",
        local_address="127.0.0.1",
        socket_options=options,
    )
    async with httpx.AsyncClient(transport=transport, timeout=4) as client:
        response = await client.get(f"http://{request_host}:8080/path")

    assert response.status_code == 200
    assert response.text == "OK"
    backend.connect_tcp.assert_awaited_once_with(
        host=expected_host,
        port=8080,
        timeout=4,
        local_address="127.0.0.1",
        socket_options=options,
    )
    assert b"GET /path HTTP/1.1" in b"".join(stream.writes)


@pytest.mark.asyncio
async def test_transport_forwards_unix_socket_options(network):
    backend, _ = network
    options = [(1, 2, 3)]
    transport = safe_http.PinnedDNSAsyncHTTPTransport(
        hostname="peer.example",
        resolved_ip="203.0.113.7",
        uds="/unused.sock",
        socket_options=options,
    )
    async with httpx.AsyncClient(transport=transport, timeout=3) as client:
        assert (await client.get("http://peer.example/")).text == "OK"
    backend.connect_unix_socket.assert_awaited_once_with(
        path="/unused.sock",
        timeout=3,
        socket_options=options,
    )
    backend.connect_tcp.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_propagates_connection_error(network):
    backend, _ = network
    backend.connect_tcp.side_effect = httpcore.ConnectError("connection failed")
    transport = safe_http.PinnedDNSAsyncHTTPTransport(hostname="peer.example", resolved_ip="203.0.113.7")
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError, match="connection failed"):
            await client.get("http://peer.example/")


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_private", [None, False, True])
async def test_make_safe_client_validates_and_pins(network, monkeypatch, allow_private):
    backend, stream = network
    stream.response = (
        b"HTTP/1.1 302 Found\r\nLocation: http://other.example/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    )
    validated = net_validation.ValidatedWebhookURL(
        url="http://peer.example/path",
        hostname="peer.example",
        port=80,
        resolved_ip="203.0.113.7",
    )
    validate = AsyncMock(return_value=validated)
    monkeypatch.setattr(net_validation, "validate_webhook_url", validate)

    client, result = await safe_http.make_safe_client(validated.url, timeout=2.5, allow_private=allow_private)
    async with client:
        response = await client.get(validated.url)

    validate.assert_awaited_once_with(validated.url, allow_private=allow_private)
    assert result is validated
    assert response.status_code == 302
    assert response.headers["location"] == "http://other.example/"
    backend.connect_tcp.assert_awaited_once_with(
        host="203.0.113.7",
        port=80,
        timeout=2.5,
        local_address=None,
        socket_options=None,
    )


@pytest.mark.asyncio
async def test_make_safe_client_rejects_invalid_url_before_transport(monkeypatch):
    transport = AsyncMock()
    monkeypatch.setattr(safe_http, "PinnedDNSAsyncHTTPTransport", transport)
    with pytest.raises(HTTPException) as exc:
        await safe_http.make_safe_client("ftp://peer.example", timeout=1)
    assert exc.value.status_code == 422
    assert exc.value.detail == "url must start with http:// or https://"
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_make_safe_client_private_override(network):
    backend, _ = network
    url = "http://127.0.0.1/"
    with pytest.raises(HTTPException) as exc:
        await safe_http.make_safe_client(url, timeout=1, allow_private=False)
    assert exc.value.status_code == 422
    backend.connect_tcp.assert_not_awaited()

    client, validated = await safe_http.make_safe_client(url, timeout=1, allow_private=True)
    async with client:
        assert (await client.get(url)).text == "OK"
    assert validated.resolved_ip == "127.0.0.1"
    backend.connect_tcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_make_safe_client_resolves_once_and_pins(network, monkeypatch):
    backend, _ = network
    resolve = AsyncMock(return_value=["127.0.0.1"])
    monkeypatch.setattr(net_validation, "_resolve_addrs", resolve)
    url = "http://peer.example/"

    client, validated = await safe_http.make_safe_client(url, timeout=1, allow_private=True)
    async with client:
        assert (await client.get(url)).text == "OK"

    assert validated.hostname == "peer.example"
    assert validated.resolved_ip == "127.0.0.1"
    resolve.assert_awaited_once_with("peer.example")
    backend.connect_tcp.assert_awaited_once_with(
        host="127.0.0.1", port=80, timeout=1, local_address=None, socket_options=None
    )


@pytest.mark.asyncio
async def test_make_safe_client_rejects_blocked_dns_answer(network, monkeypatch):
    backend, _ = network
    resolve = AsyncMock(return_value=["127.0.0.1"])
    monkeypatch.setattr(net_validation, "_resolve_addrs", resolve)

    with pytest.raises(HTTPException) as exc:
        await safe_http.make_safe_client("http://peer.example/", timeout=1, allow_private=False)
    assert exc.value.status_code == 422
    assert exc.value.detail == "url host resolves to a non-routable address"
    resolve.assert_awaited_once_with("peer.example")
    backend.connect_tcp.assert_not_awaited()
