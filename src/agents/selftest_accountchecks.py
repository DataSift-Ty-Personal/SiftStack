"""Account checks and the quarterly business audit. Zero network: a fake DataSift, events seeded into the
log copy through the real normaliser, its own database and its own report folder.

Covered: a sequence text that keeps failing (tied to its sequence by the run's other rows), a sequence
that texts records that are not leads, who holds each open task, tasks piling up past their date,
tasks made and not done, tasks that do not fit their record's status, the lead check's rule that a
lead held by someone who is not a caller is unassigned, and the business audit's scores, caps, owner
list, critical items and quarterly clock. Every name, street and id here is invented; numbers use
the never-assigned 865-000 exchange.
"""
from __future__ import annotations

import copy
import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .selftest_autopilot import EMPLOYEES

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 16, 13, 0, tzinfo=timezone.utc)          # Monday 8:00am Eastern

CASEY = {"uuid": "u-casey", "first_name": "Casey", "is_active": True}       # the caller
MORGAN = {"uuid": "u-morgan", "first_name": "Morgan", "is_active": True}    # a staff account that does not call
JORDAN = {"uuid": "u-jordan", "first_name": "Jordan", "is_active": False}   # left the team

# The exact lines for autopilot/registry.py EXTRA. Registered for the length of this section when the
# table does not hold them yet, so the section passes before and after the orchestrator adds them.
REGISTRY_LINES = {
    "accountchecks": ("accountchecks", "run_accountchecks", "free", False, False),
    "bizaudit": ("bizaudit", "run_bizaudit", "free", False, True),
}

# The exact stage blocks for team/autopilot.yaml. Parsed below, so a block that would not load fails here.
PLAYBOOK_BLOCKS = """
  # ------------------------------------------------------------------ account checks and the business audit
  # Both read only. A finding is for a person to fix in the DataSift app: sequences live in Sequence
  # Builder, which the published API does not reach.
  account_checks:
    kind: accountchecks
    owner: ops
    mode: "plan"
    schedule: "07:25 Mon-Fri"
    summary: The account's own automations read for what they get wrong (a sequence text that keeps failing, a sequence that texts records that are not leads, tasks on someone who is not a caller, tasks that do not fit their record).
    window_days: 7              # the sequence text checks read this many days of the log copy
    match_seconds: 120          # a sequence's text and the card, task or assignment the same run made land this close
    min_failures: 3             # a sequence is named once this many of its texts failed...
    fail_share: 0.5             # ...and at least this share of what it sent
    min_prospect_sends: 2       # texts to records that were not leads before a sequence is named
    overdue_bulk: 10            # one person holding this many tasks past their date is named
    stale_task_days: 30
    min_created: 20
    min_completed_share: 0.1    # tasks made and not done: named when fewer than this share were completed
    also_ok: []                 # people besides the callers who may hold open tasks (a closer), by first name or user uuid
    prospecting_statuses: [follow_up]   # a record in one of these is still prospecting (lesson 7-07 at 2:31)
    task_fits: {}               # a task title -> the status(es) it belongs to, where the account's titles differ from the lead board's
    max_task_pages: 10          # 100 tasks a page
    max_record_reads: 60

  business_audit:
    kind: bizaudit
    owner: analysis
    mode: "plan"
    schedule: "1st 07:45"
    summary: The course's quarterly business audit scored ahead of time from the account's own data; the questions only the owner can answer are listed for him.
    # The scheduler has no quarterly slot: the stage runs on the 1st and audits only a period it has
    # not audited yet. An ask from Slack always answers.
    every_months: 3             # quarterly; 1 while starting out (C13.59)
    window_days: 91
    max_data_score: 4           # a true 5 is rare: the data never claims one, the owner can
    answers: {}                 # the owner's own 1 to 5 by question id (M1 to K5); an answer also overrides the data
    labels: {}                  # K5's wording, from the audit sheet the Day 5 workbook links to
    ni_follow_days: 30          # a Not Interested owner is due a call back after this long
    note_hours: 24              # a note this long after a conversation documents it
    max_record_reads: 30        # per question that joins records to owners
"""

_N = [0]


def _id(prefix: str) -> str:
    _N[0] += 1
    return "%s-%d" % (prefix, _N[0])


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _et(month: int, day: int, hour: int = 11, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ET)


def sms(owner, when, kind="failed", source="sequence", direction="outbound"):
    return {"id": _id("sms"), "event_type": "owner.sms.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": source, "author": "system", "timestamp": _ts(when),
            "payload": {"sms": {"direction": direction, "destination_number": "8650000101", "origin_number": "8650000199",
                                "status": kind, "integration": "smrtphone"}}}


def seq(kind, rec, when, name, **kw):
    """One row a sequence run logs WITH its name (extra_info.source.title)."""
    extra = {"source": {"uuid": "seq-%d" % (abs(hash(name)) % 1000), "title": name}}
    if kind == "card.created":
        extra["card"] = {"prop": rec, "column": [None, "New Lead (Unqualified)"], "board": "Lead Management"}
        payload, resource, rtype = {"card": {"prop": rec}, "extra_info": extra}, _id("card"), "card"
    elif kind == "task.created":
        payload = {"task": {"title": kw.get("title", "call new lead"), "assigned_to_property": rec, "event_type": "task",
                            "due_date": kw.get("due", "")}, "extra_info": extra}
        resource, rtype = kw.get("task") or _id("task"), "task"
    elif kind == "property.assigned":
        payload, resource, rtype = {"property": {"assigned_to_uuid": [None, kw.get("to", "u-morgan")]}, "extra_info": extra}, rec, "property"
    else:
        payload = {"property": {"status": [kw.get("old"), kw.get("new")]}, "extra_info": extra}
        resource, rtype = rec, "property"
    return {"id": _id("ev"), "event_type": kind, "resource": resource, "resource_type": rtype, "source": "sequence",
            "author": "system", "timestamp": _ts(when), "payload": payload}


def status_ev(rec, when, old, new, source="internal-api"):
    return {"id": _id("st"), "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": source, "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"property": {"status": [old, new]}}}


def task_ev(kind, rec, task, when, source="sequence", by=""):
    payload = {"task": {"title": "a task", "assigned_to_property": rec, "event_type": "task"}}
    if by:
        payload["extra_info"] = {"source": {"title": by}}
    return {"id": _id("tk"), "event_type": kind, "resource": task, "resource_type": "task", "source": source,
            "author": "system", "timestamp": _ts(when), "payload": payload}


def call_ev(owner, when, kind="made", duration=0, inbound=False):
    return {"id": _id("call"), "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"direction": "inbound" if inbound else "outbound", "duration": duration,
                                 "destination_number": "8650000199" if inbound else "8650000101",
                                 "origin_number": "8650000101" if inbound else "8650000199", "source": "kixie",
                                 "uuid": _id("c"), "extra": {}, "user": {"first_name": "Casey", "uuid": "u-casey"}}}}


