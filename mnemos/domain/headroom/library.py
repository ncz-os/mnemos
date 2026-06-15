"""Public HEADROOM compression API.

Supported transforms are intentionally conservative and provably lossless.
Unsupported content passes through unchanged with metadata explaining that no
transform was applied; the library must never corrupt prompts or tool args.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, overload

from .code_strip import strip_fenced_code_lossless
from .json_minify import JSONMinifyError, is_json_lossless_equivalent, minify_json_text

TransformName = Literal["json-minify", "code-strip-noop"]


@dataclass(frozen=True)
class TransformRecord:
    """Metadata for one attempted HEADROOM transform."""

    name: TransformName
    supported: bool
    changed: bool
    path: str = "$"
    reason: str | None = None
    original_bytes: int = 0
    compressed_bytes: int = 0


@dataclass(frozen=True)
class CompressionResult:
    """Result returned by :func:`compress`.

    ``supported`` means at least one supported transform was applied somewhere
    in the input.  ``changed`` means the returned payload differs from the
    original.  ``lossless`` is false only if an internal proof check fails; in
    that case this library returns the original content as a passthrough.
    """

    original: Any
    compressed: Any
    supported: bool
    changed: bool
    lossless: bool
    transforms: tuple[TransformRecord, ...] = field(default_factory=tuple)

    @property
    def bytes_saved(self) -> int:
        return sum(max(0, item.original_bytes - item.compressed_bytes) for item in self.transforms if item.changed)

    @property
    def compression_ratio(self) -> float:
        changed = [item for item in self.transforms if item.changed and item.original_bytes > 0]
        if not changed:
            return 1.0
        original_bytes = sum(item.original_bytes for item in changed)
        compressed_bytes = sum(item.compressed_bytes for item in changed)
        return compressed_bytes / original_bytes


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _compress_json_string(text: str, *, path: str) -> tuple[str, TransformRecord]:
    try:
        compressed = minify_json_text(text)
    except JSONMinifyError as exc:
        return text, TransformRecord(
            name="json-minify",
            supported=False,
            changed=False,
            path=path,
            reason=f"not valid JSON: {exc}",
            original_bytes=_byte_len(text),
            compressed_bytes=_byte_len(text),
        )

    proof_ok = is_json_lossless_equivalent(text, compressed)
    if not proof_ok:
        return text, TransformRecord(
            name="json-minify",
            supported=False,
            changed=False,
            path=path,
            reason="lossless proof failed; returned original passthrough",
            original_bytes=_byte_len(text),
            compressed_bytes=_byte_len(text),
        )

    return compressed, TransformRecord(
        name="json-minify",
        supported=True,
        changed=compressed != text,
        path=path,
        reason=None if compressed != text else "already minified JSON",
        original_bytes=_byte_len(text),
        compressed_bytes=_byte_len(compressed),
    )


def compress_text(text: str) -> CompressionResult:
    """Compress one text payload if it is a supported lossless format.

    Current primary support: complete JSON documents / JSON tool-call argument
    strings.  Fenced-code AST stripping is recorded as a safe no-op for future
    expansion; it is not attempted because this phase requires no corruption.
    """

    compressed, json_record = _compress_json_string(text, path="$")
    if json_record.supported:
        return CompressionResult(
            original=text,
            compressed=compressed,
            supported=True,
            changed=json_record.changed,
            lossless=True,
            transforms=(json_record,),
        )

    code_text, code_changed = strip_fenced_code_lossless(text)
    code_record = TransformRecord(
        name="code-strip-noop",
        supported=False,
        changed=code_changed,
        path="$",
        reason="AST fenced-code stripping deferred; passthrough to avoid corruption",
        original_bytes=_byte_len(text),
        compressed_bytes=_byte_len(code_text),
    )
    return CompressionResult(
        original=text,
        compressed=code_text,
        supported=False,
        changed=False,
        lossless=True,
        transforms=(json_record, code_record),
    )


def _compress_strings(value: Any, *, path: str) -> tuple[Any, list[TransformRecord], bool, bool, bool]:
    if isinstance(value, str):
        result = compress_text(value)
        transforms = [
            TransformRecord(
                name=record.name,
                supported=record.supported,
                changed=record.changed,
                path=path,
                reason=record.reason,
                original_bytes=record.original_bytes,
                compressed_bytes=record.compressed_bytes,
            )
            for record in result.transforms
        ]
        return result.compressed, transforms, result.supported, result.changed, result.lossless

    if isinstance(value, list):
        output: list[Any] = []
        transforms: list[TransformRecord] = []
        supported = changed = False
        lossless = True
        for index, item in enumerate(value):
            child, child_records, child_supported, child_changed, child_lossless = _compress_strings(
                item, path=f"{path}[{index}]"
            )
            output.append(child)
            transforms.extend(child_records)
            supported = supported or child_supported
            changed = changed or child_changed
            lossless = lossless and child_lossless
        return output, transforms, supported, changed, lossless

    if isinstance(value, dict):
        output: dict[Any, Any] = {}
        transforms: list[TransformRecord] = []
        supported = changed = False
        lossless = True
        for key, item in value.items():
            child_path = f"{path}.{key}" if isinstance(key, str) and key.isidentifier() else f"{path}[{key!r}]"
            child, child_records, child_supported, child_changed, child_lossless = _compress_strings(
                item, path=child_path
            )
            output[key] = child
            transforms.extend(child_records)
            supported = supported or child_supported
            changed = changed or child_changed
            lossless = lossless and child_lossless
        return output, transforms, supported, changed, lossless

    return value, [], False, False, True


def compress_messages(messages: list[dict[str, Any]]) -> CompressionResult:
    """Compress JSON strings inside an OpenAI-style messages list.

    The message structure is deep-copied and only string leaves that are valid
    complete JSON documents are minified.  Already-parsed Python numbers are
    never serialized/reparsed here, avoiding float digit mutation.
    """

    original = deepcopy(messages)
    compressed, transforms, supported, changed, lossless = _compress_strings(original, path="$")
    return CompressionResult(
        original=messages,
        compressed=compressed,
        supported=supported,
        changed=changed,
        lossless=lossless,
        transforms=tuple(transforms),
    )


@overload
def compress(payload: str) -> CompressionResult: ...


@overload
def compress(payload: list[dict[str, Any]]) -> CompressionResult: ...


def compress(payload: str | list[dict[str, Any]]) -> CompressionResult:
    """Compress supported prompt/message content with no-corruption passthrough.

    ``str`` payloads are treated as a single candidate JSON/tool-argument text.
    ``list[dict]`` payloads are treated as chat messages and recursively scan
    string leaves.  Unsupported payload types raise ``TypeError`` so callers do
    not mistakenly assume a transform ran.
    """

    if isinstance(payload, str):
        return compress_text(payload)
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return compress_messages(payload)
    raise TypeError("HEADROOM compress supports str or list[dict] message payloads")
