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


async def run(
    lead: Lead,
    heir_map: HeirMap | None,
) -> tuple[DecisionMaker, list[SourceCheck], CostBreakdown]:
    checks: list[SourceCheck] = []
    cost = CostBreakdown()

    chosen, subject_role, confidence, why = _pick_dm(lead, heir_map)

    # Build the DM. Address fields are blank here — Phase skip-trace
    # fills them in. We're producing the *identification* of the DM.
    dm_name = (chosen.name if chosen else lead.input.owner) or "(unknown)"
    dm_relationship = (
        chosen.relationship if chosen
        else ("owner" if not heir_map or not heir_map.heirs else "(unknown)")
    )
    dm_status = (
        "VERIFIED_LIVING" if chosen and chosen.status == "LIVING" else "UNVERIFIED"
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    sonnet_reasoning = _write_reasoning_with_sonnet(
        lead, heir_map, chosen, subject_role, confidence, why, api_key=api_key,
    )
    if sonnet_reasoning:
        reasoning = sonnet_reasoning
        cost.anthropic += RATE_SONNET_PER_REASONING
        checks.append(SourceCheck(
            source="obit_search", status="HIT",
            notes=f"phase 3 sonnet reasoning, subject_role={subject_role}",
        ))
    else:
        reasoning = _deterministic_reasoning(lead, heir_map, chosen, why)
        checks.append(SourceCheck(
            source="obit_search", status="EMPTY",
            notes=(
                f"phase 3 fell back to deterministic reasoning "
                f"(subject_role={subject_role})"
            ),
        ))

    dm = DecisionMaker(
        name=dm_name,
        relationship=dm_relationship,
        status=dm_status,
        subject_role=subject_role,
        contact=ContactInfo(),
        confidence=confidence,
        reasoning=reasoning.strip(),
    )
    return dm, checks, cost
