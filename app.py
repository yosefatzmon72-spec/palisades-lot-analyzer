import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path

from src.config import DEFAULT_ASSUMPTIONS
from src.data_loader import load_lots, load_comps
from src.data_validator import validate_and_repair_comps, validate_and_repair_lots
from src.comp_engine import comp_dataset_quality, LUXURY_PRICE_MIN
from src.report_generator import rank_lots
from src.address_analyzer import analyze_single_address, generate_memo_md, write_outputs

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Palisades Lot Analyzer",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

CHART_CFG = {"displayModeBar": False}
CHART_TEMPLATE = "plotly_white"
REC_COLORS = {
    "Strong Buy": "#16a34a",
    "Buy":        "#2563eb",
    "Maybe":      "#d97706",
    "Pass":       "#dc2626",
}

# ── Global style ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
    .stTabs [data-baseweb="tab"] {
        height: 40px; padding: 0 18px;
        background: #f1f5f9; border-radius: 6px 6px 0 0;
        font-weight: 500; font-size: 13px; color: #475569;
    }
    .stTabs [aria-selected="true"] {
        background: #1e40af !important; color: white !important;
    }
    h1 { font-size: 1.6rem !important; font-weight: 700; margin-bottom: 0; }
    h4 { font-size: 0.95rem !important; font-weight: 600; color: #1e293b; margin-top: 0.5rem; }
    [data-testid="stMetricValue"] { font-size: 1.25rem !important; font-weight: 700; }
    [data-testid="stMetricLabel"] { font-size: 0.75rem !important; color: #64748b; }
    .section-rule { border: none; border-top: 1px solid #e2e8f0; margin: 1rem 0; }
    div[data-testid="stDataFrame"] { border: 1px solid #e2e8f0; border-radius: 6px; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Palisades Lot Analyzer")
    st.caption("Ultra-luxury development · Pacific Palisades")
    st.divider()

    st.markdown("**Data Inputs**")
    lot_file  = st.file_uploader("Lots file (CSV / XLSX)",  type=["csv","xlsx","xls"], label_visibility="collapsed")
    st.caption("Lots file — leave blank to use default")
    comp_file = st.file_uploader("Comps file (CSV / XLSX)", type=["csv","xlsx","xls"], label_visibility="collapsed")
    st.caption("Comps file — leave blank to use default")

    st.divider()
    st.markdown("**Financial Assumptions**")

    a = DEFAULT_ASSUMPTIONS.copy()
    a["construction_cost_per_sqft"] = st.number_input(
        "Construction cost / sqft ($)", value=800, min_value=200, max_value=3000, step=50)
    a["additional_lot_cost_rate"] = st.number_input(
        "Annual carrying cost rate", value=0.045, min_value=0.0, max_value=0.20,
        step=0.005, format="%.3f")
    a["analysis_year"] = st.number_input(
        "Analysis year", value=2026, min_value=2024, max_value=2030, step=1)
    a["target_sale_year"] = st.number_input(
        "Target sale year", value=2029, min_value=2025, max_value=2040, step=1)
    _yrs = max(1, int(a["target_sale_year"]) - int(a["analysis_year"]))
    st.caption(
        f"Hold period: **{_yrs} yrs** · "
        f"Carrying = {a['additional_lot_cost_rate']:.1%}/yr × {_yrs} = "
        f"**{a['additional_lot_cost_rate']*_yrs:.1%}** of land"
    )

    a["annual_appreciation"] = st.number_input(
        "Annual appreciation", value=0.03, min_value=0.0, max_value=0.20, step=0.005, format="%.3f")
    a["new_construction_premium"] = st.number_input(
        "New construction premium", value=0.12, min_value=0.0, max_value=0.40, step=0.01, format="%.2f")

    with st.expander("View & Privacy Premiums"):
        a["ocean_view_premium"]        = st.slider("Ocean view",        0.0, 0.30, 0.15, 0.01)
        a["canyon_view_premium"]       = st.slider("Canyon view",       0.0, 0.20, 0.10, 0.01)
        a["mountain_view_premium"]     = st.slider("Mountain view",     0.0, 0.15, 0.07, 0.01)
        a["city_view_premium"]         = st.slider("City view",         0.0, 0.12, 0.05, 0.01)
        a["partial_view_premium"]      = st.slider("Partial view",      0.0, 0.10, 0.05, 0.01)
        a["very_high_privacy_premium"] = st.slider("Very high privacy", 0.0, 0.20, 0.10, 0.01)
        a["high_privacy_premium"]      = st.slider("High privacy",      0.0, 0.15, 0.06, 0.01)
        a["medium_privacy_premium"]    = st.slider("Medium privacy",    0.0, 0.10, 0.04, 0.01)

    with st.expander("Topography"):
        a["flat_usability_premium"]   = st.slider("Flat pad",          0.0, 0.15, 0.05, 0.01)
        a["gentle_usability_premium"] = st.slider("Gentle slope",      0.0, 0.10, 0.02, 0.01)
        a["moderate_slope_discount"]  = st.slider("Moderate discount", 0.0, 0.15, 0.05, 0.01)
        a["steep_slope_discount"]     = st.slider("Steep discount",    0.0, 0.25, 0.12, 0.01)

    with st.expander("Neighborhood Premiums"):
        a["riviera_premium"]         = st.slider("Riviera",          0.0, 0.25, 0.13, 0.01)
        a["castellammare_premium"]   = st.slider("Castellammare",    0.0, 0.25, 0.12, 0.01)
        a["highlands_premium"]       = st.slider("Highlands",        0.0, 0.20, 0.11, 0.01)
        a["upper_palisades_premium"] = st.slider("Upper Palisades",  0.0, 0.20, 0.10, 0.01)
        a["village_premium"]         = st.slider("Village",          0.0, 0.20, 0.10, 0.01)
        a["marquez_knolls_premium"]  = st.slider("Marquez Knolls",   0.0, 0.15, 0.08, 0.01)

    st.divider()
    comp_scenario = st.radio(
        "Min comp sale price",
        ["$3M — Expanded", "$5M — Strict luxury"],
        index=0,
    )
    a["min_comp_sale_price"] = 3_000_000 if comp_scenario.startswith("$3M") else 5_000_000


# ─────────────────────────────────────────────────────────────────────────────
# Data load (cached)
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data
def load_and_validate(lot_file, comp_file):
    app_dir  = Path(__file__).resolve().parent
    data_dir = app_dir / "data"

    def _lots_path():
        for p in sorted(data_dir.glob("cleaned_current_land_listings_database*.xlsx")):
            return str(p)
        for ext in ("xlsx", "xls", "csv"):
            p = data_dir / f"lots.{ext}"
            if p.exists():
                return str(p)
        return str(data_dir / "lots.csv")

    def _comps_path():
        for p in sorted(data_dir.glob("cleaned_redfin_comps_database*.xlsx")):
            return str(p)
        for ext in ("xlsx", "xls", "csv"):
            p = data_dir / f"comps.{ext}"
            if p.exists():
                return str(p)
        return str(data_dir / "comps.csv")

    raw_lots  = load_lots (lot_file  if lot_file  else _lots_path())
    raw_comps = load_comps(comp_file if comp_file else _comps_path())
    lots_clean,  lot_report  = validate_and_repair_lots(raw_lots)
    comps_clean, comp_report = validate_and_repair_comps(raw_comps)
    return lots_clean, comps_clean, lot_report, comp_report


lots_df, comps_df, lot_report, comp_report = load_and_validate(lot_file, comp_file)
cq = comp_dataset_quality(comps_df, min_price=a.get("min_comp_sale_price", LUXURY_PRICE_MIN))

if lots_df.empty:
    st.error("No usable lot data.")
    st.stop()

with st.spinner("Running analysis…"):
    ranked_df = rank_lots(lots_df, comps_df, a)

if ranked_df.empty:
    st.error("No lots could be analyzed.")
    st.stop()

_all = ranked_df.copy()
_top = ranked_df.head(15).copy()


# ─────────────────────────────────────────────────────────────────────────────
# Page header
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("# Palisades Lot Analyzer")
st.caption(
    f"Pacific Palisades  ·  {len(_all)} lots analyzed  ·  "
    f"{(_all['Recommendation']=='Strong Buy').sum()} Strong Buy  ·  "
    f"Median asking ${_all['Asking Price'].median():,.0f}"
)

# ─────────────────────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────────────────────
(tab_ranked, tab_all, tab_comps,
 tab_how, tab_single) = st.tabs([
    "Ranked Lots",
    "All Listings",
    "Comp Analysis",
    "How It Works",
    "Single Address",
])


# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — RANKED LOTS (TOP 15)
# ═════════════════════════════════════════════════════════════════════════════
with tab_ranked:
    st.markdown("#### Top 15 Lots by Return on Cost")
    st.caption(
        "Sorted by profit % (estimated sale ÷ total project cost). "
        "All sqft from ParcelQuest x 1.10 unless permitted plans on file."
    )

    disp = _top.copy()
    disp["Rank"]   = range(1, len(disp) + 1)
    disp["Street"] = disp["Address"].str.split(",").str[0]
    disp["View"]   = disp["View Quality"].str.capitalize()
    disp["Topo"]   = disp["Topography"].str.capitalize()
    disp["Rec"]    = disp["Recommendation"]

    CLEAN_COLS = [
        "Rank", "Street",
        "Asking Price", "After-Build Sqft",
        "View", "Topo",
        "Market PPSF", "Exit PPSF",
        "Estimated Sale Price", "Total Project Cost",
        "Estimated Profit", "Profit Percentage",
        "Rec",
    ]
    disp_show = disp[[c for c in CLEAN_COLS if c in disp.columns]]

    st.dataframe(
        disp_show,
        use_container_width=True,
        hide_index=True,
        height=560,
        column_config={
            "Rank":                         st.column_config.NumberColumn("Rank",      width=60),
            "Street":                       st.column_config.TextColumn("Address",     width=200),
            "Asking Price":                 st.column_config.NumberColumn("Asking",    format="$%,.0f", width=110),
            "After-Build Sqft":             st.column_config.NumberColumn("Bld Sqft",  format="%,.0f",  width=90),
            "View":                         st.column_config.TextColumn("View",        width=70),
            "Topo":                         st.column_config.TextColumn("Topo",        width=80),
            "Market PPSF":           st.column_config.NumberColumn("Market PPSF", format="$%,.0f", width=100),
            "Exit PPSF":             st.column_config.NumberColumn("Exit PPSF",   format="$%,.0f", width=95),
            "Estimated Sale Price":  st.column_config.NumberColumn("Est Sale",  format="$%,.0f", width=110),
            "Total Project Cost":           st.column_config.NumberColumn("Total Cost",format="$%,.0f", width=110),
            "Estimated Profit":             st.column_config.NumberColumn("Profit",    format="$%,.0f", width=110),
            "Profit Percentage":            st.column_config.NumberColumn("ROC %",     format="%.1f%%", width=80),
            "Rec":                          st.column_config.TextColumn("Signal",      width=100),
        },
    )

    dl1, dl2 = st.columns(2)
    dl1.download_button("Download Top 15 (CSV)", data=_top.to_csv(index=True),
                        file_name="top_15_lots.csv", mime="text/csv")
    dl2.download_button("Download All Lots (CSV)", data=_all.to_csv(index=True),
                        file_name="all_lots_ranked.csv", mime="text/csv")

    st.divider()
    with st.expander("Add a lot manually"):
        with st.form("manual_lot"):
            c1, c2, c3 = st.columns(3)
            with c1:
                m_addr  = st.text_input("Address")
                m_ask   = st.number_input("Asking price ($)", value=0.0, step=50_000.0)
                m_lot   = st.number_input("Lot size (sqft)",  value=0.0, step=500.0)
            with c2:
                m_prev  = st.number_input("Pre-fire sqft",  value=0.0, step=100.0)
                m_build = st.number_input("Permitted sqft (0=unknown)", value=0.0, step=100.0)
                m_view  = st.selectbox("View", ["ocean","canyon","city","mountain","partial","none"])
            with c3:
                m_priv  = st.selectbox("Privacy",      ["very_high","high","medium","low"])
                m_topo  = st.selectbox("Topography",   ["flat","gentle","moderate","steep"])
                m_nbhd  = st.selectbox("Neighborhood", ["Upper Palisades","Lower Palisades","Village","Custom"])
            m_desc = st.text_area("Listing description (optional)", height=60)
            if st.form_submit_button("Add lot", type="primary") and m_addr:
                st.success(f"Added '{m_addr}'. Restart the app to include it in analysis.")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — ALL LISTINGS
# ═════════════════════════════════════════════════════════════════════════════
with tab_all:
    st.markdown("#### All Vacant Properties for Sale")
    st.caption(f"{len(_all)} lots · sorted by rank (return on cost). Use column headers to re-sort.")

    all_disp = _all.copy()
    all_disp.insert(0, "Rank", range(1, len(all_disp) + 1))
    all_disp["Street"]      = all_disp["Address"].str.split(",").str[0]
    all_disp["View"]        = all_disp["View Quality"].str.capitalize()
    all_disp["Topo"]        = all_disp["Topography"].str.capitalize()
    all_disp["Neighborhood"]= all_disp.get("Neighborhood", "—")

    ALL_COLS = [
        "Rank", "Street",
        "Asking Price", "Lot Size Sqft", "After-Build Sqft",
        "View", "Topo", "Neighborhood",
        "Exit PPSF",
        "Estimated Sale Price", "Total Project Cost",
        "Estimated Profit", "Profit Percentage",
        "Recommendation",
    ]
    available_all = [c for c in ALL_COLS if c in all_disp.columns]
    all_show = all_disp[available_all]

    st.dataframe(
        all_show,
        use_container_width=True,
        hide_index=True,
        height=640,
        column_config={
            "Rank":                        st.column_config.NumberColumn("Rank",        width=60),
            "Street":                      st.column_config.TextColumn("Address",       width=200),
            "Asking Price":                st.column_config.NumberColumn("Asking",      format="$%,.0f", width=110),
            "Lot Size Sqft":               st.column_config.NumberColumn("Lot Sqft",    format="%,.0f",  width=90),
            "After-Build Sqft":            st.column_config.NumberColumn("Bld Sqft",    format="%,.0f",  width=90),
            "View":                        st.column_config.TextColumn("View",          width=70),
            "Topo":                        st.column_config.TextColumn("Topo",          width=80),
            "Neighborhood":                st.column_config.TextColumn("Neighborhood",  width=140),
            "Exit PPSF":            st.column_config.NumberColumn("Exit PPSF",   format="$%,.0f", width=95),
            "Estimated Sale Price": st.column_config.NumberColumn("Est Sale",    format="$%,.0f", width=110),
            "Total Project Cost":          st.column_config.NumberColumn("Total Cost",  format="$%,.0f", width=110),
            "Estimated Profit":            st.column_config.NumberColumn("Profit",      format="$%,.0f", width=110),
            "Profit Percentage":           st.column_config.NumberColumn("ROC %",       format="%.1f%%", width=75),
            "Recommendation":              st.column_config.TextColumn("Signal",        width=100),
        },
    )

    st.download_button("Download All Listings (CSV)", data=_all.to_csv(index=True),
                       file_name="all_listings.csv", mime="text/csv")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 — COMP ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════
with tab_comps:

    # ── Comp data summary ─────────────────────────────────────────────────────
    st.markdown("#### Comparable Sales — Source Data")
    st.caption("These are the actual transactions used to benchmark price per sqft across all lots.")

    comp_disp = comps_df.copy()
    comp_show_cols = []
    rename_map = {}

    for raw, pretty in [
        ("address",     "Address"),
        ("sale_date",   "Sale Date"),
        ("sale_price",  "Sale Price"),
        ("finished_sqft","Finished Sqft"),
        ("lot_size_sqft","Lot Sqft"),
        ("bedrooms",    "Beds"),
        ("price_per_sqft","PPSF"),
        ("view_quality","View"),
        ("neighborhood","Neighborhood"),
    ]:
        if raw in comp_disp.columns:
            rename_map[raw] = pretty
            comp_show_cols.append(raw)

    comp_disp = comp_disp[comp_show_cols].rename(columns=rename_map)
    if "Sale Date" in comp_disp.columns:
        comp_disp["Sale Date"] = pd.to_datetime(comp_disp["Sale Date"], errors="coerce").dt.strftime("%b %Y")
    if "View" in comp_disp.columns:
        comp_disp["View"] = comp_disp["View"].astype(str).str.capitalize()

    col_config_comps = {}
    if "Sale Price" in comp_disp.columns:
        col_config_comps["Sale Price"] = st.column_config.NumberColumn("Sale Price", format="$%,.0f", width=120)
    if "PPSF" in comp_disp.columns:
        col_config_comps["PPSF"] = st.column_config.NumberColumn("PPSF", format="$%,.0f", width=90)
    if "Finished Sqft" in comp_disp.columns:
        col_config_comps["Finished Sqft"] = st.column_config.NumberColumn("Finished Sqft", format="%,.0f", width=110)
    if "Address" in comp_disp.columns:
        col_config_comps["Address"] = st.column_config.TextColumn("Address", width=280)

    st.dataframe(comp_disp, use_container_width=True, hide_index=True,
                 column_config=col_config_comps)

    if not comp_disp.empty and "PPSF" in comp_disp.columns:
        ppsf_vals = comps_df["price_per_sqft"].dropna()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Comps in Dataset",   len(comp_disp))
        m2.metric("Min PPSF",           f"${ppsf_vals.min():,.0f}")
        m3.metric("Weighted Avg PPSF",  f"${ppsf_vals.mean():,.0f}")
        m4.metric("Max PPSF",           f"${ppsf_vals.max():,.0f}")

    st.markdown('<hr class="section-rule">', unsafe_allow_html=True)

    # ── Per-lot comp detail ───────────────────────────────────────────────────
    st.markdown("#### Comp Selection — Top 15 Lots")
    st.caption(
        "For each lot: which comps were selected, their PPSF, match score, "
        "and how premiums were applied to arrive at the adjusted PPSF."
    )

    # Summary table first
    comp_summary_rows = []
    for rank, row in _top.iterrows():
        comp_summary_rows.append({
            "Rank":           rank,
            "Address":        row["Address"].split(",")[0],
            "# Comps":        row["Selected Comp Count"],
            "Raw Comp PPSF":  row["Raw Comp PPSF"],
            "Market PPSF":    row["Market PPSF"],
            "Exit PPSF":      row["Exit PPSF"],
            "Confidence":     f"{row['Comp Confidence Score']:.0%}",
            "View":           row["View Quality"].capitalize(),
            "Neighborhood":   row.get("Neighborhood", "—"),
        })
    cs_df = pd.DataFrame(comp_summary_rows)
    st.dataframe(
        cs_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Rank":        st.column_config.NumberColumn("Rank",        width=60),
            "Address":     st.column_config.TextColumn("Address",       width=190),
            "# Comps":     st.column_config.NumberColumn("Comps",       width=65),
            "Raw Comp PPSF": st.column_config.NumberColumn("Raw Comp PPSF", format="$%,.0f", width=110),
            "Market PPSF":   st.column_config.NumberColumn("Market PPSF",  format="$%,.0f", width=100),
            "Exit PPSF":     st.column_config.NumberColumn("Exit PPSF",    format="$%,.0f", width=90),
            "Confidence":  st.column_config.TextColumn("Confidence",    width=85),
            "View":        st.column_config.TextColumn("View",          width=70),
            "Neighborhood":st.column_config.TextColumn("Neighborhood",  width=140),
        },
    )

    st.markdown('<hr class="section-rule">', unsafe_allow_html=True)
    st.markdown("#### PPSF Breakdown — Per Lot")
    st.caption(
        "Shows exactly how the base comp PPSF is adjusted to reach the final projected PPSF "
        "for each of the top 15 lots."
    )

    for rank, row in _top.iterrows():
        base_ppsf   = row["Raw Comp PPSF"]
        market_ppsf = row["Market PPSF"]
        exit_ppsf   = row["Exit PPSF"]
        breakdown   = row.get("PPSF Breakdown", [])

        years_held   = row.get("Years Held", _yrs)
        appreciation = (1 + a["annual_appreciation"]) ** years_held

        st.markdown(f"**#{rank} — {row['Address'].split(',')[0]}**")

        # Selected comps table
        addrs  = [x.strip() for x in row["Selected Comp Addresses"].split(" | ")] \
                 if row["Selected Comp Addresses"] else []
        ppsfs  = [x.strip() for x in row["Selected Comp PPSFs"].split(" | ")] \
                 if row["Selected Comp PPSFs"] else []
        scores = [x.strip() for x in row["Selected Comp Match Scores"].split(" | ")] \
                 if row["Selected Comp Match Scores"] else []

        if addrs:
            detail_df = pd.DataFrame({
                "Comp Sale":    addrs,
                "PPSF":         ppsfs  + ["—"] * max(0, len(addrs) - len(ppsfs)),
                "Match Score":  scores + ["—"] * max(0, len(addrs) - len(scores)),
            })
            st.dataframe(detail_df, use_container_width=True, hide_index=True,
                         column_config={
                             "Comp Sale":   st.column_config.TextColumn("Comp Sale",   width=300),
                             "PPSF":        st.column_config.TextColumn("PPSF",        width=90),
                             "Match Score": st.column_config.TextColumn("Match Score", width=100),
                         })
        else:
            st.caption("No comps selected.")

        notes = row.get("Comp Notes", "")
        if notes and ("Fallback" in notes or "weak" in notes.lower()):
            st.warning(notes)

        # PPSF waterfall: Raw → attribute adjustments → Market PPSF → appreciation → new build → Exit PPSF
        waterfall_rows = [{
            "Step":          "Raw comp PPSF (score-weighted avg)",
            "Lot":           "—",
            "Neutral Base":  "—",
            "Net Delta":     "—",
            "$/sf Change":   "—",
            "Running PPSF":  f"${base_ppsf:,.0f}",
        }]
        running = base_ppsf
        for adj in (breakdown or []):
            if str(adj.get("Adjustment", "")).startswith("Net Applied"):
                continue
            delta = adj["Delta ($/sf)"]
            running += delta
            waterfall_rows.append({
                "Step":         adj["Adjustment"],
                "Lot":          adj.get("Lot", "—"),
                "Neutral Base": adj.get("Comp Baseline", "—"),
                "Net Delta":    adj.get("Net Delta", "—"),
                "$/sf Change":  f"+${delta:,.0f}" if delta >= 0 else f"-${abs(delta):,.0f}",
                "Running PPSF": f"${running:,.0f}",
            })
        floor_gap = round(market_ppsf - running)
        if floor_gap > 0:
            running = market_ppsf
            waterfall_rows.append({
                "Step":         "Floor applied (lot not below comp baseline)",
                "Lot":          "—", "Neutral Base": "—", "Net Delta": "—",
                "$/sf Change":  f"+${floor_gap:,.0f}",
                "Running PPSF": f"${running:,.0f}",
            })
        waterfall_rows.append({
            "Step":         "= Market PPSF (today's calibrated value)",
            "Lot":          "—", "Neutral Base": "—", "Net Delta": "—",
            "$/sf Change":  "—",
            "Running PPSF": f"${market_ppsf:,.0f}",
        })
        waterfall_rows.append({
            "Step":         f"Appreciation ({a['annual_appreciation']:.1%}/yr × {years_held} yrs)",
            "Lot":          "—", "Neutral Base": "—",
            "Net Delta":    f"×{appreciation:.3f}",
            "$/sf Change":  f"+${market_ppsf * (appreciation - 1):,.0f}",
            "Running PPSF": f"${market_ppsf * appreciation:,.0f}",
        })
        waterfall_rows.append({
            "Step":         f"New construction premium (+{a['new_construction_premium']:.0%})",
            "Lot":          "—", "Neutral Base": "—", "Net Delta": "—",
            "$/sf Change":  f"+${market_ppsf * appreciation * a['new_construction_premium']:,.0f}",
            "Running PPSF": f"${exit_ppsf:,.0f}",
        })
        waterfall_rows.append({
            "Step":         "= Exit PPSF (expected sale price/sqft at close)",
            "Lot":          "—", "Neutral Base": "—", "Net Delta": "—",
            "$/sf Change":  "—",
            "Running PPSF": f"${exit_ppsf:,.0f}",
        })

        wf_df = pd.DataFrame(waterfall_rows)
        st.dataframe(wf_df, use_container_width=True, hide_index=True,
                     column_config={
                         "Step":          st.column_config.TextColumn("Step",          width=300),
                         "Lot":           st.column_config.TextColumn("Lot",           width=75),
                         "Neutral Base":  st.column_config.TextColumn("Neutral Base",  width=105),
                         "Net Delta":     st.column_config.TextColumn("Net Delta",     width=90),
                         "$/sf Change":   st.column_config.TextColumn("$/sf Change",   width=95),
                         "Running PPSF":  st.column_config.TextColumn("Running PPSF",  width=110),
                     })

        st.markdown('<hr class="section-rule">', unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 — HOW IT WORKS
# ═════════════════════════════════════════════════════════════════════════════
with tab_how:
    st.markdown("#### How the Analysis Works")
    st.caption("A full walkthrough of the methodology — from raw listings to the final ranking.")

    _appr_yrs = max(1, int(a["target_sale_year"]) - int(a["analysis_year"]))
    _appr_factor = (1 + a["annual_appreciation"]) ** _appr_yrs

    st.markdown(f"""
---
### 1. Data & Eligibility

**Starting universe:** All vacant land listings in Pacific Palisades from the uploaded database —
{len(lots_df)} properties total.

**Eligibility:** Every lot in the database is ranked. Lots without any verified square footage
(no ParcelQuest records and no permitted plans) fall back to a 4,000 sqft default and are
flagged as low-confidence. This analysis currently covers **{len(_all)} lots**.

---
### 2. After-Build Square Footage

The projected home size is sourced in priority order:

| Priority | Source | Confidence |
|---|---|---|
| 1 | Permitted / approved / RTI plans on file | High |
| 2 | ParcelQuest pre-fire sqft × 1.10 | Medium |
| 3 | 4,000 sqft default | Low |

The 1.10 multiplier reflects the standard rebuild-plus: owners rebuilding after a total loss
almost always increase slightly beyond the original footprint.

---
### 3. Comparable Sales Selection

For each lot the engine scores every comp in the database and takes the top matches (up to 8).
Each comp is scored out of **{30+25+20+20+15+10} points** across five factors:

| Factor | Max Points | Logic |
|---|---|---|
| Neighborhood match | 30 | Exact sub-neighborhood match (15 pts for same broad tier: hillside or lower) |
| View type match | 25 | Same view category (ocean / canyon / city / mountain / partial / none) |
| Home size proximity | 20 | Finished sqft within 25% of target (10 pts within 67%, 4 pts within 150%) |
| Geographic distance | 20 | Within 0.5 mi (14 pts within 1 mi, 7 pts within 2 mi) |
| Lot size proximity | 15 | Lot sqft within 33% of target (7 pts within 100%) |
| Price tier | 3–10 | $3M=3 pts · $5M=6 pts · $7M=8 pts · $10M=10 pts |

Only comps with a sale price above the threshold set in the sidebar ($3M or $5M) and with a
sale date before 2025 are eligible.

If fewer than 3 targeted comps meet the threshold, the engine falls back to all available
comps globally and flags the result as low-confidence.

**Outlier removal:** When 4 or more comps are selected, IQR-based filtering removes any comp
whose PPSF falls outside 1.5× the interquartile range, provided at least 3 comps remain.

---
### 4. Score-Weighted Average PPSF

The base price per sqft is the **score-weighted average** of the selected comps:

```
comp_ppsf = Σ(ppsf_i × score_i) / Σ(score_i)
```

A comp with a match score of 90 pulls the estimate 3× harder than a comp scoring 30,
so weak matches have proportionally less influence on the final number.

---
### 5. PPSF Adjustment for Lot Attributes

The raw comp PPSF is adjusted upward based on the lot's specific attributes relative to a
**neutral baseline** (partial view · medium privacy · Village neighborhood). Premiums are
applied as the net delta above that baseline.

**Why conservative?** The comp selection engine already scores comps on both neighborhood
(30 pts) and view type (25 pts). That means for a well-matched lot, the selected comps
already reflect those attributes in their sale prices — applying large additional premiums
on top would count the same factor twice. Premiums are therefore set as small residual
adjustments only. Privacy and topography are more defensible as standalone adjustments
since the comp scorer does not score on either.

| Attribute | Level | Premium | Net delta vs neutral |
|---|---|---|---|
| View | Ocean | +15% | +10% above partial baseline |
| View | Canyon | +10% | +5% above partial baseline |
| View | Mountain | +7% | +2% above partial baseline |
| View | City | +5% | neutral (0 delta) |
| View | Partial | +5% | neutral (0 delta) |
| View | None | 0% | −5% (floored at 0) |
| Privacy | Very High | +10% | +6% above medium baseline |
| Privacy | High | +6% | +2% above medium baseline |
| Privacy | Medium | +4% | neutral (0 delta) |
| Privacy | Low | 0% | −4% (floored at 0) |
| Topography | Flat | +5% | +5% above 0 baseline |
| Topography | Gentle | +2% | +2% above 0 baseline |
| Topography | Moderate | −5% | −5% (floored at 0) |
| Topography | Steep | −12% | −12% (floored at 0) |
| Neighborhood | Riviera | +13% | +3% above Village baseline |
| Neighborhood | Castellammare | +12% | +2% above Village baseline |
| Neighborhood | Highlands | +11% | +1% above Village baseline |
| Neighborhood | Upper Palisades | +10% | neutral (0 delta) |
| Neighborhood | Village | +10% | neutral (0 delta) |
| Neighborhood | Marquez Knolls | +8% | −2% (floored at 0) |

**Floor rule:** the adjusted PPSF is never allowed to go below the raw comp PPSF. If the
net delta across all four attributes is negative (lot is below neutral on all dimensions),
the adjustment is simply set to zero and the raw comp PPSF is used as-is. This prevents
penalizing a lot for being slightly below a baseline when the comp pool may already reflect
similar characteristics.

The full step-by-step waterfall (Raw Comp PPSF → attribute adjustments → Market PPSF → appreciation → new build premium → Exit PPSF) is shown in the Comp Analysis tab for each of the top 15 lots.

---
### 6. Market PPSF → Exit PPSF

```
exit_ppsf      = market_ppsf × appreciation_factor × (1 + new_construction_premium)
estimated_sale = exit_ppsf × after_build_sqft
```

- **Appreciation:** {a['annual_appreciation']:.1%}/yr compounded over {_appr_yrs} years
  ({int(a['analysis_year'])} → {int(a['target_sale_year'])}) = ×{_appr_factor:.3f}
- **New construction premium:** +{a['new_construction_premium']:.0%} — brand-new builds
  command a premium over the resale comps that set the baseline PPSF

---
### 7. Total Project Cost

```
total_cost = land + construction + carrying
```

| Component | Formula | Current default |
|---|---|---|
| Land | Asking price | — |
| Construction | $/sqft × after-build sqft | ${a['construction_cost_per_sqft']:,.0f}/sqft |
| Carrying | rate/yr × years held × land | {a['additional_lot_cost_rate']:.1%}/yr × {_appr_yrs} yrs = {a['additional_lot_cost_rate']*_appr_yrs:.1%} of land |

Carrying cost models the financing and opportunity cost of holding the land through
entitlement, permitting, and construction before the eventual sale.

---
### 8. Return on Cost & Buy Signal

```
profit = estimated_future_sale − total_project_cost
ROC %  = profit / total_project_cost × 100
```

| Signal | Threshold |
|---|---|
| Strong Buy | Profit > 25% of total cost |
| Buy | Profit > 15% of total cost |
| Maybe | Profit > 5% of total cost |
| Pass | Profit ≤ 5% of total cost |

---
### 9. Why These 15?

The top 15 are the **{len(_all)} analyzed lots sorted by ROC%**, highest first. Ties break
on absolute profit, then on the overall lot quality score (view + privacy + usability +
location + price attractiveness − risk). There is no manual selection — change any sidebar
assumption and the ranking updates automatically.

---
### 10. Attribute Sources

| Attribute | Source |
|---|---|
| Asking price, lot sqft | Listings database |
| Pre-fire sqft | ParcelQuest county records (sqft_enrichment.csv) |
| View quality | Satellite + street-view photo verification (50 lots manually reviewed) |
| Topography | USGS SRTM elevation API — 5-point slope measurement per lot |
| Privacy | Street-suffix heuristic + known Pacific Palisades cul-de-sac list |
| Neighborhood | Geographic lat/lon classifier (Riviera / Castellammare / Highlands / Upper Palisades / Village / Marquez Knolls) |
| PPSF comps | Redfin comparable sales database |
    """)




# ═════════════════════════════════════════════════════════════════════════════
# TAB 7 — SINGLE ADDRESS
# ═════════════════════════════════════════════════════════════════════════════
with tab_single:
    st.markdown("#### Analyze Any Address")
    st.caption("Searches the loaded database first, then runs the full pipeline.")

    with st.form("single_address_form"):
        fc1, fc2 = st.columns([2, 1])
        with fc1:
            sa_address = st.text_input("Property address",
                                       placeholder="1116 Maroney Ln, Pacific Palisades, CA")
            sa_url  = st.text_input("Listing URL (optional)", placeholder="https://redfin.com/…")
            sa_desc = st.text_area("Paste listing description (optional)", height=80)
        with fc2:
            sa_ask   = st.number_input("Asking price override ($0 = database)", value=0.0, step=50_000.0)
            sa_lot   = st.number_input("Lot sqft override (0 = database)",       value=0.0, step=500.0)
            sa_build = st.number_input("Buildable sqft override (0 = database)", value=0.0, step=100.0)
            sa_view  = st.selectbox("View override",       ["(use data)","ocean","canyon","city","mountain","partial","none"])
            sa_priv  = st.selectbox("Privacy override",    ["(use data)","very_high","high","medium","low"])
            sa_topo  = st.selectbox("Topography override", ["(use data)","flat","gentle","moderate","steep"])
        sa_submitted = st.form_submit_button("Run Analysis", type="primary")

    if sa_submitted and sa_address.strip():
        overrides = {}
        if sa_ask   > 0:             overrides["asking_price"]   = sa_ask
        if sa_lot   > 0:             overrides["lot_size_sqft"]  = sa_lot
        if sa_build > 0:             overrides["buildable_sqft"] = sa_build
        if sa_view  != "(use data)": overrides["view_quality"]   = sa_view
        if sa_priv  != "(use data)": overrides["privacy"]        = sa_priv
        if sa_topo  != "(use data)": overrides["topography"]     = sa_topo

        with st.spinner(f"Analyzing {sa_address}…"):
            r = analyze_single_address(
                address=sa_address.strip(), comps_df=comps_df, lots_df=lots_df,
                assumptions=a, overrides=overrides,
                listing_description=sa_desc, listing_url=sa_url,
            )

        if r["found_in_db"]:
            st.success(f"Found in database (match {r['db_match_score']:.0%})")
        else:
            st.warning("Not in database — blank record used with your inputs.")

        if r["missing_fields"]:
            st.warning("Missing fields (fallbacks used): " +
                       ", ".join(f"`{f}`" for f in r["missing_fields"]))

        st.markdown(f"### {r['recommendation']}  —  {r['address']}")

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        yrs  = r.get("years_held", 4)
        rate = r.get("lot_cost_rate", 0.045)
        m1.metric("Asking Price",  f"${r['asking_price']/1e6:.2f}M")
        m2.metric("Proj. Sale",    f"${r['estimated_future_sale']/1e6:.2f}M")
        m3.metric("Total Cost",    f"${r['total_project_cost']/1e6:.2f}M")
        m4.metric("Est. Profit",   f"${r['estimated_profit']/1e6:.2f}M")
        m5.metric("Profit Margin", f"{r.get('profit_margin', 0):.1%}",
                  help="Profit / sale price")
        m6.metric("Return on Cost",f"{r['profit_percentage']:.1%}",
                  help="Profit / total project cost")

        st.divider()
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            st.markdown("**Build Sqft**")
            st.write(f"{r['after_build_sqft']:,.0f} sf")
            st.caption(f"{r['build_sqft_source']}  ·  {r['build_sqft_confidence']} confidence")
        with sc2:
            st.markdown("**PPSF**")
            st.write(f"Market: **${r['adjusted_comp_ppsf']:,.0f}**  →  Exit: **${r['projected_future_ppsf']:,.0f}**")
            st.caption(f"{r['selected_comp_count']} comp(s)  ·  {r['comp_confidence']:.0%} confidence")
        with sc3:
            st.markdown("**Carrying Cost**")
            st.write(f"${r['transaction_cost']:,.0f}")
            st.caption(f"{rate:.1%}/yr × {yrs} yrs = {rate*yrs:.1%} of land")

        with st.expander(f"Selected Comps ({r['selected_comp_count']})"):
            if r["comp_notes"]:
                st.info(r["comp_notes"])
            sa_addrs   = r["selected_comp_addresses"].split(" | ") if r["selected_comp_addresses"] else []
            sa_ppsfs   = r["selected_comp_ppsfs"].split(" | ")     if r["selected_comp_ppsfs"]     else []
            sa_mscores = r["selected_comp_match_scores"].split(" | ") if r["selected_comp_match_scores"] else []
            if sa_addrs:
                st.dataframe(pd.DataFrame({
                    "Address":     sa_addrs,
                    "PPSF":        sa_ppsfs   + ["—"] * max(0, len(sa_addrs) - len(sa_ppsfs)),
                    "Match Score": sa_mscores + ["—"] * max(0, len(sa_addrs) - len(sa_mscores)),
                }), use_container_width=True, hide_index=True)

        with st.expander("Investment Memo"):
            memo_md = generate_memo_md(r)
            st.markdown(memo_md)
            st.download_button("Download memo (.md)", data=memo_md,
                               file_name=f"memo_{r['address'][:40].replace(' ','_').replace(',','')}.md",
                               mime="text/markdown")

        sa_out   = Path("output/address_analysis") / \
                   __import__("src.address_analyzer", fromlist=["_slug"])._slug(r["address"])
        sa_paths = write_outputs(r, sa_out)
        dl_c1, dl_c2 = st.columns(2)
        with dl_c1:
            with open(sa_paths["analysis"]) as f:
                st.download_button("Download analysis.csv", f.read(), "analysis.csv", "text/csv")
        with dl_c2:
            with open(sa_paths["comps"]) as f:
                st.download_button("Download selected_comps.csv", f.read(), "selected_comps.csv", "text/csv")
