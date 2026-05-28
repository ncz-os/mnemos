# GRAEAE Hive Mind

Fleet-wide agent coordination + triage queue + cost-tier dispatch.

Part of MNEMOS. Sister to memory + GRAEAE consensus reasoning.

## Quick reference

| Concern | Where |
|---|---|
| REST service | `service.py` (FastAPI single file, ~600 LOC) |
| MCP-SSE bridge | `mcp_bridge.py` (exposes hive + mnemos + graeae as MCP tools) |
| Worker daemon template | `worker_template.py` (Python; ports trivial to bash/node) |
| Schema migrations | `../../db/migrations_v6_0_hive_mind.sql` (PG) and `../../db/migrations_oracle/0010_hive_mind.sql` |

## Roles

- **Orchestrators** (submit + claim): `claude-code`, `claude-cli`, `human`, `mnemos`
- **Workers** (claim-only — 403 if they POST jobs): `opencode`, `goose`, `codex`, `hermes`, `zeroclaw`, `openclaw`, `ic-engine`

## Cost tiers (per `~/.claude/rules/llm-usage-policy-2026-05-22.md`)

- **A** = FREE: ngc, nvidia, local-llamacpp, local-vllm, ollama (try first)
- **B** = CHEAP PAID: groq, xai, deepseek-direct, together, gemini-flash, openai-mini, perplexity
- **C** = RESERVE: anthropic, openai-pro/gpt55, gemini-pro, together-pro (only when explicitly authorized)

Jobs default `max_cost_tier="A"`. Workers above the cap get HTTP 204 "no eligible work" — token-miser by design.

## Phase 1 (current, v0.2.0)

- SQLite WAL backend (single-writer ceiling ~50 agents)
- SSE + in-process broadcast pub/sub
- Minimal MCP shim + proper SSE bridge for opencode/Claude-Desktop
- Standalone FastAPI service (deployment target: HYDRA today, fold into mnemos-api Phase 2)

## Phase 2 (deferred per Nemotron advisory)

- PostgreSQL backend with LISTEN/NOTIFY for SSE push (unblocks ~50 → ~500 agents)
- NATS pub/sub layer (eliminates heartbeat-table writes, O(1) sub fan-out)
- Full mcp-server-sdk integration (replace shim entirely)
- Per-agent inbox subscription
- Job DAG with depends_on[]
- Result cache + capability scoring (killer feature — 30-70% LLM spend cut on repetitive workloads)
- Web dashboard

## Deployment as standalone (current)

```
apt install -y python3-fastapi python3-uvicorn python3-aiosqlite python3-sse-starlette python3-pydantic
cp service.py /srv/agent-bus/agent_bus.py
# systemd unit at ../../ops/systemd/graeae-hive.service
systemctl enable --now graeae-hive.service
curl http://localhost:5005/health
```

## Deployment folded into mnemos-api (Phase 2)

`mnemos/api/main.py` should add:
```python
from mnemos.hive_mind.service import app as hive_app
app.mount("/hive", hive_app)
```
And the migrations land via mnemos-installer.

## License

Apache 2.0 (matches MCP ecosystem).