def note_ev(resource, when, kind="property.message.added", author="person@example.com", source="internal-api"):
    return {"id": _id("nt"), "event_type": kind, "resource": resource, "resource_type": "property", "source": source,
            "author": author, "timestamp": _ts(when), "payload": {}}


def created_ev(rec, when):
    return {"id": _id("cr"), "event_type": "property.created", "resource": rec, "resource_type": "property",
            "source": "bulk", "author": "system", "timestamp": _ts(when), "payload": {}}


def rec(u, street, status=None, owner=None, assigned=None, phones=None, tasks=0):
    return {"uuid": u, "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37917"},
            "owner": {"uuid": owner or "o-" + u, "first_name": "Pat", "last_name": "Example", "phones": phones or []},
            "status": status, "assigned_to": assigned, "tags": [], "lists": [], "_tasks": tasks}


def task(tid, prop, title, holder=None, due=None, completed=None, kind="task", role=None):
    return {"uuid": tid, "title": title, "due_date": due.isoformat() if due else None, "assigned_to_property": prop,
            "completed": completed, "event_type": kind, "assigned_to_users": [], "assigned_to_role": role,
            "assigned_to_user": {"uuid": holder["uuid"], "first_name": holder["first_name"]} if holder else None}


class FakeAcct:
    """The few reads these stages make: the team list, records, records by status or owner, the
    published task list and the account log. A write of any kind fails the section."""

    def __init__(self, recs=(), users=(CASEY, MORGAN, JORDAN), tasks=()):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in recs}
        self.users_ = list(users)
        self.tasks = list(tasks)
        self.paths, self.reads, self.user_reads, self.log_calls = [], 0, 0, 0
        self.fail_users = self.fail_tasks = False
        self.writes = []

    def users(self):
        self.user_reads += 1
        if self.fail_users:
            raise RuntimeError("the team list answered 500")
        return self.users_

    def record(self, u):
        self.reads += 1
        return copy.deepcopy(self.recs.get(u) or {})

    def _row(self, r):
        return {"uuid": r["uuid"], "address": r["address"], "status": r.get("status"), "owner": {"uuid": r["owner"]["uuid"]},
                "tasks": {"total": r.get("_tasks", 0), "complete": 0, "incomplete": r.get("_tasks", 0)}}

    def rows(self, must, cap=10000):
        if "any_property_status" in must:
            want = set(must["any_property_status"])
            return [self._row(r) for r in self.recs.values() if r.get("status") in want][:cap]
        if "owners" in must:
            return [self._row(r) for r in self.recs.values() if r["owner"]["uuid"] in must["owners"]][:cap]
        return []

    def owner_records(self, owner, cap=250):
        return self.rows({"owners": [owner]}, cap=cap)

    def logs(self, must, limit=100, cursor=""):
        self.log_calls += 1
        return {"results": [], "next_cursor": ""}

    def call(self, path, method="GET", body=None, headers=None):
        if method != "GET":
            self.writes.append((method, path))
            raise AssertionError("these stages only read")
        self.paths.append(path)
        if path.startswith("/api/internal/task/?"):
            if self.fail_tasks:
                raise RuntimeError("the task list answered 502")
            q = urllib.parse.parse_qs(path.split("?", 1)[1])
            off, lim = int(q["offset"][0]), int(q["limit"][0])
            nxt = "next" if off + lim < len(self.tasks) else None
            return {"count": len(self.tasks), "next": nxt, "previous": None, "results": copy.deepcopy(self.tasks[off:off + lim])}
        raise AssertionError("unexpected read %s" % path)

    # anything that would change the account
    def assign(self, *a, **k):
        self.writes.append(("assign",) + a)

    def set_status(self, *a, **k):
        self.writes.append(("status",) + a)

    def add_tags(self, *a, **k):
        self.writes.append(("add_tags",) + a)

    def message(self, *a, **k):
        self.writes.append(("message",) + a)


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import policy, registry, sweep
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE, getattr(config, "SLACK_VOICE", "plain"))
    base = Path(tmp) / "accountchecks"
    base.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR, config.DB_PATH = base / "main", base / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.MODE, config.SLACK_VOICE = "dry", "plain"
    store.init()
    saved = (policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS, set(sweep.REPORT_KINDS))
    added = {k: row for k, row in REGISTRY_LINES.items() if k not in registry.EXTRA}
    registry.EXTRA.update(added)
    policy.KINDS = tuple(dict.fromkeys(policy.KINDS + tuple(added)))
    policy.SPEND_BY_KIND.update({k: row[2] for k, row in added.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + tuple(k for k, row in added.items() if not row[3])))
    sweep.REPORT_KINDS.update(k for k, row in REGISTRY_LINES.items() if row[4])
    try:
        _checks(check, base)
    finally:
        for k in added:
            registry.EXTRA.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved[0], saved[2]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved[1])
        sweep.REPORT_KINDS.clear()
        sweep.REPORT_KINDS.update(saved[3])
        from .autopilot import kpi
        kpi.override(None)
        config.DATA_DIR, config.DB_PATH, config.MODE, config.SLACK_VOICE = keep


