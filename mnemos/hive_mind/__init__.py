"""GRAEAE Hive Mind — agent coordination + triage queue + cost-tier dispatch.

Part of the MNEMOS family (sister to memory + GRAEAE consensus).
Folded into mnemos-production tree 2026-05-23 from standalone graeae-hive-mind v0.2.0.

Modules:
- service        — FastAPI app exposing /v1/agents/*, /v1/jobs/*, /v1/messages/*, /v1/events, /v1/stats/costs
- mcp_bridge     — SSE MCP server wrapping Hive + MNEMOS + GRAEAE as MCP tools
- worker_template — DEPRECATED: legacy goose-worker daemon reference (goose retired 2026-05-25; kept as archival diff)

Identity: urn:agent:<kind>:<host>:<session_uuid>
Roles: claude-code/human/mnemos = orchestrators (submit+claim); opencode/codex/hermes/claw-family/ic-engine = workers (claim only)
Cost tiers: A=free (NGC + local), B=cheap (Groq/xAI/DeepSeek-direct/Together/Gemini-Flash), C=reserve (Anthropic/OpenAI-GPT5.5/Gemini-Pro)
"""
__version__ = "0.2.0"
