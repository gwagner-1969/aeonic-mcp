"""Aeonic Digital Collateral Intelligence ‚Äî remote (streamable-HTTP) server. Tier 1.

Deploy this to a host (Railway, Render, Fly.io, your own VPS) and share the
resulting URL for a proof-of-concept / interest-gauging phase. This version
is AUTHLESS by default ‚Äî no API key required ‚Äî to match how Claude's actual
"Add custom connector" UI works today (URL + optional OAuth Client ID/Secret;
no plain bearer-token field exists in that flow). Anyone with the URL can use
it. That's an acceptable tradeoff right now because every tool here only
touches public data (DefiLlama, on-chain queries) and the demonstration
model itself ‚Äî nothing proprietary or sensitive sits behind this server.

Optional bearer-token gate: if you set AEONIC_MCP_API_KEY, the server will
still enforce it (useful for direct API/script access, just not compatible
with Claude's connector UI, which has no field for it). Leave it unset for
the authless PoC.

When it's time for real access control, this file is the only one that
changes ‚Äî swap the auth layer for a TokenVerifier backed by a managed
identity provider (Auth0, WorkOS, Clerk). aeonic_core.py and server_stdio.py
are untouched by that upgrade.

Environment variables:
  AEONIC_MCP_API_KEY   optional ‚Äî if set, requires 'Authorization: Bearer <key>'.
                        Leave unset for the authless PoC.
  PORT                  optional ‚Äî defaults to 8000. Most hosts (Railway, Render)
                        set this automatically; do not hardcode it.
"""

import os

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from aeonic_core import register_tools

API_KEY = os.environ.get("AEONIC_MCP_API_KEY")  # None => authless PoC mode

# The MCP SDK enforces DNS-rebinding protection by validating the Host header
# against an explicit allowlist. Without this, every request is rejected with
# "Invalid Host header" / HTTP 421 -- this is what bit the first Railway deploy.
#
# PUBLIC_HOST: comma-separated hostnames this server is reachable at (no
# scheme, no path), e.g. "aeonic-mcp-production.up.railway.app" or, once a
# custom domain exists, "mcp.aeonic.vc,aeonic-mcp-production.up.railway.app"
_public_hosts_env = os.environ.get("PUBLIC_HOST", "")
ALLOWED_HOSTS = [h.strip() for h in _public_hosts_env.split(",") if h.strip()] or [
    "aeonic-mcp-production.up.railway.app",
]
ALLOWED_HOSTS += ["localhost:8000", "127.0.0.1:8000"]  # keep local testing working too

TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=ALLOWED_HOSTS,
    allowed_origins=["https://claude.ai", "https://*.claude.ai"] + ALLOWED_HOSTS,
)

mcp = MCPServer("aeonic-digital-collateral")
register_tools(mcp)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Only enforced if AEONIC_MCP_API_KEY is set. Unset = authless PoC mode."""

    async def dispatch(self, request: Request, call_next):
        if not API_KEY or request.url.path == "/health":
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        if not token or token != API_KEY:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        return await call_next(request)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "server": "aeonic-digital-collateral",
        "auth_mode": "bearer-token" if API_KEY else "authless (PoC)",
        "allowed_hosts": ALLOWED_HOSTS,
    })


def build_app() -> Starlette:
    mcp_app = mcp.streamable_http_app(stateless_http=True, transport_security=TRANSPORT_SECURITY)
    mcp_app.add_middleware(ApiKeyMiddleware)
    mcp_app.router.routes.append(Route("/health", health, methods=["GET"]))
    return mcp_app


app = build_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mode = "bearer-token" if API_KEY else "AUTHLESS (PoC mode)"
    print(f"Starting aeonic-digital-collateral in {mode} mode on port {port}")
    print(f"Allowed hosts: {ALLOWED_HOSTS}")
    uvicorn.run(app, host="0.0.0.0", port=port)


