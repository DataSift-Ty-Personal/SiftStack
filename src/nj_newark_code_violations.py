"""Newark, NJ code enforcement importer via CKAN Open Data.

STATUS: PARKED (2026-04-17). The Newark Open Data portal at
data.ci.newark.nj.us returns Cloudflare-edge HTTP 503 for every request
(server: cloudflare, cf-cache-status: DYNAMIC, no cf-mitigated: challenge
header). That combination indicates the origin server is down, not a
Cloudflare bot block. Verified via curl, Playwright (bundled Chromium,
real Chrome + stealth patches), and WebFetch over a 20+ minute window.

The module below is complete and ready to run when the portal comes
back online. To reactivate: confirm the portal loads in a browser, then
wire the nj-newark-cv mode into main.py and run the smoke test.

Source: https://data.ci.newark.nj.us (Cloudflare-protected CKAN v2)

Primary resource: Open Complaints (UUID a79bb702-...)
  Fields: ActionTaken, Block, ComplaintID, DateCreated, DateUpdated,
          Department, Findings, Latitude, Location, Longitude, Lot,
          Request, ServiceID, Status, Url
  No owner name — enrichment must pull owner from NJ MOD-IV by Block/Lot.

Strategy: navigate to the CKAN datastore_search URL directly. Chrome
renders JSON as a <pre>. Grab innerText, parse, transform to NoticeData.
Paginate via offset/limit, sort DateCreated desc, stop when we cross the
last-seen timestamp in state.
"""

import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import urlencode

from playwright.async_api import BrowserContext, Page, async_playwright

from config import (
    NEWARK_CE_COMPLAINTS_ID,
    NEWARK_CE_COOKIES_FILE,
    NEWARK_CE_STATE_FILE,
    NEWARK_CKAN_BASE,
    load_state,
    save_state,
)
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000  # CKAN default max for datastore_search
MAX_HISTORICAL_RECORDS = 20000  # safety cap for historical pulls
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _build_search_url(resource_id: str, limit: int, offset: int) -> str:
    """Build a CKAN datastore_search URL sorted by DateCreated desc."""
    params = {
        "resource_id": resource_id,
        "limit": limit,
        "offset": offset,
        "sort": "DateCreated desc",
    }
    return f"{NEWARK_CKAN_BASE}/api/3/action/datastore_search?{urlencode(params)}"


async def _load_cookies(context: BrowserContext) -> bool:
    if not NEWARK_CE_COOKIES_FILE.exists():
        return False
    try:
        cookies = json.loads(NEWARK_CE_COOKIES_FILE.read_text())
        await context.add_cookies(cookies)
        logger.debug("Loaded %d Newark cookies", len(cookies))
        return True
    except Exception:
        return False


async def _save_cookies(context: BrowserContext) -> None:
    try:
        cookies = await context.cookies()
        NEWARK_CE_COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
        logger.debug("Saved %d Newark cookies", len(cookies))
    except Exception as e:
        logger.debug("Cookie save failed: %s", e)


async def _wait_for_cloudflare(page: Page, timeout_ms: int = 45000) -> bool:
    """Wait until the page body contains parseable JSON (Cloudflare cleared)."""
    import time

    deadline = time.monotonic() + (timeout_ms / 1000)
    last_state = ""
    while time.monotonic() < deadline:
        try:
            body = await page.inner_text("body", timeout=2000)
        except Exception:
            body = ""
        stripped = body.strip()
        if stripped.startswith("{") and '"success"' in stripped[:200]:
            return True
        if "Just a moment" in body or "Checking your browser" in body or "Verifying you are human" in body:
            state = "cf-challenge"
        elif "503" in body and "Temporarily Unavailable" in body:
            state = "cf-503"
        elif not body:
            state = "empty"
        else:
            state = f"other: {body[:80].replace(chr(10), ' ')!r}"
        if state != last_state:
            logger.debug("CF wait state: %s", state)
            last_state = state
        await page.wait_for_timeout(1500)
    logger.warning("CF challenge did not clear. Final body: %s", body[:200] if body else "<empty>")
    return False


async def _fetch_page(page: Page, resource_id: str, offset: int) -> dict:
    """Fetch a single page of records. Returns CKAN result dict."""
    url = _build_search_url(resource_id, PAGE_SIZE, offset)
    logger.debug("Fetching offset=%d: %s", offset, url)
    await page.goto(url, wait_until="domcontentloaded")
    if not await _wait_for_cloudflare(page):
        raise RuntimeError(f"Cloudflare challenge did not clear within timeout for offset={offset}")

    body = await page.inner_text("body")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        snippet = body[:300].replace("\n", " ")
        raise RuntimeError(f"Non-JSON response at offset={offset}: {snippet!r}") from e

    if not payload.get("success"):
        raise RuntimeError(f"CKAN reported failure: {payload.get('error')}")

    return payload.get("result", {})


def _parse_address(location: str) -> tuple[str, str, str, str]:
    """Crude parse of Newark Location field → (street, city, state, zip).

    Newark's Location is typically just the street address (e.g. "123 MAIN ST").
    City is always Newark, state always NJ. ZIP is not in the field.
    """
    street = (location or "").strip()
    return street, "Newark", "NJ", ""


