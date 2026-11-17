"""Account checks (research 2026-11-15): the account's own automations, read for what they get wrong.

Four things nothing watched. Each was measured on the live account before this was written:

  * A SEQUENCE TEXT THAT FAILS EVERY TIME. "Call New Lead" sends a text through smrtPhone on every
    new lead, and all 46 it sent in 30 days failed. The failure is logged (owner.sms.failed, source
    "sequence") at the same second as the card, task and assignment the same run made, and nowhere
    a person looks. The new lead's first touch never happened, and nobody knew.
  * A SEQUENCE THAT TEXTS A RECORD THAT IS NOT A LEAD. A sequence's text and email actions are for
    instant follow-up with a lead, never for prospecting (lesson 9-02 at 0:42). A text fired off a
    tag or a list reaches a cold owner from a number nobody watches for the reply.
  * TASKS ON SOMEONE WHO IS NOT A CALLER. Every task the lead sequences made in 30 days (175) went to
    a staff account that does not call. None was completed by anyone, 100 of the 102 that went
    overdue were that account's, and the next sequence deleted them. The lead manager's day never
    showed one of them.
  * A TASK THAT DOES NOT FIT ITS RECORD. A Call New Lead task on a record back in prospecting
    (status Follow Up) sends a caller to a cold owner as if they had just raised a hand, and a COLD
    Follow-Up task on a Hot Lead waits 45 days to call someone who wanted a call in two (lessons
    7-07 at 0:03 and 2:31, 7-06 at 1:03).

How each is read, without a request it does not need:

  * Sequence texts come from the shared log copy. A failed text carries no sequence name, so it is
    tied to the run that sent it: the card, task, assignment or status change logged with a
    sequence's name within `match_seconds` of the text. When two sequences fired at that moment,
    the one whose record belongs to the texted owner is the one (a capped number of record reads).
    Whether the record was a lead comes from the last status change the log holds for it at that
    moment, and only when the log holds none is the record read.
  * Open tasks come from the published task list (GET /api/internal/task/, paged, capped), each
    with its assignee. Who made a task comes from the log copy's task.created row, which names the
    sequence, so a problem says which sequence to fix.
  * A task fits its record when its title is the task a lead column's own sequence makes and the
    record reads the status that column sets (the phase table in leadroutes.py, overridable under
    `task_fits`). A title the check has no rule for, and every appointment, is left alone: a
    person's own task is never second-guessed.

It reads only, so plan and live do the same thing, and it speaks only when something is wrong. A
finding is a problem for a person to fix in the DataSift app (most of these live in Sequence
Builder, which the published API does not reach), so `acted` stays 0.

`caller_ids` lives here and the lead check uses it too: a lead held by someone who is not a caller
is a lead nobody calls.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import activity, budget, dealsource, lanes, ledger
from . import leadroutes as routes
from .crm import rows_of
from .stages import Ctx, addr

log = logging.getLogger(__name__)

TASKS = "/api/internal/task/"
PAGE = 100
# The events a sequence run logs WITH the sequence's name (slim() keeps extra_info.source.title as
# `by` on these). The run's text is logged without it, at the same second.
NAMED = ("property.status.updated", "property.assigned", "card.created", "card.moved", "task.created")
SEND_TYPES = ("owner.sms.sent", "owner.sms.delivered", "owner.sms.failed")
FAILED_TYPES = ("owner.sms.failed",)
FAILED_WORDS = ("fail", "undeliver", "reject", "error")
# A record in one of these is still prospecting, whatever the code elsewhere calls it: the lesson's
# own example of a task that does not fit is a Call New Lead task on a Follow Up record (7-07 2:31).
PROSPECTING_STATUSES = ["follow_up"]

DEFAULTS = {
    "window_days": 7,             # how far back the sequence text checks read the log copy
    "settle_minutes": 30,         # a text newer than this waits for the next run (its run's other rows may not be synced)
    "match_seconds": 120,         # a sequence's text and the card, task or assignment the same run made land this close
    "min_failures": 3,            # a sequence is named once this many of its texts failed...
    "fail_share": 0.5,            # ...and at least this share of everything it sent
    "min_prospect_sends": 2,      # texts a sequence sent to records that were not leads, before it is named
    "overdue_bulk": 10,           # one person holding this many tasks past their date is named
    "stale_task_days": 30,        # the window for "tasks are made and not done"
    "min_created": 20,            # ...counted only once this many tasks were made in it
    "min_completed_share": 0.1,   # ...and named when fewer than this share were completed
    "max_task_pages": 10,         # 100 tasks a page
    "max_record_reads": 60,
    "max_scan": 2000,             # lead rows read to know each task's record status
    "max_log_calls": 300,
    "floor_days": 14,
    "sync": True,
    "examples": 2,
}


def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def setting(st, key: str):
    v = st.get(key)
    return DEFAULTS.get(key) if v is None else v


def status_name(s: str) -> str:
    """A stored status as a person says it: new_lead reads New Lead, an empty one reads no status."""
    s = str(s or "").strip()
    if not s:
        return "no status"
    return " ".join(w[:1].upper() + w[1:] for w in s.replace("_", " ").split())


def lead_statuses(st) -> set[str]:
    """The statuses a record is a lead in, for these checks: the lead statuses, less the ones the
    course treats as still prospecting."""
    prospecting = {str(s).strip().lower() for s in (_list(st.get("prospecting_statuses")) or PROSPECTING_STATUSES)}
    return {s for s in lanes.LEAD_STATUSES if s not in prospecting}


# ---------------------------------------------------------------------- people
def caller_ids(ctx: Ctx, names: list | None = None) -> tuple[dict, dict, list[str]]:
    """(callers, team, notes). `callers` maps each named caller's user uuid to a first name;
    `team` maps every user the team list returned; `notes` says what could not be resolved.

    Names come from the stage's own `callers`, else people.callers in the playbook, each a DataSift
    first name, a user uuid or {name, uuid}. An inactive user is not a caller: a lead or a task held
    by someone who cannot log in is held by nobody. The team list is read once (the client keeps it),
    and an install that lists every caller by uuid never reads it. Nothing is posted from here: each
    stage decides what a gap means for its own check."""
    if names is None:
        names = _list(ctx.stage.get("callers")) or list(getattr(ctx.policy, "callers", None) or [])
    names = [n for n in names if n not in (None, "")]
    if not names:
        return {}, {}, ["the playbook names no callers"]
    out: dict = {}
    plain = []
    for n in names:
        if isinstance(n, dict) and n.get("uuid"):
            out[str(n["uuid"])] = str(n.get("name") or str(n["uuid"])[:8])
        else:
            plain.append(str(n.get("name") if isinstance(n, dict) else n).strip())
    team: dict = {}
    notes: list[str] = []
    if plain:
        try:
            users = list(ctx.crm.users() or [])
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (an unreadable team list is said by the caller, not guessed)
            return out, dict(out), ["could not read the team list (%s)" % str(exc)[:80]]
        for u in users:
            if u.get("uuid"):
                team[str(u["uuid"])] = str(u.get("first_name") or "").strip() or str(u["uuid"])[:8]
        for name in plain:
            low = name.lower()
            matches = [u for u in users if (u.get("first_name") or "").strip().lower() == low or str(u.get("uuid") or "") == name]
            active = [u for u in matches if u.get("is_active", True)]
            if not matches:
                notes.append("no DataSift user is named %s" % name)
            elif not active:
                notes.append("%s is inactive in DataSift" % name)
            elif len(active) > 1:
                notes.append("two active users are named %s" % name)
            else:
                out[str(active[0]["uuid"])] = str(active[0].get("first_name") or name)
    for k, v in out.items():
        team.setdefault(k, v)
    return out, team, notes


# ---------------------------------------------------------------------- small reads, capped
def _record(ctx: Ctx, u: str, reads: dict, recs: dict) -> dict | None:
    """One record, read at most once a run and inside the run's read cap. None past the cap."""
    if u in recs:
        return recs[u]
    if reads.get("left", 0) <= 0:
        reads["skipped"] = reads.get("skipped", 0) + 1
        return None
    reads["left"] -= 1
    try:
        rec = ctx.crm.record(u) or {}
    except budget.BudgetExhausted:
        reads["left"], reads["budget"] = 0, True
        return None
    except Exception:  # noqa: BLE001  (one unreadable record must not stop the checks)
        rec = {}
    recs[u] = rec
    owner = (rec.get("owner") or {}).get("uuid") or ""
    if owner:
        ledger.set_fact(u, "owner_uuid", owner)       # who owns the record: knowledge, kept in plan mode too
    return rec


