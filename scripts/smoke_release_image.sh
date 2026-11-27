#!/bin/bash
set -euo pipefail
image=$1
architecture=$2
docker pull "$image"
test "$(docker image inspect --format '{{.Architecture}}' "$image")" = "$architecture"
container=$(docker run -d -e MNEMOS_DATABASE_DSN=sqlite:////data/smoke.db "$image")
trap 'docker rm -f "$container" >/dev/null' EXIT
ready=false
for attempt in {1..60}; do
    if docker exec "$container" python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:5002/health", timeout=2)' >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 2
done
[ "$ready" = true ]
docker exec "$container" mnemos doctor
docker exec "$container" python -c 'import mnemos.domain.graeae.engine; import mnemos.api.main; import oracledb; import aiomysql; import asyncpg'
docker exec "$container" python -c 'import importlib.util, sys; assert bool(importlib.util.find_spec("ibm_db")) == (sys.argv[1] == "amd64")' "$architecture"
