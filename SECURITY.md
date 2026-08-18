# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 6.1.x | Yes — current release line |
| 6.0.x | Security fixes only; upgrade to 6.1 |
| Earlier | No |

Only the most recent release line receives fixes. If you are on 6.0 or
6.0.1, upgrade to 6.1 before reporting an issue so the report is against
supported code.

## Current security invariants

These hold in 6.1:

- Memory read visibility is symmetric across list/get/search/rehydrate,
  OpenAI-compatible gateway context, version history, DAG history, and MCP
  version tools. The live-memory predicate is centralized in
  `read_visibility_predicate` (`mnemos/core/visibility.py`).
- Version history is gated per snapshot by `version_visibility_predicate`
  (`mnemos/core/visibility.py`), so a later-public memory does not expose
  an earlier private snapshot.
- DAG logs stay within one memory and do not bridge across invisible
  snapshots. `parent_hash` is emitted only when the immediate parent is
  visible to the caller.
- Branch creation is race-safe: HTTP and MCP paths lock the parent memory
  row, resolve the start snapshot inside the transaction, and insert with
  `ON CONFLICT DO NOTHING RETURNING`.
- `mnemos/db_migrations/migrations_v3_5_trigger_same_memory_parent.sql`
  rejects missing, NULL, or cross-memory branch heads with SQLSTATE
  `MN001`; the API maps that condition to HTTP 409 with branch
  reconciliation guidance.
- `mnemos/db_migrations/migrations_v3_5_rls_group_select_unix_bits.sql`
  keeps the `mnemos_group_select` RLS policy and the application
  `read_visibility_predicate` on the same Unix group-read bit expression,
  `((permission_mode / 10) % 10) >= 4`.
- Consultation audit metadata is owner-scoped for non-root callers:
  `/v1/consultations/audit` returns only the caller's consultation audit
  rows, and `/v1/consultations/audit/verify` verifies only that caller's
  rows. Root keeps the global operational audit view.
- Webhook delivery uses persisted leases, retry-chain convergence, terminal
  success guards, and SSRF checks at subscription and delivery time.
- MCP stdio and HTTP/SSE use the same registry under `mnemos/mcp/tools/`, with
  per-user HTTP token mapping available through `MNEMOS_MCP_TOKENS`.
- Circuit-breaker, rate-limit, and concurrency state is process-local by
  default (`memory://`), which is correct for the single-worker edge and dev
  profiles. Multi-worker or multi-node deployments should install the
  `redis` extra and set `RATE_LIMIT_STORAGE_URI` to a shared Redis, because
  a per-process counter multiplies the effective ceiling by the number of
  processes. Startup logs a warning when more than one worker is configured
  against process-local state.
- Runtime configuration is centralized in the Pydantic Settings singleton;
  direct `os.environ` reads are limited to `mnemos/core/config.py` and the
  installer path.
- The OpenAI-compatible gateway passes supported generation controls through
  to providers and rejects unsupported tool, response-format, or multimodal
  requests instead of silently ignoring them.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability.

Report it as a **confidential issue** on the canonical GitLab project:
<https://gitlab.com/ncz-os/mnemos/-/issues/new> — tick **"This issue is
confidential"** before you submit. Confidential issues are visible only to
project maintainers.

Please include:

- a description of the issue
- impact assessment
- reproduction steps
- the MNEMOS version, backend, and deployment shape (container or pip,
  single- or multi-worker)
- any suggested remediation

Expect an acknowledgement within a week. There is no bug bounty.

## Secrets policy

- Never commit `.env` files or live credentials.
- Store provider keys outside the repository.
- Keep infrastructure-specific detail — hostnames, private addresses,
  internal topology — out of the repository entirely, including docs.
