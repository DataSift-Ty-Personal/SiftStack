"""Configuration for SiftStack — full-stack REI operations platform."""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ── Run date / timezone ────────────────────────────────────────────────
# Stamp date_added (and other "today" values) in the operator's business
# timezone, not the server clock. The Apify cloud container runs in UTC, so a
# naive datetime.now() stamps tomorrow's date on any evening run. Knox and
# Blount County, TN operate on US Eastern.
BUSINESS_TIMEZONE = os.getenv("BUSINESS_TIMEZONE", "America/New_York")


def run_date() -> str:
    """Today's calendar date (YYYY-MM-DD) in the business timezone."""
    try:
        return datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = PROJECT_ROOT / "last_run.json"
SEEN_IDS_FILE = PROJECT_ROOT / "seen_ids.json"
SEEN_IDS_PRUNE_DAYS = 90
# Notices that exhausted all CAPTCHA retries during scraping.
# Persisted so the next run's summary can surface them instead of
# silently dropping — and a future retry pass can prioritize them.
CAPTCHA_FAILED_IDS_FILE = PROJECT_ROOT / "captcha_failed_ids.json"
CAPTCHA_FAILED_PRUNE_DAYS = 14
COOKIES_FILE = PROJECT_ROOT / "cookies.json"
DROPBOX_STATE_FILE = PROJECT_ROOT / "dropbox_state.json"
PHOTO_STATE_FILE = PROJECT_ROOT / "photo_state.json"

