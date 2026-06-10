"""Niche cohort gating for probate leads.

Rick's "niche" cohort is the highest-value slice of the probate list: the
heir is out of state (so they can't easily manage the property), the
property is a single family home (cleanest to wholesale), and there's real
equity to work with. A record has to clear ALL THREE gates to earn the tag.

This is a read-only tagging layer — it never drops records. It runs AFTER
the enrichment pipeline (which fills equity_percent + property_type) and
BEFORE the output CSV is written, stamping NoticeData.niche with a
"Niche Week NN YYYY" label so the tag matches the format Rick's county
prepper already uses.

Field mapping note: the pipeline doesn't carry a literal `classification`
or `mailing_state` attribute. S/P/N lives in `notice_type`
(probate_intake._CLASSIFICATION_MAP maps P->"probate",
S->"probate_same_address", N->"probate_no_property"), and the heir's
mailing state is `owner_state` (the PR/contact mailing state). The "P"
gate therefore means notice_type == "probate" — which excludes S
(same-address heir, i.e. lives AT the property) and N (no property).
"""
from __future__ import annotations

from datetime import date

from notice_parser import NoticeData

# Gate 1: equity must be strictly above this percentage.
MIN_EQUITY_PCT = 40.0

# Gate 2: single family only. A property type containing any NON_SF token
# is rejected outright; if a property type is present it must also match a
# SF token (a blank/unknown property type fails closed — no equity-style
# benefit of the doubt, because we can't confirm it's single family).
_NON_SINGLE_FAMILY = (
    "CONDO", "TOWNHOUSE", "TOWN HOUSE", "MULTI", "DUPLEX", "TRIPLEX",
    "QUAD", "APARTMENT", "MOBILE", "MANUFACTURED", "VACANT", "LAND",
    "COMMERCIAL", "MIXED",
)
_SINGLE_FAMILY = (
    "SINGLE FAMILY", "SINGLE-FAMILY", "SFR", "RESIDENTIAL", "DETACHED",
)

_NON_NJ_BLANK = {"", "NJ", "NEW JERSEY"}


def _equity_pct(notice: NoticeData) -> float | None:
    """Parse equity_percent ('63', '63.0', '63%') to a float, or None."""
    raw = (getattr(notice, "equity_percent", "") or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace("%", "").strip())
    except ValueError:
        return None


def _passes_equity(notice: NoticeData) -> bool:
    pct = _equity_pct(notice)
    return pct is not None and pct > MIN_EQUITY_PCT


def _passes_single_family(notice: NoticeData) -> bool:
    pt = (getattr(notice, "property_type", "") or "").upper()
    if any(tok in pt for tok in _NON_SINGLE_FAMILY):
        return False
    # Unknown/blank property type can't be confirmed single family → reject.
    return any(tok in pt for tok in _SINGLE_FAMILY)


def _passes_out_of_state_heir(notice: NoticeData) -> bool:
    # "P" classification = notice_type "probate" (not the S/_same_address or
    # N/_no_property variants), i.e. the heir does not live at the property.
    if (getattr(notice, "notice_type", "") or "").strip().lower() != "probate":
        return False
    state = (getattr(notice, "owner_state", "") or "").upper().strip()
    return state not in _NON_NJ_BLANK


def is_niche_lead(notice: NoticeData) -> bool:
    """True only if the record clears all three niche gates."""
    return (
        _passes_equity(notice)
        and _passes_single_family(notice)
        and _passes_out_of_state_heir(notice)
    )


def default_week_label(today: date | None = None) -> str:
    """'Niche Week 24 2026' from the ISO week of `today` (default: today).

    Verified to match Rick's runner numbering — e.g. 2026-06-10 is ISO
    week 24, and the runner files for that batch are 'Week 24 2026'.
    """
    iso = (today or date.today()).isocalendar()
    return f"Niche Week {iso.week} {iso.year}"


def tag_niche_leads(
    notices: list[NoticeData],
    week_label: str | None = None,
    *,
    today: date | None = None,
) -> dict:
    """Stamp `niche` on qualifying records in place; return gate stats.

    Read-only w.r.t. the record set — nothing is filtered. Only probate
    records are even considered for the per-gate breakdown so the numbers
    in the Slack summary describe the probate cohort, not sheriff/NOD rows
    that share the run.

    Returns a stats dict: {probate_total, niche, equity_pass, sf_pass,
    oos_pass, week_label} where the *_pass counts are independent
    single-gate tallies (so the operator can see which gate is tightest).
    """
    label = week_label or default_week_label(today)
    probate = [n for n in notices if (n.notice_type or "").strip().lower() == "probate"]
    stats = {
        "probate_total": len(probate),
        "niche": 0,
        "equity_pass": 0,
        "sf_pass": 0,
        "oos_pass": 0,
        "week_label": label,
    }
    for n in probate:
        eq = _passes_equity(n)
        sf = _passes_single_family(n)
        oos = _passes_out_of_state_heir(n)
        stats["equity_pass"] += int(eq)
        stats["sf_pass"] += int(sf)
        stats["oos_pass"] += int(oos)
        if eq and sf and oos:
            n.niche = label
            stats["niche"] += 1
    return stats


def niche_slack_line(stats: dict) -> str:
    """One-line gate breakdown for the Slack summary."""
    t = stats["probate_total"]
    return (
        f"  Niche Leads: {stats['niche']}/{t} probate records qualified\n"
        f"    Gate breakdown: {stats['equity_pass']}/{t} equity>40%, "
        f"{stats['sf_pass']}/{t} single family, "
        f"{stats['oos_pass']}/{t} out-of-state P"
    )
