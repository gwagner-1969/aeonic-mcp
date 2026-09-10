"""
Aeonic Digital Collateral Intelligence — shared model core.

All data, calculation logic, and tool definitions live here. Both the local
(stdio, Tier 0) and remote (streamable-http, Tier 1) servers import this
module and register the same tools against their own MCPServer instance, so
the two deployments can never drift out of sync with each other.

This is a direct extraction of the Tier 0 server's logic — no formulas were
changed. run_scenario(preset="current") still returns LCR 151.5%, NSFR
120.3%, capital $106.05mm, funding cost $100.06mm, matching the workbook
and the aeonic.vc web tool exactly.
"""

import json
from typing import Optional

import httpx

# =====================================================================
# Static reference data
# =====================================================================

ASSETS = [
    {"name": "Cash / Central Bank Reserves", "level": "Level 1", "haircut": 0.000, "rsf": 0.00,
     "rw": 0.00, "tenor": "O/N", "spread_bps": 0, "settlement": "T+0",
     "notes": "Basel Level 1 HQLA — no haircut."},
    {"name": "U.S. Treasuries (Traditional)", "level": "Level 1", "haircut": 0.005, "rsf": 0.05,
     "rw": 0.00, "tenor": "O/N", "spread_bps": 0, "settlement": "T+1",
     "notes": "Basel Level 1 HQLA sovereign debt."},
    {"name": "U.S. Treasuries (DTCC-Tokenized)", "level": "Level 1", "haircut": 0.005, "rsf": 0.05,
     "rw": 0.00, "tenor": "1M", "spread_bps": -3, "settlement": "Near-instant",
     "notes": "Same HQLA treatment as traditional UST under SEC No-Action Letter; funding benefit "
              "is an illustrative settlement-speed assumption, not observed market pricing."},
    {"name": "Agency MBS", "level": "Level 2A", "haircut": 0.150, "rsf": 0.15,
     "rw": 0.20, "tenor": "1M", "spread_bps": 5, "settlement": "T+2",
     "notes": "Basel Level 2A HQLA."},
    {"name": "Investment-Grade Corporates", "level": "Level 2B", "haircut": 0.500, "rsf": 0.50,
     "rw": 1.00, "tenor": "3M", "spread_bps": 15, "settlement": "T+2",
     "notes": "Basel Level 2B HQLA; subject to the 15% sub-cap within total HQLA."},
    {"name": "Major-Index ETFs (Traditional)", "level": "Level 2A", "haircut": 0.250, "rsf": 0.20,
     "rw": 0.20, "tenor": "3M", "spread_bps": 10, "settlement": "T+1",
     "notes": "Simplified look-through treatment as Level 2A equity-index ETF collateral."},
    {"name": "Major-Index ETFs (DTCC-Tokenized)", "level": "Level 2A", "haircut": 0.250, "rsf": 0.20,
     "rw": 0.20, "tenor": "1M", "spread_bps": -2, "settlement": "Near-instant",
     "notes": "Authorized under SEC No-Action Letter; same HQLA treatment as traditional."},
    {"name": "Russell 1000 Equities (DTCC-Tokenized)", "level": "Not HQLA", "haircut": 0.000, "rsf": 0.85,
     "rw": 1.00, "tenor": "3M", "spread_bps": 20, "settlement": "Near-instant",
     "notes": "Authorized under SEC No-Action Letter; not HQLA-eligible — modeled as posted "
              "collateral only."},
    {"name": "Non-HQLA Loans / Other", "level": "Not HQLA", "haircut": 0.000, "rsf": 1.00,
     "rw": 1.00, "tenor": "6M", "spread_bps": 30, "settlement": "Varies",
     "notes": "Not HQLA-eligible; illustrative catch-all for less-liquid assets."},
]
ASSET_NAMES = [a["name"] for a in ASSETS]

TENORS = {
    "O/N": {"secured": 0.0480, "asf": 0.00, "outflow_eligible": True},
    "1M":  {"secured": 0.0485, "asf": 0.00, "outflow_eligible": True},
    "3M":  {"secured": 0.0490, "asf": 0.00, "outflow_eligible": False},
    "6M":  {"secured": 0.0495, "asf": 0.50, "outflow_eligible": False},
    "1Y":  {"secured": 0.0505, "asf": 0.90, "outflow_eligible": False},
    "2Y+": {"secured": 0.0520, "asf": 1.00, "outflow_eligible": False},
}

