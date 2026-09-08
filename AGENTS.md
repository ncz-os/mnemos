# AGENTS.md — the MNEMOS repo family

This repo was split into many smaller repos for maintainability. That makes
"audit mnemos" ambiguous — there is no single checkout that contains
everything. This file exists so an agent asked to do a full MNEMOS audit
(dead code / security / performance) knows what "everything" actually means,
without re-deriving it from scratch by grepping docs and guessing.

**Before trusting this file:** it is a snapshot (2026-09-08). Repos get
added, renamed, or retired. If something here looks stale (a repo is gone,
a "separate" repo turns out to be merged in-tree, a branch name is wrong),
verify against the live state rather than assuming this file is current —
and update it once you've confirmed the correction.

## Authoritative source

**`gitlab.com/ncz-os/mnemos` is the authoritative line for mnemos core.**
As of 2026-09-08, a separate bare repo on ARGONAS (`mnemos-production.git`,
what `/opt/mnemos` on PYTHIA actually tracks) had independently diverged
from GitLab since an April fork point (`d093bc33`) — 517 commits unique to
the ARGONAS line, 1255 unique to GitLab's, with GitLab's line materially
more current. Do not treat `mnemos-production.git` or any ARGONAS checkout
as source of truth without first checking it against GitLab; work aimed at
core mnemos should target a branch off GitLab's current `master`.

## Installation and optional modules

Core installs stay small; almost everything beyond memory CRUD is an
opt-in pip extra (`pip install mnemos-os[<extra>]`, comma-separate for
more than one). Without an extra installed, its feature is a documented
no-op, not an error — e.g. `install_tracing()` silently does nothing
without `[tracing]`.

**v5 feature extras** (the named subsystems from the section above):

| Extra | Adds | Subsystem |
|---|---|---|
| `morpheus` | numpy | MORPHEUS / APOLLO S-IVB dream-state pipeline |
| `persephone` | zstandard | PERSEPHONE archival |
| `pantheon` | *(none — pure in-tree)* | PANTHEON LLM proxy/facade |
| `kronos` | numpy | KRONOS recall observability |
| `kronos-gpu` | cupy | KRONOS GPU acceleration |
| `knossos` | *(none — pure in-tree)* | KNOSSOS (MemPalace-compatible interop) |
| `apollo` | *(none — pure in-tree)* | APOLLO compression worker |
| `artemis` | networkx, scipy | graph-based subsystem |
| `nats` | nats-py | NATS event bus integration |
| `hot` | mnemos-hot | pulls in `mnemos-hot-rs`'s PyO3 extension |
| `edge` | aiosqlite, sqlite-vec | edge/embedded SQLite deployment |

**Bundles** (combine several extras for a deployment shape):

- `server` = nats + persephone + pantheon
- `ml` = morpheus + kronos + apollo + artemis + hot
- `interop` = knossos
- `full` = everything above

**Cross-cutting extras** (not subsystem-specific):

- `build` — pyinstaller + sqlite-vec, for producing a standalone binary
- `docling` — document-import support
- `tracing` — OpenTelemetry (OTLP/HTTP spans); no-op without it
- `structlog` — structured JSON logs, opt in at runtime via `MNEMOS_STRUCTURED_LOGS=true`
- `sqlite` — aiosqlite + sqlite-vec (base SQLite backend support, distinct from `edge`)
- `semantic` — CPU semantic-similarity scoring via fastembed (ONNX, no torch/CUDA)
- `gpu` — CUDA-accelerated embeddings (fastembed-gpu); **do not install on non-NVIDIA hardware** (Apple Silicon, Intel iGPU, Tegra, ARM) — use `semantic` or `phi` instead
- `phi` — Intel-iGPU-accelerated path (OpenVINO + FastEmbed); this is the production path on PYTHIA
- `dev` — test/lint tooling (pytest, import-linter, etc.)

