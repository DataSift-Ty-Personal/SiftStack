"""Phase Skip-Trace — multi-person via Tracerfy + CBC source merge.

Slice 2 step 7: loops over `decision_makers` (top-N verified-living
heirs from Phase 3, primary at index 0) and skip-traces each via
Tracerfy + CBC. Per-heir phones are tagged with `person_name` so the
writer's downstream star-derivation produces the **/***/**** marker
map.

Per-heir flow:
  1. CBC fetch using (heir_name, property_city, property_state) — the
     property city is the seed because heirs tied to the property
     surface even when they live elsewhere (CBC's
     /people/{name}/{city}-{state} URL is association-keyed, not
     residence-keyed). Catherine in Milltown returns her NYC address
     this way.
  2. If CBC returned a current address for the heir, use it as the
     Tracerfy lookup key. Otherwise fall back to property city/state.
  3. Tracerfy filter:
       - drop persons where `deceased=True` (Tracerfy returns historical
         residents — Louise + Michael Olshen surfaced at Catherine's
         NYC apt as +100yo deceased records in step 3 testing)
       - name-match heir.first + heir.last (case-insensitive)
       - on multiple alive same-surname matches: take the highest-rank
         candidate (Tracerfy ranks 1=best); log warning
  4. Source-merge phones by E.164 within the heir:
       - shared phone → sources=['cbc', 'tracerfy'] (Verified 2+ Sites)
       - single-source → sources=['cbc'] or sources=['tracerfy']
  5. Tag every Phone with person_name=heir.name

If CBC + Tracerfy BOTH return nothing for an heir, emit warning
'phase_skiptrace_unresolved:{heir.name}' for the orchestrator to push
onto lead.warnings, and continue. One heir's failure never aborts.

Cost: Tracerfy $0.10/hit (miss free) + CBC ~$0.011/heir (2 Firecrawl
pages + 1 Serper). Per L3 case w/ 3 heirs all hitting: max ~$0.33.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

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
    PhoneType,
    SkipTraceResult,
    SourceCheck,
    SourceState,
)
from deep_prospecting.sources.cbc import cbc_fetch_person
from deep_prospecting.sources import tracerfy
from deep_prospecting.sources.tracerfy import TracerfyPerson, TracerfyResult

logger = logging.getLogger(__name__)


RATE_FIRECRAWL_PER_PAGE = 0.005
RATE_SERPER_PER_SEARCH = 0.001
RATE_TRACERFY_PER_HIT = 0.10


# Trestle line_type → Phone.type Literal. Shared with phase_trestle but
# kept independent here since Tracerfy returns the same string values.
_TRACERFY_TYPE_MAP: dict[str, PhoneType] = {
    "Mobile": "MOBILE",
    "Landline": "LANDLINE",
    "FixedVOIP": "VOIP",
    "NonFixedVOIP": "VOIP",
}


# ── Helpers ─────────────────────────────────────────────────────────────


def _parse_city_state_from_address(addr: str) -> tuple[str, str]:
    """'8 Phyllis Pl, Milltown, NJ 08850' → ('Milltown', 'NJ')."""
    if not addr:
        return "", ""
    s = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", addr.strip())
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) < 3:
        return "", ""
    return parts[-2].strip(), parts[-1].strip()[:2].upper()


def _name_tokens(name: str) -> tuple[str, str]:
    """Return (first_lower, last_lower) for case-insensitive comparison.

    Strips honorifics ("Dr.", "Mr.", etc.) and middle names/initials.
    """
    tokens = re.findall(r"[A-Za-z]+", (name or ""))
    if not tokens:
        return "", ""
    # Drop common honorifics
    HONORIFICS = {"Dr", "Mr", "Mrs", "Ms", "Sir", "Madam", "Prof", "Rev"}
    if tokens[0] in HONORIFICS:
        tokens = tokens[1:]
    if not tokens:
        return "", ""
    first = tokens[0].lower()
    last = tokens[-1].lower() if len(tokens) > 1 else ""
    return first, last


def _tracerfy_person_matches_heir(p: TracerfyPerson, heir_name: str) -> bool:
    """Case-insensitive first+last token match. None of Tracerfy's
    historical-resident artifacts (Louise/Michael Olshen) match a
    Geczik heir; only same-surname-same-first-name candidates do."""
    h_first, h_last = _name_tokens(heir_name)
    p_first = (p.first_name or "").strip().lower()
    p_last = (p.last_name or "").strip().lower()
    if not h_first or not h_last:
        return False
    return p_first == h_first and p_last == h_last


def _pick_tracerfy_candidate(
    candidates: list[TracerfyPerson], heir_name: str,
) -> tuple[TracerfyPerson | None, str | None]:
    """Among same-surname-same-first-name alive candidates, take the
    one whose primary phone has the lowest rank (rank 1 = best per
    Tracerfy's internal scoring). Returns (candidate, ambiguity_warning).
    """
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0], None

    def _primary_rank(p: TracerfyPerson) -> int:
        if not p.phones:
            return 999
        return min(ph.rank or 999 for ph in p.phones)

    candidates_sorted = sorted(candidates, key=_primary_rank)
    chosen = candidates_sorted[0]
    warn = (
        f"phase_skiptrace_ambiguous:{heir_name} "
        f"(took top-rank of {len(candidates)} same-name candidates)"
    )
    return chosen, warn


def _build_phone_from_tracerfy(
    tp_phone, *, person_name: str, person: TracerfyPerson,
) -> Phone | None:
    mapped_type = _TRACERFY_TYPE_MAP.get(tp_phone.type, "UNKNOWN")
    try:
        return Phone(
            number=tp_phone.number,
            type=mapped_type,
            sources=["tracerfy"],
            confidence="HIGH",
            person_name=person_name,
            dnc=tp_phone.dnc,
            carrier=tp_phone.carrier or None,
            is_litigator=person.litigator,
        )
    except ValueError as e:
        logger.debug("rejected tracerfy phone %s: %s", tp_phone.number, e)
        return None


def _merge_tracerfy_into(
    phones_by_e164: dict[str, Phone],
    tp_persons: list[TracerfyPerson],
    *,
    person_name: str,
) -> int:
    """Fold Tracerfy phones into the by-E.164 map. Returns count of NEW
    unique phones added (post-dedup against CBC)."""
    added = 0
    for tp in tp_persons:
        for tp_phone in tp.phones:
            phone = _build_phone_from_tracerfy(
                tp_phone, person_name=person_name, person=tp,
            )
            if phone is None:
                continue
            if phone.number in phones_by_e164:
                # Cross-source overlap → upgrade existing CBC-only phone.
                existing = phones_by_e164[phone.number]
                updates: dict = {}
                new_sources = list(existing.sources)
                if "tracerfy" not in new_sources:
                    new_sources.append("tracerfy")
                    updates["sources"] = new_sources
                if existing.type == "UNKNOWN" and phone.type != "UNKNOWN":
                    updates["type"] = phone.type
                if existing.dnc is None and phone.dnc is not None:
                    updates["dnc"] = phone.dnc
                if not existing.carrier and phone.carrier:
                    updates["carrier"] = phone.carrier
                if existing.is_litigator is None and phone.is_litigator is not None:
                    updates["is_litigator"] = phone.is_litigator
                if updates:
                    phones_by_e164[phone.number] = existing.model_copy(update=updates)
            else:
                phones_by_e164[phone.number] = phone
                added += 1
    return added


# ── Per-heir skip-trace ─────────────────────────────────────────────────


@dataclass
class _HeirResult:
    phones: list[Phone] = field(default_factory=list)
    emails: list[Email] = field(default_factory=list)
    associates: list[Associate] = field(default_factory=list)
    site_state: list[SourceState] = field(default_factory=list)
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    warnings: list[str] = field(default_factory=list)
    cbc_current_address: str | None = None
    cbc_addresses: list[str] = field(default_factory=list)
    cbc_age: int | None = None
    family_overlap: int = 0
    cbc_hit: bool = False
    tracerfy_hit: bool = False


async def _skiptrace_one_heir(
    dm: DecisionMaker,
    property_city: str,
    property_state: str,
    heir_map: HeirMap | None,
) -> _HeirResult:
    result = _HeirResult()
    phones_by_e164: dict[str, Phone] = {}

    # ── Stage 1: CBC ──
    cbc_pair = await _safe_call(
        lambda: cbc_fetch_person(dm.name, property_city, property_state),
        name=f"cbc[{dm.name}]",
    )
    if cbc_pair is None:
        cbc_person, cbc_status = None, "ERROR"
    else:
        cbc_person, cbc_status = cbc_pair

    # CBC cost is charged regardless of HIT/EMPTY — Firecrawl bills on
    # fetch, not on data depth.
    result.cost.firecrawl = round(2 * RATE_FIRECRAWL_PER_PAGE, 4)
    result.cost.serper = round(RATE_SERPER_PER_SEARCH, 4)

    if cbc_person is not None and cbc_status == "HIT":
        result.cbc_hit = True
        result.site_state.append(SourceState(source="cbc", status="HIT"))
        result.cbc_age = cbc_person.age
        result.cbc_addresses = list(cbc_person.addresses)
        if cbc_person.addresses:
            result.cbc_current_address = cbc_person.addresses[0]

        # Build phones tagged with this heir's name.
        for digits in cbc_person.phones:
            try:
                phones_by_e164[
                    Phone(
                        number=digits, type="UNKNOWN",
                        sources=["cbc"], confidence="MEDIUM",
                        person_name=dm.name,
                    ).number
                ] = Phone(
                    number=digits, type="UNKNOWN",
                    sources=["cbc"], confidence="MEDIUM",
                    person_name=dm.name,
                )
            except ValueError as e:
                logger.debug("rejected cbc phone %s: %s", digits, e)

        for addr in cbc_person.emails:
            result.emails.append(Email(address=addr, sources=["cbc"]))

        # Associates + family overlap (only for primary's relatives —
        # this is the obit-family-overlap signal that promotes confidence).
        known_family_lower: set[str] = set()
        if heir_map:
            for h in (heir_map.heirs or []):
                for tok in h.name.lower().split():
                    known_family_lower.add(tok)
            if heir_map.decedent_name:
                for tok in heir_map.decedent_name.lower().split():
                    known_family_lower.add(tok)
        for nm in cbc_person.relatives + cbc_person.associates:
            if not nm:
                continue
            nm_tokens = set(nm.lower().split())
            is_rel = nm in cbc_person.relatives
            if is_rel and nm_tokens & known_family_lower:
                result.family_overlap += 1
            result.associates.append(Associate(
                name=nm,
                relationship=("relative" if is_rel else "associate"),
                sources=["cbc"],
            ))
    else:
        result.site_state.append(SourceState(
            source="cbc", status=cbc_status,
            blocked_reason=("listing empty" if cbc_status == "EMPTY" else None),
        ))

    # ── Stage 2: Tracerfy ──
    # Lookup address: prefer CBC's current address (more specific). Fall
    # back to property city/state with name-anchored search (find_owner
    # false + first/last) when CBC didn't return a useful address.
    tr: TracerfyResult | None = None
    if result.cbc_current_address:
        # CBC's address strings look like "145 E 22nd St APT 6C,
        # New York, NY 10010 5510". Parse out street / city / state / zip.
        addr_parts = _parse_cbc_address(result.cbc_current_address)
        if addr_parts:
            street, city, state, zip_ = addr_parts
            tr = await _safe_call(
                lambda: tracerfy.search(
                    address=street, city=city, state=state, zip=zip_,
                    find_owner=True,
                ),
                name=f"tracerfy[{dm.name}]",
            )

    if tr is None or not tr.hit:
        # Address-based miss / no CBC address — try name-anchored at
        # property city. find_owner=False so Tracerfy looks for THIS
        # person, not whoever owns the property.
        first, last = _name_tokens(dm.name)
        if first and last and property_city and property_state:
            tr = await _safe_call(
                lambda: tracerfy.search(
                    address=property_city,  # weak hint — Tracerfy ignores when find_owner=False
                    city=property_city, state=property_state,
                    find_owner=False,
                    first_name=first.title(), last_name=last.title(),
                ),
                name=f"tracerfy[name,{dm.name}]",
            )

    if tr is not None:
        result.cost.tracerfy = round(result.cost.tracerfy + tr.cost_usd, 4)
        if tr.hit:
            result.tracerfy_hit = True
            result.site_state.append(SourceState(source="tracerfy", status="HIT"))
            # Filter persons: drop deceased + name-match.
            alive_named = [
                p for p in tr.persons
                if not p.deceased and _tracerfy_person_matches_heir(p, dm.name)
            ]
            chosen, ambig = _pick_tracerfy_candidate(alive_named, dm.name)
            if ambig:
                result.warnings.append(ambig)
            if chosen is not None:
                _merge_tracerfy_into(
                    phones_by_e164, [chosen], person_name=dm.name,
                )
                # Add Tracerfy emails not already present (case-insensitive)
                existing_emails_lower = {e.address.lower() for e in result.emails}
                for tp_email in chosen.emails:
                    addr_lower = tp_email.email.strip().lower()
                    if addr_lower and addr_lower not in existing_emails_lower:
                        result.emails.append(Email(
                            address=tp_email.email.strip(), sources=["tracerfy"],
                        ))
                        existing_emails_lower.add(addr_lower)
        else:
            result.site_state.append(SourceState(
                source="tracerfy", status=tr.status,
                blocked_reason=tr.error,
            ))
    else:
        result.site_state.append(SourceState(
            source="tracerfy", status="ERROR",
            blocked_reason="all retries failed",
        ))

    # ── Resolution check ──
    if not phones_by_e164 and not result.cbc_addresses:
        result.warnings.append(f"phase_skiptrace_unresolved:{dm.name}")

    result.phones = list(phones_by_e164.values())
    return result


def _parse_cbc_address(addr: str) -> tuple[str, str, str, str] | None:
    """CBC: '145 E 22nd St APT 6C, New York, NY 10010 5510' →
    (street, city, state, zip). Returns None on unparseable shape."""
    if not addr:
        return None
    # The 'NY 10010 5510' tail = state + zip + zip4 (with space). Drop the zip4.
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if len(parts) < 3:
        return None
    street = parts[0]
    city = parts[1] if len(parts) >= 3 else ""
    tail = parts[-1]
    # 'NY 10010 5510' or 'NY 10010'
    m = re.match(r"^([A-Z]{2})\s+(\d{5})(?:[-\s]\d{4})?$", tail)
    if not m:
        return None
    state, zip_ = m.group(1), m.group(2)
    return street, city, state, zip_


# ── Public entry point ─────────────────────────────────────────────────


def _confidence_for_primary(
    phones: list[Phone], addresses: list[str], family_overlap: int,
    *, title_address_overlap: bool = False,
) -> Confidence:
    if len(phones) >= 2 and addresses and family_overlap >= 1:
        return "HIGH"
    if phones and addresses and title_address_overlap:
        return "HIGH"
    if phones:
        return "MEDIUM"
    if addresses:
        return "LOW"
    return "LOW"


def _title_address_overlaps_cbc(title: str | None, cbc_addrs: list[str]) -> bool:
    if not title or not cbc_addrs:
        return False
    def _key(s: str) -> str:
        s = re.sub(r"\s+", " ", s.upper()).strip().split(",")[0]
        s = re.sub(r"\b(STREET|AVENUE|ROAD|PLACE|DRIVE|LANE|COURT|"
                   r"BOULEVARD|TERRACE|CIRCLE|PARKWAY)\b", "", s)
        s = re.sub(r"\b(ST|AVE|RD|PL|DR|LN|CT|BLVD|TER|CIR|PKWY)\b", "", s)
        s = re.sub(r"\b(APT|APARTMENT|UNIT|STE|#)\b.*$", "", s)
        return " ".join(s.split())
    tk = _key(title)
    return tk != "" and any(_key(a).startswith(tk) for a in cbc_addrs)


async def run(
    decision_makers: list[DecisionMaker],
    lead: Lead,
    heir_map: HeirMap | None,
) -> tuple[SkipTraceResult, list[SourceCheck], CostBreakdown, list[str]]:
    """Skip-trace every DM in `decision_makers`. Returns
    (skip_trace, checks, cost, warnings_to_append_to_lead).

    The 4th return value is a list of warning strings the orchestrator
    folds onto `lead.warnings` so the markdown report + downstream
    consumers can see which heirs couldn't be resolved.
    """
    checks: list[SourceCheck] = []
    cost = CostBreakdown()
    warnings: list[str] = []

    # TPS / FPS BLOCKED preamble — operator sees the substitution.
    site_state: list[SourceState] = [
        SourceState(source="tps", status="BLOCKED",
                    blocked_reason="TruePeopleSearch hard-blocked"),
        SourceState(source="fps", status="BLOCKED",
                    blocked_reason="FastPeopleSearch Cloudflare-walled"),
    ]
    checks.append(SourceCheck(
        source="tps", status="BLOCKED",
        notes="TPS unreachable; using CBC + Tracerfy as the source mix",
    ))

    if not decision_makers:
        # No DM (e.g. no caller owner + no heirs) — empty result.
        return (
            SkipTraceResult(
                decision_maker=DecisionMaker(
                    name="(unknown)", relationship="(unknown)",
                    status="UNVERIFIED", subject_role="SUBJECT",
                    confidence="LOW", reasoning="no decision-makers to skip-trace",
                ),
                phones=[], emails=[], associates=[], site_state=site_state,
            ),
            checks, cost, warnings,
        )

    property_city, property_state = _parse_city_state_from_address(
        lead.input.address or "")

    # Run per-heir skip-trace in parallel — independent calls, faster
    # wall clock for L3 cases with 3 heirs.
    import asyncio
    heir_results = await asyncio.gather(*[
        _skiptrace_one_heir(dm, property_city, property_state, heir_map)
        for dm in decision_makers
    ], return_exceptions=False)

    # Accumulate
    all_phones: list[Phone] = []
    all_emails: list[Email] = []
    all_associates: list[Associate] = []
    primary_result: _HeirResult | None = None

    for i, (dm, r) in enumerate(zip(decision_makers, heir_results)):
        if i == 0:
            primary_result = r
        all_phones.extend(r.phones)
        all_emails.extend(r.emails)
        all_associates.extend(r.associates)
        # Site state: dedupe by (source, status) — every heir contributes
        # one cbc + one tracerfy entry; folding them down to one per
        # source per status keeps the report readable.
        for s in r.site_state:
            site_state.append(s)
        cost = CostBreakdown(
            anthropic=round(cost.anthropic + r.cost.anthropic, 6),
            serper=round(cost.serper + r.cost.serper, 6),
            firecrawl=round(cost.firecrawl + r.cost.firecrawl, 6),
            smarty=round(cost.smarty + r.cost.smarty, 6),
            tracerfy=round(cost.tracerfy + r.cost.tracerfy, 6),
            trestle=round(cost.trestle + r.cost.trestle, 6),
            other=round(cost.other + r.cost.other, 6),
        )
        warnings.extend(r.warnings)
        # Per-heir source check note
        per_heir_phones = len(r.phones)
        per_heir_status = "HIT" if (r.cbc_hit or r.tracerfy_hit) else "EMPTY"
        sources_hit = []
        if r.cbc_hit: sources_hit.append("cbc")
        if r.tracerfy_hit: sources_hit.append("tracerfy")
        checks.append(SourceCheck(
            source="cbc",
            status=per_heir_status,
            notes=(
                f"heir={dm.name}: {per_heir_phones} phones, "
                f"{len(r.emails)} emails, sources={sources_hit or 'none'}"
            ),
        ))

    # Build the primary DM with contact info from primary_result.
    primary_dm = decision_makers[0]
    if primary_result is not None:
        addresses_current = (
            [primary_result.cbc_current_address]
            if primary_result.cbc_current_address else []
        )
        addresses_previous = [
            a for a in primary_result.cbc_addresses
            if a != primary_result.cbc_current_address
        ]
        contact = ContactInfo(
            addresses_current=addresses_current,
            addresses_previous=addresses_previous,
            age_estimate=(
                (primary_result.cbc_age - 1, primary_result.cbc_age + 1)
                if primary_result.cbc_age else None
            ),
        )
        # Confidence promotion for L1 primary subjects (title-address match).
        title_overlap = _title_address_overlaps_cbc(
            lead.mailing_address, primary_result.cbc_addresses,
        )
        primary_phones = [p for p in all_phones if p.person_name == primary_dm.name]
        new_confidence = _confidence_for_primary(
            primary_phones, addresses_current, primary_result.family_overlap,
            title_address_overlap=title_overlap,
        )
        # Phase 3 sets confidence based on relationship + caller match;
        # don't downgrade it (HIGH from Phase 3 stays HIGH).
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        if rank.get(new_confidence, 3) < rank.get(primary_dm.confidence, 3):
            final_conf = new_confidence
        else:
            final_conf = primary_dm.confidence

        # Promote status if Phase 3 left it UNVERIFIED but skip-trace found data.
        new_status = primary_dm.status
        if primary_dm.status == "UNVERIFIED" and (primary_phones or addresses_current):
            new_status = "VERIFIED_LIVING"

        primary_dm = primary_dm.model_copy(update={
            "contact": contact, "confidence": final_conf, "status": new_status,
        })

    return (
        SkipTraceResult(
            decision_maker=primary_dm,
            phones=all_phones, emails=all_emails, associates=all_associates,
            site_state=site_state,
        ),
        checks, cost, warnings,
    )
