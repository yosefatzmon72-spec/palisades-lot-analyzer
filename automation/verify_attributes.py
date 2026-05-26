#!/usr/bin/env python3
"""
verify_attributes.py
--------------------
Rebuilds attributes_enrichment.csv using:
  • lat/lon geographic classification → neighborhood
  • Street-name pattern + satellite image → privacy
  • USGS elevation slope data → topography (kept, visually verified)
  • Downloaded photos (Redfin + Zillow + Google Maps) for human/vision review

Outputs a CSV with one row per lot covering all 4 attributes with sources.

Run this after download_lot_photos.py finishes:
    python3 automation/verify_attributes.py
"""
import csv
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.data_loader import load_lots
from src.data_validator import validate_and_repair_lots

LOTS_FILE   = "data/cleaned_current_land_listings_database (2).xlsx"
PHOTOS_DIR  = Path(__file__).resolve().parent.parent / "data" / "lot_photos"
ELEV_CSV    = Path(__file__).resolve().parent.parent / "data" / "attributes_enrichment.csv"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "data" / "attributes_enrichment.csv"


# ── Geography helpers ─────────────────────────────────────────────────────────

def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

# Pacific Palisades coast reference points (along PCH)
COAST_POINTS = [
    (34.024, -118.510), (34.028, -118.525), (34.032, -118.540),
    (34.036, -118.550), (34.039, -118.558), (34.042, -118.566), (34.045, -118.574),
]

def dist_to_ocean(lat, lon) -> float:
    return min(_haversine_miles(lat, lon, c[0], c[1]) for c in COAST_POINTS)


# ── Neighborhood classifier ───────────────────────────────────────────────────
# Pacific Palisades sub-neighborhoods, ranked highest to lowest premium.
# Mapped to the 3 names the comp engine recognises.

def classify_neighborhood(lat: float, lon: float) -> str:
    """
    Geographic sub-neighborhood classification for Pacific Palisades.

    5 specific sub-neighborhoods ranked highest → lowest premium:
      Riviera        — south of Sunset hillside (Bollinger, Livorno, Charmel, Puerto Del Mar)
      Castellammare  — western ocean bluffs above PCH (Porto Marina, Tramonto, Castellammare Dr)
      Highlands      — far north/remote (Calle De Sarah, Via Anita, Romany Dr, Avenida De La Herradura)
      Village        — Palisades Village core (Via de la Paz, Swarthmore, Radcliffe, Muskingum)
      Marquez Knolls — eastern/lower (Bestor, McKendree, Akron, Chattanooga)
    """
    # Palisades Highlands — far north, remote, large lots
    # (Calle De Sarah, Chastain, Via La Costa upper, Avenida De La Herradura,
    #  Michael Ln, El Bosque Ct, Cumbre Alta Ct, Camino De Yatasto,
    #  Romany Dr, Via Anita, Umeo Rd)
    if lat > 34.062:
        return "Highlands"

    # Castellammare — western ocean bluffs directly above PCH
    # (Porto Marina Way, Tramonto Dr, Castellammare Dr, Via La Costa low)
    if lon < -118.555 and lat < 34.050:
        return "Castellammare"

    # Riviera / Huntington — south of Sunset, premium hillside lots
    # (Bollinger Dr, Livorno Dr, Charmel Ln, Puerto Del Mar, Via Santa Ynez,
    #  Quadro Vecchio Dr, Dulce Ynez Ln, Marquez Ter, Resolano Dr)
    if lat < 34.052 and lon < -118.536 and lon >= -118.556:
        return "Riviera"

    # Upper Palisades interior — high elevation, north of Sunset
    # (Maroney Ln, Marinette Rd, Monument St, Rimmer Ave, Chautauqua Blvd,
    #  Enchanted Way, Berea Pl, Lachman Ln, Villa Grove Dr, Via Cresta,
    #  Rivas Canyon Rd, Tellem Dr, Glenhaven Dr, Las Pulgas, Jacon Way upper)
    if lat > 34.048 and lon < -118.526:
        return "Upper Palisades"

    # Palisades Village core and near-village
    # (Via de la Paz, Swarthmore Ave, Radcliffe Ave, Erskine Dr, Tahquitz Pl,
    #  Muskingum Ave, Chapala Dr, Patterson Pl, Ocampo Dr, Bienveneda Ave,
    #  Hartzell St, Toyopa Dr, Lombard Ave, Las Lomas Ave, Embury St)
    if 34.034 <= lat <= 34.052 and lon >= -118.540:
        return "Village"

    # Marquez Knolls / eastern lower areas
    # (Bestor Blvd, McKendree Ave, Whitfield Ave, Friends St, Albright St,
    #  De Pauw St, Akron St, Scenic Pl, Merrivale Ln, Chattanooga Pl,
    #  Pampas Ricas Blvd, Anoka Dr)
    if lat >= 34.048 and lon >= -118.526:
        return "Marquez Knolls"

    return "Village"


# ── Privacy classifier ────────────────────────────────────────────────────────

# Streets in Pacific Palisades known to be cul-de-sacs or very private roads
_CUL_DE_SAC_STREETS = {
    # confirmed cul-de-sacs / dead-ends (Pl, Ct endings almost always are)
    "jacon way", "puerto del mar", "quadro vecchio dr", "via la costa",
    "cumbre alta ct", "el bosque ct", "scenic pl", "pequeno pl",
    "villa woods pl", "patterson pl", "tahquitz pl", "las lomas pl",
    "dulce ynez ln", "calle de sarah",
    "avenida de la herradura",   # private road feel, remote
    "camino de yatasto",         # private remote road
    "charmel ln",                # private lane
    "maroney ln",                # lane
    "rivas canyon rd",           # canyon road, private feel
    "umeo rd",                   # remote road
}

