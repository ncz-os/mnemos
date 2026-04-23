# MNEMOS + OpenClaw

Three artifacts. Skill (discovery), MCP (access), AGENTS.md snippet (enforcement).

## Install

1. **Copy the skill:**
   ```bash
   mkdir -p .agents/skills/mnemos-memory
   cp integrations/openclaw/.agents/skills/mnemos-memory/SKILL.md \
      .agents/skills/mnemos-memory/SKILL.md
   ```

2. **Add the MCP server block** to your OpenClaw config (`.agents/mcp.json` or equivalent; check your project's convention):
   ```bash
   cat integrations/openclaw/mcp.example.json
   ```
   Set `MNEMOS_BASE` in the env to your MNEMOS instance (e.g. `http://mnemos.internal:5002`). There is no default.

3. **Append the enforcement snippet** to your repo's root `AGENTS.md`:
   ```bash
   cat integrations/openclaw/AGENTS.md.snippet >> AGENTS.md
   ```
   This is what actually makes the agent USE the skill. Without it you have discovery and access but no enforcement — the skill is only a soft hint to the model.

## Why all three

OpenClaw's enforcement model is AGENTS.md (progressive disclosure — root AGENTS.md + nested per-dir). The skill describes *how* to use MNEMOS; the AGENTS.md snippet tells the agent *when it must*. MCP provides the wire.

If you skip step 3, the MNEMOS skill is available but the agent may skip it for short-path reasoning. The snippet changes that default.

## Notes

OpenClaw's MCP config lives alongside the `.agents/` skills tree; check the most recent OpenClaw release for exact config file name if this snippet doesn't drop in as-is.
