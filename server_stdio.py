"""Aeonic Digital Collateral Intelligence — local (stdio) server. Tier 0.

Run this on your own machine and add it to Claude Desktop's config. Only you
can use it; no hosting or authentication required. See README.md for setup.
"""

from mcp.server.mcpserver import MCPServer

from aeonic_core import register_tools

mcp = MCPServer("aeonic-digital-collateral")
register_tools(mcp)

if __name__ == "__main__":
    mcp.run(transport="stdio")
