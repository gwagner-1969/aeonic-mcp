                                 "own regulatory reporting process.",
            "funding_cost_note": "Annual funding cost is not computed for real portfolios -- it "
                                  "would require YOUR actual borrowing rates and credit spreads "
                                  "per asset class, which aren't derivable from regulatory "
                                  "parameters the way haircuts and risk weights are. Provide your "
                                  "own blended cost of funds if you want that figure estimated.",
        }
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
