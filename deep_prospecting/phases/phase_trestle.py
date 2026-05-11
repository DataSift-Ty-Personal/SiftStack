"""Phase Trestle — post-skip-trace phone scoring.

Runs after Phase Skiptrace. Loops every Phone in
`pack.skip_trace.phones`, calls `trestle_phone_intel.score(phone.number)`
for each, and folds the result back into the Phone model:

  - activity_score: always populated (new field, no override concern)
  - type:           UNKNOWN → Trestle's line_type mapping. Never overwrites
                    a Phone that was already classified by Tracerfy / CBC /
                    the operator (treated as authoritative).
  - carrier:        fill if blank, never overwrite
  - is_prepaid:     always populated (no override concern)

`Phone Status N` (CSV column) is intentionally untouched here — Trestle's
`is_valid` answers a different question than the operator's post-dial
outcome. Writer-side rules (Slice 1) already preserve operator-set
status on existing row phones.

The Phone Tags `Dial First/Second/Third` label is NOT appended here —
that's the writer's job at CSV emit time, derived from
`Phone.activity_score`. Phase Trestle's contract is "populate the
score; writer formats the label."

Cost: $0.015 per phone scored. The score-all policy (no skip on
operator-validated phones) keeps the model simple and gives the
operator a fresh activity_score on every dial cycle — point-in-time
validation 2 years ago doesn't mean live today.
"""

from __future__ import annotations

import logging

from deep_prospecting._utils import _safe_call
from deep_prospecting.models import (
    CostBreakdown,
    Phone,
    PhoneType,
    ResearchPack,
    SourceCheck,
)
from deep_prospecting.sources import trestle_phone_intel
from deep_prospecting.sources.trestle_phone_intel import TrestleIntelResult

logger = logging.getLogger(__name__)


# Trestle line_type → Phone.type (model Literal MOBILE/LANDLINE/VOIP/UNKNOWN).
# Premium / TollFree / Voicemail / Other collapse to UNKNOWN — they're
# rare in the residential skip-trace context and don't change the
# operator's dial strategy.
_LINE_TYPE_MAP: dict[str, PhoneType] = {
    "Mobile": "MOBILE",
    "Landline": "LANDLINE",
    "FixedVOIP": "VOIP",
    "NonFixedVOIP": "VOIP",
}


def _map_line_type(trestle_value: str | None) -> PhoneType | None:
    if not trestle_value:
        return None
    return _LINE_TYPE_MAP.get(trestle_value)


def _apply_intel(phone: Phone, intel: TrestleIntelResult) -> Phone:
    """Fold a Trestle Phone Intel response into one Phone (returns a copy).

    Conservative rules:
      - activity_score: always set (None → 0..100 from Trestle)
      - type:           upgrade from UNKNOWN only — preserves Tracerfy /
                        CBC / operator classification when present
      - carrier:        fill from blank only — preserves upstream carrier
      - is_prepaid:     always set
      - sources:        append "trestle" if it scored (HIT/EMPTY); dedupe
    """
    updates: dict = {}

    if intel.activity_score is not None:
        updates["activity_score"] = intel.activity_score

    if phone.type == "UNKNOWN":
        mapped = _map_line_type(intel.line_type)
        if mapped:
            updates["type"] = mapped

    if not phone.carrier and intel.carrier:
        updates["carrier"] = intel.carrier

    # is_prepaid intentionally not folded onto Phone — the model doesn't
    # carry it (Tracerfy's field set was dnc / carrier / is_litigator).
    # If the operator workflow ever needs prepaid signals, add the field
    # in a future slice; until then Phase Trestle's `intel.is_prepaid`
    # stays in the raw TrestleIntelResult for diagnostics.

    # NOTE: we intentionally do NOT add "trestle" to phone.sources here.
    # The `sources` list is for DISCOVERY provenance — "which site
    # surfaced this phone first." Trestle SCORES phones; it doesn't
    # discover them. Conflating the two would fire `Verified 2+ Sites`
    # on every Tracerfy-found-then-Trestle-scored phone, falsely claiming
    # independent cross-source confirmation. The `activity_score` field
    # is the load-bearing signal that Trestle ran.

    return phone.model_copy(update=updates) if updates else phone


# ── Public entry point ─────────────────────────────────────────────────


async def run(
    pack: ResearchPack,
) -> tuple[ResearchPack, list[SourceCheck], CostBreakdown]:
    """Score every phone in the pack's skip-trace result via Phone Intel.

    Returns (updated_pack, checks, cost_delta). The pack is returned
    with a freshly-rebuilt SkipTraceResult that has the same emails /
    associates / site_state but updated phones. Cost is the sum of all
    per-call charges (HITs + 200-but-empty; ERRORs and BLOCKED are free).
    """
    checks: list[SourceCheck] = []
    cost = CostBreakdown()

    if not pack.skip_trace or not pack.skip_trace.phones:
        return pack, checks, cost

    phones_in = list(pack.skip_trace.phones)
    updated: list[Phone] = []
    hits = 0
    errors = 0
    blocked = 0

    for ph in phones_in:
        # Trestle accepts E.164 / 10-digit / 1+10-digit. We canonicalize
        # to the 10-digit US form because most of our phones came in via
        # csv_value boundary and live as +1NNNNNNNNNN in E.164 form.
        intel = await _safe_call(
            lambda p=ph: trestle_phone_intel.score(p.csv_value),
            name=f"trestle.intel[{ph.csv_value}]",
        )
        if intel is None:
            errors += 1
            updated.append(ph)
            continue

        cost.trestle = round(cost.trestle + intel.cost_usd, 4)

        if intel.status == "HIT":
            hits += 1
        elif intel.status == "BLOCKED":
            blocked += 1
        elif intel.status == "ERROR":
            errors += 1

        updated.append(_apply_intel(ph, intel))

    # Record one summary SourceCheck so the report's checklist shows the
    # phase ran without producing N per-phone entries.
    note_parts = [f"{hits} HIT"]
    if errors:
        note_parts.append(f"{errors} ERROR")
    if blocked:
        note_parts.append(f"{blocked} BLOCKED")
    note_parts.append(f"${cost.trestle:.4f}")
    checks.append(SourceCheck(
        source="trestle",
        status="HIT" if hits else ("BLOCKED" if blocked else "ERROR"),
        notes=f"phone_intel: {', '.join(note_parts)} across {len(phones_in)} phones",
    ))

    updated_pack = pack.model_copy(update={
        "skip_trace": pack.skip_trace.model_copy(update={"phones": updated}),
    })
    return updated_pack, checks, cost


def derive_dial_rank_label(activity_score: int | None) -> str | None:
    """Bucket activity_score → dial-rank label. Public so the CSV writer
    can call the same function (single source of truth for the buckets)."""
    if activity_score is None:
        return None
    if activity_score >= 81:
        return "Dial First"
    if activity_score >= 41:
        return "Dial Second"
    if activity_score >= 1:
        return "Dial Third"
    return None  # score 0 → not dialable, no label appended
