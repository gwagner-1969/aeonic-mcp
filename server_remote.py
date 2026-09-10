"""Aeonic Digital Collateral Intelligence — remote (streamable-HTTP) server. Tier 1.

Deploy this to a host (Railway, Render, Fly.io, your own VPS) and share the
resulting URL for a proof-of-concept / interest-gauging phase. This version
is AUTHLESS by default — no API key required — to match how Claude's actual
"Add custom connector" UI works today (URL + optional OAuth Client ID/Secret;
no plain bearer-token field exists in that flow). Anyone with the URL can use
it. That's an acceptable tradeoff right now because every tool here only
touches public data (DefiLlama, on-chain queries) and the demonstration
model itself — nothing proprietary or sensitive sits behind this server.

Optional bearer-token gate: if you set AEONIC_MCP_API_KEY, the server will
still enforce it (useful for direct API/script access, just not compatible
with Claude's connector UI, which has no field for it). Leave it unset for
the authless PoC.

When it's time for real access control, this file is the only one that
changes — swap the auth layer for a TokenVerifier backed by a managed
identity provider (Auth0, WorkOS, Clerk). aeonic_core.py and server_stdio.py
are untouched by that upgrade.

Environment variables:
  AEONIC_MCP_API_KEY   optional — if set, requires 'Authorization: Bearer <key>'.
                        Leave unset for the authless PoC.
  PORT                  optional — defaults to 8000. Most hosts (Railway, Render)
                        set this automatically; do not hardcode it.
"""

import json
import os
import time
from collections import defaultdict, deque

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from aeonic_core import register_tools

API_KEY = os.environ.get("AEONIC_MCP_API_KEY")  # None => authless PoC mode

# --- Chat widget config (new) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # required only for /chat
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-haiku-4-5-20251001")
CHAT_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "CHAT_ALLOWED_ORIGINS", "https://aeonic.vc"
).split(",") if o.strip()]
MAX_MESSAGE_CHARS = 800
MAX_HISTORY_TURNS = 6  # user+assistant pairs kept from client-supplied history
RATE_LIMIT_PER_HOUR = 20  # per IP, in-memory (resets on redeploy -- fine for a PoC)

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

# The /chat endpoint calls Anthropic's API pointing back at OUR OWN /mcp
# endpoint via the MCP connector feature -- this is how the chat widget
# reuses the exact same six tools/formulas without reimplementing anything.
SELF_MCP_URL = f"https://{ALLOWED_HOSTS[0]}/mcp"

TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=ALLOWED_HOSTS,
    allowed_origins=["https://claude.ai", "https://*.claude.ai"] + ALLOWED_HOSTS,
)

mcp = MCPServer("aeonic-digital-collateral")
register_tools(mcp)

_rate_limit_buckets: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(client_ip: str) -> bool:
    """Simple sliding-window limiter. Returns True if the request is allowed."""
    now = time.time()
    bucket = _rate_limit_buckets[client_ip]
    while bucket and now - bucket[0] > 3600:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_PER_HOUR:
        return False
    bucket.append(now)
    return True


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Only enforced if AEONIC_MCP_API_KEY is set. Unset = authless PoC mode.
    Only applies to the MCP endpoint, not /health or /chat (which has its own
    rate limiting instead, since it's meant for public website visitors)."""

    async def dispatch(self, request: Request, call_next):
        if not API_KEY or request.url.path != "/mcp":
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
        "chat_enabled": bool(ANTHROPIC_API_KEY),
        "chat_model": CHAT_MODEL,
    })


