"""Append a one-row-per-day summary to output/daily_summary.csv.

Every successful daily run calls append_daily_summary() at the end of the
pipeline. The resulting CSV is the multi-day trend dataset:

    date, total, probate, foreclosure, tax_sale, lis_pendens,
    franklin, montgomery, greene,
    deceased, with_heirs,
    tier_dial_now, tier_first, tier_second, tier_third, tier_fourth, tier_drop,
    entity_unresolved, parse_failures,
    duration_seconds, run_id

Open in Google Sheets / Excel for charts and week-over-week trend analysis.
Mike uses this in his Friday Weekly Review (see docs/SOP-WEEKLY-REVIEW.md).
"""

from __future__ import annotations

import csv
import logging
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from models import NoticeData

logger = logging.getLogger(__name__)


SUMMARY_COLUMNS = [
    # Identity
    "date",
    "run_id",
    "duration_seconds",
    # Volume
    "total_records",
    # By notice type
    "probate", "foreclosure", "tax_sale", "lis_pendens",
    "tax_delinquent", "eviction", "code_violation", "divorce",
    # By county
    "franklin", "montgomery", "greene", "other_counties",
    # Status
    "deceased", "with_heirs", "entity_owned", "entity_resolved",
    # Phone tier distribution (decision-makers)
    "tier_dial_now", "tier_dial_first", "tier_dial_second",
    "tier_dial_third", "tier_dial_fourth", "tier_drop",
    "no_phones",
    # Quality
    "with_property_address", "with_mailable_flag",
    "entity_unresolved", "parse_failures",
    # Cost
    "tracerfy_cost",
    "tracerfy_phones_found", "tracerfy_emails_found",
    # Errors
    "error_count",
]


def _classify_tier(tier_name: str) -> str:
    """Map Trestle tier name to summary column key."""
    return {
        "Dial Now": "tier_dial_now",
        "Dial First": "tier_dial_first",
        "Dial Second": "tier_dial_second",
        "Dial Third": "tier_dial_third",
        "Dial Fourth": "tier_dial_fourth",
        "Drop": "tier_drop",
    }.get(tier_name, "")


def build_summary(
    notices: list[NoticeData],
    run_id: str,
    duration_seconds: float,
    tracerfy_stats: dict | None = None,
    tiers_map: dict | None = None,
    error_count: int = 0,
) -> dict:
    """Compute the summary row for today's run."""
    by_type: Counter = Counter()
    by_county: Counter = Counter()
    by_tier: Counter = Counter()
    deceased_count = 0
    with_heirs_count = 0
    entity_owned_count = 0
    entity_resolved_count = 0
    with_address_count = 0
    with_mailable_count = 0
    entity_unresolved_count = 0
    parse_failure_count = 0
    no_phone_count = 0

    for n in notices:
        # Notice type
        if n.notice_type:
            by_type[n.notice_type] += 1

        # County
        county_lower = (n.county or "").strip().lower()
        if county_lower in {"franklin", "montgomery", "greene"}:
            by_county[county_lower] += 1
        elif county_lower:
            by_county["other"] += 1

        # Status
        if n.owner_deceased == "yes":
            deceased_count += 1
        if n.heir_map_json:
            with_heirs_count += 1
        if n.entity_type:
            entity_owned_count += 1
            if n.entity_person_name:
                entity_resolved_count += 1
            else:
                entity_unresolved_count += 1
        if n.address.strip():
            with_address_count += 1
        if (n.mailable or "").lower() == "yes":
            with_mailable_count += 1

        # Parse-failure proxy: living owner with no first/last after splitting
        if not n.entity_type and not n.owner_deceased and n.owner_name:
            parts = n.owner_name.strip().split()
            if len(parts) < 2:
                parse_failure_count += 1

        # Phones
        phones = [n.primary_phone, n.mobile_1, n.mobile_2, n.mobile_3,
                  n.mobile_4, n.mobile_5, n.landline_1, n.landline_2,
                  n.landline_3]
        if not any((p or "").strip() for p in phones):
            no_phone_count += 1

    # Phone tier distribution from tiers_map (phone -> tier_name)
    if tiers_map:
        for tier_name in tiers_map.values():
            by_tier[tier_name] += 1

    tracerfy = tracerfy_stats or {}

    return {
        "date": date.today().isoformat(),
        "run_id": run_id,
        "duration_seconds": round(duration_seconds, 1),
        "total_records": len(notices),
        "probate": by_type.get("probate", 0),
        "foreclosure": by_type.get("foreclosure", 0),
        "tax_sale": by_type.get("tax_sale", 0),
        "lis_pendens": by_type.get("lis_pendens", 0),
        "tax_delinquent": by_type.get("tax_delinquent", 0),
        "eviction": by_type.get("eviction", 0),
        "code_violation": by_type.get("code_violation", 0),
        "divorce": by_type.get("divorce", 0),
        "franklin": by_county.get("franklin", 0),
        "montgomery": by_county.get("montgomery", 0),
        "greene": by_county.get("greene", 0),
        "other_counties": by_county.get("other", 0),
        "deceased": deceased_count,
        "with_heirs": with_heirs_count,
        "entity_owned": entity_owned_count,
        "entity_resolved": entity_resolved_count,
        "tier_dial_now": by_tier.get("Dial Now", 0),
        "tier_dial_first": by_tier.get("Dial First", 0),
        "tier_dial_second": by_tier.get("Dial Second", 0),
        "tier_dial_third": by_tier.get("Dial Third", 0),
        "tier_dial_fourth": by_tier.get("Dial Fourth", 0),
        "tier_drop": by_tier.get("Drop", 0),
        "no_phones": no_phone_count,
        "with_property_address": with_address_count,
        "with_mailable_flag": with_mailable_count,
        "entity_unresolved": entity_unresolved_count,
        "parse_failures": parse_failure_count,
        "tracerfy_cost": round(tracerfy.get("cost", 0.0), 2),
        "tracerfy_phones_found": tracerfy.get("phones_found", 0),
        "tracerfy_emails_found": tracerfy.get("emails_found", 0),
        "error_count": error_count,
    }


def append_daily_summary(
    notices: list[NoticeData],
    run_id: str,
    duration_seconds: float,
    tracerfy_stats: dict | None = None,
    tiers_map: dict | None = None,
    error_count: int = 0,
    output_path: Path | None = None,
) -> Path:
    """Append one row to output/daily_summary.csv (creates with header on first run).

    Idempotent: if a row already exists for today's date + run_id, skips.
    """
    if output_path is None:
        from config import OUTPUT_DIR
        output_path = OUTPUT_DIR / "daily_summary.csv"

    summary = build_summary(
        notices, run_id, duration_seconds,
        tracerfy_stats=tracerfy_stats, tiers_map=tiers_map,
        error_count=error_count,
    )

    # Check existing rows for dedup on (date, run_id)
    existing_rows: list[dict] = []
    if output_path.exists():
        try:
            with open(output_path, encoding="utf-8") as f:
                existing_rows = list(csv.DictReader(f))
        except Exception as e:
            logger.warning("Could not read %s: %s — recreating", output_path, e)
            existing_rows = []

    for row in existing_rows:
        if row.get("date") == summary["date"] and row.get("run_id") == summary["run_id"]:
            logger.info("Daily summary already has entry for %s / %s — skipping", summary["date"], summary["run_id"])
            return output_path

    existing_rows.append(summary)

    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in existing_rows:
            writer.writerow(row)

    logger.info("Daily summary appended: %s (%d total rows)", output_path, len(existing_rows))
    return output_path
