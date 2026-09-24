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

import base64
import hashlib
import json
import os
import re
import secrets
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
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route

from aeonic_core import (
    register_tools,
    get_asset_universe_impl, run_scenario_impl, classify_asset_impl,
    lookup_identifier_impl, list_sources_impl, get_live_collateral_inventory_impl,
    classify_portfolio_impl,
)

API_KEY = os.environ.get("AEONIC_MCP_API_KEY")  # None => authless PoC mode (or OAuth-only, see below)

# ---------------------------------------------------------------------------
# OAuth 2.0 for /mcp -- lets claude.ai's standard "Add custom connector" button
# work (paste URL, click Add, log in, done), instead of a hand-copied bearer
# token that only Claude Desktop/Code's local config file can use.
#
# This implements just enough of the MCP Authorization spec for that button to
# work: Protected Resource Metadata (RFC 9728), Authorization Server Metadata
# (RFC 8414), Dynamic Client Registration (RFC 7591), and the Authorization
# Code grant with PKCE (S256) -- no refresh-token grant, since a 90-day pilot
# with a long-lived access token is simpler and does not need one.
#
# The "who can sign in" check is a single shared username/password pair (set
# via MCP_OAUTH_USER / MCP_OAUTH_PASSWORD below), not a real user directory --
# an intentional, stated simplification for a single-partner pilot, not a
# claim of enterprise-grade identity management.
# ---------------------------------------------------------------------------
OAUTH_USER = os.environ.get("MCP_OAUTH_USER", "")
OAUTH_PASSWORD = os.environ.get("MCP_OAUTH_PASSWORD", "")
OAUTH_ENABLED = bool(OAUTH_USER and OAUTH_PASSWORD)

OAUTH_CODE_TTL_SECONDS = 600            # time allowed to complete the redirect + token exchange
OAUTH_TOKEN_TTL_SECONDS = 365 * 24 * 3600  # long-lived on purpose -- no refresh grant implemented

_oauth_clients: dict = {}   # client_id -> {"redirect_uris": [...]}
_oauth_codes: dict = {}     # code -> {"client_id", "redirect_uri", "code_challenge", "expires_at"}
_oauth_tokens: dict = {}    # access_token -> {"client_id", "issued_at"}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    return _b64url(hashlib.sha256(verifier.encode()).digest()) == challenge


def _oauth_base(request: Request) -> str:
    # Railway terminates TLS in front of the app, so the app itself sees http; trust the
    # forwarded proto for the URLs we hand back to the OAuth client.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.url.netloc}"


async def oauth_protected_resource(request: Request) -> JSONResponse:
    base = _oauth_base(request)
    return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})


