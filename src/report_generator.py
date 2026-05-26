import pandas as pd

from .comp_engine import compute_lot_supported_price_per_sqft
from .description_parser import parse_description_signals, apply_description_signals_to_lot
from .scoring_model import score_lot
from .valuation_model import analyze_financials, compute_after_build_sqft


def _enrich_lot_from_description(row: pd.Series) -> pd.Series:
    """Fill missing lot fields from its listing description."""
    desc = str(row.get("listing_description", ""))
    if not desc.strip():
        return row
    signals = parse_description_signals(desc)
    enriched = apply_description_signals_to_lot(row.to_dict(), signals)
    return pd.Series(enriched)


def rank_lots(
    lots_df: pd.DataFrame,
    comps_df: pd.DataFrame,
    assumptions: dict,
) -> pd.DataFrame:
    records = []
    for _, raw_row in lots_df.iterrows():
        row = _enrich_lot_from_description(raw_row)

        comp_info = compute_lot_supported_price_per_sqft(row, comps_df, assumptions)
        _, build_source, build_conf = compute_after_build_sqft(row)
        scores = score_lot(
            row,
            comp_info.get("comp_ppsf"),
            comp_info=comp_info,
            build_sqft_confidence=build_conf,
        )
        financials = analyze_financials(row, comps_df, assumptions, comp_info=comp_info)

        profit_pct = financials["profit_percentage"]
        if profit_pct > 0.25:
            recommendation = "Strong Buy"
        elif profit_pct > 0.15:
            recommendation = "Buy"
        elif profit_pct > 0.05:
            recommendation = "Maybe"
        else:
            recommendation = "Pass"

        record = {
            # Identity
            "Address":                    row.get("address", ""),
            "Neighborhood":               row.get("neighborhood", ""),
            "Fire Status":                row.get("fire_status", ""),
            "Zoning":                     row.get("zoning", ""),
            # Asking
            "Asking Price":               row.get("asking_price", 0),
            "Lot Size (sqft)":            row.get("lot_size_sqft", 0),
            # Sqft tracking (Phase 3)
            "Pre-Fire Sqft":              row.get("prev_home_sqft", 0),
            "Permitted Buildable Sqft":   row.get("buildable_sqft", 0),
            "After-Build Sqft":           financials["after_build_sqft"],
            "Build Sqft Source":          financials["build_sqft_source"],
            "Build Sqft Confidence":      financials["build_sqft_confidence"],
            # Comp results (Phase 4)
            "Selected Comp Count":        comp_info.get("selected_comp_count", 0),
            "Selected Comp Addresses":    comp_info.get("selected_comp_addresses", ""),
            "Selected Comp PPSFs":        comp_info.get("selected_comp_ppsfs", ""),
            "Selected Comp Match Scores": comp_info.get("selected_comp_match_scores", ""),
            "Raw Comp PPSF":              comp_info.get("comp_ppsf") or 0,
            "Market PPSF":                comp_info.get("adjusted_ppsf") or 0,
            "PPSF Breakdown":             comp_info.get("ppsf_breakdown", []),
            "Comp Confidence Score":      comp_info.get("comp_confidence", 0.0),
            "Comp Notes":                 comp_info.get("comp_notes", ""),
            # Valuation (Phase 5 + 6)
            "Exit PPSF":                  financials["projected_ppsf"],
            "Estimated Sale Price":        financials["estimated_future_sale"],
            "Construction Cost":          financials["construction_cost"],
            "Carrying Cost": financials["transaction_cost"],
            "Years Held":    financials.get("years_held", 4),
            "Total Project Cost":         financials["total_project_cost"],
            "Estimated Profit":           financials["estimated_profit"],
            "Profit Percentage":          (
                financials["estimated_profit"] / financials["total_project_cost"] * 100
                if financials["total_project_cost"] else 0.0
            ),
            "Return on Land Cost":        financials["return_on_land"] * 100,
            # Scores (Phase 7)
            "View Quality":               row.get("view_quality", ""),
            "Privacy":                    row.get("privacy", ""),
            "Topography":                 row.get("topography", ""),
            "View Score":                 scores["view_score"],
            "Privacy Score":              scores["privacy_score"],
            "Usability Score":            scores["usability_score"],
            "Location Score":             scores["location_score"],
            "Buildability Score":         scores["buildability_score"],
            "Price Attractiveness":       scores["price_attractiveness"],
            "Risk Score":                 scores["risk_score"],
            "Risk Factors":               scores["risk_factors"],
            "Overall Lot Score":          scores["overall_score"],
            "Recommendation":             recommendation,
            # URLs
            "Zillow URL":                 row.get("zillow_url", ""),
            "Redfin URL":                 row.get("redfin_url", ""),
            "Listing Description":        row.get("listing_description", ""),
            "Assumptions Applied":        " | ".join(row.get("assumptions", [])),
        }
        records.append(record)

    ranked = pd.DataFrame(records)
    if ranked.empty:
        return ranked

    # Rank: profit % first, then absolute profit, then overall score
    ranked.sort_values(
        by=["Profit Percentage", "Estimated Profit", "Overall Lot Score"],
        ascending=[False, False, False],
        inplace=True,
    )
    ranked.reset_index(drop=True, inplace=True)
    ranked.index += 1
    return ranked


