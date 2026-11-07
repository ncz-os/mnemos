"""Characterisation tests for ``mnemos/domain/openai_compat/streaming.py``.

The module is the SSE half of the OpenAI-compatible gateway: it turns a
provider delta stream into ``data: {...}\\n\\n`` frames, translates a
pre-response exception into an ``OpenAICompatError``, and closes the
provider stream when the client stops reading.

These tests PIN today's behaviour (2026-11-05, branch head). They are not a
design proposal: when a value here changes on purpose, update the pin with
the change. Everything is driven by plain fixtures and fakes -- no network,
no sleeping, no database, no clock reads (``created``/``stream_id`` are
passed in as arguments).

One suspected defect is pinned as ``xfail(strict=True)``: the stream error
frame uses the bare ``str(exc)`` as its message, which for a genuine
``OpenAICompatError`` body is a Python dict repr rather than the provider's
human message. See ``test_stream_error_frame_uses_readable_provider_message``.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from mnemos.domain.openai_compat.schemas import ChatCompletionDelta
from mnemos.domain.openai_compat.streaming import (
    _route_to_provider_stream,
    _stream_chunk_event,
    _stream_error_event,
    _stream_event,
    _stream_events_for_provider_delta,
    _stream_preflight_exception,
    stream_event_source,
)
from mnemos.domain.openai_compat.providers import OpenAICompatError

# Logger name (NOT ``__name__``: this is the module under test's logger,
# which ``caplog.at_level`` matches by name).
STREAMING_LOGGER = "mnemos.domain.openai_compat.streaming"

_SSE_TERMINATOR = "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProviderStream:
    """Async iterator over fixed deltas that records its own close."""

    def __init__(self, deltas=(), *, error=None, close_error=None):
        self._deltas = list(deltas)
        self._error = error
        self._close_error = close_error
        self.closed = False
        self.aclose_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._deltas:
            return self._deltas.pop(0)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        raise StopAsyncIteration

    async def aclose(self):
        self.aclose_calls += 1
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _FakeGraeae:
    """Stand-in for the GRAEAE engine: one provider table, a route_stream."""

    def __init__(self, *, providers=None, chunks=(), error=None):
        self.providers = {} if providers is None else providers
        self._chunks = list(chunks)
        self._error = error
        self.calls = []

    async def route_stream(self, provider, model, prompt, **kwargs):
        self.calls.append(
            {
                "provider": provider,
                "model": model,
                "prompt": prompt,
                **kwargs,
            }
        )
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


def _engine_factory(engine):
    return lambda: engine


def _resolver(provider):
    async def _resolve(model):
        return provider

    return _resolve


async def _collect(agen):
    """Drain an async iterator into a list."""
    return [item async for item in agen]


def _frames(events):
    """Return the JSON payloads of ``data:`` frames.

    The ``data: [DONE]`` sentinel has no JSON body, so it is returned as
    ``None`` (not as an empty dict). A ``None`` entry is therefore the DONE
    frame -- never a data frame, and never a decode failure.
    """
    payloads = []
    for event in events:
        assert event.startswith("data: "), event
        assert event.endswith("\n\n"), event
        body = event[len("data: ") : -2]
        payloads.append(None if body == "[DONE]" else json.loads(body))
    return payloads


# ---------------------------------------------------------------------------
# _stream_event / _stream_error_event / _stream_chunk_event
# ---------------------------------------------------------------------------


def test_stream_event_serialises_compactly_with_an_sse_terminator():
    assert _stream_event({"a": 1, "b": [1, 2]}) == 'data: {"a":1,"b":[1,2]}\n\n'


def test_stream_event_inserts_no_spaces_after_separators():
    assert _stream_event({"x": {"y": 1}}) == 'data: {"x":{"y":1}}\n\n'


def test_stream_event_keeps_non_ascii_ascii_escaped():
    # json.dumps defaults to ensure_ascii=True: the SSE body is pure ASCII.
    assert _stream_event({"t": "\u00e9"}) == 'data: {"t":"\\u00e9"}\n\n'


def test_stream_event_renders_an_empty_object_as_an_empty_frame():
    assert _stream_event({}) == "data: {}\n\n"


def test_stream_event_rejects_a_non_json_serialisable_payload():
    with pytest.raises(TypeError):
        _stream_event({"obj": object()})


def test_stream_error_event_wraps_the_message_and_type():
    assert (
        _stream_error_event("boom")
        == 'data: {"error":{"message":"boom","type":"provider_stream_error"}}\n\n'
    )


def test_stream_error_event_defaults_to_provider_stream_error_type():
    payload = _frames([_stream_error_event("x")])[0]
    assert payload["error"]["type"] == "provider_stream_error"


def test_stream_error_event_honours_an_explicit_type():
    payload = _frames([_stream_error_event("x", error_type="custom")])[0]
    assert payload["error"] == {"message": "x", "type": "custom"}


def test_stream_error_event_keeps_an_empty_message():
    payload = _frames([_stream_error_event("")])[0]
    assert payload["error"]["message"] == ""


def test_stream_error_event_keeps_only_the_message_and_type_keys():
    payload = _frames([_stream_error_event("x")])[0]
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"message", "type"}


def test_stream_chunk_event_builds_a_chat_completion_chunk_envelope():
    payload = _frames(
        [
            _stream_chunk_event(
                stream_id="chatcmpl-x",
                created=1700000000,
                model="gpt-4o",
                index=0,
                delta=ChatCompletionDelta(role="assistant"),
            )
        ]
    )[0]
    assert payload == {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": {"role": "assistant"}}],
    }


def test_stream_chunk_event_omits_unset_delta_fields():
    payload = _frames(
        [
            _stream_chunk_event(
                stream_id="s",
                created=1,
                model="m",
                index=0,
                delta=ChatCompletionDelta(content="hi"),
            )
        ]
    )[0]
    assert payload["choices"][0]["delta"] == {"content": "hi"}


def test_stream_chunk_event_omits_finish_reason_when_none():
    payload = _frames(
        [
            _stream_chunk_event(
                stream_id="s", created=1, model="m", index=0, delta=ChatCompletionDelta()
            )
        ]
    )[0]
    assert "finish_reason" not in payload["choices"][0]


def test_stream_chunk_event_keeps_finish_reason_and_an_empty_delta():
    payload = _frames(
        [
            _stream_chunk_event(
                stream_id="s",
                created=1,
                model="m",
                index=2,
                delta=ChatCompletionDelta(),
                finish_reason="stop",
            )
        ]
    )[0]
    assert payload["choices"][0] == {
        "index": 2,
        "delta": {},
        "finish_reason": "stop",
    }


def test_stream_chunk_event_keeps_an_empty_string_content():
    # exclude_none=True drops None only; "" is a real (if empty) delta.
    payload = _frames(
        [
            _stream_chunk_event(
                stream_id="s",
                created=1,
                model="m",
                index=0,
                delta=ChatCompletionDelta(content=""),
            )
        ]
    )[0]
    assert payload["choices"][0]["delta"] == {"content": ""}


def test_stream_chunk_event_keeps_an_empty_tool_calls_list():
    payload = _frames(
        [
            _stream_chunk_event(
                stream_id="s",
                created=1,
                model="m",
                index=0,
                delta=ChatCompletionDelta(tool_calls=[]),
            )
        ]
    )[0]
    assert payload["choices"][0]["delta"] == {"tool_calls": []}


def test_stream_chunk_event_passes_tool_calls_through_unchanged():
    tool_calls = [
        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "f"}}
    ]
    payload = _frames(
        [
            _stream_chunk_event(
                stream_id="s",
                created=1,
                model="m",
                index=0,
                delta=ChatCompletionDelta(tool_calls=tool_calls),
            )
        ]
    )[0]
    assert payload["choices"][0]["delta"]["tool_calls"] == tool_calls


# ---------------------------------------------------------------------------
# _stream_events_for_provider_delta
# ---------------------------------------------------------------------------

_STREAM_KW = {"stream_id": "chatcmpl-t", "created": 42, "model": "gpt-4o"}


def _deltas(delta, *, started=None, finished=None):
    """Run one provider delta through the grouping helper."""
    started = set() if started is None else started
    finished = set() if finished is None else finished
    events = _stream_events_for_provider_delta(
        delta=delta,
        started_indexes=started,
        finished_indexes=finished,
        **_STREAM_KW,
    )
    return _frames(events), started, finished


def test_first_delta_opens_the_choice_with_an_assistant_role_frame():
    frames, started, _ = _deltas({"index": 0, "role": "assistant", "content": "hi"})
    assert frames[0]["choices"][0] == {"index": 0, "delta": {"role": "assistant"}}
    assert started == {0}


def test_a_delta_with_role_and_content_emits_role_then_content():
    frames, _, _ = _deltas({"index": 0, "role": "assistant", "content": "hi"})
    assert [frame["choices"][0]["delta"] for frame in frames] == [
        {"role": "assistant"},
        {"content": "hi"},
    ]


def test_the_role_frame_defaults_to_assistant_when_role_is_absent():
    frames, _, _ = _deltas({"index": 0, "content": "hi"})
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}


def test_an_empty_string_role_falls_back_to_assistant():
    frames, _, _ = _deltas({"index": 0, "role": "", "content": "hi"})
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}


def test_a_non_empty_role_is_used_verbatim():
    frames, _, _ = _deltas({"index": 0, "role": "tool", "content": "hi"})
    assert frames[0]["choices"][0]["delta"] == {"role": "tool"}


def test_the_role_frame_is_emitted_only_once_per_index():
    started = set()
    first, started, _ = _deltas({"index": 0, "content": "a"}, started=started)
    second, started, _ = _deltas({"index": 0, "content": "b"}, started=started)
    assert len(first) == 2
    assert [frame["choices"][0]["delta"] for frame in second] == [{"content": "b"}]


def test_a_content_only_delta_after_the_first_emits_just_content():
    frames, _, _ = _deltas({"index": 0, "content": "!"}, started={0})
    assert [frame["choices"][0]["delta"] for frame in frames] == [{"content": "!"}]


def test_an_index_defaults_to_zero_when_absent():
    frames, started, _ = _deltas({"content": "x"})
    assert frames[0]["choices"][0]["index"] == 0
    assert started == {0}


def test_a_string_index_is_coerced_with_int():
    frames, started, _ = _deltas({"index": "3", "content": "x"})
    assert all(frame["choices"][0]["index"] == 3 for frame in frames)
    assert started == {3}


def test_a_non_numeric_index_raises_value_error():
    with pytest.raises(ValueError):
        _deltas({"index": "not-a-number", "content": "x"})


def test_each_index_gets_its_own_role_frame():
    started = set()
    frames0, started, _ = _deltas({"index": 0, "content": "a"}, started=started)
    frames1, started, _ = _deltas({"index": 1, "content": "b"}, started=started)
    assert frames0[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert frames1[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert started == {0, 1}


def test_a_delta_with_no_content_and_no_tool_calls_emits_only_the_role_frame():
    frames, _, _ = _deltas({"index": 0})
    assert frames == [
        {
            "id": "chatcmpl-t",
            "object": "chat.completion.chunk",
            "created": 42,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }
    ]


def test_an_empty_string_content_still_emits_a_content_frame():
    frames, _, _ = _deltas({"index": 0, "content": ""})
    assert len(frames) == 2
    assert frames[1]["choices"][0]["delta"] == {"content": ""}


def test_an_empty_tool_calls_list_still_emits_a_payload_frame():
    frames, _, _ = _deltas({"index": 0, "tool_calls": []})
    assert frames[1]["choices"][0]["delta"] == {"tool_calls": []}


def test_tool_calls_alone_emit_the_payload_frame():
    tool_calls = [{"index": 0, "id": "c1", "type": "function", "function": {"name": "f"}}]
    frames, _, _ = _deltas({"index": 0, "tool_calls": tool_calls})
    assert frames[1]["choices"][0]["delta"] == {"tool_calls": tool_calls}


def test_content_and_tool_calls_are_emitted_in_one_payload_frame():
    frames, _, _ = _deltas({"index": 0, "content": "x", "tool_calls": []})
    assert len(frames) == 2
    assert frames[1]["choices"][0]["delta"] == {"content": "x", "tool_calls": []}


def test_a_finish_reason_emits_an_empty_delta_frame():
    frames, _, finished = _deltas({"index": 0, "finish_reason": "stop"})
    assert frames[-1]["choices"][0] == {
        "index": 0,
        "delta": {},
        "finish_reason": "stop",
    }
    assert finished == {0}


def test_a_finish_reason_is_emitted_once_per_index():
    started = set()
    finished = set()
    first, started, finished = _deltas(
        {"index": 0, "content": "a", "finish_reason": "stop"},
        started=started,
        finished=finished,
    )
    second, started, finished = _deltas(
        {"index": 0, "content": "b", "finish_reason": "stop"},
        started=started,
        finished=finished,
    )
    assert [frame["choices"][0].get("finish_reason") for frame in first] == [
        None,
        None,
        "stop",
    ]
    # Index 0 is already started and already finished: only the content frame.
    assert [frame["choices"][0]["delta"] for frame in second] == [{"content": "b"}]
    assert finished == {0}


def test_a_repeated_finish_on_an_unstarted_index_still_opens_the_choice():
    # The start check runs before the finish check, so the second pass over
    # the same delta emits a role frame plus the finish frame -- the empty
    # ``started_indexes`` is what keeps the finish itself idempotent.
    finished = set()
    first, _, finished = _deltas(
        {"index": 0, "finish_reason": "stop"}, finished=finished
    )
    second, _, finished = _deltas(
        {"index": 0, "finish_reason": "stop"}, finished=finished
    )
    assert [frame["choices"][0]["delta"] for frame in first] == [
        {"role": "assistant"},
        {},
    ]
    assert second == [first[0]]
    assert finished == {0}


def test_one_delta_can_emit_role_content_and_finish_together():
    frames, started, finished = _deltas(
        {"index": 0, "role": "assistant", "content": "done", "finish_reason": "length"}
    )
    assert [frame["choices"][0].get("finish_reason") for frame in frames] == [
        None,
        None,
        "length",
    ]
    assert started == {0}
    assert finished == {0}


def test_a_finish_reason_is_kept_verbatim_as_a_free_form_value():
    frames, _, _ = _deltas({"index": 0, "finish_reason": "content_filter"})
    assert frames[-1]["choices"][0]["finish_reason"] == "content_filter"


def test_an_unknown_extra_key_is_dropped_silently():
    frames, _, _ = _deltas({"index": 0, "content": "x", "logprobs": {"a": 1}})
    assert len(frames) == 2
    assert frames[1]["choices"][0]["delta"] == {"content": "x"}


def test_the_helpers_do_not_emit_a_done_frame():
    frames, _, _ = _deltas({"index": 0, "content": "x", "finish_reason": "stop"})
    assert None not in frames


# ---------------------------------------------------------------------------
# _stream_preflight_exception
# ---------------------------------------------------------------------------


class _UpstreamError(Exception):
    """An exception carrying an upstream HTTP status (httpx-style)."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def _preflight(message_or_exc):
    exc = (
        message_or_exc
        if isinstance(message_or_exc, BaseException)
        else Exception(message_or_exc)
    )
    return _stream_preflight_exception(exc)