async def oauth_authorization_server(request: Request) -> JSONResponse:
    base = _oauth_base(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def oauth_register(request: Request) -> JSONResponse:
    """Dynamic Client Registration (RFC 7591). Claude calls this itself the first time
    someone adds the connector -- there is no pre-shared client ID to configure."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "redirect_uris is required"}, status_code=400)
    client_id = "aeonic-" + secrets.token_urlsafe(16)
    _oauth_clients[client_id] = {"redirect_uris": redirect_uris}
    return JSONResponse({
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }, status_code=201)


_LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Sign in | Aeonic Digital</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{{color-scheme:dark}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0D1145;
    color:#F4F6FB;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
  form{{background:#172070;padding:2rem 2.2rem;border-radius:12px;width:320px;
    border:1px solid rgba(47,128,255,0.35)}}
  h1{{font-size:1.05rem;margin:0 0 0.3rem;font-weight:700}}
  p.sub{{font-size:0.85rem;color:rgba(244,246,251,0.65);margin:0 0 1.3rem}}
  label{{display:block;font-size:0.8rem;margin-bottom:0.3rem;color:rgba(244,246,251,0.8)}}
  input[type=text],input[type=password]{{width:100%;padding:0.6rem 0.7rem;margin-bottom:1rem;
    border-radius:6px;border:1px solid rgba(47,128,255,0.5);background:#0D1145;color:#fff;
    box-sizing:border-box;font-size:0.95rem}}
  button{{width:100%;padding:0.65rem;background:#2F80FF;color:#fff;border:none;border-radius:6px;
    font-weight:600;font-size:0.95rem;cursor:pointer}}
  button:hover{{background:#4a91ff}}
  p.err{{color:#F87171;font-size:0.85rem;margin:0 0 1rem}}
</style></head><body>
<form method="POST">
  <h1>Sign in to Aeonic Digital</h1>
  <p class="sub">Connecting an AI assistant to the collateral intelligence tools.</p>
  {error}
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <label for="u">Username</label>
  <input type="text" id="u" name="username" autocomplete="username" autofocus>
  <label for="p">Password</label>
  <input type="password" id="p" name="password" autocomplete="current-password">
  <button type="submit">Sign in</button>
</form></body></html>"""


async def oauth_authorize(request: Request):
    """GET shows the login form; POST checks the credential and redirects back to the
    client with a short-lived authorization code, per RFC 6749 section 4.1 with PKCE."""
    if request.method == "GET":
        q = request.query_params
        if q.get("code_challenge_method", "S256") != "S256":
            return JSONResponse({"error": "invalid_request", "error_description": "only S256 PKCE is supported"}, status_code=400)
        return HTMLResponse(_LOGIN_PAGE.format(
            error="", client_id=q.get("client_id", ""), redirect_uri=q.get("redirect_uri", ""),
            state=q.get("state", ""), code_challenge=q.get("code_challenge", "")))

    form = await request.form()
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state", "")
    code_challenge = form.get("code_challenge", "")

    client = _oauth_clients.get(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        # Never redirect on this failure -- an unrecognized redirect_uri is exactly the
        # open-redirect case PKCE/registration exists to prevent.
        return JSONResponse({"error": "invalid_client_or_redirect_uri"}, status_code=400)

    if not OAUTH_ENABLED or form.get("username") != OAUTH_USER or form.get("password") != OAUTH_PASSWORD:
        return HTMLResponse(_LOGIN_PAGE.format(
            error='<p class="err">Incorrect username or password.</p>',
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            code_challenge=code_challenge), status_code=401)

    code = secrets.token_urlsafe(24)
    _oauth_codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri,
                           "code_challenge": code_challenge, "expires_at": time.time() + OAUTH_CODE_TTL_SECONDS}
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}" + (f"&state={state}" if state else "")
    return RedirectResponse(location, status_code=302)


async def oauth_token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    entry = _oauth_codes.pop(form.get("code", ""), None)
    if not entry or entry["expires_at"] < time.time():
        return JSONResponse({"error": "invalid_grant", "error_description": "code is invalid, used, or expired"}, status_code=400)
    if form.get("redirect_uri") != entry["redirect_uri"] or form.get("client_id") != entry["client_id"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri or client_id mismatch"}, status_code=400)
    if not _pkce_ok(form.get("code_verifier", ""), entry["code_challenge"]):
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

    token = secrets.token_urlsafe(32)
    _oauth_tokens[token] = {"client_id": entry["client_id"], "issued_at": time.time()}
    return JSONResponse({"access_token": token, "token_type": "Bearer", "expires_in": OAUTH_TOKEN_TTL_SECONDS})

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
    """Only enforced on /mcp, and only once a gate is actually configured: either
    AEONIC_MCP_API_KEY (a single static token, for Claude Desktop/Code's local config
    file) or MCP_OAUTH_USER + MCP_OAUTH_PASSWORD (real OAuth, for claude.ai's "Add
    custom connector" button). Neither set => authless PoC mode, unchanged.
    Does not apply to /health, /chat, or the /oauth* and /.well-known/* routes, which
    must stay reachable without a token so the OAuth flow itself can run."""

    OPEN_PATHS = {"/health", "/chat", "/oauth/authorize", "/oauth/token", "/oauth/register",
                  "/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server"}

    async def dispatch(self, request: Request, call_next):
        gated = bool(API_KEY) or OAUTH_ENABLED
        if not gated or request.url.path != "/mcp" or request.url.path in self.OPEN_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        valid = bool(token) and ((API_KEY and token == API_KEY) or token in _oauth_tokens)
        if not valid:
            headers = {}
            if OAUTH_ENABLED:
                base = _oauth_base(request)
                headers["WWW-Authenticate"] = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=headers)

        return await call_next(request)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "server": "aeonic-digital-collateral",
        "auth_mode": "oauth" if OAUTH_ENABLED else ("bearer-token" if API_KEY else "authless (PoC)"),
        "allowed_hosts": ALLOWED_HOSTS,
        "chat_enabled": bool(ANTHROPIC_API_KEY),
        "chat_model": CHAT_MODEL,
    })


