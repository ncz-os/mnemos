# MNEMOS integrations

Drop-in bundles wiring MNEMOS memory into external agent frameworks.

Each bundle contains:

- A **skill file** teaching the agent when and how to use MNEMOS.
- A **config snippet** for the framework's MCP / hook system.
- A **README** with install steps specific to that framework.

All bundles talk to MNEMOS through the stdio MCP server at `mcp_server.py` in the repo root. **You must set `MNEMOS_BASE`** to your MNEMOS deployment's URL — there is no default. MNEMOS is network infrastructure; localhost is not a meaningful fallback.

| Bundle | Discovery | Access | Enforcement |
|--------|-----------|--------|-------------|
| `claude-code/` | skill via `.mcp.json` | MCP | **Hooks** — SessionStart/UserPromptSubmit inject memories before the model speaks (strongest: pre-inference) |
| `openclaw/` | `.agents/skills/mnemos-memory/SKILL.md` | MCP | `AGENTS.md.snippet` appended to repo root AGENTS.md (progressive-disclosure directive) |
| `zeroclaw/` | `.claude/skills/mnemos-memory/SKILL.md` | `.mcp.json` | `AGENTS.md.snippet` appended to repo root AGENTS.md |
| `hermes/` | `optional-skills/memory/mnemos/SKILL.md` | `~/.hermes/config.yaml` (`hermes mcp add`) | `hermes skills enable mnemos --global` (no required-skill tier exists) |

**Enforcement matters.** MCP + skill gives an agent *awareness* of MNEMOS. It does not compel use. Each framework's enforcement layer is what turns "the agent could query memory" into "the agent must query memory before committing to an approach." Install all three artifacts per framework.

## Deployment model

MNEMOS is designed to run as a shared network service on a dedicated host (a "memory server" the way a database server is dedicated). Every agent — Claude Code, OpenClaw, ZeroClaw, Hermes — connects to the same MNEMOS over HTTP.

Pick your deployment URL:

- **Single-process MNEMOS on its own port:** `http://mnemos.internal:5000`
- **Unified MNEMOS+GRAEAE on one port:** `http://mnemos.internal:5002`
- **Loopback-only personal dev:** `http://127.0.0.1:5000`

Use the same `MNEMOS_BASE` value everywhere. If you run team or enterprise profile, set `MNEMOS_API_KEY` too.

## Authentication

The bundled MCP config examples omit `MNEMOS_API_KEY`. For team / enterprise profiles, current `mcp_server.py` does not forward Bearer tokens. Workarounds:

1. Run the MCP server on the same host as MNEMOS, with MNEMOS bound to loopback only (`listen_host = "127.0.0.1"`). Local MCP connects without auth; external network can't reach MNEMOS directly.
2. Use SSH transport: set `command: ssh` and `args: ["user@host", "/opt/mnemos/venv/bin/python", "/opt/mnemos/mcp_server.py"]`.
3. Patch `mcp_server.py` to read `MNEMOS_API_KEY` from env and attach `Authorization: Bearer $MNEMOS_API_KEY` — tracked as a TODO; not landed.

The Claude Code hooks (bundle in `claude-code/hooks/`) already handle `MNEMOS_API_KEY` correctly — they attach the Bearer header when the key is set and omit it when unset.
