"""Two tracks for the marketing presets (the owner's restructure, approved 2026-11-13).

The account worked four tiers (Hottest, Strong, first-to-market and Bulk), each with its own call
and mail presets, and they held far more than the team can work: the 11-12 census counted 9,277
Strong records ready to call, and one caller held 2,977 open records against a taught ceiling of
about 2,000. So the account now has two tracks, and every record is in exactly one of them:

  * Priority, worked in-house through the whole sequence (Smarter Contact texts first, then calls,
    then mail, then deep prospecting): every first-to-market record (tag FTM) and a scored Top
    1,000 (tag Top 1000, applied by the scoring track).
  * Agency: everything else in the buy box, exported to a calling agency every month (the top
    5,000 by score). What was sent is ONE list (Agency) and ONE date field (Sent to Agency), never
    a tag per month: a tag per month is a new filter every month, and the old ones are never
    cleaned up.

This tool writes that structure as one change list for the owner to approve:

  * the tag Top 1000, the list Agency and the date field Sent to Agency, each only when missing;
  * four folders, 01. PRIORITY - TEXT, 02. PRIORITY - CALL, 03. PRIORITY - MAIL and 04. AGENCY.
    The Priority call and mail presets are clones of the first-to-market ones with one change, the
    anchor: FTM becomes "FTM or Top 1000" where it stands. Every other key is copied exactly, so
    the exclusions the 11-13 change list proved stay proven;
  * the eight tier folders archived by renaming each one "99. Archive - <old name>". Nothing is
    deleted and no preset title changes, because the autopilot reads its presets by title.

Three rules hold it together:

  * The anchor is proven before anything is written. "FTM or Top 1000" is two tags in any_tags,
    and the live search must show that two tags there mean either-or: asked for two tags, it must
    count more records than either tag alone. If it does not, the plan is blocked and apply
    refuses, because a Priority track that reads "FTM and Top 1000" would hold almost nobody.
  * A filter key counts only when a count moves, exactly as in preset_patch: the search ignores a
    key it does not know and still answers. The text counter and any custom date filter are asked
    with and without before they are used.
  * `plan` only reads. `apply` needs BOTH --commit and RESTRUCTURE_ALLOW=1; it writes a backup of
    every folder and preset before its first write, reads back every object it makes or changes,
    stops at the first difference, and logs each write with what it replaced. A second run changes
    nothing, and `undo` replays the log backwards.

Every route here is in the published API, and every request goes through the autopilot's own
client, so it counts against the app's shared request budget (budget.py).

    python src/agents/autopilot/restructure.py plan
    python src/agents/autopilot/restructure.py apply --plan output/autopilot/restructure/plan_<day>.json [--commit]
    python src/agents/autopilot/restructure.py undo --log output/autopilot/restructure/applied_<day>.jsonl [--commit]
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

from agents.autopilot import preset_patch as pp    # noqa: E402
from agents.autopilot import windows               # noqa: E402
from agents.autopilot.crm import rows_of           # noqa: E402

OUT_DIR = ROOT / "output" / "autopilot" / "restructure"
BACKUP_DIR = ROOT / "output" / "backups"
ALLOW_ENV = "RESTRUCTURE_ALLOW"
Refused = pp.Refused

# ==================================================================================================
# THE TABLE. Every name this tool relies on is in this block. Edit it, run `plan` again, read it.
# ==================================================================================================

FTM_TAG = "FTM"
TOP_TAG = "Top 1000"                  # applied by the scoring track; created here when missing
AGENCY_LIST = "Agency"                # every record ever sent to the agency
SENT_FIELD = "Sent to Agency"         # the day it was sent: a date field on the property
SENT_FIELD_GROUP = "Misc."            # the custom field group it goes in (a create needs a group)

# A record sent to the agency is not sent again for this many days. A preset can say that only if
# the search reads a custom date field, which is proven by count below; when it is not, the queue
# skips the whole Agency list and a sent record never comes back on its own. None: always skip
# the whole list.
RESEND_AFTER_DAYS = 90
# Existing date fields the custom date filter is proven on (a field that carries dates already).
PROOF_DATE_FIELDS = ["Foreclosure Date", "Date Added", "Death Date"]

# The eight tier folders, exactly as the account titles them (the first is misspelled there).
TIER_FOLDERS = ["01. HOTTEST (Prioty 1) - CALL", "02. HOTTEST (Priority 1) - MAIL", "03. STRONG - CALL",
                "04. STRONG - MAIL", "05. FTM - CALL", "06. FTM - MAIL", "09. BULK - CALL", "10. BULK - MAIL"]
ARCHIVE_PREFIX = "99. Archive - "

# Where the Priority presets are cloned from, and how their titles change.
FTM_CALL_FOLDER = "05. FTM - CALL"
FTM_MAIL_FOLDER = "06. FTM - MAIL"
SOURCE_PREFIX = "FTM - "
TARGET_PREFIX = "Priority - "
FTM_READY = "FTM - 02 Ready to Call"
STRONG_READY = "Strong - 02 Ready to Call"
BULK_READY = "Bulk - 02 Ready to Call"

# The old tiers' tags. The Agency queue does not route on them: a record tagged Priority 1 or
# Priority 2 that is neither FTM nor in the Top 1000 is an Agency record, because every record is
# in exactly one track. STRONG_TAG is the one the queue is split on if Strong and Bulk cannot
# share one preset.
TIER_TAGS = ["Priority 1", "Priority 2"]
STRONG_TAG = "Priority 2"
# Keys of the Ready to Call presets that place a record in the in-house call round, not in the buy
# box. The queue leaves them out, or every record the team ever dialed would sit in no track.
AGENCY_DROPS = ["predictivecall_attempts"]
# More tags the queue skips besides FTM and Top 1000. Empty: everything outside Priority goes out.
AGENCY_ALSO_SKIPS: list[str] = []

TEXT_FOLDER = "01. PRIORITY - TEXT"
CALL_FOLDER = "02. PRIORITY - CALL"
MAIL_FOLDER = "03. PRIORITY - MAIL"
AGENCY_FOLDER = "04. AGENCY"
NEW_FOLDERS = [TEXT_FOLDER, CALL_FOLDER, MAIL_FOLDER, AGENCY_FOLDER]

TEXT_READY = "Priority - 01 Ready to Text"
TEXT_SENT = "Priority - 02 Texted, Not Yet Called"
AGENCY_QUEUE = "Agency - 01 Queue"
AGENCY_QUEUE_STRONG = "Agency - 01 Queue (Strong)"
AGENCY_QUEUE_BULK = "Agency - 01 Queue (Bulk)"
AGENCY_SENT = "Agency - 02 Sent"
AGENCY_BACK = "Agency - 03 Came Back"

# How a filter on a custom date field might be spelled. The published reference does not say, so
# each is asked on the search with an existing date field, and one is used only when its count
# moves both ways (as a requirement and as an exclusion). `f` carries the field's uuid, id and
# machine name; `w` is a [from, to] window. Measured on the live search 2026-11-13: a LIST under
# custom_fields is refused with 400 "Expected a dictionary of items but got type list", so the
# search knows the key and wants a dictionary; a machine name as a key of its own is silently
# ignored. These dictionary spellings are the candidates; none is proven until a plan says so.
CUSTOM_DATE_SHAPES = [
    ("by_uuid", lambda f, w: {"custom_fields": {f["uuid"]: list(w)}} if f.get("uuid") else None),
    ("by_id", lambda f, w: {"custom_fields": {str(f["id"]): list(w)}} if f.get("id") is not None else None),
    ("by_machine_name", lambda f, w: {"custom_fields": {f["machine_name"]: list(w)}} if f.get("machine_name") else None),
]

# ==================================================================================================
# End of the table.
# ==================================================================================================

R_FOLDERS = "/api/internal/filter-preset-folder/?type=properties&limit=999"
R_FOLDER_NEW = "/api/internal/filter-preset-folder/"
R_FOLDER_ONE = "/api/internal/filter-preset-folder/%s/"
R_FOLDER_PRESETS = "/api/internal/filter-preset-folder/%s/filter-preset/?limit=999"
R_PRESET_NEW = "/api/internal/filter-preset/"
R_PRESET_ONE = "/api/internal/filter-preset/%s/"
R_TAGS = "/api/internal/tag/?offset=0&limit=10000&ordering=title"
R_TAG_NEW = "/api/internal/tag/"
R_TAG_ONE = "/api/internal/tag/%s/"
R_LISTS = "/api/internal/list/?offset=0&limit=1000"
R_LIST_NEW = "/api/internal/list/"
R_LIST_ONE = "/api/internal/list/%s/"
R_FIELDS = "/api/internal/custom-fields/?limit=999"
R_FIELD_NEW = "/api/internal/custom-fields/"
R_FIELD_ONE = "/api/internal/custom-fields/%s/"
R_GROUPS = "/api/internal/custom-fields/group/?limit=999"
R_STATUSES = "/api/internal/global-status/"

# A planned field in a filter KEY (the dictionary under custom_fields is keyed by the field): the
# prefix says which of the field's ids goes there once apply has made it.
FIELD_KEYS = {"$uuid:": "uuid", "$id:": "id", "$machine:": "machine_name"}
REF_KEYS = ("any_tags", "all_tags", "any_lists", "all_lists")
WIDE = ["1900-01-01", "2100-12-31"]


# ------------------------------------------------------------------------------------------ names
def ref(kind: str, title: str) -> dict:
    """An object the plan makes. It is written into a filter as this marker and replaced by the
    object's id once apply has made it, so the plan can be read before the object exists."""
    return {"$ref": kind, "title": title}


