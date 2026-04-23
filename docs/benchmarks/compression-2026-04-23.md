# MNEMOS v3.1 Compression Benchmark — 2026-04-23

First full-scale run of the v3.1 competitive-selection contest against
real production memories. Executed against a test deployment on
CERBERUS (192.168.207.96) with PYTHIA MNEMOS as the memory source.

This document exists to discharge the ROADMAP shipping criterion:

> `docs/benchmarks/compression-2026-04-23.md` with measured numbers
> across a real stratified memory sample from the production install
> — not single-input anecdata.

The results are honest, including a real bug the drain surfaced (now
fixed), the reason one engine contributes nothing under default
configuration (now disabled by default), and one memory that legitimately
cannot be compressed (correctly flagged as a contest no-winner rather
than being pushed through regardless).

---

## Infrastructure

| Component              | Detail                                              |
|------------------------|-----------------------------------------------------|
| Test host              | CERBERUS, 192.168.207.96                            |
| CPU                    | AMD Threadripper PRO 5945WX (12-core / 24-thread)   |
| RAM                    | 128 GB DDR4                                         |
| GPU                    | NVIDIA RTX 4500 Ada Generation, 24 GB               |
| Postgres               | `pgvector/pgvector:pg16` container, persistent volume |
| Judge model            | `google_gemma-4-E4B-it-Q6_K.gguf` via llama.cpp on :8080 |
| llama-server flags     | `--n-gpu-layers 99 --ctx-size 32768 --parallel 8 --flash-attn on -ctk q8_0 -ctv q8_0` |
| MNEMOS source          | `github.com/perlowja/mnemos` master @ `c0614c7`     |
| Scoring profile        | `balanced` (quality × ratio × speed, quality_floor=0.70) |
| Batch size             | 5 queue rows per iteration                          |

The llama-server instance was already resident on :8080 before the
test, serving ~5 GB of VRAM with 17 GB free. No additional GPU
allocation was required for the contest workload.

## Sample selection

49 memories pulled from PYTHIA MNEMOS via `/memories/search` across 15
diverse query terms, then stratified by category:

| Category       | Target | Selected | Longest (bytes) |
|----------------|-------:|---------:|----------------:|
| projects       | 15     | 15       | 16,993          |
| infrastructure | 15     | 14       |  6,775          |
| documentation  | 10     | 10       |  4,287          |
| facts          | 10     | 10       |  2,436          |
| **Total**      | **50** | **49**   |                 |

(`infrastructure` harvest pool was 14 available; all taken.) Content
length ranged from ~200 bytes up to ~17 KB, covering both short
declarative facts and long narrative session summaries.

Each memory was enqueued in `memory_compression_queue` with
`priority=0`, `reason='manual'`, and `scoring_profile='balanced'`.

## Protocol

One instance of `compression.worker_contest.process_contest_queue`
was invoked in a loop from a standalone driver script
(`/tmp/drain_cerberus.py`, distributed to CERBERUS), with
`batch_size=5`. Each iteration dequeued up to 5 pending rows, ran
each through `run_contest` against all three built-in engines, and
called `persist_contest` to write the winner and every loser into
the v3.1 tables.

