"""CivilView sheriff-sale detail-page enrichment.

The listing-page scraper (nj_sheriff_sales.scrape_county) captures the
basics — sheriff #, sale date, plaintiff, defendant, property address,
PropertyId. This module fetches each record's SaleDetails page and
extracts the supplementary fields the listing leaves out: docket #,
judgment amount, attorney + phone + file #, parcel id, status history,
disposition. Used by nj_sheriff_sales.scrape_civilview_notices().

ROOT CAUSE OF THE 2026 "Middlesex/Union 0%" SAGA (HARD-WON, July 2026):
**CivilView PropertyIds are EPHEMERAL.** The Vital Communications backend
rebuilds its dataset snapshot every few minutes, reallocating every
PropertyId from a global running counter (~1.5M/day drift; verified:
two fetches 10 minutes apart from the SAME IP shared 0 of 167 ids).
Consequences, all previously misdiagnosed as ELB session affinity:
  - A PropertyId captured at listing-scrape time is dead within minutes.
    Direct GET of SaleDetails with a stale id 302s to /Home/Index —
    that's why "direct GET doesn't work" appeared true; with a FRESH id
    (same snapshot) a plain GET + session cookie returns the page fine.
  - The old click-from-listing flow keyed on scrape-time ids, so any
    county whose batch started after a snapshot rotation went 0%.
    Essex "worked" purely because it enriched first, inside the TTL.
  - Fresh browser contexts, container-per-county egress IPs, and county
    warm-up navigation were all red herrings: `SalesSearch?countyId=N`
    is honored statelessly (no cookies needed, correct county served).

Therefore this module NEVER trusts a scrape-time PropertyId. Per county
it fetches the LIVE listing, indexes rows by sheriff # (stable, unique —
e.g. "F-24001837"), resolves each record to its CURRENT PropertyId, and
GETs the SaleDetails page immediately within the same requests.Session.
If a detail fetch bounces mid-batch (snapshot rotated under us), the
listing index is refreshed once and the record retried. Plain HTTP —
no Playwright, no clicks, no warm-up.

Records that no longer appear in the live listing have been retired
(auction completed / removed between scrape and enrichment) — they pass
through with no detail data, counted as parse_failures.

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
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from config import NJ_CIVILVIEW_BASE
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

DETAIL_DELAY_MIN = 1.8
DETAIL_DELAY_MAX = 2.5
PROPERTY_ID_RE = re.compile(r"PropertyId=(\d+)")

# Listing fetch retries — transient HTTP failures only (the wrong-county /
# ELB-bounce recovery dance is gone; countyId is honored statelessly).
_LISTING_FETCH_MAX_ATTEMPTS = 3
_LISTING_FETCH_DELAY_S = 4.0

# Per-county fill counters — read by modal_app/nj_sheriff_sales for
# the weekly Slack summary so 0% county failures surface immediately
# instead of being hidden inside the aggregate sheriff count.
LAST_DETAIL_RESULTS_BY_COUNTY: dict[str, dict] = {}

# Resolve our county labels to CivilView's countyId URL param. PropertyId
# alone doesn't tell us which county — we rely on the NoticeData.county
# field set by the listing scraper.
_COUNTY_TO_CIVILVIEW_ID = {"Essex": 2, "Middlesex": 73, "Union": 15}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Listing-row parsing (same shapes as nj_sheriff_sales; duplicated locally
# so this module keeps zero imports from the listing scraper).
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_DETAIL_HREF_RE = re.compile(r'href="[^"]*SaleDetails\?PropertyId=(\d+)"')
_TAG_RE = re.compile(r"<[^>]+>")

# Sheriff # as embedded in NoticeData.raw_text by the listing scraper:
# "Sheriff# F-24001837 | Plaintiff: ...". Everything up to the first pipe.
_SHERIFF_NUM_RE = re.compile(r"Sheriff#\s*([^|]+)")


def _get_egress_ip() -> str:
    """Best-effort public egress IP, kept for ops correlation in Slack."""
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
    "approx. upset*": "approx_judgment",    # Essex calls it "Approx. Upset"
    "approx. upset": "approx_judgment",
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


# ── Live-listing PropertyId resolution ────────────────────────────────

def _norm_key(s: str) -> str:
    """Normalize a sheriff# / address string for exact-match keying."""
    s = (s or "").replace("‐", "-").replace("–", "-")
    return re.sub(r"\s+", " ", s).strip().upper()


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", s)).strip()


