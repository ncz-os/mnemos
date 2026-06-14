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


def _redact_field(value: Any) -> Any:
    if value is None:
        return None
    from mnemos.core.secret_detection import redact_content

    return redact_content(str(value))


def _is_vault_row(row: Mapping[str, Any]) -> bool:
    from mnemos.core.secret_detection import VAULT_NAMESPACE

    return str(row.get("namespace") or "") == VAULT_NAMESPACE


def _redacted_row(row: Mapping[str, Any]) -> dict[str, Any]:
    # Native serializers receive raw DB rows and bypass the Pydantic
    # _memory_item_from_row redaction path.  Apply the same federation
    # retrieval policy here before either the Rust extension or pure-Python
    # serializer can emit bytes: vault rows never leave the node, and every
    # content-bearing field is span-redacted for credentials.
    out = dict(row)
    if out.get("type") == "consolidation":
        return out
    for key in ("content", "compressed_content", "verbatim_content"):
        if key in out:
            out[key] = _redact_field(out.get(key))
    return out


def _redacted_non_vault_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_redacted_row(row) for row in rows if not _is_vault_row(row)]


def _memory_item_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    archived_at = _first_optional(row, "archived_at")
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        metadata = json.loads(metadata) if metadata else None
    elif metadata is not None:
        metadata = dict(metadata)
    return {
        "id": row["id"],
        "content": row["content"],
        "category": row["category"],
        "subcategory": row.get("subcategory"),
        "created": _iso_value(_first_present(row, "created", "created_at")) or "",
        "updated": _iso_value(_first_optional(row, "updated", "updated_at")),
        "metadata": metadata,
        "quality_rating": row.get("quality_rating"),
        "compressed_content": row.get("compressed_content"),
        "verbatim_content": row.get("verbatim_content"),
        "source": "openclaw",
        "owner_id": row.get("owner_id"),
        "group_id": row.get("group_id"),
        "namespace": row.get("namespace"),
        "permission_mode": row.get("permission_mode"),
        "source_model": row.get("source_model"),
        "source_provider": row.get("source_provider"),
        "source_session": row.get("source_session"),
        "source_agent": row.get("source_agent"),
        "archived_at": _iso_value(archived_at),
        "archived": archived_at is not None,
        "embedding": row.get("embedding"),
        "embedding_model": row.get("embedding_model"),
        "embedding_dim": row.get("embedding_dim"),
        "audit_latest_entry_id": row.get("audit_latest_entry_id"),
        "audit_latest_entry_hash": row.get("audit_latest_entry_hash"),
    }


def _wire_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    if row.get("type") == "consolidation":
        return {
            "type": "consolidation",
            "id": row["id"],
            "consolidated_into": row["consolidated_into"],
            "consolidated_at": _iso_value(row["consolidated_at"]) or "",
        }
    return _memory_item_payload(row)


def pure_python_serialize_memory_for_feed(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return [json.dumps(_feed_payload(row), separators=(",", ":"), ensure_ascii=False) for row in rows]


def serialize_memory_for_feed(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if _NATIVE_FEDERATION is not None:
        try:
            return list(_NATIVE_FEDERATION.serialize_memory_for_feed(rows))
        except Exception:
            pass
    return pure_python_serialize_memory_for_feed(rows)


def pure_python_serialize_memory_rows(rows: Sequence[Mapping[str, Any]]) -> bytes:
    dict_rows = _redacted_non_vault_rows(rows)
    return json.dumps(
        [_wire_payload(row) for row in dict_rows],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def serialize_memory_rows(rows: Sequence[Mapping[str, Any]]) -> bytes:
    dict_rows = _redacted_non_vault_rows(rows)
    if _NATIVE_FEDERATION is not None:
        try:
            return bytes(_NATIVE_FEDERATION.serialize_memory_rows(dict_rows))
        except Exception:
            pass
    return json.dumps(
        [_wire_payload(row) for row in dict_rows],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def serialize_feed_response(rows: Sequence[Mapping[str, Any]], *, next_cursor: str | None, has_more: bool) -> bytes:
    return (
        b'{"memories":'
        + serialize_memory_rows(rows)
        + b',"next_cursor":'
        + json.dumps(next_cursor, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        + b',"has_more":'
        + (b"true" if has_more else b"false")
        + b"}"
    )
