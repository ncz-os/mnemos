#!/usr/bin/env python3
"""OpenAI-compatible smoke tests for the PANTHEON shadow gateway (:4101).

Exercises health, chat, tool-call passthrough, and Codex /responses routing.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def _assert_echo_tool_call(response_data: dict) -> list[dict]:
    first_tool_calls = ((response_data.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
    if not first_tool_calls:
        raise AssertionError("expected at least one tool_call for echo(x=1)")

    first = first_tool_calls[0]
    if not first.get("id"):
        raise AssertionError("first tool_call is missing id")
    function = first.get("function") or {}
    if function.get("name") != "echo":
        raise AssertionError(f"expected echo tool_call, got {function.get('name')!r}")

    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"tool_call arguments are not valid JSON: {arguments!r}") from exc
    elif isinstance(arguments, dict):
        parsed_arguments = arguments
    else:
        raise AssertionError(f"tool_call arguments have unexpected shape: {arguments!r}")

    if parsed_arguments != {"x": 1}:
        raise AssertionError(f"expected echo arguments {{'x': 1}}, got {parsed_arguments!r}")
    return first_tool_calls


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4101")
    parser.add_argument("--api-key", default="shadow-smoke")
    parser.add_argument("--model", default="auto:cheap")
    parser.add_argument("--codex-model", default="gpt-5.3-codex")
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.api_key}"}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=120) as client:
        r = client.get("/health")
        r.raise_for_status()
        chat = client.post(
            "/v1/chat/completions",
            json={"model": args.model, "messages": [{"role": "user", "content": "reply with ok"}]},
        )
        chat.raise_for_status()
        tool = client.post(
            "/v1/chat/completions",
            json={
                "model": args.model,
                "messages": [{"role": "user", "content": "call the echo tool with x=1"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "description": "echo args",
                            "parameters": {
                                "type": "object",
                                "properties": {"x": {"type": "integer"}},
                                "required": ["x"],
                            },
                        },
                    }
                ],
                "tool_choice": "auto",
            },
        )
        tool.raise_for_status()
        first_tool_calls = _assert_echo_tool_call(tool.json())
        tool_result = client.post(
            "/v1/chat/completions",
            json={
                "model": args.model,
                "messages": [
                    {"role": "user", "content": "call the echo tool with x=1"},
                    {"role": "assistant", "content": None, "tool_calls": first_tool_calls},
                    {"role": "tool", "tool_call_id": first_tool_calls[0]["id"], "content": json.dumps({"x": 1})},
                ],
            },
        )
        tool_result.raise_for_status()
        codex = client.post(
            "/v1/responses",
            json={"model": args.codex_model, "input": "Return the word ok."},
        )
        codex.raise_for_status()
    print("pantheon shadow smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
