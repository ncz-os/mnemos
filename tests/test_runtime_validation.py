"""Validate staging, transport, session limits, and backend ownership."""
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import urllib.request
import urllib.error

import pytest


@pytest.mark.parametrize('kind', ['', 'RSA ', 'EC '])
@pytest.mark.parametrize('terminated', [True, False])
def test_redact_complete_private_blocks(kind, terminated):
    from mnemos.core.secret_detection import redact_content
    body = 'YWJjZGVmZ2hpamtsbW5vcA==\n'
    block = f'-----BEGIN {kind}PRIVATE KEY-----\n' + body
    if terminated:
        block += f'-----END {kind}PRIVATE KEY-----'
    text = 'Opening prose.\n' + block
    result = redact_content(text)
    assert body.strip() not in result
    assert result.startswith('Opening prose.')
    if terminated:
        assert redact_content(text + '\nClosing prose.').endswith('Closing prose.')


@pytest.mark.asyncio
async def test_staging_validates_permissions_and_overlap(tmp_path, monkeypatch):
    from mnemos.tools.styx import runner
    from mnemos.tools.styx.config import StyxConfig
    config = StyxConfig(gdrive_folder_id='test', sqlite_path=tmp_path / 'store', age_recipient='age1test', host_label='test', work_dir=tmp_path / 'work')
    entered = asyncio.Event()
    release = asyncio.Event()
    async def produce(config, dest, **kw):
        assert dest.parent.stat().st_mode & 0o777 == 0o700
        assert config.work_dir.stat().st_mode & 0o777 == 0o700
        entered.set()
        await release.wait()
        raise RuntimeError('export stopped')
    monkeypatch.setattr(runner, 'produce_bundle', produce)
    first = asyncio.create_task(runner.run_backup(config))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        from dataclasses import replace
        other_config = replace(config, work_dir=tmp_path / 'other-work')
        with pytest.raises(Exception, match='already running'):
            await runner.run_backup(other_config)
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['public', 'symlink'])
async def test_staging_rejects_unapproved_parent(tmp_path, monkeypatch, mode):
    from mnemos.tools.styx import runner
    from mnemos.tools.styx.config import StyxConfig
    parent = tmp_path / 'work'
    if mode == 'public':
        parent.mkdir(mode=0o755)
    else:
        parent.symlink_to(tmp_path, target_is_directory=True)
    produce = AsyncMock(side_effect=RuntimeError('unexpected export'))
    monkeypatch.setattr(runner, 'produce_bundle', produce)
    config = StyxConfig(gdrive_folder_id='test', sqlite_path=tmp_path / 'store', age_recipient='age1test', host_label='test', work_dir=parent)
    with pytest.raises(Exception):
        await runner.run_backup(config)
    produce.assert_not_called()


def test_archive_permissions(tmp_path):
    from mnemos.tools.styx.source import archive_bundle
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    (bundle / 'record').write_text('example')
    archive = archive_bundle(bundle, tmp_path / 'archive.tgz')
    assert archive.stat().st_mode & 0o777 == 0o600


def test_export_transport_rejects_redirects_and_ignores_proxies(monkeypatch, tmp_path):
    from mnemos.tools.styx import source
    from mnemos.tools.styx.config import StyxConfig
    requests = []
    class Transport(urllib.request.HTTPHandler):
        def http_open(self, request):
            requests.append(request)
            if len(requests) > 1:
                raise RuntimeError('unexpected second request')
            from email.message import Message
            headers = Message()
            headers['Location'] = 'http://example.invalid/export'
            response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
            response.msg = 'Found'
            return response
    original = urllib.request.build_opener
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:8080')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8080')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: original(*handlers, Transport()))
    monkeypatch.setattr(urllib.request, '_opener', None)
    config = StyxConfig(gdrive_folder_id='test', sqlite_path=tmp_path / 'store', age_recipient='age1test', host_label='test', source_mode='http', http_endpoint='http://127.0.0.1/export', http_token='example')
    with pytest.raises(source.StyxError):
        source._produce_from_http(config, tmp_path / 'bundle')
    assert len(requests) == 1
    assert requests[0].host == '127.0.0.1'