CONST = {
    "total_book": 2000.0,
    "capital_ratio": 0.105,
    "level2_cap": 0.40,
    "level2b_cap": 0.15,
    "return_on_capital": 0.12,
    "other_outflows": 550.0,
    "other_asf": 800.0,
}

PRESETS = {
    "current": [0.025, 0.20, 0.05, 0.15, 0.125, 0.10, 0.025, 0.075, 0.25],
    "scenario_a": [0.025, 0.15, 0.10, 0.15, 0.125, 0.075, 0.05, 0.075, 0.25],
    "scenario_b": [0.025, 0.05, 0.35, 0.10, 0.075, 0.025, 0.125, 0.10, 0.15],
}

CROSSWALK = [
    {"asset": "BlackRock USD Institutional Digital Liquidity Fund (BUIDL)", "underlying_type": "US Treasury MMF interest",
     "traditional_id": "Lookup required (transfer agent)", "digital_id": "Lookup required (DTIF registry)",
     "networks": "Ethereum, Solana, Aptos, Avalanche, Binance, Optimism, Arbitrum, Polygon",
     "beneficial_ownership": "Direct registered fund interest",
     "notes": "Largest tokenized Treasury product, live figure via get_live_collateral_inventory."},
    {"asset": "Franklin OnChain U.S. Government Money Fund (BENJI)", "underlying_type": "US Govt MMF interest",
     "traditional_id": "Lookup required (transfer agent)", "digital_id": "Lookup required (DTIF registry)",
     "networks": "Stellar (majority of AUM), Polygon, Ethereum, Arbitrum, Avalanche, Base, Aptos, Solana",
     "beneficial_ownership": "Direct registered fund interest",
     "notes": "SEC-registered 1940 Act fund; not on DefiLlama, tracked by rwa.xyz (paid API)."},
    {"asset": "Superstate Short Duration US Government Securities Fund (USTB)", "underlying_type": "US T-Bill fund interest",
     "traditional_id": "N/A — private fund, Qualified Purchasers only", "digital_id": "Lookup required (DTIF registry)",
     "networks": "Ethereum (single-chain by deliberate compliance choice)",
     "beneficial_ownership": "Direct registered fund interest (Delaware statutory trust)",
     "notes": "Token + Chainlink oracle addresses verified from Superstate's own developer docs; "
              "live figure via get_live_collateral_inventory."},
    {"asset": "DTCC-tokenized U.S. Treasuries", "underlying_type": "US Treasury (digital twin)",
     "traditional_id": "Same CUSIP/ISIN as underlying UST", "digital_id": "N/A — digital twin retains underlying identifier",
     "networks": "HyperLedger Besu / Canton", "beneficial_ownership": "Direct — same entitlements as traditional form",
     "notes": "SEC No-Action Letter scope; DTC-held, convertible both directions."},
    {"asset": "DTCC-tokenized Russell 1000 equities / major-index ETFs", "underlying_type": "Equity / ETF (digital twin)",
     "traditional_id": "Same CUSIP/ISIN as underlying security", "digital_id": "N/A — digital twin",
     "networks": "HyperLedger Besu / Canton", "beneficial_ownership": "Direct — same entitlements as traditional form",
     "notes": "Same SEC No-Action Letter scope as above."},
    {"asset": "Canton Network tokenized Gilts / EGBs (intraday repo)", "underlying_type": "Sovereign debt (digital twin)",
     "traditional_id": "Same ISIN as underlying Gilt/EGB", "digital_id": "N/A — digital twin",
     "networks": "Canton", "beneficial_ownership": "Direct — same entitlements as traditional form",
     "notes": "Cross-border, cross-currency intraday repo, confirmed live."},
    {"asset": "Coinbase Prime custodied Bitcoin", "underlying_type": "Native crypto asset (not a security)",
     "traditional_id": "N/A — not a security", "digital_id": "4H95J0R2X (Bitcoin, DTI Foundation registry)",
     "networks": "Bitcoin", "beneficial_ownership": "Direct (custodied or self-custodied)",
     "notes": "Not HQLA-eligible under Basel; separate prudential crypto-asset treatment."},
    {"asset": "Coinbase Prime custodied Ethereum", "underlying_type": "Native crypto asset (not a security)",
     "traditional_id": "N/A — not a security", "digital_id": "XB0MQJ1K5 (Ethereum, DTI Foundation registry)",
     "networks": "Ethereum", "beneficial_ownership": "Direct (custodied or self-custodied)",
     "notes": "Not HQLA-eligible under Basel; separate prudential crypto-asset treatment."},
]

