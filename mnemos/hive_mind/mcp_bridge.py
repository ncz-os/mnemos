"""
GRAEAE Hive Mind — MCP SSE bridge.

Exposes Hive (HYDRA :5005) + MNEMOS REST (PYTHIA :5002) as a single MCP server
on HYDRA :5006/sse. Speaks proper SSE MCP transport for opencode/Claude-Desktop.

Endpoint matrix:
  /sse  : SSE GET — server→client event stream
  /messages : POST — client→server JSON-RPC

Tools:
  hive.agent_list           -> GET /v1/agents on HIVE
  hive.agent_register       -> POST /v1/agents/register
  hive.job_create           -> POST /v1/jobs
  hive.job_next             -> POST /v1/jobs/next?agent_urn=
  hive.job_list             -> GET /v1/jobs
  hive.job_update           -> PATCH /v1/jobs/{id}
  hive.message_publish      -> POST /v1/messages
  hive.message_list         -> GET /v1/messages
  mnemos.memory_search      -> POST /memories/search on PYTHIA
  mnemos.memory_create      -> POST /memories
  mnemos.memory_get         -> GET /memories/{id}
"""

from __future__ import annotations
import json
import os
from typing import Any

import httpx
import uvicorn
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent, Resource, Prompt
from starlette.applications import Starlette
from starlette.routing import Mount, Route

HIVE_URL = os.environ.get("HIVE_URL", "http://127.0.0.1:5005")
MNEMOS_URL = os.environ.get("MNEMOS_URL", "http://192.168.207.67:5002")
MNEMOS_TOKEN = os.environ.get("MNEMOS_TOKEN", "d3a3bc609583005f4a077b6ffd00154b4f03f70104d0cdbfbb019fceb28daca9")

server = Server("graeae-hive-mind")
client = httpx.AsyncClient(timeout=15.0)


async def _hive(method: str, path: str, **kw) -> dict:
    r = await client.request(method, f"{HIVE_URL}{path}", **kw)
    r.raise_for_status()
    if r.status_code == 204:
        return {"_status": 204, "_note": "no eligible job"}
    return r.json()


async def _mnemos(method: str, path: str, **kw) -> dict:
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {MNEMOS_TOKEN}"
    r = await client.request(method, f"{MNEMOS_URL}{path}", headers=headers, **kw)
    r.raise_for_status()
    return r.json()