def owner_of(ctx: Ctx, u: str, reads: dict) -> str:
    """The record's owner: the lead clock's or the deal-source table's fact first, else one read."""
    info = ledger.fact(u, "lead_owner")
    if isinstance(info, dict) and info.get("owner"):
        return str(info["owner"])
    return dealsource.owner_of(ctx, u, reads)


# ---------------------------------------------------------------------- sequence texts
def _by_sequence(e: dict) -> bool:
    return "sequence" in (str(e.get("source") or "").lower(), str((e.get("data") or {}).get("source") or "").lower())


def _failed(e: dict, failed_types) -> bool:
    if e["event_type"] in failed_types:
        return True
    s = str((e.get("data") or {}).get("status") or "").lower()
    return any(w in s for w in FAILED_WORDS)


def sequence_sends(since: datetime, until: datetime, send_types, failed_types, match: timedelta) -> list[dict]:
    """Every text a sequence sent in the window, one per owner per run, oldest first. A text logged
    as sent and then as failed is one text that failed, not one sent and one failed."""
    by_owner: dict[str, list] = defaultdict(list)
    for e in activity.events(tuple(send_types), since=since, until=until):
        if e["data"].get("direction") == "inbound" or not e["resource"] or not _by_sequence(e):
            continue
        by_owner[e["resource"]].append(e)
    out = []
    for owner, evs in by_owner.items():
        cur = None
        for e in evs:
            if cur is not None and e["when"] - cur["when"] <= match:
                cur["failed"] = cur["failed"] or _failed(e, failed_types)
                continue
            cur = {"owner": owner, "when": e["when"], "failed": _failed(e, failed_types)}
            out.append(cur)
    out.sort(key=lambda s: s["when"])
    return out