SOURCES = [
    {"source": "Anchorage Digital", "type": "Custodian API", "provides": "Custodied crypto & tokenized-asset positions",
     "ownership_visibility": "Yes — OCC-chartered qualified custodian", "status": "Requires data partnership"},
    {"source": "Fireblocks", "type": "Infrastructure / API (not itself a custodian)",
     "provides": "Wallet-level positions & policy data for platforms built on it",
     "ownership_visibility": "Depends on the underlying custodian", "status": "Requires data partnership"},
    {"source": "Taurus (via State Street)", "type": "Custodian API (Taurus-EXPLORER)",
     "provides": "Tokenized asset custody & lifecycle data", "ownership_visibility": "Yes",
     "status": "Requires data partnership"},
    {"source": "State Street Digital", "type": "Custodian / fund administration API",
     "provides": "Institutional tokenized fund administration & custody", "ownership_visibility": "Yes",
     "status": "Requires data partnership"},
    {"source": "BNY Digital Assets", "type": "Custodian API", "provides": "Digital asset custody",
     "ownership_visibility": "Yes", "status": "Requires data partnership"},
    {"source": "Broadridge Digital Ledger Repo", "type": "Platform / ledger data feed",
     "provides": "Tokenized repo transaction & inventory data",
     "ownership_visibility": "Yes, for platform participants", "status": "Requires data partnership"},
    {"source": "DefiLlama public API", "type": "Public API, no key required",
     "provides": "Live TVL for tokenized funds that report on-chain",
     "ownership_visibility": "Partial — shows TVL, not whose inventory it is", "status": "Live"},
    {"source": "Direct on-chain query (Ethereum RPC + Chainlink)", "type": "Public blockchain query, no key required",
     "provides": "Live supply + price computed straight from the token contract and its oracle",
     "ownership_visibility": "Partial — same caveat as above", "status": "Live"},
    {"source": "DTCC Tokenization Service", "type": "Market infrastructure feed",
     "provides": "Digital-twin issuance/conversion records for DTC-eligible assets",
     "ownership_visibility": "Yes, for DTC participant wallets", "status": "Requires DTC participant relationship"},
    {"source": "Canton Network / Global Synchronizer", "type": "Permissioned network data",
     "provides": "Intraday repo & collateral mobility transactions among validators",
     "ownership_visibility": "Yes, for network participants", "status": "Requires network participation"},
    {"source": "Existing securities-finance inventory feeds (traditional stock loan)", "type": "Existing market data feed",
     "provides": "Traditional securities lending inventory", "ownership_visibility": "Yes",
     "status": "Already standard practice"},
]

LIVE_SLUGS = {
    "blackrock-buidl": "BlackRock BUIDL",
    "circle-usyc": "Circle USYC (formerly Hashnote)",
    "ondo-yield-assets": "Ondo Yield Assets (OUSG/USDY)",
    "spiko": "Spiko (USTBL/EUTBL)",
    "centrifuge-protocol": "Centrifuge Protocol",
    "wisdomtree": "WisdomTree (WTGXX)",
}

USTB_TOKEN = "0x43415eB6ff9DB7E26A15b704e7A3eDCe97d31C4e"
USTB_ORACLE = "0x289B5036cd942e619E1Ee48670F98d214E745AAC"
ETH_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://cloudflare-eth.com",
    "https://rpc.ankr.com/eth",
    "https://eth.llamarpc.com",
]


# =====================================================================
# Model engine
# =====================================================================

def outflow_factor(level: str) -> float:
    return {"Level 1": 0.00, "Level 2A": 0.15, "Level 2B": 0.25}.get(level, 1.00)


