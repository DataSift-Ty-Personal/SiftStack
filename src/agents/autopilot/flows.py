"""Three marketing flows for the account (the owner's ask, 2026-11-17): the change list that lays the
saved presets out as three separate flows, in this order.

    Foreclosure  first-to-market foreclosures (tag FTM, list Foreclosure): Smarter Contact texts
                 from day 0, the call round, and mail dropped every week until the sale
    Estates      probates (tag FTM, list Probate) and obituary records (list Obituary), each worked
                 to the person who can sign
    Top 1,000    the Priority presets, unchanged; their folders move down to 07-09, the agency to 10

Measured 2026-11-17: every record carrying the FTM tag is on the Foreclosure, Probate or Obituary
list (436, 735 and 144 of 1,166), so the tag splits cleanly. A record on two of those lists goes
to the estate flow: the auction does not change who has to sign.

What the plan does:
  (a) folders: the Top 1,000 and agency folders are renumbered 07 to 10, "05. FTM - CALL" becomes
      "02. FORECLOSURE - CALL", the monthly "06. FTM - MAIL" moves to the archive (the weekly drops
      replace it in the flow; the evergreen mail stage still reads its presets by title), and five
      folders are made: Foreclosure text and weekly mail, Estates text, call and mail.
  (b) the "FTM - ..." call and mail presets keep their titles (every stage and the texting app read
      them by title) and are narrowed to the Foreclosure list, skipping the Probate and Obituary
      lists. Nothing else in their filters moves.
  (c) new presets, each built from those filters: Foreclosure Ready to Text and Texted Not Yet
      Called; six weekly mail presets, one per piece of the auction run, each a week after the last;
      the probate twins of the call, text and mail presets (the probate mail two weeks apart, then
      monthly, as the probate mail stage runs it).
  (d) the obituary presets move into the estate folders with their titles and filters as they are.
      The obituary research preset stays in Deep Prospecting, which is the research hub for every
      flow.
  (f) the estate call flow, one family for probates and obituaries (Ty, 2026-11-17: "probate/obits that
      have 30 days passed date of death"): "Estate - 00..06" read the courthouse probate notice tag OR
      the tag "Obituary 30+ Days", which goes on an obituary record once its obituary date is 30 days
      old (apply backfills it; `ready` and the obituary lane's ready_tag keep it current). The seven
      probate call presets made on the first run are converted in place, so they keep their ids; the
      old obituary call presets go to the archive and "Obituary - All" to Deep Prospecting. The probate
      text and mail presets stay: a text or a letter goes to the personal representative only.
  (g) the hot auction list folded into the foreclosure calls: "Hot Auction + Buy Box" becomes "FTM - Hot
      Auction (sale in 45 days)" in 02. FORECLOSURE - CALL (same id, the foreclosure anchor), and its
      emptied folder "13. HOT AUCTION SALE" goes to the archive.
  (e) the second chance after a no: the 30-day foreclosure and probate presets in 12. REACTIVATION
      read the tags Foreclosure and Probate, which few records carry (measured 2026-11-17: they held
      2 and 0 while 12 foreclosures and 55 probates had said no 30 or more days before). They are
      re-anchored on FTM and the list; the 90-day one keeps the first-to-market records on neither.

`plan` only reads. `apply` needs --commit AND FLOWS_ALLOW=1. It writes a backup of every folder and
preset first, checks every preset still reads as the plan saw it, reads back each change, stops at
the first difference, and logs each write with what it replaced. A second run changes nothing.
`undo` replays the log backwards: a narrowed preset gets its old filter back, a made preset and an
emptied folder go away, a moved preset goes back, a renamed folder takes its old name.

    python src/agents/autopilot/flows.py plan
    python src/agents/autopilot/flows.py apply --plan output/autopilot/flows/plan_<day>.json [--commit]
    python src/agents/autopilot/flows.py undo --log output/autopilot/flows/applied_<day>.jsonl [--commit]
    python src/agents/autopilot/flows.py ready [--commit]      # the daily upkeep of the thirty-day tag
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
ROOT = SRC.parent
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would be first on the path and its modules (mail, policy,
    # windows) would shadow anything else of the same name. Only src belongs there.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.autopilot import preset_patch as pp        # noqa: E402
from agents.autopilot import restructure as rs         # noqa: E402

OUT_DIR = ROOT / "output" / "autopilot" / "flows"
BACKUP_DIR = ROOT / "output" / "backups"
ALLOW_ENV = "FLOWS_ALLOW"
Refused = pp.Refused

# ==================================================================================================
# THE TABLE. Every name this tool relies on is in this block. Edit it, run `plan` again, read it.
# ==================================================================================================

FTM_TAG = "FTM"
FORECLOSURE_LIST = "Foreclosure"
PROBATE_LIST = "Probate"
OBITUARY_LIST = "Obituary"

# Folders renamed, in this order: (now, after). The Top 1,000 and the agency move down so the three
# flows read top to bottom in the app; the monthly first-to-market mail leaves the flow.
RENAMES = [
    ("01. TOP 1000 - TEXT", "07. TOP 1000 - TEXT"),
    ("02. TOP 1000 - CALL", "08. TOP 1000 - CALL"),
    ("03. TOP 1000 - MAIL", "09. TOP 1000 - MAIL"),
    ("04. AGENCY", "10. AGENCY"),
    ("05. FTM - CALL", "02. FORECLOSURE - CALL"),
    ("06. FTM - MAIL", "99. Archive - FTM - MAIL (monthly)"),
    ("13. HOT AUCTION SALE", "99. Archive - 13. HOT AUCTION SALE"),   # its one preset moves to the foreclosure calls
]
FC_TEXT = "01. FORECLOSURE - TEXT"
FC_CALL = "02. FORECLOSURE - CALL"
FC_MAIL = "03. FORECLOSURE - MAIL (WEEKLY)"
ES_TEXT = "04. ESTATES - TEXT"
ES_CALL = "05. ESTATES - CALL"
ES_MAIL = "06. ESTATES - MAIL"
OBIT_ARCHIVE = "99. Archive - Obituary Calls"
DP_FOLDER = "11. DEEP PROSPECTING (ALL TIERS)"
NEW_FOLDERS = [FC_TEXT, FC_MAIL, ES_TEXT, ES_CALL, ES_MAIL, OBIT_ARCHIVE]

# The first-to-market presets the flows are built from (their titles stay).
CALL_PRESETS = ["FTM - 00 Needs Skipped", "FTM - 01 Skipped No Numbers", "FTM - 02 Ready to Call",
                "FTM - 03 Call Attempt 1", "FTM - 04 Call Attempt 2", "FTM - 05 Call Attempt 3",
                "FTM - 06 Call Attempt 4"]
MAIL_PRESETS = ["FTM - Mail 01 Handwritten Letter", "FTM - Mail 02 Family Postcard", "FTM - Mail 03 Soft-Offer Check",
                "FTM - Mail 04 Handwritten Letter", "FTM - Mail 05 Family Postcard", "FTM - Mail 06 Soft-Offer Check"]
READY = "FTM - 02 Ready to Call"
MAIL_FIRST = "FTM - Mail 01 Handwritten Letter"

FC_TEXT_READY = "FTM - 01 Ready to Text"
FC_TEXT_SENT = "FTM - 02 Texted, Not Yet Called"
# One preset per piece of the auction run (auction_mail A1 to A6), each a week after the last.
WEEKLY = ["FTM - Weekly 01 Handwritten Letter", "FTM - Weekly 02 Options Letter", "FTM - Weekly 03 Check Letter",
          "FTM - Weekly 04 Street View Postcard", "FTM - Weekly 05 Closing Letter", "FTM - Weekly 06 Last Note"]
WEEK_GATE = ["36-months", "week"]          # last mailed more than a week ago (the app's own window form)
MONTH_GATE = ["36-months", "month"]        # last mailed more than a month ago

PROBATE_PREFIX = "Probate - "
PB_TEXT_READY = "Probate - 01 Ready to Text"
PB_TEXT_SENT = "Probate - 02 Texted, Not Yet Called"
# (title, mail counter range, gate): the probate mail stage's opening run (PB1 at once, PB2 and PB3
# two weeks apart, then monthly to month 24). The app's window has no two-week point, so the
# preset shows the second and third piece due after a week; the stage holds them to fourteen days.
PROBATE_MAIL = [("Probate - Mail 01 Letter", [0, 0], None),
                ("Probate - Mail 02 Handwritten Note", [1, 1], WEEK_GATE),
                ("Probate - Mail 03 Options Letter", [2, 2], WEEK_GATE),
                ("Probate - Mail 04 Street View Postcard", [3, 3], MONTH_GATE),
                ("Probate - Mail 05 Letter", [4, 4], MONTH_GATE),
                ("Probate - Mail 06 Handwritten Note", [5, 5], MONTH_GATE),
                ("Probate - Mail 07+ Monthly", [6, None], MONTH_GATE)]

# The estate call flow (Ty, 2026-11-17: "consolidate this to have probate/obits that have 30 days
# passed date of death"). One family reads the courthouse probate tag OR the tag the obituary lane puts
# on a record once its death is 30 days old. The app ANDs a preset's keys and reads a date window only
# on the include side (fromPropertiesFilters reads must[key], never must_not), so "a probate, or an
# obituary 30 days past death" can only be said as two tags in any_tags. Probates need no wait: none of
# the 133 courthouse probates that carry an obituary date died inside 30 days of 2026-11-17 (the notice
# comes after the estate opens), and most carry no obituary date at all.
PROBATE_TAG = "probate"                    # the notice-type tag the uploader puts on every courthouse probate
READY_TAG = "Obituary 30+ Days"            # the obituary lane's ready_tag (team/autopilot.yaml)
HOLD_DAYS = 30
ESTATE_PREFIX = "Estate - "
READY_CAP = 3000                           # records tagged in one run at most

# The obituary presets: "Obituary - All" (the obituary lane's own list, every age) to Deep Prospecting,
# the old obituary call presets to the archive (the estate call presets replace them), the obituary
# mail preset stays in the estate mail folder: (title, folder).
OBITUARY_MOVES = [("Obituary - All", DP_FOLDER), ("06 Obituary - Ready to Call", OBIT_ARCHIVE),
                  ("07 Obituary - Call Attempt 1", OBIT_ARCHIVE), ("08 Obituary - Call Attempt 2", OBIT_ARCHIVE),
                  ("09 Obituary - Call Attempt 3", OBIT_ARCHIVE), ("10 Obituary - Call Attempt 4", OBIT_ARCHIVE),
                  ("10 Obituary - Ready to Mail", ES_MAIL)]

# The hot auction list, folded into the foreclosure calls (Ty, 2026-11-17: "consolidate the hot auction
# and buy box ... move it more towards the foreclosure setup ... less confusing and has fewer steps").
# The preset keeps its id, takes the foreclosure anchor (so an estate on the Probate list leaves it)
# and a name that sits with the FTM presets; its old folder, then empty, goes to the archive.
HOT_FROM = "Hot Auction + Buy Box"
HOT_TO = "FTM - Hot Auction (sale in 45 days)"

# The second chance after a no: (title, the list it reads; None keeps the rest of FTM).
REANCHOR = [("Not Interested - FTM Foreclosure (30d)", FORECLOSURE_LIST),
            ("Not Interested - FTM Probate (30d)", PROBATE_LIST),
            ("Not Interested - FTM rest (90d)", None)]

# ==================================================================================================
# End of the table.
# ==================================================================================================

_must = rs._must


# ------------------------------------------------------------------------------------------ filters
def on_list(must: dict, list_id: str) -> dict:
    """The filter that also requires the record to be on this list. all_lists ANDs, so a filter's
    own list either-or (any_lists) keeps its meaning."""
    m = copy.deepcopy(must or {})
    cur = list(m.get("all_lists") or []) if isinstance(m.get("all_lists"), list) else []
    if list_id not in cur:
        cur.append(list_id)
    m["all_lists"] = cur
    return m


def foreclosure_must(must: dict, foreclosure: str, probate: str, obituary: str) -> dict:
    """A first-to-market filter read for foreclosures alone: on the Foreclosure list, off the Probate
    and Obituary lists (those records belong to the estate flow). Nothing else moves."""
    m = on_list(must, foreclosure)
    rs._skip(m, "any_lists", [probate, obituary])
    return m


def probate_must(must: dict, probate: str) -> dict:
    """The same filter read for probates: on the Probate list. The FTM anchor stays, so these are the
    courthouse probates (a probate notice names the personal representative)."""
    return on_list(must, probate)


def reanchor_must(must: dict, ftm: str, list_id: str, skip_ids: list) -> dict:
    """A second-chance filter read on the list instead of the tag of the same name: the anchor becomes
    FTM and the record must be on the list. With no list, the filter keeps its FTM anchor and skips
    the lists the other two read, so a no is never counted twice."""
    m = copy.deepcopy(must or {})
    if list_id:
        m["any_tags"] = [ftm]
        return on_list(m, list_id)
    rs._skip(m, "any_lists", skip_ids)
    return m


def estate_must(must: dict, probate_tag: str, ready) -> dict:
    """A first-to-market call filter read for the estate flow: the FTM anchor gives way to "the
    courthouse probate tag or the thirty-day tag" (two tags in any_tags are either-or). Every other key,
    the exclusions above all, stays as it was."""
    m = copy.deepcopy(must or {})
    m["any_tags"] = [probate_tag, ready]
    return m


def ready_query(obituary: str, probate: str, ready_uuid: str, today: date, hold_days: int = HOLD_DAYS) -> dict:
    """The obituary records due the thirty-day tag: on the Obituary list, off the Probate list (a probate
    is in through its own tag), an obituary date at least `hold_days` ago, and not tagged yet. A record
    with no obituary date is left to the obituary lane, which also reads the Date of Death field."""
    last = (today - timedelta(days=hold_days)).isoformat()
    m = {"all_lists": [obituary], "last_obituary_date": ["1900-01-01", last], "must_not": {"any_lists": [probate]}}
    if ready_uuid:
        m["must_not"]["any_tags"] = [ready_uuid]
    return m


def search_rows(crm, must: dict, cap: int) -> list:
    """Record ids for a filter, a page at a time (the documented search, POST read as GET)."""
    out, off = [], 0
    while off < cap:
        n = min(100, cap - off)
        r = crm.call("/api/internal/property/", "POST", {"limit": n, "offset": off, "query": {"must": must}},
                     headers={"x-http-method-override": "GET"})
        rows = [x for x in rs.rows_of(r) if isinstance(x, dict) and x.get("uuid")]
        out += rows
        if len(rows) < n:
            break
        off += n
    return out


def text_ready(ready_must: dict) -> dict:
    """Ready to Text, from Ready to Call: the text counter at 0 instead of the call counter."""
    m = copy.deepcopy(ready_must)
    m.pop("predictivecall_attempts", None)
    m["sms_attempts"] = [0, 0]
    return m


def text_sent(ready_must: dict) -> dict:
    """Texted at least once and never called."""
    m = copy.deepcopy(ready_must)
    m["sms_attempts"] = [1, None]
    m["predictivecall_attempts"] = [0, 0]
    return m


def mail_step(mail_must: dict, counter: list, gate) -> dict:
    """A mail preset at one place in its run: the mail counter in `counter`, last mailed before the
    gate (none for a first piece, which is never mailed)."""
    m = copy.deepcopy(mail_must)
    m["directmail_attempts"] = list(counter)
    if gate:
        m["last_direct_mailed"] = list(gate)
    else:
        m.pop("last_direct_mailed", None)
    return m


# --------------------------------------------------------------------------------------------- plan
def build_plan(reader, *, day: str = "") -> dict:
    """The change list, read from the account through `reader` (preset_patch.Reader: it cannot
    write)."""
    today = reader.today
    acct = rs.read_account(reader)
    folders, presets = acct["folders"], acct["presets"]
    account = rs._account_of(presets)
    blocking, flags, notes = [], [], []
    by_title = {p["title"]: p for p in presets}

    def obj(rows, title, kind):
        row, twins = rs._exact(rows, title)
        row = row or (twins[0] if len(twins) == 1 else None)
        if not row:
            blocking.append("The %s %s does not exist." % (kind, title))
        return (row or {}).get("uuid") or ""

    ftm = obj(acct["tags"], FTM_TAG, "tag")
    fc = obj(acct["lists"], FORECLOSURE_LIST, "list")
    pb = obj(acct["lists"], PROBATE_LIST, "list")
    ob = obj(acct["lists"], OBITUARY_LIST, "list")
    # Exact titles only for the two estate tags: "Probate" (capitalized) is a different tag from the
    # notice-type tag "probate", and only the second is on every courthouse probate.
    pt_row, _ = rs._exact(acct["tags"], PROBATE_TAG)
    probate_tag = (pt_row or {}).get("uuid") or ""
    if not probate_tag:
        blocking.append("The tag %s (the notice type on every courthouse probate) does not exist." % PROBATE_TAG)
    rd_row, _ = rs._exact(acct["tags"], READY_TAG)
    ready_uuid = (rd_row or {}).get("uuid") or ""
    ready = ready_uuid or rs.ref("tag", READY_TAG)          # a marker until apply makes the tag
    have = {("tag", READY_TAG): ready_uuid} if ready_uuid else {}

    # ---- (a) folders
    renames = []
    for old, new in RENAMES:
        f, _ = rs._exact(folders, old)
        g, _ = rs._exact(folders, new)
        row = {"title": old, "new_title": new, "uuid": (f or g or {}).get("uuid")}
        if f and g:
            row["action"] = "conflict"
            blocking.append("Both %s and %s exist, so the folder cannot be renamed." % (old, new))
        elif f:
            row["action"] = "rename"
        elif g:
            row["action"] = "already"
        else:
            row["action"] = "missing"
            blocking.append("The folder %s was not found." % old)
        renames.append(row)
    made_folders = []
    for title in NEW_FOLDERS:
        f, _ = rs._exact(folders, title)
        made_folders.append({"title": title, "uuid": (f or {}).get("uuid"), "action": "already" if f else "create"})
    taken = [t for t in NEW_FOLDERS if t in {r["new_title"] for r in renames}]
    if taken:
        blocking.append("A new folder takes a renamed folder's title: %s" % ", ".join(taken))

    # ---- (b) the first-to-market presets narrowed to foreclosures
    narrow = []
    sources = {}
    for title in CALL_PRESETS + MAIL_PRESETS:
        p = by_title.get(title)
        if p is None:
            blocking.append("The preset %s does not exist." % title)
            continue
        before = _must(p)
        tags = list(before.get("any_tags") or []) + list(before.get("all_tags") or [])
        if ftm and ftm not in tags:
            blocking.append("%s does not read the FTM tag, so it is not a first-to-market preset." % title)
            continue
        sources[title] = p
        already = fc in (before.get("all_lists") or []) and pb in ((before.get("must_not") or {}).get("any_lists") or [])
        after = before if already else foreclosure_must(before, fc, pb, ob)
        narrow.append({"title": title, "uuid": p["uuid"], "folder": p["folder"], "before_sha": pp.sha(before),
                       "must_before": before, "must_after": after, "action": "already" if already else "change",
                       "count_before": None, "count_after": None})

    # The probate twins are built from the filters as they read before the narrowing; a second run
    # (presets already narrowed) takes the narrowing back off first, so both runs plan the same twin.
    def original(title):
        r = next((x for x in narrow if x["title"] == title), None)
        if r is None:
            return None
        if r["action"] == "change":
            return r["must_before"]
        m = copy.deepcopy(r["must_before"])
        m["all_lists"] = [x for x in m.get("all_lists") or [] if x != fc]
        if not m["all_lists"]:
            m.pop("all_lists")
        mn = m.get("must_not") or {}
        kept = [x for x in mn.get("any_lists") or [] if x not in (pb, ob)]
        if kept:
            mn["any_lists"] = kept
        else:
            mn.pop("any_lists", None)
        return m

    # ---- (c) new presets
    planned = []

    def add(title, folder, flow, must, source, says, convert_from=""):
        live = by_title.get(title)
        old = by_title.get(convert_from) if convert_from else None
        row = {"title": title, "folder": folder, "flow": flow, "must": must, "account": account,
               "quick_filter": bool((sources.get(source) or {}).get("quick_filter")),
               "sources": [{"title": source, "sha": pp.sha(_must(sources[source]))}] if source in sources else [],
               "says": says, "count": None, "uuid": (live or {}).get("uuid")}
        if live is None and old is not None:
            # The preset this one replaces is renamed and given the new filter, so it keeps its id.
            row.update({"action": "convert", "uuid": old["uuid"], "convert_from": convert_from,
                        "convert_sha": pp.sha(_must(old)), "convert_before": _must(old)})
            if old["folder"] != folder:
                blocking.append("%s is in %s, not %s; move it there before it is converted." % (convert_from, old["folder"],
                                                                                              folder))
        elif live is None:
            row["action"] = "create"
        elif _must(live) == must:
            row["action"] = "already"
        else:
            row["action"] = "conflict"
            blocking.append("A preset named %s exists with a different filter; look at it in the app." % title)
        planned.append(row)

    fc_ready = next((x["must_after"] for x in narrow if x["title"] == READY), None)
    fc_mail = next((x["must_after"] for x in narrow if x["title"] == MAIL_FIRST), None)
    if fc_ready is not None:
        add(FC_TEXT_READY, FC_TEXT, "foreclosure", text_ready(fc_ready), READY,
            ["Same filter as %s, narrowed to foreclosures" % READY, "No text sent yet: the Smarter Contact cohort for day 0"])
        add(FC_TEXT_SENT, FC_TEXT, "foreclosure", text_sent(fc_ready), READY,
            ["Texted at least once and never called"])
    if fc_mail is not None:
        for i, title in enumerate(WEEKLY):
            add(title, FC_MAIL, "foreclosure", mail_step(fc_mail, [i, i], WEEK_GATE if i else None), MAIL_FIRST,
                ["Mailed %d time(s)%s" % (i, ", the last more than a week ago" if i else ", the first piece goes at once")])
    for title in CALL_PRESETS:
        base = original(title)
        if base is not None and probate_tag:
            step = title[len("FTM - "):]
            add(ESTATE_PREFIX + step, ES_CALL, "estate", estate_must(base, probate_tag, ready), title,
                ["Same filter as %s, read for courthouse probates and obituary records 30 days past the death" % title],
                convert_from=PROBATE_PREFIX + step)
    pb_ready_src = original(READY)
    if pb_ready_src is not None:
        pr = probate_must(pb_ready_src, pb)
        add(PB_TEXT_READY, ES_TEXT, "estate", text_ready(pr), READY, ["No text sent yet, to the personal representative"])
        add(PB_TEXT_SENT, ES_TEXT, "estate", text_sent(pr), READY, ["Texted at least once and never called"])
    pb_mail_src = original(MAIL_FIRST)
    if pb_mail_src is not None:
        pm = probate_must(pb_mail_src, pb)
        for title, counter, gate in PROBATE_MAIL:
            add(title, ES_MAIL, "estate", mail_step(pm, counter, gate), MAIL_FIRST,
                ["Mail counter %s%s" % (counter, ", last mailed more than a %s ago" % gate[1] if gate else "")])

    # ---- (d) the obituary presets moved as they are
    moves = []
    for title, folder in OBITUARY_MOVES:
        p = by_title.get(title)
        if p is None:
            flags.append("The preset %s was not found, so it is not moved." % title)
            continue
        must = _must(p)
        if pb and pb not in ((must.get("must_not") or {}).get("any_lists") or []):
            flags.append("%s does not skip the Probate list, so a probate on the Obituary list shows in it as well as in "
                         "the probate presets." % title)
        if folder not in NEW_FOLDERS and not rs._exact(folders, folder)[0]:
            blocking.append("The folder %s does not exist, so %s has nowhere to go." % (folder, title))
        moves.append({"title": title, "uuid": p["uuid"], "from": p["folder"], "folder": folder, "must_sha": pp.sha(must),
                      "action": "already" if p["folder"] == folder else "move", "count": None, "must": must})

    # ---- (e) the second chance after a no, read on the lists
    reanchor = []
    for title, list_name in REANCHOR:
        p = by_title.get(title)
        if p is None:
            flags.append("The preset %s was not found, so it is not re-anchored." % title)
            continue
        before = _must(p)
        lid = {FORECLOSURE_LIST: fc, PROBATE_LIST: pb}.get(list_name, "") if list_name else ""
        after = reanchor_must(before, ftm, lid, [fc, pb])
        said = ("re-anchored %s on FTM and the %s list" % (title, list_name) if list_name
                else "%s now skips the foreclosure and probate lists" % title)
        reanchor.append({"title": title, "uuid": p["uuid"], "folder": p["folder"], "before_sha": pp.sha(before),
                         "must_before": before, "must_after": after, "said": said,
                         "action": "already" if after == before else "change", "count_before": None, "count_after": None})

    # ---- (g) the hot auction list, folded into the foreclosure calls
    hot = {}
    p_from, p_to = by_title.get(HOT_FROM), by_title.get(HOT_TO)
    if p_to is not None:
        want = foreclosure_must(_must(p_to), fc, pb, ob)
        hot = {"title": HOT_TO, "uuid": p_to["uuid"], "folder": FC_CALL, "must": want, "count": None,
               "action": "already" if want == _must(p_to) and p_to["folder"] == FC_CALL else "conflict"}
        if hot["action"] == "conflict":
            blocking.append("%s exists but is not the foreclosure hot list in %s; look at it in the app." % (HOT_TO, FC_CALL))
    elif p_from is not None and fc and pb and ob:
        before = _must(p_from)
        hot = {"title": HOT_TO, "uuid": p_from["uuid"], "convert_from": HOT_FROM, "convert_sha": pp.sha(before),
               "convert_before": before, "must": foreclosure_must(before, fc, pb, ob), "folder": FC_CALL,
               "from_folder": p_from["folder"], "count": None, "action": "convert"}
    else:
        flags.append("Neither %s nor %s exists, so there is no hot auction list to fold in." % (HOT_FROM, HOT_TO))

    # ---- counts (the documented search; date windows worked out as of today)
    if hot:
        hot["count"] = reader.count(hot["must"])
    for r in narrow + reanchor:
        r["count_before"] = reader.count(r["must_before"])
        r["count_after"] = r["count_before"] if r["action"] == "already" else reader.count(r["must_after"])
    for r in planned:
        counted = rs.for_count(r["must"], have, today)     # the thirty-day tag holds nobody until it is made
        r["count"] = reader.count(counted) if counted is not None else 0
    for r in moves:
        r["count"] = reader.count(r.pop("must"))
    # ---- (f) the thirty-day tag, and the obituary records due it today
    ready_plan = {"title": READY_TAG, "uuid": ready_uuid, "action": "already" if ready_uuid else "create",
                  "hold_days": HOLD_DAYS, "due": None, "waiting": None}
    if ob and pb:
        ready_plan["due"] = reader.count(ready_query(ob, pb, ready_uuid, today))
        recent = (today - timedelta(days=HOLD_DAYS)).isoformat()
        ready_plan["waiting"] = reader.count({"all_lists": [ob], "must_not": {"any_lists": [pb]},
                                              "last_obituary_date": [recent, (today + timedelta(days=1)).isoformat()]})
    split = {}
    if ftm and fc and pb and ob:
        split = {"ftm": reader.count({"any_tags": [ftm]}),
                 "foreclosure": reader.count({"any_tags": [ftm], "all_lists": [fc], "must_not": {"any_lists": [pb, ob]}}),
                 "probate": reader.count({"any_tags": [ftm], "all_lists": [pb]}),
                 "obituary_only": reader.count({"any_tags": [ftm], "all_lists": [ob], "must_not": {"any_lists": [pb]}}),
                 "neither": reader.count({"any_tags": [ftm], "must_not": {"any_lists": [fc, pb, ob]}})}
        if split["neither"]:
            flags.append("%s FTM record(s) are on none of the Foreclosure, Probate or Obituary lists, so no flow holds "
                         "them." % pp._n(split["neither"]))
    acts = Counter(r["action"] for r in planned)
    return {
        "tool": "flows", "plan_date": day or date.today().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"), "counted_as_of": today.isoformat(),
        "account": account,
        "requests": {"made": reader.made, "from_cache": reader.from_cache, "budget": reader.max_requests,
                     "not_counted": reader.skipped, "stopped": reader.stopped, "errors": reader.errors[:20],
                     "notes": sorted(set(reader.notes))[:20]},
        "summary": {"presets_read": len(presets), "folders_read": len(folders),
                    "folders_to_rename": sum(1 for r in renames if r["action"] == "rename"),
                    "folders_to_create": sum(1 for f in made_folders if f["action"] == "create"),
                    "presets_to_narrow": sum(1 for r in narrow if r["action"] == "change"),
                    "presets_to_create": acts.get("create", 0), "presets_already": acts.get("already", 0),
                    "presets_to_convert": acts.get("convert", 0),
                    "presets_to_move": sum(1 for m in moves if m["action"] == "move"),
                    "presets_to_reanchor": sum(1 for r in reanchor if r["action"] == "change"),
                    "records_to_tag": ready_plan["due"] or 0},
        "blocking": blocking, "flags": flags, "notes": notes, "split": split,
        "objects": {"ftm": ftm, "foreclosure": fc, "probate": pb, "obituary": ob, "probate_tag": probate_tag},
        "renames": renames, "folders": made_folders, "narrow": narrow, "presets": planned, "moves": moves,
        "reanchor": reanchor, "ready": ready_plan, "hot": hot,
    }


def plan_lines(plan: dict) -> list[str]:
    """Every change the plan makes, one line each, with the records each preset holds."""
    s = plan["summary"]
    out = ["Three flows, plan for %s: %d presets in %d folders read, %d requests to the account."
           % (plan["plan_date"], s["presets_read"], s["folders_read"], plan["requests"]["made"])]
    if plan["blocking"]:
        out.append("BLOCKED, apply refuses while any of these stands:")
        out += ["  - %s" % b for b in plan["blocking"]]
    sp = plan.get("split") or {}
    if sp:
        out.append("The FTM tag holds %s: foreclosure %s, probate %s, obituary only %s, none of the three %s."
                   % tuple(pp._n(sp.get(k)) for k in ("ftm", "foreclosure", "probate", "obituary_only", "neither")))
    for r in plan["renames"]:
        out.append("%s folder %s -> %s" % ({"rename": "Rename", "already": "Already:", "missing": "NOT FOUND:",
                                            "conflict": "CONFLICT:"}[r["action"]], r["title"], r["new_title"]))
    for f in plan["folders"]:
        out.append("%s folder %s" % ("Create" if f["action"] == "create" else "Keep", f["title"]))
    for r in plan["narrow"]:
        out.append("%s %s: %s now, %s after" % ("Narrow" if r["action"] == "change" else "Already narrowed:",
                                               r["title"], pp._records(r["count_before"]), pp._records(r["count_after"])))
    rd = plan.get("ready") or {}
    if rd:
        out.append("%s tag %s; %s obituary record(s) are 30+ days past the death and due it, %s are inside the 30 days"
                   % ("Create" if rd["action"] == "create" else "Keep", rd["title"], pp._n(rd.get("due")),
                      pp._n(rd.get("waiting"))))
    for p in plan["presets"]:
        verb = {"create": "Create", "already": "Already:", "conflict": "CONFLICT:",
                "convert": "Convert %s to" % p.get("convert_from")}[p["action"]]
        out.append("%s %s [%s]: %s%s" % (verb, p["title"], p["folder"], pp._records(p["count"]),
                                        " today (more once the obituary records are tagged)" if p.get("flow") == "estate"
                                        and p["title"].startswith(ESTATE_PREFIX) and rd.get("action") == "create" else ""))
    for m in plan["moves"]:
        out.append("%s %s -> %s: %s" % ("Move" if m["action"] == "move" else "Already in place:", m["title"], m["folder"],
                                        pp._records(m["count"])))
    for r in plan.get("reanchor") or []:
        out.append("%s %s: %s now, %s after" % ("Re-anchor" if r["action"] == "change" else "Already re-anchored:",
                                               r["title"], pp._records(r["count_before"]), pp._records(r["count_after"])))
    hot = plan.get("hot") or {}
    if hot.get("action") == "convert":
        out.append("Fold %s into %s as %s: %s" % (hot["convert_from"], hot["folder"], hot["title"], pp._records(hot["count"])))
    elif hot.get("action") == "already":
        out.append("Already folded in: %s [%s]: %s" % (hot["title"], hot["folder"], pp._records(hot["count"])))
    out += ["Flag: %s" % x for x in plan["flags"]]
    return out


def render_md(plan: dict) -> str:
    out = ["# Three flows: the change list for %s" % plan["plan_date"], "",
           "Nothing has been changed in the account. This list was written from a read-only pass on %s (%d requests), "
           "with date windows worked out as of %s." % (plan["generated_at"].replace("T", " at "),
                                                       plan["requests"]["made"], plan["counted_as_of"]), ""]
    out += ["- %s" % line for line in plan_lines(plan)[1:]]
    out += ["", "## When you approve", "",
            "The same tool sends exactly what is recorded here. It needs two switches set by a person (--commit and "
            "%s=1). It backs up every folder and preset first, reads back every change and stops at the first "
            "difference. Every write is logged with what it replaced, and `undo` puts it all back." % ALLOW_ENV]
    return "\n".join(out).rstrip() + "\n"


def write_plan(plan: dict, out_dir: Path | str = OUT_DIR) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / ("plan_%s.json" % plan["plan_date"])
    m = out_dir / ("plan_%s.md" % plan["plan_date"])
    j.write_text(json.dumps(plan, indent=1), encoding="utf-8", newline="\n")
    m.write_text(render_md(plan), encoding="utf-8", newline="\n")
    return j, m


# -------------------------------------------------------------------------------------------- apply
def may_write(commit: bool) -> bool:
    """Both switches, or nothing is sent."""
    return bool(commit) and os.environ.get(ALLOW_ENV) == "1"


def _write(crm, allowed: bool, path: str, method: str, body=None):
    """The one place this module sends a write. Refuses unless both switches are on."""
    if not allowed:
        raise Refused("refused: a write needs --commit AND %s=1" % ALLOW_ENV)
    return crm.call(path, method, body)


def write_backup(state, backup_dir, label: str, day: str) -> Path:
    """Every folder and preset as apply found them, before its first write. A second run the same day
    keeps the first file: the first one is the true before."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    base = "presets_%s_%s_before_flows" % (label, day)
    path, n = backup_dir / (base + ".json"), 2
    while path.exists():
        path, n = backup_dir / ("%s_%d.json" % (base, n)), n + 1
    path.write_text(json.dumps(state.raw, indent=1), encoding="utf-8", newline="\n")
    return path


