"""Phase 1 — Title lookup + death-signal detection.

Input:  ProspectInput (address + optional owner + county)
Output: Lead (always returned, even sparse — never raises)

Sources consulted (in order):
  1. NJ MOD-IV (taxrecords-nj.com) via _siftstack_bridge.
     Covers Middlesex / Somerset / Union. Essex falls through (different
     vendor, deferred). Knox/Blount (TN) also fall through here — Phase 1
     for TN counties is a stub that returns an empty Lead with a warning.
  2. Owner-name death-indicator classifier (et_al / life_est / personal_rep
     / care_of / trustee) — pure string→string from SiftStack's
     tax_enricher.

Death-signal heuristics (any one trips `death_signal=True`):
  - MOD-IV owner-name contains: "personal_rep", "life_estate", "care_of",
    "et_al", or "trustee" (trustee filtered to exclude business entities).
  - Owner mismatch: caller-supplied owner ≠ MOD-IV owner BUT they share
    the same last name. Strong signal of an estate transfer ("OLIVE
    GECZIK" on title vs "Catherine Geczik" on probate docket = same
    family, owner died).
  - "ESTATE OF" appears in the MOD-IV owner field literally.

Phase 1 NEVER consults LLMs and NEVER hits the obit endpoints — those
are Phase 2 concerns. It costs ~$0.000 (one HTTP POST). Keep it that way.
"""

from __future__ import annotations

import logging
import re

from deep_prospecting import _utils
from deep_prospecting._siftstack_bridge import (
    ModIVParcel,
    classify_owner_death_indicator,
    modiv_lookup_by_address,
    modiv_lookup_by_owner,
)
from deep_prospecting.models import (
    Lead,
    ProspectInput,
    SourceCheck,
)

logger = logging.getLogger(__name__)


# Counties this phase can resolve via taxrecords-nj.com. Anything else
# returns an empty Lead with a "skipped" SourceCheck so the run continues.
_MODIV_COUNTIES = {"Middlesex", "Somerset", "Union"}


# ── Address normalization ───────────────────────────────────────────────


_STREET_SUFFIX_NORMALIZE = {
    "STREET": "ST", "ST.": "ST",
    "AVENUE": "AVE", "AVE.": "AVE",
    "ROAD": "RD", "RD.": "RD",
    "PLACE": "PL", "PL.": "PL",
    "DRIVE": "DR", "DR.": "DR",
    "LANE": "LN", "LN.": "LN",
    "COURT": "CT", "CT.": "CT",
    "BOULEVARD": "BLVD", "BLVD.": "BLVD",
    "TERRACE": "TER", "TER.": "TER",
    "CIRCLE": "CIR", "CIR.": "CIR",
    "PARKWAY": "PKWY", "PKWY.": "PKWY",
}


def _normalize_property_address(addr: str) -> str:
    """Uppercase + collapse whitespace + abbreviate street suffix.

    Mirrors what taxrecords-nj stores so we can exact-compare candidate
    rows against the caller's address. Strips trailing ZIP / city.
    """
    if not addr:
        return ""
    s = addr.upper().strip()
    # Drop a trailing zip if present
    s = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", s)
    # Drop everything after a comma (city/state tail)
    s = s.split(",")[0].strip()
    parts = s.split()
    parts = [_STREET_SUFFIX_NORMALIZE.get(p, p) for p in parts]
    return " ".join(parts)


def _house_number(addr: str) -> str:
    """Extract the leading house number from a normalized address.

    Used to disambiguate substring hits — "8 PHYLLIS PL" must not match
    "18 PHYLLIS PL" or "108 PHYLLIS PL".
    """
    m = re.match(r"^\s*(\d+[A-Z]?)\b", addr.upper())
    return m.group(1) if m else ""


def _last_name(name: str) -> str:
    """Pull the surname out of a free-text owner field.

    Handles both "LAST, FIRST" (MOD-IV format) and "First Last" (caller
    input). Lower-cases for case-insensitive comparison.
    """
    if not name:
        return ""
    n = name.strip()
    if "," in n:
        return n.split(",", 1)[0].strip().lower()
    tokens = [t for t in re.split(r"\s+", n) if t]
    return tokens[-1].lower() if tokens else ""


def _name_token_key(name: str) -> str:
    """Order-independent identity key: sorted lowercased alphanumeric tokens.

    "Sally Baksh" and "BAKSH, SALLY" both → "baksh sally" — that's the
    SAME person in two formats, NOT a mismatch. Used to short-circuit
    the same-surname death-signal check.
    """
    if not name:
        return ""
    return " ".join(sorted(re.findall(r"[a-z0-9]+", name.lower())))


