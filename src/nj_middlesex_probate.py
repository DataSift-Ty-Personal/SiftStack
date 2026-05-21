"""NJ surrogate probate scraper — Bluestone public portal.

Two NJ counties expose probate filings via the same DevExpress/ASPxGridView
"Bluestone Public View" platform with cosmetic differences (host, sub-path,
detail-page filename suffix). The shared pieces — search form selectors,
grid-row regex, detail-page parser, parties-grid parser — are factored
behind a `BluestoneCountyConfig`.

Coverage today:
  - Middlesex:  https://surrogatesearch.co.middlesex.nj.us/SurrogateSearch/
  - Somerset:   https://csi.co.somerset.nj.us/bluestonepublicview/

Essex + Union surrogate offices have NO online portal — they require
in-person/phone/email requests and are not covered here.

Site characteristics (both counties):
- No login, no captcha, no rate-limiting seen in probes
- ASP.NET WebForms + DevExpress controls (dxe*/dxgv*/dxflGroupBox)
- Search form requires ≥1 non-default filter (banner enforces it); the
  Death Date field works as a single-day filter that returns all probates
  for decedents who died on that exact date — so we loop day-by-day
- Detail pages have stable GET URLs:
  /WebPages/web_case_detail_<county>.aspx?Q_PK_ID=NNN
  (no ViewState required) — fetch with plain requests
- Flow: Playwright submits one search per day, scrapes grid → collect
  detail URLs, then requests.get each detail page concurrently to extract
  decedent mailing address + executor name/relation

Produces NoticeData with:
  notice_type="probate", county=<Middlesex|Somerset>
  owner_name=<executor>, decedent_name=<decedent>
  address/city/state/zip=<decedent's mailing address>  (usually the property)
  decision_maker_name=<executor>, decision_maker_relationship=<spouse|child|...>
  dm_confidence="high" (court-named)
  raw_text="Docket: N | Case: Probate | Age: N | Filed: DATE | Relation: R"
"""

import asyncio
import html as html_lib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.async_api import Page, async_playwright

from config import OUTPUT_DIR
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


class CloudflareBlockError(RuntimeError):
    """Raised when the Bluestone portal returns a Cloudflare bot-block page.

    Bluestone sits behind Cloudflare. Modal's egress IPs occasionally land
    on Cloudflare's bot blocklist (typically triggered by rapid request
    patterns). Detected via the response title containing "Cloudflare" or
    the page being orders of magnitude smaller than the real form.

    Caught by the `_safe()` wrapper in modal_app.py so a CF block on one
    scraper doesn't kill the rest of the parallel weekly run. The block
    is surfaced in the Slack summary; the other scrapers ship as normal.
    """


async def _check_cloudflare_block(page, county_name: str) -> None:
    """Raise CloudflareBlockError if the current page is a CF challenge.

    Fast pre-flight check after the first goto, before we burn 180+ days
    of timeouts hammering a blocked IP. The CF block page weighs ~5KB and
    has 'Cloudflare' in the <title>; the real Bluestone form is ~80KB+.
    """
    title = (await page.title()) or ""
    if "Cloudflare" in title or "Attention Required" in title:
        raise CloudflareBlockError(
            f"{county_name}: Cloudflare blocked the IP (title={title!r}). "
            f"Scraper skipped — other scrapers continue."
        )


@dataclass(frozen=True)
class BluestoneCountyConfig:
    """Per-county Bluestone parameters. Everything else is shared."""
    name: str               # "Middlesex" / "Somerset"
    base_url: str           # ends in '/' — e.g. "https://csi.co.somerset.nj.us/bluestonepublicview/"
    detail_aspx: str        # "web_case_detail_<county>.aspx"
    # Path appended to base_url for the search-form page. Middlesex serves
    # it at default.aspx; Somerset's IIS app serves the form on the root
    # (`/bluestonepublicview/`) and 404s when default.aspx is requested
    # explicitly. So this is per-county.
    search_path: str = "default.aspx"
    # Per-county form selectors. Middlesex's "to_date_I" Death Date input is
    # always visible; Somerset's form is collapsed by default and uses a
    # different ID prefix entirely (SEARCHLAYOUT_*).
    death_date_input_selector: str = (
        "#ContentPlaceHolder1_ASPxSplitterDefaultMain_ASPxTextBox_to_date_I"
    )
    show_dates_button_selector: str = ""  # "" = no toggle needed (Middlesex)
    search_button_selector: str = (
        "#ContentPlaceHolder1_ASPxSplitterDefaultMain_ASPxButton_search"
    )

    @property
    def search_url(self) -> str:
        return f"{self.base_url}{self.search_path}"

    @property
    def detail_link_re(self) -> "re.Pattern[str]":
        return re.compile(
            rf'href="([^"]*{re.escape(self.detail_aspx)}\?Q_PK_ID=(\d+))"',
            re.IGNORECASE,
        )

    def build_source_url(self, pk_id: str) -> str:
        return f"{self.base_url}WebPages/{self.detail_aspx}?Q_PK_ID={pk_id}"