def _steps(plan: dict) -> list[str]:
    out = ["rename folder %s to %s" % (r["title"], r["new_title"]) for r in plan["renames"] if r["action"] == "rename"]
    out += ["create folder %s" % f["title"] for f in plan["folders"] if f["action"] == "create"]
    rd = plan.get("ready") or {}
    if rd.get("action") == "create":
        out.append("create tag %s" % rd["title"])
    out += ["narrow preset %s" % r["title"] for r in plan["narrow"] if r["action"] == "change"]
    out += ["create preset %s" % p["title"] for p in plan["presets"] if p["action"] == "create"]
    out += ["convert preset %s to %s" % (p["convert_from"], p["title"]) for p in plan["presets"] if p["action"] == "convert"]
    out += ["move preset %s" % m["title"] for m in plan["moves"] if m["action"] == "move"]
    out += ["re-anchor preset %s" % r["title"] for r in plan.get("reanchor") or [] if r["action"] == "change"]
    if rd.get("due"):
        out.append("tag %s obituary record(s) %s (counted at plan time; apply asks again)" % (rd["due"], rd["title"]))
    hot = plan.get("hot") or {}
    if hot.get("action") == "convert":
        out.append("convert preset %s to %s" % (hot["convert_from"], hot["title"]))
        out.append("move preset %s into %s" % (hot["title"], hot["folder"]))
    return out


