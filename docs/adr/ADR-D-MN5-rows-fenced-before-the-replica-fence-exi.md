# ADR D-MN5 — Rows fenced before the replica fence existed (MN-24)

**Status:** Proposed
**Date:** 2026-11-07
**Decision owner:** operator (Jason Perlow)
**Scope:** documentation decision only. No production code changes in this ADR.
**Backlog source:** D-MN5 is item MN-24: *"Auto-repair or manual reconcile.
**Rec:** ship `repair_fenced_replicas` with dry-run as the default; the operator
applies it once."* The backlog file
`refactor-research-20261031/BACKLOG-FROM-FABLE-20261107.md` is **not in this
repository** and could not be read (see §2.4).

---

## 1. Decision needed

Some replica rows on a receiving node were damaged by *local* lifecycle workers
(archival, dedup, consolidation, deletion sweeps) **before** the code that
protects replicas from those workers existed. That damage is permanent by
design: the receiver's sequence fence makes it drop the source's replay, so the
source never fixes those rows again. We must decide **how those already-damaged
rows get repaired** — automatically, or by a one-off operator command. We must
decide **now** because the fence stops new damage but leaves old damage in
place, and nothing in the product detects or repairs it, so the gap grows
quieter the longer it sits. If we do nothing, those rows stay wrong forever and
the only fix is a full re-seed of the whole peer.

**Recommended:** ship one repair tool, `repair_fenced_replicas`, **dry run by
default**; the operator reads the list and applies it once, explicitly.

---

## 2. Context — what we verified

Checked against the source at `026d9cec` on branch `hv/adr-d-mn5-20261107`.
Line numbers are from that checkout. Unverifiable claims are in §2.4 and are not
load-bearing for the recommendation.

### 2.1 The damage is permanent by design

- A replica row is any `memories` row whose `federation_source` is non-`NULL`
  (`mnemos/core/eligibility.py:33-50`).
- The receiver keeps a durable per-`(peer, remote_id)` fence in
  `federation_receive_state`; `accept_event`
  (`mnemos/persistence/federation_journal.py:421`) returns `False` when the
  incoming `sequence <= last_sequence` (`:437-440`), **before any write**.
- `_store_memories` calls it and, on `False`, only tries the narrow replay
  repair and then `continue`s (`mnemos/domain/federation.py:1364-1377`). No
  other write path exists for an already-fenced sequence.
- Therefore a replica that a local worker soft-deleted, archived
  (`content = 'ARCHIVED:<id>'`) or consolidated **before** the fence shipped
  returns only via the narrow replay repair, a brand-new source event, or a full
  re-seed.

### 2.2 The fence (the fix for the *future*) is shipped and on by default

- Switch `MNEMOS_FEDERATION_REPLICA_FENCE`, resolved by
  `federation_replica_fence_enabled()` (`mnemos/core/config.py:2253-2284`):
  **unset = ON** (`:2279-2280`), a typo falls back to ON with a warning
  (`:2285-2291`). One shared predicate, `local_sweep_predicate()`
  (`mnemos/core/eligibility.py:33-50`), appended by
  `with_local_sweep_predicate` (`:52-55`).
- It is applied at every local sweep site I checked, on all six backends:
  PERSEPHONE archival (`mnemos/persistence/worker_lifecycle.py:724-736`,
  `mnemos/domain/persephone/runner.py:42-71`,
  `mnemos/domain/admin_lifecycle_repo.py:142-150`); deletion soft/hard sweeps
  and restores (`worker_lifecycle.py:415`, `:475`, `:506`, `:558`); ARTEMIS
  dedup/consolidation (`sqlite.py:2119`, `:2176`; `postgres.py:1477`, `:1519`;
  `mysql.py:2979`, `:3037`; `oracle.py:2355`, `:2415`; `db2.py:2064`, `:2181`,
  `:2211`); MORPHEUS eligibility (`mnemos/core/eligibility.py:57-67`).
- Verified by running `pytest tests/test_federation_replica_fence.py` →
  **16 passed**.
- **What it does not do:** repair rows damaged while it was absent or switched
  off. Every sweep now skips those rows — correctly — so nothing else touches
  them again, and they stay invisible unless something looks for them.

### 2.3 Detection and reconcile exist, but neither covers this population

- `replica_parity()` (`mnemos/domain/federation/parity.py:399`) buckets the
  replica's `fed:<peer>:*` rows into `missing` / `extra` / `stale` /
  `null_embedding` / `unreachable` (`:48`, `:450-455`); `in_sync` is true only
  when the source's export set was available and all buckets are empty
  (`:466`). Read-only, keyset-paged.
- **But `missing` is the wrong label.** `_replica_keyset` counts every row whose
  `local_id` exists (`parity.py:84-111`), so a pre-fence-damaged replica is
  *present*: at best it shows as `stale` (same id, different `content_hash`), and
  an archived row with an unchanged hash reads as healthy. **There is no
  `damaged` population** — the operator cannot even see the problem.