When auditing or extending a subsystem, check whether it's gated behind
one of these extras before assuming a feature is dead code — an
apparently-unused import guarded by `is_extra_installed("kronos")` (see
`mnemos/mcp/tools/__init__.py`'s handling of KRONOS_TOOLS for the pattern)
is conditional, not dead.

## Repos that are real MNEMOS subsystems (separate git repos)

| Repo | What it is |
|---|---|
| `mnemos` (this repo) | Core: API, MCP tool registry, domain logic |
| `mnemos-bridge-core` | Shared MCP transport/dispatch library used by the other bridges |
| `mnemos-bridge-openai` | OpenAI SDK adapter (Chat Completions tool-call shape) |
| `mnemos-bridge-anthropic` | Anthropic SDK adapter |
| `mnemos-bridge-gemini` | Google Gemini adapter |
| `mnemos-bridge-claude-connector` | OAuth-fronted remote MCP connector for Claude |
| `mnemos-bridge-aider` | Aider IDE/CLI integration |
| `mnemos-bridge-crewai` | CrewAI tool adapter |
| `mnemos-hot-rs` | PyO3 Rust extension (hot-path scoring/embedding math) |
| `mnemos-rs` | Rust CLI client |
| `mnemosctl` | Rust admin CLI (root-level operations: sync, migration, etc.) |
| `mnemos-stiphos` | Job/hive bus service (agent registration, job claim/update, dashboard) |

A **full audit** = all of the above, at minimum. Confirmed empty/unused as
of 2026-09-08, exclude unless repopulated: `mnemos-embedkit`, `mnemos-mobile`.

## In-tree subsystems (NOT separate repos — live inside `mnemos/domain/` etc.)

Covered automatically by auditing this repo; do not go looking for a
separate deployable for these unless you have a specific reason to doubt
that:

- **GRAEAE** — multi-provider consensus/consultation engine (`mnemos/domain/graeae/`)
- **PANTHEON** — the MNEMOS LLM proxy/unified facade (`mnemos/domain/pantheon/`; `pantheon` is an empty pip-extras group, confirming zero external package dependency)
- **KRONOS** — recall-pattern observability/forecasting
- **MORPHEUS** / release-name **APOLLO S-IVB** — the dream-state synthesis pipeline (REPLAY → CLUSTER → SYNTHESISE → COMMIT)
- **PERSEPHONE** — cold-storage archival subsystem
- **CHARON** — the portability/export-import hub (`mnemos/tools/`); superseded the older **MPF** format with **MIF**
- **MOIRAI** — the compression subsystem

## Related repos with an unconfirmed relationship to mnemos core

These exist on ARGONAS under mnemos-adjacent names but their exact
relationship to the in-tree subsystems above has not been fully verified —
check whether each is a live dependency, an earlier standalone predecessor
later merged in-tree (candidate for retirement), or a genuinely separate
service, before including/excluding it from an audit:

- `charon.git` — standalone repo, has real branches; relationship to in-tree CHARON unconfirmed
- `pantheon.git` — standalone repo; relationship to in-tree PANTHEON unconfirmed
- `etlantis.git` — internal operations/telemetry registry rolling up APOLLO/KRONOS/archival worker health; referenced in mnemos docs only as in-tree function calls (`get_operation_health()`), not as an external service
- `calliope.git` — used together with ETLANTIS (per operator)
- `clio.git` — used together with ETLANTIS (per operator)
- `knemon.git` — budgeting/tokenomics system for GRAEAE/HIVE (per operator)
- `mpf.git` — **deprecated**, superseded by MIF (implemented in CHARON) — likely a retirement candidate, not an active dependency

## Explicitly NOT mnemos subsystems

Zero mentions anywhere in mnemos's own docs/deps; separate fleet tooling
that happens to share the naming neighborhood. Do not include these in a
"MNEMOS audit" scope:

- `hermes-agent` — a separate interactive CLI tool (fleet-wide, not MNEMOS-specific)
- `dashboards`, `mcp-contracts`, `mcp-stdio-rs`, `worker-hive-relay` — unconfirmed non-MNEMOS; re-verify if asked to touch them

## When asked to do a "full MNEMOS audit"

1. Read this file first.
2. Confirm the list is still accurate (repo may have been added/retired since).
3. Audit every repo in the "real MNEMOS subsystems" table, plus this repo itself.
4. For anything in "unconfirmed relationship," check live before deciding in/out of scope — don't silently skip or silently include.
5. Target GitLab's current `master` as the base for mnemos core, not a possibly-stale ARGONAS mirror.
