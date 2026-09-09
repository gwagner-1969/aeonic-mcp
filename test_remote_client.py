import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    url = "http://127.0.0.1:8123/mcp"
    headers = {"Authorization": "Bearer test-key-12345"}
    http_client = httpx.AsyncClient(headers=headers, timeout=30.0)

    async with streamable_http_client(url, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("=== TOOLS DISCOVERED OVER HTTP ===")
            for t in tools.tools:
                print(f"- {t.name}")
            print()

            print("=== run_scenario(preset='scenario_b') over HTTP ===")
            r = await session.call_tool("run_scenario", {"preset": "scenario_b"})
            data = json.loads(r.content[0].text)
            print(json.dumps(data["vs_current"], indent=2))
            print()

            print("=== list_sources(status_filter='Live') over HTTP ===")
            r = await session.call_tool("list_sources", {"status_filter": "Live"})
            print(r.content[0].text)


asyncio.run(main())