def _have_tag(st, title: str) -> dict:
    row, _ = rs._exact(st.tags, title)
    return {("tag", title): row["uuid"]} if row and row.get("uuid") else {}


def _resolved(p: dict, have: dict):
    """A planned filter with the thirty-day tag's id in place of its marker, or None while the tag is
    not made yet."""
    try:
        return rs.resolve(p["must"], have)
    except Refused:
        return None


def _preflight(st, plan: dict) -> list[str]:
    """What would stop the run part way, found before its first write: a preset that changed since
    the plan read it, or a new title someone has taken since."""
    out = []
    for r in plan["narrow"] + (plan.get("reanchor") or []):
        live = st.preset(r["title"])
        if live is None:
            out.append("%s is gone" % r["title"])
        elif _must(live) != r["must_after"] and pp.sha(_must(live)) != r["before_sha"]:
            out.append("%s changed since the plan was written" % r["title"])
    have = _have_tag(st, READY_TAG)
    for p in plan["presets"]:
        live = st.preset(p["title"])
        want = _resolved(p, have)
        if live is not None and _must(live) != want:
            out.append("a preset named %s exists with a different filter" % p["title"])
        if live is None and p["action"] == "convert":
            old = st.preset(p["convert_from"])
            if old is None:
                out.append("%s is gone, so it cannot become %s" % (p["convert_from"], p["title"]))
            elif pp.sha(_must(old)) != p["convert_sha"]:
                out.append("%s changed since the plan was written" % p["convert_from"])
    for m in plan["moves"]:
        live = st.preset(m["title"])
        if live is None:
            out.append("%s is gone" % m["title"])
        elif pp.sha(_must(live)) != m["must_sha"]:
            out.append("%s changed since the plan was written" % m["title"])
    hot = plan.get("hot") or {}
    if hot.get("action") == "convert" and st.preset(hot["title"]) is None:
        old = st.preset(hot["convert_from"])
        if old is None:
            out.append("%s is gone" % hot["convert_from"])
        elif pp.sha(_must(old)) != hot["convert_sha"]:
            out.append("%s changed since the plan was written" % hot["convert_from"])
    if out:
        out.append("run plan again")
    return list(dict.fromkeys(out))


