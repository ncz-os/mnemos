# AGENTS.md — machine-readable install guide for MNEMOS

This file lets an automated agent install/deploy MNEMOS for an operator given a
**requested set of modules**, deterministically. Human guide: [docs/INSTALL.md](docs/INSTALL.md).

The pip package is `mnemos-core`. `mnemos` is an **image** name, not a pip
package — never run `pip install mnemos` or `pip install mnemos-os`.

---

## Module registry

```yaml
# id -> how to obtain it. Subsystems share the mnemos.* namespace and are
# runtime-gated: installing the dist mounts the routes; absence => HTTP 503.
modules:
  core:       { dist: mnemos-core,     extra: null,       kind: kernel,   arch: [amd64, arm64] }
  graeae:     { dist: mnemos-graeae,   extra: graeae,     kind: router,   arch: [amd64, arm64] }
  pantheon:   { dist: mnemos-pantheon, extra: pantheon,   kind: router,   arch: [amd64, arm64] }
  knemon:     { dist: mnemos-knemon,   extra: knemon,     kind: router,   arch: [amd64, arm64] }
  charon:     { dist: mnemos-charon,   extra: charon,     kind: router,   arch: [amd64, arm64] }
  stiphos:    { dist: mnemos-stiphos,  extra: null,       kind: service,  arch: [amd64, arm64], port: 8080, note: "separate service, not in the everything image" }

backends:           # selected at RUNTIME via MNEMOS_DATABASE_DSN, not by image
  sqlite:   { driver: bundled,  dsn: "sqlite:////data/mnemos.db", arch: [amd64, arm64], default: true }
  postgres: { driver: asyncpg,  dsn: "postgres://USER:PASS@HOST:5432/DB", arch: [amd64, arm64] }
  oracle:   { driver: oracledb, extra: oracle, dsn: "oracle://USER:PASS@HOST:1521/SERVICE", arch: [amd64, arm64], thin: true }
  db2:      { driver: ibm_db,   extra: db2,    dsn: "db2://USER:PASS@HOST:50000/DB", arch: [amd64], note: "no arm64 Linux wheel exists and the sdist fails to build there; every other backend, including oracle, is arm64-clean (verified 2026-09-10, O6N, incl. a live query against a real production Oracle DB)" }
  mysql:    { driver: aiomysql, extra: mysql,  dsn: "mysql://USER:PASS@HOST:3306/DB", arch: [amd64, arm64] }
  mariadb:  { driver: aiomysql, extra: mysql,  dsn: "mariadb://USER:PASS@HOST:3306/DB", arch: [amd64, arm64] }

accelerators:       # OPTIONAL embedder accel; default is portable CPU llama-cpp
  openvino: { extra: openvino, arch: [amd64] }          # Intel x86-only
  cuda:     { extra: cuda,     arch: [amd64, arm64] }    # needs PyTorch CUDA index
  amd:      { extra: amd,      arch: [amd64] }           # ROCm, linux-only

images:
  mnemos-enterprise: { ref: "ghcr.io/ncz-os/mnemos-enterprise", contains: [core, graeae, pantheon, knemon, charon, oracle, mysql], contains_amd64_only: [db2], arch: [amd64, arm64], port: 5002, note: "the ONLY published container image (operator directive 2026-09-10) — a single multi-arch OCI manifest under one tag, not two images; docker/podman pull automatically resolves the right platform layer. The mysql driver (aiomysql) also serves the mariadb backend. Db2 support is present on the amd64 layer only — see the db2 backend note above." }
```

`mnemos-core`, `mnemos`, and `mnemos-stiphos` container images were retired
2026-09-10 (operator directive: one image, one version, fleet-wide — no
image-variant sprawl). The `mnemos-core` **pip package** still exists and is
unaffected; only the separately-published *container images* were removed.
`stiphos` has no container image at all now — it is pip-only (see below).