def name_send(ctx: Ctx, send: dict, match: timedelta, reads: dict) -> tuple[str, str]:
    """(the sequence that sent the text, the record it was about); either may be empty.

    The run that sent it logged its other actions with the sequence's name at the same moment. One
    record in that moment is taken unless its owner is known to be somebody else. With several, the
    record whose owner is the texted owner decides; failing that, a name every candidate shares is
    still the name, with no record behind it."""
    lo, hi = send["when"] - match, send["when"] + match + timedelta(seconds=1)
    by_prop: dict[str, set] = defaultdict(set)
    for e in activity.events(NAMED, since=lo, until=hi):
        name = str(e["data"].get("by") or "").strip()
        if name and str(e.get("source") or "").lower() == "sequence" and e["resource"]:
            by_prop[e["resource"]].add(name)
    if not by_prop:
        return "", ""
    if len(by_prop) == 1:
        prop, names = next(iter(by_prop.items()))
        info = ledger.fact(prop, "lead_owner")
        known = (info or {}).get("owner") if isinstance(info, dict) else ""
        known = known or ledger.fact(prop, "owner_uuid") or ""
        if known and str(known) != send["owner"]:
            return "", ""                     # that run was about someone else's house
        return " or ".join(sorted(names)), prop
    mine = [p for p in sorted(by_prop) if owner_of(ctx, p, reads) == send["owner"]]
    if len(mine) == 1:
        return " or ".join(sorted(by_prop[mine[0]])), mine[0]
    every = set().union(*by_prop.values())
    if len(every) == 1:
        return next(iter(every)), ""
    return "", ""


