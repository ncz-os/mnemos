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
from mnemos.api.persistence_helpers import backend_or_503 as _backend_or_503
from mnemos.audit import derive_writer_keypair, load_root_keypair
from mnemos.audit.route_helper import memory_id_to_audit_bytes
from mnemos.workers.audit_sealer import audit_chain_enabled

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/audit", tags=["audit"])


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
    backend = _backend_or_503()
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


def _to_iso(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
