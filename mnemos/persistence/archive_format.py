"""Archive payload format shared by the Persephone runner and persistence.

These constants and helpers describe the on-disk archive record: how a memory
row is flattened, JSON-encoded and zstd-compressed, and the schema version that
stamps it.

They live in the persistence layer rather than in ``mnemos.domain.persephone``
because both layers need them and ``mnemos.persistence`` is forbidden from
importing ``mnemos.domain`` (import-linter contract "persistence has no upward
deps"). The record format is a storage concern, so this is its correct home;
the domain runner re-exports it for existing callers.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import zstandard as zstd

ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_CONTENT_PREFIX = "ARCHIVED:"
DEFAULT_ARCHIVED_BY = "system:persephone"


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _coerce_json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _coerce_json_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_coerce_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_coerce_json_value(item) for item in value]
    return value


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _archive_payload(row: Any) -> dict[str, Any]:
    metadata = _row_get(row, "metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {"_raw": metadata}
    elif metadata is None:
        metadata = {}

    fields = {
        "id": _row_get(row, "id"),
        "content": _row_get(row, "content"),
        "category": _row_get(row, "category"),
        "subcategory": _row_get(row, "subcategory"),
        "metadata": metadata,
        "quality_rating": _row_get(row, "quality_rating"),
        "verbatim_content": _row_get(row, "verbatim_content"),
        "owner_id": _row_get(row, "owner_id"),
        "group_id": _row_get(row, "group_id"),
        "namespace": _row_get(row, "namespace"),
        "permission_mode": _row_get(row, "permission_mode"),
        "source_model": _row_get(row, "source_model"),
        "source_provider": _row_get(row, "source_provider"),
        "source_session": _row_get(row, "source_session"),
        "source_agent": _row_get(row, "source_agent"),
        "source_memories": _row_get(row, "source_memories"),
        "provenance": _row_get(row, "provenance"),
        "morpheus_run_id": _row_get(row, "morpheus_run_id"),
        "consolidated_into": _row_get(row, "consolidated_into"),
        "triples_extracted_at": _row_get(row, "triples_extracted_at"),
        "recall_count": _row_get(row, "recall_count"),
        "last_recalled_at": _row_get(row, "last_recalled_at"),
        "created": _row_get(row, "created"),
        "updated": _row_get(row, "updated"),
    }
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "memory": _coerce_json_value(fields),
    }


def _compress_payload(payload: dict[str, Any]) -> tuple[bytes, int]:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    compressed = zstd.ZstdCompressor().compress(raw)
    return compressed, len(raw)


def _decompress_payload(compressed: bytes) -> dict[str, Any]:
    raw = zstd.ZstdDecompressor().decompress(bytes(compressed))
    payload = json.loads(raw.decode("utf-8"))
    if int(payload.get("schema_version") or 0) != ARCHIVE_SCHEMA_VERSION:
        raise ValueError("unsupported memory archive schema_version")
    memory = payload.get("memory")
    if not isinstance(memory, dict) or "content" not in memory:
        raise ValueError("invalid memory archive payload")
    return payload
