# Claude Code → MNEMOS

Claude Code runs the MNEMOS stdio MCP server locally and points it at your
MNEMOS instance's REST API over the network — no SSH hop required.

## Requirements

- MNEMOS bearer token (see `~/.api_keys_master.json` or your shell env)
- MNEMOS REST reachable at `http://<mnemos-host>:5002` from the machine
  running Claude Code
- `mnemos` installed and on `PATH` where Claude Code runs (`pip install
  mnemos-core` or equivalent)

## Configuration

Merge into `~/.claude.json`:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos",
      "args": ["serve", "mcp-stdio"],
      "env": {
        "MNEMOS_BASE": "http://<mnemos-host>:5002",
        "MNEMOS_API_KEY": "<your-token-here>"
      }
    }
  }
}
```

`MNEMOS_BASE` can point at `localhost:5002` (same machine) or any reachable
host — the `env` block here is passed to the locally-spawned process, so it
applies with no SSH-specific handling needed.

For a persistent, remotely-reachable connection instead of a
locally-spawned process, use the HTTP/SSE transport
(`mnemos serve mcp-http`) — see the [ChatGPT Pro Developer Mode
guide](./chatgpt-pro-developer-mode.md)'s OAuth 2.1 and bearer-token setup,
which applies to any MCP client that speaks SSE, including Claude Code.

## Notes

- Port 5002 is the unified API port for both MNEMOS and GRAEAE.
- GRAEAE reasoning is available as the `graeae_consult` MCP tool, or over REST at `POST http://<mnemos-host>:5002/v1/consultations` with a Bearer token. Both require the `graeae` extra to be installed.

## Idempotent fix script (for pre-v5.x configs)

```bash
python3 ~/.claude/scripts/fix-mnemos-mcp-auth.py
```

Safe to run multiple times.