def test_preflight_returns_an_openai_compat_error():
    assert isinstance(_preflight("boom"), OpenAICompatError)


def test_preflight_defaults_to_503_without_any_status_hint():
    err = _preflight("boom")
    assert err.status_code == 503
    assert err.detail == "Streaming request failed: boom"


def test_preflight_prefixes_the_detail_and_mirrors_it_in_str():
    err = _preflight("upstream exploded")
    assert err.detail == "Streaming request failed: upstream exploded"
    assert str(err) == "Streaming request failed: upstream exploded"


def test_preflight_keeps_an_empty_message_in_the_detail():
    assert _preflight("").detail == "Streaming request failed: "


def test_preflight_uses_the_exception_status_code_attribute():
    err = _preflight(_UpstreamError("upstream failed", 502))
    assert err.status_code == 502
    assert err.detail == "Streaming request failed: upstream failed"


def test_preflight_keeps_a_none_status_code_attribute_verbatim():
    # getattr finds the attribute, so the 503 default never applies.
    err = _preflight(_UpstreamError("x", None))
    assert err.status_code is None


def test_preflight_keeps_a_zero_status_code_attribute_verbatim():
    # The 503 default is a *missing-attribute* default, not a falsy one: an
    # explicit 0 survives even though it is not a valid HTTP status.
    err = _preflight(_UpstreamError("x", 0))
    assert err.status_code == 0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503, 599])
