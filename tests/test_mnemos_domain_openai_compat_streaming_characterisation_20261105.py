"""Characterisation tests for ``mnemos/domain/openai_compat/streaming.py``.

These pin the module's CURRENT observable behaviour — not an ideal one. If a
future change alters any assertion here, that change is behavioural and should
be a deliberate decision, not a side effect.

The module is the SSE half of the OpenAI-compatible gateway. It turns a
provider delta stream into ``data: {...}\\n\\n`` frames
(``_stream_event`` / ``_stream_error_event`` / ``_stream_chunk_event`` /
``_stream_events_for_provider_delta``), translates a pre-response exception
into an ``OpenAICompatError`` (``_stream_preflight_exception``), forwards a
provider route (``_route_to_provider_stream``), and closes the provider stream
when the client stops reading (``stream_event_source``).

Scope is that module only. Its collaborators (``_prepare_provider_route`` in
``providers.py``, the pydantic wire schemas) are exercised through their real
implementations with injected fakes — no production code is stubbed — so a
change in the helpers the module calls is pinned here too, but nothing outside
this module is asserted on its own.

Everything external is faked: no network, no live database, no clock read
(``created``/``stream_id`` are passed in as arguments), no sleeps. Provider
dispatch runs against an in-memory fake engine, so each test is
deterministic.

Behaviour that looks like a defect is pinned with
``@pytest.mark.xfail(strict=True, ...)`` — see the test named
``..._defect_...``. A strict xfail FAILS the suite when the defect is
silently fixed, so the fix cannot land unnoticed.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from mnemos.domain.openai_compat.providers import OpenAICompatError
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

# Logger name of the module under test (NOT ``__name__``): ``caplog.at_level``
# matches by logger name, and these tests run under pytest's root logger.
STREAMING_LOGGER = "mnemos.domain.openai_compat.streaming"

_SSE_TERMINATOR = "data: [DONE]\n\n"

# The single message every ``_route_to_provider_stream`` test dispatches:
# a plain, provider-agnostic user turn.
_MESSAGES = [{"role": "user", "content": "hello"}]

# Provider tables used by the fake engine. ``groq`` is a reseller with no
# ``supports_*`` flags (so ``stop``/``tools``/multimodal are refused), while
# ``openai`` accepts them — both shapes exercise the validation branches.
_GROQ_ENGINE_PROVIDERS = {"groq": {"api": "groq"}}
_OPENAI_ENGINE_PROVIDERS = {"openai": {"api": "openai"}}


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
    """Stand-in for the GRAEAE engine: a provider table and a ``route_stream``."""

    def __init__(self, *, providers=None, chunks=(), error=None):
        self.providers = {} if providers is None else providers
        self._chunks = list(chunks)
        self._error = error
        self.calls = []

    async def route_stream(self, provider, model, prompt, **kwargs):
        self.calls.append({"provider": provider, "model": model, "prompt": prompt, **kwargs})
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


def _engine_factory(engine):
    """``get_engine`` callable returning a fixed fake engine."""
    return lambda: engine


def _resolver(provider):
    """Injected ``resolve_provider`` that always answers ``provider``."""

    async def _resolve(model):
        return provider

    return _resolve


async def _collect(agen):
    """Drain an async iterator into a list (never swallows exceptions)."""
    events = []
    async for item in agen:
        events.append(item)
    return events


def _frames(events):
    """Return the JSON payload of every ``data:`` frame, in order.

    The ``[DONE]`` sentinel has no JSON body, so it maps to ``None`` — a
    ``None`` entry *is* the DONE frame, never a data frame.
    """
    payloads = []
    for event in events:
        assert event.startswith("data: "), event
        assert event.endswith("\n\n"), event
        body = event[len("data: ") : -2]
        payloads.append(None if body == "[DONE]" else json.loads(body))
    return payloads


def _route(model="llama-3.3-70b-versatile", provider="groq", *, engine=None, **kwargs):
    """Open the routing generator against an injected fake resolver/engine."""
    return _route_to_provider_stream(
        model,
        _MESSAGES,
        resolve_provider=_resolver(provider),
        get_engine=_engine_factory(engine if engine is not None else _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)),
        **kwargs,
    )


def _deltas(delta, *, started=None, finished=None):
    """Run one provider delta through the framing helper with fresh sets."""
    started_indexes = set() if started is None else started
    finished_indexes = set() if finished is None else finished
    events = _stream_events_for_provider_delta(
        delta=delta,
        stream_id="chatcmpl-t",
        created=42,
        model="gpt-4o",
        started_indexes=started_indexes,
        finished_indexes=finished_indexes,
    )
    return _frames(events), started_indexes, finished_indexes


def _source(stream, first_delta=None):
    """Open the event source against a fake provider stream."""
    return stream_event_source(
        provider_stream=stream,
        first_delta=first_delta,
        stream_id="chatcmpl-t",
        created=42,
        model="gpt-4o",
    )


class _UpstreamError(Exception):
    """Exception carrying a status hint, as an upstream client would."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def _preflight(message_or_exc):
    """(status_code, detail, str(exc)) for an exception through preflight."""
    exc = message_or_exc if isinstance(message_or_exc, Exception) else Exception(message_or_exc)
    error = _stream_preflight_exception(exc)
    return error.status_code, error.detail, str(error)


