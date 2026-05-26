#!/usr/bin/env python3
"""
enrich_attributes.py
--------------------
Enriches every lot with view_quality, topography, and privacy using:

  Phase 1 — OpenTopoData elevation API (batched, fast, no auth)
             → slope  → flat / gentle / moderate / steep
             → elevation + ocean distance → ocean / canyon / city view estimate

  Phase 2 — Playwright headless Redfin scrape
             → full listing description + Facts & Features
             → overrides Phase 1 where explicit data found

Saves results to data/attributes_enrichment.csv (incremental, resumable).

Usage:
    python3 automation/enrich_attributes.py
    python3 automation/enrich_attributes.py --headless --limit 20 --resume
"""
import argparse
import asyncio
import csv
import json
import math
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from playwright.async_api import async_playwright

from src.data_loader import load_lots
from src.data_validator import validate_and_repair_lots
from src.description_parser import parse_description_signals

OUTPUT_FILE = Path(__file__).resolve().parent.parent / "data" / "attributes_enrichment.csv"
LOTS_FILE   = "data/cleaned_current_land_listings_database (2).xlsx"

# Pacific Palisades coastline reference points (lat, lon) — used to compute
# distance-to-ocean for view estimation.
COAST_POINTS = [
    (34.024, -118.510),
    (34.028, -118.525),
    (34.032, -118.540),
    (34.036, -118.550),
    (34.039, -118.558),
    (34.042, -118.566),
    (34.045, -118.574),
]

# ── Geometry helpers ──────────────────────────────────────────────────────────

def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def dist_to_ocean_miles(lat, lon) -> float:
    return min(_haversine_miles(lat, lon, clat, clon) for clat, clon in COAST_POINTS)


# ── Phase 1: Elevation → topography + view estimate ──────────────────────────

def _build_elevation_batch(lots: list[dict]) -> list[tuple]:
    """Return list of (address, lat, lon, role) for all sample points."""
    points = []
    step_lat = 0.00045  # ~50 m
    step_lon = 0.00054  # ~50 m at 34° lat
    for lot in lots:
        lat, lon = lot["lat"], lot["lon"]
        points.append((lot["address"], lat, lon, "center"))
        points.append((lot["address"], lat + step_lat, lon, "N"))
        points.append((lot["address"], lat - step_lat, lon, "S"))
        points.append((lot["address"], lat, lon + step_lon, "E"))
        points.append((lot["address"], lat, lon - step_lon, "W"))
    return points