- Repair machinery that exists, and its limits:
  - Automatic, replay only: `_repair_locally_damaged_replica`
    (`mnemos/domain/federation.py:1198-1255`), called **only** when
    `accept_event` returned `False` (`:1364-1377`), i.e. only when the same
    sequence is replayed. Does nothing when the switch is off (`:1218-1219`).
  - Repository method: `restore_federated_memory_after_local_damage`
    (`mnemos/persistence/base.py:2793-2817`; SQLite `:7099`, Postgres `:6221`,
    Oracle `:7793`, MySQL `:7917`, Db2 `:3912`; MariaDB inherits MySQL). One
    conditional UPDATE clearing `deleted_at`/`archived_at`/`consolidated_into`
    and reinstating the content (`sqlite.py:7114-7140`). The caller must supply
    the content; the method does not fetch it.
  - Manual: `reconcile_peer(..., apply=False)` (`parity.py:502-506`), via
    `POST /v1/federation/peers/{id}/reconcile`
    (`mnemos/api/routes/federation.py:1166-1188`) and
    `mnemos federation reconcile` (`mnemos/cli/main.py:1349-1366`). It re-pulls
    **only** ids reported `missing` (`:540-541`), which these rows are not.
- `repair_fenced_replicas` **does not exist in this repository.** A repo-wide
  search for `repair_fenced_replicas`, `repair_fenced`, `fenced_replica`
  returned no hits. It is the backlog's proposed name; §5 specifies what it
  must do — it does not pretend it is built.

### 2.4 What I could not verify

- **The backlog file and MN-24's internals.** Not in this checkout. The D-MN5
  grouping and severity come from the task brief only, so §5 names the work it
  unblocks by code, not by id.
- **Fleet reality.** I did not measure how many rows are pre-fence-damaged —
  the 0.7 % in `tests/test_federation_replica_fence.py:9-11` is that file's
  prose about the symptom observed on one replica, not something I
  re-measured. §6 asks.
- **Non-`memories` tables.** The fence covers `memories` only
  (`worker_lifecycle.py:415`, `:475`, `:558`). Whether archival strands
  `memory_versions`/KG rows for a replica, I did not trace; the repair is scoped
  to `memories`.

---

## 3. Options

### (a) Do nothing — rely on the fence and the existing reconcile

- **Benefit:** no new code, no new operator step; the fence already stops
  further damage (§2.2).
- **Cost:** pre-fence damage never heals; the only remedy is a full re-seed of
  the peer, riskier and less bounded than fixing a few rows.
- **Risk:** silent permanent divergence — the replica serves fewer/older rows
  than the source, parity calls it `stale` at best, and every report built on
  the replica under-counts.
- **Reversibility:** high (it is the status quo, and (b) stays possible later),
  but it does not undo the divergence.

### (b) `repair_fenced_replicas`, dry run by default, operator applies once

- **Benefit:** one discoverable command whose *default changes nothing*. It
  reuses the already-tested primitive on all five implementing backends (§2.3),
  so it adds a *selection*, not a new SQL surface. It is per-row and bounded,
  safe on a live system, and its dry-run output is what the operator reviews
  before applying.
- **Cost:** a repository read + a service function + CLI/route wrappers, and it
  must fetch each damaged row's current state from the source over the by-id
  path (the same HTTP client `_redispatch_on_source` uses, `parity.py:634-663`).
- **Risk:** an apply rewrites replica rows to what the source says *now*, so a
  row an operator deliberately soft-deleted (a supported policy —
  `docs/FEDERATION_JOURNAL.md:94-95`) would be resurrected if the tool cannot
  tell the two cases apart. §2.3 says the row carries no such marker, so this
  is a real question (§6 Q2), not a formality.
- **Reversibility:** the repair is idempotent and per-row with no schema change;
  a wrong apply shows up in the next dry run and any row can be soft-deleted
  again. Hand-made damage is not automatically reproduced.

### (c) Automatic repair on every sync, no operator step

- **Benefit:** divergence closes by itself; nobody remembers a one-off.
- **Cost:** it changes what a sync means. Today a soft-deleted replica is
  counted in `skipped_deleted` and deliberately left alone
  (`federation.py:1423`, `:1519`; `docs/FEDERATION_JOURNAL.md:94-95`: reviving a
  replica is a policy decision, not a sync side effect). Automatic repair needs
  to separate sweep damage from operator deletion, which the schema cannot do
  (§2.3).
- **Risk:** silent writes that undo operator decisions; `skipped_deleted`
  becomes meaningless. Large blast radius for a one-time backlog.
- **Reversibility:** poor — once rows are resurrected in production, nobody can
  reconstruct which were operator deletions.

---

## 4. Recommendation

**Take (b): `repair_fenced_replicas`, dry run by default, applied once by the
operator.**

1. The fence already stops new damage (§2.2), so this is a **one-time cleanup**,
   not a standing behaviour — and (c) permanently changes what a sync means to
   buy that one-time result.
