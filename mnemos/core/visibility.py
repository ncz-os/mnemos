"""Shared read-visibility predicate for non-root callers.

Mirrors the active PostgreSQL RLS read policies (see
db/migrations_v1_multiuser.sql and follow-up policy migrations) at
the app layer. The same predicate must be applied across every read
surface — list, get, search, rehydrate, gateway context — because
PostgreSQL combines RLS with the handler's WHERE via AND. RLS cannot
re-add rows the handler has already excluded; if one read path uses a
narrower predicate than another, that path silently hides rows the
rest of the contract admits.

Mutation paths (update, delete) deliberately do NOT use this
predicate — writes stay strictly owner-scoped so a non-owner can't
edit a world/group-readable row they happen to be able to read.
"""

from __future__ import annotations

from typing import List, NoReturn, Tuple

from fastapi import HTTPException

# Unix-style permission bits reused by the per-principal ACL escape hatch
# (memory_acl.perm). read=4, write=2 — execute/1 is unused for memories.
ACL_READ_BIT = 4
ACL_WRITE_BIT = 2


def acl_principals(user_id: str, group_ids: List[str]) -> list[str]:
    """Typed principal set for a caller: 'user:<id>' plus 'group:<id>' each.

    This is the set matched against ``memory_acl.principal`` in the read
    predicate's EXISTS disjunct. Empty ids are dropped so an
    unauthenticated caller (user_id == "") can never match a malformed
    ``'user:'`` grant — grant validation forbids creating one, and this
    is the matching-side backstop.
    """
    principals: list[str] = []
    if user_id:
        principals.append(f"user:{user_id}")
    principals.extend(f"group:{g}" for g in group_ids if g)
    return principals


def handle_trigger_pgerror(exc: Exception) -> NoReturn:
    """Translate trigger-raised Postgres errors into API conflicts."""
    if getattr(exc, "sqlstate", None) == "MN001":
        raise HTTPException(
            status_code=409,
            detail=(
                "Memory branch state is inconsistent: "
                "the branch row is missing, has a NULL "
                "head_version_id, or points to a version from "
                "another memory. Reconcile memory_branches and "
                "memory_versions for this memory before retrying."
            ),
        ) from exc
    raise exc


def read_visibility_predicate(
    user_id: str,
    group_ids: List[str],
    start_param_idx: int,
    table_alias: str = "",
) -> Tuple[str, list]:
    """Build the read-visibility WHERE clause + its params.

    Returns ``(clause, params)`` where ``clause`` is a parenthesized
    SQL fragment using $-placeholders starting at
    ``start_param_idx``, and ``params`` is the list of values to
    extend the caller's params list with (in the order the
    placeholders appear).

    Branches mirror the active RLS read policies:

    - ``mnemos_owner_select``  → ``owner_id = $caller``
    - ``federation`` (v3.2 H1) → ``federation_source IS NOT NULL``
    - ``mnemos_world_select``  → ``(permission_mode % 10) >= 4``
      (extract Unix-style world bits via ones-digit)
    - ``mnemos_group_select``  → ``((permission_mode / 10) % 10) >= 4
                                   AND group_id IS NOT NULL
                                   AND group_id = ANY($groups)``
      (extract Unix-style group bits via tens-digit; permission_mode
      = 700 has group bits = 0, so the row is owner-only even though
      the owner bit is readable).
    - ``mnemos_acl_select``    → ``EXISTS (SELECT 1 FROM memory_acl …)``
      the per-principal escape hatch: a row is also readable if it has
      an ACL grant to any of the caller's principals
      (``user:<id>``/``group:<id>``) carrying the read bit. This only
      ever widens visibility on top of the mode bits.

    ``group_ids`` is sourced from ``UserContext.group_ids`` (resolved
    at auth time) rather than re-querying ``user_groups`` via EXISTS;
    same authoritative source the RLS policy uses, just pre-resolved.

    ``table_alias`` is prepended to every column reference (e.g.
    ``"m"`` → ``m.owner_id``) for queries that join multiple tables
    and need disambiguation. Default empty produces unqualified
    column names suitable for single-table queries.

    """
    n = start_param_idx
    p = f"{table_alias}." if table_alias else ""
    clause = (
        "("
        f"{p}owner_id=${n}"
        f" OR {p}federation_source IS NOT NULL"
        f" OR ({p}permission_mode % 10) >= 4"
        f" OR ((({p}permission_mode / 10) % 10) >= 4 "
        f"AND {p}group_id IS NOT NULL "
        f"AND {p}group_id = ANY(${n + 1}::text[]))"
        f" OR EXISTS (SELECT 1 FROM memory_acl macl "
        f"WHERE macl.memory_id = {p}id "
        f"AND macl.principal = ANY(${n + 2}::text[]) "
        f"AND (macl.perm & {ACL_READ_BIT}) <> 0)"
        ")"
    )
    return clause, [user_id, list(group_ids), acl_principals(user_id, group_ids)]


