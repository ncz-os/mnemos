# Runbook — multiuser/ACL deploy to fleet (Oracle) + NVIDIA/Spark

**Status:** READY — held at the live-apply gate (operator OK required before Stage B on PYTHIA).
**Target code:** `ncz-os/mnemos` master `a6210bd` (95-commit multiuser/ACL/oauth delta over the deployed `66804e6` / `6.0.0rc1`).
**Reviewed:** multi-model adversarial (gpt-5.5 + codex via NGC proxy), findings verified; migration idempotency/dedup fixed (`0043` Oracle ORA-955 guards + `add_col`; `0043` DB2 bare-create+`@`+42711; dup `0041`→`0044`).
**Backup (pre-taken):** `~/backups/oracle-premulti/mnemos_pre_multiuser_20260608-012218.dmp` (1.2GB schema expdp, PYTHIA `pythia-oracle` ORCLPDB1).

## Why the whole chain is safe to re-apply
Every delta migration is now idempotent (Oracle: `IF NOT EXISTS`/ORA-955 `EXECUTE IMMEDIATE`/`user_tab_columns` guards; matches deployed `0001`/`0011`/`0041`). Already-applied migrations no-op; partial/replay is safe. There is no version-ledger — apply is per-file in numeric order.

## Pre-gate (done / verify)
1. Backup present + restorable: `ls -lh ~/backups/oracle-premulti/*.dmp` (1.2GB). Optional restore-test on a scratch PDB.
2. Copy backup off-host: `rsync ~/backups/oracle-premulti/*.dmp argonas:/mnt/datapool/backups/mnemos/`.

## Stage A — deploy code (PYTHIA)
1. `cd ~/mnemos-prod-working && git fetch && git checkout master && git reset --hard origin/master` (→ a6210bd).
2. Rebuild image: `docker build -t mnemos-os:latest .` (or pull if CI-built).
3. DO NOT restart mnemos-api yet (migrations first).

## Stage B — Oracle migration apply (GATED — operator OK)
Apply the delta in numeric order via the rewrite-aware runner against prod ORCLPDB1
(`oracle://mnemos:***@127.0.0.1:1521/ORCLPDB1`). New/changed since 66804e6:
`0035_subscription_plans_date_aware`, `0041_knemon_tier_assignments`,
`0042_knemon_baseline_tables`, `0043_memory_acl`, `0044_model_registry_pricing`
(plus any 0004-0008 oauth/groups not yet applied — idempotent, safe to re-run).
```
for m in 0035_subscription_plans_date_aware 0041_knemon_tier_assignments \
         0042_knemon_baseline_tables 0043_memory_acl 0044_model_registry_pricing; do
  .venv/bin/python scripts/oracle_apply_migration.py \
    --dsn "$ORACLE_PROD_DSN" --file db/migrations_oracle/$m.sql || { echo "ABORT at $m"; break; }
done
```
Verify (NOT via apply-OK — query the catalog):
```
select count(*) from user_tables where table_name='MEMORY_ACL';        -- expect 1
select count(*) from user_tab_columns where table_name='USER_GROUPS' and column_name='IS_ADMIN'; -- expect 1
```

## Stage C — restart + verify
1. `docker compose -f deploy/docker-compose.mnemos-api.yml up -d --force-recreate`
2. `curl :5002/health` → version advanced past 6.0.0rc1, `acl` in capabilities, `database_connected`.
3. Persistence conformance smoke; ACL read/write smoke (grant → read-as-principal → revoke).

## Stage D — replicas + NVIDIA/Spark
1. PEGASUS / ACHILLES / MEDUSA: same Stage A+B+C (each its own backend/DSN).
2. Spark (`spark-0c53`, NVIDIA side via .4): deploy master a6210bd. CAVEAT: Spark mnemos is Postgres/768-dim vs fleet Oracle/1024-dim — apply the **Postgres** migration chain (`db/migrations/`), not Oracle; reconcile embedding-dim separately. Same code, different backend.

## Rollback
Restore schema from the expdp dump:
```
impdp mnemos/***@ORCLPDB1 directory=DATA_PUMP_DIR \
  dumpfile=mnemos_pre_multiuser_20260608-012218.dmp \
  table_exists_action=replace
```
(or drop the added objects: `memory_acl`, `user_groups.is_admin`, `0044` tables).

## Known-open (NOT blocking fleet Oracle deploy)
- DB2 12.1.5 GA track: rewrite precision gap (`TIMESTAMP(6) WITH TIME ZONE`) + `db2_apply` success-reporting — MNEMOS `mem_1780883880054`. DB2 is GA-test only, not in fleet prod.
- Oracle idempotency live re-run: constructs proven by deployed `0041`/`0011` patterns; fresh-23ai re-run pending a writable instance (cerb=standby, proteus=down).
