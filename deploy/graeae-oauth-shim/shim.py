#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = "127.0.0.1"
PORT = 5079
HOME = "/home/jasonperlow"
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


def _run_codex(prompt: str) -> tuple[int, str, str, float]:
    start = time.monotonic()
    env = os.environ.copy()
    env.update({"HOME": HOME, "CODEX_HOME": str(Path(HOME) / ".codex")})
    with tempfile.NamedTemporaryFile(prefix="graeae-codex-", delete=False) as out:
        out_path = out.name
    try:
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--output-last-message",
            out_path,
            "-",
        ]
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=HOME,
            env=env,
            timeout=TIMEOUT_SECONDS,
        )
        try:
            final = Path(out_path).read_text(encoding="utf-8")
        except OSError:
            final = ""
        text = final.strip() or proc.stdout.strip()
        return proc.returncode, _clean_text(text), proc.stderr, time.monotonic() - start
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"timeout after {TIMEOUT_SECONDS}s: {exc}", time.monotonic() - start
    finally:
        try:
            Path(out_path).unlink()
        except OSError:
            pass


def _run_claude(prompt: str) -> tuple[int, str, str, float]:
    start = time.monotonic()
    env = os.environ.copy()
    env.update({"HOME": HOME})
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            text=True,
            capture_output=True,
            cwd=HOME,
            env=env,
            timeout=TIMEOUT_SECONDS,
        )
        return proc.returncode, _clean_text(proc.stdout), proc.stderr, time.monotonic() - start
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"timeout after {TIMEOUT_SECONDS}s: {exc}", time.monotonic() - start


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
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

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
        try:
            if self.path == "/openai/v1/chat/completions":
                return self._handle_openai()
            if self.path == "/anthropic/v1/messages":
                return self._handle_anthropic()
            self.send_error(404)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": {"message": f"invalid JSON: {exc}"}})
        except Exception as exc:
            print(f"provider=unknown latency_ms=0 exit_code=500 error={exc}", file=sys.stderr, flush=True)
            self._send_json(500, {"error": {"message": str(exc)}})

    def _handle_openai(self) -> None:
        payload = self._read_json()
        prompt = _messages_to_prompt(payload.get("messages"))
        rc, text, stderr, elapsed = _run_codex(prompt)
        print(f"provider=openai latency_ms={int(elapsed * 1000)} exit_code={rc}", file=sys.stderr, flush=True)
        if rc != 0:
            self._send_json(502, {"error": {"message": (stderr or text or "codex failed")[-2000:]}})
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
        prompt = _messages_to_prompt(payload.get("messages"), system=payload.get("system"))
        rc, text, stderr, elapsed = _run_claude(prompt)
        print(f"provider=claude latency_ms={int(elapsed * 1000)} exit_code={rc}", file=sys.stderr, flush=True)
        if rc != 0:
            self._send_json(502, {"type": "error", "error": {"type": "api_error", "message": (stderr or text or "claude failed")[-2000:]}})
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
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"graeae-oauth-shim listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
