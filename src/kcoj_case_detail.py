"""KCOJ CourtNet 2.0 case-detail enrichment (Phase 2c).

For KY probate records with a case_number (populated by kcoj_scraper.py),
look up the case's party list via the guest-access CourtNet 2.0 search.
Extracts executor/administrator/fiduciary name (populating ``owner_name``
so downstream Tracerfy/Trestle skip-trace works against the PR) and the
estate attorney name.

Flow:
  1. Navigate to ``https://kcoj.kycourts.net/kyecourts/login/guestlogin``
  2. Solve the reCAPTCHA v2 via 2Captcha (CAPTCHA_API_KEY)
  3. Call ``window.verify(token)`` to trigger the server-side AJAX that
     sets ``.CNEAuthCookie`` and redirects to /CourtNet/Search/Index
  4. For each case: expand the "Search by Case" accordion, select
     Jefferson via the Select2 widget, fill the case number, click
     ``button[name='submit-case-search']``. The search is AJAX-driven
     and updates the page in-place — no navigation.
  5. Parse every ``<section class="(odd|even)Row">`` — each is one
     party on the case. The ``class="case-info-hid"`` input inside
     carries an HTML-entity-encoded JSON blob with partyname, partytype,
     county, casenumber, Party_Id, etc.

Party-type codes seen during recon: AP (Attorney For Plaintiff). Other
expected codes (AD, EX, FI, PE, HE) are pattern-guesses based on probate
terminology — this module treats any of them as executor-like for the
purpose of populating ``owner_name``, and logs unknown codes so the set
can be expanded as they're observed.

Session posture: solve CAPTCHA once per batch, reuse the same Playwright
page across all lookups. kcoj.kycourts.net has a blanket-disallow robots.txt
but records are public under KY Open Records Act; stay polite (~2s between
case lookups, single run/day).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── URLs + behavior knobs ─────────────────────────────────────────────
COURTNET_GUESTLOGIN_URL = "https://kcoj.kycourts.net/kyecourts/login/guestlogin"
COURTNET_SEARCH_URL = "https://kcoj.kycourts.net/CourtNet/Search/Index"
COURTNET_RECAPTCHA_SITEKEY = "6LeSYfwSAAAAALTmOl5RV_gvlPAyhpI6qSZN4Fk4"

# Polite cadence between case lookups in the same session.
COURTNET_DELAY_MIN = 1.5
COURTNET_DELAY_MAX = 2.5

# Party-type codes and their meaning. Codes observed in live CourtNet
# responses on Jefferson KY probate cases (2026-04-22 / 2026-04-23 recon):
#   AP   — Attorney For Plaintiff/Petitioner (estate attorney)
#   P    — Petitioner (files the probate petition; this is the PR in most cases)
#   EE   — Executor / Executrix (explicit executor role; sometimes appears
#          alone without a P row — e.g. 25-P-003797)
#   DEC  — Decedent (deceased person; name confirms the probate subject)
#   PJ   — Unclear. Observed with a person sharing the decedent's surname
#          on 26-P-001544 (ROLAND family) — likely a relative / joint
#          petitioner, NOT a judge. Left as "other" pending more data.
#   AA   — Unclear. Appears alongside AP in several cases (25-P-001477,
#          26-P-000247, 25-P-002387). Possibly "Attorney Ad Litem" /
#          "Associate Attorney". Not the executor.
#   AROP — Unclear. Seen once on 25-P-001477. Possibly
#          "Administrator Respondent Original Plaintiff"?
# Codes not yet observed but plausible based on KY probate terminology:
#   FI — Fiduciary, AD — Administrator, EX — Executor, HE — Heir.
# Broaden these sets as new codes surface in real data.
_EXECUTOR_PARTY_TYPES = {"P", "PE", "EE", "FI", "AD", "EX", "PR", "ADM"}
_ATTORNEY_PARTY_TYPES = {"AP", "AD-P", "ATTY"}
_DECEDENT_PARTY_TYPES = {"DEC", "DE", "DECD"}
_JUDGE_PARTY_TYPES = {"J", "JUDGE"}  # PJ intentionally NOT here — unclear meaning
# Warning-Order-Attorney (Phase 6 / COVER-02): a court-appointed attorney who
# represents UNKNOWN / non-appearing heirs in a lis-pendens or tax-foreclosure —
# a tell that the death surfaced with NO probate party graph (McGarvey, Walker,
# Combs, Cooper, Dorsey, Gonzalez, Herflicker, Rutter, Spencer, Thompson-Hale).
# These are NEVER promoted to owner_name/estate_attorney_name/DM: the WOA cannot
# sell the property — they only signal that the no-probate / unknown-heir branch
# (heir_identifier.identify_heirs) should fire instead of dropping the lead.
_WARNING_ORDER_PARTY_TYPES = {
    "WOA", "WO", "WARNING ORDER ATTORNEY", "WARNING-ORDER ATTORNEY",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ── Response parsing ──────────────────────────────────────────────────

_CASE_INFO_HID_RE = re.compile(
    r'class="case-info-hid"[^>]*value="([^"]+)"',
    re.IGNORECASE,
)
_RESULT_INFO_RE = re.compile(
    r'class="result-info"[^>]*value="([^"]+)"',
    re.IGNORECASE,
)


def _parse_case_info_hid(raw: str) -> dict | None:
    """Decode the HTML-entity-encoded JSON inside a case-info-hid value."""
    try:
        decoded = html.unescape(raw)
        return json.loads(decoded)
    except (json.JSONDecodeError, ValueError):
        return None


def parse_party_sections(search_response_html: str) -> list[dict]:
    """Extract one party dict per party-row from a Search/Search response.

    CourtNet renders one ``<section class="(odd|even)Row">`` per party. The
    richest data lives in a hidden input ``<input class="case-info-hid"
    value="{...JSON...}">`` inside each section. Rather than requiring
    matched section start/end tags (response can be truncated mid-stream
    in bad conditions), iterate every case-info-hid — each is exactly one
    party row. Fall back to result-info if case-info-hid is absent.

    Each returned dict has keys: casenumber, county, countydesc, partyname,
    partytype, Party_Id, casetypecode, court, division. String fields are
    right-trimmed (CourtNet pads fixed-length columns).
    """
    parties: list[dict] = []
    seen_party_ids: set[str] = set()

    # Primary: every case-info-hid is one party row
    for m in _CASE_INFO_HID_RE.finditer(search_response_html):
        data = _parse_case_info_hid(m.group(1))
        if not data:
            continue
        for k, v in list(data.items()):
            if isinstance(v, str):
                data[k] = v.strip()
        pid = str(data.get("Party_Id", "")).strip()
        if pid and pid in seen_party_ids:
            continue
        if pid:
            seen_party_ids.add(pid)
        parties.append(data)

    # Fallback: if no case-info-hid found (unusual), try result-info
    if not parties:
        for m in _RESULT_INFO_RE.finditer(search_response_html):
            data = _parse_case_info_hid(m.group(1))
            if not data:
                continue
            norm = {}
            for k, v in data.items():
                nk = k[0].lower() + k[1:] if k[:1].isupper() else k
                norm[nk] = v.strip() if isinstance(v, str) else v
            parties.append(norm)

    return parties


def _classify_party(pt: str) -> str:
    """Group a party-type code into a coarse category."""
    code = pt.strip().upper()
    if code in _EXECUTOR_PARTY_TYPES:
        return "executor"
    # Check Warning-Order-Attorney BEFORE the generic attorney bucket so a
    # "WOA"/"WO" code is recognized as its own no-probate signal and is NOT
    # swallowed as a plain estate attorney (it must never become DM/owner).
    if code in _WARNING_ORDER_PARTY_TYPES:
        return "warning_order_attorney"
    if code in _ATTORNEY_PARTY_TYPES:
        return "attorney"
    if code in _DECEDENT_PARTY_TYPES:
        return "decedent"
    if code in _JUDGE_PARTY_TYPES:
        return "judge"
    return "other"


def has_warning_order_attorney(notice: NoticeData) -> bool:
    """True when notice.courtnet_party_types contains a Warning-Order-Attorney
    code — i.e. the death surfaced with a court-appointed attorney for unknown
    heirs (a no-probate / unknown-heir tell). Reads the pipe-joined codes that
    apply_parties_to_notice records; never re-parses the party graph."""
    codes = (getattr(notice, "courtnet_party_types", "") or "")
    return any(
        c.strip().upper() in _WARNING_ORDER_PARTY_TYPES
        for c in codes.split("|")
        if c.strip()
    )


def no_usable_party_graph(notice: NoticeData) -> bool:
    """True when CourtNet gave us no one who can SELL — the no-probate branch
    trigger (Phase 6 / COVER-02).

    Fires when owner_name is still blank AND either:
      * courtnet_party_types is empty (0 parties — fresh/just-filed or no probate
        case at all, e.g. McGarvey tax-foreclosure with no probate ever), OR
      * the only meaningful representation is a Warning-Order-Attorney (a normal
        probate would have an executor-class party that filled owner_name).

    A notice with a real executor-filled owner_name returns False, so the branch
    never overwrites a populated DM (T-06-10).
    """
    if (getattr(notice, "owner_name", "") or "").strip():
        return False
    party_codes = (getattr(notice, "courtnet_party_types", "") or "").strip()
    if not party_codes:
        return True
    return has_warning_order_attorney(notice)


# ── Session management (Playwright, headless) ─────────────────────────


async def login_as_guest(page: Page) -> bool:
    """Solve reCAPTCHA and land on /CourtNet/Search/Index.

    Returns True on success. Uses config.CAPTCHA_API_KEY for 2Captcha.
    """
    if not config.CAPTCHA_API_KEY:
        logger.warning("  [CourtNet] CAPTCHA_API_KEY not set — cannot auto-solve")
        return False

    try:
        await page.goto(COURTNET_GUESTLOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
    except Exception as exc:
        logger.warning("  [CourtNet] guestlogin nav failed: %s", exc)
        return False

    # Solve reCAPTCHA via 2Captcha. The sync API is blocking; run in a
    # thread to keep the Playwright event loop responsive. 2Captcha has
    # occasional 5xx outages — retry up to 3 times with a short backoff.
    from twocaptcha import TwoCaptcha
    solver = TwoCaptcha(config.CAPTCHA_API_KEY)
    logger.info("  [CourtNet] solving reCAPTCHA v2 (sitekey=%s...)",
                COURTNET_RECAPTCHA_SITEKEY[:12])
    token = ""
    for attempt in range(1, 4):
        try:
            result = await asyncio.to_thread(
                solver.recaptcha,
                sitekey=COURTNET_RECAPTCHA_SITEKEY,
                url=page.url,
            )
            token = result.get("code") if isinstance(result, dict) else str(result)
            if token:
                break
            logger.warning("  [CourtNet] 2Captcha empty token (attempt %d/3)", attempt)
        except Exception as exc:
            logger.warning("  [CourtNet] 2Captcha error (attempt %d/3): %s", attempt, exc)
        if attempt < 3:
            await asyncio.sleep(5 * attempt)  # 5s, 10s backoff

    if not token:
        logger.warning("  [CourtNet] 2Captcha failed after 3 attempts — aborting login")
        return False

    # Call the page's own verify() callback. That AJAX-POSTs to
    # /kyecourts/login/ValidateCaptcha and on success runs
    # window.location.replace(result.URL).
    try:
        await page.evaluate("(token) => window.verify(token)", token)
    except Exception as exc:
        logger.warning("  [CourtNet] verify(token) call failed: %s", exc)
        return False

    # Wait for redirect to /CourtNet/Search/Index
    try:
        await page.wait_for_url(
            lambda u: "guestlogin" not in u, timeout=15000,
        )
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)
    except Exception:
        logger.warning("  [CourtNet] no redirect after verify() — aborting")
        return False

    logger.info("  [CourtNet] authenticated as guest (session .CNEAuthCookie set)")
    return True


# ── Per-case search ───────────────────────────────────────────────────


async def _ensure_case_accordion_open(page: Page) -> None:
    """Click the 'Search by Case' accordion header if the panel isn't open."""
    expanded = await page.evaluate(
        """() => {
            const panel = document.querySelector('#searchByCase');
            if (!panel) return false;
            return panel.classList.contains('in') || !panel.classList.contains('collapse');
        }"""
    )
    if expanded:
        return
    try:
        await page.locator("a.accordion-toggle:has-text('Search by Case')").click()
        await page.wait_for_timeout(700)
    except Exception as exc:
        logger.debug("  [CourtNet] accordion toggle failed: %s", exc)


