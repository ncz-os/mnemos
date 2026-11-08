"""Characterisation tests for ``mnemos/domain/openai_compat/schemas.py``.

The module is the OpenAI-compatible wire contract (request, non-streaming
response, streaming chunk, tool and model-list envelopes) built on pydantic
v2. It had zero direct test references before this file, so everything
pinned here is *today's* behaviour, not a design proposal: if a value is
changed on purpose, update the pin along with the change.

Scope: the whole public surface reachable without I/O — every model class in
the module, the ``ChatContent`` union, the two ``model_validator`` branches
on ``ContentBlock``, and each ``extra="forbid"`` / ``extra="ignore"``
asymmetry. No network, clock, database or sleep is involved, so no fakes are
needed: the schemas are pure data transforms.

Notable behaviour pinned as-is (see the test names for detail):

* ``ContentBlock`` enforces the payload for its own ``type`` but does not
  forbid the *other* payload — a ``text`` block may carry an ``image_url``
  and vice versa. Only the required side is checked.
* ``ChatMessage.function_call`` and
  ``ChatCompletionRequest.mnemos_inject_memory`` are ``exclude=True``: they
  are accepted on input but dropped from ``model_dump()`` / JSON. The
  request field therefore does NOT survive a ``model_validate(model_dump())``
  round-trip, while the message field does survive because it is read back
  with attribute access that bypasses the dump. Both are pinned, the lossy
  round-trip included.
* The request models use ``extra="forbid"``, but the response/stream/model
  envelopes do not — extra keys on those are silently dropped.
* ``ChatCompletionStreamRequest`` only changes the *default* of ``stream``;
  an explicit ``stream=False`` is accepted and a non-bool (``None``) is a
  ``bool_type`` error.
"""

from __future__ import annotations

from typing import Dict, List

import pytest
from pydantic import BaseModel, ValidationError

