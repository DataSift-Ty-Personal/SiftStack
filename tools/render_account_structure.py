#!/usr/bin/env python3
"""Render the account blueprint as a step-by-step guide: docs/account-structure.md.

The guide is generated, never edited by hand. The folders, presets, filters, boards, sequences and task
presets come from the newest blueprint the account-blueprint skill ships
(skills/account-blueprint/blueprints/account_*.json); which SiftStack stage works each preset comes from
team/autopilot.yaml, and who owns each stage from team/roster.yaml. After any change to the account:

    python src/clone_account.py --phase export          # a new blueprint in output/blueprints/
    python tools/sync_account_blueprint_skill.py         # copy it into the skill
    python tools/render_account_structure.py             # rewrite this guide
    python tools/render_account_structure.py --check     # exit 1 when the guide is out of date
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINTS = ROOT / "skills" / "account-blueprint" / "blueprints"
PLAYBOOK = ROOT / "team" / "autopilot.yaml"
ROSTER = ROOT / "team" / "roster.yaml"
OUT = ROOT / "docs" / "account-structure.md"

# What each folder is for, by its name without the number. Written by hand; the rest of the guide is not.
FOLDER_NOTES = {
    "FORECLOSURE - TEXT": "First-to-market foreclosures (tag FTM, Foreclosure list) waiting on their opening texts. "
                          "The autopilot loads texts 1 to 3 into Smarter Contact on days 0 to 2, so a record leaves "
                          "Ready to Text once its first text goes out.",
    "FORECLOSURE - CALL": "The foreclosure call round, step by step on the dialer's own attempt counter: needs a skip "
                          "trace, traced with no numbers, ready to call, call attempts 1 to 4, and the hot auction "
                          "list of sales inside 45 days.",
    "FORECLOSURE - MAIL": "Two mail runs for foreclosures: the weekly letters that count down to the sale date "
                          "(FTM - Weekly 01 to 06) and the monthly evergreen letters (FTM - Mail 01 to 06).",
    "ESTATES - TEXT": "Probates waiting on their opening texts, written to the personal representative, never "
                      "about the death or the court.",
    "ESTATES - CALL": "One call round for the estates: probates, and obituary records once they are 30 days past "
                      "the death (the tag Obituary 30+ Days).",
    "ESTATES - MAIL": "Letters to the personal representative, counted from the probate filing (at once, two "
                      "follow-ups, then monthly to month 24), and the obituary records ready for a family letter.",
    "CURATIVE TITLE": "Heir houses worked on the Curative Title board: every living heir called over five days, the "
                      "heir report on the record, no texts. Curative - All holds every tagged house; Ready to Call "
                      "is the dial list.",
    "TOP 1000 - TEXT": "The month's top 1,000 owners by deal odds, plus county records always put in (liens, tax "
                       "sale and tax delinquent notices, condemned houses, evictions). Opening texts come first.",
    "TOP 1000 - CALL": "The Top 1,000 call round, step by step on the dialer's own attempt counter.",
    "TOP 1000 - MAIL": "Monthly mail steps for the Top 1,000. Mail runs on the first-to-market records only, so "
                       "these wait.",
    "AGENCY": "Everyone else in the buy box. Each month the top 5,000 owners not sent in 90 days go to the calling "
              "agency from Agency - 01 Queue; Sent and Came Back track the file.",
    "DEEP PROSPECTING (ALL TIERS)": "Records that need research or heir work before anyone can reach the owner, "
                                    "from every flow, plus Obituary - All (every obituary record, any age).",
    "REACTIVATION": "Second chances: a Not Interested record comes back after 30 days (foreclosures, probates, the "
                    "Top 1,000) or 90 days (the rest), and records that never answered return in the rehash dial "
                    "file.",
    "CLEAN UP": "Upkeep views: traced records with no number, and sold houses kept out of marketing.",
    "Lead Management": "The lead manager's task views, one per stage of the Lead Management board, plus tax sale "
                       "owners who can still redeem.",
    "Acquisitions": "Offer and follow-up task views for the acquisitions board.",
    "Transactions": "Contract and closing task views for the transactions board.",
    "My Tasks": "Each person's own overdue, due today and future tasks.",
    "QC": "Quality checks on the boards: cards with no next task, overdue tasks, more than one open task, no "
          "confirmed number.",
    "CALLER QUEUES": "Each caller's own working views: what is assigned, what is being prospected, what has not "
                     "been started.",
    "Dispo Sequential Marketing": "Buyer outreach for a deal under contract: buyers ready to text, texted, ready "
                                  "to call, and call attempts.",
}

POINTS = {"today": "today", "yesterday": "yesterday", "week": "a week ago", "month": "a month ago",
          "quarter": "a quarter ago", "year": "a year ago"}
TRIGGERS = {"property.tags.added": "a tag is added", "property.tags.removed": "a tag is removed",
            "property.lists.added": "a list is added", "property.lists.removed": "a list is removed",
            "property.status.updated": "the status changes", "property.assignee.updated": "the assignee changes",
            "task.created": "a task is created", "task.completed": "a task is completed",
            "card.created": "a board card is created", "card.moved": "a board card moves"}
SHARED_MIN_LEN = 6      # a value list this long, used by two or more presets, is named once
SHARED_MIN_USES = 2
# Stage kinds that only read a preset to count or report on it: they are not what works the records.
REPORT_KINDS = {"digest", "alarms", "goals", "kpitracker", "contactrate", "conventions", "accountchecks", "laddercheck",
                "cadence", "bizaudit", "lostdeals", "suppression", "researchstats", "dealsource", "capture", "audit",
                "limits", "watchdog"}
# Stages that work a folder's records from a tag or a board instead of naming one of its presets.
EXTRA_STAGES = {"CURATIVE TITLE": ["curative_pipeline", "heir_lane"]}


def newest_blueprint() -> Path:
    found = sorted(glob.glob(str(BLUEPRINTS / "account_*.json")))
    if not found:
        raise SystemExit("no blueprint in %s" % BLUEPRINTS)
    return Path(found[-1])


def title_of(v) -> str:
    if isinstance(v, dict) and "$ref" in v:
        t = str(v.get("title") or "")
        return "%s / %s" % (v["board"], t) if v.get("board") else t
    if isinstance(v, dict) and "$unresolved" in v:
        return "(an item that exists only in the source account)"
    return str(v)


def names(vals) -> list[str]:
    return [title_of(v) for v in (vals or [])]


def span(v) -> str:
    """A counter range [low, high] in words."""
    if isinstance(v, list) and len(v) == 2:
        lo, hi = v
        if lo == hi:
            return str(lo)
        if hi in (None, ""):
            return "%s or more" % lo
        if lo in (None, ""):
            return "up to %s" % hi
        return "%s to %s" % (lo, hi)
    return str(v)


def window(v) -> str:
    """A date filter: a relative window [N-units, point] or two dates."""
    if isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v):
        back, point = v
        if point in POINTS and "-" in back:
            n, unit = back.split("-", 1)
            return "from %s %s before %s up to %s" % (n, unit, POINTS[point], POINTS[point])
        return "from %s to %s" % (back, point)
    return json.dumps(v)


class Shared:
    """Long value lists many presets repeat (the standard exclusions), named once."""

    def __init__(self, presets: list[dict]):
        use = Counter()
        for p in presets:
            for key, vals in _value_lists(p):
                if len(vals) >= SHARED_MIN_LEN:
                    use[(key, tuple(vals))] += 1
        keep = sorted((k for k, n in use.items() if n >= SHARED_MIN_USES), key=lambda k: (k[0], -use[k], k[1]))
        self.names = {}
        self.sets = []
        for i, (key, vals) in enumerate(keep, 1):
            label = "Exclusion set %d" % i if key.startswith("must_not.") else "Value set %d" % i
            self.names[(key, vals)] = label
            self.sets.append((label, key, list(vals), use[(key, vals)]))

    def say(self, key: str, vals: list[str]) -> str:
        label = self.names.get((key, tuple(vals)))
        return label if label else ", ".join(vals)


def _value_lists(p: dict):
    must = ((p.get("filters") or {}).get("must")) or {}
    for k, v in must.items():
        if k == "must_not" and isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, list) and k2 != "any_neighborhood":
                    yield "must_not." + k2, names(v2)
        elif isinstance(v, list) and k.startswith(("any_", "all_")):
            yield k, names(v)


LIST_WORDS = {
    "any_tags": "has any of these tags", "all_tags": "has every one of these tags",
    "any_lists": "is on any of these lists", "all_lists": "is on every one of these lists",
    "any_property_status": "status is one of", "any_phone_status": "has a number marked",
    "any_boards": "has a card on", "assigned_to": "is assigned to",
    "any_mail_campaign_situation": "mail campaign situation is one of",
}
NOT_WORDS = {
    "any_tags": "does not have the tags", "any_lists": "is not on the lists",
    "any_property_status": "status is not", "any_phone_status": "has no number marked",
}


def filter_lines(p: dict, shared: Shared) -> list[str]:
    must = ((p.get("filters") or {}).get("must")) or {}
    out = []
    for k, v in must.items():
        if k == "must_not":
            continue
        if k in LIST_WORDS and isinstance(v, list):
            out.append("%s: %s" % (LIST_WORDS[k], shared.say(k, names(v))))
        elif k == "assigned_to":
            out.append("is assigned to %s" % title_of(v))
        elif k == "phone":
            out.append("has a phone number" if v else "has no phone number")
        elif k == "skiptraced":
            out.append("has been skip traced" if v else "has not been skip traced")
        elif k == "sms_attempts":
            out.append("texts sent: %s" % span(v))
        elif k == "predictivecall_attempts":
            out.append("dialer call attempts: %s" % span(v))
        elif k == "directmail_attempts":
            out.append("mail pieces sent: %s" % span(v))
        elif k == "owner_vacant":
            out.append("the owner's mailing address is %sflagged vacant" % ("" if v else "not "))
        elif k == "do_not_mail_ever":
            out.append("%smarked do not mail" % ("" if v else "not "))
        elif k == "last_direct_mailed":
            out.append("last mailed %s" % window(v))
        elif k == "last_obituary_date":
            out.append("obituary date %s" % window(v))
        elif k == "last_updated_date":
            if isinstance(v, list) and v and all(isinstance(x, dict) and "options" in x for x in v):
                for x in v:
                    out.append("the %s last changed %s" % (x.get("field") or "record", window(x.get("options"))))
            else:
                out.append("last updated %s" % window(v))
        elif k == "ownerPropertiesOwned":
            if isinstance(v, dict) and v.get("show_properties") == "one_per_owner":
                out.append("shows one property per owner")
            else:
                out.append("owner's property count: %s" % (span(v) if isinstance(v, list) else json.dumps(v)))
        elif k in ("tasks", "tasks_incomplete"):
            out.append("%s: %s" % ("tasks" if k == "tasks" else "open tasks", span(v) if isinstance(v, list) else json.dumps(v)))
        else:
            out.append("`%s`: %s" % (k, json.dumps(v, default=title_of)))
    nots = must.get("must_not") if isinstance(must.get("must_not"), dict) else {}
    for k, v in nots.items():
        if k == "any_neighborhood":
            out.append("leaves out %d neighborhoods of the source market (left out when the blueprint is applied)" % len(v or []))
        elif k in NOT_WORDS and isinstance(v, list):
            out.append("%s: %s" % (NOT_WORDS[k], shared.say("must_not." + k, names(v))))
        else:
            out.append("not `%s`: %s" % (k, json.dumps(v, default=title_of)))
    return out


def playbook_users(pb: dict, roster: dict) -> dict:
    """preset title -> [(stage id, owner name, mode, summary)] for every stage whose block names it."""
    owners = {}
    for eid, e in ((roster or {}).get("employees") or {}).items():
        owners[eid] = (e or {}).get("display_name") or (e or {}).get("name") or eid
    users = defaultdict(list)

    def strings(o):
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from strings(v)
        elif isinstance(o, list):
            for v in o:
                yield from strings(v)
    for sid, st in ((pb or {}).get("stages") or {}).items():
        if str(st.get("mode")) == "off" or st.get("kind") in REPORT_KINDS:
            continue
        seen = set()
        for s in strings({k: v for k, v in st.items() if k not in ("summary",)}):
            if s not in seen:
                seen.add(s)
                users[s].append((sid, owners.get(st.get("owner"), st.get("owner")), str(st.get("mode")), st.get("summary") or ""))
    return users


def stage_info(pb: dict, roster: dict) -> dict:
    owners = {eid: (e or {}).get("display_name") or eid for eid, e in ((roster or {}).get("employees") or {}).items()}
    out = {}
    for sid, st in ((pb or {}).get("stages") or {}).items():
        sched = st.get("schedule")
        out[sid] = {"owner": owners.get(st.get("owner"), st.get("owner")), "mode": str(st.get("mode")),
                    "summary": (st.get("summary") or "").strip(),
                    "schedule": "; ".join(sched) if isinstance(sched, list) else str(sched or "")}
    return out


def refs_of(o, kind: str) -> set:
    found = set()
    if isinstance(o, dict):
        if o.get("$ref") == kind and o.get("title"):
            found.add(str(o["title"]))
        for v in o.values():
            found |= refs_of(v, kind)
    elif isinstance(o, list):
        for v in o:
            found |= refs_of(v, kind)
    return found


def short(folder: str) -> str:
    head, _, rest = folder.partition(". ")
    return rest if head.isdigit() and rest else folder


def _place(v) -> str:
    """A board card's place, {board, column} or a column ref, in words."""
    if isinstance(v, dict) and "column" in v:
        return title_of(v.get("column"))
    return title_of(v)


