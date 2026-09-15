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


def sft_lend_rsf(hqla_level: str, lend_tenor: str) -> float:
    """RSF for the LEND leg of a matched-book / SFT-style financing position -- a simplified
    approximation of Basel's short-residual-maturity secured-lending treatment.

    This is deliberately DIFFERENT from an asset's generic held-position RSF weight. A security
    lent out for 90 days is a 90-day secured loan to a counterparty, not a year-long holding of
    that asset type -- Basel's real SFT rules grade RSF by the LOAN's own tenor and whether it's
    backed by Level 1 HQLA collateral, not by how illiquid the underlying normally is when held
    outright. This does NOT replicate every nuance of BCBS 295 (counterparty-type distinctions,
    specific netting rules, jurisdictional variations) -- it's a directionally-correct, stated
    simplification, same discipline as the rest of this model.

    Short tenor + Level 1 HQLA collateral -> lowest (preferential) RSF.
    Short tenor + anything else -> a higher but still short-term-appropriate RSF.
    Longer tenors converge toward full RSF, since a long-dated lend is economically closer to an
    outright holding regardless of collateral quality.
    """
    short_tenors = ("O/N", "1M", "3M")
    if lend_tenor in short_tenors:
        return 0.10 if hqla_level == "Level 1" else 0.15
    if lend_tenor == "6M":
        return 0.50
    return 1.00  # 1Y, 2Y+: treat as effectively a full-tenor holding


def _compute_from_positions(positions: list[dict], other_outflows_mm: float, other_asf_mm: float,
                             include_lcr_nsfr: bool = True) -> dict:
    """Core engine, generalized to any list of positions (not just the fixed 9-asset book).
    Each position dict needs: notional_mm, level, haircut, rsf, rw, tenor, spread_bps.
    other_outflows_mm/other_asf_mm are firm-wide figures outside this position list --
    required for a meaningful LCR/NSFR (see classify_portfolio's docstring for why)."""
    level1 = level2a = level2b = 0.0
    outflow_asset = rsf_total = asf_asset = rwa_total = funding_cost = 0.0
    total_book = sum(p["notional_mm"] for p in positions)

    for p in positions:
        tenor = TENORS[p["tenor"]]
        notional = p["notional_mm"]
        hqla_val = 0.0 if p["level"] == "Not HQLA" else notional * (1 - p["haircut"])
        if p["level"] == "Level 1":
            level1 += hqla_val
        elif p["level"] == "Level 2A":
            level2a += hqla_val
        elif p["level"] == "Level 2B":
            level2b += hqla_val
        of = outflow_factor(p["level"])
        if tenor["outflow_eligible"]:
            outflow_asset += notional * of
        rsf_total += notional * p["rsf"]
        asf_asset += notional * tenor["asf"]
        rwa_total += notional * p["rw"]
        funding_cost += notional * (tenor["secured"] + p["spread_bps"] / 10000)

    level2b_capped = min(level2b, (CONST["level2b_cap"] / (1 - CONST["level2b_cap"])) * (level1 + level2a))
    level2_capped = min(level2a + level2b_capped, (CONST["level2_cap"] / (1 - CONST["level2_cap"])) * level1)
    hqla_total = level1 + level2_capped
    rwa_capital = rwa_total * CONST["capital_ratio"]

    result = {
        "total_book_mm": round(total_book, 1),
        "hqla_stock_mm": round(hqla_total, 1),
        "rsf_mm": round(rsf_total, 1),
        "rwa_mm": round(rwa_total, 1),
        "capital_required_mm": round(rwa_capital, 2),
        "annual_funding_cost_mm": round(funding_cost, 2),
    }
    if include_lcr_nsfr:
        total_outflow = outflow_asset + other_outflows_mm
        asf_total = asf_asset + other_asf_mm
        result["lcr"] = round(hqla_total / total_outflow, 4) if total_outflow > 0 else None
        result["nsfr"] = round(asf_total / rsf_total, 4) if rsf_total > 0 else None
        result["asf_mm"] = round(asf_total, 1)
    return result