def version_visibility_predicate(
    user_id: str,
    group_ids: List[str],
    start_param_idx: int,
    table_alias: str = "",
) -> Tuple[str, list]:
    """Per-snapshot visibility predicate for ``memory_versions`` rows.

    Snapshot tenancy is evaluated against THE SNAPSHOT's own
    ``owner_id`` / ``namespace`` / ``permission_mode`` columns, NOT
    the live memory's. This closes a class of bug Codex flagged
    where a memory created private (mode 600), snapshotted into v1,
    later relaxed to public (mode 644) lets every reader of v2+ also
    fetch the v1 private snapshot via ``list_versions`` /
    ``get_version`` / ``diff_versions``.

    Mirrors ``read_visibility_predicate`` for the per-snapshot read
    set so snapshot reads honor the SAME widening the live-memory
    surface already does (PR for GitLab #2 — ncz-os/mnemos#2,
    aligned with the feat/multiuser-acl-group-admin merge that
    widened live-memory reads). Snapshots inherit:

    - owner via ``owner_id = $caller``
    - world via ``(permission_mode % 10) >= 4``
    - group via ``((permission_mode / 10) % 10) >= 4 AND
                 group_id IS NOT NULL AND group_id = ANY($groups)``
    - per-principal ACL via ``EXISTS (SELECT 1 FROM memory_acl ...)``

    The federation-source disjunct is deliberately absent: a
    federated-pulled live memory whose author has never written
    locally does not have local snapshots for the federation row
    (snapshots are produced by the writer's own trigger), so
    widening to ``federation_source IS NOT NULL`` on snapshots
    would only return ``deleted_at IS NULL`` rows that survive as
    historical artifacts, which is a leak/expansion we don't want
    here. Federation read access is enforced UP STREAM at the
    live-memory gate (``read_visibility_predicate``) and snapshots
    are only visible via ``/log`` etc. when the caller has live
    access; once that gate passes, owner/world/group/ACL widening
    is the right next layer.

    The namespace pin (a separate ``namespace = $`` predicate) is
    expected to be added by the caller alongside this clause.

    Schema prerequisites (see migration 0048_memory_versions_acl.sql):
    ``memory_versions`` must carry ``group_id`` plus an
    ``memory_acl`` join key. The pre-#2 snapshot table did NOT
    carry ``group_id``; backfill is required for the group branch
    to fire correctly for snapshots taken before the migration.
    ACL rows are keyed on ``memory_id`` so they compose with any
    snapshot table shape — no schema change required for the ACL
    branch beyond snapshot rows inheriting the live memory's id
    (which they already do).

    ``group_ids`` is sourced from ``UserContext.group_ids`` (resolved
    at auth time) rather than re-querying ``user_groups`` via EXISTS;
    same authoritative source the RLS policy uses, just pre-resolved.

    ``table_alias`` is prepended to every column reference (e.g.
    ``"mv"`` → ``mv.owner_id``) for queries that join multiple tables
    and need disambiguation. Default empty produces unqualified
    column names suitable for single-table queries.
    """
    n = start_param_idx
    p = f"{table_alias}." if table_alias else ""
    clause = (
        "("
        f"{p}owner_id=${n}"
        f" OR ({p}permission_mode % 10) >= 4"
        f" OR ((({p}permission_mode / 10) % 10) >= 4 "
        f"AND {p}group_id IS NOT NULL "
        f"AND {p}group_id = ANY(${n + 1}::text[]))"
        f" OR EXISTS (SELECT 1 FROM memory_acl macl "
        f"WHERE macl.memory_id = {p}memory_id "
        f"AND macl.principal = ANY(${n + 2}::text[]) "
        f"AND (macl.perm & {ACL_READ_BIT}) <> 0)"
        ")"
    )
    return clause, [user_id, list(group_ids), acl_principals(user_id, group_ids)]


def version_visibility_owner_world_predicate(
    user_id: str,
    start_param_idx: int,
    table_alias: str = "",
) -> Tuple[str, list]:
    """Backwards-compatible narrow snapshot predicate (owner + world only).

    Retained for tests and code paths that deliberately want the
    pre-#2 narrow behavior. New code should call
    ``version_visibility_predicate`` (above) instead so the same
    widening the live-memory surface already ships also applies
    to per-version reads.
    """
    n = start_param_idx
    p = f"{table_alias}." if table_alias else ""
    clause = (
        "("
        f"{p}owner_id=${n}"
        f" OR ({p}permission_mode % 10) >= 4"
        ")"
    )
    return clause, [user_id]


async def _assert_target_head_visible(
    conn,
    head_version_id: str,
    user,
    not_found_detail: str,
) -> None:
    """Fail closed when a write target HEAD is invisible to the caller.

    DAG write paths copy tenancy from the target branch HEAD into the new
    commit. Non-root callers must therefore be able to read that target
    snapshot directly before it can define the tenancy of a new version.
    Callers are expected to run this after locking the branch row that yielded
    ``head_version_id``.
    """
    if getattr(user, "role", None) == "root":
        return

    vis_clause, vis_params = version_visibility_predicate(
        user.user_id,
        list(getattr(user, "group_ids", []) or []),
        start_param_idx=2,
    )
    ns_ph = f"${len(vis_params) + 2}"
    row = await conn.fetchrow(
        f"SELECT 1 FROM memory_versions "
        f"WHERE id = $1 "
        f"AND deleted_at IS NULL AND {vis_clause} AND namespace = {ns_ph}",
        head_version_id, *vis_params, user.namespace,
    )
    if not row:
        raise HTTPException(status_code=404, detail=not_found_detail)