MIDDLESEX = BluestoneCountyConfig(
    name="Middlesex",
    base_url="https://surrogatesearch.co.middlesex.nj.us/SurrogateSearch/",
    detail_aspx="web_case_detail_middlesex.aspx",
)
SOMERSET = BluestoneCountyConfig(
    name="Somerset",
    base_url="https://csi.co.somerset.nj.us/bluestonepublicview/",
    # Bluestone follows a `web_case_detail_<county>` naming convention.
    detail_aspx="web_case_detail_somerset.aspx",
    # Somerset's IIS app serves the search form at the root path itself —
    # default.aspx is a 404. Form action attribute is "./" (root).
    search_path="",
    # Somerset's form uses a completely different DevExpress layout
    # ("SEARCHLAYOUT" prefix) than Middlesex's "ASPxSplitterDefaultMain".
    # Date filter is collapsed by default; clicking "Show Dates" reveals it.
    # The literal IDs nest deeply, so we use [id$="..."] suffix selectors.
    death_date_input_selector='[id$="SEARCHLAYOUT_to_date_I"]',
    show_dates_button_selector='[id$="SEARCHLAYOUT_button_show_dates"]',
    search_button_selector='[id$="SEARCHLAYOUT_button_search"]',
)
ALL_BLUESTONE_COUNTIES: tuple[BluestoneCountyConfig, ...] = (MIDDLESEX, SOMERSET)


# Backwards-compat aliases — Middlesex constants used to be module-level
# globals; some downstream code may reference them.
BLUESTONE_BASE = MIDDLESEX.base_url
BLUESTONE_SEARCH_URL = MIDDLESEX.search_url
BLUESTONE_DOWNLOAD_DIR = OUTPUT_DIR / "nj_downloads"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DETAIL_TIMEOUT = 30

# Search form element IDs
_DEATH_DATE_INPUT = "#ContentPlaceHolder1_ASPxSplitterDefaultMain_ASPxTextBox_to_date_I"
_BIRTH_DATE_INPUT = "#ContentPlaceHolder1_ASPxSplitterDefaultMain_ASPxTextBox_from_date_I"
_SEARCH_BUTTON = "#ContentPlaceHolder1_ASPxSplitterDefaultMain_ASPxButton_search"

# Grid row: <a class="dxeHyperlink" href="...Q_PK_ID=N"> wraps an <img> (not
# text — the decedent name sits in a separate <td column="full_name"> cell).
# Cells carry explicit column="<col>" attrs so we can parse by name instead
# of position — robust to column reordering. The detail-link regex is built
# per-county (filename varies) — see BluestoneCountyConfig.detail_link_re.
_GRID_ROW_RE = re.compile(
    r'<tr id="[^"]*ASPxGridView_search_DXDataRow(\d+)"[^>]*>(.*?)</tr>',
    re.DOTALL,
)
_CELL_WITH_COL_RE = re.compile(
    r'<td[^>]*\bcolumn="([^"]+)"[^>]*>(.*?)</td>',
    re.DOTALL,
)

# Detail page field: <label for="ID">LABEL:</label> … <input id="ID" value="V">
_DETAIL_FIELD_RE = re.compile(
    r'<label[^>]+for="([^"]+)"[^>]*>([^<:]+):</label>\s*(?:[^<]|<(?!input))*?<input[^>]+id="\1"[^>]+value="([^"]*)"',
    re.IGNORECASE | re.DOTALL,
)

# Detail page parties grid (hidden tab): ASPxGridView2 rows.
# Row cells in order: Name | Type | Relation | Status.
# The parties grid lives inside nested <table>s (loading panels, headers),
# so scoping via <table>...</table> is fragile. Just match the unique row IDs.
_PARTY_ROW_RE = re.compile(
    r'<tr id="[^"]*ASPxGridView2_DXDataRow\d+"[^>]*>(.*?)</tr>',
    re.DOTALL,
)


def _clean(s: str) -> str:
    s = re.sub(r'<[^>]+>', ' ', s or "")
    s = html_lib.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