def compute_metrics(allocation: list[float]) -> dict:
    """allocation: list of 9 fractions (0-1), same order as ASSETS, should sum to ~1.0"""
    notionals = [p * CONST["total_book"] for p in allocation]
    positions = [
        {"notional_mm": n, "level": a["level"], "haircut": a["haircut"], "rsf": a["rsf"],
         "rw": a["rw"], "tenor": a["tenor"], "spread_bps": a["spread_bps"]}
        for a, n in zip(ASSETS, notionals)
    ]
    result = _compute_from_positions(positions, CONST["other_outflows"], CONST["other_asf"])
    result["notionals_mm"] = {a["name"]: round(n, 1) for a, n in zip(ASSETS, notionals)}
    return result


CURRENT_METRICS = compute_metrics(PRESETS["current"])


# =====================================================================
# Real-portfolio classification (distinct from the illustrative 9-asset
# book above). Three confidence tiers, all surfaced to the caller so
# nothing is silently guessed.
# =====================================================================

# Default regulatory attributes per HQLA bucket, used only when a position
# doesn't match one of the 9 known asset classes exactly. These are
# reasonable Basel-style defaults for that bucket, not client-specific --
# always lower confidence than an exact match.
LEVEL_DEFAULTS = {
    "Level 1": {"haircut": 0.005, "rsf": 0.05, "rw": 0.00, "tenor": "O/N", "spread_bps": 0},
    "Level 2A": {"haircut": 0.25, "rsf": 0.20, "rw": 0.20, "tenor": "3M", "spread_bps": 10},
    "Level 2B": {"haircut": 0.50, "rsf": 0.50, "rw": 1.00, "tenor": "3M", "spread_bps": 15},
    "Not HQLA": {"haircut": 0.00, "rsf": 1.00, "rw": 1.00, "tenor": "6M", "spread_bps": 30},
}

# Keyword -> known asset class, checked most-specific-first (tokenized
# variants before their generic counterparts) so "tokenized treasury"
# doesn't get caught by the generic "treasury" keyword.
KNOWN_CATEGORY_KEYWORDS = [
    (["tokenized treasur", "digital treasur", "dtcc treasur", "dtcc-tokenized treasur"],
     "U.S. Treasuries (DTCC-Tokenized)"),
    (["treasur", "t-bill", "t-note", "t-bond", "government bond", "sovereign debt"],
     "U.S. Treasuries (Traditional)"),
    (["tokenized etf", "digital etf", "dtcc-tokenized etf", "dtcc etf"],
     "Major-Index ETFs (DTCC-Tokenized)"),
    (["etf", "exchange-traded fund", "exchange traded fund"],
     "Major-Index ETFs (Traditional)"),
    (["tokenized equit", "tokenized stock", "russell 1000", "dtcc-tokenized equit"],
     "Russell 1000 Equities (DTCC-Tokenized)"),
    (["agency mbs", "mortgage-backed", "mortgage backed", "gnma", "fnma", "freddie mac", "fannie mae"],
     "Agency MBS"),
    (["investment-grade corporate", "investment grade corporate", "ig corporate", "corporate bond"],
     "Investment-Grade Corporates"),
    (["cash", "central bank reserve", "central bank deposit"],
     "Cash / Central Bank Reserves"),
]

# Keyword -> HQLA level, used only when no known-category match is found.
# Order matters: more specific / higher-quality indicators first.
HEURISTIC_LEVEL_KEYWORDS = [
    (["sovereign", "government-guaranteed", "supranational", "multilateral development bank"], "Level 1"),
    (["agency", "gse", "covered bond"], "Level 2A"),
    (["corporate bond", "convertible bond", "investment grade"], "Level 2B"),
    (["equit", "common stock", "preferred stock", "private credit", "loan", "receivable",
      "real estate", "commodit"], "Not HQLA"),
]


