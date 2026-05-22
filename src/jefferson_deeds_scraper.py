"""Scraper for Jefferson County Clerk online deed records (Louisville, KY).

Uses simple HTTP POST — no login, no CAPTCHA, no Playwright required.
Site: https://search.jeffersondeeds.com/

Two independent search surfaces on this site:

1. **Instrument-type search** (`p6.php`, single-step) — used by
   ``scrape_jefferson_deeds()`` for LIS PENDENS (pre-foreclosure)
   filings by date range. Each filing contains grantor (debtor/owner),
   grantees (lenders/plaintiffs), legal description, case number, and
   filing date.

2. **Owner-name search** (`p3.php` → `dlist.php`, two-step) — used by
   ``enrich_mortgage_balances()`` (Phase 2b) for decedent deed history.
   Step 1 returns a "Unique Hit List" grouping the query's matches by
   owner name. Step 2 posts a specific owner row's colon-delimited
   checkbox value to get that owner's actual deed records.

NOTE: Louisville legal descriptions are metes-and-bounds or subdivision-lot
format. They do NOT include street numbers. For ``scrape_jefferson_deeds``,
``address`` is left blank; the enrichment pipeline can resolve the property
address via the Jefferson County PVA or Smarty geocoding from the legal
description.
"""

from __future__ import annotations

import http.cookiejar
import logging
import random
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from kentucky_name_resolver import SUFFIX_RE, generate_variants
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

JCD_BASE_URL = "https://search.jeffersondeeds.com"
JCD_SEARCH_URL = f"{JCD_BASE_URL}/p6.php"
JCD_DETAIL_URL = f"{JCD_BASE_URL}/pdetail.php"
JCD_COUNTY_NUM = "20"       # Jefferson County internal code on this system
LP_INSTRUMENT_CODE = "LP"   # Instrument type value for Lis Pendens

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ── HTTP helpers ──────────────────────────────────────────────────────


def _delay() -> None:
    time.sleep(random.uniform(1.0, 2.5))


def _post(url: str, params: dict) -> str:
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("User-Agent", _USER_AGENT)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Referer", f"{JCD_BASE_URL}/insttype.php")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _get(url: str) -> str:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Result parsing ────────────────────────────────────────────────────

# Columns inside each FORM block (confirmed from live HTML):
#   <a href='pdetail.php?instnum=N&year=Y&db=D&cnum=C'> — detail link
#   <td width=20%><div class="textContainer_Truncate">PARTY\n...</div></td> — all parties
#   <td width=20%><div class="textContainer_Truncate">...</div></td>       — secondary (often empty)
#   <td id=detils width=15%>CASE# LEGAL_DESC</td>                          — legal description
#   <td width=7%>MM/DD/YYYY</td>                                           — file date
#   <td width=6%>L NNNN NNN</td>                                           — book/page
#   <td width=10% ...>LIS PENDENS</td>                                     — doc type

_FORM_RE = re.compile(
    r"<FORM ACTION=pdetail\.php.*?</[Ff][Oo][Rr][Mm]>",
    re.DOTALL | re.IGNORECASE,
)
# VIEW link is in the <td> BEFORE each FORM — base64-encoded TIFF path wrapped as PDF
_VIEW_IMG_RE = re.compile(
    r"viewimg\.php\?img=([A-Za-z0-9+/=]+)&type=pdf",
    re.IGNORECASE,
)
_INSTNUM_RE = re.compile(r"instnum=(\d+)&year=(\d+)&db=(\d+)", re.IGNORECASE)
_PARTY_DIV_RE = re.compile(
    r'<div class="textContainer_Truncate">(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_DETILS_TD_RE = re.compile(
    r'<td[^>]+id=detils[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE,
)
_DATE_TD_RE = re.compile(
    r'<td width=7%[^>]*>\s*<span[^>]*>(\d{2}/\d{2}/\d{4})</span>',
    re.IGNORECASE,
)
_BOOK_TD_RE = re.compile(
    r'<td width=6%[^>]*>\s*<span[^>]*>(L\s+\d+\s+\d+)</span>',
    re.IGNORECASE,
)
_CASE_NUM_RE = re.compile(r"^(\d{1,3}[A-Z]{2}\d+)\s+", re.IGNORECASE)


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _parse_results_table(html: str) -> list[dict]:
    """Parse each FORM block from the p6.php HIT LIST response into records."""
    records = []
    # VIEW links sit in the <td> immediately before each FORM — same count, same order
    view_imgs = _VIEW_IMG_RE.findall(html)
    forms = list(_FORM_RE.finditer(html))
    for idx, form_match in enumerate(forms):
        form_html = form_match.group(0)
        view_img = view_imgs[idx] if idx < len(view_imgs) else ""

        # Instrument number + year + db from detail link
        m = _INSTNUM_RE.search(form_html)
        if not m:
            continue
        instnum, year, db = m.group(1), m.group(2), m.group(3)
        detail_url = (
            f"{JCD_DETAIL_URL}?instnum={instnum}&year={year}"
            f"&db={db}&cnum={JCD_COUNTY_NUM}"
        )

        # Party names — first textContainer_Truncate div holds all parties
        # (grantor on the first <br/>-delimited line, grantees below)
        party_divs = _PARTY_DIV_RE.findall(form_html)
        grantor = ""
        grantees: list[str] = []
        if party_divs:
            lines = [
                _strip_tags(ln)
                for ln in re.split(r"<br\s*/?>", party_divs[0], flags=re.IGNORECASE)
                if _strip_tags(ln)
            ]
            if lines:
                grantor = lines[0]
                grantees = lines[1:]

        # Legal description (includes case number as prefix)
        legal_raw = ""
        m2 = _DETILS_TD_RE.search(form_html)
        if m2:
            legal_raw = _strip_tags(m2.group(1))

        # Strip case number prefix from legal description
        case_num = ""
        cn_m = _CASE_NUM_RE.match(legal_raw)
        if cn_m:
            case_num = cn_m.group(1)
            legal_desc = legal_raw[cn_m.end():].strip()
        else:
            legal_desc = legal_raw

        # Filed date
        date_m = _DATE_TD_RE.search(form_html)
        date_filed = date_m.group(1) if date_m else ""

        # Book/page
        book_m = _BOOK_TD_RE.search(form_html)
        book_page = book_m.group(1) if book_m else ""

        records.append({
            "instnum": instnum,
            "year": year,
            "db": db,
            "detail_url": detail_url,
            "grantor": grantor,
            "grantees": grantees,
            "legal_desc": legal_desc,
            "case_num": case_num,
            "date_filed": date_filed,
            "book_page": book_page,
            "view_img": view_img,
        })

    return records


# ── Address parsing ───────────────────────────────────────────────────

# Louisville metes-and-bounds: "STREET_NAME WS/ES/NS/SS ..."
# e.g. "HEMLOCK ST WS 30' 205' S OF SOUTHERN AVE"
_MB_RE = re.compile(
    r"^([\w\s.]+?)\s+(?:WS|ES|NS|SS|NWC|SWC|NEC|SEC|NW|SW|NE|SE)\b",
    re.IGNORECASE,
)