`mnemos-enterprise` itself is a single multi-arch manifest (amd64 + arm64)
under one tag as of 2026-09-10 — this is standard OCI tooling (the same
mechanism `postgres:16`/`python:3.11-slim` use), not two separate images.
`docker pull`/`podman pull ghcr.io/ncz-os/mnemos-enterprise:latest` transparently
fetches the layer matching the puller's own architecture. The one asymmetry:
the arm64 layer omits the Db2 driver (`ibm_db` has no arm64 Linux wheel and
its sdist build fails there) — every other backend, including Oracle, is
identical on both architectures.

---

## Decision procedure

Given `requested` (a set of module ids) and `backend` (one backend id) and
`deploy` (`container` | `pip`) and `arch` (`amd64` | `arm64`):

```
1. VALIDATE ARCH
   - if backend.arch excludes arch  -> ERROR: backend unsupported on arch
     (notably: db2 is amd64-only — including inside the container, see step 2)
   - if any requested accelerator excludes arch -> drop it + warn
   - deploy == container works on BOTH amd64 and arm64; there is no arch
     restriction on choosing the container path itself.

2. IF deploy == container (any arch):
   a. image = mnemos-enterprise. There is no other choice — it contains
      every module (core, graeae, pantheon, knemon, charon) and every
      backend driver except Db2 on arm64 (oracle and mysql/mariadb are
      present on both arches), so it is always correct regardless of
      `requested`/`backend`, including plain SQLite.
   b. if backend == db2 and arch == arm64 -> ERROR: the Db2 driver is
      amd64-only even inside this image (no arm64 Linux wheel exists).
      Either pick a different backend or deploy on amd64.
   c. if "stiphos" in requested -> pip-install it as a SEPARATE service
      alongside the container (no stiphos image exists — see step 3's
      stiphos line, which applies here too).
   d. run: docker run -p 5002:5002 -v mnemos-data:/data \
            -e MNEMOS_DATABASE_DSN='<backends[backend].dsn>' \
            ghcr.io/ncz-os/mnemos-enterprise:latest
        (omit the -e line to use the default SQLite backend; the same
        command works unmodified on amd64 or arm64 — docker/podman resolve
        the right platform layer automatically)

3. IF deploy == pip (bare metal / no-container, any arch):
   - extras = [ modules[m].extra for m in requested if m not in {core,stiphos} and extra ]
            + [ backends[backend].extra if present ]
   - if arch == arm64: ensure 'openvino' is NOT in extras; prefer extra "server", which does not pull openvino.
   - pip install 'mnemos-core[<comma-joined extras>]'
   - if "stiphos" in requested: ALSO pip install 'mnemos-stiphos[mcp]' and run it as a separate service.
```

---

## Canonical recipes

```bash
# Everything, SQLite (amd64 or arm64 — same command either way; docker
# resolves the right platform layer automatically):
docker run -p 5002:5002 -v mnemos-data:/data ghcr.io/ncz-os/mnemos-enterprise:latest

# Everything on PostgreSQL
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='postgres://mnemos:pass@db:5432/mnemos' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# Everything on Oracle (thin) — works on amd64 and arm64
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='oracle://MNEMOS:pass@ora:1521/ORCLPDB1' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# Db2 — amd64 ONLY, even in this container (no arm64 Linux wheel for ibm_db)
docker run --platform linux/amd64 -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='db2://MNEMOS:pass@db2:50000/MNEMOS' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# MariaDB 11.7+ (aiomysql driver also serves this) — amd64 and arm64
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='mariadb://mnemos:pass@mariadb:3306/mnemos' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# Bare metal / no-container install (any arch) — pip, not a container.
# kernel + reasoning + routing, arm64-safe (no openvino):
pip install 'mnemos-core[graeae,knemon,pantheon,charon]'

# pip: everything + enterprise drivers (any arch except db2, which needs amd64)
pip install 'mnemos-core[full,enterprise]'

# Hive service (stiphos) — pip-only, no container image exists. Run
# alongside a container deployment above or a bare pip install, either way.
pip install 'mnemos-stiphos[mcp]'
```

---

## Client connectivity (ask the operator)

After the module/image/backend is deployed, ASK the operator how they want
to connect their AI clients before finishing setup — do not silently pick
one. Two modes, not mutually exclusive:

