# MNEMOS Layered Architecture — Distributions, Images, Backends

**Status:** current (split-distribution model). Supersedes the earlier
monorepo-extras scaffold.

MNEMOS is layered along three **orthogonal** axes. You select each
independently.

## Axis 1 — Feature layers (separate distributions, runtime-gated)

Every subsystem is its own pip distribution on the shared `mnemos.*` PEP 420
namespace. Core never hard-imports them; routes/MCP tools/workers are mounted
only when the dist is present (`mnemos.core.extras` presence probes +
`_include_optional_router`). A missing subsystem returns HTTP 503 with its exact
install command — never an `ImportError`.

| Distribution | Layer | Depends on |
|---|---|---|
| `mnemos-core` | kernel: EPIMONE persistence, API runtime, MCP, embedder | — |
| `mnemos-graeae` | GRAEAE multi-muse reasoning bus | core |
| `mnemos-pantheon` | PANTHEON model catalog/facade | core |
| `mnemos-knemon` | KNEMON cost/model routing + usage ledger | core |
| `mnemos-charon` | CHARON portability (MPF, migrate-in, Docling) | core |
| `mnemos-stiphos` | **STIPHOS hive** — agent coordination, job queue, cost-tier dispatch (beta) | core |

**STIPHOS is a separate *service*, not a router** — its own ASGI app and port
(8080), so it is not part of the `mnemos` everything image. Deploy it as its own
container/process.

Dependency direction is enforced at runtime (the layer validators) and at
install time (extras chaining in `pyproject.toml`).

## Axis 2 — Published images (OCI layering)

The image matrix is a small, principled set — **not** a combinatorial matrix.
Each image is built `FROM` the one above, so the heavy base (llama-cpp-python
compile + baked GGUF embedder) is built once and shared via registry layer
dedup.

```
ghcr.io/ncz-os/mnemos-core           kernel                       amd64 + arm64
        └─ ghcr.io/ncz-os/mnemos      + graeae+pantheon+knemon+charon  amd64 + arm64   ← canonical "everything"
              └─ ghcr.io/ncz-os/mnemos-enterprise  + Oracle/Db2/MySQL  amd64 ONLY

ghcr.io/ncz-os/mnemos-stiphos        hive service (FROM core)     amd64 + arm64
```

Why not a separate "core+graeae" image tier? graeae/pantheon/knemon/charon all
mount into the **one** `mnemos.api.main:app` process and are runtime-gated, so a
separate image just toggles routers that are already lazy. graeae is the heavy
one; the other three are nearly free. The only real image boundaries are:
kernel · full-API · separate hive service · heavy enterprise drivers.

Build sources: `Dockerfile.core`, `Dockerfile.everything`, `Dockerfile.enterprise`
(core repo) and `Dockerfile` (mnemos-stiphos repo). Published by
`.github/workflows/release-images.yml`.

### Multi-arch notes

- `mnemos-core` / `mnemos` / `mnemos-stiphos` are multi-arch (`amd64` + `arm64`).
- `llama-cpp-python` is the only base dep that compiles from source — it builds
  per-arch (AVX on amd64, NEON on arm64). Everything else ships aarch64 wheels.
- The everything image installs add-on wheels **explicitly**, never via
  `mnemos-core[full]`, because `full` no longer pulls the Intel-only
  `openvino` accelerator (x86-only). Accelerators are host-opt-in.
- `mnemos-enterprise` is **amd64-only** (`ibm_db` has no reliable arm64 wheel;
  enterprise big iron is x86). Other arches are a sponsor-provided-CI request.

## Axis 3 — Storage backend (runtime, behind EPIMONE)

The persistence layer (**EPIMONE**, `mnemos/persistence/`) is a single
`abc.ABC` contract with swappable backends. The backend is chosen at **runtime**
by `MNEMOS_DATABASE_DSN` — not by a separate image. SQLite is the portable
default baked into every image.

| Backend | Driver | In which image |
|---|---|---|
| SQLite + sqlite-vec | bundled | all (default) |
| PostgreSQL + pgvector | `asyncpg` (bundled) | all |
| Oracle Database 26ai | `oracledb` (thin) | `mnemos-enterprise`, or `mnemos` + `pip install oracledb` |
| IBM Db2 12.1.5 | `ibm_db` | `mnemos-enterprise` |
| MySQL 9.0+ | `aiomysql` | `mnemos-enterprise` |

Backend × layer support is gated honestly: `assert_backend_supports_layers()`
fails fast at startup if an enabled layer needs a capability the chosen backend
lacks.

## Picking a deployment

| You want | Use |
|---|---|
| Minimal memory kernel, edge/embedded | `mnemos-core` image, or `pip install 'mnemos-core[sqlite]'` |
| Full agent stack, any arch | `mnemos` image, or `pip install 'mnemos-core[server]'` |
| Full stack on Oracle/Db2/MySQL | `mnemos-enterprise` image |
| Fleet coordination / job queue | `mnemos-stiphos` image (alongside the above) |
| A custom subset | `FROM ghcr.io/ncz-os/mnemos-core` + `pip install` the dists you want |

See [INSTALL.md](INSTALL.md) for commands and [../AGENTS.md](../AGENTS.md) for
the machine-readable agent install matrix.
