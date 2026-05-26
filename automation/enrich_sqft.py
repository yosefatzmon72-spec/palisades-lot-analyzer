#!/usr/bin/env python3
"""
enrich_sqft.py
--------------
Bulk agent: finds actual pre-fire home sqft for every lot.

Priority per lot:
  1. Already has buildable_sqft or prev_home_sqft in database  → skip
  2. Listing description mentions sqft                          → use it
  3. ParcelQuest Appraise search                               → scrape it

Saves results to data/sqft_enrichment.csv.
The analysis pipeline reads this file and merges sqft before computing
after-build sqft (the ×1.10 premium is already in compute_after_build_sqft).

Usage:
    python3 automation/enrich_sqft.py
    python3 automation/enrich_sqft.py --headless      # no browser window
    python3 automation/enrich_sqft.py --limit 10      # first 10 lots only
    python3 automation/enrich_sqft.py --resume        # skip already-done rows
"""
import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_loader import load_lots
from src.description_parser import parse_description_signals

# ── Credentials ───────────────────────────────────────────────────────────────
PQ_USERNAME  = "yoava"
PQ_PASSWORD  = "10181959ya"
PQ_LOGIN_URL = "https://parcelquestappraise.com/PQAppraise/Login"
PQ_SEARCH_URL= "https://parcelquestappraise.com/search/search_single.aspx"

# ASP.NET form field IDs (confirmed from live page inspection)
ID_USERNAME   = "#ctl00_ContentPlaceHolder1_txtUserName"
ID_PASSWORD   = "#ctl00_ContentPlaceHolder1_txtPassword"
ID_BTN_LOGIN  = "#ctl00_ContentPlaceHolder1_btnLogin"
ID_BTN_DUP    = "a#ctl00_ContentPlaceHolder1_btnContinue"   # "Login Now" on duplicate page
ID_STATE      = "#ctl00_ContentPlaceHolder1_FindCounty1_ddlState"
ID_COUNTY     = "#ctl00_ContentPlaceHolder1_FindCounty1_ddlCounty"
ID_HOUSE_NUM  = "#ctl00_ContentPlaceHolder1_SearchAddressFieldsExtended1_msHouseNumber"
ID_STREET_NAME= "#ctl00_ContentPlaceHolder1_SearchAddressFieldsExtended1_msStreetName"
ID_SUBMIT     = "#ctl00_ContentPlaceHolder1_btnSubmitForm"

LOTS_FILE    = "data/cleaned_current_land_listings_database (2).xlsx"
OUTPUT_FILE  = "data/sqft_enrichment.csv"

# ── Street suffix stripping ───────────────────────────────────────────────────
_SUFFIX_RE = re.compile(
    r"\b(street|st|avenue|ave|boulevard|blvd|drive|dr|lane|ln|road|rd|"
    r"way|court|ct|place|pl|terrace|ter|circle|cir|highway|hwy|"
    r"parkway|pkwy|trail|trl|loop|run|pass|path|alley|aly|"
    r"bend|brg|bridge|byu|byp|bypass|camp|cp|canyon|cyn|"
    r"causeway|cswy|center|ctr|cliff|clf|club|clb|common|commons|"
    r"corner|cor|crossing|xing|dale|dl|dam|dm|divide|dv|"
    r"estate|est|expressway|expy|extension|ext|falls|fls|"
    r"ferry|fry|field|fld|flats|flt|ford|frd|freeway|fwy|"
    r"garden|gdn|gardens|gdns|gateway|gtwy|glen|gln|green|grn|"
    r"grove|grv|harbor|hbr|haven|hvn|heights|hts|highway|hwy|"
    r"hill|hl|hills|hls|hollow|holw|inlet|inlt|island|is|"
    r"junction|jct|key|ky|knoll|knl|lake|lk|lakes|lks|"
    r"landing|lndg|light|lgt|lights|lgts|loaf|lf|lock|lck|"
    r"lodge|ldg|manor|mnr|meadow|mdw|meadows|mdws|mill|ml|"
    r"mills|mls|mission|msn|motorway|mtwy|mount|mt|mountain|mtn|"
    r"mountains|mtns|neck|nck|orchard|orch|oval|ovl|overpass|opas|"
    r"park|pk|parks|plaza|plz|point|pt|points|pts|port|prt|"
    r"prairie|pr|radial|radl|ramp|ranch|rnch|rapid|rpd|rapids|rpds|"
    r"rest|rst|ridge|rdg|ridges|rdgs|river|riv|row|rue|run|"
    r"shoal|shl|shoals|shls|shore|shr|shores|shrs|skyway|skwy|"
    r"spring|spg|springs|spgs|spur|sq|square|station|sta|"
    r"stravenue|stra|stream|strm|summit|smt|turnpike|tpke|"
    r"underpass|un|union|unions|valley|vly|valleys|vlys|"
    r"viaduct|via|view|vw|views|vws|village|vlg|villages|vlgs|"
    r"ville|vis|vista|walk|wall|wells|wls)\b\.?$",
    re.IGNORECASE,
)


