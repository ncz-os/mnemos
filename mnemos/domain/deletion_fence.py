"""The CF-09 GDPR active-deletion write fence.

Refuse to write a row onto a scope that is currently being deleted.

Without this fence, a memory added during the 30-day grace window (after the
soft-delete sweep has marked the scope, before the hard-delete phase runs)
would survive past ``hard_deleted``: the hard-delete only removes rows already
carrying ``deleted_at IS NOT NULL``, so a row inserted during grace still has
``deleted_at IS NULL`` and is skipped forever, even though the audit log
claims the user was hard-deleted.

Why this lives in ``mnemos.domain`` and not in ``mnemos.api``
--------------------------------------------------------------
The fence is not route policy — it is a storage-integrity rule that every
write path shares, including ones below the API layer (the MPF/CHARON
importers in ``mnemos.domain.portability`` and ``mnemos.portability``). Those
modules may not import ``mnemos.api`` under the layered import-linter
contract in ``pyproject.toml``, and putting the rule there would force the
importers to either duplicate it (the drift CF-09 is about) or break the
layering. ``mnemos.domain`` sits below both, so it is the one home that every
caller can reach without an upward import. ``mnemos.api.memory_mutations``
re-exports this object under its historical private name, so route imports and
the tests that patch it are unchanged.

Root is the exception: operators may need to inject tombstone rows even
mid-deletion. The bypass is the CALLER's decision — this helper fences
whatever scope it is handed.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException


def _active_deletion_detail(active: dict) -> str:
    detail = (
        "This scope is currently subject to an active deletion request; "
        "new writes are rejected until the grace window expires. "
    )
    restore_by = active.get("restore_by")
    if restore_by is not None:
        detail += f"Restore deadline: {restore_by}."
    return detail


async def assert_no_active_deletion(
    backend,
    *,
    owner_id: str,
    namespace: str | None,
) -> None:
    """Refuse (409) a write onto a (owner_id, namespace) scope mid-deletion."""
    from mnemos.persistence.worker_lifecycle import active_deletion_for_scope

    try:
        active = await active_deletion_for_scope(backend, target_user_id=owner_id, target_namespace=namespace)
    except Exception:
        # If the fence itself errors (e.g. backend doesn't expose
        # transactional), fall through. The hard-delete resweep+verify loop in
        # deletion_request_worker.py is the second line of defence and refuses
        # to mark the request complete on live rows.
        return
    if active is not None:
        raise HTTPException(status_code=409, detail=_active_deletion_detail(active))


async def assert_no_active_deletion_for_scopes(
    backend,
    scopes: Iterable[tuple[str, str | None]],
    *,
    skip: Iterable[tuple[str, str | None]] = (),
) -> None:
    """Probe every DISTINCT target scope of a multi-row write, once each.

    Bulk/import paths write many rows in one request, potentially across
    several (owner_id, namespace) pairs (``preserve_owner`` restores). Probing
    per item makes a 1000-row batch 1000 round trips, so the pairs are
    de-duplicated first: one probe per scope, and the whole request is refused
    with the same 409 the single create returns.

    ``skip`` names scopes the caller is allowed to write to anyway — the root
    tombstone-injection bypass. Sorted before probing so the refusal is
    deterministic when several scopes are mid-deletion.
    """
    blocked = set(skip)
    for owner_id, namespace in sorted(set(scopes) - blocked, key=lambda s: (s[0], s[1] or "")):
        await assert_no_active_deletion(backend, owner_id=owner_id, namespace=namespace)
