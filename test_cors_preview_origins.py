"""CORS allow-list for Netlify deploy previews of aeonic.digital.

Never calls Anthropic. Verifies exact-origin allow-list is unchanged and that the
optional CHAT_ALLOWED_ORIGIN_REGEX admits only this site's Netlify preview hosts.
"""
import importlib
import os
import sys
import unittest

os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
os.environ["MCP_OAUTH_USER"] = "sonarx"
os.environ["MCP_OAUTH_PASSWORD"] = "test-password-not-real"
os.environ.pop("AEONIC_MCP_API_KEY", None)
sys.path.insert(0, os.path.dirname(__file__))

import server_remote  # noqa: F401  (fails fast if the module cannot import)
from starlette.testclient import TestClient  # noqa: E402

REGEX = r"^https://([a-z0-9]+|deploy-preview-[0-9]+)--charming-rabanadas-898d4a\.netlify\.app$"
PREVIEW = "https://6ad24375a84da8e0b8cad0be--charming-rabanadas-898d4a.netlify.app"


def preflight(origin, regex):
    if regex is None:
        os.environ.pop("CHAT_ALLOWED_ORIGIN_REGEX", None)
    else:
        os.environ["CHAT_ALLOWED_ORIGIN_REGEX"] = regex
    sys.modules.pop("server_remote", None)
    sr = importlib.import_module("server_remote")
    c = TestClient(sr.build_app())
    return c.options("/chat", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })


class PreviewCors(unittest.TestCase):
    def test_default_unchanged_preview_rejected(self):
        self.assertEqual(preflight(PREVIEW, None).status_code, 400)

    def test_default_production_allowed(self):
        r = preflight("https://aeonic.digital", None)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["access-control-allow-origin"], "https://aeonic.digital")

    def test_regex_allows_this_sites_previews_only(self):
        for o in (PREVIEW, "https://deploy-preview-12--charming-rabanadas-898d4a.netlify.app"):
            r = preflight(o, REGEX)
            self.assertEqual(r.status_code, 200, o)
            self.assertEqual(r.headers["access-control-allow-origin"], o)
        for o in (
            "https://evil--charming-rabanadas-898d4a.netlify.app.evil.com",
            "https://6ad2--other-site.netlify.app",
            "http://6ad2--charming-rabanadas-898d4a.netlify.app",
            "https://charming-rabanadas-898d4a.netlify.app",
            "https://evil.com",
        ):
            self.assertEqual(preflight(o, REGEX).status_code, 400, o)

    def test_unanchored_or_wildcard_regex_refused(self):
        for bad in (".*", r"https://.*\.netlify\.app", r"^https://.*"):
            with self.assertRaises(RuntimeError):
                preflight("https://aeonic.digital", bad)
        os.environ.pop("CHAT_ALLOWED_ORIGIN_REGEX", None)


if __name__ == "__main__":
    unittest.main()