def is_ref(v) -> bool:
    return isinstance(v, dict) and set(v) == {"$ref", "title"}


def _exact(rows, title: str, key: str = "title"):
    """(the row titled exactly `title`, rows that differ only in case or spaces). A twin is never
    taken for the real thing: a tag 'top 1000' is not the tag the scoring track applies."""
    want = str(title or "").strip()
    hit = next((r for r in rows or [] if str(r.get(key) or "") == title), None) \
        or next((r for r in rows or [] if str(r.get(key) or "").strip() == want), None)
    twins = [r for r in rows or [] if r is not hit and str(r.get(key) or "").strip().lower() == want.lower()]
    return hit, twins


def _must(p: dict) -> dict:
    return ((p or {}).get("filters") or {}).get("must") or {}


def _folder_of(row) -> str:
    f = (row or {}).get("folder")
    return f.get("uuid") if isinstance(f, dict) else (f or "")


def _preset_row(p: dict, f: dict) -> dict:
    return {"uuid": p.get("uuid"), "title": p.get("title") or "", "folder": f.get("title") or "",
            "folder_uuid": f.get("uuid"), "quick_filter": bool(p.get("quick_filter")),
            "filters": p.get("filters") or {}}


def _ident(kind: str, title: str, have: dict):
    got = have.get((kind, title))
    if kind == "custom_field":
        return (got or {}).get("uuid")
    return got


def _field_key(k, have: dict) -> tuple:
    """(the key with a planned field filled in or None when that field is missing, whether the key
    named a planned field at all). Any other key comes back as it is."""
    if isinstance(k, str):
        for prefix, attr in FIELD_KEYS.items():
            if k.startswith(prefix):
                v = (have.get(("custom_field", k[len(prefix):])) or {}).get(attr)
                return (str(v) if v is not None else None), True
    return k, False


def resolve(must: dict, have: dict) -> dict:
    """The filter with every marker replaced by the id of the object it names. Refuses when an
    object is still missing: a preset that names nothing is a different preset."""
    def walk(v):
        if is_ref(v):
            got = _ident(v["$ref"], v["title"], have)
            if not got:
                raise Refused("stopped: the %s %s does not exist, so a preset that names it cannot be written"
                              % (v["$ref"].replace("_", " "), v["title"]))
            return got
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                nk, marked = _field_key(k, have)
                if marked and nk is None:
                    raise Refused("stopped: the field %s does not exist, so a preset that filters on it cannot be "
                                  "written" % k.split(":", 1)[1])
                out[nk] = walk(x)
            return out
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(copy.deepcopy(must))


def _dated(v, today: date):
    """A window kept the way the app keeps its own ('90-days' back to 'today'), as fixed dates,
    because the search refuses the relative form."""
    if isinstance(v, dict):
        return {k: _dated(x, today) for k, x in v.items()}
    if isinstance(v, list):
        if len(v) == 2 and all(isinstance(x, str) or x is None for x in v):
            got, _ = windows.resolve({"w": list(v)}, today)
            return got.get("w", list(v))
        return [_dated(x, today) for x in v]
    return v


def for_count(must: dict, have: dict, today: date) -> dict | None:
    """The filter as it can be counted today, or None when it matches nobody today. An object the
    plan has not made yet is carried by no record: in an either-or it is left out, in an exclusion
    it excludes nobody, and a filter that requires it matches nobody."""
    m = copy.deepcopy(must or {})
    for key in REF_KEYS:
        if not isinstance(m.get(key), list):
            continue
        got = [_ident(x["$ref"], x["title"], have) if is_ref(x) else x for x in m[key]]
        if key.startswith("any_"):
            got = [x for x in got if x]
            if not got:
                return None
        elif not all(got):
            return None
        m[key] = got
    mn = m.get("must_not")
    if isinstance(mn, dict):
        for key in list(mn):
            v = mn.pop(key)
            if key == "custom_fields" and isinstance(v, dict):
                kept = {}
                for k2, w in v.items():
                    nk, _ = _field_key(k2, have)
                    if nk is not None:            # a field not made yet: nobody carries a date in it
                        kept[nk] = _dated(w, today)
                if kept:
                    mn[key] = kept
            elif key in REF_KEYS and isinstance(v, list):
                v = [x for x in (_ident(y["$ref"], y["title"], have) if is_ref(y) else y for y in v) if x]
                if v:
                    mn[key] = v
            else:
                mn[key] = v
        if not mn:
            m.pop("must_not")
    return m


def canon(v, key: str = ""):
    """A filter for comparison: the members of an any_ or all_ list are a set, everything else
    (ranges above all, where [None, 5] and [5, None] are different) keeps its order."""
    if isinstance(v, dict):
        return {k: canon(x, k) for k, x in sorted(v.items())}
    if isinstance(v, list):
        items = [canon(x) for x in v]
        if key.startswith(("any_", "all_")) and all(isinstance(x, (str, int, float)) for x in items):
            return sorted(set(items), key=str)
        return items
    return v


def differs(a: dict, b: dict) -> list[str]:
    """The keys (and must_not keys) where two filters differ."""
    ca, cb, out = canon(a), canon(b), []
    for k in sorted(set(ca) | set(cb)):
        if k == "must_not":
            ma, mb = ca.get(k) or {}, cb.get(k) or {}
            out += ["must_not." + k2 for k2 in sorted(set(ma) | set(mb)) if ma.get(k2) != mb.get(k2)]
        elif ca.get(k) != cb.get(k):
            out.append(k)
    return out


def _drop_tags(m: dict, gone: set) -> None:
    """Take these tag ids out of the filter's own tags and out of its exclusions."""
    for block in (m, m.get("must_not") if isinstance(m.get("must_not"), dict) else {}):
        tags = block.get("any_tags")
        if isinstance(tags, list):
            kept = [t for t in tags if is_ref(t) or t not in gone]
            if kept:
                block["any_tags"] = kept
            else:
                block.pop("any_tags")
    if isinstance(m.get("must_not"), dict) and not m["must_not"]:
        m.pop("must_not")


def agency_base(must: dict, routing: set) -> dict:
    """A Ready to Call preset's buy box: its filter without the routing that says which tier a
    record is in (the tier's own tag, the other tiers it skips) and without the keys that place a
    record in the in-house call round."""
    m = copy.deepcopy(must or {})
    for key in AGENCY_DROPS:
        m.pop(key, None)
    _drop_tags(m, routing)
    return m


def _skip(m: dict, key: str, values: list) -> None:
    """Add values to the filter's exclusions under `key`, after what is already there, never twice."""
    values = [v for v in values or [] if v]
    if not values:
        return
    mn = m.setdefault("must_not", {})
    cur = list(mn.get(key)) if isinstance(mn.get(key), list) else []
    for v in values:
        if v not in cur:
            cur.append(v)
    mn[key] = cur


def priority_must(must: dict, ftm: str, anchor: list) -> tuple[dict | None, str]:
    """The same filter read for the Priority track: the FTM tag becomes 'FTM or Top 1000' where it
    stands, and nothing else moves. Returns (filter, '') or (None, why it cannot be said)."""
    m = copy.deepcopy(must or {})
    tags = m.get("any_tags")
    if isinstance(tags, list) and ftm in tags:
        out: list = []
        for t in tags:
            for x in (anchor if t == ftm else [t]):
                if x not in out:
                    out.append(x)
        m["any_tags"] = out
        return m, ""
    req = m.get("all_tags")
    if isinstance(req, list) and ftm in req:
        if m.get("any_tags"):
            return None, ("it requires FTM together with an either-or block of other tags, so 'FTM or Top 1000' "
                          "cannot be added without changing what those tags mean")
        rest = [t for t in req if t != ftm]
        if rest:
            m["all_tags"] = rest
        else:
            m.pop("all_tags")
        m["any_tags"] = list(anchor)
        return m, ""
    return None, "it does not read the FTM tag, so there is no anchor to replace"


def clone_title(title: str) -> str | None:
    return TARGET_PREFIX + title[len(SOURCE_PREFIX):] if (title or "").startswith(SOURCE_PREFIX) else None


# ------------------------------------------------------------------------------------------ reading
def read_account(reader) -> dict:
    """Every folder with its presets (listings default to ten rows, so each asks for all), the
    tags, lists, custom fields and their groups, and the published status listing."""
    folders = reader.rows(R_FOLDERS)
    presets = []
    for f in folders:
        for p in reader.rows(R_FOLDER_PRESETS % f["uuid"]):
            presets.append(_preset_row(p, f))
    return {"folders": folders, "presets": presets, "tags": reader.rows(R_TAGS), "lists": reader.rows(R_LISTS),
            "fields": reader.rows(R_FIELDS), "groups": reader.rows(R_GROUPS, optional=True),
            "published": reader.rows(R_STATUSES, optional=True)}