def _parse_listing_index(html: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Parse a live listing page into PropertyId lookup structures.

    Returns ({normalized sheriff# → pid}, [(normalized address cell, pid)]).
    Sheriff # is always cells[1] (right after the View Details link);
    Middlesex's extra Status column sits at cells[2], so tail-relative
    indexing must NOT be used for it. Address is always the last cell.
    """
    sheriff_map: dict[str, str] = {}
    addr_rows: list[tuple[str, str]] = []
    for row_match in _ROW_RE.finditer(html):
        inner = row_match.group(1)
        href = _DETAIL_HREF_RE.search(inner)
        if not href:
            continue
        cells = _CELL_RE.findall(inner)
        if len(cells) < 6:
            continue
        pid = href.group(1)
        sheriff = _norm_key(_strip_tags(cells[1]))
        address = _norm_key(_strip_tags(cells[-1]))
        if sheriff and sheriff not in sheriff_map:
            sheriff_map[sheriff] = pid
        if address:
            addr_rows.append((address, pid))
    return sheriff_map, addr_rows


def _fetch_listing_index(
    session: requests.Session, county: str, cid: int
) -> tuple[dict[str, str], list[tuple[str, str]]] | None:
    """GET the county listing and build the PropertyId index; None on failure."""
    url = f"{NJ_CIVILVIEW_BASE}/Sales/SalesSearch?countyId={cid}"
    for attempt in range(1, _LISTING_FETCH_MAX_ATTEMPTS + 1):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                sheriff_map, addr_rows = _parse_listing_index(resp.text)
                if sheriff_map or addr_rows:
                    logger.info(
                        "%s listing index: %d rows (attempt %d)",
                        county, len(addr_rows), attempt,
                    )
                    return sheriff_map, addr_rows
            logger.warning(
                "%s listing fetch attempt %d/%d: HTTP %d, %d rows",
                county, attempt, _LISTING_FETCH_MAX_ATTEMPTS,
                resp.status_code, len(_parse_listing_index(resp.text)[1]),
            )
        except requests.RequestException as e:
            logger.warning(
                "%s listing fetch attempt %d/%d failed: %s",
                county, attempt, _LISTING_FETCH_MAX_ATTEMPTS, e,
            )
        time.sleep(_LISTING_FETCH_DELAY_S * attempt)
    return None


def _resolve_pid(
    n: NoticeData,
    sheriff_map: dict[str, str],
    addr_rows: list[tuple[str, str]],
) -> str | None:
    """Find the record's CURRENT PropertyId on the live listing.

    Primary key: sheriff # from raw_text ("Sheriff# F-24001837 | ...").
    Fallback: unique address-prefix match (street is the leading token
    run of the listing's address cell). None → record retired from the
    listing (auction completed) or unmatchable.
    """
    m = _SHERIFF_NUM_RE.search(n.raw_text or "")
    if m:
        pid = sheriff_map.get(_norm_key(m.group(1)))
        if pid:
            return pid
    street = _norm_key(n.address)
    if street:
        hits = {pid for addr, pid in addr_rows if addr.startswith(street)}
        if len(hits) == 1:
            return hits.pop()
    return None


def _fetch_detail_html(
    session: requests.Session, pid: str, referer: str
) -> str | None:
    """GET a SaleDetails page; None when the pid is stale or fetch fails.

    A stale pid 302s to /Home/Index which requests follows to a 200 —
    so validity is judged by the sale-detail-label content marker, not
    the status code.
    """
    url = f"{NJ_CIVILVIEW_BASE}/Sales/SaleDetails?PropertyId={pid}"
    try:
        resp = session.get(url, headers={"Referer": referer}, timeout=30)
    except requests.RequestException as e:
        logger.warning("Detail fetch failed (pid=%s): %s", pid, e)
        return None
    if resp.status_code != 200 or "sale-detail-label" not in resp.text:
        return None
    return resp.text


def _enrich_civilview_sync(
    by_county: dict[str, list[NoticeData]], today: date
) -> tuple[list[NoticeData], int, int]:
    """Blocking enrichment engine — runs in a worker thread.

    Per county: one requests.Session, one live listing fetch to build the
    sheriff# → current-PropertyId index, then a direct SaleDetails GET per
    record. A mid-batch snapshot rotation (detail GET bounces) triggers a
    single index refresh + retry for that record; the refreshed index then
    serves the rest of the batch. Returns (kept, dropped, parse_failures).
    """
    kept: list[NoticeData] = []
    dropped = 0
    parse_failures = 0

    egress_ip = _get_egress_ip()
    logger.info(
        "CivilView detail enrichment — egress IP=%s; records per county: %s",
        egress_ip, {c: len(r) for c, r in by_county.items()},
    )

    # Reset module-level per-county counters for this run. Cleared
    # at start (not end) so a crashed run still shows partial state.
    LAST_DETAIL_RESULTS_BY_COUNTY.clear()

    for county, records in by_county.items():
        cid = _COUNTY_TO_CIVILVIEW_ID[county]
        listing_url = f"{NJ_CIVILVIEW_BASE}/Sales/SalesSearch?countyId={cid}"
        logger.info(
            "Sheriff detail enrichment: %s — starting %d records",
            county, len(records),
        )

        session = requests.Session()
        session.headers.update({"User-Agent": _UA})

        index = _fetch_listing_index(session, county, cid)
        if index is None:
            logger.error(
                "⚠️ %s: listing fetch exhausted (%d attempts, egress IP=%s). "
                "Skipping %d records — detail fields will be empty.",
                county, _LISTING_FETCH_MAX_ATTEMPTS, egress_ip, len(records),
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
                "bounce_reason": "listing fetch failed",
                "egress_ip": egress_ip,
            }
            continue
        sheriff_map, addr_rows = index

        county_enriched = 0
        county_dropped = 0
        county_parse_failures = 0
        county_refreshes = 0

        for i, n in enumerate(records, 1):
            try:
                pid = _resolve_pid(n, sheriff_map, addr_rows)
                html = _fetch_detail_html(session, pid, listing_url) if pid else None
                if html is None and pid is not None:
                    # The snapshot rotated mid-batch and invalidated our
                    # index's PropertyIds. Refresh the index once and
                    # retry this record; later records reuse the fresh
                    # index automatically.
                    logger.info(
                        "%s: pid %s bounced at record %d/%d — refreshing "
                        "listing index (snapshot rotation)",
                        county, pid, i, len(records),
                    )
                    fresh = _fetch_listing_index(session, county, cid)
                    if fresh is not None:
                        sheriff_map, addr_rows = fresh
                        county_refreshes += 1
                        pid = _resolve_pid(n, sheriff_map, addr_rows)
                        if pid:
                            html = _fetch_detail_html(session, pid, listing_url)

                if html is None:
                    # Not on the live listing (retired/completed) or the
                    # retry also bounced — pass through un-enriched.
                    parse_failures += 1
                    county_parse_failures += 1
                    kept.append(n)
                else:
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
                        else:
                            kept.append(n)
            except Exception as e:
                logger.warning("Detail fetch failed (%s): %s", n.source_url, e)
                parse_failures += 1
                county_parse_failures += 1
                kept.append(n)

            if i % 25 == 0:
                logger.info("  %s [%d/%d] detail pages fetched", county, i, len(records))
            # Throttle between every fetch (last record included is
            # fine — total runtime is dominated by enrichment, not
            # this final 2s).
            time.sleep(random.uniform(DETAIL_DELAY_MIN, DETAIL_DELAY_MAX))

        # Per-county summary — visible in logs + stashed for Slack.
        pct = (100 * county_enriched / len(records)) if records else 0.0
        LAST_DETAIL_RESULTS_BY_COUNTY[county] = {
            "enriched": county_enriched,
            "dropped": county_dropped,
            "parse_failures": county_parse_failures,
            "total": len(records),
            "listing_bounce": False,
            "listing_refreshes": county_refreshes,
            "egress_ip": egress_ip,
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
                "%d dropped (resolved), %d parse-failures, %d index refreshes",
                county, county_enriched, len(records), pct,
                county_dropped, county_parse_failures, county_refreshes,
            )

    return kept, dropped, parse_failures


async def enrich_sheriff_records(
    notices: list[NoticeData],
    *,
    headless: bool = True,  # retained for API compat; Playwright is gone
    today: date | None = None,
) -> list[NoticeData]:
    """Fetch & merge detail-page data for each CivilView sheriff record.

    Records with no CivilView PropertyId in their source_url (e.g.
    Somerset PDF-hosted sales) pass through unchanged with blank
    detail fields. Records whose case_disposition resolves to a drop
    bucket (Sold / Redeemed / Cancelled) are removed.

    PropertyIds from scrape time are NEVER used to fetch (they are
    ephemeral — see module docstring); they only serve as the marker
    that a record is CivilView-sourced. Each record is re-resolved to
    its current PropertyId on the live listing by sheriff # (address
    fallback) and fetched via plain HTTP in the same session. Per-record
    cost is ~2-3s including the throttle sleep.

    Returns the surviving list (CivilView-enriched + non-CivilView
    passthroughs).
    """
    if not notices:
        return notices

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

    # Group by county so each county resolves against its own listing.
    # Records with an unknown county name fall back to Middlesex's listing
    # — most reliable + sheriff numbers still match if the record actually
    # belongs there.
    by_county: dict[str, list[NoticeData]] = {}
    for n in civilview:
        county = (n.county or "").strip().title()
        if county not in _COUNTY_TO_CIVILVIEW_ID:
            county = "Middlesex"  # safe fallback for unmapped records
        by_county.setdefault(county, []).append(n)

    kept, dropped, parse_failures = await asyncio.to_thread(
        _enrich_civilview_sync, by_county, today
    )

    logger.info(
        "Sheriff detail enrichment complete: %d kept / %d dropped (resolved cases) "
        "/ %d parse-failures (retired from listing or unmatchable)",
        len(kept), dropped, parse_failures,
    )
    return kept + other
