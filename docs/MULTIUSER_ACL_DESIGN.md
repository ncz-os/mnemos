# Multiuser ACL + delegated group-admin

Status: implemented (PostgreSQL, Oracle, Db2); SQLite read-honors-only.
Slice: per-principal `memory_acl` escape hatch + `user_groups.is_admin` tier.

## Problem

The base multiuser model scopes a memory to a single `group_id` plus UNIX
mode bits (`permission_mode`). Two real needs fall outside that model:

1. Share *one* memory with a *second* group, or with a *named user*, without
   moving it or loosening its mode bits for everyone.
2. Let a group owner delegate ACL administration to a trusted member without
   making them root.

## Design

### `memory_acl` — a read-widening escape hatch

A per-memory grant table. A grant **only ever widens read visibility**; it
never grants write/admin and never narrows.

- `principal` — typed string `user:<id>` or `group:<id>`.
- `perm` — UNIX-style bitmask. **Only the read bit (4) is accepted today.**
  The route rejects anything but `perm == 4`: no backend read predicate honors
  the write bit, so accepting write/admin bits would persist a grant that
  silently does nothing or misleads operators. `ACL_WRITE_BIT` is retained as a
  constant for a future write-delegation slice.
- Read predicates honor a grant via an `EXISTS` disjunct, added to the
  visibility OR-group on every multi-user backend. The disjunct is **pinned to
  the caller's namespace**: a grant widens read only *within* the caller's
  namespace, never across tenants.

### Capability-gated management surface

Grant/revoke/list ACL is an ABC sub-repo (`AclRepository`) advertised via the
`acl` capability (`ACL_CAPABILITY`). PostgreSQL, Oracle, and Db2 advertise it;
the single-user SQLite laptop tier does **not** — its management routes degrade
to 503 rather than pretend to manage rows it cannot enforce. SQLite's read
predicate still honors pre-existing `memory_acl` rows (so a workgroup DB
migrated down to SQLite keeps its grants readable).

### Delegated group-admin authorization

Management is gated at the **route layer** (`api/routes/acl.py`), since the
repository SQL contract is principal-agnostic by design. Any one of:

- root (bypasses the visibility predicate entirely);
- the memory's `owner_id`;
- a delegated admin (`user_groups.is_admin = TRUE`) of the memory's `group_id`.

The memory is loaded under `VisibilityFilter.for_read` first, so a caller who
cannot even *see* the memory gets a 404 — cross-tenant existence stays
invisible. A non-owner group-admin can therefore only manage memories already
readable to them; an owner-only (mode 700) memory is not manageable by a
group-admin (the fail-closed choice).

Full add/remove-user-to-group CRUD is **deferred** — only the `is_admin`
column + the authz predicate land in this slice.

### Read-widening scope: live memories only, not version history

A grant widens reads of the **live memory** on every read surface that shares
the central `read_visibility_predicate` — the main `GET`/search path and the
DAG read preflight (`_assert_memory_readable`), which this slice updated to use
that shared predicate so an ACL-granted reader is no longer 404'd at the DAG
layer.

It does **not** widen reads of **per-version snapshot history**
(`/log` rows, `/commits/{hash}`, `/versions`). Snapshot tenancy is evaluated by
`version_visibility_predicate` + the `_snap_visible` post-walk filter, which are
**owner-or-world-only by design and predate this slice**: `memory_versions`
does not carry `group_id`/`federation_source`/principal columns, so snapshot
reads fail closed against group, federation, *and* ACL grants alike. A
group-reader already sees an empty `/log` for the same reason; an ACL-reader now
behaves identically (parity, not a regression). No snapshot content leaks — the
reader gets an empty list / 404, never an unauthorized row, and can already read
the *live* content via the grant. Widening snapshot history to honor
group/ACL grants requires backfilling those columns onto `memory_versions` (a
schema migration the codebase explicitly defers) and is tracked as a follow-up
— see ncz-os/mnemos#2.

## Cross-backend notes

- **Postgres** keeps native RLS as defense-in-depth; upsert via `ON CONFLICT`
  (race-safe); schema column is `created_at`.
- **Oracle / Db2** schemas name the column `created` (not `created_at`); every
  SELECT aliases `created AS created_at` so the principal-agnostic
  application layer is uniform. Upsert via `MERGE`, wrapped in a
  dialect-aware `_is_unique_violation` retry-once to honor the "repeat grant
  never raises duplicate-key" contract under a concurrent first-grant race.
- **SQLite** schema column is `created_at`; no management API.

## Cache coherency

ACL grant/revoke mutate read visibility, so both invalidate the per-user
search cache (`mnemos:search:*`) after commit — at parity with every other
visibility-narrowing mutation (memory delete, permission-mode change, archive,
admin ops, deletion worker). All of these share a one-shot post-commit
invalidation that has a known write-after-invalidate race bounded by the search
TTL; closing it uniformly (visibility-epoch-versioned cache keys) is tracked as
a separate, systemic follow-up — see ncz-os/mnemos#1.

## Tests

`tests/test_acl_routes.py` covers principal/perm validation, the
owner/root/group-admin gate (incl. 403/404 fail-closed paths), capability
503-gating, handler round-trips, search-cache invalidation on grant/revoke
(and the 404 no-op), plus an end-to-end SQLite proof that a `memory_acl` row
widens read visibility within the namespace. Backend-specific SQL is exercised
by the live per-backend suites.