def test_preflight_adopts_an_http_status_token_in_range(status):
    err = _preflight(f"HTTP {status}: upstream rejected")
    assert err.status_code == status


@pytest.mark.parametrize("status", [399, 600, 0, 999])
def test_preflight_ignores_an_out_of_range_http_status_token(status):
    err = _preflight(f"HTTP {status}: upstream rejected")
    assert err.status_code == 503


def test_preflight_uses_the_first_http_token_when_two_are_present():
    assert _preflight("HTTP 429: upstream said HTTP 500").status_code == 429


def test_preflight_ignores_an_http_token_that_is_not_an_integer():
    assert _preflight("HTTP abc: bad gateway").status_code == 503


def test_preflight_ignores_a_single_token_http_prefix():
    # ``"HTTP:"`` splits into one token, so the ``len == 2`` guard skips it.
    assert _preflight("HTTP: bad gateway").status_code == 503


def test_preflight_accepts_the_http_token_without_a_space_before_the_colon():
    # The prefix is everything before the first colon, whitespace-split; the
    # colon itself may be adjacent to the status.
    assert _preflight("HTTP 404:no space").status_code == 404


def test_preflight_accepts_whitespace_before_the_colon():
    assert _preflight("HTTP 404 : spaced").status_code == 404


def test_preflight_uses_the_last_token_before_the_colon_as_the_status():
    # Only a two-token prefix is inspected; extra words push it out of shape.
    assert _preflight("upstream HTTP 404: x").status_code == 503


