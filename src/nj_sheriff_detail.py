"""CivilView sheriff-sale detail-page enrichment.

The listing-page scraper (nj_sheriff_sales.scrape_county) captures the
basics — sheriff #, sale date, plaintiff, defendant, property address,
PropertyId. This module fetches each record's SaleDetails page and
extracts the supplementary fields the listing leaves out: docket #,
judgment amount, attorney + phone + file #, parcel id, status history,
disposition. Used by nj_sheriff_sales.scrape_civilview_notices().

Site quirks:
  - The page is server-rendered ASP-ish HTML with a clean
    `<div class="sale-detail-label">LABEL:</div>` immediately followed
    by `<div class="sale-detail-value">VALUE</div>` for each field.
  - The Status History is a `<table>` with [Status, Date] rows. Most
    recent status is the first data row.
  - Direct HTTP to /SaleDetails bounces to the directory page unless
    the request comes from a session that already visited /SalesSearch
    (AWS-ELB session cookie path). We use Playwright with a one-time
    /SalesSearch warmup to set the cookie, then reuse the page.
  - Completed / cancelled PropertyIds get retired from the site and
    redirect to the directory — those parse to empty result; we keep
    the record with whatever listing-page data we already had.

Auto-skip: records whose case_disposition resolves to Sold / Redeemed /
Cancelled are dropped from the returned list — these auctions are over
and not worth marketing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

DETAIL_DELAY_MIN = 1.5
DETAIL_DELAY_MAX = 2.0
PROPERTY_ID_RE = re.compile(r"PropertyId=(\d+)")

# Map detail-page labels (lowercased, trimmed of trailing ":") to
# NoticeData field names. Add new labels here as the site evolves.
_LABEL_TO_FIELD = {
    "court case #": "court_case_number",
    "approx. judgment*": "approx_judgment",
    "approx. judgment": "approx_judgment",  # rare variant without asterisk
    "minimum bid": "minimum_bid",
    "attorney": "plaintiff_attorney",
    "attorney phone": "plaintiff_attorney_phone",
    "parcel #": "parcel_number",
    "property note": "property_note",
}

# Case-disposition buckets — keyword in lowercased current_status →
# bucket. Order matters: more-specific keywords first.
_CASE_DISPOSITION_RULES = (
    ("scheduled", "Open"),
    ("purchased", "Sold"),
    ("sold", "Sold"),
    ("redeemed", "Redeemed"),
    ("bankruptcy", "Bankruptcy"),
    ("cancelled", "Cancelled"),
    ("canceled", "Cancelled"),
)

_DROP_DISPOSITIONS = frozenset({"Sold", "Redeemed", "Cancelled"})


def _parse_money(s: str) -> str:
    """Strip $ and commas; return numeric string (empty in → empty out)."""
    s = (s or "").strip().replace("$", "").replace(",", "")
    return s


def parse_detail_html(html: str) -> dict:
    """Parse a CivilView SaleDetails HTML page into a flat dict.

    Returns a dict keyed by NoticeData field names. Fields not found
    in the HTML are omitted (callers should treat absence as blank).
    Always returns `status_history_json` and `current_status` — both
    empty strings if no history table is present.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}

    for label_div in soup.select("div.sale-detail-label"):
        raw_label = label_div.get_text(strip=True).rstrip(":").strip().lower()
        value_div = label_div.find_next("div", class_="sale-detail-value")
        if value_div is None:
            continue
        value = value_div.get_text(" ", strip=True)
        field = _LABEL_TO_FIELD.get(raw_label)
        if not field:
            continue
        if field in ("approx_judgment", "minimum_bid"):
            value = _parse_money(value)
        out[field] = value

    # Status History — first <table>, header row [Status, Date, ...].
    history: list[dict] = []
    table = soup.find("table")
    if table is not None:
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            status = cells[0].get_text(" ", strip=True)
            date_str = cells[1].get_text(" ", strip=True)
            # Skip header + the [Collapse All] toggle row variants.
            if not status or not date_str:
                continue
            if status.lower() == "status" and date_str.lower() == "date":
                continue
            if status.startswith("[") and status.endswith("]"):
                continue
            history.append({"status": status, "date": date_str})

    out["status_history_json"] = json.dumps(history) if history else ""
    out["current_status"] = history[0]["status"] if history else ""
    return out