def _few(vals: list[str], most: int = 5) -> str:
    return ", ".join(vals) if len(vals) <= most else "%s and %d more" % (", ".join(vals[:most]), len(vals) - most)


def sequence_line(s: dict) -> str:
    when = TRIGGERS.get(s.get("trigger"), "`%s`" % s.get("trigger"))
    conds = []
    for c in s.get("conditions") or []:
        kind, pay = c.get("condition"), c.get("payload") or {}
        field = str(pay.get("field") or "")
        if kind == "from_to" and field == "status":
            conds.append("the new status is %s" % title_of(pay.get("updated_to")))
        elif kind == "from_to" and field == "column":
            conds.append("the card lands in %s" % _place(pay.get("updated_to")))
        elif kind == "equals_to" and field == "title":
            conds.append("the task is %s" % title_of(pay.get("value")))
        elif field == "tags_uuid":
            conds.append("the record has the tag %s" % ", ".join(names(pay.get("values"))))
        else:
            vals = names(pay.get("values")) or [title_of(pay.get("value") or pay.get("updated_to") or "")]
            conds.append("%s is %s" % (field.replace("_uuid", ""), ", ".join(v for v in vals if v)))
    acts = []
    for a in s.get("actions") or []:
        name, pay = a.get("action"), a.get("payload") or {}
        vals = pay.get("values")
        if "$manual" in a or "$manual" in pay:
            acts.append("a %s step to re-add with your own integration" % (name or "message"))
        elif name == "set-field-value":
            val = pay.get("value")
            fld = {"assigned_to": "assignee"}.get(str(pay.get("field") or "").replace("prop.", ""),
                                                  str(pay.get("field") or "").replace("prop.", ""))
            acts.append("clear the %s" % fld if val in (None, "") else "set the %s to %s" % (fld, title_of(val)))
        elif name == "remove":
            what = str(pay.get("field") or "items")
            acts.append("remove the %s %s" % (what, _few(names(vals if isinstance(vals, list) else []))))
        elif name == "property-tasks-delete-all":
            acts.append("delete all its tasks")
        elif name == "property-assign":
            acts.append("assign it to %s" % title_of(pay.get("value")))
        elif name == "create-task-by-preset":
            acts.append("create the task %s" % title_of(pay.get("task_preset")))
        elif name == "create-siftline-card":
            acts.append("put a card in %s" % _place(vals or {}))
        elif name == "move-siftline-card":
            acts.append("move the card to %s" % _place((vals or {}).get("to") if isinstance(vals, dict) else vals))
        elif name == "send-webhook":
            acts.append("send the webhook %s (made in the app for each account)" % (pay.get("webhook_name") or ""))
        elif name in ("add-lists", "remove-lists", "add-tags", "remove-tags"):
            verb, what = name.split("-")
            acts.append("%s the %s %s" % (verb, what, _few(names(vals or pay.get(what) or []))))
        else:
            acts.append(str(name).replace("-", " "))
    text = "When %s" % when
    if conds:
        text += " and %s" % "; ".join(conds)
    return text + ": " + ("; ".join(acts) or "no action") + "."