def apply_plan(crm, plan: dict, *, commit: bool = False, log_path=None, backup_dir=BACKUP_DIR, label: str = "account",
               out=print, today: date | None = None) -> dict:
    """Send the approved plan. Dry unless both switches are on (a dry run touches nothing, not even to
    read). `today` sets the thirty-day line for the obituary tag (default: today)."""
    if plan.get("tool") != "flows":
        raise Refused("refused: this file is not a flows plan")
    if plan.get("blocking"):
        raise Refused("refused: the plan has %d blocking item(s), the first: %s" % (len(plan["blocking"]), plan["blocking"][0]))
    allowed = may_write(commit)
    done = {"allowed": allowed, "made": [], "already": [], "skipped": [], "would": [], "backup": ""}
    if not allowed:
        done["would"] = _steps(plan)
        out("DRY: nothing sent and nothing read. %d steps would run. A real run needs --commit and %s=1."
            % (len(done["would"]), ALLOW_ENV))
        return done
    st = rs.State(crm)
    done["backup"] = str(write_backup(st, backup_dir, label, plan.get("plan_date") or date.today().isoformat()))
    out("backup   %s" % done["backup"])
    if plan.get("account") and st.account() and st.account() != plan["account"]:
        raise Refused("refused: the key reads a different account from the one the plan was written for")
    problems = _preflight(st, plan)
    if problems:
        raise Refused("refused before any write: %s" % "; ".join(problems))

    def say(kind, text):
        done[kind].append(text)
        out("%-8s %s" % ({"made": "done", "already": "already", "skipped": "skipped"}[kind], text))

    for r in plan["renames"]:
        if r["action"] in ("rename", "already"):
            _rename(crm, st, r, allowed, log_path, say)
    folder_ids = {}
    for f in plan["folders"]:
        folder_ids[f["title"]] = _folder(crm, st, f["title"], allowed, log_path, say)
    for r in plan["renames"]:
        g = st.folder(r["new_title"])
        if g:
            folder_ids[r["new_title"]] = g["uuid"]

    def fid(title):
        got = folder_ids.get(title) or (st.folder(title) or {}).get("uuid")
        if not got:
            raise Refused("stopped: the folder %s is not in the account" % title)
        return got

    have = _ensure_tag(crm, st, READY_TAG, allowed, log_path, say) if plan.get("ready") else {}
    for r in plan["narrow"]:
        _narrow(crm, st, r, allowed, log_path, say)
    for p in plan["presets"]:
        _preset(crm, st, dict(p, must=rs.resolve(p["must"], have)), fid(p["folder"]), allowed, log_path, say)
    for m in plan["moves"]:
        _move(crm, st, m, fid(m["folder"]), allowed, log_path, say)
    for r in plan.get("reanchor") or []:
        _narrow(crm, st, r, allowed, log_path, say)
    hot = plan.get("hot") or {}
    if hot.get("action") == "convert":
        if st.preset(hot["title"]) is None:
            _convert(crm, st, hot, allowed, log_path, say)        # title and filter, the id kept
        _move(crm, st, {"title": hot["title"], "folder": hot["folder"]}, fid(hot["folder"]), allowed, log_path, say)
    elif hot.get("action") == "already":
        say("already", "preset %s in %s" % (hot["title"], hot["folder"]))
    if plan.get("ready"):
        ob = plan["objects"]
        n = tag_ready_records(crm, ob["obituary"], ob["probate"], have[("tag", READY_TAG)], today or date.today(), allowed,
                              log_path, out=out)
        say("made" if n else "already", "tagged %d obituary record(s) %s (30 days past the death)" % (n, READY_TAG)
            if n else "no obituary record was due the tag %s" % READY_TAG)
        done["tagged"] = n
    return done


