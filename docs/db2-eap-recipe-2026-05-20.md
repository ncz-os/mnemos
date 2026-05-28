# Db2 12.1.5 (Early Access Program) container — local build recipe + June 6 GA repackage guide

> **⚠️ Important — IBM Db2 Early Access Program (EAP) terms apply.**
>
> This document describes a procedure for building a **local development
> container** from IBM-supplied Db2 12.1.5 AESE media, for use under your
> own IBM Db2 EAP / Passport Advantage entitlement. The procedure here
> contains **no IBM Db2 binaries, license files, or response media** — you
> must obtain those directly from IBM via the EAP / Passport Advantage
> program and supply them at build time.
>
> Before using this recipe:
> 1. Confirm you have a valid IBM Db2 EAP enrollment that permits local
>    container builds for evaluation.
> 2. Treat the resulting image as **non-redistributable** under EAP terms.
>    Do not push it to a public registry, share it cross-organization, or
>    publish performance numbers derived from it without **written IBM
>    publication approval**.
> 3. The instructions below are written to assume the standard EAP terms;
>    if your specific EAP agreement differs, defer to the agreement.
>
> The author is **not** an IBM employee, partner, or reseller; this
> recipe was developed against a personal IBM Db2 EAP enrollment for
> open-source agent-memory R&D and is provided here as a build pattern
> for other EAP participants.

---

## Why this exists

IBM publishes the official `icr.io/db2_community/db2` container only for
12.1.x GA releases. EAP participants who want a 12.1.5-vNext container
for local development must build their own image from the IBM-supplied
AESE media. This document captures one working build pattern; **the IBM
binaries themselves are not included** — bring your own under your EAP
entitlement.

When the GA equivalent of 12.1.5 ships on June 6, two paths:

| Path | What you do | When |
|---|---|---|
| **A — wait for icr.io** | Switch to `icr.io/db2_community/db2:12.1.5` once IBM publishes it (typically 2-4 weeks post-GA) | Easy, no rebuild |
| **B — repackage now** | Drop the GA tarball in, rerun the same Dockerfile, retag as `mnemos/db2:12.1.5-ga` | Same-day on June 6 |

Path B is the reason this doc exists. The Dockerfile + entrypoint + response.rsp here are **GA-agnostic** — they don't bake in anything EAP-specific.

---

## File layout

Drop all of these into the same dir on the build host:

```
db2-12.1.x/
  Dockerfile              # the recipe (below)
  response.rsp            # 5 lines, binary-only install
  entrypoint.sh           # runtime user creation + instance create + DB create
  server/                 # extracted from the IBM DB2 tarball
    db2setup
    db2/install/
    db2/license/db2adv.lic
    db2/license/db2ese.lic
    ...
```

Extract the IBM tarball into `server/` (for EAP: `db2vnext_aese_linux64.tar.gz`; for GA: `v12.1.5_linuxx64_server_dec.tar.gz` or whatever IBM ships).

---

## Build command

```bash
docker build \
  --no-cache \
  --add-host buildkitsandbox:127.0.0.1 \
  -t mnemos/db2-eap:vnext \
  .
```

For GA repackage on June 6:

```bash
docker build \
  --no-cache \
  --add-host buildkitsandbox:127.0.0.1 \
  -t mnemos/db2:12.1.5-ga \
  .
```

**Critical: `--add-host buildkitsandbox:127.0.0.1`** — `db2prereqcheck` calls `getent hosts $(hostname)` and `/etc/hosts` is read-only in BuildKit. This flag injects the entry at build time.

---

## Run command

```bash
docker run -d --name pythia-db2 \
  --privileged=true \
  --ulimit memlock=-1:-1 \
  --shm-size=2g \
  -p 50000:50000 \
  -e DBNAME=MNEMOS \
  mnemos/db2-eap:vnext
# ENABLE_ORACLE_COMPATIBILITY defaults to false as of PR #12 (2026-05-22).
# Set ENABLE_ORACLE_COMPATIBILITY=true to fall back to ORA-compat mode.
```

