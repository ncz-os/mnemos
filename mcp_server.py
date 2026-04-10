#!/usr/bin/env python3
"""
MNEMOS MCP Server — Model Context Protocol interface to MNEMOS memory system.

Transport: stdio (Claude Code spawns this process directly)
Backend:   MNEMOS REST API (default http://localhost:5002, override via MNEMOS_BASE env var)

For remote MNEMOS (e.g. from macOS connecting to PYTHIA):
  Set MNEMOS_BASE=http://192.0.2.67:5002 in the MCP server config,
  or use SSH transport: command=ssh, args=[jasonperlow@192.0.2.67,
  /opt/mnemos/venv/bin/python, /opt/mnemos/mcp_server.py]

IMPORTANT: All logging must go to stderr. Any stdout output corrupts MCP JSON-RPC framing.
"""
import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# Stderr-only logging — stdout is reserved for JSON-RPC frames
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("mnemos-mcp")

MNEMOS_BASE = os.getenv("MNEMOS_BASE", "http://localhost:5002").rstrip("/")
HTTP_TIMEOUT = 30.0

app = Server("mnemos")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _get(path: str, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(f"{MNEMOS_BASE}{path}", params=params)
        r.raise_for_status()
        return r.json() if r.content else {}


async def _post(path: str, body: dict) -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.post(f"{MNEMOS_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}


async def _delete(path: str) -> int:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.delete(f"{MNEMOS_BASE}{path}")
        return r.status_code


# ── Tool registry ─────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_memories",
            description=(
                "Full-text search across MNEMOS memories. Returns ranked results. "
                "Filter by category (infrastructure/solutions/patterns/decisions/"
                "projects/standards/facts) and/or subcategory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":       {"type": "string",  "description": "Search query"},
                    "limit":       {"type": "integer", "default": 10},
                    "category":    {"type": "string",  "description": "Optional category filter"},
                    "subcategory": {"type": "string",  "description": "Optional subcategory filter"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_memory",
            description="Retrieve a single memory by its ID (mem_xxxxxxxxxxxx).",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                },
                "required": ["memory_id"],
            },
        ),
        types.Tool(
            name="create_memory",
            description=(
                "Store a new memory in MNEMOS. "
                "Categories: infrastructure, solutions, patterns, decisions, "
                "projects, standards, facts. Use subcategory for scoped retrieval."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content":     {"type": "string"},
                    "category":    {"type": "string", "default": "facts"},
                    "subcategory": {"type": "string"},
                    "metadata":    {"type": "object"},
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="delete_memory",
            description="Delete a memory by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                },
                "required": ["memory_id"],
            },
        ),
        types.Tool(
            name="list_memories",
            description="List memories with optional category/subcategory filter and pagination.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category":    {"type": "string"},
                    "subcategory": {"type": "string"},
                    "limit":       {"type": "integer", "default": 20},
                    "offset":      {"type": "integer", "default": 0},
                },
            },
        ),
        types.Tool(
            name="get_stats",
            description="Get MNEMOS system stats: total memories, breakdown by category, compression.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="kg_create_triple",
            description=(
                "Add a knowledge graph triple (subject → predicate → object). "
                "Records facts, relationships, and temporal knowledge. "
                "Example: subject='PYTHIA', predicate='runs', object='MNEMOS', "
                "subject_type='server', object_type='service'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject":      {"type": "string"},
                    "predicate":    {"type": "string"},
                    "object":       {"type": "string"},
                    "subject_type": {"type": "string"},
                    "object_type":  {"type": "string"},
                    "valid_from":   {"type": "string", "description": "ISO8601 datetime"},
                    "valid_until":  {"type": "string", "description": "ISO8601 datetime (leave null if still valid)"},
                    "memory_id":    {"type": "string", "description": "Link to source memory"},
                    "confidence":   {"type": "number", "default": 1.0, "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["subject", "predicate", "object"],
            },
        ),
        types.Tool(
            name="kg_search",
            description=(
                "Search knowledge graph triples. Filter by subject, predicate, object, "
                "subject_type, and/or object_type (all AND logic, all optional)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject":      {"type": "string"},
                    "predicate":    {"type": "string"},
                    "object":       {"type": "string"},
                    "subject_type": {"type": "string"},
                    "object_type":  {"type": "string"},
                    "limit":        {"type": "integer", "default": 50},
                },
            },
        ),
        types.Tool(
            name="kg_timeline",
            description=(
                "Get the chronological history of an entity: all triples where it "
                "is the subject, ordered by valid_from. Shows how facts change over time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "limit":   {"type": "integer", "default": 100},
                },
                "required": ["subject"],
            },
        ),
    ]


# ── Tool dispatch ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except httpx.HTTPStatusError as e:
        detail = {}
        try:
            detail = e.response.json()
        except Exception:
            detail = {"raw": e.response.text[:500]}
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": str(e), "detail": detail}, indent=2),
        )]
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}", exc_info=True)
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": str(e)}, indent=2),
        )]


async def _dispatch(name: str, args: dict) -> Any:
    if name == "search_memories":
        body: dict = {"query": args["query"], "limit": args.get("limit", 10)}
        if args.get("category"):
            body["category"] = args["category"]
        if args.get("subcategory"):
            body["subcategory"] = args["subcategory"]
        return await _post("/memories/search", body)

    elif name == "get_memory":
        return await _get(f"/memories/{args['memory_id']}")

    elif name == "create_memory":
        body = {"content": args["content"], "category": args.get("category", "facts")}
        if args.get("subcategory"):
            body["subcategory"] = args["subcategory"]
        if args.get("metadata"):
            body["metadata"] = args["metadata"]
        return await _post("/memories", body)

    elif name == "delete_memory":
        status = await _delete(f"/memories/{args['memory_id']}")
        return {"deleted": True, "status": status}

    elif name == "list_memories":
        params: dict = {}
        for k in ("category", "subcategory", "limit", "offset"):
            if args.get(k) is not None:
                params[k] = args[k]
        return await _get("/memories", params=params)

    elif name == "get_stats":
        return await _get("/stats")

    elif name == "kg_create_triple":
        return await _post("/kg/triples", {k: v for k, v in args.items() if v is not None})

    elif name == "kg_search":
        params = {
            k: v for k, v in args.items()
            if k in ("subject", "predicate", "object", "subject_type", "object_type", "limit")
            and v is not None
        }
        return await _get("/kg/triples", params=params)

    elif name == "kg_timeline":
        return await _get(
            f"/kg/timeline/{args['subject']}",
            params={"limit": args.get("limit", 100)},
        )

    else:
        raise ValueError(f"Unknown tool: {name}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
