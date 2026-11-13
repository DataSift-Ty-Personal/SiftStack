"""Lead management selftest: the board routes, the lead clock, speed to lead, drips, the next-action
tasks, new distress on a lead, the nurture bucket, appointments and clean exits. Zero network: the
autopilot selftest's fake DataSift, grown a lead board, cards, tasks and task presets, with a fake
account log. Every name, street, number and id here is invented.
"""
from __future__ import annotations

import copy
import csv
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, ET, T0, FakeCRM, _rec

MOBILE = {"number": "8655550141", "type": "MOBILE", "tags": ["Dial First"], "status": "CORRECT"}
MOBILE2 = {"number": "8655550142", "type": "MOBILE", "tags": ["Dial Second"], "status": "UNKNOWN"}
LANDLINE = {"number": "8655550143", "type": "LANDLINE", "tags": ["Dial First"], "status": "UNKNOWN"}

LEAD_COLS = ["New Lead (Unqualified)", "No Contact New Lead (Unqualified)", "Nurture New Lead (Unqualified)",
             "COLD Leads (Qualified)", "WARM Leads (Qualified)", "HOT Leads (Qualified)", "Send to Acquisitions",
             "Needs Review", "Ghosting Lead", "Not Interested Anymore", "Dead Lead"]
OFFER_COLS = ["Appointment Booked", "Offer Made", "Offer Rejected", "Send Back to Lead Management"]
TASK_PRESETS = ["Call New Lead", "No Contact New Lead", "Nurture New Lead", "COLD Follow-Up", "WARM Follow-Up",
                "HOT Follow-Up", "Send Back to LM"]

# The exact lines for autopilot/registry.py EXTRA. The selftest loads each one, and registers any
# that is not in the table yet for the length of this section, so it passes before and after the
# orchestrator adds them.
REGISTRY_LINES = {
    "leadclock": ("leadclock", "run_leadclock", "free", False, False),
    "drips": ("drips", "run_drips", "outreach", False, False),
    "leadtasks": ("leadtasks", "run_leadtasks", "free", False, False),
    "appointments": ("appointments", "run_appointments", "free", False, False),
    "exits": ("exits", "run_exits", "free", False, False),
}

# The exact stage blocks for team/autopilot.yaml. Parsed below, so a block that would not load fails here.
PLAYBOOK_BLOCKS = """
  # ------------------------------------------------------------------ Phase 2: lead management on the clock
  # A lead is its own lane, and these are the only stages that may touch one (gate_allow: [lead]).
  # The board's columns are learned from one card's timeline and remembered for a week. If a stage
  # says it cannot learn them, give it the id of one column that holds a card under seed_columns,
  # or every column under column_ids.
  speed_to_lead:
    kind: leadclock
    job: speed
    owner: sales
    mode: "plan"
    schedule: "every 5 min 09:00-18:00 Mon-Fri"
    summary: Each new lead timed from the status change to the first outbound call; the assignee is told at 5 minutes and the owner at 15.
    # Plan 2.2, the clock half. It reads the shared log copy and writes nothing to DataSift, so
    # switching it to live changes one thing only, that every ping posts (a plan run posts once a
    # day). A lead that arrives at night is timed from the next opening. The text to the seller
    # belongs to the texting stage.
    assignee_minutes: 5
    owner_minutes: 15
    lead_manager: ""            # decision D13. Name the lead manager and the 5-minute line goes to that name; empty tells the record's assignee
    owner_label: Owner          # how the 15-minute line addresses the owner (a name, or a Slack mention)
    people: {}                  # user id to first name, for an assignee the call log has never named
    window_hours: 24
    max_reads: 20

  lead_clock:
    kind: leadclock
    owner: ops
    mode: "plan"
    schedule: "07:40 Mon-Fri"
    summary: Lead cards moved between phases on time, measured from the last real conversation, never from time in a column.
    # Plan 2.5, decision D15. A real conversation is an answered call of a minute or more, an inbound
    # text or a note a person wrote. A card holding a future task a person dated is never moved.
    gate_allow: [lead]
    board: Lead Management
    offers_board: Acquisitions
    first_run: staged           # the first live run stages its moves for a person to release
    sequences_fire: unknown     # true, false or unknown. Unknown moves the card and checks on the next run that the column's sequence did its work
    clear_old_tasks: true       # when the stage does a sequence's work itself, the old column's sequence-made tasks go, as the sequence would have done
    settle_minutes: 30
    trust_column_time: false    # true lets a card's time in its column stand in for what the 200-day log copy cannot prove
    future_task_filter: true    # also ask the board which cards hold a task due in the future
    rejected_offers: true       # a card a week in Offer Rejected goes back to the lead manager
    conversation_seconds: 60
    marginal_tags: [Marginal Lead]
    limits:
      new_lead_hours: 24
      new_lead_needs_dial: true # a new lead nobody dialed is named, not filed as No Contact
      no_contact_tries: 5
      nurture_strong_days: 180
      nurture_marginal_days: 60
      nurture_touches: 31
      hot_quiet_days: 14
      warm_quiet_days: 60
      cold_quiet_days: 365
      offer_rejected_days: 7
    column_ids: {}              # {"<board title>": {"<column title>": "<column id>"}}
    seed_columns: {}            # {"<board title>": "<the id of any one column that holds a card>"}
    max_moves: 25
    max_reads: 120

  lead_drips:
    kind: drips
    owner: ops
    mode: "plan"
    schedule: "every 60 min 09:00-18:00 Mon-Fri"
    summary: Ghosting, cold and said-no leads kept on a slow drip of staged texts, emails and review tasks; a reply or a status change ends it.
    # Plan 2.6. Runs on the brain's clock. A text or an email is staged for a person and nothing is
    # sent from here. The three default drips are in drips.py; a `drips:` block here changes one
    # or switches it off (cold: false). An email step adds a tag, and a sequence built in DataSift
    # on that tag sends the email. Until that sequence exists the email step is passed over.
    gate_allow: [lead, not_interested]
    never: [Hot Lead]
    sender_name: ""             # signs the texts. Empty uses the assignee's first name, else the text goes unsigned
    lead_manager_id: ""         # decision D13. A user id here gets the review tasks; empty gives them to the record's assignee
    conversation_seconds: 60
    max_chars: 160
    daily_items: 25
    max_reads: 60

  lead_tasks:
    kind: leadtasks
    owner: ops
    mode: "plan"
    schedule: "08:05 Mon-Fri"   # five minutes ahead of lead_check, so the check lists what could not be fixed
    summary: A lead with no open task gets the task its column's own sequence would have created.
    # Plan 2.7. A ghosting lead with a drip step ahead of it already has its next action.
    gate_allow: [lead]
    board: Lead Management
    task_assignee: preset       # preset keeps the task preset's own assignee; record gives the task to the record's assignee
    task_for: {}                # a status to a task preset title, where the column's own preset is not the right one
    proof_preset: "Board - No Tasks (no next action)"
    max_create: 50
    max_reads: 80

  lead_weekly:
    kind: leadtasks
    job: weekly
    owner: ops
    mode: "plan"
    schedule: "Wed 08:20"
    summary: The weekly deeper pass on every lead, card against status, notes after conversations, overdue and undated tasks, and the check presets themselves.
    # Plan 2.7. Reads only.
    board: Lead Management
    check_presets: ["3. Leads Missing Tasks"]
    column_ids: {}
    seed_columns: {}

  lead_nurture:
    kind: leadtasks
    job: nurture
    owner: sales
    mode: "plan"
    schedule: "08:15 Mon-Fri"
    summary: Every Follow Up record on a confirmed number holds one dated call task 90 days out.
    # Plan 2.13. The cold drip in lead_drips covers the months between.
    gate_allow: [lead]
    statuses: [follow_up]
    due_days: 90
    require_confirmed: true
    lead_manager_id: ""

  lead_appointments:
    kind: appointments
    owner: sales
    mode: "plan"
    schedule: "every 30 min 08:00-19:00 Mon-Sat"
    summary: Every walkthrough gets a post and comps when it is booked, a confirm call the day before and a recorded outcome that sets the next move.
    # Plan 2.10. An appointment is a task with a start, an end and an outcome, made in DataSift or
    # through the booking workflow. `outcomes:` changes what an outcome makes the next task.
    gate_allow: [lead]
    offers_board: Acquisitions
    comps_workflow: quick_screen
    comps_agent: sales
    outcome_grace_hours: 24
    give_up_days: 14
    check_column: true          # once a day, count cards in Appointment Booked that have no appointment
    lead_manager_id: ""
    column_ids: {}
    seed_columns: {}
    max_reads: 40

  lead_exits:
    kind: exits
    owner: ops
    mode: "plan"
    schedule: "1st 07:10"
    summary: Dead and Not Interested cards leave the board after 30 days with the status kept; a dead lead returns to marketing after 180 days if the house has not sold.
    # Plan 2.14, decision D27 (yours). remarket_dead false keeps dead leads dead.
    board: Lead Management
    card_days: 30
    remarket_dead: true
    remarket_days: 180
    column_ids: {}
    seed_columns: {}
    max_remove: 40
    max_remarket: 25
    max_reads: 120
"""


API = "/api/internal"


def _uid(n: int) -> str:
    return "00000000-0000-4000-8000-%012d" % n


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _et(month: int, day: int, hour: int = 11, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ET).astimezone(timezone.utc)


def call_ev(i, owner, when, kind="made", seconds=0, inbound=False, caller="Adriana"):
    return {"id": "call-%s" % i, "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"destination_number": "8655550100" if inbound else "8655550141",
                                 "origin_number": "8655550141" if inbound else "8655550100",
                                 "direction": "inbound" if inbound else "outbound", "duration": seconds, "source": "kixie",
                                 "uuid": "c-%s" % i, "extra": {}, "user": {"first_name": caller, "uuid": "u-adriana"}}}}