from mnemos.domain.openai_compat import schemas
from mnemos.domain.openai_compat.schemas import (
    ChatCompletionChoice,
    ChatCompletionDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseMessage,
    ChatCompletionStreamChoice,
    ChatCompletionStreamRequest,
    ChatCompletionStreamResponse,
    ChatContent,
    ChatMessage,
    ContentBlock,
    ModelInfo,
    ModelsResponse,
    Tool,
    ToolFunction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_errors(exc_info: pytest.ExceptionInfo[ValidationError]) -> List[Dict[str, object]]:
    """Errors of a ValidationError as plain dicts (type + message)."""
    return [
        {"type": err["type"], "msg": err["msg"], "loc": err["loc"]}
        for err in exc_info.value.errors()
    ]


def _model_classes() -> List[type[BaseModel]]:
    """Every pydantic model class the module declares."""
    found = []
    for name in dir(schemas):
        obj = getattr(schemas, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            found.append(obj)
    return sorted(found, key=lambda cls: cls.__name__)


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_declares_exactly_the_expected_model_classes():
    names = [cls.__name__ for cls in _model_classes()]
    assert names == [
        "ChatCompletionChoice",
        "ChatCompletionDelta",
        "ChatCompletionRequest",
        "ChatCompletionResponse",
        "ChatCompletionResponseMessage",
        "ChatCompletionStreamChoice",
        "ChatCompletionStreamRequest",
        "ChatCompletionStreamResponse",
        "ChatMessage",
        "ContentBlock",
        "ModelInfo",
        "ModelsResponse",
        "Tool",
        "ToolFunction",
    ]


def test_chat_content_is_the_str_or_content_block_list_union():
    # The alias is used by ChatMessage.content and the response message; it
    # resolves to exactly two members, in that order.
    args = ChatContent.__args__
    assert args[0] is str
    assert args[1] == List[ContentBlock]
    assert len(args) == 2


# ---------------------------------------------------------------------------
# ContentBlock — extra="forbid", the two payload validators, literal type
# ---------------------------------------------------------------------------


def test_content_block_text_block_keeps_defaults_for_other_fields():
    block = ContentBlock(type="text", text="hello")

    assert block.type == "text"
    assert block.text == "hello"
    assert block.image_url is None
    assert block.model_dump() == {"type": "text", "text": "hello", "image_url": None}


def test_content_block_image_url_block_keeps_default_for_text():
    block = ContentBlock(type="image_url", image_url={"url": "https://example.test/a.png"})

    assert block.type == "image_url"
    assert block.text is None
    assert block.image_url == {"url": "https://example.test/a.png"}


def test_content_block_text_type_without_text_raises_value_error_with_message():
    with pytest.raises(ValidationError) as exc_info:
        ContentBlock(type="text")

    errors = _validate_errors(exc_info)
    assert len(errors) == 1
    assert errors[0]["type"] == "value_error"
    assert errors[0]["msg"] == "Value error, text content block requires text"


def test_content_block_image_url_type_without_image_url_raises_value_error():
    with pytest.raises(ValidationError) as exc_info:
        ContentBlock(type="image_url")

    errors = _validate_errors(exc_info)
    assert len(errors) == 1
    assert errors[0]["type"] == "value_error"
    assert errors[0]["msg"] == "Value error, image_url content block requires image_url"


def test_content_block_empty_text_string_is_accepted_as_present():
    # The validator checks ``is None``, not falsiness: "" satisfies the
    # "requires text" rule, and an empty image_url dict satisfies the other.
    assert ContentBlock(type="text", text="").text == ""
    assert ContentBlock(type="image_url", image_url={}).image_url == {}


def test_content_block_requires_at_least_one_of_own_payload_but_not_exclusivity():
    # A text block carrying an image_url is legal today; the validator only
    # insists that the payload matching ``type`` is present.
    block = ContentBlock(type="text", text="caption", image_url={"url": "u"})

    assert block.model_dump() == {
        "type": "text",
        "text": "caption",
        "image_url": {"url": "u"},
    }


def test_content_block_rejects_unknown_type_with_literal_error():
    with pytest.raises(ValidationError) as exc_info:
        ContentBlock(type="audio", text="hi")

    errors = _validate_errors(exc_info)
    assert len(errors) == 1
    assert errors[0]["type"] == "literal_error"
    assert errors[0]["loc"] == ("type",)
    assert "Input should be 'text' or 'image_url'" in str(errors[0]["msg"])


def test_content_block_requires_the_type_field():
    with pytest.raises(ValidationError) as exc_info:
        ContentBlock(text="hi")

    errors = _validate_errors(exc_info)
    assert [err["loc"] for err in errors] == [("type",)]
    assert errors[0]["type"] == "missing"


def test_content_block_rejects_extra_keys():
    with pytest.raises(ValidationError) as exc_info:
        ContentBlock(type="text", text="hi", unexpected=1)

    errors = _validate_errors(exc_info)
    assert len(errors) == 1
    assert errors[0]["type"] == "extra_forbidden"
    assert errors[0]["loc"] == ("unexpected",)


def test_content_block_text_must_be_a_string_and_image_url_a_mapping():
    with pytest.raises(ValidationError) as text_exc:
        ContentBlock(type="text", text=123)
    assert _validate_errors(text_exc)[0]["type"] == "string_type"

    with pytest.raises(ValidationError) as url_exc:
        ContentBlock(type="image_url", image_url="https://example.test/a.png")
    assert _validate_errors(url_exc)[0]["type"] == "dict_type"


# ---------------------------------------------------------------------------
# ToolFunction / Tool
# ---------------------------------------------------------------------------


def test_tool_function_only_requires_name_and_leaves_optionals_none():
    fn = ToolFunction(name="lookup")

    assert fn.model_dump() == {
        "name": "lookup",
        "description": None,
        "parameters": None,
        "strict": None,
    }


def test_tool_function_serialises_json_schema_parameters_verbatim():
    fn = ToolFunction(
        name="lookup",
        description="Find a thing",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        strict=False,
    )

    assert fn.model_dump() == {
        "name": "lookup",
        "description": "Find a thing",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        "strict": False,
    }


def test_tool_function_requires_name():
    with pytest.raises(ValidationError) as exc_info:
        ToolFunction()

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ("name",)


def test_tool_function_rejects_extra_keys():
    with pytest.raises(ValidationError) as exc_info:
        ToolFunction(name="lookup", extras=True)

    errors = _validate_errors(exc_info)
    assert [(err["type"], err["loc"]) for err in errors] == [("extra_forbidden", ("extras",))]


def test_tool_accepts_only_the_function_type_and_values_the_function_field():
    tool = Tool(type="function", function={"name": "lookup"})

    assert tool.type == "function"
    assert isinstance(tool.function, ToolFunction)
    assert tool.function.name == "lookup"
    assert tool.model_dump() == {
        "type": "function",
        "function": {"name": "lookup", "description": None, "parameters": None, "strict": None},
    }


def test_tool_rejects_other_type_literal():
    with pytest.raises(ValidationError) as exc_info:
        Tool(type="retrieval", function={"name": "lookup"})

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "literal_error"
    assert errors[0]["loc"] == ("type",)
    assert "Input should be 'function'" in str(errors[0]["msg"])


def test_tool_requires_the_function_field():
    with pytest.raises(ValidationError) as exc_info:
        Tool(type="function")

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ("function",)


# ---------------------------------------------------------------------------
# ChatMessage — content union, extra="forbid", function_call excluded
# ---------------------------------------------------------------------------


def test_chat_message_accepts_a_plain_string_content():
    message = ChatMessage(role="user", content="hello")

    assert message.content == "hello"
    assert message.model_dump() == {
        "role": "user",
        "content": "hello",
        "name": None,
        "tool_calls": None,
        "tool_call_id": None,
    }


def test_chat_message_accepts_a_content_block_list_in_order():
    message = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        ],
    )

    assert [block.type for block in message.content] == ["text", "image_url"]
    assert message.model_dump()["content"] == [
        {"type": "text", "text": "describe this", "image_url": None},
        {"type": "image_url", "text": None, "image_url": {"url": "https://example.test/a.png"}},
    ]


