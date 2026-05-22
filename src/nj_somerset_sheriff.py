"""
Somerset County Sheriff Sales Scraper
Scrapes https://www.somersetcountynj.gov sheriff sales page,
downloads PDF advertisements, and parses structured data.

Output: List of NoticeData-compatible dicts for the SiftStack enrichment pipeline.
"""

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

# Shared cutoff with the CivilView scraper — defined in nj_sheriff_sales
# so both sheriff scrapers honor one SHERIFF_SALE_MAX_AGE_DAYS env var.
from nj_sheriff_sales import SHERIFF_SALE_MAX_AGE_DAYS

# Last filter pass's stale-drop count — read by modal_app.py for Slack.
LAST_STALE_DROPPED: int = 0

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://www.somersetcountynj.gov"
SHERIFF_SALES_URL = f"{BASE_URL}/government/elected-officials/sheriff-s-office/divisions/sheriff-sales"

# Statuses that indicate the sale is NOT active/upcoming
INACTIVE_STATUSES = {
    "sold", "canceled", "cancelled", "settled", "reinstated",
    "redeemed", "settled without sale", "settled with sale",
}


# ── Data Model ────────────────────────────────────────────────────────────────

@dataclass
class SomersetSheriffSale:
    """Parsed sheriff sale record."""
    sale_number: str = ""
    docket_number: str = ""
    plaintiff: str = ""
    defendants: str = ""
    sale_date: str = ""
    property_address: str = ""
    city: str = ""
    state: str = "NJ"
    zip_code: str = ""
    county: str = "Somerset"
    block: str = ""
    lot: str = ""
    judgment_amount: str = ""
    upset_bid: str = ""
    property_description: str = ""
    nearest_cross_street: str = ""
    owner_occupied: bool = False
    pdf_url: str = ""
    status: str = ""
    raw_text: str = ""


# ── Page Parser ───────────────────────────────────────────────────────────────

def parse_sales_list(html: str) -> list[dict]:
    """Parse the main sheriff sales page to extract sale links and metadata."""
    soup = BeautifulSoup(html, "html.parser")
    sales = []

    for link in soup.find_all("a"):
        text = link.get_text(strip=True)
        if not text.startswith("Sale#"):
            continue

        href = link.get("href", "")
        if not href:
            continue

        # Make absolute URL
        if href.startswith("/"):
            href = BASE_URL + href

        # Extract sale number
        sale_num_match = re.search(r"Sale#\s*(\d+)", text)
        sale_number = sale_num_match.group(1) if sale_num_match else ""

        # Get the surrounding text for date and status
        # The structure is: <a>Sale# XXXXX</a> date status
        parent = link.parent
        if parent:
            full_text = parent.get_text(" ", strip=True)
        else:
            full_text = text

        # Extract date (MM/DD/YY format)
        date_match = re.search(r"(\d{2}/\d{2}/\d{2})", full_text)
        date_str = date_match.group(1) if date_match else ""

        # Extract status (everything after the date)
        status = ""
        if date_match:
            after_date = full_text[date_match.end():].strip()
            status = after_date
        else:
            # No date found, try to get text after sale number
            after_sale = full_text[full_text.find(text) + len(text):].strip()
            status = after_sale

        sales.append({
            "sale_number": sale_number,
            "date": date_str,
            "status": status,
            "pdf_url": href,
        })

    logger.info(f"Found {len(sales)} total sales on page")
    return sales