def _parse_legal_desc_address(legal_desc: str) -> str:
    """Extract a best-effort street name from a Louisville legal description.

    Returns a partial address string (no house number) or empty string if
    the description is subdivision-lot format (no parseable street name).
    """
    desc = legal_desc.strip()

    m = _MB_RE.match(desc)
    if m:
        street = m.group(1).strip().title()
        return street  # e.g. "Hemlock St" — no number, but useful for lookup

    return ""  # subdivision lot format — no usable street info


# ── Document PDF fetch + address extraction ───────────────────────────

# Patterns for a full street address in a Lis Pendens document body
_ADDR_LABELED_RE = re.compile(
    r"(?:located\s+at|property\s+address|premises\s+(?:at|known\s+as)|"
    r"commonly\s+known\s+as|street\s+address)[:\s]+(\d{2,5}\s+[^\n,;]{5,60})",
    re.IGNORECASE,
)
_ADDR_NUMBER_RE = re.compile(
    r"\b(\d{2,5})\s+"
    r"((?:[NSEW]\.\s+)?[A-Z][A-Za-z]{1,20}(?:\s+[A-Z][A-Za-z]{1,20}){0,3}\s+"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Rd|Road|Ln|Lane|"
    r"Ct|Court|Way|Pl|Place|Pkwy|Parkway|Cir|Circle|Ter|Terrace|Hwy|Highway)\.?)",
    re.IGNORECASE,
)

# Extended version that also captures city and ZIP on the same or following line.
# Matches defendant address blocks like:
#   10824 Milwaukee Way\nLouisville, KY 40272
#   10824 Milwaukee Way, Louisville, KY 40272
_ADDR_WITH_CITY_RE = re.compile(
    r"\b(\d{2,5})\s+"
    r"((?:[NSEW]\.\s+)?[A-Za-z][A-Za-z]{1,20}(?:\s+[A-Za-z][A-Za-z]{1,20}){0,3}\s+"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Rd|Road|Ln|Lane|"
    r"Ct|Court|Way|Pl|Place|Pkwy|Parkway|Cir|Circle|Ter|Terrace|Hwy|Highway)\.?)"
    r"[\s,\n\r]{0,6}"
    r"([A-Za-z][A-Za-z\s]{2,25}),\s*KY\s+(\d{5})",
    re.IGNORECASE,
)

# Parcel/Map ID as labeled in Jefferson County Lis Pendens documents.
# Jefferson County format: exactly 12 alphanumeric chars (e.g. 109801200022, 014J01500000).
# OCR often inserts spaces, newlines, or trailing garbage around the number.
# Strategy: capture generously after the label, then take the first 12 alphanumeric chars.
_PARCEL_ID_RE = re.compile(
    r"(?:Parcel[/\\ ]?Map\s+ID[^:\n]{0,25}?|"
    r"Parcel\s+(?:ID|Number|No\.?)|"
    r"Map\s+(?:No\.?|Number|ID))"
    r"[:\s#]*(\d[\d\s\-A-Z\n\r]{10,40})",
    re.IGNORECASE | re.DOTALL,
)

# OCR commonly misreads digits as letters in ordinal street names.
# These substitutions run on the already-extracted address string so the
# regex above can still match (e.g. "Sth" as a word), then we clean up.
_OCR_ORDINAL_FIXES = [
    (re.compile(r"\bSth\b", re.IGNORECASE), "5th"),   # 5 → S
    (re.compile(r"\blst\b"),                "1st"),    # 1 → l (lowercase L)
    (re.compile(r"\bIst\b"),                "1st"),    # 1 → I (uppercase i)
    (re.compile(r"\bBth\b", re.IGNORECASE), "8th"),   # 8 → B
]


def _fix_ocr_ordinals(addr: str) -> str:
    for pattern, replacement in _OCR_ORDINAL_FIXES:
        addr = pattern.sub(replacement, addr)
    return addr


def _extract_address_from_text(text: str) -> tuple[str, str, str]:
    """Return (street, city, zip) from OCR'd document text. Any element may be empty.

    Priority: labeled address > multiline address-with-city > street-only fallback.
    """
    m = _ADDR_LABELED_RE.search(text)
    if m:
        return _fix_ocr_ordinals(m.group(1).strip()), "", ""
    m = _ADDR_WITH_CITY_RE.search(text)
    if m:
        street = _fix_ocr_ordinals(f"{m.group(1)} {m.group(2).strip()}")
        return street, m.group(3).strip().title(), m.group(4).strip()
    m = _ADDR_NUMBER_RE.search(text)
    if m:
        return _fix_ocr_ordinals(f"{m.group(1)} {m.group(2).strip()}"), "", ""
    return "", "", ""


def _lookup_pva_address(parcel_id: str) -> tuple[str, str, str]:
    """Look up the property address from Jefferson County PVA by parcel ID.

    Returns (street, city, zip) title-cased, or ("", "", "") on any failure.
    Address comes from the data-address attribute which contains the full
    "STREET, CITY, KY, ZIP" value, or falls back to the <h1> street-only field.
    """
    url = (
        f"https://jeffersonpva.ky.gov/property-search/property-details/"
        f"?parcel_id={parcel_id}"
    )
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _USER_AGENT)
        req.add_header("Referer", "https://jeffersonpva.ky.gov/property-search/")
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Preferred: data-address="STREET, CITY, KY, ZIP" — full address in one place
        m = re.search(
            r'data-address="(\d+\s+[^"]{3,60}),\s*([^",]+),\s*KY,\s*(\d{5})"',
            html, re.IGNORECASE,
        )
        if m:
            return m.group(1).strip().title(), m.group(2).strip().title(), m.group(3).strip()

        # Fallback: <h1> has the street address only
        m2 = re.search(r"<h1[^>]*>(\d+\s+[^<]{3,60})</h1>", html, re.IGNORECASE)
        if m2:
            return m2.group(1).strip().title(), "", ""

    except Exception as exc:
        logger.debug("JCD: PVA lookup failed for parcel %s: %s", parcel_id, exc)
    return "", "", ""