def test_chat_message_content_defaults_to_none_and_accepts_an_empty_list():
    assert ChatMessage(role="assistant").content is None
    assert ChatMessage(role="assistant", content=[]).content == []


def test_chat_message_content_requires_an_str_or_block_list():
    # Neither member of the union matches: an int is not a str and not a
    # list of blocks, so the union reports one error per member.
    with pytest.raises(ValidationError) as exc_info:
        ChatMessage(role="user", content=5)

    errors = _validate_errors(exc_info)
    assert errors[0]["loc"] == ("content", "str")
    assert errors[0]["type"] == "string_type"
    # The list member of the union is named after the ContentBlock schema
    # once its after-validator is attached; pin the shape, not the label.
    assert errors[1]["loc"][0] == "content"
    assert errors[1]["loc"][1].startswith("list[function-after[validate_payload(), ")
    assert errors[1]["loc"][1].endswith("ContentBlock]]")
    assert errors[1]["type"] == "list_type"


def test_chat_message_invalid_block_in_list_surfaces_the_value_error():
    with pytest.raises(ValidationError) as exc_info:
        ChatMessage(role="user", content=[{"type": "text"}])

    messages = [err["msg"] for err in _validate_errors(exc_info)]
    assert any("text content block requires text" in msg for msg in messages)


def test_chat_message_requires_role():
    with pytest.raises(ValidationError) as exc_info:
        ChatMessage()

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ("role",)


def test_chat_message_role_accepts_any_string_not_just_the_openai_roles():
    # role is a bare ``str``; no literal restricts it.
    assert ChatMessage(role="developer").role == "developer"
    assert ChatMessage(role="").role == ""


def test_chat_message_role_must_be_a_string():
    with pytest.raises(ValidationError) as exc_info:
        ChatMessage(role=1)

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "string_type"
    assert errors[0]["loc"] == ("role",)


def test_chat_message_keeps_tool_call_fields_verbatim():
    calls = [{"id": "call_1", "type": "function", "function": {"name": "lookup"}}]
    message = ChatMessage(role="assistant", content=None, tool_calls=calls, tool_call_id="call_1")

    assert message.tool_calls == calls
    # tool_calls are untyped dicts: unknown sub-keys survive.
    assert message.model_dump()["tool_calls"] == calls


