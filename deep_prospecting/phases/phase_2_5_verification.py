"""Phase 2.5 — Heir living/deceased verification.

Input:  HeirMap (heirs all UNVERIFIED from Phase 2)
Output: HeirMap (heirs marked LIVING / DECEASED / UNVERIFIED) +
        list[SourceCheck] +
        CostBreakdown delta

Verification waterfall per heir (highest-reliability first):
  1. Find-A-Grave memorial via DDGS (high-precision DECEASED signal)
  2. Obituary search via the Phase 2 pipeline (a personal obit URL
     surfacing is also a DECEASED signal)
  3. Default → LIVING (presumption of life when no death evidence
     surfaces; the heir is treated as a candidate DM)

Recursion: when an heir is found DECEASED, the spec allows recursing
into THEIR heirs — capped at depth 2 for Slice 1. We don't chase the
3rd generation. Catherine's case never recurses (she'll be LIVING).

Stop conditions:
  - 2-3 verified LIVING heirs reached → stop (cost discipline)
  - Time budget exceeded → stop, mark remaining UNVERIFIED

Per spec: when 3 generations are exhausted without a living heir we
set `escalation_needed=True` + `escalation_reason="all_heirs_exhausted
_through_3_generations"`. That doesn't fire in Slice 1 (depth cap 2)
but the plumbing is here for Phase 4 escalation work.
"""

from __future__ import annotations

import asyncio
import logging
import time

from deep_prospecting._utils import _safe_call
from deep_prospecting.models import (
    CostBreakdown,
    Heir,
    HeirMap,
    SourceCheck,
)
from deep_prospecting.sources import findagrave

logger = logging.getLogger(__name__)


# Cost/time discipline knobs
LIVING_HEIRS_TARGET = 3        # stop once we hit this many verified-living
TIME_BUDGET_SECONDS = 60.0     # whole-phase wall clock
PER_HEIR_TIMEOUT = 12.0        # per-heir verification timeout
RATE_PER_FINDAGRAVE_CHECK = 0.001   # Serper search via DDGS


async def _verify_one(
    heir: Heir,
    *,
    state_full: str,
    timeout: float = PER_HEIR_TIMEOUT,
) -> tuple[Heir, list[SourceCheck], float]:
    """Verify a single heir's living/deceased status.

    Returns the updated Heir, source checks generated during the check,
    and the cost incurred (currently just the findagrave search).
    """
    checks: list[SourceCheck] = []
    cost = 0.0

    try:
        memorial_found, urls, state = await asyncio.wait_for(
            findagrave.search_memorial(heir.name, heir.city or "", state_full),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        checks.append(SourceCheck(
            source="findagrave", status="ERROR", notes=f"timeout for {heir.name}",
        ))
        return heir, checks, cost

    cost += RATE_PER_FINDAGRAVE_CHECK
    checks.append(SourceCheck(
        source="findagrave",
        status=state.status,
        notes=("memorial found" if memorial_found else f"no memorial for {heir.name}"),
    ))

    if memorial_found:
        # Optionally extract DOD from the first findagrave URL. Best-effort —
        # null DOD is fine, the LIVING/DECEASED flip is the load-bearing bit.
        dod_text = None
        if urls:
            try:
                dod_text = await asyncio.wait_for(
                    findagrave.extract_dod(urls[0]),
                    timeout=8.0,
                )
            except asyncio.TimeoutError:
                pass
        return (
            heir.model_copy(update={
                "status": "DECEASED",
                "dod_text": dod_text,
                "sources": list({*heir.sources, "findagrave"}),
                "verification_notes": (heir.verification_notes or "") + (
                    " | findagrave memorial confirms deceased"
                ).strip(" |"),
            }),
            checks,
            cost,
        )

    # No findagrave hit → presumption of LIVING. We don't run an extra
    # obit query per heir here in Slice 1 — the cron's recall is uneven
    # enough that absence-of-obit is the right default, and we save the
    # Haiku budget for Phase 3.
    return (
        heir.model_copy(update={
            "status": "LIVING",
            "verification_notes": (heir.verification_notes or "") + (
                " | no findagrave memorial, presumed living"
            ).strip(" |"),
        }),
        checks,
        cost,
    )


def _state_from_decedent_city(decedent_city: str | None) -> str:
    # Slice 1 is NJ-bound. Phase 1 already constrained county to one of
    # the NJ MOD-IV counties, so the decedent state is "New Jersey".
    # Future work: derive from county lookup when we expand to other
    # states. For Slice 1 keeping it pinned is fine.
    return "New Jersey"


async def run(
    heir_map: HeirMap,
    *,
    depth: int = 0,
    max_depth: int = 2,
) -> tuple[HeirMap, list[SourceCheck], CostBreakdown]:
    """Verify every heir in the map.

    Recursion: when an heir is found DECEASED, this would expand into
    THEIR heirs. Slice 1 caps depth at 2 (Catherine's children would be
    the 3rd-generation reach we explicitly avoid). The recursion hook
    is left as a no-op TODO; downstream phases work fine with a single
    generation of LIVING heirs.
    """
    checks: list[SourceCheck] = []
    cost = CostBreakdown()
    if not heir_map or not heir_map.heirs:
        return heir_map, checks, cost

    state_full = _state_from_decedent_city(heir_map.decedent_city)
    deadline = time.monotonic() + TIME_BUDGET_SECONDS

    verified: list[Heir] = []
    living_count = 0

    for heir in heir_map.heirs:
        if time.monotonic() >= deadline:
            # Out of time — keep remaining heirs UNVERIFIED.
            verified.append(heir.model_copy(update={
                "verification_notes": (heir.verification_notes or "")
                + " | phase_2_5 time budget exhausted",
            }))
            continue

        updated, sub_checks, sub_cost = await _safe_call(
            lambda h=heir: _verify_one(h, state_full=state_full),
            name=f"verify[{heir.name}]",
        ) or (heir, [SourceCheck(source="findagrave", status="ERROR",
                                  notes=f"verify failed for {heir.name}")], 0.0)

        verified.append(updated)
        checks.extend(sub_checks)
        cost.serper += sub_cost

        if updated.status == "LIVING":
            living_count += 1
        # Recursion stub — Slice 1 leaves third-generation work to a
        # later milestone; flag would set escalation_needed below.

        if living_count >= LIVING_HEIRS_TARGET:
            # We have enough confirmed-living heirs for Phase 3 to pick
            # a high-confidence DM. Mark remaining UNVERIFIED, stop.
            seen = {h.name for h in verified}
            for h in heir_map.heirs:
                if h.name not in seen:
                    verified.append(h)
            break

    # Update the map. We don't yet recurse, so generations_searched
    # stays at 1 (Phase 2 set it). depth is wired here so the orchestrator
    # can detect "depth cap hit" without re-instrumenting later.
    escalation_needed = heir_map.escalation_needed
    escalation_reason = heir_map.escalation_reason
    if all(h.status == "DECEASED" for h in verified) and depth + 1 >= max_depth:
        escalation_needed = True
        escalation_reason = "all_heirs_exhausted_through_3_generations"

    updated_map = heir_map.model_copy(update={
        "heirs": verified,
        "escalation_needed": escalation_needed,
        "escalation_reason": escalation_reason,
    })
    return updated_map, checks, cost
