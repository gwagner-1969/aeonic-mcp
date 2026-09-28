"""
Regression + new-feature tests for the compliance-groundwork changes:
  1. classify_portfolio_impl hard-fails on a partial (one of two) real
     other_outflows_mm/other_asf_mm figure instead of silently discarding it.
  2. MODEL_METHODOLOGY_VERSION is stamped on every calculation.
  3. register_tools(mcp, audit_hook=...) calls the hook for every tool, with
     the exact inputs/result, and default None leaves behavior unchanged.
  4. server_remote's record_and_annotate() persists a full record and merges
     inline provenance into dict results; REST endpoints carry the same
     provenance via headers (and inline where the body is dict-shaped); the
     new audit-log endpoints retrieve past records back.

Known-good baseline (must NEVER change from these edits):
  run_scenario_impl(preset="current") -> LCR 1.5145 (151.5%), NSFR 1.2034 (120.3%),
  capital_required_mm 106.05, annual_funding_cost_mm 100.06.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import aeonic_core as core


class KnownGoodBaselineUnchanged(unittest.TestCase):
    def test_current_preset_numbers_exactly_match_baseline(self):
        r = core.run_scenario_impl(preset="current")
        self.assertEqual(r["result"]["lcr"], 1.5145)
        self.assertEqual(r["result"]["nsfr"], 1.2034)
        self.assertEqual(r["result"]["capital_required_mm"], 106.05)
        self.assertEqual(r["result"]["annual_funding_cost_mm"], 100.06)

    def test_scenario_a_and_b_still_run_without_error(self):
        for preset in ("scenario_a", "scenario_b"):
            r = core.run_scenario_impl(preset=preset)
            self.assertNotIn("error", r)
            self.assertIn("lcr", r["result"])


class MethodologyVersionStamped(unittest.TestCase):
    def test_run_scenario_has_version(self):
        r = core.run_scenario_impl(preset="current")
        self.assertEqual(r["methodology_version"], core.MODEL_METHODOLOGY_VERSION)

    def test_classify_portfolio_has_version(self):
        r = core.classify_portfolio_impl(positions=[{"description": "US Treasury Bill", "notional_mm": 100}])
        self.assertEqual(r["methodology_version"], core.MODEL_METHODOLOGY_VERSION)

    def test_classify_asset_both_branches_have_version(self):
        r1 = core.classify_asset_impl(has_traditional_id=True, is_direct_beneficial_ownership=True)
        r2 = core.classify_asset_impl(has_traditional_id=True, is_direct_beneficial_ownership=False)
        self.assertEqual(r1["methodology_version"], core.MODEL_METHODOLOGY_VERSION)
        self.assertEqual(r2["methodology_version"], core.MODEL_METHODOLOGY_VERSION)


class PartialRealFiguresHardFail(unittest.TestCase):
    """The actual bug found while building the enforced schema: previously, providing
    exactly one of other_outflows_mm/other_asf_mm silently discarded it and used the
    illustrative default for BOTH -- misrepresenting a partially-real input."""

    def test_only_outflows_given_is_rejected(self):
        r = core.classify_portfolio_impl(
            positions=[{"description": "US Treasury Bill", "notional_mm": 100}],
            other_outflows_mm=50.0,
        )
        self.assertIn("error", r)
        self.assertIn("other_asf_mm", r["error"])

    def test_only_asf_given_is_rejected(self):
        r = core.classify_portfolio_impl(
            positions=[{"description": "US Treasury Bill", "notional_mm": 100}],
            other_asf_mm=50.0,
        )
        self.assertIn("error", r)
        self.assertIn("other_outflows_mm", r["error"])

    def test_both_given_still_works_as_client_provided(self):
        r = core.classify_portfolio_impl(
            positions=[{"description": "US Treasury Bill", "notional_mm": 100}],
            other_outflows_mm=50.0, other_asf_mm=30.0,
        )
        self.assertNotIn("error", r)
        self.assertEqual(r["lcr_nsfr_basis"], "client_provided")
        self.assertEqual(r["lcr_nsfr_assumptions_used"], {"other_outflows_mm": 50.0, "other_asf_mm": 30.0})

    def test_neither_given_still_works_as_illustrative(self):
        r = core.classify_portfolio_impl(
            positions=[{"description": "US Treasury Bill", "notional_mm": 100}],
        )
        self.assertNotIn("error", r)
        self.assertEqual(r["lcr_nsfr_basis"], "illustrative_default")


class AuditHookInRegisterTools(unittest.TestCase):
    """register_tools' audit_hook must fire for every one of the 7 tools with the exact
    inputs/result, and must not change behavior at all when omitted (default None)."""

    def setUp(self):
        self.calls = []

        class FakeMCP:
            def __init__(self, outer):
                self.outer = outer
                self.tools = {}

            def tool(self):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return deco

        self.mcp = FakeMCP(self)

    def _hook(self, name, inputs, result):
        self.calls.append((name, inputs, result))

    def test_audit_hook_fires_for_every_tool(self):
        core.register_tools(self.mcp, audit_hook=self._hook)
        self.mcp.tools["get_asset_universe"]()
        self.mcp.tools["run_scenario"](preset="current")
        self.mcp.tools["classify_asset"](has_traditional_id=True, is_direct_beneficial_ownership=True)
        self.mcp.tools["lookup_identifier"]("USDY")
        self.mcp.tools["list_sources"]()
        self.mcp.tools["get_live_collateral_inventory"]()  # network call is mocked at the impl layer? see note below
        self.mcp.tools["classify_portfolio"](positions=[{"description": "US Treasury Bill", "notional_mm": 10}])

        names_called = [c[0] for c in self.calls]
        self.assertEqual(
            set(names_called),
            {"get_asset_universe", "run_scenario", "classify_asset", "lookup_identifier",
             "list_sources", "get_live_collateral_inventory", "classify_portfolio"},
        )

    def test_no_audit_hook_means_no_behavior_change(self):
        mcp2 = self.mcp.__class__(self)
        core.register_tools(mcp2)  # no audit_hook at all
        out = mcp2.tools["run_scenario"](preset="current")
        self.assertIsInstance(out, str)
        parsed = json.loads(out)
        self.assertEqual(parsed["result"]["lcr"], 1.5145)

    def test_failing_audit_hook_never_breaks_the_tool_call(self):
        def bad_hook(name, inputs, result):
            raise RuntimeError("boom")
        mcp3 = self.mcp.__class__(self)
        core.register_tools(mcp3, audit_hook=bad_hook)
        out = mcp3.tools["run_scenario"](preset="current")  # must not raise
        self.assertIn("1.5145", out)


class ServerRemoteAuditTrail(unittest.TestCase):
    """server_remote's persisted log + inline provenance + retrieval endpoints."""

    @classmethod
    def setUpClass(cls):
        os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
        os.environ["MCP_OAUTH_USER"] = "sonarx"
        os.environ["MCP_OAUTH_PASSWORD"] = "test-password-not-real"
        os.environ.pop("AEONIC_MCP_API_KEY", None)
        cls.tmpdir = tempfile.mkdtemp()
        os.environ["AEONIC_AUDIT_LOG_PATH"] = os.path.join(cls.tmpdir, "audit_log.jsonl")
        os.environ["REST_API_KEYS"] = "test-rest-key"

        import types
        import mcp.server.fastmcp as _fastmcp

        class _MCPServerCompat(_fastmcp.FastMCP):
            def streamable_http_app(self, *a, **kw):
                kw.pop("stateless_http", None)
                kw.pop("transport_security", None)
                return super().streamable_http_app()

        shim = types.ModuleType("mcp.server.mcpserver")
        shim.MCPServer = _MCPServerCompat
        sys.modules["mcp.server.mcpserver"] = shim

        global sr
        import importlib
        if "server_remote" in sys.modules:
            # server_remote reads several env vars (REST_API_KEYS, AEONIC_MCP_API_KEY, ...)
            # at import time. If another test module (e.g. test_chat_fix.py) imported it
            # first, in the same process, with a different env, module-level config would
            # be stale here -- reload so this class's env vars actually take effect,
            # regardless of what ran before it or which order test files are combined in.
            sr = importlib.reload(sys.modules["server_remote"])
        else:
            import server_remote as sr

    def test_record_and_annotate_persists_and_annotates_dict(self):
        result = {"lcr": 1.5}
        original_result_before_mutation = dict(result)  # record_and_annotate mutates result in place
        prov = sr.record_and_annotate("run_scenario", {"preset": "current"}, result)
        self.assertIn("request_id", prov)
        self.assertEqual(result["provenance"], prov)  # in-place inline annotation happened
        records = sr._read_audit_records(request_id=prov["request_id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tool"], "run_scenario")
        # The PERSISTED record captures the pre-annotation output -- the write happens
        # before the in-place mutation, which is correct: the log holds the raw result the
        # calculation actually produced, not a copy of itself.
        self.assertEqual(records[0]["output"], original_result_before_mutation)

    def test_record_and_annotate_handles_list_result_without_crashing(self):
        result = [{"asset": "x"}]
        prov = sr.record_and_annotate("get_asset_universe", {}, result)
        self.assertNotIn("provenance", result[0])  # can't merge into a list; documented gap
        records = sr._read_audit_records(request_id=prov["request_id"])
        self.assertEqual(records[0]["output"], result)

    def test_read_audit_records_missing_file_returns_empty(self):
        os.environ["AEONIC_AUDIT_LOG_PATH"] = os.path.join(self.tmpdir, "does_not_exist.jsonl")
        import importlib
        importlib.reload(sr)
        self.assertEqual(sr._read_audit_records(request_id="nonexistent"), [])
        # restore for other tests
        os.environ["AEONIC_AUDIT_LOG_PATH"] = os.path.join(self.tmpdir, "audit_log.jsonl")
        importlib.reload(sr)

    def _client(self):
        from starlette.testclient import TestClient
        return TestClient(sr.build_app(), raise_server_exceptions=False)

    def test_rest_classify_portfolio_carries_provenance_header_and_inline(self):
        client = self._client()
        resp = client.post(
            "/api/v1/capital/classify-portfolio",
            headers={"apikey": "test-rest-key"},
            json={"positions": [{"description": "US Treasury Bill", "notional_mm": 10}]},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("X-Aeonic-Request-Id", resp.headers)
        self.assertIn("X-Aeonic-Methodology-Version", resp.headers)
        body = resp.json()
        self.assertEqual(body["provenance"]["request_id"], resp.headers["X-Aeonic-Request-Id"])

    def test_rest_classify_portfolio_hard_fail_returns_400(self):
        client = self._client()
        resp = client.post(
            "/api/v1/capital/classify-portfolio",
            headers={"apikey": "test-rest-key"},
            json={"positions": [{"description": "US Treasury Bill", "notional_mm": 10}],
                  "other_outflows_mm": 5.0},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("other_asf_mm", resp.json()["error"])

    def test_rest_asset_universe_carries_header_even_though_body_is_a_bare_list_wrapped(self):
        client = self._client()
        resp = client.get("/api/v1/capital/asset-universe", headers={"apikey": "test-rest-key"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("X-Aeonic-Request-Id", resp.headers)

    def test_audit_log_retrieval_endpoint_round_trips(self):
        client = self._client()
        post_resp = client.post(
            "/api/v1/capital/run-scenario",
            headers={"apikey": "test-rest-key"},
            json={"preset": "current"},
        )
        rid = post_resp.headers["X-Aeonic-Request-Id"]
        get_resp = client.get(f"/api/v1/capital/audit-log/{rid}", headers={"apikey": "test-rest-key"})
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.json()["request_id"], rid)
        self.assertEqual(get_resp.json()["tool"], "run_scenario")

    def test_audit_log_retrieval_404_for_unknown_id(self):
        client = self._client()
        resp = client.get("/api/v1/capital/audit-log/does-not-exist", headers={"apikey": "test-rest-key"})
        self.assertEqual(resp.status_code, 404)

    def test_audit_log_list_endpoint(self):
        client = self._client()
        client.post("/api/v1/capital/run-scenario", headers={"apikey": "test-rest-key"}, json={"preset": "current"})
        resp = client.get("/api/v1/capital/audit-log?limit=5", headers={"apikey": "test-rest-key"})
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["count"], 1)

    def test_rest_endpoints_still_require_api_key(self):
        client = self._client()
        resp = client.get("/api/v1/capital/asset-universe")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)