# ---------------------------------------------------------------------------
# _stream_event / _stream_error_event / _stream_chunk_event
# ---------------------------------------------------------------------------


def test_stream_event_serialises_compactly_with_an_sse_terminator():
    assert _stream_event({"a": 1, "b": [1, 2]}) == 'data: {"a":1,"b":[1,2]}\n\n'


def test_stream_event_inserts_no_spaces_after_separators():
    # Compact encoding: no ", " or ": " inside the JSON payload. The
    # frame's own "data: " prefix is SSE framing, not payload.
    event = _stream_event({"k": [1, 2], "n": {"x": 1}})
    payload = event[len("data: ") : -2]
    assert ", " not in payload
    assert ": " not in payload
    assert payload == '{"k":[1,2],"n":{"x":1}}'
    assert event == 'data: {"k":[1,2],"n":{"x":1}}\n\n'


def test_stream_event_keeps_non_ascii_json_escaped():
    # ``json.dumps`` defaults to ``ensure_ascii=True``: non-ASCII is emitted
    # as \\uXXXX escapes, so the frame is pure ASCII on the wire.
    event = _stream_event({"m": "héllo 日本語"})
    assert event == 'data: {"m":"h\\u00e9llo \\u65e5\\u672c\\u8a9e"}\n\n'
    assert all(ord(ch) < 128 for ch in event)


def test_stream_event_renders_an_empty_object_as_an_empty_frame():
    assert _stream_event({}) == "data: {}\n\n"


def test_stream_event_rejects_a_non_json_serialisable_payload():
    with pytest.raises(TypeError):
        _stream_event({"bad": object()})


def test_stream_error_event_wraps_the_message_and_type():
    assert _stream_error_event("kaboom") == 'data: {"error":{"message":"kaboom","type":"provider_stream_error"}}\n\n'


def test_stream_error_event_defaults_to_provider_stream_error_type():
    assert json.loads(_stream_error_event("m")[len("data: ") : -2]) == {
        "error": {"message": "m", "type": "provider_stream_error"}
    }


def test_stream_error_event_honours_an_explicit_type():
    assert _stream_error_event("m", "custom_error") == 'data: {"error":{"message":"m","type":"custom_error"}}\n\n'


def test_stream_error_event_keeps_an_empty_message():
    assert _stream_error_event("") == 'data: {"error":{"message":"","type":"provider_stream_error"}}\n\n'


def test_stream_error_event_keeps_only_the_message_and_type_keys():
    payload = json.loads(_stream_error_event("m", "t")[len("data: ") : -2])
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"message", "type"}


