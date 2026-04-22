"""Scraper for Jefferson County Clerk online deed records (Louisville, KY).

Uses simple HTTP POST — no login, no CAPTCHA, no Playwright required.
Site: https://search.jeffersondeeds.com/

Searches for LIS PENDENS (pre-foreclosure) filings by date range.
Each filing contains: grantor (debtor/owner), grantees (lenders/plaintiffs),
legal description, case number, and filing date.

NOTE: Louisville legal descriptions are metes-and-bounds or subdivision-lot
format. They do NOT include street numbers. `address` is left blank; the
enrichment pipeline can resolve the property address via the Jefferson
County PVA or Smarty geocoding from the legal description.
"""

import logging
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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