def status_ev(i, rec, when, old, new, source="app"):
    return {"id": "st-%s" % i, "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": source, "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"property": {"status": [old, new]}}}


def text_ev(i, owner, when, inbound=True):
    return {"id": "sms-%s" % i, "event_type": "owner.sms.%s" % ("received" if inbound else "sent"), "resource": owner,
            "resource_type": "owner", "source": "smrtphone", "author": "system", "timestamp": _ts(when),
            "payload": {"sms": {"direction": "inbound" if inbound else "outbound", "origin_number": "8655550141",
                                "destination_number": "8655550100", "status": "delivered"}}}


def note_ev(i, rec, when, source="app", author="person@example.com"):
    return {"id": "note-%s" % i, "event_type": "property.message.added", "resource": rec, "resource_type": "property",
            "source": source, "author": author, "timestamp": _ts(when), "payload": {}}


class LeadCRM(FakeCRM):
    """The fake DataSift with the lead half: two boards, cards, tasks, task presets and the board's
    own sequences (which fire on an API move only when `fire` is set)."""

    def __init__(self, records, fire=True):
        super().__init__(records)
        self.fire = fire
        self.now = T0
        self.n = 0
        self.boards_, self.cols, self.cards, self.tasks = {}, {}, {}, {}
        self.task_presets_ = {t: "tp-%d" % i for i, t in enumerate(TASK_PRESETS)}
        self.log_rows = []
        self.http = []
        self.names_record = False      # card rows carry no record, as in the published shape
        self.log_ids = False           # the log names a column by title only
        self.detail_columns = False
        self.stuck = False             # a card PATCH that answers and moves nothing
        self.no_task_id = False        # a task create that answers with no id
        self.sticky_cards = False      # a card DELETE that answers and removes nothing
        self.lenient = False           # a timeline route that finds the card whatever column the path names
        self.tags_known = None         # None: every tag exists
        self.lead_board = self.add_board("Lead Management", LEAD_COLS)
        self.offer_board = self.add_board("Acquisitions", OFFER_COLS)

    # ---- building the scene
    def _next(self) -> int:
        self.n += 1
        return self.n

    def add_board(self, title, cols):
        b = _uid(self._next())
        self.boards_[b] = {"uuid": b, "title": title, "order": len(self.boards_)}
        for i, t in enumerate(cols):
            self.cols[_uid(self._next())] = {"title": t, "order": i, "board": b}
        return b

    def col(self, title):
        return next(u for u, c in self.cols.items() if c["title"] == title)

    def ids(self):
        out = {}
        for u, c in self.cols.items():
            out.setdefault(self.boards_[c["board"]]["title"], {})[c["title"]] = u
        return out

    def _card_row(self, card, when, old, new, source="app", by=""):
        board = self.boards_[self.cols[new]["board"]]["title"]
        extra = {"card": {"prop": card["prop"], "column": [self.cols[old]["title"] if old else None, self.cols[new]["title"]],
                          "board": board}}
        if by:
            extra["source"] = {"title": by}
        body = {"prop": card["prop"]}
        if self.log_ids:
            body["column"] = [old, new]
        return {"id": "cd-%d" % self._next(), "event_type": "card.moved", "resource": card["uuid"], "resource_type": "card",
                "source": source, "author": "person@example.com", "timestamp": _ts(when),
                "payload": {"card": body, "extra_info": extra}}

    def add_card(self, prop, column, entered, log=True):
        cu = "card-%d" % self._next()
        card = {"uuid": cu, "prop": prop, "column": self.col(column), "since": entered, "spent": {}}
        self.cards[cu] = card
        if log:
            self.log_rows.append(self._card_row(card, entered, None, card["column"]))
        return cu

    def add_task(self, prop, title, due=None, kind="task", source="sequence", by="", created=None, log=True, **extra):
        tid = "task-%d" % self._next()
        t = {"uuid": tid, "title": title, "due_date": _iso(due), "event_type": kind, "assigned_to_property": prop,
             "completed": None, "outcome_status": None, "outcome_result": None, "start_time": None, "end_time": None,
             "assigned_to_user": None, "address": None}
        t.update(extra)
        self.tasks[tid] = t
        if log:
            info = {"source": {"title": by}} if by else {}
            self.log_rows.append({"id": "tk-%d" % self._next(), "event_type": "task.created", "resource": tid,
                                  "resource_type": "task", "source": source,
                                  "author": "person@example.com" if source == "app" else "system",
                                  "timestamp": _ts(created or self.now - timedelta(days=1)),
                                  "payload": {"task": {"title": title, "assigned_to_property": prop, "event_type": kind,
                                                       "due_date": _iso(due)}, "extra_info": info}})
        return tid

    def card_of(self, prop):
        return next((c for c in self.cards.values() if c["prop"] == prop), None)

    def column_of(self, prop):
        c = self.card_of(prop)
        return self.cols[c["column"]]["title"] if c else ""

    def open_tasks(self, prop):
        return [t for t in self.tasks.values() if t["assigned_to_property"] == prop and not t["completed"]]

    def wrote(self, method=None):
        return [(m, p) for m, p in self.http if m != "GET" and (method is None or m == method)]

    # ---- what the engine reads
    def _row(self, r):
        row = super()._row(r)
        mine = [t for t in self.tasks.values() if t["assigned_to_property"] == r["uuid"]]
        done = sum(1 for t in mine if t["completed"])
        row["tasks"] = {"total": len(mine), "complete": done, "incomplete": len(mine) - done}
        return row

    def tag_uuid(self, title):
        if self.tags_known is not None and title not in self.tags_known:
            return ""
        return super().tag_uuid(title)

    def _sequence(self, prop, column):
        """What the board's own sequence does when a card lands in a column."""
        from .autopilot import leadroutes as routes
        role = "lead" if self.cols[column]["board"] == self.lead_board else "offers"
        phase = routes.title_phase(self.cols[column]["title"], routes.PHASES, role)
        if not phase:
            return
        row = routes.PHASES[phase]
        at = self.now + timedelta(seconds=2)
        for t in self.open_tasks(prop):
            del self.tasks[t["uuid"]]
            self.log_rows.append({"id": "td-%d" % self._next(), "event_type": "task.deleted", "resource": t["uuid"],
                                  "resource_type": "task", "source": "sequence", "author": "system", "timestamp": _ts(at),
                                  "payload": {"task": {"assigned_to_property": prop, "title": t["title"]}}})
        if row["preset"]:
            self.add_task(prop, row["preset"], due=self.now + timedelta(days=2), by="[NEW] %s" % phase, created=at)
        if row["status"] and self.recs[prop].get("status") != row["status"]:
            old = self.recs[prop].get("status")
            self.recs[prop]["status"] = row["status"]
            self.log_rows.append(status_ev("seq-%d" % self._next(), prop, at, old, row["status"], source="sequence"))
        if row.get("then"):
            card = self.card_of(prop)
            dest = self.col(next(t for t in LEAD_COLS if t.startswith(routes.PHASES[row["then"]]["column"])))
            self.log_rows.append(self._card_row(card, at, card["column"], dest, source="sequence", by="[MOVE] back"))
            card["column"], card["since"] = dest, self.now

    def call(self, path, method="GET", body=None, headers=None):
        p, _, qs = path.partition("?")
        q = dict(urllib.parse.parse_qsl(qs))
        self.http.append((method, p))
        body = body or {}
        # Matched below without the API prefix: the route audit reads every string that starts with
        # the prefix as a route this code calls, and a pattern is not a route.
        p = p[len(API):] if p.startswith(API) else p
        if p == "/siftline/board/":
            return {"results": [dict(b) for b in self.boards_.values()]}
        m = re.fullmatch(r"/siftline/board/column/([^/]+)/card/([^/]+)/timeline/", p)
        if m:
            card = self.cards.get(m.group(2))
            if not card or (card["column"] != m.group(1) and not self.lenient):
                raise LookupError("404")
            out = {}
            for u, c in self.cols.items():
                if c["board"] == self.cols[card["column"]]["board"]:
                    secs = card["spent"].get(u, 0) + ((self.now - card["since"]).total_seconds() if u == card["column"] else 0)
                    out[u] = {"title": c["title"], "order": c["order"], "time": secs, "current": u == card["column"]}
            return out
        m = re.fullmatch(r"/siftline/board/column/([^/]+)/card/([^/]+)/", p)
        if m:
            card = self.cards.get(m.group(2))
            if not card or card["column"] != m.group(1):
                raise LookupError("404")
            if method == "GET":
                return {"uuid": card["uuid"], "order": 0}
            if method == "DELETE":
                if not self.sticky_cards:
                    del self.cards[card["uuid"]]
                return None
            if method == "PATCH":
                if self.stuck:
                    return {"uuid": card["uuid"]}
                old, new = card["column"], body["column"]
                card["spent"][old] = card["spent"].get(old, 0) + (self.now - card["since"]).total_seconds()
                card["column"], card["since"] = new, self.now
                self.log_rows.append(self._card_row(card, self.now, old, new, source="internal-api"))
                if self.fire:
                    self._sequence(card["prop"], new)
                return {"uuid": card["uuid"]}
        m = re.fullmatch(r"/siftline/board/column/([^/]+)/card/", p)
        if m:
            if method == "POST":
                cu = "card-%d" % self._next()
                self.cards[cu] = {"uuid": cu, "prop": body["prop"], "column": m.group(1), "since": self.now, "spent": {}}
                return {"uuid": cu}
            rows = [c for c in self.cards.values() if c["column"] == m.group(1)]
            if q.get("task_due_date_after"):
                after = datetime.fromisoformat(q["task_due_date_after"].replace("Z", "+00:00"))
                rows = [c for c in rows if any(t["due_date"] and datetime.fromisoformat(t["due_date"].replace("Z", "+00:00")) >= after
                                               for t in self.open_tasks(c["prop"]))]
            out = [dict({"uuid": c["uuid"], "order": i}, **({"prop": c["prop"]} if self.names_record else {}))
                   for i, c in enumerate(rows)]
            off, lim = int(q.get("offset") or 0), int(q.get("limit") or 100)
            return {"count": len(out), "results": out[off:off + lim]}
        m = re.fullmatch(r"/siftline/board/([^/]+)/", p)
        if m:
            b = dict(self.boards_[m.group(1)])
            if self.detail_columns:
                b["columns"] = [{"uuid": u, "title": c["title"], "order": c["order"]} for u, c in self.cols.items()
                                if c["board"] == m.group(1)]
            return b
        if p == "/task-preset/":
            return {"results": [{"uuid": u, "title": t} for t, u in self.task_presets_.items()]}
        if p == "/task/create-by-preset/":
            title = next(t for t, u in self.task_presets_.items() if u == body["task_preset"])
            tid = self.add_task(body["assigned_to_property"], title, due=body["end_of_day"], source="internal-api", created=self.now)
            self.tasks[tid]["assigned_to_user"] = {"uuid": body["assigned_to_user"]} if body.get("assigned_to_user") else None
            return {} if self.no_task_id else {"uuid": tid, "title": title}
        if p == "/task/" and method == "POST":
            tid = self.add_task(body["assigned_to_property"], body["title"], due=body.get("due_date"),
                                kind=body.get("event_type") or "task", source="internal-api", created=self.now,
                                start_time=body.get("start_time"), end_time=body.get("end_time"), address=body.get("address"),
                                assigned_to_user=({"uuid": body["assigned_to_users"][0]} if body.get("assigned_to_users") else None),
                                notes=body.get("notes"))
            return {} if self.no_task_id else {"uuid": tid}
        m = re.fullmatch(r"/task/([^/]+)/set-outcome/", p)
        if m:
            self.tasks[m.group(1)].update(body)
            return dict(self.tasks[m.group(1)])
        m = re.fullmatch(r"/task/([^/]+)/", p)
        if m:
            if m.group(1) not in self.tasks:
                raise LookupError("404")
            if method == "DELETE":
                gone = self.tasks.pop(m.group(1))
                self.log_rows.append({"id": "td-%d" % self._next(), "event_type": "task.deleted", "resource": gone["uuid"],
                                      "resource_type": "task", "source": "internal-api", "author": "system",
                                      "timestamp": _ts(self.now),
                                      "payload": {"task": {"assigned_to_property": gone["assigned_to_property"]}}})
                return None
            return dict(self.tasks[m.group(1)])
        m = re.fullmatch(r"/property/([^/]+)/add-lists/", p)
        if m:
            self.recs[m.group(1)].setdefault("lists", []).append(body["lists"])
            self.writes.append(("add_list", m.group(1), body["lists"]))
            return None
        return super().call(path, method, body, headers)


def _iso(v):
    if v is None or isinstance(v, str):
        return v
    return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lead(u, street, status, **kw):
    kw.setdefault("phones", [dict(MOBILE)])
    kw.setdefault("assigned_to", "u-adriana")
    r = _rec(u, street, status=status, **kw)
    r["tags"] = [t for t in kw.get("tags", [])]
    r.setdefault("lists", [])
    return r


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import policy, registry
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    config.MODE = "live"
    store.init()
    saved = (policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS)
    added = {k: row for k, row in REGISTRY_LINES.items() if k not in registry.EXTRA}
    registry.EXTRA.update(added)
    policy.KINDS = tuple(dict.fromkeys(policy.KINDS + tuple(added)))
    policy.SPEND_BY_KIND.update({k: row[2] for k, row in added.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + tuple(k for k, row in added.items() if not row[3])))
    try:
        _run(check, tmp, store, policy, registry)
    finally:
        for k in added:
            registry.EXTRA.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved[0], saved[2]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved[1])
        config.MODE = "dry"


