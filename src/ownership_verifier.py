"""Ownership verification for probate runner records.

Probate addresses come from the courthouse filing (the decedent's address),
NOT from a property-ownership lookup — so a large share of records are
non-owner matches: the decedent received mail at the address but does not
own the property (renter, family member, prior owner, estate already sold).
A Week 25 manual pass found 47% (62/132) were non-owners.

This module cross-checks the decedent against the MOD-IV owner of record and
flags each probate record `verified` / `mismatch` / `unknown`. It NEVER drops
a record — the downstream county data cleaner owns filtering. The flag exists
to surface the N-rate at pipeline time and to measure the matcher's accuracy
against the cleaner's verified output before we ever trust it to auto-filter.

Owner data source: nj_taxrecords.lookup_by_address (taxrecords-nj.com /
Vital Communications MOD-IV) — plain HTTP, no auth, $0 per call, already
exercised inside the Modal enrichment run via the obituary DM waterfall.
Covers Middlesex / Somerset / Union. Essex MOD-IV lives on a different
backend (taxdatahub.com) and is not supported here — Essex probate records
are left `ownership_status="unknown"` (NJPR Playwright is the documented
future fallback for Essex coverage).

Field mapping note: the matcher compares `decedent_name` (the deceased)
against `tax_owner_name` (owner of record). It is gated to probate records
with a street address; `probate_no_property` (N) records have no address and
are skipped naturally.
"""
from __future__ import annotations

import logging
import re
import time

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# Counties whose MOD-IV owner-of-record is reachable via nj_taxrecords.
# Mirrors nj_taxrecords.COUNTY_CODES (Essex intentionally absent).
try:
    from nj_taxrecords import COUNTY_CODES as _TAXRECORDS_COUNTIES
    _SUPPORTED_COUNTIES = set(_TAXRECORDS_COUNTIES.keys())
except Exception:  # pragma: no cover — keep the module importable if the dep moves
    _SUPPORTED_COUNTIES = {"Middlesex", "Somerset", "Union"}

# Name tokens that are never a surname (suffixes / honorifics / estate words).
_NAME_NOISE = {
    "JR", "SR", "II", "III", "IV", "V", "ESQ", "MR", "MRS", "MS", "DR",
    "THE", "ESTATE", "OF", "DECEASED",
}


def _supported_county(county: str) -> bool:
    return (county or "").strip().title() in _SUPPORTED_COUNTIES


def _name_tokens(name: str) -> list[str]:
    """Upper-case alpha tokens with noise (suffixes/honorifics) removed."""
    toks = re.findall(r"[A-Z]+", (name or "").upper())
    return [t for t in toks if t not in _NAME_NOISE and len(t) > 1]


def _extract_last_name(name: str) -> str:
    """Best-effort surname from a decedent name.

    Handles both "FIRST [MIDDLE] LAST" and "LAST, FIRST" orderings. Returns
    "" when no usable token survives.
    """
    raw = (name or "").upper().strip()
    if not raw:
        return ""
    # "LAST, FIRST [MIDDLE]" — surname is the part before the first comma.
    if "," in raw:
        head = _name_tokens(raw.split(",", 1)[0])
        if head:
            return head[-1]
    toks = _name_tokens(raw)
    return toks[-1] if toks else ""


def verify_ownership(notice: NoticeData) -> str:
    """Compare the decedent against the owner of record.

    Returns 'verified', 'mismatch', or 'unknown'. Pure comparison — does no
    network I/O (reads notice.decedent_name + notice.tax_owner_name only).

    The match is surname-based and order-agnostic: MOD-IV names arrive as
    "LAST FIRST" while filings are often "FIRST LAST", so we test whether the
    decedent's surname appears as a whole token in the owner string. This also
    catches family trusts/LLCs that carry the surname
    (e.g. "DOE FAMILY TRUST") and multi-owner parcels ("DOE JOHN & MARY").
    """
    owner = (notice.tax_owner_name or "").upper().strip()
    if not owner:
        return "unknown"

    deceased_last = _extract_last_name(notice.decedent_name)
    if not deceased_last:
        return "unknown"

    if deceased_last in set(_name_tokens(owner)):
        return "verified"
    return "mismatch"


