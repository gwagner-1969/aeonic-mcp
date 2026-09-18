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

from aeonic_core import (
    register_tools,
    get_asset_universe_impl, run_scenario_impl, classify_asset_impl,
    lookup_identifier_impl, list_sources_impl, get_live_collateral_inventory_impl,
    classify_portfolio_impl,
)

API_KEY = os.environ.get("AEONIC_MCP_API_KEY")  # None => authless PoC mode

# --- REST API (v1) config -- a separate, deliberately simple concern from the MCP/chat auth
# above. One or more comma-separated keys; each request must send one in the 'apikey' header
# (same convention as SonarX's public API -- a familiar pattern for anyone integrating).
# Empty/unset REST_API_KEYS means the REST API is not exposed at all (routes 404), rather than
# silently running authless -- unlike the MCP server, these routes are meant for real
# institutional integration, not casual public access, so there's no "authless PoC mode" here.
REST_API_KEYS = {k.strip() for k in os.environ.get("REST_API_KEYS", "").split(",") if k.strip()}
REST_RATE_LIMIT_PER_HOUR = int(os.environ.get("REST_RATE_LIMIT_PER_HOUR", "120"))

# --- Chat widget config (new) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # required only for /chat
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-haiku-4-5-20251001")
CHAT_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "CHAT_ALLOWED_ORIGINS", "https://aeonic.vc,https://aeonic.digital"
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
_rest_rate_limit_buckets: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(client_ip: str, buckets: dict[str, deque] = None, limit: int = None) -> bool:
    """Simple sliding-window limiter. Returns True if the request is allowed. Defaults to the
    chat's own bucket/limit; pass buckets/limit explicitly for a separate namespace (e.g. REST)."""
    buckets = _rate_limit_buckets if buckets is None else buckets
    limit = RATE_LIMIT_PER_HOUR if limit is None else limit
    now = time.time()
    bucket = buckets[client_ip]
    while bucket and now - bucket[0] > 3600:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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

    client_ip = _client_ip(request)
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
        "not state it as a fact.\n\n"
        "If the user pastes or describes their OWN real positions (not the illustrative demo "
        "book), use classify_portfolio, not run_scenario. Parse their input into a list of "
        "{description, notional_mm} positions as best you can, call the tool, and report each "
        "position's confidence tier plainly -- do not present a 'heuristic' or 'unclassified' "
        "result as if it were an exact match. The tool ALWAYS returns LCR/NSFR now -- check "
        "lcr_nsfr_basis in the response: if 'illustrative_default', state clearly that these are "
        "placeholder assumptions (quote the exact other_outflows_mm/other_asf_mm the tool used "
        "from lcr_nsfr_assumptions_used, don't restate them from memory) and offer to recompute "
        "with their real figures; if 'client_provided', these are their real numbers. Never "
        "invent your own illustrative assumption on the fly -- the tool's defaults exist "
        "precisely so you don't have to. classify_portfolio never returns a funding cost figure "
        "for real portfolios (by design -- it would require the client's own borrowing rates); "
        "proactively mention this limitation up front rather than waiting to be asked.\n\n"
        "CRITICAL for HQLA totals: classify_portfolio's response has THREE different level-based "
        "numbers that are easy to confuse -- read table_building_instructions in the response "
        "every time. raw_notional_by_level_mm and haircut_adjusted_value_by_level_mm are both "
        "per-level breakdowns for building a table; NEITHER should be summed and labeled 'Total "
        "HQLA Stock'. Only aggregate.hqla_stock_mm is the correct total (it reflects both "
        "haircuts and the Basel Level 2 caps, which can make it slightly less than the simple "
        "sum of the per-level figures). Getting this wrong is a real, embarrassing error for a "
        "finance audience -- double check you're using aggregate.hqla_stock_mm specifically for "
        "any 'Total HQLA Stock' line."
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


# =====================================================================
# REST API v1 -- plain JSON in/out, no LLM in the loop. Same underlying logic as the MCP
# tools and the chat widget (both call the identical *_impl functions in aeonic_core.py),
# reached with a normal HTTP request instead of a natural-language question. Meant for a
# bank's or partner's own systems to call directly -- batch jobs, their own dashboards,
# anything that wants deterministic, fast, structured access without an LLM round-trip.
#
# Namespaced under /api/v1/capital/... deliberately, even though it's the only module today
# -- /api/v1/risk/..., /api/v1/wallet/..., /api/v1/collateral/... are reserved for future
# modules, so adding one later never requires restructuring or breaking this one.
# =====================================================================

def _rest_auth_and_rate_limit(request: Request):
    """Returns None if the request may proceed, or a JSONResponse to return immediately."""
    if not REST_API_KEYS:
        return JSONResponse(
            {"error": "The REST API is not enabled on this server (no REST_API_KEYS configured)."},
            status_code=503,
        )
    key = request.headers.get("apikey", "")
    if key not in REST_API_KEYS:
        return JSONResponse({"error": "Missing or invalid API key. Send it in the 'apikey' header."}, status_code=401)
    if not _check_rate_limit(_client_ip(request), _rest_rate_limit_buckets, REST_RATE_LIMIT_PER_HOUR):
        return JSONResponse({"error": "Rate limit reached. Please try again later."}, status_code=429)
    return None