_COL_ALIASES = {
    "full_name": "decedent_name",
    "instr_num": "docket",
    "ix_data_2": "case_desc",
    "name_1_township": "town",
    "ix_date_1": "filed",
    "ix_date_5": "issued",
    "ix_date_2": "dod",
    "ix_date_4": "dob",
}


def _parse_grid_rows(html: str, cfg: BluestoneCountyConfig,
                     require_detail_link: bool = True) -> list[dict]:
    """Extract per-row dicts from the search-results grid HTML.

    Cells carry `column="<col>"` attributes — parse by name, not position.

    Middlesex grid rows have a `web_case_detail_*.aspx?Q_PK_ID=N` href and
    we use that pk_id as the unique identifier + stable detail URL. Somerset
    rows have only postback buttons (no GET URL), so callers handling
    Somerset pass `require_detail_link=False` and we synthesize a pk_id
    from the docket / row position.
    """
    records = []
    detail_link_re = cfg.detail_link_re
    for row_m in _GRID_ROW_RE.finditer(html):
        # Group 1 is now the actual DXDataRow{N} index — DevExpress numbers
        # rows globally across pages (page 2 has rows 20, 21, 22 not 0, 1, 2),
        # and the per-row click button IDs use that same number, so we have
        # to extract it instead of using enumerate() — otherwise pagination
        # row clicks miss every row past page 1.
        row_idx = int(row_m.group(1))
        inner = row_m.group(2)
        link_m = detail_link_re.search(inner)
        if link_m:
            detail_path, pk_id = link_m.group(1), link_m.group(2)
            detail_url = urljoin(cfg.base_url, detail_path.lstrip("/"))
        elif require_detail_link:
            continue
        else:
            # Synthesize a pk_id — we'll look the docket up in cells below
            # and set it after the cell loop.
            pk_id = ""
            detail_url = ""

        rec: dict = {"pk_id": pk_id, "detail_url": detail_url, "row_idx": row_idx}
        for col, value in _CELL_WITH_COL_RE.findall(inner):
            alias = _COL_ALIASES.get(col)
            if alias:
                rec[alias] = _clean(value)
        # Guarantee keys the downstream code reads
        rec.setdefault("decedent_name", "")
        rec.setdefault("docket", "")
        rec.setdefault("case_desc", "")
        rec.setdefault("town", "")
        rec.setdefault("filed", "")
        rec.setdefault("issued", "")
        rec.setdefault("dod", "")
        rec.setdefault("dob", "")
        # Backfill synthesized pk_id with docket — gives us a stable dedup
        # key for Somerset rows even without a Q_PK_ID in the URL.
        if not rec["pk_id"]:
            rec["pk_id"] = rec.get("docket") or f"row_{row_idx}"
        records.append(rec)
    return records


def _parse_detail_fields(html: str) -> dict:
    """Extract {label → value} pairs from the detail page's form layout."""
    fields: dict = {}
    for m in _DETAIL_FIELD_RE.finditer(html):
        label = _clean(m.group(2)).rstrip(":")
        value = html_lib.unescape(m.group(3)).strip()
        if label and value:
            # Prefer the first occurrence — repeated labels (e.g. Docket appears
            # twice in the doc) keep the header-bar one.
            fields.setdefault(label, value)
    return fields


def _parse_parties(html: str) -> list[dict]:
    """Extract parties (Executor/Legatee/etc) from the hidden parties grid.

    Each row: Name | Type | Relation | Status.
    """
    parties = []
    # Parties grid cells have class="dxgv" but no column= attr — match by tag.
    plain_cell_re = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
    for row_m in _PARTY_ROW_RE.finditer(html):
        cells = [_clean(c) for c in plain_cell_re.findall(row_m.group(1))]
        if len(cells) >= 4:
            parties.append({
                "name": cells[0],
                "type": cells[1],
                "relation": cells[2],
                "status": cells[3],
            })
    return parties


def _pick_executor(parties: list[dict]) -> dict | None:
    """Pick the primary executor/administrator. Prefer type=Executor with
    status=Accept; fall back to Administrator, then any named fiduciary."""
    priorities = [
        lambda p: p["type"].lower() == "executor" and p["status"].lower() == "accept",
        lambda p: p["type"].lower() == "executor",
        lambda p: p["type"].lower() == "administrator",
        lambda p: "fiduciary" in p["type"].lower(),
    ]
    for pred in priorities:
        hit = next((p for p in parties if pred(p)), None)
        if hit:
            return hit
    return None


def _fetch_detail(url: str, session: requests.Session) -> tuple[dict, list[dict]]:
    """GET detail page and parse fields + parties grid."""
    resp = session.get(url, timeout=DETAIL_TIMEOUT)
    resp.raise_for_status()
    return _parse_detail_fields(resp.text), _parse_parties(resp.text)


