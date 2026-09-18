"""Helpers for posting foreign MPF records to MNEMOS's memory-only importer."""

from __future__ import annotations

from typing import Any, Dict


def new_import_totals() -> Dict[str, Any]:
    """Create aggregate counters for one or more /v1/import responses."""
    return {
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "sidecars_imported": {},
        "sidecars_failed": {},
        "unsupported_kinds": {},
    }


def accumulate_import_response(totals: Dict[str, Any], body: Dict[str, Any]) -> None:
    """Accumulate record and mapping-valued sidecar result counters."""
    for key in ("imported", "skipped", "failed"):
        totals[key] += int(body.get(key, 0))
    for key in ("sidecars_imported", "sidecars_failed", "unsupported_kinds"):
        target = totals[key]
        for kind, count in (body.get(key) or {}).items():
            target[kind] = target.get(kind, 0) + int(count)


def import_totals_failed(totals: Dict[str, Any]) -> bool:
    """Return whether the server reported any incomplete import work."""
    return bool(
        totals.get("failed", 0)
        or sum((totals.get("sidecars_failed") or {}).values())
        or sum((totals.get("unsupported_kinds") or {}).values())
    )


def normalize_record_for_mnemos(record: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a non-memory MPF record to a searchable MNEMOS memory.

    Adapter file output retains the foreign MPF kind. Only the direct-migration
    POST path uses this conversion because ``/v1/import`` persists memories and
    deliberately skips other record kinds.
    """
    if record.get("kind") == "memory":
        return record

    normalized = dict(record)
    payload = dict(record.get("payload") or {})
    metadata = dict(payload.get("metadata") or {})
    mpf_metadata = dict(metadata.get("mpf") or {})
    mpf_metadata.update(
        original_kind=record.get("kind"),
        original_payload_version=record.get("payload_version"),
    )
    metadata["mpf"] = mpf_metadata
    payload["metadata"] = metadata

    if not payload.get("content"):
        payload["content"] = (
            payload.get("statement")
            or payload.get("name")
            or " ".join(
                str(payload.get(key)) for key in ("subject", "predicate", "object") if payload.get(key) is not None
            )
        )

    normalized["kind"] = "memory"
    normalized["payload_version"] = "mnemos-3.1"
    normalized["payload"] = payload
    return normalized
