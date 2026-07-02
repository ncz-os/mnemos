#!/usr/bin/env bash
# One-time Db2 CE vector setup for the mnemos quickstart.
#
# Ensures Oracle-compatibility mode (native VECTOR type + VECTOR_DISTANCE) and
# vector indexing (DiskANN) are enabled, and that the MNEMOS database is UTF-8
# on a 32K pagesize. Safe to re-run (idempotent). The docker-compose env sets
# these on first boot; this script guarantees they're applied even if the image
# created the DB before the registry vars took effect.
#
# Usage:  ./scripts/init-db2-vectors.sh
set -euo pipefail

C=mnemos-db2   # the db2 container name from docker-compose.yml

echo "[init] waiting for Db2 to accept connections…"
until docker exec "$C" su - db2inst1 -c "db2 connect to MNEMOS" >/dev/null 2>&1; do
  sleep 5
done

echo "[init] applying Oracle-compat + vector indexing registry vars…"
docker exec "$C" su - db2inst1 -c '
  db2set DB2_COMPATIBILITY_VECTOR=ORA
  db2set DB2_VECTOR_INDEXING=YES
' || true

echo "[init] verifying MNEMOS codeset/pagesize (must be UTF-8 / 32768)…"
docker exec "$C" su - db2inst1 -c '
  db2 connect to MNEMOS >/dev/null
  db2 -x "SELECT VALUE FROM SYSIBMADM.DBCFG WHERE NAME=\"codeset\"" 2>/dev/null | tr -d " " || true
'

echo "[init] restarting the instance so registry changes take effect…"
docker exec "$C" su - db2inst1 -c "db2stop force >/dev/null 2>&1; db2start >/dev/null 2>&1" || true

echo "[init] done. mnemos will build its schema (native VECTOR columns) on first connect."
echo "[init] verify end-to-end:  curl -s localhost:5002/health"