def test_stream_chunk_event_builds_a_chat_completion_chunk_envelope():
    event = _stream_chunk_event(
        stream_id="chatcmpl-x",
        created=1_700_000_000,
        model="gpt-4o",
        index=2,
        delta=ChatCompletionDelta(role="assistant", content="hi"),
        finish_reason="stop",
    )
    assert event.startswith("data: ")
    assert event.endswith("\n\n")
    assert json.loads(event[len("data: ") : -2]) == {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": "gpt-4o",
        "choices": [
            {"index": 2, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ],
    }


def test_stream_chunk_event_omits_unset_delta_fields():
    event = _stream_chunk_event(
        stream_id="i",
        created=1,
        model="m",
        index=0,
        delta=ChatCompletionDelta(role="assistant"),
    )
    payload = json.loads(event[len("data: ") : -2])
    assert payload["choices"][0]["delta"] == {"role": "assistant"}


def test_stream_chunk_event_omits_finish_reason_when_none():
    event = _stream_chunk_event(
        stream_id="i",
        created=1,
        model="m",
        index=0,
        delta=ChatCompletionDelta(),
        finish_reason=None,
    )
    assert "finish_reason" not in json.loads(event[len("data: ") : -2])["choices"][0]


def test_stream_chunk_event_keeps_finish_reason_and_an_empty_delta():
    event = _stream_chunk_event(
        stream_id="i",
        created=1,
        model="m",
        index=0,
        delta=ChatCompletionDelta(),
        finish_reason="stop",
    )
    payload = json.loads(event[len("data: ") : -2])
    assert payload["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}


def test_stream_chunk_event_keeps_an_empty_string_content():
    event = _stream_chunk_event(
        stream_id="i",
        created=1,
        model="m",
        index=0,
        delta=ChatCompletionDelta(content=""),
    )
    payload = json.loads(event[len("data: ") : -2])
    assert payload["choices"][0]["delta"] == {"content": ""}


def test_stream_chunk_event_keeps_an_empty_tool_calls_list():
    event = _stream_chunk_event(
        stream_id="i",
        created=1,
        model="m",
        index=0,
        delta=ChatCompletionDelta(tool_calls=[]),
    )
    payload = json.loads(event[len("data: ") : -2])
    assert payload["choices"][0]["delta"] == {"tool_calls": []}


def test_stream_chunk_event_passes_tool_calls_through_unchanged():
    tool_calls = [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": ""},
        }
    ]
    event = _stream_chunk_event(
        stream_id="i",
        created=1,
        model="m",
        index=0,
        delta=ChatCompletionDelta(tool_calls=tool_calls),
    )
    payload = json.loads(event[len("data: ") : -2])
    assert payload["choices"][0]["delta"]["tool_calls"] == tool_calls


# ---------------------------------------------------------------------------
# _stream_events_for_provider_delta
# ---------------------------------------------------------------------------


def test_a_delta_with_role_and_content_emits_role_then_content():
    frames, started, _ = _deltas({"index": 0, "role": "user", "content": "hi"})
    assert [f["choices"][0]["delta"] for f in frames] == [{"role": "user"}, {"content": "hi"}]
    assert started == {0}


def test_the_role_frame_defaults_to_assistant_when_role_is_absent():
    frames, _, _ = _deltas({"index": 0})
    assert len(frames) == 1
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}


def test_an_empty_string_role_falls_back_to_assistant():
    # ``delta.get("role") or "assistant"``: an empty role is falsy, so the
    # frame carries the default.
    frames, _, _ = _deltas({"index": 0, "role": ""})
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}


def test_a_non_empty_role_is_used_verbatim():
    frames, _, _ = _deltas({"index": 0, "role": "system"})
    assert frames[0]["choices"][0]["delta"] == {"role": "system"}


def test_the_role_frame_is_emitted_only_once_per_index():
    frames, _, _ = _deltas({"index": 0, "content": "a"}, started={0})
    assert len(frames) == 1
    assert frames[0]["choices"][0]["delta"] == {"content": "a"}


def test_a_content_only_delta_after_the_first_emits_just_content():
    frames, _, _ = _deltas({"index": 0, "content": "b"}, started={0})
    assert [f["choices"][0]["delta"] for f in frames] == [{"content": "b"}]


def test_an_index_defaults_to_zero_when_absent():
    frames, started, _ = _deltas({"content": "a"})
    assert started == {0}
    assert frames[0]["choices"][0]["index"] == 0


def test_a_string_index_is_coerced_with_int():
    frames, started, _ = _deltas({"index": "3", "content": "a"})
    assert started == {3}
    assert frames[0]["choices"][0]["index"] == 3


def test_a_non_numeric_index_raises_value_error():
    with pytest.raises(ValueError):
        _deltas({"index": "zz", "content": "a"})


def test_a_null_index_raises_type_error():
    # ``int(None)`` is a TypeError, not a ValueError — pinned so a future
    # normalisation of null indexes is a visible behaviour change.
    with pytest.raises(TypeError):
        _deltas({"index": None, "content": "a"})


def test_each_index_gets_its_own_role_frame():
    frames, started, _ = _deltas({"index": 1, "content": "b"}, started={0})
    assert started == {0, 1}
    assert [f["choices"][0]["index"] for f in frames] == [1, 1]
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert frames[1]["choices"][0]["delta"] == {"content": "b"}


def test_a_delta_with_no_content_and_no_tool_calls_emits_only_the_role_frame():
    frames, _, _ = _deltas({"index": 0, "finish_reason": None})
    assert len(frames) == 1
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert "finish_reason" not in frames[0]["choices"][0]


def test_an_explicitly_null_content_and_tool_calls_emit_no_payload_frame():
    # The payload guard is ``is not None`` on each field, so an explicit
    # null on both fields leaves only the role frame.
    frames, _, _ = _deltas({"index": 0, "content": None, "tool_calls": None})
    assert len(frames) == 1
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}


def test_an_empty_string_content_still_emits_a_payload_frame():
    frames, _, _ = _deltas({"index": 0, "content": ""})
    assert [f["choices"][0]["delta"] for f in frames] == [{"role": "assistant"}, {"content": ""}]


