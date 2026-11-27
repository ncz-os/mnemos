#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import hmac
import selectors
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


HOST = "127.0.0.1"
PORT = 5079
SERVICE_HOME = "/var/lib/graeae-shim"
MAX_BODY_BYTES = 262144
MAX_OUTPUT_BYTES = 1048576
MAX_REQUESTS = 4
READ_TIMEOUT_SECONDS = 10
TIMEOUT_SECONDS = 120
CODEX_MODEL = "gpt-5.5"
CLAUDE_MODEL = "claude-opus-4-8"


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part)
    return str(content)


def _messages_to_prompt(messages: Any, system: Any = None) -> str:
    parts: list[str] = []
    system_text = _flatten_content(system)
    if system_text:
        parts.append(f"system:\n{system_text}")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            text = _flatten_content(msg.get("content"))
            if text:
                parts.append(f"{role}:\n{text}")
    return "\n\n".join(parts).strip()


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    prefixes = ("assistant final:", "final:", "assistant:")
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _run_adapter(cmd, prompt):
    start = time.monotonic()
    env = {"HOME": SERVICE_HOME, "PATH": "/usr/local/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "CODEX_HOME": SERVICE_HOME + "/.codex"}
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=SERVICE_HOME, env=env,
                            start_new_session=True)
    output = bytearray()
    pending = memoryview(prompt.encode("utf-8"))
    selector = selectors.DefaultSelector()
    total = 0
    try:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE)
        selector.register(proc.stdout, selectors.EVENT_READ)
        selector.register(proc.stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = TIMEOUT_SECONDS - (time.monotonic() - start)
            if remaining <= 0:
                return 124, "", "provider_timeout", time.monotonic() - start
            for key, _ in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                if stream is proc.stdin:
                    if pending:
                        try:
                            pending = pending[os.write(stream.fileno(), pending[:8192]):]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                else:
                    chunk = os.read(stream.fileno(), 8192)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        return 125, "", "provider_output_limit", time.monotonic() - start
                    if stream is proc.stdout:
                        output.extend(chunk)
        rc = proc.wait(timeout=max(0.01, TIMEOUT_SECONDS - (time.monotonic() - start)))
        return rc, _clean_text(output.decode("utf-8", errors="replace")), "", time.monotonic() - start
    except subprocess.TimeoutExpired:
        return 124, "", "provider_timeout", time.monotonic() - start
    finally:
        selector.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


def _run_codex(prompt: str, model: str = CODEX_MODEL):
    return _run_adapter(["codex", "exec", "--model", model, "--skip-git-repo-check",
                         "--sandbox", "read-only", "--ephemeral",
                         "-c", "features.shell_tool=false", "-"], prompt)


def _run_claude(prompt: str, model: str = CLAUDE_MODEL):
    return _run_adapter(["claude", "-p", "--model", model, "--tools", "",
                         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'], prompt)


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.admission = threading.BoundedSemaphore(MAX_REQUESTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        request.settimeout(READ_TIMEOUT_SECONDS)
        if not self.admission.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.admission.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.admission.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "graeae-oauth-shim/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise ValueError("Invalid body length")
        length = int(lengths[0])
        if length <= 0 or length > MAX_BODY_BYTES or self.headers.get("Transfer-Encoding"):
            raise ValueError("Invalid body length")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Incomplete body")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return data

    def _validate_controls(self, data, model):
        if data.get("model", model) != model or data.get("stream", False) is not False:
            raise ValueError("Unsupported model or streaming mode")
        if any(key in data for key in ("tools", "tool_choice", "temperature", "top_p", "max_tokens", "n")):
            raise ValueError("Unsupported execution control")
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Messages are required")
        if any(not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant", "system")
               or not isinstance(msg.get("content"), (str, list)) for msg in messages):
            raise ValueError("Invalid messages")


    def do_GET(self) -> None:
        if self.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        credential = os.environ.get("GRAEAE_SHIM_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not credential or not hmac.compare_digest(supplied.encode(), ("Bearer " + credential).encode()):
            self._send_json(401, {"error": {"message": "authentication_required"}})
            return
        try:
            if self.path == "/openai/v1/chat/completions":
                return self._handle_openai()
            if self.path == "/anthropic/v1/messages":
                return self._handle_anthropic()
            self.send_error(404)
        except (ValueError, UnicodeError):
            self._send_json(400, {"error": {"message": "invalid_request"}})
        except TimeoutError:
            self._send_json(408, {"error": {"message": "request_timeout"}})
        except Exception:
            self._send_json(502, {"error": {"message": "provider_unavailable"}})

    def _handle_openai(self) -> None:
        payload = self._read_json()
        self._validate_controls(payload, CODEX_MODEL)
        prompt = _messages_to_prompt(payload.get("messages"))
        rc, text, stderr, elapsed = _run_codex(prompt, CODEX_MODEL)
        print(f"provider=openai latency_ms={int(elapsed * 1000)} exit_code={rc}", file=sys.stderr, flush=True)
        if rc != 0:
            self._send_json(502, {"error": {"message": "provider_unavailable"}})
            return
        self._send_json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model") or CODEX_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def _handle_anthropic(self) -> None:
        payload = self._read_json()
        self._validate_controls(payload, CLAUDE_MODEL)
        prompt = _messages_to_prompt(payload.get("messages"), system=payload.get("system"))
        rc, text, stderr, elapsed = _run_claude(prompt, CLAUDE_MODEL)
        print(f"provider=claude latency_ms={int(elapsed * 1000)} exit_code={rc}", file=sys.stderr, flush=True)
        if rc != 0:
            self._send_json(502, {"type": "error", "error": {"type": "api_error", "message": "provider_unavailable"}})
            return
        self._send_json(
            200,
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "model": payload.get("model") or CLAUDE_MODEL,
            },
        )


def main() -> None:
    if not os.environ.get("GRAEAE_SHIM_TOKEN"):
        raise RuntimeError("Service credential is required")
    httpd = BoundedHTTPServer((HOST, PORT), Handler)
    print(f"graeae-oauth-shim listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
