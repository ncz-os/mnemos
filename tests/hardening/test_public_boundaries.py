import pytest
from mnemos.api.errors import database_disconnected_detail
from mnemos.tunnels.ngrok_bridge import NgrokBridge, _pick_public_url


def test_redact_public_disconnect(caplog):
    text = 'sample-private-value'
    result = database_disconnected_detail(ConnectionRefusedError(text))
    assert text not in str(result)
    assert result['error'] == 'database_disconnected'
    assert result.get('request_id')
    assert result['request_id'] in caplog.text
    assert text not in caplog.text


def test_forward_ngrok_environment(monkeypatch):
    monkeypatch.setattr('mnemos.tunnels.ngrok_bridge.find_free_port', lambda: 12345)
    monkeypatch.setenv('NGROK_AUTHTOKEN', 'sample-environment-token')
    bridge = NgrokBridge()
    argv, env = bridge._spawn_spec(target_port=5002, authtoken=None)
    assert env['NGROK_AUTHTOKEN'] == 'sample-environment-token'
    assert 'sample-environment-token' not in str(argv)
    _, env = bridge._spawn_spec(target_port=5002, authtoken='sample-request-token')
    assert env['NGROK_AUTHTOKEN'] == 'sample-request-token'


def test_require_https_tunnel():
    assert _pick_public_url({'tunnels': [{'public_url': 'http://sample.example.test'}]}) is None
    assert _pick_public_url({'tunnels': [{'public_url': 'http://sample.example.test'}, {'public_url': 'https://sample.example.test'}]}) == 'https://sample.example.test'


@pytest.mark.asyncio
async def test_allow_put_preflight():
    from mnemos.api.main import app
    from starlette.middleware.cors import CORSMiddleware
    from httpx import ASGITransport, AsyncClient
    options = next(m.kwargs for m in app.user_middleware if m.cls is CORSMiddleware)
    async def endpoint(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
    policy = CORSMiddleware(endpoint, **{**options, 'allow_origins': ['https://client.example.test']})
    async with AsyncClient(transport=ASGITransport(app=policy), base_url='http://test') as client:
        allowed = await client.options('/state/key', headers={'Origin': 'https://client.example.test', 'Access-Control-Request-Method': 'PUT'})
        denied = await client.options('/state/key', headers={'Origin': 'https://other.example.test', 'Access-Control-Request-Method': 'PUT'})
    assert allowed.status_code == 200
    assert denied.status_code == 400
