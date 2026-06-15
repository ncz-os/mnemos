#!/usr/bin/env sh
set -eu

: "${WEB_CONCURRENCY:=4}"
: "${PANTHEON_BIND:=127.0.0.1:4110}"
: "${MNEMOS_PANTHEON_ENABLED:=true}"
: "${MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT:=600/minute}"
: "${MNEMOS_NATS_URL:?MNEMOS_NATS_URL must point at the shared NATS state bus}"

export MNEMOS_PANTHEON_ENABLED
export MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT
export MNEMOS_NATS_URL

exec gunicorn \
  mnemos.api.pantheon_shadow:app \
  -k uvicorn.workers.UvicornWorker \
  -w "${WEB_CONCURRENCY}" \
  -b "${PANTHEON_BIND}" \
  --access-logfile - \
  --error-logfile - \
  --forwarded-allow-ips 127.0.0.1
