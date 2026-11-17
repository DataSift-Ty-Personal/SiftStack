"""The DataSift log as a KPI source: calls, texts, phone statuses and stages, from the brain's own copy of it.

The account log is read ONCE into the brain's database (autopilot/activity.py) and the stages that need it keep
it current, so this adapter makes no DataSift request at all. A tracker syncs the copy first (kpi.sync_log);
this source then says how far the copy reaches, and a short copy is a problem, never a quiet day.

What it reports, and why each rule exists:
  * ONE CALL IS ONE ACT, however many rows describe it. owner.call.made is the dial; owner.call.answered (with
    the duration and the caller's name) and owner.call.noanswer come later for the same call, so they share its
    source id. An answer whose call id matches no dial is joined to the latest open dial to the same owner and
    number in the two hours before it, so a mismatched id can never turn one dial into two. Inbound calls are
    owner.call.received (answered) and owner.call.missed.
  * NEVER THE ATTEMPT COUNTERS. Calls through connected tools are said to raise the call counter, and on this
    account Kixie-dialed records still read zero (autopilot/attempts.py), so dials come from call events only.
  * A TEXT'S DELIVERY OR FAILURE IS THE SAME TEXT. owner.sms.delivered and owner.sms.failed fold into the
    owner.sms.sent row before them to the same number within ten minutes; counted apart they double the texts.
  * A PHONE STATUS KEEPS THE TOOL THAT WROTE IT (Kixie writes its own), so a number confirmed on a call can be
    told from one confirmed by a text reply or by a person, as kpi.correct_numbers does.
  * STAGES COME FROM A STAGE MAP. A lead is a record entering a lead status from outside one (kpi.new_leads); a
    contract, a close or an offer is a status, board column or tag the owner named; an appointment is a task of
    that kind. DataSift has no offer or contract object, so offers count nothing until the owner names them.
  * A ROW OUR OWN KEY WROTE (author self) IS NEVER A PERSON'S WORK, and when the ledger shows which of our
    workflows made that write (ap_changes within half an hour), the event says so (via). collect() then drops it
    when that workflow's own source is connected, so the agency's lead is counted once, as the agency's.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .. import store
from ..autopilot import activity, lanes
from ..autopilot.gate import digits
from . import Adapter, Batch, Event, Provides, fmt_when, setting, tool_key, words

MADE, ANSWERED, NOANSWER = "owner.call.made", "owner.call.answered", "owner.call.noanswer"
RECEIVED, MISSED = "owner.call.received", "owner.call.missed"
CALL_TYPES = (MADE, ANSWERED, NOANSWER, RECEIVED, MISSED)
SENT, RECEIVED_TEXT, DELIVERED, FAILED = "owner.sms.sent", "owner.sms.received", "owner.sms.delivered", "owner.sms.failed"
TEXT_TYPES = (SENT, RECEIVED_TEXT, DELIVERED, FAILED)
PHONE_TYPES = ("owner.phone.status.updated",)
STAGE_TYPES = ("property.status.updated", "property.tags.added", "card.created", "card.moved", "task.created")
GROUPS_FOR = {"call": {"calls"}, "phone_status": {"calls"}, "text": {"texts"}, "stage": {"state", "board"}}
WHAT = {"calls": "calls and phone statuses", "texts": "texts", "state": "status changes and tags",
        "board": "tasks and board cards"}
# Rows DataSift itself wrote (a person in the app, a sequence, a bulk action, our key): their tool is DataSift.
APP_SOURCES = {"", "internal-api", "sequence", "bulk", "auto-update", "data-enriching", "system", "import", "api"}
ANSWER_JOIN = timedelta(hours=2)
TEXT_FOLLOW = timedelta(minutes=10)

DEFAULT_STAGES = {
    "offer": {"statuses": [], "columns": [], "tags": []},
    "contract": {"statuses": ["under_contract"], "columns": [], "tags": []},
    "closed": {"statuses": ["closed"], "columns": [], "tags": []},
    "appointment": {"task_kinds": ["appointment"]},
}


def tool_of(src) -> str:
    s = str(src or "").strip().lower()
    return "datasift" if s in APP_SOURCES else tool_key(s)


def stage_map(settings: dict | None) -> dict:
    """The default map with the owner's own entries over it. Every value is compared as plain words, so
    "Under Contract" and "under_contract" meet."""
    out = {k: {kk: [words(x) for x in vv] for kk, vv in v.items()} for k, v in DEFAULT_STAGES.items()}
    for stage, given in ((settings or {}).get("stages") or {}).items():
        if stage not in out or not isinstance(given, dict):
            continue
        for key, vals in given.items():
            if key in out[stage]:
                vals = vals if isinstance(vals, list) else [vals]
                out[stage][key] = [words(x) for x in vals if words(x)]
    return out


def _author(r: dict) -> str:
    a = str(r.get("author") or "")
    if a == "self":
        return "self"
    if activity.by_person(r):
        return "person"
    return a


def _maybe_ours(r: dict) -> bool:
    """A row our key may have written: marked self, or stored before the author split (2026-11-15) as the
    ambiguous "user" through the API. The ledger settles the second kind: a write of ours at that moment makes it
    ours, and nothing else does."""
    a = str(r.get("author") or "")
    return a == "self" or (a == "user" and str(r.get("source") or "").strip().lower() == "internal-api")


def _writes(since: datetime, until: datetime) -> tuple[dict, dict]:
    """Our own workflows' writes around the window, from the ledger's change log: (record -> [(when, stage)] for
    status changes, number -> [(when, stage)] for phone changes)."""
    win = timedelta(minutes=max(1, setting("AGENTS_KPI_ECHO_MINUTES", 30)))
    lo = (since - win - timedelta(days=1)).isoformat(timespec="seconds")
    hi = (until + win + timedelta(days=1)).isoformat(timespec="seconds")
    status, phones = defaultdict(list), defaultdict(list)
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT at, stage, record_uuid, what, after FROM ap_changes WHERE at>=? AND at<? "
                         "AND what IN ('status', 'phones')", (lo, hi)).fetchall()
    for r in rows:
        try:
            when = datetime.fromisoformat(str(r["at"]))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if r["what"] == "status":
            status[str(r["record_uuid"] or "").lower()].append((when, r["stage"] or ""))
            continue
        try:
            after = json.loads(r["after"] or "[]")
        except ValueError:
            after = []
        for x in after if isinstance(after, list) else []:
            n = x.get("number") if isinstance(x, dict) else x
            if digits(n):
                phones[digits(n)].append((when, r["stage"] or ""))
    return status, phones


def _via(writes: dict, key: str, when: datetime) -> str:
    win = timedelta(minutes=max(1, setting("AGENTS_KPI_ECHO_MINUTES", 30)))
    near = [(abs((w - when).total_seconds()), stage) for w, stage in writes.get(key) or [] if abs(w - when) <= win]
    return min(near)[1] if near else ""


class DataSiftLog(Adapter):
    id = "datasift_log"
    tool = ""
    provides = (
        Provides("call", "api_key", frozenset({"direction", "phone", "line", "owner", "answered", "seconds", "outcome",
                                               "person_ref"}), per_person=True, scope="on_record", latency="poll",
                 backfill_days=activity.FLOOR_DAYS),
        Provides("text", "api_key", frozenset({"direction", "phone", "owner", "status"}), scope="on_record",
                 latency="poll", backfill_days=activity.FLOOR_DAYS),
        Provides("phone_status", "api_key", frozenset({"phone", "owner", "outcome"}), scope="on_record", latency="poll",
                 backfill_days=activity.FLOOR_DAYS),
        Provides("stage", "api_key", frozenset({"record", "outcome"}), scope="on_record", latency="poll",
                 backfill_days=activity.FLOOR_DAYS),
    )

    def settings_problems(self, settings: dict) -> list:
        out = []
        st = (settings or {}).get("stages")
        if st is None:
            return out
        if not isinstance(st, dict):
            return ["stages maps offer, contract, closed and appointment to what names them"]
        for stage, given in st.items():
            if stage not in DEFAULT_STAGES:
                out.append("stages has %r (it takes %s)" % (stage, ", ".join(DEFAULT_STAGES)))
            elif not isinstance(given, dict) or any(k not in DEFAULT_STAGES[stage] for k in given):
                out.append("stages.%s takes %s" % (stage, ", ".join(DEFAULT_STAGES[stage])))
        return out

    def pull(self, conn, since, until, kinds, now=None) -> Batch:
        now = now or datetime.now(timezone.utc)
        b = Batch()
        groups, types = set(), []
        for k in kinds:
            groups |= GROUPS_FOR.get(k, set())
        if "call" in kinds:
            types += CALL_TYPES
        if "phone_status" in kinds:
            types += PHONE_TYPES
        if "text" in kinds:
            types += TEXT_TYPES
        if "stage" in kinds:
            types += STAGE_TYPES
        slack = timedelta(minutes=max(0, setting("AGENTS_KPI_LOG_SLACK_MINUTES", 30)))
        need = min(until, now) - slack
        reached = []
        for g in sorted(groups):
            t = activity.synced_through(g)
            if t is None:
                b.complete = False
                b.problems.append("the DataSift log copy has not been read yet for %s, so they are not counted"
                                  % WHAT[g])
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            reached.append(t)
            if t < need:
                b.complete = False
                b.problems.append("the DataSift log copy holds %s only through %s, so later ones are not counted yet"
                                  % (WHAT[g], fmt_when(t)))
        b.through = min(reached) if reached else None
        rows = activity.events(tuple(types), since=since, until=until) if types else []
        if rows:
            b.last_at = max(r["when"] for r in rows)
        writes = _writes(since, until) if any(_maybe_ours(r) for r in rows) else ({}, {})
        sm = stage_map(conn.get("settings"))
        if "stage" in kinds and not any(sm["offer"].values()):
            b.notes.append("Offers are not counted: no status, board column or tag is named as an offer yet.")
        calls = [r for r in rows if r["event_type"] in CALL_TYPES]
        texts = [r for r in rows if r["event_type"] in TEXT_TYPES]
        for r in rows:
            et = r["event_type"]
            try:
                if et in PHONE_TYPES:
                    ev = self._phone(conn, r, writes[1])
                    if ev:
                        b.events.append(ev)
                elif et in STAGE_TYPES:
                    b.events += self._stages(conn, r, sm, writes[0])
            except ValueError as exc:
                b.problems.append("a DataSift log row could not be read (%s)" % str(exc)[:100])
        b.events += self._calls(conn, calls)
        b.events += self._texts(conn, texts)
        return b

    # ------------------------------------------------------------------ calls
    def _calls(self, conn, rows) -> list:
        """One act per call: the dial, then its answer or no answer, keyed on the call id with a fallback join."""
        out, by_uuid = [], {}
        open_dials: dict = defaultdict(list)          # (owner, number) -> [[when, key, closed]]
        for r in sorted(rows, key=lambda x: (x["when"], x["id"])):
            et, d = r["event_type"], r["data"] or {}
            uuid = str(d.get("call") or "")
            pair = (r["resource"], d.get("number") or "")
            if et == MADE:
                key = ("call:%s" % uuid) if uuid else "row:%s" % r["id"]
                if uuid:
                    by_uuid[uuid] = key
                open_dials[pair].append([r["when"], key, False])
            elif et in (ANSWERED, NOANSWER):
                key = by_uuid.get(uuid) if uuid else None
                slot = None
                if key:
                    slot = next((s for s in open_dials[pair] if s[1] == key), None)
                else:
                    cands = [s for s in open_dials[pair] if not s[2] and r["when"] - ANSWER_JOIN <= s[0] <= r["when"]]
                    slot = max(cands, key=lambda s: s[0]) if cands else None
                    key = slot[1] if slot else (("call:%s" % uuid) if uuid else "row:%s" % r["id"])
                if slot:
                    slot[2] = True
            else:
                key = ("call:%s" % uuid) if uuid else "row:%s" % r["id"]
            inbound = et in (RECEIVED, MISSED) or d.get("direction") == "inbound"
            answered = {ANSWERED: True, RECEIVED: True, NOANSWER: False, MISSED: False}.get(et)
            secs = int(d.get("duration") or 0) if et in (ANSWERED, RECEIVED) else 0
            out.append(Event(kind="call", at=r["when"], source=conn["id"], source_id=key,
                             tool=tool_of(d.get("source") or r.get("source")),
                             person_ref=str(d.get("caller") or d.get("caller_uuid") or ""),
                             direction="in" if inbound else "out", phone=d.get("number") or "",
                             line=d.get("line") or "", owner=r["resource"] or "", answered=answered,
                             seconds=secs if secs > 0 else None, outcome=str(d.get("status") or ""),
                             raw_ref="log:%s" % r["id"], author=_author(r)))
        return out

    # ------------------------------------------------------------------ texts
    def _texts(self, conn, rows) -> list:
        out, recent = [], {}
        for r in sorted(rows, key=lambda x: (x["when"], x["id"])):
            et, d = r["event_type"], r["data"] or {}
            pair = (r["resource"], d.get("number") or "")
            key = "row:%s" % r["id"]
            if et == RECEIVED_TEXT:
                direction, status = "in", "received"
            else:
                direction = "out"
                status = {SENT: "sent", DELIVERED: "delivered", FAILED: "failed"}[et]
                if et == SENT:
                    recent[pair] = (r["when"], key)
                elif pair in recent and r["when"] - recent[pair][0] <= TEXT_FOLLOW:
                    key = recent[pair][1]
            out.append(Event(kind="text", at=r["when"], source=conn["id"], source_id=key,
                             tool=tool_of(d.get("integration") or d.get("source") or r.get("source")),
                             direction=direction, phone=d.get("number") or "", owner=r["resource"] or "",
                             status=status, raw_ref="log:%s" % r["id"], author=_author(r)))
        return out

    # ------------------------------------------------------------------ phone statuses and stages
    def _phone(self, conn, r, writes: dict):
        d = r["data"] or {}
        to = str(d.get("to") or "").strip()
        if not to:
            return None
        who = _author(r)
        via = _via(writes, digits(d.get("number")), r["when"]) if _maybe_ours(r) else ""
        if via:
            who = "self"
        return Event(kind="phone_status", at=r["when"], source=conn["id"], source_id="row:%s" % r["id"],
                     tool=tool_of(r.get("source")), phone=d.get("number") or "", owner=r["resource"] or "",
                     outcome=to, raw_ref="log:%s" % r["id"], author=who, via=via)

    def _stages(self, conn, r, sm: dict, writes: dict) -> list:
        et, d = r["event_type"], r["data"] or {}
        hits = []                                     # (stage, what named it)
        if et == "property.status.updated":
            to, frm = str(d.get("to") or "").strip().lower(), str(d.get("from") or "").strip().lower()
            if to in lanes.LEAD_STATUSES and frm not in lanes.LEAD_STATUSES:
                hits.append(("lead", to))
            for st in ("offer", "contract", "closed"):
                if to and words(to) in sm[st]["statuses"]:
                    hits.append((st, to))
        elif et == "property.tags.added":
            for t in d.get("tags") or []:
                for st in ("offer", "contract", "closed"):
                    if words(t) and words(t) in sm[st]["tags"]:
                        hits.append((st, str(t)))
        elif et in ("card.created", "card.moved"):
            col = words(d.get("column"))
            for st in ("offer", "contract", "closed"):
                if col and col in sm[st]["columns"]:
                    hits.append((st, str(d.get("column"))))
        elif et == "task.created":
            if words(d.get("kind")) and words(d.get("kind")) in sm["appointment"]["task_kinds"]:
                hits.append(("appointment", str(d.get("kind"))))
        if not hits or not r["resource"]:
            return []
        who = _author(r)
        via = _via(writes, str(r["resource"]).lower(), r["when"]) if (
            _maybe_ours(r) and et == "property.status.updated") else ""
        if via:
            who = "self"
        return [Event(kind="stage", stage=st, at=r["when"], source=conn["id"], source_id="row:%s:%s" % (r["id"], st),
                      tool="datasift", record=r["resource"], outcome=what, raw_ref="log:%s" % r["id"], author=who,
                      via=via) for st, what in dict.fromkeys(hits)]


ADAPTER = DataSiftLog()