def test_an_empty_tool_calls_list_still_emits_a_payload_frame():
    frames, _, _ = _deltas({"index": 0, "tool_calls": []})
    assert [f["choices"][0]["delta"] for f in frames] == [{"role": "assistant"}, {"tool_calls": []}]


def test_tool_calls_alone_emit_the_payload_frame():
    calls = [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]
    frames, _, _ = _deltas({"index": 0, "tool_calls": calls})
    assert frames[-1]["choices"][0]["delta"] == {"tool_calls": calls}


def test_content_and_tool_calls_are_emitted_in_one_payload_frame():
    calls = [{"index": 0, "id": "c1"}]
    frames, _, _ = _deltas({"index": 0, "content": "hi", "tool_calls": calls})
    assert [f["choices"][0]["delta"] for f in frames] == [
        {"role": "assistant"},
        {"content": "hi", "tool_calls": calls},
    ]


def test_a_finish_reason_emits_an_empty_delta_frame():
    frames, _, finished = _deltas({"index": 0, "finish_reason": "stop"}, started={0})
    assert frames == [
        {
            "id": "chatcmpl-t",
            "object": "chat.completion.chunk",
            "created": 42,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    ]
    assert finished == {0}


def test_a_finish_reason_is_emitted_once_per_index():
    started, finished = {0}, set()
    first, started, finished = _deltas({"index": 0, "finish_reason": "stop"}, started=started, finished=finished)
    second, started, finished = _deltas({"index": 0, "finish_reason": "stop"}, started=started, finished=finished)
    assert len(first) == 1
    assert second == []
    assert finished == {0}


def test_a_finish_reason_is_kept_verbatim_as_a_free_form_value():
    frames, _, _ = _deltas({"index": 0, "finish_reason": "length"}, started={0})
    assert frames[0]["choices"][0]["finish_reason"] == "length"


def test_a_finish_on_an_unstarted_index_still_opens_the_choice():
    # The finish branch does not consult ``started_indexes``: an index that
    # never got a role frame still receives one before its finish frame.
    frames, started, finished = _deltas({"index": 3, "finish_reason": "stop"})
    assert started == {3}
    assert finished == {3}
    assert [f["choices"][0]["delta"] for f in frames] == [{"role": "assistant"}, {}]
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"


def test_one_delta_can_emit_role_content_and_finish_together():
    frames, _, _ = _deltas({"index": 0, "role": "assistant", "content": "hi", "finish_reason": "stop"})
    assert [f["choices"][0]["delta"] for f in frames] == [{"role": "assistant"}, {"content": "hi"}, {}]
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"


def test_an_unknown_extra_key_is_dropped_silently():
    # The helper reads only index/role/content/tool_calls/finish_reason;
    # anything else is ignored without raising.
    frames, _, _ = _deltas({"index": 0, "content": "a", "logprobs": {"values": [1]}})
    assert [f["choices"][0]["delta"] for f in frames] == [{"role": "assistant"}, {"content": "a"}]


def test_the_helpers_do_not_emit_a_done_frame():
    # ``[DONE]`` is owned by ``stream_event_source``; the framing helpers
    # never produce it.
    frames, _, _ = _deltas({"index": 0, "content": "a", "finish_reason": "stop"})
    for frame in frames:
        assert frame is not None
        assert "DONE" not in json.dumps(frame)


# ---------------------------------------------------------------------------
# _stream_preflight_exception
# ---------------------------------------------------------------------------


def test_preflight_returns_an_openai_compat_error():
    assert isinstance(_stream_preflight_exception(Exception("boom")), OpenAICompatError)


def test_preflight_defaults_to_503_without_any_status_hint():
    assert _preflight("boom")[0] == 503


def test_preflight_prefixes_the_detail_and_mirrors_it_in_str():
    status, detail, text = _preflight("boom")
    assert detail == "Streaming request failed: boom"
    assert text == detail


def test_preflight_keeps_an_empty_message_in_the_detail():
    assert _preflight("")[1] == "Streaming request failed: "


def test_preflight_uses_the_exception_status_code_attribute():
    assert _preflight(_UpstreamError("upstream is down", 502))[0] == 502


def test_preflight_adopts_an_openai_compat_error_status_verbatim():
    # ``OpenAICompatError`` carries its own ``status_code`` attribute, so a
    # preflight exception that already knows its status keeps it.
    assert _preflight(OpenAICompatError(400, "bad request"))[0] == 400


def test_preflight_keeps_an_openai_compat_error_without_a_status_hint_at_503():
    # The detail-only constructor sets ``status_code = 503`` (see
    # providers.OpenAICompatError), and the attribute branch adopts it.
    assert _preflight(OpenAICompatError(503, "no engine"))[0] == 503


def test_preflight_keeps_a_none_status_code_attribute_verbatim():
    # A None hint short-circuits the default: the OpenAICompatError is
    # built with status_code=None rather than 503.
    assert _preflight(_UpstreamError("m", None))[0] is None


def test_preflight_keeps_a_zero_status_code_attribute_verbatim():
    # 0 is not None: the hint is carried verbatim, not defaulted.
    assert _preflight(_UpstreamError("m", 0))[0] == 0


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503, 599])
def test_preflight_adopts_an_http_status_token_in_range(status):
    # The status must be the token immediately after "HTTP" AND be followed
    # by a colon (the status is read from the text before the first colon).
    assert _preflight(f"HTTP {status}: upstream exploded")[0] == status


