# PANTHEON production worker-pool cutover

This directory contains **ops artifacts only** for moving the stable `:4100`
VIP from the single-process shadow/dev launch to a production PANTHEON worker
pool behind Caddy.

Current dev launch:

```sh
uvicorn mnemos.api.pantheon_shadow:app --port 4101
```

Production launch:

```sh
gunicorn mnemos.api.pantheon_shadow:app \
  -k uvicorn.workers.UvicornWorker \
  -w "${WEB_CONCURRENCY}" \
  -b 127.0.0.1:4110
```

Caddy remains the public `:4100` VIP. Gunicorn listens on loopback
`127.0.0.1:4110`, so the VIP address and client endpoint stay stable during
cutover and rollback.

## Files

- `pantheon-gunicorn.sh` — validated shell launcher for the worker pool.
- `pantheon-gunicorn.env.example` — environment template for
  `/etc/mnemos/pantheon-gateway.env`.
- `pantheon-gateway.service` — systemd unit for the worker pool.
- `Caddyfile.pantheon-vip-4100.snippet` — review-only Caddy site snippet for
  the `:4100` VIP. It is intentionally not the live Caddy config.

## Shared state requirements

Do not run multiple PANTHEON workers until the NATS shared-state job is live and
all workers use the same bus:

```env
MNEMOS_NATS_URL=nats://127.0.0.1:4222
MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT=600/minute
```

Keep `MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT` identical across the pool. For HTTP
rate-limit counters, use a shared `limits` storage backend instead of
`memory://`; the example uses Redis:

```env
RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/2
RATE_LIMIT_TRUST_PROXY=true
```

`RATE_LIMIT_TRUST_PROXY=true` is safe only when Caddy is the sole ingress and
strips/replaces client-supplied forwarding headers.

## Install / start

```sh
sudo install -d -o mnemos -g mnemos /opt/mnemos
sudo rsync -a --delete ./ /opt/mnemos/
sudo install -m 600 deploy/pantheon/pantheon-gunicorn.env.example \
  /etc/mnemos/pantheon-gateway.env
sudo install -m 0644 deploy/pantheon/pantheon-gateway.service \
  /etc/systemd/system/pantheon-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now pantheon-gateway.service
curl -fsS http://127.0.0.1:4110/health
```

Tune `/etc/mnemos/pantheon-gateway.env` before enabling the service, especially
`WEB_CONCURRENCY`, `MNEMOS_NATS_URL`, auth settings, and the shared rate-limit
storage URI.

## VIP-stable cutover

1. Start and health-check `pantheon-gateway.service` on `127.0.0.1:4110`.
2. Confirm NATS shared state and the configured rate limit are present in every
   worker environment.
3. Review `Caddyfile.pantheon-vip-4100.snippet` against the live Caddy site.
4. During the window, change only the live `:4100` upstream to
   `127.0.0.1:4110`, then reload Caddy.
5. Validate `curl -fsS http://127.0.0.1:4100/health` and an OpenAI-compatible
   `/v1/models` request through the VIP.

## Rollback

One-line rollback: point the live Caddy `reverse_proxy` upstream back to
`inference-api` (the pre-cutover upstream) and reload Caddy.

Leave `pantheon-gateway.service` running during rollback if you want a fast
re-attempt; stop it only after the VIP is confirmed back on the old upstream.
