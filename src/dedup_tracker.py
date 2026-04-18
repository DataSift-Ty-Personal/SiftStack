"""Cross-run deduplication for NJ scrapers.

Stores a JSON index of already-processed record IDs per source, keyed by:
  - `njlp`              — NJ Lis Pendens docket number (F-NNNNNN-YY)
  - `probate`           — Middlesex surrogate case (docket) number
  - `somerset`          — Somerset County sheriff-sale number (5-digit)
  - `tax_sale`          — RealAuction cert composite: {subdomain}{adv_number}{tax_year}
  - `civilview_sheriff` — salesweb.civilview.com Sheriff# (Essex/Middlesex/Union)

Used by modal_app.py to skip records already uploaded to DataSift in
a previous run, so the Wednesday cron only enriches + uploads new
filings each week. Tracking persists in a Modal Volume mounted at
/tracking in the scheduled/manual function containers.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

_SOURCES = ("njlp", "probate", "somerset", "tax_sale", "civilview_sheriff")


def _empty_tracking() -> dict:
    return {s: {} for s in _SOURCES}


def load_tracking(path: str | Path) -> dict:
    """Load the tracking JSON. Returns an empty-but-well-formed dict if
    the file is missing or unreadable — first-run scraping just treats
    every record as new."""
    p = Path(path)
    if not p.exists():
        return _empty_tracking()
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        logger.warning("tracking file unreadable (%s) — starting fresh", e)
        return _empty_tracking()
    for s in _SOURCES:
        data.setdefault(s, {})
    return data


def save_tracking(tracking: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tracking, indent=2, default=str))


# ── ID extractors ─────────────────────────────────────────────────────

_NJLP_DOCKET_RE = re.compile(r"Docket:\s*(F[-‐–]\d{6}[-‐–]\d{2})", re.IGNORECASE)
_PROBATE_DOCKET_RE = re.compile(r"Docket:\s*(\d{5,})")
_SOMERSET_SALE_RE = re.compile(r"Sale#:\s*(\d{4,})")
_PROBATE_PK_RE = re.compile(r"Q_PK_ID=(\d+)")
_TAX_SALE_ADV_RE = re.compile(r"ADV:\s*([^\s|]+)")
_TAX_SALE_SUBDOMAIN_RE = re.compile(r"Subdomain:\s*([^\s|]+)")
_TAX_SALE_YEAR_RE = re.compile(r"Tax Year:\s*(\d{4})")
# CivilView: "Sheriff# F-24001837" or "Sheriff# CH-25001605" etc. Prefix is
# stable per-court and the digits carry the year + sequence.
_CIVILVIEW_SHERIFF_RE = re.compile(r"Sheriff#\s*([A-Z]+[-‐–]?\d+)", re.IGNORECASE)
# URL fallback: /Sales/SaleDetails?PropertyId=NNN — unique per county-year.
_CIVILVIEW_PROPERTY_ID_RE = re.compile(r"PropertyId=(\d+)")


def extract_id(notice: NoticeData, source: str) -> str | None:
    """Pull the canonical dedup key for this notice.

    Falls back to source_url patterns if raw_text is thin.
    """
    raw = notice.raw_text or ""
    url = notice.source_url or ""
    if source == "njlp":
        m = _NJLP_DOCKET_RE.search(raw)
        return m.group(1).replace("‐", "-").replace("–", "-") if m else None
    if source == "probate":
        # Prefer the Q_PK_ID in source_url (stable numeric key) but fall back
        # to the docket number in raw_text.
        m = _PROBATE_PK_RE.search(url)
        if m:
            return m.group(1)
        m = _PROBATE_DOCKET_RE.search(raw)
        return m.group(1) if m else None
    if source == "somerset":
        m = _SOMERSET_SALE_RE.search(raw)
        return m.group(1) if m else None
    if source == "tax_sale":
        # Composite key: {subdomain}{adv_number}{tax_year}
        # ADV# is unique per sale cycle; subdomain and year disambiguate
        # across municipalities and years (same ADV# reused annually).
        sub = _TAX_SALE_SUBDOMAIN_RE.search(raw)
        adv = _TAX_SALE_ADV_RE.search(raw)
        yr = _TAX_SALE_YEAR_RE.search(raw)
        if not (sub and adv):
            return None
        year = yr.group(1) if yr else ""
        return f"{sub.group(1)}-{adv.group(1)}-{year}"
    if source == "civilview_sheriff":
        # Prefer Sheriff# (stable across re-listings); fall back to the
        # numeric PropertyId in source_url if the raw_text is thin.
        m = _CIVILVIEW_SHERIFF_RE.search(raw)
        if m:
            return m.group(1).upper().replace("‐", "-").replace("–", "-")
        m = _CIVILVIEW_PROPERTY_ID_RE.search(url)
        return m.group(1) if m else None
    return None


def filter_new(
    notices: list[NoticeData],
    source: str,
    tracking: dict,
) -> tuple[list[NoticeData], int]:
    """Return (new_notices, skipped_count) and mutate tracking in place.

    Notices without an extractable ID are kept as "new" but are not added
    to the tracking index — better to double-ship an odd record once than
    to silently drop it forever because we couldn't find its ID.
    """
    if source not in _SOURCES:
        raise ValueError(f"unknown source {source!r}; expected one of {_SOURCES}")
    source_tracking = tracking.setdefault(source, {})
    now_iso = datetime.utcnow().isoformat()
    new_notices: list[NoticeData] = []
    skipped = 0
    for n in notices:
        rid = extract_id(n, source)
        if rid is None:
            logger.warning(
                "%s: no dedup ID for notice %s — keeping as new, not tracked",
                source, (n.owner_name or "")[:40],
            )
            new_notices.append(n)
            continue
        if rid in source_tracking:
            skipped += 1
        else:
            new_notices.append(n)
            source_tracking[rid] = now_iso
    return new_notices, skipped
