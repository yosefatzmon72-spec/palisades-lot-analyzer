from pathlib import Path

import pandas as pd

from .description_parser import parse_description_signals

SQFT_ENRICHMENT_FILE  = Path(__file__).resolve().parent.parent / "data" / "sqft_enrichment.csv"
ATTR_ENRICHMENT_FILE  = Path(__file__).resolve().parent.parent / "data" / "attributes_enrichment.csv"


def normalize_currency(value):
    if pd.isna(value):
        return None
    if isinstance(value, str):
        return float(value.replace("$", "").replace(",", "").strip())
    return float(value)


def _build_full_address(df: pd.DataFrame) -> pd.DataFrame:
    """Combine address + city + state + zip into one full address string.

    Only applies when the source file splits location across multiple columns
    (e.g. Redfin exports).  If no city/state columns exist, returns unchanged.
    """
    if "city" not in df.columns and "state" not in df.columns:
        return df
    df = df.copy()
    parts = df["address"].fillna("").astype(str).str.strip()
    if "city" in df.columns:
        parts = parts + ", " + df["city"].fillna("").astype(str).str.strip()
    if "state" in df.columns:
        parts = parts + ", " + df["state"].fillna("").astype(str).str.strip()
    if "zip" in df.columns:
        # zip may be int/float — convert cleanly to 5-digit string
        zip_str = df["zip"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
        parts = parts + " " + zip_str
    df["address"] = parts
    return df


def load_lots(path_or_buffer):
    is_excel = (
        isinstance(path_or_buffer, str)
        and str(path_or_buffer).lower().endswith((".xlsx", ".xls"))
    ) or (
        hasattr(path_or_buffer, "name")
        and str(path_or_buffer.name).lower().endswith((".xlsx", ".xls"))
    )
    if is_excel:
        df = pd.read_excel(path_or_buffer, engine="openpyxl")
    else:
        df = pd.read_csv(path_or_buffer)

    # Build full address from Redfin-style split columns
    if "address" in df.columns:
        df = _build_full_address(df)

    # Rename lat/lon to the names expected by the comp distance scorer
    if "latitude" in df.columns:
        df = df.rename(columns={"latitude": "lat", "longitude": "lon"})

    # Normalize common column names from Excel/Redfin exports
    lot_rename = {}
    if "list_price" in df.columns:
        lot_rename["list_price"] = "asking_price"
    if "neighborhood_or_location" in df.columns:
        lot_rename["neighborhood_or_location"] = "neighborhood"
    if lot_rename:
        df = df.rename(columns=lot_rename)

    df["asking_price"] = df["asking_price"].apply(normalize_currency)
    df["lot_size_sqft"] = pd.to_numeric(df.get("lot_size_sqft", 0), errors="coerce").fillna(0)

    if "prev_home_sqft" in df.columns:
        df["prev_home_sqft"] = pd.to_numeric(df.get("prev_home_sqft"), errors="coerce").fillna(0)
    else:
        df["prev_home_sqft"] = 0

    if "buildable_sqft" not in df.columns:
        df["buildable_sqft"] = 0.0
    else:
        df["buildable_sqft"] = pd.to_numeric(df["buildable_sqft"], errors="coerce").fillna(0)

    if "usable_lot_area_sqft" not in df.columns:
        df["usable_lot_area_sqft"] = df["lot_size_sqft"]
    else:
        df["usable_lot_area_sqft"] = pd.to_numeric(
            df["usable_lot_area_sqft"], errors="coerce"
        ).fillna(df["lot_size_sqft"])

    if "distance_to_ocean_miles" in df.columns:
        df["distance_to_ocean_miles"] = pd.to_numeric(
            df.get("distance_to_ocean_miles"), errors="coerce"
        ).fillna(0)
    else:
        df["distance_to_ocean_miles"] = 0

    if "prev_sale_price" in df.columns:
        df["prev_sale_price"] = df["prev_sale_price"].apply(
            lambda v: normalize_currency(v) if v else 0
        )
    else:
        df["prev_sale_price"] = 0

    if "assessed_value" in df.columns:
        df["assessed_value"] = df["assessed_value"].apply(
            lambda v: normalize_currency(v) if v else 0
        )
    else:
        df["assessed_value"] = 0

    df["asking_price"] = df["asking_price"].fillna(0)
    if "listing_description" not in df.columns:
        df["listing_description"] = ""
    else:
        df["listing_description"] = df["listing_description"].fillna("")

    signals = df["listing_description"].apply(parse_description_signals)
    signals_df = pd.DataFrame(signals.tolist())
    # Only add flat boolean columns — structured (value, confidence) tuple columns
    # like view_quality/privacy/topography would duplicate existing lot fields and
    # cause lot.get("view_quality") to return a Series instead of a scalar.
    bool_cols = [
        c for c in signals_df.columns
        if signals_df[c].dropna().apply(lambda v: isinstance(v, bool)).all()
        and c not in df.columns
    ]
    df = pd.concat([df, signals_df[bool_cols]], axis=1)

    # Merge sqft enrichment from ParcelQuest agent if available
    enrichment_path = Path(SQFT_ENRICHMENT_FILE)
    if enrichment_path.exists():
        enrich = pd.read_csv(enrichment_path)
        enrich = enrich[enrich["prev_home_sqft"].notna() & (enrich["source"] != "not_found")]
        if not enrich.empty and "address" in df.columns:
            enrich = enrich[["address", "prev_home_sqft"]].copy()
            enrich.columns = ["address", "_enriched_sqft"]
            enrich["address"] = enrich["address"].str.strip().str.lower()
            df["_addr_key"] = df["address"].str.strip().str.lower()
            df = df.merge(enrich, left_on="_addr_key", right_on="address",
                          how="left", suffixes=("", "_enrich"))
            # Only fill in where prev_home_sqft is currently 0/missing
            df["prev_home_sqft"] = df["prev_home_sqft"].astype(float)
            mask = (df["prev_home_sqft"] == 0) & df["_enriched_sqft"].notna()
            df.loc[mask, "prev_home_sqft"] = df.loc[mask, "_enriched_sqft"].astype(float)
            df.drop(columns=["_addr_key", "_enriched_sqft", "address_enrich"],
                    errors="ignore", inplace=True)

    # Merge attribute enrichment (view_quality, privacy, topography) if available
    attr_path = Path(ATTR_ENRICHMENT_FILE)
    if attr_path.exists():
        attrs = pd.read_csv(attr_path)
        if not attrs.empty and "address" in df.columns:
            attrs = attrs[["address", "view_quality", "privacy", "topography",
                           *[c for c in ["neighborhood"] if c in attrs.columns]]].copy()
            attrs["address"] = attrs["address"].str.strip().str.lower()
            df["_addr_key"] = df["address"].str.strip().str.lower()
            df = df.merge(attrs, left_on="_addr_key", right_on="address",
                          how="left", suffixes=("", "_attr"))
            # Generic "Pacific Palisades" labels from the source file are not
            # sub-neighborhoods — always replace them with the enriched value.
            _GENERIC_NBHD = {
                "pacific palisades", "c15 - pacific palisades",
                "not applicable", "", "nan", "none", "0", "unknown",
            }
            for field in ["view_quality", "privacy", "topography", "neighborhood"]:
                attr_col = f"{field}_attr"
                if attr_col in df.columns:
                    orig = df[field].astype(str).str.strip()
                    if field == "neighborhood":
                        is_empty = orig.str.lower().isin(_GENERIC_NBHD)
                    else:
                        is_empty = orig.isin(["", "nan", "None", "0", "unknown"])
                    new_val  = df[attr_col].astype(str).str.strip()
                    is_valid = new_val.notna() & ~new_val.isin(["", "nan", "None", "unknown"])
                    df.loc[is_empty & is_valid, field] = df.loc[is_empty & is_valid, attr_col]
            df.drop(columns=["_addr_key", "address_attr"] +
                    [f"{f}_attr" for f in ["view_quality", "privacy", "topography"]],
                    errors="ignore", inplace=True)

    return df


def load_comps(path_or_buffer):
    is_excel = (
        isinstance(path_or_buffer, str)
        and str(path_or_buffer).lower().endswith((".xlsx", ".xls"))
    ) or (
        hasattr(path_or_buffer, "name")
        and str(path_or_buffer.name).lower().endswith((".xlsx", ".xls"))
    )
    if is_excel:
        df = pd.read_excel(path_or_buffer, engine="openpyxl")
    else:
        df = pd.read_csv(path_or_buffer)

    # Build full address from Redfin-style split columns
    if "address" in df.columns:
        df = _build_full_address(df)

    # Rename lat/lon to the names expected by the comp distance scorer
    if "latitude" in df.columns:
        df = df.rename(columns={"latitude": "lat", "longitude": "lon"})

    # Normalize Excel/Redfin export columns to the internal comp schema
    rename_map = {}
    if "sold_date" in df.columns:
        rename_map["sold_date"] = "sale_date"
    if "price" in df.columns:
        rename_map["price"] = "sale_price"
    if "square_feet" in df.columns:
        rename_map["square_feet"] = "finished_sqft"
    if "lot_size" in df.columns:
        rename_map["lot_size"] = "lot_size_sqft"
    if "price_per_square_foot" in df.columns:
        rename_map["price_per_square_foot"] = "price_per_sqft"
    if "beds" in df.columns:
        rename_map["beds"] = "bedrooms"
    if "baths" in df.columns:
        rename_map["baths"] = "bathrooms"
    if "neighborhood_or_location" in df.columns:
        rename_map["neighborhood_or_location"] = "neighborhood"
    if rename_map:
        df = df.rename(columns=rename_map)

    # ── Column-shift detection (must happen BEFORE date parsing) ──────────────
    # If sale_date contains view words (ocean, canyon, …) the CSV rows are
    # misaligned by one column.  Find the real date column and remap.
    import re as _re
    _VIEW = {"ocean", "canyon", "city", "mountain", "partial", "none"}
    # Matches YYYY-MM-DD, MM/DD/YYYY, DD-MM-YYYY date strings with separators.
    # Intentionally excludes bare integers (prices/sqft) which would otherwise
    # be misidentified as nanosecond Unix timestamps by pd.to_datetime.
    _DATE_PAT = _re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
    if "sale_date" in df.columns:
        _view_hit = df["sale_date"].astype(str).str.strip().str.lower().isin(_VIEW).any()
        if _view_hit:
            _real_date_col = None
            for _c in df.columns:
                if _c == "sale_date":
                    continue
                _str_vals = df[_c].dropna().astype(str).str.strip()
                if len(_str_vals) > 0 and _str_vals.str.match(_DATE_PAT).mean() > 0.5:
                    _real_date_col = _c
                    break
            if _real_date_col:
                if "view_quality" in df.columns:
                    if not df["view_quality"].astype(str).str.strip().str.lower().isin(_VIEW).any():
                        df = df.drop(columns=["view_quality"])
                df = df.rename(columns={_real_date_col: "__tmp_date__",
                                        "sale_date": "view_quality"})
                df = df.rename(columns={"__tmp_date__": "sale_date"})
                if "lot_size_sqft" in df.columns:
                    _lsv = pd.to_numeric(df["lot_size_sqft"], errors="coerce")
                    if not _lsv.dropna().empty and float(_lsv.dropna().max()) < 100:
                        df["lot_size_sqft"] = float("nan")
    # ─────────────────────────────────────────────────────────────────────────

    df["sale_date"] = pd.to_datetime(df["sale_date"], errors="coerce")
    df["sale_price"] = df["sale_price"].apply(normalize_currency)
    df["finished_sqft"] = pd.to_numeric(df["finished_sqft"], errors="coerce").fillna(0)
    df["lot_size_sqft"] = pd.to_numeric(df["lot_size_sqft"], errors="coerce").fillna(0)
    df["price_per_sqft"] = df.apply(
        lambda row: row["sale_price"] / row["finished_sqft"]
        if row["finished_sqft"] > 0 else 0,
        axis=1,
    )
    return df
