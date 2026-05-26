#!/usr/bin/env python3
"""
download_lot_photos.py
----------------------
Downloads listing photos and map screenshots for each lot from:
  1. Redfin listing page  (existing URLs)
  2. Zillow listing page  (searched by address)
  3. Google Maps          (satellite + street-view screenshot at lot coordinates)

Saves to data/lot_photos/{slug}/:
    redfin_1.jpg ... redfin_3.jpg
    zillow_1.jpg ... zillow_3.jpg
    maps_satellite.png
    maps_streetview.png

Usage:
    python3 automation/download_lot_photos.py [--limit N] [--resume]
"""
import argparse
import asyncio
import re
import ssl
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from playwright.async_api import async_playwright

from src.data_loader import load_lots
from src.data_validator import validate_and_repair_lots

LOTS_FILE  = "data/cleaned_current_land_listings_database (2).xlsx"
PHOTOS_DIR = Path(__file__).resolve().parent.parent / "data" / "lot_photos"
MAX_PHOTOS = 3


def _slug(address: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", address.lower()).strip("_")[:60]


def _done_slugs() -> set:
    if not PHOTOS_DIR.exists():
        return set()
    return {
        p.name for p in PHOTOS_DIR.iterdir()
        if p.is_dir() and any(f.suffix in (".jpg", ".png") for f in p.iterdir())
    }


def _zillow_url(address: str) -> str:
    """Build a Zillow search URL from an address string."""
    parts = address.split(",")
    street = parts[0].strip().replace(" ", "-")
    city   = parts[1].strip().replace(" ", "-") if len(parts) > 1 else "Pacific-Palisades"
    state  = "CA"
    zip_   = "90272"
    return f"https://www.zillow.com/homes/{street}-{city}-{state}-{zip_}_rb/"


def _gmaps_satellite_url(lat: float, lon: float, zoom: int = 18) -> str:
    return f"https://www.google.com/maps/@{lat},{lon},{zoom}z/data=!3m1!1e3"


def _gmaps_streetview_url(lat: float, lon: float) -> str:
    # Heading ~270° = facing west (toward Pacific Ocean from Pacific Palisades)
    return (
        f"https://www.google.com/maps/@{lat},{lon},3a,75y,270h,90t"
        f"/data=!3m6!1e1!3m4!1sAF1Qip!2e10!7i13312!8i6656"
    )


async def _get_redfin_photos(page, url: str) -> list[str]:
    urls = []
    try:
        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        content = await page.content()

        # Extract full-size CDN photos from page JSON
        raw = re.findall(
            r'"(https://ssl\.cdn-redfin\.com/[^"]+\.(?:jpg|jpeg))"',
            content, re.IGNORECASE,
        )
        full = [u for u in raw if "/t_" not in u]  # skip thumbnail variants
        urls = list(dict.fromkeys(full))[:MAX_PHOTOS]

        if not urls:
            # Fallback: img elements
            imgs = await page.locator("img[src*='cdn-redfin']").all()
            for img in imgs[:10]:
                src = await img.get_attribute("src") or ""
                if "jpg" in src.lower() and "/t_" not in src:
                    urls.append(src)
                if len(urls) >= MAX_PHOTOS:
                    break
    except Exception as e:
        print(f"      Redfin error: {e}")
    return urls[:MAX_PHOTOS]


async def _get_zillow_photos(page, address: str) -> list[str]:
    urls = []
    try:
        zurl = _zillow_url(address)
        await page.goto(zurl, timeout=25000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        content = await page.content()

        # Zillow photo URLs in JSON
        raw = re.findall(
            r'"(https://photos\.zillowstatic\.com/fp/[^"]+\.(?:jpg|jpeg|webp))"',
            content, re.IGNORECASE,
        )
        # Keep only uncropped / full-size
        full = [u for u in raw if "p_f" in u or "p_e" in u or "p_d" in u or "_p_f" in u]
        if not full:
            full = list(dict.fromkeys(raw))
        urls = list(dict.fromkeys(full))[:MAX_PHOTOS]

    except Exception as e:
        print(f"      Zillow error: {e}")
    return urls[:MAX_PHOTOS]


async def _screenshot_gmaps(page, lat: float, lon: float, lot_dir: Path):
    """Take satellite + street view screenshots from Google Maps."""
    # Satellite
    try:
        await page.goto(_gmaps_satellite_url(lat, lon), timeout=20000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)
        # Dismiss cookie banner if present
        try:
            await page.locator("button:has-text('Accept all')").click(timeout=2000)
            await page.wait_for_timeout(1000)
        except Exception:
            pass
        sat_path = lot_dir / "maps_satellite.png"
        await page.screenshot(path=str(sat_path), full_page=False)
        print(f"      ✓ maps_satellite.png")
    except Exception as e:
        print(f"      satellite error: {e}")

    # Street view
    try:
        sv_url = _gmaps_streetview_url(lat, lon)
        await page.goto(sv_url, timeout=20000, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        sv_path = lot_dir / "maps_streetview.png"
        await page.screenshot(path=str(sv_path), full_page=False)
        print(f"      ✓ maps_streetview.png")
    except Exception as e:
        print(f"      streetview error: {e}")


def _download(url: str, dest: Path):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.redfin.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            dest.write_bytes(resp.read())
    except Exception as e:
        print(f"      download failed: {e}")


async def run(limit: int | None, resume: bool):
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    done = _done_slugs() if resume else set()

    raw  = load_lots(LOTS_FILE)
    lots_clean, _ = validate_and_repair_lots(raw)
    lots = lots_clean.to_dict("records")

    todo = [l for l in lots if _slug(l["address"]) not in done]
    if limit:
        todo = todo[:limit]

    print(f"\nFetching photos for {len(todo)} lots → {PHOTOS_DIR}")
    print("Sources: Redfin + Zillow + Google Maps (satellite + street view)\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx_b = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx_b.new_page()

        for i, lot in enumerate(todo, 1):
            addr   = lot["address"]
            rf_url = str(lot.get("redfin_url", "")).strip()
            lat    = float(lot.get("lat") or 0)
            lon    = float(lot.get("lon") or 0)
            slug   = _slug(addr)
            lot_dir = PHOTOS_DIR / slug
            lot_dir.mkdir(exist_ok=True)

            print(f"[{i:>3}/{len(todo)}] {addr[:60]}")

            # ── Redfin ──────────────────────────────────────────────────────
            if rf_url.startswith("http"):
                rf_photos = await _get_redfin_photos(page, rf_url)
                for j, purl in enumerate(rf_photos, 1):
                    dest = lot_dir / f"redfin_{j}.jpg"
                    _download(purl, dest)
                    if dest.exists() and dest.stat().st_size > 5000:
                        print(f"      ✓ redfin_{j}.jpg ({dest.stat().st_size//1024}KB)")
                    else:
                        dest.unlink(missing_ok=True)
                await asyncio.sleep(1.0)

            # ── Zillow ──────────────────────────────────────────────────────
            zl_photos = await _get_zillow_photos(page, addr)
            for j, purl in enumerate(zl_photos, 1):
                dest = lot_dir / f"zillow_{j}.jpg"
                _download(purl, dest)
                if dest.exists() and dest.stat().st_size > 5000:
                    print(f"      ✓ zillow_{j}.jpg ({dest.stat().st_size//1024}KB)")
                else:
                    dest.unlink(missing_ok=True)
            await asyncio.sleep(1.0)

            # ── Google Maps ─────────────────────────────────────────────────
            if lat and lon:
                await _screenshot_gmaps(page, lat, lon, lot_dir)
            await asyncio.sleep(0.8)

            total_files = len(list(lot_dir.glob("*.jpg"))) + len(list(lot_dir.glob("*.png")))
            print(f"      → {total_files} file(s) saved")

        await browser.close()

    total = sum(1 for _ in PHOTOS_DIR.rglob("*") if _.suffix in (".jpg", ".png"))
    print(f"\nDone. {total} images across {len(todo)} lots.")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--limit",  type=int, default=None)
    p.add_argument("--resume", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(limit=args.limit, resume=args.resume))
