"""Optional native federation feed serialization helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

try:
    import mnemos_native_search as _NATIVE_FEDERATION  # type: ignore[import-not-found]
except ImportError:
    _NATIVE_FEDERATION = None

NATIVE_AVAILABLE = _NATIVE_FEDERATION is not None


def _iso_value(value: Any) -> Any:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return value


def _first_present(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    raise KeyError(keys[0])


def _first_optional(row: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return default


def _feed_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "id": _first_present(row, "id"),
        "content": _first_present(row, "content"),
        "category": _first_present(row, "category"),
        "tags": row["tags"] if row.get("tags") is not None else [],
    }
    if row.get("embedding") is not None:
        payload["embedding"] = row["embedding"]
    payload.update(
        {
            "refs": _first_optional(row, "refs", "source_memory_ids", "memory_refs", default=[]),
            "created_at": _iso_value(_first_present(row, "created_at", "created")),
            "updated_at": _iso_value(_first_present(row, "updated_at", "updated")),
        }
    )
    return payload


def pure_python_serialize_memory_for_feed(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return [json.dumps(_feed_payload(row), separators=(",", ":"), ensure_ascii=False) for row in rows]


def serialize_memory_for_feed(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if _NATIVE_FEDERATION is not None:
        try:
            return list(_NATIVE_FEDERATION.serialize_memory_for_feed(rows))
        except Exception:
            pass
    return pure_python_serialize_memory_for_feed(rows)