def _run(check, tmp, store, policy, registry) -> None:
    import importlib

    import yaml

    from .autopilot import activity, appointments, drips, exits, leadclock, leadtasks, ledger, sweep
    from .autopilot import leadroutes as routes
    from .autopilot.stages import Ctx, Summary

    def wipe():
        with store._LOCK, store._db() as c:
            for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_log", "ap_phones"):
                c.execute("DELETE FROM " + table)
            c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")

    def prime(crm, now, old_call_days=44):
        """Load the fake's log into the shared copy as if it had been synced up to an hour ago, with
        one old dial to a stranger so the copy reaches back `old_call_days`. One connection for the
        marks: the sync itself is tested in phase 0, and walking 45 days of it here buys nothing."""
        crm.reach = now - timedelta(days=old_call_days) if old_call_days else getattr(crm, "reach", None)
        mark = (now - timedelta(hours=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with store._LOCK, store._db() as c:
            c.execute("DELETE FROM ap_log")
            c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:log:%'")
            c.executemany("INSERT INTO kv (key, value, at) VALUES (?,?,?)",
                          [("autopilot:log:%s:through" % g, mark, mark) for g in activity.GROUPS])
        rows = list(crm.log_rows)
        if crm.reach is not None:
            rows.append(call_ev("reach", "o-stranger", crm.reach))
        activity._store(rows)
        crm.now = now

    ids = LeadCRM([]).ids()
    base = {"owner": "ops", "mode": "live", "schedule": "07:40 Mon-Fri", "gate_allow": ["lead"], "column_ids": ids}
    book = {
        "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
        "caps": {"daily_usd": 25, "record_usd": 1.0},
        "people": {"callers": ["Adriana"]},
        "stages": {
            "l_clock": dict(base, kind="leadclock", first_run="auto", sequences_fire=True),
            "l_staged": dict(base, kind="leadclock", first_run="staged", sequences_fire=True),
            "l_unknown": dict(base, kind="leadclock", first_run="auto", sequences_fire="unknown"),
            "l_false": dict(base, kind="leadclock", first_run="auto", sequences_fire=False),
            "l_nogate": dict(base, kind="leadclock", first_run="auto", gate_allow=[]),
            "l_behind": dict(base, kind="leadclock", first_run="auto", max_log_calls=0),
            "l_capped": dict(base, kind="leadclock", first_run="auto", sequences_fire=True, max_moves=2),
            "l_noreads": dict(base, kind="leadclock", first_run="auto", sequences_fire=True, max_reads=0),
            "l_learn": {"kind": "leadclock", "owner": "ops", "mode": "live", "schedule": "07:40", "gate_allow": ["lead"],
                        "first_run": "auto", "rejected_offers": False},
            "l_leadonly": {"kind": "leadclock", "owner": "ops", "mode": "live", "schedule": "07:40", "gate_allow": ["lead"],
                           "first_run": "auto", "column_ids": {"Lead Management": ids["Lead Management"]}},
            "l_speed": {"kind": "leadclock", "job": "speed", "owner": "sales", "mode": "live",
                        "schedule": "every 5 min 09:00-18:00 Mon-Fri", "owner_label": "Owner"},
            "l_speed_lm": {"kind": "leadclock", "job": "speed", "owner": "sales", "mode": "live",
                           "schedule": "every 5 min 09:00-18:00 Mon-Fri", "lead_manager": "Morgan"},
            "l_drips": {"kind": "drips", "owner": "ops", "mode": "live", "schedule": "every 60 min",
                        "gate_allow": ["lead", "not_interested"]},
            "l_drips_hot": {"kind": "drips", "owner": "ops", "mode": "live", "schedule": "every 60 min", "gate_allow": ["lead"],
                            "drips": {"cold": False, "said_no": False,
                                      "ghosting": {"statuses": ["Ghosting Lead", "Hot Lead"]}}},
            "l_drips_behind": {"kind": "drips", "owner": "ops", "mode": "live", "schedule": "every 60 min",
                               "gate_allow": ["lead"], "max_log_calls": 0},
            "l_tasks": {"kind": "leadtasks", "owner": "ops", "mode": "live", "schedule": "08:05 Mon-Fri", "gate_allow": ["lead"]},
            "l_tasks_nogate": {"kind": "leadtasks", "owner": "ops", "mode": "live", "schedule": "08:05 Mon-Fri"},
            "l_weekly": dict(base, kind="leadtasks", job="weekly", mode="plan", schedule="Wed 08:20"),
            "l_nurture": {"kind": "leadtasks", "job": "nurture", "owner": "sales", "mode": "live", "schedule": "08:15 Mon-Fri",
                          "gate_allow": ["lead"]},
            "l_nurture_lm": {"kind": "leadtasks", "job": "nurture", "owner": "sales", "mode": "live", "schedule": "08:15 Mon-Fri",
                             "gate_allow": ["lead"], "lead_manager_id": "u-morgan"},
            "l_check_names": {"kind": "leadcheck", "owner": "sales", "mode": "live", "schedule": "08:10 Mon-Fri"},
            "l_check": {"kind": "leadcheck", "owner": "sales", "mode": "live", "schedule": "08:10 Mon-Fri", "gate_allow": ["lead"],
                        "research_per_day": 2},
            "l_appts": dict(base, kind="appointments", owner="sales", schedule="every 30 min"),
            "l_exits": dict(base, kind="exits", schedule="1st 07:10", gate_allow=[]),
            "l_exits_keep": dict(base, kind="exits", schedule="1st 07:10", gate_allow=[], remarket_dead=False),
            "l_exits_lead": dict(base, kind="exits", schedule="1st 07:10", gate_allow=[], return_status="new_lead"),
            "l_exits_behind": dict(base, kind="exits", schedule="1st 07:10", gate_allow=[], max_log_calls=0),
            "l_tasks_for": {"kind": "leadtasks", "owner": "ops", "mode": "live", "schedule": "08:05 Mon-Fri", "gate_allow": ["lead"],
                            "task_for": {"lead": "Call New Lead"}},
        },
    }
    pol = policy.parse(copy.deepcopy(book), EMPLOYEES)
    wipe()
    now = T0

    real_sync = activity.sync

    def quick_sync(crm, group, now=None, floor_days=45, max_calls=400):
        """What a sync leaves behind, in one connection: every row of the group up to now is in the
        copy. A stage that allows it no calls is told the log is behind, as the real one would."""
        if int(max_calls) <= 0:
            return {"group": group, "added": 0, "calls": 0, "complete": False, "through": ""}
        kinds, stamp = set(activity.GROUPS[group]), _ts(now)
        added = activity._store([e for e in crm.log_rows if e["event_type"] in kinds and e["timestamp"] <= stamp])
        return {"group": group, "added": added, "calls": 1, "complete": True, "through": now.isoformat(timespec="seconds")}

    def go(stage, crm, when=None, plan=False, enqueue=None, whole_sync=False):
        if when is not None and when - crm.now > timedelta(days=1):
            prime(crm, when, old_call_days=0)       # a jump of days: the log copy is brought up to an hour before it
        crm.now = when or crm.now
        activity.sync = real_sync if whole_sync else quick_sync
        try:
            return sweep.run(stage, crm=crm, now=crm.now, pol=pol, plan=plan, enqueue=enqueue)
        finally:
            activity.sync = real_sync

    # ------------------------------------------------------------------ the routes
    print("  lead routes")
    titles = {u: c["title"] for u, c in LeadCRM([]).cols.items()}
    new_col = routes.match_title(titles, "New Lead")
    check(titles.get(new_col) == "New Lead (Unqualified)" and titles.get(routes.match_title(titles, "COLD Leads")) == "COLD Leads (Qualified)",
          "a column is found by the start of its title, and New Lead does not find No Contact New Lead")
    check(routes.match_title({"a": "Lost Deal", "b": "Lost Deal (old)"}, "Lost") == "",
          "a name two columns start with finds neither: a wrong column is worse than none")
    check(routes.title_phase("No Contact New Lead (Unqualified)", routes.PHASES) == "no_contact"
          and routes.title_phase("New Lead (Unqualified)", routes.PHASES) == "new_lead"
          and routes.title_phase("Offer Rejected", routes.PHASES, "offers") == "offer_rejected",
          "a logged column title is read as its phase, the longest name winning")
    check(routes.end_of_day(T0, "America/New_York") == "2026-11-12T04:59:59.999Z"
          and routes.end_of_day(T0, "America/New_York", 2) == "2026-11-14T04:59:59.999Z",
          "a task due today is due at the end of the business day, not at UTC midnight")
    check(routes.business_day(_et(11, 13, 16), "America/New_York", 1).strftime("%a") == "Mon",
          "one business day after a Friday is Monday")

    wipe()
    c0 = LeadCRM([])
    b0 = routes.load_board(c0, "Lead Management", column_ids=ids["Lead Management"], now=now, problems=[])
    check(len(b0.columns) == len(LEAD_COLS) and not c0.http and b0.find("HOT Leads") == c0.col("HOT Leads (Qualified)"),
          "column ids given in the playbook are used as they are, with no request")
    probs: list = []
    b1 = routes.load_board(c0, "Lead Management", expect=["New Lead"], now=now, problems=probs)
    check(not b1.columns and any("seed_columns" in p and "column_ids" in p for p in probs),
          "a board whose columns cannot be learned comes back empty and says which setting to fill")
    c1 = LeadCRM([lead("a1", "1 Seed St", "Hot Lead")])
    c1.log_ids = True
    c1.add_card("a1", "HOT Leads (Qualified)", now - timedelta(days=3))
    wipe()
    b2 = routes.load_board(c1, "Lead Management", expect=["New Lead"], now=now, problems=[])
    check(len(b2.columns) == len(LEAD_COLS) and b2.source == "a card's timeline",
          "one card's timeline teaches every column of its board, seeded from a log row that carries a column id")
    before = len(c1.http)
    b3 = routes.load_board(c1, "Lead Management", expect=["New Lead"], now=now + timedelta(days=1), problems=[])
    check(b3.columns == b2.columns and len(c1.http) == before, "the columns are remembered: the next run makes no request for them")
    wipe()
    b4 = routes.load_board(c1, "Acquisitions", expect=["Offer Rejected"], now=now, problems=[])
    check(not b4.columns, "a seed card from another board is not mistaken for this board's columns")
    wipe()
    c1.log_ids = False
    c1.log_rows = []
    c1.add_card("a1", "WARM Leads (Qualified)", now - timedelta(days=1), log=False)
    b5 = routes.load_board(c1, "Lead Management", seed_column=c1.col("WARM Leads (Qualified)"), expect=["New Lead"], now=now, problems=[])
    check(len(b5.columns) == len(LEAD_COLS), "one column id under seed_columns is enough: its first card's timeline gives the rest")
    wipe()
    c1.detail_columns = True
    b6 = routes.load_board(c1, "Lead Management", expect=["New Lead"], now=now, problems=[])
    check(len(b6.columns) == len(LEAD_COLS) and b6.source == "the board", "a board detail that carries its columns is used first")
    wipe()
    c7 = LeadCRM([lead("a7", "7 Probe St", "Hot Lead")])
    c7.add_card("a7", "HOT Leads (Qualified)", now - timedelta(days=2))
    prime(c7, now)
    p7: list = []
    b7 = routes.load_board(c7, "Lead Management", expect=["New Lead"], now=now, problems=p7)
    check(not b7.columns and len([h for h in c7.http if h[1].endswith("/timeline/")]) == 1 and p7,
          "with only a column title in the log, one timeline read is tried with the board's id, and a refusal is reported, not retried")
    wipe()
    c7.lenient = True
    prime(c7, now)
    b8 = routes.load_board(c7, "Lead Management", expect=["New Lead"], now=now, problems=[])
    check(len(b8.columns) == len(LEAD_COLS), "where the route does answer, that one read teaches every column with nothing set by hand")

    wipe()
    c2 = LeadCRM([lead("a2", "2 Card St", "Hot Lead"), lead("a3", "3 Card St", "Hot Lead")])
    c2.add_card("a2", "HOT Leads (Qualified)", now - timedelta(days=4))
    c2.add_card("a3", "HOT Leads (Qualified)", now - timedelta(days=4), log=False)
    prime(c2, now)
    where, unknown = routes.cards_by_record(c2, [c2.col("HOT Leads (Qualified)")])
    check(list(where) == ["a2"] and unknown == 1,
          "a card row that names no record is matched through the log copy, and a card nobody can name is counted, never guessed")
    c2.names_record = True
    check(set(routes.cards_by_record(c2, [c2.col("HOT Leads (Qualified)")])[0]) == {"a2", "a3"},
          "a card row that does name its record is believed")
    t_seq = c2.add_task("a2", "HOT Follow-Up", due=now + timedelta(days=2), by="[NEW] Hot Lead")
    t_me = c2.add_task("a2", "Call her back", due=now + timedelta(days=5), source="app")
    t_done = c2.add_task("a2", "Old task", due=now - timedelta(days=3), source="app")
    c2.log_rows.append({"id": "tc-1", "event_type": "task.completed", "resource": t_done, "resource_type": "task", "source": "app",
                        "author": "person@example.com", "timestamp": _ts(now - timedelta(hours=5)), "payload": {"task": {}}})
    prime(c2, now)
    open_ = {t["task"]: t for t in routes.open_tasks("a2")}
    check(set(open_) == {t_seq, t_me}, "a record's open tasks are the ones created and not completed, even when the completion names no property")
    check(routes.made_by_machine(open_[t_seq]) and not routes.made_by_machine(open_[t_me])
          and not routes.made_by_machine(dict(open_[t_seq], kind="appointment")),
          "a task a sequence made is a machine's, a task a person made is not, and an appointment is always a person's")

    # ------------------------------------------------------------------ what counts as a conversation
    print("  the lead clock: conversations and rules")
    wipe()
    cv = LeadCRM([lead("v1", "5 Talk St", "Hot Lead")])
    cv.log_rows = [call_ev(1, "o-v1", now - timedelta(days=2), kind="answered", seconds=40)]
    prime(cv, now)
    check(leadclock.last_conversation("o-v1", "v1")[0] is None, "an answered call of 40 seconds is not a conversation: a voicemail greeting answers")
    cv.log_rows.append(call_ev(2, "o-v1", now - timedelta(days=9), kind="answered", seconds=75))
    prime(cv, now)
    when, how = leadclock.last_conversation("o-v1", "v1")
    check(how == "call" and (now - when).days == 9, "an answered call of a minute or more is")
    cv.log_rows.append(text_ev(3, "o-v1", now - timedelta(days=4)))
    cv.log_rows.append(text_ev(4, "o-v1", now - timedelta(days=1), inbound=False))
    prime(cv, now)
    check(leadclock.last_conversation("o-v1", "v1") == (now - timedelta(days=4), "text"), "an inbound text is, and a text we sent is not")
    cv.log_rows.append(note_ev(5, "v1", now - timedelta(days=3)))
    cv.log_rows.append(note_ev(6, "v1", now - timedelta(hours=2), source="internal-api"))
    cv.log_rows.append(call_ev(7, "o-v1", now - timedelta(hours=3), kind="missed", inbound=True))
    prime(cv, now)
    check(leadclock.last_conversation("o-v1", "v1") == (now - timedelta(days=3), "note"),
          "a note a person wrote is; a note a machine wrote and a missed call are not")
    check(leadclock.last_conversation("o-v1", "v1", notes=False)[1] == "text", "a drip asks only about replies, so a note does not end it")

    lim = dict(leadclock.LIMITS)
    D = timedelta(days=1)
    J = lambda phase, **kw: leadclock.judge(phase, now, lim, **kw)  # noqa: E731
    reach = now - 44 * D
    check(J("new_lead", entered=now - 25 * D / 24, became=now - 25 * D / 24, dial_days=1)[0] == "no_contact"
          and J("new_lead", entered=now - 23 * D / 24, dial_days=1)[0] == "",
          "New Lead goes to No Contact after one day unreached, not before")
    check(J("new_lead", entered=now - 2 * D, conversation=now - D, dial_days=2) == ("", ""), "a new lead who was reached is not moved by the clock")
    check(J("new_lead", entered=now - 2 * D, dial_days=0) == ("", "not dialed"),
          "a new lead nobody dialed is not filed as No Contact: it is named instead")
    check(J("no_contact", entered=now - 9 * D, dial_days=5)[0] == "nurture" and J("no_contact", entered=now - 9 * D, dial_days=4)[0] == ""
          and J("no_contact", entered=now - 9 * D, dial_days=6, conversation=now - 2 * D)[0] == "",
          "No Contact goes to Nurture after five daily tries, and never once the owner was reached")
    check(J("nurture", entered=now - 181 * D, conversation=now - 181 * D, reach=reach)[0] == "ghosting"
          and J("nurture", entered=now - 70 * D, conversation=now - 70 * D, reach=reach)[0] == ""
          and J("nurture", entered=now - 70 * D, conversation=now - 70 * D, reach=reach, marginal=True)[0] == "ghosting",
          "Nurture goes to Ghosting after six months, or two for a marginal lead")
    check(J("nurture", entered=now - 40 * D, touches=31, reach=reach) == ("ghosting", "31 touches with no conversation"),
          "or after 31 touches, whichever comes first")
    check(J("hot", entered=now - 15 * D, reach=reach)[0] == "warm" and J("hot", entered=now - 13 * D, reach=reach)[0] == ""
          and J("warm", entered=now - 61 * D, conversation=now - 61 * D, reach=reach)[0] == "cold",
          "a quiet Hot lead drops to Warm after 14 days and a Warm one to Cold after 60")
    check(J("hot", entered=now - 300 * D, conversation=now - D, reach=reach) == ("", ""),
          "time is measured from the last real conversation: a hot lead spoken to yesterday stays hot though its card sat 300 days")
    check(J("hot", entered=now - 3 * D, conversation=now - 20 * D, reach=reach) == ("", ""),
          "the card's time in its column is the floor: a lead placed in Hot three days ago is not 20 days quiet")
    wait = J("cold", entered=now - 400 * D, reach=reach)
    check(wait[0] == "" and wait[1].startswith("wait") and "proves 44" in wait[1],
          "a limit longer than the log can prove waits: a year of quiet cannot be read off 44 days of log")
    check(J("cold", entered=now - 400 * D, reach=reach, trust_column_time=True)[0] == "ghosting",
          "unless the stage is told to trust the card's own time in its column")
    check(J("warm", entered=now - 70 * D, reach=None)[0] == "", "with no call in the log at all, nothing is proven and nothing moves")
    check(J("cold", entered=now - 400 * D, conversation=now - 366 * D, reach=now - 380 * D)[0] == "ghosting",
          "Cold goes to Ghosting after a year when the log does reach that far")
    check(J("offer_rejected", entered=now - 8 * D)[0] == "send_back" and J("offer_rejected", entered=now - 6 * D)[0] == "",
          "a rejected offer goes back to the lead manager after a week")
    check(leadclock.fire_mode(pol.stage("l_clock")) == "true" and leadclock.fire_mode(pol.stage("l_false")) == "false"
          and leadclock.fire_mode(pol.stage("l_unknown")) == "unknown" and leadclock.fire_mode(pol.stage("l_learn")) == "unknown",
          "sequences_fire reads true, false or unknown, and unknown is the default")

    # ------------------------------------------------------------------ the clock on a board
    print("  the lead clock: moving cards")

    def board_scene(fire=True):
        recs = [lead("h1", "11 Quiet St", "Hot Lead"), lead("h2", "12 Fresh St", "Hot Lead"),
                lead("h3", "13 Dated St", "Hot Lead"), lead("n1", "14 New St", "new_lead"),
                lead("n2", "15 Undialed St", "new_lead"), lead("c1", "16 Tried St", "No Contact New Lead")]
        crm = LeadCRM(recs, fire=fire)
        crm.add_card("h1", "HOT Leads (Qualified)", now - timedelta(days=16))
        crm.add_card("h2", "HOT Leads (Qualified)", now - timedelta(days=16))
        crm.add_card("h3", "HOT Leads (Qualified)", now - timedelta(days=16))
        crm.add_card("n1", "New Lead (Unqualified)", now - timedelta(hours=30))
        crm.add_card("n2", "New Lead (Unqualified)", now - timedelta(hours=30))
        crm.add_card("c1", "No Contact New Lead (Unqualified)", now - timedelta(days=8))
        crm.log_rows += [call_ev("h2", "o-h2", now - timedelta(days=2), kind="answered", seconds=120),
                         status_ev("n1", "n1", now - timedelta(hours=30), None, "new_lead"),
                         status_ev("n2", "n2", now - timedelta(hours=30), None, "new_lead"),
                         call_ev("n1", "o-n1", now - timedelta(hours=28))]
        crm.log_rows += [call_ev("c1-%d" % i, "o-c1", now - timedelta(days=i)) for i in range(1, 6)]
        crm.add_task("h1", "HOT Follow-Up", due=now + timedelta(days=1), by="[NEW] Hot Lead")
        crm.add_task("h3", "Call her after the 20th", due=now + timedelta(days=9), source="app")
        return crm

    wipe()
    cm = board_scene()
    prime(cm, now)
    sp = go("l_clock", cm, plan=True, whole_sync=True)
    check(sp.acted == 3 and "Would move 3 card(s)" in sp.head and not cm.wrote(),
          "a plan run lists the moves it would make and writes nothing")
    check(any("11 Quiet St" in ln and "Hot to Warm, quiet 16 days" in ln for ln in sp.lines)
          and any("14 New St" in ln and "New Lead to No Contact" in ln for ln in sp.lines)
          and any("16 Tried St" in ln and "No Contact to Nurture, 5 days of calls" in ln for ln in sp.lines),
          "each move says which card, from where to where, and why")
    check(any("13 Dated St" in ln and "a task a person dated for Nov 20" in ln for ln in sp.lines) and cm.column_of("h3").startswith("HOT"),
          "a card that holds a future task a person dated is never moved, and the line says so")
    check(not any("12 Fresh St" in ln for ln in sp.lines), "the hot lead spoken to two days ago is not on the list")
    check(any("15 Undialed St" in p and "never dialed" in p for p in sp.problems), "a new lead nobody dialed in a day is named as a problem")

    sl = go("l_clock", cm)
    check(sl.acted == 3 and cm.column_of("h1").startswith("WARM") and cm.column_of("n1").startswith("No Contact")
          and cm.column_of("c1").startswith("Nurture") and cm.column_of("h2").startswith("HOT") and cm.column_of("h3").startswith("HOT"),
          "a live run moves exactly those cards")
    check(cm.recs["h1"]["status"] == "Warm Lead" and [t["title"] for t in cm.open_tasks("h1")] == ["WARM Follow-Up"]
          and not any(w[0] == "status" for w in cm.writes),
          "with sequences_fire true the board's own sequence sets the status and the task, and the stage writes neither")
    ch = ledger.changes(record_uuid="h1")
    check(len(ch) == 1 and ch[0]["what"] == "card_move" and ch[0]["before"]["column"] == cm.col("HOT Leads (Qualified)")
          and ch[0]["after"]["column"] == cm.col("WARM Leads (Qualified)"),
          "each move is recorded with the column the card came from")
    check(routes.undo_step(cm, ch[0]) == "card moved back" and cm.column_of("h1").startswith("HOT"), "and one step puts the card back")
    check("card_move" in routes.UNDOABLE and "task_create" in routes.UNDOABLE, "the new change kinds are listed for undo")

    wipe()
    cg = board_scene()
    cg.recs["h1"]["tags"].append("Do Not Market")
    prime(cg, now)
    sg = go("l_clock", cg)
    check(cg.column_of("h1").startswith("HOT") and sg.tally.get("held back: tagged Do Not Market") == 1,
          "a lead tagged Do Not Market is not moved into a column that creates a call task")
    wipe()
    cn = board_scene()
    prime(cn, now)
    sn = go("l_nogate", cn)
    check(sn.acted == 0 and not cn.wrote() and any(k.startswith("held back: already a lead") for k in sn.tally),
          "a stage that does not name leads in gate_allow moves no lead")
    wipe()
    cb = board_scene()
    prime(cb, now)
    sb = go("l_behind", cb)
    check(sb.acted == 0 and not cb.wrote() and "log is behind" in sb.head and any("catching up" in p for p in sb.problems),
          "while the log is behind nothing is judged or moved: silence in an unread log is not silence from the seller")

    wipe()
    cf = board_scene()
    cf.add_task("h1", "Machine task a person re-dated", due=now + timedelta(days=1), by="[LOOP] Hot")
    redated = [t for t in cf.tasks.values() if t["title"].startswith("Machine task")][0]
    prime(cf, now)
    redated["due_date"] = _iso(now + timedelta(days=12))
    sf = go("l_clock", cf, plan=True)
    check(any("11 Quiet St" in ln and "a task a person moved to Nov 23" in ln for ln in sf.lines),
          "a sequence-made task a person re-dated in the app holds the card too: it is read once before any move")
    wipe()
    ca = board_scene()
    ca.add_task("h1", "Walkthrough", due=now + timedelta(days=3), kind="appointment", source="internal-api")
    prime(ca, now)
    check(any("11 Quiet St" in ln and "an appointment" in ln for ln in go("l_clock", ca, plan=True).lines),
          "a booked appointment holds the card whoever created it")
    wipe()
    cu = board_scene()
    cu.tasks = {}
    cu.log_rows = [r for r in cu.log_rows if not r["event_type"].startswith("task.")]
    cu.add_task("h1", "Something the log never saw", due=now + timedelta(days=4), log=False)
    prime(cu, now)
    su = go("l_clock", cu, plan=True)
    check(any("11 Quiet St" in ln and "a future task the log cannot explain" in ln for ln in su.lines),
          "a future task the log cannot explain is treated as a person's")

    wipe()
    cs = board_scene()
    prime(cs, now)
    s1 = go("l_staged", cs)
    bid = s1.batch
    check(bid and s1.acted == 3 and "staged as batch" in s1.head and not cs.wrote(),
          "the first live run stages its moves for a person and moves nothing")
    check("still staged" in go("l_staged", cs).head and len(ledger.batches(status="staged")) == 1, "a second run does not stage them twice")
    dry = leadclock.release(bid, crm=cs, pol=pol)
    check(dry["dry"] and dry["cards"] == 3 and not cs.wrote(), "a release without commit only describes the batch")
    cs.log_rows.append(call_ev("late", "o-h1", now + timedelta(minutes=5), kind="answered", seconds=300))
    rel = leadclock.release(bid, crm=cs, pol=pol, commit=True, now=now + timedelta(minutes=30))
    check(rel["results"].get("moved") == 2 and rel["results"].get("no longer due") == 1 and cs.column_of("h1").startswith("HOT"),
          "release judges every card again: the lead called since staging stays where it is")
    check(ledger.batch(bid)["status"] == "sent" and store.kv_get(leadclock._approved_key("l_staged")), "and from then on the stage moves cards on its own")
    wipe()
    cs2 = board_scene()
    prime(cs2, now)
    first = go("l_staged", cs2).batch
    cs2.recs["h2"]["status"] = "Hot Lead"
    cs2.log_rows = [r for r in cs2.log_rows if r["id"] != "call-h2"]
    prime(cs2, now)
    second = go("l_staged", cs2, when=now + timedelta(hours=1)).batch
    leadclock.release(first, crm=cs2, pol=pol, commit=True, now=now + timedelta(hours=2))
    check(second and second != first and ledger.batch(second)["status"] == "cancelled",
          "a second list staged while the first waited is closed when the first is approved, not left for ever")
    wipe()
    co = board_scene()
    prime(co, now)
    so = go("l_leadonly", co, plan=True)
    check(so.acted == 3 and not any("Acquisitions" in p for p in so.problems)
          and any("Rejected offers were not timed" in ln and "Acquisitions" in ln for ln in so.lines),
          "not knowing the offers board is said in a line; the lead board's clock still runs")

    wipe()
    cx = board_scene(fire=False)
    prime(cx, now)
    sx = go("l_false", cx)
    check(cx.column_of("h1").startswith("WARM") and cx.recs["h1"]["status"] == "Warm Lead"
          and [t["title"] for t in cx.open_tasks("h1")] == ["WARM Follow-Up"],
          "with sequences_fire false the stage does the sequence's work itself: the old task goes, the status is set, the column's task is created")
    check(cx.recs["c1"]["status"] == "No Contact New Lead" and [t["title"] for t in cx.open_tasks("c1")] == ["Nurture New Lead"],
          "the Nurture column sets no status, so none is written, and its weekly task is created")
    kinds = sorted({c["what"] for c in ledger.changes(record_uuid="h1")})
    check(kinds == ["card_move", "status", "task_create", "task_delete"] and sx.acted == 3, "every one of those writes is recorded with what it replaced")
    check([c["before"] for c in ledger.changes(record_uuid="h1") if c["what"] == "status"] == ["Hot Lead"],
          "the status is recorded in the spelling the account stores, so putting it back writes a status the account knows")
    fake_stage = policy.Stage(id="l_false", kind="leadclock", owner="ops", mode="live", summary="", schedule=[], presets=[],
                              params={"follow_on": {"warm": {"status": "new_lead", "preset": ""}}, "gate_allow": ["lead"]})
    out = Summary(stage="l_false", kind="leadclock", owner="ops", mode="live", live=True)
    ctx = Ctx(policy=pol, stage=fake_stage, crm=cx, run_id="t-newlead", now=now, live=True, out=out)
    n_status = sum(1 for w in cx.writes if w[0] == "status")
    leadclock.follow_on(ctx, "h1", "warm", routes.phases(fake_stage), {})
    check(sum(1 for w in cx.writes if w[0] == "status") == n_status and any("refused to set New Lead" in p for p in out.problems),
          "a record that already holds a lead status is never set to New Lead, whatever the playbook says")

    wipe()
    ck = board_scene(fire=False)
    prime(ck, now)
    go("l_unknown", ck)
    check(ck.column_of("h1").startswith("WARM") and ck.recs["h1"]["status"] == "Hot Lead" and (ledger.fact("h1", "clock_move") or {}).get("to") == "warm",
          "with sequences_fire unknown the card is moved and the stage remembers to look again")
    s2 = go("l_unknown", ck, when=now + timedelta(days=1))
    check(ck.recs["h1"]["status"] == "Warm Lead" and [t["title"] for t in ck.open_tasks("h1")] == ["WARM Follow-Up"]
          and s2.tally.get("API moves that did not, so far") == 3,
          "on the next run the sequence's task is not there, so the stage sets the status and creates the task, and counts the miss")
    wipe()
    cy = board_scene(fire=True)
    prime(cy, now)
    go("l_unknown", cy)
    n_writes = len(cy.wrote())
    s3 = go("l_unknown", cy, when=now + timedelta(days=1))
    check(s3.tally.get("API moves that fired the board's sequence so far") == 3 and len(cy.wrote()) == n_writes,
          "where the sequence did fire, the next run finds its task, counts the hit and writes nothing")

    wipe()
    cc = board_scene()
    prime(cc, now)
    sc = go("l_capped", cc)
    check(sc.acted == 2 and sc.tally.get("left for the next run (cap 2 a run)") == 1, "a run moves at most max_moves cards and says how many it left")
    wipe()
    cnr = board_scene()
    prime(cnr, now)
    snr = go("l_noreads", cnr)
    check(snr.acted == 0 and not cnr.wrote() and snr.tally.get("due to move, not checked this run (read cap, next run)") == 4,
          "a card is never moved unread: at the read cap the due cards wait for the next run")
    wipe()
    cnl = board_scene()
    cnl.recs["h1"]["status"] = None
    prime(cnl, now)
    snl = go("l_clock", cnl)
    check(cnl.column_of("h1").startswith("HOT") and snl.tally.get("cards whose record is not in a lead status (left alone)") == 1,
          "a card whose record is no longer in a lead status is left alone, not moved on a stale card")
    wipe()
    crs = board_scene()
    prime(crs, now)
    seen_h = crs.record
    crs.record = lambda u: dict(seen_h(u), status="not_interested") if u == "h1" else seen_h(u)
    srs = go("l_clock", crs)
    check(crs.column_of("h1").startswith("HOT") and srs.tally.get("cards whose record is not in a lead status (left alone)") == 1,
          "a card is judged on the record's own status, not the search row's: a lead that just left the pipeline is not moved")
    wipe()
    cst = board_scene()
    cst.stuck = True
    prime(cst, now)
    sst = go("l_clock", cst)
    check(sst.acted == 0 and len(cst.wrote("PATCH")) == 1 and any("did not move" in p for p in sst.problems),
          "the first move is read back, and a card that did not move stops the run at one")

    wipe()
    cr = LeadCRM([lead("r1", "21 Offer St", "Hot Lead"), lead("m1", "22 Maybe St", "No Contact New Lead", tags=["Marginal Lead"]),
                  lead("m2", "23 Strong St", "No Contact New Lead")])
    cr.add_card("r1", "Offer Rejected", now - timedelta(days=8))
    cr.add_card("m1", "Nurture New Lead (Unqualified)", now - timedelta(days=70))
    cr.add_card("m2", "Nurture New Lead (Unqualified)", now - timedelta(days=70))
    prime(cr, now, old_call_days=120)
    srj = go("l_clock", cr)
    check(cr.column_of("r1") == "Needs Review" and cr.column_of("m1") == "Ghosting Lead" and cr.column_of("m2").startswith("Nurture"),
          "a rejected offer lands back with the lead manager, and only the lead tagged marginal leaves Nurture at two months")
    check(any("a marginal lead" in ln for ln in srj.lines), "the line says the lead was marginal")

    wipe()
    cl = LeadCRM([lead("q1", "24 Learn St", "Hot Lead")])
    cl.add_card("q1", "HOT Leads (Qualified)", now - timedelta(days=20))
    prime(cl, now)
    sq = go("l_learn", cl)
    check(sq.acted == 0 and "columns are not known" in sq.head and any("seed_columns" in p for p in sq.problems),
          "when the board's columns cannot be learned the stage moves nothing and says what to set")

    # ------------------------------------------------------------------ speed to lead
    print("  speed to lead")
    wipe()
    born = _et(11, 11, 10, 0)
    cq = LeadCRM([lead("s1", "31 Fast St", "new_lead"), lead("s2", "32 Night St", "new_lead"), lead("s3", "33 Oncall St", "new_lead")])
    cq.log_rows = [status_ev("s1", "s1", born, None, "new_lead"),
                   call_ev("old", "o-zz", born - timedelta(days=3), kind="answered", seconds=90)]
    prime(cq, born + timedelta(minutes=2))
    q0 = go("l_speed", cq, when=born + timedelta(minutes=3))
    check(q0.acted == 0 and not q0.problems and "waiting on a first call" in q0.head, "three minutes in, nobody is told yet")
    q1 = go("l_speed", cq, when=born + timedelta(minutes=6))
    check(q1.acted == 1 and any(p.startswith("Adriana: 31 Fast St") and "became a new lead 6 min ago" in p for p in q1.problems),
          "at five minutes with no outbound call the assignee is told, by the name the call log knows")
    check(go("l_speed", cq, when=born + timedelta(minutes=9)).acted == 0, "and told once")
    q2 = go("l_speed", cq, when=born + timedelta(minutes=16))
    check(q2.acted == 1 and any(p.startswith("Owner: 31 Fast St") and "has waited 16 min" in p for p in q2.problems),
          "at fifteen minutes the owner is told")
    cq.log_rows.append(call_ev("s1", "o-s1", born + timedelta(minutes=20)))
    q3 = go("l_speed", cq, when=born + timedelta(minutes=25))
    check(q3.acted == 0 and q3.tally.get("speed to lead, median minutes") == 20 and "has been called" in q3.head,
          "once the call is in the log the wait is measured and nobody is told again")
    check(not cq.wrote() and not cq.writes, "the speed clock writes nothing to the account")
    cq.priority = False
    seen_flag = []
    real_logs = cq.logs
    cq.logs = lambda *a, **k: (seen_flag.append(cq.priority) or real_logs(*a, **k))
    go("l_speed", cq, when=born + timedelta(minutes=27), whole_sync=True)
    cq.logs = real_logs
    check(seen_flag and all(seen_flag) and cq.priority is False,
          "the speed clock reads on the urgent part of the request budget, and hands the client back as it found it")
    night = _et(11, 11, 23, 0)
    cq.log_rows.append(status_ev("s2", "s2", night, None, "new_lead"))
    check(go("l_speed", cq, when=night + timedelta(minutes=10)).acted == 0, "a lead that arrives at night pings nobody at night")
    check(leadclock.business_open(night, pol) == _et(11, 12, 9, 0) and leadclock.business_open(_et(11, 13, 19, 0), pol) == _et(11, 16, 9, 0),
          "its clock starts at the next opening, and a Friday evening lead starts Monday")
    check(go("l_speed", cq, when=_et(11, 12, 9, 3)).acted == 0 and go("l_speed", cq, when=_et(11, 12, 9, 6)).acted == 1,
          "so it is told five minutes after the doors open, not before")
    oncall = _et(11, 12, 11, 0)
    cq.log_rows += [call_ev("s3", "o-s3", oncall - timedelta(minutes=4), kind="answered", seconds=200),
                    status_ev("s3", "s3", oncall, None, "new_lead")]
    q5 = go("l_speed", cq, when=oncall + timedelta(minutes=30))
    check(q5.tally.get("new leads set on a call (no wait to measure)") == 1 and not any("33 Oncall St" in p for p in q5.problems),
          "a status set on a call has no wait to measure and pings nobody")
    wipe()
    cmv = LeadCRM([lead("s8", "38 Moved St", "Warm Lead")])
    cmv.log_rows = [status_ev("s8a", "s8", born, None, "new_lead"),
                    status_ev("s8b", "s8", born + timedelta(minutes=4), "new_lead", "Warm Lead")]
    prime(cmv, born + timedelta(minutes=5))
    qmv = go("l_speed", cmv, when=born + timedelta(minutes=20))
    check(qmv.acted == 0 and not qmv.problems and qmv.tally.get("new leads a person moved on without a logged call") == 1,
          "a new lead a person already moved to another status is not pinged about")
    wipe()
    clm = LeadCRM([lead("s9", "39 Named St", "new_lead")])
    clm.log_rows = [status_ev("s9", "s9", born, None, "new_lead")]
    prime(clm, born + timedelta(minutes=5))
    qlm = go("l_speed_lm", clm, when=born + timedelta(minutes=6))
    check(any(p.startswith("Morgan: 39 Named St") for p in qlm.problems),
          "with the lead manager named on the stage (decision D13), the five-minute line goes to that name")
    wipe()
    prime(cq, _et(11, 12, 7, 0), old_call_days=0)
    for u in ("s1", "s2"):
        ledger.set_fact(u, "lead_owner", {"owner": "o-" + u, "street": "x", "assignee": "u-adriana"})
    line = leadclock.speed_lines(_et(11, 12, 8, 0), "America/New_York")
    check(len(line) == 1 and "median 20 min" in line[0] and "1 never called" in line[0], "yesterday's speed to lead is one plain line for the digest")

    # ------------------------------------------------------------------ drips
    print("  drips")
    wipe()
    plans_, bad = drips.plans(pol.stage("l_drips"))
    check(set(plans_) == {"ghosting", "cold", "said_no"} and not bad and [s["do"] for s in plans_["ghosting"]["steps"]] == ["text", "email", "task"],
          "the default drips load: ghosting is a text at day 30, an email at day 60 and a review task at day 90")
    check("names 'foreclos'" in drips.copy_problem("Hi {first}, I saw the foreclosure notice on {street}.")
          and "more than one question" in drips.copy_problem("Hi {first}? Still there? {street}")
          and "dash" in drips.copy_problem("Hi {first}" + chr(8212) + "about {street}")
          and "'$'" in drips.copy_problem("Hi {first}, I can pay $90,000 for {street}.")
          and drips.copy_problem("Hi {first}, are you my client at {street}?") == "",
          "a text template that names a list, asks two questions, carries a dash or a price is refused; an ordinary word is not")
    odd = policy.Stage(id="x", kind="drips", owner="ops", mode="live", summary="", schedule=[], presets=[], params={
        "drips": {"cold": False, "said_no": False, "ghosting": {"steps": [
            {"day": 30, "do": "text", "copy": "Hi {first}, about the auction at {street}."},
            {"day": 60, "do": "task", "title": "Review"}]}}})
    p2, bad2 = drips.plans(odd)
    check([s["do"] for s in p2["ghosting"]["steps"]] == ["task"] and any("step 1" in b and "skipped" in b for b in bad2),
          "a bad step is dropped and named, and the rest of the drip still runs")
    r_named = lead("d0", "40 Example St", "Ghosting Lead")
    check(drips.render(plans_["ghosting"]["steps"][0]["copy"], r_named, "Adriana")
          == "Hi Pat, we talked about 40 Example St a while back. Is selling still on your mind? This is Adriana."
          and drips.render(plans_["ghosting"]["steps"][0]["copy"], r_named, "")
          == "Hi Pat, we talked about 40 Example St a while back. Is selling still on your mind?",
          "the text is signed with the sender's first name, and goes unsigned when no name is known: never a guessed one")
    check(drips.first_name(lead("d9", "1 A St", "Ghosting Lead", first="J")) == "there"
          and drips.first_name(lead("d9", "1 A St", "Ghosting Lead", company="Example Holdings LLC")) == "there"
          and drips.first_name(lead("d9", "1 A St", "Ghosting Lead", first="MARIA")) == "Maria",
          "an initial or a company is not greeted by name")

    def drip_scene():
        recs = [lead("g1", "41 Ghost St", "Ghosting Lead"), lead("g2", "42 Ghost St", "Ghosting Lead"),
                lead("g3", "43 Hot St", "Hot Lead"), lead("g4", "44 Early St", "Ghosting Lead"),
                lead("x1", "45 Said No St", "not_interested"), lead("x2", "46 Cold No St", "not_interested")]
        crm = LeadCRM(recs)
        crm.log_rows = [status_ev("g1", "g1", now - timedelta(days=31), "Warm Lead", "Ghosting Lead"),
                        status_ev("g2", "g2", now - timedelta(days=100), "Cold Lead", "Ghosting Lead"),
                        status_ev("g4", "g4", now - timedelta(days=10), "Cold Lead", "Ghosting Lead"),
                        status_ev("x1", "x1", now - timedelta(days=61), "Hot Lead", "not_interested"),
                        status_ev("x2", "x2", now - timedelta(days=61), None, "not_interested"),
                        call_ev("who", "o-zz", now - timedelta(days=5), kind="answered", seconds=90)]
        return crm

    wipe()
    cd = drip_scene()
    prime(cd, now, old_call_days=150)
    dp = go("l_drips", cd, plan=True, whole_sync=True)
    check(dp.acted == 3 and dp.head.startswith("Would stage 3 text(s).") and not ledger.batches() and not cd.wrote(),
          "a plan run says what it would stage and stages nothing")
    check(ledger.fact("x2", "drip") is None and not any("46 Cold No St" in ln for ln in dp.lines)
          and any("45 Said No St" in ln for ln in dp.lines),
          "a lead who later said no gets the said-no drip; a cold no that never was a lead does not")
    check(not any("44 Early St" in ln or "43 Hot St" in ln for ln in dp.lines), "a lead ten days into Ghosting is not due, and a hot lead is not on a drip")
    check(any("no inbound text has reached the log in 45 days" in p and "check each thread" in p for p in dp.problems),
          "when no reply by text has ever reached the log, the batch says a text reply cannot end a drip here")
    dl = go("l_drips", cd)
    bid = dl.batch
    items = ledger.batch(bid)["items"]
    check(bid and len(items) == 3 and {it["do"] for it in items} == {"text"} and not cd.wrote() and not cd.writes,
          "a live run stages the texts in a batch for a person; nothing is sent and nothing is written to the account")
    g2 = next(it for it in items if it["record_uuid"] == "g2")
    check(g2["step"] == 0 and g2["to"] == "8655550141" and g2["body"].startswith("Hi Pat, we talked about 42 Ghost St"),
          "a lead first seen on day 100 gets the first step only: one touch, not three")
    check(go("l_drips", cd).tally.get("already staged, waiting on release") == 3 and len(ledger.batches()) == 1, "a step already staged is not staged again")

    cd.log_rows.append(text_ev("g1", "o-g1", now + timedelta(minutes=10)))
    cd.recs["g2"]["status"] = "Warm Lead"
    de = go("l_drips", cd, when=now + timedelta(hours=1))
    left = [it["record_uuid"] for it in ledger.batch(bid)["items"]]
    check(left == ["x1"] and de.tally.get("staged steps pulled because the drip ended") == 2,
          "a reply ends the drip at once, a status change ends it too, and both leads' staged steps are pulled")
    check(any("41 Ghost St" in p and "call them today" in p for p in de.problems), "the lead who answered is handed to a person the same hour")
    check((ledger.fact("g1", "drip") or {}).get("ended", "").startswith("replied") and (ledger.fact("g2", "drip") or {}).get("ended") == "status changed",
          "and each drip remembers why it ended")
    s40 = go("l_drips", cd, when=now + timedelta(days=40))
    check(s40.batch and not any("no inbound text has reached the log" in p for p in s40.problems),
          "once a reply by text is in the log, a staged batch no longer carries that warning")
    later = [it["record_uuid"] for b in ledger.batches(status="staged") for it in ledger.batch(b["id"])["items"]]
    check("g1" not in later and "g2" not in later and "g4" in later,
          "an ended drip stages nothing more, however long the lead stays quiet, while the others carry on")

    wipe()
    ce = drip_scene()
    ce.recs["g1"]["row_phone"] = {"number": "8655550141", "doNotCall": True}
    ce.recs["g2"]["tags"].append("Do Not Market")
    prime(ce, now, old_call_days=150)
    dg = go("l_drips", ce)
    check(dg.tally.get("steps passed over: no number a text may use") == 1 and dg.tally.get("held back: tagged Do Not Market") == 1
          and [it["record_uuid"] for it in ledger.batch(dg.batch)["items"]] == ["x1"],
          "the gate runs on every step: a number the registry flags is never texted, and Do Not Market stops the drip cold")
    after_text = drips.scheduled(pol, "g1", now + timedelta(hours=1))
    check(after_text is not None and after_text[1] == "email" and drips.scheduled(pol, "g2", now + timedelta(hours=1))[1] == "text",
          "a lead with no number a text may use is passed on to the next step, so the drip does not stall; a held lead stays where it is")

    wipe()
    cr2 = drip_scene()
    prime(cr2, now, old_call_days=150)
    bid2 = go("l_drips", cr2).batch
    check(drips.release(bid2, crm=cr2, pol=pol) == {"batch": bid2, "texts": 3, "emails": 0, "dry": True}, "a release without commit only counts the batch")
    cr2.log_rows.append(call_ev("g2r", "o-g2", now + timedelta(minutes=20), kind="answered", seconds=95))
    cr2.recs["x1"]["tags"].append("Do Not Market")
    rel2 = drips.release(bid2, crm=cr2, pol=pol, commit=True, now=now + timedelta(hours=2))
    check(rel2["texts"] == 1 and rel2["held"] == {"the lead answered": 1, "tagged Do Not Market": 1},
          "release checks every item again: the lead who answered and the one tagged since are held back")
    with open(rel2["file"], newline="", encoding="utf-8") as f:
        sent = list(csv.DictReader(f))
    check(len(sent) == 1 and sent[0]["phone"] == "8655550141" and sent[0]["street"].startswith("41 Ghost St") and "This is Adriana." in sent[0]["message"],
          "the texts that pass are written to one file for the texting tool: this code sends nothing")
    check(ledger.batch(bid2)["status"] == "sent" and 0 in drips.done_steps("g1", "ghosting", ledger.fact("g1", "drip")["anchor"]),
          "and the step is recorded as done only at release")
    nxt = drips.scheduled(pol, "g1", now + timedelta(hours=3))
    check(nxt is not None and nxt[1] == "email" and (nxt[0] - (now + timedelta(hours=2))).days == 30,
          "the next step waits the gap the plan put between them, counted from when the one before it went")

    cr2.recs["g1"]["owner"]["emails"] = ["pat@example.com"]
    cr2.tags_known = {"Do Not Market"}
    d_mail = go("l_drips", cr2, when=now + timedelta(days=33))
    staged_now = [it for b in ledger.batches(status="staged") for it in ledger.batch(b["id"])["items"]]
    check(not any(it["do"] == "email" for it in staged_now)
          and any("no tag 'Drip Email Ghosting'" in p and "Sequence Builder" in p for p in d_mail.problems),
          "an email step whose tag does not exist is passed over, and the stage says to build the sequence that fires on it")
    with store._LOCK, store._db() as c:        # take that pass-over back, as if the tag had been there all along
        c.execute("DELETE FROM ap_events WHERE record_uuid='g1' AND action='drip' AND detail LIKE '%no email sequence yet%'")
    cr2.tags_known = None
    d_mail = go("l_drips", cr2, when=now + timedelta(days=33, hours=1))
    mail_items = ledger.batch(d_mail.batch)["items"]
    check(len(mail_items) == 1 and mail_items[0]["do"] == "email" and not any(w[0] == "add_tags" for w in cr2.writes),
          "with the tag in place the email is staged, and the tag is not added until a person releases it")
    rel3 = drips.release(d_mail.batch, crm=cr2, pol=pol, commit=True, now=now + timedelta(days=33, hours=2))
    check(rel3["emails"] == 1 and ("add_tags", "g1", ("Drip Email Ghosting",)) in cr2.writes, "release adds the tag that fires the email sequence")
    n_tasks = len(cr2.open_tasks("g1"))
    d_task = go("l_drips", cr2, when=now + timedelta(days=64))
    review = [t for t in cr2.open_tasks("g1") if t["title"].startswith("Review this ghosting lead")]
    check(d_task.acted == 1 and len(review) == 1 and len(cr2.open_tasks("g1")) == n_tasks + 1 and not d_task.batch
          and review[0]["assigned_to_user"] == {"uuid": "u-adriana"},
          "the day-90 step is a task for the lead's assignee, created directly: it reaches no seller")
    check(drips.scheduled(pol, "g1", now + timedelta(days=65)) is None, "after the last step nothing more is scheduled")

    wipe()
    cno = drip_scene()
    prime(cno, now, old_call_days=150)
    go("l_drips", cno)
    kept = (ledger.fact("g1", "drip") or {}).get("anchor")
    activity.reset()
    for g in activity.GROUPS:
        store.kv_set("autopilot:log:%s:through" % g, (now - timedelta(hours=1)).isoformat(timespec="seconds"))
    cno.log_rows = []
    go("l_drips", cno, when=now + timedelta(hours=2))
    check(kept and (ledger.fact("g1", "drip") or {}).get("anchor") == kept,
          "a lead already on a drip keeps its start when the log forgets the status change, so the drip never runs twice")

    wipe()
    ccd = LeadCRM([lead("c7", "47 Cold St", "Cold Lead"), lead("c8", "48 Later St", "follow_up"), lead("c9", "49 Nomail St", "Ghosting Lead")])
    ccd.log_rows = [status_ev("c7", "c7", now - timedelta(days=31), "Warm Lead", "Cold Lead"),
                    status_ev("c8", "c8", now - timedelta(days=40), "Warm Lead", "follow_up"),
                    status_ev("c9", "c9", now - timedelta(days=61), "Cold Lead", "Ghosting Lead")]
    prime(ccd, now, old_call_days=150)
    start9 = (now - timedelta(days=61)).isoformat(timespec="seconds")
    ledger.event("earlier", "l_drips", "c9", "drip", {"drip": "ghosting", "step": 0, "do": "text", "anchor": start9},
                 now=now - timedelta(days=31))
    dcd = go("l_drips", ccd)
    cold_items = {it["record_uuid"]: it for it in ledger.batch(dcd.batch)["items"]}
    check(set(cold_items) == {"c7", "c8"} and cold_items["c7"]["drip"] == "cold" and "checking in on 47 Cold St" in cold_items["c7"]["body"],
          "a cold lead and a Follow Up record are on the cold drip, with its own text")
    nxt9 = drips.scheduled(pol, "c9", now + timedelta(hours=1))
    check(dcd.tally.get("steps passed over: no email address on the owner") == 1 and nxt9 is not None and nxt9[1] == "task",
          "an owner with no email address is passed on to the review task: the email step does not stall the drip")
    wipe()
    ch2 = drip_scene()
    ch2.log_rows.append(status_ev("g3", "g3", now - timedelta(days=45), "Warm Lead", "Hot Lead"))
    prime(ch2, now, old_call_days=150)
    dh = go("l_drips_hot", ch2)
    check(any("lists Hot Lead" in p and "never dripped" in p for p in dh.problems) and ledger.fact("g3", "drip") is None
          and not any("43 Hot St" in ln for ln in dh.lines),
          "hot leads are never dripped, even when the playbook lists the status")
    wipe()
    cbh = drip_scene()
    prime(cbh, now, old_call_days=150)
    dbh = go("l_drips_behind", cbh)
    check(dbh.acted == 0 and not ledger.batches() and "log is behind" in dbh.head,
          "while the log is behind no step is staged: a reply could be in the part not read yet")

    # ------------------------------------------------------------------ the next action
    print("  next actions")

    def task_scene():
        recs = [lead("t1", "51 Cold St", "Cold Lead"), lead("t2", "52 Nurture St", "No Contact New Lead"),
                lead("t3", "53 Busy St", "Hot Lead"), lead("t4", "54 Late St", "Warm Lead"),
                lead("t5", "55 Ghost St", "Ghosting Lead"), lead("t6", "56 Ghost St", "Ghosting Lead"),
                lead("t7", "57 Deal St", "under_contract"), lead("t8", "58 Odd St", "lead")]
        recs[3]["lists"] = ["Obituary"]
        crm = LeadCRM(recs)
        crm.add_card("t1", "COLD Leads (Qualified)", now - timedelta(days=50))
        crm.add_card("t2", "Nurture New Lead (Unqualified)", now - timedelta(days=20))
        crm.add_task("t3", "HOT Follow-Up", due=now + timedelta(days=1), by="[NEW] Hot Lead")
        crm.log_rows.append(status_ev("t5", "t5", now - timedelta(days=5), "Cold Lead", "Ghosting Lead"))
        return crm

    wipe()
    ct = task_scene()
    prime(ct, now)
    go("l_drips", ct, plan=True)                 # the drips stage learns who is on a drip
    ledger.set_fact("t6", "drip", dict(ledger.fact("t6", "drip"), ended="replied"))
    tp = go("l_tasks", ct, plan=True)
    check(tp.acted == 2 and "Would create 2" in tp.head and not ct.wrote(), "a plan run says how many tasks it would create and creates none")
    tl = go("l_tasks", ct)
    check([t["title"] for t in ct.open_tasks("t1")] == ["COLD Follow-Up"] and tl.acted == 2,
          "a lead with no open task gets the task its column's sequence would have created")
    check([t["title"] for t in ct.open_tasks("t2")] == ["Nurture New Lead"],
          "the column is read from the card, not only the status: a nurture lead reads No Contact and gets the weekly Nurture task")
    check(len(ct.open_tasks("t3")) == 1, "a lead that has an open task is left alone")
    check(not ct.open_tasks("t4") and tl.tally.get("held back: the owner has died or may have (Obituary list); research first") == 1,
          "a lead whose owner may have died gets no call task: the gate holds it")
    check(not ct.open_tasks("t5") and tl.tally.get("ghosting leads whose next action is a drip step") == 1,
          "a ghosting lead with a drip step ahead of it already has its next action")
    check(any("56 Ghost St" in p and "no drip step ahead" in p for p in tl.problems), "a ghosting lead with nothing ahead of it is listed for a decision")
    check(not ct.open_tasks("t7") and any("58 Odd St" in p and "no task preset mapped" in p for p in tl.problems),
          "a record under contract needs no lead task, and a status with no preset mapped is listed, not guessed")
    stale = LeadCRM([lead("t1", "51 Cold St", "Cold Lead")])        # the same lead, as an index that has not caught up shows it
    stale.add_card("t1", "COLD Leads (Qualified)", now - timedelta(days=50))
    stale.log_rows = list(ct.log_rows)
    ts = go("l_tasks", stale, when=now + timedelta(minutes=20))
    check(ts.acted == 0 and not stale.wrote() and ts.tally.get("given a task earlier today (the open-task count has not caught up)") == 1,
          "a task made twenty minutes ago is not made again because the open-task count has not caught up")
    ev = [e for e in ledger.events("t1", "lead_task")]
    check(len(ev) == 1 and ev[0]["detail"]["preset"] == "COLD Follow-Up" and ev[0]["detail"]["task"] in ct.tasks
          and ledger.changes(record_uuid="t1")[0]["what"] == "task_create",
          "each task is recorded with its id, so the run can be put back")
    check(routes.undo_step(ct, ledger.changes(record_uuid="t1")[0]) == "1 task(s) deleted" and not ct.open_tasks("t1"), "and undo deletes exactly that task")
    wipe()
    ctf = task_scene()
    prime(ctf, now)
    go("l_tasks_for", ctf)
    check([t["title"] for t in ctf.open_tasks("t8")] == ["Call New Lead"],
          "task_for names the task preset for a status the board's columns do not cover")
    wipe()
    ctn = task_scene()
    prime(ctn, now)
    tn = go("l_tasks_nogate", ctn)
    check(tn.acted == 0 and not ctn.wrote(), "without leads in gate_allow no task is created on a lead")
    wipe()
    cti = task_scene()
    cti.no_task_id = True
    prime(cti, now)
    ti = go("l_tasks", cti)
    check(ti.acted == 0 and len(cti.wrote("POST")) == 1 and any("no task could be read back" in p for p in ti.problems),
          "the first task is read back, and a route that answers with no task behind it stops the run at one record")

    wipe()
    cw = LeadCRM([lead("w1", "61 Wrong St", "Cold Lead"), lead("w2", "62 Nocard St", "Hot Lead"),
                  lead("w3", "63 Nonote St", "Warm Lead"), lead("w4", "64 Noted St", "Warm Lead")])
    cw.add_card("w1", "HOT Leads (Qualified)", now - timedelta(days=4))
    cw.add_card("w3", "WARM Leads (Qualified)", now - timedelta(days=9))
    cw.add_card("w4", "WARM Leads (Qualified)", now - timedelta(days=9))
    cw.add_task("w3", "First", due=now - timedelta(days=2), by="[NEW] Warm")
    cw.add_task("w3", "Second", due=now + timedelta(days=2), source="app")
    cw.add_task("w4", "Sometime", due=None, source="app")
    cw.log_rows += [call_ev("w3", "o-w3", now - timedelta(days=3), kind="answered", seconds=140),
                    call_ev("w4", "o-w4", now - timedelta(days=3), kind="answered", seconds=140),
                    note_ev("w4", "w4", now - timedelta(days=3) + timedelta(minutes=9))]
    real_must = cw.preset_must
    cw.preset_must = lambda t: ({"tasks_incomplete": [0, 0], "any_property_status": ["New Lead", "Cold Lead"]}
                                if t == "3. Leads Missing Tasks" else real_must(t))
    prime(cw, now)
    ww = go("l_weekly", cw)
    check(any("61 Wrong St" in p and "(HOT Leads (Qualified) card, status cold lead)" in p for p in ww.lines),
          "the weekly pass names a card that sits in one column while the record holds another column's status")
    check(any("no card on the Lead Management board" in p and "62 Nocard St" in p for p in ww.lines), "a lead with no card on the board is named")
    check(any("a conversation with no note after it" in p and "63 Nonote St" in p for p in ww.lines)
          and not any("a conversation with no note" in p and "64 Noted St" in p for p in ww.lines),
          "a conversation with no note after it is named; one with a note is not")
    check(any("a task past its date" in p and "63 Nonote St" in p for p in ww.lines)
          and ww.tally.get("leads with two or more open tasks") == 1, "overdue tasks and doubled tasks are counted")
    check(any("an open task with no date on it" in p and "64 Noted St" in p for p in ww.lines),
          "a lead whose only open task has no date is named: an undated task never comes due")
    check(any("'3. Leads Missing Tasks'" in p and "New Lead (stored as new_lead)" in p for p in ww.problems),
          "a check preset that filters on a status the account does not store is named with the stored spelling")
    check(not cw.wrote() and not cw.writes, "the weekly pass reads only")

    wipe()
    cnu = LeadCRM([lead("f1", "71 Later St", "follow_up"), lead("f2", "72 Unsure St", "follow_up", phones=[dict(MOBILE2)]),
                   lead("f3", "73 Set St", "follow_up")])
    cnu.add_task("f3", "Call back", due=now + timedelta(days=30), source="app")
    cnu.log_rows.append(status_ev("f1", "f1", now - timedelta(days=10), "Warm Lead", "follow_up"))
    prime(cnu, now)
    fn = go("l_nurture", cnu)
    made = cnu.open_tasks("f1")
    check(fn.acted == 1 and len(made) == 1 and made[0]["title"] == "Call back: said not now" and made[0]["due_date"].startswith("2027-01-31"),
          "a Follow Up record on a confirmed number gets one call task 90 days from the day the owner said not now")
    check(not cnu.open_tasks("f2") and any("72 Unsure St" in p and "no confirmed number" in p for p in fn.problems),
          "one with no confirmed number is listed instead: a call task on an unconfirmed number is a cold call")
    check(len(cnu.open_tasks("f3")) == 1 and "1 already had a dated task" in fn.head, "one that already holds a dated task is left alone")
    check(cnu.recs["f1"]["status"] == "follow_up" and (ledger.fact("f1", "drip") is None), "the nurture job sets no status")
    check(made[0]["assigned_to_user"] == {"uuid": "u-adriana"}, "the task goes to the record's assignee")
    wipe()
    cnl = LeadCRM([lead("f5", "75 Managed St", "follow_up")])
    prime(cnl, now)
    go("l_nurture_lm", cnl)
    check(cnl.open_tasks("f5")[0]["assigned_to_user"] == {"uuid": "u-morgan"},
          "or to the lead manager named on the stage: leads belong to the lead manager (decision D13)")

    wipe()
    csc = LeadCRM([lead("t9", "59 Moved St", "Cold Lead")])
    csc.add_card("t9", "COLD Leads (Qualified)", now - timedelta(days=50))
    prime(csc, now)
    seen_t = csc.record
    csc.record = lambda u: dict(seen_t(u), status="not_interested") if u == "t9" else seen_t(u)
    ssc = go("l_tasks", csc)
    check(not csc.open_tasks("t9") and ssc.tally.get("status changed since the search (left for the next run)") == 1,
          "a lead whose status changed after the search is not given the old column's task")
    wipe()
    cig = LeadCRM([lead("t10", "60 Lead St", "Cold Lead"), lead("p10", "62 Prospect St", "prospecting"),
                   lead("p11", "63 Quiet St", None)])
    prime(cig, now)
    cig.rows = lambda must, cap=10000: [cig._row(r) for r in cig.recs.values()][:cap]
    sig = go("l_tasks", cig)
    check(sig.found == 1 and [t["title"] for t in cig.open_tasks("t10")] == ["COLD Follow-Up"]
          and not cig.open_tasks("p10") and not cig.open_tasks("p11")
          and any("outside the status(es) asked for" in x for x in sig.problems),
          "a records search that stops filtering by status gives no prospecting record a lead's task, and the stage says so")

    # ------------------------------------------------------------------ new distress on a lead
    print("  new distress on a lead")
    wipe()
    cdn = LeadCRM([lead("e1", "81 Lien St", "Warm Lead"), lead("e2", "82 Obit St", "Hot Lead", phones=[dict(MOBILE), dict(MOBILE2)]),
                   lead("e3", "83 Quiet St", "Cold Lead"), lead("e4", "84 Stop St", "Hot Lead")])
    for u in ("e1", "e2", "e3", "e4"):
        cdn.add_task(u, "Follow up", due=now + timedelta(days=1), by="[NEW]")
        cdn.recs[u]["lists"] = ["Absentee Owners"]
    cdn.recs["e4"]["tags"].append("Do Not Market")
    prime(cdn, now)
    enq = []
    fake_enqueue = lambda *a, **k: (enq.append((a, k)) or (len(enq), True))  # noqa: E731
    d0 = go("l_check", cdn, enqueue=fake_enqueue)
    check(not any("gained a list" in p for p in d0.problems) and not cdn.wrote(), "the first check only remembers what each lead looks like")
    cdn.recs["e1"]["lists"].append("Liens")
    cdn.recs["e2"]["lists"].append("Obituary")
    cdn.recs["e4"]["lists"].append("Liens")
    dpn = go("l_check", cdn, when=now + timedelta(days=1), plan=True, enqueue=fake_enqueue)
    check(dpn.tally.get("tasks for a lead that gained a list") == 2 and not cdn.wrote() and not cdn.writes and not enq,
          "in plan mode the new lists are named and nothing is written or queued")
    ledger.set_fact("e1", "lead_lists", ["Absentee Owners"])
    ledger.set_fact("e2", "lead_lists", ["Absentee Owners"])
    ledger.set_fact("e4", "lead_lists", ["Absentee Owners"])
    dln = go("l_check", cdn, when=now + timedelta(days=1), enqueue=fake_enqueue)
    t_e1 = [t for t in cdn.open_tasks("e1") if t["title"].startswith("New on this lead: Liens")]
    check(len(t_e1) == 1 and t_e1[0]["due_date"] == routes.end_of_day(now + timedelta(days=1), "America/New_York")
          and t_e1[0]["assigned_to_user"] == {"uuid": "u-adriana"},
          "a lead whose record gained a list gets a task for the lead manager due that day")
    t_e2 = [t for t in cdn.open_tasks("e2") if "obituary landed" in t["title"]]
    check(len(t_e2) == 1, "a new obituary still gets its task: the warning before the next call is the point, so a possible death does not hold it")
    phones = {p["number"]: p for p in cdn.recs["e2"]["owner"]["phones"]}
    check("Verify Number" in phones["8655550141"]["tags"] and phones["8655550141"]["status"] == "CORRECT"
          and "Verify Number" not in phones["8655550142"]["tags"],
          "the confirmed number is marked as one to verify and keeps its status; an unconfirmed number is not touched")
    check(len(enq) == 1 and enq[0][0][:3] == ("research", "deep_prospect", "e2") and any(m[0] == "e2" and "obituary" in m[1].lower() for m in cdn.msgs),
          "the record is queued for research and a note says why")
    check(cdn.recs["e2"]["status"] == "Hot Lead" and cdn.recs["e1"]["status"] == "Warm Lead" and not any(w[0] == "status" for w in cdn.writes),
          "the lead status is never changed")
    check(not [t for t in cdn.open_tasks("e4") if "New on this lead" in t["title"]]
          and dln.tally.get("held back: tagged Do Not Market") == 1, "a lead tagged Do Not Market gets no task")
    check(len([t for t in cdn.open_tasks("e3")]) == 1, "a lead that gained nothing is left alone")
    again = Ctx(policy=pol, stage=pol.stage("l_check"), crm=cdn, run_id="again", now=now + timedelta(days=1, hours=1), live=True,
                out=Summary(stage="l_check", kind="leadcheck", owner="sales", mode="live", live=True), enqueue=fake_enqueue)
    leadtasks.new_distress(again, cdn.record("e2"), None, ["Obituary"])
    check(len([t for t in cdn.open_tasks("e2") if "obituary landed" in t["title"]]) == 1 and len(enq) == 1,
          "meeting the same change twice makes no second task and no second research run")
    wipe()
    cng = LeadCRM([lead("e7", "87 Named St", "Warm Lead")])
    cng.add_task("e7", "Follow up", due=now + timedelta(days=1), by="[NEW]")
    prime(cng, now)
    go("l_check_names", cng)
    cng.recs["e7"]["lists"].append("Liens")
    dng = go("l_check_names", cng, when=now + timedelta(hours=20))
    check(any("87 Named St" in p and "gained Liens" in p for p in dng.problems) and len(cng.open_tasks("e7")) == 1 and not cng.wrote(),
          "a morning check that does not name leads in gate_allow still names the change and creates nothing")
    wipe()
    ccap = LeadCRM([lead("k%d" % i, "9%d Cap St" % i, "Hot Lead") for i in range(1, 4)])
    for i in range(1, 4):
        ccap.recs["k%d" % i]["lists"] = ["Obituary"]
    prime(ccap, now)
    enq2 = []
    capq = lambda *a, **k: (enq2.append(a) or (len(enq2), True))  # noqa: E731
    cap_ctx = Ctx(policy=pol, stage=pol.stage("l_check"), crm=ccap, run_id="cap", now=now, live=True,
                  out=Summary(stage="l_check", kind="leadcheck", owner="sales", mode="live", live=True), enqueue=capq)
    for i in range(1, 4):
        leadtasks.new_distress(cap_ctx, ccap.record("k%d" % i), None, ["Obituary"])
    check(len(enq2) == 2 and cap_ctx.out.tally.get("research waiting (cap 2 a day)") == 1 and ledger.fact("k3", "research_wanted"),
          "research is queued inside the stage's day cap, and the lead left over is remembered")
    next_day = Ctx(policy=pol, stage=pol.stage("l_check"), crm=ccap, run_id="cap2", now=now + timedelta(days=1), live=True,
                   out=Summary(stage="l_check", kind="leadcheck", owner="sales", mode="live", live=True), enqueue=capq)
    leadtasks.flush_research(next_day)
    check(len(enq2) == 3 and enq2[2][2] == "k3" and ledger.fact("k3", "research_wanted") is None, "and queued the next morning")
    cnp = LeadCRM([lead("e9", "89 Prospect St", "prospecting")])
    np_ctx = Ctx(policy=pol, stage=pol.stage("l_check"), crm=cnp, run_id="np", now=now, live=True,
                 out=Summary(stage="l_check", kind="leadcheck", owner="sales", mode="live", live=True), enqueue=capq)
    leadtasks.new_distress(np_ctx, cnp.record("e9"), None, ["Liens"])
    check(not cnp.open_tasks("e9") and not cnp.wrote() and not cnp.writes and not cnp.msgs and len(enq2) == 3
          and np_ctx.out.tally.get("gained a list and is not a lead (left alone)") == 1,
          "a record that is not a lead gets nothing from the new-distress step, whoever hands it over")
    wipe()
    cfl = LeadCRM([lead("e10", "90 Lead St", "Warm Lead"), lead("p12", "92 Prospect St", "prospecting")])
    cfl.add_task("e10", "Follow up", due=now + timedelta(days=1), by="[NEW]")
    for u in ("e10", "p12"):
        cfl.recs[u]["lists"] = ["Absentee Owners"]
    cfl.recs["p12"]["lists"].append("Liens")
    prime(cfl, now)
    for u in ("e10", "p12"):
        ledger.set_fact(u, "lead_lists", ["Absentee Owners"])
    cfl.rows = lambda must, cap=10000: [cfl._row(r) for r in cfl.recs.values()][:cap]
    dfl = go("l_check", cfl, enqueue=fake_enqueue)
    check(dfl.found == 1 and not cfl.open_tasks("p12") and not any("92 Prospect St" in x for x in dfl.problems if "gained" in x)
          and any("outside the status(es) asked for" in x for x in dfl.problems),
          "a records search that stops filtering hands the morning check no prospecting record, and the check says so")

    # ------------------------------------------------------------------ appointments
    print("  appointments")
    wipe()
    cap_ = LeadCRM([lead("p1", "101 Walk St", "Hot Lead"), lead("p2", "102 Today St", "Hot Lead"),
                    lead("p3", "103 Gone St", "Hot Lead")])
    start1 = _et(11, 13, 14, 0)
    a1 = cap_.add_task("p1", "Walkthrough", due=start1, kind="appointment", source="app", created=now - timedelta(hours=1),
                       start_time=_iso(start1), end_time=_iso(start1 + timedelta(hours=1)), assigned_to_user={"uuid": "u-adriana"})
    prime(cap_, now)
    enq3 = []
    q3 = lambda *a, **k: (enq3.append(a) or (len(enq3), True))  # noqa: E731
    ap = go("l_appts", cap_, plan=True, enqueue=q3)
    check(any(ln == "Walkthrough booked: 101 Walk St, Knoxville, Fri Nov 13 2:00 PM (in 2 day(s))" for ln in ap.lines)
          and not cap_.wrote() and not enq3, "a plan run announces the walkthrough and writes nothing")
    al = go("l_appts", cap_, enqueue=q3)
    confirm = [t for t in cap_.open_tasks("p1") if t["title"].startswith("Confirm the walkthrough")]
    check(al.acted == 2 and len(confirm) == 1 and confirm[0]["due_date"] == routes.end_of_day(now + timedelta(days=1), "America/New_York")
          and confirm[0]["assigned_to_user"] == {"uuid": "u-adriana"},
          "a booked walkthrough gets a confirm-call task due the day before, for whoever holds the appointment")
    check(enq3 == [("sales", "quick_screen", "p1", "101 Walk St, Knoxville")], "and the comp workflow is queued, so the numbers exist before anyone drives out")
    a2run = go("l_appts", cap_, when=now + timedelta(minutes=30), enqueue=q3)
    check(a2run.acted == 0 and len(enq3) == 1 and len(cap_.open_tasks("p1")) == 2, "each step is done once")
    moved_to = _et(11, 17, 10, 0)
    cap_.tasks[a1].update(start_time=_iso(moved_to), end_time=_iso(moved_to + timedelta(hours=1)))
    a3run = go("l_appts", cap_, when=now + timedelta(hours=1), enqueue=q3)
    check(a3run.acted == 1 and any("moved to Tue Nov 17 10:00 AM" in ln for ln in a3run.lines)
          and len([t for t in cap_.open_tasks("p1") if t["title"].startswith("Confirm")]) == 2,
          "a moved appointment gets a new confirm call for its new day")
    again_to = _et(11, 18, 15, 0)
    cap_.tasks[a1].update(start_time=_iso(again_to), end_time=_iso(again_to + timedelta(hours=1)), outcome_status="rescheduled")
    check(go("l_appts", cap_, when=now + timedelta(hours=2), enqueue=q3).acted == 0,
          "an appointment days away is read again once a day, not every half hour")
    a3b = go("l_appts", cap_, when=now + timedelta(hours=23), enqueue=q3)
    check(any("moved to Wed Nov 18 3:00 PM" in ln for ln in a3b.lines) and not (ledger.fact("p1", "appt:%s" % a1) or {}).get("closed"),
          "an appointment marked rescheduled with a new time ahead is still watched, not closed as finished")
    cap_.tasks[a1].update(start_time=_iso(moved_to), end_time=_iso(moved_to + timedelta(hours=1)), outcome_status=None)
    go("l_appts", cap_, when=now + timedelta(hours=46), enqueue=q3)
    after = moved_to + timedelta(hours=3)
    a4run = go("l_appts", cap_, when=after, enqueue=q3)
    ask = [t for t in cap_.open_tasks("p1") if t["title"].startswith("Record how the walkthrough went")]
    check(len(ask) == 1 and not a4run.problems and "1 waiting on an outcome" in a4run.head,
          "once the end time has passed with no outcome, one task asks for it")
    a5run = go("l_appts", cap_, when=after + timedelta(hours=30), enqueue=q3)
    check(any("ended more than 24 hours ago with no outcome" in p and "101 Walk St" in p for p in a5run.problems)
          and len([t for t in cap_.open_tasks("p1") if t["title"].startswith("Record how")]) == 1,
          "a day later it is a problem line, and the task is not made twice")
    routes.set_outcome(cap_, a1, status="no_show")
    a6run = go("l_appts", cap_, when=after + timedelta(hours=31), enqueue=q3)
    rebook = [t for t in cap_.open_tasks("p1") if t["title"].startswith("No show at the walkthrough: call to rebook")]
    check(len(rebook) == 1 and rebook[0]["due_date"] == routes.end_of_day(after + timedelta(hours=31), "America/New_York")
          and any("no show. Next:" in ln for ln in a6run.lines), "a no-show becomes a call-to-rebook task due the same day")
    reads_before = len([h for h in cap_.http if h[0] == "GET" and "/task/" in h[1]])
    go("l_appts", cap_, when=after + timedelta(hours=40), enqueue=q3)
    check(len([h for h in cap_.http if h[0] == "GET" and "/task/" in h[1]]) == reads_before,
          "an appointment whose outcome was handled is closed and not read again")

    wipe()
    cto = LeadCRM([lead("p2", "102 Today St", "Hot Lead"), lead("p3", "103 Gone St", "Hot Lead"), lead("p4", "104 Offer St", "Hot Lead")])
    today2 = _et(11, 11, 17, 30)
    cto.add_task("p2", "Walkthrough", due=today2, kind="appointment", source="app", created=now - timedelta(hours=1),
                 start_time=_iso(today2), end_time=_iso(today2 + timedelta(hours=1)))
    gone = cto.add_task("p3", "Walkthrough", due=today2, kind="appointment", source="app", created=now - timedelta(hours=2),
                        start_time=_iso(today2), end_time=_iso(today2 + timedelta(hours=1)))
    done_at = _et(11, 10, 15, 0)
    offered = cto.add_task("p4", "Walkthrough", due=done_at, kind="appointment", source="app", created=now - timedelta(days=3),
                           start_time=_iso(done_at), end_time=_iso(done_at + timedelta(hours=1)),
                           outcome_status="attended", outcome_result="offer_made")
    del cto.tasks[gone]
    cto.add_card("p4", "Appointment Booked", now - timedelta(days=4))
    prime(cto, now)
    at = go("l_appts", cto, enqueue=q3)
    check(at.tally.get("set for the same day") == 1 and any("102 Today St" in ln and "(today)" in ln for ln in at.lines)
          and [t["due_date"] for t in cto.open_tasks("p2") if t["title"].startswith("Confirm")] == [routes.end_of_day(now, "America/New_York")],
          "a walkthrough set for today is counted as same-day and its confirm call is due today")
    check((ledger.fact("p3", "appt:%s" % gone) or {}).get("closed") == "the appointment is gone" and not cto.open_tasks("p3"),
          "an appointment deleted in the app is dropped, not chased")
    follow = [t for t in cto.open_tasks("p4") if t["title"].startswith("Follow up on the offer")]
    check(len(follow) == 1 and follow[0]["due_date"] == routes.end_of_day(_et(11, 13, 12), "America/New_York"),
          "an offer made at the walkthrough gets a follow-up task two business days out")
    check(any("1 card(s) sit in Appointment Booked with no appointment on the record" in p and "Make Offer sequence" in p for p in at.problems),
          "a card in Appointment Booked with no appointment ahead is counted, so the column can mean what it says")
    check(not any("Appointment Booked" in p for p in go("l_appts", cto, when=now + timedelta(minutes=30), enqueue=q3).problems),
          "and that check runs once a day, not every half hour")

    wipe()
    cag = LeadCRM([lead("p6", "106 Stop St", "Hot Lead"), lead("p7", "107 Old St", "Hot Lead")])
    cag.recs["p6"]["tags"].append("Do Not Market")
    soon = _et(11, 12, 13, 0)
    long_ago = _et(10, 20, 13, 0)
    cag.add_task("p6", "Walkthrough", due=soon, kind="appointment", source="app", created=now - timedelta(hours=1),
                 start_time=_iso(soon), end_time=_iso(soon + timedelta(hours=1)))
    old_appt = cag.add_task("p7", "Walkthrough", due=long_ago, kind="appointment", source="app", created=long_ago - timedelta(days=1),
                            start_time=_iso(long_ago), end_time=_iso(long_ago + timedelta(hours=1)))
    prime(cag, now)
    ag = go("l_appts", cag, enqueue=q3)
    check(len(cag.open_tasks("p6")) == 1 and ag.tally.get("held back: tagged Do Not Market") == 1,
          "no confirm task is created on a record tagged Do Not Market: the gate runs on every task")
    check(len(cag.open_tasks("p7")) == 1 and (ledger.fact("p7", "appt:%s" % old_appt) or {}).get("closed") == "no outcome was ever recorded",
          "a walkthrough three weeks past with no outcome is closed, not chased for ever")
    wipe()
    cbk = LeadCRM([lead("b1", "111 Book St", "Hot Lead"), lead("b2", "112 Stop St", "Hot Lead")])
    cbk.recs["b2"]["tags"].append("Do Not Market")
    prime(cbk, now)
    dry_b = appointments.book(cbk, "b1", "2026-11-12 14:00", pol=pol, now=now)
    check(dry_b["ok"] and dry_b["dry"] and dry_b["would_book"]["when"] == "Thu Nov 12 2:00 PM" and not cbk.wrote(),
          "booking is dry unless asked to commit, and a bare time is read as business-local time")
    check(not appointments.book(cbk, "b1", "2026-11-10 14:00", pol=pol, now=now, commit=True)["ok"]
          and not appointments.book(cbk, "b1", "next tuesday", pol=pol, now=now, commit=True)["ok"] and not cbk.wrote(),
          "a time in the past or one that cannot be read books nothing")
    held_b = appointments.book(cbk, "b2", "2026-11-12 14:00", pol=pol, now=now, commit=True)
    check(not held_b["ok"] and "Do Not Market" in held_b["say"] and not cbk.wrote(), "a record tagged Do Not Market is refused, with the reason")
    live_b = appointments.book(cbk, "b1", "2026-11-11 17:00", pol=pol, now=now, commit=True, booked_by="Adriana")
    made_b = cbk.tasks.get(live_b.get("task") or "")
    check(live_b["ok"] and made_b and made_b["event_type"] == "appointment" and made_b["start_time"] == "2026-11-11T22:00:00Z"
          and made_b["end_time"] == "2026-11-11T23:00:00Z" and "(today)" in live_b["say"],
          "a committed booking on a live box creates the appointment task, start and end in UTC")
    booked = ledger.changes(record_uuid="b1")
    check(len(booked) == 1 and booked[0]["run_id"].startswith("book-") and booked[0]["what"] == "task_create",
          "a booking is recorded under its own run id")
    cbk.log_rows = []
    prime(cbk, now)
    seen = go("l_appts", cbk, enqueue=q3)
    check(any("111 Book St" in ln and "Walkthrough booked" in ln for ln in seen.lines),
          "the stage picks up a booking made through it even before the log shows the task")
    config.MODE = "dry"
    try:
        check(appointments.book(cbk, "b1", "2026-11-12 09:00", pol=pol, now=now, commit=True).get("dry") is True,
              "on a box that is not live a booking stays dry even with commit")
    finally:
        config.MODE = "live"
    check(routes.undo_step(cbk, booked[0]) == "1 task(s) deleted" and live_b["task"] not in cbk.tasks,
          "and undo takes back exactly that appointment")
    asked = {"record_uuid": "b1", "payload": '{"requested_by": "Roma", "inputs": {"when": "2026-11-12 10:30", "minutes": "45"}}'}
    wf = appointments.book_run(asked, crm=cbk, pol=pol, now=now)
    made_wf = cbk.tasks.get(wf.get("task") or "") or {}
    check(wf["ok"] and made_wf.get("start_time") == "2026-11-12T15:30:00Z" and made_wf.get("end_time") == "2026-11-12T16:15:00Z"
          and appointments.book_render(asked, wf) == "Booked: 111 Book St, Knoxville, Thu Nov 12 10:30 AM.",
          "told the record, the day and the time, the booking workflow makes the appointment and says so in one line")
    check(not appointments.book_run({"record_uuid": "b1", "payload": "{}"}, crm=cbk, pol=pol, now=now)["ok"],
          "and with no time given it books nothing")

    # ------------------------------------------------------------------ clean exits
    print("  clean exits")

    def exit_scene():
        recs = [lead("z1", "121 Dead St", "Dead Lead"), lead("z2", "122 Recent St", "Dead Lead"),
                lead("z3", "123 Revived St", "Hot Lead"), lead("z4", "124 No St", "not_interested"),
                lead("z5", "125 Long Dead St", "Dead Lead"), lead("z6", "126 Blocked St", "Dead Lead"),
                lead("z7", "127 Sold St", "Dead Lead"), lead("z8", "128 Passed St", "Dead Lead"),
                lead("z9", "129 Unknown St", "Dead Lead")]
        recs[5]["tags"].append("Do Not Market")
        recs[6]["last_sold"] = "2026-09-01"
        recs[7]["owner"]["deceased"] = True
        crm = LeadCRM(recs)
        crm.add_card("z1", "Dead Lead", now - timedelta(days=31))
        crm.add_card("z2", "Dead Lead", now - timedelta(days=20))
        crm.add_card("z3", "Dead Lead", now - timedelta(days=40))
        crm.add_card("z4", "Not Interested Anymore", now - timedelta(days=40))
        crm.log_rows += [status_ev("z1", "z1", now - timedelta(days=31), "Cold Lead", "Dead Lead"),
                         status_ev("z2", "z2", now - timedelta(days=20), "Cold Lead", "Dead Lead")]
        crm.log_rows += [status_ev(u, u, now - timedelta(days=181), "Cold Lead", "Dead Lead") for u in ("z5", "z6", "z7", "z8")]
        return crm

    wipe()
    cz = exit_scene()
    prime(cz, now, old_call_days=190)
    zp = go("l_exits", cz, plan=True)
    check(zp.acted == 3 and zp.head == "Would remove 2 finished card(s) from the board and send 1 dead lead(s) back to marketing."
          and not cz.wrote() and not cz.writes, "a plan run lists the exits and writes nothing")
    zl = go("l_exits", cz)
    check(cz.card_of("z1") is None and cz.card_of("z4") is None and cz.card_of("z2") is not None,
          "Dead and Not Interested cards leave the board after 30 days, and a card 20 days in stays")
    check(cz.recs["z1"]["status"] == "Dead Lead" and cz.recs["z4"]["status"] == "not_interested", "the status stays: only the card goes")
    check(cz.card_of("z3") is not None and any("123 Revived St" in ln and "left for a person" in ln for ln in zl.lines),
          "a card in the Dead column whose record is a lead again is left for a person")
    check(cz.recs["z5"]["status"] is None and any(m[0] == "z5" and "Back in marketing after 180 days" in m[1] for m in cz.msgs),
          "a dead lead returns to marketing after 180 days: the status is cleared and a note says where it has been")
    check(cz.recs["z1"]["status"] == "Dead Lead" and cz.recs["z2"]["status"] == "Dead Lead", "a lead dead for a month stays dead")
    check(cz.recs["z6"]["status"] == "Dead Lead" and zl.tally.get("stay dead: tagged Do Not Market") == 1,
          "Do Not Market keeps a dead lead dead: only the dead status itself is forgiven")
    check(cz.recs["z7"]["status"] == "Dead Lead" and zl.tally.get("stay dead: the house sold since") == 1, "a house that sold since stays dead")
    check(cz.recs["z8"]["status"] == "Dead Lead" and any(k.startswith("stay dead: the owner has died") for k in zl.tally),
          "a dead lead whose owner has died is not sent back to the callers")
    check(cz.recs["z9"]["status"] == "Dead Lead" and zl.tally.get("dead leads older than the log reaches (counted from the day first seen)") == 1,
          "a lead dead for longer than the log reaches is counted from the day it was first seen, never guessed back in early")
    chg = {c["what"] for c in ledger.changes(record_uuid="z5")} | {c["what"] for c in ledger.changes(record_uuid="z1")}
    check(chg == {"status", "card_remove"} and ledger.changes(record_uuid="z5")[0]["before"] == "Dead Lead",
          "both exits are recorded with what was there before")
    check(routes.undo_step(cz, ledger.changes(record_uuid="z1")[0]) == "card created again" and cz.card_of("z1") is not None,
          "a removed card can be created again in its column")
    wipe()
    ckp = exit_scene()
    prime(ckp, now, old_call_days=190)
    zk = go("l_exits_keep", ckp)
    check(ckp.recs["z5"]["status"] == "Dead Lead" and "back to marketing" not in zk.head, "with remarket_dead off, dead leads stay dead (decision D27)")
    wipe()
    cgd = exit_scene()
    prime(cgd, now, old_call_days=190)
    zg = go("l_exits_lead", cgd)
    check(cgd.recs["z5"]["status"] == "Dead Lead" and any("return_status is new_lead" in p for p in zg.problems),
          "a dead lead is never sent back into the lead pipeline by machine, whatever return_status says")
    wipe()
    cxb = exit_scene()
    prime(cxb, now, old_call_days=190)
    zb = go("l_exits_behind", cxb)
    check(zb.acted == 0 and not cxb.wrote() and not cxb.writes and "log is behind" in zb.head,
          "while the log is behind no card is removed and no lead is sent back")
    wipe()
    csk = exit_scene()
    csk.sticky_cards = True
    prime(csk, now, old_call_days=190)
    zs = go("l_exits", csk)
    check(len(csk.wrote("DELETE")) == 1 and any("still on the board" in p for p in zs.problems), "the first removal is read back, and a card that stayed stops the run")
    wipe()
    crc = exit_scene()
    prime(crc, now, old_call_days=190)
    seen_z = crc.record
    crc.record = lambda u: dict(seen_z(u), status="Hot Lead") if u == "z5" else seen_z(u)
    zr = go("l_exits", crc)
    check(crc.recs["z5"]["status"] == "Dead Lead" and not any(m[0] == "z5" for m in crc.msgs)
          and zr.tally.get("left: the record is no longer dead (or could not be read)") == 1,
          "a record the search still shows as dead, whose own status says otherwise, is never sent back to marketing")
    wipe()
    cpf = exit_scene()
    cpf.recs["z10"] = lead("z10", "130 Prospect St", "prospecting")
    cpf.log_rows += [status_ev("z10a", "z10", now - timedelta(days=183), "Cold Lead", "Dead Lead"),
                     status_ev("z10b", "z10", now - timedelta(days=100), "Dead Lead", "prospecting")]
    prime(cpf, now, old_call_days=190)
    cpf.rows = lambda must, cap=10000: [cpf._row(r) for r in cpf.recs.values()][:cap]
    zf = go("l_exits", cpf)
    check(cpf.recs["z10"]["status"] == "prospecting" and not any(m[0] == "z10" for m in cpf.msgs)
          and any("outside the status(es) asked for" in x for x in zf.problems),
          "a dead-lead search that stops filtering is checked row by row: a record that came back to life keeps its status")
    cby = LeadCRM([lead("y1", "131 Buyer St", None), lead("y2", "132 Seller St", "Hot Lead")])
    dry_y = exits.buyer_exit(cby, "y1", ["Dispo Buyers"])
    check(dry_y["dry"] and dry_y["would"]["add_lists"] == ["Dispo Buyers"] and not cby.writes, "the buyer exit is dry unless asked to commit")
    exits.buyer_exit(cby, "y1", ["Dispo Buyers"], commit=True)
    check(cby.recs["y1"]["status"] == "buyer" and cby.recs["y1"]["lists"] == ["Dispo Buyers"],
          "a reply that shows a buyer sets the buyer status and adds the record to the dispo list")
    check(not exits.buyer_exit(cby, "y2", ["Dispo Buyers"], commit=True)["ok"] and cby.recs["y2"]["status"] == "Hot Lead",
          "a record that is already a lead is not turned into a buyer by a machine")

    # ------------------------------------------------------------------ what the orchestrator adds
    print("  registry and playbook lines")
    loaded = 0
    for kind, row in REGISTRY_LINES.items():
        fn = getattr(importlib.import_module("%s.autopilot.%s" % (__package__, row[0])), row[1], None)
        loaded += callable(fn) and row[2] in ("free", "paid", "outreach") and len(row) == 5
    check(loaded == len(REGISTRY_LINES) == 5, "each registry line names a module and a run function that load")
    blocks = yaml.safe_load("stages:" + PLAYBOOK_BLOCKS)["stages"]
    full = {"business": book["business"], "caps": book["caps"], "people": book["people"], "stages": blocks}
    try:
        stages_ = policy.parse(full, EMPLOYEES).stages
    except policy.PolicyError as exc:
        stages_ = {}
        print("    " + str(exc))
    check(len(stages_) == 8 and all(s.mode == "plan" and s.schedule for s in stages_.values()),
          "the eight playbook blocks parse, each has a schedule, and every one ships in plan mode")
    check({s.owner for k, s in stages_.items() if k in ("lead_clock", "lead_drips", "lead_tasks", "lead_weekly", "lead_exits")} == {"ops"}
          and {s.owner for k, s in stages_.items() if k in ("speed_to_lead", "lead_nurture", "lead_appointments")} == {"sales"},
          "Quinn owns the clock, the drips, the tasks and the exits; Roma owns speed to lead, the nurture bucket and appointments")
    lead_touchers = [s for k, s in stages_.items() if k not in ("speed_to_lead", "lead_weekly", "lead_exits")]
    check(len(lead_touchers) == 5 and all("lead" in (s.get("gate_allow") or []) for s in lead_touchers),
          "every stage that acts on a lead names leads in gate_allow")
    here = Path(__file__).resolve().parent
    mine = [here / "autopilot" / n for n in ("leadroutes.py", "leadclock.py", "drips.py", "leadtasks.py", "appointments.py",
                                             "exits.py", "leadcheck.py")] + [Path(__file__).resolve()]
    text = "".join(f.read_text(encoding="utf-8") for f in mine)
    check(chr(8212) not in text and chr(8211) not in text and chr(8) not in text, "no dash character and no backspace byte in the lead files")
    spec = here.parents[1] / "output" / "datasift_api" / "specs" / "datasift.yaml"
    route_rx = re.compile(r'"(/api/internal/[^"\n]*)"')
    used = set()
    for f in mine[:-1]:
        for m in route_rx.finditer(f.read_text(encoding="utf-8")):
            r = re.sub(r"%\([a-z_]+\)[sd]|%[sd]", "{x}", m.group(1).split("?")[0])
            used.add(r if r.endswith("/") else r + "/")
    if spec.is_file():
        paths = [[s for s in p.strip("/").split("/") if s] for p in
                 re.findall(r"^  (/api/internal/[^:\s]+):\s*$", spec.read_text(encoding="utf-8"), flags=re.M)]

        def documented(route):
            ours = [s for s in route.strip("/").split("/") if s]
            return any(len(t) == len(ours) and all((b.startswith("{") and a == "{x}") or a == b for a, b in zip(ours, t))
                       for t in paths)
        missing = sorted(r for r in used if not documented(r))
        check(not missing and len(used) >= 10, "every DataSift route the lead modules call is in the published reference: %s" % (missing or "ok"))
    else:
        check(len(used) >= 10, "the lead modules name their routes as plain strings (the published reference is not on this machine to check them)")
