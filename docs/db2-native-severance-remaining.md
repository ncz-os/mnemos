# DB2-native severance — remaining work (handoff)

Status as of 2026-06-13. Branch `wip/studio/2026-06-12-db2-native-slice0-guard`
(10 commits, ARGONAS). **Recommend human review before merge** — keystone
persistence (`mnemos/persistence/db2.py`, `db/migrations_db2/*`, the applier).

MNEMOS: plan `mem_1781315914455_0db361`, Phase-1 summary `mem_1781323498527_6e214e`,
run-2 addendum `mem_1781324471312_c76d39`, schema-fix `mem_1781319557336_2fa07e`.

## Done (autonomous, EAP-validated, codex+ngc gated)
- **Schema bootstrap fixed** — applier rewrite (3-terminator detect, string-aware
  split, Warning=ok, benign set, `--all`) + DDL compat fixes + `model_registry`
  (0002) + idempotent seeds. Clean-slate `--all` = all tables, **0 failures**,
  fully re-runnable. Tool: `scripts/db2_apply_migration.py --all`.
- **Native dialect fall-throughs overridden**: facade (ping/upsert_category_decay/
  open), compression-queue (new repo), audit-chain (8 methods, **cross-dialect
  hash-equivalence + tamper-detection proven**), `create_consultation_with_audit`
  fenced (decision D).
- **Ground-truth gap finder**: `scripts/db2_native_gap_finder.py` (run from repo
  root). Currently **7** flagged — see below; none are remaining *dialect* bugs.
- Gated tests: `tests/test_db2_{native_oracle_gap_baseline,compression_queue_native,
  audit_chain_native}.py` (DB2_DSN-gated).

## Remaining — NEEDS HUMAN / PRODUCT DECISIONS

### 1. The product decision that unblocks everything
**Is the Db2 backend a supported OAuth + GRAEAE-consultations deployment, or
memory-store-only?**
- If **memory-store-only**: fence OAuth/sessions/GRAEAE methods with loud
  `NotImplementedError` (the slice-8 pattern) → gap-finder drops to the 2
  false-positives → **Phase 3 sever becomes unblocked**.
- If **OAuth/GRAEAE supported on Db2**: complete the schemas (below) + reimplement.

### 2. OAuth/sessions (4 OAuth + get_session) — SCHEMA INCOMPLETE, not just dialect
Codex-confirmed: the Db2 OAuth schema lacks columns the callers need; EAP-validation
alone is insufficient — the full caller chain must be traced.
- `oauth_providers` (db2) has `name/client_id/client_secret_hash/auth_url/token_url/
  scopes/enabled`. But `core/oauth.py build_client()` (via `api/routes/oauth.py
  _load_provider`) needs `kind/issuer_url/authorize_url/token_url/userinfo_url/
  client_secret/scope`. → Db2 OAuth **login cannot work** until `oauth_providers`
  gains those columns (or db2 OAuth is fenced).
- `revoke_session`/`revoke_all_sessions`: tractable (db2 `oauth_sessions` keys on
  `id` + `revoked_at`; user via `identity_id -> oauth_identities.user_id`) BUT the
  `oauth_sessions.id` ↔ route `session_id` (cookie) mapping is unverified — trace
  how db2 oauth sessions are created before overriding.
- `get_identity_for_session`: needs PROFILE-json parse (email/display_name) +
  `provider_id -> provider` resolution.
- `get_session` (the `sessions` table, not oauth): db2 `sessions` has no
  `namespace`/`deleted_at` (Oracle method assumes both) and the caller reads
  `last_activity` while the column is `last_active_at`. Reconcile the contract.

### 3. Phase 2 / Phase 3 sever (the GRAEAE two-phase end-state)
- **Phase 2**: prove no Oracle SQL reaches the cursor in native mode (a
  guard-everywhere exerciser over the full overridden surface). The static
  gap-finder gives ground truth today; a runtime exerciser would fence regressions.
- **Phase 3**: reparent `Db2Backend` off `OracleBackend` onto a shared
  `BaseBackend`. **Blocked** until #2 is resolved (OAuth/sessions/GRAEAE must be
  overridden or fenced — they fall through to Oracle today). High blast radius.

### 4. Smaller follow-ups
- GRAEAE `graeae_*` tables port (slice 8 fenced loud; port if Db2 GRAEAE needed).
- Full sealer seal/verify round-trip + crash-mid-seal recovery test (hash-equiv +
  tamper + concurrent-claim are already validated).
- `record_schema_abort` is native-safe by delegation but will need relocation to a
  neutral base in Phase 3.

## Run loop
EAP: `db2eap` on PEGASUS .85, DSN `db2://db2inst1:Db2Fleet%232026pega@127.0.0.1:50000/testdb`,
`MNEMOS_DB2_DIALECT=native`, `~/mnemos/.venv/bin/python`. Always write overrides
against the **real** db2 columns (`syscat.columns` / `grep db/migrations_db2`),
never copy Oracle SQL, and trace the full caller chain before shipping a
schema-contract method.
