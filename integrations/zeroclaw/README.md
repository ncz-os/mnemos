# MNEMOS + ZeroClaw

Three artifacts. Skill (discovery), MCP (access), AGENTS.md snippet (enforcement).

## Install

1. **Copy the skill:**
   ```bash
   mkdir -p .claude/skills/mnemos-memory
   cp integrations/zeroclaw/.claude/skills/mnemos-memory/SKILL.md \
      .claude/skills/mnemos-memory/SKILL.md
   ```

2. **Drop the MCP config** into the repo root (or merge the `mcpServers` block into existing config):
   ```bash
   cp integrations/zeroclaw/mcp.example.json ./.mcp.json
   ```
   Set `MNEMOS_BASE` in `.mcp.json` to your MNEMOS deployment URL. There is no default — the example ships a placeholder.

3. **Append the enforcement snippet** to your repo's root `AGENTS.md`:
   ```bash
   cat integrations/zeroclaw/AGENTS.md.snippet >> AGENTS.md
   ```
   ZeroClaw's `CLAUDE.md` defers shared instructions to `AGENTS.md`, so the snippet lands there and applies to any agent reading the repo.

## Why all three

ZeroClaw discovers skills in `.claude/skills/` and reads MCP config on session start. That gives the agent awareness of the MNEMOS skill and access to its tools. The AGENTS.md snippet is what turns awareness into policy — without it, the skill is a hint the agent may skip under time pressure. With it, memory lookup becomes a required step before architectural decisions or workaround writes.
