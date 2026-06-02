"""Post with-slash gateway routing regression guard.

The resolver fix taught the gateway to resolve namespaced model ids like
``together/Qwen/...`` without breaking upstream ids that legitimately
contain slashes. This test pins the follow-on dispatch contract: once the
provider is resolved, the actual provider call must receive the bare
upstream model id rather than the synthetic gateway namespace.
"""

from __future__ import annotations

import asyncio

from mnemos.api.dependencies import UserContext


def _user() -> UserContext:
    return UserContext(
        user_id="alice",
        group_ids=[],
        role="user",
        namespace="default",
        authenticated=True,
    )


def test_route_to_provider_passes_bare_upstream_model_after_with_slash_lookup(monkeypatch):
    """The top-level compatibility shim must preserve the bare upstream id."""
    from mnemos.api.routes import openai_compat

    bare_model = "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"
    gateway_model = f"together/{bare_model}"

    class _FakeGraeae:
        def __init__(self):
            self.providers = {
                "together": {
                    "api": "openai",
                    "model": bare_model,
                    "url": "https://api.together.xyz/v1/chat/completions",
                    "key_name": "together",
                }
            }
            self.route_calls = []

        async def route(self, *args, **kwargs):
            self.route_calls.append((args, kwargs))
            return {"status": "success", "response_text": "ok"}

    async def _resolver(_model: str) -> str:
        assert _model == gateway_model
        return "together"

    fake = _FakeGraeae()
    monkeypatch.setattr(openai_compat, "_resolve_provider_for_model", _resolver)
    monkeypatch.setattr(openai_compat, "_compat_get_engine", lambda: fake)

    messages = [{"role": "user", "content": "hello"}]
    expected_prompt = openai_compat._flatten_messages_for_prompt(messages)

    result = asyncio.run(
        openai_compat._route_to_provider(
            model=gateway_model,
            messages=messages,
            temperature=0.2,
            max_tokens=64,
            top_p=0.9,
            user=_user(),
        )
    )

    assert result == "ok"
    assert fake.route_calls
    args, kwargs = fake.route_calls[0]
    assert args[0] == "together"
    assert args[1] == bare_model
    assert args[2] == expected_prompt
    assert kwargs["task_type"] == "reasoning"
    assert kwargs["timeout"] == 30
    assert kwargs["generation_params"] == {
        "temperature": 0.2,
        "max_tokens": 64,
        "top_p": 0.9,
    }
    assert kwargs["request_params"] is None
    assert kwargs["messages"] == [{"role": "user", "content": "hello"}]
