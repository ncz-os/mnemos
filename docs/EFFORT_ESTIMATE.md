# MNEMOS Development Effort Estimate

**Companion to**: `docs/SPECIFICATION.md` (v3.2.0).
**Purpose**: calibrate what it cost (and would cost) to build MNEMOS,
in terms a scoping tool can sanity-check.
**Status**: analytical — based on the v3.2.0 artifact as measured,
not on future work.

This is not a bid, a quote, or a capacity plan. It's a set of
bounded numbers derived from the actual codebase, with the
calibration assumptions named so you can substitute your own and
re-derive.

---

## 1. Raw baseline (LOC-anchored)

At v3.2.0:

| Metric | Value |
|--------|-------|
| Total Python LOC | 31,662 |
| Production LOC | 21,562 |
| Test LOC | 10,100 |
| SQL migrations | 17 files |
| REST endpoints | 91 across 21 routers |
| MCP tools | 13 |
| DB tables | 32 |
| Tests | 467 passing + 8 skipped |

Professional output rate for **correctness-critical infrastructure
Python** (Postgres + asyncio + multi-subsystem):
**150–250 shipped LOC/day**. This bakes in time for design
decisions, review, debugging, and test-writing that happen alongside
typing. Research prototypes clock 500–1500 LOC/day but throw most
of it away; line-to-line infrastructure is slower.

**Floor**: 31,662 ÷ 200 LOC/day ≈ **158 person-days of coding**.

That's coding only. Real projects pay for everything around the
coding too.

---

## 2. Multipliers for a ground-up build

| Layer | Multiplier | Why |
|-------|------------|-----|
| Raw coding | 1.0× | ~158 person-days |
| Design + architecture decisions | +40% | 22 subsystems × interface decisions × integration shapes. Each has a right answer that's not obvious until the second time. |
| Debugging + integration correctness | +30% | Hash-chain audit, DAG merge, compression contest, stranded-running sweep — each was wrong on first implementation and produced a documented incident before reaching the shipped shape. |
| Schema evolution (17 migrations) | +15% | Several are backfill fixes for earlier shape mistakes (see `migrations_v3_1_2_audit_log_columns.sql` for a representative case). |
| Docs + examples + install | +15% | Install profiles, service units, Docker, MPF export — non-trivial operational surface. |

**Effective**: 158 × 2.0 ≈ **~320 person-days of focused work
≈ 15 person-months at a 21-day work-month.**

---

## 3. Realistic calendar shapes

| Team shape | Calendar time | Notes |
|------------|---------------|-------|
| Solo senior engineer, focused full-time | **9–14 months** | Context-switching between subsystems is the real drag; the three correctness-heavy areas (compression contest, DAG merge, hash-chain audit) each need quiet thinking time that doesn't parallelize with other tasks |
| Solo senior engineer, part-time (nights + weekends) | 18–30 months | Linear extrapolation undercounts: async work loses more to re-context than solo focused sprints |
| 2-person team, clean subsystem ownership | **6–9 months** | Good fit: one owns storage / tenancy / DAG, the other owns reasoning / compression / gateway |
| 3-person team | 4–6 months | Subsystem boundaries are clean enough to parallelize; the third seat owns observability / ops / install |
| 4+ people | Diminishing returns | Coordination overhead exceeds parallelism gains |

---

## 4. LLM-assisted coding compression factor

On this specific codebase the LLM-assistance effect is measurable
and uneven:

| Work type | Speedup |
|-----------|---------|
| Schema design, regex authoring, test scaffolding, commit-message prose | **2–3×** |
| Architectural decisions, subtle correctness (hash chain, DAG merge) | **1.2–1.5×** — the LLM surfaces options but the operator still picks |
| Debugging distributed-state issues (race conditions, sweep-vs-dequeue conflicts) | **≈1.0×** — LLMs don't meaningfully speed up "why does this sometimes race" |

**Net aggregate**: **~1.5–2× on aggregate**.

So the solo-full-time 9–14 month range compresses to
**~5–9 calendar months with LLM assistance**, which matches the
actual timeline if MNEMOS has been under active development for
a couple of quarters.