def derive_fields(parsed: dict, today: date) -> dict:
    """Compute adjournment_count, days_since_first_scheduled,
    case_disposition, is_open from a parsed detail dict."""
    out: dict = {}
    try:
        history = json.loads(parsed.get("status_history_json") or "[]")
    except json.JSONDecodeError:
        history = []

    out["adjournment_count"] = str(
        sum(1 for h in history if "adjourned" in h.get("status", "").lower())
    )

    parsed_dates = []
    for h in history:
        try:
            parsed_dates.append(datetime.strptime(h["date"], "%m/%d/%Y").date())
        except (KeyError, ValueError):
            continue
    if parsed_dates:
        earliest = min(parsed_dates)
        out["first_scheduled_date"] = earliest.isoformat()  # YYYY-MM-DD
        out["days_since_first_scheduled"] = str((today - earliest).days)
    else:
        out["first_scheduled_date"] = ""
        out["days_since_first_scheduled"] = ""

    cs = (parsed.get("current_status") or "").lower()
    disposition = ""
    for keyword, bucket in _CASE_DISPOSITION_RULES:
        if keyword in cs:
            disposition = bucket
            break
    out["case_disposition"] = disposition
    out["is_open"] = "yes" if disposition == "Open" else ""
    return out


async def enrich_sheriff_records(
    notices: list[NoticeData],
    *,
    headless: bool = True,
    today: date | None = None,
) -> list[NoticeData]:
    """Fetch & merge detail-page data for each CivilView sheriff record.

    Records with no CivilView PropertyId in their source_url (e.g.
    Somerset PDF-hosted sales) pass through unchanged with blank
    detail fields. Records whose case_disposition resolves to a drop
    bucket (Sold / Redeemed / Cancelled) are removed.

    Returns the surviving list (CivilView-enriched + non-CivilView
    passthroughs).
    """
    if not notices:
        return notices

    from playwright.async_api import async_playwright

    today = today or date.today()

    civilview: list[NoticeData] = []
    other: list[NoticeData] = []
    for n in notices:
        if PROPERTY_ID_RE.search(n.source_url or ""):
            civilview.append(n)
        else:
            other.append(n)

    logger.info(
        "Sheriff detail enrichment: %d CivilView records to enrich, "
        "%d non-CivilView passthroughs",
        len(civilview), len(other),
    )
    if not civilview:
        return notices

    kept: list[NoticeData] = []
    dropped = 0
    parse_failures = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"] if headless else [],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await ctx.new_page()

        # Session warmup: hit a county search so AWS-ELB sets the
        # session cookie before we navigate to SaleDetails directly.
        try:
            await page.goto(
                "https://salesweb.civilview.com/Sales/SalesSearch?countyId=73",
                wait_until="domcontentloaded", timeout=20000,
            )
            await page.wait_for_timeout(1500)
        except Exception as e:
            logger.warning("CivilView session warmup failed: %s", e)

        for i, n in enumerate(civilview, start=1):
            try:
                await page.goto(n.source_url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(600)
                html = await page.content()
                parsed = parse_detail_html(html)
                if not parsed.get("current_status") and not parsed.get("court_case_number"):
                    # PropertyId retired / page redirected to directory.
                    parse_failures += 1
                    kept.append(n)
                else:
                    for k, v in {**parsed, **derive_fields(parsed, today)}.items():
                        setattr(n, k, v)
                    if n.case_disposition in _DROP_DISPOSITIONS:
                        dropped += 1
                        continue
                    kept.append(n)
            except Exception as e:
                logger.warning("Detail fetch failed (%s): %s", n.source_url, e)
                kept.append(n)

            if i < len(civilview):
                await asyncio.sleep(random.uniform(DETAIL_DELAY_MIN, DETAIL_DELAY_MAX))
            if i % 25 == 0:
                logger.info("  [%d/%d] detail pages fetched", i, len(civilview))

        await browser.close()

    logger.info(
        "Sheriff detail enrichment complete: %d kept / %d dropped (resolved cases) "
        "/ %d parse-failures (retired PropertyIds)",
        len(kept), dropped, parse_failures,
    )
    return kept + other
