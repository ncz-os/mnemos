# Doctor-role security audit (2026-06-02)

Requested in the MNEMOS adversarial-review handoff: audit the "doctor role" — permissions, whether it can read private memory, mutate/repair/backfill, or bypass tenant isolation.

## Finding: there is NO MNEMOS "doctor" role

MNEMOS user roles are **`root`**, **`operator`**, **`user`**. Visibility/tenant bypass is gated **only on `root`**:
- `mnemos/core/security.py:43` — `return user.role == "root"`
- `mnemos/core/visibility.py:147` — `if getattr(user, "role", None) == "root"`
- `mnemos/core/observability.py:386` — root-only
- `mnemos/persistence/visibility.py` — `for_read` requires a namespace for non-root; non-root callers cannot cross namespaces.

There is **no `doctor` role** anywhere in the role checks. So the handoff's concern — "doctor reads private memory / bypasses tenant isolation" — **does not apply**: no such role exists to over-privilege.

## What "doctor" actually means in this codebase

1. **`mnemos doctor` CLI** (`mnemos/cli/main.py:915` → `mnemos/runtime/hardware.py::cli_doctor`) — a local hardware/health diagnostic. No memory or DB-row access; no network role.
2. **`doctor:codex-fix` hive job-kind** (`mnemos/domain/knemon/router.py:81`) — a scarce-path job type, not a user role.
3. **Triage doctor service** (`/srv/agent-bus/zeroclaw_doctor.py`, `zeroclaw-doctor.service` on the primary) — claims `triage:*` jobs from the **hive bus (:5005)**, builds context from the **failed job's description + result JSON** (`zeroclaw_doctor.py:828` "Build searchable text from job description + failed-job results"), invokes codex-cli to decide an action (release / codex-fix / cancel), and patches job status.

## Triage doctor's data access — NOT MNEMOS memory

The triage doctor reads **hive-bus job data**, never MNEMOS memories. It does not query `/v1/memories`, does not render a `VisibilityFilter`, and does not touch per-tenant memory ACLs. Its only MNEMOS interaction is **best-effort commit-DAG node writes** (audit trail of which job produced which commit) via `MNEMOS_TOKEN`. So it **cannot leak private memory or cross tenants** — it operates in a different system (hive coordination), not the memory store.

## Recommendation (the one real hardening)

The triage doctor authenticates to MNEMOS with `MNEMOS_TOKEN` for its commit-DAG writes. If that token is **root-privileged** (the shared master token), the doctor *could* read any private memory *if its code were changed to do so* — latent over-privilege, not an active leak. **Least-privilege:** issue the doctor an `operator`- or write-scoped token limited to its own commit-DAG/audit namespace, not the root master token. Then even a future code change can't read tenant memory.

## Tests
Allowed/denied coverage for the memory roles already exists (`tests/test_memories_permission_mode.py`, visibility/namespace enforcement). No "doctor"-role test is needed because the role does not exist; the meaningful test is the least-privilege-token assertion once a scoped token is issued.