def _to_iso_date(s: str) -> str:
    """'3/12/2026' or '03/12/2026' → '2026-03-12'. Empty on parse failure."""
    if not s:
        return ""
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _town_to_city(town: str) -> str:
    """'South Plainfield Borough' → 'South Plainfield'.
    NJ municipal suffixes ('Borough', 'Township', 'City') aren't part of the
    postal city name that geocoders/Zillow expect."""
    if not town:
        return ""
    tokens = town.strip().split()
    if tokens and tokens[-1].lower() in {"borough", "township", "city", "town", "village"}:
        tokens = tokens[:-1]
    return " ".join(tokens).strip()


def _build_notice(grid_row: dict, detail_fields: dict, parties: list[dict],
                  cfg: BluestoneCountyConfig) -> NoticeData | None:
    """Merge grid row + detail fields + parties grid into a NoticeData.
    Returns None if the row lacks the minimum usable data."""
    # Pull the decedent's full name in normal spoken order. Grid gives "Last
    # First Mid" with extra spaces; detail has separate Last/First/Mid.
    last = detail_fields.get("Last Name", "").strip()
    first = detail_fields.get("First Name", "").strip()
    mid = detail_fields.get("Mid ID", "").strip()
    decedent_name = " ".join(t for t in (first, mid, last) if t) or grid_row.get("decedent_name", "")

    # Decedent mailing address (usually the target property)
    addr = detail_fields.get("Address", "").strip()
    city = _town_to_city(detail_fields.get("City", "") or grid_row.get("town", ""))
    zip_code = detail_fields.get("Zip", "").strip()

    executor = _pick_executor(parties)
    executor_name = executor["name"] if executor else ""
    executor_relation = executor["relation"] if executor else ""

    filed_iso = _to_iso_date(detail_fields.get("Date Filed", "") or grid_row.get("filed", ""))
    dod_iso = _to_iso_date(detail_fields.get("Date of Death", "") or grid_row.get("dod", ""))

    # Without an address we can't mail — skip. Executor-only records with no
    # decedent address aren't actionable in the SiftStack enrichment pipeline.
    if not addr:
        return None

    raw_bits = [
        f"Docket: {detail_fields.get('Docket #', grid_row.get('docket', ''))}",
        f"Case: {grid_row.get('case_desc', 'Probate')}",
        f"Age: {detail_fields.get('Age', '')}" if detail_fields.get("Age") else "",
        f"Will Pages: {detail_fields.get('Will Pages', '')}" if detail_fields.get("Will Pages") else "",
        f"Relation: {executor_relation}" if executor_relation else "",
    ]
    raw_text = " | ".join(b for b in raw_bits if b)

    # Stable detail URLs only exist on counties that expose Q_PK_ID in the
    # row href (Middlesex). Somerset rows have postback buttons only — fall
    # back to the search URL so source_url still points to a useful place.
    pk_id = grid_row.get("pk_id", "")
    if pk_id and pk_id.isdigit():
        source_url = cfg.build_source_url(pk_id)
    else:
        source_url = cfg.search_url

    notice = NoticeData(
        date_added=filed_iso or datetime.now().strftime("%Y-%m-%d"),
        address=addr,
        city=city,
        state="NJ",
        zip=zip_code,
        owner_name=executor_name or decedent_name,
        notice_type="probate",
        county=cfg.name,
        source_url=source_url,
        raw_text=raw_text,
        decedent_name=decedent_name,
        date_of_death=dod_iso,
    )
    # Deep-prospecting fields the downstream pipeline reads
    if executor_name:
        notice.decision_maker_name = executor_name
        notice.decision_maker_relationship = executor_relation
        notice.decision_maker_source = "court_record"
        notice.dm_confidence = "high"
    return notice


# ---- search-loop driver ----