def lead_at_send(ctx: Ctx, send: dict, prop: str, match: timedelta, reads: dict, recs: dict,
                 leads: set[str]) -> tuple[bool | None, str]:
    """(was the texted record a lead at that moment, the status read). None when it cannot be told
    inside the read cap. The log's last status change at that moment is used first, so a record
    that has moved on since does not change the answer; with no change in the log, the record is
    read as it stands now."""
    if prop:
        evs = activity.events("property.status.updated", resource=prop, until=send["when"] + match + timedelta(seconds=1))
        if evs:
            s = str(evs[-1]["data"].get("to") or "").strip().lower()
            return s in leads, s
        rec = _record(ctx, prop, reads, recs)
        if rec is None:
            return None, ""
        s = lanes.status_of(rec)
        return s in leads, s
    if reads.get("left", 0) <= 0:
        reads["skipped"] = reads.get("skipped", 0) + 1
        return None, ""
    reads["left"] -= 1
    try:
        rows = ctx.crm.owner_records(send["owner"], cap=5) or []
    except budget.BudgetExhausted:
        reads["left"], reads["budget"] = 0, True
        return None, ""
    except Exception:  # noqa: BLE001
        return None, ""
    statuses = [lanes.status_of(r) for r in rows]
    if not statuses:
        return None, ""
    hit = [s for s in statuses if s in leads]
    return (True, hit[0]) if hit else (False, statuses[0])


def check_texts(ctx: Ctx, reads: dict, recs: dict, leads: set[str]) -> dict:
    st = ctx.stage
    days = float(setting(st, "window_days"))
    match = timedelta(seconds=float(setting(st, "match_seconds")))
    until = ctx.now - timedelta(minutes=float(setting(st, "settle_minutes")))
    sends = sequence_sends(ctx.now - timedelta(days=days), until, _list(st.get("send_types")) or SEND_TYPES,
                           _list(st.get("failed_types")) or FAILED_TYPES, match)
    per: dict[str, dict] = defaultdict(lambda: {"sent": 0, "failed": 0, "failed_props": [], "cold": Counter(),
                                                "cold_props": []})
    for s in sends:
        name, prop = name_send(ctx, s, match, reads)
        row = per[name]
        row["sent"] += 1
        if s["failed"]:
            row["failed"] += 1
            if prop:
                row["failed_props"].append(prop)
        verdict, status = lead_at_send(ctx, s, prop, match, reads, recs, leads)
        if verdict is None:
            ctx.out.tally["sequence texts whose record could not be read (read cap)"] += 1
        elif verdict is False:
            row["cold"][status] += 1
            if prop:
                row["cold_props"].append(prop)
    n_ex = int(setting(st, "examples"))
    window = int(days)
    out = {"sends": len(sends), "failed": sum(r["failed"] for r in per.values()), "problems": 0}
    for name in sorted(per, key=lambda k: (-per[k]["failed"], k)):
        row = per[name]
        who = "Sequence %s" % name if name else "A sequence the log does not name"
        ctx.out.tally["%s: texts sent" % (name or "unnamed sequence")] = row["sent"]
        if row["failed"]:
            ctx.out.tally["%s: texts failed" % (name or "unnamed sequence")] = row["failed"]
        if row["failed"] >= int(setting(st, "min_failures")) and row["failed"] >= float(setting(st, "fail_share")) * row["sent"]:
            out["problems"] += 1
            ex = _streets(ctx, row["failed_props"], reads, recs, n_ex)
            ctx.out.problem("%s: %d of %d texts it sent in the last %d days failed. Fix its text action (the sending "
                            "number, or its carrier registration) or take the action out%s" % (
                                who, row["failed"], row["sent"], window, " (for example %s)" % "; ".join(ex) if ex else ""))
        cold = sum(row["cold"].values())
        if cold >= int(setting(st, "min_prospect_sends")):
            out["problems"] += 1
            mix = ", ".join("%s %d" % (status_name(k), v) for k, v in row["cold"].most_common(3))
            ex = _streets(ctx, row["cold_props"], reads, recs, n_ex)
            ctx.out.problem("%s texts records that are not leads: %d in the last %d days (%s). A sequence text is for "
                            "instant follow-up with a lead only%s" % (
                                who, cold, window, mix, "; for example %s" % "; ".join(ex) if ex else ""))
    if sends:
        names = [k for k in per if k]
        ctx.out.line("Sequence texts, last %d days: %d sent by %d sequence(s)%s, %d failed." % (
            window, len(sends), len(per), "" if len(names) == len(per) else " (one the log does not name)", out["failed"]))
    else:
        ctx.out.line("No sequence sent a text in the last %d days." % window)
    return out


