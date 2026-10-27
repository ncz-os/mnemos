# `ModelRegistryRepository` — the backend-neutral model registry contract

Status: **phase 1.5** (provider/pricing DB consolidation).
ABC: [`mnemos/persistence/base.py`](../mnemos/persistence/base.py) →
`ModelRegistryRepository`.
Contract suite: [`tests/persistence/test_model_registry_contract.py`](../tests/persistence/test_model_registry_contract.py).

The model registry is the authoritative catalog of providers and models:
the `(provider, model_id)` rows, the price rows, per-field observations with
provenance, the operator-owned curated routing layer, the provider-sync audit
log, and the registry health counters.

Phase 1.5 makes that surface **backend-neutral**. Before this change the write
path was Oracle-only (`ROWNUM`, `:named` binds) and the Postgres path was a
separate price upsert; neither was expressible against a second backend, and
SQLite could not run the pipeline at all. The contract now lives in an ABC,
SQLite implements it fully, and every other backend either implements it or
declares its non-conformance by name.

## Conforming backends

| Backend | Status | Notes |
|---|---|---|
| **SQLite** | **Conforming** | Full implementation; the CI reference. Always runs in the contract suite. |
| **Postgres** | **Partial** | Implements the operations its existing schema supports; the rest raise a named error. Runs in the suite when `MNEMOS_TEST_POSTGRES_DSN` is set. |
| Oracle | Not yet | `unsupported backend: oracle — phase 2` |
| MySQL | Not yet | `unsupported backend: mysql — phase 2` |
| MariaDB | Not yet | `unsupported backend: mariadb — phase 2` |
| DB2 | Not yet | `unsupported backend: db2 — phase 2` |

A backend that does not conform does **not** get a silent stub. Its
repository class inherits `UnsupportedModelRegistryRepository`, every operation
raises `ModelRegistryNotSupported("<backend>", "<operation>")`, and the
contract suite skips it with the reason string above. Nothing returns an empty
list or a zero counter that could be mistaken for a real answer.

### Postgres: what is implemented, and what is not

Postgres phase 1.5 adds **no DDL and no migration**. Operations are implemented
only where the columns already exist:

Implemented — `upsert_model`, `list_models`, `get_model`, `set_alias`,
`resolve_alias`, `record_field_obs`, `merge_fields`, `sync_log_append`,
`health_read`.

Listed in `PostgresModelRegistryRepository.MISSING_OPERATIONS` (raises
`ModelRegistryNotSupported`) — `upsert_price`, `upsert_benchmark`,
`curated_routing_get`, `curated_routing_replace`, `snapshot_digest`,
`health_write`.

The band-aware pricing upsert that predates the ABC (`upsert_model_pricing`,
`write_price_history`) is unchanged and keeps working; it is not the ABC's
`upsert_price`.

## Operations

Every operation takes the caller's `Transaction` (`tx`) so a registry write
commits or rolls back with the operation that triggered it.

| Operation | Purpose |
|---|---|
| `upsert_model(tx, model) -> bool` | Insert or update a model row. `True` when inserted. |
| `list_models(tx, filters) -> list[ModelRecord]` | Models matching `ModelFilter`, ordered by `(provider, model_id)`. Alias rows never surface. |
| `get_model(tx, provider, model_id) -> ModelRecord \| None` | One model row; `None` when absent. Never fabricates a row. |
| `set_alias(tx, provider, alias, target_model_id)` | Register an alias. Raises `ValueError` on self-pointer, alias-to-alias, or lookalike collision. |
| `resolve_alias(tx, provider, alias) -> ModelRecord \| None` | Resolve exactly one hop. A non-alias id resolves to itself. |
| `upsert_price(tx, band)` | Insert or replace one `(model, pricing_model, band)` price row. |
| `record_field_obs(tx, obs)` | Upsert one per-field value with its `source` and `fetched_at`. |
| `merge_fields(tx, provider, model_id, *, authoritative_source=None)` | Project observations onto the model row. Never creates a model. |
| `upsert_benchmark(tx, ...)` | Upsert one `(model, benchmark)` score with provenance. |
| `curated_routing_get(tx) -> CuratedRouting \| None` | Newest curated snapshot (read-only). |
| `curated_routing_replace(tx, *, rules, note=None) -> CuratedRouting` | Append a new version. The only write path to the curated layer. |
| `sync_log_append(tx, entry)` | Append one provider-sync audit row. |
| `snapshot_digest(tx) -> str` | Stable hex digest over the registry's logical contents. |
| `health_read(tx) -> ModelRegistryHealth` | Registry counters. |
| `health_write(tx, health)` | Record a health snapshot. |

## The NULL rule — unpriced is not zero

**A price that is unknown is `NULL`. A price of `0.0` means "demonstrably
free".** These are different facts and the contract never conflates them.

* `upsert_model` does **not** write price columns at all. Prices belong to
  `upsert_price`, which stores `None` as SQL `NULL`.