Container reaches "ready" in ~90-120s on first start (creates instance + database). Subsequent restarts ~10s.

---

## Dockerfile

```dockerfile
# syntax=docker/dockerfile:1.6
FROM ubuntu:22.04

ARG DB2_RESPONSE_FILE=response.rsp

ENV DEBIAN_FRONTEND=noninteractive \
    DB2INSTANCE=db2inst1 \
    DB2INST1_PASSWORD=<password> \
    DBNAME=MNEMOS \
    ENABLE_ORACLE_COMPATIBILITY=false \
    PATH=/opt/ibm/db2/V12.1/bin:/opt/ibm/db2/V12.1/adm:/opt/ibm/db2/V12.1/misc:${PATH}

SHELL ["/bin/bash", "-c"]

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        binutils \
        ca-certificates \
        file \
        ksh \
        libaio1 \
        libaio1:i386 \
        libcurl4 \
        libcurl4:i386 \
        libnsl2 \
        libnsl2:i386 \
        libnuma1 \
        libnuma1:i386 \
        libpam0g \
        libpam0g:i386 \
        libstdc++6 \
        libstdc++6:i386 \
        libxml2 \
        libxml2:i386 \
        locales \
        passwd \
        procps \
        sudo \
        tzdata \
    && locale-gen en_US.UTF-8 \
    && touch /etc/services \
    && mkdir -p /database/config /database/data \
    && ln -sf /bin/bash /bin/sh \
    && (test -x /usr/bin/ksh93 && ln -sf /usr/bin/ksh93 /bin/ksh || true) \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

COPY ${DB2_RESPONSE_FILE} /tmp/db2-response.rsp
COPY entrypoint.sh /usr/local/bin/db2-eap-entrypoint

RUN --mount=type=bind,source=server,target=/mnt/db2-server,readonly \
    set -eux; \
    test -x /mnt/db2-server/db2setup; \
    /mnt/db2-server/db2setup -r /tmp/db2-response.rsp -l /tmp/db2setup.log -t /tmp/db2setup.trc \
      || { echo "db2setup failed; trace:"; tail -n 200 /tmp/db2setup.log /tmp/db2setup.trc 2>/dev/null || true; exit 1; }; \
    test -x /opt/ibm/db2/V12.1/bin/db2; \
    chmod 0755 /usr/local/bin/db2-eap-entrypoint; \
    rm -f /tmp/db2-response.rsp /tmp/db2setup.trc; \
    rm -rf /tmp/*

COPY server/db2/license/db2adv.lic server/db2/license/db2ese.lic /opt/ibm/db2/V12.1/license/

EXPOSE 50000

HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=20 \
    CMD ["bash", "-c", "su - db2inst1 -c \"db2 connect to ${DBNAME:-MNEMOS} >/dev/null 2>&1\" || exit 1"]

ENTRYPOINT ["/usr/local/bin/db2-eap-entrypoint"]
```

---

## response.rsp (binary-only install)

```
PROD = DB2_SERVER_EDITION
FILE = /opt/ibm/db2/V12.1
INSTALL_TYPE = TYPICAL
LIC_AGREEMENT = ACCEPT
INTERACTIVE = NONE
```

**Note:** Intentionally NO `INSTANCE = DB2_INST` block. The split-install pattern installs binaries at build time, defers instance creation to entrypoint runtime. This is the whole reason the recipe works.

---

## entrypoint.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

DB2_HOME="/opt/ibm/db2/V12.1"
DB2INSTANCE="${DB2INSTANCE:-db2inst1}"
DB2FENCED_USER="${DB2FENCED_USER:-db2fenc1}"
DB2INST1_PASSWORD="${DB2INST1_PASSWORD:?DB2INST1_PASSWORD env var required — set at docker run, not in source}"
DBNAME="${DBNAME:-MNEMOS}"
ENABLE_ORACLE_COMPATIBILITY="${ENABLE_ORACLE_COMPATIBILITY:-true}"

