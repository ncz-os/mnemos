"""Probe MNEMOS MCP HTTP server via SSE transport."""

import asyncio
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

URL = os.environ.get("MCP_URL", "http://192.168.207.25:5004/sse")
TOK = os.environ.get("MCP_TOK", "")


async def main():
    headers = {"Authorization": f"Bearer {TOK}"} if TOK else None
    async with sse_client(URL, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"protocol={init.protocolVersion}")
            print(f"server={init.serverInfo.name} v{init.serverInfo.version}")
            tools = await session.list_tools()
            print(f"tools count: {len(tools.tools)}")
            for t in tools.tools[:10]:
                print(f"  - {t.name}: {(t.description or '')[:60]}")
            # Call one tool
            if tools.tools:
                # Find a read-only tool
                for t in tools.tools:
                    if "list" in t.name.lower() or "stats" in t.name.lower():
                        print(f"--- call {t.name} ---")
                        try:
                            r = await session.call_tool(t.name, arguments={})
                            print(f"  result content count: {len(r.content)}")
                            if r.content:
                                print(f"  first content (200ch): {str(r.content[0])[:200]}")
                        except Exception as e:
                            print(f"  call failed: {e}")
                        break


if __name__ == "__main__":
    asyncio.run(main())