async def chat(request: Request) -> JSONResponse:
    if not ANTHROPIC_API_KEY:
        return JSONResponse(
            {"error": "Chat is not configured yet -- ANTHROPIC_API_KEY is not set on this server."},
            status_code=503,
        )

    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    if not _check_rate_limit(client_ip):
        return JSONResponse(
            {"error": "Rate limit reached. Please try again later."},
            status_code=429,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Missing 'message'."}, status_code=400)
    if len(message) > MAX_MESSAGE_CHARS:
        return JSONResponse(
            {"error": f"Message too long (max {MAX_MESSAGE_CHARS} characters)."},
            status_code=400,
        )

    history = body.get("history") or []
    if not isinstance(history, list):
        history = []
    history = history[-(MAX_HISTORY_TURNS * 2):]  # cap growth of the conversation

    messages = []
    for turn in history:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    messages.append({"role": "user", "content": message})

    system_prompt = (
        "You are the chat assistant embedded on Aeonic Advisory's Capital & Liquidity "
        "Optimization Model page. Answer questions about collateral, LCR, NSFR, capital, "
        "and digital asset sourcing using the aeonic-collateral tools available to you. "
        "Call tools rather than guessing numbers. Keep answers concise -- a few sentences "
        "or a short table, not a long report. If the tools' own methodology notes flag "
        "something as illustrative or simplified, pass that caveat along rather than "
        "presenting figures as more authoritative than they are.\n\n"
        "For any question of the form 'what if I shift/move/reallocate X% into <asset>' -- "
        "call run_scenario with shift_into=<exact asset class name> and shift_pct=<the "
        "number> DIRECTLY, in a single tool call, using the current book as the baseline. "
        "Do not ask the user to specify the remaining allocation, do not ask them to choose "
        "between options, and do not describe what you're about to do before doing it -- "
        "the shift_into/shift_pct parameters exist precisely so this never requires "
        "clarification. Only ask a clarifying question if the asset class name genuinely "
        "doesn't match anything in get_asset_universe().\n\n"
        "CRITICAL -- do not invent numbers: every dollar figure, percentage, and ratio you "
        "state must come directly from a tool's JSON response, not from your own arithmetic "
        "on top of it. Do not narrate intermediate calculations ('the book is $X, so a Y% "
        "shift means moving $Z') -- the tool already returns the exact figures involved "
        "(total_book_mm, shift_summary with old/new notional and share, vs_current deltas). "
        "Quote those fields directly. If you did not get a field from a tool response, do "
        "not state it as a fact."
    )

    try:
        async with httpx.AsyncClient(timeout=75.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "mcp-client-2025-04-04",
                    "content-type": "application/json",
                },
                json={
                    "model": CHAT_MODEL,
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": messages,
                    "mcp_servers": [
                        {"type": "url", "url": SELF_MCP_URL, "name": "aeonic-collateral"}
                    ],
                },
            )
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"Could not reach Claude API: {e}"}, status_code=502)

    if resp.status_code != 200:
        return JSONResponse(
            {"error": f"Claude API returned {resp.status_code}", "detail": resp.text[:500]},
            status_code=502,
        )

    data = resp.json()
    text_parts = [block["text"] for block in data.get("content", []) if block.get("type") == "text"]
    answer = "\n".join(text_parts).strip() or "(No text response -- the model may have only called a tool.)"

    return JSONResponse({"answer": answer})


def build_app() -> Starlette:
    mcp_app = mcp.streamable_http_app(stateless_http=True, transport_security=TRANSPORT_SECURITY)
    mcp_app.add_middleware(ApiKeyMiddleware)
    mcp_app.add_middleware(
        CORSMiddleware,
        allow_origins=CHAT_ALLOWED_ORIGINS,
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )
    mcp_app.router.routes.append(Route("/health", health, methods=["GET"]))
    mcp_app.router.routes.append(Route("/chat", chat, methods=["POST"]))
    return mcp_app


app = build_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mode = "bearer-token" if API_KEY else "AUTHLESS (PoC mode)"
    print(f"Starting aeonic-digital-collateral in {mode} mode on port {port}")
    print(f"Allowed hosts: {ALLOWED_HOSTS}")
    print(f"Chat enabled: {bool(ANTHROPIC_API_KEY)} (model: {CHAT_MODEL})")
    uvicorn.run(app, host="0.0.0.0", port=port)
