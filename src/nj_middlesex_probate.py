"""Middlesex County NJ surrogate probate scraper — Bluestone public portal.

Site: https://surrogatesearch.co.middlesex.nj.us/SurrogateSearch/default.aspx
- No login, no captcha, no rate-limiting seen in probes
- ASP.NET WebForms + DevExpress controls (dxe*/dxgv*/dxflGroupBox)
- Search form requires ≥1 non-default filter (banner enforces it); the
  Death Date field works as a single-day filter that returns all probates
  for decedents who died on that exact date — so we loop day-by-day
- Detail pages have stable GET URLs:
  /WebPages/web_case_detail_middlesex.aspx?Q_PK_ID=NNN
  (no ViewState required) — fetch with plain requests
- Flow: Playwright submits one search per day, scrapes grid → collect
  detail URLs, then requests.get each detail page concurrently to extract
  decedent mailing address + executor name/relation

Produces NoticeData with:
  notice_type="probate", county="Middlesex"
  owner_name=<executor>, decedent_name=<decedent>
  address/city/state/zip=<decedent's mailing address>  (usually the property)
  decision_maker_name=<executor>, decision_maker_relationship=<spouse|child|...>
  dm_confidence="high" (court-named)
  raw_text="Docket: N | Case: Probate | Age: N | Filed: DATE | Relation: R"

Middlesex only. Other NJ counties use different surrogate software.
"""

import asyncio
import html as html_lib
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.async_api import Page, async_playwright

from config import OUTPUT_DIR
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

BLUESTONE_BASE = "https://surrogatesearch.co.middlesex.nj.us/SurrogateSearch/"
BLUESTONE_SEARCH_URL = f"{BLUESTONE_BASE}default.aspx"
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
# of position — robust to column reordering.
_DETAIL_LINK_RE = re.compile(
    r'href="([^"]*web_case_detail_middlesex\.aspx\?Q_PK_ID=(\d+))"',
    re.IGNORECASE,
)
_GRID_ROW_RE = re.compile(
    r'<tr id="[^"]*ASPxGridView_search_DXDataRow\d+"[^>]*>(.*?)</tr>',
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


def _parse_grid_rows(html: str) -> list[dict]:
    """Extract per-row dicts from the search-results grid HTML.

    Cells carry `column="<col>"` attributes — parse by name, not position.
    """
    records = []
    for row_m in _GRID_ROW_RE.finditer(html):
        inner = row_m.group(1)
        link_m = _DETAIL_LINK_RE.search(inner)
        if not link_m:
            continue
        detail_path, pk_id = link_m.group(1), link_m.group(2)
        detail_url = urljoin(BLUESTONE_BASE, detail_path.lstrip("/"))

        rec: dict = {"pk_id": pk_id, "detail_url": detail_url}
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


def _build_notice(grid_row: dict, detail_fields: dict, parties: list[dict]) -> NoticeData | None:
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

    notice = NoticeData(
        date_added=filed_iso or datetime.now().strftime("%Y-%m-%d"),
        address=addr,
        city=city,
        state="NJ",
        zip=zip_code,
        owner_name=executor_name or decedent_name,
        notice_type="probate",
        county="Middlesex",
        source_url=f"{BLUESTONE_BASE}WebPages/web_case_detail_middlesex.aspx?Q_PK_ID={grid_row['pk_id']}",
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

async def _submit_day(page: Page, day: datetime) -> list[dict]:
    """Run one search for decedents who died on `day`. Returns grid rows."""
    await page.goto(BLUESTONE_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2000)

    date_str = day.strftime("%m/%d/%Y")
    box = page.locator(_DEATH_DATE_INPUT)
    await box.click()
    await box.fill(date_str)
    await page.wait_for_timeout(300)

    await page.click(_SEARCH_BUTTON)
    await page.wait_for_timeout(3500)

    html = await page.content()
    if "No data to display" in html:
        return []
    if "Must Enter a Name" in html:
        logger.warning("Middlesex probate: search rejected for %s — fell through", date_str)
        return []
    return _parse_grid_rows(html)


async def scrape_middlesex_probates(
    days_back: int = 30,
    headless: bool = True,
    max_detail_workers: int = 4,
) -> list[NoticeData]:
    """Scrape probates from Middlesex surrogate for the past `days_back` days.

    Iterates day-by-day on Death Date (can't batch since the filter is
    single-day, not range). Each day returns few rows (typical 0–10).
    """
    logger.info("Middlesex probate: scanning last %d days of death dates", days_back)
    today = datetime.now()

    # 1) Collect grid rows across all days (Playwright)
    all_rows: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()

        for offset in range(days_back):
            day = today - timedelta(days=offset)
            try:
                rows = await _submit_day(page, day)
                if rows:
                    logger.info("  %s: %d probate(s)", day.strftime("%Y-%m-%d"), len(rows))
                all_rows.extend(rows)
            except Exception as e:
                logger.warning("Middlesex: failed %s: %s", day.strftime("%Y-%m-%d"), e)

        await browser.close()

    # Dedup on pk_id (same decedent shouldn't appear twice, but be defensive)
    seen: set[str] = set()
    unique_rows = []
    for r in all_rows:
        if r["pk_id"] not in seen:
            seen.add(r["pk_id"])
            unique_rows.append(r)
    logger.info("Middlesex probate: %d unique rows across %d days",
                len(unique_rows), days_back)

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
                logger.warning("detail fetch failed pk=%s: %s", row["pk_id"], e)
                return None
            return _build_notice(row, fields, parties)

    notices_or_none = await asyncio.gather(*(_fetch_one(r) for r in unique_rows))
    notices = [n for n in notices_or_none if n]
    logger.info("Middlesex probate: built %d NoticeData records (of %d rows)",
                len(notices), len(unique_rows))
    return notices


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