1. **LAN / local stdio MCP bridge** — the client and MNEMOS are on the same
   machine or LAN, no public exposure needed. Covers Claude Code, Claude
   Desktop, Cursor, Codex CLI, and Codex desktop when it's on the same
   network as MNEMOS. The client spawns `mnemos serve mcp-stdio` as a child
   process; a bearer token is the only credential. See
   [docs/connectors/README.md — "If you already have MNEMOS running locally"](docs/connectors/README.md#if-you-already-have-mnemos-running-locally-and-just-want-stdio-mcp).

2. **Remote OAuth 2.1 gateway** — for ChatGPT (web/mobile/desktop) or Codex
   away from the LAN. Requires **Developer Mode** enabled on the operator's
   OpenAI account (ChatGPT → Settings → Connectors → Advanced → Enable
   Developer Mode — a one-time, account-wide toggle Codex shares), a public
   HTTPS endpoint (Cloudflare Tunnel — operator-verified with ChatGPT and
   Codex, stable URL, API-scriptable — or ngrok, faster to try but the free
   tier rotates the URL), and `MNEMOS_OAUTH_ISSUER` /
   `MNEMOS_OAUTH_ADMIN_PASSPHRASE` / `MNEMOS_DATABASE_DSN` set before
   starting `mnemos serve mcp-http`. Full walkthrough:
   [docs/connectors/chatgpt-pro-developer-mode.md](docs/connectors/chatgpt-pro-developer-mode.md),
   Codex-specific notes in
   [docs/connectors/codex-cli.md](docs/connectors/codex-cli.md).

Ask which mode(s) the operator wants, then follow only the relevant guide —
don't stand up a public tunnel for an operator who only wanted local stdio.

---

## Verification

```bash
docker exec <ctr> mnemos doctor        # lists installed extras/bundles + backend health
curl -fsS http://localhost:5002/health # everything/core/enterprise
curl -fsS http://localhost:8080/health # stiphos
```

`mnemos doctor` is the source of truth for what is actually installed and which
backend is live. Routes for absent subsystems return HTTP 503 with the exact
`pip install` command to enable them.

---

## Hard rules

- `mnemos-core` is the only pip-installable base; `mnemos`/`mnemos-os` are NOT pip packages.
- Backend = runtime `MNEMOS_DATABASE_DSN`, never a rebuild.
- `mnemos-enterprise` is the ONLY published container image (operator directive
  2026-09-10 — one image, one version, fleet-wide). It is a single multi-arch
  manifest (amd64 + arm64) under one tag — `docker pull` on either arch
  resolves the correct layer automatically. There is no lean/core/everything
  image anymore, and no separate enterprise-only tier to opt out of — every
  container deployment gets the full driver set (Oracle, MySQL/MariaDB,
  Postgres, SQLite on both arches; Db2 on amd64 only) regardless of which
  backend you actually run.
- Db2 needs amd64 specifically — `ibm_db` has no arm64 Linux wheel and its
  sdist build fails there (verified 2026-09-10). Everything else in the image
  is arch-symmetric. `docker run --platform linux/amd64 ...` if you need to
  force the amd64 layer from an arm64 host for a Db2 deployment.
- Anyone who wants a smaller footprint than the full enterprise image, or to
  run on bare metal without a container at all: use the `pip` path
  (`mnemos-core[...]`). This is the ONLY way to run MNEMOS without the full
  driver set, on any arch — it is not an arm64-specific fallback anymore.
- STIPHOS has no container image at all; it is pip-only (`mnemos-stiphos`),
  run as a separate service alongside any deployment. Never expect it on
  port 5002.
- On arm64, never install the `openvino` accelerator. The `full` extra does not pull it.
- MariaDB has no extra of its own: it is wire-compatible with `aiomysql`, so
  install the `mysql` extra and use a `mariadb://` DSN.
- Keep exactly ONE version tag live on ghcr.io/ncz-os/mnemos-enterprise at a
  time (plus its `latest`/`sha-*` aliases). When cutting a new release, the
  old version's package version is deleted from ghcr.io, not just superseded
  by a new tag — see `.github/workflows/release-images.yml` (or wire this
  into it if it isn't automated yet).