@pytest.mark.asyncio
async def test_cli_uses_configured_factory(monkeypatch):
    from mnemos.core import lifecycle
    from mnemos.cli import main
    backend = object()
    factory = AsyncMock(return_value=('mariadb', backend))
    monkeypatch.setattr(lifecycle, '_persistence_backend', None)
    monkeypatch.setattr(lifecycle, 'build_configured_persistence_backend', factory)
    assert await main._open_cli_persistence_backend() == (backend, True)
    factory.assert_awaited_once()
    monkeypatch.setattr(lifecycle, '_persistence_backend', backend)
    assert await main._open_cli_persistence_backend() == (backend, False)
    factory.assert_awaited_once()


@pytest.mark.parametrize('result', ['copy', 'move', 'failure', 'exception'])
def test_install_cleans_staging(tmp_path, monkeypatch, result):
    from mnemos.installer import service
    from mnemos.installer.wizard import Config
    import shutil
    sources = []
    destination = tmp_path / 'installed.env'
    def run(command):
        if command[:2] == ['sudo', 'install']:
            sources.append(Path(command[-2]))
            if result == 'exception':
                raise RuntimeError('copy stopped')
            if result == 'copy':
                shutil.copyfile(command[-2], command[-1])
                return 0, '', ''
            return 1, '', ''
        if command[:2] == ['sudo', 'mv']:
            if result == 'move':
                shutil.move(command[-2], command[-1])
                return 0, '', ''
            return 1, '', ''
        return 0, '', ''
    monkeypatch.setattr(service, '_run', run)
    assert service._write_env_file(Config(), str(destination)) == (result in {'copy', 'move'})
    assert sources and all(not path.exists() for path in sources)
    if result in {'copy', 'move'}:
        assert 'PG_HOST=' in destination.read_text()


@pytest.mark.asyncio
async def test_mcp_registers_backend_without_budget(monkeypatch):
    from tests.test_runtime_validation import _fresh_http
    from mnemos.core import lifecycle
    from starlette.applications import Starlette
    http = _fresh_http(monkeypatch)
    backend = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(lifecycle, '_persistence_backend', None)
    factory = AsyncMock(return_value=('test', backend))
    monkeypatch.setattr(lifecycle, 'build_configured_persistence_backend', factory)
    monkeypatch.setattr(http, 'get_settings', lambda: SimpleNamespace(oauth=SimpleNamespace(database_url='', issuer=''), knemon=None))
    async with http._mcp_http_lifespan(Starlette()):
        assert lifecycle._persistence_backend is backend
    assert lifecycle._persistence_backend is None
    backend.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_bounds_initial_body(monkeypatch):
    from tests.test_runtime_validation import _fresh_http
    http = _fresh_http(monkeypatch)
    manager = http._StreamableHTTPMCP()
    manager.max_body_bytes = 8
    receive = AsyncMock(side_effect=[{'type': 'http.request', 'body': b' ' * 9, 'more_body': True}, RuntimeError('body limit required')])
    sent = []
    async def send(message):
        sent.append(message)
    await asyncio.wait_for(manager({'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': []}, receive, send), 1)
    assert sent[0]['status'] == 413
    assert receive.await_count == 1
    assert not manager._sessions


@pytest.mark.asyncio
async def test_mcp_bounds_sessions_and_closes_tasks(monkeypatch):
    from tests.test_runtime_validation import _fresh_http
    http = _fresh_http(monkeypatch)
    manager = http._StreamableHTTPMCP()
    manager.max_sessions = 1
    manager.max_principal_sessions = 1
    context = SimpleNamespace(user_id='a', role='user', namespace='default')
    monkeypatch.setattr(http, '_resolve_mcp_user_context', AsyncMock(return_value=context))
    request = SimpleNamespace(state=SimpleNamespace(mnemos_mcp_principal=SimpleNamespace(api_key='example'), mnemos_mcp_principal_id='a'))
    async def run(sid, session):
        session.ready.set_result(None)
        await asyncio.Event().wait()
    monkeypatch.setattr(manager, '_run_session', run)
    first = await manager._new_session(request)
    try:
        second = await manager._new_session(request)
        assert second.status_code == 429
        assert len(manager._sessions) == 1
    finally:
        await manager.shutdown()
    assert first.task.done()
    assert not manager._sessions


