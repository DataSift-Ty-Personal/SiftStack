"""Batch runner — apply the deep_prospecting orchestrator to every row
of a DataSift CSV export.

Reads the input CSV, extracts (owner, address, county, notice_type) per
row, runs the orchestrator on each, and accumulates overlays into a
single output CSV. Per-record markdown packs + JSON go to
`outputs/{date}/{slug}/`. The combined overlay lives at
`outputs/{date}/sample_export_overlay.csv`.

Run shape (Slice 1):

    python -m deep_prospecting.run_batch <input.csv>

Failure handling: each row is wrapped in try/except — one bad row never
aborts the batch. Failed rows emit the original record unchanged + a
"Deep Prospecting Failed" tag for triage.

Notice-type guessing: input CSVs don't always have a `notice_type`
column. We infer from row tags or fall back to "foreclosure". Phase 1
doesn't use notice_type strongly; it's mostly metadata that flows
through to the markdown report.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from deep_prospecting import datasift_csv_writer, orchestrator
from deep_prospecting.models import (
    County,
    NoticeType,
    ProspectInput,
    ResearchPack,
)

logger = logging.getLogger(__name__)


# ── County / notice-type extraction ─────────────────────────────────────

_NOTICE_TYPE_KEYWORDS: list[tuple[str, NoticeType]] = [
    ("probate", "probate"),
    ("foreclosure", "foreclosure"),
    ("tax sale", "tax_sale"),
    ("tax delinquent", "tax_delinquent"),
    ("eviction", "eviction"),
    ("code violation", "code_violation"),
    ("divorce", "divorce"),
]

_VALID_COUNTIES: set[str] = {"Essex", "Middlesex", "Somerset", "Union"}


def _normalize_county(raw: str) -> County | None:
    if not raw:
        return None
    cleaned = raw.strip()
    for c in _VALID_COUNTIES:
        if cleaned.lower() == c.lower():
            return c  # type: ignore[return-value]
    return None


def _guess_notice_type(row: dict) -> NoticeType:
    """Best-effort notice_type from Lists / Tags columns.

    DataSift's Lists column carries the notice category as the primary
    signal. Tags can name it too. Default foreclosure when nothing
    matches — Phase 1 doesn't gate on the value.
    """
    haystack = " ".join([
        (row.get("Lists") or ""),
        (row.get("Tags") or ""),
    ]).lower()
    for keyword, ntype in _NOTICE_TYPE_KEYWORDS:
        if keyword in haystack:
            return ntype
    return "foreclosure"


def _row_to_prospect(row: dict) -> ProspectInput | None:
    """Convert a DataSift CSV row → ProspectInput. None if the row lacks
    enough address signal to drive Phase 1."""
    street = (row.get("Property address") or "").strip()
    city = (row.get("Property city") or "").strip()
    state = (row.get("Property state") or "").strip()
    zp = (row.get("Property zip") or "").strip()
    if not street:
        return None
    parts = [street]
    if city:
        parts.append(city)
    if state:
        parts.append(f"{state} {zp}".strip() if zp else state)
    address = ", ".join(parts)

    first = (row.get("First Name") or "").strip()
    last = (row.get("Last Name") or "").strip()
    owner = f"{first} {last}".strip() or None

    county = _normalize_county(row.get("Property county") or "")
    notice_type = _guess_notice_type(row)

    return ProspectInput(
        address=address,
        owner=owner,
        county=county,
        notice_type=notice_type,
    )


# ── Outcome computed against per-pack overlay results ──────────────────


def _summary_outcome(
    pack: ResearchPack, *, phones_added_after_dedup: int,
) -> str:
    """One-word run outcome for the per-row summary table.

    Matches the CSV-row tag semantics: NUMBERS_ADDED only when at least
    one new phone slot was filled (post-dedup). Pre-dedup `pack.skip_trace
    .phones` can be non-empty when CBC returns duplicates of phones the
    row already had — that's NO_NUMBERS for the operator's purposes.
    """
    if pack.aborted:
        return f"ABORTED({pack.abort_reason})"
    if pack.heir_map and pack.heir_map.escalation_needed:
        return "ESCALATE"
    return "NUMBERS_ADDED" if phones_added_after_dedup > 0 else "NO_NUMBERS"


# ── Batch driver ────────────────────────────────────────────────────────


async def run_batch(input_csv: Path, output_root: Path | None = None) -> dict:
    """Run the orchestrator on every row of `input_csv`.

    Returns a dict with per-row results + paths to the combined output
    CSV and per-record markdown directories.
    """
    rows: list[dict]
    with input_csv.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    logger.info("Batch run: %d rows from %s", len(rows), input_csv)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_root = (
        output_root
        if output_root is not None
        else Path(__file__).resolve().parent / "outputs" / today
    )
    output_root.mkdir(parents=True, exist_ok=True)

    overlay_csv = output_root / "sample_export_overlay.csv"
    # Start from a fresh copy of the input — datasift_csv_writer.overlay
    # mutates in place per-pack.
    import shutil
    shutil.copyfile(input_csv, overlay_csv)

    packs: list[ResearchPack] = []
    summary_rows: list[dict] = []
    started_total = time.monotonic()

    for idx, raw in enumerate(rows, 1):
        first = (raw.get("First Name") or "").strip()
        last = (raw.get("Last Name") or "").strip()
        label = f"{first} {last}".strip() or f"row#{idx}"
        prospect = _row_to_prospect(raw)
        if prospect is None:
            summary_rows.append({
                "row": idx,
                "owner": label,
                "outcome": "SKIPPED",
                "reason": "no Property address",
                "duration_s": 0.0,
                "cost_usd": 0.0,
                "phones_added": 0,
            })
            continue

        t0 = time.monotonic()
        try:
            pack = await orchestrator.run(
                prospect,
                output_base=output_root.parent,  # outputs/{date}/{slug}/...
                skip_outputs=False,
                csv_overlay_path=None,  # batch overlays after loop
                csv_overlay_out=None,
            )
            elapsed = time.monotonic() - t0
            packs.append(pack)
            summary_rows.append({
                "row": idx,
                "owner": label,
                "address": prospect.address,
                "level": pack.level_selected,
                "death_signal": pack.lead.death_signal,
                "death_reason": pack.lead.death_signal_reason or "-",
                "p2_warning": (
                    "phase_2_no_obit_found"
                    if "phase_2_no_obit_found" in pack.lead.warnings
                    else "-"
                ),
                "heirs": (len(pack.heir_map.heirs) if pack.heir_map else 0),
                "dm_name": pack.primary_dm.name if pack.primary_dm else "-",
                "dm_role": (
                    pack.primary_dm.subject_role if pack.primary_dm else "-"
                ),
                "dm_status": (
                    pack.primary_dm.status if pack.primary_dm else "-"
                ),
                "dm_conf": (
                    pack.primary_dm.confidence if pack.primary_dm else "-"
                ),
                "phones_found": (
                    len(pack.skip_trace.phones) if pack.skip_trace else 0
                ),
                "emails_found": (
                    len(pack.skip_trace.emails) if pack.skip_trace else 0
                ),
                # Post-overlay delta gets filled in once the overlay step
                # runs (we don't know phones_added until then).
                "phones_added": 0,
                "outcome": "(pending overlay)",
                "duration_s": round(elapsed, 2),
                "cost_usd": round(pack.cost.total, 4),
                "aborted": pack.aborted,
            })
        except Exception as e:
            logger.exception("Row %d (%s) failed: %s", idx, label, e)
            summary_rows.append({
                "row": idx,
                "owner": label,
                "outcome": "ERROR",
                "reason": str(e)[:80],
                "duration_s": round(time.monotonic() - t0, 2),
                "cost_usd": 0.0,
                "phones_added": 0,
            })

    # Per-pack overlay tracking: call overlay once per pack so we can
    # capture the post-dedup phones_added count for each row's outcome
    # column. The CSV ends up identical to the single-pass call — overlay
    # accumulates state into the same file across invocations.
    total_matched = 0
    total_unmatched = 0
    total_phones_added = 0
    total_truncated = 0
    pack_idx = 0
    for s in summary_rows:
        if s.get("outcome") != "(pending overlay)":
            continue
        pack = packs[pack_idx]
        pack_idx += 1
        single = datasift_csv_writer.overlay(overlay_csv, overlay_csv, [pack])
        total_matched += single["matched"]
        total_unmatched += single["unmatched"]
        total_phones_added += single["phones_added"]
        total_truncated += single["truncated_rows"]
        s["phones_added"] = single["phones_added"]
        s["outcome"] = _summary_outcome(
            pack, phones_added_after_dedup=single["phones_added"],
        )

    overlay_result = {
        "matched": total_matched,
        "unmatched": total_unmatched,
        "phones_added": total_phones_added,
        "truncated_rows": total_truncated,
        "out_csv": str(overlay_csv),
    }

    total_duration = round(time.monotonic() - started_total, 2)
    total_cost = round(sum(s.get("cost_usd", 0.0) for s in summary_rows), 4)

    return {
        "rows_processed": len(summary_rows),
        "packs_compiled": len(packs),
        "overlay_csv": str(overlay_csv),
        "overlay_result": overlay_result,
        "summary": summary_rows,
        "total_duration_s": total_duration,
        "total_cost_usd": total_cost,
        "output_root": str(output_root),
    }


def _print_summary_table(result: dict) -> None:
    print()
    print("=" * 120)
    print("BATCH RUN SUMMARY")
    print("=" * 120)
    header = (
        f"{'row':3} {'owner':24} {'level':5} {'ds':5} {'p2warn':22} "
        f"{'heirs':5} {'DM':22} {'role':10} {'stat':16} {'conf':6} "
        f"{'fnd':3} {'add':3} {'em':3} {'outcome':14} {'dur':6} {'cost':7}"
    )
    print(header)
    print("-" * 120)
    for r in result["summary"]:
        if r["outcome"] in ("SKIPPED", "ERROR"):
            print(
                f"{r['row']:>3} {r.get('owner','-'):24.24} "
                f"{r['outcome']:<14} reason={r.get('reason','-')}"
            )
            continue
        print(
            f"{r['row']:>3} {r['owner']:24.24} "
            f"{r.get('level','-'):5} "
            f"{('T' if r.get('death_signal') else 'F'):5} "
            f"{r.get('p2_warning','-'):22.22} "
            f"{r.get('heirs',0):>5} "
            f"{r.get('dm_name','-'):22.22} "
            f"{r.get('dm_role','-'):10.10} "
            f"{r.get('dm_status','-'):16.16} "
            f"{r.get('dm_conf','-'):6} "
            f"{r.get('phones_found',0):>3} "
            f"{r.get('phones_added',0):>3} "
            f"{r.get('emails_found',0):>3} "
            f"{r['outcome']:14} "
            f"{r['duration_s']:>5.1f}s "
            f"${r['cost_usd']:>6.4f}"
        )
    print("-" * 120)
    print(
        f"TOTAL: {result['rows_processed']} rows, "
        f"{result['packs_compiled']} packs compiled, "
        f"matched={result['overlay_result']['matched']}, "
        f"unmatched={result['overlay_result']['unmatched']}, "
        f"phones_added={result['overlay_result']['phones_added']}, "
        f"duration={result['total_duration_s']}s, "
        f"cost=${result['total_cost_usd']:.4f}"
    )
    print(f"Overlay CSV: {result['overlay_csv']}")
    print(f"Pack dirs:   {result['output_root']}/<slug>/")


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch deep_prospecting runner")
    parser.add_argument("input_csv", type=Path, help="DataSift CSV export to process")
    parser.add_argument(
        "--output-root", type=Path, default=None,
        help="Override outputs/{date}/ root (default: deep_prospecting/outputs/{utc-date})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    result = asyncio.run(run_batch(args.input_csv, args.output_root))
    _print_summary_table(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
