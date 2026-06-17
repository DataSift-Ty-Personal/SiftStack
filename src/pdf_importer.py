"""Process scanned tax sale PDF files into NoticeData records.

Renders PDF pages to images via pypdfium2, rotates upside-down pages,
runs Tesseract OCR, then parses tabular data using Claude Haiku (with
regex fallback) to extract parcel_id, address, and owner_name per row.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

import config
from notice_parser import NoticeData
from image_utils import fix_rotation, ocr_page

logger = logging.getLogger(__name__)

# Street suffix patterns for regex parser (subset of common ones)
_SUFFIXES = (
    r"(?:Ave(?:nue)?|Blvd|Cir(?:cle)?|Ct|Dr(?:ive)?|Hwy|Ln|Loop|"
    r"Park|Pk|Pike|Pkwy|Pl|Point|Pt|Rd|Ridge|Row|Run|"
    r"St|Ter(?:race)?|Trl|Trail|Way)"
)

# Parcel ID pattern: 3 digits + optional 1-2 letters + dash + 2-5 digits + optional letter
PARCEL_RE = re.compile(r"(\d{3}[A-Z]{0,2}-\d{2,5}[A-Z]?)", re.IGNORECASE)

# Full row regex: optional row number, parcel, address (up to suffix), then owner
ROW_RE = re.compile(
    r"(?:^\d{1,4}\s+)?"              # optional row number
    r"(\d{3}[A-Z]{0,2}-\d{2,5}[A-Z]?)"  # parcel ID (group 1)
    r"\s+"
    r"(\d+\s+[\w\s.,'#-]+?"          # address: house number + street
    + _SUFFIXES +
    r"\.?(?:\s*#\s*\w+)?)"           # optional unit
    r"\s{2,}"                         # 2+ spaces separating address from owner
    r"(.+)",                          # owner name (group 3)
    re.IGNORECASE,
)

# LLM prompt template for structured extraction
LLM_PROMPT = """You are parsing OCR text from a scanned tax sale property list for {county} County, Tennessee.
The table has these columns (left to right):
1. Row number (may be missing or garbled — ignore it)
2. Parcel ID (format: digits, optional letters, dash, digits — e.g. "003-04913", "005LB-00801", "018AA-022")
3. Street address (house number + street name, e.g. "8428 Graceland Rd") — NO city, state, or zip
4. Owner name (one or more names, e.g. "Mashburn Mell & Rosa", "Zaccone Curtis Gabriel Jr")

IMPORTANT: Some entries span MULTIPLE lines in the OCR text. For example:
175 060-06701
6328 Millertown Pike
Shelton Doyle
This is ONE property: parcel_id="060-06701", address="6328 Millertown Pike", owner_name="Shelton Doyle".
A new entry starts when you see a new parcel ID (digits-dash-digits pattern).

The text contains OCR artifacts from table borders: |, ~, *, and Unicode garbage. Ignore these.
Some addresses use "0" as the house number (vacant land). Include these.
The "%" symbol in owner names means "care of".

Extract EVERY property row. Return a JSON array of objects with these exact keys:
- "parcel_id": the parcel ID exactly as written (include the dash)
- "address": the street address only (no city/state/zip)
- "owner_name": the property owner name(s), cleaned up

Skip header rows, page numbers, and lines that are clearly OCR noise.
If a field is unreadable, use "".
Return ONLY a valid JSON array. No markdown fences, no explanation.

OCR text:
{ocr_text}"""


def render_pdf_pages(pdf_path: Path, dpi: int = 200) -> list[Image.Image]:
    """Render each PDF page to a PIL Image at the specified DPI."""
    doc = pdfium.PdfDocument(str(pdf_path))
    images = []
    for i in range(len(doc)):
        page = doc[i]
        scale = dpi / 72  # PDF default is 72 DPI
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        images.append(pil_image)
    doc.close()
    logger.info("Rendered %d pages from %s at %d DPI", len(images), pdf_path.name, dpi)
    return images



def _has_parcel_id(text: str) -> bool:
    """Check if text contains a parcel ID, tolerating OCR digit/letter swaps."""
    # Normalize common OCR confusions for matching only: O→0, l→1
    normalized = re.sub(r"(?<=[0-9])O|O(?=[0-9])", "0", text)
    normalized = re.sub(r"(?<=[0-9])l|l(?=[0-9])", "1", normalized)
    return bool(PARCEL_RE.search(normalized))


def merge_continuation_lines(ocr_text: str) -> str:
    """Merge multi-line OCR entries into single lines.

    Some table rows get split across 2-3 OCR lines. A new entry starts when
    a line contains a parcel ID pattern (digits-dash-digits). Lines without
    a parcel ID are continuations of the previous entry.
    """
    merged_lines = []
    for line in ocr_text.split("\n"):
        stripped = re.sub(r"[|~=*]", " ", line).strip()
        if not stripped:
            continue
        # A line is a new entry if it contains a parcel ID anywhere
        if _has_parcel_id(stripped) or not merged_lines:
            merged_lines.append(stripped)
        else:
            # Continuation line — append to previous
            merged_lines[-1] += "  " + stripped
    return "\n".join(merged_lines)


def parse_page_llm(ocr_text: str, county: str, api_key: str) -> list[dict]:
    """Parse OCR text using Claude Haiku for structured extraction."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    prompt = LLM_PROMPT.format(county=county, ocr_text=ocr_text[:12000])

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system="You extract structured data from OCR text. Return ONLY valid JSON arrays.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        rows = json.loads(raw)
        if not isinstance(rows, list):
            logger.warning("LLM returned non-list: %s", type(rows))
            return []
        return rows
    except json.JSONDecodeError as e:
        logger.warning("LLM JSON parse error: %s", e)
        return []
    except Exception as e:
        logger.warning("LLM extraction failed: %s", e)
        return []