def test_chat_message_function_call_is_accepted_but_excluded_from_dump():
    message = ChatMessage(
        role="assistant",
        content=None,
        name="bot",
        function_call={"name": "lookup", "arguments": "{}"},
    )

    # Readable through the attribute...
    assert message.function_call == {"name": "lookup", "arguments": "{}"}
    # ...but Field(exclude=True) keeps it out of both dump forms.
    assert "function_call" not in message.model_dump()
    assert "function_call" not in message.model_dump_json()


def test_chat_message_rejects_extra_keys():
    with pytest.raises(ValidationError) as exc_info:
        ChatMessage(role="user", content="hi", mnemos_inject_memory=True)

    errors = _validate_errors(exc_info)
    assert [(err["type"], err["loc"]) for err in errors] == [
        ("extra_forbidden", ("mnemos_inject_memory",))
    ]


# ---------------------------------------------------------------------------
# ChatCompletionRequest — defaults, coercion, extra="forbid", excluded field
# ---------------------------------------------------------------------------


def test_request_defaults_model_to_auto_and_stream_to_false():
    request = ChatCompletionRequest(messages=[])

    assert request.model == "auto"
    assert request.stream is False
    for optional in (
        "temperature",
        "max_tokens",
        "top_p",
        "tools",
        "tool_choice",
        "response_format",
        "stop",
        "n",
        "presence_penalty",
        "frequency_penalty",
        "user",
    ):
        assert getattr(request, optional) is None, optional


def test_request_explicit_none_model_stays_none_rather_than_the_default():
    # The default only applies when the key is absent.
    assert ChatCompletionRequest(messages=[], model=None).model is None


def test_request_requires_messages():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest()

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ("messages",)


def test_request_accepts_an_empty_message_list():
    request = ChatCompletionRequest(messages=[])

    assert request.messages == []
    assert request.model_dump()["messages"] == []


def test_request_values_dict_messages_into_chat_message_objects():
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}])

    assert len(request.messages) == 1
    assert isinstance(request.messages[0], ChatMessage)
    assert request.messages[0].role == "user"


def test_request_dump_carries_the_neutral_optional_fields_as_null():
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}])

    assert request.model_dump() == {
        "model": "auto",
        "messages": [
            {"role": "user", "content": "hi", "name": None, "tool_calls": None, "tool_call_id": None}
        ],
        "temperature": None,
        "max_tokens": None,
        "top_p": None,
        "stream": False,
        "tools": None,
        "tool_choice": None,
        "response_format": None,
        "stop": None,
        "n": None,
        "presence_penalty": None,
        "frequency_penalty": None,
        "user": None,
    }


def test_request_json_serialises_excluded_field_out():
    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        mnemos_inject_memory=True,
    )

    payload = request.model_dump_json()
    assert "mnemos_inject_memory" not in payload
    assert '"model":"auto"' in payload
    # The attribute is still readable on the instance.
    assert request.mnemos_inject_memory is True


def test_request_mnemos_inject_memory_defaults_to_none():
    assert ChatCompletionRequest(messages=[]).mnemos_inject_memory is None


def test_request_round_trip_through_dump_loses_mnemos_inject_memory():
    # Because the field is Field(exclude=True) it is absent from model_dump(),
    # so re-validating the dump cannot restore it. Pinned current behaviour.
    original = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        mnemos_inject_memory=True,
    )

    restored = ChatCompletionRequest.model_validate(original.model_dump())

    assert restored.mnemos_inject_memory is None
    assert restored.messages[0].content == "hi"


def test_request_rejects_unknown_keys_while_forbidding_extras():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest(messages=[], unknown_key=1)

    errors = _validate_errors(exc_info)
    assert [(err["type"], err["loc"]) for err in errors] == [
        ("extra_forbidden", ("unknown_key",))
    ]


def test_request_coerces_numeric_strings_for_scalar_fields():
    request = ChatCompletionRequest(
        messages=[],
        temperature="0.5",
        max_tokens="10",
        top_p="0.9",
        presence_penalty="0.1",
        frequency_penalty="0.2",
        n="2",
    )

    assert request.temperature == 0.5
    assert request.max_tokens == 10
    assert request.top_p == 0.9
    assert request.presence_penalty == 0.1
    assert request.frequency_penalty == 0.2
    assert request.n == 2


