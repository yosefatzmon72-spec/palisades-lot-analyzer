import pandas as pd

LUXURY_PRICE_MIN     = 3_000_000   # default minimum comp sale price ($3M)
COMP_PRICE_PREFERRED = 5_000_000   # used for confidence tier scoring
COMP_CUTOFF_DATE     = pd.Timestamp("2025-01-01")
COMP_MIN_DATE        = pd.Timestamp("2020-01-01")
COMP_QUALITY_THRESHOLD = 10
COMP_MAX_PER_LOT       = 8


# ── Filtering ────────────────────────────────────────────────────────────────

def filter_luxury_comps(comps: pd.DataFrame, min_price: float | None = None) -> pd.DataFrame:
    """Return pre-2025 comps above min_price with valid price_per_sqft."""
    if min_price is None:
        min_price = LUXURY_PRICE_MIN
    df = comps.copy()
    df["sale_date"] = pd.to_datetime(df["sale_date"], errors="coerce")
    if "price_per_sqft" not in df.columns or (df["price_per_sqft"].fillna(0) == 0).all():
        df["price_per_sqft"] = df["sale_price"] / df["finished_sqft"].replace(0, float("nan"))
    return df[
        df["sale_date"].notna()
        & (df["sale_date"] >= COMP_MIN_DATE)
        & (df["sale_date"] < COMP_CUTOFF_DATE)
        & (df["sale_price"] >= min_price)
        & df["price_per_sqft"].notna()
        & (df["price_per_sqft"] > 0)
    ].copy()


# ── Per-comp scoring ─────────────────────────────────────────────────────────

def _score_comp_match(lot_row: pd.Series, comp_row: pd.Series) -> float:
    """Score how well a comp matches the subject lot (higher = better).

    Points:
      30 — neighborhood match
      25 — view type match
      20 — home size proximity
      15 — lot size proximity
       3–10 — price tier ($3M=3, $5M=6, $7M=8, $10M=10)
      20 — distance bonus (only when lat/lon available)
    """
    score = 0.0

    lot_nbhd  = str(lot_row.get("neighborhood",  "")).strip().lower()
    comp_nbhd = str(comp_row.get("neighborhood", "")).strip().lower()
    _HILLSIDE = {"riviera", "castellammare", "highlands", "upper palisades"}
    _LOWER    = {"village", "marquez knolls"}
    if lot_nbhd and comp_nbhd:
        if lot_nbhd == comp_nbhd:
            score += 30.0
        elif (lot_nbhd in _HILLSIDE and comp_nbhd in _HILLSIDE) or \
             (lot_nbhd in _LOWER and comp_nbhd in _LOWER):
            score += 15.0

    lot_view  = str(lot_row.get("view_quality",  "")).strip().lower()
    comp_view = str(comp_row.get("view_quality", "")).strip().lower()
    if lot_view and comp_view:
        if lot_view == comp_view:
            score += 25.0
        elif {lot_view, comp_view} <= {"ocean", "canyon", "mountain"}:
            score += 8.0

    target_sqft = max(
        float(lot_row.get("buildable_sqft")  or 0),
        float(lot_row.get("prev_home_sqft")  or 0),
    )
    comp_sqft = float(comp_row.get("finished_sqft") or 0)
    if target_sqft > 0 and comp_sqft > 0:
        ratio = comp_sqft / target_sqft
        if   0.80 <= ratio <= 1.25: score += 20.0
        elif 0.60 <= ratio <= 1.67: score += 10.0
        elif 0.40 <= ratio <= 2.50: score +=  4.0

    lot_size  = float(lot_row.get("lot_size_sqft")  or 0)
    comp_lot  = float(comp_row.get("lot_size_sqft") or 0)
    if lot_size > 0 and comp_lot > 0:
        ratio = comp_lot / lot_size
        if   0.75 <= ratio <= 1.33: score += 15.0
        elif 0.50 <= ratio <= 2.00: score +=  7.0

    lot_lat,  lot_lon  = lot_row.get("lat"),  lot_row.get("lon")
    comp_lat, comp_lon = comp_row.get("lat"), comp_row.get("lon")
    if all(v is not None and v == v for v in [lot_lat, lot_lon, comp_lat, comp_lon]):
        try:
            dist = ((float(lot_lat) - float(comp_lat)) ** 2
                    + (float(lot_lon) - float(comp_lon)) ** 2) ** 0.5 * 69.0
            score += 20.0 if dist <= 0.5 else 14.0 if dist <= 1.0 else 7.0 if dist <= 2.0 else 0.0
        except (TypeError, ValueError):
            pass

    comp_price = float(comp_row.get("sale_price") or 0)
    score += (
        10.0 if comp_price >= 10_000_000 else
         8.0 if comp_price >=  7_000_000 else
         6.0 if comp_price >=  5_000_000 else
         3.0
    )

    return score


