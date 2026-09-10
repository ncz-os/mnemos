# ChatGPT Pro Developer Mode → MNEMOS

> **Status: experimental.** See [README.md](./README.md) for the
> stability framing. This recipe targets developers and power users
> running their own MNEMOS instance who want ChatGPT Pro to read and
> write the same memory their other agents use.

## What this gets you

ChatGPT remembers across conversations by querying *your* MNEMOS
instance for relevant memories at prompt-time and writing new ones
when you ask it to. Same memory backs Claude Desktop, Cursor, Codex
CLI, and any other MCP-aware client. No data goes to OpenAI's memory
service; everything stays in your MNEMOS.

## Prerequisites

- A ChatGPT subscription on **Pro, Team, Enterprise, or Edu** tier.
  Developer Mode connectors are not available on free or Plus.
- Developer Mode enabled on your account: ChatGPT → Settings →
  Connectors → Advanced → Enable Developer Mode.
- A running MNEMOS instance you control (see `DEPLOYMENT.md`).
- A way to expose MNEMOS over HTTPS to the public internet. Options:
  - **ngrok** (easiest): free tier rotates the URL on every restart;
    the $10/mo tier gives a stable subdomain.
  - **Cloudflare Named Tunnel** (free, stable URL, requires owning a
    domain you manage in Cloudflare).
  - **Tailscale Funnel** (free, stable URL on `*.ts.net`, requires a
    Tailnet — which you might already have).
  - **Your own reverse proxy + DNS** (Caddy, nginx, etc.) — most
    control, most setup work.

## Architecture

```
┌────────────────────┐                ┌──────────────────┐
│  ChatGPT Pro web   │                │  MNEMOS          │
│                    │                │                  │
│  Custom Connector  │  HTTPS  ───▶   │  mnemos-mcp-http │
│  Bearer auth       │  /sse          │  :5004 (SSE)     │
└────────────────────┘                │                  │
                                       │  ↓               │
                                       │  Postgres + KG   │
                                       │  + MORPHEUS etc. │
                                       └──────────────────┘
                  via tunnel: ngrok / cloudflared / Tailscale
```

The MCP HTTP/SSE bridge (`mnemos serve mcp-http`) shares the exact same
25 tool definitions as the stdio MCP server. A memory written from Claude
Desktop is queryable from ChatGPT and vice versa.

## Setup — OAuth 2.1 path

The MCP edge now exposes OAuth 2.1 discovery, dynamic client registration,
authorization-code + PKCE S256, refresh tokens, and bearer-JWT validation.
Dynamic client registration is intentionally public, as required by automatic
MCP clients; authorization still requires the admin passphrase and registration
is bounded by request-size, redirect-count, and per-address rate limits.

Configure the public issuer and the same database DSN used by the node before
starting the edge (SQLite shown; Oracle, Postgres, MySQL/MariaDB and Db2 also work):

```bash
export MNEMOS_OAUTH_ISSUER="https://mnemos.example.com"
export MNEMOS_OAUTH_ADMIN_PASSPHRASE="$(openssl rand -hex 32)"
export MNEMOS_DATABASE_DSN="sqlite:////data/mnemos.db"
```

Treat the admin passphrase as a credential: keep it out of URLs, command
history, access logs, and checked-in environment files. The authorization UI
submits it only in the POST body.

Backend startup provisions OAuth tables. Leave `MNEMOS_OAUTH_SIGNING_KEY`
unset to generate `secrets.token_urlsafe(32)` on first boot and persist it in
that same database on every supported backend. Concurrent first boots reuse
the first persisted key. An explicit signing key overrides the stored key.
Clients, authorization codes and refresh tokens also survive restarts.

`MNEMOS_OAUTH_DATABASE_URL` is no longer supported: unset it and configure
`MNEMOS_DATABASE_DSN`. Startup rejects the old variable with a migration error;
it does not silently select another database. Existing Postgres OAuth tables
are reused when the shared DSN points to their database. If the old OAuth
store used a separate database, its clients/tokens/key must be migrated before
switching DSNs to preserve existing authorizations.

## Setup — legacy static bearer path

### 1. Bring up the MCP HTTP/SSE bridge

Add to your `docker-compose.override.yml` (<pg-host> prod example):

