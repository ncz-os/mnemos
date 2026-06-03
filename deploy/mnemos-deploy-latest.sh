#!/bin/bash
# mnemos-deploy-latest.sh — keep the running mnemos-api container at master HEAD.
# Builds Dockerfile.oracle from the current repo, tags mnemos-os:master-<sha> +
# :latest, recreates the compose-managed container, health-checks, and rolls back
# to the previous image if the new one fails to come up healthy.
#
# Usage:  sudo bash mnemos-deploy-latest.sh [--no-pull]
# Run from anywhere; REPO is fixed below.
set -euo pipefail

REPO=/home/jasonperlow/mnemos-prod-working
COMPOSE=/etc/mnemos/docker-compose.mnemos-api.yml
DOCKERFILE=Dockerfile.oracle
HEALTH=http://localhost:5002/health

cd "$REPO"
[ "${1:-}" = "--no-pull" ] || git pull --ff-only || echo "WARN: git pull skipped/failed; building current HEAD"
SHA=$(git rev-parse --short HEAD)
echo "==> building mnemos-os:master-$SHA from $DOCKERFILE"

# remember the currently-deployed image for rollback
PREV=$(docker inspect mnemos-api --format '{{.Config.Image}}' 2>/dev/null || echo "")
PREV_RESOLVED=$(docker inspect "$PREV" --format '{{.Id}}' 2>/dev/null || echo "")

DOCKER_BUILDKIT=1 docker build -f "$DOCKERFILE" \
  -t "mnemos-os:master-$SHA" -t mnemos-os:latest \
  --label git_commit="$SHA" .

echo "==> recreating compose container on mnemos-os:latest"
docker-compose -f "$COMPOSE" up -d

echo "==> health check"
ok=0
for i in $(seq 1 15); do
  if curl -fs -m 5 "$HEALTH" >/dev/null 2>&1; then ok=1; break; fi
  sleep 4
done

if [ "$ok" = 1 ]; then
  echo "==> HEALTHY on mnemos-os:master-$SHA (latest)"
  docker inspect mnemos-api --format 'deployed image={{.Config.Image}} git={{index .Config.Labels "git_commit"}}'
else
  echo "!! NEW IMAGE UNHEALTHY — rolling back to $PREV"
  if [ -n "$PREV_RESOLVED" ]; then
    docker tag "$PREV_RESOLVED" mnemos-os:latest
    docker-compose -f "$COMPOSE" up -d
    echo "rolled back to $PREV"
  else
    echo "no previous image recorded — manual recovery needed"
  fi
  exit 1
fi