def _ensure_tag(crm, st, title: str, allowed: bool, log_path, say) -> dict:
    """The thirty-day tag, made when missing and read back. Returns {("tag", title): id}."""
    have = _have_tag(st, title)
    if have:
        say("already", "tag %s" % title)
        return have
    made = _write(crm, allowed, rs.R_TAG_NEW, "POST", {"title": title})
    uuid = rs._record_made(crm, log_path, made, {"step": "tag", "title": title}, rs.R_TAGS)
    if not uuid or str((crm.call(rs.R_TAG_ONE % uuid) or {}).get("title") or "") != title:
        raise Refused("stopped: the tag %s was sent but does not read back" % title)
    st.tags.append({"uuid": uuid, "title": title})
    say("made", "tag %s" % title)
    return {("tag", title): uuid}


def tag_ready_records(crm, obituary: str, probate: str, ready_uuid: str, today: date, allowed: bool, log_path,
                      cap: int = READY_CAP, out=None) -> int:
    """Put the thirty-day tag on every obituary record due it today: asked fresh, so a record that
    crossed the line since the plan is included and one tagged since is not. Each record is logged
    before the next request, so undo can take the tag off again. Returns how many were tagged."""
    if not ready_uuid:
        raise Refused("stopped: the tag %s does not exist yet" % READY_TAG)
    rows = search_rows(crm, ready_query(obituary, probate, ready_uuid, today), cap)
    n = 0
    for row in rows:
        _write(crm, allowed, "/api/internal/property/%s/add-tags/" % row["uuid"], "POST", {"tags": READY_TAG})
        rs._log(log_path, {"step": "tag_record", "uuid": row["uuid"], "title": READY_TAG})
        n += 1
        if out and n % 100 == 0:
            out("tagged %d of %d" % (n, len(rows)))
    # No re-count here: the search index trails a tag write by about half a minute, so a record tagged a
    # moment ago can still read as due. The next run asks again, and a tag added twice is one tag.
    return n