def compute_metrics(allocation: list[float]) -> dict:
    """allocation: list of 9 fractions (0-1), same order as ASSETS, should sum to ~1.0"""
    notionals = [p * CONST["total_book"] for p in allocation]
    level1 = level2a = level2b = 0.0
    outflow_asset = rsf_total = asf_asset = rwa_total = funding_cost = 0.0

    for asset, notional in zip(ASSETS, notionals):
        tenor = TENORS[asset["tenor"]]
        hqla_val = 0.0 if asset["level"] == "Not HQLA" else notional * (1 - asset["haircut"])
        if asset["level"] == "Level 1":
            level1 += hqla_val
        elif asset["level"] == "Level 2A":
            level2a += hqla_val
        elif asset["level"] == "Level 2B":
            level2b += hqla_val
        of = outflow_factor(asset["level"])
        if tenor["outflow_eligible"]:
            outflow_asset += notional * of
        rsf_total += notional * asset["rsf"]
        asf_asset += notional * tenor["asf"]
        rwa_total += notional * asset["rw"]
        funding_cost += notional * (tenor["secured"] + asset["spread_bps"] / 10000)

    level2b_capped = min(level2b, (CONST["level2b_cap"] / (1 - CONST["level2b_cap"])) * (level1 + level2a))
    level2_capped = min(level2a + level2b_capped, (CONST["level2_cap"] / (1 - CONST["level2_cap"])) * level1)
    hqla_total = level1 + level2_capped
    total_outflow = outflow_asset + CONST["other_outflows"]
    lcr = hqla_total / total_outflow
    asf_total = asf_asset + CONST["other_asf"]
    nsfr = asf_total / rsf_total
    capital_required = rwa_total * CONST["capital_ratio"]

    return {
        "total_book_mm": round(CONST["total_book"], 1),
        "hqla_stock_mm": round(hqla_total, 1),
        "lcr": round(lcr, 4),
        "rsf_mm": round(rsf_total, 1),
        "asf_mm": round(asf_total, 1),
        "nsfr": round(nsfr, 4),
        "rwa_mm": round(rwa_total, 1),
        "capital_required_mm": round(capital_required, 2),
        "annual_funding_cost_mm": round(funding_cost, 2),
        "notionals_mm": {a["name"]: round(n, 1) for a, n in zip(ASSETS, notionals)},
    }


CURRENT_METRICS = compute_metrics(PRESETS["current"])


def _eth_call(client: httpx.Client, to: str, data: str) -> str:
    last_err = None
    for rpc in ETH_RPCS:
        try:
            r = client.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                        "params": [{"to": to, "data": data}, "latest"]}, timeout=10.0)
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise RuntimeError(body["error"].get("message", "RPC error"))
            return body["result"]
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("All RPC endpoints failed")


def get_ustb_live_aum() -> float:
    with httpx.Client() as client:
        supply_hex = _eth_call(client, USTB_TOKEN, "0x18160ddd")
        supply_dec_hex = _eth_call(client, USTB_TOKEN, "0x313ce567")
        price_hex = _eth_call(client, USTB_ORACLE, "0x50d25bcd")
        price_dec_hex = _eth_call(client, USTB_ORACLE, "0x313ce567")
    supply = int(supply_hex, 16) / (10 ** int(supply_dec_hex, 16))
    price = int(price_hex, 16) / (10 ** int(price_dec_hex, 16))
    return (supply * price) / 1e6


# =====================================================================
# Tool registration — call this once against any MCPServer instance
# =====================================================================