def _collapse_ws(s: str | None) -> str | None:
    """Collapse internal whitespace runs to single spaces; preserve None."""
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ── Death-signal classifier ─────────────────────────────────────────────


_BUSINESS_RE = re.compile(
    r"\b(LLC|L\.L\.C\.|INC|CORP|TRUST|LP|LTD|CO\.|COMPANY|BANK|ASSOC)\b",
    re.IGNORECASE,
)


def _classify_death_signal(
    *,
    modiv_owner: str,
    caller_owner: str | None,
) -> tuple[bool, str | None, list[str]]:
    """Decide whether the title evidence justifies a death signal.

    Returns (death_signal, reason, warnings).
    """
    warnings: list[str] = []

    if not modiv_owner:
        return False, None, warnings

    upper = modiv_owner.upper()

    # 1. NJ MOD-IV "(ESTATE)" suffix OR "ESTATE OF X" form — both fire.
    #    Examples seen in the wild:
    #      SCHWICHTENBERG, MARIE (ESTATE)
    #      BERNSHOCK, DANIEL S (ESTATE)
    #      ESTATE OF JOHN SMITH
    #    Skip when ESTATE is part of a business name ("ESTATE GARDENS
    #    LLC", "REAL ESTATE TRUST OF NJ") so we don't false-positive on
    #    commercial owners.
    if re.search(r"\bESTATE\b", upper) and not _BUSINESS_RE.search(upper):
        return True, "title_owner_estate_marker", warnings

    # 2. Owner-name pattern classifier (life_est, personal_rep, et_al, etc.)
    indicator = classify_owner_death_indicator(modiv_owner) or ""
    if indicator:
        return True, f"title_owner_indicator_{indicator}", warnings

    # 3. Surname-share with caller-supplied owner. Catches "Catherine
    #    Geczik" on probate docket vs "GECZIK, OLIVE" on MOD-IV — same
    #    family, deceased owner transferred title to surviving relative.
    if caller_owner:
        caller_last = _last_name(caller_owner)
        modiv_last = _last_name(modiv_owner)
        if caller_last and modiv_last and caller_last == modiv_last:
            # Same-person check: token-key compare so "Sally Baksh" and
            # "BAKSH, SALLY" don't fire a false-positive death signal.
            if _name_token_key(caller_owner) != _name_token_key(modiv_owner):
                return True, "title_owner_mismatch_same_surname", warnings
        elif caller_last and modiv_last and caller_last != modiv_last:
            # Different surname entirely — might mean stale caller data
            # (recent sale), might mean ID confusion. Warn but don't
            # fire the death signal.
            warnings.append(
                f"caller-supplied owner '{caller_owner}' has different "
                f"surname than MOD-IV owner '{modiv_owner}'"
            )

    return False, None, warnings


# ── Address-match filtering ─────────────────────────────────────────────


def _select_parcel_for_address(
    parcels: list[ModIVParcel],
    target_address: str,
) -> ModIVParcel | None:
    """Pick the parcel whose property_location exactly matches the caller's
    address, ignoring suffix variations (ST vs STREET).

    Returns None when no parcel matches or multiple ambiguous matches
    remain — caller treats this as "title unknown" rather than guessing.
    """
    if not parcels:
        return None

    target_norm = _normalize_property_address(target_address)
    target_house = _house_number(target_norm)

    if not target_house:
        # No house number to disambiguate on — fall back to first row,
        # but only if there's exactly one.
        return parcels[0] if len(parcels) == 1 else None

    matches: list[ModIVParcel] = []
    for p in parcels:
        cand_norm = _normalize_property_address(p.property_location)
        cand_house = _house_number(cand_norm)
        if cand_house == target_house and cand_norm == target_norm:
            matches.append(p)

    if len(matches) == 1:
        return matches[0]
    # Loose retry: same house number, suffix differs slightly.
    if not matches:
        loose = [
            p for p in parcels
            if _house_number(_normalize_property_address(p.property_location))
            == target_house
        ]
        if len(loose) == 1:
            return loose[0]
    return None


# ── Public entry point ──────────────────────────────────────────────────