def parse_address_for_search(address: str) -> tuple[str, str] | tuple[None, None]:
    """
    Returns (house_number, street_name_without_suffix) for ParcelQuest search.

    '1116 Maroney Ln, Pacific Palisades, CA 90272' → ('1116', 'Maroney')
    '15976 Alcima Ave, Pacific Palisades, CA 90272' → ('15976', 'Alcima')
    '1785 Alta Mura Rd, Pacific Palisades, CA 90272' → ('1785', 'Alta Mura')
    """
    # Take only the street portion (before first comma)
    street_part = address.split(",")[0].strip()

    # Split off house number (leading digits, possibly with letters like 12A)
    m = re.match(r"^(\d+\w*)\s+(.+)$", street_part)
    if not m:
        return None, None

    house_num   = m.group(1)
    street_name = m.group(2).strip()

    # Strip trailing suffix word(s)
    words = street_name.split()
    while words and _SUFFIX_RE.fullmatch(words[-1].rstrip(".")):
        words.pop()

    if not words:
        return None, None

    return house_num, " ".join(words)


# ── Description-based sqft extraction ────────────────────────────────────────

def sqft_from_description(description: str) -> float | None:
    """
    Returns home sqft found in listing description, or None.
    Checks: prev_home_sqft (explicit), then est_home_sqft (generic mention).
    """
    if not description or not str(description).strip():
        return None
    signals = parse_description_signals(str(description))

    for key in ("prev_home_sqft", "buildable_sqft", "est_home_sqft"):
        val = signals.get(key)
        if val and isinstance(val, tuple):
            sqft = float(val[0])
            if sqft >= 500:
                return sqft

    return None


# ── ParcelQuest scraper ───────────────────────────────────────────────────────

async def login(page) -> bool:
    """Log in to ParcelQuest Appraise. Returns True on success."""
    await page.goto(PQ_LOGIN_URL, wait_until="networkidle", timeout=30_000)
    try:
        await page.fill(ID_USERNAME, PQ_USERNAME)
        await page.fill(ID_PASSWORD, PQ_PASSWORD)
        await page.click(ID_BTN_LOGIN)
        await page.wait_for_load_state("networkidle", timeout=20_000)
        # Duplicate session page — click "Login Now" to force past it
        if "duplicate" in page.url.lower():
            async with page.expect_navigation(timeout=15_000):
                await page.locator(ID_BTN_DUP).click()
        return "login" not in page.url.lower() and "duplicate" not in page.url.lower()
    except Exception as e:
        print(f"  ✗ Login error: {e}")
        return False


async def _select_la_county(page):
    """Select California → Los Angeles in the county finder (once per session)."""
    await page.wait_for_selector(ID_STATE, timeout=10_000)
    current_state = await page.locator(f"{ID_STATE} option:checked").get_attribute("value") or ""
    if "CA" not in current_state and "California" not in current_state:
        await page.select_option(ID_STATE, label="California")
        await page.wait_for_load_state("networkidle", timeout=10_000)
    # Wait for counties to populate then pick LA
    await page.wait_for_function(
        f"document.querySelector('{ID_COUNTY}').options.length > 1", timeout=10_000
    )
    county_opts = await page.locator(f"{ID_COUNTY} option").all_text_contents()
    la = next((o for o in county_opts if "los angeles" in o.lower()), None)
    if la:
        await page.select_option(ID_COUNTY, label=la)
        await page.wait_for_load_state("networkidle", timeout=10_000)


