"""Lossless JSON minification with numeric lexeme preservation.

The standard ``json.loads`` / ``json.dumps`` minification pattern is not safe
for HEADROOM's financial use case: a float reparse can silently mutate digits,
trim lexical zeros, or normalize scientific notation.  This module instead
validates JSON with Python's parser configured to retain numeric tokens as
strings, then performs a lexical whitespace pass that copies every non-space
JSON token byte-for-byte.  Number lexemes are never converted to ``float``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

_JSON_WS: Final = frozenset(" \t\r\n")
_NUMBER_RE: Final = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


class JSONMinifyError(ValueError):
    """Raised when input is not a complete valid JSON document."""


@dataclass(frozen=True)
class _LexicalJSON:
    minified: str
    number_tokens: tuple[str, ...]


def _validate_json(text: str) -> None:
    """Validate JSON without materializing numbers as floats.

    ``parse_int`` and ``parse_float`` return the exact source token string for
    each JSON number.  ``object_pairs_hook`` preserves duplicate object keys
    while validating, avoiding accidental key collapse during validation.
    """

    decoder = json.JSONDecoder(
        object_pairs_hook=list,
        parse_int=str,
        parse_float=str,
        parse_constant=lambda token: (_ for _ in ()).throw(JSONMinifyError(f"invalid JSON constant {token!r}")),
    )
    # raw_decode does not skip leading whitespace; advance past it so documents
    # with leading JSON whitespace (e.g. "  {...}  ") validate and can minify.
    start = 0
    n = len(text)
    while start < n and text[start] in _JSON_WS:
        start += 1
    if start >= n:
        raise JSONMinifyError("empty JSON document")
    try:
        _, end = decoder.raw_decode(text, start)
    except JSONMinifyError:
        raise
    except json.JSONDecodeError as exc:
        raise JSONMinifyError(str(exc)) from exc
    if any(ch not in _JSON_WS for ch in text[end:]):
        raise JSONMinifyError("extra data after complete JSON document")


def _lexical_minify(text: str) -> _LexicalJSON:
    out: list[str] = []
    numbers: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if ch in _JSON_WS:
            i += 1
            continue

        if ch == '"':
            start = i
            i += 1
            escaped = False
            while i < n:
                c = text[i]
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    i += 1
                    break
                i += 1
            out.append(text[start:i])
            continue

        if ch == "-" or ch.isdigit():
            match = _NUMBER_RE.match(text, i)
            if match is not None:
                token = match.group(0)
                numbers.append(token)
                out.append(token)
                i = match.end()
                continue

        out.append(ch)
        i += 1

    return _LexicalJSON("".join(out), tuple(numbers))


def minify_json_text(text: str) -> str:
    """Return a JSON document with insignificant whitespace removed.

    The transform is lossless for valid JSON documents: strings and numeric
    lexemes are copied exactly, including big integers, high-precision decimals,
    exponent spelling, plus/minus exponent signs, and trailing fractional zeros.
    Unsupported/invalid JSON raises :class:`JSONMinifyError`; callers that need
    no-corruption passthrough should use ``mnemos.domain.headroom.compress``.
    """

    if not isinstance(text, str):
        raise TypeError("minify_json_text expects str")
    _validate_json(text)
    return _lexical_minify(text).minified


def is_json_lossless_equivalent(original: str, compressed: str) -> bool:
    """Return true iff ``compressed`` is a safe minification of ``original``.

    This proof check validates both documents, confirms the compressed document
    is the lexical minifier's exact output, and verifies the ordered numeric
    token stream did not change.  The numeric-token assertion is deliberately
    stricter than ordinary JSON semantic equality because HEADROOM must not
    mutate financial/accounting digits.
    """

    try:
        original_lexed = _lexical_minify(original)
        compressed_lexed = _lexical_minify(compressed)
        _validate_json(original)
        _validate_json(compressed)
    except (JSONMinifyError, TypeError):
        return False
    return (
        compressed == original_lexed.minified
        and compressed == compressed_lexed.minified
        and original_lexed.number_tokens == compressed_lexed.number_tokens
    )
