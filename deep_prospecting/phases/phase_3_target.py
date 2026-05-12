"""Phase 3 — Decision-maker selection.

Input:  Lead (from Phase 1)
        HeirMap (from Phase 2.5, with heirs marked LIVING / DECEASED)
Output: DecisionMaker + list[SourceCheck] + CostBreakdown

Selection priority (per the deep-prospecting skill):
  1. Named executor — if obit/legal docs identified one and they're
     LIVING → relationship="executor", subject_role=EXECUTOR.
  2. Surviving spouse → subject_role=HEIR (spouse is highest-priority
     non-executor heir).
  3. Oldest LIVING child (first listed son/daughter) → subject_role=HEIR.
  4. Sibling → subject_role=HEIR.
  5. Family pivot — when none of the above are LIVING but the caller's
     `input.owner` matches an obit survivor → subject_role=FAMILY_PIVOT.
  6. Lead.input.owner as fallback subject when no obit was parsed →
     subject_role=SUBJECT (Phase 2 returned nothing useful).

Confidence:
  - HIGH: LIVING executor, OR LIVING heir matching caller's input owner,
          OR sole LIVING child of decedent.
  - MEDIUM: LIVING heir without strong tie-breaking signal (e.g., one
            of several siblings).
  - LOW: UNVERIFIED heir, or fallback to input.owner with no verification.

Reasoning paragraph: written by Claude Sonnet 4.6 (claude-sonnet-4-6)
in 1-2 paragraphs. The prompt provides the Lead, HeirMap (with
statuses), and the chosen DM; Sonnet returns prose explaining WHY this
person is the right decision-maker. Single Anthropic call per Phase 3
run (~$0.01-0.02). Falls back to a deterministic template if the LLM
call fails or no API key is available — Phase 3 never crashes the run.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from deep_prospecting._siftstack_bridge import sonnet_text
from deep_prospecting.models import (
    Confidence,
    ContactInfo,
    CostBreakdown,
    DecisionMaker,
    Heir,
    HeirMap,
    Lead,
    SourceCheck,
    SubjectRole,
)

logger = logging.getLogger(__name__)


# Per-call Sonnet cost — coarse: 1 paragraph in / out, ~700 tokens roundtrip.
# Sonnet 4.6 is $3/MTok input, $15/MTok output. ~700 in + 200 out ≈ $0.005.
RATE_SONNET_PER_REASONING = 0.005


_SPOUSE_TERMS = ("wife", "husband", "spouse", "partner")
_CHILD_TERMS = ("son", "daughter", "child", "stepson", "stepdaughter")
_SIBLING_TERMS = ("brother", "sister", "sibling")
_GRANDCHILD_TERMS = ("grandchild", "grandson", "granddaughter")


# Relationship → role tier for multi-DM ranking. Lower number = higher
# priority. Matches the deep-prospecting skill's contact-priority spec:
#   1 executor, 2 spouse, 3 child, 4 sibling, 5 grandchild, 6 other.
_ROLE_TIER_EXECUTOR = 1
_ROLE_TIER_SPOUSE = 2
_ROLE_TIER_CHILD = 3
_ROLE_TIER_SIBLING = 4
_ROLE_TIER_GRANDCHILD = 5
_ROLE_TIER_OTHER = 6

_CONFIDENCE_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _role_tier_for(heir: Heir) -> int:
    if _is_executor(heir):
        return _ROLE_TIER_EXECUTOR
    if _is_relationship(heir, _SPOUSE_TERMS):
        return _ROLE_TIER_SPOUSE
    # Grandchild MUST be checked before child — "granddaughter" / "grandson"
    # substring-match the child terms ("daughter" / "son"), so the
    # narrower category needs to win first.
    if _is_relationship(heir, _GRANDCHILD_TERMS):
        return _ROLE_TIER_GRANDCHILD
    if _is_relationship(heir, _CHILD_TERMS):
        return _ROLE_TIER_CHILD
    if _is_relationship(heir, _SIBLING_TERMS):
        return _ROLE_TIER_SIBLING
    return _ROLE_TIER_OTHER


def _is_relationship(heir: Heir, terms: tuple[str, ...]) -> bool:
    rel = (heir.relationship or "").lower()
    return any(t in rel for t in terms)


def _is_executor(heir: Heir) -> bool:
    rel = (heir.relationship or "").lower()
    return "executor" in rel or "personal representative" in rel


def _matches_caller_owner(heir: Heir, caller_owner: str | None) -> bool:
    if not caller_owner:
        return False
    caller = caller_owner.lower()
    heir_lower = (heir.name or "").lower()
    # Token-overlap heuristic: first AND last tokens of caller appear in
    # the heir's name. Catches "Catherine Geczik" vs "Catherine M. Geczik".
    caller_tokens = [t for t in caller.split() if t]
    if len(caller_tokens) < 2:
        return caller in heir_lower
    return caller_tokens[0] in heir_lower and caller_tokens[-1] in heir_lower


@dataclass(frozen=True)
class _RankedDM:
    """Internal struct used during the multi-DM ranking."""
    heir: Heir
    subject_role: SubjectRole
    confidence: Confidence
    why: str
    role_tier: int


def _classify_heir(
    heir: Heir, caller_owner: str | None,
) -> tuple[SubjectRole, Confidence, str, int]:
    """Compute (subject_role, confidence, why, role_tier) for one heir."""
    tier = _role_tier_for(heir)
    caller_match = _matches_caller_owner(heir, caller_owner)

    if tier == _ROLE_TIER_EXECUTOR:
        return ("EXECUTOR", "HIGH",
                f"named executor in obituary; verified LIVING",
                tier)

    if caller_match:
        role: SubjectRole = "HEIR"
        return (role, "HIGH",
                f"heir '{heir.name}' matches caller-supplied owner "
                f"'{caller_owner}'; verified LIVING",
                tier)

    if tier == _ROLE_TIER_SPOUSE:
        return ("HEIR", "HIGH",
                f"surviving spouse '{heir.name}', LIVING",
                tier)

    if tier == _ROLE_TIER_CHILD:
        return ("HEIR", "MEDIUM",
                f"LIVING child '{heir.name}'",
                tier)

    if tier == _ROLE_TIER_SIBLING:
        return ("HEIR", "MEDIUM",
                f"LIVING sibling '{heir.name}'",
                tier)

    if tier == _ROLE_TIER_GRANDCHILD:
        return ("HEIR", "MEDIUM",
                f"LIVING grandchild '{heir.name}'",
                tier)

    return ("HEIR", "MEDIUM",
            f"LIVING heir '{heir.name}' ({heir.relationship})",
            tier)


def pick_decision_makers(
    heir_map: HeirMap | None,
    *,
    caller_owner: str | None = None,
    max_n: int | None = None,
) -> list[_RankedDM]:
    """Rank LIVING heirs and return top-N as a list of _RankedDM.

    Selection order within LIVING heirs only:
      1 executor, 2 spouse, 3 child, 4 sibling, 5 grandchild, 6 other.
    Within tier: HIGH > MEDIUM > LOW confidence.
    Within tier + confidence: stable alphabetical by name.
    Cap at `max_n`; if None, read PROSPECT_MAX_DMS env var, default 3.

    Heir-level signals:
      - caller_owner match boosts an heir's confidence to HIGH
        (matches caller-supplied owner = strong on-docket signal)
      - tier stays whatever the relationship dictates — caller-match
        is a CONFIDENCE boost, not a tier promotion

    Returns empty list when heir_map has no LIVING heirs. Callers handle
    the empty case (typically: fall back to caller-supplied owner as
    SUBJECT, or fire FAMILY_PIVOT for UNVERIFIED caller match).
    """
    if max_n is None:
        try:
            max_n = int(os.environ.get("PROSPECT_MAX_DMS", "3"))
        except ValueError:
            max_n = 3
    max_n = max(1, max_n)

    if heir_map is None or not heir_map.heirs:
        return []

    living = [h for h in heir_map.heirs if h.status == "LIVING"]
    if not living:
        return []

    ranked: list[_RankedDM] = []
    for heir in living:
        role, conf, why, tier = _classify_heir(heir, caller_owner)
        ranked.append(_RankedDM(
            heir=heir, subject_role=role, confidence=conf,
            why=why, role_tier=tier,
        ))

    # Sort: tier asc, confidence rank asc, name asc.
    ranked.sort(key=lambda r: (
        r.role_tier,
        _CONFIDENCE_RANK.get(r.confidence, 3),
        r.heir.name.lower(),
    ))

    # Cap. When more LIVING heirs exist beyond max_n, drop the lowest-
    # priority ones — never raise.
    return ranked[:max_n]


def _pick_dm(
    lead: Lead,
    heir_map: HeirMap | None,
) -> tuple[Heir | None, SubjectRole, Confidence, str]:
    """Pick the DM. Returns (heir_or_None, subject_role, confidence, why).

    `heir_or_None` is the chosen heir; None means "fall back to input.owner"
    in the caller. `why` is a short bullet string used in the Sonnet prompt
    and as the deterministic-template fallback.
    """
    caller_owner = (lead.input.owner or "").strip() or None
    heirs = (heir_map.heirs if heir_map else []) or []
    living = [h for h in heirs if h.status == "LIVING"]
    unverified = [h for h in heirs if h.status == "UNVERIFIED"]

    # 1. LIVING executor.
    living_exec = next((h for h in living if _is_executor(h)), None)
    if living_exec:
        return (
            living_exec, "EXECUTOR", "HIGH",
            f"named executor in obituary; verified LIVING",
        )

    # 2. LIVING heir matching the caller's input owner — typically the
    #    PR/petitioner/heir-on-docket. Strong signal that this is the
    #    person the runner already identified.
    caller_match = next(
        (h for h in living if _matches_caller_owner(h, caller_owner)),
        None,
    )
    if caller_match:
        role: SubjectRole = "EXECUTOR" if _is_executor(caller_match) else "HEIR"
        return (
            caller_match, role, "HIGH",
            f"heir '{caller_match.name}' matches caller-supplied "
            f"owner '{caller_owner}'; verified LIVING",
        )

    # 3. LIVING spouse.
    spouse = next((h for h in living if _is_relationship(h, _SPOUSE_TERMS)), None)
    if spouse:
        return spouse, "HEIR", "HIGH", f"surviving spouse '{spouse.name}', LIVING"

    # 4. Oldest LIVING child (first listed — obits typically order by age).
    child = next((h for h in living if _is_relationship(h, _CHILD_TERMS)), None)
    if child:
        # Sole LIVING child → HIGH; multiple → MEDIUM (would normally pick
        # the eldest but obit ordering is the only proxy we have).
        all_children_living = [h for h in living if _is_relationship(h, _CHILD_TERMS)]
        confidence: Confidence = "HIGH" if len(all_children_living) == 1 else "MEDIUM"
        return (
            child, "HEIR", confidence,
            f"first-listed LIVING child '{child.name}' "
            f"(of {len(all_children_living)} living children)",
        )

    # 5. LIVING sibling.
    sibling = next((h for h in living if _is_relationship(h, _SIBLING_TERMS)), None)
    if sibling:
        return sibling, "HEIR", "MEDIUM", f"LIVING sibling '{sibling.name}'"

    # 6. Any remaining LIVING heir.
    if living:
        return living[0], "HEIR", "MEDIUM", (
            f"first LIVING heir on record: '{living[0].name}' "
            f"({living[0].relationship})"
        )

    # 7. Family pivot — caller's name matches an UNVERIFIED heir.
    if caller_owner:
        pivot = next(
            (h for h in unverified if _matches_caller_owner(h, caller_owner)),
            None,
        )
        if pivot:
            return pivot, "FAMILY_PIVOT", "LOW", (
                f"caller owner '{caller_owner}' present as UNVERIFIED heir; "
                f"treat as family pivot"
            )

    # 8. Fallback — no usable heir. Use the caller's owner string itself
    #    as the subject; phase 3 will return a sparse DM that downstream
    #    phases (skip trace) can still attempt against.
    return None, "SUBJECT", "LOW", (
        "no obit heirs available; falling back to caller-supplied owner"
    )


# ── Sonnet reasoning ────────────────────────────────────────────────────


_REASONING_PROMPT = """\
You are writing the "Decision-Maker Identified" section of a real-estate \
prospecting report. The property is in pre-foreclosure or probate. The \
decision-maker is the person we should call.