def _account_of(presets) -> str:
    """The account the presets belong to (each saved filter names it)."""
    c = Counter(str((p.get("filters") or {}).get("account") or "") for p in presets)
    c.pop("", None)
    return c.most_common(1)[0][0] if c else ""


# ------------------------------------------------------------------------------------------ proofs
def prove_or(reader, ftm: str, others: list) -> dict:
    """Two tags in any_tags must mean either-or. Under either-or, asking for two tags can never
    count fewer records than one of them alone, and counts MORE whenever each tag has a record the
    other lacks. So: more than both alone proves it, fewer than one alone disproves it, and equal
    says nothing (one tag's records hold the other's), and the next tag is asked."""
    out = {"proven": None, "rows": []}
    if not ftm:
        out["say"] = "The FTM tag does not exist, so the anchor could not be asked about."
        return out
    a = reader.count({"any_tags": [ftm]})
    for uuid, title in others:
        b = reader.count({"any_tags": [uuid]})
        ab = reader.count({"any_tags": [ftm, uuid]})
        row = {"with": title, "ftm": a, "other": b, "either": ab}
        out["rows"].append(row)
        if None in (a, b, ab):
            row["verdict"] = "not counted"
            continue
        if ab > max(a, b):
            row["verdict"] = "either-or"
            out["proven"] = True
            break
        if ab < max(a, b):
            row["verdict"] = "both-at-once"
            out["proven"] = False
            break
        row["verdict"] = "one holds the other"
    if out["proven"] is True:
        r = out["rows"][-1]
        out["say"] = ("Two tags in any_tags mean either-or: FTM %s, %s %s, either of them %s."
                      % (pp._n(r["ftm"]), r["with"], pp._n(r["other"]), pp._n(r["either"])))
    elif out["proven"] is False:
        r = out["rows"][-1]
        out["say"] = ("Two tags in any_tags mean BOTH AT ONCE here: FTM %s, %s %s, the two together %s. A Priority "
                      "preset built on 'FTM or Top 1000' would hold only records carrying both. Nothing is applied. "
                      "The way out is one anchor tag that both the first-to-market upload and the scoring track "
                      "apply (for example 'Priority'), or two presets for every step, one per tag."
                      % (pp._n(r["ftm"]), r["with"], pp._n(r["other"]), pp._n(r["either"])))
    else:
        out["say"] = ("It could not be shown that two tags in any_tags mean either-or (no pair of tags gave a "
                      "clear answer in this run), so nothing is applied until a plan proves it.")
    return out


def prove_counter(reader, key: str) -> dict:
    """A counter key the account's own presets do not use yet: asked with and without. When
    nobody's counter differs today, asking for the records that DO carry one shows it is read."""
    base = reader.count(pp.PROOF_BASE)
    zero = reader.count(dict(pp.PROOF_BASE, **{key: [0, 0]}))
    row = {"key": key, "without": base, "with": zero, "proven": None}
    if None in (base, zero):
        return row
    if zero != base:
        row["proven"] = True
        return row
    some = reader.count(dict(pp.PROOF_BASE, **{key: [1, None]}))
    row["with_opposite"] = some
    row["proven"] = None if some is None else some != base
    return row


def prove_custom_date(reader, fields: list, base_must: dict, today: date) -> dict:
    """Whether the search reads a filter on a custom date field, and how it is spelled. Asked on a
    date field that already carries dates, as a requirement (a wide and a 90-day window) and as an
    exclusion. A spelling counts only when both move: a requirement the search reads as 'nothing
    matches' moves the count too, and the exclusion is what tells the two apart."""
    out = {"proven": False, "shape": None, "field": None, "rows": [], "why": ""}
    field = None
    for label in PROOF_DATE_FIELDS:
        field = next((f for f in fields or [] if (f.get("label") or "").strip().lower() == label.lower()
                      and (f.get("field_type") or "") == "date" and (f.get("entity_type") or "property") == "property"
                      and f.get("is_active") is not False and f.get("uuid")), None)
        if field:
            break
    if not field:
        out["why"] = "no date field with a known name was found to test on (%s)" % ", ".join(PROOF_DATE_FIELDS)
        return out
    out["field"] = field.get("label")
    n = reader.count(base_must)
    f = {"uuid": field.get("uuid"), "id": field.get("id"), "machine_name": field.get("machine_name")}
    narrow = [(today - timedelta(days=RESEND_AFTER_DAYS or 90)).isoformat(), today.isoformat()]
    for name, shape in CUSTOM_DATE_SHAPES:
        sw, sn = shape(f, WIDE), shape(f, narrow)
        row = {"shape": name, "without": n}
        out["rows"].append(row)
        if not sw:
            row["verdict"] = "the field carries no id of this kind"
            continue
        cw, cn = reader.count(dict(base_must, **sw)), reader.count(dict(base_must, **sn))
        row.update(wide=cw, narrow=cn)
        if None in (n, cw, cn):
            row["verdict"] = "refused, or not counted"
            continue
        if cw == n and cn == n:
            row["verdict"] = "did not move"
            continue
        cx = reader.count(dict(base_must, must_not=dict(base_must.get("must_not") or {}, **sw)))
        row["excluded"] = cx
        if cx is None or cx == n:
            row["verdict"] = "moved as a requirement, not as an exclusion"
            continue
        row["verdict"] = "read"
        out.update(proven=True, shape=name)
        return out
    out["why"] = "none of the %d spellings tried on %s moved the count both ways" % (len(CUSTOM_DATE_SHAPES), out["field"])
    return out


