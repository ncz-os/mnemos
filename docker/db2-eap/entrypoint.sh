#!/usr/bin/env bash
set -euo pipefail

DB2_HOME="/opt/ibm/db2/V12.1"
DB2INSTANCE="${DB2INSTANCE:-db2inst1}"
DB2FENCED_USER="${DB2FENCED_USER:-db2fenc1}"
DB2INST1_PASSWORD="${DB2INST1_PASSWORD:-mnemos_dev}"
DBNAME="${DBNAME:-MNEMOS}"
# Db2 native is the default as of PR #12 (2026-05-22). MNEMOS ships a
# fully native Db2 backend (Memory + Federation repos closed out per
# PRs #1-9; migration DDL native per PR #10). Set ENABLE_ORACLE_COMPATIBILITY=true
# to fall back to ORA-compat mode (e.g. for older clients running pre-PR-1
# repository overrides). The toggle stays for emergency fallback.
ENABLE_ORACLE_COMPATIBILITY="${ENABLE_ORACLE_COMPATIBILITY:-false}"

DB2INST_HOME="/database/config/${DB2INSTANCE}"
DB2FENCED_HOME="/database/config/${DB2FENCED_USER}"
INSTANCE_MARKER="/database/config/.db2-eap-instance-created"

log() {
  printf '[db2-eap] %s\n' "$*"
}

run_db2() {
  su - "$DB2INSTANCE" -c "$*"
}

is_true() {
  case "${1,,}" in
    1|true|yes|y|on) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ "$(id -u)" != "0" ]]; then
  echo "This entrypoint must run as root so it can initialize Db2." >&2
  exit 1
fi

if [[ ! "$DBNAME" =~ ^[A-Za-z][A-Za-z0-9_]{0,7}$ ]]; then
  echo "DBNAME must be a valid Db2 database name: 1-8 chars, starting with a letter." >&2
  exit 1
fi
DBNAME="${DBNAME^^}"

mkdir -p /database/config /database/data

HOST="$(hostname)"
if ! grep -Fqw "$HOST" /etc/hosts; then
  printf '127.0.0.1\t%s\n' "$HOST" >> /etc/hosts
fi

if [[ ! -f "$INSTANCE_MARKER" ]]; then
  log "initializing users and Db2 instance"

  getent group db2iadm1 >/dev/null || groupadd db2iadm1
  getent group db2fadm1 >/dev/null || groupadd db2fadm1

  if ! id "$DB2INSTANCE" >/dev/null 2>&1; then
    useradd -m -d "$DB2INST_HOME" -g db2iadm1 -s /bin/bash "$DB2INSTANCE"
  fi
  if ! id "$DB2FENCED_USER" >/dev/null 2>&1; then
    useradd -m -d "$DB2FENCED_HOME" -g db2fadm1 -s /bin/bash "$DB2FENCED_USER"
  fi

  echo "${DB2INSTANCE}:${DB2INST1_PASSWORD}" | chpasswd
  echo "${DB2FENCED_USER}:${DB2INST1_PASSWORD}" | chpasswd
  chown -R "${DB2INSTANCE}:db2iadm1" "$DB2INST_HOME" /database/data
  chown -R "${DB2FENCED_USER}:db2fadm1" "$DB2FENCED_HOME"

  log "applying Db2 EAP license"
  if ! "$DB2_HOME/adm/db2licm" -a "$DB2_HOME/license/db2adv.lic"; then
    "$DB2_HOME/adm/db2licm" -a "$DB2_HOME/license/db2ese.lic" || true
  fi

  "$DB2_HOME/instance/db2icrt" -nosharedgroup -u "$DB2FENCED_USER" "$DB2INSTANCE"

  if is_true "$ENABLE_ORACLE_COMPATIBILITY"; then
    run_db2 'db2set DB2_COMPATIBILITY_VECTOR=ORA'
  fi
  run_db2 'db2set DB2COMM=TCPIP'
  run_db2 'db2 update dbm cfg using SVCENAME 50000'

  touch "$INSTANCE_MARKER"
fi

echo "${DB2INSTANCE}:${DB2INST1_PASSWORD}" | chpasswd || true
chown -R "${DB2INSTANCE}:db2iadm1" /database/data

if is_true "$ENABLE_ORACLE_COMPATIBILITY"; then
  run_db2 'db2set DB2_COMPATIBILITY_VECTOR=ORA'
fi
run_db2 'db2set DB2COMM=TCPIP'
run_db2 'db2 update dbm cfg using SVCENAME 50000'

log "starting Db2 instance ${DB2INSTANCE}"
if ! start_output="$(run_db2 'db2start' 2>&1)"; then
  printf '%s\n' "$start_output"
  if ! grep -q 'SQL1026N' <<<"$start_output"; then
    exit 1
  fi
fi

if ! run_db2 "db2 list db directory | grep -Eiq 'Database alias[[:space:]]+= ${DBNAME}$'"; then
  log "creating database ${DBNAME}"
  run_db2 "db2 \"CREATE DATABASE ${DBNAME} ON /database/data USING CODESET UTF-8 TERRITORY US PAGESIZE 32768\""
  run_db2 "db2 \"UPDATE DB CFG FOR ${DBNAME} USING LOGARCHMETH1 OFF\""
  if is_true "$ENABLE_ORACLE_COMPATIBILITY"; then
    run_db2 "db2 \"UPDATE DB CFG FOR ${DBNAME} USING ORA_COMPATIBILITY ON\"" || true
  fi
fi

run_db2 "db2 connect to ${DBNAME}"
run_db2 'db2 terminate'

stop_db2() {
  log "stopping Db2"
  run_db2 'db2stop force' || true
}

trap 'stop_db2; exit 0' TERM INT

log "ready"
while true; do
  sleep 3600 &
  wait $!
done
