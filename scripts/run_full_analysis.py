#!/usr/bin/env python3
"""
run_full_analysis.py
--------------------
One-command automation: load → validate → repair → enrich → rank → export.

Usage:
    python scripts/run_full_analysis.py
    python scripts/run_full_analysis.py --lots data/lots.csv --comps data/comps.csv
    python scripts/run_full_analysis.py --top 20 --output-dir results/
"""
import argparse
import sys
import os
from pathlib import Path
from datetime import datetime

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.config import DEFAULT_ASSUMPTIONS
from src.data_loader import load_lots, load_comps
from src.data_validator import validate_and_repair_comps, validate_and_repair_lots
from src.comp_engine import (
    filter_luxury_comps, comp_dataset_quality,
    compute_lot_supported_price_per_sqft,
)
from src.valuation_model import compute_after_build_sqft
from src.report_generator import rank_lots, generate_investment_memos


# ── CLI args ──────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Palisades Lot Analyzer — full automation")
    p.add_argument("--lots",
                   default="data/cleaned_current_land_listings_database (2).xlsx",
                   help="Path to lots CSV/XLSX")
    p.add_argument("--comps",
                   default="data/cleaned_redfin_comps_database (1).xlsx",
                   help="Path to comps CSV/XLSX")
    p.add_argument("--top",        type=int, default=15,     help="Number of top lots to report")
    p.add_argument("--output-dir", default="output",         help="Directory for CSV/memo output")
    return p.parse_args()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(title: str, width: int = 70):
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


def _section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def _warn(msg: str):
    print(f"  ⚠  WARNING: {msg}")