def _rename(crm, st, r: dict, allowed: bool, log_path, say) -> None:
    old, new = st.folder(r["title"]), st.folder(r["new_title"])
    if new and not old:
        say("already", "folder %s" % r["new_title"])
        return
    if old and new:
        raise Refused("stopped: both %s and %s exist now" % (r["title"], r["new_title"]))
    if not old:
        say("skipped", "folder %s (not found)" % r["title"])
        return
    _write(crm, allowed, rs.R_FOLDER_ONE % old["uuid"], "PATCH", {"title": r["new_title"]})
    rs._log(log_path, {"step": "rename", "uuid": old["uuid"], "before": r["title"], "after": r["new_title"]})
    if (crm.call(rs.R_FOLDER_ONE % old["uuid"]) or {}).get("title") != r["new_title"]:
        raise Refused("stopped: %s was renamed but does not read back as %s" % (r["title"], r["new_title"]))
    old["title"] = r["new_title"]
    say("made", "renamed folder %s to %s" % (r["title"], r["new_title"]))


def _folder(crm, st, title: str, allowed: bool, log_path, say) -> str:
    f = st.folder(title)
    if f:
        say("already", "folder %s" % title)
        return f["uuid"]
    made = _write(crm, allowed, rs.R_FOLDER_NEW, "POST", {"title": title, "type": "properties", "permissions": []})
    uuid = rs._record_made(crm, log_path, made, {"step": "folder", "title": title}, rs.R_FOLDERS)
    if not uuid or (crm.call(rs.R_FOLDER_ONE % uuid) or {}).get("title") != title:
        raise Refused("stopped: the folder %s was sent but does not read back" % title)
    st.folders.append({"uuid": uuid, "title": title})
    say("made", "folder %s" % title)
    return uuid


