#!/usr/bin/env python3
"""
analyze_address.py
------------------
Single-address acquisition analysis.

Usage:
    python automation/analyze_address.py "1116 Maroney Ln, Pacific Palisades, CA"
    python automation/analyze_address.py "ADDRESS" --url "https://..." --description "TEXT"
    python automation/analyze_address.py "ADDRESS" --no-interactive
    python automation/analyze_address.py "ADDRESS" --min-comp-price 5000000
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DEFAULT_ASSUMPTIONS
from src.data_loader import load_lots, load_comps
from src.address_analyzer import analyze_single_address, write_outputs, _slug

LOTS_DEFAULT  = "data/cleaned_current_land_listings_database (2).xlsx"
COMPS_DEFAULT = "data/cleaned_redfin_comps_database (1).xlsx"

VIEW_OPTS    = ["ocean", "canyon", "city", "mountain", "partial", "none", "unknown"]
TOPO_OPTS    = ["flat", "gentle", "moderate", "steep", "unknown"]
PRIVACY_OPTS = ["very_high", "high", "medium", "low", "unknown"]


def _prompt(question: str, options: list) -> str:
    while True:
        print(f"\n  {question}")
        print(f"  Options: {', '.join(options)}")
        ans = input("  > ").strip().lower()
        if ans in [o.lower() for o in options] or ans == "":
            return ans
        print(f"  Invalid — choose from: {', '.join(options)}")


def _prompt_number(question: str) -> float | None:
    print(f"\n  {question}")
    raw = input("  > ").strip().replace(",", "").replace("$", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print("  Could not parse — skipping.")
        return None


def _interactive_fill(missing_fields: list) -> dict:
    """Ask the user for any missing high-impact fields. Returns override dict."""
    overrides = {}
    for field in missing_fields:
        if "view_quality" in field:
            ans = _prompt("I could not determine the view quality.", VIEW_OPTS)
            if ans and ans != "unknown":
                overrides["view_quality"] = ans
        elif "topography" in field:
            ans = _prompt("I could not determine topography.", TOPO_OPTS)
            if ans and ans != "unknown":
                overrides["topography"] = ans
        elif "privacy_level" in field:
            ans = _prompt("I could not determine privacy level.", PRIVACY_OPTS)
            if ans and ans != "unknown":
                overrides["privacy"] = ans
        elif "asking_price" in field:
            val = _prompt_number("Asking price not found. Enter asking price (or Enter to skip):")
            if val:
                overrides["asking_price"] = val
        elif "lot_size_sqft" in field:
            val = _prompt_number("Lot size not found. Enter lot size in sqft (or Enter to skip):")
            if val:
                overrides["lot_size_sqft"] = val
    return overrides


def run(
    address: str,
    url: str,
    description: str,
    min_comp_price: float,
    interactive: bool,
    lots_path: str,
    comps_path: str,
):
    div = "=" * 70
    print(f"\n{div}")
    print("  PALISADES LOT ANALYZER — SINGLE ADDRESS ANALYSIS")
    print(f"{div}")
    print(f"  Address  : {address}")
    print(f"  Comp min : ${min_comp_price:,.0f}")

    # ── Load data ──────────────────────────────────────────────────────────────
    print("\n  Loading data…")
    try:
        lots_df  = load_lots(lots_path)
        comps_df = load_comps(comps_path)
        print(f"  Loaded {len(lots_df)} lots  |  {len(comps_df)} comps.")
    except FileNotFoundError as e:
        print(f"\n  ERROR: {e}")
        print(f"  Check that '{lots_path}' and '{comps_path}' exist.")
        sys.exit(1)

    assumptions = DEFAULT_ASSUMPTIONS.copy()
    assumptions["min_comp_sale_price"] = min_comp_price

    overrides = {}

    # ── First-pass analysis ────────────────────────────────────────────────────
    results = analyze_single_address(
        address=address, comps_df=comps_df, lots_df=lots_df,
        assumptions=assumptions, overrides=overrides,
        listing_description=description, listing_url=url,
    )

    # ── Interactive follow-up ──────────────────────────────────────────────────
    if interactive and results["missing_fields"]:
        print(f"\n  ⚠  Missing fields: {', '.join(results['missing_fields'])}")
        extra = _interactive_fill(results["missing_fields"])
        if extra:
            overrides.update(extra)
            results = analyze_single_address(
                address=address, comps_df=comps_df, lots_df=lots_df,
                assumptions=assumptions, overrides=overrides,
                listing_description=description, listing_url=url,
            )

    # ── Print results ──────────────────────────────────────────────────────────
    r   = results
    sep = "─" * 60
    db_status = "Found in lots database" if r["found_in_db"] else "NOT found — blank record created"
    print(f"\n  {sep}")
    print(f"  Database : {db_status}")
    if r["found_in_db"]:
        print(f"  Match    : {r['db_match_score']:.0%}")

    print(f"\n  PROPERTY")
    print(f"  {'─'*40}")
    print(f"  Address      : {r['address']}")
    print(f"  Asking Price : ${r['asking_price']:>12,.0f}")
    print(f"  Lot Size     : {r['lot_size_sqft']:>12,.0f} sqft")
    print(f"  Neighborhood : {r['neighborhood'] or '—'}")
    print(f"  View Quality : {r['view_quality'] or '—'}")
    print(f"  Privacy      : {r['privacy'] or '—'}")
    print(f"  Topography   : {r['topography'] or '—'}")

    print(f"\n  AFTER-BUILD SQFT")
    print(f"  {'─'*40}")
    print(f"  Sqft         : {r['after_build_sqft']:>10,.0f}")
    print(f"  Source       : {r['build_sqft_source']}")
    print(f"  Confidence   : {r['build_sqft_confidence']}")

    addrs   = r["selected_comp_addresses"].split(" | ") if r["selected_comp_addresses"] else []
    ppsfs   = r["selected_comp_ppsfs"].split(" | ")     if r["selected_comp_ppsfs"]     else []
    mscores = r["selected_comp_match_scores"].split(" | ") if r["selected_comp_match_scores"] else []
    print(f"\n  COMPS  ({r['selected_comp_count']} selected — {r['comp_confidence']:.0%} confidence)")
    print(f"  {'─'*40}")
    for j, addr in enumerate(addrs):
        p = ppsfs[j]   if j < len(ppsfs)   else "—"
        s = mscores[j] if j < len(mscores) else "—"
        print(f"  • {addr:<52} {p}/sqft  (match {s})")

    print(f"\n  VALUATION")
    print(f"  {'─'*40}")
    print(f"  Adj Comp PPSF     : ${r['adjusted_comp_ppsf']:>10,.0f}")
    print(f"  Projected PPSF    : ${r['projected_future_ppsf']:>10,.0f}")
    print(f"  Est. Future Sale  : ${r['estimated_future_sale']:>10,.0f}")

    print(f"\n  COST & PROFIT")
    print(f"  {'─'*40}")
    years = r.get('years_held', 4)
    rate  = r.get('lot_cost_rate', 0.045)
    print(f"  Land              : ${r['asking_price']:>10,.0f}")
    print(f"  Construction      : ${r['construction_cost']:>10,.0f}")
    print(f"  Carrying ({rate:.1%}/yr×{years}yr): ${r['transaction_cost']:>10,.0f}")
    print(f"  Total Cost        : ${r['total_project_cost']:>10,.0f}")
    print(f"  Est. Profit       : ${r['estimated_profit']:>10,.0f}")
    print(f"  Profit Margin     : {r.get('profit_margin', 0):>10.1%}  (profit ÷ sale price)")
    print(f"  Return on Cost    : {r['profit_percentage']:>10.1%}  (profit ÷ total cost)")
    print(f"  Return on Land    : {r['return_on_land']:>10.1%}  (profit ÷ land cost)")

    rec_icon = {"Strong Buy": "🟢", "Buy": "🟡", "Maybe": "🟠", "Pass": "🔴"}.get(r["recommendation"], "⚪")
    print(f"\n  {'─'*40}")
    print(f"  RECOMMENDATION: {rec_icon} {r['recommendation']}")

    if r["missing_fields"]:
        print(f"\n  ⚠  FALLBACK FIELDS (assumptions used):")
        for f in r["missing_fields"]:
            print(f"     • {f}")

    # ── Write outputs ──────────────────────────────────────────────────────────
    out_dir = Path("output/address_analysis") / _slug(address)
    paths   = write_outputs(r, out_dir)

    print(f"\n  {sep}")
    print(f"  Output → {out_dir}/")
    for label, p in paths.items():
        print(f"    {label:10} → {p}")
    print()


def parse_args():
    p = argparse.ArgumentParser(description="Palisades Lot Analyzer — single address")
    p.add_argument("address", help='Property address, e.g. "1116 Maroney Ln, Pacific Palisades, CA"')
    p.add_argument("--url",            default="", help="Listing URL (optional)")
    p.add_argument("--description",    default="", help="Pasted listing description (optional)")
    p.add_argument("--min-comp-price", type=float, default=3_000_000,
                   help="Minimum comp sale price (default: 3000000)")
    p.add_argument("--lots",  default=LOTS_DEFAULT,  help="Path to lots file")
    p.add_argument("--comps", default=COMPS_DEFAULT, help="Path to comps file")
    p.add_argument("--no-interactive", action="store_true",
                   help="Skip interactive prompts for missing fields")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        address=args.address,
        url=args.url,
        description=args.description,
        min_comp_price=args.min_comp_price,
        interactive=not args.no_interactive,
        lots_path=args.lots,
        comps_path=args.comps,
    )
