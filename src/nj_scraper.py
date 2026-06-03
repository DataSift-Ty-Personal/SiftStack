"""NJ Lis Pendens auto-scraper — login + scrape Property Search results.

Site: https://www.njlispendens.com (aMember Pro auth — weekly pre-foreclosure
lis pendens filings for all 21 NJ counties).

Flow:
  1. Login (reuses cookies; falls back to email+password)
  2. Navigate to Property Search with filters as URL params (no form-click needed)
  3. Paginate through results, scrape each record block from the HTML (Docket#,
     File Date, Defendant, Plaintiff, Orig Mortgage, Mortgage Date, Attorney,
     Lot-Block, County, City/State/Zip tail)
  4. Export CSV from the same search — the CSV has the street address in plain
     text (the results HTML renders street as an anti-scrape <img>)
  5. Join CSV rows → HTML records by normalized-name + zip (verified 70/70)
  6. Build NoticeData with all merged fields

Requires NJLISPENDENS_EMAIL / NJLISPENDENS_PASSWORD in .env.
"""

import asyncio
import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import Page, async_playwright

import config
from config import OUTPUT_DIR
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

NJ_LP_BASE_URL = "https://www.njlispendens.com"
NJ_LP_LOGIN_URL = f"{NJ_LP_BASE_URL}/member/login"
NJ_LP_PROPERTY_URL = f"{NJ_LP_BASE_URL}/member/property"
NJ_LP_DOWNLOAD_DIR = OUTPUT_DIR / "nj_downloads"

LOGIN_MAX_RETRIES = 3
LOGIN_RETRY_DELAY = 30

# "Date Added To System" filter values (from the select[name=date_added] options)
DATE_ADDED_WEEK = "7"
DATE_ADDED_2WEEKS = "14"
DATE_ADDED_MONTH = "30"


async def _save_cookies(page: Page) -> None:
    cookies = await page.context.cookies()
    config.NJ_LP_COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
    logger.debug("Saved %d NJ LP cookies", len(cookies))


async def _load_cookies(context) -> bool:
    if not config.NJ_LP_COOKIES_FILE.exists():
        return False
    try:
        cookies = json.loads(config.NJ_LP_COOKIES_FILE.read_text())
        await context.add_cookies(cookies)
        logger.debug("Loaded %d NJ LP cookies", len(cookies))
        return True
    except Exception:
        return False


async def _screenshot(page: Page, name: str) -> None:
    path = OUTPUT_DIR / f"njlp_{name}.png"
    try:
        await page.screenshot(path=str(path))
        logger.debug("Screenshot: %s", path.name)
    except Exception:
        pass


async def _is_logged_in(page: Page) -> bool:
    """Probe the member property page — if the amember login form is absent
    and the property search form is present, we're authenticated."""
    try:
        await page.goto(NJ_LP_PROPERTY_URL, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)
    except Exception:
        return False
    # Check for property search form (authenticated) and absence of login form
    has_property_form = await page.locator('select[name="date_added"]').count() > 0
    has_login_form = await page.locator('input[name="amember_login"]').count() > 0
    return has_property_form and not has_login_form


