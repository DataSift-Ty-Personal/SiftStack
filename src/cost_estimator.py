"""API cost estimation for SiftStack pipeline runs.

Single source of truth for per-API rates and per-run cost calculation.
Used by:
  - scripts/estimate_run_cost.py (CLI inspection of past runs)
  - modal_app.nj_weekly_all (live cost estimate posted to Slack)

Rates are midpoint estimates — adjust if a provider's billing differs.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

# ── Per-API rate estimates (USD) ──────────────────────────────────────

RATE_SMARTY_PER_RECORD = 0.003
RATE_ZILLOW_PER_RECORD = 0.001
RATE_SERPER_PER_SEARCH = 0.001
RATE_FIRECRAWL_PER_PAGE = 0.010
RATE_ANTHROPIC_PER_PARSE = 0.020

# Average pages fetched per obit candidate (cache + retry adjustment)
AVG_FIRECRAWL_PAGES_PER_RECORD = 1.4

# Business-entity heuristic — these get filtered before obit search runs,
# so they don't incur Serper/Firecrawl/Anthropic costs.
BUSINESS_PATTERNS = (
    " LLC", " L.L.C", " CORP", " INC", " TRUST", " LP", " LLP",
    " LTD", " HOLDINGS", " PARTNERS", " ASSOCIATION",
)


def is_business(owner_name: str) -> bool:
    if not owner_name:
        return False
    upper = owner_name.upper()
    return any(p in upper for p in BUSINESS_PATTERNS)


def _row_owner_name(r: dict) -> str:
    """Pull owner from whichever column the CSV uses.

    write_csv (raw NoticeData) uses `full_name`; DataSift-formatted CSVs
    split into `Owner First Name` + `Owner Last Name`. Support both.
    """
    name = r.get("full_name") or r.get("name") or ""
    if not name:
        first = r.get("Owner First Name", "")
        last = r.get("Owner Last Name", "")
        name = f"{first} {last}".strip()
    return name


def tally_csv(path: Path) -> dict:
    """Count records and enrichment hits in one CSV file."""
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    if not rows:
        return {"path": Path(path), "rows": 0, "non_business": 0,
                "smarty_hits": 0, "zillow_hits": 0, "deceased": 0}

    total = len(rows)
    business = sum(1 for r in rows if is_business(_row_owner_name(r)))

    smarty_hits = sum(
        1 for r in rows
        if (r.get("dpv_match_code") or r.get("zip_plus4") or "").strip()
    )
    zillow_hits = sum(
        1 for r in rows
        if (r.get("estimated_value") or r.get("mls_status") or
            r.get("Estimated Value") or r.get("MSL Status") or "").strip()
    )
    deceased = sum(
        1 for r in rows
        if (r.get("owner_deceased") or r.get("Owner Deceased") or "").strip().lower() == "yes"
    )

    return {
        "path": Path(path),
        "rows": total,
        "business": business,
        "non_business": total - business,
        "smarty_hits": smarty_hits,
        "zillow_hits": zillow_hits,
        "deceased": deceased,
    }


def tally_notices(notices: list) -> dict:
    """Same shape as tally_csv() but reads from NoticeData objects directly.

    Avoids re-reading a CSV when modal_app.py already has the live records.
    """
    if not notices:
        return {"path": None, "rows": 0, "non_business": 0,
                "smarty_hits": 0, "zillow_hits": 0, "deceased": 0}

    total = len(notices)
    business = sum(1 for n in notices if is_business(n.owner_name or ""))
    smarty_hits = sum(
        1 for n in notices
        if (n.dpv_match_code or n.zip_plus4)
    )
    zillow_hits = sum(
        1 for n in notices
        if (n.estimated_value or n.mls_status)
    )
    deceased = sum(1 for n in notices if (n.owner_deceased or "").lower() == "yes")

    return {
        "path": None,
        "rows": total,
        "business": business,
        "non_business": total - business,
        "smarty_hits": smarty_hits,
        "zillow_hits": zillow_hits,
        "deceased": deceased,
    }


def estimate_costs(t: dict) -> dict:
    """Apply per-API rates to tallied counts. Returns {api: dollars}."""
    rows = t.get("rows", 0)
    obit_eligible = t.get("non_business", 0)
    return {
        "Smarty": rows * RATE_SMARTY_PER_RECORD,
        "Zillow": rows * RATE_ZILLOW_PER_RECORD,
        "Serper": obit_eligible * RATE_SERPER_PER_SEARCH,
        "Firecrawl": obit_eligible * AVG_FIRECRAWL_PAGES_PER_RECORD * RATE_FIRECRAWL_PER_PAGE,
        "Anthropic": obit_eligible * RATE_ANTHROPIC_PER_PARSE,
    }


def total_cost(t: dict) -> float:
    return sum(estimate_costs(t).values())


def slack_summary_line(t: dict) -> str:
    """One-line Slack summary: total + per-API breakdown.

    Matches the existing Slack message style — bold total, italicized
    breakdown, dollar figures with two decimals.
    """
    if t.get("rows", 0) == 0:
        return ""
    costs = estimate_costs(t)
    parts = [f"{api} ${amt:.2f}" for api, amt in costs.items()]
    return f"*Cost:* ${sum(costs.values()):.2f}  _({' · '.join(parts)})_"


def fmt_money(n: float) -> str:
    return f"${n:,.2f}"


def fmt_pct(num: int, denom: int) -> str:
    if denom == 0:
        return "—"
    return f"{(num / denom) * 100:.0f}%"


def render_report(tallies: list[dict]) -> str:
    """Multi-CSV report with grand total — used by the CLI script."""
    if not tallies:
        return "No CSVs found."
    lines = ["=" * 78, "SIFTSTACK RUN COST ESTIMATE", "=" * 78]
    grand = defaultdict(int)
    grand_costs = defaultdict(float)

    for t in tallies:
        if t.get("rows", 0) == 0:
            continue
        costs = estimate_costs(t)
        rows = t["rows"]
        path_label = t["path"].name if t.get("path") else "(in-memory)"
        lines.append("")
        lines.append(f"📄 {path_label}")
        lines.append(f"   Records:           {rows}")
        lines.append(f"   Non-business:      {t['non_business']} (obit-eligible)")
        lines.append(f"   Smarty enriched:   {t['smarty_hits']:>4}  ({fmt_pct(t['smarty_hits'], rows)})")
        lines.append(f"   Zillow enriched:   {t['zillow_hits']:>4}  ({fmt_pct(t['zillow_hits'], rows)})")
        lines.append(f"   Deceased matches:  {t['deceased']:>4}  ({fmt_pct(t['deceased'], t['non_business'] or 1)})")
        lines.append("")
        lines.append("   Estimated cost:")
        for api, amt in costs.items():
            lines.append(f"      {api:<12s} {fmt_money(amt)}")
        lines.append(f"      {'─' * 25}")
        lines.append(f"      {'Total':<12s} {fmt_money(sum(costs.values()))}")

        for k in ("rows", "non_business", "smarty_hits", "zillow_hits", "deceased"):
            grand[k] += t[k]
        for api, amt in costs.items():
            grand_costs[api] += amt

    if len(tallies) > 1:
        lines.extend(["", "=" * 78, "GRAND TOTAL", "=" * 78])
        lines.append(f"   Records:           {grand['rows']}")
        lines.append(f"   Non-business:      {grand['non_business']}")
        lines.append(f"   Smarty enriched:   {grand['smarty_hits']:>4}  ({fmt_pct(grand['smarty_hits'], grand['rows'])})")
        lines.append(f"   Zillow enriched:   {grand['zillow_hits']:>4}  ({fmt_pct(grand['zillow_hits'], grand['rows'])})")
        lines.append(f"   Deceased matches:  {grand['deceased']:>4}  ({fmt_pct(grand['deceased'], grand['non_business'] or 1)})")
        lines.append("")
        lines.append("   Estimated cost:")
        for api, amt in grand_costs.items():
            lines.append(f"      {api:<12s} {fmt_money(amt)}")
        total = sum(grand_costs.values())
        lines.append(f"      {'─' * 25}")
        lines.append(f"      {'Total':<12s} {fmt_money(total)}")
        if grand['rows']:
            lines.append("")
            lines.append(f"   ≈ {fmt_money(total / grand['rows'])} per record")

    lines.extend([
        "",
        "Notes:",
        "  • Rates are estimated midpoints — actual provider bills vary.",
        "  • Smarty + Zillow fire on every record. Serper/Firecrawl/Anthropic",
        "    fire only on non-business records (obit search candidates).",
        "  • Modal compute (~$0.05-0.10/hr container time) not included.",
        "=" * 78,
    ])
    return "\n".join(lines)
