"""Cross-reference DataSift Sold Properties buyers against JCD deed records.

Phase 1B of the Jefferson County KY buyer prospector. The DataSift
Investor tab is a curated AI-flagged subset — it misses transactions
(including the user's own). The Jefferson County Clerk deed records are
the ground truth: every recorded property transfer in the public record.

Cross-referencing does two things:

  1. **Verify** each DataSift buyer by counting their appearances as
     grantee in JCD deed records for the same window. A DataSift buyer
     with zero deed matches is suspect (data error or non-investor
     activity); one with many matches is a high-confidence investor.

  2. **Discover** investors DataSift's AI missed entirely. We surface
     entity grantees that appear N+ times in JCD deeds but are absent
     from the DataSift Investor list.

Match key: normalized buyer/grantee name. DataSift gives us buyer name
plus property address; JCD gives grantor + grantee + legal description
(no street numbers — Louisville uses metes-and-bounds). Property-level
join would require a PVA-per-parcel resolver step; out of scope here.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jefferson_deeds_scraper import DeedTransfer

# Lazy import to break a circular dependency — jefferson_buyer_prospector
# imports from THIS module too. Type-only at module load; runtime helpers
# are imported inside cross_reference().
if TYPE_CHECKING:
    from jefferson_buyer_prospector import BuyerRanking

logger = logging.getLogger(__name__)


# ── Name normalization ────────────────────────────────────────────────


# Punctuation we strip wholesale (commas, periods, apostrophes, hyphens
# turn into a space then collapsed). Ampersand is preserved as " AND " so
# "SMITH & JONES" matches "SMITH AND JONES".
_PUNCT_RE = re.compile(r"[.,'’\-/]+")
_MULTISPACE_RE = re.compile(r"\s+")
_AMP_RE = re.compile(r"\s*&\s*")


def normalize_buyer_name(name: str) -> str:
    """Canonicalize a buyer/grantee name for cross-source matching.

    - Uppercase
    - Strip periods, commas, apostrophes, hyphens
    - Normalize `&` to ` AND `
    - Collapse to single spaces
    - Drop trailing legal-form abbreviation variants (L.L.C. → LLC etc.)

    Returns "" for empty/None input.
    """
    if not name:
        return ""
    s = name.upper().strip()
    # Common abbreviation collisions before punctuation strip
    s = re.sub(r"\bL\.?L\.?C\.?\b", "LLC", s)
    s = re.sub(r"\bL\.?P\.?\b", "LP", s)
    s = re.sub(r"\bL\.?L\.?P\.?\b", "LLP", s)
    s = re.sub(r"\bL\.?T\.?D\.?\b", "LTD", s)
    s = re.sub(r"\bINC\.?\b", "INC", s)
    # Normalize "COMPANY" and "CO." both to "CO" so e.g.
    # "ARMM ASSET COMPANY 2 LLC" matches "ARMM ASSET CO 2 LLC".
    s = re.sub(r"\bCOMPANY\b", "CO", s)
    s = re.sub(r"\bCO\.\b", "CO", s)
    # Same for COMPANIES → CO (rarer but happens)
    s = re.sub(r"\bCOMPANIES\b", "CO", s)
    # CORPORATION ↔ CORP
    s = re.sub(r"\bCORPORATION\b", "CORP", s)
    # General punctuation strip
    s = _PUNCT_RE.sub(" ", s)
    s = _AMP_RE.sub(" AND ", s)
    s = _MULTISPACE_RE.sub(" ", s).strip()
    return s


# ── Cross-reference ───────────────────────────────────────────────────


@dataclass
class DeedOnlyBuyer:
    """An entity grantee from JCD that does NOT appear in DataSift's
    Investor list — a candidate cash buyer DataSift's AI missed.
    """
    buyer_name: str = ""            # display name from JCD (first occurrence)
    normalized_name: str = ""
    deed_count: int = 0             # number of times this name appears as grantee
    months_active: int = 0
    first_filing: str = ""          # YYYY-MM-DD of earliest deed in window
    last_filing: str = ""           # YYYY-MM-DD of latest deed in window
    properties: list[str] = field(default_factory=list)  # legal descriptions
    is_entity: bool = False
    score: float = 0.0
    rank: int = 0
    # Same shape as BuyerRanking.category — separates wholesale-target
    # deed-only buyers from bulk acquirers (subdivision developers,
    # estate liquidations, single-day portfolio sweeps).
    is_bulk: bool = False
    bulk_signal: str = ""


@dataclass
class CrossRefResult:
    """Output of cross-referencing DataSift buyers against JCD deeds."""
    verified_count: int = 0         # DataSift buyers with >=1 deed match
    unverified_count: int = 0       # DataSift buyers with 0 deed matches
    deed_only_count: int = 0        # wholesale-target entity grantees in JCD
    deed_only_bulk_count: int = 0   # bulk/builder entity grantees in JCD
    total_deeds: int = 0            # total JCD deeds in window
    total_entity_grantees: int = 0  # JCD grantees flagged as entities
    deed_only_buyers: list[DeedOnlyBuyer] = field(default_factory=list)
    deed_only_bulk_buyers: list[DeedOnlyBuyer] = field(default_factory=list)


# Bulk thresholds (mirror jefferson_buyer_prospector for consistency).
# Adapted for deeds: legal_desc replaces street, filing_date replaces month.
_DEED_BULK_SAME_LEGAL_THRESHOLD = 5
_DEED_BULK_SAME_DATE_THRESHOLD = 5      # 5+ deeds filed same day = portfolio sweep
_DEED_BULK_HIGH_TEMPO_COUNT = 20        # combined w/ tempo: ≥20 deeds…
_DEED_BULK_HIGH_TEMPO_PER_MONTH = 6     # …at ≥6/month sustained = bulk


def _classify_deed_only_buyer(buyer: DeedOnlyBuyer, deeds: list[DeedTransfer]) -> None:
    """Mark a deed-only buyer as bulk if their pattern matches subdivision /
    estate-sweep / portfolio behavior. Mutates `buyer` in place.
    """
    if not deeds:
        return
    legal_counts = Counter(d.legal_desc.strip() for d in deeds if d.legal_desc.strip())
    date_counts = Counter(d.date_filed for d in deeds if d.date_filed)
    max_legal = max(legal_counts.values()) if legal_counts else 0
    max_legal_desc = legal_counts.most_common(1)[0][0] if legal_counts else ""
    max_date = max(date_counts.values()) if date_counts else 0
    max_date_str = date_counts.most_common(1)[0][0] if date_counts else ""

    signals: list[str] = []
    if max_legal >= _DEED_BULK_SAME_LEGAL_THRESHOLD:
        signals.append(f"{max_legal}x same legal desc ({max_legal_desc[:30]}…)")
    if max_date >= _DEED_BULK_SAME_DATE_THRESHOLD:
        signals.append(f"{max_date}x filed on {max_date_str}")
    tempo = (buyer.deed_count / buyer.months_active) if buyer.months_active else 0
    if buyer.deed_count >= _DEED_BULK_HIGH_TEMPO_COUNT and tempo >= _DEED_BULK_HIGH_TEMPO_PER_MONTH:
        signals.append(f"{buyer.deed_count} deeds at {tempo:.1f}/month sustained")

    if signals:
        buyer.is_bulk = True
        buyer.bulk_signal = "; ".join(signals)


# Min recurring-buyer threshold for entity grantees DataSift missed.
# Setting too low surfaces one-time owner-LLC vehicles; too high misses
# real small investors. Two deeds = repeat behavior, three = pattern.
DEED_ONLY_MIN_COUNT = 2


def cross_reference(
    rankings: list[BuyerRanking],
    deed_transfers: list[DeedTransfer],
    *,
    deed_only_min_count: int = DEED_ONLY_MIN_COUNT,
) -> CrossRefResult:
    """Annotate DataSift rankings with deed verification + return missed buyers.

    Imports `_is_entity` and `_is_excluded` lazily here to break the
    circular dependency on jefferson_buyer_prospector (which imports
    from this module).

    Mutates `rankings` in place: each BuyerRanking gets `deed_verified_count`,
    `deed_first_filing`, `deed_last_filing` populated.

    Returns a CrossRefResult with the deed-only buyer list (entity
    grantees DataSift didn't flag) ranked by deed count.
    """
    # Lazy import — module-level import would create a cycle with
    # jefferson_buyer_prospector.
    from jefferson_buyer_prospector import _is_entity, _is_excluded

    # 1. Index deed transfers by normalized grantee name
    deeds_by_grantee: dict[str, list[DeedTransfer]] = defaultdict(list)
    grantee_displays: dict[str, str] = {}  # normalized -> first display form
    total_entity_grantees = 0

    for d in deed_transfers:
        grantee = d.primary_grantee
        if not grantee:
            continue
        norm = normalize_buyer_name(grantee)
        if not norm:
            continue
        deeds_by_grantee[norm].append(d)
        if norm not in grantee_displays:
            grantee_displays[norm] = grantee
        if _is_entity(grantee):
            total_entity_grantees += 1

    # 2. Verify each DataSift buyer
    verified = 0
    unverified = 0
    matched_norms: set[str] = set()
    for r in rankings:
        norm = normalize_buyer_name(r.buyer_name)
        matching_deeds = deeds_by_grantee.get(norm, [])
        r.deed_verified_count = len(matching_deeds)
        if matching_deeds:
            dates = sorted(d.date_filed for d in matching_deeds if d.date_filed)
            r.deed_first_filing = dates[0] if dates else ""
            r.deed_last_filing = dates[-1] if dates else ""
            verified += 1
            matched_norms.add(norm)
        else:
            r.deed_first_filing = ""
            r.deed_last_filing = ""
            unverified += 1

    # 3. Find deed-only entity buyers (in JCD, not in DataSift), then split
    # bulk acquirers (subdivision/portfolio) from real wholesale targets.
    deed_only_all: list[tuple[DeedOnlyBuyer, list[DeedTransfer]]] = []
    for norm, deeds in deeds_by_grantee.items():
        if norm in matched_norms:
            continue
        display = grantee_displays.get(norm, "")
        # Skip non-entities — too noisy (every individual homebuyer is a deed)
        # and skip the excluded list (Habitat, Kentucky Housing, Landbank etc.)
        if not _is_entity(display) or _is_excluded(display):
            continue
        if len(deeds) < deed_only_min_count:
            continue
        dates = sorted(d.date_filed for d in deeds if d.date_filed)
        months = sorted({d.date_filed[:7] for d in deeds if d.date_filed})
        buyer = DeedOnlyBuyer(
            buyer_name=display,
            normalized_name=norm,
            deed_count=len(deeds),
            months_active=len(months),
            first_filing=dates[0] if dates else "",
            last_filing=dates[-1] if dates else "",
            properties=[d.legal_desc for d in deeds[:10]],
            is_entity=True,
        )
        _classify_deed_only_buyer(buyer, deeds)
        deed_only_all.append((buyer, deeds))

    # Split into main (wholesale targets) and bulk (builders/sweeps).
    deed_only = [b for b, _ in deed_only_all if not b.is_bulk]
    deed_only_bulk = [b for b, _ in deed_only_all if b.is_bulk]

    # 4. Score + rank each group separately (same as main scorecard pattern).
    for group in (deed_only, deed_only_bulk):
        if not group:
            continue
        max_count = max(b.deed_count for b in group) or 1
        max_months = max(b.months_active for b in group) or 1
        for b in group:
            freq = (b.deed_count / max_count) * 100
            months = (b.months_active / max_months) * 100
            # 70/30 weighting — for deed-only we don't have $ data,
            # so consistency over time matters more.
            b.score = round(freq * 0.7 + months * 0.3, 1)
        group.sort(key=lambda b: (-b.score, -b.deed_count))
        for i, b in enumerate(group, 1):
            b.rank = i

    logger.info(
        "Cross-ref: %d DataSift buyers (%d verified, %d unverified) vs "
        "%d JCD deeds (%d entity grantees) -> %d wholesale-target + %d bulk "
        "deed-only entity buyers (>= %d deeds each)",
        len(rankings), verified, unverified,
        len(deed_transfers), total_entity_grantees,
        len(deed_only), len(deed_only_bulk), deed_only_min_count,
    )

    return CrossRefResult(
        verified_count=verified,
        unverified_count=unverified,
        deed_only_count=len(deed_only),
        deed_only_bulk_count=len(deed_only_bulk),
        total_deeds=len(deed_transfers),
        total_entity_grantees=total_entity_grantees,
        deed_only_buyers=deed_only,
        deed_only_bulk_buyers=deed_only_bulk,
    )