async def _select_county_jefferson(page: Page) -> None:
    """Set County=Jefferson on the Case panel. Idempotent."""
    case_panel = page.locator("#searchByCase")
    try:
        # Is Jefferson already selected?
        current = await case_panel.locator(".select2-chosen").first.inner_text(timeout=2000)
        if "jefferson" in current.lower():
            return
    except Exception:
        pass

    try:
        await case_panel.locator(".select2-choice, .select2-container a").first.click()
        await page.wait_for_timeout(400)
        await page.keyboard.type("jefferson", delay=30)
        await page.wait_for_timeout(500)
        await page.locator(".select2-result-label:has-text('JEFFERSON')").first.click()
        await page.wait_for_timeout(400)
    except Exception as exc:
        logger.debug("  [CourtNet] county Select2 failed: %s", exc)


async def _dismiss_bootbox(page: Page) -> None:
    """Close any Bootbox.js modal intercepting pointer events.

    CourtNet pops a 'session expired' / 'search limit' alert as a
    ``<div class="bootbox modal fade bootbox-alert in">``. Left open it
    intercepts every subsequent click and eventually triggers an auto
    /CourtNet/User/GuestLogout redirect. Dismiss proactively before each
    search by clicking the OK button or removing the modal from the DOM.
    """
    try:
        handled = await page.evaluate(
            """() => {
                const modals = document.querySelectorAll('.bootbox.modal.in');
                if (!modals.length) return false;
                // Try clicking the first submit/ok button inside
                for (const m of modals) {
                    const btn = m.querySelector('button.btn-primary, button.btn-default');
                    if (btn) { btn.click(); }
                }
                // Also remove any lingering backdrops
                setTimeout(() => {
                    document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                    document.querySelectorAll('.bootbox.modal.in').forEach(m => m.remove());
                }, 200);
                return true;
            }"""
        )
        if handled:
            await page.wait_for_timeout(400)
    except Exception:
        pass