def _narrow(crm, st, r: dict, allowed: bool, log_path, say) -> None:
    path = rs.R_PRESET_ONE % r["uuid"]
    live = crm.call(path) or {}
    filters = copy.deepcopy(live.get("filters") or {})
    if (filters.get("must") or {}) == r["must_after"]:
        say("already", "preset %s %s" % (r["title"], "re-anchored" if r.get("said") else "narrowed"))
        return
    if pp.sha(filters.get("must") or {}) != r["before_sha"]:
        raise Refused("stopped: %s changed since the plan was written; run plan again" % r["title"])
    filters["must"] = r["must_after"]                               # the PATCH replaces the whole filters object
    _write(crm, allowed, path, "PATCH", {"filters": filters})
    rs._log(log_path, {"step": "narrow", "uuid": r["uuid"], "title": r["title"], "before": r["must_before"],
                       "after": r["must_after"]})
    back = crm.call(path) or {}
    if (back.get("filters") or {}).get("must") != r["must_after"]:
        raise Refused("stopped: %s reads back differently from what was sent; nothing after it was touched" % r["title"])
    say("made", r.get("said") or "narrowed %s to foreclosures" % r["title"])


def _preset(crm, st, p: dict, folder_uuid: str, allowed: bool, log_path, say) -> None:
    live = st.preset(p["title"])
    if live is not None:
        if _must(live) != p["must"]:
            raise Refused("stopped: a preset named %s exists with a different filter; run plan again" % p["title"])
        say("already", "preset %s" % p["title"])
        return
    if p["action"] == "convert":
        _convert(crm, st, p, allowed, log_path, say)
        return
    filters = {"must": p["must"]}
    if p.get("account"):
        filters["account"] = p["account"]
    made = _write(crm, allowed, rs.R_PRESET_NEW, "POST", {"title": p["title"], "folder": folder_uuid,
                                                          "quick_filter": bool(p.get("quick_filter")), "filters": filters,
                                                          "type": "properties"})
    row = {"step": "preset", "title": p["title"], "folder": folder_uuid, "must": p["must"]}
    uuid = rs._record_made(crm, log_path, made, row, rs.R_FOLDER_PRESETS % folder_uuid)
    back = (crm.call(rs.R_PRESET_ONE % uuid) if uuid else {}) or {}
    if _must(back) != p["must"] or rs._folder_of(back) != folder_uuid:
        if uuid:
            rs._log(log_path, dict(row, uuid=uuid, must=_must(back)))   # what it stored, so undo can take it away
        raise Refused("stopped: %s was created but reads back differently from what was sent; nothing after it was "
                      "touched" % p["title"])
    st.presets.append({"uuid": uuid, "title": p["title"], "folder": p["folder"], "folder_uuid": folder_uuid,
                       "quick_filter": bool(p.get("quick_filter")), "filters": filters})
    say("made", "preset %s (%s)" % (p["title"], pp._records(p.get("count"))))


def _convert(crm, st, p: dict, allowed: bool, log_path, say) -> None:
    """The preset this one replaces takes its title and filter, so it keeps its id (and its place in
    any view that points at it). Read back before the next step."""
    old = st.preset(p["convert_from"])
    if old is None:
        raise Refused("stopped: %s is gone, so it cannot become %s" % (p["convert_from"], p["title"]))
    path = rs.R_PRESET_ONE % old["uuid"]
    live = crm.call(path) or {}
    filters = copy.deepcopy(live.get("filters") or {})
    if pp.sha(filters.get("must") or {}) != p["convert_sha"]:
        raise Refused("stopped: %s changed since the plan was written; run plan again" % p["convert_from"])
    before = copy.deepcopy(filters.get("must") or {})
    filters["must"] = p["must"]
    _write(crm, allowed, path, "PATCH", {"title": p["title"], "filters": filters})
    rs._log(log_path, {"step": "convert", "uuid": old["uuid"], "title": p["title"], "before_title": p["convert_from"],
                       "before": before, "after": p["must"]})
    back = crm.call(path) or {}
    if back.get("title") != p["title"] or (back.get("filters") or {}).get("must") != p["must"]:
        raise Refused("stopped: %s was converted but does not read back as %s" % (p["convert_from"], p["title"]))
    old["title"] = p["title"]
    old["filters"] = filters
    say("made", "converted %s to %s (%s)" % (p["convert_from"], p["title"], pp._records(p.get("count"))))


def _move(crm, st, m: dict, folder_uuid: str, allowed: bool, log_path, say) -> None:
    live = st.preset(m["title"])
    if live is None:
        raise Refused("stopped: %s is gone" % m["title"])
    if live["folder_uuid"] == folder_uuid:
        say("already", "preset %s in %s" % (m["title"], m["folder"]))
        return
    must = _must(live)
    _write(crm, allowed, rs.R_PRESET_ONE % live["uuid"], "PATCH", {"folder": folder_uuid})
    rs._log(log_path, {"step": "move", "uuid": live["uuid"], "title": m["title"], "before": live["folder_uuid"],
                       "after": folder_uuid})
    back = crm.call(rs.R_PRESET_ONE % live["uuid"]) or {}
    if rs._folder_of(back) != folder_uuid or _must(back) != must:
        raise Refused("stopped: %s was moved but does not read back in its new folder" % m["title"])
    live["folder_uuid"] = folder_uuid
    say("made", "moved preset %s into %s" % (m["title"], m["folder"]))


# --------------------------------------------------------------------------------------------- undo
def undo(crm, log_path, *, commit: bool = False, out=print) -> dict:
    """Put the logged writes back, newest first. A made preset someone has edited since, a folder that
    holds presets, and a folder someone renamed since are left where they are and named."""
    rows = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    allowed = may_write(commit)
    done = {"allowed": allowed, "restored": [], "left": [], "would": [], "stopped": ""}
    seen: set = set()
    own = {"narrow": _undo_narrow, "convert": _undo_convert, "tag_record": _undo_tag_record}
    for row in reversed(rows):
        step = row.get("step")
        key = (step, row.get("uuid")) if step in own else rs._undo_key(row)
        if key in seen:
            continue
        seen.add(key)
        what = "%s %s" % (step, row.get("title") or row.get("after") or row.get("uuid"))
        if step == "tag_record":
            what = "tag_record %s on %s" % (row.get("title"), row.get("uuid"))
        if not allowed:
            done["would"].append(what)
            out("would undo %s" % what)
            continue
        try:
            left = own[step](crm, row, allowed) if step in own else rs._undo_one(crm, row, allowed)
        except Refused as exc:
            done["stopped"] = "%s: %s" % (what, exc)
            out("STOP   " + done["stopped"])
            break
        if left:
            done["left"].append("%s: %s" % (what, left))
            out("left   %s: %s" % (what, left))
        else:
            done["restored"].append(what)
            out("undone %s" % what)
    return done


