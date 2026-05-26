from .comp_engine import compute_lot_supported_price_per_sqft


# ── After-build sqft ────────────────────────────────────────────────────────

def compute_after_build_sqft(lot_row) -> tuple:
    """
    Returns (sqft: float, source: str, confidence: str).

    Priority:
    1. buildable_sqft  — permitted / approved / RTI sqft
    2. prev_home_sqft × 1.10  — real pre-fire sqft from ParcelQuest enrichment
    3. Fixed 4,000 sqft default — no real data available
    """
    buildable = float(lot_row.get("buildable_sqft") or 0)
    if buildable > 0:
        return buildable, "permitted/approved/RTI", "high"

    prev = float(lot_row.get("prev_home_sqft") or 0)
    if prev > 0:
        return prev * 1.10, f"ParcelQuest: {prev:,.0f} sqft pre-fire × 1.10", "medium"

    return 4_000.0, "default (no sqft data — needs verification)", "low"


# ── Appreciation ─────────────────────────────────────────────────────────────

def compute_appreciation_factor(analysis_year: int, target_year: int, rate: float) -> float:
    return (1 + rate) ** max(0, target_year - analysis_year)


# ── Future sale price ────────────────────────────────────────────────────────

def estimate_future_sale_price(lot_row, comp_info: dict, assumptions: dict) -> tuple:
    """
    Returns (future_sale_price: float, projected_ppsf: float).

    projected_future_ppsf = adjusted_comp_ppsf
                            × appreciation_factor
                            × (1 + new_construction_premium)

    Premiums for view / privacy / topography are already baked into
    adjusted_comp_ppsf by adjust_price_per_sqft() — not re-applied here.
    The new_construction_premium is the only forward-looking multiplier added.
    """
    adjusted_ppsf = comp_info.get("adjusted_ppsf")
    after_build_sqft, _, _ = compute_after_build_sqft(lot_row)
    if not adjusted_ppsf or after_build_sqft == 0:
        return 0.0, 0.0

    appreciation = compute_appreciation_factor(
        assumptions.get("analysis_year", 2025),
        assumptions.get("target_sale_year", 2029),
        assumptions.get("annual_appreciation", 0.04),
    )
    ncp = 1 + assumptions.get("new_construction_premium", 0.10)
    projected_ppsf = adjusted_ppsf * appreciation * ncp
    return after_build_sqft * projected_ppsf, projected_ppsf


# ── Project cost ─────────────────────────────────────────────────────────────

def estimate_project_cost(lot_row, after_build_sqft: float, assumptions: dict) -> dict:
    """
    Total Project Cost = Land
                       + (construction_cost_per_sqft × After-Build Sqft)
                       + (additional_lot_cost_rate × years_held × Land)

    years_held = target_sale_year − analysis_year
    """
    land = float(lot_row.get("asking_price") or 0)
    cpp   = assumptions.get("construction_cost_per_sqft", 800)
    alcr  = assumptions.get("additional_lot_cost_rate", 0.045)
    years_held = max(1, assumptions.get("target_sale_year", 2029)
                        - assumptions.get("analysis_year",   2025))

    construction = cpp * after_build_sqft
    transaction  = alcr * years_held * land
    total = land + construction + transaction
    return {
        "land_cost":        land,
        "construction_cost": construction,
        "transaction_cost":  transaction,
        "total_project_cost": total,
        "years_held":        years_held,
        "lot_cost_rate":     alcr,
    }


# ── Full financial analysis ──────────────────────────────────────────────────

def analyze_financials(lot_row, comps, assumptions: dict, comp_info=None):
    comp_info = comp_info or compute_lot_supported_price_per_sqft(
        lot_row, comps, assumptions
    )
    after_build_sqft, build_sqft_source, build_sqft_confidence = compute_after_build_sqft(lot_row)
    estimated_sale, projected_ppsf = estimate_future_sale_price(lot_row, comp_info, assumptions)
    costs = estimate_project_cost(lot_row, after_build_sqft, assumptions)

    profit = estimated_sale - costs["total_project_cost"]
    # Return on Cost = profit / total invested (developer's standard metric)
    profit_pct = profit / costs["total_project_cost"] if costs["total_project_cost"] else 0.0
    # Profit Margin = profit / sale price (what % of revenue is profit)
    profit_margin = profit / estimated_sale if estimated_sale else 0.0
    rol = profit / costs["land_cost"] if costs["land_cost"] > 0 else 0.0

    return {
        **costs,
        **comp_info,
        "after_build_sqft": after_build_sqft,
        "build_sqft_source": build_sqft_source,
        "build_sqft_confidence": build_sqft_confidence,
        "projected_ppsf": projected_ppsf,
        "estimated_future_sale": estimated_sale,
        "estimated_profit": profit,
        "profit_percentage": profit_pct,
        "profit_margin": profit_margin,
        "return_on_land": rol,
        "years_held": costs.get("years_held", 4),
        "lot_cost_rate": costs.get("lot_cost_rate", 0.045),
        "selling_cost": 0.0,
    }