def test_preflight_ignores_tokens_before_the_colon_that_are_not_http():
    assert _preflight("ERROR 429: rate limited").status_code == 503


def test_preflight_does_not_look_past_the_first_colon():
    assert _preflight("upstream: HTTP 429: rate limited").status_code == 503


def test_preflight_maps_a_rate_limited_message_to_429():
    assert _preflight("rate-limited: try again later").status_code == 429


def test_preflight_finds_rate_limited_anywhere_in_the_message():
    assert _preflight("upstream says rate-limited for now").status_code == 429


def test_preflight_prefers_the_http_token_over_the_rate_limited_hint():
    assert _preflight("HTTP 500: rate-limited upstream").status_code == 500


def test_preflight_is_case_sensitive_for_the_rate_limited_hint():
    err = _preflight("Rate-Limited: try later")
    assert err.status_code == 503
    # The original message survives in the detail whatever the status.
    assert err.detail == "Streaming request failed: Rate-Limited: try later"


def test_preflight_does_not_credit_a_bare_rate_limit_phrase():
    # Only the hyphenated ``rate-limited`` marker is matched today.
    assert _preflight("rate limit exceeded").status_code == 503


def test_preflight_keeps_the_cause_message_for_a_plain_value_error():
    err = _preflight(ValueError("bad payload"))
    assert err.status_code == 503
    assert err.detail == "Streaming request failed: bad payload"