async def _rest_json_body(request: Request):
    """Returns (body_dict, None) or (None, JSONResponse) on a parse error."""
    if not await request.body():
        return {}, None
    try:
        return await request.json(), None
    except Exception:
        return None, JSONResponse({"error": "Invalid JSON body."}, status_code=400)


async def rest_asset_universe(request: Request) -> JSONResponse:
    denied = _rest_auth_and_rate_limit(request)
    if denied:
        return denied
    return JSONResponse({"assets": get_asset_universe_impl()})


async def rest_sources(request: Request) -> JSONResponse:
    denied = _rest_auth_and_rate_limit(request)
    if denied:
        return denied
    status_filter = request.query_params.get("status")
    return JSONResponse({"sources": list_sources_impl(status_filter)})


async def rest_lookup_identifier(request: Request) -> JSONResponse:
    denied = _rest_auth_and_rate_limit(request)
    if denied:
        return denied
    asset_name = request.query_params.get("asset_name", "")
    if not asset_name:
        return JSONResponse({"error": "Missing required query parameter 'asset_name'."}, status_code=400)
    return JSONResponse(lookup_identifier_impl(asset_name))


async def rest_live_inventory(request: Request) -> JSONResponse:
    denied = _rest_auth_and_rate_limit(request)
    if denied:
        return denied
    return JSONResponse(get_live_collateral_inventory_impl())


async def rest_classify_asset(request: Request) -> JSONResponse:
    denied = _rest_auth_and_rate_limit(request)
    if denied:
        return denied
    body, err = await _rest_json_body(request)
    if err:
        return err
    required = ("has_traditional_id", "is_direct_beneficial_ownership")
    missing = [k for k in required if k not in body]
    if missing:
        return JSONResponse({"error": f"Missing required field(s): {', '.join(missing)}."}, status_code=400)
    return JSONResponse(classify_asset_impl(
        has_traditional_id=body["has_traditional_id"],
        is_direct_beneficial_ownership=body["is_direct_beneficial_ownership"],
        venue_recognizes_as_collateral=body.get("venue_recognizes_as_collateral", True),
        underlying_security_type=body.get("underlying_security_type"),
    ))


async def rest_run_scenario(request: Request) -> JSONResponse:
    denied = _rest_auth_and_rate_limit(request)
    if denied:
        return denied
    body, err = await _rest_json_body(request)
    if err:
        return err
    result = run_scenario_impl(
        allocation=body.get("allocation"),
        preset=body.get("preset"),
        shift_into=body.get("shift_into"),
        shift_pct=body.get("shift_pct"),
    )
    status = 400 if "error" in result else 200
    return JSONResponse(result, status_code=status)


async def rest_classify_portfolio(request: Request) -> JSONResponse:
    denied = _rest_auth_and_rate_limit(request)
    if denied:
        return denied
    body, err = await _rest_json_body(request)
    if err:
        return err
    result = classify_portfolio_impl(
        positions=body.get("positions"),
        financing_positions=body.get("financing_positions"),
        other_outflows_mm=body.get("other_outflows_mm"),
        other_asf_mm=body.get("other_asf_mm"),
    )
    status = 400 if "error" in result else 200
    return JSONResponse(result, status_code=status)