def test_request_max_tokens_rejects_a_fractional_float():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest(messages=[], max_tokens=1.5)

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "int_from_float"
    assert errors[0]["loc"] == ("max_tokens",)


def test_request_accepts_streaming_as_an_integer_flag():
    assert ChatCompletionRequest(messages=[], stream=1).stream is True
    assert ChatCompletionRequest(messages=[], stream=0).stream is False


def test_request_stream_coerces_recognised_boolish_strings():
    # Lax bool parsing accepts the usual spellings, case-insensitively.
    for truthy in ("true", "TRUE", "on", "yes", "1"):
        assert ChatCompletionRequest(messages=[], stream=truthy).stream is True
    for falsy in ("false", "off", "no", "0"):
        assert ChatCompletionRequest(messages=[], stream=falsy).stream is False


def test_request_stream_rejects_an_uninterpretable_string():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest(messages=[], stream="maybe")

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "bool_parsing"
    assert errors[0]["loc"] == ("stream",)


def test_request_stream_rejects_a_non_scalar_value():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest(messages=[], stream=[])

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "bool_type"
    assert errors[0]["loc"] == ("stream",)


def test_request_stream_rejects_the_empty_string_and_floats():
    for value in ("", 2.0):
        with pytest.raises(ValidationError) as exc_info:
            ChatCompletionRequest(messages=[], stream=value)
        assert _validate_errors(exc_info)[0]["type"] == "bool_parsing"


def test_request_n_accepts_zero_and_negative_boundaries():
    # n is an unconstrained Optional[int]; nothing clamps it.
    assert ChatCompletionRequest(messages=[], n=0).n == 0
    assert ChatCompletionRequest(messages=[], n=-1).n == -1


def test_request_stop_accepts_a_single_string_or_a_list_of_strings():
    assert ChatCompletionRequest(messages=[], stop="END").stop == "END"
    assert ChatCompletionRequest(messages=[], stop=["END", "STOP"]).stop == ["END", "STOP"]
    assert ChatCompletionRequest(messages=[], stop=[]).stop == []


def test_request_tool_choice_accepts_a_string_or_a_mapping():
    assert ChatCompletionRequest(messages=[], tool_choice="auto").tool_choice == "auto"
    assert (
        ChatCompletionRequest(messages=[], tool_choice={"type": "function"}).tool_choice
        == {"type": "function"}
    )


def test_request_tools_values_dicts_into_tool_objects():
    request = ChatCompletionRequest(
        messages=[],
        tools=[{"type": "function", "function": {"name": "lookup"}}],
    )

    assert [type(tool) for tool in request.tools] == [Tool]
    assert request.tools[0].function.name == "lookup"


def test_request_tools_rejects_a_tool_entry_without_function():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest(messages=[], tools=[{"type": "function"}])

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ("tools", 0, "function")


def test_request_response_format_is_an_unvalidated_mapping():
    request = ChatCompletionRequest(
        messages=[],
        response_format={"type": "json_schema", "json_schema": {"name": "x"}},
    )

    assert request.response_format == {"type": "json_schema", "json_schema": {"name": "x"}}


def test_request_json_schema_requires_only_messages():
    schema = ChatCompletionRequest.model_json_schema()

    assert schema["required"] == ["messages"]
    assert schema["additionalProperties"] is False


# ---------------------------------------------------------------------------
# ChatCompletionStreamRequest — only the stream default changes
# ---------------------------------------------------------------------------


def test_stream_request_defaults_stream_to_true():
    request = ChatCompletionStreamRequest(messages=[])

    assert request.stream is True
    assert request.model == "auto"


def test_stream_request_is_a_chat_completion_request():
    request = ChatCompletionStreamRequest(messages=[])

    assert isinstance(request, ChatCompletionRequest)
    assert not isinstance(ChatCompletionRequest(messages=[]), ChatCompletionStreamRequest)


def test_stream_request_accepts_an_explicit_false_stream():
    # It is a default override, not a constraint: False is still valid.
    assert ChatCompletionStreamRequest(messages=[], stream=False).stream is False