def _streets(ctx: Ctx, props: list[str], reads: dict, recs: dict, n: int) -> list[str]:
    """Up to n example streets, in a fixed order so the same finding reads the same way each day."""
    out = []
    for u in sorted(dict.fromkeys(props)):
        if len(out) >= n:
            break
        rec = _record(ctx, u, reads, recs)
        if rec:
            out.append(addr(rec))
    return out


# ---------------------------------------------------------------------- tasks
def task_listing(ctx: Ctx, max_pages: int) -> tuple[list[dict], int | None, bool]:
    """(tasks read, the count the route reports, whether every task was read)."""
    out: list[dict] = []
    total = None
    for i in range(max(1, int(max_pages))):
        r = ctx.crm.call("%s?limit=%d&offset=%d" % (TASKS, PAGE, i * PAGE))
        rows = [x for x in rows_of(r) if isinstance(x, dict)]
        if isinstance(r, dict) and total is None and isinstance(r.get("count"), int):
            total = r["count"]
        out.extend(rows)
        if len(rows) < PAGE or (isinstance(r, dict) and "next" in r and not r.get("next")):
            return out, total, True
    return out, total, total is not None and len(out) >= total


def holder_of(t: dict, team: dict) -> tuple[str, str]:
    """(who holds the task, their first name). A task given to a role is held by the role; one given
    to nobody returns ("", "")."""
    u = t.get("assigned_to_user")
    if isinstance(u, dict) and u.get("uuid"):
        uid = str(u["uuid"])
        return uid, str(u.get("first_name") or team.get(uid) or "someone").strip()
    if isinstance(u, str) and u:
        return u, team.get(u) or "someone"
    many = [x for x in (t.get("assigned_to_users") or []) if x]
    if many:
        first = str(many[0].get("uuid") if isinstance(many[0], dict) else many[0])
        return first, team.get(first) or "someone"
    role = t.get("assigned_to_role")
    if role:
        return "role:%s" % role, "the %s role" % str(role).replace("-", " ")
    return "", ""


def made_by(t: dict) -> str | None:
    """The sequence that made a task, from the log copy's task.created row: its name, "" when a
    person made it, None when the log copy does not reach back to it (an old task is not called a
    person's just because its row is gone)."""
    prop, tid = t.get("assigned_to_property") or "", t.get("uuid") or ""
    if not prop or not tid:
        return None
    for e in activity.events("task.created", resource=prop):
        if e["data"].get("task") == tid:
            if str(e.get("source") or "").lower() == "sequence":
                return str(e["data"].get("by") or "") or "a sequence"
            return ""
    return None


BY_HAND, UNSEEN = "by hand", "older than the log copy"


def _makers(made: Counter) -> tuple[str, bool]:
    """('Call New Lead 46, [NEW] Nurture New Lead 29, by hand 3', whether any sequence made them)."""
    seq = [(k, v) for k, v in made.most_common() if k not in (BY_HAND, UNSEEN)]
    parts = ["%s %d" % kv for kv in seq[:3]]
    parts += ["%s %d" % (k, made[k]) for k in (BY_HAND, UNSEEN) if made.get(k)]
    return ", ".join(parts), bool(seq)