def filter_active_sales(sales: list[dict]) -> list[dict]:
    """Filter for upcoming/active sales only.

    Drops sales older than SHERIFF_SALE_MAX_AGE_DAYS (shared with the
    CivilView scraper) so the same env var controls both sheriff sources.
    Records the stale drop count to LAST_STALE_DROPPED for Slack summary.
    """
    global LAST_STALE_DROPPED
    active = []
    stale_dropped = 0
    today = datetime.now()
    cutoff = today - timedelta(days=SHERIFF_SALE_MAX_AGE_DAYS)

    for sale in sales:
        status_lower = sale["status"].lower().strip()

        # Skip inactive statuses
        if any(inactive in status_lower for inactive in INACTIVE_STATUSES):
            continue

        # Skip sales with no date (00/00/00 = bankruptcy hold)
        if sale["date"] == "00/00/00" or not sale["date"]:
            # Bankruptcy holds have address in status — still useful for prospecting
            if "[bankruptcy]" in status_lower:
                # Include bankruptcy holds — they have property addresses
                active.append(sale)
            continue

        # Parse date and check if upcoming
        try:
            sale_date = datetime.strptime(sale["date"], "%m/%d/%y")
            if sale_date >= cutoff:
                active.append(sale)
            else:
                stale_dropped += 1
        except ValueError:
            # Can't parse date — include it to be safe
            active.append(sale)

    LAST_STALE_DROPPED = stale_dropped
    if stale_dropped:
        logger.info(
            "Filtered %d stale Somerset sheriff sales (older than %d days, before %s)",
            stale_dropped, SHERIFF_SALE_MAX_AGE_DAYS, cutoff.date().isoformat(),
        )
    logger.info(f"Filtered to {len(active)} active/upcoming sales")
    return active


# ── PDF Parser ────────────────────────────────────────────────────────────────

def parse_sheriff_sale_pdf(pdf_bytes: bytes, sale_meta: dict) -> SomersetSheriffSale:
    """Parse a sheriff sale PDF advertisement into structured data."""
    import pdfplumber

    record = SomersetSheriffSale()
    record.sale_number = sale_meta.get("sale_number", "")
    record.sale_date = sale_meta.get("date", "")
    record.status = sale_meta.get("status", "")
    record.pdf_url = sale_meta.get("pdf_url", "")

    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        pdf.close()
        text = "\n".join(text_parts)
    except Exception as e:
        logger.error(f"Failed to parse PDF for Sale# {record.sale_number}: {e}")
        return record

    record.raw_text = text

    # ── Docket Number ──
    docket_match = re.search(r"DOCKET\s*(?:NO\.?|NUMBER)\s*:?\s*(F[-‐–]\d{6}[-‐–]\d{2})", text, re.IGNORECASE)
    if docket_match:
        record.docket_number = docket_match.group(1).strip()

    # ── Sale Number from PDF ──
    sale_match = re.search(r"SHERIFF['']?S\s+SALE\s*(?:NO\.?|NUMBER)\s*:?\s*(\d+)", text, re.IGNORECASE)
    if sale_match:
        record.sale_number = sale_match.group(1).strip()

    # ── Plaintiff ──
    plaintiff_match = re.search(
        r"Between\s*\n?(.*?)\s*\n?\s*PLAINTIFF",
        text, re.IGNORECASE | re.DOTALL
    )
    if plaintiff_match:
        record.plaintiff = re.sub(r"\s+", " ", plaintiff_match.group(1)).strip()

    # ── Defendants ──
    defendant_match = re.search(
        r"vs\.?\s*\n?(.*?)\s*\n?\s*DEFENDANT",
        text, re.IGNORECASE | re.DOTALL
    )
    if defendant_match:
        record.defendants = re.sub(r"\s+", " ", defendant_match.group(1)).strip()

    # ── Property Address (PREMISES COMMONLY KNOWN AS) ──
    address_match = re.search(
        r"PREMISES\s+COMMONLY\s+KNOWN\s+AS:?\s*\n?(.*?)\n\s*(.*?),?\s*(NEW\s+JERSEY|NJ)",
        text, re.IGNORECASE | re.DOTALL
    )
    if address_match:
        street = address_match.group(1).strip()
        city = address_match.group(2).strip()
        # Clean up the street address
        street = re.sub(r"\s+", " ", street).strip()
        city = re.sub(r"\s+", " ", city).strip().rstrip(",")
        record.property_address = street
        record.city = city

    # ── Block and Lot ──
    # Pattern: "Tax Lot No. X in Block No. Y" or "Block X Lot Y"
    block_lot_match = re.search(
        r"(?:Tax\s+)?Lot\s+(?:No\.?\s*)?(\S+)\s+in\s+Block\s+(?:No\.?\s*)?(\S+)",
        text, re.IGNORECASE
    )
    if block_lot_match:
        record.lot = block_lot_match.group(1).strip().rstrip(";,")
        record.block = block_lot_match.group(2).strip().rstrip(";,")
    else:
        # Try "Block X, Lot Y" pattern
        bl_match = re.search(
            r"Block\s+(?:No\.?\s*)?(\S+),?\s+(?:and\s+)?Lot\s+(?:No\.?\s*)?(\S+)",
            text, re.IGNORECASE
        )
        if bl_match:
            record.block = bl_match.group(1).strip().rstrip(";,")
            record.lot = bl_match.group(2).strip().rstrip(";,")

    # ── Judgment Amount ──
    judgment_match = re.search(
        r"(?:Amount\s+of\s+Judgment|Judgment\s+Amount)\s+.*?\$([0-9,]+\.?\d*)",
        text, re.IGNORECASE
    )
    if judgment_match:
        record.judgment_amount = judgment_match.group(1).strip()

    # ── Upset Bid ──
    upset_match = re.search(
        r"(?:upset\s+bid|upset\s+price)\s+.*?\$([0-9,]+\.?\d*)",
        text, re.IGNORECASE
    )
    if upset_match:
        record.upset_bid = upset_match.group(1).strip()

    # ── Nearest Cross Street ──
    cross_match = re.search(
        r"Nearest\s+Cross\s+Street:?\s*(.*?)(?:\.|;|\n)",
        text, re.IGNORECASE
    )
    if cross_match:
        record.nearest_cross_street = cross_match.group(1).strip()

    # ── Owner Occupied ──
    if re.search(r"owner\s+occupied", text, re.IGNORECASE):
        record.owner_occupied = True

    # ── Property Description / Dimensions ──
    dim_match = re.search(
        r"Dimensions\s+of\s+property:?\s*(.*?)(?:;|\n|Nearest)",
        text, re.IGNORECASE | re.DOTALL
    )
    if dim_match:
        record.property_description = re.sub(r"\s+", " ", dim_match.group(1)).strip()

    # ── Municipality from body text ──
    if not record.city:
        muni_match = re.search(
            r"(?:Township|Borough|City)\s+of\s+([\w\s]+),\s+County\s+of\s+Somerset",
            text, re.IGNORECASE
        )
        if muni_match:
            record.city = muni_match.group(1).strip()

    logger.info(
        f"Parsed Sale# {record.sale_number}: "
        f"{record.property_address}, {record.city} | "
        f"Docket: {record.docket_number} | "
        f"Judgment: ${record.judgment_amount}"
    )
    return record