def test_stream_request_inherits_extra_forbidden():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionStreamRequest(messages=[], unknown_key=1)

    errors = _validate_errors(exc_info)
    assert [(err["type"], err["loc"]) for err in errors] == [
        ("extra_forbidden", ("unknown_key",))
    ]


def test_stream_request_rejects_none_stream_as_bool_type():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionStreamRequest(messages=[], stream=None)

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "bool_type"
    assert errors[0]["loc"] == ("stream",)


# ---------------------------------------------------------------------------
# ChatCompletionResponseMessage — permissive envelope, no excludes
# ---------------------------------------------------------------------------


def test_response_message_dump_includes_all_optional_fields_as_null():
    message = ChatCompletionResponseMessage(role="assistant", content="hi")

    assert message.model_dump() == {
        "role": "assistant",
        "content": "hi",
        "name": None,
        "tool_calls": None,
        "tool_call_id": None,
        "function_call": None,
        "refusal": None,
        "audio": None,
        "annotations": None,
    }


def test_response_message_keeps_function_call_in_the_dump_unlike_chat_message():
    message = ChatCompletionResponseMessage(
        role="assistant",
        function_call={"name": "lookup", "arguments": "{}"},
    )

    assert message.model_dump()["function_call"] == {"name": "lookup", "arguments": "{}"}
    assert "function_call" in message.model_dump_json()


def test_response_message_requires_role():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionResponseMessage()

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ("role",)


def test_response_message_accepts_optional_audio_refusal_and_annotations():
    message = ChatCompletionResponseMessage(
        role="assistant",
        content="",
        refusal="",
        audio={"id": "a1", "data": "..."},
        annotations=[{"type": "url_citation", "url": "https://example.test"}],
    )

    dumped = message.model_dump()
    assert dumped["refusal"] == ""
    assert dumped["audio"] == {"id": "a1", "data": "..."}
    assert dumped["annotations"] == [{"type": "url_citation", "url": "https://example.test"}]


def test_response_message_rejects_extra_keys():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionResponseMessage(role="assistant", bogus=1)

    errors = _validate_errors(exc_info)
    assert [(err["type"], err["loc"]) for err in errors] == [("extra_forbidden", ("bogus",))]


# ---------------------------------------------------------------------------
# ChatCompletionChoice / ChatCompletionResponse
# ---------------------------------------------------------------------------


def _response_message() -> ChatCompletionResponseMessage:
    return ChatCompletionResponseMessage(role="assistant", content="hi")


def test_choice_requires_index_message_and_finish_reason():
    choice = ChatCompletionChoice(index=0, message=_response_message(), finish_reason="stop")

    assert choice.index == 0
    assert choice.finish_reason == "stop"
    assert choice.message.content == "hi"


@pytest.mark.parametrize("missing", ["index", "message", "finish_reason"])
def test_choice_missing_required_field_reports_that_field(missing):
    kwargs = {"index": 0, "message": _response_message(), "finish_reason": "stop"}
    kwargs.pop(missing)

    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionChoice(**kwargs)

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == (missing,)


def test_choice_values_a_dict_message():
    choice = ChatCompletionChoice(
        index=0,
        message={"role": "assistant", "content": "hi"},
        finish_reason="stop",
    )

    assert isinstance(choice.message, ChatCompletionResponseMessage)


def test_choice_accepts_negative_index_and_ignores_unknown_keys():
    # index is an unconstrained int and this envelope does not forbid extras.
    choice = ChatCompletionChoice(
        index=-1,
        message=_response_message(),
        finish_reason="length",
        logprobs={"content": []},
    )

    assert choice.index == -1
    assert "logprobs" not in choice.model_dump()


def test_response_defaults_object_and_requires_usage():
    response = ChatCompletionResponse(
        id="chatcmpl-1",
        created=1_700_000_000,
        model="mnemos-default",
        choices=[],
        usage={},
    )

    assert response.object == "chat.completion"
    assert response.usage == {}