async def _submit_day(page: Page, day: datetime, cfg: BluestoneCountyConfig) -> list[dict]:
    """Run one search for decedents who died on `day`. Returns grid rows."""
    await page.goto(cfg.search_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2000)

    # CF block check before each day — IPs can get flagged mid-scrape.
    # Propagates CloudflareBlockError up to scrape_bluestone_probates
    # which raises out for Modal to retry on a fresh container.
    await _check_cloudflare_block(page, cfg.name)

    # DevExpress sizes the form's textboxes via post-load JS. On Modal's
    # headless Chromium the layout briefly resolves to offsetWidth=0, which
    # tripped Playwright's actionability wait and timed out at 30s per day.
    # Local Chromium (headed and headless) doesn't hit this race. Wait for
    # the box to have non-zero width before interacting.
    try:
        await page.wait_for_function(
            "(sel) => { const el = document.querySelector(sel); return el && el.offsetWidth > 0; }",
            arg=cfg.death_date_input_selector,
            timeout=15000,
        )
    except Exception as e:
        logger.warning("%s: death-date input never rendered on %s — %s",
                       cfg.name, day.strftime("%Y-%m-%d"), e)
        return []

    # Some Bluestone deployments collapse the date filters by default and
    # require clicking "Show Dates" to reveal the inputs.
    if cfg.show_dates_button_selector:
        try:
            await page.click(cfg.show_dates_button_selector, timeout=8000)
            await page.wait_for_timeout(800)
        except Exception as e:
            logger.warning("%s: show_dates click failed: %s", cfg.name, e)

    date_str = day.strftime("%m/%d/%Y")
    box = page.locator(cfg.death_date_input_selector)
    # fill() handles focus on its own — the standalone click() before it
    # was the 30s actionability timeout in Modal headless. Dropping it.
    await box.fill(date_str)
    await page.wait_for_timeout(300)

    await page.click(cfg.search_button_selector)
    await page.wait_for_timeout(3500)

    html = await page.content()
    if "No data to display" in html:
        return []
    if "Must Enter a Name" in html:
        logger.warning("%s probate: search rejected for %s — fell through", cfg.name, date_str)
        return []
    return _parse_grid_rows(html, cfg)