def _checks(check, base: Path) -> None:
    import yaml

    from . import store
    from .autopilot import accountchecks, activity, bizaudit, capture, goals, kpi, ledger, policy, sweep

    def wipe():
        with store._LOCK, store._db() as c:
            for table in ("ap_log", "ap_facts", "ap_events", "ap_capture"):
                c.execute("DELETE FROM " + table)
            c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")

    def book(stages, callers=("Casey",)):
        return {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
                "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": list(callers)}, "stages": stages}

    def go(stages, crm, sid, now=NOW, task_row=None, callers=("Casey",)):
        pol = policy.parse(copy.deepcopy(book(stages, callers)), EMPLOYEES)
        return sweep.run(sid, task_row, crm=crm, now=now, pol=pol, plan=True)

    acct = {"kind": "accountchecks", "owner": "ops", "mode": "plan", "schedule": "07:25 Mon-Fri", "sync": False}

    # ------------------------------------------------------------------ house rules on the files themselves
    here = Path(__file__).resolve().parent
    mine = [here / "selftest_accountchecks.py"] + [here / "autopilot" / n for n in ("accountchecks.py", "bizaudit.py", "leadcheck.py")]
    bad = [f.name for f in mine if any(ch in f.read_text(encoding="utf-8") for ch in (chr(0x2014), chr(0x2013), chr(0x08)))]
    check(not bad, "the account-checks and audit files carry no dash characters and no backspace byte: %s" % (bad or "ok"))

    # ------------------------------------------------------------------ the playbook blocks
    print("  playbook blocks")
    blocks = yaml.safe_load("stages:\n" + PLAYBOOK_BLOCKS)["stages"]
    pol = policy.parse(book(copy.deepcopy(blocks)), EMPLOYEES)
    check(pol.stage("account_checks").mode == "plan" and pol.stage("business_audit").mode == "plan"
          and pol.stage("business_audit").get("every_months") == 3,
          "the two blocks for team/autopilot.yaml load, both in plan mode, the audit quarterly")
    for bad, why in ((dict(acct, fail_share=2), "fail_share"), (dict(acct, failed_types=["owner.email.failed"]), "does not keep"),
                     (dict(acct, task_fits=["Call New Lead"]), "task_fits"), (dict(acct, overdue_bulk=-1), "overdue_bulk")):
        try:
            policy.parse(book({"acct": bad}), EMPLOYEES)
            refused = ""
        except policy.PolicyError as exc:
            refused = str(exc)
        check(why in refused, "a mistyped account-checks setting is refused when the playbook loads (%s)" % why)
    biz_base = {"kind": "bizaudit", "owner": "analysis", "mode": "plan", "schedule": "1st 07:45", "sync": False}
    for bad, why in ((dict(biz_base, answers={"Z9": 3}), "not a question"), (dict(biz_base, answers={"M2": 7}), "1 to 5"),
                     (dict(biz_base, every_months=0), "every_months"), (dict(biz_base, labels={"K5": 4}), "labels"),
                     (dict(biz_base, max_data_score=6), "max_data_score")):
        try:
            policy.parse(book({"biz": bad}), EMPLOYEES)
            refused = ""
        except policy.PolicyError as exc:
            refused = str(exc)
        check(why in refused, "a mistyped audit setting is refused when the playbook loads (%s)" % why)

    # ------------------------------------------------------------------ the callers
    print("  callers")
    crm = FakeAcct()
    pol = policy.parse(book({"acct": dict(acct)}, callers=("Casey", "Jordan", "Riley")), EMPLOYEES)
    from .autopilot.stages import Ctx, Summary
    ctx = Ctx(policy=pol, stage=pol.stage("acct"), crm=crm, run_id="t", now=NOW, live=False,
              out=Summary(stage="acct", kind="accountchecks", owner="ops", mode="plan", live=False))
    callers, team, notes = accountchecks.caller_ids(ctx)
    check(callers == {"u-casey": "Casey"} and team.get("u-morgan") == "Morgan"
          and any("inactive" in n for n in notes) and any("Riley" in n for n in notes),
          "callers resolve by first name to active users only; an inactive or unknown name is noted, never guessed")
    crm2 = FakeAcct()
    pol2 = policy.parse(book({"acct": dict(acct, callers=[{"name": "Casey", "uuid": "u-casey"}])}), EMPLOYEES)
    ctx2 = Ctx(policy=pol2, stage=pol2.stage("acct"), crm=crm2, run_id="t", now=NOW, live=False,
               out=Summary(stage="acct", kind="accountchecks", owner="ops", mode="plan", live=False))
    check(accountchecks.caller_ids(ctx2)[0] == {"u-casey": "Casey"} and crm2.user_reads == 0,
          "callers given by uuid need no read of the team list")

    # ------------------------------------------------------------------ a sequence text that keeps failing
    print("  sequence texts")
    wipe()
    recs = [rec("r%d" % i, "%d Signal St" % (10 + i), status="new_lead") for i in range(1, 5)]
    recs.append(rec("r9", "19 Quiet St", status="Hot Lead"))
    rows = []
    for i in range(1, 5):
        t = NOW - timedelta(days=i, hours=2)
        rows += [status_ev("r%d" % i, t - timedelta(seconds=1), "prospecting", "new_lead"),
                 seq("card.created", "r%d" % i, t, "Lead Intake"), seq("task.created", "r%d" % i, t, "Lead Intake"),
                 seq("property.assigned", "r%d" % i, t, "Lead Intake"), sms("o-r%d" % i, t)]
    # A person's own text that failed, and one inbound text: neither is a sequence's.
    rows += [sms("o-r9", NOW - timedelta(days=1), source="smrtphone"), sms("o-r9", NOW - timedelta(days=1, hours=3), kind="received",
                                                                            source="smrtphone", direction="inbound")]
    # One owner's text logged as sent and then failed seconds later: one text, not two.
    t5 = NOW - timedelta(days=2, hours=5)
    recs.append(rec("r5", "15 Signal St", status="new_lead"))
    rows += [status_ev("r5", t5, "prospecting", "new_lead"), seq("card.created", "r5", t5, "Lead Intake"),
             sms("o-r5", t5, kind="sent"), sms("o-r5", t5 + timedelta(seconds=20))]
    activity._store(rows)
    crm = FakeAcct(recs)
    out = go({"acct": dict(acct)}, crm, "acct")
    fail = [p for p in out.problems if p.startswith("Sequence Lead Intake:")]
    check(len(fail) == 1 and "5 of 5 texts it sent in the last 7 days failed" in fail[0],
          "a sequence whose texts keep failing is named with its count, tied to it by the card and task its run made: %s" % fail[:1])
    check("11 Signal St" in fail[0] and "Fix its text action" in fail[0],
          "the finding gives example houses by street and says what to fix")
    check(not any("8650000" in p for p in out.problems + out.lines), "no phone number appears in anything the checks say")
    check(out.tally.get("Lead Intake: texts sent") == 5,
          "a text logged as sent and then as failed counts once, and a person's own or an inbound text not at all")
    check(not crm.writes and out.writes == 0 and out.acted == 0, "the checks read and write nothing")
    crm = FakeAcct(recs)
    out = go({"acct": dict(acct, max_record_reads=0)}, crm, "acct")
    fail = [p for p in out.problems if p.startswith("Sequence Lead Intake:")]
    check(fail and "for example" not in fail[0] and crm.reads == 0 and out.tally.get("records not read (read cap)"),
          "with no record reads allowed the failing sequence is still named from the log alone, and the cap is counted")

    wipe()
    rows = []
    for i in range(1, 6):
        t = NOW - timedelta(days=i)
        rows += [seq("card.created", "r%d" % i, t, "Lead Intake"), sms("o-r%d" % i, t, kind="sent" if i > 1 else "failed")]
    activity._store(rows)
    out = go({"acct": dict(acct)}, FakeAcct(recs), "acct")
    check(not any(p.startswith("Sequence Lead Intake") for p in out.problems),
          "one failure in five sends is not named: it is under the failure count and the share")

    wipe()
    rows = [sms("o-x%d" % i, NOW - timedelta(days=i)) for i in range(1, 4)]
    activity._store(rows)
    out = go({"acct": dict(acct)}, FakeAcct([]), "acct")
    check(any(p.startswith("A sequence the log does not name: 3 of 3") for p in out.problems),
          "failing texts with no named run beside them are still reported, as a sequence the log does not name")

    # Two sequences fire in the same second; the text is the one whose record belongs to the texted owner.
    wipe()
    tt = NOW - timedelta(days=1)
    activity._store([seq("card.created", "ra", tt, "Alpha Flow"), seq("card.created", "rb", tt, "Beta Flow"), sms("o-rb", tt)])
    cb = FakeAcct([rec("ra", "1 Alpha St", status="new_lead"), rec("rb", "2 Beta St", status="new_lead")])
    pol = policy.parse(book({"acct": dict(acct)}), EMPLOYEES)
    ctx = Ctx(policy=pol, stage=pol.stage("acct"), crm=cb, run_id="t", now=NOW, live=False,
              out=Summary(stage="acct", kind="accountchecks", owner="ops", mode="plan", live=False))
    send = accountchecks.sequence_sends(NOW - timedelta(days=7), NOW, accountchecks.SEND_TYPES, accountchecks.FAILED_TYPES,
                                        timedelta(seconds=120))
    check(len(send) == 1 and accountchecks.name_send(ctx, send[0], timedelta(seconds=120), {"left": 5}) == ("Beta Flow", "rb"),
          "two sequences firing in the same moment are told apart by whose record the texted owner holds")
    ledger.set_fact("ra", "owner_uuid", "o-someone-else")
    wipe()
    activity._store([seq("card.created", "ra", tt, "Alpha Flow"), sms("o-rb", tt)])
    ledger.set_fact("ra", "owner_uuid", "o-someone-else")
    send = accountchecks.sequence_sends(NOW - timedelta(days=7), NOW, accountchecks.SEND_TYPES, accountchecks.FAILED_TYPES,
                                        timedelta(seconds=120))
    check(accountchecks.name_send(ctx, send[0], timedelta(seconds=120), {"left": 5}) == ("", ""),
          "a lone run whose record is known to be someone else's is not credited with the text")

    # ------------------------------------------------------------------ a sequence that texts records that are not leads
    print("  prospecting texts")
    wipe()
    cold = [rec("p1", "31 Cold St", status="prospecting"), rec("p2", "32 Cold St"), rec("p3", "33 Cold St", status="follow_up"),
            rec("p4", "34 Warm St", status="Warm Lead")]
    rows = []
    for i, r in enumerate(cold, 1):
        t = NOW - timedelta(days=i, hours=1)
        rows += [seq("property.assigned", r["uuid"], t, "Tag Blast", to="u-casey"), sms(r["owner"]["uuid"], t, kind="sent")]
    rows.append(status_ev("p1", NOW - timedelta(days=30), None, "prospecting"))
    activity._store(rows)
    crm = FakeAcct(cold)
    out = go({"acct": dict(acct)}, crm, "acct")
    pros = [p for p in out.problems if p.startswith("Sequence Tag Blast texts records that are not leads")]
    check(len(pros) == 1 and ": 3 in the last 7 days" in pros[0] and "Follow Up 1" in pros[0] and "no status 1" in pros[0]
          and "Prospecting 1" in pros[0], "a sequence texting records that are not leads is named, with their statuses: %s" % pros[:1])
    check("for example" in pros[0] and "instant follow-up with a lead only" in pros[0],
          "it gives example streets and the rule it breaks")
    check(crm.reads <= 4, "the status at the time comes from the log first, and a record is read only when the log has none")
    out = go({"acct": dict(acct, min_prospect_sends=4)}, FakeAcct(cold), "acct")
    check(not any("texts records that are not leads" in p for p in out.problems), "under min_prospect_sends it is not named")
    out = go({"acct": dict(acct, prospecting_statuses=["nothing"])}, FakeAcct(cold), "acct")
    pros = [p for p in out.problems if "texts records that are not leads" in p]
    check(pros and ": 2 in the last 7 days" in pros[0], "Follow Up counts as prospecting only through prospecting_statuses")

    # ------------------------------------------------------------------ who holds each open task
    print("  task holders")
    wipe()
    early = NOW - timedelta(days=3)
    tasks = [task("t%d" % i, "h%d" % i, "call new lead", MORGAN, due=early if i <= 3 else NOW + timedelta(days=2)) for i in range(1, 6)]
    tasks += [task("t6", "h6", "HOT Follow-Up", CASEY, due=NOW + timedelta(days=1)), task("t7", "h7", "HOT Follow-Up", CASEY),
              task("t8", "h8", "Walkthrough notes"), task("t9", "h9", "call new lead", MORGAN, completed="2026-11-10T15:00:00Z"),
              task("t10", "h10", "Review", role="lead-manager")]
    activity._store([seq("task.created", "h%d" % i, NOW - timedelta(days=5), "Lead Intake", task="t%d" % i) for i in range(1, 5)]
                    + [task_ev("task.created", "h5", "t5", NOW - timedelta(days=5), source="internal-api")])
    hold = [rec("h%d" % i, "%d Hold St" % (40 + i), status="new_lead") for i in range(1, 6)]
    hold += [rec("h6", "46 Hold St", status="Hot Lead"), rec("h7", "47 Hold St", status="Hot Lead"), rec("h8", "48 Hold St")]
    crm = FakeAcct(hold, tasks=tasks)
    out = go({"acct": dict(acct)}, crm, "acct")
    sit = [p for p in out.problems if p.startswith("Open tasks sit with Morgan")]
    check(len(sit) == 1 and "who is not on the callers list: 5, 3 of them past their date" in sit[0]
          and "Lead Intake 4" in sit[0] and "by hand 1" in sit[0] and "Point the sequences" in sit[0],
          "open tasks on someone who is not a caller are named, with what made them: %s" % sit[:1])
    check(not any("Casey" in p for p in out.problems), "a caller holding tasks on time is not named")
    check(any(p.startswith("Open tasks assigned to nobody: 1") for p in out.problems), "a task nobody holds is named")
    check(out.tally.get("open tasks given to a role") == 1 and out.tally.get("open tasks read") == 9,
          "a completed task is not counted, and a task given to a role is not called a wrong person")
    out = go({"acct": dict(acct, also_ok=["Morgan"])}, FakeAcct(hold, tasks=tasks), "acct")
    check(not any(p.startswith("Open tasks sit with Morgan") for p in out.problems),
          "someone listed under also_ok may hold tasks")

    wipe()
    many = [task("c%d" % i, "h6", "HOT Follow-Up", CASEY, due=NOW - timedelta(days=i)) for i in range(1, 13)]
    out = go({"acct": dict(acct)}, FakeAcct(hold, tasks=many), "acct")
    pile = [p for p in out.problems if p.startswith("Tasks pile up past their date for Casey")]
    check(pile and "12 overdue" in pile[0] and "the oldest due Nov 4" in pile[0],
          "a caller holding a pile of tasks past their date is named, with the oldest: %s" % pile[:1])
    out = go({"acct": dict(acct, overdue_bulk=13)}, FakeAcct(hold, tasks=many), "acct")
    check(not any(p.startswith("Tasks pile up") for p in out.problems), "under overdue_bulk nobody is named")

    crm = FakeAcct(hold, tasks=tasks)
    crm.fail_users = True
    out = go({"acct": dict(acct)}, crm, "acct")
    check(any("could not tell who the callers are" in p and "team list" in p for p in out.problems)
          and not any(p.startswith("Open tasks sit with") for p in out.problems),
          "when the team list cannot be read, nobody is called a wrong holder, and the check says why")

    big = [task("b%d" % i, "h6", "HOT Follow-Up", CASEY, due=NOW + timedelta(days=1)) for i in range(230)]
    crm = FakeAcct(hold, tasks=big)
    out = go({"acct": dict(acct)}, crm, "acct")
    check(out.tally.get("open tasks read") == 230 and len([p for p in crm.paths if p.startswith("/api/internal/task/")]) == 3,
          "the task list is paged 100 at a time to its end")
    crm = FakeAcct(hold, tasks=big)
    out = go({"acct": dict(acct, max_task_pages=2)}, crm, "acct")
    check(any(p.startswith("read the first 200 tasks of 230") for p in out.problems), "a capped read of the task list says it is short")
    crm = FakeAcct(hold, tasks=tasks)
    crm.fail_tasks = True
    out = go({"acct": dict(acct)}, crm, "acct")
    check(any(p.startswith("could not read the task list") for p in out.problems), "a task list that cannot be read is said")

    # ------------------------------------------------------------------ tasks made and not done
    print("  tasks made and not done")
    wipe()
    rows = [task_ev("task.created", "d1", "dt%d" % i, NOW - timedelta(days=10, minutes=i)) for i in range(25)]
    rows += [task_ev("task.deleted", "d1", "dt%d" % i, NOW - timedelta(days=4, minutes=i)) for i in range(20)]
    activity._store(rows)
    out = go({"acct": dict(acct)}, FakeAcct([]), "acct")
    done = [p for p in out.problems if p.startswith("Tasks are made and not done")]
    check(done and "25 created in the last" in done[0] and "0 completed, 20 deleted by a sequence" in done[0],
          "tasks that are made, never completed and deleted by the next sequence are named: %s" % done[:1])
    activity._store([task_ev("task.completed", "d1", "dt%d" % i, NOW - timedelta(days=3, minutes=i)) for i in range(5)])
    out = go({"acct": dict(acct)}, FakeAcct([]), "acct")
    check(not any(p.startswith("Tasks are made and not done") for p in out.problems), "five completed of 25 is not named")
    wipe()
    activity._store([task_ev("task.created", "d1", "dq%d" % i, NOW - timedelta(days=2, minutes=i)) for i in range(5)])
    out = go({"acct": dict(acct)}, FakeAcct([]), "acct")
    check(not any(p.startswith("Tasks are made and not done") for p in out.problems), "too few tasks made to judge is not named")

    # ------------------------------------------------------------------ tasks that do not fit their record
    print("  task fit")
    wipe()
    fitrecs = [rec("f1", "51 Fit St", status="follow_up"), rec("f2", "52 Fit St", status="Hot Lead"),
               rec("f3", "53 Fit St", status="Cold Lead"), rec("f4", "54 Fit St", status="No Contact New Lead"),
               rec("f5", "55 Fit St", status="prospecting"), rec("f6", "56 Fit St", status="prospecting"),
               rec("f7", "57 Fit St", status="prospecting")]
    fittasks = [task("x1", "f1", "Call New Lead", CASEY), task("x2", "f2", "COLD Follow-Up", CASEY),
                task("x3", "f3", "COLD Follow-Up", CASEY), task("x4", "f4", "Nurture New Lead", CASEY),
                task("x5", "f5", "call new lead", CASEY), task("x6", "f6", "Walkthrough notes", CASEY),
                task("x7", "f7", "Call New Lead", CASEY, kind="appointment")]
    crm = FakeAcct(fitrecs, tasks=fittasks)
    out = go({"acct": dict(acct)}, crm, "acct")
    nl = [p for p in out.problems if p.startswith("Lead tasks on records that are not leads")]
    ws = [p for p in out.problems if p.startswith("Tasks for one lead stage on a lead in another")]
    check(nl and nl[0].startswith("Lead tasks on records that are not leads: 2") and "Call New Lead on 51 Fit St, Knoxville, which reads Follow Up" in nl[0]
          and "55 Fit St" in nl[0], "a lead task on a record still prospecting is named, Follow Up included: %s" % nl[:1])
    check(ws and ws[0].startswith("Tasks for one lead stage on a lead in another: 1") and "COLD Follow-Up on 52 Fit St, Knoxville, a Hot Lead" in ws[0],
          "a task for one lead stage on a lead in another is named: %s" % ws[:1])
    check(not any("53 Fit St" in p or "54 Fit St" in p for p in out.problems),
          "a task that fits its record is left alone, nurture included (a nurture lead still reads No Contact)")
    check(not any("56 Fit St" in p or "57 Fit St" in p for p in out.problems),
          "a title the check has no rule for, and an appointment, are never second-guessed")
    check(crm.reads == 1, "only the record the lead rows do not cover is read")
    out = go({"acct": dict(acct, task_fits={"Call New Lead": ["new_lead", "follow_up"]})}, FakeAcct(fitrecs, tasks=fittasks), "acct")
    nl = [p for p in out.problems if p.startswith("Lead tasks on records that are not leads")]
    check(nl and nl[0].startswith("Lead tasks on records that are not leads: 1") and "51 Fit St" not in nl[0],
          "task_fits changes which status a title belongs to")

    # ------------------------------------------------------------------ reading the log, and posting
    print("  log and posts")
    wipe()
    out = go({"acct": dict(acct, sync=True, max_log_calls=0)}, FakeAcct([]), "acct")
    check(any(p.startswith("the texts log is still catching up") for p in out.problems),
          "a log copy that could not be brought up to date is said")
    out = go({"acct": dict(acct, sync=True)}, FakeAcct([]), "acct")
    check(not out.problems and out.head.startswith("Account checks: 0 sequence text(s) and 0 open task(s) read"),
          "a quiet account reads as quiet: %s" % out.head)
    check(sweep.posts({"id": 1, "workflow": "acct", "source": "schedule"}, out.as_dict()) == [],
          "a quiet plan-mode run posts nothing")
    wipe()
    activity._store([sms("o-x%d" % i, NOW - timedelta(days=i)) for i in range(1, 4)])
    out = go({"acct": dict(acct)}, FakeAcct([]), "acct")
    posts = sweep.posts({"id": 2, "workflow": "acct", "source": "schedule"}, out.as_dict())
    check(posts and all(p["kind"] == "issues" for p in posts), "a finding posts to the issues channel, once a business day")

    # ------------------------------------------------------------------ the lead check: a lead held by someone who is not a caller
    print("  lead check callers")
    wipe()
    lc = {"kind": "leadcheck", "owner": "sales", "mode": "plan", "schedule": "08:10 Mon-Fri"}
    ok_phone = [{"number": "8650000141", "type": "MOBILE", "tags": ["Dial First"], "status": "CORRECT"}]
    leads = [rec("l1", "61 Lead St", status="Hot Lead", assigned="u-casey", phones=ok_phone, tasks=1),
             rec("l2", "62 Lead St", status="Warm Lead", assigned="u-morgan", phones=ok_phone, tasks=1),
             rec("l3", "63 Lead St", status="Cold Lead", phones=ok_phone, tasks=1),
             rec("l4", "64 Lead St", status="Cold Lead", assigned="u-jordan", phones=ok_phone, tasks=1)]
    crm = FakeAcct(leads)
    out = go({"lc": dict(lc)}, crm, "lc", callers=("Casey", "Jordan"))
    held = [p for p in out.problems if p.startswith("Leads held by someone who is not on the callers list count as unassigned")]
    check(out.tally.get("leads with no assignee") == 3 and out.tally.get("leads held by someone who is not a caller") == 2,
          "a lead held by someone who is not a caller, or by a caller who is inactive, counts as unassigned")
    check(held and "Jordan 1" in held[0] and "Morgan 1" in held[0], "and the check names who holds them: %s" % held[:1])
    check(not any("61 Lead St" in p for p in out.problems), "a lead the caller holds is not named")
    check(not crm.writes, "the lead check still writes nothing in plan mode")
    crm = FakeAcct(leads)
    crm.fail_users = True
    out = go({"lc": dict(lc)}, crm, "lc")
    check(out.tally.get("leads with no assignee") == 1 and any("could not tell who the callers are" in p for p in out.problems),
          "when the team list cannot be read, only a missing assignee counts, and the check says why")
    crm = FakeAcct(leads)
    out = go({"lc": dict(lc)}, crm, "lc", callers=())
    check(out.tally.get("leads with no assignee") == 1 and crm.user_reads == 0 and not any("callers" in p for p in out.problems),
          "with no callers named, the check is as it was: only a missing assignee counts, and the team list is not read")
    crm = FakeAcct(leads)
    out = go({"lc": dict(lc, callers=[{"name": "Casey", "uuid": "u-casey"}])}, crm, "lc")
    check(out.tally.get("leads with no assignee") == 3 and crm.user_reads == 0,
          "callers given on the stage by uuid are used without reading the team list")

    # ------------------------------------------------------------------ the business audit
    print("  business audit")
    _audit(check, base, wipe, book, go, store, activity, bizaudit, capture, goals, kpi, ledger, sweep)