def _fetch_address_from_document(view_img: str) -> tuple[str, str, str, str, str]:
    """Fetch the Lis Pendens PDF and extract the property address and parcel ID.

    Scans all pages for a labeled Parcel/Map ID and OCR address.  When a valid
    12-char Jefferson County parcel ID is found, the PVA is queried for the
    authoritative street address (avoids picking up lender/trustee addresses).

    Returns (address, city, zip, parcel_id, source) where source is "pva" or "ocr".
    Any element may be empty on failure.
    """
    url = f"{JCD_BASE_URL}/viewimg.php?img={view_img}&type=pdf"
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _USER_AGENT)
        req.add_header("Referer", f"{JCD_BASE_URL}/p6.php")
        with urllib.request.urlopen(req, timeout=30) as resp:
            pdf_bytes = resp.read()
    except Exception as exc:
        logger.warning("JCD: document fetch failed: %s", exc)
        return "", "", "", "", ""

    try:
        import pypdfium2 as pdfium  # noqa: PLC0415
    except ImportError:
        logger.debug("JCD: pypdfium2 not available — skipping document OCR")
        return "", "", "", "", ""

    ocr_addr = ""
    ocr_city = ""
    ocr_zip = ""
    parcel_id = ""

    try:
        doc = pdfium.PdfDocument(pdf_bytes)
        num_pages = len(doc)

        # Check pages 2, 1, 3 in that order (address most often on page 2).
        # We scan ALL pages until we find an address that includes a ZIP — a
        # street-only match keeps us searching so a later page with the full
        # defendant address block can upgrade it.
        page_order = []
        if num_pages >= 2:
            page_order.append(1)
        page_order += [i for i in range(min(num_pages, 4)) if i not in page_order]

        for page_idx in page_order:
            page = doc[page_idx]

            # Fast path: text layer (some counties run OCR on their TIFF archives)
            try:
                text = page.get_textpage().get_text_range().strip()
            except Exception:
                text = ""

            # Slow path: render → OCR
            if not text:
                try:
                    import pytesseract  # noqa: PLC0415
                    from PIL import Image as _PIL  # noqa: PLC0415, F401
                    import os as _os  # noqa: PLC0415
                    _win_tess = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
                    if _os.path.exists(_win_tess):
                        pytesseract.pytesseract.tesseract_cmd = _win_tess
                    bitmap = page.render(scale=3.0)
                    pil_image = bitmap.to_pil()
                    text = pytesseract.image_to_string(pil_image, config="--psm 3")
                except ImportError:
                    logger.debug("JCD: pytesseract/Pillow not available — skipping OCR")
                    break
                except Exception as exc:
                    logger.warning("JCD: OCR page %d failed: %s", page_idx + 1, exc)
                    continue

            # Extract parcel ID from every page (may appear on any page).
            # Strip all non-alphanumeric chars (OCR spaces/newlines) then take
            # the first 12 chars — Jefferson County parcel IDs are exactly 12.
            if not parcel_id:
                pm = _PARCEL_ID_RE.search(text)
                if pm:
                    raw = pm.group(1)
                    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
                    candidate = cleaned[:12]
                    if len(candidate) == 12:
                        parcel_id = candidate
                        logger.debug(
                            "JCD: found parcel ID on doc page %d: %s (raw: %r)",
                            page_idx + 1, parcel_id, raw[:40],
                        )
                    else:
                        ctx_start = max(0, pm.start() - 60)
                        ctx_end = min(len(text), pm.end() + 40)
                        logger.debug(
                            "JCD: rejected parcel ID candidate '%s' (%d chars); "
                            "context: %r",
                            cleaned, len(cleaned), text[ctx_start:ctx_end],
                        )

            # Accumulate the best OCR address seen across all pages.
            # Upgrade if we find a more complete match (one that includes a ZIP).
            # Stop scanning for addresses once we have street + ZIP.
            if not ocr_zip:
                addr, city, zip_ = _extract_address_from_text(text)
                if addr and (not ocr_addr or zip_):
                    ocr_addr, ocr_city, ocr_zip = addr, city, zip_
                    logger.debug(
                        "JCD: found OCR address on doc page %d: %s%s",
                        page_idx + 1, addr,
                        f", {city} {zip_}".rstrip() if city or zip_ else "",
                    )

    except Exception as exc:
        logger.warning("JCD: document parsing failed: %s", exc)

    # Prefer PVA address when we have a parcel ID — it's the authoritative source
    # and avoids picking up lender/trustee addresses from the document body.
    if parcel_id:
        pva_street, pva_city, pva_zip = _lookup_pva_address(parcel_id)
        if pva_street:
            logger.debug(
                "JCD: PVA address for parcel %s: %s, %s %s",
                parcel_id, pva_street, pva_city, pva_zip,
            )
            return pva_street, pva_city, pva_zip, parcel_id, "pva"
        logger.debug(
            "JCD: PVA returned no address for parcel %s — using OCR fallback", parcel_id
        )

    return ocr_addr, ocr_city, ocr_zip, parcel_id, "ocr"


# ── Name normalisation ────────────────────────────────────────────────

def _normalize_name(raw: str) -> str:
    """Convert 'LASTNAME FIRSTNAME [MIDDLE]' to 'Firstname Lastname'.

    Jefferson County stores individual names LAST FIRST. Takes the first
    party only (grantor may list multiple parties separated by newlines).
    """
    name = raw.strip()
    if not name:
        return ""

    # Skip obvious non-person names (government agencies, banks, LLCs)
    upper = name.upper()
    for skip in ("COMMONWEALTH", "UNITED STATES", "METRO GOVERNMENT",
                 "BANK", "LLC", " INC", "ASSOCIATION", "AUTHORITY",
                 "DEPARTMENT", "DIVISION", "INSURANCE", "FINANCIAL"):
        if skip in upper:
            return name.title()  # keep as-is, title-cased

    parts = name.split()
    if len(parts) == 1:
        return parts[0].title()
    if len(parts) == 2:
        return f"{parts[1].title()} {parts[0].title()}"
    # 3+ parts: treat first token as last name
    return f"{parts[1].title()} {parts[0].title()}"


# ── Date helpers ──────────────────────────────────────────────────────

def _normalize_date(date_str: str) -> str:
    """Convert MM/DD/YYYY to YYYY-MM-DD."""
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return date_str


def last_n_business_days(n: int) -> tuple[str, str]:
    """Return (start_date, end_date) spanning the last N business days.

    Both dates in YYYY-MM-DD. End date is today. Start date is the
    Monday-through-Friday date exactly N weekdays before today.
    """
    today = datetime.now().date()
    current = today
    days_counted = 0
    while days_counted < n:
        current -= timedelta(days=1)
        if current.weekday() < 5:  # Mon=0 … Fri=4
            days_counted += 1
    return current.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


# ── Main scraper ──────────────────────────────────────────────────────


def scrape_jefferson_deeds(
    start_date: str,
    end_date: str,
    notice_type: str = "lis_pendens",
    county: str = "Jefferson",
    fetch_details: bool = True,
) -> list[NoticeData]:
    """Scrape LIS PENDENS filings from Jefferson County Clerk online records.

    Args:
        start_date: YYYY-MM-DD inclusive start
        end_date:   YYYY-MM-DD inclusive end
        notice_type: Value to write into NoticeData.notice_type
        county:      Value to write into NoticeData.county
        fetch_details: If True (default), fetch the filed document PDF for
                       each record and extract the full street address from
                       page 2.  Set False to skip document fetches and rely
                       only on the legal description from the hit list.

    Returns:
        List of NoticeData objects, one per LIS PENDENS filing.
    """
    def _to_form(d: str) -> str:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d/%Y")

    bdate = _to_form(start_date)
    edate = _to_form(end_date)

    logger.info(
        "JCD: searching LIS PENDENS %s → %s (Jefferson County, KY)",
        start_date, end_date,
    )

    try:
        html = _post(JCD_SEARCH_URL, {
            "cnum": "CNUM",
            "searchtype": "ITYPE",
            "itype1": LP_INSTRUMENT_CODE,
            "itype2": "",
            "itype3": "",
            "bDate": bdate,
            "eDate": edate,
            "search": "Execute Search",
        })
    except Exception as exc:
        logger.error("JCD: search request failed: %s", exc)
        return []

    if "HIT LIST" not in html:
        logger.warning(
            "JCD: response does not contain HIT LIST — site may be down "
            "or returned an error page."
        )
        return []

    records = _parse_results_table(html)
    logger.info("JCD: %d LP filings found", len(records))

    notices: list[NoticeData] = []
    for i, rec in enumerate(records):
        logger.info(
            "JCD: [%d/%d] %s — %s",
            i + 1, len(records), rec["grantor"], rec["legal_desc"],
        )

        legal_desc = rec["legal_desc"]

        # Try to get a full street address (with house number) from the filed document.
        # PVA lookup (via parcel ID) is preferred over raw OCR address.
        # Fall back to street-name-only extracted from the legal description.
        address = ""
        pva_city = ""
        pva_zip = ""
        parcel_id_found = ""
        if fetch_details and rec.get("view_img"):
            _delay()
            address, pva_city, pva_zip, parcel_id_found, addr_src = _fetch_address_from_document(rec["view_img"])
            if address:
                src = f"PVA parcel {parcel_id_found}" if addr_src == "pva" else "OCR"
                logger.info(
                    "JCD: [%d/%d] address from %s: %s%s",
                    i + 1, len(records), src, address,
                    f", {pva_city} {pva_zip}".rstrip() if pva_city or pva_zip else "",
                )

        if not address:
            address = _parse_legal_desc_address(legal_desc)

        notice = NoticeData(
            date_added=_normalize_date(rec["date_filed"]) if rec["date_filed"] else datetime.now().strftime("%Y-%m-%d"),
            address=address,
            city=pva_city or "Louisville",
            state="KY",
            zip=pva_zip,
            owner_name=_normalize_name(rec["grantor"]),
            notice_type=notice_type,
            county=county,
            source_url=rec["detail_url"],
            raw_text=legal_desc,
            parcel_id=parcel_id_found or rec["case_num"],
        )
        notices.append(notice)

    logger.info("JCD: returning %d LIS PENDENS notices", len(notices))
    return notices


