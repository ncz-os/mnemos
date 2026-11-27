import importlib.util
from pathlib import Path
from io import BytesIO
from email.message import Message
import pytest

spec = importlib.util.spec_from_file_location('shim', Path(__file__).resolve().parents[2] / 'deploy/graeae-oauth-shim/shim.py')
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)


def handler(body=b'{}', token=None):
    h = object.__new__(shim.Handler)
    h.path = '/openai/v1/chat/completions'
    h.headers = Message()
    h.headers['Content-Length'] = str(len(body))
    if token:
        h.headers['Authorization'] = 'Bearer ' + token
    h.rfile = BytesIO(body)
    h.responses = []
    h._send_json = lambda status, data: h.responses.append((status, data))
    return h


@pytest.mark.parametrize('path', ['/openai/v1/chat/completions', '/anthropic/v1/messages'])
@pytest.mark.parametrize('token', [None, 'other'])
def test_validate_shim_credentials(monkeypatch, path, token):
    monkeypatch.setenv('GRAEAE_SHIM_TOKEN', 'sample-service-credential')
    def unused(*args):
        pytest.fail('Provider must not run')
    monkeypatch.setattr(shim, '_run_codex', unused)
    monkeypatch.setattr(shim, '_run_claude', unused)
    h = handler(token=token)
    h.path = path
    h.do_POST()
    assert h.responses[0][0] == 401


@pytest.mark.parametrize('length', ['-1', 'invalid', '999999999'])
def test_bound_shim_length(length):
    h = handler()
    h.headers.replace_header('Content-Length', length)
    with pytest.raises(ValueError):
        h._read_json()


@pytest.mark.parametrize('body', [b'{"model":"unavailable","messages":[{"role":"user","content":"sample"}]}', b'{"stream":true,"messages":[{"role":"user","content":"sample"}]}'])
def test_validate_shim_execution_controls(monkeypatch, body):
    monkeypatch.setenv('GRAEAE_SHIM_TOKEN', 'sample-service-credential')
    monkeypatch.setattr(shim, '_run_codex', lambda *args: (0, 'sample', '', 0))
    h = handler(body, 'sample-service-credential')
    h.do_POST()
    assert h.responses[0][0] == 400


@pytest.mark.parametrize('provider,model,path', [
    ('_run_codex', shim.CODEX_MODEL, '/openai/v1/chat/completions'),
    ('_run_claude', shim.CLAUDE_MODEL, '/anthropic/v1/messages'),
])
def test_select_shim_model(monkeypatch, provider, model, path):
    import json
    monkeypatch.setenv('GRAEAE_SHIM_TOKEN', 'sample-service-credential')
    calls = []
    def run(prompt, selected):
        calls.append(selected)
        return 0, 'sample response', '', 0
    monkeypatch.setattr(shim, provider, run)
    h = handler(json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'sample question'}]}).encode(),
                'sample-service-credential')
    h.path = path
    h.do_POST()
    assert calls == [model]
    assert h.responses[0][1]['model'] == model


def test_bound_adapter_output(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(shim, 'SERVICE_HOME', str(tmp_path))
    monkeypatch.setattr(shim, 'MAX_OUTPUT_BYTES', 32)
    result = shim._run_adapter([sys.executable, '-c', 'print("sample" * 100)'], 'sample')
    assert result[0] == 125
    assert result[1] == ''


def test_bound_adapter_lifetime(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(shim, 'SERVICE_HOME', str(tmp_path))
    monkeypatch.setattr(shim, 'TIMEOUT_SECONDS', 0.05)
    result = shim._run_adapter([sys.executable, '-c', 'import time; time.sleep(10)'], 'sample')
    assert result[0] == 124
    assert result[3] < 1