* `upsert_price` mirrors a `band == 'standard'` row onto the model row so the
  existing `GET /v1/models` and recommendation paths keep working. The guard is
  exactly `incoming is payg OR (all three mirrored prices are currently NULL)`:
  * a `payg` row always applies and is authoritative — it overwrites whatever
    was mirrored before, including an earlier non-`payg` price;
  * any other pricing model fills in **only** while the row carries no price
    yet. It never displaces a price already mirrored, so a later
    `subscription` row cannot clobber a `free_tier` / `local` `0.0`;
  * a row with no prices at all (`pricing_model='unknown'`) mirrors nothing —
    an unpriced model stays `NULL` rather than looking free.

  The "already priced" test reads the row **before** the update, so the three
  `IS NULL` checks must be `AND`ed together and `AND`ed with the non-`payg`
  branch. `OR`ing in `pricing_model <> 'payg'` would let every non-`payg` row
  through and is the defect this rule exists to prevent;
  `tests/persistence/test_model_registry_contract.py::test_non_payg_price_never_displaces_another_pricing_models_price`
  is the regression guard.
* `ModelFilter(priced=True)` selects rows where **any** price column is
  `NOT NULL`; `priced=False` selects rows where all are `NULL`.
* `health_read` reports `priced_count` and `unpriced_count` with
  `priced + unpriced == model_count`, and counts a `0.0` model as **priced**.
* **Alias rows are never counted as models.** An alias (`alias_of IS NOT NULL`)
  is a pointer to a model, so `model_count`, `priced_count`,
  `unpriced_count`, `list_models` and `snapshot_digest` all exclude it. Adding
  an alias must not change the export hash — otherwise a pure rename would
  look like a data change to every downstream consumer.
* `snapshot_digest` distinguishes `NULL` from `0.0`, so a price change from
  unknown to free (or back) changes the digest.

Legacy note: the pre-existing SQLite columns `input_cost_per_mtok` /
`output_cost_per_mtok` are `NOT NULL DEFAULT 0` and are **not** part of the
contract. The repository omits them from its `INSERT` so the table default
applies, and never reads them for ABC results. The authoritative columns are
`price_in` / `price_out` / `price_cached`, which are nullable on purpose.

## Price bands and pricing models

Price rows are keyed by `(provider, model_id, pricing_model, band)`.

* `pricing_model` ∈ `payg` | `subscription` | `free_tier` | `local` | `unknown`
* `band` ∈ `standard` | `peak` | `off_peak`

Both round-trip verbatim and are validated on write (`ValueError` on an
unknown value). A row lives only on its own key: `peak` never overwrites
`standard`, and `subscription` never overwrites `payg`.

## Provenance

`source` and `fetched_at` belong to the **field**, not to the row. Refreshing
`context_window` from one upstream never stamps a sibling price observation as
fresh. `merge_fields` applies only observations whose `source` matches
`authoritative_source` (or all of them when it is `None`); fields with no
observation are left untouched.

## Aliases

`set_alias` is the only write path. It refuses:

* an alias pointing at itself;
* an alias whose target is itself an alias (one hop only);
* a lookalike — an id that differs from an existing model id or alias only in
  case or separators (`gpt-4o` vs `GPT_4O`).

An alias is unique per provider. `list_models` never returns alias rows.

## Curated routing

The curated layer is operator-owned and versioned. `curated_routing_replace`
appends at `version = previous + 1` and never rewrites a prior snapshot. The
catalog refresh path has **read-only** access: it calls `curated_routing_get`
and has no path to `curated_routing_replace`. The contract suite asserts that
running a price refresh leaves the curated version and rules untouched.

## Deterministic export

`snapshot_digest` hashes canonically ordered, canonically encoded rows
(models, price rows, benchmarks, curated version). The same logical registry
yields the same digest on every backend, in any insertion order, so an export
can be compared across backends and across runs.

## Running the contract suite

```bash
python3 -m pytest -q tests/persistence/test_model_registry_contract.py

# with a live Postgres
MNEMOS_TEST_POSTGRES_DSN='postgres://user:pass@host:5432/db' \
  python3 -m pytest -q tests/persistence/test_model_registry_contract.py
```

SQLite always runs. Postgres runs when the DSN variable is set and otherwise
skips with a reason naming it. The unsupported backends skip with
`unsupported backend: <name> — phase 2`; a case that asserts the exact reason
string fails if those backends ever start silently passing.

## What remains for phase 2

1. **Oracle** — land the `model_prices`, `model_field_obs`, `model_benchmarks`
   and versioned `curated_routing` DDL, and translate the existing Oracle
   `ROWNUM`/`:named` write path onto the ABC. Phase 1.5 deliberately makes no
   Oracle DDL change and no behaviour change to the existing path.
2. **Postgres** — add the four missing tables and implement the six operations
   listed in `MISSING_OPERATIONS`.
3. **MySQL / MariaDB / DB2** — implement once the Oracle port establishes the
   physical shape; MariaDB rides the MySQL implementation.
4. Delete each `*ModelRegistryRepository` class in
   `mnemos/persistence/model_registry_unsupported.py` as its backend lands, and
   drop the name from the suite's phase-2 list.

Non-goals for phase 1.5, by design: no Oracle DDL, no Oracle behaviour change,
no live migrations, no live database connections in CI.