def _undo_narrow(crm, row: dict, allowed: bool) -> str:
    path = rs.R_PRESET_ONE % row["uuid"]
    cur = crm.call(path) or {}
    filters = copy.deepcopy(cur.get("filters") or {})
    if (filters.get("must") or {}) == row["before"]:
        return ""
    if (filters.get("must") or {}) != row["after"]:
        return "its filter was edited since it was narrowed"
    filters["must"] = row["before"]
    _write(crm, allowed, path, "PATCH", {"filters": filters})
    if ((crm.call(path) or {}).get("filters") or {}).get("must") != row["before"]:
        raise Refused("the old filter did not read back")
    return ""


def _undo_convert(crm, row: dict, allowed: bool) -> str:
    path = rs.R_PRESET_ONE % row["uuid"]
    cur = crm.call(path) or {}
    filters = copy.deepcopy(cur.get("filters") or {})
    if cur.get("title") == row["before_title"] and (filters.get("must") or {}) == row["before"]:
        return ""
    if cur.get("title") != row["title"] or (filters.get("must") or {}) != row["after"]:
        return "it was edited since it was converted"
    filters["must"] = row["before"]
    _write(crm, allowed, path, "PATCH", {"title": row["before_title"], "filters": filters})
    back = crm.call(path) or {}
    if back.get("title") != row["before_title"] or (back.get("filters") or {}).get("must") != row["before"]:
        raise Refused("the old title and filter did not read back")
    return ""


def _undo_tag_record(crm, row: dict, allowed: bool) -> str:
    _write(crm, allowed, "/api/internal/property/%s/remove-tags/" % row["uuid"], "POST", {"tags": row["title"]})
    return ""


def ready(crm, *, commit: bool = False, today: date | None = None, log_path=None, out=print,
          allowed: bool | None = None) -> dict:
    """The daily upkeep: tag every obituary record that crossed thirty days past its death since the last
    run. Dry unless both switches are on; then it reads the account and says how many are due.
    `allowed` replaces the two switches for a caller that has its own: the first-to-market runner
    passes its --commit (its daily run on Fly is the deliberate go)."""
    obituary, probate = crm.list_uuid(OBITUARY_LIST), crm.list_uuid(PROBATE_LIST)
    ready_uuid = crm.tag_uuid(READY_TAG)
    if not (obituary and probate and ready_uuid):
        raise Refused("refused: the Obituary and Probate lists and the tag %s must all exist (run apply first)" % READY_TAG)
    day = today or date.today()
    due = crm.count(ready_query(obituary, probate, ready_uuid, day))
    if not (may_write(commit) if allowed is None else bool(allowed)):
        out("DRY: %d obituary record(s) are 30+ days past the death and due the tag %s. Nothing was sent." % (due, READY_TAG))
        return {"allowed": False, "due": due, "tagged": 0}
    n = tag_ready_records(crm, obituary, probate, ready_uuid, day, True, log_path, out=out)
    out("tagged %d obituary record(s) %s" % (n, READY_TAG))
    return {"allowed": True, "due": due, "tagged": n}


# ---------------------------------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    from agents.autopilot.budget import BudgetExhausted
    ap = argparse.ArgumentParser(description="Three flows: plan (read-only), apply and undo (each needs --commit and %s=1)."
                                             % ALLOW_ENV)
    sub = ap.add_subparsers(dest="cmd")
    pl = sub.add_parser("plan", help="read the account, count, write the change list (the default)")
    pl.add_argument("--date", default="", help="the day the plan is for, YYYY-MM-DD (default today)")
    pl.add_argument("--out-dir", default=str(OUT_DIR))
    pl.add_argument("--cache", default="", help="a file that keeps listings and counts, so a re-run does not re-read")
    pl.add_argument("--max-requests", type=int, default=300)
    pl.add_argument("--gap", type=float, default=2.0, help="seconds between requests (2.0 is 30 a minute)")
    pl.add_argument("--as-of", default="", help="the day date windows are worked out from (default today)")
    aq = sub.add_parser("apply", help="send an approved plan; dry unless --commit and %s=1" % ALLOW_ENV)
    aq.add_argument("--plan", required=True)
    aq.add_argument("--commit", action="store_true")
    aq.add_argument("--gap", type=float, default=2.0)
    aq.add_argument("--label", default="account", help="the account's short name in the backup file name")
    uq = sub.add_parser("undo", help="replay an apply log backwards; dry unless --commit and %s=1" % ALLOW_ENV)
    uq.add_argument("--log", required=True)
    uq.add_argument("--commit", action="store_true")
    uq.add_argument("--gap", type=float, default=2.0)
    rq = sub.add_parser("ready", help="the daily upkeep: tag obituary records 30 days past the death; dry unless "
                                      "--commit and %s=1" % ALLOW_ENV)
    rq.add_argument("--commit", action="store_true")
    rq.add_argument("--gap", type=float, default=1.0)
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] not in ("plan", "apply", "undo", "ready", "-h", "--help"):
        raw = ["plan"] + raw
    args = ap.parse_args(raw)
    if args.cmd in ("apply", "undo", "ready") and args.commit and not may_write(True):
        print("refused: --commit was passed but %s is not 1. Nothing was sent." % ALLOW_ENV)
        return 3
    try:
        if args.cmd == "ready":
            log_path = OUT_DIR / ("ready_%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
            done = ready(pp.key_only_crm(args.gap), commit=args.commit, log_path=log_path)
            if done["allowed"]:
                print("Log: %s" % log_path)
            return 0
        if args.cmd == "apply":
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            if not may_write(args.commit):
                apply_plan(None, plan, commit=False)
                return 0
            log_path = OUT_DIR / ("applied_%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
            done = apply_plan(pp.key_only_crm(args.gap), plan, commit=True, log_path=log_path, label=args.label)
            print("made %d, already %d, skipped %d. Log: %s" % (len(done["made"]), len(done["already"]),
                                                                len(done["skipped"]), log_path))
            return 0
        if args.cmd == "undo":
            if not may_write(args.commit):
                undo(None, args.log, commit=False)
                return 0
            done = undo(pp.key_only_crm(args.gap), args.log, commit=True)
            print("undone %d, left %d%s" % (len(done["restored"]), len(done["left"]),
                                            "; STOPPED: " + done["stopped"] if done["stopped"] else ""))
            return 1 if done["stopped"] else 0
        reader = pp.Reader(pp.key_only_crm(args.gap), cache_path=args.cache or None, max_requests=args.max_requests,
                           now=date.fromisoformat(args.as_of) if args.as_of else None)
        plan = build_plan(reader, day=args.date)
        j, m = write_plan(plan, args.out_dir)
        for line in plan_lines(plan):
            print(line)
        print(j)
        print(m)
        return 2 if plan["blocking"] else 0
    except BudgetExhausted as exc:
        print("stopped: %s. Run it again after the hour: apply and undo pick up where they stopped." % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