def generate_investment_memos(ranked_df: pd.DataFrame) -> list:
    memos = []
    for _, row in ranked_df.iterrows():
        memo_lines = [
            f"{'=' * 60}",
            f"Address:                {row['Address']}",
            f"Recommendation:         {row['Recommendation']}",
            f"{'─' * 60}",
            f"Asking Price:           ${row['Asking Price']:>14,.0f}",
            f"After-Build Sqft:       {row['After-Build Sqft']:>14,.0f}  [{row['Build Sqft Source']} · confidence: {row['Build Sqft Confidence']}]",
            f"{'─' * 60}",
            f"Market PPSF:            ${row['Market PPSF']:>14,.0f}",
            f"Exit PPSF:              ${row['Exit PPSF']:>14,.0f}",
            f"Comp Confidence:        {row['Comp Confidence Score']:>14.0%}",
            f"{'─' * 60}",
            f"Est. Sale Price:        ${row['Estimated Sale Price']:>14,.0f}",
            f"Total Project Cost:     ${row['Total Project Cost']:>14,.0f}",
            f"  Land:                 ${row['Asking Price']:>14,.0f}",
            f"  Construction ($800/sqft): ${row['Construction Cost']:>10,.0f}",
            f"  Carrying Cost:        ${row['Carrying Cost']:>14,.0f}",
            f"Estimated Profit:       ${row['Estimated Profit']:>14,.0f}",
            f"Profit %:               {row['Profit Percentage']:>14.1%}",
            f"Return on Land:         {row['Return on Land Cost']:>14.1%}",
            f"{'─' * 60}",
            f"View Score:             {row['View Score']:>14.0f}  ({row['View Quality']})",
            f"Privacy Score:          {row['Privacy Score']:>14.0f}  ({row['Privacy']})",
            f"Buildability Score:     {row['Buildability Score']:>14.0f}",
            f"Risk Score:             {row['Risk Score']:>14.0f}",
            f"Risk Factors:           {row['Risk Factors']}",
            f"{'─' * 60}",
            f"Supporting Comps ({row['Selected Comp Count']}):",
        ]
        addrs = row['Selected Comp Addresses'].split(" | ") if row['Selected Comp Addresses'] else []
        ppsfs = row['Selected Comp PPSFs'].split(" | ") if row['Selected Comp PPSFs'] else []
        mscores = row['Selected Comp Match Scores'].split(" | ") if row['Selected Comp Match Scores'] else []
        for j, addr in enumerate(addrs):
            p = ppsfs[j] if j < len(ppsfs) else "—"
            s = mscores[j] if j < len(mscores) else "—"
            memo_lines.append(f"  • {addr}  {p}/sqft  (match score {s})")
        if row.get("Assumptions Applied"):
            memo_lines.append(f"{'─' * 60}")
            memo_lines.append(f"Assumptions from description:")
            for a in row["Assumptions Applied"].split(" | "):
                memo_lines.append(f"  • {a}")
        memo_lines.append(f"{'=' * 60}")

        memos.append({"Address": row["Address"], "Memo": "\n".join(memo_lines)})
    return memos