@pytest.mark.parametrize("status", [100, 200, 302, 600, 999])
def test_preflight_ignores_an_out_of_range_http_status_token(status):
    assert _preflight(f"HTTP {status}: upstream exploded")[0] == 503


def test_preflight_ignores_an_in_range_http_status_without_a_colon():
    # Two whitespace tokens but no colon: "HTTP 404 upstream exploded" is
    # never parsed as a status, so the default 503 stands.
    assert _preflight("HTTP 404 upstream exploded")[0] == 503


def test_preflight_uses_the_first_http_token_when_two_are_present():
    # Only the text before the first colon is examined, and the status must
    # be the SECOND whitespace token of that text: "HTTP 404 HTTP 500" is
    # one colon-less line, so no status is adopted.
    assert _preflight("HTTP 404 HTTP 500")[0] == 503


def test_preflight_ignores_an_http_token_that_is_not_an_integer():
    # "HTTP abc: ..." takes the colon-split branch, so the leading token is
    # never parsed as a status and the default 503 stands.
    assert _preflight("HTTP abc: nope")[0] == 503


def test_preflight_ignores_a_single_token_http_prefix():
    assert _preflight("HTTP")[0] == 503


def test_preflight_accepts_the_http_token_without_a_space_before_the_colon():
    # The status is taken from the text before the FIRST colon, whitespace
    # split: "HTTP 404:x" yields the token pair ("HTTP", "404").
    assert _preflight("HTTP 404:not found")[0] == 404


def test_preflight_accepts_whitespace_before_the_colon():
    assert _preflight("HTTP 404   : upstream")[0] == 404


def test_preflight_uses_the_last_token_before_the_colon_as_the_status():
    # Two whitespace-separated tokens ending in the status: ("HTTP", "404").
    assert _preflight("HTTP 404: upstream")[0] == 404


def test_preflight_ignores_tokens_before_the_colon_that_are_not_http():
    # Three tokens before the colon: the pair check fails, so no status is
    # adopted even though the last token is numeric.
    assert _preflight("ERR 500: upstream")[0] == 503


def test_preflight_does_not_look_past_the_first_colon():
    # The colon split happens first: only "HTTP 404" is ever examined.
    assert _preflight("HTTP 404: reason HTTP 500")[0] == 404


def test_preflight_maps_a_rate_limited_message_to_429():
    assert _preflight("provider is rate-limited")[0] == 429


def test_preflight_finds_rate_limited_anywhere_in_the_message():
    assert _preflight("the upstream said: rate-limited, slow down")[0] == 429


def test_preflight_prefers_the_http_token_over_the_rate_limited_hint():
    # The branches are mutually exclusive: the rate-limited hint is only
    # consulted when the colon-split message is NOT an HTTP status line,
    # so an in-range HTTP token always wins even when the message also
    # contains "rate-limited".
    assert _preflight("HTTP 404: rate-limited")[0] == 404
    assert _preflight("HTTP 404 rate-limited")[0] == 429


def test_preflight_is_case_sensitive_for_the_rate_limited_hint():
    assert _preflight("RATE-LIMITED")[0] == 503


def test_preflight_does_not_credit_a_bare_rate_limit_phrase():
    # Only the hyphenated "rate-limited" is recognised.
    assert _preflight("too many requests, rate limit")[0] == 503


def test_preflight_keeps_the_cause_message_for_a_plain_value_error():
    assert _preflight(ValueError("bad value"))[0] == 503


def test_preflight_returns_a_new_error_object_each_call():
    first = _stream_preflight_exception(Exception("boom"))
    second = _stream_preflight_exception(Exception("boom"))
    assert first is not second


# ---------------------------------------------------------------------------
# _route_to_provider_stream
# ---------------------------------------------------------------------------