2. The repair primitive already exists on every backend (§2.3). The only missing
   piece is *selection*: `reconcile` selects `missing`, and these rows are not
   missing. That is a small, testable increment.
3. Dry-run-first matches the repo's existing posture: `reconcile_peer` defaults
   `apply=False` (`parity.py:506`), the CLI spells it `--apply`
   (`main.py:1352`). An operator who has not read this ADR still cannot damage
   anything by running the tool.
4. (c) is ruled out by §2.3: without a marker that distinguishes local-sweep
   damage from deliberate deletion, automatic repair cannot be made safe.

**Strongest argument against:** the tool's accuracy rests on a new selector over
data the current report does not even name (§2.3), so the operator is trusting
fresh code on a live replica — while the fallback, a one-time re-seed, is a
procedure they already know.

---

## 5. Implementation plan

Nothing rewrites data before the operator has seen a dry run.

1. **Classifier first (read-only).** Add a `damaged` population to
   `replica_parity` (`parity.py:48`, `:450-455`) so the damage is visible
   *before* any repair exists — additive: `in_sync` (`:466`) gains a term, and a
   row is never both `damaged` and `missing`/`stale`. Reuses step 2's selector.
2. **Selector (read-only).** A repository read returning the peer's
   pre-fence-damaged replicas: `local_id = fed:<peer>:<remote_id>` **and** any of
   `deleted_at`, `archived_at`, `consolidated_into` set — the existing repair
   predicate (`sqlite.py:7135-7137`) lifted from one row to a peer-wide,
   keyset-paged scan like `fetch_source_export_keyset_page` (`base.py:2903`).
   ABC method → all six backends → facade → caller, per `AGENTS.md`'s ABC-first
   rule; never read `_lc._pool`.
3. **Service.** `repair_fenced_replicas(backend, peer_id, *, apply=False,
   limit=None)` in `parity.py`, beside `reconcile_peer`. Dry run: ids plus what
   is wrong with each. Apply: per row, fetch current state from the source and
   call `restore_federated_memory_after_local_damage` (one transaction per row).
   Per-row failures are collected, never raised — one bad id must not stop the
   run, as `reconcile_peer` already models (`parity.py:600-627`).
4. **Surface.** `POST /v1/federation/peers/{id}/repair-fenced-replicas` with
   `apply=Query(False)` (mirrors `routes/federation.py:1166-1188`) and CLI
   `mnemos federation repair-fenced-replicas --apply` (mirrors
   `main.py:1349-1366`), same federation role guard.
5. **Docs.** A section in `docs/FEDERATION_JOURNAL.md` after "Reconcile"
   (`:65-77`): what pre-fence damage is, why it cannot self-heal, and the
   dry-run-then-apply procedure.
6. **Tests.** (i) Selector finds soft-deleted, archived and consolidated
   replicas; ignores a healthy one, a local row and another peer's replica.
   (ii) Real SQLite like `tests/test_federation_replica_fence.py`, reusing its
   `_insert_local_and_replica` (`:200-228`): damage a replica the way that file
   does — dry run leaves it untouched, apply restores it (assertions as its
   `test_replayed_event_restores_*`, `:109-166`). (iii) An archived row returns
   with the *source's* content, not `ARCHIVED:<id>`. (iv) `parity` reports
   `damaged`, and `in_sync` is false while it is non-empty.
7. **Rollout.** One minor release. Release note: the sync path is unchanged; run
   `mnemos federation repair-fenced-replicas <peer>` on each receiver, read the
   dry run, then re-run with `--apply`. No migration, no default change.
8. **Rollback.** Stop using the command. An applied repair leaves rows in the
   source's state, which is a valid state; there is no schema to reverse.

**Unblocked by this (named by code — the backlog file is unreadable, §2.4):**
MN-24 itself; the replay-only limit of the F-3 resurrect path
(`federation.py:1198-1255`); the missing `damaged` population in the F-15
parity report (`parity.py:48`); and the F-2 `skipped_deleted` rule
(`federation.py:1423`, `:1519`), which this repair must respect, not override.

---

## 6. Open questions for the operator

1. **How much damage is there?** I could not measure it (§2.4). If a dry run
   lists a handful of rows, (b) is obviously right; if thousands, re-seeding may
   genuinely be cheaper.
2. **What about rows an operator soft-deleted on purpose?** The schema cannot
   tell them from sweep damage. Should the tool skip `deleted_at`-only rows and
   repair only archived/consolidated ones, or list both and let the operator
   pick per id?
3. **Who may run it?** The federation role (like `reconcile`) or root only?
   Rewriting replica content is a data-integrity action.
4. **Is non-`memories` archival damage in scope** (§2.4), or `memories` only?
5. **Should the `damaged` bucket ship on its own first**, so the problem is
   visible even if the repair tool slips?

---

## 7. Status

**Proposed.** Awaiting the operator's decision on §6. No code merged under this
ADR.