DB2INST_HOME="/database/config/${DB2INSTANCE}"
DB2FENCED_HOME="/database/config/${DB2FENCED_USER}"
INSTANCE_MARKER="/database/config/.db2-eap-instance-created"

log() { printf '[db2-eap] %s\n' "$*"; }
run_db2() { su - "$DB2INSTANCE" -c "$*"; }
is_true() {
  case "${1,,}" in 1|true|yes|y|on) return 0 ;; *) return 1 ;; esac
}

[[ "$(id -u)" == "0" ]] || { echo "must run as root"; exit 1; }
[[ "$DBNAME" =~ ^[A-Za-z][A-Za-z0-9_]{0,7}$ ]] || { echo "DBNAME must be 1-8 chars, alphanumeric+underscore, leading letter"; exit 1; }
DBNAME="${DBNAME^^}"

mkdir -p /database/config /database/data

# At runtime hostname IS resolvable from /etc/hosts (writable here)
HOST="$(hostname)"
grep -Fqw "$HOST" /etc/hosts || printf '127.0.0.1\t%s\n' "$HOST" >> /etc/hosts

if [[ ! -f "$INSTANCE_MARKER" ]]; then
  log "initializing users and Db2 instance"

  getent group db2iadm1 >/dev/null || groupadd db2iadm1
  getent group db2fadm1 >/dev/null || groupadd db2fadm1
  id "$DB2INSTANCE" >/dev/null 2>&1 || useradd -m -d "$DB2INST_HOME" -g db2iadm1 -s /bin/bash "$DB2INSTANCE"
  id "$DB2FENCED_USER" >/dev/null 2>&1 || useradd -m -d "$DB2FENCED_HOME" -g db2fadm1 -s /bin/bash "$DB2FENCED_USER"
  echo "${DB2INSTANCE}:${DB2INST1_PASSWORD}" | chpasswd
  echo "${DB2FENCED_USER}:${DB2INST1_PASSWORD}" | chpasswd
  chown -R "${DB2INSTANCE}:db2iadm1" "$DB2INST_HOME" /database/data
  chown -R "${DB2FENCED_USER}:db2fadm1" "$DB2FENCED_HOME"

  log "applying Db2 license (advanced first, ese fallback)"
  if ! "$DB2_HOME/adm/db2licm" -a "$DB2_HOME/license/db2adv.lic"; then
    "$DB2_HOME/adm/db2licm" -a "$DB2_HOME/license/db2ese.lic" || true
  fi

  # db2icrt MUST get -nosharedgroup (or -sharedgroup) in 12.1.5+; missing it → DBI20196E
  "$DB2_HOME/instance/db2icrt" -nosharedgroup -u "$DB2FENCED_USER" "$DB2INSTANCE"

  is_true "$ENABLE_ORACLE_COMPATIBILITY" && run_db2 'db2set DB2_COMPATIBILITY_VECTOR=ORA'
  run_db2 'db2set DB2COMM=TCPIP'
  run_db2 'db2 update dbm cfg using SVCENAME 50000'

  touch "$INSTANCE_MARKER"
fi

# Idempotent per-start config (handles password rotation, etc.)
echo "${DB2INSTANCE}:${DB2INST1_PASSWORD}" | chpasswd || true
chown -R "${DB2INSTANCE}:db2iadm1" /database/data
is_true "$ENABLE_ORACLE_COMPATIBILITY" && run_db2 'db2set DB2_COMPATIBILITY_VECTOR=ORA'
run_db2 'db2set DB2COMM=TCPIP'
run_db2 'db2 update dbm cfg using SVCENAME 50000'

log "starting Db2 instance ${DB2INSTANCE}"
if ! start_output="$(run_db2 'db2start' 2>&1)"; then
  printf '%s\n' "$start_output"
  grep -q 'SQL1026N' <<<"$start_output" || exit 1   # SQL1026N = already started
