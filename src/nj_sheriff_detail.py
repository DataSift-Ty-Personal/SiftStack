"""CivilView sheriff-sale detail-page enrichment.

The listing-page scraper (nj_sheriff_sales.scrape_county) captures the
basics — sheriff #, sale date, plaintiff, defendant, property address,
PropertyId. This module fetches each record's SaleDetails page and
extracts the supplementary fields the listing leaves out: docket #,
judgment amount, attorney + phone + file #, parcel id, status history,
disposition. Used by nj_sheriff_sales.scrape_civilview_notices().

Site quirks (HARD-WON — direct GET does NOT work):
  - The page is server-rendered ASP-ish HTML with a clean
    `<div class="sale-detail-label">LABEL:</div>` immediately followed
    by `<div class="sale-detail-value">VALUE</div>` for each field.
  - The Status History is a `<table>` with [Status, Date] rows. Most
    recent status is the first data row.
  - **Direct `page.goto(SaleDetails URL)` redirects to /Home/Index
    even with a fresh AWS-ELB cookie + matching Referer header.** The
    only consistently-working flow is: navigate to the county listing
    → locate the `a[href*="PropertyId=N"]` link → click it. We re-
    navigate to the listing before every click; trying to chain clicks
    from a single listing render bounces after a few hits.
  - Completed / cancelled PropertyIds get retired from the listing
    between weekly cycles. Records that no longer appear in the live
    listing pass through with no detail data — UNKNOWN tier downstream.

Auto-skip: records whose case_disposition resolves to Sold / Redeemed /
Cancelled are dropped from the returned list — these auctions are over
and not worth marketing.

Status row format: CivilView puts the adjournment trigger in the status
itself, e.g. "Adjourned - Plaintiff" or "Adjourned - Court". We split on
" - " into status + reason so the downstream tier logic can count only
PLAINTIFF adjournments against NJ's 2-adjournment cap (court adjournments
are unlimited and don't count against the homeowner's option).
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

DETAIL_DELAY_MIN = 1.8
DETAIL_DELAY_MAX = 2.5
PROPERTY_ID_RE = re.compile(r"PropertyId=(\d+)")

# AWS-ELB sometimes rotates session affinity mid-run, causing the
# county-switch listing navigation to bounce to /Home/Index. Detect and
# recover instead of silently failing every record in that county. Set
# above zero so we still ship if recovery exhausts.
_LISTING_RECOVERY_MAX_ATTEMPTS = 3
_LISTING_RECOVERY_DELAY_S = 4.0

# Per-county fill counters — read by modal_app/nj_sheriff_sales for
# the weekly Slack summary so 0% county failures surface immediately
# instead of being hidden inside the aggregate sheriff count.
LAST_DETAIL_RESULTS_BY_COUNTY: dict[str, dict] = {}

# Resolve our county labels to CivilView's countyId URL param. PropertyId
# alone doesn't tell us which county — we rely on the NoticeData.county
# field set by the listing scraper.
_COUNTY_TO_CIVILVIEW_ID = {"Essex": 2, "Middlesex": 73, "Union": 15}


def _get_egress_ip() -> str:
    """Best-effort public egress IP, for ELB-affinity diagnostics.

    The Middlesex/Union detail-enrichment 0% (Essex 100%) is suspected to be
    AWS-ELB session affinity keyed to the Modal container's egress IP. Logging
    the IP per run lets us correlate which IP keeps landing on Essex's listing.
    """
    import urllib.request
    for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            continue
    return "unknown"

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
# bucket. Order matters: more-specific keywords first. "Adjourn" cases
# stay Open — the auction is just deferred to a future date.
_CASE_DISPOSITION_RULES = (
    ("scheduled", "Open"),
    ("adjourn", "Open"),         # Plaintiff/Defendant/Court adjournment → still active
    ("on hold", "Open"),
    ("purchased", "Sold"),
    ("sold", "Sold"),
    ("redeemed", "Redeemed"),
    ("bankruptcy", "Bankruptcy"),
    ("bankuptcy", "Bankruptcy"),  # CivilView typo in some counties
    ("cancelled", "Cancelled"),
    ("canceled", "Cancelled"),
)

_DROP_DISPOSITIONS = frozenset({"Sold", "Redeemed", "Cancelled"})


def _parse_money(s: str) -> str:
    """Strip $ and commas; return numeric string (empty in → empty out)."""
    s = (s or "").strip().replace("$", "").replace(",", "")
    return s


def _split_status(raw: str) -> tuple[str, str]:
    """Split CivilView's status cell into (status, reason).

    "Adjourned - Plaintiff"     → ("Adjourned", "Plaintiff")
    "Adjourned - Plaintiff req." → ("Adjourned", "Plaintiff req.")
    "Scheduled - Foreclosure"   → ("Scheduled", "Foreclosure")
    "Scheduled"                 → ("Scheduled", "")
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    parts = raw.split(" - ", 1)
    if len(parts) == 1:
        return parts[0].strip(), ""
    return parts[0].strip(), parts[1].strip()


