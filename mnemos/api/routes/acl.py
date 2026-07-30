"""Per-principal memory ACL management endpoints.

The ``memory_acl`` escape hatch lets an owner (or root, or a delegated
group-admin) share a single memory with a *second* group or a *named
user* beyond the memory's own ``group_id`` + UNIX mode bits. A grant only
ever widens read visibility; the read predicate in
``mnemos.core.visibility`` honors it via an EXISTS disjunct on every
multi-user backend.

Capability-gated: only backends advertising ``ACL_CAPABILITY``
(Postgres, Oracle, Db2) mount a working surface here. The single-user
SQLite laptop tier omits the capability, so every route degrades to 503
rather than pretending to manage rows it cannot enforce.

Authorization (owner OR root OR group-admin of the memory's ``group_id``)
is enforced here, at the route layer — the repository SQL contract is
principal-agnostic by design. Group-membership CRUD (add/remove users to
a group) is a separate, deferred slice.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import (
    maybe_set_pg_rls,
    require_acl_backend,
)
from mnemos.core import lifecycle as _lc
from mnemos.core.security import is_root
from mnemos.core.visibility import ACL_READ_BIT, ACL_WRITE_BIT
from mnemos.persistence.visibility import VisibilityFilter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/memories", tags=["acl"])


async def _invalidate_search_caches_after_acl_change() -> None:
    """Drop the per-user search cache after an ACL grant/revoke.

    Search responses are cached under keys derived from
    user/group/namespace/query state — *not* ACL grant state. A grant
    widens read visibility and a revoke narrows it, so a response cached
    while a principal held a grant would otherwise be replayable after the
    grant is revoked until TTL expiry — a permission-revocation leak. This
    mirrors ``_invalidate_caches_after_mutation`` on the memories route.

    Also bumps the visibility epoch so in-flight search writes land under
    the old epoch (orphaned) rather than leaking stale visibility.
    """
    if not _lc._cache:
        return
    try:
        async for _k in _lc._cache.scan_iter(match="mnemos:search:*", count=500):
            await _lc._cache.delete(_k)
    except Exception:
        pass
    try:
        await _lc._vis_epoch_get_incr()  # bump; errors silently
    except Exception:
        pass


_ALLOWED_PERM_BITS = ACL_READ_BIT | ACL_WRITE_BIT


class AclGrantRequest(BaseModel):
    principal: str = Field(
        ...,
        description="Typed principal: 'user:<id>' or 'group:<id>'.",
    )
    perm: int = Field(
        ...,
        description="Unix-style permission bitmask: read=4, write=2.",
    )


class AclEntry(BaseModel):
    memory_id: str
    principal: str
    perm: int
    granted_by: Optional[str] = None
    created_at: Optional[str] = None


class AclListResponse(BaseModel):
    memory_id: str
    grants: list[AclEntry]


def _validate_principal(principal: str) -> None:
    if ":" not in principal:
        raise HTTPException(
            status_code=422,
            detail="principal must be 'user:<id>' or 'group:<id>'",
        )
    kind, _, ident = principal.partition(":")
    if kind not in ("user", "group") or not ident:
        raise HTTPException(
            status_code=422,
            detail="principal must be 'user:<id>' or 'group:<id>' with a non-empty id",
        )


def _validate_perm(perm: int) -> None:
    # The ACL escape hatch only ever *widens read visibility* — every backend's
    # read predicate honors solely the read bit (4). Write/admin bits are not
    # enforced anywhere, so accepting them would persist a grant that silently
    # does nothing (a write-only grant) or that misleads operators into thinking
    # a write was delegated. Until a write-path predicate exists, reject anything
    # but a bare read grant. ACL_WRITE_BIT is retained for that future slice.
    if perm != ACL_READ_BIT:
        raise HTTPException(
            status_code=422,
            detail="perm must be exactly 4 (read); ACL grants only widen read visibility",
        )


def _iso(value: object) -> Optional[str]:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _to_entry(row: object) -> AclEntry:
    get = row.get  # asyncpg Record and dict both support .get
    return AclEntry(
        memory_id=str(get("memory_id")),
        principal=str(get("principal")),
        perm=int(get("perm")),
        granted_by=(None if get("granted_by") is None else str(get("granted_by"))),
        created_at=_iso(get("created_at")),
    )


async def _load_manageable_memory(backend, tx, memory_id: str, user: UserContext):
    """Fetch the memory and assert the caller may manage its ACL.

    Authorization tiers (any one suffices):
      * root — bypasses the visibility predicate entirely;
      * the memory's owner;
      * a delegated admin (``user_groups.is_admin``) of the memory's
        ``group_id``.

    The memory is loaded under ``VisibilityFilter.for_read`` first, so a
    caller who cannot even *see* the memory gets a 404 — cross-tenant
    existence stays invisible, same contract as ``GET /v1/memories/{id}``.
    A non-owner group-admin therefore can only manage memories that are
    already readable to them (group/world bits or an existing grant);
    an owner-only (mode 700) memory is not manageable by a group-admin,
    which is the fail-closed choice.
    """
    visibility = VisibilityFilter.for_read(
        user,
        namespace=None if is_root(user) else user.namespace,
    )
    row = await backend.memories.get_memory(tx, memory_id, visibility=visibility, include_archived=True)
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")

    if is_root(user):
        return row

    if row.get("owner_id") == user.user_id:
        return row

    group_id = row.get("group_id")
    if (
        group_id
        and group_id in set(user.group_ids)
        and await backend.acl.is_group_admin(tx, user_id=user.user_id, group_id=group_id)
    ):
        return row

    raise HTTPException(
        status_code=403,
        detail="ACL management requires the memory owner, root, or a group admin",
    )


@router.get("/{memory_id}/acl", response_model=AclListResponse)
async def list_memory_acl(
    memory_id: str,
    user: UserContext = Depends(get_current_user),
):
    """List the ACL grants on a memory (management-gated)."""
    backend = require_acl_backend()
    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        await _load_manageable_memory(backend, tx, memory_id, user)
        rows = await backend.acl.list_acl(tx, memory_id)
    return AclListResponse(
        memory_id=memory_id,
        grants=[_to_entry(r) for r in rows],
    )


@router.post("/{memory_id}/acl", response_model=AclEntry)
async def grant_memory_acl(
    memory_id: str,
    request: AclGrantRequest,
    user: UserContext = Depends(get_current_user),
):
    """Grant (or update) a per-principal ACL on a memory."""
    backend = require_acl_backend()
    _validate_principal(request.principal)
    _validate_perm(request.perm)
    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        await _load_manageable_memory(backend, tx, memory_id, user)
        row = await backend.acl.grant_acl(
            tx,
            memory_id=memory_id,
            principal=request.principal,
            perm=request.perm,
            granted_by=user.user_id,
        )
    await _invalidate_search_caches_after_acl_change()
    logger.info(
        "[ACL] %s granted %s perm=%d on memory %s",
        user.user_id,
        request.principal,
        request.perm,
        memory_id,
    )
    return _to_entry(row)


@router.delete("/{memory_id}/acl/{principal}")
async def revoke_memory_acl(
    memory_id: str,
    principal: str,
    user: UserContext = Depends(get_current_user),
):
    """Revoke a per-principal ACL grant on a memory."""
    backend = require_acl_backend()
    _validate_principal(principal)
    async with backend.transactional() as tx:
        await maybe_set_pg_rls(tx, user)
        await _load_manageable_memory(backend, tx, memory_id, user)
        removed = await backend.acl.revoke_acl(tx, memory_id=memory_id, principal=principal)
    if not removed:
        raise HTTPException(status_code=404, detail="ACL grant not found")
    await _invalidate_search_caches_after_acl_change()
    logger.info("[ACL] %s revoked %s on memory %s", user.user_id, principal, memory_id)
    return {"status": "revoked", "memory_id": memory_id, "principal": principal}