async def scrape_bluestone_probates(
    cfg: BluestoneCountyConfig,
    days_back: int = 30,
    headless: bool = True,
    max_detail_workers: int = 4,
) -> list[NoticeData]:
    """Scrape probates from a Bluestone-backed county surrogate.

    Iterates day-by-day on Death Date (can't batch since the filter is
    single-day, not range). Each day returns few rows (typical 0–10).
    """
    logger.info("%s probate: scanning last %d days of death dates", cfg.name, days_back)
    today = datetime.now()

    # 1) Collect grid rows across all days (Playwright)
    all_rows: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()

        # Pre-flight CF check — bail fast on blocked IPs instead of
        # burning days_back × ~17s of futile timeouts. Raises out so
        # Modal's function-level retries can rotate the container.
        try:
            await page.goto(cfg.search_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            await _check_cloudflare_block(page, cfg.name)
        except CloudflareBlockError:
            await browser.close()
            raise

        for offset in range(days_back):
            day = today - timedelta(days=offset)
            try:
                rows = await _submit_day(page, day, cfg)
                if rows:
                    logger.info("  %s %s: %d probate(s)", cfg.name, day.strftime("%Y-%m-%d"), len(rows))
                all_rows.extend(rows)
            except CloudflareBlockError:
                # Mid-scrape CF block — IP got flagged partway through.
                # Propagate so Modal retries with a fresh container.
                await browser.close()
                raise
            except Exception as e:
                logger.warning("%s: failed %s: %s", cfg.name, day.strftime("%Y-%m-%d"), e)

        await browser.close()

    # Dedup on pk_id (same decedent shouldn't appear twice, but be defensive)
    seen: set[str] = set()
    unique_rows = []
    for r in all_rows:
        if r["pk_id"] not in seen:
            seen.add(r["pk_id"])
            unique_rows.append(r)
    logger.info("%s probate: %d unique rows across %d days",
                cfg.name, len(unique_rows), days_back)

    if not unique_rows:
        return []

    # 2) Fetch detail pages in parallel via requests (no ViewState needed)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(max_detail_workers)

    async def _fetch_one(row: dict) -> NoticeData | None:
        async with sem:
            try:
                fields, parties = await loop.run_in_executor(
                    None, _fetch_detail, row["detail_url"], session
                )
            except Exception as e:
                logger.warning("%s detail fetch failed pk=%s: %s", cfg.name, row["pk_id"], e)
                return None
            return _build_notice(row, fields, parties, cfg)

    notices_or_none = await asyncio.gather(*(_fetch_one(r) for r in unique_rows))
    notices = [n for n in notices_or_none if n]
    logger.info("%s probate: built %d NoticeData records (of %d rows)",
                cfg.name, len(notices), len(unique_rows))
    return notices


async def scrape_middlesex_probates(
    days_back: int = 30,
    headless: bool = True,
    max_detail_workers: int = 4,
) -> list[NoticeData]:
    """Backwards-compatible wrapper — scrapes Middlesex specifically."""
    return await scrape_bluestone_probates(
        MIDDLESEX, days_back=days_back, headless=headless,
        max_detail_workers=max_detail_workers,
    )


async def scrape_somerset_probates(
    days_back: int = 30,
    headless: bool = True,
    max_detail_workers: int = 4,  # accepted for API parity; unused (one browser session)
) -> list[NoticeData]:
    """Scrape Somerset surrogate probates via the Bluestone Public View portal.

    Somerset's deployment differs from Middlesex's enough that the Middlesex
    "submit day-by-day, then fetch details concurrently via plain requests"
    pattern doesn't apply:
      - Search form uses date *range* dropdowns (Last 5/10/30 Days, monthly
        buckets), not a single-day Date input — so we run ONE search using
        the smallest preset that covers `days_back`, then filter client-side.
      - Grid rows have NO direct detail URLs. The "View Details" button is
        an ASP.NET postback (autoPostBack:true), so detail navigation must
        stay inside the Playwright session.
      - Detail panel exposes decedent info only (Address, City, ZIP, DOB,
        DOD, etc.). Parties/executor info appears to be behind a tab that
        only loads with login. So we don't get court-named executor — the
        downstream obit pipeline reconstructs heirs from obituaries instead
        (same path used for non-probate deceased records).
    """
    cfg = SOMERSET
    logger.info("%s probate: searching last %d days via Bluestone date range", cfg.name, days_back)

    # Pick the smallest predefined range that covers `days_back`. Bluestone's
    # range dropdown options: Today / Yesterday / Last 5 / Last 10 / Last 30
    # Days / monthly + yearly buckets. We use a daily preset for ≤30d and
    # filter client-side; for longer windows the cron lives on Last 30 Days.
    if days_back <= 5:
        range_label = "Last 5 Days"
    elif days_back <= 10:
        range_label = "Last 10 Days"
    else:
        range_label = "Last 30 Days"

    notices: list[NoticeData] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()

        await page.goto(cfg.search_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)

        # CF pre-flight — raise out so Modal can retry on a fresh IP
        # rather than burning 30s on a doomed show_dates click.
        try:
            await _check_cloudflare_block(page, cfg.name)
        except CloudflareBlockError:
            await browser.close()
            raise

        # Reveal the date filter section
        try:
            await page.click(cfg.show_dates_button_selector, timeout=8000)
            await page.wait_for_timeout(1500)
        except Exception as e:
            logger.error("%s: show_dates click failed: %s", cfg.name, e)
            await browser.close()
            return []

        # Date Type = File Date.
        # Earlier we tried "Death Date" — that gave only ~10% overlap with
        # the runner's manual reports because most week's filings cover
        # decedents who died months earlier. Filing date matches the
        # surrogate court's actual workflow.
        await page.locator('[id$="search_create_or_issue_date_B-1"]').first.click()
        await page.wait_for_timeout(700)
        await page.locator(
            '[id$="search_create_or_issue_date_DDD_L_LBT"] td:has-text("File Date")'
        ).first.click()
        await page.wait_for_timeout(800)

        # Range = Last N Days (preset)
        await page.locator('[id$="search_entry_date_range_B-1"]').first.click()
        await page.wait_for_timeout(700)
        await page.locator(
            f'[id$="search_entry_date_range_DDD_L_LBT"] td:has-text("{range_label}")'
        ).first.click()
        await page.wait_for_timeout(800)

        # Submit
        await page.click(cfg.search_button_selector)
        await page.wait_for_timeout(5000)

        notices = await _somerset_extract_records(page, cfg, days_back)

        await browser.close()

    logger.info("%s probate: built %d NoticeData records", cfg.name, len(notices))
    return notices


async def _somerset_extract_records(
    page: Page, cfg: BluestoneCountyConfig, days_back: int,
) -> list[NoticeData]:
    """Walk Somerset search-result rows, click each detail button, parse.

    No client-side DOD cutoff: we filter by File Date at the Bluestone
    level, so DODs in the result set can legitimately stretch back months
    (someone died 6 months ago, filing happens this week). Bluestone's
    range preset is the authority.
    """
    notices: list[NoticeData] = []

    page_idx = 0
    while True:
        page_idx += 1
        # Snapshot the rows on this page (Somerset rows have no detail href —
        # only postback buttons — so accept rows without a detail link).
        html = await page.content()
        rows = _parse_grid_rows(html, cfg, require_detail_link=False)
        if not rows:
            logger.info("%s probate: page %d has no rows — stopping", cfg.name, page_idx)
            break

        logger.info("%s probate: page %d has %d rows", cfg.name, page_idx, len(rows))

        for row in rows:
            # row["row_idx"] is the GLOBAL DevExpress row number (page 2 has
            # 20, 21, 22 etc) — required for the per-row button IDs to match.
            ridx = row["row_idx"]

            # The DevExpress detail button drives an ASP.NET postback via
            # `__doPostBack(uniqueID, '')`. Playwright's normal click()
            # times out on the wrapper div, and a JS .click() on the inner
            # <input> doesn't trigger the ASPxClientButton callback. So we
            # bypass DevExpress entirely: read the input's `name` attribute
            # (which IS the postback uniqueID) and call __doPostBack
            # directly. Same end result, no event-handler chain.
            #
            # Note: button IDs follow `cell{ridx}_29_ASPxButtonViewDAta_{ridx}`,
            # not the `_0` suffix you'd expect. The trailing index matches
            # the row, not the column-button ordinal.
            cnt = await page.locator(f'input[id$="cell{ridx}_29_ASPxButtonViewDAta_{ridx}_I"]').count()
            if cnt == 0:
                logger.warning("%s: row %d input not in DOM (cnt=0) — skipping", cfg.name, ridx)
                continue
            try:
                target = await page.evaluate(
                    """(ridx) => {
                        const input = document.querySelector(
                            `input[id$="cell${ridx}_29_ASPxButtonViewDAta_${ridx}_I"]`
                        );
                        if (!input) return null;
                        const name = input.getAttribute('name');
                        if (!name) return null;
                        if (typeof __doPostBack === 'function') {
                            __doPostBack(name, '');
                            return name;
                        }
                        return null;
                    }""",
                    ridx,
                )
                if not target:
                    logger.warning("%s: row %d __doPostBack target not found", cfg.name, ridx)
                    continue
                # Postback is async — wait for the detail panel to render.
                await page.wait_for_timeout(3500)
            except Exception as e:
                logger.warning("%s: detail postback row %d failed: %s", cfg.name, ridx, e)
                continue

            detail_html = await page.content()
            # Scope the detail-panel parsing to the case_detail_main panel so
            # we don't pick up search-form labels.
            scoped = _isolate_panel(detail_html, "ASPxPanel_case_detail_main")
            fields = _parse_detail_fields(scoped)
            # Somerset public view doesn't expose parties — pass empty list
            notice = _build_notice(row, fields, [], cfg)
            if notice:
                notices.append(notice)
            else:
                # Trace which decedent we couldn't build a NoticeData for —
                # _build_notice returns None when it can't pull a mailing
                # street address from the detail panel. We still want to
                # surface those names so the runner can chase them manually.
                logger.info(
                    "%s: row %d (%s) skipped — no street address in detail panel",
                    cfg.name, ridx,
                    row.get("decedent_name") or "(unknown)",
                )

            # Navigate back to the result grid. The "Search" tab button in
            # the page-control returns us to the list without resubmitting.
            try:
                await page.locator('[id$="ASPxPageControl_Search_hit_T0T"]').first.click(timeout=5000)
                await page.wait_for_timeout(1500)
            except Exception:
                # Fall back: click the back-style button or re-run search
                logger.warning("%s: couldn't return to results — stopping", cfg.name)
                return notices

        # Pagination — Somerset's DXPager uses class-based <a> tags (no
        # stable id). Each visible page-number is `a.dxp-num`; the icon-only
        # next-button is `a.dxp-button.dxp-bi`. The current page's number
        # has class `dxp-current` (no anchor) so by clicking dxp-num that
        # text == (page_idx + 1) we advance one page.
        next_page_text = str(page_idx + 1)
        next_link = page.locator(
            f'[id$="DXPagerBottom"] a.dxp-num:has-text("{next_page_text}")'
        ).first
        try:
            if await next_link.count() == 0:
                logger.info("%s: no page-%s link — pagination done", cfg.name, next_page_text)
                break
            await next_link.click(timeout=5000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning("%s: pagination to page %s failed: %s", cfg.name, next_page_text, e)
            break

    # Output dedup — the detail-postback flow occasionally double-counts a
    # row when navigating back to results re-renders the same case briefly
    # before the grid restores. Key on (decedent_name, address) since pk_id
    # is synthesized from docket and may not be stable across re-renders.
    seen: set[tuple[str, str]] = set()
    deduped: list[NoticeData] = []
    for n in notices:
        key = (n.decedent_name.upper().strip(), n.address.upper().strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    if len(deduped) < len(notices):
        logger.info(
            "%s probate: dedup removed %d duplicate row(s)",
            cfg.name, len(notices) - len(deduped),
        )
    return deduped


def _isolate_panel(html: str, panel_id_substring: str) -> str:
    """Crude best-effort scope to a specific panel by id substring.

    Bluestone HTML doesn't have nested div boundaries we can match
    cleanly with regex, so this just slices from the panel's opening
    tag to the next outer-panel sibling — good enough for the field
    label/input pairing the detail parser cares about.
    """
    start = html.find(panel_id_substring)
    if start < 0:
        return html
    # Walk back to the enclosing tag's start
    open_tag = html.rfind("<", 0, start)
    if open_tag < 0:
        return html
    # Take a generous chunk after the panel start — the detail panel is
    # ~30-50KB. Take the first 200KB to be safe.
    return html[open_tag:open_tag + 200_000]


async def scrape_all_bluestone_probates(
    days_back: int = 30,
    headless: bool = True,
    max_detail_workers: int = 4,
    counties: tuple[BluestoneCountyConfig, ...] = ALL_BLUESTONE_COUNTIES,
) -> list[NoticeData]:
    """Run every supported Bluestone county sequentially. Returns the merged list.

    Sequential rather than parallel — Playwright contention + the per-day
    loop already saturates one browser; running two at once doubles RAM
    without much wall-clock gain on Modal.
    """
    out: list[NoticeData] = []
    for cfg in counties:
        try:
            n = await scrape_bluestone_probates(
                cfg, days_back=days_back, headless=headless,
                max_detail_workers=max_detail_workers,
            )
            out.extend(n)
        except Exception as e:
            logger.error("%s probate scrape failed (other counties continue): %s", cfg.name, e)
    return out


async def run_middlesex_probate_scrape(
    days_back: int = 30,
    headless: bool = True,
    upload_datasift: bool = True,
    notify_slack: bool = True,
    skip_enrichment: list[str] | None = None,
) -> dict:
    """Full pipeline: scrape → enrich → output CSV → optional DataSift + Slack."""
    from data_formatter import write_csv
    from datasift_formatter import auto_week_tag, write_datasift_split_csvs
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    import config

    result: dict = {
        "success": False, "records": 0, "output_csv": "", "message": "",
    }

    notices = await scrape_middlesex_probates(days_back=days_back, headless=headless)
    if not notices:
        result["message"] = f"No probates found in last {days_back} days"
        return result

    # NJ probates: we have the executor (DM) directly from the court record.
    # Obit search runs anyway — the probate_preset path inside obituary_enricher
    # detects (notice_type=probate + decedent_name + owner_name) and uses the
    # court-named executor as DM without overriding from an obit match. This
    # keeps data safe while still surfacing additional obit URLs / heir hints
    # for deeper prospecting.
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        skip_obituary=False,
        skip_ancestry=True,
        skip_dm_address=True,           # executor address needs Tracerfy; leave off by default
        skip_heir_verification=True,
        skip_parcel_lookup=True,
        source_label="Middlesex Surrogate Probate",
    )
    if skip_enrichment:
        for flag in skip_enrichment:
            if hasattr(opts, flag):
                setattr(opts, flag, True)

    enriched = run_enrichment_pipeline(notices, opts)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_path = write_csv(enriched, f"nj_middlesex_probate_{timestamp}.csv")
    result["output_csv"] = str(output_path)
    result["records"] = len(enriched)

    # DataSift upload
    if upload_datasift and config.DATASIFT_EMAIL and config.DATASIFT_PASSWORD:
        from datasift_uploader import upload_to_datasift
        csv_infos = write_datasift_split_csvs(enriched, list_name="")
        upload_result = await upload_to_datasift(
            csv_infos[0]["path"], enrich=True, skip_trace=True,
        )
        result["upload"] = upload_result

    # Slack
    if notify_slack and config.SLACK_WEBHOOK_URL:
        try:
            from slack_notifier import _send_webhook
            week_tag = auto_week_tag("probate")
            msg = (
                f"*Middlesex Probate — {week_tag}*\n"
                f"Last {days_back} days of death dates\n"
                f"Total: {result['records']} records\n"
                f"Output: {output_path.name}"
            )
            if upload_datasift:
                msg += "\nDataSift: uploaded + enrich + skip trace started"
            _send_webhook(msg)
        except Exception as e:
            logger.warning("Slack notification failed: %s", e)

    result["success"] = True
    result["message"] = f"{result['records']} records processed"
    return result


if __name__ == "__main__":
    import json
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    days_back = 30
    headless = True
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days_back = int(arg.split("=", 1)[1])
        elif arg == "--headed":
            headless = False

    r = asyncio.run(run_middlesex_probate_scrape(
        days_back=days_back,
        headless=headless,
        upload_datasift=False,
        notify_slack=False,
    ))
    print(json.dumps(r, indent=2, default=str))