# ── Comp selection ───────────────────────────────────────────────────────────

def select_relevant_comps(
    lot_row: pd.Series,
    comps: pd.DataFrame,
    min_comps: int = 3,
    max_comps: int = COMP_MAX_PER_LOT,
    min_price: float | None = None,
):
    """Return (selected_df, is_fallback, scores_series)."""
    luxury = filter_luxury_comps(comps, min_price=min_price)
    if luxury.empty:
        return luxury, True, pd.Series(dtype=float)

    scores = luxury.apply(lambda r: _score_comp_match(lot_row, r), axis=1)
    ranked = luxury.assign(_match_score=scores).sort_values("_match_score", ascending=False)
    top      = ranked.head(max_comps)
    fallback = len(top) < min_comps
    result   = ranked if fallback else top
    return result.drop(columns=["_match_score"]), fallback, result["_match_score"]


# ── PPSF adjustment ──────────────────────────────────────────────────────────

def _view_premium(view: str, a: dict) -> float:
    return {
        "ocean":    a.get("ocean_view_premium",    0.20),
        "canyon":   a.get("canyon_view_premium",   0.15),
        "city":     a.get("city_view_premium",     0.08),
        "mountain": a.get("mountain_view_premium", 0.10),
        "partial":  a.get("partial_view_premium",  0.05),
        "none":     0.0,
    }.get(str(view).lower(), 0.0)


def _privacy_premium(privacy: str, a: dict) -> float:
    return {
        "very_high": a.get("very_high_privacy_premium", 0.12),
        "high":      a.get("high_privacy_premium",      0.08),
        "medium":    a.get("medium_privacy_premium",    0.04),
        "low":       0.0,
    }.get(str(privacy).lower(), 0.0)