# --------------------------------------------------------------------------------------------- plan
def build_plan(reader, *, day: str = "") -> dict:
    today = reader.today
    acct = read_account(reader)
    folders, presets = acct["folders"], acct["presets"]
    by_title: dict = {}
    for p in presets:
        by_title.setdefault(p["title"], p)
    statuses, snotes = pp.known_statuses(acct["published"])
    reader.notes += snotes
    ix = pp.Index(acct["tags"], acct["lists"], statuses)
    blocking: list[str] = []
    flags: list[str] = []
    notes: list[str] = []

    # ---- the three objects the tracks rest on
    ftm_row, _ = _exact(acct["tags"], FTM_TAG)
    if not ftm_row:
        blocking.append("The tag FTM does not exist, so there is no first-to-market anchor to build on.")
    ftm = (ftm_row or {}).get("uuid") or ""
    top_row, top_twins = _exact(acct["tags"], TOP_TAG)
    for t in top_twins:
        flags.append("A tag %r exists beside the one this plan uses (%r). Tags are matched exactly, so the Priority "
                     "presets read %r; make sure the scoring track applies that spelling." % (t.get("title"), TOP_TAG,
                                                                                               TOP_TAG))
    list_row, list_twins = _exact(acct["lists"], AGENCY_LIST)
    if not list_row and len(list_twins) == 1:
        list_row = list_twins[0]
        flags.append("The list is spelled %r in the account; list titles are unique whatever their case, so that list "
                     "is used." % list_row.get("title"))
    field_row = _field_row(acct["fields"])
    objects = [
        {"kind": "tag", "title": TOP_TAG, "uuid": (top_row or {}).get("uuid"),
         "action": "exists" if top_row else "create", "say": "the Top 1000 tag (the scoring track applies it)"},
        {"kind": "list", "title": (list_row or {}).get("title") or AGENCY_LIST, "uuid": (list_row or {}).get("uuid"),
         "action": "exists" if list_row else "create", "say": "the Agency list (every record ever sent)"},
    ]
    fentry = {"kind": "custom_field", "title": SENT_FIELD, "uuid": (field_row or {}).get("uuid"),
              "id": (field_row or {}).get("id"),
              "action": "create" if not field_row else ("reactivate" if field_row.get("is_active") is False
                                                        else "exists"),
              "say": "the Sent to Agency date field (the day a record was sent)"}
    if field_row and (field_row.get("field_type") or "") != "date":
        fentry["action"] = "conflict"
        blocking.append("A field named %s exists but holds %s, not a date. Rename it in the app, then run the plan "
                        "again." % (SENT_FIELD, field_row.get("field_type") or "an unknown type"))
    if fentry["action"] == "create":
        group, _ = _exact(acct["groups"], SENT_FIELD_GROUP, key="label")
        if not group:
            group = next((g for g in acct["groups"] if (g.get("entity_type") or "property") == "property"), None)
            if group:
                flags.append("There is no custom field group called %s, so the field goes in %s."
                             % (SENT_FIELD_GROUP, group.get("label")))
        if not group:
            blocking.append("No custom field group could be read, and a field cannot be created without one.")
        fentry["group"] = (group or {}).get("label")
        fentry["body"] = {"label": SENT_FIELD, "field_type": "date", "entity_type": "property",
                          "group_id": (group or {}).get("id")}
    objects.append(fentry)

    have: dict = {("tag", FTM_TAG): ftm, ("tag", TOP_TAG): (top_row or {}).get("uuid"),
                  ("list", AGENCY_LIST): (list_row or {}).get("uuid"),
                  ("custom_field", SENT_FIELD): ({"uuid": field_row.get("uuid"), "id": field_row.get("id"),
                                                 "machine_name": field_row.get("machine_name")}
                                                if field_row and fentry["action"] in ("exists", "reactivate")
                                                else None)}
    tiers = {}
    for t in TIER_TAGS:
        row, _ = _exact(acct["tags"], t)
        tiers[t] = (row or {}).get("uuid")
        have[("tag", t)] = tiers[t]
    top_ref = have[("tag", TOP_TAG)] or ref("tag", TOP_TAG)
    agency_ref = have[("list", AGENCY_LIST)] or ref("list", AGENCY_LIST)
    anchor = [ftm, top_ref]

    # ---- proofs
    others = ([(have[("tag", TOP_TAG)], TOP_TAG)] if have[("tag", TOP_TAG)] else []) \
        + [(u, t) for t, u in tiers.items() if u]
    proof_or = prove_or(reader, ftm, others)
    if proof_or["proven"] is not True:
        blocking.append(proof_or["say"])
    proof_sms = prove_counter(reader, "sms_attempts")
    if RESEND_AFTER_DAYS:
        proof_date = prove_custom_date(reader, acct["fields"], {"any_tags": [ftm]} if ftm else dict(pp.PROOF_BASE),
                                       today)
    else:
        proof_date = {"proven": False, "shape": None, "field": None, "rows": [],
                      "why": "the resend window is switched off in the table (RESEND_AFTER_DAYS)"}

    # ---- the sources
    def folder_either(title: str):
        f, _ = _exact(folders, title)
        return f or _exact(folders, ARCHIVE_PREFIX + title)[0]

    call_src, mail_src = folder_either(FTM_CALL_FOLDER), folder_either(FTM_MAIL_FOLDER)
    for title, f in ((FTM_CALL_FOLDER, call_src), (FTM_MAIL_FOLDER, mail_src)):
        if not f:
            blocking.append("The folder %s was not found, so its presets cannot be cloned." % title)
    ready, strong, bulk = by_title.get(FTM_READY), by_title.get(STRONG_READY), by_title.get(BULK_READY)
    for title, p in ((FTM_READY, ready), (STRONG_READY, strong), (BULK_READY, bulk)):
        if not p:
            blocking.append("The preset %s was not found; the two tracks are built from it." % title)

    def src(p):
        return {"title": p["title"], "uuid": p["uuid"], "sha": pp.sha(_must(p))}

    planned: list[dict] = []
    account = _account_of(presets)

    def add(title, folder, kind, must, sources, quick, says, held=""):
        planned.append({"title": title, "folder": folder, "kind": kind, "must": must, "sources": sources,
                        "quick_filter": bool(quick), "account": account, "says": says, "held": held,
                        "action": "", "count": None, "uuid": None})

    # ---- Priority: texts, from the first-to-market Ready to Call
    if ready and ftm:
        m, why = priority_must(_must(ready), ftm, anchor)
        if m is None:
            flags.append("%s cannot be read for the Priority track: %s. The text presets are not built." % (FTM_READY, why))
        else:
            held = "" if proof_sms["proven"] is True else (
                "the search did not show that it reads the text counter, so a preset built on it is not proposed")
            r = copy.deepcopy(m)
            r.pop("predictivecall_attempts", None)
            r["sms_attempts"] = [0, 0]
            s = copy.deepcopy(r)
            s["sms_attempts"] = [1, None]
            s["predictivecall_attempts"] = [0, 0]
            add(TEXT_READY, TEXT_FOLDER, "text", r, [src(ready)], ready["quick_filter"],
                ["Same filter as %s, read for FTM or Top 1000" % FTM_READY,
                 "No text sent yet (text counter at 0) instead of the call counter at 0"], held)
            add(TEXT_SENT, TEXT_FOLDER, "text", s, [src(ready)], ready["quick_filter"],
                ["Same filter as %s, read for FTM or Top 1000" % FTM_READY,
                 "Texted at least once (text counter 1 or more) and never called (call counter at 0)"], held)

    # ---- Priority: call and mail, one clone per first-to-market preset
    for folder_src, target in ((call_src, CALL_FOLDER), (mail_src, MAIL_FOLDER)):
        if not folder_src or not ftm:
            continue
        for p in [q for q in presets if q["folder_uuid"] == folder_src["uuid"]]:
            nt = clone_title(p["title"])
            if not nt:
                flags.append("%s in %s does not start with %r, so it is not cloned. It is archived with its folder."
                             % (p["title"], folder_src["title"], SOURCE_PREFIX))
                continue
            m, why = priority_must(_must(p), ftm, anchor)
            if m is None:
                flags.append("%s is not cloned: %s." % (p["title"], why))
                continue
            add(nt, target, "call" if target == CALL_FOLDER else "mail", m, [src(p)], p["quick_filter"],
                ["Same filter as %s, read for FTM or Top 1000" % p["title"]])

    # ---- Agency: the queue, what was sent, what came back
    agency = {"one_preset": None, "differs": [], "former": [], "window": None, "skips_list": True}
    if strong and bulk and ftm:
        also = []
        for t in AGENCY_ALSO_SKIPS:
            row, _ = _exact(acct["tags"], t)
            if row:
                also.append(row["uuid"])
            else:
                flags.append("The table asks the queue to skip the tag %s, which does not exist." % t)
        window = None
        if proof_date["proven"] and RESEND_AFTER_DAYS:
            shape = dict(CUSTOM_DATE_SHAPES)[proof_date["shape"]]
            fobj = have[("custom_field", SENT_FIELD)] or {attr: prefix + SENT_FIELD
                                                         for prefix, attr in FIELD_KEYS.items()}
            window = shape(fobj, ["%d-days" % RESEND_AFTER_DAYS, "today"])
        agency["window"] = window
        agency["skips_list"] = not window
        routing = {u for u in [ftm, have[("tag", TOP_TAG)]] + list(tiers.values()) if u}

        def finish(m):
            for k in AGENCY_DROPS:
                m.pop(k, None)
            _skip(m, "any_tags", [ftm, top_ref] + also)
            if window:
                mn = m.setdefault("must_not", {})
                for k, v in copy.deepcopy(window).items():
                    mn[k] = dict(mn[k], **v) if isinstance(v, dict) and isinstance(mn.get(k), dict) else v
            else:
                _skip(m, "any_lists", [agency_ref])
            return m

        skip_say = ("Skips records sent to the agency in the last %d days (the Sent to Agency date)" % RESEND_AFTER_DAYS
                    if window else "Skips the Agency list (every record already sent)")
        common = ["Skips FTM and Top 1000 (the Priority track)", skip_say,
                  "The old tier tags are not used: a Priority 1 or Priority 2 record outside FTM and the Top 1000 is "
                  "an Agency record",
                  "The call counter is not used: a record the team dialed before is still sent"]
        bs, bb = agency_base(_must(strong), routing), agency_base(_must(bulk), routing)
        agency["differs"] = differs(bs, bb)
        if not agency["differs"]:
            agency["one_preset"] = True
            add(AGENCY_QUEUE, AGENCY_FOLDER, "agency", finish(copy.deepcopy(bb)), [src(strong), src(bulk)], False,
                ["The buy box of %s and %s, which match once the tier tags and the call counter are set aside"
                 % (STRONG_READY, BULK_READY)] + common)
        else:
            agency["one_preset"] = False
            split = tiers.get(STRONG_TAG)
            anchor_s = _must(strong).get("any_tags")
            if not split or anchor_s != [split]:
                blocking.append("%s and %s differ (%s), and the Strong preset is not anchored on %s alone, so the queue "
                                "cannot be split in two either." % (STRONG_READY, BULK_READY,
                                                                    ", ".join(agency["differs"]), STRONG_TAG))
            else:
                gone = {u for u in list(tiers.values()) + [ftm, have[("tag", TOP_TAG)]] if u and u != split}
                s, b = copy.deepcopy(_must(strong)), copy.deepcopy(_must(bulk))
                _drop_tags(s, gone)
                _drop_tags(b, gone)
                add(AGENCY_QUEUE_STRONG, AGENCY_FOLDER, "agency", finish(s), [src(strong)], False,
                    ["%s with the Priority track and sent records taken out (records tagged %s)"
                     % (STRONG_READY, STRONG_TAG)] + common)
                add(AGENCY_QUEUE_BULK, AGENCY_FOLDER, "agency", finish(b), [src(bulk)], False,
                    ["%s with the Priority track and sent records taken out (records not tagged %s)"
                     % (BULK_READY, STRONG_TAG)] + common)
                flags.append("One preset cannot hold Strong and Bulk: they differ on %s. Presets AND their keys, so the "
                             "queue is two presets split on the tag %s. Between them they hold every Agency record "
                             "once." % (", ".join(agency["differs"]), STRONG_TAG))
    leads = [ix.status(s) for s in pp.LEAD_STATUSES if ix.status(s)]
    add(AGENCY_SENT, AGENCY_FOLDER, "agency", {"any_lists": [agency_ref]}, [], False,
        ["Every record on the Agency list"])
    add(AGENCY_BACK, AGENCY_FOLDER, "agency", {"any_lists": [agency_ref], "any_property_status": leads}, [], False,
        ["Records on the Agency list now in a lead status (%d statuses)" % len(leads)])

    # ---- what each new preset means for the account as it stands
    for p in planned:
        if p["held"]:
            p["action"] = "held"
            continue
        live = by_title.get(p["title"])
        if live is None:
            p["action"] = "create"
            continue
        p["uuid"] = live["uuid"]
        try:
            want = resolve(p["must"], have)
        except Refused:
            want = None
        if want is not None and _must(live) == want:
            p["action"] = "already" if live["folder"].strip() == p["folder"] else "move"
            p["from_folder"] = live["folder"]
        else:
            p["action"] = "conflict"
            blocking.append("A preset named %s already exists in %s with a different filter. Titles are unique in an "
                            "account: rename or remove it in the app, then run the plan again."
                            % (p["title"], live["folder"]))

    fplan = []
    for t in NEW_FOLDERS:
        f, twins = _exact(folders, t)
        fplan.append({"title": t, "action": "exists" if f else "create", "uuid": (f or {}).get("uuid")})
        if twins and not f:
            flags.append("A folder %r exists beside %r; a new folder is made with the exact title."
                         % (twins[0].get("title"), t))

    archive = []
    for old in TIER_FOLDERS:
        f, _ = _exact(folders, old)
        a, _ = _exact(folders, ARCHIVE_PREFIX + old)
        held_by = f or a
        inside = [q["title"] for q in presets if held_by and q["folder_uuid"] == held_by["uuid"]]
        row = {"title": old, "new_title": ARCHIVE_PREFIX + old, "uuid": (held_by or {}).get("uuid"), "presets": inside}
        if f and a:
            row["action"] = "conflict"
            blocking.append("Both %s and %s exist, so the folder cannot be archived by renaming it." % (old, row["new_title"]))
        elif f:
            row["action"] = "rename"
        elif a:
            row["action"] = "already"
        else:
            row["action"] = "missing"
            notes.append("The folder %s was not found; there is nothing to archive under that name." % old)
        archive.append(row)

    # ---- counts (the documented search, proven keys only; an object not made yet holds nobody)
    for p in planned:
        if p["action"] == "held":
            continue
        m = for_count(p["must"], have, today)
        p["count"] = 0 if m is None else reader.count(m)
    one = next((p for p in planned if p["title"] == AGENCY_QUEUE and p["action"] != "held"), None)
    if one:
        qm = for_count(one["must"], have, today)
        if qm is not None and "all_tags" not in qm:
            for t, u in tiers.items():
                if u:
                    agency["former"].append({"tag": t, "count": reader.count(dict(qm, all_tags=[u]))})

    if not have[("tag", TOP_TAG)]:
        notes.append("The Top 1000 tag does not exist yet, so every Priority preset is counted as it stands today: it "
                     "holds what its first-to-market source holds until the scoring track tags records.")
    elif top_row and reader.count({"any_tags": [top_row["uuid"]]}) == 0:
        notes.append("The Top 1000 tag exists but no record carries it yet, so every Priority preset holds what its "
                     "first-to-market source holds until the scoring track tags records.")
    if not have[("list", AGENCY_LIST)]:
        notes.append("The Agency list is new, so the Sent and Came Back presets hold nobody until the first export.")

    return _assemble(reader, day, today, acct, objects, fplan, planned, archive, agency, blocking, flags, notes,
                     {"or": proof_or, "sms_attempts": proof_sms, "custom_date": proof_date})


