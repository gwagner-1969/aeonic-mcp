"""
Regression test for the "Claude API returned 400" chat bug.

Root cause: once /mcp required a bearer token (bearer-key era, and now OAuth),
the /chat endpoint's own outbound call to Anthropic (mcp_servers -> self /mcp)
never supplied one, so Anthropic's connector auth check failed the whole
/v1/messages request with HTTP 400 before any tool ever ran.

This test never calls the real Anthropic API and never touches
ANTHROPIC_API_KEY -- it mocks httpx.AsyncClient.post to capture the exact
outbound payload/headers server_remote.py builds, and separately exercises
the real ApiKeyMiddleware against real requests to prove the self-auth path
works in every auth mode, without weakening external auth in any of them.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

# Configure env BEFORE importing the module (module reads os.environ at import time).
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
os.environ["MCP_OAUTH_USER"] = "sonarx"
os.environ["MCP_OAUTH_PASSWORD"] = "test-password-not-real"
os.environ.pop("AEONIC_MCP_API_KEY", None)  # OAuth-only mode, matches production today

sys.path.insert(0, os.path.dirname(__file__))

# --- local-sandbox-only shim ---------------------------------------------
# This sandbox's pinned `mcp` package (1.27.0) exposes the high-level server
# class as mcp.server.fastmcp.FastMCP, not mcp.server.mcpserver.MCPServer.
# Production (Railway) imports fine today -- confirmed live via /health --
# so its requirements.txt/lockfile resolves an mcp version that still ships
# the mcpserver.MCPServer name. This alias exists ONLY so this test can
# import server_remote.py unmodified in this sandbox; it changes nothing
# about the file being shipped to Railway.
import types
import mcp.server.fastmcp as _fastmcp


class _MCPServerCompat(_fastmcp.FastMCP):
    """Same shim, tolerant of kwargs this older-API-shaped code passes
    (stateless_http, transport_security) that this sandbox's newer FastMCP
    signature doesn't accept. Only affects this local test process."""

    def streamable_http_app(self, *a, **kw):
        kw.pop("stateless_http", None)
        kw.pop("transport_security", None)
        return super().streamable_http_app()


_shim = types.ModuleType("mcp.server.mcpserver")
_shim.MCPServer = _MCPServerCompat
sys.modules["mcp.server.mcpserver"] = _shim
# --------------------------------------------------------------------------

import server_remote as sr  # noqa: E402
from starlette.requests import Request
from starlette.testclient import TestClient


class FakeAnthropicResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"content": [{"type": "text", "text": "The book's LCR is 1.52 (152%)."}]}


class ChatConnectorAuthFix(unittest.TestCase):

    def test_mcp_servers_payload_includes_internal_authorization_token(self):
        """The exact payload server_remote sends to Anthropic must now carry
        authorization_token, or the 400 recurs regardless of anything else."""
        captured = {}

        async def fake_post(self_client, url, headers=None, json=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeAnthropicResponse()

        req = self._make_request(b'{"message": "What happens if I sell my treasuries?", "history": []}')

        with patch("httpx.AsyncClient.post", new=fake_post):
            resp = asyncio.run(sr.chat(req))

        self.assertEqual(resp.status_code, 200, "chat() should succeed end to end")
        mcp_servers = captured["json"]["mcp_servers"]
        self.assertEqual(len(mcp_servers), 1)
        entry = mcp_servers[0]
        self.assertIn("authorization_token", entry, "mcp_servers entry is missing authorization_token -- the 400 will recur")
        self.assertEqual(entry["authorization_token"], sr.INTERNAL_CHAT_TOKEN)
        self.assertEqual(entry["url"], sr.SELF_MCP_URL)
        # sanity: unrelated fields untouched
        self.assertEqual(captured["json"]["model"], sr.CHAT_MODEL)
        self.assertEqual(captured["headers"]["x-api-key"], sr.ANTHROPIC_API_KEY)

    def test_internal_token_authenticates_to_own_mcp_endpoint(self):
        """The token /chat now sends must itself be accepted by the /mcp gate --
        otherwise Anthropic's callback into /mcp would 401 even after the 400 is fixed."""
        client = self._build_test_client()
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {sr.INTERNAL_CHAT_TOKEN}"})
        self.assertNotEqual(resp.status_code, 401, "internal chat token was rejected by /mcp's own gate")

    def test_external_requests_without_any_token_still_rejected(self):
        """Regression guard: fixing the self-call must not loosen the OAuth gate
        that Sergio/claude.ai rely on for external access."""
        client = self._build_test_client()
        resp = client.get("/mcp")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_still_rejected(self):
        client = self._build_test_client()
        resp = client.get("/mcp", headers={"Authorization": "Bearer totally-wrong-token"})
        self.assertEqual(resp.status_code, 401)

    def test_valid_oauth_token_still_accepted(self):
        """Regression guard: real OAuth tokens (what Sergio/the user actually use)
        must still work -- the fix only ADDS an accepted token, never replaces the check."""
        sr._oauth_tokens["a-real-oauth-token"] = {"client_id": "test", "issued_at": 0}
        client = self._build_test_client()
        resp = client.get("/mcp", headers={"Authorization": "Bearer a-real-oauth-token"})
        self.assertNotEqual(resp.status_code, 401)

    def test_missing_anthropic_key_still_returns_503_not_a_400(self):
        """Unrelated existing behavior must be untouched by this fix."""
        old = sr.ANTHROPIC_API_KEY
        sr.ANTHROPIC_API_KEY = None
        try:
            req = self._make_request(b'{"message": "hi", "history": []}')
            resp = asyncio.run(sr.chat(req))
            self.assertEqual(resp.status_code, 503)
        finally:
            sr.ANTHROPIC_API_KEY = old

    # -- helpers --
    def _make_request(self, body: bytes) -> Request:
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        scope = {
            "type": "http", "method": "POST", "path": "/chat",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
        }
        return Request(scope, receive=receive)

    def _build_test_client(self):
        app = sr.mcp.streamable_http_app()
        app.add_middleware(sr.ApiKeyMiddleware)
        return TestClient(app, raise_server_exceptions=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RequiredTestPrompts(unittest.TestCase):
    """The two exact prompts from the bug report, run through the real chat()
    function end to end (Anthropic call mocked -- no real API key used)."""

    PROMPTS = [
        "What happens if I sell my treasuries?",
        "What happens if I sell half my treasuries and buy back 30% in digital "
        "equities and hold the remaining difference in cash?",
    ]

    def test_both_reported_prompts_no_longer_400(self):
        async def fake_post(self_client, url, headers=None, json=None, **kw):
            # Assert the fix is present on EVERY call this endpoint would make,
            # for EITHER prompt, not just a single hardcoded case.
            assert json["mcp_servers"][0].get("authorization_token") == sr.INTERNAL_CHAT_TOKEN, \
                "regression: outbound call missing authorization_token again"
            return FakeAnthropicResponse()

        with patch("httpx.AsyncClient.post", new=fake_post):
            for prompt in self.PROMPTS:
                body = json_module.dumps({"message": prompt, "history": []}).encode()
                req = ChatConnectorAuthFix()._make_request(body)
                resp = asyncio.run(sr.chat(req))
                self.assertEqual(resp.status_code, 200, f"prompt still failing: {prompt!r}")


import json as json_module  # noqa: E402

if __name__ == "__main__":
    unittest.main(verbosity=2)