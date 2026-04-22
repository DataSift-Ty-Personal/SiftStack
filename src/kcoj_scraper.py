"""Scraper for Kentucky Court of Justice daily dockets.

Source: https://kcoj.kycourts.net/dockets

Discovers newly-heard probate cases in a given KY county + District/Circuit
division on a given date. Kentucky handles probate in District Court, with the
case-class code ``P`` (e.g. ``26-P-001544``). The daily docket lists every
hearing scheduled in that courthouse for that date — appointment of
administrator, motion hour, settlement review, etc. Catching a case at its
first hearing puts us ~1–3 weeks behind real-time filing, which is the best
public-data latency available short of a paid CourtNet 2.0 subscription.

Output: list[NoticeData] with decedent_name + case_number populated; address
and PR/executor fields are left blank for the downstream enrichment pipeline
(Jefferson PVA lookup → deed trail → CourtNet case detail → deep prospecting).

Filtering policy (matches user's Phase 1 research):
  * case class must be P
  * case number year prefix must be 25 or 26 (drops old re-hearings)
  * title must start with "ESTATE OF:" — excludes guardianship, name change,
    curatorship, and other non-decedent P-class filings

Polite cadence: single run per day, ~2s between pagination clicks. The KCOJ
robots.txt is a blanket disallow applied to all crawlers; records are public
record under KY Open Records Act, but we avoid hammering the endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

import config
from config import (
    KCOJ_SEEN_CASES_FILE,
    KCOJ_SEEN_CASES_PRUNE_DAYS,
)
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Cross-run dedup ──────────────────────────────────────────────────
# KCOJ recurs the same probate case on multiple days' dockets (motion hours,
# settlements, reviews). Without dedup, every daily Apify run would resend the
# same case to DataSift — polluting the CRM and wasting skip-trace credits.
# Keyed by case_number (globally unique within KY court system), value is the
# YYYY-MM-DD we first emitted it, used only for pruning.


def load_kcoj_seen_cases() -> dict[str, str]:
    """Load previously-emitted case numbers, pruning entries older than
    KCOJ_SEEN_CASES_PRUNE_DAYS. Probate cases can stay open for years but the
    90-day window is pragmatic — if a case reappears after that, it's either a
    long-running settlement where re-emit is intentional, or a court-system
    renumbering edge case we'd want to flag manually."""
    from datetime import timedelta
    data = config.load_state(KCOJ_SEEN_CASES_FILE)
    if not data:
        return {}
    cutoff = (datetime.now() - timedelta(days=KCOJ_SEEN_CASES_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {cn: d for cn, d in data.items() if d >= cutoff}
    if len(pruned) < len(data):
        logger.info(
            "KCOJ: pruned %d cases older than %d days",
            len(data) - len(pruned), KCOJ_SEEN_CASES_PRUNE_DAYS,
        )
    return pruned


def save_kcoj_seen_cases(seen: dict[str, str]) -> None:
    config.save_state(KCOJ_SEEN_CASES_FILE, seen)

# ── URLs + behavior knobs ─────────────────────────────────────────────
KCOJ_DOCKETS_URL = "https://kcoj.kycourts.net/dockets"
KCOJ_PAGE_DELAY_MIN = 1.5
KCOJ_PAGE_DELAY_MAX = 2.5
KCOJ_MAX_PAGES = 200  # safety cap — Jefferson District runs ~40 pages/day

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── Parsing regex ────────────────────────────────────────────────────
# Case line: "26-P-001544ESTATE OF: ROLAND, WELDON GENE"
# The case number runs directly into the title with no delimiter. Use a
# lookahead-style anchor (digits then a capital letter) rather than \b.
_CASE_RE = re.compile(
    r"(?P<casenum>\d{2}-[A-Z]{1,3}-\d{4,7})(?P<title>[A-Z].*)"
)
# Year prefix guard — only keep 25- and 26- cases per user's filter
_YEAR_OK_RE = re.compile(r"^(25|26)-P-\d+$")
# Title-prefix dispatch. Order matters — "ESTATE OF:" is the accept signal;
# everything else is dropped. Matching is case-sensitive because the docket
# emits uppercase titles; we normalize input to upper before matching anyway.
_ESTATE_TITLE_RE = re.compile(r"^ESTATE\s+OF:\s*(?P<name>.+?)(?:\s+DOD[:\s].*)?$")
# DOD: supports "DOD: 02/08/2011-E", "DOD:11/22/2016-EE", "DOD:11/4/24 PDA"
_DOD_RE = re.compile(
    r"DOD[:\s]+(?P<d>\d{1,2}/\d{1,2}/\d{2,4})"
)
# Room headings inside the results body, e.g. "Room: HJ202" or "Room: Unassigned"
_ROOM_RE = re.compile(r"^Room:\s*(?P<room>.+)$", re.MULTILINE)
# Time line, e.g. "01:00 PM" or "12:00 AM"
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*(?:AM|PM)$", re.MULTILINE)
# Hearing type in parens on its own line, e.g. "(MOTION HOUR)"
_HEARING_RE = re.compile(r"^\((?P<hearing>[^)]+)\)$", re.MULTILINE)
# "Page 1 of 40" pagination indicator
_PAGE_OF_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)")


# ── Helpers ──────────────────────────────────────────────────────────


async def _delay() -> None:
    await asyncio.sleep(random.uniform(KCOJ_PAGE_DELAY_MIN, KCOJ_PAGE_DELAY_MAX))


async def _find_county_select_index(page: Page) -> int | None:
    """The county dropdown has neither id nor name — locate it by inspecting
    which <select> contains the target county as an option."""
    idx = await page.evaluate(
        """(needle) => {
            const selects = document.querySelectorAll('select');
            for (let i = 0; i < selects.length; i++) {
                for (const o of selects[i].options) {
                    if (o.text.toUpperCase().includes(needle)) return i;
                }
            }
            return -1;
        }""",
        "JEFFERSON",
    )
    return None if idx is None or idx < 0 else idx


def _normalize_dod(raw: str) -> str:
    """Turn '11/4/24' or '02/08/2011' into 'YYYY-MM-DD'. Returns '' on failure."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


# ── Per-page parsing ──────────────────────────────────────────────────


def _parse_case_block(block: str) -> dict | None:
    """Parse a single case block — the line containing the case number + title,
    optionally followed by one or more metadata lines (hearing type, room, time).

    Returns None if the block doesn't parse as a target probate case. This is
    where the filters (class P, year 25/26, ESTATE OF only) apply — dropping
    here is cheaper than emitting and re-filtering downstream.
    """
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    if not lines:
        return None

    head = lines[0]
    m = _CASE_RE.match(head)
    if not m:
        return None
    casenum = m.group("casenum")
    title = m.group("title").strip()

    # Year guard — only 25-P-* and 26-P-*
    if not _YEAR_OK_RE.match(casenum):
        return None

    # Estate-only guard — excludes GUARDIANSHIP, NAME CHANGE, CURATOR, GDN, etc.
    estate_m = _ESTATE_TITLE_RE.match(title.upper())
    if not estate_m:
        return None

    # Extract decedent name (strip any trailing DOD clause captured by head)
    decedent_raw = estate_m.group("name").strip()
    # The ESTATE_TITLE_RE already strips " DOD:..." from the name, but some
    # variations have "DOD:MM/DD/YY-E" with no leading space, which the regex's
    # " DOD[:\s]" won't strip. Handle that belt-and-suspenders.
    decedent = re.split(r"\s+DOD[:\s]|DOD:", decedent_raw, maxsplit=1)[0].strip()
    # Remove trailing punctuation
    decedent = re.sub(r"[\s,]+$", "", decedent)

    # DOD: look in the full block (not just title) — some rows wrap it
    dod = ""
    dod_m = _DOD_RE.search(head)
    if dod_m:
        dod = _normalize_dod(dod_m.group("d"))

    return {
        "case_number": casenum,
        "decedent_name": decedent,
        "date_of_death": dod,
        "raw_text": block.strip(),
    }


def _split_page_into_case_blocks(body_text: str) -> list[str]:
    """Chunk the docket body text into per-case blocks.

    The docket renders as a flat stream: Room headings, time headings, then for
    each case a line with ``CASENUM TITLE`` followed by the hearing type in
    parens. We split on the case-number boundary because it's the most reliable
    anchor — every case starts with one and no other line does.
    """
    # Find every index where a case-number pattern appears
    anchors: list[int] = [m.start() for m in re.finditer(r"\d{2}-[A-Z]{1,3}-\d{4,7}", body_text)]
    if not anchors:
        return []
    # Append sentinel at end so the last slice has a stop
    anchors.append(len(body_text))
    return [body_text[anchors[i]:anchors[i + 1]] for i in range(len(anchors) - 1)]


async def _extract_current_page_cases(page: Page) -> list[dict]:
    """Pull the results-section text from the current page and parse cases."""
    # Grab the full body text — we already know the "Search Results" section
    # is a flat text blob and anchoring on case-number regex is cleaner than
    # hunting DOM nodes.
    body_text = await page.evaluate(
        """() => {
            // The results area is the last visible table/container with rows.
            // Safer to just take document.body.innerText and anchor on case#.
            return document.body.innerText;
        }"""
    )
    cases: list[dict] = []
    for block in _split_page_into_case_blocks(body_text):
        parsed = _parse_case_block(block)
        if parsed:
            cases.append(parsed)
    return cases


async def _advance_to_next_page(page: Page) -> bool:
    """Click the 'Next' pagination button. Returns False when already on last page."""
    # KCOJ renders pagination as text-with-arrow links, not semantic buttons.
    # We use a locator for 'Next' text; it will find the clickable pager element.
    # Also check the 'Page X of Y' indicator to know when to stop.
    snap = await page.evaluate(
        """() => document.body.innerText"""
    )
    m = _PAGE_OF_RE.search(snap)
    if m:
        current, total = int(m.group(1)), int(m.group(2))
        if current >= total:
            return False
    else:
        return False  # no pagination indicator — assume single page

    next_link = page.get_by_text("Next", exact=False).first
    try:
        await next_link.click(timeout=10_000)
    except Exception as e:
        logger.warning("KCOJ: Next click failed on page %s: %s", m.group(1) if m else "?", e)
        return False
    await _delay()
    # Wait for the "Page X of Y" text to update
    expected_next_page = int(m.group(1)) + 1
    for _ in range(20):
        new_body = await page.evaluate("() => document.body.innerText")
        nm = _PAGE_OF_RE.search(new_body)
        if nm and int(nm.group(1)) >= expected_next_page:
            return True
        await asyncio.sleep(0.5)
    logger.warning("KCOJ: page did not advance past %d within 10s", expected_next_page - 1)
    return False


# ── Public entry point ───────────────────────────────────────────────


async def scrape_kcoj_dockets(
    county: str,
    division: str,
    target_date: str,
    notice_type: str = "probate",
    headless: bool = True,
    seen_cases: dict[str, str] | None = None,
) -> list[NoticeData]:
    """Scrape KCOJ dockets for a single (county, division, date).

    Args:
        county: KY county name (e.g. "Jefferson"). Case-insensitive; matched
                against the county dropdown's option text.
        division: "District" or "Circuit".
        target_date: YYYY-MM-DD. Kentucky probate is District Court class P;
                     passing a weekend date will return an empty docket.
        notice_type: Written into NoticeData.notice_type for the DataSift list
                     mapping. Default "probate".
        headless: Set False to watch the browser for debugging.
        seen_cases: Cross-run dedup dict {case_number: first_seen_date}. If
                    None, loads from KCOJ_SEEN_CASES_FILE. Caller mutates the
                    dict in-place so Apify can persist to KVS. If a case is
                    already in seen_cases, we skip emitting it (but still log
                    the hit count).

    Returns a list of NoticeData, one per NEW target-qualifying case. Cases
    already in seen_cases are filtered out — only fresh hits are returned.
    """
    if seen_cases is None:
        seen_cases = load_kcoj_seen_cases()
    # Lazy Playwright import — mirrors scraper.py so JCD-only / KCOJ-only runs
    # don't trip the greenlet DLL issue on Windows.
    from playwright.async_api import async_playwright

    logger.info(
        "KCOJ scrape: %s %s %s (type=%s)",
        county, division, target_date, notice_type,
    )

    notices: list[NoticeData] = []
    today_str = datetime.now().strftime("%Y-%m-%d")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1400, "height": 1000},
        )
        page = await context.new_page()
        page.set_default_timeout(30_000)

        try:
            await page.goto(KCOJ_DOCKETS_URL, wait_until="domcontentloaded", timeout=60_000)
            # SPA hydration
            await page.wait_for_selector("select", state="attached", timeout=20_000)
            await asyncio.sleep(3)

            # ── Fill the form ────────────────────────────────────────
            idx = await _find_county_select_index(page)
            if idx is None:
                logger.error("KCOJ: county <select> not found on page")
                return notices
            county_handle = (await page.query_selector_all("select"))[idx]
            await county_handle.select_option(label=county.upper())
            await asyncio.sleep(0.75)

            div_id = "district" if division.lower().startswith("d") else "circuit"
            div_radio = await page.query_selector(f"#{div_id}")
            if not div_radio:
                logger.error("KCOJ: #%s division radio not found", div_id)
                return notices
            await div_radio.check()
            await asyncio.sleep(0.5)

            date_input = await page.query_selector('input[type="date"]')
            if not date_input:
                logger.error("KCOJ: date input not found")
                return notices
            await date_input.fill(target_date)
            await asyncio.sleep(0.5)

            submit = await page.query_selector('button[type="submit"]')
            if not submit:
                logger.error("KCOJ: submit button not found")
                return notices
            await submit.click()

            # Wait for results — look for "Search Results" header text or
            # "Page X of Y" pagination indicator to appear
            for _ in range(30):
                body = await page.evaluate("() => document.body.innerText")
                if _PAGE_OF_RE.search(body) or "Records" in body:
                    break
                await asyncio.sleep(0.5)
            else:
                logger.warning("KCOJ: results did not render within 15s for %s", target_date)
                # Still attempt extraction — some dockets are very small
            await asyncio.sleep(1)

            # ── Paginate + collect ───────────────────────────────────
            # Two layers of dedup:
            #   1. Within-run: same case often appears on multiple hearings in
            #      one day (Motion Hour + Other Hearing) — use local set.
            #   2. Cross-run: case already emitted on a prior day's docket —
            #      use `seen_cases` dict, persisted to KCOJ_SEEN_CASES_FILE.
            within_run_seen: set[str] = set()
            skipped_cross_run = 0
            skipped_within_run = 0
            total_pages_expected: int | None = None
            for page_idx in range(1, KCOJ_MAX_PAGES + 1):
                body = await page.evaluate("() => document.body.innerText")
                page_m = _PAGE_OF_RE.search(body)
                if page_m:
                    total_pages_expected = int(page_m.group(2))
                cases_on_page = await _extract_current_page_cases(page)
                kept_on_page = 0
                for c in cases_on_page:
                    cn = c["case_number"]
                    if cn in within_run_seen:
                        skipped_within_run += 1
                        continue
                    within_run_seen.add(cn)
                    if cn in seen_cases:
                        skipped_cross_run += 1
                        continue
                    # New case — emit it and record in cross-run cache
                    seen_cases[cn] = today_str
                    nd = NoticeData(
                        date_added=today_str,
                        state="KY",
                        county=county,
                        notice_type=notice_type,
                        decedent_name=c["decedent_name"],
                        date_of_death=c["date_of_death"],
                        case_number=cn,
                        source_url=KCOJ_DOCKETS_URL,
                        raw_text=c["raw_text"],
                    )
                    notices.append(nd)
                    kept_on_page += 1
                logger.info(
                    "KCOJ: page %d%s — %d qualifying (%d new)",
                    page_idx,
                    f"/{total_pages_expected}" if total_pages_expected else "",
                    len(cases_on_page), kept_on_page,
                )

                advanced = await _advance_to_next_page(page)
                if not advanced:
                    break

            logger.info(
                "KCOJ scrape complete: %d new cases, %d skipped cross-run (prior days), "
                "%d skipped within-run (same-day duplicate hearings) — %d pages",
                len(notices), skipped_cross_run, skipped_within_run, page_idx,
            )

        finally:
            await browser.close()

    return notices


# ── Sync wrapper for top-level callers ───────────────────────────────


def scrape_kcoj(
    county: str,
    division: str,
    target_date: str,
    notice_type: str = "probate",
    headless: bool = True,
) -> list[NoticeData]:
    """Sync wrapper around scrape_kcoj_dockets for callers not already in asyncio."""
    return asyncio.run(
        scrape_kcoj_dockets(
            county=county,
            division=division,
            target_date=target_date,
            notice_type=notice_type,
            headless=headless,
        )
    )


# ── CLI for standalone runs + debugging ──────────────────────────────


if __name__ == "__main__":
    import argparse
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ap = argparse.ArgumentParser(description="KCOJ docket scraper")
    ap.add_argument("--county", default="Jefferson")
    ap.add_argument("--division", default="District")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--headed", action="store_true", help="Show browser")
    ap.add_argument("--out", default="", help="Write JSON output to this path")
    args = ap.parse_args()

    results = scrape_kcoj(
        county=args.county,
        division=args.division,
        target_date=args.date,
        headless=not args.headed,
    )
    print(f"\nFound {len(results)} qualifying probate cases")
    for nd in results[:20]:
        print(f"  {nd.case_number:15s}  {nd.decedent_name:35s}  DOD={nd.date_of_death or '-'}")
    if len(results) > 20:
        print(f"  ... {len(results) - 20} more")
    if args.out:
        from dataclasses import asdict
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([asdict(n) for n in results], f, indent=2)
        print(f"\nWrote {args.out}")
    sys.exit(0)