def _final_answer_text(blocks: list) -> str:
    """The text the model wrote AFTER its last tool result -- i.e. the actual answer.

    The API returns every block in order: text, tool call, tool result, text, ... Text written
    before a tool call is working narration ("Let me use the exact asset class name:"), which
    should not be shown to the visitor. If nothing follows the last tool result (or no tool was
    called), fall back to all text blocks so an answer is never lost."""
    last_result = -1
    for i, block in enumerate(blocks):
        if block.get("type") == "mcp_tool_result":
            last_result = i
    tail = [b.get("text", "") for b in blocks[last_result + 1:] if b.get("type") == "text"]
    answer = "\n".join(t for t in tail if t).strip()
    if not answer:
        answer = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    return answer


_TABLE_SEPARATOR = re.compile(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*")


def _plain_text(text: str) -> str:
    """Safety net for the chat window, which shows plain text and cannot render markdown.
    The prompt already asks for plain text; this removes any markdown that slips through so
    visitors never see stray asterisks, pipes or hashes."""
    lines = []
    for line in text.splitlines():
        s = line.rstrip()
        if _TABLE_SEPARATOR.fullmatch(s) and "-" in s:
            continue                                   # |---|---| divider rows
        if s.strip().startswith("|") and s.strip().endswith("|"):
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            s = " - ".join(c for c in cells if c)      # table row -> one plain line
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)         # headings
        s = re.sub(r"^(\s*)\*\s+", r"\1- ", s)          # '* item' bullets -> '- item'
        lines.append(s)
    t = "\n".join(lines)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)          # **bold**
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)                # __bold__
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", t)   # *italic*
    t = t.replace("**", "").replace("`", "")
    return re.sub(r"\n{3,}", "\n\n", t).strip()


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
        "any 'Total HQLA Stock' line.\n\n"
        "FORMAT: plain text only. The chat window cannot render markdown, so never use "
        "asterisks for bold or italics, pipe tables, or headers. Use short lines and simple "
        "hyphen lists. Begin directly with the answer, with no lead-in sentence about what you "
        "are doing. You may show a ratio as a percentage by multiplying by 100 (for example, "
        "an LCR of 2.3746 is about 237%); show both, like 'LCR 2.37 (237%)'.\n\n"
        "Never mention tool or function names (such as classify_portfolio, run_scenario or "
        "lookup_identifier) in an answer; describe what the model did in plain words. Do not "
        "narrate what you are about to do before calling a tool.\n\n"
        "SIGN CONVENTION for matched-book financing: for each financing position, state the "
        "direction and size of the effect by quoting nsfr_effect_plain from the tool. "
        "net_nsfr_drag_mm is required stable funding minus available stable funding credited: "
        "a NEGATIVE value means the trade IMPROVES the stable-funding position, because the "
        "borrow leg credits more stable funding than the lend leg requires; a POSITIVE value "
        "means it consumes stable funding. Never describe a negative value as a drag that "
        "consumes funding. Example: -262.5 means the trade improves stable funding by $262.5M. "
        "Borrowing long to fund a short lend helps NSFR; borrowing short to fund a longer lend "
        "hurts it.\n\n"
        "LIVE INVENTORY: report each row as the tool returns it, with its source. Do NOT add a "
        "total across rows -- the rows are on different bases (token supply, platform-wide "
        "totals across all of an issuer's funds, and on-chain supply times an oracle price), so "
        "a sum would mislead. Do not add facts about a fund that the tools did not return, such "
        "as who issues it or what it invests in. If a row's value is null, say the free public "
        "data source does not report it. Franklin BENJI is the Franklin OnChain U.S. "
        "Government Money Fund; it is not covered by the free live data."
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
    answer = _plain_text(_final_answer_text(data.get("content", []))) or "(No text response -- the model may have only called a tool.)"

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
    mcp_app.router.routes.append(Route("/.well-known/oauth-protected-resource", oauth_protected_resource, methods=["GET"]))
    mcp_app.router.routes.append(Route("/.well-known/oauth-authorization-server", oauth_authorization_server, methods=["GET"]))
    mcp_app.router.routes.append(Route("/oauth/register", oauth_register, methods=["POST"]))
    mcp_app.router.routes.append(Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]))
    mcp_app.router.routes.append(Route("/oauth/token", oauth_token, methods=["POST"]))

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
    mode = "OAuth" if OAUTH_ENABLED else ("bearer-token" if API_KEY else "AUTHLESS (PoC mode)")
    print(f"Starting aeonic-digital-collateral in {mode} mode on port {port}")
    print(f"Allowed hosts: {ALLOWED_HOSTS}")
    print(f"Chat enabled: {bool(ANTHROPIC_API_KEY)} (model: {CHAT_MODEL})")
    uvicorn.run(app, host="0.0.0.0", port=port)