def _record_to_notice(rec: dict) -> NoticeData | None:
    """Transform a CKAN Open Complaints record into NoticeData.

    Returns None if the record has no usable address (skip).
    """
    location = (rec.get("Location") or "").strip()
    if not location:
        return None

    street, city, state, zip_code = _parse_address(location)

    block = (rec.get("Block") or "").strip()
    lot = (rec.get("Lot") or "").strip()
    parcel_id = f"{block}-{lot}" if block and lot else ""

    date_created = (rec.get("DateCreated") or "").strip()
    date_added = date_created.split("T")[0] if date_created else ""

    notes_parts = []
    if rec.get("Department"):
        notes_parts.append(f"Dept: {rec['Department']}")
    if rec.get("Request"):
        notes_parts.append(f"Request: {rec['Request']}")
    if rec.get("Findings"):
        notes_parts.append(f"Findings: {rec['Findings']}")
    if rec.get("ActionTaken"):
        notes_parts.append(f"Action: {rec['ActionTaken']}")
    if rec.get("Status"):
        notes_parts.append(f"Status: {rec['Status']}")
    raw_text = " | ".join(notes_parts)

    complaint_id = rec.get("ComplaintID") or rec.get("_id") or ""
    source_url = (
        f"{NEWARK_CKAN_BASE}/dataset/code-enforcement/"
        f"resource/{NEWARK_CE_COMPLAINTS_ID}?q={complaint_id}"
        if complaint_id else ""
    )

    lat = rec.get("Latitude")
    lng = rec.get("Longitude")

    return NoticeData(
        date_added=date_added,
        address=street,
        city=city,
        state=state,
        zip=zip_code,
        notice_type="code_violation",
        county="Essex",
        source_url=source_url,
        raw_text=raw_text,
        parcel_id=parcel_id,
        latitude=str(lat) if lat is not None else "",
        longitude=str(lng) if lng is not None else "",
    )


async def fetch_complaints(
    since: str | None = None,
    max_records: int = 0,
    headless: bool = True,
) -> list[NoticeData]:
    """Fetch Open Complaints from Newark CKAN.

    Args:
        since: ISO date (YYYY-MM-DD). Records with DateCreated < since are skipped.
               If None, uses last-seen timestamp from state file.
        max_records: Cap total records (0 = no cap, subject to safety limit).
        headless: Run Playwright headless (False for debugging).

    Returns:
        List of NoticeData. Caller is responsible for writing CSV.
    """
    state = load_state(NEWARK_CE_STATE_FILE)
    cutoff = since or state.get("last_seen", "")
    logger.info("Newark CE fetch: cutoff=%s (state last_seen=%s)", cutoff, state.get("last_seen", "never"))

    cap = max_records if max_records > 0 else MAX_HISTORICAL_RECORDS
    notices: list[NoticeData] = []
    newest_seen: str = cutoff

    async with async_playwright() as pw:
        launch_kwargs = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        }
        try:
            browser = await pw.chromium.launch(channel="chrome", **launch_kwargs)
            logger.debug("Launched real Chrome")
        except Exception as e:
            logger.debug("Real Chrome not available (%s) — falling back to bundled Chromium", e)
            browser = await pw.chromium.launch(**launch_kwargs)

        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        # Patch common webdriver detection points before any page load.
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
            """
        )
        await _load_cookies(context)
        page = await context.new_page()

        # Warm up the Cloudflare challenge by hitting the homepage first.
        logger.debug("Warming Cloudflare session via homepage...")
        await page.goto(NEWARK_CKAN_BASE, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        offset = 0
        total_returned = 0
        while True:
            result = await _fetch_page(page, NEWARK_CE_COMPLAINTS_ID, offset)
            records = result.get("records", [])
            total = result.get("total", 0)
            if offset == 0:
                logger.info("Newark CE dataset total=%d records", total)

            if not records:
                break

            cross_cutoff = False
            for rec in records:
                date_created = (rec.get("DateCreated") or "").strip()
                if cutoff and date_created and date_created < cutoff:
                    cross_cutoff = True
                    break

                if not newest_seen or (date_created and date_created > newest_seen):
                    newest_seen = date_created

                notice = _record_to_notice(rec)
                if notice:
                    notices.append(notice)

                total_returned += 1
                if total_returned >= cap:
                    cross_cutoff = True
                    break

            if cross_cutoff:
                break
            if len(records) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        await _save_cookies(context)
        await browser.close()

    logger.info("Newark CE fetched %d notices (newest_seen=%s)", len(notices), newest_seen)

    if notices and newest_seen:
        save_state(NEWARK_CE_STATE_FILE, {"last_seen": newest_seen, "last_run": datetime.now().isoformat()})

    return notices


async def run_newark_code_violations(
    mode: str = "daily",
    since: str | None = None,
    max_records: int = 0,
    headless: bool = True,
) -> list[NoticeData]:
    """Entry point invoked from main.py.

    Args:
        mode: "daily" (use state cutoff) or "historical" (no cutoff).
        since: Override cutoff with an explicit date.
        max_records: Cap records returned.
        headless: Run browser headless.
    """
    if mode == "historical" and since is None:
        since = "2020-01-01"
    elif mode == "daily" and since is None:
        since = None

    return await fetch_complaints(since=since, max_records=max_records, headless=headless)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    headed = "--headed" in sys.argv
    historical = "--historical" in sys.argv

    mode = "historical" if historical else "daily"
    notices = asyncio.run(run_newark_code_violations(mode=mode, headless=not headed, max_records=10))

    print(f"\nFetched {len(notices)} notices")
    for n in notices[:5]:
        print(f"  {n.date_added} | {n.address}, {n.city} | parcel={n.parcel_id} | {n.raw_text[:80]}")
