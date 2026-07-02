# Wiring an AI agent to mnemos (Claude Code · Cursor · Codex)

mnemos exposes its memory to agents over **MCP** via a small **stdio bridge** that talks to
the mnemos REST API (the compose stack publishes it on `http://localhost:5002`). Each agent
below spawns that bridge and gets two tools:

- **`save_memory`** — persist a memory (content + category → embedded + stored in Db2)
- **`search_memory`** — semantic recall (`semantic:true`) over the Db2 `VECTOR` index

> The bridge is `mnemos-mcp-server` (ships with mnemos; also runnable from the container).
> It reads `MNEMOS_API_URL` (default `http://localhost:5002`) and, if auth is enabled,
> `MNEMOS_API_KEY`. In this quickstart `MNEMOS_AUTH_ENABLED=false`, so no token is needed —
> **enable auth + set a token before any shared/prod use.**

Sanity-check the bridge on its own:
```bash
MNEMOS_API_URL=http://localhost:5002 mnemos-mcp-server --stdio    # speaks MCP on stdio
```

---

## Claude Code
Add an MCP server (project-scoped `.mcp.json`, or `claude mcp add`):

```jsonc
// .mcp.json  (project root)
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos-mcp-server",
      "args": ["--stdio"],
      "env": { "MNEMOS_API_URL": "http://localhost:5002" }
    }
  }
}
```
Or: `claude mcp add mnemos -- mnemos-mcp-server --stdio` (then set `MNEMOS_API_URL`).
Verify inside Claude Code with `/mcp` — you should see `mnemos` with `save_memory` /
`search_memory`.

## Cursor
`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):
```jsonc
{
  "mcpServers": {
    "mnemos": {
      "command": "mnemos-mcp-server",
      "args": ["--stdio"],
      "env": { "MNEMOS_API_URL": "http://localhost:5002" }
    }
  }
}
```
Cursor → Settings → MCP shows the server + its tools once the file is saved.

## Codex
Codex reads MCP servers from `~/.codex/config.toml` (or `codex mcp add`):
```toml
# ~/.codex/config.toml
[mcp_servers.mnemos]
command = "mnemos-mcp-server"
args = ["--stdio"]
env = { MNEMOS_API_URL = "http://localhost:5002" }
```
Or: `codex mcp add mnemos -- mnemos-mcp-server --stdio`. Confirm with `codex mcp list`.

---

## Usage pattern (all three)
Give the agent a standing instruction so it uses memory continuously, e.g.:

> Before starting non-trivial work, `search_memory` for prior context. After solving something
> non-obvious, `save_memory` (category `project`/`reference`/`feedback`) so the next session
> inherits it.

That turns Db2 CE into durable cross-session memory for your agent — the same pattern mnemos
runs in production, now free and local.

## No bridge on your PATH?
Run it straight from the mnemos container (it's in the image):
```bash
docker exec -i mnemos mnemos-mcp-server --stdio     # MNEMOS_API_URL defaults to the in-container REST
```
and set the agent's `command` to a tiny wrapper that runs that `docker exec -i …` line.