# ── Dropbox Watcher ────────────────────────────────────────────────────
DROPBOX_POLL_INTERVAL = int(os.getenv("DROPBOX_POLL_INTERVAL", "900"))  # seconds (default 15 min)
DROPBOX_ROOT_FOLDER = os.getenv("DROPBOX_ROOT_FOLDER", "")  # root folder path in Dropbox, e.g. "/TN Public Notice"
DROPBOX_STORAGE_WARN_PERCENT = 80  # warn when storage usage exceeds this %

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ── Credentials ────────────────────────────────────────────────────────
TNPN_EMAIL = os.getenv("TNPN_EMAIL", "")
TNPN_PASSWORD = os.getenv("TNPN_PASSWORD", "")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")  # 2Captcha API key
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Claude Haiku for LLM parsing
SMARTY_AUTH_ID = os.getenv("SMARTY_AUTH_ID", "")        # Smarty address standardization
SMARTY_AUTH_TOKEN = os.getenv("SMARTY_AUTH_TOKEN", "")
OPENWEBNINJA_API_KEY = os.getenv("OPENWEBNINJA_API_KEY", "")  # Zillow property enrichment
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")              # Serper.dev Google Search API
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")        # Firecrawl JS-rendered scraping
TRACERFY_API_KEY = os.getenv("TRACERFY_API_KEY", "")          # Tracerfy skip tracing
TRESTLE_API_KEY = os.getenv("TRESTLE_API_KEY", "")            # Trestle phone validation (legacy single key)
# Trestle affiliate: exhaust the FREE key (1,000 free lookups) first, then spill to PAID.
# Router = _api/clients/trestle_router.py. If only the legacy TRESTLE_API_KEY is set it is
# treated as PAID (set TRESTLE_LEGACY_IS_FREE=1 to treat it as the free key instead).
TRESTLE_FREE_API_KEY = os.getenv("TRESTLE_FREE_API_KEY", "")  # affiliate free-allotment key (1,000 free)
TRESTLE_PAID_API_KEY = os.getenv("TRESTLE_PAID_API_KEY", "")  # billed key (used after free is exhausted)
TRESTLE_FREE_LIMIT = int(os.getenv("TRESTLE_FREE_LIMIT", "1000"))   # size of the free allotment
TRESTLE_FREE_RESET = os.getenv("TRESTLE_FREE_RESET", "cumulative")  # "cumulative" (one-time) or "monthly"
ENFORMION_AP_NAME = os.getenv("ENFORMION_AP_NAME", "")        # Enformion/Endato API access profile name
ENFORMION_AP_PASSWORD = os.getenv("ENFORMION_AP_PASSWORD", "")  # Enformion/Endato API access profile password
SCRAPFLY_KEY = os.getenv("SCRAPFLY_KEY", "")                  # Scrapfly web scraping API (residential proxy + ASP/CAPTCHA + screenshots)
DATASIFT_EMAIL = os.getenv("DATASIFT_EMAIL", "")              # DataSift.ai login
DATASIFT_PASSWORD = os.getenv("DATASIFT_PASSWORD", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")        # Slack/Discord webhook
ANCESTRY_EMAIL = os.getenv("ANCESTRY_EMAIL", "")              # Ancestry.com login
ANCESTRY_PASSWORD = os.getenv("ANCESTRY_PASSWORD", "")
NJLISPENDENS_EMAIL = os.getenv("NJLISPENDENS_EMAIL", "")      # NJ Lis Pendens login
NJLISPENDENS_PASSWORD = os.getenv("NJLISPENDENS_PASSWORD", "")
NJ_LP_COUNTIES = ["Essex", "Middlesex", "Somerset", "Union"]  # Default NJ counties
NJ_LP_COOKIES_FILE = PROJECT_ROOT / "nj_lp_cookies.json"

# Newark Code Enforcement (CKAN — Cloudflare-protected, needs Playwright)
NEWARK_CKAN_BASE = "https://data.ci.newark.nj.us"
NEWARK_CE_PACKAGE_ID = "f7853d7a-d544-4006-b8f7-b4b064f4f597"
NEWARK_CE_VIOLATIONS_ID = "72ab72f0-b437-49cb-ae82-0b4d3860127a"
NEWARK_CE_INSPECTIONS_ID = "3f1b0cc7-48ab-4cf4-9100-c90f99be98e7"
NEWARK_CE_COMPLAINTS_ID = "a79bb702-28dc-4998-89e6-bd7f1ece4ea0"
NEWARK_CE_STATE_FILE = PROJECT_ROOT / "newark_cv_state.json"
NEWARK_CE_COOKIES_FILE = PROJECT_ROOT / "newark_cv_cookies.json"

# NJ Sheriff Sales (salesweb.civilview.com) — plain HTTP, no auth, no CF
NJ_CIVILVIEW_BASE = "https://salesweb.civilview.com"
NJ_CIVILVIEW_COUNTIES = {
    "Essex": 2,
    "Middlesex": 73,
    "Union": 15,
}
NJ_SHERIFF_STATE_FILE = PROJECT_ROOT / "nj_sheriff_state.json"

# Cross-run dedup index for local CLI runs. Modal has its own Volume-backed
# path (/tracking/processed_ids.json) — this local file is used when the
# same scrapers run from the command line outside Modal.
LOCAL_TRACKING_FILE = PROJECT_ROOT / "tracking" / "processed_ids.json"

# Comma-separated notice_types that are paused from DataSift upload.
# Records of these types still get scraped, enriched, and written to the
# combined CSV — but are held back from the auto-upload so they can be
# manually cleaned (e.g. probate ownership verification) before ingestion.
# Set the env var to "" (empty) to re-enable all uploads. Default pauses
# probate until we add ownership verification against NJ MOD-IV.
SIFTSTACK_UPLOAD_PAUSED_TYPES = {
    t.strip().lower()
    for t in os.getenv("SIFTSTACK_UPLOAD_PAUSED_TYPES", "probate").split(",")
    if t.strip()
}
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "")            # Dropbox OAuth2 app key
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")          # Drive folder for CSV/report/screenshot uploads (CLI)
GOOGLE_SERVICE_ACCOUNT_KEY = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY", "")  # base64-encoded service account JSON (CLI)

# ── LLM Backend ──────────────────────────────────────────────────────
LLM_BACKEND = os.getenv("LLM_BACKEND", "anthropic")           # "anthropic", "ollama", or "openrouter"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")  # Anthropic model name (default for all LLM calls)
# High-stakes obituary identity + heir/survivor extraction uses a stronger model.
# Getting the heir/decision-maker chain right is critical: a wrong heir map sends
# the whole deal down the wrong path, so this defaults to Sonnet rather than Haiku.
OBITUARY_LLM_MODEL = os.getenv("OBITUARY_LLM_MODEL", "claude-sonnet-4-6")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")        # Local Ollama model
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1/")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")       # OpenRouter API key
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# ── Site URLs ──────────────────────────────────────────────────────────
BASE_URL = "https://www.tnpublicnotice.com"
LOGIN_URL = f"{BASE_URL}/authenticate.aspx"
SMART_SEARCH_URL = f"{BASE_URL}/Smartsearch/Default.aspx"

