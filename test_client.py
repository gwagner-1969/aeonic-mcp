import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command="python3", args=["/home/claude/mcp_server_v2/server_stdio.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("=== TOOLS DISCOVERED ===")
            for t in tools.tools:
                print(f"- {t.name}: {t.description[:80]}...")
            print()

            print("=== get_asset_universe (first item) ===")
            r = await session.call_tool("get_asset_universe", {})
            data = json.loads(r.content[0].text)
            print(json.dumps(data[0], indent=2))
            print()

            print("=== run_scenario(preset='current') ===")
            r = await session.call_tool("run_scenario", {"preset": "current"})
            print(r.content[0].text)
            print()

            print("=== run_scenario(preset='scenario_b') ===")
            r = await session.call_tool("run_scenario", {"preset": "scenario_b"})
            print(r.content[0].text)
            print()

            print("=== run_scenario(bad allocation, should error cleanly) ===")
            r = await session.call_tool("run_scenario", {"allocation": {"Cash / Central Bank Reserves": 50}})
            print(r.content[0].text)
            print()

            print("=== classify_asset(tokenized UST) ===")
            r = await session.call_tool("classify_asset", {
                "has_traditional_id": True, "is_direct_beneficial_ownership": True,
                "venue_recognizes_as_collateral": False, "underlying_security_type": "US Treasury"})
            print(r.content[0].text)
            print()

            print("=== classify_asset(wrapped synthetic exposure) ===")
            r = await session.call_tool("classify_asset", {
                "has_traditional_id": False, "is_direct_beneficial_ownership": False})
            print(r.content[0].text)
            print()

            print("=== lookup_identifier('BUIDL') ===")
            r = await session.call_tool("lookup_identifier", {"asset_name": "BUIDL"})
            print(r.content[0].text)
            print()

            print("=== list_sources(status_filter='Live') ===")
            r = await session.call_tool("list_sources", {"status_filter": "Live"})
            print(r.content[0].text)
            print()

            print("=== get_live_collateral_inventory() [REAL NETWORK CALL] ===")
            r = await session.call_tool("get_live_collateral_inventory", {})
            print(r.content[0].text)


asyncio.run(main())