def default_fits(stage) -> dict[str, set]:
    """task title (lower case) -> the statuses (lower case) it belongs to. The task a lead column's
    own sequence makes belongs to the status that column sets. Nurture sets no status of its own and
    a nurture lead still reads No Contact, so its task fits the new-lead statuses; a review task
    fits any lead."""
    ph = routes.phases(stage)
    every = {s.lower() for s in lanes.LEAD_STATUS_VALUES}
    out: dict[str, set] = {}
    for key, row in ph.items():
        title = str(row.get("preset") or "").strip().lower()
        if not title or row.get("board") != "lead":
            continue
        status = str(row.get("status") or "").strip().lower()
        if key == "nurture":
            fit = {str(ph["no_contact"]["status"]).lower(), str(ph["new_lead"]["status"]).lower()}
        elif status:
            fit = {status}
        else:
            fit = set(every)
        out[title] = out.get(title, set()) | fit
    for title, statuses in (stage.get("task_fits") or {}).items():
        out[str(title).strip().lower()] = {str(s).strip().lower() for s in _list(statuses)}
    return out


def check_tasks(ctx: Ctx, reads: dict, recs: dict, leads: set[str]) -> dict:
    st = ctx.stage
    tz = ctx.policy.tz
    out = {"open": 0, "problems": 0}
    callers, team, notes = caller_ids(ctx)
    also, team2, notes2 = caller_ids(ctx, names=_list(st.get("also_ok"))) if _list(st.get("also_ok")) else ({}, {}, [])
    team = {**team2, **team}
    allowed = {**callers, **also}
    try:
        tasks, total, whole = task_listing(ctx, int(setting(st, "max_task_pages")))
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001  (an unreadable task list is said, and the log checks still run)
        ctx.out.problem("could not read the task list (%s); who holds a task and whether it fits its record were not checked" % (
            str(exc)[:100]))
        return out
    if not whole:
        ctx.out.problem("read the first %d tasks of %s; the task checks below may be short (raise max_task_pages)" % (
            len(tasks), total if total is not None else "more"))
    opened = [t for t in tasks if not t.get("completed")]
    out["open"] = len(opened)
    ctx.out.tally["open tasks read"] = len(opened)
    now = ctx.now
    by_holder: dict[str, dict] = {}
    nobody = []
    for t in opened:
        uid, name = holder_of(t, team)
        due = routes.parse_dt(t.get("due_date"))
        late = bool(due and due < now)
        if not uid:
            nobody.append(t)
            continue
        if uid.startswith("role:"):
            ctx.out.tally["open tasks given to a role"] += 1
            continue
        h = by_holder.setdefault(uid, {"name": name, "open": 0, "late": 0, "oldest": None, "made": Counter()})
        h["open"] += 1
        if late:
            h["late"] += 1
            h["oldest"] = due if h["oldest"] is None or due < h["oldest"] else h["oldest"]
        m = made_by(t)
        h["made"][UNSEEN if m is None else (m or BY_HAND)] += 1
    if not callers:
        why = notes[0] if notes else "the callers list did not resolve"
        ctx.out.problem("could not tell who the callers are (%s), so who holds each open task was not checked" % why)
    zone = ZoneInfo(tz)
    for uid, h in sorted(by_holder.items(), key=lambda kv: (-kv[1]["open"], kv[1]["name"])):
        oldest = h["oldest"].astimezone(zone).strftime("%b %d").replace(" 0", " ") if h["oldest"] else ""
        if callers and uid not in allowed:
            out["problems"] += 1
            makers, by_sequence = _makers(h["made"])
            fix = "Point the sequences that make them at a caller on the list" if by_sequence else "Give them to a caller on the list"
            ctx.out.tally["open tasks with someone who is not a caller"] += h["open"]
            ctx.out.problem("Open tasks sit with %s, who is not on the callers list: %d, %d of them past their date. "
                            "%s (made by %s)" % (h["name"], h["open"], h["late"], fix, makers))
        elif h["late"] >= int(setting(st, "overdue_bulk")):
            out["problems"] += 1
            ctx.out.problem("Tasks pile up past their date for %s: %d overdue, the oldest due %s" % (h["name"], h["late"], oldest))
        ctx.out.line("%s: %d open task(s), %d past their date." % (h["name"], h["open"], h["late"]))
    if nobody:
        out["problems"] += 1
        ctx.out.tally["open tasks assigned to nobody"] = len(nobody)
        first = sorted(nobody, key=lambda t: str(t.get("title") or ""))[0]
        ctx.out.problem("Open tasks assigned to nobody: %d. A task nobody holds is on nobody's list (for example %s)" % (
            len(nobody), str(first.get("title") or "a task")))
    out["problems"] += _check_fit(ctx, opened, reads, recs, leads)
    return out