# ── ASP.NET Selectors ─────────────────────────────────────────────────
# Login form
SEL_LOGIN_EMAIL = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtEmailAddress"
SEL_LOGIN_PASSWORD = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtPassword"
SEL_LOGIN_SUBMIT = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_btnAuth"

# Smart Search dashboard
SEL_SAVED_SEARCHES_DROPDOWN = "#ctl00_ContentPlaceHolder1_as1_ddlSavedSearches"
SEL_PER_PAGE_DROPDOWN = 'select[name$="ddlPerPage"]'
# Ad-hoc keyword search builder (used by run_keyword_search for backfill).
SEL_KEYWORD_SEARCH = "#ctl00_ContentPlaceHolder1_as1_txtSearch"
SEL_KEYWORD_MONTHS_RADIO = "#ctl00_ContentPlaceHolder1_as1_rbLastNumMonths"
SEL_KEYWORD_MONTHS_INPUT = "#ctl00_ContentPlaceHolder1_as1_txtLastNumMonths"
SEL_SEARCH_GO = "#ctl00_ContentPlaceHolder1_as1_btnGo"

# Search results (authenticated grid)
SEL_RESULTS_GRID = "#ctl00_ContentPlaceHolder1_WSExtendedGrid1_GridView1"
SEL_VIEW_BUTTON_PATTERN = "input[name$='btnView']"
SEL_NEXT_PAGE_BUTTON = "input[title='Next page']"
SEL_PAGE_INFO = "td:has-text('Page ')"

# Notice detail page
SEL_CAPTCHA_IFRAME = "iframe[src*='recaptcha']"
SEL_VIEW_NOTICE_BUTTON = "#ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_btnViewNotice"

# ── Notice gate CAPTCHA ────────────────────────────────────────────────
# tnpublicnotice migrated from Google reCAPTCHA v2 to CLOUDFLARE TURNSTILE
# around 2026-07-13. That is what silently killed the scrape: 2Captcha was still
# being asked to solve a reCAPTCHA that no longer exists, and the token was being
# injected into a `g-recaptcha-response` field the page no longer reads. The live
# page now renders <div class="cf-turnstile" data-sitekey="0x..."> with a
# `cf-turnstile-response` hidden input.
CAPTCHA_KIND = os.getenv("CAPTCHA_KIND", "turnstile").strip().lower()  # turnstile | recaptcha
TURNSTILE_SITEKEY = os.getenv("TURNSTILE_SITEKEY", "0x4AAAAAADs-29tdUBxeI6cO")
SEL_TURNSTILE = ".cf-turnstile"
TURNSTILE_RESPONSE_FIELD = "cf-turnstile-response"
# Legacy Google key, kept for the reCAPTCHA fallback path.
RECAPTCHA_SITEKEY = "6LdtSg8sAAAAADTdRyZxJ2R2sS82pKALNMvMqSyL"

# ── Rate Limiting ──────────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.0  # seconds between requests
REQUEST_DELAY_MAX = 3.0
MAX_RETRIES = 3
RESULTS_PER_PAGE = 50  # max the site allows

# ── Scraping Backend ───────────────────────────────────────────────────
# "playwright" = in-house Playwright + 2Captcha (drives one continuous session
# through login -> saved search -> view, which the site requires).
# "scrapfly" = route the detail fetch through Scrapfly. EXPERIMENTAL: Scrapfly
# cleanly handles login, residential proxy, reCAPTCHA, and screenshots, but
# tnpublicnotice.com detail pages need stateful search-session context (ASP.NET
# cookieless session), so a direct Details.aspx?ID= fetch returns an empty
# shell. Until the full in-session flow is built, default stays "playwright".
SCRAPE_BACKEND = os.getenv("SCRAPE_BACKEND", "playwright").strip().lower()
SCRAPFLY_COUNTRY = os.getenv("SCRAPFLY_COUNTRY", "us")          # proxy geolocation for Scrapfly
SCRAPFLY_RENDER_WAIT_MS = int(os.getenv("SCRAPFLY_RENDER_WAIT_MS", "3500"))  # wait after View Notice click
SCRAPFLY_TIMEOUT_MS = int(os.getenv("SCRAPFLY_TIMEOUT_MS", "90000"))         # per-call ceiling
SCRAPFLY_MAX_RETRIES = int(os.getenv("SCRAPFLY_MAX_RETRIES", "2"))           # extra attempts on gate/CAPTCHA miss
SCRAPFLY_PROXY_POOL = os.getenv("SCRAPFLY_PROXY_POOL", "public_residential_pool")  # residential proxies for the generic ASP fallback fetcher (scrapfly_browser)

