"""Niche cohort gating for high-value leads.

Rick's "niche" cohort is the highest-value slice of a weekly run. Every niche
lead needs real equity AND a single-family home; the third qualifying signal
is record-type specific:

  - Probate: a decision-maker is identified OR the death is confirmed.
    (The old rule ALSO required an out-of-state heir — but probate heirs
    almost always have an in-NJ mailing address, so that gate silently killed
    ~100% of probate niche leads for weeks. Removed for probate.)
  - Non-probate (NOD / lis pendens, sheriff sale): an OUT-OF-STATE owner —
    an absentee-owner signal. For these the owner still holds the property,
    so an out-of-NJ mailing address means they aren't living there.

This is a read-only tagging layer — it never drops records. It runs AFTER
the enrichment pipeline (which fills equity_percent, property_type,
owner_state, owner_deceased, decision_maker_name) and BEFORE the output CSV
is written, stamping NoticeData.niche with a "Niche Week NN YYYY" label so
the tag matches the format Rick's county prepper already uses.

Field mapping note: probate-family notice_types (probate / probate_same_
address / probate_no_property) all use the probate gate; owner_state is the
owner/contact mailing state used for the non-probate out-of-state check.
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


def _is_probate_family(notice: NoticeData) -> bool:
    """True for probate and its S/N variants — all use the probate gate."""
    return (getattr(notice, "notice_type", "") or "").strip().lower().startswith("probate")


def _passes_out_of_state_owner(notice: NoticeData) -> bool:
    """Owner's mailing state is outside NJ — the absentee-owner signal.

    Third gate for NON-PROBATE records (NOD, sheriff sale) only: the owner
    still holds the property, so an out-of-NJ mailing address means they don't
    live there. Blank/unknown state fails closed (can't confirm absentee).
    NOT used for probate — probate heirs are usually in-NJ, and requiring
    out-of-state there silently killed ~100% of probate niche leads for weeks.
    """
    state = (getattr(notice, "owner_state", "") or "").upper().strip()
    return state not in _NON_NJ_BLANK


def _passes_dm_or_deceased(notice: NoticeData) -> bool:
    """Third gate for PROBATE: something to act on — a decision-maker is
    identified, or death is confirmed (probate notices confirm death by
    definition, so this is a light actionability check rather than the tight
    out-of-state filter that was zeroing out the cohort)."""
    if (getattr(notice, "decision_maker_name", "") or "").strip():
        return True
    return (getattr(notice, "owner_deceased", "") or "").strip().lower() == "yes"


def is_niche_lead(notice: NoticeData) -> bool:
    """Equity + single family + a record-type-specific third gate.

    Probate → decision-maker identified OR death confirmed.
    Non-probate (NOD, sheriff sale) → out-of-state owner.
    """
    if not (_passes_equity(notice) and _passes_single_family(notice)):
        return False
    if _is_probate_family(notice):
        return _passes_dm_or_deceased(notice)
    return _passes_out_of_state_owner(notice)


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

    Read-only w.r.t. the record set — nothing is filtered. Evaluates ALL
    record types: probate uses the DM/deceased gate, non-probate (NOD,
    sheriff) uses the out-of-state gate. Every niche lead also needs
    equity >40% and a single-family home.

    Returns a stats dict with an overall tally plus per-type and per-gate
    breakdowns so the operator can see which gate is tightest.
    """
    label = week_label or default_week_label(today)
    stats = {
        "week_label": label,
        "total": len(notices),
        "niche": 0,
        "probate_total": 0,
        "probate_niche": 0,
        "nonprobate_total": 0,
        "nonprobate_niche": 0,
        "equity_pass": 0,
        "sf_pass": 0,
        "dm_or_deceased_pass": 0,   # probate third gate
        "oos_pass": 0,              # non-probate third gate
    }
    for n in notices:
        is_probate = _is_probate_family(n)
        stats["probate_total" if is_probate else "nonprobate_total"] += 1

        eq = _passes_equity(n)
        sf = _passes_single_family(n)
        stats["equity_pass"] += int(eq)
        stats["sf_pass"] += int(sf)

        if is_probate:
            third = _passes_dm_or_deceased(n)
            stats["dm_or_deceased_pass"] += int(third)
        else:
            third = _passes_out_of_state_owner(n)
            stats["oos_pass"] += int(third)

        if eq and sf and third:
            n.niche = label
            stats["niche"] += 1
            stats["probate_niche" if is_probate else "nonprobate_niche"] += 1
    return stats


def niche_slack_line(stats: dict) -> str:
    """Multi-line niche breakdown for the Slack summary."""
    pt = stats["probate_total"]
    npt = stats["nonprobate_total"]
    return (
        f"  Niche Leads: {stats['niche']} qualified "
        f"({stats['probate_niche']}/{pt} probate, "
        f"{stats['nonprobate_niche']}/{npt} non-probate)\n"
        f"    Gates: {stats['equity_pass']} equity>40%, {stats['sf_pass']} single family, "
        f"{stats['dm_or_deceased_pass']}/{pt} probate DM-or-deceased, "
        f"{stats['oos_pass']}/{npt} non-probate out-of-state"
    )
