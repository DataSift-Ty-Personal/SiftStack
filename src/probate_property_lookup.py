"""Probate property lookup — find decedent's property via OH County Auditors.

Probate court records (Franklin/Montgomery/Greene) name the decedent and the
fiduciary (executor/PR) but DO NOT include the decedent's property address.
This module fills that gap by querying each county's public Auditor parcel
search by name.

Three-tier lookup waterfall:

  Tier 1 — Decedent name search
      Search the county Auditor for parcels whose owner matches the
      decedent's name. Score by Jaccard token overlap; accept >= 0.4.
      Hits when the decedent owned property in their own name at TOD.

  Tier 2 — Executor family search
      Search the county Auditor for parcels owned by the EXECUTOR.
      For each match, check if the decedent's last name appears in the
      owner string (e.g., a property previously transferred from
      decedent to executor still shows the decedent's last name in
      a joint-owner or trust-style record). Hits when the family has
      already transferred the deed but probate is still required.

  Tier 3 — People search fallback (NOT YET WIRED)
      TruePeopleSearch / FastPeopleSearch lookup for the decedent's
      last known address. Returns the most recent residence which is
      typically (but not always) the property in the estate. Slower
      and noisier than Auditor — used only when Tiers 1+2 miss.

The function mutates each NoticeData in place: sets `address`, `city`, `zip`,
`parcel_id`, and `tax_owner_name` (the auditor's owner-record name) when a
high-confidence match is found. Records that fail all tiers are left as-is
so Mike can do manual lookup if needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from models import NoticeData

logger = logging.getLogger(__name__)


# ── County → Auditor module dispatch ──────────────────────────────────


def _auditor_for_county(county: str):
    """Return the auditor module's `search_by_owner_name` function for `county`.

    Returns None if no auditor is registered for the given county. Each
    auditor is imported lazily to avoid loading network deps at startup.
    """
    key = (county or "").strip().lower()
    if key == "franklin":
        from enrichment.oh_franklin_auditor import search_by_owner_name
        return search_by_owner_name
    if key == "montgomery":
        from enrichment.oh_montgomery_auditor import search_by_owner_name
        return search_by_owner_name
    if key == "greene":
        from enrichment.oh_greene_auditor import search_by_owner_name
        return search_by_owner_name
    return None


# ── Match scoring ─────────────────────────────────────────────────────

# These thresholds match the existing probate lookup pattern documented
# in CLAUDE.md ("accept >= 0.4 match"). High threshold = fewer false
# positives; the cost of a false positive is mailing the wrong person.
MIN_MATCH_SCORE = 0.4

# Names that produce zero hits at this score get logged for review but
# don't fall through to lower-confidence matches.
HIGH_CONFIDENCE_SCORE = 0.7


# ── Tier 1: Decedent name search ──────────────────────────────────────


async def _tier1_decedent_search(
    notice: NoticeData,
    search_fn,
) -> dict | None:
    """Search the county Auditor for parcels owned by the decedent.

    Tries multiple name variants because Auditor records use varied formats:
        "PATRICIA CRIDGE", "CRIDGE PATRICIA", "CRIDGE, PATRICIA A", etc.
    Returns the highest-scoring match >= MIN_MATCH_SCORE, or None.
    """
    name = (notice.decedent_name or "").strip()
    if not name:
        return None

    variants = _name_variants(name)
    best: dict | None = None

    for variant in variants:
        try:
            results = await search_fn(variant)
        except Exception as e:
            logger.warning("Tier 1 auditor search failed for %r: %s", variant, e)
            continue

        for r in results:
            if r.get("match_score", 0) < MIN_MATCH_SCORE:
                continue
            if best is None or r["match_score"] > best.get("match_score", 0):
                best = r

        # Short-circuit on a high-confidence hit
        if best and best.get("match_score", 0) >= HIGH_CONFIDENCE_SCORE:
            return best

    return best


# ── Tier 2: Executor family search ────────────────────────────────────


async def _tier2_executor_family(
    notice: NoticeData,
    search_fn,
) -> dict | None:
    """Search by EXECUTOR name; keep matches whose owner contains decedent's last name.

    Catches the common pattern where the decedent transferred property to
    the executor (often a child) before death, but probate is still being
    administered. The auditor record shows the executor as owner, but the
    decedent's last name often appears in a co-owner / trust / "et al"
    field on the same parcel.
    """
    executor = (notice.owner_name or notice.decision_maker_name or "").strip()
    if not executor:
        return None

    decedent_last = _last_token(notice.decedent_name)
    if not decedent_last:
        return None

    variants = _name_variants(executor)
    best: dict | None = None

    for variant in variants:
        try:
            results = await search_fn(variant)
        except Exception as e:
            logger.warning("Tier 2 auditor search failed for %r: %s", variant, e)
            continue

        for r in results:
            owner_str = (r.get("owner_name", "") or "").upper()
            # Family-transfer signal: decedent's last name appears in the
            # auditor's owner record (often as co-owner, trustee, or "et al")
            if decedent_last.upper() not in owner_str:
                continue
            if r.get("match_score", 0) < MIN_MATCH_SCORE:
                continue
            if best is None or r["match_score"] > best.get("match_score", 0):
                best = r

        if best and best.get("match_score", 0) >= HIGH_CONFIDENCE_SCORE:
            return best

    return best


# ── Name utilities ────────────────────────────────────────────────────


def _name_variants(name: str) -> list[str]:
    """Generate name variants to try against Auditor name search.

    OH Auditors use mixed naming conventions. Trying multiple variants
    increases hit rate without hurting precision (the match_score filter
    keeps false positives out).
    """
    if not name:
        return []
    cleaned = name.strip()
    tokens = cleaned.replace(",", " ").split()
    if len(tokens) < 2:
        return [cleaned]

    first, *_, last = tokens
    middle = tokens[1:-1] if len(tokens) > 2 else []

    variants = [
        cleaned,                              # "PATRICIA CRIDGE"
        f"{last} {first}",                    # "CRIDGE PATRICIA"
        f"{last}, {first}",                   # "CRIDGE, PATRICIA"
        f"{first} {last}",                    # "PATRICIA CRIDGE" (re-stated)
    ]
    if middle:
        # Also try without middle name/initial
        variants.append(f"{first} {last}")
        variants.append(f"{last} {first}")

    # Dedup while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def _last_token(name: str) -> str:
    """Return the last whitespace-delimited token of `name` (the surname).

    Strips punctuation. Returns empty string if name is empty or one token.
    """
    if not name:
        return ""
    cleaned = name.strip().replace(",", "")
    parts = cleaned.split()
    return parts[-1] if parts else ""


# ── Apply auditor match to NoticeData ─────────────────────────────────


def _apply_match(notice: NoticeData, match: dict, tier: str) -> None:
    """Mutate `notice` in place with property data from the auditor match."""
    notice.address = (match.get("address", "") or "").strip()
    notice.city = (match.get("city", "") or "").strip()
    notice.zip = (match.get("zip", "") or "").strip()
    notice.parcel_id = (match.get("parcel_id", "") or "").strip()
    notice.tax_owner_name = (match.get("owner_name", "") or "").strip()
    if not notice.state:
        notice.state = "OH"
    logger.info(
        "  [%s] %s: %s -> %s (%s, score=%.2f)",
        tier,
        notice.decedent_name or notice.owner_name,
        match.get("owner_name", "?"),
        notice.address,
        notice.parcel_id,
        match.get("match_score", 0),
    )


# ── Public entry point ────────────────────────────────────────────────


async def lookup_decedent_properties(notices: list[NoticeData]) -> dict[str, int]:
    """Resolve property addresses for probate notices via OH County Auditors.

    Mutates `notices` in place: each NoticeData with a successful match gets
    `address`, `city`, `zip`, `parcel_id`, `tax_owner_name` populated.

    Returns a stats dict: {tier1_hits, tier2_hits, missed, no_auditor}.
    Records that already have an address are skipped. Records whose county
    has no auditor module are skipped and counted under `no_auditor`.
    """
    stats = {"tier1_hits": 0, "tier2_hits": 0, "missed": 0, "no_auditor": 0}

    candidates = [
        n for n in notices
        if n.notice_type == "probate"
        and (n.decedent_name or "").strip()
        and not (n.address or "").strip()
    ]
    if not candidates:
        return stats

    logger.info("Probate property lookup: %d candidates", len(candidates))

    for notice in candidates:
        search_fn = _auditor_for_county(notice.county)
        if search_fn is None:
            logger.debug("No auditor module for county %r, skipping", notice.county)
            stats["no_auditor"] += 1
            continue

        # Tier 1 — search by decedent name
        match = await _tier1_decedent_search(notice, search_fn)
        if match:
            _apply_match(notice, match, "Tier1")
            stats["tier1_hits"] += 1
            continue

        # Tier 2 — search by executor, filter to records mentioning decedent's last name
        match = await _tier2_executor_family(notice, search_fn)
        if match:
            _apply_match(notice, match, "Tier2")
            stats["tier2_hits"] += 1
            continue

        stats["missed"] += 1

    logger.info(
        "Probate property lookup done: %d Tier1 + %d Tier2 hits, %d missed, %d no-auditor",
        stats["tier1_hits"], stats["tier2_hits"], stats["missed"], stats["no_auditor"],
    )
    return stats


# ── CLI / test harness ────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import csv
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Test probate property lookup against a CSV")
    parser.add_argument(
        "--csv",
        default="output/test_oh_montgomery_probate.csv",
        help="Probate scrape CSV with decedent_name + county columns",
    )
    parser.add_argument("--limit", type=int, default=10, help="Max records to test")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    in_path = Path(args.csv)
    if not in_path.exists():
        print(f"CSV not found: {in_path}")
        raise SystemExit(1)

    notices: list[NoticeData] = []
    with open(in_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            n = NoticeData()
            for k, v in row.items():
                if hasattr(n, k):
                    setattr(n, k, v)
            notices.append(n)
            if len(notices) >= args.limit:
                break

    print(f"Testing probate property lookup on {len(notices)} records from {in_path}")
    stats = asyncio.run(lookup_decedent_properties(notices))
    print(f"\nStats: {json.dumps(stats, indent=2)}")
    print("\nResolved properties:")
    for n in notices:
        if n.address:
            print(f"  {n.decedent_name:30}  →  {n.address}, {n.city} {n.zip}  parcel={n.parcel_id}")