# ══════════════════════════════════════════════════════════════════════
# Phase 2b — Owner-name search & mortgage history
# ══════════════════════════════════════════════════════════════════════
#
# The name-search surface is a 2-step flow on the same site, distinct from
# the LIS PENDENS flow above. See memory/project_jefferson_deeds_namesearch.md
# for the hard-won param quirks.

JCD_NAME_SEARCH_URL = f"{JCD_BASE_URL}/p3.php"
JCD_DLIST_URL = f"{JCD_BASE_URL}/dlist.php"
JCD_DISCLAIMER_URL = f"{JCD_BASE_URL}/index.php?acceptDisclaimer=true"

# Jefferson County's internal id on this system for the dlist.php POST.
# DO NOT confuse with the literal placeholder '.CNUM.' used by p3.php.
JCD_CNUM_DLIST = "20"

# Amortization assumptions for mortgage balance estimation.
# 30-year fixed at 6% — a pragmatic middle-ground when we have no signal
# about the loan's actual terms. Old mortgages (pre-2010) might have been
# refinanced into lower rates; new ones (post-2022) often higher. If the
# deed scraper ever lifts the actual interest rate out of the document,
# these become per-record.
_MORTGAGE_TERM_YEARS = 30
_MORTGAGE_RATE = 0.06

# Suffixes to strip when normalizing a decedent/owner name for JCD search are
# stripped via the canonical SUFFIX_RE imported from kentucky_name_resolver.


@dataclass
class DeedRecord:
    """One row parsed from dlist.php — an individual deed filing."""
    instnum: str
    year: str
    db: str
    filed_date: str              # YYYY-MM-DD
    book_page: str
    doc_type: str                # "MORTGAGE", "DEED", "STATE LIEN", "REL MTG", etc.
    grantor: str
    grantee: str
    legal_desc: str
    detail_url: str
    view_img: str = ""
    xrefs: list[str] = field(default_factory=list)  # cross-referenced instnums


def _accept_disclaimer(opener: urllib.request.OpenerDirector) -> None:
    """Hit the disclaimer acceptance URL once per session."""
    try:
        req = urllib.request.Request(JCD_DISCLAIMER_URL, headers={"User-Agent": _USER_AGENT})
        opener.open(req, timeout=20).read()
    except Exception as exc:
        logger.debug("JCD: disclaimer hit failed (non-fatal): %s", exc)


def _make_opener() -> urllib.request.OpenerDirector:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", _USER_AGENT)]
    return opener


def _parse_checkbox_value(value: str) -> tuple[str, str]:
    """Extract (display_name, checkbox_value) from a raw VALUE attribute.

    JCD's colon-delimited format:
        <NORMALIZED>:<nametype>:<searchtype>:<itypes>:<group>:<SURNAME>:<GIVEN>

    SURNAME is field 6, GIVEN is field 7 (may be empty; entities keep the
    whole name in SURNAME). Returns the joined display name plus the
    VALUE string as-is so we can replay it verbatim to dlist.php.
    """
    parts = value.split(":")
    if len(parts) < 6:
        return "", value
    surname = parts[5].strip()
    given = parts[6].strip() if len(parts) >= 7 else ""
    display = f"{surname} {given}".strip() if given else surname
    return display, value


def _search_names_unique(
    opener: urllib.request.OpenerDirector,
    owner_name: str,
    nametype: str = "2",
    searchtype: str = "PA",
) -> list[tuple[str, str, int]]:
    """Step 1: GET p3.php. Return (display, checkbox_value, count) rows.

    ``checkbox_value`` is the server's exact InstDetail[paname][] string to
    POST back in step 2 — never synthesize it, the format has quirks
    (e.g., entities keep ampersands in the SURNAME field while personal
    names are split SURNAME:GIVEN across two fields).
    """
    params = {
        "cnum": ".CNUM.",
        "nametype": nametype,
        "searchtype": searchtype,
        "param1": owner_name,
        "bdate": "", "edate": "",
        "itypes[]": "ALL",
        "group": "0",
        "stype": "name",
        "search": "Execute Search",
    }
    url = JCD_NAME_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    _delay()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Referer": f"{JCD_BASE_URL}/name.php",
        })
        with opener.open(req, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("JCD name search failed for %r: %s", owner_name, exc)
        return []

    if "NO HITS FOUND" in html:
        return []

    # Count appears in a <b>N</b> cell within ~800 chars after each VALUE
    # attribute. Use two independent walks rather than a single
    # tag-spanning regex — the source HTML nesting varies.
    results: list[tuple[str, str, int]] = []
    count_re = re.compile(r"<b>\s*(\d+)\s*</b>", re.IGNORECASE)

    for vm in re.finditer(r"VALUE='([^']+)'", html, re.IGNORECASE):
        value = vm.group(1)
        display, _ = _parse_checkbox_value(value)
        if not display:
            continue
        window = html[vm.end(): vm.end() + 800]
        cm = count_re.search(window)
        count = int(cm.group(1)) if cm else 0
        results.append((display, value, count))

    return results


def _score_display_name(query: str, display: str) -> float:
    """Score how well a display name matches a query (0..1).

    Query-term-all-present + adjacency > surname-only > partial.
    """
    q_tokens = [t for t in re.split(r"\s+", query.upper()) if t]
    d_tokens = [t for t in re.split(r"\s+", display.upper()) if t]
    if not q_tokens or not d_tokens:
        return 0.0

    if display.upper().strip() == query.upper().strip():
        return 1.0

    # All query tokens present in display?
    matched = sum(1 for t in q_tokens if any(t in dt for dt in d_tokens))
    score = matched / len(q_tokens) * 0.9

    # Boost if query is a prefix of display (common case: "SMITH JOHN"
    # matching "SMITH JOHN A" — the decedent with a middle initial)
    if display.upper().startswith(query.upper()):
        score = max(score, 0.85)
    return score


def _pick_best_name_match(
    query: str, rows: list[tuple[str, str, int]], min_score: float = 0.6,
) -> tuple[str, str, int] | None:
    """Pick the display name most likely to be the same person as the query.

    Returns (display, checkbox_value, count). Prefers exact match, then
    prefix match, then substring. Rejects rows that look like LLCs / trusts
    / unrelated aggregates when the query is a personal name.
    """
    if not rows:
        return None

    q_upper = query.upper()
    corp_re = re.compile(r"\b(LLC|INC|CORP|COMPANY|BANK|CHURCH|TRUST|LP|LTD)\b")
    query_is_personal = not corp_re.search(q_upper)

    scored: list[tuple[float, str, str, int]] = []
    for display, value, count in rows:
        if query_is_personal and corp_re.search(display.upper()):
            continue
        s = _score_display_name(query, display)
        if s >= min_score:
            scored.append((s, display, value, count))

    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], -x[3]))  # score desc, then count desc
    _, display, value, count = scored[0]
    return display, value, count


