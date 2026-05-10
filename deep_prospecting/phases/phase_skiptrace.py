"""Phase Skip-Trace — phones / addresses / associates for the DM.

Input:  DecisionMaker (from Phase 3)
        Lead (for context: property city/state)
        HeirMap (optional, used to cross-validate associates against
                 known family members)
Output: SkipTraceResult + list[SourceCheck] + CostBreakdown

Source policy:
  Slice 1 uses CBC only. The original spec named TPS / FPS / CBC but
  TruePeopleSearch is hard-blocked at every transport tried and
  FastPeopleSearch is Cloudflare-walled. CBC is the only free site that
  renders through Firecrawl with phones + relatives + associates intact.
  This is documented in deep_prospecting/sources/cbc.py and reflected
  here in `site_state` so the operator sees the substitution.

Address selection: when CBC returns a multi-address history, we promote
the most recent address (first in the listing) to ContactInfo.addresses
_current; everything else lands in addresses_previous.

Phone normalization: Phone() validator normalizes to E.164 at
construction. CBC returns 10-digit US strings; we wrap each in a Phone
object with type=UNKNOWN, source=cbc, confidence=MEDIUM. Phase
skip-trace doesn't classify mobile vs landline — that's a Trestle-tier
enrichment future phase.

Confidence:
  - HIGH: phone count >= 2 AND >= 1 address AND at least one known
          family relative cross-validates.
  - MEDIUM: phone count >= 1.
  - LOW: addresses only, no phones.
"""

from __future__ import annotations

import logging

from deep_prospecting._utils import _safe_call
from deep_prospecting.models import (
    Associate,
    Confidence,
    ContactInfo,
    CostBreakdown,
    DecisionMaker,
    Email,
    HeirMap,
    Lead,
    Phone,
    SkipTraceResult,
    SourceCheck,
    SourceState,
)
from deep_prospecting.sources.cbc import cbc_fetch_person

logger = logging.getLogger(__name__)


# CBC fetch consumes one Firecrawl page for the listing + one for the
# detail page = 2 pages. cost_estimator rate is $0.005/page.
RATE_FIRECRAWL_PER_PAGE = 0.005
RATE_SERPER_PER_SEARCH = 0.001  # for the Serper fallback URL discovery


def _parse_city_state_from_address(addr: str) -> tuple[str, str]:
    """'8 Phyllis Pl, Milltown, NJ 08850' → ('Milltown', 'NJ')."""
    import re
    if not addr:
        return "", ""
    s = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", addr.strip())
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) < 3:
        return "", ""
    state_tail = parts[-1].strip()
    state_code = state_tail[:2].upper()
    city = parts[-2].strip()
    return city, state_code


def _confidence_for(phones: list, addresses: list, family_overlap: int) -> Confidence:
    if len(phones) >= 2 and addresses and family_overlap >= 1:
        return "HIGH"
    if phones:
        return "MEDIUM"
    if addresses:
        return "LOW"
    return "LOW"