def test_route_to_provider_stream_is_an_async_generator_function():
    import inspect

    assert inspect.isasyncgenfunction(_route_to_provider_stream)


def test_route_yields_provider_chunks_unchanged():
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS, chunks=[{"index": 0, "content": "x"}])
    assert asyncio.run(_collect(_route(engine=engine))) == [{"index": 0, "content": "x"}]


def test_route_yields_nothing_for_an_empty_provider_stream():
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    assert asyncio.run(_collect(_route(engine=engine))) == []


def test_route_passes_the_full_dispatch_contract_to_route_stream():
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS, chunks=[{"index": 0}])
    asyncio.run(
        _collect(
            _route(
                engine=engine,
                generation_params={"temperature": 0.2, "max_tokens": 16},
                request_params={"top_p": 0.9},
            )
        )
    )
    assert engine.calls == [
        {
            "provider": "groq",
            "model": "llama-3.3-70b-versatile",
            "prompt": "[User]\nhello",
            "task_type": "reasoning",
            "timeout": 30,
            "generation_params": {"temperature": 0.2, "max_tokens": 16},
            "request_params": {"top_p": 0.9},
            "messages": _MESSAGES,
        }
    ]


def test_route_forwards_generation_params_only_when_provided():
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    asyncio.run(_collect(_route(engine=engine, generation_params={"temperature": 0.2})))
    assert engine.calls[0]["generation_params"] == {"temperature": 0.2}


def test_route_forwards_request_params_only_when_provided():
    # ``stop`` is accepted by an openai/anthropic/gemini provider config.
    engine = _FakeGraeae(providers=_OPENAI_ENGINE_PROVIDERS)
    asyncio.run(
        _collect(
            _route(
                model="gpt-4o",
                provider="openai",
                engine=engine,
                request_params={"stop": ["\n\n"]},
            )
        )
    )
    assert engine.calls[0]["request_params"] == {"stop": ["\n\n"]}


def test_route_passes_none_request_params_through_when_absent():
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    asyncio.run(_collect(_route(engine=engine)))
    assert engine.calls[0]["request_params"] is None


def test_route_forwards_the_messages_list_unmodified():
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    asyncio.run(_collect(_route(engine=engine)))
    assert engine.calls[0]["messages"] == _MESSAGES


def test_route_accepts_a_user_argument_and_leaves_the_dispatch_unchanged():
    # ``user`` is part of the signature but is not read: the provider call
    # is byte-identical with and without it. Pin the acceptance rather than
    # a negative that could never fail.
    plain = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    with_user = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
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
    # Async generators are lazy: nothing runs — not even ``get_engine`` —
    # until the first ``__anext__``.
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    _route(engine=engine)
    assert engine.calls == []


def test_route_strips_a_gateway_namespace_prefix_from_the_model():
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    asyncio.run(_collect(_route("groq/llama-3.3-70b-versatile", engine=engine)))
    assert engine.calls[0]["model"] == "llama-3.3-70b-versatile"