def _fetch_deed_list(
    opener: urllib.request.OpenerDirector, checkbox_value: str,
) -> str:
    """Step 2: POST dlist.php with the server's exact checkbox value."""
    data = urllib.parse.urlencode({
        "InstDetail[paname][]": checkbox_value,
        "cnum": JCD_CNUM_DLIST,
        "bdate": "", "edate": "",
    }).encode("utf-8")
    _delay()
    try:
        req = urllib.request.Request(
            JCD_DLIST_URL, data=data, method="POST",
            headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{JCD_BASE_URL}/p3.php",
            },
        )
        with opener.open(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("JCD dlist fetch failed: %s", exc)
        return ""


# dlist.php record parsing. Records are rendered as table blocks linked to
# pdetail.php via <a href="pdetail.php?instnum=...&year=...&db=&cnum=20">.
# Structure of one block (whitespace collapsed):
#   <a href="pdetail.php?instnum=XXXX&year=YYYY&db=&cnum=20">VIEW DETAILS</a>
#   <td>GRANTOR</td>
#   <td>GRANTEE</td>
#   <td>INSTNUM</td>
#   <td>MM/DD/YYYY</td>
#   <td>L BBBB PPP</td>  (book/page)
#   <td>DOC TYPE</td>
#   <td>Y/N</td>  (image on file)
#   ... optionally Xref block ...
_DLIST_PDETAIL_A = re.compile(
    r"""href=['"]?pdetail\.php\?instnum=(\d+)&(?:amp;)?year=(\d+)&(?:amp;)?db=(\d*)&(?:amp;)?cnum=(\d+)['"]?""",
    re.IGNORECASE,
)


def _parse_deed_list(html: str) -> list[DeedRecord]:
    """Parse dlist.php HTML into DeedRecord rows.

    Each record on the dlist page is rendered as its own ``<table border=3>``
    with a single TR of 9 cells:
        [0] Image?  [1] Details  [2] Grantor/Debtor  [3] Grantee/Secured Party
        [4] Legal Desc  [5] Date  [6] Book Info / FileNum  [7] Document Type
        [8] XRef
    The first such table is the column header; skip it.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("JCD: bs4 not available — dlist parsing skipped")
        return []

    soup = BeautifulSoup(html, "html.parser")
    records: list[DeedRecord] = []

    for table in soup.find_all("table", border="3"):
        tr = table.find("tr")
        if not tr:
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 9:
            continue

        def cell_text(td) -> str:
            return re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip()

        # Skip the header row (contains literal 'Grantor/Debtor' text)
        c0, c1, c2 = cell_text(cells[0]), cell_text(cells[1]), cell_text(cells[2])
        if c0 == "Image?" or c2 == "Grantor/Debtor":
            continue

        # Details cell contains the pdetail.php anchor
        details_a = cells[1].find("a", href=re.compile(r"pdetail\.php"))
        if not details_a:
            continue
        href = details_a.get("href", "")
        m = re.search(
            r"instnum=(\d+)&(?:amp;)?year=(\d+)&(?:amp;)?db=(\d*)&(?:amp;)?cnum=(\d+)",
            href,
        )
        if not m:
            continue
        instnum, year, db, cnum = m.group(1), m.group(2), m.group(3) or "", m.group(4)

        # Image cell — viewimg.php?img=...&type=pdf
        view_img = ""
        img_a = cells[0].find("a", href=re.compile(r"viewimg\.php"))
        if img_a:
            img_m = re.search(r"img=([A-Za-z0-9+/=]+)", img_a.get("href", ""))
            if img_m:
                view_img = img_m.group(1)

        grantor = cell_text(cells[2])
        grantee = cell_text(cells[3])
        legal_desc = cell_text(cells[4])
        date_raw = cell_text(cells[5])
        filed_date = _normalize_date(date_raw) if re.match(r"\d{2}/\d{2}/\d{4}", date_raw) else ""
        book_page = cell_text(cells[6])
        doc_type = cell_text(cells[7])

        # XRef cell — holds cross-referenced instrument numbers, often as a
        # nested table of (Inst#, Inst Desc, Year, XRef Book, XRef Page).
        xref_text = cell_text(cells[8])
        xrefs = re.findall(r"\b(\d{9,14})\b", xref_text)

        detail_url = f"{JCD_DETAIL_URL}?instnum={instnum}&year={year}&db={db}&cnum={cnum}"
        records.append(DeedRecord(
            instnum=instnum,
            year=year,
            db=db,
            filed_date=filed_date,
            book_page=book_page,
            doc_type=doc_type,
            grantor=grantor,
            grantee=grantee,
            legal_desc=legal_desc,
            detail_url=detail_url,
            view_img=view_img,
            xrefs=list(dict.fromkeys(xrefs)),
        ))
    return records


def _fetch_pdetail(
    opener: urllib.request.OpenerDirector, detail_url: str,
) -> dict[str, str]:
    """GET a pdetail.php page and extract labelled fields.

    Returns dict with keys: 'mort_amount', 'tax_amount', 'trans_amount',
    'doc_type', 'file_date', 'book', 'page', 'instrument_date'.
    """
    _delay()
    try:
        req = urllib.request.Request(detail_url, headers={
            "User-Agent": _USER_AGENT,
            "Referer": JCD_DLIST_URL,
        })
        with opener.open(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.debug("JCD pdetail fetch failed: %s", exc)
        return {}

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    def grab(pattern: str) -> str:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    return {
        "mort_amount": grab(r"Mort\s*\$\s*([\d,.]+)"),
        "tax_amount":  grab(r"Tax\s*\$\s*([\d,.]+)"),
        "trans_amount": grab(r"Trans\s*\$\s*([\d,.]+)"),
        "doc_type":    grab(r"Doc\s+Type[:\s]+([A-Z][A-Z &/]+)"),
        "file_date":   grab(r"File\s+Date[:\s]+(\d{2}/\d{2}/\d{4})"),
        "book":        grab(r"Book\s*#\s*(\d+)"),
        "page":        grab(r"Page\s*#\s*(\d+)"),
        "instrument_date": grab(r"Instrument\s+Date[:\s]+(\d{2}/\d{2}/\d{4})"),
    }


# Doc-type classification
_MORTGAGE_TYPES = {"MORTGAGE", "MTG", "FIXTURE FILING", "ASSG OF MTG", "ASGN OF MTG"}
_RELEASE_TYPES = {"REL MTG", "RELEASE OF MORTGAGE", "MTG RELEASE", "REL OF MTG"}
_DEED_TYPES = {"DEED", "WARRANTY DEED", "QUIT CLAIM", "GRANT DEED"}


def _classify(doc_type: str) -> str:
    """Group a JCD document type string into a coarse category.

    JCD stores types in short abbreviations: "MTG ELEC REGIST" (mortgage with
    MERS), "REL MTG" or bare "RELEASE" (satisfaction of mortgage), plain
    "DEED", etc. The 6-char abbreviations in the Instrument Type dropdown
    are aliases for the same stored values.
    """
    up = doc_type.upper().strip()
    if not up:
        return "other"
    release_hints = (
        "REL MTG", "RELEASE OF MORTGAGE", "REL OF MTG", "MTG RELEASE",
        "RELEASE",  # catch-all; JCD also uses bare "RELEASE" for mortgage releases
        "REL ",     # "REL STATE LIEN", "REL FIX", etc.
        "SATISFACTION",
    )
    if any(t in up for t in release_hints):
        return "release"
    if "MORTGAGE" in up or re.search(r"\bMTG\b", up):
        return "mortgage"
    if "DEED" in up:
        return "deed"
    if "LIEN" in up:
        return "lien"
    return "other"


def _parse_money(raw: str) -> int:
    """'$123,456.78' or '123,456' → int dollars (truncated). 0 on failure."""
    if not raw:
        return 0
    cleaned = re.sub(r"[^\d.]", "", raw)
    if not cleaned:
        return 0
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def _amortized_balance(
    original_amount: int, origination_date: str,
    term_years: int = _MORTGAGE_TERM_YEARS, annual_rate: float = _MORTGAGE_RATE,
    as_of: datetime | None = None,
) -> int:
    """Remaining principal on a fully-amortizing fixed-rate mortgage.

    Formula: B(t) = P * (((1+r)^n - (1+r)^t) / ((1+r)^n - 1))
    where r = monthly rate, n = total months, t = months elapsed.
    """
    if original_amount <= 0 or not origination_date:
        return 0
    try:
        origin = datetime.strptime(origination_date, "%Y-%m-%d")
    except ValueError:
        return 0
    now = as_of or datetime.now()
    months_elapsed = max(0, (now.year - origin.year) * 12 + (now.month - origin.month))
    n = term_years * 12
    if months_elapsed >= n:
        return 0
    r = annual_rate / 12.0
    if r == 0:
        return max(0, original_amount - int(original_amount * months_elapsed / n))
    factor_n = (1 + r) ** n
    factor_t = (1 + r) ** months_elapsed
    remaining = original_amount * (factor_n - factor_t) / (factor_n - 1)
    return int(max(0, remaining))


# Number of months back to consider a deed transfer "recent" for heir-
# property detection. Probate properties that have been transferred to
# heirs in this window are still fresh REI opportunities (the new heir
# often wants to sell inherited property quickly).
_HEIR_TRANSFER_LOOKBACK_MONTHS = 24


def _surname(name: str) -> str:
    """Extract a best-guess surname for a name string. Returns upper-case.

    Three input formats seen in practice:
      * 'SMITH, DOLLY'         — comma format (KCOJ decedents). Surname first.
      * 'SMITH JANE'           — all-caps LAST FIRST (JCD deed grantors/grantees).
                                  Surname first.
      * 'Jane Smith'           — natural order. Surname last.
    The heuristic: comma first, else all-caps → surname-first, else surname-last.
    """
    if "," in name:
        return name.split(",", 1)[0].strip().upper()
    tokens = [t for t in re.split(r"\s+", name.strip()) if t]
    if not tokens:
        return ""
    # JCD deed records are uniformly upper-case with LAST FIRST token order.
    # If the full string is upper-case alpha, treat first token as surname.
    letters_only = re.sub(r"[^A-Za-z]", "", name)
    if letters_only and letters_only.isupper():
        return tokens[0].upper()
    return tokens[-1].upper()


# Patterns that indicate a non-individual title holder (trust, LLC, estate).
# Used by _find_current_holder to tag the holder relationship.
_TRUST_RE = re.compile(r"\bTRUST\b|\bTRUSTEE\b|\bLIVING\s+TRUST\b", re.IGNORECASE)
_ENTITY_RE = re.compile(r"\b(?:LLC|INC|CORP|CO\.?|LP|LTD|FOUNDATION)\b", re.IGNORECASE)
_ESTATE_OF_RE = re.compile(r"\bESTATE\s+OF\b", re.IGNORECASE)


def _clean_holder_name(raw: str) -> str:
    """Tidy a deed grantee string for use as a downstream search target.

    JCD often emits doubled phrases like 'ROBERT G REAGAN TRUST REAGAN ROBERT G TRUST'
    where the trust appears twice (one form per side of the conveyance). Detect
    that pattern by splitting on whitespace and finding a repeated TRUST token,
    then return only the first half.
    """
    s = re.sub(r"\s+", " ", raw).strip()
    # Detect a repeated phrase: split into tokens, find if the first half == second half
    tokens = s.split()
    if len(tokens) >= 4 and len(tokens) % 2 == 0:
        half = len(tokens) // 2
        if tokens[:half] == tokens[half:]:
            s = " ".join(tokens[:half])
    # Detect "X TRUST Y TRUST" duplication where X and Y are reorderings of the same name
    m = re.match(r"^(.+?\bTRUST\b)\s+(.+?\bTRUST\b)$", s, re.IGNORECASE)
    if m:
        first, second = m.group(1).strip(), m.group(2).strip()
        # If the two halves share the same canonical token set, keep only one
        first_tokens = set(re.findall(r"\b[A-Za-z]+\b", first.upper()))
        second_tokens = set(re.findall(r"\b[A-Za-z]+\b", second.upper()))
        if first_tokens == second_tokens and first_tokens:
            s = first  # Either half is fine; first is the natural order
    return s


# Maximum age (months) for a "decedent-as-grantor" deed to be considered
# a recent post-death transfer. Older grantor-out deeds are pre-death sales
# (decedent didn't own at time of death) and should NOT trigger heir lookup.
# 36 months = 3 years; covers cases where probate filed up to ~2 years
# after death, plus a buffer for the deed-recording lag.
_RECENT_TRANSFER_MAX_AGE_MONTHS = 36


def _find_current_holder(
    records: list[DeedRecord],
    decedent_name: str,
    as_of: datetime | None = None,
) -> tuple[str, str, str] | None:
    """Walk the deed chain to identify who currently holds title.

    Strategy: among all records classified as 'deed' (transfers of title)
    where the decedent appears as either grantor or grantee, find the
    MOST RECENT one. The current title holder is:
      * the GRANTEE of that deed (if recent-enough transfer FROM decedent
        or their estate to a trust / heir / buyer)
      * the decedent themselves (if most recent deed has them as grantee —
        i.e., they received title and never transferred out)

    Date guardrail: if the most recent grantor-out deed is older than
    ``_RECENT_TRANSFER_MAX_AGE_MONTHS``, return None — the decedent gave
    up title long before death and the grantee is an unrelated buyer, not
    an heir/trust.

    Returns (holder_name, relationship, deed_date) or None. ``relationship``
    is one of:
      * "self"        — decedent is the most recent grantee, still holds title
      * "trust"       — title transferred to a trust the decedent created
      * "heir_recent" — title transferred to an individual heir (probably family)
    """
    if not decedent_name.strip():
        return None
    dec_surname = _surname(decedent_name)
    if not dec_surname:
        return None

    # Filter to actual deeds (skip mortgages, liens, releases) where the
    # decedent's surname appears in either grantor or grantee.
    candidates: list[DeedRecord] = []
    for rec in records:
        if _classify(rec.doc_type) != "deed":
            continue
        if not rec.filed_date:
            continue
        grantor_u = rec.grantor.upper()
        grantee_u = rec.grantee.upper()
        if dec_surname in grantor_u or dec_surname in grantee_u:
            candidates.append(rec)

    if not candidates:
        return None
    candidates.sort(key=lambda r: r.filed_date, reverse=True)
    most_recent = candidates[0]

    grantor_u = most_recent.grantor.upper()
    grantee_u = most_recent.grantee.upper()
    decedent_was_grantor = dec_surname in grantor_u
    decedent_was_grantee = dec_surname in grantee_u

    if decedent_was_grantee and not decedent_was_grantor:
        # Decedent received title and didn't transfer it out — they still hold.
        return (decedent_name, "self", most_recent.filed_date)

    # Decedent (or their estate) gave up title. Apply date guardrail:
    # transfers older than the cutoff were pre-death sales, not heir
    # transfers. The grantee is an unrelated buyer; decedent didn't
    # own at time of death.
    now = as_of or datetime.now()
    cutoff = now - timedelta(days=_RECENT_TRANSFER_MAX_AGE_MONTHS * 31)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    if most_recent.filed_date < cutoff_str:
        # Old sale — decedent has no current property here.
        return None

    holder = _clean_holder_name(most_recent.grantee)
    if not holder:
        return None

    if _TRUST_RE.search(holder) or _ENTITY_RE.search(holder):
        relationship = "trust"
    else:
        relationship = "heir_recent"
    return (holder, relationship, most_recent.filed_date)


def _find_recent_transfer(
    records: list[DeedRecord], decedent_name: str,
    lookback_months: int = _HEIR_TRANSFER_LOOKBACK_MONTHS,
    as_of: datetime | None = None,
) -> DeedRecord | None:
    """Find a deed where the decedent transferred property within the window.

    Looks for records classified as 'deed' where the grantor contains the
    decedent's name (substring, case-insensitive). Returns the most recent
    such record within ``lookback_months``, or None.

    Note: we look at GRANTOR — the party giving up ownership — because in a
    post-death transfer the decedent (or "ESTATE OF <decedent>") is the
    grantor and the heir is the grantee.
    """
    if not decedent_name.strip():
        return None
    now = as_of or datetime.now()
    cutoff = now.replace(day=1) - timedelta(days=lookback_months * 31)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    dec_surname = _surname(decedent_name)
    if not dec_surname:
        return None

    candidates: list[DeedRecord] = []
    for rec in records:
        if _classify(rec.doc_type) != "deed":
            continue
        if not rec.filed_date or rec.filed_date < cutoff_str:
            continue
        grantor_upper = rec.grantor.upper()
        # Must mention the decedent's surname. ESTATE OF transfers typically
        # have grantor strings like "ESTATE OF SMITH DOLLY" or just
        # "SMITH DOLLY" (pre-probate transfer).
        if dec_surname in grantor_upper:
            candidates.append(rec)

    if not candidates:
        return None
    # Most recent wins
    candidates.sort(key=lambda r: r.filed_date, reverse=True)
    return candidates[0]


def _choose_active_mortgage(
    records: list[DeedRecord],
) -> DeedRecord | None:
    """Return the most recent mortgage that hasn't been released.

    Two release-detection signals:
      1. A RELEASE record in ``records`` whose xref list contains the
         mortgage's instnum (explicit release within the same owner's
         deed history).
      2. The mortgage's own xref list contains an instnum that looks
         YYYY-prefixed and later than the mortgage's own filed year
         (implicit release: releases are typically filed by the lender
         so they won't appear under the decedent's name, but the
         mortgage record is updated to cross-reference the release).
    """
    mortgages = [r for r in records if _classify(r.doc_type) == "mortgage"]
    releases = [r for r in records if _classify(r.doc_type) == "release"]

    # Signal 1: explicit release records
    released_instnums: set[str] = set()
    for rel in releases:
        released_instnums.update(rel.xrefs)

    def is_released(m: DeedRecord) -> bool:
        if m.instnum in released_instnums:
            return True
        # Signal 2: the mortgage's own xrefs include a later YYYY* instnum.
        # JCD instrument numbers start with the 4-digit year (e.g. 2017214642).
        mortgage_year = int(m.year) if m.year.isdigit() else 0
        for xref in m.xrefs:
            if len(xref) >= 4 and xref[:4].isdigit():
                xref_year = int(xref[:4])
                if xref_year > mortgage_year:
                    return True
        return False

    active = [m for m in mortgages if not is_released(m)]
    if not active:
        return None
    active.sort(key=lambda r: r.filed_date, reverse=True)
    return active[0]


def lookup_owner_deed_history(
    notice: NoticeData,
    opener: urllib.request.OpenerDirector | None = None,
) -> None:
    """Populate mortgage_* fields on ``notice`` from JCD deed history.

    Uses ``notice.decedent_name`` (for probate) or ``notice.owner_name``
    (otherwise). Runs the 2-step p3.php → dlist.php flow, picks the
    best-matching owner row, classifies deeds, and estimates remaining
    mortgage balance via straight-line amortization of the most recent
    unreleased mortgage.
    """
    query = notice.decedent_name.strip() or notice.owner_name.strip()
    if not query:
        return

    # Normalize KCOJ-style "LAST, FIRST MIDDLE" to "LAST FIRST MIDDLE" and
    # strip suffixes. JCD owner search wants space-separated tokens.
    query = SUFFIX_RE.sub("", query).strip()
    query = query.replace(",", " ")
    query = re.sub(r"\s+", " ", query).strip()

    own = opener is None
    if own:
        opener = _make_opener()
        _accept_disclaimer(opener)

    # Drive the deed name search off the resolver's ordered variant set
    # (NAME-01) instead of a single normalized query. Maiden/prior/co-borrower
    # surnames surface mortgage/lien chains indexed under a name other than the
    # decedent's current one (fixes the Burkhart "indexed only under wife MARY"
    # miss). Maiden/aka context comes from the obituary step (Plan 03); getattr
    # falls back to None so this still works when obituary didn't run.
    #
    # CourtNet name->case DEPENDENCY (spec section 3 — documented, NOT solved
    # here): the resolver feeds name->case discovery for the PVA + deeds
    # surfaces. The KCOJ/CourtNet docket layer cannot be re-searched by a
    # maiden/prior name today because `kcoj_case_detail.search_case` is
    # case-number-only (no by-name search exists). Wiring a CourtNet by-name
    # search is a separate task (parent plan 2c); the maiden re-search benefit
    # at the docket layer is therefore a documented downstream dependency.
    src_name = (notice.decedent_name or notice.owner_name or "").strip()
    variants = generate_variants(
        src_name,
        maiden_name=getattr(notice, "decedent_obit_maiden_name", None) or None,
        prior_surnames=getattr(notice, "decedent_also_known_as", None) or None,
    )
    # Fall back to the cleaned single query if the resolver yields nothing.
    variant_values = [v.value for v in variants] or [query]

    try:
        best = None
        matched_query = query
        rows: list[tuple[str, str, int]] = []
        # Loop variants highest-confidence-first; stop on the FIRST variant
        # whose best row clears the existing 0.6 floor.
        for vq in variant_values:
            rows = _search_names_unique(opener, vq)
            best = _pick_best_name_match(vq, rows)
            if best:
                matched_query = vq
                break
        if not best:
            logger.info("JCD: no deed-list match for %r (checked %d rows)", query, len(rows))
            return
        display, checkbox_value, count = best
        logger.info("JCD: matched %r → %r (%d records)", matched_query, display, count)

        html = _fetch_deed_list(opener, checkbox_value)
        if not html:
            return

        records = _parse_deed_list(html)
        logger.debug("JCD: parsed %d deed records for %r", len(records), display)

        # Current-title-holder detection: walk the deed chain to identify
        # who currently holds title to the decedent's property. This is
        # the primary search target for the downstream PVA lookup —
        # bypasses the "PVA-by-decedent-name" miss rate that plagued
        # earlier runs (trust-held, estate-titled, joint-with-spouse cases
        # all resolve through here).
        holder = _find_current_holder(records, notice.decedent_name or display)
        if holder:
            holder_name, relationship, holder_date = holder
            notice.current_property_holder = holder_name
            notice.current_holder_relationship = relationship
            logger.info(
                "JCD: current holder for %r -> %r (%s, deed %s)",
                display, holder_name, relationship, holder_date,
            )

        # Extract parcel ID directly from deed legal descriptions. Many
        # JCD records include the 12-char Jefferson PIDN embedded in the
        # legal_desc text (e.g. "4-6-16 23088900270000 ACME WAY") — no OCR
        # needed, much more reliable than the mortgage-PDF extraction.
        # Take the PIDN from the most recent deed where decedent is a party.
        if not notice.deed_discovered_parcel_id.strip():
            dec_surname = _surname(notice.decedent_name or display)
            for rec in sorted(records, key=lambda r: r.filed_date, reverse=True):
                if _classify(rec.doc_type) not in ("deed", "mortgage"):
                    continue
                if dec_surname and (dec_surname not in rec.grantor.upper()
                                    and dec_surname not in rec.grantee.upper()):
                    continue
                pid_m = re.search(r"\b(\d{3}[A-Z]?\d{7,9})\b", rec.legal_desc)
                if pid_m and len(re.sub(r"\D", "", pid_m.group(1))) >= 11:
                    notice.deed_discovered_parcel_id = pid_m.group(1)
                    logger.info(
                        "JCD: parcel id from deed legal_desc: %s (deed %s)",
                        pid_m.group(1), rec.filed_date,
                    )
                    break

        # Heir-transfer detection (within 24 months): retains its original
        # purpose of flagging recent family transfers as a same-surname
        # signal. Phase 2a will use ``current_property_holder`` for the
        # actual PVA search; this is supplementary metadata.
        transfer = _find_recent_transfer(records, notice.decedent_name or display)
        if transfer and transfer.grantee:
            notice.heir_transferred_to = transfer.grantee
            notice.heir_transfer_date = transfer.filed_date
            dec_surname = _surname(notice.decedent_name or display)
            grantee_surname = _surname(transfer.grantee)
            if dec_surname and dec_surname == grantee_surname:
                notice.heir_same_surname = "yes"
            logger.info(
                "JCD: recent transfer found for %r: grantor=%r -> grantee=%r on %s%s",
                display, transfer.grantor, transfer.grantee, transfer.filed_date,
                " (same-surname)" if notice.heir_same_surname == "yes" else "",
            )

        active_mtg = _choose_active_mortgage(records)
        if not active_mtg:
            # Distinguish "found mortgages but all released" (real $0 signal)
            # from "no mortgage records at all" (unknown, leave empty so the
            # equity estimator falls back to 85%-of-assessed).
            mortgage_count = sum(1 for r in records if _classify(r.doc_type) == "mortgage")
            if mortgage_count > 0:
                logger.info(
                    "JCD: %r had %d mortgage(s) — all released, setting balance=0",
                    display, mortgage_count,
                )
                notice.mortgage_balance_estimate = "0"
                notice.mortgage_origination_date = ""
                notice.mortgage_original_amount = "0"
            else:
                logger.info("JCD: no mortgage records at all for %r", display)
            return

        # Fetch dollar amount from the pdetail page
        detail = _fetch_pdetail(opener, active_mtg.detail_url)
        original = _parse_money(detail.get("mort_amount", ""))
        if original <= 0:
            logger.info(
                "JCD: active mortgage %s has no dollar amount (doc_type=%r)",
                active_mtg.instnum, active_mtg.doc_type,
            )
            return

        balance = _amortized_balance(original, active_mtg.filed_date)
        notice.mortgage_original_amount = str(original)
        notice.mortgage_origination_date = active_mtg.filed_date
        notice.mortgage_balance_estimate = str(balance)
        logger.info(
            "JCD: %r mortgage $%s (%s) → estimated balance $%s",
            display, f"{original:,}", active_mtg.filed_date, f"{balance:,}",
        )

        # OCR the active mortgage PDF to extract the property's street
        # address and parcel ID. PVA owner-search frequently misses the
        # decedent (married-name records, middle-initial variants, etc.);
        # the address is a more reliable second-tier lookup key. Cheap
        # because we only OCR when there's an active mortgage to begin with.
        if active_mtg.view_img and not notice.deed_discovered_address.strip():
            try:
                addr, city, zipc, parcel_id, src = _fetch_address_from_document(
                    active_mtg.view_img,
                )
                if addr:
                    notice.deed_discovered_address = addr
                    logger.info(
                        "JCD: OCR'd address from mortgage %s: %r (source=%s)",
                        active_mtg.instnum, addr, src,
                    )
                if parcel_id:
                    notice.deed_discovered_parcel_id = parcel_id
                    logger.info(
                        "JCD: OCR'd parcel id from mortgage %s: %s",
                        active_mtg.instnum, parcel_id,
                    )
            except Exception as exc:
                logger.debug("JCD: OCR for mortgage %s failed: %s",
                             active_mtg.instnum, exc)
    finally:
        pass  # opener has no explicit close; cookies expire with process


def enrich_mortgage_balances(notices: list[NoticeData]) -> None:
    """Phase 2b entry point: populate mortgage_* fields on Jefferson records.

    Target set: Jefferson County records where we have *something* to search
    by — decedent_name (probate) or owner_name. Records whose PVA lookup
    already populated a street address are prioritized since a confirmed
    property adds signal, but we don't gate on it: a match against the deed
    history still tells us whether the decedent had an active mortgage.
    """
    candidates = [
        n for n in notices
        if n.county.lower() == "jefferson"
        and (n.decedent_name.strip() or n.owner_name.strip())
        and not n.mortgage_balance_estimate.strip()
    ]
    if not candidates:
        return

    logger.info("JCD: Phase 2b deed lookup for %d Jefferson record(s)", len(candidates))
    opener = _make_opener()
    _accept_disclaimer(opener)

    for notice in candidates:
        try:
            lookup_owner_deed_history(notice, opener=opener)
        except Exception as exc:
            logger.warning(
                "JCD: deed lookup failed for %r: %s",
                notice.decedent_name or notice.owner_name, exc,
            )
