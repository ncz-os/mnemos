from __future__ import annotations

import json
import random
import re
from decimal import Decimal

import pytest

from mnemos.domain.headroom import compress, is_json_lossless_equivalent, minify_json_text
from mnemos.domain.headroom.json_minify import JSONMinifyError

_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_JSON_WS = frozenset(" \t\r\n")


def _number_tokens(text: str) -> list[str]:
    """Extract JSON number lexemes while ignoring quoted strings."""

    tokens: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in _JSON_WS:
            i += 1
            continue
        if ch == '"':
            i += 1
            escaped = False
            while i < len(text):
                c = text[i]
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "-" or ch.isdigit():
            match = _NUMBER_RE.match(text, i)
            if match is not None:
                tokens.append(match.group(0))
                i = match.end()
                continue
        i += 1
    return tokens


def _spaced_json_for_number(number: str) -> str:
    return f'{{\n  "value" : {number},\n  "array" : [ {number}, {{ "again" : {number} }} ],\n  "text" : "{number}"\n}}'


@pytest.mark.parametrize(
    "number",
    [
        "0",
        "-0",
        "1234567890123456789012345678901234567890",
        "-9876543210987654321098765432109876543210",
        "0.0000",
        "-0.00000000000000000000",
        "0.0000000000000000000000000000000000000001",
        "1234567890.1234567890123456789012345678900000",
        "1.2300000000000000000000000000000000000000",
        "1e0",
        "1E+000",
        "-9.990000000000000000000000000000e-123",
        "6.022140760000000000000000000000E+23",
    ],
)
def test_json_minify_preserves_edge_numeric_lexemes_exactly(number: str) -> None:
    original = _spaced_json_for_number(number)
    compressed = minify_json_text(original)

    assert " " not in compressed
    assert "\n" not in compressed
    assert _number_tokens(compressed) == [number, number, number]
    assert is_json_lossless_equivalent(original, compressed)

    # Decimal construction proves tests never need float reparsing for high precision.
    Decimal(number)


def _random_number(rng: random.Random) -> str:
    sign = "-" if rng.choice([False, True]) else ""
    kind = rng.choice(["int", "decimal", "scientific"])
    if rng.randrange(5) == 0:
        int_part = "0"
    else:
        int_part = str(rng.randrange(1, 10)) + "".join(
            str(rng.randrange(10)) for _ in range(rng.randrange(0, 50))
        )
    if kind == "int":
        return sign + int_part

    frac = "".join(str(rng.randrange(10)) for _ in range(rng.randrange(1, 60)))
    if rng.randrange(3) == 0:
        frac += "0" * rng.randrange(1, 20)
    value = f"{sign}{int_part}.{frac}"
    if kind == "decimal":
        return value

    exp_marker = rng.choice(["e", "E"])
    exp_sign = rng.choice(["", "+", "-"])
    exp_digits = "".join(str(rng.randrange(10)) for _ in range(rng.randrange(1, 8)))
    if set(exp_digits) == {"0"}:
        exp_digits = "0"
    return f"{value}{exp_marker}{exp_sign}{exp_digits}"


def test_random_numeric_property_exact_round_trip_for_json_minify() -> None:
    rng = random.Random(0x5EADF00D)
    for _ in range(500):
        numbers = [_random_number(rng) for _ in range(rng.randrange(1, 12))]
        original = "{\n  " + ",\n  ".join(f'"n{i}" : {number}' for i, number in enumerate(numbers)) + "\n}"
        compressed = minify_json_text(original)

        assert is_json_lossless_equivalent(original, compressed)
        assert _number_tokens(compressed) == numbers
        assert json.loads(original, parse_int=str, parse_float=str) == json.loads(
            compressed,
            parse_int=str,
            parse_float=str,
        )


def test_json_minify_preserves_duplicate_keys_and_string_whitespace() -> None:
    original = '{\n  "a" : 1,\n  "a" : 2,\n  "s" : " keep spaces and \\n escapes "\n}'

    assert minify_json_text(original) == '{"a":1,"a":2,"s":" keep spaces and \\n escapes "}'


def test_json_minify_rejects_invalid_or_non_complete_json() -> None:
    with pytest.raises(JSONMinifyError):
        minify_json_text('{"a": 1} trailing')

    with pytest.raises(JSONMinifyError):
        minify_json_text('{"a": 01}')


def test_compress_text_json_and_unsupported_passthrough() -> None:
    result = compress('{\n  "a" : 1.2300,\n  "b" : [ true, null ]\n}')

    assert result.supported is True
    assert result.changed is True
    assert result.lossless is True
    assert result.compressed == '{"a":1.2300,"b":[true,null]}'
    assert result.bytes_saved > 0

    unsupported = compress("plain prompt with no complete JSON document")
    assert unsupported.supported is False
    assert unsupported.changed is False
    assert unsupported.lossless is True
    assert unsupported.compressed == unsupported.original


def test_compress_messages_recursively_minifies_json_string_leaves_only() -> None:
    messages = [
        {"role": "user", "content": "not json"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "quote",
                        "arguments": '{\n  "amount" : 1234567890.000000000000000001,\n  "exp" : 1E+000\n}',
                    }
                }
            ],
        },
    ]

    result = compress(messages)

    assert result.supported is True
    assert result.changed is True
    assert result.lossless is True
    assert messages[1]["tool_calls"][0]["function"]["arguments"].startswith("{\n")
    assert result.compressed[0]["content"] == "not json"
    assert result.compressed[1]["tool_calls"][0]["function"]["arguments"] == (
        '{"amount":1234567890.000000000000000001,"exp":1E+000}'
    )
