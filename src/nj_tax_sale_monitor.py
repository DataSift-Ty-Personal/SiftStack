"""
NJ Tax Sale Monitor — Seasonal Scraper
Checks ~35 municipality subdomains on newjerseytaxsale.com (RealAuction platform)
weekly. Sites are seasonal — only live ~30 days before each municipality's annual
tax sale (typically May-June).

When a site is offline → skip (no data).
When a site is live → scrape the "Preview Items for Sale" table + detail popups
for property addresses, owner names, face amounts, block/lot, and valuations.

Covers: Middlesex, Essex, Somerset, Union counties.
Output: List of NoticeData-compatible dicts for the SiftStack enrichment pipeline.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

# ── Municipality Registry ─────────────────────────────────────────────────────
# subdomain → (municipality_name, county)
# URL pattern: https://{subdomain}.newjerseytaxsale.com

MUNICIPALITIES = {
    # ── Middlesex County ──
    "carteret":         ("Carteret", "Middlesex"),
    "cranbury":         ("Cranbury", "Middlesex"),
    "dunellen":         ("Dunellen", "Middlesex"),
    "eastbrunswick":    ("East Brunswick", "Middlesex"),
    "millstonetownship":("Millstone Township", "Middlesex"),
    "milltown":         ("Milltown", "Middlesex"),
    "monroe":           ("Monroe", "Middlesex"),
    "newbrunswick":     ("New Brunswick", "Middlesex"),
    "perthamboy":       ("Perth Amboy", "Middlesex"),
    "piscataway":       ("Piscataway", "Middlesex"),
    "sayreville":       ("Sayreville", "Middlesex"),
    "southamboy":       ("South Amboy", "Middlesex"),
    "southbrunswick":   ("South Brunswick", "Middlesex"),
    "southriver":       ("South River", "Middlesex"),
    "spotswood":        ("Spotswood", "Middlesex"),

    # ── Essex County ──
    "bloomfield":       ("Bloomfield", "Essex"),
    "caldwellborough":  ("Caldwell Borough", "Essex"),
    "cedargrove":       ("Cedar Grove", "Essex"),
    "eastorange":       ("East Orange", "Essex"),
    "essexfells":       ("Essex Fells", "Essex"),
    "montclair":        ("Montclair", "Essex"),
    "newark":           ("Newark", "Essex"),
    "nutley":           ("Nutley", "Essex"),
    "southorangevillage":("South Orange Village", "Essex"),
    "verona":           ("Verona", "Essex"),
    "westorange":       ("West Orange", "Essex"),

    # ── Somerset County ──
    "bernards":         ("Bernards", "Somerset"),
    "boundbrook":       ("Bound Brook", "Somerset"),
    "bridgewater":      ("Bridgewater", "Somerset"),
    "montgomery":       ("Montgomery", "Somerset"),
    "peapackgladstone": ("Peapack Gladstone", "Somerset"),
    "raritantownship":  ("Raritan Township", "Somerset"),
    "somerville":       ("Somerville", "Somerset"),
    "southboundbrook":  ("South Bound Brook", "Somerset"),
    "watchung":         ("Watchung", "Somerset"),

    # ── Union County ──
    "clark":            ("Clark", "Union"),
    "cranford":         ("Cranford", "Union"),
    "elizabeth":        ("Elizabeth", "Union"),
    "fanwood":          ("Fanwood", "Union"),
    "linden":           ("Linden", "Union"),
    "northplainfield":  ("North Plainfield", "Union"),
    "plainfieldcity":   ("Plainfield", "Union"),
    "roselle":          ("Roselle", "Union"),
    "scotchplains":     ("Scotch Plains", "Union"),
    "springfield":      ("Springfield", "Union"),
    "westfield":        ("Westfield", "Union"),
}


# ── Data Model ────────────────────────────────────────────────────────────────

@dataclass
class TaxSaleRecord:
    """Parsed tax sale certificate record."""
    adv_number: str = ""
    tax_year: str = ""
    parcel_id: str = ""          # Block-Lot-Qualifier
    owner_name: str = ""
    charge_type: str = ""        # T=Tax, S=Sewer, W=Water
    face_amount: str = ""
    status: str = ""             # Active, Unavailable
    property_address: str = ""
    city: str = ""
    state: str = "NJ"
    zip_code: str = ""
    county: str = ""
    municipality: str = ""
    block: str = ""
    lot: str = ""
    qualifier: str = ""
    assessed_value: str = ""
    building_value: str = ""
    land_value: str = ""
    total_value: str = ""
    owner_address: str = ""
    use_code: str = ""
    year_built: str = ""
    subdomain: str = ""


# ── Site Status Detection ─────────────────────────────────────────────────────

async def check_site_status(page, subdomain: str) -> str:
    """
    Check if a municipality's tax sale site is live.
    Returns: 'live', 'offline', or 'error'
    """
    url = f"https://{subdomain}.newjerseytaxsale.com"
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        if not response or response.status >= 400:
            return "error"

        await page.wait_for_timeout(1500)
        content = await page.content()

        if "currently offline" in content.lower():
            return "offline"
        elif "preview items for sale" in content.lower() or "previewitems" in content.lower():
            return "live"
        elif "start here" in content.lower():
            # Home page without "offline" message = site is active
            return "live"
        else:
            return "offline"

    except Exception as e:
        logger.debug(f"  {subdomain}: connection error — {e}")
        return "error"


# ── Table Scraper ─────────────────────────────────────────────────────────────

async def scrape_preview_table(page, subdomain: str) -> list[dict]:
    """
    Scrape the Preview Items for Sale table across all pages.
    Returns raw table rows as dicts.
    """
    preview_url = f"https://{subdomain}.newjerseytaxsale.com/index.cfm?folder=previewitems"
    await page.goto(preview_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)

    all_rows = []
    page_num = 1

    while True:
        # Parse current page's table
        rows = await page.evaluate("""
            () => {
                const table = document.getElementById('previewResults') ||
                              document.querySelector('table.center') ||
                              document.querySelector('table');
                if (!table) return [];

                const rows = [];
                const trs = table.querySelectorAll('tr');
                for (let i = 1; i < trs.length; i++) {
                    const tds = trs[i].querySelectorAll('td');
                    if (tds.length >= 7) {
                        rows.push({
                            adv_number: tds[0].textContent.trim(),
                            tax_year: tds[1].textContent.trim(),
                            parcel_id: tds[2].textContent.trim(),
                            owner_name: tds[3].textContent.trim(),
                            charge_type: tds[4].textContent.trim(),
                            face_amount: tds[5].textContent.trim(),
                            status: tds[6].textContent.trim(),
                            has_link: !!tds[0].querySelector('a') || !!tds[2].querySelector('a'),
                        });
                    }
                }
                return rows;
            }
        """)

        all_rows.extend(rows)
        logger.info(f"  Page {page_num}: {len(rows)} rows (total: {len(all_rows)})")

        # Check for Next page
        has_next = await page.evaluate("""
            () => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    if (a.textContent.includes('Next')) return true;
                }
                return false;
            }
        """)

        if has_next:
            page_num += 1
            await page.evaluate(f"goToPage({page_num})")
            await page.wait_for_timeout(1500)
        else:
            break

    return all_rows


# ── Detail Popup Scraper ──────────────────────────────────────────────────────

async def scrape_record_details(page, adv_number: str) -> dict:
    """
    Click an ADV number to open the detail popup and extract
    property address, valuation, and owner info.
    """
    details = {}

    try:
        # Click the ADV number link to open detail popup
        # The links use javascript:void(0) with onclick handlers
        clicked = await page.evaluate(f"""
            () => {{
                const links = document.querySelectorAll('a');
                for (const a of links) {{
                    if (a.textContent.trim() === '{adv_number}') {{
                        a.click();
                        return true;
                    }}
                }}
                return false;
            }}
        """)

        if not clicked:
            return details

        # Wait for popup/modal to load
        await page.wait_for_timeout(2000)

        # Extract detail fields from the popup
        details = await page.evaluate("""
            () => {
                const result = {};
                const body = document.body.innerHTML;

                // Property Address
                const addrMatch = body.match(/Property\\s+Address[\\s\\S]*?<td[^>]*>(.*?)<\\/td>/i);
                if (addrMatch) {
                    result.property_address = addrMatch[1].replace(/<br\\s*\\/?>/gi, ', ').replace(/<[^>]+>/g, '').trim();
                }

                // Look for all label-value pairs in the detail view
                const cells = document.querySelectorAll('td');
                for (let i = 0; i < cells.length - 1; i++) {
                    const label = cells[i].textContent.trim();
                    const value = cells[i + 1].textContent.trim();

                    if (label === 'Property Address') {
                        result.property_address = value.replace(/\\s+/g, ' ').trim();
                    } else if (label === 'Owner Name') {
                        result.owner_name_detail = value;
                    } else if (label === 'Owner Address') {
                        result.owner_address = value.replace(/\\s+/g, ' ').trim();
                    } else if (label === 'Assessed Value') {
                        result.assessed_value = value;
                    } else if (label === 'Building Value') {
                        result.building_value = value;
                    } else if (label === 'Land Value') {
                        result.land_value = value;
                    } else if (label === 'Total Value') {
                        result.total_value = value;
                    } else if (label === 'Use Code') {
                        result.use_code = value;
                    } else if (label === 'Year Built') {
                        result.year_built = value;
                    } else if (label === 'Face Value') {
                        result.face_value_detail = value;
                    }
                }

                return result;
            }
        """)

        # Close the popup
        close_btn = await page.query_selector('img[src*="close"], .ui-dialog-titlebar-close, [title="Close"]')
        if close_btn:
            await close_btn.click()
        else:
            # Try pressing Escape or clicking the X
            await page.evaluate("""
                () => {
                    const closes = document.querySelectorAll('a, button, img');
                    for (const el of closes) {
                        if (el.alt === 'X' || el.textContent === 'X' ||
                            el.src && el.src.includes('close')) {
                            el.click();
                            return;
                        }
                    }
                }
            """)
        await page.wait_for_timeout(500)

    except Exception as e:
        logger.debug(f"  Detail scrape failed for ADV {adv_number}: {e}")

    return details


# ── Parcel ID Parser ──────────────────────────────────────────────────────────

def parse_parcel_id(parcel_id: str) -> tuple[str, str, str]:
    """
    Parse RealAuction parcel ID format: "7 - 4 - -C007B"
    Returns: (block, lot, qualifier)
    """
    parts = re.split(r'\s*-\s*', parcel_id.strip())
    block = parts[0].strip() if len(parts) > 0 else ""
    lot = parts[1].strip() if len(parts) > 1 else ""
    qualifier = parts[2].strip() if len(parts) > 2 else ""
    # Clean up qualifier (remove leading dash or empty)
    qualifier = qualifier.lstrip("-").strip()
    return block, lot, qualifier


# ── Main Scraper ──────────────────────────────────────────────────────────────

async def scrape_nj_tax_sales(
    counties: list[str] = None,
    fetch_details: bool = True,
    max_detail_records: int = 0,
    headless: bool = True,
) -> list[dict]:
    """
    Monitor all municipality tax sale sites in target counties.

    Args:
        counties: Filter to specific counties (default: all 4)
        fetch_details: Click into each record for property address (slower)
        max_detail_records: Max detail lookups per municipality (0 = all)
        headless: Run browser headless

    Returns:
        List of TaxSaleRecord dicts
    """
    if counties is None:
        counties = ["Middlesex", "Essex", "Somerset", "Union"]

    # Filter municipalities by target counties
    target_munis = {
        sub: info for sub, info in MUNICIPALITIES.items()
        if info[1] in counties
    }

    logger.info(
        f"Tax Sale Monitor: checking {len(target_munis)} municipalities "
        f"across {', '.join(counties)}"
    )

    all_records = []
    live_sites = []
    offline_sites = []
    error_sites = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
            ] if headless else []
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )

        # Stealth: remove webdriver flag
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        page = await context.new_page()

        # ── Phase 1: Check all sites for live status ──
        logger.info("Phase 1: Checking site status...")
        for subdomain, (muni_name, county) in target_munis.items():
            status = await check_site_status(page, subdomain)

            if status == "live":
                live_sites.append((subdomain, muni_name, county))
                logger.info(f"  ✓ {muni_name} ({county}) — LIVE")
            elif status == "offline":
                offline_sites.append(subdomain)
                logger.debug(f"  · {muni_name} ({county}) — offline")
            else:
                error_sites.append(subdomain)
                logger.debug(f"  ✗ {muni_name} ({county}) — error")

        logger.info(
            f"Phase 1 complete: {len(live_sites)} live, "
            f"{len(offline_sites)} offline, {len(error_sites)} errors"
        )

        if not live_sites:
            logger.info("No live tax sale sites found. Quiet week.")
            await browser.close()
            return []

        # ── Phase 2: Scrape live sites ──
        logger.info("Phase 2: Scraping live sites...")

        for subdomain, muni_name, county in live_sites:
            try:
                logger.info(f"Scraping {muni_name} ({county})...")

                # Get the preview table
                rows = await scrape_preview_table(page, subdomain)
                logger.info(f"  {len(rows)} records in table")

                # Build records
                for i, row in enumerate(rows):
                    block, lot, qualifier = parse_parcel_id(row.get("parcel_id", ""))

                    record = TaxSaleRecord(
                        adv_number=row.get("adv_number", ""),
                        tax_year=row.get("tax_year", ""),
                        parcel_id=row.get("parcel_id", ""),
                        owner_name=row.get("owner_name", ""),
                        charge_type=row.get("charge_type", ""),
                        face_amount=row.get("face_amount", ""),
                        status=row.get("status", ""),
                        county=county,
                        municipality=muni_name,
                        state="NJ",
                        block=block,
                        lot=lot,
                        qualifier=qualifier,
                        subdomain=subdomain,
                    )

                    # Fetch detail page for property address
                    if fetch_details and row.get("has_link"):
                        if max_detail_records == 0 or i < max_detail_records:
                            details = await scrape_record_details(
                                page, row["adv_number"]
                            )
                            if details:
                                addr = details.get("property_address", "")
                                if addr:
                                    # Split "610 NEWARK ST, Hoboken, NJ" or
                                    # "610 NEWARK ST Hoboken, NJ"
                                    parts = addr.split(",")
                                    record.property_address = parts[0].strip()
                                    if len(parts) > 1:
                                        record.city = parts[1].strip()

                                record.owner_address = details.get("owner_address", "")
                                record.assessed_value = details.get("assessed_value", "")
                                record.building_value = details.get("building_value", "")
                                record.land_value = details.get("land_value", "")
                                record.total_value = details.get("total_value", "")
                                record.use_code = details.get("use_code", "")
                                record.year_built = details.get("year_built", "")

                    # If no city from detail, use municipality name
                    if not record.city:
                        record.city = muni_name

                    all_records.append(asdict(record))

                logger.info(
                    f"  {muni_name} complete: {len(rows)} records"
                )

                # Polite delay between municipalities
                await page.wait_for_timeout(2000)

            except Exception as e:
                logger.error(f"  {muni_name} failed: {e}")

        await browser.close()

    logger.info(
        f"Tax Sale Monitor complete: {len(all_records)} total records "
        f"from {len(live_sites)} live sites"
    )
    return all_records


# ── NoticeData Converter ──────────────────────────────────────────────────────

def to_notice_data(records: list[dict]) -> list[dict]:
    """Convert TaxSaleRecord dicts to NoticeData format for SiftStack pipeline."""
    notices = []
    for r in records:
        # Build raw_text for CRM notes.
        # ADV# + Subdomain are included up front so dedup_tracker can build
        # the cross-run dedup key {subdomain}{adv_number}{tax_year}.
        raw_parts = []
        if r.get("adv_number"):
            raw_parts.append(f"ADV: {r['adv_number']}")
        if r.get("subdomain"):
            raw_parts.append(f"Subdomain: {r['subdomain']}")
        if r.get("charge_type"):
            charge_labels = {"T": "Tax", "S": "Sewer", "W": "Water"}
            raw_parts.append(
                f"Charge: {charge_labels.get(r['charge_type'], r['charge_type'])}"
            )
        if r.get("face_amount"):
            raw_parts.append(f"Face Amount: {r['face_amount']}")
        if r.get("tax_year"):
            raw_parts.append(f"Tax Year: {r['tax_year']}")
        if r.get("parcel_id"):
            raw_parts.append(f"Parcel: {r['parcel_id']}")
        if r.get("assessed_value") and r["assessed_value"] != "$0.00":
            raw_parts.append(f"Assessed: {r['assessed_value']}")
        if r.get("status"):
            raw_parts.append(f"Status: {r['status']}")
        if r.get("municipality"):
            raw_parts.append(f"Municipality: {r['municipality']}")

        notice = {
            "name": r.get("owner_name", ""),
            "address": r.get("property_address", ""),
            "city": r.get("city", ""),
            "state": r.get("state", "NJ"),
            "zip": r.get("zip_code", ""),
            "county": r.get("county", ""),
            "notice_type": "tax_sale",
            "source": f"RealAuction - {r.get('municipality', '')}",
            "filing_date": "",
            "raw_text": " | ".join(raw_parts),
            "block": r.get("block", ""),
            "lot": r.get("lot", ""),
        }
        notices.append(notice)

    return notices


# ── NoticeData Wrapper (for modal_app.py nj_weekly_all) ───────────────────────

async def scrape_nj_tax_sale_notices(
    counties: list[str] = None,
    fetch_details: bool = True,
    max_detail_records: int = 0,
    headless: bool = True,
) -> list:
    """Scrape all live tax sale sites and return NoticeData objects.

    Same signature/pattern as scrape_nj_lp_notices, scrape_middlesex_probates,
    and scrape_somerset_notices so modal_app.py can fan them out uniformly.
    """
    from notice_parser import NoticeData  # local import — NoticeData lives in src/

    records = await scrape_nj_tax_sales(
        counties=counties,
        fetch_details=fetch_details,
        max_detail_records=max_detail_records,
        headless=headless,
    )
    if not records:
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    flat = to_notice_data(records)
    notices = []
    for f in flat:
        notices.append(NoticeData(
            date_added=today,
            address=f.get("address", ""),
            city=f.get("city", ""),
            state=f.get("state", "NJ"),
            zip=f.get("zip", ""),
            owner_name=f.get("name", ""),
            notice_type="tax_sale",
            county=f.get("county", ""),
            source_url=f.get("source", ""),
            raw_text=f.get("raw_text", ""),
        ))
    return notices


# ── CLI Entry Point ───────────────────────────────────────────────────────────

async def main():
    """Run the tax sale monitor standalone."""
    import json
    import csv
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    logger.info("Starting NJ Tax Sale Monitor...")
    records = await scrape_nj_tax_sales(
        counties=["Middlesex", "Essex", "Somerset", "Union"],
        fetch_details=True,
        headless=True,
    )

    if not records:
        logger.info("No live tax sale sites found. Nothing to do.")
        return

    notices = to_notice_data(records)

    # Save CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = Path(f"nj_tax_sales_{timestamp}.csv")

    if notices:
        keys = notices[0].keys()
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(notices)
        logger.info(f"Saved {len(notices)} records to {csv_path}")

    # Summary
    print(f"\n{'='*60}")
    print(f"NJ Tax Sale Monitor Summary")
    print(f"{'='*60}")
    print(f"Total records:     {len(records)}")

    # By county
    by_county = {}
    for r in records:
        c = r.get("county", "Unknown")
        by_county[c] = by_county.get(c, 0) + 1
    print(f"\nBy county:")
    for county, count in sorted(by_county.items(), key=lambda x: -x[1]):
        print(f"  {county}: {count}")

    # By municipality
    by_muni = {}
    for r in records:
        m = r.get("municipality", "Unknown")
        by_muni[m] = by_muni.get(m, 0) + 1
    print(f"\nBy municipality:")
    for muni, count in sorted(by_muni.items(), key=lambda x: -x[1]):
        print(f"  {muni}: {count}")

    # By charge type
    by_charge = {}
    for r in records:
        ct = r.get("charge_type", "?")
        charge_labels = {"T": "Tax", "S": "Sewer", "W": "Water"}
        by_charge[charge_labels.get(ct, ct)] = by_charge.get(
            charge_labels.get(ct, ct), 0
        ) + 1
    print(f"\nBy charge type:")
    for ct, count in sorted(by_charge.items(), key=lambda x: -x[1]):
        print(f"  {ct}: {count}")

    with_addr = sum(1 for r in records if r.get("property_address"))
    print(f"\nWith property address: {with_addr}/{len(records)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