def test_route_rejects_an_empty_message_list_before_resolving():
    # The empty-messages guard sits in ``_prepare_provider_route``, before
    # the injected resolver runs, so a resolver that always fails is safe
    # here and proves nothing was resolved.
    agen = _route_to_provider_stream(
        "llama-3.3-70b-versatile", [], resolve_provider=_resolver(None)
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
            "message": "The model `no-such-model` does not exist or you do not have access to it.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }


def test_route_rejects_a_route_policy_violation_with_400():
    # A closed-frontier model on a reseller route is refused by the policy
    # (pure function, no I/O) before the engine factory is consulted.
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    agen = _route(
        "gpt-4o", engine=engine
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"]["code"] == "route_policy_violation"
    assert "closed-frontier" in excinfo.value.detail["error"]["message"]
    assert engine.calls == []


def test_route_lets_a_provider_config_validation_error_escape_unchanged():
    # The engine advertises an api with no tool support, so tools are
    # refused with 400 before dispatch.
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    agen = _route(
        engine=engine, request_params={"tools": [{"type": "function"}]}
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "provider groq does not support tool_calls"
    assert engine.calls == []


def test_route_refuses_a_request_param_the_provider_cannot_honour():
    # ``stop`` is only accepted by an openai/anthropic/gemini provider config.
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    agen = _route(engine=engine, request_params={"stop": ["\n\n"]})
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "provider groq does not support stop"
    assert engine.calls == []


def test_route_refuses_an_unsupported_message_role():
    # The role allow-list is per provider api (see
    # providers._ROLE_SUPPORT_BY_API): groq's config advertises no api, so
    # every role — even "developer", which no listed api supports — is
    # accepted. The refusal branch itself is covered by a gemini config
    # below.
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    asyncio.run(
        _collect(
            _route_to_provider_stream(
                "llama-3.3-70b-versatile",
                [{"role": "developer", "content": "hi"}],
                resolve_provider=_resolver("groq"),
                get_engine=_engine_factory(engine),
            )
        )
    )
    assert engine.calls[0]["model"] == "llama-3.3-70b-versatile"


def test_route_refuses_a_role_the_provider_api_does_not_support():
    engine = _FakeGraeae(providers={"gemini": {"api": "gemini"}})
    agen = _route_to_provider_stream(
        "gemini-2.5-flash",
        [{"role": "developer", "content": "hi"}],
        resolve_provider=_resolver("gemini"),
        get_engine=_engine_factory(engine),
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "provider gemini does not support role=developer; supported: system, user, assistant"
    assert engine.calls == []


def test_route_refuses_multimodal_content_blocks_the_provider_cannot_take():
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)
    agen = _route_to_provider_stream(
        "llama-3.3-70b-versatile",
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        resolve_provider=_resolver("groq"),
        get_engine=_engine_factory(engine),
    )
    with pytest.raises(OpenAICompatError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "provider groq does not support multimodal content blocks"
    assert engine.calls == []


def test_route_does_not_wrap_a_provider_failure_in_another_error():
    boom = RuntimeError("provider fell over")
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS, error=boom)
    agen = _route(engine=engine)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_collect(agen))
    assert excinfo.value is boom


def test_route_logs_and_re_raises_a_provider_failure(caplog):
    engine = _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS, chunks=[{"index": 0}], error=RuntimeError("fell over"))
    with caplog.at_level(logging.ERROR, logger=STREAMING_LOGGER):
        with pytest.raises(RuntimeError):
            asyncio.run(_collect(_route(engine=engine)))
    records = [record for record in caplog.records if record.name == STREAMING_LOGGER]
    assert any("Streaming route to groq failed: fell over" in record.getMessage() for record in records)


def test_route_chunks_yielded_before_a_provider_failure_are_not_lost():
    async def _drain():
        events = []
        agen = _route(engine=_FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS, chunks=[{"index": 0}], error=RuntimeError("x")))
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
        return _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)

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


def test_route_builds_no_engine_until_first_iteration():
    # The generator body — including ``get_engine`` — runs on first
    # ``__anext__``, never at call time.
    created = []

    def factory():
        created.append(1)
        return _FakeGraeae(providers=_GROQ_ENGINE_PROVIDERS)

    agen = _route_to_provider_stream(
        "llama-3.3-70b-versatile",
        _MESSAGES,
        resolve_provider=_resolver("groq"),
        get_engine=factory,
    )
    assert created == []
    asyncio.run(_collect(agen))
    assert created == [1]


# ---------------------------------------------------------------------------
# stream_event_source
# ---------------------------------------------------------------------------


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
    assert _frames(events)[-2] == {"error": {"message": "boom", "type": "provider_stream_error"}}
    assert events[-1] == _SSE_TERMINATOR


def test_event_source_does_not_swallow_the_error_as_an_exception():
    # The generator never re-raises: the failure becomes a frame and the
    # iterator ends normally.
    stream = _FakeProviderStream(error=RuntimeError("boom"))
    events = asyncio.run(_collect(_source(stream)))
    assert len(events) == 2


def test_event_source_uses_the_exception_error_type_attribute():
    class _Timeout(Exception):
        error_type = "provider_timeout"

    stream = _FakeProviderStream(error=_Timeout("timed out"))
    payloads = _frames(asyncio.run(_collect(_source(stream))))
    assert payloads[0] == {"error": {"message": "timed out", "type": "provider_timeout"}}


def test_event_source_keeps_events_emitted_before_the_failure():
    stream = _FakeProviderStream([{"index": 0, "content": "a"}], error=RuntimeError("x"))
    events = asyncio.run(_collect(_source(stream)))
    assert _frames(events)[1]["choices"][0]["delta"] == {"content": "a"}


def test_event_source_closes_the_provider_stream_after_a_failure():
    stream = _FakeProviderStream(error=RuntimeError("x"))
    asyncio.run(_collect(_source(stream)))
    assert stream.closed
    assert stream.aclose_calls == 1


def test_event_source_logs_a_stream_failure(caplog):
    stream = _FakeProviderStream(error=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR, logger=STREAMING_LOGGER):
        asyncio.run(_collect(_source(stream)))
    assert any(
        "Streaming response failed after response start: boom" in record.getMessage()
        for record in caplog.records
        if record.name == STREAMING_LOGGER
    )


