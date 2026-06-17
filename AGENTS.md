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
  db2:      { driver: ibm_db,   extra: db2,    dsn: "db2://USER:PASS@HOST:50000/DB", arch: [amd64] }
  mysql:    { driver: aiomysql, extra: mysql,  dsn: "mysql://USER:PASS@HOST:3306/DB", arch: [amd64, arm64] }

accelerators:       # OPTIONAL embedder accel; default is portable CPU llama-cpp
  openvino: { extra: openvino, arch: [amd64] }          # Intel x86-only
  cuda:     { extra: cuda,     arch: [amd64, arm64] }    # needs PyTorch CUDA index
  amd:      { extra: amd,      arch: [amd64] }           # ROCm, linux-only

images:
  mnemos-core:       { ref: "ghcr.io/ncz-os/mnemos-core",       contains: [core],                              arch: [amd64, arm64], port: 5002 }
  mnemos:            { ref: "ghcr.io/ncz-os/mnemos",            contains: [core, graeae, pantheon, knemon, charon], arch: [amd64, arm64], port: 5002, canonical_everything: true }
  mnemos-enterprise: { ref: "ghcr.io/ncz-os/mnemos-enterprise", contains: [core, graeae, pantheon, knemon, charon, oracle, db2, mysql], arch: [amd64], port: 5002 }
  mnemos-stiphos:    { ref: "ghcr.io/ncz-os/mnemos-stiphos",    contains: [stiphos],                           arch: [amd64, arm64], port: 8080 }
```

---

## Decision procedure

Given `requested` (a set of module ids) and `backend` (one backend id) and
`deploy` (`container` | `pip`) and `arch` (`amd64` | `arm64`):

```
1. VALIDATE ARCH
   - if backend.arch excludes arch  -> ERROR: backend unsupported on arch
     (notably: db2 is amd64-only)
   - if any requested accelerator excludes arch -> drop it + warn
   - NEVER select mnemos-enterprise on arm64.

2. IF deploy == container:
   a. needs_enterprise = backend in {db2}  OR  (backend in {oracle,mysql} AND operator wants the driver baked)
   b. if "stiphos" in requested -> ALSO deploy image mnemos-stiphos (separate container)
   c. choose primary image:
        - requested ⊆ {core}                          -> mnemos-core
        - requested ⊆ {core,graeae,pantheon,knemon,charon} AND not needs_enterprise -> mnemos   (everything)
        - needs_enterprise                             -> mnemos-enterprise   (requires arch == amd64)
   d. run: docker run -p PORT:PORT -v mnemos-data:/data \
            -e MNEMOS_DATABASE_DSN='<backends[backend].dsn>' <image.ref>:latest
        (omit the -e line to use the default SQLite backend)

3. IF deploy == pip:
   - extras = [ modules[m].extra for m in requested if m not in {core,stiphos} and extra ]
            + [ backends[backend].extra if present ]
   - if arch == arm64: ensure 'openvino' is NOT in extras; prefer extra "server" over "full".
   - pip install 'mnemos-core[<comma-joined extras>]'
   - if "stiphos" in requested: ALSO pip install 'mnemos-stiphos[mcp]' and run it as a separate service.
```

---

## Canonical recipes

```bash
# Everything, SQLite, any arch (turnkey)
docker run -p 5002:5002 -v mnemos-data:/data ghcr.io/ncz-os/mnemos:latest

# Everything on PostgreSQL
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='postgres://mnemos:pass@db:5432/mnemos' \
  ghcr.io/ncz-os/mnemos:latest

# Everything on Oracle (thin) — no enterprise image needed
docker run -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='oracle://MNEMOS:pass@ora:1521/ORCLPDB1' \
  ghcr.io/ncz-os/mnemos:latest

# Db2 (amd64) — enterprise image required
docker run --platform linux/amd64 -p 5002:5002 \
  -e MNEMOS_DATABASE_DSN='db2://MNEMOS:pass@db2:50000/MNEMOS' \
  ghcr.io/ncz-os/mnemos-enterprise:latest

# Hive service (separate container, runs alongside any of the above)
docker run -p 8080:8080 -v stiphos-data:/data ghcr.io/ncz-os/mnemos-stiphos:latest

# pip: kernel + reasoning + routing, arm64-safe (no openvino)
pip install 'mnemos-core[graeae,knemon,pantheon,charon]'

# pip: everything + enterprise drivers (amd64)
pip install 'mnemos-core[full,enterprise]'

# Custom container: FROM core + only what you want
#   FROM ghcr.io/ncz-os/mnemos-core:latest
#   RUN pip install --no-cache-dir mnemos-graeae mnemos-charon
```

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
- `mnemos-enterprise` is amd64-only. Do not deploy it on arm64; use `mnemos` + Oracle-thin/Postgres/MySQL instead.
- STIPHOS is a separate service/image; never expect it on port 5002 or inside the everything image.
- On arm64, never install the `openvino` accelerator or the `full` extra that pulls it.
