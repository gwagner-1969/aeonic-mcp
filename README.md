# Aeonic Digital Collateral Intelligence — MCP Server

Two ways to run this. Both use the exact same tools and model logic
(`aeonic_core.py`) — they can never drift out of sync with each other.

| | `server_stdio.py` (Tier 0) | `server_remote.py` (Tier 1) |
|---|---|---|
| Runs on | Your machine only | A hosted server, reachable by URL |
| Who can use it | Just you | Anyone with the URL (authless PoC) |
| Setup | Claude Desktop config file | Claude's "Add custom connector" UI |
| Auth | None needed (local) | None by default — see note below |

## Tier 1 — hosting it (Railway, recommended)

Railway is the easiest path for a Python app like this: free tier, no Docker
knowledge needed, deploys straight from a folder or a GitHub repo.

**1. Push this folder to a GitHub repo** (or use Railway's CLI to deploy a
local folder directly — either works).

**2. In Railway:**
   - New Project → Deploy from GitHub repo (or "Deploy from local directory" via the CLI)
   - Select this repo/folder
   - Railway auto-detects Python via `requirements.txt` and `Procfile`
   - No environment variables needed for the authless PoC — leave `AEONIC_MCP_API_KEY` unset
   - Deploy. Railway will give you a URL like `https://your-app-name.up.railway.app`

**3. Confirm it's live:** visit `https://your-app-name.up.railway.app/health` in
a browser. You should see:
```json
{"status": "ok", "server": "aeonic-digital-collateral", "auth_mode": "authless (PoC)"}
```

**Alternative hosts:** Render and Fly.io both work the same way — Python
buildpack + `Procfile` is a standard pattern all three support.

## Adding it to Claude as a custom connector

1. In Claude (web or desktop): **Settings → Connectors → Add custom connector**
2. Paste your server's MCP endpoint URL: `https://your-app-name.up.railway.app/mcp`
3. Give it a name, e.g. "Aeonic Digital Collateral"
4. **Leave the OAuth Client ID / Client Secret fields blank** — this server is
   authless, matching that blank-fields case
5. Click Add, then Connect

**Two things worth knowing before you test this with anyone:**

- Remote custom connectors may require a **Pro, Team, or Enterprise** Claude
  plan (not the free tier) — worth confirming on the account you're testing
  with before assuming a connection failure is something I built wrong.
- There are documented reports of Claude's connector flow attempting OAuth
  discovery even against servers that declare no auth at all, which can
  cause a connection to fail for reasons unrelated to this server's code.
  If "Connect" fails immediately, that's the first thing to check — try
  again, or note the exact error text so we can tell whether it's this known
  behavior versus something specific to this deployment.

## Local testing (already verified working)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 server_remote.py
# in another terminal:
curl http://127.0.0.1:8000/health
```

## Security note — read before sharing the URL widely

This server is **authless by design** for the proof-of-concept phase — no
login, no key, anyone with the URL can call every tool. That's an acceptable
tradeoff right now because every tool only touches public data (DefiLlama,
direct on-chain queries) and the demonstration model itself — nothing
proprietary sits behind this server today.

**If that ever changes** — if you add anything client-specific, proprietary,
or costly to run — don't keep this authless. The upgrade path is additive,
not a rewrite: `aeonic_core.py` (the tools and model) stays untouched;
`server_remote.py` gets a real `TokenVerifier` backed by a managed identity
provider (Auth0, WorkOS, Clerk are the standard choices — avoid hand-rolling
OAuth 2.1 yourself, it's a notoriously easy protocol to get subtly wrong).

## Optional: re-enabling simple bearer-token auth

If you want basic gatekeeping without full OAuth (e.g. for direct script/API
access, not through Claude's connector UI, which has no field for a plain
key), set the `AEONIC_MCP_API_KEY` environment variable when starting the
server — `server_remote.py` will automatically start enforcing it. Generate
one with:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