def _fresh_http(monkeypatch):
    import sys
    from types import ModuleType
    from tests.test_mcp_http_health import _fresh_http as fresh
    stdio = ModuleType("mnemos.mcp.stdio")
    stdio.app = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "mnemos.mcp.stdio", stdio)
    return fresh(monkeypatch)


def test_redact_stored_header_spans_and_multiple_blocks():
    from mnemos.api.read_policy import _redacted_for_webhook
    from mnemos.mcp.tools.dag import _redact_history_content
    from mnemos.api.dependencies import UserContext
    from mnemos.core.secret_detection import redact_field_with_stored
    from mnemos.api.routes.memories import _content_redacted_for_embedding
    from mnemos.core.persisted_text_classification import classify_persisted_text_fields
    header = '-----BEGIN PRIVATE KEY-----'
    text = '\n'.join([header, 'YWJjZGVmZ2g=', '-----END PRIVATE KEY-----', 'Middle prose.', header, 'aWprbG1ub3A=', '-----END PRIVATE KEY-----'])
    old_metadata = {'secret_redact_fields': {'content': [(0, len(header))]}}
    classified = classify_persisted_text_fields(content=text, namespace='default', classified_at='ingest')
    user = UserContext(user_id='example', group_ids=[], role='user', namespace='default', authenticated=True)
    for result in (redact_field_with_stored(text, old_metadata, 'content'),
                   _content_redacted_for_embedding(text, classified),
                   _redacted_for_webhook(text, old_metadata),
                   _redact_history_content(text, user)):
        assert 'YWJjZGVmZ2g=' not in result
        assert 'aWprbG1ub3A=' not in result
        assert 'Middle prose.' in result


@pytest.mark.asyncio
async def test_mcp_idle_expiry_and_startup_timeout(monkeypatch):
    http = _fresh_http(monkeypatch)
    manager = http._StreamableHTTPMCP()
    manager.max_sessions = 1
    manager.idle_seconds = .01
    manager.startup_seconds = .01
    context = SimpleNamespace(user_id='a', role='user', namespace='default')
    monkeypatch.setattr(http, '_resolve_mcp_user_context', AsyncMock(return_value=context))
    request = SimpleNamespace(state=SimpleNamespace(mnemos_mcp_principal=SimpleNamespace(api_key='example'), mnemos_mcp_principal_id='a'))
    async def run(sid, session):
        session.ready.set_result(None)
        await asyncio.Event().wait()
    monkeypatch.setattr(manager, '_run_session', run)
    first = await manager._new_session(request)
    first.last_activity = 0
    await manager._expire_idle()
    assert first.task.done()
    assert not manager._sessions
    async def pending(sid, session):
        await asyncio.Event().wait()
    monkeypatch.setattr(manager, '_run_session', pending)
    try:
        with pytest.raises(TimeoutError):
            await manager._new_session(request)
        assert not manager._sessions
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_shutdown_precedes_backend_close(monkeypatch):
    from mnemos.core import lifecycle
    from starlette.applications import Starlette
    http = _fresh_http(monkeypatch)
    order = []
    backend = SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append('close')))
    monkeypatch.setattr(lifecycle, '_persistence_backend', None)
    monkeypatch.setattr(lifecycle, 'build_configured_persistence_backend', AsyncMock(return_value=('test', backend)))
    monkeypatch.setattr(http, 'get_settings', lambda: SimpleNamespace(oauth=SimpleNamespace(database_url='', issuer='')))
    monkeypatch.setattr(http.streamable_mcp, 'shutdown', AsyncMock(side_effect=lambda: order.append('sessions')))
    monkeypatch.setattr(http, '_drain_audit_tasks_on_shutdown', AsyncMock(side_effect=lambda: order.append('audit')))
    async with http._mcp_http_lifespan(Starlette()):
        pass
    assert order == ['sessions', 'audit', 'close']


