"""Redemption-window watcher — Common Pleas docket monitor.

ORC §2329.33 grants Ohio homeowners a redemption right that runs from the
moment of the sheriff sale UNTIL the court confirms the sale (typically
7-30 days after the auction). Most operators drop foreclosure records the
moment "Sold" appears at auction; this module keeps them active in the
pipeline through the confirmation window so we can run a 14-day aggressive
cadence (mail + door knock + ISA call) on properties where the homeowner
can still legally redeem.

Architecture:
  This is NOT a NoticeScraper. It's a STATUS UPDATER. It takes existing
  NoticeData records (from prior scrape runs) and updates their redemption
  fields by querying the appropriate Common Pleas portal for each case.

  Wiring:
    daily flow → scrape → run_redemption_watch(records) → enrich → format

Per-county portals:
  - Franklin: fcdcfcjs.co.franklin.oh.us/CaseInformationOnline (no auth, no CAPTCHA)
  - Montgomery: PRO Common Pleas (reCAPTCHA v3 — same gating as lis pendens scraper)
  - Greene: JWorks eServices (currently disabled — sentinel returns no updates)

Docket event patterns (Franklin court terminology; others vary):
  Sale held:
    - "RETURN OF SALE FILED"
    - "ORDER OF SALE RETURNED"
    - "SHERIFF'S RETURN"
  Confirmation hearing scheduled:
    - "CONFIRMATION HEARING SET"
    - "HEARING SCHEDULED" + "CONFIRMATION"
  Sale confirmed:
    - "ENTRY CONFIRMING SHERIFF SALE"
    - "JE CONFIRMING SALE"
    - "ORDER CONFIRMING SALE"

Output: each input record gets `redemption_window_status`, `sheriff_sale_held_date`,
`confirmation_hearing_date`, and `redemption_window_days_remaining` set or
left blank. Records flowing through this module without a foreclosure
notice_type are returned unchanged.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

import config
from models import NoticeData

logger = logging.getLogger(__name__)


# ── Portal config ──────────────────────────────────────────────────────

FRANKLIN_BASE = "https://fcdcfcjs.co.franklin.oh.us/CaseInformationOnline"
FRANKLIN_LANDING = f"{FRANKLIN_BASE}/"
FRANKLIN_ACCEPT = f"{FRANKLIN_BASE}/acceptDisclaimer"
FRANKLIN_CASE_SEARCH = f"{FRANKLIN_BASE}/caseSearch"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# How close to confirmation = "closing" (urgent). Default: 14 days.
CLOSING_THRESHOLD_DAYS = 14


# ── Output type ────────────────────────────────────────────────────────

@dataclass
class RedemptionStatus:
    """Status update for a single foreclosure case."""
    case_number: str
    sale_held_date: str = ""           # YYYY-MM-DD or ""
    confirmation_hearing_date: str = ""  # YYYY-MM-DD or ""
    status: str = ""                   # "open" | "closing" | "closed" | ""
    days_remaining: str = ""           # int as string, or ""


# ── Docket event regexes ──────────────────────────────────────────────

# Match docket entries like "04/22/2026 RETURN OF SALE FILED" or
# "<td>04/22/2026</td><td>RETURN OF SALE FILED</td>".
_DOCKET_LINE_RE = re.compile(
    r"(?P<date>\d{2}/\d{2}/\d{4})\s*[</td>\s]*\s*(?P<event>[A-Z][A-Z0-9 .'/&,\-]{4,200})",
    re.IGNORECASE,
)

_SALE_HELD_RE = re.compile(
    r"\b(RETURN\s+OF\s+SALE\s+FILED|ORDER\s+OF\s+SALE\s+RETURNED|SHERIFF'?S?\s+RETURN(?:\s+FILED)?)\b",
    re.IGNORECASE,
)
_CONFIRMATION_HEARING_RE = re.compile(
    r"\b(CONFIRMATION\s+HEARING\s+(?:SET|SCHEDULED)|HEARING\s+SET\s+FOR\s+CONFIRMATION)\b",
    re.IGNORECASE,
)
_SALE_CONFIRMED_RE = re.compile(
    r"\b(JE\s+CONFIRMING|ENTRY\s+CONFIRMING\s+SHERIFF'?S?\s+SALE|ORDER\s+CONFIRMING\s+SALE)\b",
    re.IGNORECASE,
)


# ── Public entry point ─────────────────────────────────────────────────

def run_redemption_watch(records: list[NoticeData]) -> list[NoticeData]:
    """Update redemption fields on `records` by querying Common Pleas dockets.

    Records without `notice_type=foreclosure` or without a `case_number`
    are returned unchanged. Records with no docket events found are also
    unchanged (their redemption fields remain blank).

    The function mutates the records in place AND returns the same list
    for convenience in pipeline chaining.

    NOTE: For full autonomy across daily runs, callers should pass the
    OUTPUT of `notice_state.merge_with_today(today_scrape)` — that returns
    the union of today's scrape and all persisted active records, ensuring
    no cases drop out between runs. Otherwise this function only sees
    today's scrape.
    """
    if not records:
        return records

    # Bucket records by county for per-portal querying. Each bucket maps
    # case_number → record so we can update by case_number lookup.
    buckets: dict[str, dict[str, NoticeData]] = {
        "Franklin": {},
        "Montgomery": {},
        "Greene": {},
    }
    for r in records:
        if r.notice_type != "foreclosure":
            continue
        if not r.case_number:
            continue
        if r.county not in buckets:
            continue
        buckets[r.county][r.case_number] = r

    total_eligible = sum(len(b) for b in buckets.values())
    if total_eligible == 0:
        logger.info("Redemption watch: 0 foreclosure records with case_number; skipping")
        return records

    logger.info(
        "Redemption watch: checking %d cases (Franklin=%d, Montgomery=%d, Greene=%d)",
        total_eligible,
        len(buckets["Franklin"]),
        len(buckets["Montgomery"]),
        len(buckets["Greene"]),
    )

    # Per-county query
    franklin_updates = _watch_franklin(list(buckets["Franklin"].keys()))
    montgomery_updates = _watch_montgomery(list(buckets["Montgomery"].keys()))
    greene_updates = _watch_greene(list(buckets["Greene"].keys()))

    # Merge updates back into records
    all_updates: dict[str, dict[str, RedemptionStatus]] = {
        "Franklin": franklin_updates,
        "Montgomery": montgomery_updates,
        "Greene": greene_updates,
    }
    open_count = 0
    closing_count = 0
    closed_count = 0
    for county, county_updates in all_updates.items():
        for case_number, status in county_updates.items():
            record = buckets[county].get(case_number)
            if record is None:
                continue
            record.sheriff_sale_held_date = status.sale_held_date
            record.confirmation_hearing_date = status.confirmation_hearing_date
            record.redemption_window_status = status.status
            record.redemption_window_days_remaining = status.days_remaining
            if status.status == "open":
                open_count += 1
            elif status.status == "closing":
                closing_count += 1
            elif status.status == "closed":
                closed_count += 1

    logger.info(
        "Redemption watch done — windows: open=%d, closing=%d, closed=%d",
        open_count, closing_count, closed_count,
    )
    return records


# ── Franklin (CIO) ─────────────────────────────────────────────────────

def _watch_franklin(case_numbers: list[str]) -> dict[str, RedemptionStatus]:
    """Query Franklin CIO for each case, parse docket for redemption events."""
    if not case_numbers:
        return {}

    session = _build_franklin_session()
    if not _accept_franklin_disclaimer(session):
        logger.warning("Franklin redemption watch: failed to seed CIO session")
        return {}

    out: dict[str, RedemptionStatus] = {}
    today = date.today()

    for case_number in case_numbers:
        parts = _parse_franklin_case_number(case_number)
        if parts is None:
            logger.debug("Franklin redemption watch: skipping malformed case %s", case_number)
            continue
        year, ctype, seq = parts

        try:
            html = _fetch_franklin_case_html(session, year, ctype, seq)
        except requests.RequestException as e:
            logger.debug("Franklin redemption watch: case %s fetch failed: %s", case_number, e)
            continue
        _sleep()

        if not html:
            continue

        sale_held, hearing_date, confirmed = _parse_docket_events(html)

        status = _compute_status(sale_held, hearing_date, confirmed, today)
        if status is None:
            # No relevant events found — leave fields blank.
            continue
        out[case_number] = status

    logger.info("Franklin redemption watch: %d updates from %d cases", len(out), len(case_numbers))
    return out


def _build_franklin_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _accept_franklin_disclaimer(session: requests.Session) -> bool:
    try:
        r1 = session.get(FRANKLIN_LANDING, timeout=30)
        if r1.status_code != 200:
            return False
        _sleep()
        r2 = session.post(
            FRANKLIN_ACCEPT,
            data={"fromPage": "index", "Accept": "ACCEPT"},
            timeout=30,
            headers={"Referer": FRANKLIN_LANDING},
        )
        return r2.status_code == 200
    except requests.RequestException:
        return False


def _fetch_franklin_case_html(
    session: requests.Session,
    year: str,
    ctype: str,
    seq: str,
) -> str:
    """POST caseSearch and return the response HTML (full case detail w/ docket)."""
    data = {
        "setField": "2",
        "caseYear": year,
        "caseYear_h": year,
        "caseType": ctype,
        "caseType_h": ctype,
        "caseSeq": seq,
        "caseSeq_h": seq,
        "selType": "",
        "lname": "",
        "fname": "",
        "mint": "",
        "advFlag": "show",
        "reallySubmit": "true",
        "personType": "P",
    }
    detail_url = f"{FRANKLIN_CASE_SEARCH}?" + urlencode({
        "caseYear": year,
        "caseType": ctype,
        "caseSeq": seq,
    })
    resp = session.post(
        FRANKLIN_CASE_SEARCH,
        data=data,
        timeout=45,
        headers={"Referer": detail_url},
    )
    if resp.status_code != 200:
        return ""
    return resp.text


# ── Montgomery (PRO Common Pleas) ──────────────────────────────────────

MONTGOMERY_BASE = "https://pro.mcohio.org"
MONTGOMERY_LANDING = f"{MONTGOMERY_BASE}/"
MONTGOMERY_INITIALIZE = f"{MONTGOMERY_BASE}/Helpers/initializeSession.aspx"
MONTGOMERY_DISCLAIMER = f"{MONTGOMERY_BASE}/Helpers/acceptDisclaimer.aspx"
MONTGOMERY_SEARCH = f"{MONTGOMERY_BASE}/Helpers/generalSearchResults.aspx"
MONTGOMERY_CASE_INFO = f"{MONTGOMERY_BASE}/Helpers/caseInformation.aspx"

# Hardcoded by the site (same as Montgomery lis pendens scraper).
_MONTGOMERY_RECAPTCHA_SITEKEY = "6LcIVYQcAAAAAB3UDYAT2rh-EelDlT7i48-tTvhv"
_MONTGOMERY_RECAPTCHA_ACTION = "genSearch"


def _watch_montgomery(case_numbers: list[str]) -> dict[str, RedemptionStatus]:
    """Montgomery PRO Common Pleas docket — full implementation.

    Reuses the bootstrap + reCAPTCHA v3 + 2Captcha flow proven by the
    Montgomery lis pendens scraper. One CAPTCHA solve per WATCH RUN
    (not per case) — the same token handles a date-range search that
    returns case_id mappings for our active cases. Then per-case docket
    fetches use the established session cookies (no further CAPTCHA).

    Per-run cost: ~$0.003 (one 2Captcha solve) regardless of case count.
    """
    if not case_numbers:
        return {}
    if not config.CAPTCHA_API_KEY:
        logger.warning(
            "Montgomery redemption watch: CAPTCHA_API_KEY not set — skipping %d case(s). "
            "Add CAPTCHA_API_KEY=... to .env to enable.",
            len(case_numbers),
        )
        return {}

    session = _build_montgomery_session()
    if not _bootstrap_montgomery(session):
        logger.warning("Montgomery redemption watch: failed to bootstrap PRO session")
        return {}

    token = _solve_montgomery_captcha()
    if not token:
        logger.warning("Montgomery redemption watch: CAPTCHA solve failed")
        return {}

    # Resolve case_id for each case_number via a date-range search.
    # MoCo PRO's search accepts a single case_number filter — for large
    # batches we'd want to batch by date range instead, but per-case is
    # simpler and reliable for the volumes we expect (≤25 redemption
    # records per county per day).
    case_id_map = _montgomery_resolve_case_ids(session, case_numbers, token)
    if not case_id_map:
        logger.warning(
            "Montgomery redemption watch: 0/%d cases resolved to case_ids",
            len(case_numbers),
        )
        return {}

    out: dict[str, RedemptionStatus] = {}
    today = date.today()

    for case_number, case_id in case_id_map.items():
        try:
            html = _montgomery_fetch_case_html(session, case_id)
        except requests.RequestException as e:
            logger.debug(
                "Montgomery redemption watch: case %s fetch failed: %s",
                case_number, e,
            )
            continue
        _sleep()
        if not html:
            continue

        sale_held, hearing_date, confirmed = _parse_docket_events(html)
        status = _compute_status(sale_held, hearing_date, confirmed, today)
        if status is None:
            continue
        out[case_number] = status

    logger.info(
        "Montgomery redemption watch: %d updates from %d cases (%d resolved)",
        len(out), len(case_numbers), len(case_id_map),
    )
    return out


def _build_montgomery_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": MONTGOMERY_LANDING,
    })
    return s


def _bootstrap_montgomery(session: requests.Session) -> bool:
    """Replicate the SPA's GET-init → accept-disclaimer dance."""
    try:
        session.get(MONTGOMERY_LANDING, timeout=20)
    except requests.RequestException:
        pass

    try:
        session.get(MONTGOMERY_INITIALIZE, timeout=20)
    except requests.RequestException:
        pass

    try:
        session.post(
            MONTGOMERY_DISCLAIMER,
            data={"disclaimer": "true"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=20,
        )
        return True
    except requests.RequestException as e:
        logger.warning("Montgomery acceptDisclaimer failed: %s", e)
        return False


def _solve_montgomery_captcha() -> str | None:
    """Mint a reCAPTCHA v3 token via 2Captcha. Returns token or None."""
    try:
        from twocaptcha import TwoCaptcha
    except ImportError as e:
        logger.error("twocaptcha-python not installed: %s", e)
        return None

    solver = TwoCaptcha(config.CAPTCHA_API_KEY)
    for attempt in range(config.MAX_RETRIES):
        try:
            logger.debug("Montgomery: requesting reCAPTCHA v3 token (attempt %d)", attempt + 1)
            result = solver.recaptcha(
                sitekey=_MONTGOMERY_RECAPTCHA_SITEKEY,
                url=MONTGOMERY_LANDING,
                version="v3",
                action=_MONTGOMERY_RECAPTCHA_ACTION,
                score=0.3,
            )
            token = result.get("code", "") if isinstance(result, dict) else ""
            if token:
                return token
        except Exception as e:
            logger.warning("Montgomery CAPTCHA attempt %d failed: %s", attempt + 1, e)
            _sleep()
    return None


def _montgomery_resolve_case_ids(
    session: requests.Session,
    case_numbers: list[str],
    token: str,
) -> dict[str, str]:
    """For each case_number, hit the PRO search by case_number → extract case_id.

    Returns map case_number → case_id. Cases that don't resolve are dropped.
    """
    out: dict[str, str] = {}
    # Reasonable date window — cases stay searchable for years; we use a
    # 5-year window to be safe for older redemption-window cases that
    # might still be open (rare but possible on continuances).
    today = date.today()
    begin = date(today.year - 5, today.month, today.day)
    for case_number in case_numbers:
        data = {
            "case_number": case_number,
            "last_name": "",
            "first_name": "",
            "company_name": "",
            "ticket_number": "",
            "gen_case_type": "",   # ALL types — redemption-window cases are CV / FORC, not LP
            "gen_action_type": "",
            "begin_date": begin.isoformat(),
            "end_date": today.isoformat(),
            "searchType": "general",
            "captchaToken": token,
        }
        try:
            _sleep()
            resp = session.post(
                MONTGOMERY_SEARCH,
                data=data,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=45,
            )
            if resp.status_code != 200:
                logger.debug("Montgomery search %s → %d", case_number, resp.status_code)
                continue
            case_id = _extract_first_case_id(resp.text)
            if case_id:
                out[case_number] = case_id
            else:
                logger.debug("Montgomery search %s → no case_id matched", case_number)
        except requests.RequestException as e:
            logger.debug("Montgomery search %s raised %s", case_number, e)
    return out


def _extract_first_case_id(html: str) -> str:
    """Pull the first case_id out of a PRO search results HTML blob.

    The SPA renders results as <tr onclick="loadCaseInformation('CASE_ID', ...)">
    rows. We grab the first match.
    """
    m = re.search(
        r"loadCaseInformation\(\s*['\"]([^'\"]+)['\"]",
        html,
    )
    return m.group(1) if m else ""


def _montgomery_fetch_case_html(session: requests.Session, case_id: str) -> str:
    """POST caseInformation.aspx with the resolved case_id, return HTML."""
    data = {
        "case_id": case_id,
        "screen": "docket",
    }
    resp = session.post(
        MONTGOMERY_CASE_INFO,
        data=data,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=45,
    )
    if resp.status_code != 200:
        return ""
    return resp.text


# ── Greene (JWorks) ────────────────────────────────────────────────────

def _watch_greene(case_numbers: list[str]) -> dict[str, RedemptionStatus]:
    """Greene JWorks docket — gated on portal re-enablement.

    Per CLAUDE.md: Greene's JWorks public Case Search is currently disabled
    (portal `licenseEnabled=false`). Until the court re-enables, this is a
    sentinel that returns empty. Same gating that the lis pendens scraper
    implements.
    """
    if not case_numbers:
        return {}
    logger.warning(
        "Greene redemption watch: %d case(s) deferred — JWorks public Case Search "
        "currently disabled (portal licenseEnabled=false)",
        len(case_numbers),
    )
    return {}


# ── Docket parsing ─────────────────────────────────────────────────────

def _parse_docket_events(html: str) -> tuple[date | None, date | None, date | None]:
    """Scan a case detail HTML for redemption-relevant docket events.

    Returns (sale_held_date, confirmation_hearing_date, sale_confirmed_date).
    Each is None if not found. Multiple matches → most recent wins.
    """
    soup = BeautifulSoup(html, "html.parser")
    # The docket renders as a labeled section. Gather every (date, text) pair
    # that looks like a docket entry, then classify each.
    text = soup.get_text("\n", strip=True)

    sale_held: date | None = None
    hearing: date | None = None
    confirmed: date | None = None

    # Walk all (date, event) pairs in the page text
    for m in _DOCKET_LINE_RE.finditer(text):
        try:
            event_date = datetime.strptime(m.group("date"), "%m/%d/%Y").date()
        except ValueError:
            continue
        event_text = m.group("event").upper()

        if _SALE_CONFIRMED_RE.search(event_text):
            if confirmed is None or event_date > confirmed:
                confirmed = event_date
        elif _CONFIRMATION_HEARING_RE.search(event_text):
            if hearing is None or event_date > hearing:
                hearing = event_date
        elif _SALE_HELD_RE.search(event_text):
            if sale_held is None or event_date > sale_held:
                sale_held = event_date

    return sale_held, hearing, confirmed


def _compute_status(
    sale_held: date | None,
    hearing: date | None,
    confirmed: date | None,
    today: date,
) -> RedemptionStatus | None:
    """Compute redemption_window_status from docket events.

    Logic:
      - confirmed → status="closed"; days_remaining=0
      - sale_held + (hearing in future) → status="open" or "closing"
            (closing if days_remaining ≤ CLOSING_THRESHOLD_DAYS)
      - sale_held + no hearing date → status="open"; days_remaining blank
      - no sale_held → return None (no update)
    """
    if confirmed is not None:
        return RedemptionStatus(
            case_number="",  # filled in by caller via dict key, but kept blank here
            sale_held_date=sale_held.isoformat() if sale_held else "",
            confirmation_hearing_date=hearing.isoformat() if hearing else confirmed.isoformat(),
            status="closed",
            days_remaining="0",
        )

    if sale_held is None:
        return None

    if hearing is None:
        return RedemptionStatus(
            case_number="",
            sale_held_date=sale_held.isoformat(),
            confirmation_hearing_date="",
            status="open",
            days_remaining="",
        )

    days_remaining = max(0, (hearing - today).days)
    status = "closing" if days_remaining <= CLOSING_THRESHOLD_DAYS else "open"
    return RedemptionStatus(
        case_number="",
        sale_held_date=sale_held.isoformat(),
        confirmation_hearing_date=hearing.isoformat(),
        status=status,
        days_remaining=str(days_remaining),
    )


# ── Helpers ────────────────────────────────────────────────────────────

def _parse_franklin_case_number(case_number: str) -> tuple[str, str, str] | None:
    """Split a normalized Franklin case number ("24CV003703") into (year, type, seq).

    Returns None if the format doesn't match.
    """
    m = re.fullmatch(r"(\d{2})(CV|CR|JV|DR|PR)(\d{4,8})", case_number.upper())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3).zfill(6)


def _sleep() -> None:
    delay = random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX)
    time.sleep(delay)


# ── Standalone test harness ────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Test redemption watcher")
    parser.add_argument(
        "--case",
        action="append",
        help='Case number to test (e.g. "Franklin:24CV003703"). Repeat for multiple.',
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.case:
        print("Usage: python -m redemption_watcher --case 'Franklin:24CV003703' [--case ...]")
        raise SystemExit(1)

    test_records: list[NoticeData] = []
    for spec in args.case:
        county, _, case_number = spec.partition(":")
        test_records.append(NoticeData(
            notice_type="foreclosure",
            county=county,
            case_number=case_number,
        ))

    updated = run_redemption_watch(test_records)
    for r in updated:
        print(
            f"{r.county:12} case={r.case_number:14} "
            f"sale_held={r.sheriff_sale_held_date or '(none)':12} "
            f"hearing={r.confirmation_hearing_date or '(none)':12} "
            f"status={r.redemption_window_status or '(none)':8} "
            f"days_left={r.redemption_window_days_remaining or '-'}"
        )