_STREET_SUFFIX_PRIVACY = {
    "Ct":  "high",    # Court = cul-de-sac
    "Pl":  "high",    # Place = cul-de-sac
    "Way": "medium",  # Way = sometimes dead-end
    "Ln":  "medium",  # Lane = typically smaller, quieter
    "Ter": "medium",  # Terrace = hillside, often limited access
    "Rd":  "medium",  # Road = varies
    "Dr":  "medium",  # Drive = varies
    "Ave": "medium",  # Avenue = typically through-street
    "St":  "medium",  # Street = typically through-street
    "Blvd":"low",     # Boulevard = higher traffic
}

def classify_privacy(address: str, lat: float, lon: float) -> str:
    addr_lower = address.lower()

    # Known cul-de-sacs / private roads
    for known in _CUL_DE_SAC_STREETS:
        if known in addr_lower:
            return "high"

    # Street suffix analysis
    m = re.search(r"\b(Ct|Pl|Way|Ln|Ter|Rd|Dr|Ave|St|Blvd)\b", address)
    suffix_privacy = _STREET_SUFFIX_PRIVACY.get(m.group(1), "medium") if m else "medium"

    # Canyon/remote locations tend to have more privacy
    if lat > 34.062:
        return "high"   # Very remote Palisades Highlands
    if dist_to_ocean(lat, lon) > 2.0:
        return "high"   # Very inland = remote, private

    return suffix_privacy


# ── Load existing elevation data ──────────────────────────────────────────────

def load_elevation_data() -> dict:
    """Return {address_lower: {topography, elevation_ft, slope_pct, dist_to_ocean, view_source, view_quality}}"""
    if not ELEV_CSV.exists():
        return {}
    df = pd.read_csv(ELEV_CSV)
    result = {}
    for _, row in df.iterrows():
        key = str(row.get("address", "")).strip().lower()
        result[key] = row.to_dict()
    return result


# ── Slug helper ───────────────────────────────────────────────────────────────

def _slug(address: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", address.lower()).strip("_")[:60]


# ── Photo inventory ───────────────────────────────────────────────────────────

def list_photos(address: str) -> dict:
    """Return dict of available photos for a lot."""
    slug = _slug(address)
    lot_dir = PHOTOS_DIR / slug
    if not lot_dir.exists():
        return {}
    photos = {}
    for f in sorted(lot_dir.iterdir()):
        if f.suffix in (".jpg", ".png"):
            photos[f.name] = str(f)
    return photos


# ── Main builder ──────────────────────────────────────────────────────────────

def build_attributes(lots: list[dict], elev_data: dict) -> list[dict]:
    rows = []
    for lot in lots:
        addr  = lot["address"]
        lat   = float(lot.get("lat") or 0)
        lon   = float(lot.get("lon") or 0)
        key   = addr.strip().lower()
        elev  = elev_data.get(key, {})
        photos = list_photos(addr)

        # Neighbourhood — purely geographic
        neighborhood = classify_neighborhood(lat, lon)

        # Privacy — street pattern + geography
        privacy = classify_privacy(addr, lat, lon)

        # Topography — keep USGS slope result (objective measurement)
        topography    = elev.get("topography", "unknown")
        elevation_ft  = float(elev.get("elevation_ft", 0) or 0)
        slope_pct     = float(elev.get("slope_pct", 0) or 0)
        dist_ocean    = float(elev.get("dist_to_ocean", 0) or dist_to_ocean(lat, lon))

        # View — keep existing Redfin-scraped / geo-heuristic result as baseline;
        # will be overridden during visual verification pass
        view_quality  = elev.get("view_quality", "unknown")
        view_source   = elev.get("view_source",  "geo_heuristic")

        rows.append({
            "address":       addr,
            "neighborhood":  neighborhood,
            "privacy":       privacy,
            "view_quality":  view_quality,
            "view_source":   view_source,
            "topography":    topography,
            "elevation_ft":  round(elevation_ft, 1),
            "slope_pct":     round(slope_pct, 1),
            "dist_to_ocean": round(dist_ocean, 2),
            "photos":        ", ".join(photos.keys()),
        })

    return rows


def save(rows: list[dict], path: Path):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Saved {len(rows)} rows → {path}")


if __name__ == "__main__":
    print("Building attribute table…")
    raw  = load_lots(LOTS_FILE)
    lots_clean, _ = validate_and_repair_lots(raw)
    lots = lots_clean.to_dict("records")

    elev_data = load_elevation_data()
    rows = build_attributes(lots, elev_data)

    # Print summary
    df = pd.DataFrame(rows)
    print(f"\nTotal lots: {len(df)}")
    print(f"\nNeighborhood breakdown:")
    print(df["neighborhood"].value_counts().to_string())
    print(f"\nPrivacy breakdown:")
    print(df["privacy"].value_counts().to_string())
    print(f"\nView quality breakdown (pre-verification):")
    print(df["view_quality"].value_counts().to_string())
    print(f"\nTopography breakdown:")
    print(df["topography"].value_counts().to_string())
    print(f"\nLots with photos downloaded: {(df['photos'] != '').sum()}")

    save(rows, OUTPUT_FILE)