def parse_page_regex(ocr_text: str) -> list[dict]:
    """Parse OCR text using regex as fallback."""
    rows = []
    for line in ocr_text.split("\n"):
        # Clean OCR artifacts
        line = re.sub(r"[|~=*\x00-\x1f]", " ", line).strip()
        if not line or len(line) < 10:
            continue

        m = ROW_RE.match(line)
        if m:
            rows.append({
                "parcel_id": m.group(1).strip(),
                "address": m.group(2).strip(),
                "owner_name": m.group(3).strip(),
            })
            continue

        # Simpler fallback: just find parcel ID and split the rest
        pm = PARCEL_RE.search(line)
        if pm:
            parcel = pm.group(1)
            rest = line[pm.end():].strip()
            # Try to split at 2+ spaces (column gap)
            parts = re.split(r"\s{3,}", rest, maxsplit=1)
            if len(parts) == 2:
                rows.append({
                    "parcel_id": parcel,
                    "address": parts[0].strip(),
                    "owner_name": parts[1].strip(),
                })
            elif rest:
                rows.append({
                    "parcel_id": parcel,
                    "address": rest,
                    "owner_name": "",
                })
    return rows


def validate_row(row: dict) -> bool:
    """Validate an extracted row has reasonable data."""
    parcel = row.get("parcel_id", "").strip()
    address = row.get("address", "").strip()
    owner = row.get("owner_name", "").strip()

    # Must have a parcel ID with at least a dash
    if not parcel or "-" not in parcel:
        return False
    # Address should exist (can be "0 Street" for vacant land)
    if not address:
        return False
    # Owner can be empty (we'll still have the property)
    return True


def process_pdf(
    pdf_path: Path,
    county: str,
    api_key: str | None = None,
    date_added: str | None = None,
    regex_only: bool = False,
) -> list[NoticeData]:
    """Process a scanned tax sale PDF into a list of NoticeData records.

    Args:
        pdf_path: Path to the PDF file.
        county: "Knox" or "Blount".
        api_key: Anthropic API key for LLM parsing (optional).
        date_added: Date string (YYYY-MM-DD) for records. Defaults to today.
        regex_only: If True, skip LLM and use regex only.

    Returns:
        List of NoticeData objects ready for enrichment.
    """
    if date_added is None:
        date_added = config.run_date()

    default_city = "Knoxville" if county.lower() == "knox" else "Maryville"
    use_llm = api_key and not regex_only

    logger.info("Processing PDF: %s (%s County)", pdf_path.name, county)
    logger.info("Parsing mode: %s", "LLM + regex fallback" if use_llm else "regex only")

    # Render and OCR
    images = render_pdf_pages(pdf_path)
    all_rows: list[dict] = []

    for page_num, image in enumerate(images, 1):
        logger.info("  Page %d/%d: rotating + OCR...", page_num, len(images))
        corrected = fix_rotation(image)
        raw_text = ocr_page(corrected)
        text = merge_continuation_lines(raw_text)

        # Parse
        rows = []
        if use_llm:
            rows = parse_page_llm(text, county, api_key)
            if not rows:
                logger.info("  Page %d: LLM failed, falling back to regex", page_num)
                rows = parse_page_regex(text)
        else:
            rows = parse_page_regex(text)

        # Validate
        valid = [r for r in rows if validate_row(r)]
        logger.info("  Page %d: %d rows extracted (%d valid)", page_num, len(rows), len(valid))

        for r in valid:
            r["_page"] = page_num
        all_rows.extend(valid)

        # Free memory
        del corrected, image

    # Convert to NoticeData
    notices = []
    for row in all_rows:
        notice = NoticeData(
            address=row["address"],
            city=default_city,
            state="TN",
            owner_name=row.get("owner_name", ""),
            notice_type="tax_sale",
            county=county,
            parcel_id=row["parcel_id"],
            date_added=date_added,
            source_url=f"pdf://{pdf_path.name}#page={row.get('_page', 0)}",
        )
        notices.append(notice)

    logger.info("Total: %d valid property records from %d pages", len(notices), len(images))
    return notices