def _assemble(reader, day, today, acct, objects, fplan, planned, archive, agency, blocking, flags, notes, proofs) -> dict:
    acts = Counter(p["action"] for p in planned)
    return {
        "tool": "restructure", "plan_date": day or date.today().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"), "counted_as_of": today.isoformat(),
        "account": _account_of(acct["presets"]),
        "requests": {"made": reader.made, "from_cache": reader.from_cache, "budget": reader.max_requests,
                     "not_counted": reader.skipped, "stopped": reader.stopped, "errors": reader.errors[:20],
                     "notes": sorted(set(reader.notes))[:20]},
        "summary": {"presets_read": len(acct["presets"]), "folders_read": len(acct["folders"]),
                    "objects_to_create": sum(1 for o in objects if o["action"] == "create"),
                    "objects_to_switch_on": sum(1 for o in objects if o["action"] == "reactivate"),
                    "folders_to_create": sum(1 for f in fplan if f["action"] == "create"),
                    "presets_to_create": acts.get("create", 0), "presets_to_move": acts.get("move", 0),
                    "presets_already": acts.get("already", 0), "presets_held": acts.get("held", 0),
                    "folders_to_archive": sum(1 for a in archive if a["action"] == "rename"),
                    "folders_archived": sum(1 for a in archive if a["action"] == "already")},
        "blocking": blocking, "flags": flags, "notes": notes, "proofs": proofs, "agency": agency,
        "objects": objects, "folders": fplan, "presets": planned, "archive": archive,
        "table": {"resend_after_days": RESEND_AFTER_DAYS, "tier_tags": TIER_TAGS, "agency_drops": AGENCY_DROPS,
                  "agency_also_skips": AGENCY_ALSO_SKIPS},
    }


# ------------------------------------------------------------------------------------------ report
def _records(v) -> str:
    return pp._records(v)


def plan_lines(plan: dict) -> list[str]:
    """Every change the plan makes, one line each, with the records each new preset would hold."""
    out = []
    s = plan["summary"]
    out.append("Restructure plan for %s: %d presets in %d folders read, %d requests to the account."
               % (plan["plan_date"], s["presets_read"], s["folders_read"], plan["requests"]["made"]))
    if plan["blocking"]:
        out.append("BLOCKED, apply refuses while any of these stands:")
        out += ["  - %s" % b for b in plan["blocking"]]
    out.append("Anchor: %s" % plan["proofs"]["or"].get("say", ""))
    for o in plan["objects"]:
        verb = {"create": "Create", "exists": "Keep", "reactivate": "Switch back on", "conflict": "CONFLICT on"}.get(
            o["action"], o["action"])
        extra = " (group %s)" % o["group"] if o.get("group") else ""
        out.append("%s %s %s%s" % (verb, o["kind"].replace("_", " "), o["title"], extra))
    for f in plan["folders"]:
        out.append("%s folder %s" % ("Create" if f["action"] == "create" else "Keep", f["title"]))
    for p in plan["presets"]:
        verb = {"create": "Create", "already": "Already there:", "move": "Move", "held": "Held back:",
                "conflict": "CONFLICT:"}.get(p["action"], p["action"])
        where = " into %s" % p["folder"] if p["action"] in ("create", "move") else ""
        held = " (%s)" % p["held"] if p["action"] == "held" else ""
        out.append("%s %s%s: %s%s" % (verb, p["title"], where, "not counted" if p["count"] is None
                                       else _records(p["count"]), held))
    for a in plan["archive"]:
        if a["action"] == "rename":
            out.append("Rename folder %s to %s (%d presets keep their titles)" % (a["title"], a["new_title"],
                                                                                 len(a["presets"])))
        elif a["action"] == "already":
            out.append("Already archived: %s" % a["new_title"])
        elif a["action"] == "missing":
            out.append("Not found, nothing to archive: %s" % a["title"])
    return out


