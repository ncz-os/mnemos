import asyncio
from types import SimpleNamespace
import pytest
from mnemos.core.secret_detection import redact_content, redact_field_with_stored
from mnemos.api.read_policy import _content_redacted_for_embedding, _redacted_for_webhook
from mnemos.core.oauth import _extract_external_id


@pytest.mark.parametrize('kind', ['RSA ', 'EC ', '', 'ENCRYPTED '])
def test_redact_complete_private_key(kind):
    header = f'-----BEGIN {kind}PRIVATE KEY-----'
    content = f'before\n{header}\nc2FtcGxl\n-----END {kind}PRIVATE KEY-----\nafter'
    spans = [(7, 7 + len(header))]
    metadata = {'secret_redact_fields': {'content': spans}}
    for result in (redact_content(content), redact_field_with_stored(content, metadata, 'content'),
                   _redacted_for_webhook(content, metadata),
                   _content_redacted_for_embedding(content, SimpleNamespace(redact_fields={'content': spans}))):
        assert 'c2FtcGxl' not in result
        assert 'before' in result and 'after' in result
        assert 'END' not in result


def test_redact_incomplete_private_key():
    assert 'c2FtcGxl' not in redact_content('-----BEGIN PRIVATE KEY-----\nc2FtcGxl')


@pytest.mark.parametrize('verified', [False, True, 'true', None])
def test_require_oauth_subject(verified):
    assert _extract_external_id('custom', {'email': 'sample@example.test', 'email_verified': verified}) is None
    assert _extract_external_id('custom', {'sub': 'subject-a'}) == 'subject-a'
    assert _extract_external_id('custom', {'sub': 'subject-b'}) == 'subject-b'


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['POST', 'DELETE'])
async def test_bound_body_before_handler(method):
    from mnemos.api.main import _BodySizeLimitASGI
    sent = []
    calls = 0
    async def receive():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError('Body limit must terminate reception')
        return {'type': 'http.request', 'body': b'12345', 'more_body': True}
    async def send(message):
        sent.append(message)
    async def app(scope, receive, send):
        try:
            await receive()
        except Exception:
            pass
        await send({'type': 'http.response.start', 'status': 400})
    await _BodySizeLimitASGI(app, 4)({'type': 'http', 'method': method}, receive, send)
    assert [x['status'] for x in sent if x['type'] == 'http.response.start'] == [413]
    assert calls == 1


@pytest.mark.asyncio
async def test_bound_delivery_admission(monkeypatch):
    from mnemos.core import lifecycle as lc
    monkeypatch.setattr(lc, '_delivery_attempt_tasks', set())
    monkeypatch.setattr(lc, '_MAX_PENDING_DELIVERIES', 4, raising=False)
    gate = asyncio.Event()
    for _ in range(100000):
        lc._schedule_delivery_attempt(gate.wait())
    count = len(lc._delivery_attempt_tasks)
    gate.set()
    await asyncio.gather(*lc._delivery_attempt_tasks)
    assert count <= 4


@pytest.mark.asyncio
async def test_deliver_after_admission_available(monkeypatch):
    from mnemos.core import lifecycle as lc
    monkeypatch.setattr(lc, '_delivery_attempt_tasks', set())
    monkeypatch.setattr(lc, '_MAX_PENDING_DELIVERIES', 1)
    gate = asyncio.Event()
    seen = []
    async def delivery(value):
        await gate.wait()
        seen.append(value)
    first = lc._schedule_delivery_attempt(delivery(1))
    assert lc._schedule_delivery_attempt(delivery(2)) is None
    gate.set()
    await first
    await asyncio.sleep(0)
    second = lc._schedule_delivery_attempt(delivery(2))
    await second
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_accept_bounded_body():
    from mnemos.api.main import _BodySizeLimitASGI
    sent = []
    async def receive():
        return {'type': 'http.request', 'body': b'1234', 'more_body': False}
    async def send(message):
        sent.append(message)
    async def app(scope, receive, send):
        assert (await receive())['body'] == b'1234'
        await send({'type': 'http.response.start', 'status': 200})
    await _BodySizeLimitASGI(app, 4)({'type': 'http', 'method': 'PUT'}, receive, send)
    assert sent[0]['status'] == 200