def _check_fit(ctx: Ctx, opened: list[dict], reads: dict, recs: dict, leads: set[str]) -> int:
    """Lead tasks on records that are not leads, and tasks for one lead stage on a lead in another."""
    st = ctx.stage
    fits = default_fits(st)
    judged = [t for t in opened if str(t.get("event_type") or "task") != "appointment" and t.get("assigned_to_property")
              and str(t.get("title") or "").strip().lower() in fits]
    if not judged:
        return 0
    rows = routes.status_rows(ctx, lanes.LEAD_STATUS_VALUES, cap=int(setting(st, "max_scan")))
    status_by = {r["uuid"]: lanes.status_of(r) for r in rows}
    street_by = {r["uuid"]: addr(r) for r in rows}
    not_lead, wrong_stage = [], []
    for t in judged:
        prop = str(t["assigned_to_property"])
        title = str(t.get("title") or "").strip()
        s = status_by.get(prop)
        street = street_by.get(prop, "")
        if s is None:
            rec = _record(ctx, prop, reads, recs)
            if rec is None:
                ctx.out.tally["tasks not judged (read cap)"] += 1
                continue
            s, street = lanes.status_of(rec), addr(rec)
        if s in fits[title.lower()]:
            continue
        item = (street or prop[:8], title, s)
        (wrong_stage if s in leads else not_lead).append(item)
    problems = 0
    n_ex = int(setting(st, "examples"))
    if not_lead:
        problems += 1
        ctx.out.tally["lead tasks on records that are not leads"] = len(not_lead)
        ex = "; ".join("%s on %s, which reads %s" % (title, street, status_name(s)) for street, title, s in sorted(not_lead)[:n_ex])
        ctx.out.problem("Lead tasks on records that are not leads: %d. Delete them and let each record go on through its "
                        "call cycle (for example %s)" % (len(not_lead), ex))
    if wrong_stage:
        problems += 1
        ctx.out.tally["tasks for one lead stage on a lead in another"] = len(wrong_stage)
        ex = "; ".join("%s on %s, a %s" % (title, street, status_name(s)) for street, title, s in sorted(wrong_stage)[:n_ex])
        ctx.out.problem("Tasks for one lead stage on a lead in another: %d. Give each the task its own stage makes "
                        "(for example %s)" % (len(wrong_stage), ex))
    ctx.out.line("Lead tasks checked against their record: %d, %d that do not fit." % (len(judged), len(not_lead) + len(wrong_stage)))
    return problems


def check_done(ctx: Ctx) -> int:
    """Tasks made and not done, from the log copy: a task that is created, goes overdue and is
    deleted by the next sequence never shows as a missed task anywhere."""
    st = ctx.stage
    days = int(setting(st, "stale_task_days"))
    since = ctx.now - timedelta(days=days)
    reach = activity.events(activity.BOARD, limit=1)
    if reach and reach[0]["when"] > since:
        days = max(1, (ctx.now - reach[0]["when"]).days)       # the copy is younger than the window: say what it holds
    made = activity.events("task.created", since=since)
    done = activity.events("task.completed", since=since)
    gone = [e for e in activity.events("task.deleted", since=since) if str(e.get("source") or "").lower() == "sequence"]
    ctx.out.tally["tasks created (log)"] = len(made)
    ctx.out.tally["tasks completed (log)"] = len(done)
    if len(made) < int(setting(st, "min_created")):
        return 0
    if len(done) >= float(setting(st, "min_completed_share")) * len(made):
        return 0
    ctx.out.problem("Tasks are made and not done: %d created in the last %d days, %d completed%s" % (
        len(made), days, len(done), ", %d deleted by a sequence" % len(gone) if gone else ""))
    return 1