def render_md(plan: dict) -> str:
    s, pr, ag = plan["summary"], plan["proofs"], plan["agency"]
    out = []
    w = out.append
    w("# Two tracks: the change list for %s" % plan["plan_date"])
    w("")
    w("Nothing has been changed in the account. This list was written from a read-only pass on %s (%d requests to "
      "the account), with date windows worked out as of %s." % (plan["generated_at"].replace("T", " at "),
                                                               plan["requests"]["made"], plan["counted_as_of"]))
    w("")
    w("## The short version")
    w("")
    w("- %d presets in %d folders were read." % (s["presets_read"], s["folders_read"]))
    w("- %d objects to create (tag, list, date field), %d folders to create, %d presets to create, %d to move, %d "
      "already in place, %d held back." % (s["objects_to_create"], s["folders_to_create"], s["presets_to_create"],
                                           s["presets_to_move"], s["presets_already"], s["presets_held"]))
    w("- %d tier folders to archive by renaming (%d already archived). No preset title changes and nothing is "
      "deleted." % (s["folders_to_archive"], s["folders_archived"]))
    if plan["blocking"]:
        w("")
        w("### Blocked: apply refuses while any of these stands")
        w("")
        for b in plan["blocking"]:
            w("- %s" % b)
    w("")
    w("### Decide before you approve")
    w("")
    for f in plan["flags"]:
        w("- %s" % f)
    for x in ag.get("former") or []:
        w("- %s of the queue's records carry the tag %s from the old tiers. They go to the agency unless the Top 1000 "
          "takes them, because every record is in exactly one track." % (pp._n(x["count"]), x["tag"]))
    cd = pr["custom_date"]
    if ag.get("window"):
        w("- The queue skips records sent in the last %d days, not the whole Agency list, so a record sent more than "
          "%d days ago comes back. The search reads a custom date filter (proven on %s, spelled '%s'). The window is "
          "saved the way the app saves its own ('%d-days' back to 'today'); the search only takes fixed dates, so "
          "open the queue in the app once after apply and check its count." % (
              RESEND_AFTER_DAYS, RESEND_AFTER_DAYS, cd["field"], cd["shape"], RESEND_AFTER_DAYS))
    elif RESEND_AFTER_DAYS:
        w("- The %d-day resend window is not in the queue: %s. The queue skips the whole Agency list, so a sent record "
          "comes back only when someone takes it off the list." % (RESEND_AFTER_DAYS, cd.get("why") or "it was not proven"))
    w("- Records with no number are in the Agency track but in none of its presets: the queue holds records the "
      "agency can call. The archived Needs Skipped presets have no Agency replacement in this list.")
    w("- Records on the Obituary list stay out of the queue as they are out of Strong and Bulk today; they keep their "
      "own lane in 11. DEEP PROSPECTING.")
    for n in plan["notes"]:
        w("- %s" % n)
    w("")
    w("### How each rule was checked")
    w("")
    w("The search ignores a filter it does not know and still answers, so each new kind of filter was asked with and "
      "without it. A filter is used only when the count moved.")
    w("")
    w("| Check | Counts | Verdict |")
    w("|---|---|---|")
    for r in pr["or"].get("rows") or []:
        w("| Two tags in any_tags (FTM and %s) | FTM %s, %s %s, either %s | %s |" % (
            r["with"], pp._n(r["ftm"]), r["with"], pp._n(r["other"]), pp._n(r["either"]), r.get("verdict")))
    sm = pr["sms_attempts"]
    w("| The text counter at 0 | with a phone %s, of them never texted %s%s | %s |" % (
        pp._n(sm.get("without")), pp._n(sm.get("with")),
        ", texted %s" % pp._n(sm.get("with_opposite")) if "with_opposite" in sm else "",
        {True: "read", False: "NOT read", None: "not checked"}[sm.get("proven")]))
    for r in cd.get("rows") or []:
        w("| A date filter on %s spelled '%s' | without %s, any date %s, last %d days %s%s | %s |" % (
            cd["field"], r["shape"], pp._n(r.get("without")), pp._n(r.get("wide")), RESEND_AFTER_DAYS or 90,
            pp._n(r.get("narrow")), ", skipped %s" % pp._n(r["excluded"]) if "excluded" in r else "", r.get("verdict")))
    if not cd.get("rows"):
        w("| A custom date filter | - | not asked: %s |" % (cd.get("why") or ""))
    errs = plan["requests"].get("errors") or []
    if errs:
        w("")
        w("The search refused %d of these counts. Its first answer: %s" % (len(errs), errs[0][:300]))
    w("")
    w("## What gets made")
    w("")
    for o in plan["objects"]:
        line = "- %s %s %s: %s%s" % ({"create": "Create the", "exists": "Keep the", "reactivate": "Switch back on the",
                                      "conflict": "Conflict on the"}[o["action"]],
                                     o["kind"].replace("_", " "), o["title"], o["say"],
                                     ", in the group %s" % o["group"] if o.get("group") else "")
        w(line if line.endswith(".") else line + ".")
    for f in plan["folders"]:
        w("- %s the folder %s." % ("Create" if f["action"] == "create" else "Keep", f["title"]))
    folder = None
    for p in plan["presets"]:
        if p["folder"] != folder:
            folder = p["folder"]
            w("")
            w("### %s" % folder)
            w("")
        state = {"create": "new", "already": "already in place", "move": "moves here from %s" % p.get("from_folder"),
                 "held": "held back: %s" % p["held"], "conflict": "CONFLICT"}[p["action"]]
        w("**%s** (%s). %s." % (p["title"], state, "Not counted" if p["count"] is None
                                else "Holds %s today" % _records(p["count"])))
        for line in p["says"]:
            w("- %s." % line)
        w("")
    w("## Archive")
    w("")
    w("Each tier folder is renamed, so it sorts last and its presets keep their titles: the autopilot reads its "
      "presets by title, and a stage that reads an FTM preset keeps working.")
    w("")
    for a in plan["archive"]:
        if a["action"] == "rename":
            w("- %s becomes %s (%d presets)." % (a["title"], a["new_title"], len(a["presets"])))
        elif a["action"] == "already":
            w("- %s is already archived." % a["title"])
        elif a["action"] == "missing":
            w("- %s was not found." % a["title"])
        else:
            w("- %s: CONFLICT." % a["title"])
    w("")
    w("## When you approve")
    w("")
    w("The same tool sends exactly what is recorded with this list, and nothing else. It needs two switches set by a "
      "person (--commit and %s=1). It writes a backup of every folder and preset first, makes the tag, list and field, "
      "then the folders, then the presets, and renames the tier folders last. It reads back everything it makes and "
      "stops at the first difference. Every write goes in a log with what it replaced, and `undo` replays it "
      "backwards." % ALLOW_ENV)
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


def _log(path, row: dict) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(dict(row, at=datetime.now().isoformat(timespec="seconds"))) + "\n")


def _code(exc) -> int | None:
    return getattr(exc, "code", None)


def _gone(crm, path: str) -> bool:
    """True when the object no longer reads (a 404)."""
    try:
        crm.call(path)
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == 404:
            return True
        raise
    return False


class State:
    """The account as apply finds it, read fresh rather than from the plan: every folder with its
    presets (the backup and the checks), and the tags, lists, fields and groups."""

    def __init__(self, crm):
        self.crm = crm
        self.folders = [dict(f) for f in rows_of(crm.call(R_FOLDERS))]
        self.raw, self.presets = [], []
        for f in self.folders:
            rows = []
            for p in rows_of(crm.call(R_FOLDER_PRESETS % f["uuid"])):
                if not isinstance(p.get("filters"), dict):
                    p = crm.call(R_PRESET_ONE % p["uuid"]) or p
                rows.append(p)
                self.presets.append(_preset_row(p, f))
            self.raw.append({"folder": f, "presets": rows})
        self.tags = rows_of(crm.call(R_TAGS))
        self.lists = rows_of(crm.call(R_LISTS))
        self.fields = rows_of(crm.call(R_FIELDS))
        self.groups = rows_of(crm.call(R_GROUPS))

    def folder(self, title: str):
        return _exact(self.folders, title)[0]

    def preset(self, title: str):
        return next((p for p in self.presets if p["title"] == title), None)

    def account(self) -> str:
        return _account_of(self.presets)


