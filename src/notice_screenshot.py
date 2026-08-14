"""Capture and host proof-of-source screenshots of notice detail pages.

During scraping, right after the reCAPTCHA is solved and the legal notice is
visible on tnpublicnotice.com, we save a full-page PNG of the notice. The image
is hosted (Google Drive in CLI runs, the Apify key-value store in the cloud) and
linked from the DataSift record (Notes + the "Notice Screenshot" custom field),
so the actual published notice travels with the lead and adds legitimacy to
outreach.

Capture is best-effort: a screenshot failure must never drop a record.
"""

import json
import logging
import re
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def _screenshot_filename(notice_id: str = "", address: str = "") -> str:
    """Build a filesystem-safe PNG filename for a notice screenshot.

    Prefers the numeric notice ID (stable + unique across runs). Falls back to
    an address slug so records without a notice ID still get a sane name.
    """
    nid = re.sub(r"[^A-Za-z0-9]+", "", notice_id or "")
    if nid:
        return f"notice_{nid}.png"
    slug = re.sub(r"[^a-z0-9]+", "_", (address or "").lower()).strip("_")[:50]
    return f"notice_{slug or 'unknown'}.png"


# The notice document panel (right column: publish date + Notice Content + legal
# text), in preference order. Cropping to this drops the site chrome + sidebar so
# the image looks like the notice itself. Falls back to a full-page shot.
NOTICE_PANEL_SELECTORS = [
    "#right_content",
    "#ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_pnlNoticeContent",
]

_STREET_SUFFIXES = {
    "dr", "drive", "ln", "lane", "way", "st", "street", "ave", "avenue", "rd", "road",
    "blvd", "boulevard", "ct", "court", "cir", "circle", "pl", "place", "pike", "hwy",
    "highway", "trl", "trail", "ter", "terrace", "pkwy", "parkway", "cv", "cove", "loop",
    "run", "pt", "point", "rdg", "ridge", "xing", "crossing", "row", "trace", "walk",
}

# Wrap every occurrence of the given terms (scoped to the notice panel) in a
# yellow <mark>, merging overlaps, working on text nodes so tags never break.
_HIGHLIGHT_JS = r"""(j) => {
  const terms = JSON.parse(j).filter(t => t && t.length >= 3).map(t => t.toLowerCase());
  if (!terms.length) return 0;
  const root = document.querySelector('#right_content') || document.body;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = []; let n;
  while (n = walker.nextNode()) nodes.push(n);
  let count = 0;
  for (const node of nodes) {
    const tx = node.nodeValue, lo = tx.toLowerCase();
    let ms = [];
    for (const t of terms) { let i = 0; while ((i = lo.indexOf(t, i)) !== -1) { ms.push([i, i + t.length]); i += t.length; } }
    if (!ms.length) continue;
    ms.sort((a, b) => a[0] - b[0]);
    const mg = [];
    for (const m of ms) { if (mg.length && m[0] < mg[mg.length-1][1]) mg[mg.length-1][1] = Math.max(mg[mg.length-1][1], m[1]); else mg.push(m.slice()); }
    const f = document.createDocumentFragment(); let p = 0;
    for (const [s, e] of mg) {
      if (s > p) f.appendChild(document.createTextNode(tx.slice(p, s)));
      const mk = document.createElement('mark');
      mk.style.backgroundColor = '#fff200'; mk.style.color = '#000'; mk.style.padding = '0 1px';
      mk.textContent = tx.slice(s, e); f.appendChild(mk); p = e; count++;
    }
    if (p < tx.length) f.appendChild(document.createTextNode(tx.slice(p)));
    node.parentNode.replaceChild(f, node);
  }
  return count;
}"""


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 14 -> '14th', 21 -> '21st'."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _date_text_variants(d: str) -> list[str]:
    """Textual forms a sale/auction date appears as in a TN foreclosure notice,
    derived from a normalized date (YYYY-MM-DD or M/D/YYYY). Covers both the
    'July 14, 2026' style and the 'the 14th day of July, 2026' ordinal style that
    many trustees use. Matching the full date string (not just the year) keeps it
    off the deed/recording/publication dates."""
    from datetime import datetime

    s = (d or "").strip()
    if not s:
        return []
    dt = None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if not dt:
        return []
    mon, day, yr = dt.strftime("%B"), dt.day, dt.year  # day = int -> no leading zero
    od = _ordinal(day)
    return [
        f"{mon} {day}, {yr}",            # July 14, 2026
        f"{mon} {day} {yr}",             # July 14 2026
        f"{od} day of {mon}, {yr}",      # 14th day of July, 2026
        f"{od} day of {mon} {yr}",       # 14th day of July 2026
        f"{mon} {od}, {yr}",             # July 14th, 2026
        f"{dt.month}/{dt.day}/{yr}",     # 7/14/2026
    ]