fi

if ! run_db2 "db2 list db directory | grep -Eiq 'Database alias[[:space:]]+= ${DBNAME}$'"; then
  log "creating database ${DBNAME}"
  run_db2 "db2 \"CREATE DATABASE ${DBNAME} ON /database/data USING CODESET UTF-8 TERRITORY US PAGESIZE 32768\""
  run_db2 "db2 \"UPDATE DB CFG FOR ${DBNAME} USING LOGARCHMETH1 OFF\""
fi

run_db2 "db2 connect to ${DBNAME}"
run_db2 'db2 terminate'

trap 'log "stopping Db2"; run_db2 "db2stop force" || true; exit 0' TERM INT

log "ready"
while true; do sleep 3600 & wait $!; done
```

---

## The 12 build attempts + what each taught us

For posterity. Don't skip these gotchas if you re-derive the recipe.

| Round | Symptom | Root cause | Fix in recipe |
|---|---|---|---|
| R1 | `db2_install -f sysreq` busy-looped 35min | `-f sysreq` forces but never satisfies | Switch to `db2setup -r response.rsp` |
| R2 | `ureReadRspFile -1 / EXIT 87` (unknown keyword) | Hand-rolled rsp had keywords vNext schema doesn't recognise | Use `server/db2/linuxamd64/samples/db2server.rsp` as starting point |
| R3 | `DBI20192E shared group enable keyword missing` | rsp must explicitly state shared-group preference | Add `DB2_INST.ENABLE_SHARED_GROUP = NO` (now obsolete in our binary-only rsp) |
| R4 | user/group validation fail post sysreq | rsp had user but no HOME_DIRECTORY | Add explicit homes — moved to entrypoint instead |
| R5 | `./db2setup: 872: typeset: not found` | Ubuntu `/bin/sh` = dash, db2setup uses ksh built-ins | `SHELL ["/bin/bash", "-c"]` + `ln -sf /bin/bash /bin/sh` in earlier RUN layer |
| R6 | `Unknown keyword "INSTALL_TSAMP"` | vNext rsp schema removed TSAMP keyword | Drop `INSTALL_TSAMP` + `INSTALL_PCMK` from rsp |
| R7 | `/etc/hosts: Read-only file system` | BuildKit mounts /etc/hosts RO | Inject via `docker build --add-host buildkitsandbox:127.0.0.1` |
| R8 | `DB2SYSTEM=buildkitsandbox could not be added to Global Profile Registry` + `DBI1069E get_instance` | Build-time ephemeral hostname breaks per-instance profile registry write | **Move instance creation to entrypoint runtime when hostname is stable** — the whole split-install pattern |
| R9 | `COPY .../db2ese_t.lic not found` | Codex hypothesized trial-license filename (`_t.lic`); actual files are `db2ese.lic` + `db2adv.lic` | Fix COPY + db2licm paths to match what tarball ships |
| R10 | `DBT3609E libnuma.so.1 not found` | Codex's apt list dropped `libnuma1` | Re-add `libnuma1` + `libnuma1:i386` |
| R11 | clean build! 3.04 GB image | — | Run with `--privileged --ulimit memlock=-1 --shm-size=2g` |
| R12 | `DBI20196E db2icrt requires -sharedgroup OR -nosharedgroup` | vNext made the flag mandatory | Add `-nosharedgroup` to db2icrt invocation |

After R12: `db2 connect to MNEMOS`, `VALUES NVL(NULL, 42)`, `CREATE TABLE vec_test (id INTEGER, embedding VECTOR(3, FLOAT32))`, `VECTOR_DISTANCE(embedding, embedding, COSINE)` all return clean.

---

## Validation probes

After `docker run` reaches "[db2-eap] ready":

```bash
docker exec <container> su - db2inst1 -c '
db2 connect to MNEMOS
db2 "VALUES NVL(NULL, 42)"
db2 "CREATE TABLE vec_test (id INTEGER, embedding VECTOR(3, FLOAT32))"
db2 "INSERT INTO vec_test VALUES (1, VECTOR('"'"'[1.0,0.0,0.0]'"'"', 3, FLOAT32))"
db2 "SELECT VECTOR_DISTANCE(embedding, embedding, COSINE) FROM vec_test"
db2 terminate
'
```

Expected outputs:
- `42` (ORA-compat working)
- `DB20000I` after each DDL (VECTOR data type works)
- `+0.00000000000000E+000` for cosine distance of vector with itself

---

## June 6 2026 GA repackage steps

Assume IBM ships `v12.1.5_linuxx64_server_dec.tar.gz` (or similar name) on June 6.

```bash
# 1. Drop the GA tarball where the EAP one used to be
cd /build/db2-12.1.5/
rm -rf server/
mkdir server
tar -C server -xzf v12.1.5_linuxx64_server_dec.tar.gz