async def search_parcelquest(page, address: str, county_set: list) -> float | None:
    """
    Search one address on ParcelQuest and return the SQ.FT, or None.
    county_set is a mutable list used as a flag — first call sets county, rest skip.
    """
    house_num, street_name = parse_address_for_search(address)
    if not house_num or not street_name:
        print(f"    Could not parse address: {address}")
        return None

    try:
        await page.goto(PQ_SEARCH_URL, wait_until="networkidle", timeout=20_000)

        # Select county once per session
        if not county_set:
            await _select_la_county(page)
            county_set.append(True)

        # Fill search fields
        await page.fill(ID_HOUSE_NUM, f"{house_num}*")
        await page.fill(ID_STREET_NAME, f"{street_name}*")
        await page.click(ID_SUBMIT)
        await page.wait_for_load_state("networkidle", timeout=30_000)

        return await _extract_sqft_from_results(page, address)

    except Exception as e:
        print(f"    Search error for {address}: {e}")
        return None


async def _extract_sqft_from_results(page, original_address: str) -> float | None:
    """
    Two page types after a search:

    A) Results list (Results.aspx?t=SP) — multiple APNs for same address.
       Confirmed td column layout:
         0:APN  1:Street  2:County  3:Price  4:Date  5:Owner  6:(blank)
         7:Lot Size  8:SQ.FT  9:Year Built  10:Beds  11:Baths
       Return highest non-zero value at td[8].

    B) Property Detail (Property_Detail.aspx) — single result, goes straight to
       the detail page. Look for the "Building Area" label row.
    """
    await page.wait_for_timeout(1_000)
    url = page.url

    # ── Case B: Property Detail page ─────────────────────────────────────────
    if "Property_Detail" in url:
        rows = await page.locator("tr").all()
        for row in rows:
            cells = await row.locator("td").all_text_contents()
            cleaned = [c.strip() for c in cells]
            # Find the cell with "Building Area" and read the next numeric cell
            for i, c in enumerate(cleaned):
                if "building area" in c.lower():
                    # Value is usually in the next cell
                    for j in range(i + 1, min(i + 4, len(cleaned))):
                        val_str = cleaned[j].replace(",", "").replace(".", "")
                        try:
                            val = float(cleaned[j].replace(",", ""))
                            if 300 <= val <= 30_000:
                                return val
                        except ValueError:
                            continue
        # Fallback: regex on full page text
        body = await page.inner_text("body")
        m = re.search(r"building\s+area[:\s]+([0-9,]+)", body, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 300 <= val <= 30_000:
                    return val
            except ValueError:
                pass

    # ── Case A: Results list page ─────────────────────────────────────────────
    else:
        SQFT_COL = 8  # confirmed from live page inspection
        rows = await page.locator("table tr").all()
        best_sqft = None
        for row in rows:
            td_cells = await row.locator("td").all_text_contents()
            if len(td_cells) <= SQFT_COL:
                continue
            val_str = td_cells[SQFT_COL].strip().replace(",", "")
            try:
                val = float(val_str)
                if 500 <= val <= 30_000:
                    if best_sqft is None or val > best_sqft:
                        best_sqft = val
            except ValueError:
                continue
        if best_sqft:
            return best_sqft

    # Screenshot for debugging
    screenshot_path = f"output/debug_{re.sub(r'[^a-z0-9]', '_', original_address.lower()[:30])}.png"
    Path("output").mkdir(exist_ok=True)
    await page.screenshot(path=screenshot_path)
    print(f"    Could not find sqft — screenshot: {screenshot_path}")
    return None


# ── Main bulk loop ────────────────────────────────────────────────────────────

async def run(headless: bool, limit: int | None, resume: bool):
    from playwright.async_api import async_playwright

    # Load lots
    lots_df = load_lots(LOTS_FILE)
    if limit:
        lots_df = lots_df.head(limit)

    # Load existing enrichment if resuming
    enrichment_path = Path(OUTPUT_FILE)
    if resume and enrichment_path.exists():
        existing = pd.read_csv(enrichment_path)
        done_addresses = set(existing["address"].str.strip().str.lower())
        print(f"  Resuming — {len(done_addresses)} already done.")
    else:
        existing = pd.DataFrame(columns=["address", "prev_home_sqft", "source", "after_build_sqft"])
        done_addresses = set()

    results = existing.to_dict("records")
    total = len(lots_df)
    pq_needed = []
    skipped   = []
    desc_hits = []

    print(f"\n  Pass 1: checking existing data + descriptions ({total} lots)…")
    for _, row in lots_df.iterrows():
        addr = str(row.get("address", "")).strip()
        if not addr:
            continue
        if addr.lower() in done_addresses:
            continue

        # Already has good sqft data
        buildable = float(row.get("buildable_sqft") or 0)
        prev      = float(row.get("prev_home_sqft")  or 0)
        if buildable > 0 or prev > 0:
            sqft = buildable if buildable > 0 else prev
            source = "buildable_sqft" if buildable > 0 else "prev_home_sqft"
            results.append({
                "address":         addr,
                "prev_home_sqft":  sqft,
                "source":          f"database:{source}",
                "after_build_sqft": round(sqft * 1.10),
            })
            skipped.append(addr)
            continue

        # Try description
        desc = str(row.get("listing_description", "") or "")
        desc_sqft = sqft_from_description(desc)
        if desc_sqft:
            results.append({
                "address":         addr,
                "prev_home_sqft":  desc_sqft,
                "source":          "description",
                "after_build_sqft": round(desc_sqft * 1.10),
            })
            desc_hits.append(addr)
            continue

        pq_needed.append((addr, row))

    print(f"    Already in DB:    {len(skipped)}")
    print(f"    Found in desc:    {len(desc_hits)}")
    print(f"    Need ParcelQuest: {len(pq_needed)}")

    if pq_needed:
        print(f"\n  Pass 2: scraping ParcelQuest for {len(pq_needed)} lots…")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, slow_mo=300)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page    = await context.new_page()

            print("  Logging in…")
            logged_in = await login(page)
            if not logged_in:
                print("  ✗ Login failed. Check credentials.")
                await browser.close()
                sys.exit(1)
            print("  ✓ Logged in.")
            county_set = []  # mutable flag — first search sets CA/LA county

            for i, (addr, _) in enumerate(pq_needed, 1):
                house_num, street = parse_address_for_search(addr)
                print(f"  [{i}/{len(pq_needed)}] {addr}  →  search: {house_num}* {street}*")

                sqft = await search_parcelquest(page, addr, county_set)
                if sqft:
                    print(f"    ✓ Found: {sqft:,.0f} sqft  →  after-build: {sqft*1.10:,.0f}")
                    results.append({
                        "address":         addr,
                        "prev_home_sqft":  sqft,
                        "source":          "parcelquest",
                        "after_build_sqft": round(sqft * 1.10),
                    })
                else:
                    print(f"    ✗ Not found — will use lot-size estimate")
                    results.append({
                        "address":         addr,
                        "prev_home_sqft":  None,
                        "source":          "not_found",
                        "after_build_sqft": None,
                    })

                # Save progress after every lot
                pd.DataFrame(results).to_csv(OUTPUT_FILE, index=False)
                time.sleep(0.5)  # polite delay

            await browser.close()

    # Final save
    out_df = pd.DataFrame(results)
    out_df.to_csv(OUTPUT_FILE, index=False)

    found   = out_df[out_df["prev_home_sqft"].notna() & (out_df["source"] != "not_found")]
    missing = out_df[out_df["source"] == "not_found"]

    print(f"\n{'='*60}")
    print(f"  ENRICHMENT COMPLETE")
    print(f"{'='*60}")
    print(f"  Total lots processed : {len(out_df)}")
    print(f"  Sqft found           : {len(found)}  ({len(found)/len(out_df):.0%})")
    print(f"  Not found            : {len(missing)} (will use lot-size estimate)")
    print(f"  Saved to             : {OUTPUT_FILE}")
    print()


def main():
    p = argparse.ArgumentParser(description="Bulk sqft enrichment via description + ParcelQuest")
    p.add_argument("--headless", action="store_true", help="Run browser headlessly (no window)")
    p.add_argument("--limit",    type=int, default=None, help="Only process first N lots")
    p.add_argument("--resume",   action="store_true",   help="Skip lots already in enrichment CSV")
    args = p.parse_args()

    asyncio.run(run(headless=args.headless, limit=args.limit, resume=args.resume))


if __name__ == "__main__":
    main()