def _highlight_terms(address: str = "", owner_name: str = "", auction_date: str = "") -> list[str]:
    """Build the list of strings to highlight: the property address (full + the
    house-number + street-name form), the owner/borrower name (full + each name
    word), and the sale/auction date (textual forms), deduped."""
    terms: list[str] = []
    a = (address or "").strip()
    if a:
        terms.append(a)
        toks = a.split()
        if toks and toks[-1].lower().strip(".") in _STREET_SUFFIXES:
            terms.append(" ".join(toks[:-1]))
    o = (owner_name or "").strip()
    if o:
        terms.append(o)
        terms += [t for t in o.split() if len(t) >= 4 and t.lower() != "and"]
    terms += _date_text_variants(auction_date)
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        k = t.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


async def capture_notice_screenshot(
    page,
    notice_id: str = "",
    address: str = "",
    owner_name: str = "",
    auction_date: str = "",
    output_dir: Path | None = None,
) -> Path | None:
    """Save a cropped, highlighted PNG of the current notice.

    Must be called while the page is showing the notice (after the CAPTCHA solve,
    before navigating back). Highlights the address + owner name in yellow, then
    screenshots just the notice panel (so it reads like the notice document, not
    the whole web page). Falls back to a full-page shot if the panel isn't found.
    Returns the saved Path, or None on failure.
    """
    out_dir = output_dir or config.NOTICE_SCREENSHOT_DIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / _screenshot_filename(notice_id, address)

        # Best-effort highlight (never let it block the screenshot).
        try:
            terms = _highlight_terms(address, owner_name, auction_date)
            if terms:
                await page.evaluate(_HIGHLIGHT_JS, json.dumps(terms))
        except Exception:
            logger.debug("Highlight injection failed", exc_info=True)

        # Crop to the notice panel; fall back to full page.
        element = None
        for sel in NOTICE_PANEL_SELECTORS:
            element = await page.query_selector(sel)
            if element:
                break
        if element:
            await element.screenshot(path=str(path))
        else:
            await page.screenshot(path=str(path), full_page=True)

        logger.info("Captured notice screenshot: %s", path.name)
        return path
    except Exception:
        logger.debug("Notice screenshot capture failed", exc_info=True)
        return None


def host_screenshots_via_drive(notices, folder_id: str, key_b64: str) -> int:
    """Upload local notice screenshots to Google Drive, setting notice_screenshot_url.

    No-op (returns 0) when Drive isn't configured. Records that already have a
    hosted URL, or whose local file is missing, are skipped. Best-effort per
    record: one upload failure doesn't abort the rest.
    """
    if not folder_id or not key_b64:
        return 0

    from drive_uploader import upload_file

    hosted = 0
    for n in notices:
        path = getattr(n, "notice_screenshot_path", "")
        if not path or getattr(n, "notice_screenshot_url", ""):
            continue
        p = Path(path)
        if not p.exists():
            continue
        try:
            link = upload_file(p, folder_id, key_b64, mimetype="image/png")
            if link:
                n.notice_screenshot_url = link
                hosted += 1
        except Exception:
            logger.warning("Drive upload failed for %s", p.name, exc_info=True)
    return hosted


def set_local_screenshot_urls(notices) -> int:
    """Fallback: point notice_screenshot_url at the local PNG path.

    Used for local CLI runs where no cloud host (Drive/KVS) is configured, so
    the DataSift Notes/field still reference where the proof image lives on disk.
    Returns the count of records updated.
    """
    n_set = 0
    for n in notices:
        path = getattr(n, "notice_screenshot_path", "")
        if path and not getattr(n, "notice_screenshot_url", ""):
            n.notice_screenshot_url = path
            n_set += 1
    return n_set