# 2. Verify license filenames match (db2adv.lic + db2ese.lic). If the GA
#    ships under different names (e.g. db2aese.lic), update Dockerfile COPY
#    and entrypoint.sh db2licm path lines.
ls server/db2/license/

# 3. Verify the response.rsp keywords still parse — vNext → GA may rename
#    or remove keywords. If `db2setup -r response.rsp` fails at the 1.0s
#    mark with "Unknown keyword", inspect the new sample at
#    server/db2/linuxamd64/samples/db2server.rsp.
#
# 4. Rebuild + retag:
docker build \
  --no-cache \
  --add-host buildkitsandbox:127.0.0.1 \
  -t mnemos/db2:12.1.5-ga \
  .

# 5. Test:
docker run -d --name db2-ga-test \
  --privileged=true --ulimit memlock=-1:-1 --shm-size=2g \
  -p 50002:50000 \
  -e DBNAME=MNEMOS \
  mnemos/db2:12.1.5-ga
# Note: ENABLE_ORACLE_COMPATIBILITY default is false as of PR #12 (2026-05-22)

# 6. Run the validation probes above. If they pass, retire EAP image:
docker tag mnemos/db2-eap:vnext mnemos/db2:12.1.5-eap-archive
docker rmi mnemos/db2-eap:vnext
```

Expected GA-specific differences:
- License filenames may revert to `db2aese.lic` etc — adjust 2 lines in Dockerfile COPY + entrypoint.sh
- `db2icrt -nosharedgroup` flag may or may not be required (vNext made it mandatory; GA may relax)
- Other than that, the same Dockerfile + response.rsp + entrypoint.sh should work unchanged

---

## Local backup posture

Once built, the image is your local EAP-bound artifact. Storage and
backup is the EAP participant's responsibility under their own
entitlement. **Do not** archive the built image to a shared or
publicly-accessible location, do not push to a public registry, and do
not redistribute either the image or the AESE media used to build it.

When the GA equivalent ships (June 6, 2026), repackage using the
GA-equivalent AESE tarball; the recipe in this file is GA-agnostic, so
the build steps are identical.

---

## Cross-references

- `docs/handoff-opencode-db2-sql-overrides-2026-05-20.md` — SQL override work needed to push proof from 2/6 → 6/6
- `docs/db2-port-handoff.md` — older handoff (R1-R6 attempts)
- <archived bench artifact> — signed 2/6 proof artifact on EAP
- IBM Db2 12.1 docs (response file keywords): https://www.ibm.com/docs/en/db2/12.1
- jbonhag/db2-docker #9 (Global Profile Registry trap): https://github.com/jbonhag/db2-docker/issues/9
- aeronje/ibm_db2_community_edition_linux_ubuntu (Ubuntu prereq research): https://github.com/aeronje/ibm_db2_community_edition_linux_ubuntu

---

*Recipe cracked + documented 2026-05-20 from dev-workstation Claude session. 12 build attempts in one afternoon. Don't lose this file.*