def _iso_date(raw: str) -> str:
    """CivilView dates are M/D/YYYY. Convert to YYYY-MM-DD; passthrough on failure."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return raw


def parse_detail_html(html: str) -> dict:
    """Parse a CivilView SaleDetails HTML page into a flat dict.

    Returns a dict keyed by NoticeData field names. Fields not found
    in the HTML are omitted (callers should treat absence as blank).
    Always returns `status_history_json` and `current_status` — both
    empty strings if no history table is present.

    status_history_json is a list of {"date","status","reason"} dicts,
    dates in ISO format. The current_status field carries the full
    original CivilView label (e.g. "Adjourned - Plaintiff") so the
    case_disposition keyword matching keeps working.
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
    # CivilView orders rows chronologically (oldest first) at least for
    # Union — `current_status` is the row with the latest date, not the
    # first row encountered. We track the latest separately rather than
    # assuming row position.
    history: list[dict] = []
    raw_status_by_date: list[tuple[str, str]] = []  # (date_str, raw_status)
    table = soup.find("table")
    if table is not None:
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            raw_status = cells[0].get_text(" ", strip=True)
            raw_date = cells[1].get_text(" ", strip=True)
            # Skip header + the [Collapse All] toggle row variants.
            if not raw_status or not raw_date:
                continue
            if raw_status.lower() == "status" and raw_date.lower() == "date":
                continue
            if raw_status.startswith("[") and raw_status.endswith("]"):
                continue
            iso = _iso_date(raw_date)
            status, reason = _split_status(raw_status)
            history.append({
                "date": iso,
                "status": status,
                "reason": reason,
            })
            raw_status_by_date.append((iso, raw_status))

    # current_status = the raw status of the entry with the latest ISO
    # date. Falls back to the last row encountered if dates failed to
    # parse (passthrough form).
    current_status = ""
    if raw_status_by_date:
        try:
            parseable = [
                (datetime.strptime(d, "%Y-%m-%d").date(), s)
                for d, s in raw_status_by_date
                if re.match(r"\d{4}-\d{2}-\d{2}$", d)
            ]
            if parseable:
                current_status = max(parseable, key=lambda t: t[0])[1]
            else:
                current_status = raw_status_by_date[-1][1]
        except ValueError:
            current_status = raw_status_by_date[-1][1]

    out["status_history_json"] = json.dumps(history) if history else ""
    out["current_status"] = current_status
    return out