def _stub_catalog(kpi, goal_status="placeholder"):
    F = kpi.Figure
    figs = {
        "goal.revenue_per_year": F("goal.revenue_per_year", "$ a year", ("TEST",), goal_status, value=360000),
        "goal.average_deal": F("goal.average_deal", "$ a deal", ("TEST",), goal_status, value=15000),
        "caller.opportunities_per_day": F("caller.opportunities_per_day", "opportunities a day", ("TEST",), "recommended", low=1, high=5),
        "owner.days_to_first_deal": F("owner.days_to_first_deal", "days", ("TEST",), "recommended", value=90),
        "owner.net_margin": F("owner.net_margin", "% net margin", ("TEST",), "recommended", low=30, high=50),
        "owner.market_share_ceiling": F("owner.market_share_ceiling", "% of the market's investor deals", ("TEST",), "recommended", value=15),
    }
    return kpi.Catalog(figures=figs, definitions={"conversation": {"min_seconds": 60, "counts_inbound_text": True,
                                                                   "counts_inbound_call": True}}, inputs={})


def _audit(check, base, wipe, book, go, store, activity, bizaudit, capture, goals, kpi, ledger, sweep) -> None:
    from .autopilot.stages import Ctx, Summary
    from .autopilot import policy
    check(bizaudit.period(date(2026, 11, 16), 3) == ("2026-10/3", "Q4 2026") and bizaudit.period(date(2026, 11, 16), 1)[1] == "Nov 2026"
          and bizaudit.period(date(2026, 2, 3), 6)[1] == "Jan to Jun 2026", "the audit period is the quarter, the month or the span asked for")
    check(bizaudit.section_band(9) == "ugly" and bizaudit.section_band(14) == "bad" and bizaudit.section_band(18) == "good"
          and bizaudit.section_band(23) == "excellent", "a section's total is banded the course's way: 5 to 10, 11 to 15, 16 to 20, 21 to 25")
    check(bizaudit.band(0.84, ((0.97, 5), (0.9, 4), (0.7, 3), (0.4, 2))) == 3 and bizaudit.band(0.1, ((0.5, 2),)) == 1
          and bizaudit.band_low(45, ((5, 5), (60, 4))) == 4 and bizaudit.band_low(3000, ((5, 5),)) == 1,
          "a share or a wait maps to its score, and below the last cut is a 1")

    wipe()
    config.DATA_DIR = base / "audit"
    (config.DATA_DIR / "kpi").mkdir(parents=True, exist_ok=True)
    kpi.override(_stub_catalog(kpi))
    rows, recs = [], []
    bdays = [d for d in (date(2026, 10, 1) + timedelta(days=i) for i in range(46)) if d.weekday() < 5 and d < date(2026, 11, 16)]
    skip = {date(2026, 10, 12), date(2026, 10, 13), date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)}
    for d in bdays:
        if d not in skip:
            rows.append(created_ev("new-%s" % d.isoformat(), _et(d.month, d.day, 10)))
    # New leads: 3, 3, 1, 4, 0, 3 a week over the six full weeks from Oct 5, each called after a known wait.
    weeks = [date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2), date(2026, 11, 9)]
    per_week = [3, 3, 1, 4, 0, 3]
    waits = [3, 8, 12, 20, 25, 40, 45, 50, 70, 90, 120, 200]
    k = 0
    for w, n in zip(weeks, per_week):
        for j in range(n):
            u = "lead%d" % k
            t = _et(w.month, w.day, 10) + timedelta(days=j)
            rows.append(status_ev(u, t, "prospecting", "new_lead"))
            if k < len(waits):
                rows.append(call_ev("o-" + u, t + timedelta(minutes=waits[k])))
            elif k == len(waits):
                # set on a call: an inbound call just before the status change (short, so it is not also a conversation)
                rows.append(call_ev("o-" + u, t - timedelta(minutes=5), kind="received", duration=30, inbound=True))
            recs.append(rec(u, "%d Lead Ave" % (100 + k), status="Cold Lead", tasks=1 if k < 10 else 0))
            k += 1
    # Ten records went Not Interested in early October; one owner was called again after the wait.
    for i in range(10):
        u = "ni%d" % i
        t = _et(10, 2 + (i % 7), 11)
        rows.append(status_ev(u, t, "Cold Lead", "not_interested"))
        recs.append(rec(u, "%d Quiet Ln" % (200 + i), status="not_interested"))
        if i == 0:
            rows.append(call_ev("o-" + u, t + timedelta(days=28)))
        else:
            rows.append(call_ev("o-" + u, t + timedelta(days=3)))       # called again too soon to count
    # Tasks: 30 made, 3 completed, 20 deleted by a sequence.
    rows += [task_ev("task.created", "lead0", "tk%d" % i, _et(10, 5, 9) + timedelta(hours=i)) for i in range(30)]
    rows += [task_ev("task.completed", "lead0", "tk%d" % i, _et(10, 20, 9) + timedelta(hours=i)) for i in range(3)]
    rows += [task_ev("task.deleted", "lead0", "tk%d" % i, _et(10, 22, 9) + timedelta(hours=i)) for i in range(3, 23)]
    # Twelve calls of a minute or more; a person's note follows six (three on the owner, three on the record).
    for i in range(12):
        u = "cv%d" % i
        t = _et(11, 2, 10) + timedelta(days=i % 9, hours=i % 3)
        rows.append(call_ev("o-" + u, t, kind="answered", duration=120))
        recs.append(rec(u, "%d Talk Rd" % (300 + i), status="prospecting"))
        if i < 3:
            rows.append(note_ev("o-" + u, t + timedelta(minutes=10), kind="owner.message.added"))
        elif i < 6:
            rows.append(note_ev(u, t + timedelta(hours=2)))
        elif i == 6:
            rows.append(note_ev(u, t + timedelta(minutes=5), source="sequence", author="system"))    # a machine's note
    activity._store(rows)
    for d in bdays[:28]:
        capture.save(d.isoformat(), "Casey", {"dials": 160, "answered": 100, "conversations": 20, "talk_s": 3000, "owners": 120,
                                              "numbers": 150})
    kdir = config.DATA_DIR / "kpi"
    for name in ("lost_deals_2026-10_2026-11-01.json", "lost_deals_2026-09_2026-10-01.json", "lost_deals_2026-05_2026-06-01.json",
                 "goal_sheet_2026-11-04.json", "goal_sheet_2026-11-11.json", "goal_sheet_2026-09-01.json"):
        (kdir / name).write_text("{}", encoding="utf-8")
    biz = {"kind": "bizaudit", "owner": "analysis", "mode": "plan", "schedule": "1st 07:45", "sync": False, "weekly_leads_floor": 3}
    crm = FakeAcct(recs)
    out = go({"biz": dict(biz)}, crm, "biz")
    saved = sorted((config.DATA_DIR / "kpi").glob("business_audit_*.json"))
    report = json.loads(saved[-1].read_text(encoding="utf-8")) if saved else {"questions": []}
    q = {x["id"]: x for x in report["questions"]}
    check(q.get("M1", {}).get("score") == 3 and "27 of 32 business days" in q["M1"]["evidence"],
          "M1 scores the share of business days new records landed: %s" % q.get("M1"))
    check(q.get("M4", {}).get("score") == 3 and "3, 3, 1, 4, 0, 3" in q["M4"]["evidence"],
          "M4 scores the weeks at the lead floor: %s" % q.get("M4"))
    check(q.get("M5", {}).get("score") == 2 and "1 of 10" in q["M5"]["evidence"],
          "M5 scores the Not Interested owners worked again after their wait, not before it: %s" % q.get("M5"))
    check(q.get("S1", {}).get("score") == 4 and "median 45 min" in q["S1"]["evidence"] and "1 never called" in q["S1"]["evidence"]
          and "1 set on a call" in q["S1"]["evidence"], "S1 scores the median minutes to a first call: %s" % q.get("S1"))
    check(q.get("S2", {}).get("score") == 2 and "10 of 14 leads hold an open task" in q["S2"]["evidence"]
          and "3 of 30 tasks" in q["S2"]["evidence"] and "20 deleted by a sequence" in q["S2"]["evidence"],
          "S2 takes the lower of task coverage and tasks completed: %s" % q.get("S2"))
    check(q.get("S4", {}).get("score") == 3 and "6 of the last 12 calls" in q["S4"]["evidence"],
          "S4 scores conversations a person's note followed, on the owner or the record, a machine's note not counting: %s" % q.get("S4"))
    check(q.get("S5", {}).get("score") == 3 and "Sep, Oct" in q["S5"]["evidence"],
          "S5 reads the monthly lost-deal reports and stops at 3: %s" % q.get("S5"))
    check(q.get("K1", {}).get("score") == 2 and "placeholder" in q["K1"]["evidence"],
          "K1 scores how settled the goals are, a placeholder goal at 2: %s" % q.get("K1"))
    check(q.get("K2", {}).get("score") == 2 and "2 time(s) in the last 35 days" in q["K2"]["evidence"],
          "K2 counts the goal sheets made in the last 35 days: %s" % q.get("K2"))
    # The call log here starts Oct 5, so the scorecard is judged from then: two captured days before it do not count.
    check(q.get("K4", {}).get("score") == 4 and "26 of 30 business days" in q["K4"]["evidence"],
          "K4 scores the business days a scorecard row was written, from where the call log starts: %s" % q.get("K4"))
    check(q.get("O1", {}).get("score") is None and "no deal is recorded" in q["O1"]["evidence"],
          "O1 waits on the owner when no deal is recorded, and says so: %s" % q.get("O1"))
    waiting = sorted(i for i, x in q.items() if x["score"] is None)
    check(waiting == ["K3", "K5", "M2", "M3", "O1", "O2", "O3", "O4", "O5", "S3"],
          "the questions only the owner can answer are listed for him: %s" % waiting)
    text = "\n".join([out.head] + out.lines)
    check(out.head.startswith("Business audit for Q4 2026: marketing 8 so far, sales management 12 so far, operations 0 so far, KPIs 8 so far")
          and "10 question(s) wait on your answer" in out.head, "the head gives each section's total so far: %s" % out.head)
    fix = [ln for ln in out.lines if ln.startswith("Fix first")]
    check([ln.split(" ")[2] for ln in fix] == ["M5", "S2", "K1"],
          "critical items come in the course's order, marketing first, then by score: %s" % fix)
    check(any(ln.startswith("Your answer is needed on M2, M3, S3, O1, O2, O3, O4, O5, K3, K5") for ln in out.lines),
          "the owner's questions are named with how to answer them")
    check(any(ln.startswith("Read from the log copy since Oct 1") for ln in out.lines), "the report says how much log it read")
    check(not crm.writes and out.writes == 0, "the audit writes nothing to the account")
    check(not any(ch in text for ch in (chr(0x2014), chr(0x2013))), "the audit's words carry no dash characters")
    posts = sweep.posts({"id": 7, "workflow": "biz", "source": "schedule"}, out.as_dict())
    check([p["kind"] for p in posts] == ["chat"], "the audit is a report and goes to the team channel")

    # The owner's answers fill the sections; an answer overrides the data and both are kept.
    answers = {"M1": 5, "M2": 1, "M3": 3, "S3": 4, "K3": 2, "K5": 3, "O1": 3, "O2": 4, "O3": 3, "O4": 2, "O5": 2}
    out = go({"biz": dict(biz, answers=answers)}, FakeAcct(recs), "biz")
    report = json.loads(sorted((config.DATA_DIR / "kpi").glob("business_audit_*.json"))[-1].read_text(encoding="utf-8"))
    q = {x["id"]: x for x in report["questions"]}
    check(q["M1"]["score"] == 5 and q["M1"]["source"] == "answer" and q["M1"]["data_score"] == 3,
          "an owner's answer overrides a score from the data, and the data's score is kept beside it")
    check(out.head.startswith("Business audit for Q4 2026: marketing 14 of 25 (bad), sales management 16 of 25 (good), "
                              "operations 14 of 25 (bad), KPIs 13 of 25 (bad)") and "wait on your answer" not in out.head,
          "with every question answered each section is totalled out of 25 and banded: %s" % out.head)
    fix = [ln for ln in out.lines if ln.startswith("Fix first")]
    check(fix and fix[0].startswith("Fix first, M2 (1 of 5, you scored it)"), "the owner's own 1 is fixed first: %s" % fix[:1])
    check(not any(ln.startswith("Your answer is needed") for ln in out.lines), "nothing is left for the owner to answer")

    # A score from the data is capped: a true 5 is the owner's to claim.
    keep = bizaudit.SCORERS["M1"]
    bizaudit.SCORERS["M1"] = lambda env: (5, "every business day")
    try:
        out = go({"biz": dict(biz)}, FakeAcct(recs), "biz")
        q = {x["id"]: x for x in json.loads(sorted((config.DATA_DIR / "kpi").glob("business_audit_*.json"))[-1].read_text(
            encoding="utf-8"))["questions"]}
        capped = q["M1"]["score"]
        out = go({"biz": dict(biz, max_data_score=5)}, FakeAcct(recs), "biz")
        q = {x["id"]: x for x in json.loads(sorted((config.DATA_DIR / "kpi").glob("business_audit_*.json"))[-1].read_text(
            encoding="utf-8"))["questions"]}
        check(capped == 4 and q["M1"]["score"] == 5, "a score read from the data stops at max_data_score (4 by default)")
        bizaudit.SCORERS["M1"] = lambda env: (_ for _ in ()).throw(RuntimeError("a broken read"))
        out = go({"biz": dict(biz)}, FakeAcct(recs), "biz")
        q = {x["id"]: x for x in json.loads(sorted((config.DATA_DIR / "kpi").glob("business_audit_*.json"))[-1].read_text(
            encoding="utf-8"))["questions"]}
        check(q["M1"]["score"] is None and "could not be read" in q["M1"]["evidence"] and q["M4"]["score"] == 3,
              "one question that fails to read waits on the owner, and the other questions are still scored")
    finally:
        bizaudit.SCORERS["M1"] = keep

    # Net margin, once the goal sheet's review can read one.
    keep_review = goals.owner_review
    goals.owner_review = lambda ctx, cat, today: (["Quarterly owner review.", "Net margin, last 91 days: 35%."], {"net_margin": 35.0})
    try:
        out = go({"biz": dict(biz)}, FakeAcct(recs), "biz")
        q = {x["id"]: x for x in json.loads(sorted((config.DATA_DIR / "kpi").glob("business_audit_*.json"))[-1].read_text(
            encoding="utf-8"))["questions"]}
        check(q["O1"]["score"] == 3 and q["O1"]["evidence"].startswith("Net margin"),
              "O1 scores a known net margin (35 percent is a 3) and quotes the review's line")
    finally:
        goals.owner_review = keep_review

    # Goals the owner confirmed read higher; no catalog at all reads as a 1, and nothing else breaks.
    kpi.override(_stub_catalog(kpi, goal_status="confirmed"))
    out = go({"biz": dict(biz)}, FakeAcct(recs), "biz")
    q = {x["id"]: x for x in json.loads(sorted((config.DATA_DIR / "kpi").glob("business_audit_*.json"))[-1].read_text(
        encoding="utf-8"))["questions"]}
    check(q["K1"]["score"] == 4, "confirmed goals score K1 at 4")
    kpi.override(_stub_catalog(kpi))

    # Too little history is not guessed.
    wipe()
    config.DATA_DIR = base / "thin"
    (config.DATA_DIR / "kpi").mkdir(parents=True, exist_ok=True)
    activity._store([created_ev("thin1", NOW - timedelta(days=3)), status_ev("thin2", NOW - timedelta(days=2), None, "new_lead")])
    out = go({"biz": dict(biz)}, FakeAcct([]), "biz")
    q = {x["id"]: x for x in json.loads(sorted((config.DATA_DIR / "kpi").glob("business_audit_*.json"))[-1].read_text(
        encoding="utf-8"))["questions"]}
    check(q["M1"]["score"] is None and "business day(s)" in q["M1"]["evidence"] and q["M4"]["score"] is None
          and q["S1"]["score"] is None and q["M5"]["score"] is None,
          "a question with too little history waits on the owner with what the data does show")
    check(q["S5"]["score"] == 1 and q["K2"]["score"] == 1, "no lost-deal report and no goal sheet each read as a 1")

    # The quarterly clock: a scheduled run audits once a quarter, an ask always answers.
    print("  quarterly clock")
    wipe()
    config.DATA_DIR = base / "clock"
    (config.DATA_DIR / "kpi").mkdir(parents=True, exist_ok=True)
    first, _ = store.enqueue("analysis", "biz", source="schedule", force=True)
    out = go({"biz": dict(biz)}, FakeAcct([]), "biz", task_row={"id": first})
    check(out.head.startswith("Business audit for Q4 2026") and store.kv_get("autopilot:bizaudit:biz") == "2026-10/3",
          "the first scheduled run of the quarter audits and marks the quarter done")
    again, _ = store.enqueue("analysis", "biz", source="schedule", force=True)
    out = go({"biz": dict(biz)}, FakeAcct([]), "biz", now=NOW + timedelta(days=15), task_row={"id": again})
    check(out.head == "" and not out.lines and sweep.posts({"id": again, "workflow": "biz", "source": "schedule"}, out.as_dict()) == [],
          "a second scheduled run in the same quarter says nothing")
    ask, _ = store.enqueue("analysis", "biz", source="slack", force=True)
    out = go({"biz": dict(biz)}, FakeAcct([]), "biz", now=NOW + timedelta(days=16), task_row={"id": ask})
    check(out.head.startswith("Business audit for Q4 2026") and store.kv_get("autopilot:bizaudit:biz") == "2026-10/3",
          "an ask from Slack always answers and does not use up the quarter")
    nxt, _ = store.enqueue("analysis", "biz", source="schedule", force=True)
    out = go({"biz": dict(biz)}, FakeAcct([]), "biz", now=datetime(2027, 1, 1, 12, 45, tzinfo=timezone.utc), task_row={"id": nxt})
    check(out.head.startswith("Business audit for Q1 2027") and store.kv_get("autopilot:bizaudit:biz") == "2027-01/3",
          "the next quarter's first scheduled run audits again")
    monthly, _ = store.enqueue("analysis", "biz2", source="schedule", force=True)
    out = go({"biz2": dict(biz, every_months=1)}, FakeAcct([]), "biz2", task_row={"id": monthly})
    check(out.head.startswith("Business audit for Nov 2026"), "every_months 1 audits monthly, for an operator starting out")