@pytest.mark.parametrize("missing", ["id", "created", "model", "choices", "usage"])
def test_response_missing_required_field_reports_that_field(missing):
    kwargs = {
        "id": "chatcmpl-1",
        "created": 1_700_000_000,
        "model": "mnemos-default",
        "choices": [],
        "usage": {},
    }
    kwargs.pop(missing)

    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionResponse(**kwargs)

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == (missing,)


def test_response_usage_accepts_an_empty_mapping_and_any_int_keys():
    response = ChatCompletionResponse(
        id="chatcmpl-1",
        created=0,
        model="m",
        choices=[],
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "custom": 7},
    )

    assert response.usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "custom": 7,
    }


def test_response_usage_rejects_non_integer_values():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionResponse(
            id="x",
            created=0,
            model="m",
            choices=[],
            usage={"total_tokens": "many"},
        )

    errors = _validate_errors(exc_info)
    assert errors[0]["loc"] == ("usage", "total_tokens")
    assert errors[0]["type"] == "int_parsing"


def test_response_created_accepts_zero():
    response = ChatCompletionResponse(id="x", created=0, model="m", choices=[], usage={})

    assert response.created == 0


def test_response_json_round_trip_preserves_the_nested_choice():
    response = ChatCompletionResponse(
        id="chatcmpl-1",
        created=5,
        model="m",
        choices=[
            ChatCompletionChoice(index=0, message=_response_message(), finish_reason="stop")
        ],
        usage={"total_tokens": 3},
    )

    restored = ChatCompletionResponse.model_validate_json(response.model_dump_json())

    assert restored == response
    assert restored.object == "chat.completion"


# ---------------------------------------------------------------------------
# Streaming envelope: delta / stream choice / stream response
# ---------------------------------------------------------------------------


def test_delta_defaults_every_field_to_none_including_an_empty_delta():
    assert ChatCompletionDelta().model_dump() == {
        "role": None,
        "content": None,
        "tool_calls": None,
    }


def test_delta_content_is_a_plain_string_not_the_block_union():
    # Unlike ChatMessage.content, this is Optional[str]: a block list is
    # rejected outright.
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionDelta(content=[{"type": "text", "text": "hi"}])

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "string_type"
    assert errors[0]["loc"] == ("content",)


def test_delta_keeps_partial_tool_call_fragments_verbatim():
    fragments = [{"index": 0, "id": "call_1", "function": {"arguments": '{"q"'}}]
    delta = ChatCompletionDelta(role="assistant", tool_calls=fragments)

    assert delta.model_dump() == {"role": "assistant", "content": None, "tool_calls": fragments}


def test_delta_ignores_unknown_keys():
    delta = ChatCompletionDelta(content="x", refusal="reason")

    assert "refusal" not in delta.model_dump()


def test_stream_choice_defaults_finish_reason_to_none():
    choice = ChatCompletionStreamChoice(index=0, delta=ChatCompletionDelta(content="Hel"))

    assert choice.finish_reason is None
    assert choice.delta.content == "Hel"


def test_stream_choice_delta_accepts_an_empty_mapping():
    choice = ChatCompletionStreamChoice(index=0, delta={})

    assert choice.delta == ChatCompletionDelta()


def test_stream_choice_requires_index_and_delta():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionStreamChoice()

    errors = _validate_errors(exc_info)
    assert [(err["type"], err["loc"]) for err in errors] == [
        ("missing", ("index",)),
        ("missing", ("delta",)),
    ]


def test_stream_response_defaults_object_to_the_chunk_literal():
    response = ChatCompletionStreamResponse(
        id="chatcmpl-1",
        created=5,
        model="m",
        choices=[ChatCompletionStreamChoice(index=0, delta=ChatCompletionDelta(content="hi"))],
    )

    assert response.object == "chat.completion.chunk"
    assert response.model_dump_json().startswith(
        '{"id":"chatcmpl-1","object":"chat.completion.chunk",'
    )


def test_stream_response_accepts_an_empty_choice_list():
    # The terminating SSE chunk carries no choices.
    response = ChatCompletionStreamResponse(id="x", created=0, model="m", choices=[])

    assert response.choices == []
    # No usage field on the chunk envelope at all.
    assert "usage" not in response.model_dump()


