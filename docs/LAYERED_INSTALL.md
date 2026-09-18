# MNEMOS Layered Architecture — Distributions, Images, Backends

**Status:** current (split-distribution model). Supersedes the earlier
monorepo-extras scaffold.

MNEMOS is layered along three **orthogonal** axes. You select each
independently.

## Axis 1 — Core and external feature layers

External subsystems use separate pip distributions on the shared `mnemos.*`
PEP 420 namespace. CHARON portability, migration, ingest, and STYX moved into
core on 2026-09-18 and their routes are always mounted. Docling remains an
optional dependency because its conversion stack is large.

| Distribution | Layer | Depends on |
|---|---|---|
| `mnemos-core` | kernel, CHARON MIF/MPF portability, migrate-in adapters, ingest, STYX | — |
| `mnemos-graeae` | GRAEAE multi-muse reasoning bus | core |
| `mnemos-pantheon` | PANTHEON model catalog/facade | core |
| `mnemos-knemon` | KNEMON cost/model routing + usage ledger | core |
| `mnemos-stiphos` | **STIPHOS hive** — agent coordination, job queue, cost-tier dispatch (beta) | core |

**STIPHOS is a separate *service*, not a router** — its own ASGI app and port
(8080), so it is not part of the `mnemos` everything image. Deploy it as its own
container/process.

Dependency direction is enforced at runtime (the layer validators) and at
install time (extras chaining in `pyproject.toml`).

## Axis 2 — Published image (OCI layering)

One image is published. The build is still layered — each stage is built `FROM`
the one above, so the heavy base (llama-cpp-python compile + baked GGUF
embedder) is built once — but the intermediate stages never reach the registry,
so the layering is a build-time optimisation rather than a pull-time one.

```
core layer (Dockerfile.core)              kernel + CHARON/STYX             build-context only
  └─ everything layer (Dockerfile.everything)  + graeae+pantheon+knemon     build-context only
       └─ ghcr.io/ncz-os/mnemos-enterprise     + Oracle/Db2/MySQL          amd64 + arm64  ← the ONLY published image
```

Only the final enterprise image is pushed. The core and everything layers are
passed between BuildKit Bake targets as in-memory named contexts and are never
pushed or tagged as their own packages; `ghcr.io/ncz-os/mnemos-core` and
`ghcr.io/ncz-os/mnemos` exist in the registry but stopped being published at
6.2.5. STIPHOS has no container image at all -- it is pip-only
(`mnemos-stiphos`) and runs as its own service on port 8080.

Why not a separate "core+graeae" image tier? graeae/pantheon/knemon all
mount into the **one** `mnemos.api.main:app` process and are runtime-gated, so a
separate image just toggles routers that are already lazy. graeae is the heavy
one; the other three are nearly free. The only real image boundaries are:
kernel/CHARON/STYX · full-API · separate hive service · heavy enterprise drivers.

Build sources: `Dockerfile.core`, `Dockerfile.everything`, `Dockerfile.enterprise`
(core repo). Published by `.github/workflows/release-images.yml`, whose own
header calls `mnemos-enterprise` "the one supported MNEMOS container package".

### Multi-arch notes

- `mnemos-enterprise` is multi-arch (`amd64` + `arm64`) — one OCI index, not
  two images; `docker`/`podman pull` resolves the platform layer.
- `llama-cpp-python` is the only base dep that compiles from source — it builds
  per-arch (AVX on amd64, NEON on arm64). Everything else ships aarch64 wheels.
- The everything image installs add-on wheels **explicitly**, never via
  `mnemos-core[full]`, because `full` no longer pulls the Intel-only
  `openvino` accelerator (x86-only). Accelerators are host-opt-in.
- **Db2 is the only amd64 restriction**: `ibm_db` has no reliable arm64 wheel,
  so Db2 support is present on the amd64 layer only. Oracle (thin), MySQL and
  MariaDB work on both arches. Other arches (`ppc64le`, `s390x`) are a
  sponsor-provided-CI request.

## Axis 3 — Storage backend (runtime, behind EPIMONE)

The persistence layer (**EPIMONE**, `mnemos/persistence/`) is a single
`abc.ABC` contract with swappable backends. The backend is chosen at **runtime**
by `MNEMOS_DATABASE_DSN` — not by a separate image. SQLite is the portable
default baked into every image.

| Backend | Driver | In which image |
|---|---|---|
| SQLite + sqlite-vec | bundled | all (default) |
| PostgreSQL + pgvector | `asyncpg` (bundled) | all |
| Oracle Database 26ai | `oracledb` (thin) | `mnemos-enterprise`, or a pip install plus `pip install oracledb` |
| IBM Db2 12.1.5 | `ibm_db` | `mnemos-enterprise` |
| MySQL 9.0+ | `aiomysql` | `mnemos-enterprise` |

Backend × layer support is gated honestly: `assert_backend_supports_layers()`
fails fast at startup if an enabled layer needs a capability the chosen backend
lacks.

## Picking a deployment

| You want | Use |
|---|---|
| Minimal memory kernel, edge/embedded | `pip install 'mnemos-core[sqlite]'` |
| Full agent stack, any arch | `mnemos-enterprise` image, or `pip install 'mnemos-core[server]'` |
| Full stack on Oracle/Db2/MySQL | `mnemos-enterprise` image |
| Fleet coordination / job queue | `pip install mnemos-stiphos` (alongside the above) |
| A custom subset | `pip install` the dists you want onto `mnemos-core` |

See [INSTALL.md](INSTALL.md) for commands and [../AGENTS.md](../AGENTS.md) for
the machine-readable agent install matrix.