def classify_position(description: str) -> dict:
    """Classify one free-text position description. Returns level, the regulatory
    attributes to use, a confidence tier, and a human-readable rationale."""
    desc_lower = description.lower()

    for keywords, asset_name in KNOWN_CATEGORY_KEYWORDS:
        if any(kw in desc_lower for kw in keywords):
            asset = next(a for a in ASSETS if a["name"] == asset_name)
            return {
                "level": asset["level"],
                "attributes": {k: asset[k] for k in ["haircut", "rsf", "rw", "tenor", "spread_bps"]},
                "confidence": "exact_match",
                "matched_category": asset_name,
                "rationale": f"Matched known asset category '{asset_name}' -- using its exact "
                             f"regulatory factors from the Asset Universe.",
            }

    for keywords, level in HEURISTIC_LEVEL_KEYWORDS:
        if any(kw in desc_lower for kw in keywords):
            return {
                "level": level,
                "attributes": LEVEL_DEFAULTS[level],
                "confidence": "heuristic",
                "matched_category": None,
                "rationale": f"No exact match in the known asset universe. Keyword pattern suggests "
                             f"{level} -- using default {level} regulatory assumptions, NOT a "
                             f"client-specific determination. Verify before relying on this.",
            }

    return {
        "level": "Not HQLA",
        "attributes": LEVEL_DEFAULTS["Not HQLA"],
        "confidence": "unclassified",
        "matched_category": None,
        "rationale": "Could not confidently classify this description. Defaulted to Not HQLA "
                     "(the conservative assumption) -- this needs manual review, not automated "
                     "reliance.",
    }


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

    # Illustrative-default assumptions for firm-wide LCR/NSFR inputs, used only when the caller
    # doesn't supply their own real figures. Defined once, here, as named constants -- not
    # improvised per-conversation -- so the numbers are reproducible and the tool's own JSON
    # output states exactly what was assumed, leaving nothing for the caller to mis-describe.
    ILLUSTRATIVE_OUTFLOW_PCT_OF_BOOK = 0.25   # planning-level assumption, not derived from data
    ILLUSTRATIVE_ASF_PCT_OF_BOOK = 1.00       # planning-level assumption, not derived from data

    @mcp.tool()
    def classify_portfolio(positions: Optional[list[dict]] = None,
                            financing_positions: Optional[list[dict]] = None,
                            other_outflows_mm: Optional[float] = None,
                            other_asf_mm: Optional[float] = None) -> str:
        """Classify a REAL client portfolio (not the illustrative demo book) into HQLA levels and
        compute HQLA stock, RWA, and capital required. Use this whenever a user pastes or describes
        their own actual positions, as opposed to run_scenario (which only works with the fixed
        illustrative 9-asset demo book).

        Args:
            positions: OPTIONAL list of OUTRIGHT HELD positions (the client owns these), each a
                dict with:
                - "description": free-text description (e.g. "US Treasury Bill", "Agency MBS pool")
                - "notional_mm": notional value in $ millions
                - "funding_tenor": OPTIONAL override. If the client funds/finances this holding at
                  a specific tenor different from the security's typical default (e.g. "I hold
                  Treasuries but fund them overnight via repo"), set this to one of: "O/N", "1M",
                  "3M", "6M", "1Y", "2Y+". Omit to use the security's normal default tenor.

            financing_positions: OPTIONAL list of REPO / SECURITIES LENDING / MATCHED-BOOK
                structures -- use this for anything involving borrowing or lending a security on
                one tenor versus another, NOT the 'positions' list above. Each dict needs:
                - "description": the underlying security being financed
                - "notional_mm": notional value in $ millions
                - "structure": "matched_book" -- the client does NOT own this security outright;
                  they borrow it on one tenor and re-lend it on another (classic collateral
                  transformation / intermediation). Requires "borrow_tenor" and "lend_tenor"
                  (each one of "O/N","1M","3M","6M","1Y","2Y+").

                MATCHED-BOOK TREATMENT (a real, deliberate simplification -- state this to the
                user, don't present it as a precise regulatory determination): the security is
                NOT counted toward HQLA stock or RWA (the client doesn't own it). RSF on the LEND
                leg is graded by the lend tenor and collateral quality -- short-tenor lending
                backed by Level 1 HQLA gets preferential (lower) RSF, longer tenors converge
                toward full RSF -- NOT the asset's generic held-position RSF weight (a 90-day loan
                is a 90-day loan, not a year of holding that asset type). ASF on the BORROW leg is
                graded by the borrow tenor the same way held positions are (short borrow ~0% ASF,
                longer borrow more). Tenor and direction both matter: borrowing short to fund a
                longer lend commitment shows up as an NSFR drag (maturity transformation risk);
                borrowing long to fund a short lend commitment can actually improve NSFR. NOT
                modeled in this simplification, and you must say so if asked: LCR cash-flow/
                collateral treatment of the financing legs, and counterparty credit RWA on the SFT
                exposure itself (both are real capital considerations this tool does not attempt
                to represent). This also does not replicate every nuance of the real Basel SFT
                rules (counterparty-type distinctions, specific netting rules, jurisdictional
                variations) -- it's a directionally-correct approximation, not a precise one.

            other_outflows_mm / other_asf_mm: OPTIONAL real firm-wide figures (see below).

        LCR and NSFR are ALWAYS returned -- either from the client's own real firm-wide figures
        (if both other_outflows_mm/other_asf_mm are provided) or from stated illustrative defaults
        (if not). Check "lcr_nsfr_basis" in the response and quote "lcr_nsfr_assumptions_used"
        directly -- never restate or re-derive the assumption from memory.

        Each held position gets a confidence tier: "exact_match", "heuristic", or "unclassified"
        -- always surface this plainly, never present heuristic/unclassified as exact.
        """
        positions = positions or []
        financing_positions = financing_positions or []
        if not positions and not financing_positions:
            return json.dumps({"error": "Provide at least one position (held or financing)."})

        classified = []
        model_positions = []
        for i, pos in enumerate(positions):
            desc = pos.get("description", "")
            notional = pos.get("notional_mm")
            if not desc or notional is None:
                return json.dumps({
                    "error": f"positions[{i}] is missing 'description' or 'notional_mm'.",
                })
            c = classify_position(desc)
            attrs = dict(c["attributes"])
            funding_tenor = pos.get("funding_tenor")
            if funding_tenor:
                if funding_tenor not in TENORS:
                    return json.dumps({
                        "error": f"positions[{i}]: unknown funding_tenor '{funding_tenor}'. "
                                 f"Use one of: {list(TENORS.keys())}",
                    })
                attrs["tenor"] = funding_tenor
            classified.append({
                "description": desc,
                "notional_mm": notional,
                "hqla_level": c["level"],
                "confidence": c["confidence"],
                "matched_category": c["matched_category"],
                "rationale": c["rationale"],
                "funding_tenor_used": attrs["tenor"],
            })
            model_positions.append({"notional_mm": notional, "level": c["level"], **attrs})

        financing_classified = []
        for i, fp in enumerate(financing_positions):
            desc = fp.get("description", "")
            notional = fp.get("notional_mm")
            structure = fp.get("structure")
            if not desc or notional is None:
                return json.dumps({"error": f"financing_positions[{i}] is missing 'description' or 'notional_mm'."})
            if structure != "matched_book":
                return json.dumps({
                    "error": f"financing_positions[{i}]: 'structure' must be 'matched_book' "
                             f"(the only supported financing structure right now).",
                })
            borrow_tenor = fp.get("borrow_tenor")
            lend_tenor = fp.get("lend_tenor")
            if borrow_tenor not in TENORS or lend_tenor not in TENORS:
                return json.dumps({
                    "error": f"financing_positions[{i}]: 'borrow_tenor' and 'lend_tenor' must "
                             f"each be one of {list(TENORS.keys())}.",
                })
            c = classify_position(desc)
            lend_rsf_rate = sft_lend_rsf(c["level"], lend_tenor)
            rsf_contribution = notional * lend_rsf_rate
            asf_contribution = notional * TENORS[borrow_tenor]["asf"]
            net_nsfr_drag = rsf_contribution - asf_contribution
            financing_classified.append({
                "description": desc,
                "notional_mm": notional,
                "structure": "matched_book",
                "borrow_tenor": borrow_tenor,
                "lend_tenor": lend_tenor,
                "lend_leg_rsf_rate": lend_rsf_rate,
                "borrow_leg_asf_rate": TENORS[borrow_tenor]["asf"],
                "underlying_classification": {
                    "hqla_level": c["level"], "confidence": c["confidence"],
                    "matched_category": c["matched_category"],
                },
                "hqla_contribution_mm": 0.0,
                "rwa_contribution_mm": 0.0,
                "rsf_from_lend_commitment_mm": round(rsf_contribution, 1),
                "asf_credit_from_borrow_mm": round(asf_contribution, 1),
                "net_nsfr_drag_mm": round(net_nsfr_drag, 1),
                "note": "Not counted toward HQLA or RWA (not owned outright). RSF on the lend leg "
                        "is graded by the LEND tenor and collateral quality (lend_leg_rsf_rate) -- "
                        "short-tenor, Level-1-HQLA-backed lending gets preferential (lower) RSF, "
                        "longer tenors converge toward full RSF -- a simplified approximation of "
                        "Basel's short-residual-maturity SFT treatment, not the asset's generic "
                        "held-position RSF weight. ASF on the borrow leg (borrow_leg_asf_rate) is "
                        "graded by the BORROW tenor the same way held positions are. A positive "
                        "net_nsfr_drag_mm means this trade consumes stable funding capacity; "
                        "borrowing short to fund a longer lend commitment is unfavorable, while "
                        "borrowing long to fund a short lend commitment can actually improve NSFR.",
            })

        total_book = sum(p["notional_mm"] for p in positions)
        client_provided = other_outflows_mm is not None and other_asf_mm is not None
        if client_provided:
            used_outflows = other_outflows_mm
            used_asf = other_asf_mm
            lcr_nsfr_basis = "client_provided"
        else:
            used_outflows = round(total_book * ILLUSTRATIVE_OUTFLOW_PCT_OF_BOOK, 1)
            used_asf = round(total_book * ILLUSTRATIVE_ASF_PCT_OF_BOOK, 1)
            lcr_nsfr_basis = "illustrative_default"

        result = _compute_from_positions(
            model_positions, used_outflows, used_asf, include_lcr_nsfr=True,
        ) if model_positions else {
            "total_book_mm": 0.0, "hqla_stock_mm": 0.0, "rsf_mm": 0.0, "rwa_mm": 0.0,
            "capital_required_mm": 0.0, "lcr": None, "nsfr": None, "asf_mm": used_asf,
        }
        # Fold financing positions' NSFR impact into the aggregate (HQLA/RWA untouched, since
        # matched-book legs contribute zero to both by design).
        financing_rsf = sum(f["rsf_from_lend_commitment_mm"] for f in financing_classified)
        financing_asf = sum(f["asf_credit_from_borrow_mm"] for f in financing_classified)
        if financing_classified:
            new_rsf = result["rsf_mm"] + financing_rsf
            new_asf = result["asf_mm"] + financing_asf
            result["rsf_mm"] = round(new_rsf, 1)
            result["asf_mm"] = round(new_asf, 1)
            result["nsfr"] = round(new_asf / new_rsf, 4) if new_rsf > 0 else None

        if "annual_funding_cost_mm" in result:
            del result["annual_funding_cost_mm"]

        # Renamed from the ambiguous "by_level_mm" -- this is RAW notional per level, before
        # haircuts. A real failure mode: summing this to get "total HQLA stock" gives the wrong
        # answer, because it skips haircuts and the Level 2 caps entirely. The correctly
        # haircut-and-cap-adjusted total is aggregate['hqla_stock_mm'] -- always use that field
        # for "total HQLA stock", never sum raw_notional_by_level_mm yourself.
        raw_notional_by_level = {}
        haircut_adjusted_by_level = {}
        for p, mp in zip(classified, model_positions):
            lvl = p["hqla_level"]
            raw_notional_by_level[lvl] = raw_notional_by_level.get(lvl, 0.0) + p["notional_mm"]
            adj_val = 0.0 if lvl == "Not HQLA" else p["notional_mm"] * (1 - mp["haircut"])
            haircut_adjusted_by_level[lvl] = haircut_adjusted_by_level.get(lvl, 0.0) + adj_val

        confidence_counts = {}
        for p in classified:
            confidence_counts[p["confidence"]] = confidence_counts.get(p["confidence"], 0) + 1

        response = {
            "positions_classified": classified,
            "financing_positions_classified": financing_classified,
            "raw_notional_by_level_mm": {k: round(v, 1) for k, v in raw_notional_by_level.items()},
            "haircut_adjusted_value_by_level_mm": {k: round(v, 1) for k, v in haircut_adjusted_by_level.items()},
            "table_building_instructions": "For a per-level breakdown table, use "
                                            "haircut_adjusted_value_by_level_mm (post-haircut, "
                                            "pre-cap) as the 'value' column, and use "
                                            "aggregate.hqla_stock_mm as the 'Total HQLA Stock' row "
                                            "-- note the total may be SLIGHTLY LESS than the sum of "
                                            "haircut_adjusted_value_by_level_mm if the Basel Level 2 "
                                            "caps bind (Level 2 capped at 40% of HQLA, Level 2B "
                                            "sub-capped at 15%). Never sum raw_notional_by_level_mm "
                                            "or haircut_adjusted_value_by_level_mm and call it "
                                            "'Total HQLA Stock' -- only aggregate.hqla_stock_mm is "
                                            "correct for that label.",
            "confidence_summary": confidence_counts,
            "aggregate": result,
            "lcr_nsfr_basis": lcr_nsfr_basis,
            "lcr_nsfr_assumptions_used": {
                "other_outflows_mm": used_outflows,
                "other_asf_mm": used_asf,
            },
            "methodology_note": "Illustrative classification tool. 'exact_match' positions use "
                                 "precise regulatory factors (haircut, risk weight, RSF) from "
                                 "Aeonic's known asset universe; 'heuristic' and 'unclassified' "
                                 "positions use generic default assumptions and need manual "
                                 "review before being relied upon. This does not replace your "
                                 "own regulatory reporting process.",
            "funding_cost_note": "Annual funding cost is not computed for real portfolios -- it "
                                  "would require YOUR actual borrowing rates and credit spreads "
                                  "per asset class, which aren't derivable from regulatory "
                                  "parameters the way haircuts and risk weights are. Provide your "
                                  "own blended cost of funds if you want that figure estimated.",
        }
        if financing_classified:
            response["financing_note"] = (
                "Matched-book/financing positions are a simplified representation -- they affect "
                "NSFR (via the RSF/ASF mechanics shown per position) but NOT HQLA, RWA, or LCR. "
                "Real securities financing transactions also carry counterparty credit RWA and "
                "specific LCR collateral-flow treatment that this tool does not model. Treat this "
                "as directional, not a substitute for your own SFT capital calculation."
            )
        if lcr_nsfr_basis == "illustrative_default":
            response["lcr_nsfr_note"] = (
                f"LCR/NSFR above use ILLUSTRATIVE placeholder assumptions, not your real figures: "
                f"other net cash outflows assumed at {ILLUSTRATIVE_OUTFLOW_PCT_OF_BOOK*100:.0f}% of "
                f"book (${used_outflows}mm), other available stable funding assumed at "
                f"{ILLUSTRATIVE_ASF_PCT_OF_BOOK*100:.0f}% of book (${used_asf}mm). These are "
                f"round-number planning assumptions, not derived from your data. Provide your "
                f"real other_outflows_mm and other_asf_mm for an accurate LCR/NSFR."
            )
        return json.dumps(response, indent=2)