Property + owner context:
- Property: {address}
- County: {county}
- Notice type: {notice_type}
- Title owner of record: {title_owner}
- Death signal: {death_signal} ({death_signal_reason})

Decedent (when applicable):
- Name: {decedent_name}
- Date of death: {dod}
- Last known city: {decedent_city}

Heirs (LIVING heirs are candidates for decision-maker):
{heir_block}

Selected decision-maker:
- Name: {dm_name}
- Relationship to decedent: {dm_relationship}
- Subject role: {subject_role}
- Status: {dm_status}
- Confidence: {confidence}
- Selection rationale (mechanical): {selection_why}

Write a 1-2 paragraph explanation for the report reader (a real-estate \
acquisitions person who will be making the call). Cover:
- WHY this person is the decision-maker (lineage + verification)
- What relationship they have to the property and to the decedent
- Anything they should know before dialing (e.g., the property is in \
their late mother's estate, this person is the named executor, etc.)

Be specific and concrete. Use the names provided. Don't editorialize \
about real-estate strategy — just explain the family + legal context. \
Plain prose, no bullets or markdown headings."""


def _render_heir_block(heirs: list[Heir]) -> str:
    if not heirs:
        return "  (no heirs on file)"
    lines = []
    for h in heirs:
        loc = f", {h.city}" if h.city else ""
        lines.append(
            f"  - {h.name} ({h.relationship}{loc}) — status={h.status}"
        )
    return "\n".join(lines)


def _write_reasoning_with_sonnet(
    lead: Lead,
    heir_map: HeirMap | None,
    chosen: Heir | None,
    subject_role: SubjectRole,
    confidence: Confidence,
    why: str,
    *,
    api_key: str,
) -> str | None:
    """Call Sonnet 4.6 for the reasoning paragraph. None on failure."""
    if not api_key:
        return None
    prompt = _REASONING_PROMPT.format(
        address=lead.input.address or "(unknown)",
        county=lead.input.county or "(unknown)",
        notice_type=lead.input.notice_type or "(unknown)",
        title_owner=lead.title_owner or "(unknown)",
        death_signal=lead.death_signal,
        death_signal_reason=lead.death_signal_reason or "n/a",
        decedent_name=(heir_map.decedent_name if heir_map else None) or "(unknown)",
        dod=(
            heir_map.decedent_dod.isoformat() if heir_map and heir_map.decedent_dod
            else (heir_map.decedent_dod_text if heir_map else None) or "(unknown)"
        ),
        decedent_city=(heir_map.decedent_city if heir_map else None) or "(unknown)",
        heir_block=_render_heir_block(heir_map.heirs if heir_map else []),
        dm_name=chosen.name if chosen else (lead.input.owner or "(unknown)"),
        dm_relationship=chosen.relationship if chosen else "(unknown)",
        subject_role=subject_role,
        dm_status=chosen.status if chosen else "UNVERIFIED",
        confidence=confidence,
        selection_why=why,
    )
    return sonnet_text(
        prompt,
        system=(
            "You write concise, factual context paragraphs for a real-estate "
            "prospecting report. No emojis, no markdown headings."
        ),
        max_tokens=400,
        api_key=api_key,
        model="claude-sonnet-4-5",
    )


def _deterministic_reasoning(
    lead: Lead,
    heir_map: HeirMap | None,
    chosen: Heir | None,
    why: str,
) -> str:
    """Fallback when Sonnet is unavailable. One short paragraph from
    the structured data we already have."""
    name = chosen.name if chosen else (lead.input.owner or "the owner of record")
    rel = chosen.relationship if chosen else "owner of record"
    decedent = heir_map.decedent_name if heir_map else None
    parts = [f"Recommended decision-maker is {name} ({rel})."]
    if decedent:
        parts.append(f"The property is associated with the estate of {decedent}.")
    parts.append(f"Selection rationale: {why}.")
    return " ".join(parts)


# ── Public entry point ──────────────────────────────────────────────────


def _build_backup_reasoning(
    ranked: _RankedDM, *, primary_name: str, decedent: str | None,
) -> str:
    """Deterministic 1-2 sentence backup-DM reasoning.

    Sonnet is reserved for the primary DM only — cost discipline keeps
    Phase 3 at one Sonnet call per pack regardless of DM count. Backups
    get a structured blurb so the report's Section 8 + the People &
    Star Markers block have meaningful prose for each contact.
    """
    parts = [
        f"Backup contact. Tier-{ranked.role_tier} "
        f"{ranked.heir.relationship} of the decedent" +
        (f" ({decedent})" if decedent else "") + "."
    ]
    parts.append(
        f"Primary contact is {primary_name}; route to {ranked.heir.name} "
        f"if {primary_name} is unreachable or declines."
    )
    if ranked.confidence != "HIGH":
        parts.append(f"Confidence: {ranked.confidence} ({ranked.why}).")
    return " ".join(parts)


async def run(
    lead: Lead,
    heir_map: HeirMap | None,
) -> tuple[list[DecisionMaker], list[SourceCheck], CostBreakdown]:
    """Pick the top-N decision makers from the LIVING heirs.

    Returns (list, checks, cost). The list always has ≥1 entry unless
    we have neither LIVING heirs nor a caller-supplied owner. Primary
    DM (index 0) gets the Sonnet reasoning paragraph; backups (index
    1+) get a deterministic blurb. Single-DM L1/L2 cases produce a
    one-element list with Sonnet on that single DM (the primary).
    """
    checks: list[SourceCheck] = []
    cost = CostBreakdown()

    caller_owner = (lead.input.owner or "").strip() or None

    # Slice 5b: title_owner-alive branch. Phase 2's reverse-address
    # probe set `lead.actual_subject_name` when the title_owner is
    # alive at the property. Build a 2-DM list directly — title_owner
    # SUBJECT, DataSift contact FAMILY_PIVOT — skipping the heir-based
    # ranking (no obit was found, no heirs to rank).
    if lead.actual_subject_name:
        return _build_title_owner_alive_dms(lead, checks, cost)

    ranked = pick_decision_makers(heir_map, caller_owner=caller_owner)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    decision_makers: list[DecisionMaker] = []

    if ranked:
        primary = ranked[0]
        # Sonnet on primary only.
        sonnet_reasoning = _write_reasoning_with_sonnet(
            lead, heir_map, primary.heir,
            primary.subject_role, primary.confidence, primary.why,
            api_key=api_key,
        )
        if sonnet_reasoning:
            primary_reasoning = sonnet_reasoning
            cost.anthropic += RATE_SONNET_PER_REASONING
            checks.append(SourceCheck(
                source="obit_search", status="HIT",
                notes=(
                    f"phase 3 sonnet reasoning for primary DM, "
                    f"{len(ranked)} DMs ranked (max_n cap honored)"
                ),
            ))
        else:
            primary_reasoning = _deterministic_reasoning(
                lead, heir_map, primary.heir, primary.why,
            )
            checks.append(SourceCheck(
                source="obit_search", status="EMPTY",
                notes=(
                    f"phase 3 deterministic reasoning fallback for primary, "
                    f"{len(ranked)} DMs ranked"
                ),
            ))

        decedent_name = heir_map.decedent_name if heir_map else None
        decision_makers.append(DecisionMaker(
            name=primary.heir.name,
            relationship=primary.heir.relationship,
            status="VERIFIED_LIVING",
            subject_role=primary.subject_role,
            contact=ContactInfo(),
            confidence=primary.confidence,
            reasoning=primary_reasoning.strip(),
        ))
        # Backups — deterministic reasoning, no Sonnet.
        for backup in ranked[1:]:
            decision_makers.append(DecisionMaker(
                name=backup.heir.name,
                relationship=backup.heir.relationship,
                status="VERIFIED_LIVING",
                subject_role=backup.subject_role,
                contact=ContactInfo(),
                confidence=backup.confidence,
                reasoning=_build_backup_reasoning(
                    backup,
                    primary_name=primary.heir.name,
                    decedent=decedent_name,
                ).strip(),
            ))
        _apply_executor_label_override(decision_makers, lead, heir_map)
        return decision_makers, checks, cost

    # No LIVING heirs — fall back to legacy single-DM logic (FAMILY_PIVOT
    # on UNVERIFIED caller match, or SUBJECT for L1 / no-obit cases).
    chosen, subject_role, confidence, why = _pick_dm(lead, heir_map)

    dm_name = (chosen.name if chosen else lead.input.owner) or "(unknown)"
    dm_relationship = (
        chosen.relationship if chosen
        else ("owner" if not heir_map or not heir_map.heirs else "(unknown)")
    )
    dm_status = (
        "VERIFIED_LIVING" if chosen and chosen.status == "LIVING" else "UNVERIFIED"
    )

    sonnet_reasoning = _write_reasoning_with_sonnet(
        lead, heir_map, chosen, subject_role, confidence, why, api_key=api_key,
    )
    if sonnet_reasoning:
        reasoning = sonnet_reasoning
        cost.anthropic += RATE_SONNET_PER_REASONING
        checks.append(SourceCheck(
            source="obit_search", status="HIT",
            notes=f"phase 3 sonnet (fallback path), subject_role={subject_role}",
        ))
    else:
        reasoning = _deterministic_reasoning(lead, heir_map, chosen, why)
        checks.append(SourceCheck(
            source="obit_search", status="EMPTY",
            notes=(
                f"phase 3 deterministic reasoning (fallback path), "
                f"subject_role={subject_role}"
            ),
        ))

    decision_makers.append(DecisionMaker(
        name=dm_name,
        relationship=dm_relationship,
        status=dm_status,
        subject_role=subject_role,
        contact=ContactInfo(),
        confidence=confidence,
        reasoning=reasoning.strip(),
    ))
    _apply_executor_label_override(decision_makers, lead, heir_map)
    return decision_makers, checks, cost


def _apply_executor_label_override(
    dms: list[DecisionMaker],
    lead: Lead,
    heir_map: HeirMap | None,
) -> None:
    """Slice 5b: when Phase 1 set named_contact_role='executor' AND Phase 2
    parsed a real DOD on the decedent, relabel the DataSift contact's DM
    as EXECUTOR. Without the DOD we're still in 'suspected probate'
    territory and SUBJECT is the safer label.

    Pure label correction — no routing or skip-trace impact.
    """
    if lead.named_contact_role != "executor":
        return
    if heir_map is None or heir_map.decedent_dod is None:
        return
    caller_key = _name_token_key_phase3(lead.input.owner or "")
    if not caller_key:
        return
    for dm in dms:
        if _name_token_key_phase3(dm.name) == caller_key:
            dm.subject_role = "EXECUTOR"
            return


def _name_token_key_phase3(name: str) -> frozenset[str]:
    """Token-key compare for caller-vs-DM matching. Mirrors Phase 1's
    `_name_token_key` semantics so cross-phase identity checks agree
    on '(LAST, FIRST)' vs 'First Last' variants."""
    if not name:
        return frozenset()
    cleaned = re.sub(r"[^a-zA-Z\s,]", " ", name).replace(",", " ")
    tokens = {t.lower() for t in cleaned.split() if len(t) > 1}
    return frozenset(tokens)


def _build_title_owner_alive_dms(
    lead: Lead,
    checks: list[SourceCheck],
    cost: CostBreakdown,
) -> tuple[list[DecisionMaker], list[SourceCheck], CostBreakdown]:
    """Slice 5b: build the 2-DM output for the title_owner-alive branch.

    Primary DM = title_owner verified alive (SUBJECT).
    Backup DM = original DataSift contact (FAMILY_PIVOT).

    Deterministic reasoning only — Sonnet would burn ~$0.01-0.02 to write
    prose for a routing case where the explanation is mechanical. The
    operator's report can render the Tracerfy alive-evidence directly.
    """
    dms: list[DecisionMaker] = []

    primary_name = (lead.actual_subject_name or "").strip()
    secondary_name = (lead.secondary_contact_name or "").strip()
    evidence = lead.title_owner_alive_evidence or {}
    age = evidence.get("age")

    age_clause = f", age {age}" if age else ""
    primary_reason = (
        f"Title owner '{primary_name}' verified alive at the property"
        f"{age_clause} via Tracerfy reverse-address probe. Phase 1 set "
        f"executor_swap_confirmed (Lists tag + name mismatch) but Phase 2 "
        f"obit search returned empty — title owner is alive, not deceased. "
        f"Phase Skiptrace will pull phones + emails from the property "
        f"address. Operator dials this person FIRST."
    )
    dms.append(DecisionMaker(
        name=primary_name,
        relationship="owner",
        status="VERIFIED_LIVING",
        subject_role="SUBJECT",
        contact=ContactInfo(),
        confidence="HIGH",
        reasoning=primary_reason,
    ))

    if secondary_name:
        dms.append(DecisionMaker(
            name=secondary_name,
            relationship="family",
            status="UNVERIFIED",
            subject_role="FAMILY_PIVOT",
            contact=ContactInfo(),
            confidence="MEDIUM",
            reasoning=(
                f"DataSift-supplied contact '{secondary_name}'. The "
                f"operator's upstream workflow flagged this row as "
                f"Probate/Inheritance/NOD, but the title owner "
                f"('{primary_name}') is alive — not deceased. Demoted to "
                f"family pivot. Skip-trace as a secondary contact in case "
                f"primary doesn't answer."
            ),
        ))

    checks.append(SourceCheck(
        source="tracerfy",
        status="HIT",
        notes=(
            f"phase 3 title_owner_alive branch: SUBJECT='{primary_name}', "
            f"FAMILY_PIVOT='{secondary_name or '(none)'}'"
        ),
    ))
    return dms, checks, cost
