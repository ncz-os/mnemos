"""Full MCP proof — exercise multiple tools against Oracle-backed MNEMOS."""

import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client

URL = os.environ.get("MCP_URL", "http://192.168.207.25:5004/sse")
TOK = os.environ.get("MCP_TOK", "")
HMAC_KEY = os.environ.get("ORACLE_PROOF_HMAC_KEY", "mnemos-oracle-proof-v1")
REPO = Path(__file__).resolve().parent.parent.joinpath("Projects/mnemos-prod-working")


async def main():
    headers = {"Authorization": f"Bearer {TOK}"}
    probes = []
    async with sse_client(URL, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]

            async def run(tool, args):
                t0 = time.perf_counter()
                try:
                    r = await session.call_tool(tool, arguments=args)
                    elapsed = (time.perf_counter() - t0) * 1000
                    head = str(r.content[0])[:300] if r.content else ""
                    return {
                        "tool": tool,
                        "args": args,
                        "elapsed_ms": round(elapsed, 2),
                        "content_count": len(r.content),
                        "head": head,
                        "ok": True,
                    }
                except Exception as e:
                    return {"tool": tool, "args": args, "ok": False, "error": str(e)}

            probes.append(await run("get_stats", {}))
            probes.append(await run("list_memories", {"limit": 3}))
            probes.append(await run("search_memories", {"query": "oracle", "limit": 3}))
            probes.append(await run("list_memories", {"category": "infrastructure", "limit": 3}))

    body = {
        "schema": "mnemos-oracle-mcp-proof/v1",
        "run_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "mcp_url": URL,
        "protocol_version": init.protocolVersion,
        "server": {"name": init.serverInfo.name, "version": init.serverInfo.version},
        "tools_count": len(tools.tools),
        "tool_names": tool_names,
        "probes": probes,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    sig = hmac.new(HMAC_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    artifact = {"evidence": body, "hmac_sha256": sig, "hmac_key_id": hashlib.sha256(HMAC_KEY.encode()).hexdigest()[:16]}
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mcp_proof.json"
    Path(out).write_text(json.dumps(artifact, indent=2, default=str))
    print(f"wrote {out}")
    print(f"  protocol={init.protocolVersion}  server={init.serverInfo.name} v{init.serverInfo.version}")
    print(f"  tools_count={len(tools.tools)}")
    for p in probes:
        if p["ok"]:
            print(f"  {p['tool']}({p['args']}): {p['content_count']} content in {p['elapsed_ms']}ms")
            print(f"    head: {p['head'][:120]}")
        else:
            print(f"  {p['tool']}({p['args']}): FAIL — {p['error'][:100]}")
    print(f"  hmac: {sig[:16]}…")


if __name__ == "__main__":
    asyncio.run(main())