def _ok(msg: str):
    print(f"  ✓  {msg}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(lots_path: str, comps_path: str, top_n: int, output_dir: str):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    assumptions = DEFAULT_ASSUMPTIONS.copy()

    _banner("PALISADES LOT ANALYZER — FULL AUTOMATION WORKFLOW")
    print(f"  Lots file:   {lots_path}")
    print(f"  Comps file:  {comps_path}")
    print(f"  Top N:       {top_n}")
    print(f"  Output:      {output_dir}/")

    # ── Step 1: Load ──────────────────────────────────────────────────────────
    _section("Step 1: Loading data")
    try:
        raw_lots  = load_lots(lots_path)
        raw_comps = load_comps(comps_path)
        print(f"  Loaded {len(raw_lots)} lot row(s) and {len(raw_comps)} comp row(s).")
    except Exception as e:
        print(f"  ERROR loading data: {e}")
        sys.exit(1)

    # ── Step 2: Validate & repair ─────────────────────────────────────────────
    _section("Step 2: Validating and repairing")

    comps_clean, comp_report = validate_and_repair_comps(raw_comps)
    lots_clean,  lot_report  = validate_and_repair_lots(raw_lots)

    print(f"\n  COMPS:")
    print(f"    Uploaded         : {comp_report['total_uploaded']}")
    if comp_report["was_shift_repaired"]:
        _warn(f"Column shift detected and repaired.")
        print(f"         {comp_report['repair_notes']}")
    print(f"    Rows shift-repaired: {comp_report['rows_shift_repaired']}")
    print(f"    Rows usable      : {comp_report['rows_usable']}")
    print(f"    Rows excluded    : {comp_report['rows_excluded']}")
    for exc in comp_report["excluded_detail"]:
        print(f"      • {exc['address']}  →  {', '.join(exc['reasons'])}")

    print(f"\n  LOTS:")
    print(f"    Uploaded         : {lot_report['total_uploaded']}")
    print(f"    Rows usable      : {lot_report['rows_usable']}")
    print(f"    Rows excluded    : {lot_report['rows_excluded']}")
    for exc in lot_report["excluded_detail"]:
        print(f"      • {exc['address']}  →  {', '.join(exc['reasons'])}")

    if lots_clean.empty:
        print("\n  ERROR: No usable lot rows after validation. Cannot continue.")
        sys.exit(1)

    # ── Step 3: Comp dataset quality ──────────────────────────────────────────
    _section("Step 3: Comp dataset quality")
    cq = comp_dataset_quality(comps_clean)
    print(f"  Total uploaded            : {cq['total_uploaded']}")
    print(f"  Valid pre-Jan-2025 comps  : {cq['valid_luxury']}")
    print(f"  Excluded (post-2025)      : {cq['excluded_post_2025']}")
    print(f"  Excluded (below $5M)      : {cq['excluded_below_price']}")
    print(f"  Excluded (missing data)   : {cq['excluded_missing_data']}")
    if cq["is_limited"]:
        _warn(
            f"Limited comp dataset — only {cq['valid_luxury']} valid comp(s) "
            f"(minimum recommended: {cq['warning_threshold']}). Results may be unreliable."
        )
    else:
        _ok(f"{cq['valid_luxury']} valid comps — sufficient for analysis.")
    if cq["all_lots_share_pool"]:
        _warn(
            f"All lots share the same {cq['valid_luxury']}-comp pool "
            f"(need > {8} valid comps for per-lot subsetting). "
            "Adjusted Comp PPSF still differs per lot via lot-specific premiums."
        )

    # ── Step 4–8: Description parsing, comp matching, valuation, ranking ───────
    _section("Step 4–8: Enriching descriptions, matching comps, ranking lots")
    ranked = rank_lots(lots_clean, comps_clean, assumptions)
    print(f"  Ranked {len(ranked)} lot(s).")

    # Summary warnings
    no_comps = ranked[ranked["Selected Comp Count"] == 0]
    if not no_comps.empty:
        _warn(f"{len(no_comps)} lot(s) had zero valid comp matches.")

    low_conf = ranked[ranked["Comp Confidence Score"] < 0.4]
    if not low_conf.empty:
        _warn(f"{len(low_conf)} lot(s) have low comp confidence (< 40%).")

    low_build = ranked[ranked["Build Sqft Confidence"] == "low"]
    if not low_build.empty:
        _warn(f"{len(low_build)} lot(s) use estimated (low-confidence) after-build sqft.")

    # ── Step 9: Top N results ─────────────────────────────────────────────────
    _banner(f"TOP {min(top_n, len(ranked))} LOTS BY PROFIT PERCENTAGE")
    top = ranked.head(top_n)

    col_w = 52
    header = (
        f"{'#':>3}  {'Address':<{col_w}}  "
        f"{'Ask':>12}  {'After-Sqft':>10}  "
        f"{'Proj PPSF':>9}  {'Sale':>14}  "
        f"{'Cost':>14}  {'Profit':>12}  "
        f"{'Profit%':>7}  {'Rec'}"
    )
    print(header)
    print("─" * len(header))
    for rank, row in top.iterrows():
        print(
            f"{rank:>3}. {str(row['Address']):<{col_w}}  "
            f"${row['Asking Price']:>11,.0f}  "
            f"{row['After-Build Sqft']:>10,.0f}  "
            f"${row['Projected Future PPSF']:>8,.0f}  "
            f"${row['Estimated Future Sale Price']:>13,.0f}  "
            f"${row['Total Project Cost']:>13,.0f}  "
            f"${row['Estimated Profit']:>11,.0f}  "
            f"{row['Profit Percentage']:>7.1f}%  "
            f"{row['Recommendation']}"
        )

    # ── Step 10: Top 3 deep-dive ──────────────────────────────────────────────
    _banner("TOP 3 LOT DEEP-DIVE")
    for rank, row in top.head(3).iterrows():
        print(f"\n{'─' * 60}")
        print(f"  #{rank} — {row['Address']}")
        print(f"  {'─' * 56}")
        print(f"  After-Build Sqft     : {row['After-Build Sqft']:>10,.0f}  "
              f"[{row['Build Sqft Source']} · {row['Build Sqft Confidence']} confidence]")
        print(f"  Adjusted Comp PPSF   : ${row['Adjusted Comp PPSF']:>10,.0f}")
        print(f"  Projected PPSF       : ${row['Projected Future PPSF']:>10,.0f}")
        print(f"  Est. Future Sale     : ${row['Estimated Future Sale Price']:>10,.0f}")
        print(f"  Total Project Cost   : ${row['Total Project Cost']:>10,.0f}")
        print(f"    Land               : ${row['Asking Price']:>10,.0f}")
        print(f"    Construction       : ${row['Construction Cost']:>10,.0f}")
        print(f"    Carrying Cost      : ${row['Carrying Cost']:>10,.0f}  ({row.get('Years Held', 4)} yrs)")
        print(f"  Estimated Profit     : ${row['Estimated Profit']:>10,.0f}")
        print(f"  Profit %             : {row['Profit Percentage']:>10.1f}%  (future sale ÷ total cost)")
        print(f"  Risk Score           : {row['Risk Score']:>10.0f}  ({row['Risk Factors']})")
        print(f"  Comp Confidence      : {row['Comp Confidence Score']:>10.0%}")
        addrs  = row["Selected Comp Addresses"].split(" | ") if row["Selected Comp Addresses"] else []
        ppsfs  = row["Selected Comp PPSFs"].split(" | ")     if row["Selected Comp PPSFs"]     else []
        scores = row["Selected Comp Match Scores"].split(" | ") if row["Selected Comp Match Scores"] else []
        if addrs:
            print(f"  Selected Comps:")
            for j, addr in enumerate(addrs):
                p = ppsfs[j]  if j < len(ppsfs)  else "—"
                s = scores[j] if j < len(scores) else "—"
                print(f"    • {addr}  {p}/sqft  (match {s})")
        if row.get("Assumptions Applied"):
            print(f"  Assumptions from description:")
            for a in str(row["Assumptions Applied"]).split(" | "):
                if a.strip():
                    print(f"    • {a}")

    # ── Step 11: Export ───────────────────────────────────────────────────────
    _section("Step 11: Exporting results")

    csv_path = out / f"top_{top_n}_lots_{timestamp}.csv"
    ranked.head(top_n).to_csv(csv_path, index=True)
    print(f"  CSV exported  → {csv_path}")

    memos = generate_investment_memos(top)
    memo_path = out / f"investment_memos_{timestamp}.txt"
    with open(memo_path, "w") as f:
        f.write(f"PALISADES LOT ANALYZER — INVESTMENT MEMOS\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        for memo in memos:
            f.write(memo["Memo"] + "\n\n")
    print(f"  Memos exported → {memo_path}")

    _banner("ANALYSIS COMPLETE")
    print(f"  Lots analyzed : {len(ranked)}")
    print(f"  Valid comps   : {cq['valid_luxury']}")
    print(f"  Rows repaired : {comp_report['rows_shift_repaired'] + lot_report['rows_shift_repaired']}")
    print(f"  Rows excluded : {comp_report['rows_excluded'] + lot_report['rows_excluded']}")
    print(f"  Output dir    : {out.resolve()}/")
    if cq["is_limited"]:
        _warn("Comp dataset is limited. Upload more pre-2025 comps for reliable results.")
    print()


if __name__ == "__main__":
    args = parse_args()
    run(args.lots, args.comps, args.top, args.output_dir)
