"""
Validate, auto-repair, and normalize comps and lots DataFrames.

Column-shift detection: if the `sale_date` column contains view-type words
(ocean, canyon, city, mountain, partial, none) the CSV rows are misaligned.
The validator attempts to find the real date column and remap automatically.

Returns both a cleaned DataFrame and a human-readable quality report.
"""
import re
import pandas as pd

VIEW_WORDS = {"ocean", "canyon", "city", "mountain", "partial", "none"}

# Matches explicit date strings (YYYY-MM-DD, MM/DD/YYYY, etc.).
# Excludes bare integers so prices/sqft are never mistaken for dates.
_DATE_PAT = re.compile(
    r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$"
)
LUXURY_PRICE_MIN = 5_000_000

# Synonyms accepted on upload for comps
COMP_RENAME = {
    "home_sqft": "finished_sqft",
    "sqft": "finished_sqft",
    "square_feet": "finished_sqft",
    "living_area": "finished_sqft",
    "price": "sale_price",
    "sold_price": "sale_price",
    "close_price": "sale_price",
    "sold_date": "sale_date",
    "close_date": "sale_date",
    "closing_date": "sale_date",
    "view_type": "view_quality",
    "view": "view_quality",
    "privacy_level": "privacy_score",
    "privacy": "privacy_score",
    "neighborhood_name": "neighborhood",
    "price_per_square_foot": "price_per_sqft",
    "price_per_sf": "price_per_sqft",
    "lot_size": "lot_size_sqft",
    "beds": "bedrooms",
    "baths": "bathrooms",
    "neighborhood_or_location": "neighborhood",
}

# Synonyms accepted on upload for lots
LOT_RENAME = {
    "list_price": "asking_price",
    "listing_price": "asking_price",
    "price": "asking_price",
    "sqft": "lot_size_sqft",
    "home_sqft": "prev_home_sqft",
    "previous_home_sqft": "prev_home_sqft",
    "pre_fire_sqft": "prev_home_sqft",
    "view": "view_quality",
    "view_type": "view_quality",
    "privacy_level": "privacy",
    "privacy_score": "privacy",
    "rti_sqft": "buildable_sqft",
    "permitted_sqft": "buildable_sqft",
    "approved_sqft": "buildable_sqft",
    "neighborhood_or_location": "neighborhood",
}


# ── Type-detection helpers ──────────────────────────────────────────────────

def _frac_view(series: pd.Series) -> float:
    return series.astype(str).str.strip().str.lower().isin(VIEW_WORDS).mean()


def _frac_date(series: pd.Series) -> float:
    """Fraction of values that look like explicit date strings (not bare integers)."""
    str_vals = series.dropna().astype(str).str.strip()
    if len(str_vals) == 0:
        return 0.0
    return float(str_vals.str.match(_DATE_PAT).mean())


def _frac_large_money(series: pd.Series) -> float:
    num = pd.to_numeric(
        series.astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce"
    )
    return (num >= 100_000).mean()


def _detect_col_types(df: pd.DataFrame) -> dict:
    """Return dominant type tag for each column."""
    tags = {}
    for col in df.columns:
        s = df[col].dropna()
        if len(s) == 0:
            tags[col] = "empty"
            continue
        if _frac_view(s) > 0.5:
            tags[col] = "view_word"
        elif _frac_date(s) > 0.5 and _frac_large_money(s) < 0.3:
            tags[col] = "date"
        elif _frac_large_money(s) > 0.5:
            tags[col] = "money"
        else:
            tags[col] = "other"
    return tags


# ── Column-shift repair ─────────────────────────────────────────────────────

def _repair_comp_shift(df: pd.DataFrame):
    """
    Detect and repair a one-column shift in comps CSVs.

    Trigger: `sale_date` column contains view-type words.
    Repair:  find the column that actually holds dates, rename it `sale_date`,
             move the old `sale_date` (view words) to `view_quality`.
             Any numeric column whose max value is < 100 and is labelled
             `lot_size_sqft` is nulled out (those are bedroom counts, not sqft).

    Returns (repaired_df, was_repaired: bool, notes: str)
    """
    if "sale_date" not in df.columns:
        return df, False, ""

    view_in_sale_date = df["sale_date"].astype(str).str.strip().str.lower().isin(VIEW_WORDS).any()
    if not view_in_sale_date:
        return df, False, ""

    col_types = _detect_col_types(df)
    real_date_cols = [
        c for c, t in col_types.items()
        if t == "date" and c != "sale_date"
    ]
    if not real_date_cols:
        return df, False, (
            "Column shift detected (sale_date contains view words) "
            "but no date column found to repair from. "
            "Please ensure your CSV has a column with valid sale dates."
        )

    real_date_col = real_date_cols[0]
    df = df.copy()

    # Swap: real_date_col → sale_date, old sale_date → view_quality
    tmp = "__tmp_date__"
    df = df.rename(columns={real_date_col: tmp, "sale_date": "view_quality"})
    df = df.rename(columns={tmp: "sale_date"})

    notes = (
        f"Column shift auto-repaired: '{real_date_col}' → sale_date; "
        f"former sale_date column → view_quality."
    )

    # Null out lot_size_sqft if it looks like bedroom counts (all < 100)
    if "lot_size_sqft" in df.columns:
        num = pd.to_numeric(df["lot_size_sqft"], errors="coerce")
        if num.dropna().max() < 100:
            df["lot_size_sqft"] = float("nan")
            notes += (
                " lot_size_sqft values were < 100 (likely bedroom counts) — set to unknown."
            )

    return df, True, notes