# Hand-written OpenAPI 3.0 spec -- Starlette (unlike FastAPI) doesn't generate this
# automatically, so it's maintained by hand here. Keep this in sync whenever a route above
# changes shape.
OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "Aeonic Digital Capital & Liquidity API",
        "version": "1.0.0",
        "description": "Regulatory classification and capital/liquidity modeling for traditional "
                        "and tokenized assets. Same underlying logic as the Aeonic Digital chat "
                        "widget, reached via plain JSON instead of natural language.",
    },
    "servers": [{"url": f"https://{ALLOWED_HOSTS[0]}"}],
    "components": {
        "securitySchemes": {
            "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "apikey"}
        }
    },
    "security": [{"ApiKeyAuth": []}],
    "paths": {
        "/api/v1/capital/asset-universe": {
            "get": {
                "summary": "The full asset universe (HQLA level, haircut, RSF, risk weight, tenor per asset class)",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/api/v1/capital/sources": {
            "get": {
                "summary": "Digital collateral data sources and their connectivity status",
                "parameters": [{"name": "status", "in": "query", "required": False,
                                 "schema": {"type": "string"}, "description": "Filter, e.g. 'Live'"}],
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/api/v1/capital/lookup-identifier": {
            "get": {
                "summary": "Identifier crosswalk (ISIN/CUSIP, digital DTI, network) for a named asset",
                "parameters": [{"name": "asset_name", "in": "query", "required": True,
                                 "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"}, "400": {"description": "Missing asset_name"}},
            }
        },
        "/api/v1/capital/live-inventory": {
            "get": {
                "summary": "Live AUM/TVL for tokenized collateral products (DefiLlama + on-chain)",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/api/v1/capital/classify-asset": {
            "post": {
                "summary": "5-step HQLA classification decision chain for a single asset",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object",
                    "required": ["has_traditional_id", "is_direct_beneficial_ownership"],
                    "properties": {
                        "has_traditional_id": {"type": "boolean"},
                        "is_direct_beneficial_ownership": {"type": "boolean"},
                        "venue_recognizes_as_collateral": {"type": "boolean", "default": True},
                        "underlying_security_type": {"type": "string"},
                    },
                }}}},
                "responses": {"200": {"description": "OK"}, "400": {"description": "Missing required field(s)"}},
            }
        },
        "/api/v1/capital/run-scenario": {
            "post": {
                "summary": "LCR/NSFR/capital/funding-cost model on the illustrative demo book. "
                           "Provide exactly one of: preset, (shift_into + shift_pct), or allocation.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "preset": {"type": "string", "enum": ["current", "scenario_a", "scenario_b"]},
                        "shift_into": {"type": "string", "description": "Exact asset class name"},
                        "shift_pct": {"type": "number"},
                        "allocation": {"type": "object", "description": "Asset name -> pct of book, all 9, summing to 100"},
                    },
                }}}},
                "responses": {"200": {"description": "OK"}, "400": {"description": "Invalid scenario spec"}},
            }
        },
        "/api/v1/capital/classify-portfolio": {
            "post": {
                "summary": "Classify a real client portfolio into HQLA levels; compute HQLA stock, "
                           "RWA, capital required, LCR, and NSFR. Supports held positions and "
                           "matched-book financing positions.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "positions": {"type": "array", "items": {"type": "object", "properties": {
                            "description": {"type": "string"}, "notional_mm": {"type": "number"},
                            "funding_tenor": {"type": "string", "enum": ["O/N", "1M", "3M", "6M", "1Y", "2Y+"]},
                        }}},
                        "financing_positions": {"type": "array", "items": {"type": "object", "properties": {
                            "description": {"type": "string"}, "notional_mm": {"type": "number"},
                            "structure": {"type": "string", "enum": ["matched_book"]},
                            "borrow_tenor": {"type": "string", "enum": ["O/N", "1M", "3M", "6M", "1Y", "2Y+"]},
                            "lend_tenor": {"type": "string", "enum": ["O/N", "1M", "3M", "6M", "1Y", "2Y+"]},
                        }}},
                        "other_outflows_mm": {"type": "number", "description": "Real firm-wide figure, optional"},
                        "other_asf_mm": {"type": "number", "description": "Real firm-wide figure, optional"},
                    },
                }}}},
                "responses": {"200": {"description": "OK"}, "400": {"description": "Invalid position data"}},
            }
        },
    },
}


async def rest_openapi_spec(request: Request) -> JSONResponse:
    return JSONResponse(OPENAPI_SPEC)


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

    # REST API v1 -- registered here since Starlette routes aren't picked up just by being
    # defined as functions; they have to be explicitly added to the router like /health and
    # /chat above. (Confirmed this was missing: none of the seven routes below, or the spec
    # endpoint, were actually reachable until this fix -- everything below 404'd despite the
    # OpenAPI spec describing it as live.)
    mcp_app.router.routes.append(Route("/api/v1/capital/asset-universe", rest_asset_universe, methods=["GET"]))
    mcp_app.router.routes.append(Route("/api/v1/capital/sources", rest_sources, methods=["GET"]))
    mcp_app.router.routes.append(Route("/api/v1/capital/lookup-identifier", rest_lookup_identifier, methods=["GET"]))
    mcp_app.router.routes.append(Route("/api/v1/capital/live-inventory", rest_live_inventory, methods=["GET"]))
    mcp_app.router.routes.append(Route("/api/v1/capital/classify-asset", rest_classify_asset, methods=["POST"]))
    mcp_app.router.routes.append(Route("/api/v1/capital/run-scenario", rest_run_scenario, methods=["POST"]))
    mcp_app.router.routes.append(Route("/api/v1/capital/classify-portfolio", rest_classify_portfolio, methods=["POST"]))
    mcp_app.router.routes.append(Route("/api/v1/openapi.json", rest_openapi_spec, methods=["GET"]))
    return mcp_app


app = build_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mode = "bearer-token" if API_KEY else "AUTHLESS (PoC mode)"
    print(f"Starting aeonic-digital-collateral in {mode} mode on port {port}")
    print(f"Allowed hosts: {ALLOWED_HOSTS}")
    print(f"Chat enabled: {bool(ANTHROPIC_API_KEY)} (model: {CHAT_MODEL})")
    uvicorn.run(app, host="0.0.0.0", port=port)