def _privacy_score_to_label(score) -> str:
    """Map numeric 0-10 privacy score (from comp rows) to a label."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "medium"
    if s >= 9:  return "very_high"
    if s >= 7:  return "high"
    if s >= 5:  return "medium"
    return "low"


def _comp_pool_baseline(
    selected: pd.DataFrame,
    match_scores: "pd.Series",
    a: dict,
) -> dict:
    """
    Compute the score-weighted average attribute premiums already embedded in
    the comp pool so we can apply only the DELTA vs. the subject lot.
    """
    weights = match_scores / match_scores.sum()

    # View baseline
    if "view_quality" in selected.columns:
        view_premiums = selected["view_quality"].fillna("none").apply(
            lambda v: _view_premium(v, a)
        )
        view_base = float((view_premiums * weights).sum())
    else:
        # No view data in comps — use partial view as neutral baseline so that
        # ocean/canyon lots receive a meaningful positive delta.
        view_base = a.get("partial_view_premium", 0.05)

    # Privacy baseline — comps store numeric privacy_score (0-10)
    if "privacy_score" in selected.columns:
        priv_premiums = selected["privacy_score"].apply(
            lambda s: _privacy_premium(_privacy_score_to_label(s), a)
        )
        privacy_base = float((priv_premiums * weights).sum())
    else:
        # No privacy data in comps — use medium as neutral baseline.
        privacy_base = _privacy_premium("medium", a)

    # Location/neighborhood baseline — comps store location_prestige as a decimal premium
    if "location_prestige" in selected.columns:
        loc_premiums = pd.to_numeric(selected["location_prestige"], errors="coerce").fillna(0.10)
        location_base = float((loc_premiums * weights).sum())
    else:
        # No location data in comps — use Village (middle tier) as neutral baseline.
        location_base = a.get("village_premium", 0.10)

    # Topography: comps don't carry topo; assume neutral (0)
    return {"view": view_base, "privacy": privacy_base, "location": location_base, "topo": 0.0}


def adjust_price_per_sqft(
    base_ppsf: float,
    lot_row: pd.Series,
    assumptions: dict | None = None,
    comp_pool: pd.DataFrame | None = None,
    match_scores: "pd.Series | None" = None,
) -> float:
    """Apply delta adjustments (lot vs. comp pool) to the score-weighted comp PPSF."""
    return _adjust_with_breakdown(base_ppsf, lot_row, assumptions, comp_pool, match_scores)[0]


def _adjust_with_breakdown(
    base_ppsf: float,
    lot_row: pd.Series,
    assumptions: dict | None = None,
    comp_pool: pd.DataFrame | None = None,
    match_scores: "pd.Series | None" = None,
) -> tuple[float, list[dict]]:
    """
    Return (adjusted_ppsf, breakdown_rows).

    Adjustments are DELTAS vs. the comp pool's embedded premiums, preventing
    double-counting when comps already have ocean views, high privacy, etc.
    """
    a = assumptions or {}

    # Comp pool baseline (what's already in the PPSF)
    if comp_pool is not None and match_scores is not None and not comp_pool.empty:
        baseline = _comp_pool_baseline(comp_pool, match_scores, a)
    else:
        baseline = {"view": 0.0, "privacy": 0.0, "location": 0.0, "topo": 0.0}

    # Lot attributes
    view_label    = str(lot_row.get("view_quality", "none")).lower()
    privacy_label = str(lot_row.get("privacy", "low")).lower()
    nbhd_label    = str(lot_row.get("neighborhood", "")).lower()
    topo          = str(lot_row.get("topography", "moderate")).lower()

    lot_view     = _view_premium(view_label, a)
    lot_privacy  = _privacy_premium(privacy_label, a)
    lot_location = {
        "riviera":          a.get("riviera_premium",          0.16),
        "castellammare":    a.get("castellammare_premium",    0.15),
        "highlands":        a.get("highlands_premium",        0.13),
        "upper palisades":  a.get("upper_palisades_premium",  0.12),
        "village":          a.get("village_premium",          0.10),
        "marquez knolls":   a.get("marquez_knolls_premium",   0.08),
    }.get(nbhd_label, a.get("default_neighborhood_premium", 0.05))

    if topo in ("flat", "gentle"):
        lot_topo = {"flat": a.get("flat_usability_premium", 0.05),
                    "gentle": a.get("gentle_usability_premium", 0.02)}.get(topo, 0.0)
    else:
        lot_topo = -{"moderate": a.get("moderate_slope_discount", 0.05),
                     "steep":    a.get("steep_slope_discount",    0.15)}.get(topo, 0.05)

    # Net deltas (lot premium minus what comps already reflect)
    view_delta     = lot_view     - baseline["view"]
    privacy_delta  = lot_privacy  - baseline["privacy"]
    location_delta = lot_location - baseline["location"]
    topo_delta     = lot_topo     - baseline["topo"]

    total_adj    = view_delta + privacy_delta + location_delta + topo_delta
    adjusted_raw = base_ppsf * (1 + total_adj)
    # Comp PPSF is the floor — adjustments add premium; they never discount below
    # what comparable sold homes already support.
    floored  = adjusted_raw < base_ppsf
    adjusted = max(adjusted_raw, base_ppsf)

    def _fmt_rate(v):
        return (f"+{v:.1%}" if v >= 0 else f"{v:.1%}")

    def _fmt_delta(v):
        d = round(base_ppsf * v)
        return (f"+${d:,.0f}" if d >= 0 else f"-${abs(d):,.0f}")

    applied_adj = 0.0 if floored else total_adj

    breakdown = [
        {
            "Adjustment":    f"View — {view_label.capitalize()}",
            "Lot":           f"{lot_view:.1%}",
            "Comp Baseline": f"{baseline['view']:.1%}",
            "Net Delta":     _fmt_rate(view_delta),
            "Delta ($/sf)":  round(base_ppsf * view_delta),
        },
        {
            "Adjustment":    f"Privacy — {privacy_label.replace('_',' ').capitalize()}",
            "Lot":           f"{lot_privacy:.1%}",
            "Comp Baseline": f"{baseline['privacy']:.1%}",
            "Net Delta":     _fmt_rate(privacy_delta),
            "Delta ($/sf)":  round(base_ppsf * privacy_delta),
        },
        {
            "Adjustment":    f"Neighborhood — {str(lot_row.get('neighborhood','—'))}",
            "Lot":           f"{lot_location:.1%}",
            "Comp Baseline": f"{baseline['location']:.1%}",
            "Net Delta":     _fmt_rate(location_delta),
            "Delta ($/sf)":  round(base_ppsf * location_delta),
        },
        {
            "Adjustment":    f"Topography — {topo.capitalize()}",
            "Lot":           f"{lot_topo:.1%}",
            "Comp Baseline": "0.0%",
            "Net Delta":     _fmt_rate(topo_delta),
            "Delta ($/sf)":  round(base_ppsf * topo_delta),
        },
        {
            "Adjustment":    "Net Applied" + (" (floored — comps already reflect lot quality)" if floored else ""),
            "Lot":           "—",
            "Comp Baseline": "—",
            "Net Delta":     _fmt_rate(applied_adj),
            "Delta ($/sf)":  round(base_ppsf * applied_adj),
        },
    ]

    return adjusted, breakdown


# ── Main per-lot function ────────────────────────────────────────────────────

def compute_lot_supported_price_per_sqft(
    lot_row: pd.Series,
    comps: pd.DataFrame,
    assumptions: dict | None = None,
    min_comps: int = 3,
    max_comps: int = COMP_MAX_PER_LOT,
) -> dict:
    """Return per-lot comp PPSF, selected comp details, and metadata."""
    a = assumptions or {}
    min_price = float(a.get("min_comp_sale_price", LUXURY_PRICE_MIN))

    selected, fallback, match_scores = select_relevant_comps(
        lot_row, comps, min_comps, max_comps, min_price=min_price
    )

    if selected.empty:
        return {
            "comp_ppsf": None, "adjusted_ppsf": None,
            "selected_comp_count": 0, "selected_comp_addresses": "",
            "selected_comp_ppsfs": "", "selected_comp_match_scores": "",
            "comp_confidence": 0.0,
            "comp_notes": f"No qualifying comps (pre-2025, ≥${min_price:,.0f}).",
        }

    # Remove PPSF outliers via IQR when enough comps exist to do so safely
    if len(selected) >= 4:
        ppsf_s = selected["price_per_sqft"]
        q1, q3 = ppsf_s.quantile(0.25), ppsf_s.quantile(0.75)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        keep = (ppsf_s >= lo) & (ppsf_s <= hi)
        if keep.sum() >= 3:
            selected     = selected[keep]
            match_scores = match_scores[keep]

    comp_ppsf = float(
        (selected["price_per_sqft"] * match_scores).sum() / match_scores.sum()
    )
    adjusted_ppsf, ppsf_breakdown = _adjust_with_breakdown(
        comp_ppsf, lot_row, assumptions, comp_pool=selected, match_scores=match_scores
    )

    addresses = selected["address"].dropna().astype(str).tolist() if "address" in selected.columns else []
    ppsfs  = [f"${v:,.0f}" for v in selected["price_per_sqft"].fillna(0)]
    scores = [f"{v:.0f}"   for v in match_scores]

    # Confidence: strong if ≥8 comps with ≥5 above preferred ($5M) threshold
    n      = len(selected)
    n_high = int((selected["sale_price"] >= COMP_PRICE_PREFERRED).sum()) if "sale_price" in selected.columns else 0
    n_low  = n - n_high

    if fallback:
        notes      = f"Fallback: {n} global comps used (not enough targeted matches)."
        confidence = round(min(0.50, n / 10.0), 2)
    elif n >= 8 and n_high >= 5:
        notes      = f"{n} lot-specific comps selected (view, size, neighborhood, price tier)."
        confidence = 1.0
    elif n >= 5:
        base        = min(1.0, n / 8.0)
        high_ratio  = n_high / n if n > 0 else 0
        confidence  = round(base * (0.60 + 0.40 * high_ratio), 2)
        notes       = f"{n} lot-specific comps selected (view, size, neighborhood, price tier)."
    else:
        confidence = round(min(0.50, n / 5.0), 2)
        notes      = f"{n} lot-specific comps selected (view, size, neighborhood, price tier)."

    if n_low > 0:
        notes += f" ({n_low} comp(s) in the $3M–$5M range.)"

    return {
        "comp_ppsf":                comp_ppsf,
        "adjusted_ppsf":            adjusted_ppsf,
        "ppsf_breakdown":           ppsf_breakdown,
        "selected_comp_count":      n,
        "selected_comp_addresses":  " | ".join(addresses),
        "selected_comp_ppsfs":      " | ".join(ppsfs),
        "selected_comp_match_scores": " | ".join(scores),
        "comp_confidence":          confidence,
        "comp_notes":               notes,
    }


# ── Dataset quality audit ────────────────────────────────────────────────────

def comp_dataset_quality(comps: pd.DataFrame, min_price: float | None = None) -> dict:
    """Audit the uploaded comp file. Returns transparency report with tier counts."""
    if min_price is None:
        min_price = LUXURY_PRICE_MIN

    df = comps.copy()
    df["sale_date"] = pd.to_datetime(df["sale_date"], errors="coerce")
    for c in ("sale_price", "finished_sqft"):
        if c not in df.columns:
            df[c] = float("nan")

    total        = len(df)
    missing_mask = (
        df["sale_price"].isna()
        | (pd.to_numeric(df["sale_price"],    errors="coerce").fillna(0) <= 0)
        | df["finished_sqft"].isna()
        | (pd.to_numeric(df["finished_sqft"], errors="coerce").fillna(0) <= 0)
        | df["sale_date"].isna()
    )
    excluded_missing  = int(missing_mask.sum())
    complete          = df[~missing_mask].copy()
    excluded_post_2025 = int((complete["sale_date"] >= COMP_CUTOFF_DATE).sum())
    pre_2025          = complete[complete["sale_date"] < COMP_CUTOFF_DATE].copy()

    comps_above_3m  = int((pre_2025["sale_price"] >= 3_000_000).sum())
    comps_above_5m  = int((pre_2025["sale_price"] >= 5_000_000).sum())
    comps_above_7m  = int((pre_2025["sale_price"] >= 7_000_000).sum())
    comps_above_10m = int((pre_2025["sale_price"] >= 10_000_000).sum())
    excluded_below_price = int((pre_2025["sale_price"] < min_price).sum())

    valid_luxury = len(filter_luxury_comps(comps, min_price=min_price))

    return {
        "total_uploaded":       total,
        "valid_luxury":         valid_luxury,
        "excluded_post_2025":   excluded_post_2025,
        "excluded_below_price": excluded_below_price,
        "excluded_missing_data": excluded_missing,
        "is_limited":           valid_luxury < COMP_QUALITY_THRESHOLD,
        "all_lots_share_pool":  valid_luxury <= COMP_MAX_PER_LOT,
        "warning_threshold":    COMP_QUALITY_THRESHOLD,
        "comps_above_3m":       comps_above_3m,
        "comps_above_5m":       comps_above_5m,
        "comps_above_7m":       comps_above_7m,
        "comps_above_10m":      comps_above_10m,
        "min_price_used":       min_price,
    }


def get_lot_comp_detail_rows(
    lot_row: pd.Series,
    comps: pd.DataFrame,
    assumptions: dict | None = None,
    max_comps: int = COMP_MAX_PER_LOT,
) -> pd.DataFrame:
    """Return the full selected comp rows for this lot with match_score column."""
    a         = assumptions or {}
    min_price = float(a.get("min_comp_sale_price", LUXURY_PRICE_MIN))
    luxury    = filter_luxury_comps(comps, min_price=min_price)
    if luxury.empty:
        return pd.DataFrame()
    scores = luxury.apply(lambda r: _score_comp_match(lot_row, r), axis=1)
    ranked = luxury.assign(match_score=scores).sort_values("match_score", ascending=False)
    return ranked.head(max_comps).reset_index(drop=True)