def _fetch_elevations(points: list[tuple]) -> dict[str, float]:
    """Call OpenTopoData SRTM30m in batches of 80. Returns {addr_role: elevation_m}."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    results = {}
    batch_size = 80
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        locs = "|".join(f"{lat},{lon}" for _, lat, lon, _ in batch)
        url = f"https://api.opentopodata.org/v1/srtm30m?locations={locs}"
        try:
            resp = urllib.request.urlopen(url, timeout=15, context=ssl_ctx)
            data = json.loads(resp.read())
            for j, item in enumerate(data.get("results", [])):
                addr, _, _, role = batch[j]
                elev = item.get("elevation")
                if elev is not None:
                    results[f"{addr}|{role}"] = float(elev)
        except Exception as e:
            print(f"  Elevation batch error: {e}")
        time.sleep(0.5)
    return results


def _meters_to_feet(m: float) -> float:
    return m * 3.28084


def _slope_pct(center_m, neighbor_m, dist_m=50.0) -> float:
    return abs(neighbor_m - center_m) / dist_m * 100


def compute_topography_and_elevation(lot: dict, elevations: dict) -> tuple:
    """Returns (topography: str, elevation_ft: float, slope_pct: float)."""
    addr = lot["address"]
    center = elevations.get(f"{addr}|center")
    if center is None:
        return "unknown", 0.0, 0.0

    neighbors = [
        elevations.get(f"{addr}|N"),
        elevations.get(f"{addr}|S"),
        elevations.get(f"{addr}|E"),
        elevations.get(f"{addr}|W"),
    ]
    slopes = [_slope_pct(center, n) for n in neighbors if n is not None]
    max_slope = max(slopes) if slopes else 0.0

    if max_slope >= 25:
        topo = "steep"
    elif max_slope >= 15:
        topo = "moderate"
    elif max_slope >= 7:
        topo = "gentle"
    else:
        topo = "flat"

    return topo, _meters_to_feet(center), max_slope


def estimate_view_from_geography(lat, lon, elevation_ft) -> str:
    """
    Heuristic view estimate based on elevation and distance to ocean.
    Pacific Palisades: high elevation + close to coast = ocean view.
    Farther inland + elevated = canyon view.
    """
    dist = dist_to_ocean_miles(lat, lon)

    if elevation_ft >= 150 and dist <= 1.2:
        return "ocean"
    if elevation_ft >= 100 and dist <= 0.9:
        return "ocean"
    if elevation_ft >= 200 and dist <= 1.8:
        return "ocean"
    if elevation_ft >= 80 and dist <= 0.7:
        return "ocean"
    if elevation_ft >= 120 and 1.0 <= dist <= 2.5:
        return "canyon"
    if elevation_ft >= 80 and dist > 1.5:
        return "canyon"
    if elevation_ft >= 60 and dist <= 1.5:
        return "partial"
    return "none"


# ── Phase 2: Playwright Redfin scraper ────────────────────────────────────────

REDFIN_SELECTORS = [
    "[data-rf-test-id='listingRemarks']",
    ".remarks",
    "[class*='remarks']",
    "[class*='description']",
    ".listingRemarks",
    "section[class*='marketing-remarks']",
]

FACTS_SELECTORS = [
    "[data-rf-test-id='facts-table']",
    ".facts-table",
    "[class*='amenity']",
    "[class*='property-details']",
    ".keyDetailsList",
]

VIEW_WORDS_REDFIN = {
    "ocean": ["ocean view", "ocean views", "pacific view", "water view", "ocean glimpse",
              "stunning ocean", "sweeping ocean", "panoramic ocean", "ocean facing"],
    "canyon": ["canyon view", "canyon views", "canyon exposure", "canyon facing",
               "rustic canyon", "topanga canyon", "canyon and ocean"],
    "city": ["city view", "city views", "cityscape", "city lights", "downtown view"],
    "mountain": ["mountain view", "mountain views", "santa monica mountains"],
    "partial": ["partial view", "partial ocean", "peek-a-boo", "peekaboo", "peek a boo",
                "glimpse of ocean", "some ocean"],
}

PRIVACY_WORDS_REDFIN = {
    "very_high": ["gated estate", "private gate", "fully private", "secluded", "estate entry",
                  "private road", "private drive", "gated community", "fully gated"],
    "high": ["cul-de-sac", "cul de sac", "culdesac", "private", "gated", "end of road",
             "dead end", "no through traffic"],
    "medium": ["quiet street", "low traffic", "semi-private", "residential street"],
}


async def scrape_redfin(page, url: str) -> dict:
    """Scrape a Redfin listing page. Returns raw signals dict."""
    signals = {"description": "", "view_raw": None, "privacy_raw": None, "source": "redfin"}
    try:
        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

        # Extract listing description
        desc_text = ""
        for sel in REDFIN_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    desc_text = (await el.inner_text()).strip()
                    if len(desc_text) > 30:
                        break
            except Exception:
                continue

        # Extract facts/features sections (may contain "View: Ocean" etc.)
        facts_text = ""
        for sel in FACTS_SELECTORS:
            try:
                els = page.locator(sel)
                count = await els.count()
                if count > 0:
                    for i in range(min(count, 10)):
                        facts_text += " " + (await els.nth(i).inner_text())
            except Exception:
                continue

        full_text = (desc_text + " " + facts_text).lower()
        signals["description"] = desc_text

        # Detect view
        for vtype, phrases in VIEW_WORDS_REDFIN.items():
            if any(p in full_text for p in phrases):
                signals["view_raw"] = vtype
                break

        # Detect privacy
        for plevel, phrases in PRIVACY_WORDS_REDFIN.items():
            if any(p in full_text for p in phrases):
                signals["privacy_raw"] = plevel
                break

    except Exception as e:
        signals["error"] = str(e)
    return signals


# ── Final attribute resolver ──────────────────────────────────────────────────

def resolve_attributes(lot: dict, geo: dict, redfin: dict) -> dict:
    """
    Merge geographic estimates with Redfin data.
    Redfin explicit data wins over geographic heuristic.
    """
    view_geo    = geo.get("view_estimate", "unknown")
    view_rf     = redfin.get("view_raw")
    privacy_rf  = redfin.get("privacy_raw")
    topo        = geo.get("topography", "unknown")

    # Run description parser on Redfin text for additional signals
    desc = redfin.get("description", "")
    parser_signals = parse_description_signals(desc) if desc else {}
    view_parser  = parser_signals.get("view_quality", (None, None))[0]
    priv_parser  = parser_signals.get("privacy",      (None, None))[0]
    topo_parser  = parser_signals.get("topography",   (None, None))[0]

    # Priority: Redfin structured > parser from description > geo heuristic
    final_view    = view_rf or view_parser or view_geo or "unknown"
    final_privacy = privacy_rf or priv_parser or "unknown"
    final_topo    = topo_parser or topo or "unknown"

    view_source  = "redfin_scrape" if (view_rf or view_parser) else "geo_heuristic"
    priv_source  = "redfin_scrape" if (privacy_rf or priv_parser) else "unknown"
    topo_source  = ("description" if topo_parser else "elevation_api")

    return {
        "address":        lot["address"],
        "view_quality":   final_view,
        "privacy":        final_privacy,
        "topography":     final_topo,
        "elevation_ft":   round(geo.get("elevation_ft", 0), 1),
        "slope_pct":      round(geo.get("slope_pct", 0), 1),
        "dist_to_ocean":  round(geo.get("dist_to_ocean", 0), 2),
        "view_source":    view_source,
        "privacy_source": priv_source,
        "topo_source":    topo_source,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def _load_existing() -> set:
    if not OUTPUT_FILE.exists():
        return set()
    df = pd.read_csv(OUTPUT_FILE)
    return set(df["address"].str.strip().str.lower())


def _append_row(row: dict):
    write_header = not OUTPUT_FILE.exists()
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            w.writeheader()
        w.writerow(row)


async def run(headless: bool, limit: int | None, resume: bool):
    print("\n" + "="*70)
    print("  PALISADES LOT ANALYZER — ATTRIBUTE ENRICHMENT")
    print("="*70)

    raw  = load_lots(LOTS_FILE)
    lots_clean, _ = validate_and_repair_lots(raw)
    lots = lots_clean.to_dict("records")

    done = _load_existing() if resume else set()
    if resume and done:
        print(f"  Resuming — {len(done)} already done.")

    todo = [l for l in lots if l["address"].strip().lower() not in done]
    if limit:
        todo = todo[:limit]

    print(f"  Lots to process: {len(todo)}")

    # ── Phase 1: Batch elevation ──────────────────────────────────────────────
    print("\n  Phase 1: Fetching elevation data (OpenTopoData)…")
    valid_lots = [l for l in todo if float(l.get("lat") or 0) != 0]
    all_points = _build_elevation_batch(valid_lots)
    print(f"  Querying {len(all_points)} elevation points ({len(valid_lots)} lots × 5)…")
    elevations = _fetch_elevations(all_points)
    print(f"  Got {len(elevations)} elevation values.")

    geo_data = {}
    for lot in valid_lots:
        topo, elev_ft, slope = compute_topography_and_elevation(lot, elevations)
        dist = dist_to_ocean_miles(float(lot["lat"]), float(lot["lon"]))
        view_est = estimate_view_from_geography(float(lot["lat"]), float(lot["lon"]), elev_ft)
        geo_data[lot["address"]] = {
            "topography":    topo,
            "elevation_ft":  elev_ft,
            "slope_pct":     slope,
            "dist_to_ocean": dist,
            "view_estimate": view_est,
        }

    # ── Phase 2: Redfin scraping ──────────────────────────────────────────────
    print("\n  Phase 2: Scraping Redfin listings…")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        for i, lot in enumerate(todo, 1):
            addr = lot["address"]
            url  = str(lot.get("redfin_url", "")).strip()
            geo  = geo_data.get(addr, {"topography": "unknown", "elevation_ft": 0,
                                       "slope_pct": 0, "dist_to_ocean": 0, "view_estimate": "unknown"})

            print(f"  [{i:>3}/{len(todo)}] {addr[:60]}")

            rf_signals = {"description": "", "view_raw": None, "privacy_raw": None}
            if url and url.startswith("http"):
                rf_signals = await scrape_redfin(page, url)
                err = rf_signals.get("error", "")
                status = f"view={rf_signals['view_raw'] or '—'}  priv={rf_signals['privacy_raw'] or '—'}"
                if err:
                    print(f"         Redfin error: {err[:60]}")
                else:
                    print(f"         {status}  elev={geo['elevation_ft']:.0f}ft  slope={geo['slope_pct']:.0f}%  ocean={geo['dist_to_ocean']:.1f}mi")
                await asyncio.sleep(1.5)
            else:
                print(f"         No Redfin URL — geo only")

            result = resolve_attributes(lot, geo, rf_signals)
            _append_row(result)
            print(f"         → view={result['view_quality']}  privacy={result['privacy']}  topo={result['topography']}")

        await browser.close()

    df = pd.read_csv(OUTPUT_FILE)
    print(f"\n{'='*70}")
    print(f"  Done. {len(df)} lots enriched → {OUTPUT_FILE}")
    print(f"\n  View quality breakdown:")
    print(df["view_quality"].value_counts().to_string())
    print(f"\n  Topography breakdown:")
    print(df["topography"].value_counts().to_string())
    print(f"\n  Privacy breakdown:")
    print(df["privacy"].value_counts().to_string())
    print()


def parse_args():
    p = argparse.ArgumentParser(description="Enrich lot attributes via elevation API + Redfin scraping")
    p.add_argument("--headless", action="store_true", default=True, help="Run browser headless")
    p.add_argument("--no-headless", dest="headless", action="store_false")
    p.add_argument("--limit",   type=int, default=None, help="Process only first N lots")
    p.add_argument("--resume",  action="store_true",    help="Skip already-processed lots")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(headless=args.headless, limit=args.limit, resume=args.resume))