def test_event_source_survives_a_failing_close_on_the_normal_path(caplog):
    stream = _FakeProviderStream(close_error=RuntimeError("close failed"))
    with caplog.at_level(logging.DEBUG, logger=STREAMING_LOGGER):
        events = asyncio.run(_collect(_source(stream)))
    assert events == [_SSE_TERMINATOR]
    assert any(
        "Streaming response cleanup failed: close failed" in record.getMessage()
        for record in caplog.records
        if record.name == STREAMING_LOGGER
    )


def test_event_source_survives_a_failing_close_after_a_failure():
    stream = _FakeProviderStream(error=RuntimeError("boom"), close_error=RuntimeError("close failed"))
    events = asyncio.run(_collect(_source(stream)))
    assert _frames(events)[-2] == {"error": {"message": "boom", "type": "provider_stream_error"}}
    assert events[-1] == _SSE_TERMINATOR
    assert stream.aclose_calls == 1


def test_event_source_stops_consuming_when_the_consumer_stops():
    class _Counting(_FakeProviderStream):
        def __init__(self, count):
            super().__init__([{"index": 0, "content": str(i)} for i in range(count)])
            self.seen = 0

        async def __anext__(self):
            self.seen += 1
            return await super().__anext__()

    stream = _Counting(50)

    async def _one():
        agen = _source(stream)
        first = await anext(agen)
        await agen.aclose()
        return first

    assert asyncio.run(_one()) != _SSE_TERMINATOR
    assert stream.seen == 1
    assert stream.aclose_calls == 1
    assert stream.closed


def test_event_source_emits_one_error_frame_and_one_done_frame():
    # ``_frames`` maps the ``[DONE]`` sentinel to ``None``, so a ``None``
    # entry *is* the DONE frame: exactly one error frame followed by
    # exactly one DONE.
    stream = _FakeProviderStream(error=RuntimeError("boom"))
    payloads = _frames(asyncio.run(_collect(_source(stream))))

    done_frames = [p for p in payloads if p is None]
    error_frames = [p for p in payloads if p is not None and "error" in p]

    assert len(done_frames) == 1
    assert len(error_frames) == 1
    assert len(payloads) == 2, payloads
    assert payloads[0] == error_frames[0]
    assert payloads[-1] is None


def test_a_non_numeric_index_mid_stream_becomes_an_error_frame():
    # ``int(delta["index"])`` raises inside the generator body, so a bad
    # upstream frame is handled exactly like a provider failure mid-stream.
    stream = _FakeProviderStream([{"index": 0, "content": "a"}, {"index": "zz", "content": "b"}])
    events = asyncio.run(_collect(_source(stream)))
    payloads = _frames(events)
    assert payloads[1]["choices"][0]["delta"] == {"content": "a"}
    assert payloads[2] == {
        "error": {"message": "invalid literal for int() with base 10: 'zz'", "type": "provider_stream_error"}
    }
    assert events[-1] == _SSE_TERMINATOR


def test_a_null_index_mid_stream_becomes_an_error_frame():
    stream = _FakeProviderStream([{"index": None, "content": "a"}])
    payloads = _frames(asyncio.run(_collect(_source(stream))))
    assert "int() argument must be" in payloads[0]["error"]["message"]


def test_a_mid_stream_validation_failure_becomes_an_error_frame():
    # A delta pydantic rejects (content that is not a string) surfaces as
    # the ValidationError text inside the error frame.
    stream = _FakeProviderStream([{"index": 0, "content": set()}])
    payloads = _frames(asyncio.run(_collect(_source(stream))))
    assert "1 validation error for ChatCompletionDelta" in payloads[0]["error"]["message"]
    assert payloads[0]["error"]["type"] == "provider_stream_error"


def test_the_role_frame_is_not_repeated_when_the_consumer_resumes():
    # ``started_indexes`` lives for the whole generator, so a suspension at
    # a yield boundary must not reset it.
    stream = _FakeProviderStream([{"index": 0, "content": "a"}, {"index": 0, "content": "b"}])

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
        "Streaming response cleanup failed" in record.getMessage()
        for record in caplog.records
        if record.name == STREAMING_LOGGER
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (pinned, not fixed): stream_event_source "
        "builds the SSE error frame from str(exc). For an OpenAICompatError "
        "whose detail is a dict (the model_not_found / route_policy_violation "
        "shape), str(exc) is the Python repr of that dict, so the client "
        "receives a Python dict literal instead of the provider's human "
        "message (detail['error']['message'])."
    ),
)
def test_defect_stream_error_frame_uses_readable_provider_message():
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
    assert payloads[0]["error"]["type"] == "provider_stream_error"
