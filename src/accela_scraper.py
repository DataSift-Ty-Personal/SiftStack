"""Scrape City of Omaha code-enforcement records from Accela Citizen Access.

Omaha's code enforcement (the only authoritative, live source — the DOGIS open
dataset was removed) lives in Accela ACA, an ASP.NET WebForms app. There is no
JSON API, but the Enforcement search's **"Download results"** button exports a
clean CSV (Date, Record Number, Record Type, Address, Status) for a date-range
search — so we drive the form with Playwright, trigger the export, and parse the
CSV rather than scraping/paginating the results grid.

Anonymous search works (no login, no CAPTCHA). Owner is NOT in the export; the
address is the lead, and owner can be resolved downstream from the Douglas parcel
layer (ne_property_lookup) by address.

Verified live 2026-07-02 against https://aca-prod.accela.com/OMAHA (module=Enforcement).
"""

import asyncio
import csv
import io
import logging
import random
import re
from datetime import datetime

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

ACA_ENFORCEMENT_URL = "https://aca-prod.accela.com/OMAHA/Cap/CapHome.aspx?module=Enforcement"

# Element ids (verified on the live page).
_SEL_RECORD_TYPE = "#ctl00_PlaceHolderMain_generalSearchForm_ddlGSPermitType"
_SEL_START_DATE = "#ctl00_PlaceHolderMain_generalSearchForm_txtGSStartDate"
_SEL_END_DATE = "#ctl00_PlaceHolderMain_generalSearchForm_txtGSEndDate"
_SEL_SEARCH_BTN = "#ctl00_PlaceHolderMain_btnNewSearch"
_SEL_EXPORT_LINK = "a[id*='btnExport']"

# Enforcement record types (dropdown labels). Default excludes bare "Property
# Owner Registration" (not a distress signal); the other three are actionable.
DEFAULT_RECORD_TYPES = [
    "Citation Record",
    "Housing Inspection Case",
    "Abandoned and Vacant Property Registration",
]
ALL_RECORD_TYPES = DEFAULT_RECORD_TYPES + ["Property Owner Registration"]


def _parse_accela_address(raw: str) -> tuple[str, str, str, str]:
    """Parse '2941 N 59 ST, OMAHA NE 68104, 2941' → (street, city, state, zip).

    The trailing ', <house#>' duplicate segment Accela appends is ignored.
    """
    segs = [s.strip() for s in (raw or "").split(",") if s.strip()]
    street = segs[0] if segs else ""
    city = state = zipc = ""
    for s in segs[1:]:
        m = re.match(r"^(.*?)\s+([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?$", s)
        if m:
            city, state, zipc = m.group(1).strip().title(), m.group(2).upper(), m.group(3)
            break
    return (street, city, state, zipc)


def _parse_date(raw: str) -> str:
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _rows_to_notices(csv_text: str, record_type: str, start_iso: str, end_iso: str) -> list[NoticeData]:
    """Parse an exported RecordList CSV into NoticeData, filtered to [start,end]."""
    notices: list[NoticeData] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        # Header names as exported: Date, Record Number, Record Type, Address, Status
        date_iso = _parse_date(row.get("Date", ""))
        if start_iso and date_iso and date_iso < start_iso:
            continue
        if end_iso and date_iso and date_iso > end_iso:
            continue
        record_no = (row.get("Record Number") or "").strip()
        rtype = (row.get("Record Type") or record_type or "").strip()
        status = (row.get("Status") or "").strip()
        street, city, state, zipc = _parse_accela_address(row.get("Address", ""))
        if not street:
            continue
        notices.append(
            NoticeData(
                address=street,
                city=city or "Omaha",
                state=state or "NE",
                zip=zipc,
                owner_name="",  # not in export — resolved downstream from address
                notice_type="code_violation",
                county="Douglas",
                date_added=date_iso,
                source_url=f"accela://OMAHA/{record_no}" if record_no else ACA_ENFORCEMENT_URL,
                raw_text=f"record_type={rtype} | status={status} | record={record_no}",
            )
        )
    logger.info("  %s: %d records in range", record_type, len(notices))
    return notices


async def _search_and_export(page, record_type: str, start_mdY: str, end_mdY: str) -> str | None:
    """Run one Enforcement search and return the exported CSV text (or None)."""
    await page.goto(ACA_ENFORCEMENT_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(random.uniform(2000, 3000))

    try:
        await page.select_option(_SEL_RECORD_TYPE, label=record_type)
    except Exception as e:
        logger.warning("  Could not select record type %r: %s", record_type, e)
        return None

    await page.fill(_SEL_START_DATE, start_mdY)
    await page.fill(_SEL_END_DATE, end_mdY)
    await page.keyboard.press("Escape")  # dismiss the calendar popup

    await page.click(_SEL_SEARCH_BTN)
    await page.wait_for_timeout(random.uniform(5000, 7000))

    # No results → no export link.
    export = page.locator(_SEL_EXPORT_LINK).first
    if not await export.count():
        body = await page.evaluate("() => document.body.innerText.slice(0, 300)")
        if re.search(r"no records|not find any", body, re.IGNORECASE):
            logger.info("  %s: no records", record_type)
        else:
            logger.warning("  %s: export link not found (layout change?)", record_type)
        return None

    try:
        async with page.expect_download(timeout=30000) as dl_info:
            await export.click()
        download = await dl_info.value
        path = await download.path()
        return open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        logger.warning("  %s: export/download failed: %s", record_type, e)
        return None


async def fetch_code_violations(
    start_date: str,
    end_date: str,
    record_types: list[str] | None = None,
    headless: bool = True,
) -> list[NoticeData]:
    """Fetch Omaha code-enforcement records for a date range (YYYY-MM-DD).

    Drives Accela ACA once per record type, exports each result set to CSV, and
    returns parsed NoticeData (notice_type='code_violation', county='Douglas').
    """
    from playwright.async_api import async_playwright

    record_types = record_types or DEFAULT_RECORD_TYPES
    start_iso, end_iso = start_date, end_date
    # Accela date inputs want MM/DD/YYYY.
    start_mdY = datetime.strptime(start_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    end_mdY = datetime.strptime(end_date, "%Y-%m-%d").strftime("%m/%d/%Y")

    notices: list[NoticeData] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page(accept_downloads=True)
        page.set_default_timeout(60000)
        try:
            for rtype in record_types:
                logger.info("Accela search: %s (%s → %s)", rtype, start_date, end_date)
                csv_text = await _search_and_export(page, rtype, start_mdY, end_mdY)
                if csv_text:
                    notices.extend(_rows_to_notices(csv_text, rtype, start_iso, end_iso))
                await page.wait_for_timeout(random.uniform(1500, 2500))
        finally:
            await browser.close()

    logger.info("Accela: %d total code-violation records", len(notices))
    return notices