def test_stream_response_ignores_unknown_keys():
    response = ChatCompletionStreamResponse(
        id="x",
        created=0,
        model="m",
        choices=[],
        system_fingerprint="fp_1",
    )

    assert "system_fingerprint" not in response.model_dump()


def test_stream_response_requires_id_created_model_and_choices():
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionStreamResponse()

    errors = _validate_errors(exc_info)
    assert [err["loc"] for err in errors] == [
        ("id",),
        ("created",),
        ("model",),
        ("choices",),
    ]
    assert {err["type"] for err in errors} == {"missing"}


# ---------------------------------------------------------------------------
# ModelInfo / ModelsResponse
# ---------------------------------------------------------------------------


def test_model_info_defaults_object_to_model():
    info = ModelInfo(id="mnemos-default", owned_by="mnemos")

    assert info.object == "model"
    assert info.model_dump() == {"id": "mnemos-default", "object": "model", "owned_by": "mnemos"}


def test_model_info_requires_owned_by():
    with pytest.raises(ValidationError) as exc_info:
        ModelInfo(id="mnemos-default")

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ("owned_by",)


def test_model_info_ignores_unknown_keys():
    info = ModelInfo(id="x", owned_by="mnemos", created=1_700_000_000)

    assert "created" not in info.model_dump()


def test_models_response_defaults_object_to_list():
    response = ModelsResponse(data=[ModelInfo(id="a", owned_by="mnemos")])

    assert response.object == "list"
    assert response.model_dump() == {
        "object": "list",
        "data": [{"id": "a", "object": "model", "owned_by": "mnemos"}],
    }


def test_models_response_accepts_an_empty_model_list():
    response = ModelsResponse(data=[])

    assert response.model_dump() == {"object": "list", "data": []}


def test_models_response_values_dicts_into_model_info_objects():
    response = ModelsResponse(data=[{"id": "a", "owned_by": "mnemos"}])

    assert isinstance(response.data[0], ModelInfo)


def test_models_response_requires_data():
    with pytest.raises(ValidationError) as exc_info:
        ModelsResponse()

    errors = _validate_errors(exc_info)
    assert errors[0]["type"] == "missing"
    assert errors[0]["loc"] == ("data",)


# ---------------------------------------------------------------------------
# Cross-cutting: extra handling differs between request and response models
# ---------------------------------------------------------------------------

_EXTRA_FORBIDDING = [
    ContentBlock,
    ToolFunction,
    Tool,
    ChatMessage,
    ChatCompletionRequest,
    ChatCompletionStreamRequest,
    ChatCompletionResponseMessage,
]

_EXTRA_IGNORING = [
    ChatCompletionChoice,
    ChatCompletionResponse,
    ChatCompletionDelta,
    ChatCompletionStreamChoice,
    ChatCompletionStreamResponse,
    ModelInfo,
    ModelsResponse,
]


@pytest.mark.parametrize("model_cls", _EXTRA_FORBIDDING, ids=lambda c: c.__name__)
def test_request_side_models_configure_extra_forbid(model_cls):
    assert model_cls.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("model_cls", _EXTRA_IGNORING, ids=lambda c: c.__name__)
def test_response_side_models_leave_extra_at_the_pydantic_default(model_cls):
    # The default is "ignore": unknown response keys are dropped, not rejected.
    assert model_cls.model_config.get("extra") is None


def test_every_module_model_is_classified_as_forbidding_or_ignoring_extras():
    classified = set(_EXTRA_FORBIDDING) | set(_EXTRA_IGNORING)
    assert set(_model_classes()) == classified


def test_models_are_mutable_and_assignment_is_not_validated():
    # No frozen=True and no validate_assignment: a bad assignment sticks.
    message = ChatMessage(role="user")
    message.role = "system"
    assert message.role == "system"

    message.role = 5  # type: ignore[assignment]
    assert message.role == 5


def test_models_compare_by_value():
    assert ChatMessage(role="user", content="hi") == ChatMessage(role="user", content="hi")
    assert ChatMessage(role="user", content="hi") != ChatMessage(role="user", content="bye")