# ── Comps validator ─────────────────────────────────────────────────────────

def validate_and_repair_comps(df_raw: pd.DataFrame):
    """
    Validate and repair a raw comps DataFrame.

    Returns
    -------
    cleaned_df   : DataFrame  — only rows that are usable
    report       : dict       — quality metrics and per-exclusion details
    """
    df = df_raw.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    df = df.rename(columns={k: v for k, v in COMP_RENAME.items() if k in df.columns})

    # Attempt column-shift repair
    df, was_repaired, repair_notes = _repair_comp_shift(df)
    shift_repair_rows = 1 if was_repaired else 0  # whole-file structural repair

    # Parse core numeric/date fields
    if "sale_date" in df.columns:
        df["sale_date"] = pd.to_datetime(df["sale_date"], errors="coerce")
    if "sale_price" in df.columns:
        df["sale_price"] = pd.to_numeric(
            df["sale_price"].astype(str).str.replace(r"[$,]", "", regex=True),
            errors="coerce",
        )
    if "finished_sqft" in df.columns:
        df["finished_sqft"] = pd.to_numeric(df["finished_sqft"], errors="coerce")
    if "lot_size_sqft" in df.columns:
        df["lot_size_sqft"] = pd.to_numeric(df["lot_size_sqft"], errors="coerce")

    # Compute price_per_sqft if missing or zero
    sqft_col = df.get("finished_sqft", pd.Series(dtype=float)) if "finished_sqft" in df.columns else pd.Series(dtype=float)
    if "price_per_sqft" not in df.columns or (df["price_per_sqft"].fillna(0) == 0).all():
        if "sale_price" in df.columns and "finished_sqft" in df.columns:
            df["price_per_sqft"] = df["sale_price"] / df["finished_sqft"].replace(0, float("nan"))

    # Per-row validation
    usable_idx, excluded = [], []
    for idx, row in df.iterrows():
        reasons = []
        if pd.isna(row.get("sale_price")) or float(row.get("sale_price") or 0) <= 0:
            reasons.append("missing or invalid sale_price")
        if pd.isna(row.get("finished_sqft")) or float(row.get("finished_sqft") or 0) <= 0:
            reasons.append("missing or invalid finished_sqft")
        if pd.isna(row.get("sale_date")):
            reasons.append("missing or unparseable sale_date")

        if reasons:
            excluded.append({"address": str(row.get("address", f"row {idx}")), "reasons": reasons})
        else:
            usable_idx.append(idx)

    cleaned = df.loc[usable_idx].copy() if usable_idx else df.iloc[0:0].copy()

    report = {
        "total_uploaded": len(df_raw),
        "rows_shift_repaired": shift_repair_rows,
        "rows_usable": len(usable_idx),
        "rows_excluded": len(excluded),
        "was_shift_repaired": was_repaired,
        "repair_notes": repair_notes,
        "excluded_detail": excluded,
    }
    return cleaned, report


# ── Lots validator ──────────────────────────────────────────────────────────

def validate_and_repair_lots(df_raw: pd.DataFrame):
    """
    Validate and repair a raw lots DataFrame.

    Returns
    -------
    cleaned_df : DataFrame
    report     : dict
    """
    df = df_raw.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    df = df.rename(columns={k: v for k, v in LOT_RENAME.items() if k in df.columns})

    # Ensure required columns exist
    for col in ["asking_price", "lot_size_sqft", "address", "listing_description"]:
        if col not in df.columns:
            df[col] = "" if col in ("address", "listing_description") else float("nan")

    # Parse numeric fields
    for money_col in ["asking_price", "prev_sale_price", "assessed_value"]:
        if money_col in df.columns:
            df[money_col] = pd.to_numeric(
                df[money_col].astype(str).str.replace(r"[$,]", "", regex=True),
                errors="coerce",
            )

    for num_col in ["lot_size_sqft", "prev_home_sqft", "buildable_sqft",
                    "usable_lot_area_sqft", "distance_to_ocean_miles"]:
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors="coerce").fillna(0)

    df["listing_description"] = df["listing_description"].fillna("")

    # Per-row validation
    usable_idx, excluded = [], []
    for idx, row in df.iterrows():
        reasons = []
        if pd.isna(row.get("asking_price")) or float(row.get("asking_price") or 0) <= 0:
            reasons.append("missing or invalid asking_price")
        if not str(row.get("address", "")).strip():
            reasons.append("missing address")
        has_sqft = (
            float(row.get("buildable_sqft") or 0) > 0
            or float(row.get("prev_home_sqft") or 0) > 0
        )
        if not has_sqft:
            reasons.append("no verified sqft (not in ParcelQuest enrichment)")

        if reasons:
            excluded.append({"address": str(row.get("address", f"row {idx}")), "reasons": reasons})
        else:
            usable_idx.append(idx)

    cleaned = df.loc[usable_idx].copy() if usable_idx else df.iloc[0:0].copy()

    report = {
        "total_uploaded": len(df_raw),
        "rows_shift_repaired": 0,
        "rows_usable": len(usable_idx),
        "rows_excluded": len(excluded),
        "was_shift_repaired": False,
        "repair_notes": "",
        "excluded_detail": excluded,
    }
    return cleaned, report