@pytest.mark.asyncio
async def test_mcp_reuses_attached_backend(monkeypatch):
    from mnemos.core import lifecycle
    from starlette.applications import Starlette
    http = _fresh_http(monkeypatch)
    application = Starlette()
    backend = SimpleNamespace(close=AsyncMock())
    application.state.persistence_backend = backend
    factory = AsyncMock(side_effect=AssertionError('backend already attached'))
    monkeypatch.setattr(lifecycle, '_persistence_backend', None)
    monkeypatch.setattr(lifecycle, 'build_configured_persistence_backend', factory)
    monkeypatch.setattr(http, 'get_settings', lambda: SimpleNamespace(oauth=SimpleNamespace(database_url='', issuer='')))
    async with http._mcp_http_lifespan(application):
        assert lifecycle._persistence_backend is backend
    factory.assert_not_awaited()
    backend.close.assert_not_awaited()
    assert application.state.persistence_backend is backend


@pytest.mark.parametrize('command,method', [
    (['artemis', 'hash-backfill', '--json'], 'backfill_missing_content_hashes'),
    (['artemis', 'dedup-sweep', '--dry-run', '--json'], 'find_duplicate_content_groups'),
    (['db', 'compact-federation-journal'], 'compact_journal'),
])
@pytest.mark.parametrize('borrowed', [False, True])
def test_cli_maintenance_backend_ownership(monkeypatch, command, method, borrowed):
    from contextlib import asynccontextmanager
    from mnemos.cli import main
    from mnemos.core import lifecycle
    from typer.testing import CliRunner
    @asynccontextmanager
    async def transactional():
        yield object()
    operation = AsyncMock(return_value=[] if method == 'find_duplicate_content_groups' else 0)
    repo = SimpleNamespace(**{method: operation})
    backend = SimpleNamespace(memories=repo, federation=repo, transactional=transactional, close=AsyncMock())
    builder = AsyncMock(return_value=backend)
    settings = SimpleNamespace(database=SimpleNamespace(dsn='mariadb://example:example@localhost/example', url=''))
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    monkeypatch.setattr(lifecycle, '_persistence_backend', backend if borrowed else None)
    monkeypatch.setattr(lifecycle, '_build_mariadb_backend', builder)
    result = CliRunner().invoke(main.app, command)
    assert result.exit_code == 0, result.output
    operation.assert_awaited_once()
    if borrowed:
        backend.close.assert_not_awaited()
        builder.assert_not_awaited()
    else:
        backend.close.assert_awaited_once()
        builder.assert_awaited_once()


