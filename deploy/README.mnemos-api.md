# mnemos-api deployment (PYTHIA)

Compose-managed (was an orphan `docker run`). Image built from `Dockerfile.oracle`.

- Compose: `/etc/mnemos/docker-compose.mnemos-api.yml` (copy here = `deploy/docker-compose.mnemos-api.yml`)
- Secrets/env: `/etc/mnemos/mnemos-api.env` (root 600, NOT committed)
- Image: `mnemos-os:latest` == `mnemos-os:master-<sha>` (label `git_commit`)
- Network: host. Volume: `mnemos-api-data` (external) -> /data. Bind: api_keys.json.

## Keep latest ready / deploy a new build
    sudo mnemos-deploy-latest.sh          # pull master, build, recreate, health-check, auto-rollback
    sudo docker-compose -f /etc/mnemos/docker-compose.mnemos-api.yml up -d   # just recreate on current :latest

## Rollback (manual)
    sudo docker tag mnemos-os:master-904b35a1 mnemos-os:latest
    sudo docker-compose -f /etc/mnemos/docker-compose.mnemos-api.yml up -d
