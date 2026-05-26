"""
Core single-address analysis logic.
Shared by automation/analyze_address.py (CLI) and app.py (Streamlit).
"""
import re
import difflib
from pathlib import Path

import pandas as pd

from .config import DEFAULT_ASSUMPTIONS
from .comp_engine import compute_lot_supported_price_per_sqft, LUXURY_PRICE_MIN
from .description_parser import parse_description_signals, apply_description_signals_to_lot
from .valuation_model import compute_after_build_sqft, analyze_financials
from .scoring_model import score_lot


def _slug(address: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", address.lower()).strip("_")


def _normalize_addr(addr: str) -> str:
    return re.sub(r"[^\w\s]", "", str(addr).lower()).strip()


# ── Address lookup ────────────────────────────────────────────────────────────

def find_lot_in_database(address: str, lots_df: pd.DataFrame) -> tuple:
    """Return (matched_row, match_score) or (None, 0.0).

    Tries exact → substring → fuzzy matching in that order.
    """
    if lots_df is None or lots_df.empty or "address" not in lots_df.columns:
        return None, 0.0

    norm_target = _normalize_addr(address)
    norm_series = lots_df["address"].astype(str).apply(_normalize_addr)

    exact = norm_series == norm_target
    if exact.any():
        return lots_df[exact].iloc[0], 1.0

    for i, norm_val in enumerate(norm_series):
        if norm_target in norm_val or norm_val in norm_target:
            return lots_df.iloc[i], 0.9

    close = difflib.get_close_matches(norm_target, norm_series.tolist(), n=1, cutoff=0.65)
    if close:
        idx   = norm_series[norm_series == close[0]].index[0]
        score = difflib.SequenceMatcher(None, norm_target, close[0]).ratio()
        return lots_df.loc[idx], score

    return None, 0.0


# ── Subject property builder ──────────────────────────────────────────────────

def _identify_missing(lot: dict) -> list[str]:
    missing = []
    if not float(lot.get("asking_price") or 0):
        missing.append("asking_price")
    if not float(lot.get("lot_size_sqft") or 0):
        missing.append("lot_size_sqft")
    if not float(lot.get("buildable_sqft") or 0) and not float(lot.get("prev_home_sqft") or 0):
        missing.append("buildable_sqft (will estimate from lot size)")
    view = str(lot.get("view_quality", "")).strip().lower()
    if not view or view in ("", "nan", "none", "unknown"):
        missing.append("view_quality")
    priv = str(lot.get("privacy", "")).strip().lower()
    if not priv or priv in ("", "nan", "unknown"):
        missing.append("privacy_level")
    topo = str(lot.get("topography", "")).strip().lower()
    if not topo or topo in ("", "nan", "unknown"):
        missing.append("topography")
    return missing


def build_subject_property(
    address: str,
    db_row: pd.Series | None,
    overrides: dict,
    listing_description: str = "",
    listing_url: str = "",
) -> tuple:
    """Return (lot_series, missing_fields_list).

    Priority: database row → user overrides → parsed description.
    """
    base: dict = db_row.to_dict() if db_row is not None else {}
    base["address"] = address

    for k, v in (overrides or {}).items():
        if v is not None and v != "" and v != 0:
            base[k] = v

    desc = str(
        overrides.get("listing_description")
        or listing_description
        or base.get("listing_description", "")
    )
    if desc.strip():
        base["listing_description"] = desc
        signals = parse_description_signals(desc)
        base = apply_description_signals_to_lot(base, signals)
    else:
        base.setdefault("listing_description", "")

    if listing_url:
        if "redfin" in listing_url.lower():
            base.setdefault("redfin_url", listing_url)
        elif "zillow" in listing_url.lower():
            base.setdefault("zillow_url", listing_url)

    for col in ["asking_price", "lot_size_sqft", "buildable_sqft",
                "prev_home_sqft", "usable_lot_area_sqft", "distance_to_ocean_miles"]:
        base.setdefault(col, 0)
    if not base.get("usable_lot_area_sqft"):
        base["usable_lot_area_sqft"] = base.get("lot_size_sqft", 0)

    return pd.Series(base), _identify_missing(base)


# ── Full single-address analysis ──────────────────────────────────────────────

def analyze_single_address(
    address: str,
    comps_df: pd.DataFrame,
    lots_df: pd.DataFrame | None,
    assumptions: dict | None = None,
    overrides: dict | None = None,
    listing_description: str = "",
    listing_url: str = "",
) -> dict:
    """Run the complete analysis for one address. Returns a results dict."""
    if assumptions is None:
        assumptions = DEFAULT_ASSUMPTIONS.copy()

    db_row, match_score = find_lot_in_database(address, lots_df)
    lot_row, missing_fields = build_subject_property(
        address, db_row, overrides or {}, listing_description, listing_url
    )

    comp_info   = compute_lot_supported_price_per_sqft(lot_row, comps_df, assumptions)
    _, _, build_conf = compute_after_build_sqft(lot_row)
    scores      = score_lot(lot_row, comp_info.get("comp_ppsf"),
                            comp_info=comp_info, build_sqft_confidence=build_conf)
    financials  = analyze_financials(lot_row, comps_df, assumptions, comp_info=comp_info)

    pct = financials["profit_percentage"]
    rec = ("Strong Buy" if pct > 0.25 else
           "Buy"        if pct > 0.15 else
           "Maybe"      if pct > 0.05 else "Pass")

    return {
        # Source metadata
        "address":         address,
        "found_in_db":     db_row is not None,
        "db_match_score":  match_score,
        "missing_fields":  missing_fields,
        # Lot fields
        "asking_price":    float(lot_row.get("asking_price")  or 0),
        "lot_size_sqft":   float(lot_row.get("lot_size_sqft") or 0),
        "neighborhood":    str(lot_row.get("neighborhood", "")),
        "view_quality":    str(lot_row.get("view_quality",  "")),
        "privacy":         str(lot_row.get("privacy",       "")),
        "topography":      str(lot_row.get("topography",    "")),
        "redfin_url":      str(lot_row.get("redfin_url",    "")),
        "zillow_url":      str(lot_row.get("zillow_url",    "")),
        # Sqft
        "after_build_sqft":      financials["after_build_sqft"],
        "build_sqft_source":     financials["build_sqft_source"],
        "build_sqft_confidence": financials["build_sqft_confidence"],
        # Comps
        "comp_ppsf":                 comp_info.get("comp_ppsf")       or 0,
        "adjusted_comp_ppsf":        comp_info.get("adjusted_ppsf")   or 0,
        "comp_confidence":           comp_info.get("comp_confidence",  0.0),
        "comp_notes":                comp_info.get("comp_notes",       ""),
        "selected_comp_count":       comp_info.get("selected_comp_count", 0),
        "selected_comp_addresses":   comp_info.get("selected_comp_addresses", ""),
        "selected_comp_ppsfs":       comp_info.get("selected_comp_ppsfs",    ""),
        "selected_comp_match_scores": comp_info.get("selected_comp_match_scores", ""),
        # Financials
        "projected_future_ppsf":   financials["projected_ppsf"],
        "estimated_future_sale":   financials["estimated_future_sale"],
        "construction_cost":       financials["construction_cost"],
        "transaction_cost":        financials["transaction_cost"],
        "total_project_cost":      financials["total_project_cost"],
        "years_held":              financials["years_held"],
        "lot_cost_rate":           financials["lot_cost_rate"],
        "estimated_profit":        financials["estimated_profit"],
        "profit_percentage":       financials["profit_percentage"],
        "profit_margin":           financials["profit_margin"],
        "return_on_land":          financials["return_on_land"],
        # Scores
        "risk_score":    scores["risk_score"],
        "risk_factors":  scores["risk_factors"],
        "overall_score": scores["overall_score"],
        # Recommendation
        "recommendation": rec,
    }


# ── Output writers ────────────────────────────────────────────────────────────

def generate_memo_md(r: dict) -> str:
    """Generate a professional property underwriting memo from a results dict."""
    import datetime
    rec_emoji  = {"Strong Buy": "🟢", "Buy": "🟡", "Maybe": "🟠", "Pass": "🔴"}.get(r["recommendation"], "⚪")
    today      = datetime.date.today().strftime("%B %d, %Y")
    comp_addrs = r["selected_comp_addresses"].split(" | ") if r["selected_comp_addresses"] else []
    comp_ppsfs = r["selected_comp_ppsfs"].split(" | ")     if r["selected_comp_ppsfs"]     else []
    comp_scores= r["selected_comp_match_scores"].split(" | ") if r["selected_comp_match_scores"] else []

    lot_acres = r["lot_size_sqft"] / 43_560 if r["lot_size_sqft"] else 0.0
    profit_margin   = r.get("profit_margin", 0.0)
    return_on_cost  = r.get("profit_percentage", 0.0)
    return_on_land  = r.get("return_on_land", 0.0)

    # ── Recommendation narrative ───────────────────────────────────────────────
    if r["recommendation"] == "Strong Buy":
        rec_narrative = (
            f"This acquisition presents a compelling opportunity with a projected "
            f"**{return_on_cost:.1%} return on cost** and **{profit_margin:.1%} profit margin** "
            f"on an estimated future sale of **${r['estimated_future_sale']:,.0f}**. "
            f"The risk-adjusted returns are well above our acquisition thresholds."
        )
    elif r["recommendation"] == "Buy":
        rec_narrative = (
            f"This acquisition meets our investment criteria with a projected "
            f"**{return_on_cost:.1%} return on cost** and **{profit_margin:.1%} profit margin**. "
            f"Returns are acceptable; confirm key assumptions before proceeding."
        )
    elif r["recommendation"] == "Maybe":
        rec_narrative = (
            f"This acquisition presents marginal returns at a projected "
            f"**{return_on_cost:.1%} return on cost** and **{profit_margin:.1%} profit margin**. "
            f"Recommend additional diligence on buildable sqft and comp selection before committing."
        )
    else:
        rec_narrative = (
            f"Returns do not meet minimum thresholds. Projected return on cost is "
            f"**{return_on_cost:.1%}** with a **{profit_margin:.1%} profit margin**. Pass unless "
            f"the asking price can be negotiated down materially."
        )

    db_note = (
        f"Found in database (match {r['db_match_score']:.0%})"
        if r["found_in_db"] else "Not in database — analysis based on provided inputs"
    )

    lines = [
        "---",
        "",
        f"# PROPERTY ACQUISITION UNDERWRITING",
        f"## {r['address']}",
        "",
        f"| | |",
        f"|---|---|",
        f"| **Date** | {today} |",
        f"| **Analyst** | Palisades Lot Analyzer |",
        f"| **Database** | {db_note} |",
        f"| **Recommendation** | {rec_emoji} **{r['recommendation']}** |",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        rec_narrative,
        "",
        "---",
        "",
        "## 1. Property Overview",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| **Address** | {r['address']} |",
        f"| **Asking Price** | **${r['asking_price']:,.0f}** |",
        f"| **Lot Size** | {r['lot_size_sqft']:,.0f} sqft ({lot_acres:.2f} ac) |",
        f"| **Neighborhood** | {r['neighborhood'] or '—'} |",
        f"| **View Quality** | {r['view_quality'] or '—'} |",
        f"| **Privacy** | {r['privacy'] or '—'} |",
        f"| **Topography** | {r['topography'] or '—'} |",
        "",
    ]

    if r.get("redfin_url") and r["redfin_url"] not in ("", "nan"):
        lines.append(f"**Redfin:** {r['redfin_url']}  ")
    if r.get("zillow_url") and r["zillow_url"] not in ("", "nan"):
        lines.append(f"**Zillow:** {r['zillow_url']}  ")
    lines += ["", "---", ""]

    lines += [
        "## 2. Development Program",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| **After-Build Sqft** | **{r['after_build_sqft']:,.0f} sqft** |",
        f"| **Sqft Source** | {r['build_sqft_source']} |",
        f"| **Sqft Confidence** | {r['build_sqft_confidence']} |",
        f"| **Construction Rate** | $800 / sqft |",
        f"| **Construction Cost** | ${r['construction_cost']:,.0f} |",
        "",
        "> **Note:** Buildable sqft should be independently confirmed with city permitting "
        "records or a licensed architect before finalizing project cost estimates.",
        "",
        "---",
        "",
    ]

    lines += [
        "## 3. Comparable Sales Analysis",
        "",
        f"**Comps selected:** {r['selected_comp_count']}   |   "
        f"**Confidence:** {r['comp_confidence']:.0%}",
        "",
    ]
    if r["comp_notes"]:
        lines.append(f"> {r['comp_notes']}")
        lines.append("")

    if comp_addrs:
        lines += ["| # | Address | PPSF | Match Score |", "|---|---|---|---|"]
        for j, addr in enumerate(comp_addrs):
            p = comp_ppsfs[j]  if j < len(comp_ppsfs)  else "—"
            s = comp_scores[j] if j < len(comp_scores) else "—"
            lines.append(f"| {j+1} | {addr} | {p} | {s} |")
    else:
        lines.append("*No comps selected — insufficient data.*")

    lines += [
        "",
        "| Valuation Step | Value |",
        "|---|---|",
        f"| Median Comp PPSF | ${r['comp_ppsf']:,.0f} |",
        f"| Adjusted Comp PPSF (view/privacy/topo) | ${r['adjusted_comp_ppsf']:,.0f} |",
        f"| × Appreciation (4%/yr to 2029) | applied |",
        f"| × New Construction Premium (+10%) | applied |",
        f"| **Projected PPSF** | **${r['projected_future_ppsf']:,.0f}** |",
        f"| After-Build Sqft | {r['after_build_sqft']:,.0f} sqft |",
        f"| **Estimated Future Sale Price** | **${r['estimated_future_sale']:,.0f}** |",
        "",
        "---",
        "",
    ]

    lines += [
        "## 4. Pro Forma Financial Summary",
        "",
        "### Uses of Funds",
        "",
        "| Cost Item | Amount |",
        "|---|---|",
        f"| Land Acquisition | ${r['asking_price']:,.0f} |",
        f"| Construction ($800 × {r['after_build_sqft']:,.0f} sqft) | ${r['construction_cost']:,.0f} |",
        f"| Carrying / Closing ({r.get('lot_cost_rate', 0.045):.1%}/yr × {r.get('years_held', 4)} yrs) | ${r['transaction_cost']:,.0f} |",
        f"| **Total Project Cost** | **${r['total_project_cost']:,.0f}** |",
        "",
        "### Returns",
        "",
        "| Metric | Value | Definition |",
        "|---|---|---|",
        f"| Estimated Sale Price | ${r['estimated_future_sale']:,.0f} | Projected PPSF × after-build sqft |",
        f"| **Gross Profit** | **${r['estimated_profit']:,.0f}** | Sale price − total cost |",
        f"| **Profit Margin** | **{profit_margin:.1%}** | Profit ÷ sale price |",
        f"| **Return on Cost** | **{return_on_cost:.1%}** | Profit ÷ total project cost |",
        f"| **Return on Land** | **{return_on_land:.1%}** | Profit ÷ land cost only |",
        "",
        "---",
        "",
    ]

    lines += [
        "## 5. Risk Assessment",
        "",
        f"| | |",
        f"|---|---|",
        f"| **Risk Score** | {r['risk_score']:.0f} / 100 |",
        f"| **Risk Factors** | {r['risk_factors']} |",
        "",
    ]

    if r["missing_fields"]:
        lines += [
            "### ⚠ Data Gaps — Fallback Assumptions Used",
            "",
            "The following fields were unavailable and estimated via model defaults. "
            "Confirm these before committing capital.",
            "",
        ]
        for f in r["missing_fields"]:
            lines.append(f"- **`{f}`**")
        lines.append("")

    lines += [
        "---",
        "",
        "## 6. Key Assumptions",
        "",
        "| Assumption | Value |",
        "|---|---|",
        "| Construction cost | $800 / sqft |",
        f"| Carrying / closing rate | {r.get('lot_cost_rate', 0.045):.1%} / year of land cost |",
        f"| Hold period | {r.get('years_held', 4)} years (sale year − analysis year) |",
        "| Annual appreciation | 4.0% per year |",
        "| Target sale year | 2029 |",
        "| New construction premium | +10% |",
        "| Comp minimum sale price | $3,000,000 |",
        "| Comp date range | Jan 2020 – Dec 2024 |",
        "",
        "---",
        "",
        f"*Generated by Palisades Lot Analyzer · {today}*",
    ]

    return "\n".join(lines)


def write_outputs(results: dict, out_dir: Path) -> dict:
    """Write CSV + markdown outputs for a single-address analysis."""
    out_dir.mkdir(parents=True, exist_ok=True)

    analysis_path = out_dir / "analysis.csv"
    flat = {k: v for k, v in results.items() if isinstance(v, (str, int, float, bool))}
    pd.DataFrame([flat]).to_csv(analysis_path, index=False)

    comps_path = out_dir / "selected_comps.csv"
    addrs   = results["selected_comp_addresses"].split(" | ") if results["selected_comp_addresses"] else []
    ppsfs   = results["selected_comp_ppsfs"].split(" | ")     if results["selected_comp_ppsfs"]     else []
    mscores = results["selected_comp_match_scores"].split(" | ") if results["selected_comp_match_scores"] else []
    pd.DataFrame({
        "address":    addrs,
        "ppsf":       ppsfs   + [""] * max(0, len(addrs) - len(ppsfs)),
        "match_score": mscores + [""] * max(0, len(addrs) - len(mscores)),
    }).to_csv(comps_path, index=False)

    memo_path = out_dir / "investment_memo.md"
    memo_path.write_text(generate_memo_md(results), encoding="utf-8")

    review_path = out_dir / "manual_review_needed.csv"
    pd.DataFrame({"missing_field": results["missing_fields"]}).to_csv(review_path, index=False)

    return {
        "analysis": str(analysis_path),
        "comps":    str(comps_path),
        "memo":     str(memo_path),
        "review":   str(review_path),
    }