def test_preflight_returns_a_new_error_object_each_call():
    assert _preflight("boom") is not _preflight("boom")


# ---------------------------------------------------------------------------
# _route_to_provider_stream
# ---------------------------------------------------------------------------

_MESSAGES = [{"role": "user", "content": "hello"}]


def _route(model="llama-3.3-70b-versatile", provider="groq", *, engine=None, **kwargs):
    """Open the routing generator against a fake resolver and engine."""
    return _route_to_provider_stream(
        model,
        _MESSAGES,
        resolve_provider=_resolver(provider),
        get_engine=_engine_factory(engine if engine is not None else _FakeGraeae()),
        **kwargs,
    )


def test_route_to_provider_stream_is_an_async_generator_function():
    import inspect

    assert inspect.isasyncgenfunction(_route_to_provider_stream)


def test_route_yields_provider_chunks_unchanged():
    chunks = [{"index": 0, "content": "a"}, {"index": 0, "content": "b"}]
    engine = _FakeGraeae(chunks=chunks)
    assert asyncio.run(_collect(_route(engine=engine))) == chunks


def test_route_yields_nothing_for_an_empty_provider_stream():
    assert asyncio.run(_collect(_route(engine=_FakeGraeae()))) == []


def test_route_passes_the_full_dispatch_contract_to_route_stream():
    engine = _FakeGraeae(chunks=[{"index": 0, "content": "a"}])
    asyncio.run(_collect(_route(engine=engine)))
    assert engine.calls == [
        {
            "provider": "groq",
            "model": "llama-3.3-70b-versatile",
            "prompt": "[User]\nhello",
            "task_type": "reasoning",
            "timeout": 30,
            "generation_params": None,
            "request_params": None,
            "messages": _MESSAGES,
        }
    ]


def test_route_forwards_generation_and_request_params():
    # ``stop`` is only accepted by an openai/anthropic/gemini provider.
    engine = _FakeGraeae(providers={"openai": {"api": "openai"}})
    asyncio.run(
        _collect(
            _route(
                "gpt-4o",
                "openai",
                engine=engine,
                generation_params={"temperature": 0.2},
                request_params={"stop": ["\n\n"]},
            )
        )
    )
    call = engine.calls[0]
    assert call["generation_params"] == {"temperature": 0.2}
    assert call["request_params"] == {"stop": ["\n\n"]}