async def run(prospect: ProspectInput) -> tuple[Lead, list[SourceCheck]]:
    """Phase 1: resolve title + death signal for `prospect`.

    Always returns a (Lead, [SourceCheck]) pair. The Lead may be sparse
    when the county isn't covered or the lookup returns nothing.
    """
    checks: list[SourceCheck] = []
    warnings: list[str] = []

    county = prospect.county
    if county is None or county not in _MODIV_COUNTIES:
        reason = (
            "essex_needs_separate_vendor" if county == "Essex"
            else "county_outside_modiv_coverage"
        )
        checks.append(SourceCheck(
            source="mod_iv",
            status="SKIPPED",
            notes=reason,
        ))
        warnings.append(
            f"MOD-IV title lookup skipped: {reason} (county={county})"
        )
        return (
            Lead(input=prospect, warnings=warnings),
            checks,
        )

    # MOD-IV lookup — prefer address (most specific), fall back to owner.
    # Both must be normalized to the taxrecords-nj format BEFORE the HTTP
    # call: addr lookup needs "102 GRACEY ST" (no city tail), owner lookup
    # needs the surname alone (e.g. "BAKSH") because the form's substring
    # matcher requires "LAST,FIRST" exact-comma form, not loose "Last First".
    parcel: ModIVParcel | None = None
    if prospect.address:
        normalized_addr = _normalize_property_address(prospect.address)
        parcels_by_addr = await _utils._safe_call(
            lambda: _async_modiv_lookup_by_address(normalized_addr, county),
            name=f"modiv.lookup_by_address[{county}]",
        ) or []
        parcel = _select_parcel_for_address(parcels_by_addr, prospect.address)
        if parcel is None and parcels_by_addr:
            warnings.append(
                f"MOD-IV returned {len(parcels_by_addr)} candidates for "
                f"'{prospect.address}' but none exact-matched"
            )

    if parcel is None and prospect.owner:
        owner_last = _last_name(prospect.owner).upper()
        if owner_last:
            parcels_by_owner = await _utils._safe_call(
                lambda: _async_modiv_lookup_by_owner(owner_last, county),
                name=f"modiv.lookup_by_owner[{county}]",
            ) or []
            # When falling back to owner-only, narrow by matching the
            # caller's address (if known) against property_location.
            if prospect.address:
                parcel = _select_parcel_for_address(parcels_by_owner, prospect.address)
            elif len(parcels_by_owner) == 1:
                parcel = parcels_by_owner[0]
            elif len(parcels_by_owner) > 1:
                warnings.append(
                    f"MOD-IV returned {len(parcels_by_owner)} parcels for "
                    f"owner '{prospect.owner}' — ambiguous, title unresolved"
                )

    if parcel is None:
        checks.append(SourceCheck(
            source="mod_iv",
            status="EMPTY",
            notes="no exact-match parcel found",
        ))
        return (
            Lead(input=prospect, warnings=warnings),
            checks,
        )

    checks.append(SourceCheck(source="mod_iv", status="HIT", notes=""))

    # Classify death signal from owner-name + caller cross-check.
    death_signal, death_reason, ds_warnings = _classify_death_signal(
        modiv_owner=parcel.owner_name,
        caller_owner=prospect.owner,
    )
    warnings.extend(ds_warnings)

    # Name variants — useful for downstream obit/heir search. Caller
    # owner (if given), MOD-IV owner, and a swapped "FIRST LAST" form
    # for "LAST, FIRST" rows.
    name_variants: list[str] = []
    if prospect.owner:
        name_variants.append(prospect.owner.strip())
    if parcel.owner_name:
        name_variants.append(parcel.owner_name.strip())
        if "," in parcel.owner_name:
            last, _, rest = parcel.owner_name.partition(",")
            name_variants.append(f"{rest.strip()} {last.strip()}".strip())
    # Dedup, preserve order
    seen: set[str] = set()
    name_variants = [
        n for n in name_variants
        if not (n.lower() in seen or seen.add(n.lower()))
    ]

    return (
        Lead(
            input=prospect,
            title_owner=_collapse_ws(parcel.owner_name),
            death_signal=death_signal,
            death_signal_reason=death_reason,
            name_variants=name_variants,
            mailing_address=_collapse_ws(parcel.mailing_full),
            parcel_id=parcel.parcel_id or None,
            warnings=warnings,
        ),
        checks,
    )


# ── Async wrappers around the sync bridge calls ─────────────────────────
# nj_taxrecords is sync (`requests`). Wrap each call in a thread executor
# via asyncio.to_thread so the orchestrator can await it without
# blocking other phases.

import asyncio  # noqa: E402  (placed late to keep public API at the top)


async def _async_modiv_lookup_by_address(
    address: str,
    county: str,
) -> list[ModIVParcel]:
    return await asyncio.to_thread(modiv_lookup_by_address, address, county)


async def _async_modiv_lookup_by_owner(
    owner: str,
    county: str,
) -> list[ModIVParcel]:
    return await asyncio.to_thread(modiv_lookup_by_owner, owner, county)
