"""End-to-end deep-prospecting orchestrator.

Runs P1 → P2 → P2.5 → P3 → SkipTrace and compiles a ResearchPack. Every
phase is wrapped in `_safe_call` so a single source crashing doesn't
abort the whole run — sparse output is preferred to no output.

Cost discipline:
  - COST_CEILING_USD ($0.18) hard-stops the run before the *next* phase
    if running it would push total past the ceiling. The phase that
    detected the ceiling stamps `aborted=True` + `abort_reason=
    "cost_ceiling"` + `aborted_at_phase` on the ResearchPack.
  - COST_TARGET_USD ($0.10) is a soft hint — orchestrator doesn't act on
    it directly, but downstream phase modules can read it to choose
    cheaper variants when there's budget pressure (e.g. Haiku vs Sonnet).

Time discipline:
  - Each phase has its own internal timeout. The orchestrator additionally
    bounds the whole run at MAX_TOTAL_SECONDS. If exceeded, future phases
    are skipped and `aborted_at_phase` records where we stopped.

Level selection:
  - L1: living owner, no death signal → Phase 1 only, skip 2/2.5/3
        (we still record the title for the report). DM = owner.
  - L2: death signal, but no obit-derived heirs → Phase 1 + Phase 3 with
        SUBJECT/FAMILY_PIVOT subject_role, no Phase 2.5.
  - L3: full waterfall — death signal AND obit produced heirs → all
        phases run.
  - L4: heir_map.escalation_needed → flag for human review.

Outputs:
  - `outputs/{date}/{slug}/research_pack.md` (via output.write)
  - `outputs/{date}/{slug}/results.json`     (via output.write)
  - Optional: DataSift CSV overlay via datasift_csv_writer.overlay
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from deep_prospecting import _utils, output
from deep_prospecting._utils import COST_CEILING_USD
from deep_prospecting.models import (
    CostBreakdown,
    ProspectInput,
    ResearchLevel,
    ResearchPack,
    SourceCheck,
)
from deep_prospecting.phases import (
    phase_1_title,
    phase_2_5_verification,
    phase_2_genealogy,
    phase_3_target,
    phase_skiptrace,
)

logger = logging.getLogger(__name__)


MAX_TOTAL_SECONDS = 180.0


def _add_costs(base: CostBreakdown, delta: CostBreakdown) -> CostBreakdown:
    """Sum two CostBreakdowns into a new one."""
    return CostBreakdown(
        anthropic=round(base.anthropic + delta.anthropic, 6),
        serper=round(base.serper + delta.serper, 6),
        firecrawl=round(base.firecrawl + delta.firecrawl, 6),
        smarty=round(base.smarty + delta.smarty, 6),
        other=round(base.other + delta.other, 6),
    )


def _select_level(
    *,
    death_signal: bool,
    heir_count: int,
    escalation_needed: bool,
) -> tuple[ResearchLevel, str]:
    if escalation_needed:
        return "L3", "L3 deceased + escalation: heirs exhausted, attorney consult"
    if not death_signal:
        return "L1", "no death signal — living owner, single-phase title pull"
    if death_signal and heir_count == 0:
        return "L2", "death signal but obit search returned no heirs — DM is fallback"
    return "L3", "death signal + obit heirs — full waterfall"


async def run(
    prospect: ProspectInput,
    *,
    skip_outputs: bool = False,
    output_base: Path | None = None,
    csv_overlay_path: Path | None = None,
    csv_overlay_out: Path | None = None,
) -> ResearchPack:
    """Run the full pipeline on `prospect`. Returns the compiled ResearchPack."""
    started_at = _utils.now_utc()
    started_mono = time.monotonic()
    aborted = False
    abort_reason = None
    aborted_at_phase: str | None = None

    all_checks: list[SourceCheck] = []
    cost = CostBreakdown()

    def _would_exceed_ceiling(next_phase_estimate: float = 0.02) -> bool:
        return cost.total + next_phase_estimate > COST_CEILING_USD

    def _budget_seconds_left() -> float:
        return MAX_TOTAL_SECONDS - (time.monotonic() - started_mono)

    # ── Phase 1 — Title ─────────────────────────────────────────────────
    p1_result = await _utils._safe_call(
        lambda: phase_1_title.run(prospect),
        name="phase_1_title",
    )
    if p1_result is None:
        logger.warning("Phase 1 returned None — running with sparse Lead")
        from deep_prospecting.models import Lead
        lead = Lead(input=prospect, warnings=["phase_1_returned_none"])
        p1_checks: list[SourceCheck] = []
    else:
        lead, p1_checks = p1_result
    all_checks.extend(p1_checks)

    heir_map = None
    decision_maker = None
    skip_trace = None

    if _would_exceed_ceiling() or _budget_seconds_left() <= 0:
        aborted, abort_reason, aborted_at_phase = (
            True, "cost_ceiling" if _would_exceed_ceiling() else "time_budget",
            "phase_2_genealogy",
        )

    # ── Phase 2 — Genealogy (only on death signal) ──────────────────────
    if not aborted and lead.death_signal:
        p2_result = await _utils._safe_call(
            lambda: phase_2_genealogy.run(lead),
            name="phase_2_genealogy",
        )
        if p2_result is not None:
            heir_map, p2_checks, p2_cost = p2_result
            all_checks.extend(p2_checks)
            cost = _add_costs(cost, p2_cost)

        # When death signal fired but obit search produced no heirs, mark
        # the run for the operator. Phase 3 still runs (with a sparse
        # HeirMap or None) and will fall back to caller-supplied owner.
        if heir_map is None or not heir_map.heirs:
            if "phase_2_no_obit_found" not in lead.warnings:
                lead = lead.model_copy(update={
                    "warnings": list(lead.warnings) + ["phase_2_no_obit_found"],
                })

        if _would_exceed_ceiling() or _budget_seconds_left() <= 0:
            aborted, abort_reason, aborted_at_phase = (
                True, "cost_ceiling" if _would_exceed_ceiling() else "time_budget",
                "phase_2_5_verification",
            )

    # ── Phase 2.5 — Verification ────────────────────────────────────────
    if not aborted and heir_map is not None and heir_map.heirs:
        p25_result = await _utils._safe_call(
            lambda: phase_2_5_verification.run(heir_map),
            name="phase_2_5_verification",
        )
        if p25_result is not None:
            heir_map, p25_checks, p25_cost = p25_result
            all_checks.extend(p25_checks)
            cost = _add_costs(cost, p25_cost)

        if _would_exceed_ceiling() or _budget_seconds_left() <= 0:
            aborted, abort_reason, aborted_at_phase = (
                True, "cost_ceiling" if _would_exceed_ceiling() else "time_budget",
                "phase_3_target",
            )

    # ── Phase 3 — DM selection ──────────────────────────────────────────
    if not aborted:
        p3_result = await _utils._safe_call(
            lambda: phase_3_target.run(lead, heir_map),
            name="phase_3_target",
        )
        if p3_result is not None:
            decision_maker, p3_checks, p3_cost = p3_result
            all_checks.extend(p3_checks)
            cost = _add_costs(cost, p3_cost)

        if _would_exceed_ceiling() or _budget_seconds_left() <= 0:
            aborted, abort_reason, aborted_at_phase = (
                True, "cost_ceiling" if _would_exceed_ceiling() else "time_budget",
                "phase_skiptrace",
            )

    # ── Phase Skip Trace ────────────────────────────────────────────────
    if not aborted and decision_maker is not None:
        st_result = await _utils._safe_call(
            lambda: phase_skiptrace.run(decision_maker, lead, heir_map),
            name="phase_skiptrace",
        )
        if st_result is not None:
            skip_trace, st_checks, st_cost = st_result
            all_checks.extend(st_checks)
            cost = _add_costs(cost, st_cost)
            # Phase skip-trace returns an updated DM with contact info
            # filled in — replace.
            decision_maker = skip_trace.decision_maker

    # ── Compile + level selection ───────────────────────────────────────
    heir_count = len(heir_map.heirs) if heir_map else 0
    escalation = bool(heir_map and heir_map.escalation_needed)
    level, level_reason = _select_level(
        death_signal=lead.death_signal,
        heir_count=heir_count,
        escalation_needed=escalation,
    )

    duration = (time.monotonic() - started_mono)

    pack = ResearchPack(
        input=prospect,
        level_selected=level,
        level_reason=level_reason,
        source_checklist=all_checks,
        lead=lead,
        heir_map=heir_map,
        decision_maker=decision_maker,
        skip_trace=skip_trace,
        cost=cost,
        timestamp_utc=started_at,
        duration_seconds=round(duration, 2),
        aborted=aborted,
        abort_reason=abort_reason,
        aborted_at_phase=aborted_at_phase,
    )

    # ── Outputs ─────────────────────────────────────────────────────────
    if not skip_outputs:
        slug = _utils.slug(
            f"{prospect.owner or ''} {prospect.address or ''}",
        )
        out_dir = _utils.output_dir_for_run(slug, base=output_base)
        try:
            output.write(pack, out_dir)
        except Exception as e:
            logger.warning("output.write failed: %s", e)
        if csv_overlay_path and csv_overlay_out:
            try:
                from deep_prospecting import datasift_csv_writer
                datasift_csv_writer.overlay(csv_overlay_path, csv_overlay_out, [pack])
            except Exception as e:
                logger.warning("datasift_csv_writer.overlay failed: %s", e)

    return pack