async def _recover_to_search_page(page: Page) -> bool:
    """If the page navigated away from /CourtNet/Search/Index, put it back.

    Returns True if we're on the search page (or were able to return to it).
    Does NOT re-authenticate — caller must handle the guest-logout case
    by resolving a fresh CAPTCHA + verify() cycle.
    """
    if "CourtNet/Search/Index" in page.url:
        return True
    if "guestlogin" in page.url or "kycourts.gov" in page.url:
        return False  # logged out — caller needs to re-authenticate
    try:
        await page.goto(COURTNET_SEARCH_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        return "CourtNet/Search/Index" in page.url
    except Exception:
        return False


async def search_case(page: Page, case_number: str) -> list[dict]:
    """Run a case-number lookup and return parsed party dicts.

    Captures the /CourtNet/Search/Search AJAX response directly rather
    than scraping the post-render DOM — faster and less flaky.
    """
    # Dismiss any modal left over from the previous search. CourtNet sometimes
    # shows a bootbox alert on session-related events that, if left open,
    # intercepts the next click and eventually triggers an auto-logout.
    await _dismiss_bootbox(page)
    if not await _recover_to_search_page(page):
        logger.warning("  [CourtNet] case %s: session lost — skipping", case_number)
        return []

    await _ensure_case_accordion_open(page)
    await _select_county_jefferson(page)

    # Fill case number (clear any previous value first)
    case_input = page.locator("#searchByCase input[name='SearchCriteria.CaseNumber']")
    await case_input.fill("")
    await case_input.fill(case_number)

    # Listen for the /Search/Search response while submit is clicked
    search_response_html: str | None = None

    async def on_response(resp) -> None:
        nonlocal search_response_html
        if "/CourtNet/Search/Search" in resp.url and resp.request.method == "POST":
            try:
                search_response_html = await resp.text()
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.locator("#searchByCase button[name='submit-case-search']").click()
        # Wait for the Search POST to complete (up to 20s)
        deadline = 20.0
        step = 0.25
        waited = 0.0
        while search_response_html is None and waited < deadline:
            await asyncio.sleep(step)
            waited += step
    finally:
        page.remove_listener("response", on_response)

    if not search_response_html:
        logger.info("  [CourtNet] case %s: no Search response captured", case_number)
        return []

    parties = parse_party_sections(search_response_html)
    return parties


# ── Apply parties to NoticeData ───────────────────────────────────────


def _clean_party_name(raw: str) -> str:
    """Strip trailing padding, commas, fixed-width noise from a party name."""
    s = re.sub(r"\s+", " ", raw).strip()
    s = s.rstrip(", ")
    return s


def _title_case_party(name: str) -> str:
    if name.isupper():
        return name.title()
    return name


def apply_parties_to_notice(notice: NoticeData, parties: list[dict]) -> None:
    """Populate notice.owner_name / estate_attorney_name from parsed parties.

    Priority for owner_name: first executor-classed party wins. Attorney
    populated independently. All observed party-type codes recorded in
    ``courtnet_party_types`` for later analysis of unknown codes.
    """
    if not parties:
        return

    # ── Title-path awareness (Phase 2f) ──────────────────────────────
    # The CourtNet executor governs only the personal estate; the person who
    # can actually SELL is decided by title. Read notice.title_path (set by
    # Step 3f / kentucky_title_classifier) to gate the executor->DM overwrite:
    #   * successor_trustee / surviving_owner → a title-derived DM is already
    #     in place; keep it (locked decision 1 — title overrides executor as
    #     DM source). The executor is still captured into owner_name only.
    #   * out_of_estate / no_property → flagged for drop by Phase 4; do NOT
    #     silently name a DM here.
    #   * trustee_unconfirmed → successor_trustee but no recoverable trustee:
    #     intentionally falls back to the executor-as-DM path (locked
    #     decision 3, Smith-Charles), so it is EXCLUDED from title_derived_dm.
    tp = (notice.title_path or "").strip()
    title_derived_dm = (
        tp in ("successor_trustee", "surviving_owner")
        and not notice.trustee_unconfirmed.strip()
    )
    skip_courtnet_dm = tp in ("out_of_estate", "no_property")

    seen_codes: list[str] = []
    executor_set = False
    attorney_set = False
    decedent_set = False

    for p in parties:
        name = _clean_party_name(p.get("partyname", ""))
        ptype = (p.get("partytype") or "").strip().upper()
        if ptype and ptype not in seen_codes:
            seen_codes.append(ptype)
        if not name:
            continue

        category = _classify_party(ptype)
        if category == "executor" and not executor_set and not notice.owner_name.strip():
            executor_name = _title_case_party(name)
            # ALWAYS capture the executor into owner_name — the executor
            # governs the personal estate, so this attorney/personal-estate
            # contact must survive regardless of title path.
            notice.owner_name = executor_name
            # GATE the decision_maker overwrite on title_path. Only let the
            # CourtNet executor become the DM when title did NOT already name
            # one (standard_probate / trustee_unconfirmed) AND this is not an
            # out_of_estate / no_property drop case.
            if title_derived_dm:
                logger.info(
                    "  [CourtNet] case %s: keeping title-derived DM (%s); "
                    "executor %r captured to owner_name only",
                    notice.case_number, tp, executor_name,
                )
            elif skip_courtnet_dm:
                logger.info(
                    "  [CourtNet] case %s: title_path=%s — skipping executor->DM "
                    "assignment (flagged for drop)",
                    notice.case_number, tp,
                )
            elif not notice.decision_maker_name.strip():
                # standard_probate (or trustee_unconfirmed fallback): the
                # executor IS the DM. Populate decision_maker_name so
                # downstream consumers (DataSift CSV "Decision Maker" column,
                # deep-prospecting, ranked-DM logic) see the executor without
                # depending on a later obituary-enricher pass.
                notice.decision_maker_name = executor_name
                notice.decision_maker_relationship = "executor"
                notice.decision_maker_status = "verified_living"
                notice.decision_maker_source = "courtnet_petitioner"
                notice.dm_confidence = "high"
                notice.dm_confidence_reason = (
                    f"named in court record (CourtNet party type {ptype})"
                )
            logger.info(
                "  [CourtNet] case %s: owner_name <- %r (party type %s)",
                notice.case_number, notice.owner_name, ptype,
            )
            executor_set = True
        elif category == "attorney" and not attorney_set:
            notice.estate_attorney_name = _title_case_party(name)
            logger.info(
                "  [CourtNet] case %s: estate_attorney <- %r (party type %s)",
                notice.case_number, notice.estate_attorney_name, ptype,
            )
            attorney_set = True
        elif category == "decedent" and not decedent_set and not notice.decedent_name.strip():
            # Backfill decedent_name when a prior stage (e.g. the KCOJ docket
            # scraper) didn't supply one. DEC party rows carry the decedent's
            # name in "LAST, FIRST MIDDLE" format.
            notice.decedent_name = name.upper()  # match KCOJ docket casing
            logger.info(
                "  [CourtNet] case %s: decedent_name <- %r (party type %s)",
                notice.case_number, notice.decedent_name, ptype,
            )
            decedent_set = True
        elif category == "warning_order_attorney":
            # Recorded in courtnet_party_types (above) but DELIBERATELY NOT
            # promoted to owner_name/estate_attorney_name/DM — a Warning-Order-
            # Attorney represents unknown heirs and cannot sell. The no-probate
            # branch (Step 9.5 / heir_identifier) reads the recorded WOA code and
            # routes the lead to heir identification instead of dropping it.
            logger.info(
                "  [CourtNet] case %s: Warning-Order-Attorney %r (type=%s) — "
                "no-probate signal, NOT promoted to DM/owner",
                notice.case_number, name, ptype,
            )
        elif category == "other":
            logger.debug(
                "  [CourtNet] case %s: unclassified party %r (type=%s)",
                notice.case_number, name, ptype,
            )

    if seen_codes:
        notice.courtnet_party_types = "|".join(seen_codes)


# ── Public entry point ────────────────────────────────────────────────


async def enrich_case_parties(
    notices: list[NoticeData],
    repoll_queue: dict | None = None,
) -> None:
    """Populate owner_name / estate_attorney_name on Jefferson probate
    records that have a case_number.

    Solves the reCAPTCHA once, then iterates all candidates in a single
    browser session.

    Re-poll queue (Phase 6 / COVER-01): a just-filed case whose CourtNet
    search returns 0 parties is not yet indexed — instead of silently
    dropping it, enqueue it for re-search after a short delay when an
    opt-in ``repoll_queue`` dict is passed. ``repoll_queue=None`` (the
    default) leaves behavior unchanged for existing callers/tests.
    """
    candidates = [
        n for n in notices
        if n.notice_type == "probate"
        and n.county.lower() == "jefferson"
        and n.case_number.strip()
        and not n.owner_name.strip()  # only fill if we don't already have one
    ]
    if not candidates:
        return

    logger.info(
        "  [CourtNet] Phase 2c case-detail lookup for %d case(s)",
        len(candidates),
    )

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx: BrowserContext = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=USER_AGENT,
        )
        page = await ctx.new_page()

        try:
            if not await login_as_guest(page):
                logger.warning(
                    "  [CourtNet] guest login failed — skipping all %d candidates",
                    len(candidates),
                )
                return

            consecutive_failures = 0
            for notice in candidates:
                try:
                    # Session can be invalidated mid-batch by a CourtNet
                    # session-expired modal that triggers auto-logout.
                    # search_case's _recover_to_search_page catches the URL
                    # drift but can't re-auth on its own — if we detect the
                    # session is gone, solve a fresh CAPTCHA and continue.
                    if ("guestlogin" in page.url or "kycourts.gov" in page.url
                            or "CourtNet" not in page.url):
                        logger.info(
                            "  [CourtNet] session lost mid-batch — re-authenticating",
                        )
                        if not await login_as_guest(page):
                            logger.warning(
                                "  [CourtNet] re-auth failed — aborting remaining %d cases",
                                len(candidates) - candidates.index(notice),
                            )
                            break

                    parties = await search_case(page, notice.case_number)
                    apply_parties_to_notice(notice, parties)
                    logger.info(
                        "  [CourtNet] case %s: %d part(y/ies) parsed",
                        notice.case_number, len(parties),
                    )
                    # Re-poll, don't drop, a fresh 0-row case (COVER-01 locked
                    # decision 1). When an opt-in queue is passed and the case is
                    # docket-known (has a case_number to re-search by), enqueue it
                    # for a delayed re-search instead of leaving it silently empty.
                    # enqueue_repoll is idempotent on an existing key (the drain in
                    # 06-03b owns attempt-bumping), so re-enqueuing never resets
                    # progress toward the max-attempts cap.
                    if (
                        not parties
                        and repoll_queue is not None
                        and notice.case_number.strip()
                    ):
                        from kcoj_repoll_queue import enqueue_repoll, make_key
                        enqueue_repoll(
                            repoll_queue, make_key(notice),
                            reason="courtnet_0_parties",
                        )
                        logger.info(
                            "  [CourtNet] case %s: 0 parties — enqueued for re-poll",
                            notice.case_number,
                        )
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    logger.warning(
                        "  [CourtNet] case %s: lookup failed: %s",
                        notice.case_number,
                        str(exc).split("\n")[0][:160],  # keep log lines concise
                    )
                    # After 3 consecutive failures, try re-authenticating —
                    # strong sign the session is dead even if the URL still
                    # looks valid.
                    if consecutive_failures >= 3:
                        logger.info("  [CourtNet] 3 consecutive failures — re-authenticating")
                        if not await login_as_guest(page):
                            logger.warning("  [CourtNet] re-auth failed — stopping")
                            break
                        consecutive_failures = 0
                await asyncio.sleep(random.uniform(COURTNET_DELAY_MIN, COURTNET_DELAY_MAX))
        finally:
            try:
                await ctx.close()
                await browser.close()
            except Exception:
                pass