# ---------- tool definitions ----------


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [
        Tool(
            name="hive.agent_list",
            description="List registered agents in the GRAEAE Hive Mind.",
            inputSchema={"type": "object", "properties": {"kind": {"type": "string"}, "status": {"type": "string"}}},
        ),
        Tool(
            name="hive.agent_register",
            description="Register this session as an agent in the Hive. Returns urn + session_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "claude/goose/opencode/codex/zeroclaw/openclaw/hermes/ic-engine/mnemos/human",
                    },
                    "host": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "version": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["kind", "host"],
            },
        ),
        Tool(
            name="hive.job_create",
            description="Submit work into the Hive Mind triage queue. Eligible agents self-claim.",
            inputSchema={
                "type": "object",
                "properties": {
                    "submitter_urn": {"type": "string"},
                    "kind": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "integer", "default": 0},
                    "required_capabilities": {"type": "array", "items": {"type": "string"}},
                    "eligible_kinds": {"type": "array", "items": {"type": "string"}},
                    "deadline": {"type": "number"},
                    "parent_job_id": {"type": "string"},
                },
                "required": ["submitter_urn", "kind"],
            },
        ),
        Tool(
            name="hive.job_next",
            description="Atomic dequeue: claim highest-priority eligible job for this agent. Returns job or {204: no work}.",
            inputSchema={"type": "object", "properties": {"agent_urn": {"type": "string"}}, "required": ["agent_urn"]},
        ),
        Tool(
            name="hive.job_list",
            description="List jobs in the Hive. Filter by status/agent/since.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "agent_urn": {"type": "string"},
                    "since": {"type": "number"},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        ),
        Tool(
            name="hive.job_update",
            description="Update job status/result. Status: queued/claimed/running/done/failed/cancelled.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "result": {"type": "object"},
                    "claimed_by": {"type": "string"},
                },
                "required": ["id", "status"],
            },
        ),
        Tool(
            name="hive.message_publish",
            description="Publish a Hive message. to_urn=null for broadcast.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_urn": {"type": "string"},
                    "to_urn": {"type": "string"},
                    "topic": {"type": "string"},
                    "payload": {"type": "object"},
                    "in_reply_to": {"type": "string"},
                },
                "required": ["from_urn", "topic", "payload"],
            },
        ),
        Tool(
            name="hive.message_list",
            description="List Hive messages. Filter by recipient/topic.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to_urn": {"type": "string"},
                    "topic": {"type": "string"},
                    "since": {"type": "number"},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        ),
        Tool(
            name="mnemos.memory_search",
            description="Search MNEMOS memory (PYTHIA). Semantic+keyword.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "min_score": {"type": "number"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="mnemos.memory_create",
            description="Save a memory to MNEMOS. Category: infrastructure/solutions/patterns/decisions/projects/standards.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "category": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["content", "category"],
            },
        ),
        Tool(
            name="mnemos.memory_get",
            description="Get a MNEMOS memory by id (mem_XXX).",
            inputSchema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ),
        Tool(
            name="graeae.consult",
            description="Submit a multi-LLM consensus consultation to GRAEAE (PYTHIA). Modes: auto/single/all/debate/majority.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "task_type": {
                        "type": "string",
                        "description": "reasoning/architecture_design/code_generation/web_search",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "local", "external", "all", "single", "debate", "majority"],
                        "default": "auto",
                    },
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "explicit model list (overrides mode)",
                    },
                    "limit_chars": {"type": "integer"},
                    "format": {"type": "string", "default": "full"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="graeae.muses",
            description="List available GRAEAE muses (LLM providers + models).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="graeae.modes",
            description="List GRAEAE consultation modes + their descriptions.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="graeae.get",
            description="Get a previous GRAEAE consultation by id.",
            inputSchema={
                "type": "object",
                "properties": {"consultation_id": {"type": "string"}},
                "required": ["consultation_id"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, args: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "hive.agent_list":
            r = await _hive("GET", "/v1/agents", params={k: v for k, v in args.items() if v})
        elif name == "hive.agent_register":
            r = await _hive("POST", "/v1/agents/register", json=args)
        elif name == "hive.job_create":
            r = await _hive("POST", "/v1/jobs", json=args)
        elif name == "hive.job_next":
            r = await _hive("POST", f"/v1/jobs/next", params={"agent_urn": args["agent_urn"]})
        elif name == "hive.job_list":
            r = await _hive("GET", "/v1/jobs", params={k: v for k, v in args.items() if v})
        elif name == "hive.job_update":
            jid = args.pop("id")
            r = await _hive("PATCH", f"/v1/jobs/{jid}", json=args)
        elif name == "hive.message_publish":
            r = await _hive("POST", "/v1/messages", json=args)
        elif name == "hive.message_list":
            r = await _hive("GET", "/v1/messages", params={k: v for k, v in args.items() if v})
        elif name == "mnemos.memory_search":
            r = await _mnemos("POST", "/memories/search", json=args)
        elif name == "mnemos.memory_create":
            r = await _mnemos("POST", "/memories", json=args)
        elif name == "mnemos.memory_get":
            r = await _mnemos("GET", f"/memories/{args['id']}")
        elif name == "graeae.consult":
            r = await _mnemos("POST", "/v1/consultations", json=args)
        elif name == "graeae.muses":
            r = await _mnemos("GET", "/v1/consultations/muses")
        elif name == "graeae.modes":
            r = await _mnemos("GET", "/v1/consultations/modes")
        elif name == "graeae.get":
            r = await _mnemos("GET", f"/v1/consultations/{args['consultation_id']}")
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
        return [TextContent(type="text", text=json.dumps(r, default=str))]
    except httpx.HTTPStatusError as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "status": e.response.status_code, "body": e.response.text[:500]}),
            )
        ]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": type(e).__name__, "msg": str(e)}))]


# Silence -32601 method-not-found warnings on opencode boot.
@server.list_resources()
async def _list_resources() -> list[Resource]:
    return []


@server.list_prompts()
async def _list_prompts() -> list[Prompt]:
    return []


# ---------- SSE transport ----------

sse = SseServerTransport("/messages/")


async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="graeae-hive-mind",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


app = Starlette(
    debug=False,
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ],
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5006")))