# ---------------------------------------------------------------------- the stage
def run_accountchecks(ctx: Ctx) -> None:
    st = ctx.stage
    if setting(st, "sync"):
        for group in ("texts", "state", "board"):
            try:
                s = activity.sync(ctx.crm, group, now=ctx.now, floor_days=int(setting(st, "floor_days")),
                                  max_calls=int(setting(st, "max_log_calls")))
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001  (a log that cannot be read is said, not crashed on)
                ctx.out.problem("could not read the %s log (%s); the checks that use it may be short" % (group, str(exc)[:100]))
                continue
            if not s["complete"]:
                ctx.out.problem("the %s log is still catching up (read through %s); the checks that use it may be short" % (
                    group, s["through"] or "nothing yet"))
    reads = {"left": int(setting(st, "max_record_reads"))}
    recs: dict = {}
    leads = lead_statuses(st)
    texts = check_texts(ctx, reads, recs, leads)
    tasks = check_tasks(ctx, reads, recs, leads)
    done = check_done(ctx)
    if reads.get("budget"):
        ctx.out.problem("the hour's request budget ran out during the checks, so some records were not read")
    elif reads.get("skipped"):
        ctx.out.tally["records not read (read cap)"] = reads["skipped"]
    found = texts["problems"] + tasks["problems"] + done
    ctx.out.found = texts["sends"] + tasks["open"]
    ctx.out.acted = 0                       # it reads only: a finding is a problem for a person, not an action
    if found:
        ctx.out.head = "Account checks: %d thing(s) to fix in the account's own automations, listed below." % found
    elif ctx.out.problems:
        ctx.out.head = "Account checks: nothing found to fix, but not everything could be read; see below."
    else:
        ctx.out.head = ("Account checks: %d sequence text(s) and %d open task(s) read; the texts go out, the tasks sit with "
                        "the callers and fit their records." % (texts["sends"], tasks["open"]))


# ---------------------------------------------------------------------- the playbook block
def block_problems(st) -> list[str]:
    """A mistyped setting is refused when the playbook loads, instead of read as a default."""
    p = []
    for key in ("window_days", "settle_minutes", "match_seconds", "min_failures", "min_prospect_sends", "overdue_bulk",
                "stale_task_days", "min_created", "max_task_pages", "max_record_reads", "max_scan", "max_log_calls",
                "floor_days", "examples"):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            p.append("stage %s: %s must be a number of zero or more" % (st.id, key))
    for key in ("fail_share", "min_completed_share"):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1):
            p.append("stage %s: %s must be a share from 0 to 1" % (st.id, key))
    kept = {t for g in activity.GROUPS.values() for t in g}
    for key in ("send_types", "failed_types"):
        bad = [t for t in _list(st.get(key)) if t not in kept]
        if bad:
            p.append("stage %s: %s names %s, which the log copy does not keep (add it to a group in activity.py first)" % (
                st.id, key, ", ".join(map(str, bad))))
    fits = st.get("task_fits")
    if fits is not None and (not isinstance(fits, dict) or any(not isinstance(v, (list, str)) for v in fits.values())):
        p.append("stage %s: task_fits must map a task title to the status or statuses it belongs to" % st.id)
    if st.get("also_ok") is not None and not isinstance(st.get("also_ok"), (list, str)):
        p.append("stage %s: also_ok must list people by first name or user uuid" % st.id)
    return p