def _eligible(notice: NoticeData) -> bool:
    return (
        "probate" in (notice.notice_type or "").strip().lower()
        and bool((notice.address or "").strip())
        and bool((notice.decedent_name or "").strip())
    )


def enrich_ownership(notices: list[NoticeData], *, rate_limit: bool = True) -> dict:
    """Populate `tax_owner_name` + `ownership_status` on probate records.

    For each eligible probate record in a supported county, look up the
    MOD-IV owner of record by property address and flag the record. Records
    in unsupported counties (Essex) or that don't resolve stay 'unknown'.
    Modifies notices in place; returns the stats dict (see `ownership_stats`).

    Read-only w.r.t. the record set — nothing is filtered.
    """
    eligible = [n for n in notices if _eligible(n)]
    if not eligible:
        return ownership_stats(notices)

    try:
        from nj_taxrecords import lookup_by_address
    except ImportError:
        logger.warning("  nj_taxrecords not available — ownership verification skipped")
        return ownership_stats(notices)

    looked_up = 0
    for i, n in enumerate(eligible, 1):
        county = (n.county or "").strip().title()
        if county not in _SUPPORTED_COUNTIES:
            # Essex / anything off taxrecords-nj — can't verify from this source.
            if not n.ownership_status:
                n.ownership_status = "unknown"
            continue

        # Don't clobber an owner name another step already supplied; only
        # look it up when missing.
        if not (n.tax_owner_name or "").strip():
            try:
                parcels = lookup_by_address(n.address, county)
            except Exception as e:
                logger.debug("  ownership lookup failed for '%s' (%s): %s", n.address, county, e)
                parcels = []
            looked_up += 1
            if parcels:
                n.tax_owner_name = (parcels[0].owner_name or "").strip()
            if rate_limit:
                time.sleep(0.4)

        n.ownership_status = verify_ownership(n)

        if i % 25 == 0:
            logger.info("  Ownership verification progress: %d/%d", i, len(eligible))

    stats = ownership_stats(notices)
    logger.info(
        "  Ownership: %d verified / %d mismatch / %d unknown (of %d probate; %d lookups)",
        stats["verified"], stats["mismatch"], stats["unknown"],
        stats["probate_total"], looked_up,
    )
    return stats


def ownership_stats(notices: list[NoticeData]) -> dict:
    """Breakdown of ownership_status across probate records in the batch."""
    probate = [n for n in notices if "probate" in (n.notice_type or "").strip().lower()]
    total = len(probate)
    verified = sum(1 for n in probate if n.ownership_status == "verified")
    mismatch = sum(1 for n in probate if n.ownership_status == "mismatch")
    unknown = total - verified - mismatch
    return {
        "probate_total": total,
        "verified": verified,
        "mismatch": mismatch,
        "unknown": unknown,
    }


def ownership_health(notices: list[NoticeData]) -> dict | None:
    """Fill-rate dict for enrichment health monitoring, or None if N/A.

    The monitored metric is the *pass rate among records we could actually
    check* — verified / (verified + mismatch). 'unknown' (Essex + unresolved)
    is excluded from the denominator so structural gaps don't trip the floor.
    Returns None when nothing was determinable (e.g. sheriff-only run), which
    tells compute_enrichment_health to omit the row entirely.
    """
    s = ownership_stats(notices)
    determined = s["verified"] + s["mismatch"]
    if determined == 0:
        return None
    return {
        "count": s["verified"],
        "total": determined,
        "pct": round(s["verified"] / determined * 100, 1),
    }


def ownership_slack_line(stats: dict) -> str:
    """Multi-line ownership-verification block for the Slack summary."""
    t = stats["probate_total"]

    def _pct(x: int) -> str:
        return f"{(x / t * 100):.1f}%" if t else "0.0%"

    return (
        "Ownership Verification:\n"
        f"  Verified (name match): {stats['verified']}/{t} ({_pct(stats['verified'])})\n"
        f"  Mismatch (non-owner): {stats['mismatch']}/{t} ({_pct(stats['mismatch'])})\n"
        f"  Unknown (no owner data): {stats['unknown']}/{t} ({_pct(stats['unknown'])})"
    )