@pytest.mark.asyncio
async def test_staging_assigns_distinct_run_artifacts(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from mnemos.tools.styx import runner
    from mnemos.tools.styx.config import StyxConfig
    paths = []
    async def produce(config, dest, **kwargs):
        paths.append(dest.parent)
        dest.mkdir()
        (dest / 'record').write_text('ordinary record')
        return SimpleNamespace(record_count=1, kind='example')
    monkeypatch.setattr(runner, 'produce_bundle', produce)
    monkeypatch.setattr(runner, 'encrypt_file', lambda src, dst, recipient: dst.write_bytes(b'age-encryption.org/v1\nexample'))
    config = StyxConfig(gdrive_folder_id='test', sqlite_path=tmp_path / 'store', age_recipient='age1test', host_label='test', work_dir=tmp_path / 'work', min_bytes=1)
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = await runner.run_backup(config, now=moment, keep_workdir=True)
    second = await runner.run_backup(config, now=moment)
    assert first.artifact_name != second.artifact_name
    assert paths[0] != paths[1]
    assert (paths[0] / first.artifact_name).exists()
    assert not paths[1].exists()


def test_direct_export_ignores_proxy_and_preserves_private_modes(tmp_path, monkeypatch):
    from email.message import Message
    from mnemos.tools.styx import source
    from mnemos.tools.styx.config import StyxConfig
    requests = []
    class Transport(urllib.request.HTTPHandler):
        def http_open(self, request):
            requests.append(request)
            headers = Message()
            headers['Content-Type'] = 'application/x-ndjson'
            data = b'{"records":[{"id":"example","content":"ordinary record"}]}\n{"export_complete":true,"record_count":1}\n'
            response = urllib.response.addinfourl(io.BytesIO(data), headers, request.full_url, 200)
            response.msg = 'OK'
            return response
    build = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: build(*handlers, Transport()))
    monkeypatch.setattr(urllib.request, '_opener', None)
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:8080')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8080')
    monkeypatch.setenv('NO_PROXY', '')
    config = StyxConfig(gdrive_folder_id='test', age_recipient='age1test', source_mode='http', http_endpoint='http://127.0.0.1/export', http_token='example')
    result = source._produce_from_http(config, tmp_path / 'bundle')
    assert result.record_count == 1
    assert len(requests) == 1
    assert requests[0].host == '127.0.0.1'
    assert requests[0].get_header('Authorization') == 'Bearer example'
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in result.path.rglob('*') if path.is_file())


def test_bundle_export_files_are_private(tmp_path):
    from mnemos.portability.charon import export_bundle, _write_jsonl
    root = tmp_path / 'bundle'
    export_bundle([{'id': 'example', 'content': 'ordinary record', 'category': 'facts'}], root)
    _write_jsonl([{'id': 'example', 'content': 'ordinary record'}], root / 'sidecars' / 'memories.jsonl')
    files = [path for path in root.rglob('*') if path.is_file()]
    assert len(files) >= 3
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)


@pytest.mark.asyncio
async def test_mcp_open_get_stream_does_not_pin_session_against_idle_reap(monkeypatch):
    http = _fresh_http(monkeypatch)
    manager = http._StreamableHTTPMCP()
    manager.idle_seconds = .01
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle_request(scope, receive, send):
        entered.set()
        await release.wait()

    async def idle_task():
        await asyncio.Event().wait()

    task = asyncio.create_task(idle_task())
    session = http._StreamableSession(
        SimpleNamespace(handle_request=handle_request), 'a', SimpleNamespace(api_key='example'),
        SimpleNamespace(user_id='a', role='user', namespace='default'),
        asyncio.get_running_loop().create_future(), task=task, last_activity=0.0,
    )
    manager._sessions['s1'] = session

    def scope(method):
        return {'type': 'http', 'method': method, 'headers': [(b'mcp-session-id', b's1')],
                'state': {'mnemos_mcp_principal_id': 'a'}}

    async def noop(*_a, **_k):
        return {'type': 'http.disconnect'}

    stream = asyncio.create_task(manager(scope('GET'), noop, noop))
    await asyncio.wait_for(entered.wait(), 1)
    assert session.active_requests == 0
    session.last_activity = 0
    await manager._expire_idle()
    assert 's1' not in manager._sessions
    assert task.cancelled() or task.done()
    release.set()
    await stream

    entered.clear()
    release.clear()
    task2 = asyncio.create_task(idle_task())
    session2 = http._StreamableSession(
        SimpleNamespace(handle_request=handle_request), 'a', SimpleNamespace(api_key='example'),
        SimpleNamespace(user_id='a', role='user', namespace='default'),
        asyncio.get_running_loop().create_future(), task=task2, last_activity=0.0,
    )
    manager._sessions['s1'] = session2
    post = asyncio.create_task(manager(scope('POST'), noop, noop))
    await asyncio.wait_for(entered.wait(), 1)
    assert session2.active_requests == 1
    session2.last_activity = 0
    await manager._expire_idle()
    assert 's1' in manager._sessions
    release.set()
    await post
    task2.cancel()
    await asyncio.gather(task2, return_exceptions=True)