def derive_fields(parsed: dict, today: date) -> dict:
    """Compute derived fields from a parsed detail dict.

    `adjournment_count` is the TOTAL number of adjournments across all
    reasons (plaintiff + court + bankruptcy etc.). The downstream
    `apply_priority_tiers` recomputes plaintiff-only counts from the
    same history JSON for adjournments_remaining — see
    `nj_sheriff_sales.apply_priority_tiers` for why.
    """
    out: dict = {}
    try:
        history = json.loads(parsed.get("status_history_json") or "[]")
    except json.JSONDecodeError:
        history = []

    # Match "adjourn" prefix to catch both inflections CivilView uses:
    # - Essex/Middlesex: "Adjourned - Plaintiff"
    # - Union: "Plaintiff Adjournment" / "Defendant Adjournment" /
    #          "Adjourned per Court Order"
    # Counts ALL adjournments across all reasons; the plaintiff-only
    # subset is computed downstream in apply_priority_tiers.
    out["adjournment_count"] = str(
        sum(
            1 for h in history
            if "adjourn" in (
                ((h.get("status", "") or "") + " " + (h.get("reason", "") or ""))
                .lower()
            )
        )
    )

    parsed_dates = []
    for h in history:
        # status_history_json dates are already ISO (YYYY-MM-DD) after
        # parse_detail_html normalization. Tolerate the old M/D/YYYY in
        # case a re-import hits cached data.
        raw_d = h.get("date", "")
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                parsed_dates.append(datetime.strptime(raw_d, fmt).date())
                break
            except ValueError:
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

    Uses click-from-listing rather than direct page.goto — see module
    docstring. We group records by county, then for each record we
    navigate to that county's listing page, locate the PropertyId
    anchor, and click. Per-record cost is ~4-5s including the throttle
    sleep; a typical week of ~400 records runs ~25-30 min on Modal.

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

    # Group by county so we can use each listing as the click launchpad.
    # Records with an unknown county name fall back to Middlesex's listing
    # — most reliable + the link locator still works as long as the
    # PropertyId appears somewhere on a CivilView listing render.
    by_county: dict[str, list[NoticeData]] = {}
    for n in civilview:
        county = (n.county or "").strip().title()
        if county not in _COUNTY_TO_CIVILVIEW_ID:
            county = "Middlesex"  # safe fallback for unmapped records
        by_county.setdefault(county, []).append(n)

    # ── Week 26 diagnostics (Option C): confirm the ELB-affinity theory ──
    # Map every county's expected PropertyIds so we can identify which county a
    # listing/detail page ACTUALLY served (by PropertyId overlap), and log the
    # egress IP. If the 07/01 logs show Middlesex/Union warm-ups consistently
    # landing on Essex's listing from the same egress IP despite fresh
    # contexts, that confirms IP-keyed AWS-ELB affinity → implement one-
    # container-per-county (Option A). Read-only; changes no scrape behavior.
    _cid_to_county = {v: k for k, v in _COUNTY_TO_CIVILVIEW_ID.items()}
    _all_county_pids: dict[str, set[str]] = {}
    for _cty, _recs in by_county.items():
        _all_county_pids[_cty] = {
            mm.group(1) for nn in _recs
            if (mm := PROPERTY_ID_RE.search(nn.source_url or ""))
        }

    def _identify_landed_county(present_pids: "set[str]") -> str:
        """Best guess of which county a page served, by PropertyId overlap."""
        best, best_n = "unknown", 0
        for _cty, _pids in _all_county_pids.items():
            overlap = len(present_pids & _pids)
            if overlap > best_n:
                best, best_n = _cty, overlap
        return best if best_n else "unknown"

    egress_ip = _get_egress_ip()
    logger.info(
        "CivilView detail enrichment — egress IP=%s; expected PropertyIds per "
        "county: %s",
        egress_ip, {c: len(p) for c, p in _all_county_pids.items()},
    )

    kept: list[NoticeData] = []
    dropped = 0
    parse_failures = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"] if headless else [],
        )
        ctx = None
        page = None
        _UA = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        async def _new_context() -> None:
            """(Re)create a fresh browser context + page.

            CivilView binds the AWS-ELB session to the FIRST county loaded
            on a given context; navigating that same context to a
            different countyId keeps serving the original county's
            listing (no error, no bounce — just stale results). A fresh
            context (new cookies → new session) is the only reliable way
            to bind a new county, so we recreate one per county batch.
            """
            nonlocal ctx, page
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass
            ctx = await browser.new_context(user_agent=_UA)
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await ctx.new_page()

        async def _navigate_to_listing(
            target_url: str, expected_pids: "set[str] | None" = None
        ) -> int:
            """Navigate to a county listing; return PropertyId link count.

            Returns -1 on hard navigation failure (bounce to /Home/Index
            or 0 links visible). Returns -2 when the listing loads fine
            but is the WRONG county — none of `expected_pids` appear in
            the DOM, which is the sticky-session symptom that previously
            caused silent 100% parse-failures for Middlesex/Union.
            Positive return = PropertyId link count.
            """
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(500)
            except Exception as e:
                logger.warning("Listing nav failed (%s): %s", target_url, e)
                return -1
            # AWS-ELB session bounce — direct symptom is landing on
            # /Home/Index instead of /Sales/SalesSearch. Always check
            # before trusting the page state.
            cur_url = page.url
            if "Home/Index" in cur_url or "aspxerrorpath" in cur_url:
                return -1
            hrefs = await page.locator('a[href*="PropertyId="]').evaluate_all(
                "els => els.map(e => e.getAttribute('href'))"
            )
            total_links = len(hrefs)
            if total_links == 0:
                return -1
            # County-identity check: a real listing for the WRONG county
            # (sticky session) still has anchors, so count > 0 is not
            # enough. Confirm at least one record we're about to enrich
            # is actually present on this listing.
            if expected_pids:
                present = {
                    m.group(1)
                    for h in hrefs
                    if (m := PROPERTY_ID_RE.search(h or ""))
                }
                if not (present & expected_pids):
                    req_m = re.search(r"countyId=(\d+)", target_url)
                    req_cid = int(req_m.group(1)) if req_m else -1
                    req_county = _cid_to_county.get(req_cid, f"countyId={req_cid}")
                    landed = _identify_landed_county(present)
                    logger.warning(
                        "WRONG-COUNTY listing: requested %s (countyId=%d) but page "
                        "served %s [%d links, 0/%d expected pids present, landed-url=%s] "
                        "— AWS-ELB affinity signal (egress IP=%s)",
                        req_county, req_cid, landed, total_links,
                        len(expected_pids), page.url, egress_ip,
                    )
                    return -2
            return total_links

        async def _warm_session_for_county(
            target_cid: int, expected_pids: "set[str] | None" = None
        ) -> bool:
            """Establish a fresh AWS-ELB session bound to `target_cid`.

            Recreates the browser context on every attempt so each try
            gets a clean session — required because a context already
            bound to another county keeps serving that county no matter
            how many times we navigate. Confirms the listing really is
            this county via `expected_pids` (catches the sticky-session
            wrong-county page that a plain link-count check misses).
            """
            target_url = (
                f"https://salesweb.civilview.com/Sales/SalesSearch?countyId={target_cid}"
            )
            for attempt in range(1, _LISTING_RECOVERY_MAX_ATTEMPTS + 1):
                await _new_context()
                links = await _navigate_to_listing(target_url, expected_pids)
                if links > 0:
                    logger.info(
                        "Warm countyId=%d ok on attempt %d (%d links)",
                        target_cid, attempt, links,
                    )
                    return True
                reason = "wrong-county listing" if links == -2 else "bounce page"
                logger.warning(
                    "Warm countyId=%d attempt %d/%d landed on %s",
                    target_cid, attempt, _LISTING_RECOVERY_MAX_ATTEMPTS, reason,
                )
                await asyncio.sleep(_LISTING_RECOVERY_DELAY_S * attempt)
            return False

        # Reset module-level per-county counters for this run. Cleared
        # at start (not end) so a crashed run still shows partial state.
        LAST_DETAIL_RESULTS_BY_COUNTY.clear()

        i = 0
        for county, records in by_county.items():
            cid = _COUNTY_TO_CIVILVIEW_ID[county]
            listing_url = f"https://salesweb.civilview.com/Sales/SalesSearch?countyId={cid}"
            # PropertyIds we expect on THIS county's listing — drives the
            # wrong-county detection in _navigate_to_listing so a sticky
            # session serving another county's results is caught instead
            # of silently parse-failing every record.
            expected_pids = {
                m.group(1)
                for n in records
                if (m := PROPERTY_ID_RE.search(n.source_url or ""))
            }

            # Per-county session: _warm_session_for_county spins up a
            # FRESH browser context bound to this countyId. Without the
            # fresh context the session stays bound to the first county
            # (Essex) and every Middlesex/Union PropertyId silently misses
            # → 0% enriched. If recovery fails after 3 tries, mark every
            # record in this county as a parse failure and skip.
            logger.info(
                "Sheriff detail enrichment: %s — starting %d records",
                county, len(records),
            )
            warm_ok = await _warm_session_for_county(cid, expected_pids)
            if not warm_ok:
                logger.error(
                    "⚠️ %s: listing-page recovery exhausted (%d attempts). "
                    "Skipping %d records — detail fields will be empty. "
                    "Likely cause: AWS-ELB session/IP rotation.",
                    county, _LISTING_RECOVERY_MAX_ATTEMPTS, len(records),
                )
                for n in records:
                    parse_failures += 1
                    kept.append(n)
                LAST_DETAIL_RESULTS_BY_COUNTY[county] = {
                    "enriched": 0,
                    "dropped": 0,
                    "parse_failures": len(records),
                    "total": len(records),
                    "listing_bounce": True,
                }
                continue

            county_enriched = 0
            county_dropped = 0
            county_parse_failures = 0

            for n in records:
                i += 1
                m = PROPERTY_ID_RE.search(n.source_url or "")
                if not m:
                    parse_failures += 1
                    county_parse_failures += 1
                    kept.append(n)
                    continue
                pid = m.group(1)

                try:
                    # Bounce back to the listing before each click — chaining
                    # clicks from one render bounces after a few hits.
                    links = await _navigate_to_listing(listing_url, expected_pids)
                    if links < 0:
                        # Session bounced (or rotated to the wrong county)
                        # mid-batch. Re-warm with a fresh context once; if
                        # that fails, drop this record but don't kill the
                        # rest of the batch.
                        logger.warning(
                            "%s: listing %s mid-batch at record %d/%d — re-warming",
                            county,
                            "wrong-county" if links == -2 else "bounce",
                            len(kept) + 1, len(records),
                        )
                        if not await _warm_session_for_county(cid, expected_pids):
                            parse_failures += 1
                            county_parse_failures += 1
                            kept.append(n)
                            continue
                        links = await _navigate_to_listing(listing_url, expected_pids)
                        if links < 0:
                            parse_failures += 1
                            county_parse_failures += 1
                            kept.append(n)
                            continue

                    # Use the un-`.first` locator for count() — Playwright's
                    # `.first.count()` returns unreliable values in headless
                    # mode (intermittently 0 even when the element exists).
                    # An untrimmed locator's count() returns total matches.
                    selector = f'a[href*="PropertyId={pid}"]'
                    target_count = await page.locator(selector).count()
                    if target_count == 0:
                        # PropertyId retired between listing scrape + detail
                        # fetch — auction likely completed.
                        parse_failures += 1
                        county_parse_failures += 1
                        kept.append(n)
                    else:
                        await page.locator(selector).first.click()
                        await page.wait_for_load_state("domcontentloaded", timeout=20000)
                        await page.wait_for_timeout(500)
                        html = await page.content()
                        # Diagnostic (Option C): confirm the detail fetch landed
                        # on a real SaleDetails page for THIS county, not a
                        # bounce to another county's listing. WARNING only on a
                        # bounce so Essex's many successes don't flood the log.
                        if "SaleDetails" not in page.url:
                            try:
                                _hrefs = await page.locator('a[href*="PropertyId="]').evaluate_all(
                                    "els => els.map(e => e.getAttribute('href'))"
                                )
                                _present = {
                                    mm.group(1) for h in _hrefs
                                    if (mm := PROPERTY_ID_RE.search(h or ""))
                                }
                                logger.warning(
                                    "Detail fetch pid=%s (%s) bounced to %s page "
                                    "(served county=%s, url=%s, egress IP=%s)",
                                    pid, county,
                                    "listing" if _present else "non-detail",
                                    _identify_landed_county(_present), page.url, egress_ip,
                                )
                            except Exception:
                                pass
                        parsed = parse_detail_html(html)
                        if not parsed.get("current_status") and not parsed.get("court_case_number"):
                            parse_failures += 1
                            county_parse_failures += 1
                            kept.append(n)
                        else:
                            for k, v in {**parsed, **derive_fields(parsed, today)}.items():
                                setattr(n, k, v)
                            county_enriched += 1
                            if n.case_disposition in _DROP_DISPOSITIONS:
                                dropped += 1
                                county_dropped += 1
                                continue
                            kept.append(n)
                except Exception as e:
                    logger.warning("Detail fetch failed (%s): %s", n.source_url, e)
                    county_parse_failures += 1
                    kept.append(n)

                if i % 25 == 0:
                    logger.info("  [%d/%d] detail pages fetched", i, len(civilview))
                # Throttle between every click (last record included is
                # fine — total runtime is dominated by enrichment, not
                # this final 2s).
                await asyncio.sleep(random.uniform(DETAIL_DELAY_MIN, DETAIL_DELAY_MAX))

            # Per-county summary — visible in logs + stashed for Slack.
            pct = (100 * county_enriched / len(records)) if records else 0.0
            LAST_DETAIL_RESULTS_BY_COUNTY[county] = {
                "enriched": county_enriched,
                "dropped": county_dropped,
                "parse_failures": county_parse_failures,
                "total": len(records),
                "listing_bounce": False,
            }
            if county_enriched == 0 and len(records) > 5:
                # 0% on a batch of any meaningful size = systemic
                # failure for that county. Loud warning so it surfaces
                # in Modal logs + Slack monitoring.
                logger.error(
                    "⚠️ %s: 0/%d enriched (%.0f%%) — likely systemic failure",
                    county, len(records), pct,
                )
            else:
                logger.info(
                    "%s detail enrichment: %d/%d enriched (%.0f%%), "
                    "%d dropped (resolved), %d parse-failures",
                    county, county_enriched, len(records), pct,
                    county_dropped, county_parse_failures,
                )

        await browser.close()

    logger.info(
        "Sheriff detail enrichment complete: %d kept / %d dropped (resolved cases) "
        "/ %d parse-failures (retired PropertyIds or missing links)",
        len(kept), dropped, parse_failures,
    )
    return kept + other