# ── Image Processing ───────────────────────────────────────────────────
BLUR_THRESHOLD = int(os.getenv("BLUR_THRESHOLD", "100"))   # Laplacian variance; below = rejected as blurry
TESSERACT_PSM_PDF = 3    # fully automatic — best for PDF tax sale tables
TESSERACT_PSM_PHOTO = 4  # assume single column of variable-size text — best for terminal screen photos

# ── Notice Screenshots (proof-of-source) ────────────────────────────────
# Capture a full-page screenshot of each notice detail page during scraping so
# the actual published notice travels with the record into DataSift (a clickable
# link in Notes + the "Notice Screenshot" custom field). Adds legitimacy to
# outreach. Disable by setting CAPTURE_NOTICE_SCREENSHOTS=false.
CAPTURE_NOTICE_SCREENSHOTS = os.getenv(
    "CAPTURE_NOTICE_SCREENSHOTS", "true"
).strip().lower() not in ("0", "false", "no", "off", "")
NOTICE_SCREENSHOT_DIR = OUTPUT_DIR / "notices"
# Notice types we capture screenshots for (comma-separated env override).
NOTICE_SCREENSHOT_TYPES = {
    t.strip().lower()
    for t in os.getenv("NOTICE_SCREENSHOT_TYPES", "foreclosure").split(",")
    if t.strip()
}

# ── Notice Types ───────────────────────────────────────────────────────
NOTICE_TYPES = ["foreclosure", "probate"]


@dataclass
class SavedSearch:
    """Represents a saved search on tnpublicnotice.com."""
    county: str
    notice_type: str  # One of NOTICE_TYPES
    saved_search_name: str  # Exact name in the Saved Searches dropdown


# ── Saved Searches ─────────────────────────────────────────────────────
# These names must match exactly what appears in the dropdown on the site.
SAVED_SEARCHES: list[SavedSearch] = [
    SavedSearch("Knox", "foreclosure", "Foreclosure V2 Knox"),
    SavedSearch("Blount", "foreclosure", "Foreclosure V2 Blount"),
]

# ── Entity Detection ──────────────────────────────────────────────────
# Business entity patterns — shared across obituary_enricher, tax_enricher,
# and enrichment_pipeline for entity filtering.
BUSINESS_RE = re.compile(
    r"\b(?:LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO\b|LTD|LP|L\.P|"
    r"PARTNERSHIP|ASSOCIATION|ASSOC|BANK|CREDIT UNION|CHURCH|MINISTRIES|"
    r"HOUSING|AUTHORITY|DEVELOPMENT|ENTERPRISES|PROPERTIES|INVESTMENTS|"
    r"GROUP|HOLDINGS|MANAGEMENT|SERVICES|FOUNDATION|ORGANIZATION)\b",
    re.IGNORECASE,
)

# Trust/estate patterns — personal trusts are NOT business entities
TRUST_NAME_RE = re.compile(
    r"^(?:THE\s+)?([\w]+(?:\s+[\w.]+)+?)\s+(?:REVOCABLE\s+)?(?:LIVING\s+)?TRUST\b",
    re.IGNORECASE,
)
ESTATE_OF_RE = re.compile(
    r"^(?:THE\s+)?ESTATE\s+OF\s+([\w]+(?:\s+[\w.]+)+?)(?:\s*,|\s*$)",
    re.IGNORECASE,
)

_config_logger = logging.getLogger(__name__)


# ── State File Utilities ─────────────────────────────────────────────


def save_state(path: Path, data: dict) -> None:
    """Write JSON state to disk atomically (write tmp → rename).

    Creates a .bak copy of the previous file before overwriting.
    """
    # Back up current file
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass  # Best-effort backup

    # Atomic write: tmp → rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    """Load JSON state from disk, falling back to .bak if corrupt."""
    for candidate in [path, path.with_suffix(path.suffix + ".bak")]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _config_logger.warning("Failed to read %s: %s", candidate, e)
    return {}