---

## 5. Where the effort actually concentrated

Back-of-envelope weight against the 15-person-month budget:

| Work bucket | Est. share |
|-------------|-----------|
| Compression platform (contest + engines + GPU guard + persisted audit) | **20%** |
| Storage layer (memories + DAG + KG + tenancy enforcement) | **20%** |
| GRAEAE (providers + reliability stack + hash-chain audit) | **15%** |
| Gateway + sessions + model registry + scheduled sync | **10%** |
| Auth (Bearer + OAuth + namespace + admin API) | **10%** |
| Federation + webhooks | **10%** |
| Observability (4 instruments, middleware ordering) | **5%** |
| Install + service + Docker + CI | **5%** |
| Schema migration chain (17 files, several backfills) | **5%** |
| (Tests already counted inside each bucket as their share) | — |

The three largest buckets — compression, storage+DAG, GRAEAE —
are **55% of the project**. A scoping tool that buckets those
correctly lands within 20% of reality. One that treats them as
generic "storage + reasoning" under-counts by 30–40%.

---

## 6. The earned-complexity wildcard

MNEMOS contains a lot of invariants and edge cases that exist
because of **specific incidents**, not because the first design
anticipated them:

| Feature | Earned from |
|---------|-------------|
| v3.1.1 stranded-running sweep | Worker crash left queue rows stuck indefinitely |
| GPUGuard probe-identity handshake | Stale probes corrupted HALF_OPEN state transitions |
| Middleware LIFO fix (v3.2 tail) | RequestID wasn't bound when CORS/Session/SlowAPI logged errors |
| Consultation persistence vs. engine consensus | Local `max()` over `all_responses` diverged from engine's `_compute_consensus` on all-failure consultations |
| Clamp on `MNEMOS_CONTEST_STALE_THRESHOLD_SECS` | Negative env value would turn the cutoff into `NOW() + \|t\|` and wipe the queue |
| Three-tier COALESCE on rehydrate + gateway | Naive single-column read missed the v3.1 compression variants |

Each one is **~50–200 LOC** but each represents a production
lesson. A greenfield re-build won't know to include them on v1 and
will ship v1 without them.

**Budget another 10–20% for Year-2 hardening** that turns a
feature-complete v1 into production-credible infrastructure.

---

## 7. TL;DR — how to sanity-check a scoping tool against this

The spec at `docs/SPECIFICATION.md` is the input. A scoping tool's
output should land somewhere in this envelope:

| Scoping output | Reading |
|----------------|---------|
| "6 engineer-weeks" | **Wrong by an order of magnitude.** Probably treating LOC as lines-to-type, or ignoring the 17 migrations + 22+ FK edges + 32 tables. |
| "3 engineer-months" | **Undercounts the correctness-critical subsystems.** Compression contest, hash-chain, DAG are each multi-week efforts on their own. |
| **"~15 engineer-months of focused work" or "6–9 calendar months for a 2-person team"** | **Read the spec correctly.** Matches the artifact. |
| "2–3× higher than that" | **Double-counting integration layers** or treating the 91 REST endpoints as independent work — they share Pydantic models + tenancy filter + auth + observability scaffolding. |

If the tool doesn't weigh the three "20%/15% buckets" (compression,
storage+DAG, GRAEAE) against the rest, it's reading the 91 endpoints
as breadth and missing the depth.

---

## 8. What this is NOT

- **Not a bid.** Actual engagements involve team capacity,
  organizational friction, code review standards, and decision
  latency that don't show up in LOC.
- **Not a prediction of future work.** v3.3 scope is in
  `ROADMAP.md`; estimating that is a separate exercise.
- **Not a complexity claim vs. alternatives.** "How much would it
  cost to build a different memory system" is not what this
  answers — it answers "what did this one cost to reach v3.2.0."
- **Not validated against a stopwatch.** The numbers are
  back-of-envelope against industry calibration, not timesheet
  data. If you have the stopwatch, replace these with measured
  values.

---

*Revisions land in the same commit that changes the estimate's
inputs (LOC, subsystem count, earned-complexity wildcards).*