def write_backup(state: State, backup_dir, label: str, day: str) -> Path:
    """Every folder and preset as apply found them, before its first write. A second run the same
    day keeps the first file: the first one is the true before."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    base = "presets_%s_%s_before_restructure" % (label, day)
    path, n = backup_dir / (base + ".json"), 2
    while path.exists():
        path, n = backup_dir / ("%s_%d.json" % (base, n)), n + 1
    path.write_text(json.dumps(state.raw, indent=1), encoding="utf-8", newline="\n")
    return path


def _steps(plan: dict) -> list[str]:
    out = ["%s %s %s" % ("switch on" if o["action"] == "reactivate" else "create", o["kind"], o["title"])
           for o in plan["objects"] if o["action"] in ("create", "reactivate")]
    out += ["create folder %s" % f["title"] for f in plan["folders"] if f["action"] == "create"]
    out += ["%s preset %s" % (p["action"], p["title"]) for p in plan["presets"] if p["action"] in ("create", "move")]
    out += ["rename folder %s" % a["title"] for a in plan["archive"] if a["action"] == "rename"]
    return out


def apply_plan(crm, plan: dict, *, commit: bool = False, log_path=None, backup_dir=BACKUP_DIR, label: str = "ty2",
               out=print) -> dict:
    """Send the approved plan. Dry unless both switches are on (a dry run touches nothing, not even
    to read). Every object is checked against the account as it stands, made only when missing,
    read back, and logged; the first difference stops the run."""
    if plan.get("tool") != "restructure":
        raise Refused("refused: this file is not a restructure plan")
    if plan.get("blocking"):
        raise Refused("refused: the plan has %d blocking item(s), the first: %s" % (len(plan["blocking"]),
                                                                                    plan["blocking"][0]))
    if ((plan.get("proofs") or {}).get("or") or {}).get("proven") is not True:
        raise Refused("refused: the search was not shown to read two tags in any_tags as either-or, so the Priority "
                      "anchor is unproven")
    allowed = may_write(commit)
    done = {"allowed": allowed, "made": [], "already": [], "skipped": [], "would": [], "backup": ""}
    if not allowed:
        done["would"] = _steps(plan)
        out("DRY: nothing sent and nothing read. %d steps would run. A real run needs --commit and %s=1."
            % (len(done["would"]), ALLOW_ENV))
        return done

    st = State(crm)
    done["backup"] = str(write_backup(st, backup_dir, label, plan.get("plan_date") or date.today().isoformat()))
    out("backup   %s" % done["backup"])
    if plan.get("account") and st.account() and st.account() != plan["account"]:
        raise Refused("refused: the key reads a different account from the one the plan was written for")

    def say(kind, text):
        done[kind].append(text)
        out("%-8s %s" % ({"made": "done", "already": "already", "skipped": "skipped"}[kind], text))

    problems = _preflight(st, plan)
    if problems:
        raise Refused("refused before any write: %s" % "; ".join(problems))

    have: dict = {("tag", FTM_TAG): (_exact(st.tags, FTM_TAG)[0] or {}).get("uuid")}
    have[("tag", TOP_TAG)] = _ensure_tag(crm, st, allowed, log_path, say)
    have[("list", AGENCY_LIST)] = _ensure_list(crm, st, allowed, log_path, say)
    have[("custom_field", SENT_FIELD)] = _ensure_field(crm, st, plan, allowed, log_path, say)
    folder_ids = {f["title"]: _ensure_folder(crm, st, f["title"], allowed, log_path, say) for f in plan["folders"]}
    for p in plan["presets"]:
        if p["action"] == "held":
            say("skipped", "%s (held back in the plan: %s)" % (p["title"], p["held"]))
            continue
        _ensure_preset(crm, st, p, have, folder_ids[p["folder"]], allowed, log_path, say)
    for a in plan["archive"]:
        if a["action"] != "missing":
            _ensure_archive(crm, st, a, allowed, log_path, say)
    return done


def _field_row(fields):
    return next((f for f in fields or [] if (f.get("label") or "").strip().lower() == SENT_FIELD.lower()
                 and (f.get("entity_type") or "property") == "property"), None)


def _agency_row(lists):
    row, twins = _exact(lists, AGENCY_LIST)
    return row or (twins[0] if len(twins) == 1 else None)      # list titles are unique whatever their case


def _have_now(st: State) -> dict:
    """The objects the presets name, as the account holds them right now (None when missing)."""
    field = _field_row(st.fields)
    return {("tag", FTM_TAG): (_exact(st.tags, FTM_TAG)[0] or {}).get("uuid"),
            ("tag", TOP_TAG): (_exact(st.tags, TOP_TAG)[0] or {}).get("uuid"),
            ("list", AGENCY_LIST): (_agency_row(st.lists) or {}).get("uuid"),
            ("custom_field", SENT_FIELD): ({"uuid": field.get("uuid"), "id": field.get("id"),
                                           "machine_name": field.get("machine_name")} if field else None)}


def _preflight(st: State, plan: dict) -> list[str]:
    """What would stop the run part way, found before its first write: a source preset that changed
    since the plan was read (its clone would carry the old filter) and a target title someone has
    taken since. A run that stops part way leaves the account half built, so both are asked first."""
    out: list[str] = []
    have = _have_now(st)
    for p in plan["presets"]:
        if p["action"] == "held":
            continue
        live = st.preset(p["title"])
        if live is not None:
            try:
                want = resolve(p["must"], have)
            except Refused:
                want = None
            if want is None or _must(live) != want:
                out.append("a preset named %s exists with a different filter" % p["title"])
            continue
        for s in p.get("sources") or []:
            src = st.preset(s["title"])
            if src is None or pp.sha(_must(src)) != s["sha"]:
                out.append("%s changed since the plan was written" % s["title"])
    if out:
        out.append("run plan again")
    return list(dict.fromkeys(out))


def _record_made(crm, log_path, made, row: dict, listing: str, idkey: str = "uuid", namekey: str = "title"):
    """Log what a write made the moment it answers, then find its id when the answer did not carry
    one, and log that too. The log line goes down before any further request, so a stop at the next
    request (the hourly budget, a dropped connection) never leaves an object undo does not know of."""
    uid = made.get(idkey) if isinstance(made, dict) else None
    _log(log_path, dict(row, **{idkey: uid}))
    if uid is None:
        hit = next((r for r in rows_of(crm.call(listing)) if str(r.get(namekey) or "").strip() == row["title"]), None)
        uid = (hit or {}).get(idkey)
        if uid is not None:
            _log(log_path, dict(row, **{idkey: uid}))
    return uid


def _ensure_tag(crm, st, allowed, log_path, say) -> str:
    row, _ = _exact(st.tags, TOP_TAG)
    if row:
        say("already", "tag %s" % TOP_TAG)
        return row["uuid"]
    made = _write(crm, allowed, R_TAG_NEW, "POST", {"title": TOP_TAG})
    uuid = _record_made(crm, log_path, made, {"step": "tag", "title": TOP_TAG}, R_TAGS)
    if not uuid or (crm.call(R_TAG_ONE % uuid) or {}).get("title") != TOP_TAG:
        raise Refused("stopped: the tag %s was sent but does not read back" % TOP_TAG)
    st.tags.append({"uuid": uuid, "title": TOP_TAG})
    say("made", "tag %s" % TOP_TAG)
    return uuid


def _ensure_list(crm, st, allowed, log_path, say) -> str:
    row = _agency_row(st.lists)
    if row:
        say("already", "list %s" % row.get("title"))
        return row["uuid"]
    made = _write(crm, allowed, R_LIST_NEW, "POST", {"title": AGENCY_LIST})
    uuid = _record_made(crm, log_path, made, {"step": "list", "title": AGENCY_LIST}, R_LISTS)
    back = crm.call(R_LIST_ONE % uuid) if uuid else {}
    if str((back or {}).get("title") or "").strip().lower() != AGENCY_LIST.lower():
        raise Refused("stopped: the list %s was sent but does not read back" % AGENCY_LIST)
    st.lists.append({"uuid": uuid, "title": AGENCY_LIST})
    say("made", "list %s" % AGENCY_LIST)
    return uuid


def _ensure_field(crm, st, plan, allowed, log_path, say) -> dict:
    row = _field_row(st.fields)
    if row:
        if (row.get("field_type") or "") != "date":
            raise Refused("stopped: a field named %s exists and is not a date field" % SENT_FIELD)
        if row.get("is_active") is False:
            # An undo switches the field off (the API cannot delete one), so a run after an undo
            # switches it back on rather than making a second field of the same name.
            _write(crm, allowed, R_FIELD_ONE % row["id"], "PATCH", {"is_active": True})
            _log(log_path, {"step": "field_on", "id": row["id"], "title": SENT_FIELD})
            if (crm.call(R_FIELD_ONE % row["id"]) or {}).get("is_active") is False:
                raise Refused("stopped: the field %s was switched on but still reads off" % SENT_FIELD)
            row["is_active"] = True
            say("made", "switched the date field %s back on" % SENT_FIELD)
        else:
            say("already", "date field %s" % SENT_FIELD)
        return {"uuid": row.get("uuid"), "id": row.get("id"), "machine_name": row.get("machine_name")}
    planned = next((o for o in plan["objects"] if o["kind"] == "custom_field"), {}) or {}
    group, _ = _exact(st.groups, planned.get("group") or SENT_FIELD_GROUP, key="label")
    if not group:
        raise Refused("stopped: the custom field group %s is gone, so the field has nowhere to go"
                      % (planned.get("group") or SENT_FIELD_GROUP))
    body = {"label": SENT_FIELD, "field_type": "date", "entity_type": "property", "group_id": group.get("id")}
    made = _write(crm, allowed, R_FIELD_NEW, "POST", body)
    fid = _record_made(crm, log_path, made, {"step": "field", "title": SENT_FIELD}, R_FIELDS, idkey="id",
                       namekey="label")
    back = (crm.call(R_FIELD_ONE % fid) if fid is not None else {}) or {}
    if not (back.get("label") == SENT_FIELD and back.get("field_type") == "date"
            and back.get("entity_type", "property") == "property"):
        raise Refused("stopped: the field %s was sent but does not read back as a date field" % SENT_FIELD)
    st.fields.append(back)
    say("made", "date field %s in %s" % (SENT_FIELD, group.get("label")))
    return {"uuid": back.get("uuid"), "id": fid, "machine_name": back.get("machine_name")}


def _ensure_folder(crm, st, title, allowed, log_path, say) -> str:
    f = st.folder(title)
    if f:
        say("already", "folder %s" % title)
        return f["uuid"]
    made = _write(crm, allowed, R_FOLDER_NEW, "POST", {"title": title, "type": "properties", "permissions": []})
    uuid = _record_made(crm, log_path, made, {"step": "folder", "title": title}, R_FOLDERS)
    if not uuid or (crm.call(R_FOLDER_ONE % uuid) or {}).get("title") != title:
        raise Refused("stopped: the folder %s was sent but does not read back" % title)
    st.folders.append({"uuid": uuid, "title": title})
    say("made", "folder %s" % title)
    return uuid


def _ensure_preset(crm, st, p, have, folder_uuid, allowed, log_path, say) -> None:
    must = resolve(p["must"], have)
    live = st.preset(p["title"])
    if live is not None:
        if _must(live) != must:
            raise Refused("stopped: a preset named %s exists with a different filter; run plan again" % p["title"])
        if live["folder_uuid"] == folder_uuid:
            say("already", "preset %s" % p["title"])
            return
        _write(crm, allowed, R_PRESET_ONE % live["uuid"], "PATCH", {"folder": folder_uuid})
        _log(log_path, {"step": "move", "uuid": live["uuid"], "title": p["title"], "before": live["folder_uuid"],
                        "after": folder_uuid})
        back = crm.call(R_PRESET_ONE % live["uuid"]) or {}
        if _folder_of(back) != folder_uuid or _must(back) != must:
            raise Refused("stopped: %s was moved but does not read back in its new folder" % p["title"])
        live["folder_uuid"] = folder_uuid
        say("made", "moved preset %s into %s" % (p["title"], p["folder"]))
        return
    filters = {"must": must}
    if p.get("account"):
        filters["account"] = p["account"]
    made = _write(crm, allowed, R_PRESET_NEW, "POST", {"title": p["title"], "folder": folder_uuid,
                                                       "quick_filter": bool(p.get("quick_filter")), "filters": filters,
                                                       "type": "properties"})
    row = {"step": "preset", "title": p["title"], "folder": folder_uuid, "must": must}
    uuid = _record_made(crm, log_path, made, row, R_FOLDER_PRESETS % folder_uuid)
    back = (crm.call(R_PRESET_ONE % uuid) if uuid else {}) or {}
    if _must(back) != must or _folder_of(back) != folder_uuid:
        if uuid:
            _log(log_path, dict(row, uuid=uuid, must=_must(back)))     # what it stored, so undo can take it away
        raise Refused("stopped: %s was created but reads back differently from what was sent; nothing after it was "
                      "touched" % p["title"])
    st.presets.append({"uuid": uuid, "title": p["title"], "folder": p["folder"], "folder_uuid": folder_uuid,
                       "quick_filter": bool(p.get("quick_filter")), "filters": filters})
    say("made", "preset %s (%s)" % (p["title"], "not counted" if p.get("count") is None else _records(p["count"])))


def _ensure_archive(crm, st, a, allowed, log_path, say) -> None:
    f, z = st.folder(a["title"]), st.folder(a["new_title"])
    if z and not f:
        say("already", "folder %s" % a["new_title"])
        return
    if f and z:
        raise Refused("stopped: both %s and %s exist now" % (a["title"], a["new_title"]))
    if not f:
        say("skipped", "folder %s (not found)" % a["title"])
        return
    _write(crm, allowed, R_FOLDER_ONE % f["uuid"], "PATCH", {"title": a["new_title"]})
    _log(log_path, {"step": "rename", "uuid": f["uuid"], "before": a["title"], "after": a["new_title"]})
    if (crm.call(R_FOLDER_ONE % f["uuid"]) or {}).get("title") != a["new_title"]:
        raise Refused("stopped: %s was renamed but does not read back as %s" % (a["title"], a["new_title"]))
    f["title"] = a["new_title"]
    say("made", "archived %s" % a["new_title"])


# --------------------------------------------------------------------------------------------- undo
def undo(crm, log_path, *, commit: bool = False, out=print) -> dict:
    """Put the logged writes back, newest first. A created preset that someone has edited since,
    a folder that now holds presets, and a tag or list that records now carry are left where they
    are and named, because taking them away would undo someone else's work too. A custom field
    cannot be deleted through the API, so it is switched off instead."""
    rows = [json.loads(line) for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    allowed = may_write(commit)
    done = {"allowed": allowed, "restored": [], "left": [], "would": [], "stopped": ""}
    seen: set = set()
    for row in reversed(rows):
        key = _undo_key(row)
        if key in seen:
            continue                     # one object logged twice (its id found later, or what it stored)
        seen.add(key)
        what = "%s %s" % (row.get("step"), row.get("title") or row.get("after") or row.get("uuid"))
        if not allowed:
            done["would"].append(what)
            out("would undo %s" % what)
            continue
        try:
            left = _undo_one(crm, row, allowed)
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


def _undo_key(row: dict) -> tuple:
    if row.get("step") in ("rename", "move"):
        return (row.get("step"), row.get("uuid"))
    return (row.get("step"), row.get("title"), row.get("folder") or "")


def _find(crm, row: dict):
    """The id of a logged object whose write answered without one, looked up by its title."""
    step, title = row.get("step"), row.get("title")
    if step == "preset":
        return next((r.get("uuid") for r in rows_of(crm.call(R_FOLDER_PRESETS % row["folder"]))
                     if r.get("title") == title), None)
    if step == "field":
        return (_field_row(rows_of(crm.call(R_FIELDS))) or {}).get("id")
    listing = {"folder": R_FOLDERS, "tag": R_TAGS, "list": R_LISTS}.get(step)
    if not listing:
        return None
    hit, twins = _exact(rows_of(crm.call(listing)), title)
    if not hit and step == "list" and len(twins) == 1:
        hit = twins[0]
    return (hit or {}).get("uuid")


def _undo_one(crm, row: dict, allowed: bool) -> str:
    """Undo one logged write. Returns '' when the account is back as it was, else why it was left."""
    step = row.get("step")
    uid = row.get("id") if step in ("field", "field_on") else row.get("uuid")
    if uid is None and step in ("preset", "folder", "tag", "list", "field"):
        uid = _find(crm, row)
        if uid is None:
            return ""                    # it is not there: nothing to take away
    if step == "rename":
        cur = (crm.call(R_FOLDER_ONE % uid) or {}).get("title")
        if cur == row["before"]:
            return ""
        if cur != row["after"]:
            return "renamed by someone since (it reads %r)" % cur
        _write(crm, allowed, R_FOLDER_ONE % uid, "PATCH", {"title": row["before"]})
        if (crm.call(R_FOLDER_ONE % uid) or {}).get("title") != row["before"]:
            raise Refused("the old title did not read back")
        return ""
    if step == "move":
        if _folder_of(crm.call(R_PRESET_ONE % uid)) == row["before"]:
            return ""
        _write(crm, allowed, R_PRESET_ONE % uid, "PATCH", {"folder": row["before"]})
        if _folder_of(crm.call(R_PRESET_ONE % uid)) != row["before"]:
            raise Refused("the preset did not read back in its old folder")
        return ""
    if step == "preset":
        try:
            cur = crm.call(R_PRESET_ONE % uid) or {}
        except Exception as exc:  # noqa: BLE001
            if _code(exc) == 404:
                return ""
            raise
        if _must(cur) != row.get("must"):
            return "its filter was edited since it was made"
        _write(crm, allowed, R_PRESET_ONE % uid, "DELETE")
        if not _gone(crm, R_PRESET_ONE % uid):
            raise Refused("the preset still reads after the delete")
        return ""
    if step == "folder":
        inside = rows_of(crm.call(R_FOLDER_PRESETS % uid))
        if inside:
            return "it holds %d preset(s) now" % len(inside)
        _write(crm, allowed, R_FOLDER_ONE % uid, "DELETE")
        if not _gone(crm, R_FOLDER_ONE % uid):
            raise Refused("the folder still reads after the delete")
        return ""
    if step in ("tag", "list"):
        key, one = ("any_tags", R_TAG_ONE) if step == "tag" else ("any_lists", R_LIST_ONE)
        n = crm.count({key: [uid]})
        if n:
            return "%d record(s) carry it now" % n
        _write(crm, allowed, one % uid, "DELETE")
        if not _gone(crm, one % uid):
            raise Refused("it still reads after the delete")
        return ""
    if step in ("field", "field_on"):
        _write(crm, allowed, R_FIELD_ONE % uid, "PATCH", {"is_active": False})
        if (crm.call(R_FIELD_ONE % uid) or {}).get("is_active") is not False:
            raise Refused("the field did not read back as switched off")
        return ""
    return "not a step this tool writes"


# ---------------------------------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    from agents.autopilot.budget import BudgetExhausted
    ap = argparse.ArgumentParser(description="Two tracks for the marketing presets: plan (read-only), apply and undo "
                                             "(each needs --commit and %s=1)." % ALLOW_ENV)
    sub = ap.add_subparsers(dest="cmd")
    pl = sub.add_parser("plan", help="read the account, prove the filters, count, write the change list (the default)")
    pl.add_argument("--date", default="", help="the day the plan is for, YYYY-MM-DD (default today)")
    pl.add_argument("--out-dir", default=str(OUT_DIR))
    pl.add_argument("--cache", default="", help="a file that keeps listings and counts, so a re-run does not re-read")
    pl.add_argument("--max-requests", type=int, default=450)
    pl.add_argument("--gap", type=float, default=2.0, help="seconds between requests (2.0 is 30 a minute)")
    pl.add_argument("--as-of", default="", help="the day date windows are worked out from (default today)")
    aq = sub.add_parser("apply", help="send an approved plan; dry unless --commit and %s=1" % ALLOW_ENV)
    aq.add_argument("--plan", required=True)
    aq.add_argument("--commit", action="store_true")
    aq.add_argument("--gap", type=float, default=2.0)
    aq.add_argument("--label", default="ty2", help="the account's short name in the backup file name")
    uq = sub.add_parser("undo", help="replay an apply log backwards; dry unless --commit and %s=1" % ALLOW_ENV)
    uq.add_argument("--log", required=True)
    uq.add_argument("--commit", action="store_true")
    uq.add_argument("--gap", type=float, default=2.0)
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] not in ("plan", "apply", "undo", "-h", "--help"):
        raw = ["plan"] + raw
    args = ap.parse_args(raw)
    if args.cmd in ("apply", "undo") and args.commit and not may_write(True):
        print("refused: --commit was passed but %s is not 1. Nothing was sent." % ALLOW_ENV)
        return 3
    try:
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
