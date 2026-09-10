# Codex CLI -> MNEMOS

Codex CLI 0.125.0 and newer can register MNEMOS as an MCP server with `codex mcp add` or a `~/.codex/config.toml` block.

## What you need — token, host (<mnemos-host>), relevant port(s)

- Codex CLI `0.125.0` or newer.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- MNEMOS REST reachable at `http://<mnemos-host>:5002`.
- The `mnemos` CLI installed on the same machine as Codex CLI.
- Config path: `~/.codex/config.toml`.
- Optional HTTP/SSE MCP bridge reachable at `http://<mnemos-host>:5003/sse`.
- A new Codex process after config changes.
- Shell access to run `codex mcp add`.
- `jq` available for the verification command.
- A non-root MNEMOS token if you want namespace enforcement.

## Configuration — copy-paste-runnable code block; use $MNEMOS_TOKEN placeholder (never the live token)

> Set MNEMOS_TOKEN from ~/.api_keys_master.json or source your shell env.

Register with the CLI first. This writes the MCP server entry for you on
Codex CLI builds that support `codex mcp add`.

```bash
export MNEMOS_TOKEN="${MNEMOS_TOKEN:?set MNEMOS_TOKEN first}"
codex mcp add mnemos \
  --env MNEMOS_BASE=http://<mnemos-host>:5002 \
  --env MNEMOS_API_KEY="$MNEMOS_TOKEN" \
  -- mnemos serve mcp-stdio
```

The equivalent `~/.codex/config.toml` block for Codex `0.125.0+` is:

```toml
[mcp_servers.mnemos]
command = "mnemos"
args = ["serve", "mcp-stdio"]

[mcp_servers.mnemos.env]
MNEMOS_BASE = "http://<mnemos-host>:5002"
MNEMOS_API_KEY = "$MNEMOS_TOKEN"
```

Some pre-release Codex builds used the older table name
`[mcp.servers.mnemos]`. If your local `codex --version` is older than
`0.125.0`, upgrade before debugging the table spelling.

For HTTP/SSE, use a URL registration when the Codex build supports remote
MCP transport:

```toml
[mcp_servers.mnemos-sse]
url = "http://<mnemos-host>:5003/sse"

[mcp_servers.mnemos-sse.headers]
Authorization = "Bearer $MNEMOS_TOKEN"
```

Restart Codex after editing the TOML file. `codex exec` inherits the same
MCP registration as interactive sessions.

## Setup — remote OAuth 2.1 gateway (Codex on another machine, or ChatGPT desktop/mobile)

For Codex CLI/desktop on a *different* machine from MNEMOS, or for ChatGPT's
own desktop/mobile apps, use the same OAuth 2.1 MCP gateway documented in
[ChatGPT Pro Developer Mode](./chatgpt-pro-developer-mode.md#setup--oauth-21-path)
rather than the local stdio path above. Same MCP edge, same 25-tool registry,
same underlying MNEMOS memory — the difference is transport (stdio child
process vs. HTTPS to a public MCP endpoint) and auth (a static bearer token
vs. a full OAuth 2.1 authorization-code + PKCE flow with dynamic client
registration and short-lived JWTs).

**Prerequisite: Developer Mode.** Registering a remote MCP server — the
"connector"/"MCP registry" concept in both ChatGPT and Codex — requires
Developer Mode enabled on your OpenAI account (Pro/Team/Enterprise/Edu tier):
ChatGPT → Settings → Connectors → Advanced → Enable Developer Mode. Codex CLI
and Codex desktop consume the same account entitlement, so this is a
one-time, account-wide toggle, not something you configure per-client.

Bring up the OAuth-enabled MCP edge and expose it publicly:

```bash
export MNEMOS_OAUTH_ISSUER="https://mnemos.example.com"
export MNEMOS_OAUTH_ADMIN_PASSPHRASE="$(openssl rand -hex 32)"
export MNEMOS_DATABASE_DSN="sqlite:////data/mnemos.db"
mnemos serve mcp-http --host 0.0.0.0 --port 5004
```

Then expose port 5004 over HTTPS. Two tunnel options, operator-verified
against real ChatGPT and Codex clients:

- **Cloudflare Tunnel** (recommended — stable URL, no rotating link to
  re-paste):
  ```bash
  cloudflared tunnel login
  cloudflared tunnel create mnemos
  cloudflared tunnel route dns mnemos mnemos.yourdomain.com
  cloudflared tunnel run --url http://<mnemos-host>:5004 mnemos
  ```
- **ngrok** (faster to try, URL rotates on the free tier):
  ```bash
  ngrok config add-authtoken <your-ngrok-authtoken-from-dashboard>
  ngrok http http://<mnemos-host>:5004
  ```

Register the connector: ChatGPT → Settings → Connectors → Add custom (URL =
`https://<tunnel-url>/sse`); Codex follows the equivalent remote-MCP add flow
in its own connector/registry UI once Developer Mode is on. See the full
walkthrough — architecture diagram, verification curls, troubleshooting — in
[ChatGPT Pro Developer Mode](./chatgpt-pro-developer-mode.md).

## Verification — one curl or one tool-list call that proves registration worked

```bash
codex mcp list | grep -i mnemos
```

If your Codex build lacks `codex mcp list`, start an interactive session
and use its MCP or tool-list command to confirm the `mnemos` server loaded.

## Common gotchas — 2-4 bullets of real failure modes

- Codex versions before `0.125.0` do not understand MCP server config.
- TOML table spelling changed across early builds; prefer `codex mcp add`
  when available.
- Shell variables are not expanded inside TOML by every launcher; use the
  CLI registration path or write the resolved token into a private config.
- Long-running MNEMOS operations can hit Codex tool timeouts; keep write
  approvals manual for bulk operations.