def test_route_refuses_a_request_param_the_provider_cannot_honour():
    engine = _FakeGraeae()
    agen = _route(
        engine=engine, request_params={"stop": ["\n\n"]}
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "provider groq does not support stop"
    assert engine.calls == []


def test_route_accepts_a_user_argument_and_leaves_the_dispatch_unchanged():
    # ``user`` is part of the signature but is not read: the provider call is
    # byte-identical with and without it. Pin the acceptance, not a negative
    # that could never fail.
    plain = _FakeGraeae()
    with_user = _FakeGraeae()
    asyncio.run(_collect(_route(engine=plain)))
    asyncio.run(
        _collect(
            _route_to_provider_stream(
                "llama-3.3-70b-versatile",
                _MESSAGES,
                user={"user_id": "alice"},
                resolve_provider=_resolver("groq"),
                get_engine=_engine_factory(with_user),
            )
        )
    )
    assert with_user.calls == plain.calls


def test_a_generator_that_is_never_iterated_makes_no_provider_call():
    engine = _FakeGraeae()
    _route(engine=engine)
    assert engine.calls == []


def test_route_strips_a_gateway_namespace_prefix_from_the_model():
    engine = _FakeGraeae()
    asyncio.run(_collect(_route("groq/llama-3.3-70b-versatile", "groq", engine=engine)))
    assert engine.calls[0]["model"] == "llama-3.3-70b-versatile"


def test_route_rejects_an_empty_message_list_before_resolving():
    agen = _route_to_provider_stream(
        "llama-3.3-70b-versatile", [], resolve_provider=_resolver("groq")
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "messages required"


def test_route_rejects_an_unknown_model_with_404():
    agen = _route_to_provider_stream(
        "no-such-model", _MESSAGES, resolve_provider=_resolver(None)
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == {
        "error": {
            "message": (
                "The model `no-such-model` does not exist or you do "
                "not have access to it."
            ),
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }


def test_route_rejects_a_route_policy_violation_with_400():
    # A closed-frontier model on a reseller route is refused by the policy.
    agen = _route_to_provider_stream(
        "gpt-4o", _MESSAGES, resolve_provider=_resolver("groq")
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"]["code"] == "route_policy_violation"
    assert "closed-frontier" in excinfo.value.detail["error"]["message"]


def test_route_lets_a_provider_config_validation_error_escape_unchanged():
    # The engine advertises no tool support, so tools are refused with 400.
    engine = _FakeGraeae(providers={"groq": {"api": "groq"}})
    agen = _route_to_provider_stream(
        "llama-3.3-70b-versatile",
        _MESSAGES,
        request_params={"tools": [{"type": "function"}]},
        resolve_provider=_resolver("groq"),
        get_engine=_engine_factory(engine),
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "provider groq does not support tool_calls"
    assert engine.calls == []


def test_route_does_not_wrap_a_provider_failure_in_another_error():
    boom = RuntimeError("provider fell over")
    engine = _FakeGraeae(error=boom)
    agen = _route(engine=engine)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value is boom


def test_route_logs_and_re_raises_a_provider_failure(caplog):
    engine = _FakeGraeae(chunks=[{"index": 0}], error=RuntimeError("fell over"))
    with caplog.at_level(logging.ERROR, logger=STREAMING_LOGGER):
        with pytest.raises(RuntimeError):
            asyncio.run(_collect(_route(engine=engine)))
    records = [r for r in caplog.records if r.name == STREAMING_LOGGER]
    assert any(
        "Streaming route to groq failed: fell over" in r.getMessage() for r in records
    )


def test_route_chunks_yielded_before_a_provider_failure_are_not_lost():
    async def _drain():
        events = []
        agen = _route(engine=_FakeGraeae(chunks=[{"index": 0}], error=RuntimeError("x")))
        try:
            async for chunk in agen:
                events.append(chunk)
        except RuntimeError:
            pass
        return events

    assert asyncio.run(_drain()) == [{"index": 0}]


def test_route_uses_the_injected_engine_factory_once():
    created = []

    def factory():
        created.append(1)
        return _FakeGraeae()

    asyncio.run(
        _collect(
            _route_to_provider_stream(
                "llama-3.3-70b-versatile",
                _MESSAGES,
                resolve_provider=_resolver("groq"),
                get_engine=factory,
            )
        )
    )
    assert created == [1]


# ---------------------------------------------------------------------------
# stream_event_source
# ---------------------------------------------------------------------------


def _source(stream, first_delta=None):
    return stream_event_source(
        provider_stream=stream,
        first_delta=first_delta,
        stream_id="chatcmpl-t",
        created=42,
        model="gpt-4o",
    )


def test_event_source_emits_only_done_for_an_empty_stream():
    stream = _FakeProviderStream()
    assert asyncio.run(_collect(_source(stream))) == [_SSE_TERMINATOR]


def test_event_source_closes_the_provider_stream_on_the_normal_path():
    stream = _FakeProviderStream()
    asyncio.run(_collect(_source(stream)))
    assert stream.closed
    assert stream.aclose_calls == 1


def test_event_source_emits_the_first_delta_before_the_stream():
    stream = _FakeProviderStream([{"index": 0, "content": "b"}])
    events = asyncio.run(_collect(_source(stream, first_delta={"index": 0, "content": "a"})))
    deltas = [frame["choices"][0]["delta"] for frame in _frames(events[:-1])]
    assert deltas == [{"role": "assistant"}, {"content": "a"}, {"content": "b"}]


def test_event_source_does_not_repeat_the_role_frame_seen_in_the_first_delta():
    stream = _FakeProviderStream([{"index": 0, "content": "b"}])
    events = asyncio.run(_collect(_source(stream, first_delta={"index": 0, "content": "a"})))
    payloads = _frames(events[:-1])
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert len([p for p in payloads if p["choices"][0]["delta"] == {"role": "assistant"}]) == 1


def test_event_source_treats_a_none_first_delta_as_absent():
    stream = _FakeProviderStream([{"index": 0, "content": "a"}])
    events = asyncio.run(_collect(_source(stream, first_delta=None)))
    assert len(events) == 3
    assert events[-1] == _SSE_TERMINATOR


def test_event_source_terminates_with_a_single_done_frame():
    stream = _FakeProviderStream([{"index": 0, "content": "a", "finish_reason": "stop"}])
    events = asyncio.run(_collect(_source(stream)))
    assert [event for event in events if event == _SSE_TERMINATOR] == [_SSE_TERMINATOR]
    assert events[-1] == _SSE_TERMINATOR


def test_event_source_keeps_the_chunk_envelope_from_its_arguments():
    stream = _FakeProviderStream([{"index": 0, "content": "a"}])
    payloads = _frames(asyncio.run(_collect(_source(stream)))[:-1])
    assert {p["id"] for p in payloads} == {"chatcmpl-t"}
    assert {p["created"] for p in payloads} == {42}
    assert {p["model"] for p in payloads} == {"gpt-4o"}
    assert {p["object"] for p in payloads} == {"chat.completion.chunk"}


def test_event_source_tracks_indexes_across_the_first_delta_and_the_stream():
    first = {"index": 1, "content": "a"}
    stream = _FakeProviderStream([{"index": 1, "content": "b", "finish_reason": "stop"}])
    payloads = _frames(asyncio.run(_collect(_source(stream, first_delta=first)))[:-1])
    assert [p["choices"][0]["delta"] for p in payloads] == [
        {"role": "assistant"},
        {"content": "a"},
        {"content": "b"},
        {},
    ]
    assert {p["choices"][0]["index"] for p in payloads} == {1}


def test_event_source_converts_a_stream_failure_into_an_error_frame():
    stream = _FakeProviderStream([{"index": 0, "content": "a"}], error=RuntimeError("boom"))
    events = asyncio.run(_collect(_source(stream)))
    assert _frames(events)[-2] == {
        "error": {"message": "boom", "type": "provider_stream_error"}
    }
    assert events[-1] == _SSE_TERMINATOR


def test_event_source_does_not_swallow_the_error_as_an_exception():
    stream = _FakeProviderStream(error=RuntimeError("boom"))
    events = asyncio.run(_collect(_source(stream)))
    assert len(events) == 2


def test_event_source_uses_the_exception_error_type_attribute():
    class _Timeout(Exception):
        error_type = "provider_timeout"

    stream = _FakeProviderStream(error=_Timeout("timed out"))
    payloads = _frames(asyncio.run(_collect(_source(stream))))
    assert payloads[0] == {
        "error": {"message": "timed out", "type": "provider_timeout"}
    }


def test_event_source_keeps_events_emitted_before_the_failure():
    stream = _FakeProviderStream([{"index": 0, "content": "a"}], error=RuntimeError("x"))
    events = asyncio.run(_collect(_source(stream)))
    assert _frames(events)[1]["choices"][0]["delta"] == {"content": "a"}


def test_event_source_closes_the_provider_stream_after_a_failure():
    stream = _FakeProviderStream(error=RuntimeError("x"))
    asyncio.run(_collect(_source(stream)))
    assert stream.closed


def test_event_source_logs_a_stream_failure(caplog):
    stream = _FakeProviderStream(error=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR, logger=STREAMING_LOGGER):
        asyncio.run(_collect(_source(stream)))
    assert any(
        "Streaming response failed after response start: boom" in r.getMessage()
        for r in caplog.records
        if r.name == STREAMING_LOGGER
    )


def test_event_source_survives_a_failing_close_on_the_normal_path(caplog):
    stream = _FakeProviderStream(close_error=RuntimeError("close failed"))
    with caplog.at_level(logging.DEBUG, logger=STREAMING_LOGGER):
        events = asyncio.run(_collect(_source(stream)))
    assert events == [_SSE_TERMINATOR]
    assert any(
        "Streaming response cleanup failed: close failed" in r.getMessage()
        for r in caplog.records
        if r.name == STREAMING_LOGGER
    )


def test_event_source_survives_a_failing_close_after_a_failure():
    stream = _FakeProviderStream(
        error=RuntimeError("boom"), close_error=RuntimeError("close failed")
    )
    events = asyncio.run(_collect(_source(stream)))
    assert _frames(events)[-2] == {
        "error": {"message": "boom", "type": "provider_stream_error"}
    }
    assert events[-1] == _SSE_TERMINATOR
    assert stream.aclose_calls == 1


def test_event_source_stops_consuming_when_the_consumer_stops():
    class _Counting(_FakeProviderStream):
        def __init__(self, count):
            super().__init__([{"index": 0, "content": str(i)} for i in range(count)])

        async def __anext__(self):
            self.seen = getattr(self, "seen", 0) + 1
            return await super().__anext__()

    stream = _Counting(50)

    async def _one():
        agen = _source(stream)
        first = await anext(agen)
        await agen.aclose()
        return first

    assert asyncio.run(_one()) != _SSE_TERMINATOR
    assert stream.seen == 1


def test_event_source_emits_one_error_frame_and_one_done_frame():
    # ``_frames`` maps the ``[DONE]`` sentinel to ``None``, so a ``None`` entry
    # *is* the DONE frame. The failing run must produce exactly one error
    # dict followed by exactly one DONE.
    stream = _FakeProviderStream(error=RuntimeError("boom"))
    payloads = _frames(asyncio.run(_collect(_source(stream))))

    done_frames = [p for p in payloads if p is None]
    error_frames = [p for p in payloads if p is not None and "error" in p]

    assert len(done_frames) == 1
    assert len(error_frames) == 1
    assert len(payloads) == 2, payloads
    # Ordering: the error frame precedes the DONE sentinel.
    assert payloads[0] == error_frames[0]
    assert payloads[-1] is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: stream_event_source builds the SSE error frame from str(exc). "
        "For an OpenAICompatError whose detail is a dict (the model_not_found / "
        "route_policy_violation shape), str(exc) is the Python repr of that dict, "
        "so the client receives a Python dict literal instead of the provider's "
        "message. The frame should carry the human message (e.g. "
        "detail['error']['message']) . Not fixed here: tests-only change."
    ),
)
def test_stream_error_frame_uses_readable_provider_message():
    detail = {
        "error": {
            "message": "The model `x` does not exist or you do not have access to it.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }
    stream = _FakeProviderStream(error=OpenAICompatError(503, detail))
    payloads = _frames(asyncio.run(_collect(_source(stream))))
    assert payloads[0]["error"]["message"] == detail["error"]["message"]


# ---------------------------------------------------------------------------
# Branch gaps: mid-stream type failures, resumption, cleanup edge cases
# ---------------------------------------------------------------------------


def test_an_explicitly_null_content_and_tool_calls_emit_no_payload_frame():
    # The guard is ``is not None``, so an *explicit* null is not a payload the
    # way an empty string or an empty list is.
    frames, _, _ = _deltas({"index": 0, "content": None, "tool_calls": None})
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert len(frames) == 1


def test_a_non_numeric_index_mid_stream_becomes_an_error_frame():
    # ``int(delta["index"])`` raises inside the generator body, so the failure
    # is handled the same way as a provider raising mid-stream.
    stream = _FakeProviderStream(
        [{"index": 0, "content": "a"}, {"index": "zz", "content": "b"}]
    )
    events = asyncio.run(_collect(_source(stream)))
    payloads = _frames(events)
    assert payloads[1]["choices"][0]["delta"] == {"content": "a"}
    assert payloads[2] == {
        "error": {
            "message": "invalid literal for int() with base 10: 'zz'",
            "type": "provider_stream_error",
        }
    }
    assert events[-1] == _SSE_TERMINATOR


def test_a_null_index_mid_stream_becomes_an_error_frame():
    stream = _FakeProviderStream([{"index": None, "content": "a"}])
    payloads = _frames(asyncio.run(_collect(_source(stream))))
    assert "int() argument must be" in payloads[0]["error"]["message"]


def test_the_role_frame_is_not_repeated_when_the_consumer_resumes():
    # ``started_indexes`` lives for the whole generator, so a suspension at a
    # yield boundary must not reset it.
    stream = _FakeProviderStream(
        [{"index": 0, "content": "a"}, {"index": 0, "content": "b"}]
    )

    async def _interleaved():
        agen = _source(stream)
        first = await anext(agen)  # role frame, then suspend
        rest = [item async for item in agen]
        return [first, *rest]

    payloads = _frames(asyncio.run(_interleaved())[:-1])
    assert [p["choices"][0]["delta"] for p in payloads] == [
        {"role": "assistant"},
        {"content": "a"},
        {"content": "b"},
    ]


def test_event_source_tolerates_a_provider_stream_without_aclose(caplog):
    # A bare async generator has ``aclose``; an object without one raises
    # AttributeError, which the cleanup guard swallows at DEBUG.
    class _NoClose:
        def __init__(self):
            self._items = [{"index": 0, "content": "a"}]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._items:
                return self._items.pop(0)
            raise StopAsyncIteration

    with caplog.at_level(logging.DEBUG, logger=STREAMING_LOGGER):
        events = asyncio.run(_collect(_source(_NoClose())))
    assert events[-1] == _SSE_TERMINATOR
    assert any(
        "Streaming response cleanup failed" in r.getMessage()
        for r in caplog.records
        if r.name == STREAMING_LOGGER
    )
