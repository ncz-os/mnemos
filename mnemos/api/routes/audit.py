"""v6.2 M-2.2.1 audit-chain HTTP endpoints.

Public surface:

* ``GET /v1/audit/pubkey`` -> per-instance root Ed25519 public key
  (base64), plus optional ``writer_id`` lookup for a per-writer key.
* ``GET /v1/audit/proof?memory_id_str=...`` -> chain head for one
  memory (latest entry_id + global_root + global_seq if sealed).
  Full Merkle-inclusion-proof is a follow-up; this endpoint returns
  enough metadata for replicas to verify chain head + look up the
  containing window.

Both endpoints require authentication via the standard bearer-token
dependency. The pubkey endpoint exposes only public-key material —
no signing key leakage.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import AUDIT_CAPABILITY, backend_or_503 as _backend_or_503
from mnemos.api.persistence_helpers import _capability_503, maybe_set_pg_rls
from mnemos.audit import derive_writer_keypair, load_root_keypair
from mnemos.audit.crypto import merkle_leaf, merkle_proof, merkle_root
from mnemos.audit.route_helper import memory_id_to_audit_bytes
from mnemos.audit.verify import verify_memory_audit_chain
from mnemos.core.config import get_settings
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from mnemos.workers.audit_sealer import audit_chain_enabled

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/audit", tags=["audit"])


def _require_audit_backend():
    backend = _backend_or_503()
    capabilities = getattr(backend, "capabilities", None)
    if capabilities is not None and AUDIT_CAPABILITY not in set(capabilities):
        raise _capability_503(AUDIT_CAPABILITY, backend)
    if not hasattr(backend, "audit_chain"):
        raise _capability_503(AUDIT_CAPABILITY, backend)
    return backend


@router.get("/pubkey")
async def audit_pubkey(
    writer_id: Optional[str] = Query(
        None,
        description=(
            "When set, returns the per-writer Ed25519 public key derived "
            "from session_secret + writer_id (HKDF-SHA256). Use this to "
            "verify a specific user's audit signatures."
        ),
    ),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Return audit pubkey material (root + optionally per-writer)."""
    if not audit_chain_enabled():
        raise HTTPException(
            status_code=503,
            detail="audit chain disabled (MNEMOS_AUDIT_CHAIN not 'on')",
        )
    try:
        _, root_pub = load_root_keypair()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"audit root key not loaded: {exc}",
        )

    payload: dict = {
        "root_pubkey": base64.b64encode(root_pub).decode("ascii"),
        "algorithm": "Ed25519",
    }

    if writer_id:
        from mnemos.core.config import get_settings

        settings = get_settings()
        session_secret = (getattr(settings.server, "session_secret", "") or "").encode("utf-8")
        if not session_secret:
            raise HTTPException(
                status_code=503,
                detail="session_secret unavailable; cannot derive writer key",
            )
        try:
            _, writer_pub = derive_writer_keypair(session_secret, writer_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        payload["writer_id"] = writer_id
        payload["writer_pubkey"] = base64.b64encode(writer_pub).decode("ascii")

    return payload


@router.get("/proof")
async def audit_proof_head(
    memory_id_str: str = Query(
        ...,
        description="Memory ID (string form, e.g. 'mem_1779637500000_abc123')",
    ),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Return chain head + sealed-window metadata for one memory.

    Full Merkle inclusion proof over a sealed window's leaves is a
    follow-up; this endpoint returns the chain head sufficient for
    replicas to look up the containing root in ``memory_audit_roots``
    and re-derive the inclusion path from their own copy of the chain.
    """
    if not audit_chain_enabled():
        raise HTTPException(
            status_code=503,
            detail="audit chain disabled (MNEMOS_AUDIT_CHAIN not 'on')",
        )
    backend = _require_audit_backend()
    if backend.audit_chain is None:
        raise HTTPException(
            status_code=503,
            detail="backend has no audit_chain repository",
        )

    try:
        memory_id_bytes = memory_id_to_audit_bytes(memory_id_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    async with backend.transactional() as tx:
        row = await backend.audit_chain.get_latest_audit_entry(tx, memory_id_bytes)

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no audit entries for memory_id={memory_id_str}",
        )

    out: dict = {
        "memory_id": memory_id_str,
        "memory_id_audit_key": memory_id_bytes.hex(),
        "entry_id": row["entry_id"].hex(),
        "op": row["op"],
        "writer_id": row["writer_id"],
        "writer_pubkey": base64.b64encode(row["writer_pubkey"]).decode("ascii"),
        "payload_hash": row["payload_hash"].hex(),
        "signature": base64.b64encode(row["signature"]).decode("ascii"),
        "signed_at": _to_iso(row["signed_at"]),
        "sealed": row.get("global_root") is not None,
    }
    if row.get("global_root") is not None:
        out["global_root"] = row["global_root"].hex()
        out["global_seq"] = row.get("global_seq")
    if row.get("prev_entry_id") is not None:
        out["prev_entry_id"] = row["prev_entry_id"].hex()
    if row.get("prev_entry_hash") is not None:
        out["prev_entry_hash"] = row["prev_entry_hash"].hex()
    return out


@router.get("/inclusion_proof")
async def audit_inclusion_proof(
    entry_id: str = Query(
        ...,
        description="Hex-encoded entry_id whose inclusion proof to return",
    ),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Return Merkle inclusion proof for one audit entry.

    Caller passes ``entry_id`` (hex, 32 chars = 16 bytes). Endpoint:
    1. Looks up the entry in ``memory_audit_chain``.
    2. Errors 422 if entry is not yet sealed (global_root IS NULL).
    3. Fetches ALL entries in the same sealed window
       (WHERE global_root = X, ORDER BY signed_at, entry_id) -- the
       SAME order the sealer used to build the leaves.
    4. Computes the sibling-hash path from leaf to root via
       `mnemos.audit.crypto.merkle_proof`.

    Verifier uses `mnemos.audit.crypto.verify_inclusion(leaf, proof, root)`
    -- walks the sibling list pairwise SHA-256-hashing, returns True
    iff the reconstructed root matches the published ``global_root``.
    """
    if not audit_chain_enabled():
        raise HTTPException(
            status_code=503,
            detail="audit chain disabled (MNEMOS_AUDIT_CHAIN not 'on')",
        )
    backend = _require_audit_backend()
    if backend.audit_chain is None:
        raise HTTPException(
            status_code=503,
            detail="backend has no audit_chain repository",
        )

    try:
        entry_id_bytes = bytes.fromhex(entry_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"entry_id must be hex: {exc}",
        )
    if len(entry_id_bytes) != 16:
        raise HTTPException(
            status_code=400,
            detail=f"entry_id must be 32 hex chars (got {len(entry_id_bytes)} bytes)",
        )

    async with backend.transactional() as tx:
        # v6.2 protocol method (cleanup from inline SQL bridge).
        target_row = await backend.audit_chain.get_audit_entry_by_id(tx, entry_id_bytes)
        if target_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"audit entry {entry_id} not found",
            )
        if target_row.get("global_root") is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"audit entry {entry_id} is not yet sealed "
                    "(sealer hasn't claimed its window). Retry after "
                    "the next seal cadence."
                ),
            )

        global_root_bytes = target_row["global_root"]
        window_rows = await backend.audit_chain.list_window_entries(tx, global_root_bytes)

    if not window_rows:
        # Should never happen — target said it was sealed under this
        # root, but the window is empty. Defensive guard for caller
        # debugging.
        raise HTTPException(
            status_code=500,
            detail="audit window is empty despite sealed target; chain inconsistency",
        )

    # Build leaves in the same order the sealer used.
    leaves = [merkle_leaf(r["entry_id"], r["signature"]) for r in window_rows]
    target_idx: int | None = None
    for i, r in enumerate(window_rows):
        if r["entry_id"] == entry_id_bytes:
            target_idx = i
            break
    if target_idx is None:
        raise HTTPException(
            status_code=500,
            detail=("target entry not found in its own window; chain " "inconsistency between get + list"),
        )

    proof = merkle_proof(leaves, target_idx)

    # Self-check: reconstructed root must match the published one.
    # Fail loud if not — server-side bug, not a client error.
    computed = merkle_root(leaves)
    if computed != global_root_bytes:
        raise HTTPException(
            status_code=500,
            detail=("computed Merkle root does not match stored global_root; " "sealer/proof routine drift"),
        )

    return {
        "entry_id": entry_id,
        "leaf_hash": leaves[target_idx].hex(),
        "leaf_index": target_idx,
        "window_size": len(window_rows),
        "global_root": global_root_bytes.hex(),
        "global_seq": target_row.get("global_seq"),
        "proof": [{"sibling": sib.hex(), "position": pos} for sib, pos in proof],
    }