async def _try_login_once(page: Page, email: str, password: str) -> bool:
    """Single login attempt. Uses explicit aMember field names only — no
    generic input[type=text] fallback (which used to grab Interest_Rate on
    the property page when already authenticated)."""
    await page.goto(NJ_LP_LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    # If we landed somewhere else (already logged in and redirected), succeed.
    if "/member/login" not in page.url:
        return await _is_logged_in(page)

    email_input = page.locator('input[name="amember_login"]')
    password_input = page.locator('input[name="amember_pass"]')
    if await email_input.count() == 0 or await password_input.count() == 0:
        logger.warning("NJ LP: login form fields not found on %s", page.url)
        return False

    await email_input.first.fill(email)
    await password_input.first.fill(password)
    await page.wait_for_timeout(300)

    submit = page.locator('input[type="submit"][value*="Login" i], input[type="submit"][value*="Sign" i]')
    if await submit.count() == 0:
        submit = page.locator('input[type="submit"]')
    await submit.first.click()
    await page.wait_for_timeout(4000)

    ok = await _is_logged_in(page)
    if not ok:
        # Surface what aMember told us so we can distinguish between bad
        # credentials, rate-limit lockout, and a server-side internal error.
        try:
            post_url = page.url
            # aMember surfaces errors in .am-error, .alert-danger, .error, or a
            # generic "Incorrect login/password" body string.
            error_text = ""
            for sel in (".am-error", ".alert-danger", ".am-message", ".error"):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        error_text = (await loc.inner_text())[:200].strip()
                        if error_text:
                            break
                except Exception:
                    continue
            if not error_text:
                # Fall back to grabbing a snippet of visible body text.
                try:
                    body_text = await page.locator("body").inner_text()
                    # Common aMember failure phrases
                    for phrase in (
                        "Incorrect login", "Invalid login", "password",
                        "internal error", "SyntaxError", "locked", "disabled",
                    ):
                        idx = body_text.lower().find(phrase.lower())
                        if idx >= 0:
                            error_text = body_text[max(0, idx - 20):idx + 180].strip()
                            break
                except Exception:
                    pass
            logger.warning(
                "NJ LP login failed — post_url=%s, error=%r",
                post_url, error_text or "(no visible error message)",
            )
        except Exception as e:
            logger.warning("NJ LP login failed (couldn't capture error: %s)", e)
    return ok


async def login(page: Page, email: str = "", password: str = "") -> bool:
    email = email or config.NJLISPENDENS_EMAIL
    password = password or config.NJLISPENDENS_PASSWORD
    if not email or not password:
        logger.error("NJLISPENDENS_EMAIL / NJLISPENDENS_PASSWORD not set")
        return False

    # Skip straight to cookie check
    if await _is_logged_in(page):
        logger.info("NJ LP: already logged in via cookies")
        return True

    for attempt in range(1, LOGIN_MAX_RETRIES + 1):
        try:
            logger.info("NJ LP login attempt %d/%d...", attempt, LOGIN_MAX_RETRIES)
            if await _try_login_once(page, email, password):
                logger.info("NJ LP login successful")
                await _save_cookies(page)
                return True
        except Exception as e:
            logger.warning("NJ LP login attempt %d failed: %s", attempt, e)
            await _screenshot(page, f"login_error_{attempt}")
        if attempt < LOGIN_MAX_RETRIES:
            logger.info("Waiting %ds before retry...", LOGIN_RETRY_DELAY)
            await page.wait_for_timeout(LOGIN_RETRY_DELAY * 1000)

    logger.error("NJ LP login failed after %d attempts", LOGIN_MAX_RETRIES)
    return False


# ---- HTML record parsing ----

# Each record block is wrapped in <div class="mb_div-table"> … </div>. Inside,
# fields sit in <span class="txbld">LABEL:</span> VALUE pairs. The street
# address is served as an anti-scrape <img> (graphicaladdress?pid=...); only
# the city/state/zip tail is in plain text inside a <p> tag after the image.
_FIELD_RE = re.compile(
    r'<span class="txbld">\s*([^<:]+?)\s*:\s*</span>\s*([^<]*)',
    re.IGNORECASE,
)
_BLOCK_RE = re.compile(
    r'<div class="mb_div-table">(.*?)</div>\s*</div>\s*(?:<div class="tx_icons">|<div class="pop-row">|<div class="mb_bot_ad">|$)',
    re.DOTALL,
)
_ADDR_TAIL_RE = re.compile(r'<br>\s*([^<]+?)\s*</p>', re.IGNORECASE)
_PID_RE = re.compile(r'graphicaladdress\?pid=(\d+)', re.IGNORECASE)
_ZIP_RE = re.compile(r'(\d{5})(?:-\d{4})?')
_COUNTER_RE = re.compile(r'(\d+)\s*(?:to|-)\s*(\d+)\s*of\s*(\d+)', re.IGNORECASE)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _normalize_name_key(n: str) -> str:
    """Normalize so HTML 'Last, First' and CSV 'First Last' produce the same
    key. Strip non-alphanumerics, uppercase, split, sort tokens."""
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", n or "")
    tokens = sorted(t.upper() for t in s.split() if t)
    return " ".join(tokens)


def _parse_html_records(html: str) -> list[dict]:
    """Extract per-record dicts from a results page. Returns fields raw —
    no NoticeData construction yet (we need to join with CSV first)."""
    records = []
    for m in _BLOCK_RE.finditer(html):
        block = m.group(1)
        rec: dict = {}
        for fm in _FIELD_RE.finditer(block):
            key = fm.group(1).strip().lower().replace(" ", "_").replace("-", "_")
            rec[key] = _clean(fm.group(2))
        tail = _ADDR_TAIL_RE.search(block)
        if tail:
            rec["addr_tail"] = _clean(tail.group(1))
        pid = _PID_RE.search(block)
        if pid:
            rec["pid"] = pid.group(1)
        if rec.get("defendant") or rec.get("docket_no"):
            records.append(rec)
    return records


def _parse_addr_tail(tail: str) -> tuple[str, str, str]:
    """'Hillside NJ 07205' → ('Hillside', 'NJ', '07205')."""
    if not tail:
        return "", "", ""
    zip_m = _ZIP_RE.search(tail)
    zip_code = zip_m.group(1) if zip_m else ""
    before_zip = tail[: zip_m.start()].strip() if zip_m else tail
    # Strip trailing state
    state = ""
    state_m = re.search(r"\b(NJ|N\.J\.|New Jersey)\s*$", before_zip, re.IGNORECASE)
    if state_m:
        state = "NJ"
        before_zip = before_zip[: state_m.start()].strip()
    city = before_zip
    return city, state, zip_code


def _normalize_defendant_for_display(raw: str) -> str:
    """HTML 'Kizito, Owusu' → 'Owusu Kizito' (spoken order) for consistency
    with the rest of SiftStack. Leaves LLCs / single-token names alone."""
    if "," not in raw:
        return raw
    parts = [p.strip() for p in raw.split(",", 1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        # Skip suffix markers like ", LLC" / ", Inc"
        suffixes = {"llc", "inc", "corp", "co", "ltd", "lp", "llp", "trust", "estate"}
        if parts[1].lower().rstrip(".").strip() in suffixes:
            return f"{parts[0]} {parts[1]}"
        return f"{parts[1]} {parts[0]}"
    return raw


def _build_notice(html_rec: dict, csv_row: dict) -> NoticeData:
    """Merge one HTML record + one CSV row into a NoticeData."""
    city, state, zip_code = _parse_addr_tail(html_rec.get("addr_tail", ""))
    # CSV may have more accurate city casing/spacing — prefer CSV values if present
    csv_city = (csv_row.get("City") or "").strip()
    csv_state = (csv_row.get("State") or "").strip() or "NJ"
    csv_zip = (csv_row.get("Zip") or "").strip()

    owner_name = _normalize_defendant_for_display(html_rec.get("defendant", "").strip()) \
        or (csv_row.get("Name") or "").strip()

    file_date = html_rec.get("file_date", "").strip()
    # file_date is already YYYY-MM-DD from the site
    if file_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", file_date):
        file_date = ""

    docket_no = html_rec.get("docket_no", "").strip()
    pid = html_rec.get("pid", "").strip()

    raw_bits = [
        f"Docket: {docket_no}" if docket_no else "",
        f"Plaintiff: {html_rec.get('plaintiff', '')}" if html_rec.get("plaintiff") else "",
        f"Attorney: {html_rec.get('attorney', '')}" if html_rec.get("attorney") else "",
        f"Attorney Phone: {html_rec.get('attorney_phone', '')}" if html_rec.get("attorney_phone") else "",
        f"Orig Mortgage: ${html_rec.get('orig_mortgage', '')}" if html_rec.get("orig_mortgage") else "",
        f"Mortgage Date: {html_rec.get('mortgage_date', '')}" if html_rec.get("mortgage_date") else "",
        f"Lot-Block: {html_rec.get('lot_block', '')}" if html_rec.get("lot_block") else "",
    ]
    raw_text = " | ".join(bit for bit in raw_bits if bit)

    source_url = f"{NJ_LP_PROPERTY_URL}?pid={pid}" if pid else f"{NJ_LP_PROPERTY_URL}?docket={docket_no}"

    return NoticeData(
        date_added=file_date or datetime.now().strftime("%Y-%m-%d"),
        address=(csv_row.get("Address") or "").strip(),
        city=csv_city or city,
        state=csv_state or state or "NJ",
        zip=csv_zip or zip_code,
        owner_name=owner_name,
        notice_type="foreclosure",
        county=html_rec.get("county", "").strip(),
        source_url=source_url,
        raw_text=raw_text,
    )


# ---- Search flow ----

def _build_search_url(counties: list[str], date_added: str = DATE_ADDED_WEEK,
                      per_page: int = 50, cp: int = 0) -> str:
    params: list[tuple[str, str]] = [("County[]", c) for c in counties]
    params += [
        ("Interest_Rate", ""), ("Plaintiff", ""),
        ("mortgage_from", ""), ("mortgage_to", ""),
        ("Monthly_Payment_from", ""), ("Monthly_Payment_to", ""),
        ("date_added", date_added),
        ("dr_entry_month_from", ""), ("dr_entry_day_from", ""), ("dr_entry_year_from", ""),
        ("dr_entry_month_to", ""), ("dr_entry_day_to", ""), ("dr_entry_year_to", ""),
        ("entry_month", ""), ("entry_day", ""), ("entry_year", ""),
        ("mort_entry_month_from", ""), ("mort_entry_day_from", ""), ("mort_entry_year_from", ""),
        ("mort_entry_month_to", ""), ("mort_entry_day_to", ""), ("mort_entry_year_to", ""),
        ("Address", ""), ("City", ""), ("Zip_Code", ""), ("Attorney", ""),
        ("Docket_Number", ""), ("per_page", str(per_page)),
        ("search", "Search"), ("cp", str(cp)),
    ]
    return f"{NJ_LP_PROPERTY_URL}?{urlencode(params)}"


async def scrape_search_results(
    page: Page,
    counties: list[str] | None = None,
    date_added: str = DATE_ADDED_WEEK,
    per_page: int = 50,
    download_dir: Path | None = None,
) -> list[NoticeData]:
    """Run the Property Search, scrape all result pages, export CSV, join.

    Returns list of NoticeData ready for the enrichment pipeline.
    """
    counties = counties or config.NJ_LP_COUNTIES
    download_dir = download_dir or NJ_LP_DOWNLOAD_DIR
    download_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. Scrape HTML pages ---
    all_records: list[dict] = []
    for cp in range(60):  # 60 pages × 50/page = 3000 record ceiling
        url = _build_search_url(counties, date_added=date_added, per_page=per_page, cp=cp)
        logger.info("GET %s", f"{NJ_LP_PROPERTY_URL}?cp={cp}&counties={len(counties)}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)

        html = await page.content()
        # On cp=0, pull total-count banner for logging
        if cp == 0:
            cm = _COUNTER_RE.search(html)
            if cm:
                logger.info("NJ LP: %s records matched across %s counties", cm.group(3), len(counties))

        recs = _parse_html_records(html)
        if not recs:
            break
        all_records.extend(recs)
        logger.info("  page cp=%d: parsed %d records (running total %d)", cp, len(recs), len(all_records))
        if len(recs) < per_page:
            break

    if not all_records:
        logger.warning("NJ LP: no records returned for counties=%s, date_added=%s",
                       counties, date_added)
        return []

    # --- 2. Export CSV from the same search (page must still be on a results URL) ---
    first_url = _build_search_url(counties, date_added=date_added, per_page=per_page, cp=0)
    await page.goto(first_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    try:
        await page.select_option('select[name="download"]', "csv")
        await page.wait_for_timeout(500)
        async with page.expect_download(timeout=60000) as dl_info:
            await page.click("#export_btn")
        dl = await dl_info.value
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = download_dir / f"njlp_export_{ts}.csv"
        await dl.save_as(str(csv_path))
        logger.info("NJ LP: CSV saved %s (%d bytes)", csv_path.name, csv_path.stat().st_size)
    except Exception as e:
        logger.error("NJ LP: CSV export failed: %s", e)
        await _screenshot(page, "csv_export_fail")
        return []

    # --- 3. Parse CSV + join ---
    try:
        with open(csv_path, encoding="utf-8-sig") as fh:
            csv_rows = list(csv.DictReader(fh))
    except Exception as e:
        logger.error("NJ LP: CSV parse failed: %s", e)
        return []

    logger.info("NJ LP: CSV has %d rows; HTML has %d records",
                len(csv_rows), len(all_records))

    # Index HTML records by (normalized_name, zip). De-dupe on first-seen —
    # if a defendant has two filings at the same ZIP in one week we keep the
    # first (rare; both rows would merge with the same CSV entry anyway).
    html_by_key: dict[tuple[str, str], dict] = {}
    for rec in all_records:
        name_key = _normalize_name_key(rec.get("defendant", ""))
        zip_m = _ZIP_RE.search(rec.get("addr_tail", ""))
        zip_code = zip_m.group(1) if zip_m else ""
        if name_key and zip_code and (name_key, zip_code) not in html_by_key:
            html_by_key[(name_key, zip_code)] = rec

    notices: list[NoticeData] = []
    unmatched = 0
    for row in csv_rows:
        key = (_normalize_name_key(row.get("Name", "")), (row.get("Zip") or "").strip())
        html_rec = html_by_key.get(key)
        if not html_rec:
            unmatched += 1
            continue
        notices.append(_build_notice(html_rec, row))

    logger.info("NJ LP: joined %d notices (unmatched CSV rows: %d)",
                len(notices), unmatched)
    return notices


async def scrape_nj_lp_notices(
    counties: list[str] | None = None,
    date_added: str = DATE_ADDED_WEEK,
    headless: bool = True,
) -> list[NoticeData]:
    """Scrape-only entry point: login → scrape → join → return NoticeData list.

    No enrichment, no DataSift, no Slack. Used by the combined Wednesday cron
    (modal_app.nj_weekly_all) which runs all 3 NJ scrapers in parallel and
    then feeds their combined output through a single enrichment pipeline.
    """
    counties = counties or config.NJ_LP_COUNTIES
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            accept_downloads=True,
        )
        await _load_cookies(context)
        page = await context.new_page()
        if not await login(page):
            logger.error("NJ LP login failed — returning empty")
            await browser.close()
            return []
        notices = await scrape_search_results(page, counties=counties, date_added=date_added)
        await _save_cookies(page)
        await browser.close()
    return notices


async def run_nj_scrape(
    counties: list[str] | None = None,
    date_added: str = DATE_ADDED_WEEK,
    headless: bool = True,
    upload_datasift: bool = True,
    notify_slack: bool = True,
    skip_enrichment: list[str] | None = None,
) -> dict:
    """Full pipeline: login → scrape+join → enrich → upload → notify."""
    from data_formatter import write_csv
    from datasift_formatter import auto_week_tag, write_datasift_split_csvs
    from enrichment_pipeline import run_enrichment_pipeline

    counties = counties or config.NJ_LP_COUNTIES
    result: dict = {
        "success": False, "records": 0, "counties": {}, "output_csv": "", "message": "",
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            accept_downloads=True,
        )
        await _load_cookies(context)
        page = await context.new_page()

        if not await login(page):
            result["message"] = "NJ LP login failed"
            await browser.close()
            return result

        notices = await scrape_search_results(page, counties=counties, date_added=date_added)
        await _save_cookies(page)
        await browser.close()

    if not notices:
        result["message"] = "No records scraped"
        return result

    # County breakdown
    for n in notices:
        c = n.county or "Unknown"
        result["counties"][c] = result["counties"].get(c, 0) + 1

    logger.info("NJ LP scraped: %d notices across %s",
                len(notices), result["counties"])

    # Enrichment: NJ records have no parcel/tax upstream data, so skip those
    # (they'd fail silently or burn API credits on no-op lookups). Obit is
    # now ON so we catch deceased defendants whose estates haven't filed
    # probate yet — the probate_preset path inside obituary_enricher keeps
    # us safe from overriding court-named executors on probate records.
    from enrichment_pipeline import PipelineOptions
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        skip_obituary=False,
        skip_ancestry=True,
        skip_dm_address=True,
        skip_heir_verification=True,
        skip_parcel_lookup=True,
        source_label=f"NJ Lis Pendens ({', '.join(counties)})",
    )
    if skip_enrichment:
        for flag in skip_enrichment:
            if hasattr(opts, flag):
                setattr(opts, flag, True)

    enriched, health = run_enrichment_pipeline(notices, opts, return_health=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_path = write_csv(enriched, f"nj_lp_{timestamp}.csv")
    result["output_csv"] = str(output_path)
    result["records"] = len(enriched)

    # DataSift upload
    if upload_datasift and config.DATASIFT_EMAIL and config.DATASIFT_PASSWORD:
        from datasift_uploader import upload_to_datasift

        csv_infos = write_datasift_split_csvs(enriched, list_name="")
        for info in csv_infos:
            logger.info("DataSift CSV (%s): %s", info["label"], info["path"])
        upload_result = await upload_to_datasift(
            csv_infos[0]["path"], enrich=True, skip_trace=True,
        )
        result["upload"] = upload_result
        if upload_result.get("success"):
            logger.info("DataSift upload: %s", upload_result.get("message", "OK"))
        else:
            logger.error("DataSift upload failed: %s", upload_result.get("message"))

    # Slack notification
    if notify_slack and config.SLACK_WEBHOOK_URL:
        try:
            from slack_notifier import _send_webhook
            week_tag = auto_week_tag("foreclosure")
            county_lines = "\n".join(f"  {c}: {n} records" for c, n in result["counties"].items())
            msg = (
                f"*NJ Lis Pendens — {week_tag}*\n"
                f"{county_lines}\n"
                f"Total: {result['records']} records\n"
                f"Output: {output_path.name}"
            )
            if upload_datasift:
                msg += "\nDataSift: uploaded + enrich + skip trace started"

            # Enrichment health
            from enrichment_pipeline import evaluate_enrichment_health
            health_lines, hard_breach, soft_breach = evaluate_enrichment_health(health)
            if health_lines:
                msg += "\n\n*Enrichment Health:*\n" + "\n".join(health_lines)
                if hard_breach:
                    msg = "⚠️ ENRICHMENT HEALTH WARNING\n" + msg
                elif soft_breach:
                    msg = "📊 Enrichment health note\n" + msg

            _send_webhook(msg)
            logger.info("Slack notification sent")
        except Exception as e:
            logger.warning("Slack notification failed: %s", e)

    result["success"] = True
    result["message"] = f"{result['records']} records processed"
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    counties = None
    headless = True
    for arg in sys.argv[1:]:
        if arg.startswith("--counties="):
            counties = [c.strip() for c in arg.split("=", 1)[1].split(",")]
        elif arg == "--headed":
            headless = False
    r = asyncio.run(run_nj_scrape(
        counties=counties, headless=headless,
        upload_datasift=False, notify_slack=False,
    ))
    print(json.dumps(r, indent=2, default=str))