def render(bp_path: Path) -> str:
    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    pb = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8")) if PLAYBOOK.exists() else {}
    roster = yaml.safe_load(ROSTER.read_text(encoding="utf-8")) if ROSTER.exists() else {}
    users = playbook_users(pb, roster)
    info = stage_info(pb, roster)
    credited = []
    folders = [f for f in bp.get("preset_folders") or [] if f.get("presets")]
    presets = [p for f in folders for p in f["presets"]]
    shared = Shared(presets)
    day = bp_path.stem.split("_")[-1] if bp_path.stem.count("-") == 2 else str(bp.get("exported_at") or "")[:10]
    rel = bp_path.relative_to(ROOT).as_posix()
    L = []
    w = L.append
    w("# How the account is set up, step by step")
    w("")
    w("This is the DataSift account SiftStack runs on, exported %s: %d folders and %d presets, %d boards, "
      "%d sequences and %d task presets. It is generated from the blueprint `%s` and the autopilot playbook "
      "`team/autopilot.yaml`, so it matches the account as it was exported. Do not edit it by hand; see the "
      "last section to regenerate it." % (day, len(folders), len(presets), len(bp.get("boards") or []),
                                         len(bp.get("sequences") or []), len(bp.get("task_presets") or []), rel))
    w("")
    w("Every record sits in one flow, and each flow has its own text, call and mail folders. The folders are "
      "numbered in the order the team works them, and DataSift sorts folders by name, so the number is the order.")
    w("")
    w("## At a glance")
    w("")
    w("| Folder | Presets | What it is for | Worked on its own by |")
    w("| --- | --- | --- | --- |")
    for f in folders:
        stages = []
        ids = [sid for p in f["presets"] for sid, _, _, _ in users.get(p["title"], [])]
        ids += [sid for sid in EXTRA_STAGES.get(short(f["title"]), []) if sid in info and info[sid]["mode"] != "off"]
        for sid in ids:
            owner, mode = info[sid]["owner"], info[sid]["mode"]
            tag = "%s (%s)" % (sid, owner if mode == "live" else "%s, %s" % (owner, mode))
            if tag not in stages:
                stages.append(tag)
            if sid not in credited:
                credited.append(sid)
        w("| %s | %d | %s | %s |" % (f["title"], len(f["presets"]), FOLDER_NOTES.get(short(f["title"]), ""),
                                    ", ".join(stages[:6]) + (" and %d more" % (len(stages) - 6) if len(stages) > 6 else "")
                                    or "people, in the app"))
    w("")
    w("## Build it in your own account")
    w("")
    w("The fastest way is the `account-blueprint` skill, which creates all of it from the blueprint in one run "
      "and reads every object back:")
    w("")
    w("```bash")
    w("cd skills/account-blueprint/scripts")
    w("python clone_account.py --phase plan  --blueprint ../blueprints/%s --target you@example.com --jwt '<paste>'" % bp_path.name)
    w("python clone_account.py --phase apply --blueprint ../blueprints/%s --target you@example.com --jwt '<paste>' --commit" % bp_path.name)
    w("python clone_account.py --phase verify --blueprint ../blueprints/%s --target you@example.com --jwt '<paste>'" % bp_path.name)
    w("```")
    w("")
    w("By hand, work through the steps below in order: every later step names things the earlier ones create.")
    w("")
    w("## Step 1: statuses, lists and tags")
    w("")
    custom = [str(s.get("title")) for s in bp.get("statuses") or [] if not s.get("system")]
    w("**Statuses:** DataSift's built-in statuses, plus %d custom ones to add: %s." % (len(custom), ", ".join(custom)))
    w("")
    used = refs_of({"p": bp.get("preset_folders"), "s": bp.get("sequences"), "t": bp.get("task_presets"),
                    "m": bp.get("siftmap_presets")}, "list")
    # A SiftMap preset names its lists as plain titles, not references.
    used |= {str(t) for m in bp.get("siftmap_presets") or [] for t in (m.get("lists") or []) if isinstance(t, str)}
    lists = [str(x.get("title")) for x in bp.get("lists") or [] if str(x.get("title")) in used]
    w("**Lists** the presets, sequences and SiftMap presets use (%d): %s. The source account holds %d more for "
      "one-off pulls; they are in the blueprint but nothing here depends on them." % (
          len(lists), ", ".join(lists), len(bp.get("lists") or []) - len(lists)))
    w("")
    w("**Tags** the presets and sequences use (%d): %s." % (len(bp.get("tags") or []), ", ".join(str(x.get("title")) for x in bp.get("tags") or [])))
    w("")
    w("## Step 2: custom fields")
    w("")
    groups = defaultdict(list)
    for cf in bp.get("custom_fields") or []:
        g = cf.get("group")
        groups[title_of(g) if g else "No group"].append("%s (%s)" % (cf.get("label"), cf.get("field_type")))
    for g, fields in groups.items():
        w("- **%s:** %s" % (g, ", ".join(fields)))
    w("")
    w("## Step 3: the folders, in order")
    w("")
    for i, f in enumerate(folders, 1):
        w("%d. %s" % (i, f["title"]))
    w("")
    w("## Step 4: the presets, folder by folder")
    w("")
    w("Each preset lists its filter in plain words. A counter such as texts sent or dialer call attempts is "
      "DataSift's own counter, so a record moves from one preset to the next as the counter climbs. A date window "
      "such as \"from 36 months before a week ago up to a week ago\" is DataSift's relative date: it moves with "
      "the calendar. Long exclusion lists that many presets share are named once in Step 5.")
    w("")
    for f in folders:
        w("### %s" % f["title"])
        w("")
        note = FOLDER_NOTES.get(short(f["title"]))
        if note:
            w(note)
            w("")
        for p in f["presets"]:
            w("**%s**%s" % (p["title"], " (a quick filter)" if p.get("quick_filter") else ""))
            w("")
            for ln in filter_lines(p, shared):
                w("- %s" % ln)
            worked = users.get(p["title"], [])
            if worked:
                w("- worked by: %s" % ", ".join(sid if mode == "live" else "%s (%s)" % (sid, mode)
                                               for sid, owner, mode, summ in worked))
                for sid, _, _, _ in worked:
                    if sid not in credited:
                        credited.append(sid)
            if p.get("source_count") is not None:
                w("- records in the source account at export: %s%s" % (p["source_count"], " (approximate)" if p.get("source_count_approx") else ""))
            w("")
    w("### The SiftStack stages that work these presets")
    w("")
    w("Each runs on its own schedule in the agents brain. A stage in plan mode works out what it would do and "
      "writes nothing.")
    w("")
    w("| Stage | Owner | Mode | When | What it does |")
    w("| --- | --- | --- | --- | --- |")
    for sid in sorted(credited):
        i = info.get(sid) or {}
        w("| %s | %s | %s | %s | %s |" % (sid, i.get("owner", ""), i.get("mode", ""), i.get("schedule", ""),
                                         (i.get("summary") or "").replace("|", "/")))
    w("")
    w("## Step 5: the shared exclusion sets")
    w("")
    if shared.sets:
        for label, key, vals, n in shared.sets:
            what = {"any_property_status": "statuses", "any_tags": "tags", "any_lists": "lists",
                    "any_phone_status": "number statuses"}.get(key.replace("must_not.", ""), key)
            how = "left out" if key.startswith("must_not.") else "kept"
            w("**%s** (%s %s, used by %d presets): %s." % (label, what, how, n, ", ".join(vals)))
            w("")
    else:
        w("None.")
        w("")
    w("## Step 6: the SiftLine boards")
    w("")
    for b in bp.get("boards") or []:
        w("- **%s:** %s" % (b.get("title"), ", ".join(str(c) for c in b.get("columns") or [])))
    w("")
    w("## Step 7: the sequences")
    w("")
    w("| Sequence | Folder | Active | What it does |")
    w("| --- | --- | --- | --- |")
    for s in bp.get("sequences") or []:
        w("| %s | %s | %s | %s |" % (s.get("title"), title_of(s.get("folder")), "yes" if s.get("is_active") else "no",
                                    sequence_line(s).replace("|", "/")))
    w("")
    w("## Step 8: the task presets")
    w("")
    tgroups = defaultdict(list)
    for t in bp.get("task_presets") or []:
        tgroups[title_of(t.get("group")) if t.get("group") else "No group"].append(str(t.get("title")))
    for g, ts in tgroups.items():
        w("- **%s:** %s" % (g, ", ".join(ts)))
    w("")
    w("## Step 9: the SiftMap auto-add presets")
    w("")
    sm = bp.get("siftmap_presets") or []
    w("%d SiftMap presets feed new records into the account. They carry the source market's county and "
      "addresses, so the blueprint creates them with auto-add switched off: point each one at your own market "
      "before you switch it on. %s." % (len(sm), ", ".join(str(x.get("name") or x.get("title")) for x in sm)))
    w("")
    w("## Keeping this guide current")
    w("")
    w("```bash")
    w("python src/clone_account.py --phase export        # a new blueprint from the live account")
    w("python tools/sync_account_blueprint_skill.py       # copy it into the account-blueprint skill")
    w("python tools/render_account_structure.py           # rewrite this guide")
    w("```")
    w("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--blueprint", help="the blueprint to render (default: the newest one the skill ships)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--check", action="store_true", help="exit 1 when the guide on disk is not what it would render")
    a = ap.parse_args(argv)
    text = render(Path(a.blueprint) if a.blueprint else newest_blueprint())
    out = Path(a.out)
    if a.check:
        have = out.read_text(encoding="utf-8") if out.exists() else ""
        if have != text:
            print("%s is out of date: run python tools/render_account_structure.py" % out)
            return 1
        print("%s is current" % out)
        return 0
    out.write_bytes(text.encode("utf-8"))
    print("wrote %s (%d lines)" % (out, text.count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