@router.get("/verify")
async def audit_verify_memory(
    memory_id_str: str = Query(
        ...,
        description="Memory ID (string form, e.g. 'mem_1779637500000_abc123')",
    ),
    include_current: bool = Query(
        True,
        description="Also compare the latest signed payload hash with the current readable memory row.",
    ),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Verify one memory's Ed25519 + hash-linked audit chain.

    This endpoint is the operator-facing tamper-evidence proof: it
    retrieves the per-memory chain through ``backend.audit_chain`` and
    verifies signatures, prev-entry hash links, and (by default) that the
    chain head still commits to the current readable memory row. A silent
    modification to either the audit table or memory row turns ``valid``
    false with machine-readable ``issues``.
    """
    if not audit_chain_enabled():
        raise HTTPException(
            status_code=503,
            detail="audit chain disabled (MNEMOS_AUDIT_CHAIN not 'on')",
        )
    backend = _require_audit_backend()
    if backend.audit_chain is None:
        raise HTTPException(
            status_code=503,
            detail="backend has no audit_chain repository",
        )

    try:
        memory_id_bytes = memory_id_to_audit_bytes(memory_id_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    current_memory = None
    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        rows = await backend.audit_chain.list_memory_entries(tx, memory_id_bytes)
        if include_current:
            if user.role == "root":
                visibility = VisibilityFilter(
                    scope=VisibilityScope.ROOT_BYPASS,
                    user_id=None,
                    group_ids=(),
                    namespace=user.namespace,
                )
            else:
                visibility = VisibilityFilter.for_read(user, namespace=user.namespace)
            current_memory = await backend.memories.get_memory(
                tx,
                memory_id_str,
                visibility=visibility,
                include_archived=True,
            )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"no audit entries for memory_id={memory_id_str}",
        )

    settings = get_settings()
    session_secret = (getattr(settings.server, "session_secret", "") or "").encode("utf-8") or None
    result = verify_memory_audit_chain(
        rows,
        current_memory=current_memory,
        session_secret=session_secret,
    )
    result.update(
        {
            "memory_id": memory_id_str,
            "memory_id_audit_key": memory_id_bytes.hex(),
            "current_memory_checked": current_memory is not None,
        }
    )
    if include_current and current_memory is None and result.get("head_op") != "delete":
        result["issues"].append(
            {
                "code": "current_memory_unavailable",
                "detail": "memory row is absent or not readable; signature/link verification still ran",
            }
        )
        result["valid"] = False
    return result


@router.get("/health")
async def audit_health(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Per-backend audit-chain health snapshot.

    Returns:
    * `chain_enabled`: ``MNEMOS_AUDIT_CHAIN`` flag state.
    * `backend_has_audit_chain`: backend.audit_chain attribute non-None.
    * `total_entries`, `unsealed_count`, `oldest_unsealed_signed_at`,
      `sealed_root_count`, `last_sealed_at`: live counts from the
      chain tables. Useful for operator dashboards + alerts on
      unsealed-growth ("sealer is wedged").

    Returns 503 if backend has no audit_chain repo. Returns the
    snapshot with `chain_enabled=False` if env is off but tables
    still exist (lets operators inspect a disabled chain without
    re-enabling).
    """
    backend = _require_audit_backend()
    if backend.audit_chain is None:
        raise HTTPException(
            status_code=503,
            detail="backend has no audit_chain repository",
        )
    out: dict = {
        "chain_enabled": audit_chain_enabled(),
        "backend_has_audit_chain": True,
    }
    try:
        async with backend.transactional() as tx:
            stats = await backend.audit_chain.get_chain_stats(tx)
        out.update(stats)
    except Exception as exc:
        logger.exception("[audit/health] get_chain_stats failed")
        raise HTTPException(
            status_code=503,
            detail=f"audit_chain stats fetch failed: {exc}",
        )

    # Lag computation: time since oldest_unsealed_signed_at.
    # Useful for alerting "sealer stuck for >5 min".
    oldest = out.get("oldest_unsealed_signed_at")
    if oldest:
        try:
            from datetime import datetime, timezone

            o = datetime.fromisoformat(str(oldest).replace("Z", "+00:00"))
            if o.tzinfo is None:
                o = o.replace(tzinfo=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            out["oldest_unsealed_age_seconds"] = max(0.0, (now - o).total_seconds())
        except Exception:
            out["oldest_unsealed_age_seconds"] = None
    else:
        out["oldest_unsealed_age_seconds"] = None
    return out


def _to_iso(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