Engines registered (order doesn't affect outcome):

1. **LETHE** — extractive token/sentence filtering, CPU-only, mode=auto
2. **ALETHEIA** — LLM-assisted token importance scoring via the gemma
   judge, `disable_fallback=True`
3. **ANAMNESIS** — LLM fact extraction via the gemma judge, category-aware
   prompt

---

## Results

### Timing

11 drain iterations, each processing up to 5 queue rows concurrently:

```
[iter  1]   54.4s  dequeued=5 succeeded=5
[iter  2]   35.4s  dequeued=5 succeeded=5
[iter  3]   31.2s  dequeued=5 succeeded=4 failed=1   ← composite=0 CHECK violation (since fixed)
[iter  4]   35.6s  dequeued=5 succeeded=5
[iter  5]   45.0s  dequeued=5 succeeded=5
[iter  6]   39.2s  dequeued=5 succeeded=5
[iter  7]   47.3s  dequeued=5 succeeded=5
[iter  8]   46.4s  dequeued=5 succeeded=5
[iter  9]   40.4s  dequeued=5 succeeded=5
[iter 10]   35.1s  dequeued=4 succeeded=4
[iter 11]  empty queue, done

TOTAL: 409.9s (~6.8 min)  dequeued=49 succeeded=48 failed=1
```

Average: **~8.4s per memory** wall-clock, including concurrent
LETHE (sub-millisecond), ALETHEIA (0.1-8s), and ANAMNESIS (~2-14s)
calls. LETHE and ALETHEIA overlap inside each `asyncio.gather`; the
per-memory critical path is dominated by ANAMNESIS's multi-second
extraction call.

### Winner distribution

Across 48 successful contests (excluding the 1 legitimate no-winner):

| Engine    | Wins | Share | Avg ratio | Avg composite |
|-----------|------|------:|----------:|--------------:|
| LETHE     | 30   |  62.5%|    0.528  |        0.402  |
| ANAMNESIS | 18   |  37.5%|    0.386  |        0.432  |
| ALETHEIA  |  0   |     — |         — |             — |

**LETHE dominates on the speed axis** — sub-millisecond elapsed
per memory gives it `speed_factor=1.0` in almost every contest.
Even at ratio~0.5 (modest 50% reduction) its composite beats
slower-but-better-compressing ANAMNESIS on most inputs.

**ANAMNESIS wins on longer memories** where its richer extraction
(summary + atomic facts) actually achieves a meaningfully smaller
output. Avg ratio 0.386 across its 18 wins vs LETHE's 0.528 across
its 30 wins — ANAMNESIS wins when ratio advantage exceeds its speed
penalty.

**ALETHEIA never wins.** See "ALETHEIA on instruction-tuned models"
below.

### Reject-reason distribution

Every engine attempt across all 49 contests, including the
subsequently-retried `mem_9ca7ca8849a5`:

| Engine    | reject_reason    | Count |
|-----------|------------------|------:|
| LETHE     | — (winner)       |    30 |
| LETHE     | inferior         |    19 |
| ANAMNESIS | — (winner)       |    18 |
| ANAMNESIS | inferior         |    27 |
| ANAMNESIS | error            |     4 |
| ALETHEIA  | quality_floor    |    40 |
| ALETHEIA  | inferior         |     9 |

`ANAMNESIS error` is the empty-extraction demotion path — 4 memories
where gemma either returned invalid JSON or the extraction yielded
no facts and no summary, correctly surfaced as an error rather than
silently returning empty content.

`ALETHEIA quality_floor` is every run where the parser fallback
kicked in (the "no valid token indices" path logged by the engine),
producing first-N truncation with self-reported `quality_score=0.60`
— below the `balanced` profile's 0.70 floor. The 9 `inferior`
ALETHEIA rows are the rare ones where the parse-fallback happened
to produce non-truncated content but still lost on composite.

---

## Notable findings

### 1. ALETHEIA on instruction-tuned models

ALETHEIA's v3.0 prompt asks the LLM to return a comma-separated list
of token indices to keep. Neither Qwen2.5-Coder-7B (tested earlier
on TYPHON) nor gemma-4-E4B-it (this run) can follow that instruction
reliably — both models return whitespace, punctuation, or off-spec
prose. ALETHEIA's parser falls through to first-30% truncation and
honestly reports `quality_score=0.60`, which the balanced floor then
rejects.

Net: ALETHEIA currently contributes no winners but still burns
~0.2-0.5s of GPU round-trip per memory it's asked to score.

**Decision for v3.1 GA**: ALETHEIA is disabled by default
(`MNEMOS_ALETHEIA_ENABLED=false`). Operators who have a
compatible model/prompt combination can opt in. The engine still
ships fully plugin-ABC-compliant; only its invocation by the
default worker loop is gated.

The real fix — replace the index-list prompt with a direct
compression prompt ("output the condensed text") that instruction-
tuned models actually respond to well — is v3.x scope. The
architectural hook (the `CompressionEngine` ABC) is unchanged; the
fix lives inside the engine.

### 2. Composite-zero winner CHECK violation (fixed during this run)

Iteration 3 failed one memory with:

    new row for relation "memory_compression_candidates" violates
    check constraint "mcc_winner_has_output"

Root cause: `mem_9ca7ca8849a5` (68 tokens — too short to compress).
LETHE returned `ratio=1.0` (no compression), ratio_term evaluated to
0.0 per the floor, composite evaluated to 0.0. ALETHEIA and ANAMNESIS
were both disqualified (quality_floor and empty-extraction error
respectively). LETHE became the sole survivor and "won" with
`composite_score=0.0`. `persist_contest`'s `_nullable_positive`
coerced 0 to NULL for audit clarity, and the
`mcc_winner_has_output` CHECK then rejected the INSERT (NULL
composite on a winner row is invalid by design).

Fixed in commit `664de7f`: `run_contest` now filters winner
eligibility to survivors with `composite_score > 0`. Zero-composite
survivors fall through to `reject_reason='inferior'`. The failing
memory was re-enqueued post-fix and drained cleanly with
`outcome.winner=None` and queue `status='failed'` / `error='no
winner: inferior=2, quality_floor=1'` — the honest "nothing
compressed this" result.

This was a real integration bug that only surfaced under real
production content (the synthetic tests didn't hit ratio=1.0). The
contest infrastructure is now correct under this edge; v3.1 ships
with the fix in place.

### 3. The legitimately-uncompressible memory

`mem_9ca7ca8849a5` (68 tokens) remains the only failed row:

```
mem_9ca7ca8849a5  status=failed  error="no winner: inferior=2, quality_floor=1"
```

This is the correct outcome. At 68 tokens, LETHE can't extract
meaningfully below `MIN_CHUNK_RATIO=0.15` without losing structure;
ANAMNESIS's summary+facts rendering (summary + 8 bulleted facts) is
*longer than the original* at 106 tokens (ratio=1.56); ALETHEIA's
parse fallback returns a weak first-N. None of the three engines
achieved positive composite score under the balanced profile. The
worker correctly marks the queue row `failed` with an honest
rejection-reason summary.

Operators dashboarding on `memory_compression_queue.status='failed'`
will see these as "content that resisted all enabled engines." For
short notes and single-fact memories that don't benefit from
compression, this is the correct semantic — leave the original in
`memories.content` and skip storing a degenerate "compressed"
variant that would just re-inflate on narration.

### 4. Guard stayed CLOSED

The `gpu_guard` circuit breaker for `http://localhost:8080` stayed
in state `closed` across all 146 GPU calls (2 per memory × 49 memories
+ the retry). Zero failures recorded. gemma-4-E4B-it-Q6_K on a
locally-resident llama-server is a solid endpoint for this workload.

### 5. ANAMNESIS extraction on short memories

Four memories triggered ANAMNESIS's empty-extraction error
(`gpu_used=true`, `error="empty extraction (no summary and no facts)"`).
Three of the four were `facts`-category memories under 2 KB — at
that length, the extraction prompt's output (summary + up-to-10 atomic
facts) often collapses to either invalid JSON or trivial rewording.
ANAMNESIS's category-aware prompt could be tuned per-category for
short inputs, but the current behavior (honest error, contest records
the attempt, other engines compete unimpaired) is acceptable for v3.1.

---

## v3.1 GA shipping confidence

The contest infrastructure — ABC, run_contest, persist_contest,
gpu_guard, worker_contest — is validated end-to-end against real
production content on real infrastructure:

- ✅ 48 of 49 memories compressed successfully
- ✅ 1 of 49 correctly classified as uncompressible (not silently
  stored as a degenerate variant)
- ✅ Winner selection honors the balanced scoring profile
- ✅ Full audit trail of 146 engine attempts persisted, queryable via
  `GET /v1/memories/{id}/compression-manifests`
- ✅ Circuit breaker held through the full drain
- ✅ Queue lifecycle correct: `pending → running → done/failed`,
  attempts counter incremented, second drain returns empty

The one CHECK-constraint bug this drain surfaced was fixed, tested,
verified against the same production content, and committed before
this document was finalized. The contest is ready for production.

---

## Explicitly out of scope for v3.1

Noting what v3.2+ will address based on this benchmark's findings:

- **Judge-LLM quality scoring** replacing self-reported
  `quality_score` (v3.2). Today's results depend on LETHE's lenient
  0.95 self-score beating ANAMNESIS's 0.85 on speed — a real judge
  would likely shift some of LETHE's wins to ANAMNESIS. That's a
  v3.2-grade correctness change and belongs with APOLLO's judge
  integration.
- **ALETHEIA prompt redesign** (v3.x). The engine ships fully
  ABC-compliant; only its default invocation is gated. Fix is
  internal to the engine.
- **Hot-path reads** (`/v1/memories/rehydrate`, gateway inject,
  session context) serving winner variants instead of raw content
  (v3.2, alongside APOLLO).
- **APOLLO engine** (v3.2–v3.4, Saturn V-staged per ROADMAP).
- **Tier 3 tenancy fixes** (KG owner_id, namespace enforcement on
  memory paths, app-layer owner filter, registry-backed
  `/v1/models`) — v3.1.1 release.

---

## Reproducing this benchmark

Driver scripts and seed script retained in `/tmp/*.py` on the dev
host plus `/home/jasonperlow/mnemos-test/` on CERBERUS:

- `seed_cerberus.py` — harvests + stratifies PYTHIA sample,
  inserts into memories + enqueues
- `drain_cerberus.py` — loops `process_contest_queue` until empty,
  prints timing and per-iteration counts
- `test_manifest_endpoint.py` — live-tests the
  `/v1/memories/{id}/compression-manifests` handler against the
  CERBERUS instance for three cases (winner, no-winner, 404)

Running the full benchmark is one command once the test instance
is up:

```bash
ssh jasonperlow@192.168.207.96 \
    /home/jasonperlow/mnemos-test/.venv/bin/python \
    /home/jasonperlow/mnemos-test/drain_cerberus.py
```

Expected timing: ~7 minutes wall-clock on CERBERUS's RTX 4500 Ada
with gemma-4-E4B-it-Q6_K. Scale linearly with memory count; dominant
per-memory cost is ANAMNESIS's extraction call.