```yaml
services:
  mnemos-mcp-http:
    image: ghcr.io/ncz-os/mnemos-enterprise:6.2.4
    pull_policy: never
    depends_on:
      - mnemos
    restart: unless-stopped
    command: ["mnemos", "serve", "mcp-http", "--host", "0.0.0.0", "--port", "5004"]
    ports:
      - "5004:5004"
    environment:
      MNEMOS_MCP_TOKENS: "alice:${MNEMOS_MCP_TOKEN}:${MNEMOS_API_KEY}"
      MNEMOS_BASE: "http://mnemos:5002"
      MNEMOS_API_KEY: "<your existing MNEMOS bearer>"
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Generate a bearer token (this is what ChatGPT will send on every request).
`MNEMOS_MCP_TOKENS` maps that edge token to a backend MNEMOS API key; the
legacy single-user `MNEMOS_MCP_TOKEN` mode still exists but shares one backend
principal for every client:

```bash
export MNEMOS_MCP_TOKEN="$(openssl rand -hex 32)"
```

Bring up the service:

```bash
docker-compose up -d --build mnemos-mcp-http
```

Verify:

```bash
curl http://localhost:5004/healthz
# → ok

curl http://localhost:5004/sse  # without auth
# → 401 with WWW-Authenticate: Bearer realm="mnemos-mcp"

curl -H "Authorization: Bearer $MNEMOS_MCP_TOKEN" \
     http://localhost:5004/sse
# → 200 with content-type: text/event-stream
```

### 2. Open a tunnel

**ngrok (default recommendation):**

```bash
# One-time setup if you haven't:
brew install ngrok        # or: snap install ngrok
ngrok config add-authtoken <your-ngrok-authtoken-from-dashboard>

# Each session:
ngrok http http://<mnemos-host>:5004
```

ngrok prints something like:

```
Forwarding   https://abc-123.ngrok-free.app → http://<mnemos-host>:5004
```

The `https://abc-123.ngrok-free.app` is your public connector URL.
On free tier this rotates every restart. On paid tier you can pin a
subdomain with `--domain=mnemos.ngrok.app`.

**Cloudflare Tunnel (stable URL, free):**

```bash
cloudflared tunnel login                # one-time, requires domain in CF
cloudflared tunnel create mnemos
cloudflared tunnel route dns mnemos mnemos.yourdomain.com
cloudflared tunnel run --url http://<mnemos-host>:5004 mnemos
```

Resulting URL: `https://mnemos.yourdomain.com`.

*API auto-provisioning (skips the interactive `cloudflared tunnel login`).*
Cloudflare's Tunnel API can create the tunnel and its DNS record directly,
useful for scripted/headless setup. Requires a Cloudflare API token with
`Cloudflare Tunnel:Edit` + `DNS:Edit` on the target zone, plus your account
and zone IDs (Cloudflare dashboard → right sidebar of any domain overview):

```bash
export CF_API_TOKEN="<cloudflare-api-token>"
export CF_ACCOUNT_ID="<account-id>"
export CF_ZONE_ID="<zone-id-for-yourdomain.com>"
TUNNEL_SECRET="$(openssl rand -base64 32)"

# 1. Create the tunnel
tunnel_json=$(curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/cfd_tunnel" \
  -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" \
  --data "{\"name\":\"mnemos\",\"tunnel_secret\":\"$TUNNEL_SECRET\"}")
TUNNEL_ID=$(echo "$tunnel_json" | jq -r '.result.id')

# 2. Point mnemos.yourdomain.com at it (CNAME to <tunnel_id>.cfargotunnel.com)
curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/dns_records" \
  -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" \
  --data "{\"type\":\"CNAME\",\"name\":\"mnemos\",\"content\":\"${TUNNEL_ID}.cfargotunnel.com\",\"proxied\":true}"

# 3. Fetch the run token and start cloudflared with it (no interactive login)
TUNNEL_TOKEN=$(curl -sS \
  "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/token" \
  -H "Authorization: Bearer $CF_API_TOKEN" | jq -r '.result')
cloudflared tunnel --url http://<mnemos-host>:5004 run --token "$TUNNEL_TOKEN"
```

Same resulting URL. Treat `CF_API_TOKEN` and `TUNNEL_SECRET` as credentials —
same handling as `MNEMOS_OAUTH_ADMIN_PASSPHRASE` above.

**Tailscale Funnel:**

```bash
tailscale funnel 5004
```