async def run(
    dm: DecisionMaker,
    lead: Lead,
    heir_map: HeirMap | None,
) -> tuple[SkipTraceResult, list[SourceCheck], CostBreakdown]:
    checks: list[SourceCheck] = []
    cost = CostBreakdown()

    # Slice 1: TPS + FPS are unreachable; record them as BLOCKED so the
    # operator sees the gap, then proceed to CBC.
    site_state: list[SourceState] = [
        SourceState(
            source="tps", status="BLOCKED",
            blocked_reason="TruePeopleSearch hard-blocked (JS/captcha walled even via Firecrawl)",
        ),
        SourceState(
            source="fps", status="BLOCKED",
            blocked_reason="FastPeopleSearch Cloudflare-walled",
        ),
    ]
    checks.append(SourceCheck(
        source="tps", status="BLOCKED",
        notes="TPS unreachable in Slice 1; using CBC as the free-tier skip-trace",
    ))

    # CBC fetch.
    city, state_code = _parse_city_state_from_address(lead.input.address or "")
    person, status = await _safe_call(
        lambda: cbc_fetch_person(dm.name, city, state_code),
        name=f"cbc[{dm.name}]",
    ) or (None, "ERROR")

    # Two Firecrawl page fetches: listing + detail. Charge both even on
    # partial success — conservative cost accounting.
    cost.firecrawl += 2 * RATE_FIRECRAWL_PER_PAGE
    cost.serper += RATE_SERPER_PER_SEARCH  # listing URL discovery

    if person is None or status != "HIT":
        site_state.append(SourceState(
            source="cbc", status=status,
            blocked_reason=("listing page returned empty" if status == "EMPTY" else None),
        ))
        checks.append(SourceCheck(
            source="cbc", status=status,
            notes=f"no CBC person record for {dm.name}",
        ))
        return (
            SkipTraceResult(
                decision_maker=dm,
                phones=[], emails=[], associates=[],
                site_state=site_state,
            ),
            checks,
            cost,
        )

    site_state.append(SourceState(source="cbc", status="HIT"))
    checks.append(SourceCheck(
        source="cbc", status="HIT",
        notes=(
            f"{len(person.phones)} phones, {len(person.emails)} emails, "
            f"{len(person.addresses)} addresses, {len(person.relatives)} relatives, "
            f"{len(person.associates)} associates"
        ),
    ))

    # Build typed Phone/Email/Associate objects.
    phones: list[Phone] = []
    for digits in person.phones:
        try:
            phones.append(Phone(
                number=digits,
                type="UNKNOWN",
                sources=["cbc"],
                confidence="MEDIUM",
            ))
        except ValueError as e:
            logger.debug("rejected phone %s: %s", digits, e)

    emails: list[Email] = [
        Email(address=addr, sources=["cbc"]) for addr in person.emails
    ]

    # Cross-validate associates against known heirs (relatives from the
    # obit are a high-precision overlap signal). Skip associates whose
    # last-name overlaps with the decedent's family — those are surfaced
    # as relatives separately, not as "associates".
    known_family_lower: set[str] = set()
    if heir_map:
        for h in (heir_map.heirs or []):
            for tok in h.name.lower().split():
                known_family_lower.add(tok)
        if heir_map.decedent_name:
            for tok in heir_map.decedent_name.lower().split():
                known_family_lower.add(tok)

    family_overlap = 0
    associates: list[Associate] = []
    for nm in person.relatives + person.associates:
        nm_lower_tokens = set(nm.lower().split())
        is_known = bool(nm_lower_tokens & known_family_lower)
        if is_known and nm in person.relatives:
            family_overlap += 1
        associates.append(Associate(
            name=nm,
            relationship=("relative" if nm in person.relatives else "associate"),
            sources=["cbc"],
        ))

    # Address selection — first address listed is the most recent.
    addresses_current = person.addresses[:1]
    addresses_previous = person.addresses[1:]

    contact = ContactInfo(
        addresses_current=addresses_current,
        addresses_previous=addresses_previous,
        age_estimate=(
            (person.age - 1, person.age + 1) if person.age else None
        ),
    )

    # Promote the DM to VERIFIED_LIVING if Phase 3 left them UNVERIFIED
    # and CBC returned a populated record (someone with phones + current
    # address is necessarily alive at the time the data was indexed).
    if dm.status == "UNVERIFIED" and (phones or addresses_current):
        dm_status = "VERIFIED_LIVING"
    else:
        dm_status = dm.status

    confidence = _confidence_for(phones, addresses_current, family_overlap)

    updated_dm = dm.model_copy(update={
        "status": dm_status,
        "contact": contact,
        "confidence": confidence,
    })

    return (
        SkipTraceResult(
            decision_maker=updated_dm,
            phones=phones,
            emails=emails,
            associates=associates,
            site_state=site_state,
        ),
        checks,
        cost,
    )