# ── Main Scraper ──────────────────────────────────────────────────────────────

async def scrape_somerset_sheriff_sales(
    include_bankruptcy: bool = True,
    max_records: int = 0,
    headless: bool = True,
) -> list[dict]:
    """
    Scrape Somerset County sheriff sales.

    Args:
        include_bankruptcy: Include bankruptcy-hold properties (default True)
        max_records: Max records to process (0 = all)
        headless: Run browser headless (default True)

    Returns:
        List of dicts compatible with NoticeData/SiftStack pipeline
    """
    import pdfplumber  # Ensure available

    records = []

    async with async_playwright() as p:
        # Somerset is behind Akamai bot protection — raw headless Chromium
        # gets 403 "Access Denied" with an errors.edgesuite.net reference.
        # Chrome's `--headless=new` mode shares the rendering pipeline with
        # headed mode, which Akamai accepts. We also warm up on the main
        # domain first to collect cookies before hitting the target page.
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        if headless:
            launch_args.insert(0, "--headless=new")
        browser = await p.chromium.launch(headless=False, args=launch_args)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        # ── Step 0: Warm up on main domain (Akamai cookie priming) ──
        try:
            await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning("Somerset warm-up failed: %s -- proceeding anyway", e)

        # ── Step 1: Load main sheriff sales page ──
        logger.info(f"Loading Somerset sheriff sales page: {SHERIFF_SALES_URL}")
        await page.goto(SHERIFF_SALES_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        html = await page.content()

        # ── Step 2: Parse and filter sales ──
        all_sales = parse_sales_list(html)
        active_sales = filter_active_sales(all_sales)

        if not include_bankruptcy:
            active_sales = [
                s for s in active_sales
                if "[bankruptcy]" not in s["status"].lower()
            ]

        if max_records > 0:
            active_sales = active_sales[:max_records]

        logger.info(f"Processing {len(active_sales)} active sales")

        # ── Step 3: Download and parse each PDF ──
        success_count = 0
        error_count = 0

        for i, sale in enumerate(active_sales):
            try:
                logger.info(
                    f"[{i+1}/{len(active_sales)}] Downloading PDF for "
                    f"Sale# {sale['sale_number']}..."
                )

                # Fetch through the page's JS fetch() — inherits Akamai cookies,
                # UA, and the fingerprint the main page already passed. The
                # lighter-weight context.request.get() client got 403'd.
                import base64
                b64 = await page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {credentials: 'include'});
                        if (!r.ok) return {ok: false, status: r.status};
                        const buf = await r.arrayBuffer();
                        // Chunk the conversion — large PDFs blow the call stack
                        // when spread across String.fromCharCode.
                        const bytes = new Uint8Array(buf);
                        let binary = '';
                        for (let i = 0; i < bytes.byteLength; i += 0x8000) {
                            binary += String.fromCharCode.apply(
                                null, bytes.subarray(i, i + 0x8000)
                            );
                        }
                        return {ok: true, status: r.status, b64: btoa(binary)};
                    }""",
                    sale["pdf_url"],
                )
                if not b64.get("ok"):
                    logger.warning(
                        f"Sale# {sale['sale_number']}: HTTP {b64.get('status')}"
                    )
                    error_count += 1
                    continue
                pdf_bytes = base64.b64decode(b64["b64"])

                # Verify it's actually a PDF
                if not pdf_bytes[:5] == b"%PDF-":
                    logger.warning(
                        f"Sale# {sale['sale_number']}: Not a PDF "
                        f"(got {pdf_bytes[:20]})"
                    )
                    error_count += 1
                    continue

                # Parse the PDF
                record = parse_sheriff_sale_pdf(pdf_bytes, sale)
                records.append(asdict(record))
                success_count += 1

                # Be polite — small delay between requests
                if i < len(active_sales) - 1:
                    await page.wait_for_timeout(500)

            except Exception as e:
                logger.error(f"Sale# {sale['sale_number']}: {e}")
                error_count += 1

        await browser.close()

    logger.info(
        f"Somerset Sheriff Sales complete: "
        f"{success_count} parsed, {error_count} errors, "
        f"{len(records)} total records"
    )
    return records


async def scrape_somerset_notices(
    include_bankruptcy: bool = True,
    max_records: int = 0,
    headless: bool = True,
) -> list:
    """Scrape-only entry point: returns a list of NoticeData instances
    (not dicts) matching the rest of the SiftStack pipeline.

    Used by the combined Wednesday cron (modal_app.nj_weekly_all). The
    older `scrape_somerset_sheriff_sales` returns list[dict]; this wrapper
    converts each dict into a NoticeData so all three scrapers can feed a
    single enrichment pass.
    """
    from datetime import datetime as _dt
    from notice_parser import NoticeData

    records = await scrape_somerset_sheriff_sales(
        include_bankruptcy=include_bankruptcy,
        max_records=max_records,
        headless=headless,
    )

    notices: list = []
    for r in records:
        auction_iso = ""
        raw_date = r.get("sale_date", "")
        if raw_date and raw_date != "00/00/00":
            try:
                auction_iso = _dt.strptime(raw_date, "%m/%d/%y").strftime("%Y-%m-%d")
            except ValueError:
                pass

        raw_bits: list[str] = []
        # Sale# first so dedup_tracker.extract_id can parse it reliably
        if r.get("sale_number"): raw_bits.append(f"Sale#: {r['sale_number']}")
        if r.get("docket_number"): raw_bits.append(f"Docket: {r['docket_number']}")
        if r.get("plaintiff"): raw_bits.append(f"Plaintiff: {r['plaintiff']}")
        if r.get("judgment_amount"): raw_bits.append(f"Judgment: ${r['judgment_amount']}")
        if r.get("upset_bid"): raw_bits.append(f"Upset: ${r['upset_bid']}")
        if r.get("block") and r.get("lot"):
            raw_bits.append(f"Block-Lot: {r['block']}-{r['lot']}")
        if r.get("nearest_cross_street"):
            raw_bits.append(f"Cross St: {r['nearest_cross_street']}")
        if r.get("owner_occupied"): raw_bits.append("Owner Occupied")
        if r.get("status"): raw_bits.append(f"Status: {r['status']}")

        notices.append(NoticeData(
            date_added=_dt.now().strftime("%Y-%m-%d"),
            auction_date=auction_iso,
            address=r.get("property_address", ""),
            city=r.get("city", ""),
            state=r.get("state", "NJ"),
            zip=r.get("zip_code", ""),
            owner_name=r.get("defendants", ""),
            notice_type="sheriff_sale",
            county="Somerset",
            source_url=r.get("pdf_url", ""),
            raw_text=" | ".join(raw_bits),
        ))
    return notices


def to_notice_data(records: list[dict]) -> list[dict]:
    """
    Convert SomersetSheriffSale records to NoticeData-compatible dicts
    for the SiftStack enrichment pipeline.
    """
    notices = []
    for r in records:
        # Build full address
        address_parts = [r.get("property_address", "")]
        full_address = ", ".join(p for p in address_parts if p)

        # Build raw_text for CRM notes
        raw_parts = []
        if r.get("docket_number"):
            raw_parts.append(f"Docket: {r['docket_number']}")
        if r.get("plaintiff"):
            raw_parts.append(f"Plaintiff: {r['plaintiff']}")
        if r.get("defendants"):
            raw_parts.append(f"Defendants: {r['defendants']}")
        if r.get("judgment_amount"):
            raw_parts.append(f"Judgment: ${r['judgment_amount']}")
        if r.get("upset_bid"):
            raw_parts.append(f"Upset Bid: ${r['upset_bid']}")
        if r.get("nearest_cross_street"):
            raw_parts.append(f"Cross St: {r['nearest_cross_street']}")
        if r.get("owner_occupied"):
            raw_parts.append("Owner Occupied: Yes")
        if r.get("status"):
            raw_parts.append(f"Status: {r['status']}")

        notice = {
            "name": r.get("defendants", ""),
            "address": full_address,
            "city": r.get("city", ""),
            "state": r.get("state", "NJ"),
            "zip": r.get("zip_code", ""),
            "county": "Somerset",
            "notice_type": "Sheriff Sale",
            "source": "Somerset County Sheriff",
            "filing_date": r.get("sale_date", ""),
            "raw_text": " | ".join(raw_parts),
            "block": r.get("block", ""),
            "lot": r.get("lot", ""),
        }
        notices.append(notice)

    return notices


# ── CLI Entry Point ───────────────────────────────────────────────────────────

async def main():
    """Run the scraper standalone."""
    import json
    import csv
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    logger.info("Starting Somerset County Sheriff Sales scraper...")
    records = await scrape_somerset_sheriff_sales(
        include_bankruptcy=True,
        headless=True,
    )

    if not records:
        logger.warning("No records found!")
        return

    # Convert to NoticeData format
    notices = to_notice_data(records)

    # Save CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = Path(f"somerset_sheriff_sales_{timestamp}.csv")

    if notices:
        keys = notices[0].keys()
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(notices)
        logger.info(f"Saved {len(notices)} records to {csv_path}")

    # Also save raw JSON for debugging
    json_path = Path(f"somerset_sheriff_sales_{timestamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    logger.info(f"Saved raw JSON to {json_path}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"Somerset County Sheriff Sales Summary")
    print(f"{'='*60}")
    print(f"Total records:     {len(records)}")
    print(f"With address:      {sum(1 for r in records if r.get('property_address'))}")
    print(f"With docket:       {sum(1 for r in records if r.get('docket_number'))}")
    print(f"With judgment:     {sum(1 for r in records if r.get('judgment_amount'))}")
    print(f"Owner occupied:    {sum(1 for r in records if r.get('owner_occupied'))}")
    print(f"Bankruptcy holds:  {sum(1 for r in records if '[bankruptcy]' in r.get('status', '').lower())}")
    cities = {}
    for r in records:
        c = r.get("city", "Unknown")
        cities[c] = cities.get(c, 0) + 1
    print(f"\nBy municipality:")
    for city, count in sorted(cities.items(), key=lambda x: -x[1]):
        print(f"  {city}: {count}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
