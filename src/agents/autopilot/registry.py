"""Stage kinds added after the first build register here, one line each.

    kind -> (module in this package, run function, spend class, needs a preset, always reports)

The first build listed its kinds in policy.KINDS and an if-chain in sweep._runner, so every new
kind meant edits in three files, and two builds adding stages on the same day met on the same
lines. A new kind now adds its own module and ONE line here; policy and sweep read this table.

  spend class     free, paid or outreach (policy.SPEND_BY_KIND)
  needs a preset  False for a kind that reads the account without preset titles of its own
  always reports  True for a kind whose run is a report: it posts even when it changed nothing
"""
from __future__ import annotations

import importlib

EXTRA: dict[str, tuple[str, str, str, bool, bool]] = {
    # data, lists and hygiene (plan phase 5)
    "laddercheck": ("laddercheck", "run_laddercheck", "free", False, True),
    "conventions": ("conventions", "run_conventions", "free", False, True),
    # text lane and outreach limits (plan 1.3, 1.4, 4.5, 4.6, 0.22)
    "scrub": ("scrub", "run_scrub", "free", True, False),
    "textlane": ("textlane", "run_textlane", "outreach", False, False),
    "limits": ("limits", "run_limits", "free", False, False),
    # lead management (plan phase 2)
    "leadclock": ("leadclock", "run_leadclock", "free", False, False),
    "drips": ("drips", "run_drips", "outreach", False, False),
    "leadtasks": ("leadtasks", "run_leadtasks", "free", False, False),
    "appointments": ("appointments", "run_appointments", "free", False, False),
    "exits": ("exits", "run_exits", "free", False, False),
    # trace, auction, second chances and other lanes (plan 1.8, 4.2, 4.3, 4.4)
    "secondchance": ("secondchance", "run_secondchance", "free", False, False),
    "callerbook": ("callerbook", "run_callerbook", "free", True, False),
    "dialexport": ("dialexport", "run_dialexport", "free", True, False),
    "auctiontasks": ("auctiontasks", "run_auctiontasks", "free", False, False),
    # KPIs, alarms and measurement (plan phase 6, 3.11)
    "alarms": ("alarms", "run_alarms", "free", False, False),
    "goals": ("goals", "run_goals", "free", False, True),
    "dealsource": ("dealsource", "run_dealsource", "free", False, False),
    "cadence": ("cadence", "run_cadence", "free", False, True),
    "coachpick": ("coachpick", "run_coachpick", "free", False, False),
    "researchstats": ("researchstats", "run_researchstats", "free", False, True),
    # the research lane (plan 3.1, 3.2, 3.5 to 3.8, 3.10)
    "researchqueue": ("researchqueue", "run_researchqueue", "paid", False, False),
    "heirlane": ("heirlane", "run_heirlane", "outreach", False, False),
    "obituary": ("obituary", "run_obituary", "outreach", True, False),
    "probate": ("probate", "run_probate", "free", False, False),
    "paidlookup": ("paidlookup", "run_paidlookup", "free", True, False),
    # the two tracks (2026-11-13): the Top 1,000 kept in-house, everything else sent to the agency
    "top1000": ("top1000", "run_top1000", "free", True, False),
    "agencyexport": ("agencyexport", "run_agencyexport", "outreach", True, False),
    "ftmpipeline": ("ftmpipeline", "run_ftmpipeline", "free", True, False),
    # incomplete records owned by people, completed as the course teaches (C2.27, Ty 2026-11-15)
    "complete": ("complete", "run_complete", "free", False, False),
    # the morning callback list: every missed call, voicemail and text nobody answered (lesson, Ty 2026-11-15)
    "callbacks": ("callbacks", "run_callbacks", "free", False, True),
    # a new hire's first week: every answered call graded each evening, the notes to the owner first (Ty, 2026-11-15)
    "newhire": ("newhire", "run_newhire", "paid", False, True),
    # lead notes from the call recording: the four pillars and the intake answers on the record (plan 2.9, C8.21, Ty 2026-11-15)
    "leadnotes": ("leadnotes", "run_leadnotes", "paid", False, False),
    # encumbrances filled when a record becomes a lead (Ty, 2026-11-15; lesson 8-05)
    "encumbrance": ("encumbrance", "run_encumbrance", "free", False, False),
    # relatives' numbers moved down once a probate filing names the executor (C3.14, Ty 2026-11-15)
    "phonecleanup": ("phonecleanup", "run_phonecleanup", "free", False, False),
    # outcomes (2026-11-15): the monthly lost-deal audit and neighborhood suppression suggestions (C10.37, C10.34)
    "lostdeals": ("lostdeals", "run_lostdeals", "free", False, True),
    "suppression": ("suppression", "run_suppression", "free", False, True),
    # county tax status on the records the team works, re-read until paid (research corpus, missed workflows item 2)
    "taxwatch": ("taxwatch", "run_taxwatch", "free", False, False),
    # lead workflows (track leadprep): the property check, the closer handoff text, the primary phone
    "leadprep": ("leadprep", "run_leadprep", "paid", False, False),
    "closertext": ("handoff", "run_closertext", "outreach", False, False),
    "primaryphone": ("primaryphone", "run_primaryphone", "free", False, False),
    # account checks and the quarterly business audit (research 2026-11-15, C13.59)
    "accountchecks": ("accountchecks", "run_accountchecks", "free", False, False),
    "bizaudit": ("bizaudit", "run_bizaudit", "free", False, True),
    # Knox tax sale results: the redemption window, the winning bidders as buyers (2026-11-15)
    "taxsale": ("taxsale", "run_taxsale", "free", False, False),
    # the calling agency's monthly scorecard (missed workflows item 1, 2026-11-15)
    "agencyscore": ("agencyscore", "run_agencyscore", "free", False, True),
    # call review: the attempt log on every lead, and the evening call screen (Ty, 2026-11-15)
    "attemptlog": ("attemptlog", "run_attemptlog", "free", False, False),
    "callscreen": ("callscreen", "run_callscreen", "paid", False, True),
    # contact rate by week: of the owners first worked each week, how many were reached (a friend's bot, Ty 2026-11-16)
    "contactrate": ("contactrate", "run_contactrate", "free", False, True),
    # the KPI tracker: one snapshot three times a weekday for the page, the CSVs and the one daily Slack update (Ty, 2026-11-16)
    "kpitracker": ("kpitracker", "run_kpitracker", "free", False, False),
    # mail counted from a date: the auction countdown and the probate table (Ty, 2026-11-16; SiftMail SPEC 6.3, 6.4)
    "mailcadence": ("mailcadence", "run_mailcadence", "outreach", False, False),
}


def runner(kind: str):
    """The run function for a registered kind, imported the first time it is asked for."""
    module, fn = EXTRA[kind][0], EXTRA[kind][1]
    return getattr(importlib.import_module("%s.%s" % (__package__, module)), fn)


def problems() -> list[str]:
    """What is wrong with the table itself: a module that does not import, a function that is not
    there, a spend class nobody knows. Checked by the selftest so a bad line fails before a deploy."""
    out = []
    for kind, row in EXTRA.items():
        if len(row) != 5:
            out.append("%s: expected five fields, found %d" % (kind, len(row)))
            continue
        if row[2] not in ("free", "paid", "outreach"):
            out.append("%s: spend class %r is not free, paid or outreach" % (kind, row[2]))
        try:
            if not callable(runner(kind)):
                out.append("%s: %s.%s is not a function" % (kind, row[0], row[1]))
        except Exception as exc:  # noqa: BLE001
            out.append("%s: %s.%s could not be loaded (%s)" % (kind, row[0], row[1], str(exc)[:120]))
    return out