Resulting URL: `https://<your-machine>.<tailnet>.ts.net`.

### 3. Register the Custom Connector in ChatGPT

ChatGPT → Settings → Developer Mode → Connectors → Add custom

| Field | Value |
|---|---|
| Name | `MNEMOS` |
| Connector URL | `https://abc-123.ngrok-free.app/sse` |
| Authentication | `Bearer Token` |
| Token | `$MNEMOS_MCP_TOKEN` (the value you generated) |
| Description | `Memory across conversations` (whatever you want) |

Click Save. ChatGPT will hit the URL, complete the SSE handshake, and
list the available tools (search_memories, create_memory, get_memory,
list_memories, kg_create_triple, kg_search, DAG tools, recommend_model, etc. —
all 18).

### 4. Use it

In a new ChatGPT conversation, the MNEMOS connector is auto-available.
Ask things like:

- "Search my memory for anything about pgvector benchmarks"
- "Remember that APOLLO logs a warning when LLM fallback is enabled without a judge"
- "What did I decide about the modularization charter?"

ChatGPT calls MNEMOS's MCP tools and folds the results into the
conversation. Same memory is visible from your other agents.

## Setup — assisted path (experimental helper, currently inert)

The `mnemos-tunnel-setup` helper (`scripts/mnemos_tunnel_setup.py`) is
checked in as an aspirational contract. It expects a daemon-side
`/admin/tunnels/*` REST surface plus a `mnemos.tunnels.ngrok_bridge`
module; **neither has shipped**.
Use the manual path above. The snippet below describes the eventual
flow:

```bash
mnemos-tunnel-setup chatgpt
```

The script walks you through ngrok signup, opens the tunnel, generates the
token, prints the connector config, and copies URL+token to your clipboard.

## Operational notes

- **Token rotation**: change `MNEMOS_MCP_TOKEN`, restart the
  `mnemos-mcp-http` service, update the connector in ChatGPT.
  No coordination with stdio agents needed — they don't use this token.
- **Per-user token model**: prefer `MNEMOS_MCP_TOKENS` so each connector
  bearer maps to a backend user/API key. The legacy `MNEMOS_MCP_TOKEN`
  mode is single-user only; every client sharing it writes as the same
  backend principal.
- **Audit trail**: every connector-driven write goes through
  `/v1/memories` exactly like a normal API call, so the version DAG,
  webhooks, MORPHEUS run tagging, and federation all observe it.
- **Latency**: tunnel hop adds ~30-100 ms depending on geography.
  ChatGPT issues tool calls in parallel; aggregate impact is usually
  invisible.

## Known caveats

- **ngrok free tier URL rotates**: re-paste into ChatGPT after every
  ngrok restart. Use the paid tier or Cloudflare Named Tunnel to fix.
- **TLS is the tunnel's responsibility**: `mnemos serve mcp-http` listens
  on plain HTTP. Never bind it to a public IP without the tunnel
  providing TLS termination.
- **Legacy bearer tokens remain supported**: anyone with a configured static
  token can read/write as its mapped backend principal. OAuth JWTs and static
  tokens pass through the same MCP authorization and audit pipeline.
- **Connector tool list updates require reconnect**: if MNEMOS adds
  new tools (e.g., MORPHEUS slice 2 endpoints), ChatGPT may need the
  connector removed and re-added to pick them up.

## Troubleshooting

**ChatGPT shows "couldn't reach connector":**
- Check `curl https://your-tunnel-url/healthz` returns `ok`
- Check `curl -H "Authorization: Bearer $MNEMOS_MCP_TOKEN"
  https://your-tunnel-url/sse` returns 200
- Check the tunnel is still alive (`ngrok` ctrl-C kills it)

**ChatGPT can reach connector but tools don't appear:**
- Look at `docker logs mnemos-v3x-mnemos-mcp-http-1` for SSE handshake
  errors
- Verify the underlying MNEMOS REST API is healthy (the bridge
  delegates everything to it):
  `curl http://localhost:5002/health`

**Bearer token mismatches:**
- The `WWW-Authenticate` header on 401 responses confirms the bridge
  is enforcing bearer auth correctly.
- If ChatGPT specifically shows "401 unauthorized" — the token in
  ChatGPT's connector config doesn't match `$MNEMOS_MCP_TOKEN`. Re-
  copy/paste, or regenerate and update both sides.