def register_tools(mcp) -> None:
    """Register all six Aeonic tools against the given MCPServer instance."""

    @mcp.tool()
    def get_asset_universe() -> str:
        """Return the full Asset Universe: every asset class in the Aeonic Capital & Liquidity
        Optimization Model with its HQLA level, haircut, RSF weight, risk weight, funding tenor,
        illustrative funding spread, settlement speed, and regulatory notes."""
        return json.dumps(ASSETS, indent=2)

    @mcp.tool()
    def run_scenario(allocation: Optional[dict[str, float]] = None, preset: Optional[str] = None,
                      shift_into: Optional[str] = None, shift_pct: Optional[float] = None) -> str:
        """Run the LCR/NSFR/Capital/funding-cost model for a given collateral allocation and
        compare it to the current book.

        Provide EXACTLY ONE of these three ways to specify the scenario:

          1. preset: one of "current", "scenario_a" (Tokenization Tilt), "scenario_b" (Aggressive Tokenization)

          2. shift_into + shift_pct: THE RIGHT CHOICE for questions like "what if I move 30% of the
             book into DTCC-tokenized Treasuries" or "shift 20 points into X". Give shift_into as an
             exact asset class name from get_asset_universe(), and shift_pct as the number of
             percentage points to move into it (e.g. 30 for "30%"). This adds shift_pct to that
             asset's current share and proportionally reduces every OTHER asset class to compensate,
             preserving their relative proportions to each other -- the correct interpretation of
             "shift X% into Y, keep the rest as-is". Do NOT try to construct this by hand via the
             'allocation' parameter below; the proportional math is easy to get wrong, which is why
             this dedicated option exists.

          3. allocation: a dict mapping EVERY ONE of the 9 asset class names to a percentage of book
             (0-100), summing to 100. Only use this when the caller wants to specify a full custom
             mix from scratch, not for "shift into X" style questions.

        Returns LCR, NSFR, HQLA stock, RWA, capital required, annual funding cost, and the deltas
        (capital released, funding cost saved, net annualized value created) versus the current book.
        """
        if preset:
            if preset not in PRESETS:
                return json.dumps({"error": f"Unknown preset '{preset}'. Use one of: {list(PRESETS.keys())}"})
            alloc = PRESETS[preset]
            shift_summary = None
        elif shift_into is not None and shift_pct is not None:
            if shift_into not in ASSET_NAMES:
                return json.dumps({
                    "error": f"Unknown asset class '{shift_into}'.",
                    "valid_asset_classes": ASSET_NAMES,
                })
            idx = ASSET_NAMES.index(shift_into)
            base = PRESETS["current"]
            s = shift_pct / 100.0
            cur_i = base[idx]
            denom = 1.0 - cur_i
            if s < 0 or s > denom + 1e-9:
                return json.dumps({
                    "error": f"Cannot shift {shift_pct} points into '{shift_into}': only "
                             f"{denom*100:.1f} percentage points are available to move from other "
                             f"asset classes (current share of '{shift_into}' is {cur_i*100:.1f}%).",
                })
            scale = (1 - s / denom) if denom > 1e-9 else 0.0
            alloc = [
                (cur_i + s) if j == idx else base[j] * scale
                for j in range(len(ASSET_NAMES))
            ]
            # Report the exact dollar figures for this shift so the caller never has to
            # (mis)calculate them itself -- this is the fix for a real observed failure
            # mode where the calling model invented a plausible-sounding but wrong dollar
            # amount instead of using this.
            shift_summary = {
                "asset": shift_into,
                "shift_pct_requested": shift_pct,
                "old_share_pct": round(cur_i * 100, 2),
                "new_share_pct": round((cur_i + s) * 100, 2),
                "old_notional_mm": round(cur_i * CONST["total_book"], 1),
                "new_notional_mm": round((cur_i + s) * CONST["total_book"], 1),
                "notional_added_mm": round(s * CONST["total_book"], 1),
            }
        elif allocation:
            alloc = [allocation.get(name, 0.0) / 100.0 for name in ASSET_NAMES]
            total = sum(alloc)
            if abs(total - 1.0) > 0.01:
                return json.dumps({
                    "error": f"Allocation sums to {total*100:.1f}%, not 100%. "
                             f"Provide percentages for all asset classes summing to 100.",
                    "valid_asset_classes": ASSET_NAMES,
                })
            shift_summary = None
        else:
            return json.dumps({
                "error": "Provide 'preset', or 'shift_into'+'shift_pct', or a full 'allocation'.",
                "valid_presets": list(PRESETS.keys()),
                "valid_asset_classes": ASSET_NAMES,
            })

        result = compute_metrics(alloc)
        capital_released = CURRENT_METRICS["capital_required_mm"] - result["capital_required_mm"]
        funding_savings = CURRENT_METRICS["annual_funding_cost_mm"] - result["annual_funding_cost_mm"]
        net_value = capital_released * CONST["return_on_capital"] + funding_savings

        response = {
            "result": result,
            "vs_current": {
                "lcr_current": CURRENT_METRICS["lcr"],
                "nsfr_current": CURRENT_METRICS["nsfr"],
                "capital_released_mm": round(capital_released, 2),
                "annual_funding_savings_mm": round(funding_savings, 2),
                "net_value_created_mm_per_year": round(net_value, 2),
            },
            "methodology_note": "Illustrative demonstration model with representative sample data. "
                                 "Regulatory factors are simplified approximations of Basel III, not a "
                                 "production regulatory-reporting engine.",
        }
        if shift_summary:
            response["shift_summary"] = shift_summary
        return json.dumps(response, indent=2)

    @mcp.tool()
    def classify_asset(has_traditional_id: bool, is_direct_beneficial_ownership: bool,
                        venue_recognizes_as_collateral: bool = True,
                        underlying_security_type: Optional[str] = None) -> str:
        """Run the Aeonic regulatory classification logic (5-step decision chain) to determine
        an asset's likely HQLA treatment.

        Args:
            has_traditional_id: True if the asset carries an ISIN/CUSIP (i.e. it's a tokenized
                traditional security), False if it's a native token with no ISIN/CUSIP.
            is_direct_beneficial_ownership: True if the holder has a direct registered/legal claim
                on the underlying asset, False if it's a synthetic or wrapped exposure that only
                tracks price without conferring ownership.
            venue_recognizes_as_collateral: True unless the specific venue has a known v1 gap
                (e.g. DTCC's tokenization service does not yet recognize its own issuance as
                collateral within its own risk framework, even though external venues can).
            underlying_security_type: optional free-text description of the underlying security
                (e.g. "US Treasury", "equity"), used only for the returned rationale text.
        """
        if not is_direct_beneficial_ownership:
            return json.dumps({
                "hqla_level": "Not HQLA (ineligible)",
                "step": 3,
                "rationale": "Wrapped or synthetic exposure that only tracks price, without a direct "
                             "registered claim on the underlying asset, is ineligible regardless of "
                             "what it tracks.",
            })

        if has_traditional_id:
            level = "Driven by the underlying security's own Basel classification (see get_asset_universe)"
            step = 1
            rationale = (f"An ISIN/CUSIP is present, so classification follows the underlying "
                         f"security's own type and issuer{' (' + underlying_security_type + ')' if underlying_security_type else ''} "
                         f"— never the technology wrapper.")
        else:
            level = "Not HQLA (default)"
            step = 2
            rationale = ("No traditional ID exists — this is a native token. Default treatment under "
                         "Basel is Not HQLA unless a jurisdiction-specific crypto-asset capital "
                         "treatment applies; don't assume an override without confirming it.")

        result = {"hqla_level": level, "step": step, "rationale": rationale}
        if not venue_recognizes_as_collateral:
            result["caveat"] = ("Venue-recognition gap flagged: the venue itself does not yet recognize "
                                 "its own tokenized issuance as collateral internally, even though the "
                                 "security's own HQLA eligibility (above) is unaffected.")
        return json.dumps(result, indent=2)

    @mcp.tool()
    def lookup_identifier(asset_name: str) -> str:
        """Look up the identifier crosswalk (traditional ISIN/CUSIP, digital DTI, networks, and
        beneficial ownership type) for a named digital collateral asset or fund. Matches loosely
        on the asset name (case-insensitive substring match)."""
        q = asset_name.strip().lower()
        matches = [row for row in CROSSWALK if q in row["asset"].lower()]
        if not matches:
            return json.dumps({
                "error": f"No match for '{asset_name}'.",
                "available_assets": [row["asset"] for row in CROSSWALK],
            })
        return json.dumps(matches, indent=2)

    @mcp.tool()
    def list_sources(status_filter: Optional[str] = None) -> str:
        """List all digital collateral data sources in the Aeonic sourcing architecture, with
        connectivity type and an honest status for each.

        Args:
            status_filter: optional exact-match filter, e.g. "Live" to show only sources that are
                genuinely connected without a data partnership. Omit to see all sources.
        """
        rows = SOURCES
        if status_filter:
            rows = [r for r in rows if status_filter.lower() in r["status"].lower()]
        return json.dumps(rows, indent=2)

    @mcp.tool()
    def get_live_collateral_inventory() -> str:
        """Fetch LIVE current AUM/TVL for tokenized collateral products, right now, from two free
        public data sources: DefiLlama's public API (6 products) and a direct Ethereum RPC + Chainlink
        oracle call for Superstate USTB (independent of any data vendor). Franklin BENJI is tracked
        by rwa.xyz, which requires a paid API key not configured here — its row is omitted; use
        lookup_identifier for its static reference data instead."""
        results = []
        errors = []

        try:
            resp = httpx.get("https://api.llama.fi/protocols", timeout=15.0)
            resp.raise_for_status()
            protocols = {p["slug"]: p for p in resp.json()}
            for slug, name in LIVE_SLUGS.items():
                p = protocols.get(slug)
                results.append({
                    "asset": name,
                    "source": "DefiLlama live API",
                    "tvl_aum_mm": round(p["tvl"] / 1e6, 1) if p else None,
                    "status": "Live" if p else "Not found in DefiLlama response",
                })
        except Exception as e:
            errors.append(f"DefiLlama fetch failed: {e}")

        ustb_result = {"asset": "Superstate USTB", "source": "Direct on-chain (Ethereum RPC + Chainlink oracle)"}
        try:
            ustb_result["tvl_aum_mm"] = round(get_ustb_live_aum(), 1)
            ustb_result["status"] = "Live — no data vendor"
        except Exception as e:
            ustb_result["tvl_aum_mm"] = None
            ustb_result["status"] = f"On-chain query failed: {e}"
        results.append(ustb_result)

        return json.dumps({"live_data": results, "errors": errors or None}, indent=2)
