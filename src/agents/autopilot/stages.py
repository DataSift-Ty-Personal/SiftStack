"""The stage handlers: assign, native_trace, retrace, score, auction, reactivate, digest (mail lives in
mail.py). Each fills a Summary. Every write goes through Ctx.write, which in plan mode records what
it would have done and touches nothing, so a plan run cannot write by accident.

Rules that cut across stages:
  * Dead statuses are never touched (sold, closed, under contract, not interested, DNC ...). A
    reactivation stage is the only one that reads not-interested records, and it only hands them
    back to a caller.
  * A source counts as a hit only when it found a CALLABLE number (Dial Fourth or better, not a
    litigator). Drop-tier numbers are still written, tagged, so the caller can see them, but the
    record keeps moving down the waterfall.
  * Money: every paid call checks the stage's daily cap, the autopilot's daily cap, the brain's
    daily cap and the per-record lifetime cap, in that order, before it is made.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, store
from . import gate, lanes, ledger, windows
from .sources import Subject, digits, subject_of

log = logging.getLogger(__name__)

DEAD_STATUSES = {"sold", "closed", "close_out", "lost_deal", "listed", "under_contract", "not_interested", "dnc",
                 "opt_out", "dead lead", "buyer", "buyer_found", "buyer_lost", "push_deal", "refer_lead",
                 "transactions", "underwrite"}
WORKLOAD_EXCLUDE = ["sold", "closed", "close_out", "lost_deal", "listed", "under_contract", "not_interested", "dnc",
                    "opt_out", "Dead Lead", "prospecting"]
# Statuses where a record is finished for good (exact DataSift values, for a search's must_not).
GONE_STATUSES = ["sold", "closed", "close_out", "lost_deal", "listed", "dnc", "opt_out", "Dead Lead"]
TIER_NAMES = ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")
# What counts as REACHED by a trace. The lessons dial and export only Dial First and Dial Second
# numbers with no phone status (4-05 @ 22:54, 14-13 @ 0:29, 5-03 @ 6:33), so a record holding only
# Third or Fourth numbers keeps going down the waterfall. A stage can widen it with callable_tiers.
CALLABLE_TIERS = {"Dial First", "Dial Second"}
BAD_PHONE = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
LITIGATOR_TAG = "DNC"
MAX_LINES = 12


# ---------------------------------------------------------------------- summary + context
@dataclass
class Summary:
    stage: str
    kind: str
    owner: str
    mode: str
    live: bool
    found: int = 0
    acted: int = 0
    usd: float = 0.0
    would_usd: float = 0.0
    head: str = ""
    lines: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    tally: Counter = field(default_factory=Counter)
    batch: int = 0
    writes: int = 0

    def line(self, text: str) -> None:
        if len(self.lines) < MAX_LINES:
            self.lines.append(text)
        elif len(self.lines) == MAX_LINES:
            self.lines.append("...")

    def problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["tally"] = dict(self.tally)
        return d


@dataclass
class Ctx:
    policy: object
    stage: object
    crm: object
    run_id: str
    now: datetime
    live: bool
    out: Summary
    sources: dict = field(default_factory=dict)
    scorer: object = None
    mailer: object = None
    task_id: int = 0
    enqueue: object = None          # store.enqueue, injectable
    _reserved: float = 0.0

    # --- writes
    def write(self, what: str, fn, *args, **kw):
        self.out.writes += 1
        if not self.live:
            return None
        return fn(*args, **kw)

    def change(self, record_uuid: str, what: str, before, after) -> None:
        """Remember what a live write replaced, so the run can be put back (plan 0.15). Call it
        BEFORE the write: a write that then fails leaves a change that undo simply re-applies."""
        if self.live:
            ledger.change(self.run_id, self.stage.id, record_uuid, what, before, after)

    # --- the gate
    def passes(self, rec: dict, channel: str, row: dict | None = None, allow=None) -> bool:
        """Run the suppression gate on a full record for this stage. A record that fails is counted
        under its reason ("held back: ...") and the caller skips it."""
        v = self.verdict(rec, channel, row=row, allow=allow)
        if not v.ok:
            self.out.tally["held back: %s" % v.reason()] += 1
        return v.ok

    def verdict(self, rec: dict, channel: str, row: dict | None = None, allow=None):
        cfg = getattr(self.policy, "gate", None) or gate.GateConfig()
        lanes_cfg = getattr(self.policy, "lanes", None)
        allow = set(_list(self.stage.get("gate_allow"))) | set(allow or ())
        ow = (rec or {}).get("owner") or {}
        custom = None
        if cfg.read_fields and (rec or {}).get("uuid") and self.stage.get("read_fields", True):
            custom = ledger.fact(rec["uuid"], "custom_fields", max_age=timedelta(days=7))
            if custom is None:
                try:
                    custom = self.crm.custom_fields(rec["uuid"])
                except Exception:  # noqa: BLE001  (a failed field read must be said out loud, not passed over)
                    custom = {}
                    self.out.problem("could not read the custom fields on a record, so the Owner Deceased field was not checked")
                ledger.set_fact(rec["uuid"], "custom_fields", custom)
        lit = set()
        for p in ow.get("phones") or []:
            n = digits(p.get("number"))
            c = ledger.phone(n) if n else None
            if c and c.get("litigator"):
                lit.add(n)
        return gate.check(rec, channel, cfg=cfg, lane=str(self.stage.get("lane") or ""), allow=allow, row=row,
                          custom=custom, owner=lanes.owner_lane(ow.get("uuid") or ""), litigators=lit,
                          lanes_cfg=lanes_cfg)

    # --- money
    def budget(self, usd: float, record_uuid: str = "", record_usd: float | None = None) -> tuple[bool, str]:
        """May `usd` more be spent today? `record_usd` (default `usd`) is what lands on the one record,
        checked against its lifetime cap; the day caps see the whole amount."""
        if usd <= 0:
            return True, ""
        d = ledger.day(self.now, self.policy.tz)
        stage_cap = self.stage.get("daily_usd")
        pending = self._reserved
        if stage_cap is not None and ledger.spent(d, self.stage.id) + pending + usd > float(stage_cap):
            return False, "stage cap $%.2f/day" % float(stage_cap)
        if self.stage.spend == "outreach":
            # Marketing spend has its own budget: it does not compete with traces and scoring for
            # the data cap, and it never counts toward the brain's research cap.
            if ledger.spent(d, action="mail") + pending + usd > self.policy.outreach_daily_usd:
                return False, "outreach cap $%.2f/day" % self.policy.outreach_daily_usd
            return True, ""
        if ledger.spent(d, exclude_action="mail") + pending + usd > self.policy.daily_usd:
            return False, "autopilot cap $%.2f/day" % self.policy.daily_usd
        if store.spent(day=datetime.now(timezone.utc).isoformat()[:10], research=False) + pending + usd > config.DAILY_CAP_USD:
            return False, "brain cap $%.2f/day" % config.DAILY_CAP_USD
        one = usd if record_usd is None else record_usd
        if record_uuid and ledger.spent(record_uuid=record_uuid) + one > self.policy.record_usd:
            return False, "record cap $%.2f lifetime" % self.policy.record_usd
        return True, ""

    def reserve(self, usd: float) -> None:
        self._reserved += usd
        if not self.live:
            self.out.would_usd += usd

    def settle(self, reserved: float, actual: float, what: str) -> None:
        """Turn a reservation into what was actually billed; the brain's ledger gets the real figure."""
        self._reserved = max(0.0, self._reserved - reserved)
        if self.live and actual > 0:
            self.out.usd += actual
            if self.task_id and self.stage.spend != "outreach":
                store.add_spend(self.task_id, actual, "autopilot %s: %s" % (self.stage.id, what))


def addr(x: dict) -> str:
    a = (x or {}).get("address") or {}
    if isinstance(a, str):
        return a
    street = a.get("street") or a.get("full_address") or ""
    city = a.get("city") or ""
    return ("%s, %s" % (street, city)).strip(", ") or (x or {}).get("uuid", "")[:8]


def tag_titles(x: dict) -> list[str]:
    out = []
    for t in (x or {}).get("tags") or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def status_of(x: dict) -> str:
    s = (x or {}).get("status")
    if isinstance(s, dict):
        s = s.get("value") or s.get("title") or ""
    return str(s or "").strip().lower()


def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def assignee_of(rec: dict) -> str:
    a = (rec or {}).get("assigned_to")
    return (a.get("uuid") if isinstance(a, dict) else a) or ""


def lane_entry(ctx: "Ctx", rec: dict, lane: str) -> datetime:
    """When this record entered its lane: the moment a lane marker (the FTM tag, the Obituary list)
    first landed on it, else the day the record was created. Read once from the record's own log
    and remembered. It is what the timeline counts days from and what the attempt keeper counts
    dials from, so a call made months ago under another list never counts toward this lane."""
    u = rec.get("uuid") or ""
    key = "lane_entry:%s" % (lane or "any")
    v = ledger.fact(u, key)
    if v:
        return datetime.fromisoformat(v)
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    marks = lanes_cfg.markers.get(lane) or {}
    want = {t.lower() for t in (marks.get("tags") or []) + (marks.get("lists") or [])}
    entry = created = None
    try:
        log_rows = ctx.crm.record_logs(u, ["property.created", "property.tags.added", "property.lists.added"])
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read a record's log for its lane entry date (%s)" % str(exc)[:100])
        log_rows = []
    from . import activity
    for e in log_rows:
        at = activity.parse_ts(e.get("timestamp") or "")
        if not at:
            continue
        et = e.get("event_type")
        p = e.get("payload") or {}
        if et == "property.created":
            created = at if created is None else min(created, at)
        else:
            names = {str(x).lower() for x in (p.get("tags") or []) + (p.get("lists") or []) + (p.get("system_lists") or [])}
            if want and names & want:
                entry = at if entry is None else min(entry, at)
    when = entry or created
    if when is None:
        # Older than the log reaches: the record has been here a long time. Use a far-back date
        # (callers clamp to their own look-back) and do not cache a guess as if it were known.
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    ledger.set_fact(u, key, when.isoformat(timespec="seconds"))
    return when


def merge_must(must: dict, extra: dict | None) -> dict:
    m = dict(must)
    for k, v in (extra or {}).items():
        if k in ("all_tags", "any_tags") and isinstance(m.get(k), list):
            m[k] = list(dict.fromkeys(list(m[k]) + list(v)))
        elif k == "must_not" and isinstance(m.get("must_not"), dict):
            mn = dict(m["must_not"])
            for kk, vv in v.items():
                mn[kk] = list(dict.fromkeys(list(mn.get(kk) or []) + list(vv))) if isinstance(vv, list) else vv
            m["must_not"] = mn
        else:
            m[k] = v
    return m


def stage_rows(ctx: Ctx, extra: dict | None = None, cap: int | None = None) -> list[tuple[str, dict]]:
    """(preset title, search row) for every record in the stage's presets, deduped in preset order.
    The presets are read live by title, so an edit in DataSift changes what the stage works on."""
    seen, out = set(), []
    cap = int(cap or ctx.stage.get("max_scan", 2000))
    for title in ctx.stage.presets:
        try:
            must = ctx.crm.preset_must(title)
        except KeyError as exc:
            ctx.out.problem("%s (the stage skipped it)" % exc)
            continue
        must, notes = windows.resolve(must, ctx.now)
        for n in notes:
            if "dropped" in n:
                ctx.out.problem("%s: %s" % (title, n))
        must = merge_must(must, extra)
        for row in ctx.crm.rows(must, cap=cap):
            u = row.get("uuid")
            if u and u not in seen:
                seen.add(u)
                out.append((title, row))
    return out


def required_tags(ctx: Ctx) -> dict | None:
    req = _list(ctx.stage.get("require_tags"))
    if not req:
        return None
    ids = []
    for t in req:
        u = ctx.crm.tag_uuid(t)
        if not u:
            ctx.out.problem("tag %r does not exist in this account, so nothing can match require_tags" % t)
            return {"all_tags": ["00000000-0000-0000-0000-000000000000"]}
        ids.append(u)
    return {"all_tags": ids}


# ---------------------------------------------------------------------- people
def resolve_callers(ctx: Ctx, names: list[str] | None = None) -> list[dict]:
    """Callers by DataSift first name, or by {name, uuid}. The team-user list the names resolve
    through is not in the published reference (only the key's own user is), so an install that
    gives uuids never calls it; an install that gives names reads it and says so if it fails."""
    names = names or _list(ctx.stage.get("callers")) or ctx.policy.callers
    direct = [n for n in names if isinstance(n, dict) and n.get("uuid")]
    if direct and len(direct) == len(names):
        return [{"name": str(n.get("name") or n["uuid"][:8]), "uuid": str(n["uuid"])} for n in direct]
    try:
        users = ctx.crm.users()
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read the team list (%s); give callers as {name, uuid} in the playbook" % str(exc)[:120])
        return [{"name": str(n.get("name") or n["uuid"][:8]), "uuid": str(n["uuid"])} for n in direct]
    out = [{"name": str(n.get("name") or n["uuid"][:8]), "uuid": str(n["uuid"])} for n in direct]
    for name in [n for n in names if not isinstance(n, dict)]:
        n = str(name).strip().lower()
        matches = [u for u in users if (u.get("first_name") or "").strip().lower() == n
                   or str(u.get("uuid") or "") == str(name)]
        active = [u for u in matches if u.get("is_active", True)]
        if not matches:
            ctx.out.problem("no DataSift user named %s; check people.callers in the playbook" % name)
        elif not active:
            ctx.out.problem("%s is inactive in DataSift, so gets nothing" % name)
        elif len(active) > 1:
            ctx.out.problem("two active users are named %s; list that caller by user uuid instead" % name)
        else:
            out.append({"name": active[0].get("first_name") or str(name), "uuid": active[0].get("uuid") or ""})
    return [p for p in out if p["uuid"]]


def balanced_pick(pool: list[dict], load: dict) -> dict:
    return min(pool, key=lambda p: (load.get(p["uuid"], 0), pool.index(p)))


# ---------------------------------------------------------------------- assign
def run_assign(ctx: Ctx) -> None:
    st = ctx.stage
    people = resolve_callers(ctx)
    rows = stage_rows(ctx, extra={"assigned_to": None})
    ctx.out.found = len(rows)
    if not rows:
        ctx.out.head = "No unassigned records in %s." % ", ".join(st.presets)
        return
    if not people:
        ctx.out.problem("no active caller to assign to, so %d records stay unassigned" % len(rows))
        return
    load = {p["uuid"]: ctx.crm.count({"assigned_to": p["uuid"], "must_not": {"any_property_status": WORKLOAD_EXCLUDE}})
            for p in people}
    # A caller past the cap is said out loud every run (plan 1.5). First-to-market records are still
    # assigned: they are the one thing that never waits. Which records leave an over-full queue is
    # the owner's decision (D2), so the stage reports the number and moves nothing.
    cap = int(st.get("caller_cap", 0) or 0)
    if cap:
        for person in people:
            if load.get(person["uuid"], 0) > cap:
                ctx.out.problem("%s holds %d open records, %d over the cap of %d" % (
                    person["name"], load[person["uuid"]], load[person["uuid"]] - cap, cap))
    routes = {str(k).lower(): [str(n).lower() for n in _list(v)] for k, v in (st.get("routes") or {}).items()}
    strategy = st.get("strategy", "balanced")
    rr = int(store.kv_get("autopilot:rr:%s" % st.id) or 0)
    max_n = int(st.get("max_per_run", 200))
    # The backlog rule: the first run (plan or live) draws a line. Records already sitting unassigned
    # at that moment are the owner's call (backlog: true hands them out too); everything that lands
    # after the line is assigned the moment the stage sees it.
    baseline = store.kv_get("autopilot:baseline:%s" % st.id)
    if not baseline:
        baseline = ctx.now.isoformat(timespec="seconds")
        store.kv_set("autopilot:baseline:%s" % st.id, baseline)
    backlog = bool(st.get("backlog", False))
    given: Counter = Counter()
    readback = 0
    for title, row in rows:
        if sum(given.values()) >= max_n:
            ctx.out.tally["left for the next run"] += 1
            continue
        u = row["uuid"]
        if status_of(row) in DEAD_STATUSES:
            continue
        if not backlog and ledger.first_seen(u, st.id, ctx.now).isoformat(timespec="seconds") <= baseline:
            ctx.out.tally["backlog left as is (set backlog: true to hand these out)"] += 1
            continue
        rec = ctx.crm.record(u)
        if not ctx.passes(rec, "assign", row=row):
            continue
        pool = people
        if routes:
            tags = {t.lower() for t in tag_titles(rec)}
            for tag, names in routes.items():
                if tag in tags:
                    pool = [p for p in people if p["name"].lower() in names] or people
                    break
        if strategy == "round_robin":
            who = pool[rr % len(pool)]
            rr += 1
        else:
            who = balanced_pick(pool, load)
        ctx.change(u, "assign", assignee_of(rec), who["uuid"])
        ctx.write("assign", ctx.crm.assign, u, who["uuid"])
        if ctx.live:
            if readback < 2 or sum(given.values()) % 25 == 0:
                back = ctx.crm.record(u).get("assigned_to")
                back = back.get("uuid") if isinstance(back, dict) else back
                readback += 1
                if back != who["uuid"]:
                    ctx.out.problem("assignment did not stick on %s (read back %r); stopped assigning" % (addr(row), back))
                    break
            ledger.event(ctx.run_id, st.id, u, "assign", {"to": who["name"], "preset": title}, now=ctx.now,
                         tz=ctx.policy.tz)
        load[who["uuid"]] = load.get(who["uuid"], 0) + 1
        given[who["name"]] += 1
        ctx.out.line("%s -> %s (%s)" % (addr(row), who["name"], title))
    if strategy == "round_robin" and ctx.live:
        store.kv_set("autopilot:rr:%s" % st.id, str(rr))
    ctx.out.acted = sum(given.values())
    verb = "Assigned" if ctx.live else "Would assign"
    held = ctx.out.tally.get("backlog left as is (set backlog: true to hand these out)", 0)
    if ctx.out.acted:
        ctx.out.head = "%s %d new record(s): %s." % (verb, ctx.out.acted,
                                                     ", ".join("%s %d" % kv for kv in given.most_common()))
    else:
        ctx.out.head = "No new arrivals to assign."
    if held:
        ctx.out.head += " %d unassigned record(s) predate the autopilot and are left for the owner's call." % held


# ---------------------------------------------------------------------- native trace
def run_native_trace(ctx: Ctx) -> None:
    st = ctx.stage
    rows = stage_rows(ctx)
    ctx.out.found = len(rows)
    # A bulk trace never opens each record, so only the row's own status can be checked here.
    kept = []
    for title, r in rows:
        v = gate.check_row(r, "trace", cfg=getattr(ctx.policy, "gate", None), allow=_list(st.get("gate_allow")))
        if v.ok:
            kept.append((title, r))
        else:
            ctx.out.tally["held back: %s" % v.reason()] += 1
    rows = kept
    if not rows:
        ctx.out.head = "Nothing is waiting on a skip trace."
        return
    min_age = timedelta(hours=float(st.get("min_age_hours", 2)))
    waiting = [r for _, r in rows if ctx.now - ledger.first_seen(r["uuid"], st.id, ctx.now) >= min_age]
    if not waiting:
        ctx.out.head = "%d new record(s) need a trace; giving DataSift %s hours to trace them on its own first." % (
            len(rows), st.get("min_age_hours", 2))
        return
    if st.get("scoped", True):
        # A scoped trace touches only the waiting records, so it may run every slot (the challenge
        # teaches every 30 minutes, C2.2); a record traced in the last day is not sent again.
        recent = ctx.now - timedelta(hours=24)
        waiting = [r for r in waiting if not ledger.events(r["uuid"], "native_trace", since=recent)]
        if not waiting:
            ctx.out.head = "Every record waiting on a trace was sent to DataSift in the last day."
            return
        _native_scoped(ctx, waiting)
        return
    every = timedelta(hours=float(st.get("every_hours", 24)))
    last = store.kv_get("autopilot:native_trace:last")
    if last and ctx.now - datetime.fromisoformat(last) < every:
        ctx.out.head = "%d record(s) still need a trace; DataSift's trace ran at %s and may run again after %s." % (
            len(waiting), last, (datetime.fromisoformat(last) + every).isoformat(timespec="minutes"))
        return
    stats = ctx.crm.skiptrace_stats()
    spent, saved = float(stats.get("value_spent") or 0), float(stats.get("saved_by_unlimited_skiptracing") or 0)
    if spent > 0 or saved <= 0:
        ctx.out.problem("this account looks metered (value spent $%.2f): the account-wide trace would bill every "
                        "record, so it was not run. Use scoped: true, or the retrace stage covers these." % spent)
        return
    ctx.write("native trace", ctx.crm.skiptrace_all)
    if ctx.live:
        store.kv_set("autopilot:native_trace:last", ctx.now.isoformat(timespec="seconds"))
        ledger.event(ctx.run_id, st.id, "", "native_trace", {"waiting": len(waiting), "scope": "account"},
                     now=ctx.now, tz=ctx.policy.tz)
    ctx.out.acted = len(waiting)
    ctx.out.head = "%s DataSift's own skip trace for %d waiting record(s) (account-wide route, free on the unlimited plan)." % (
        "Ran" if ctx.live else "Would run", len(waiting))


def _native_scoped(ctx: Ctx, waiting: list[dict]) -> None:
    """The documented flow: a dry run on exactly these records, refuse unless it covers exactly
    them, then the real call carrying expected_count so the server refuses any other selection.
    That pair is the guard against the 2026-08-19 class of bug, where an ignored scope traced the
    whole account. The bulk policy treats a skip trace as an ACTIVITY: under 20 records it is not
    allocated properly and repeated small ones count as abuse, so a smaller set waits (DataSift
    traces new uploads on its own, which is where a few stragglers usually get picked up)."""
    st = ctx.stage
    floor = int(st.get("min_batch", 20))
    uuids = [r["uuid"] for r in waiting][: min(int(st.get("max_records", 1000)), 1000)]
    if len(uuids) < floor:
        ctx.out.head = ("%d record(s) wait on a trace; the bulk policy needs %d or more per job, so they wait for "
                        "more to arrive (DataSift's own upload trace usually takes them first)." % (len(uuids), floor))
        return
    ctx.out.acted = len(uuids)
    if not ctx.live:
        ctx.out.head = "Would ask DataSift to skip trace %d waiting record(s), scoped to exactly those (dry run first)." % len(uuids)
        return
    est = ctx.crm.skiptrace_scoped(uuids, dry_run=True)
    n = None
    if isinstance(est, dict):
        n = est.get("number_of_records", est.get("count", est.get("expected_count")))
    cost = float((est or {}).get("cost") or 0) if isinstance(est, dict) else 0.0
    if n != len(uuids):
        ctx.out.acted = 0
        ctx.out.problem("DataSift's dry run covers %r records, not the %d asked for, so nothing was traced "
                        "(the scope did not take; check the payload before trying again)" % (n, len(uuids)))
        return
    if cost > 0:
        ok, why = ctx.budget(cost)
        if not ok:
            ctx.out.acted = 0
            ctx.out.problem("DataSift quotes $%.2f for this trace, over the %s; not run" % (cost, why))
            return
    ctx.crm.skiptrace_scoped(uuids, dry_run=False, tags=_list(st.get("tags")))
    ctx.settle(0, cost, "native trace x%d" % len(uuids))
    store.kv_set("autopilot:native_trace:last", ctx.now.isoformat(timespec="seconds"))
    for u in uuids:
        ledger.event(ctx.run_id, st.id, u, "native_trace", {"scope": "records"}, usd=round(cost / len(uuids), 4),
                     now=ctx.now, tz=ctx.policy.tz)
    ctx.out.head = "Asked DataSift to skip trace %d record(s), scoped and estimated first%s." % (
        len(uuids), (", $%.2f" % cost) if cost else ", free on this plan")


# ---------------------------------------------------------------------- scoring (shared)
def eligible(p: dict) -> bool:
    tags = set(tag_titles(p))
    if tags & set(TIER_NAMES) or LITIGATOR_TAG in tags:
        return False
    if (p.get("status") or "UNKNOWN").upper() in BAD_PHONE:
        return False
    return bool(digits(p.get("number")))


def phone_payload(p: dict, tier: str, extra_tags: list[str] | None = None) -> dict:
    """The full phone object upsert-phones needs, with exactly one tier (mirrors d4d_call_list):
    a union would leave a re-scored phone matching two dialer presets at once."""
    tags = [t for t in tag_titles(p) if t not in TIER_NAMES]
    for t in (extra_tags or []) + ([tier] if tier and tier != "Unknown" else []):
        if t and t not in tags:
            tags.append(t)
    return {"number": digits(p.get("number")) or p.get("number"), "type": p.get("type") or "UNKNOWN",
            "tags": sorted(tags), "status": (p.get("status") or "UNKNOWN").upper(),
            "is_connected": p.get("is_connected", True), "verified": p.get("verified", False)}


def score_numbers(ctx: Ctx, numbers: list[str], record_uuid: str = "") -> dict:
    """number -> {"tier", "score", ...}. Cached numbers cost nothing; new ones go to Trestle inside
    the caps. In plan mode the new ones are priced, not scored."""
    out, need = {}, []
    for n in dict.fromkeys(numbers):
        c = ledger.phone(n)
        if c and c.get("tier"):
            out[n] = {"tier": c["tier"], "score": c.get("score"), "cached": True, "litigator": bool(c.get("litigator"))}
        else:
            need.append(n)
    if not need:
        return out
    if not ctx.scorer or not ctx.scorer.configured():
        ctx.out.problem("no Trestle key on this box, so new numbers land unscored (set TRESTLE_PAID_API_KEY)")
        return out
    unit = ctx.scorer.unit_usd
    allowed = []
    for n in need:
        ok, why = ctx.budget(unit * (len(allowed) + 1), record_uuid)
        if not ok:
            ctx.out.tally["numbers left unscored (%s)" % why] += len(need) - len(allowed)
            break
        allowed.append(n)
    if not allowed:
        return out
    cost = round(unit * len(allowed), 4)
    ctx.reserve(cost)
    if not ctx.live:
        ctx.out.tally["numbers to score"] += len(allowed)
        ctx.settle(cost, 0, "score")
        return out
    res = ctx.scorer.score(allowed)
    paid = 0
    for n, r in res.items():
        if r.get("error"):
            ctx.out.tally["scoring errors"] += 1
            continue
        paid += 1
        ledger.set_phone(n, r.get("score"), r.get("tier") or "", r.get("line_type") or "", bool(r.get("litigator")))
        out[n] = r
    ctx.settle(cost, round(paid * unit, 4), "scored %d numbers" % paid)
    if record_uuid and paid:
        ledger.event(ctx.run_id, ctx.stage.id, record_uuid, "score", {"numbers": paid}, usd=round(paid * unit, 4),
                     now=ctx.now, tz=ctx.policy.tz)
    ctx.out.tally["numbers scored"] += paid
    return out


def phone_tier(p: dict) -> str:
    return next((t for t in tag_titles(p) if t in TIER_NAMES), "")


def litigator_tag(ctx: Ctx) -> str:
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    return (cfg.litigator_tags or ["Litigator"])[0]


def suppress_litigator(ctx: Ctx, rec: dict) -> None:
    """A litigator's number is a RECORD problem, not a phone problem (plan 0.3): the record, and
    every other record the same owner holds, is tagged Litigator and Do Not Market for good, with
    a note. Marking only the one phone left the owner's other numbers, the mail lane and the
    owner's other houses open."""
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    add = [(cfg.litigator_tags or ["Litigator"])[0], (cfg.do_not_market_tags or ["Do Not Market"])[0]]
    u = rec.get("uuid") or ""
    targets = [(u, rec)]
    owner_uuid = (rec.get("owner") or {}).get("uuid") or ""
    if owner_uuid:
        try:
            for r in ctx.crm.owner_records(owner_uuid):
                if r.get("uuid") and r["uuid"] != u:
                    targets.append((r["uuid"], None))
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not list a litigator's other records (%s); only the one record was tagged" % str(exc)[:100])
    for uu, full in targets:
        have = set(tag_titles(full)) if full else set()
        need = [t for t in add if t not in have]
        if not need:
            continue
        ctx.change(uu, "add_tags", None, need)
        ctx.write("litigator", ctx.crm.add_tags, uu, need, full)
        ctx.write("litigator note", ctx.crm.message, uu,
                  "A number on this owner belongs to a known TCPA litigator. Do not call, text or mail, on any record this owner holds.")
        if ctx.live:
            ledger.event(ctx.run_id, ctx.stage.id, uu, "litigator", {"tags": need}, now=ctx.now, tz=ctx.policy.tz)
        ctx.out.tally["records suppressed for a litigator"] += 1
    ctx.out.line("%s: a litigator's number, record%s tagged %s" % (addr(rec), "s" if len(targets) > 1 else "", " and ".join(add)))


def run_score(ctx: Ctx) -> None:
    st = ctx.stage
    rows = stage_rows(ctx, extra=required_tags(ctx))
    ctx.out.found = len(rows)
    recheck = timedelta(days=float(st.get("recheck_days", 7)))
    max_records = int(st.get("max_records", 200))
    done = 0
    checked = 0
    # Finding a record's unscored numbers means reading the record, and a lane of 568 records is
    # about 750 requests: twenty minutes of this app's share that the next assignment run then waits
    # behind. So one run reads at most max_reads records, the ones never read first and then the
    # ones read longest ago, and says how many it left. In plan mode nothing is ever marked scored,
    # so without the cap every run would read the whole lane again.
    max_reads = int(st.get("max_reads", 250))
    reads = 0
    read_at = {r["uuid"]: ledger.fact(r["uuid"], "score_read") for _, r in rows}
    rows = sorted(rows, key=lambda tr: str(read_at.get(tr[1]["uuid"]) or ""))
    for _, row in rows:
        if done >= max_records:
            ctx.out.tally["left for the next run"] += 1
            continue
        u = row["uuid"]
        if ledger.fact(u, "scored", max_age=recheck):
            continue
        if reads >= max_reads:
            ctx.out.tally["not read this run (read cap %d)" % max_reads] += 1
            continue
        reads += 1
        rec = ctx.crm.record(u)
        ledger.set_fact(u, "score_read", ctx.now.isoformat(timespec="seconds"))   # the autopilot's own note, kept in plan mode too
        if not ctx.passes(rec, "score", row=row):
            continue
        ow = rec.get("owner") or {}
        elig = [p for p in (ow.get("phones") or []) if eligible(p)]
        if not elig:
            if ctx.live:
                ledger.set_fact(u, "scored", ctx.now.isoformat(timespec="seconds"))
            continue
        tiers = score_numbers(ctx, [digits(p["number"]) for p in elig], u)
        lit_tag = litigator_tag(ctx)
        payload = [phone_payload(p, (tiers.get(digits(p["number"])) or {}).get("tier", ""),
                                 [lit_tag] if (tiers.get(digits(p["number"])) or {}).get("litigator") else None)
                   for p in elig if (tiers.get(digits(p["number"])) or {}).get("tier") not in (None, "", "Unknown")]
        done += 1
        if any((tiers.get(digits(p["number"])) or {}).get("litigator") for p in elig):
            suppress_litigator(ctx, rec)
        if payload and ow.get("uuid"):
            ctx.change(u, "phones", [phone_payload(p, phone_tier(p)) for p in elig], [ph["number"] for ph in payload])
            ctx.write("tier phones", ctx.crm.upsert_phones, ow["uuid"], payload)
            if ctx.live and checked < 1:
                back = {digits(p.get("number")): p for p in ((ctx.crm.record(u).get("owner") or {}).get("phones") or [])}
                checked += 1
                for ph in payload:
                    got = [t for t in tag_titles(back.get(ph["number"]) or {}) if t in TIER_NAMES or t == LITIGATOR_TAG]
                    want = [t for t in ph["tags"] if t in TIER_NAMES or t == LITIGATOR_TAG]
                    if got != want:
                        ctx.out.problem("tier read-back mismatch on %s: wrote %s, read %s" % (addr(rec), want, got))
                        break
        if ctx.live:
            ledger.set_fact(u, "scored", ctx.now.isoformat(timespec="seconds"))
        ctx.out.line("%s: %d number(s)%s" % (addr(rec), len(elig), "" if ctx.live else " to score"))
    ctx.out.acted = done
    n = ctx.out.tally.get("numbers scored", 0) or ctx.out.tally.get("numbers to score", 0)
    ctx.out.head = "%s %d number(s) on %d record(s)%s." % (
        "Scored" if ctx.live else "Would score", n, done,
        (", $%.2f" % ctx.out.usd) if ctx.live else (", about $%.2f" % ctx.out.would_usd))


# ---------------------------------------------------------------------- retrace (the waterfall)
def _handoff(ctx: Ctx, rec: dict, subj: Subject, why: str) -> None:
    st = ctx.stage
    u = subj.record_uuid
    if ledger.handed_off(u, "deep_prospect"):
        return
    if not ctx.passes(rec, "research"):
        return
    d = ledger.day(ctx.now, ctx.policy.tz)
    today_n = sum(1 for e in ledger.events(action="handoff", stage=st.id) if e["day"] == d)
    cap = int(st.get("deep_prospect_per_day", 3))
    if today_n + ctx.out.tally.get("to Diggs for a deep prospect", 0) >= cap:
        ctx.out.tally["deep prospect waiting (cap %d/day)" % cap] += 1
        return
    enqueue = ctx.enqueue or store.enqueue
    tid = ctx.write("handoff", enqueue, "research", "deep_prospect", u, addr(rec), source="autopilot:%s" % st.id)
    if ctx.live:
        ledger.event(ctx.run_id, st.id, u, "handoff", {"to": "deep_prospect", "why": why,
                                                       "task": tid[0] if tid else 0}, now=ctx.now, tz=ctx.policy.tz)
    ctx.out.tally["to Diggs for a deep prospect"] += 1
    ctx.out.line("%s: %s, to Diggs for a deep prospect" % (addr(rec), why))


def _after(ctx: Ctx, rec: dict, subj: Subject, why: str) -> None:
    after = ctx.stage.get("after_sources", "mail_only")
    if after == "deep_prospect":
        _handoff(ctx, rec, subj, why)
    elif after == "mail_only":
        tag = ctx.stage.get("mail_only_tag", "Mail Only")
        if tag in tag_titles(rec):
            return
        ctx.change(subj.record_uuid, "add_tags", None, [tag])
        ctx.write("mail only", ctx.crm.add_tags, subj.record_uuid, [tag], rec)
        if ctx.live:
            ledger.event(ctx.run_id, ctx.stage.id, subj.record_uuid, "after", {"to": "mail_only", "why": why},
                         now=ctx.now, tz=ctx.policy.tz)
        ctx.out.tally["moved to mail only"] += 1
        ctx.out.line("%s: %s, tagged %s" % (addr(rec), why, tag))
    else:
        ctx.out.tally["left in place (%s)" % why] += 1


def _write_numbers(ctx: Ctx, rec: dict, subj: Subject, nums: list[dict], source: str) -> int:
    """Score, then add the new numbers once, tiered and tagged with their source. Returns how many
    are callable (the measure of a hit)."""
    tiers = score_numbers(ctx, [n["number"] for n in nums], subj.record_uuid)
    tag = {"smartskip": "SmartSkip"}.get(source, source.capitalize())
    reached = set(_list(ctx.stage.get("callable_tiers"))) or CALLABLE_TIERS
    lit_tag = litigator_tag(ctx)
    phones, callable_n, litigator = [], 0, False
    for n in nums:
        res = tiers.get(n["number"]) or {}
        t = res.get("tier", "")
        is_lit = bool(res.get("litigator"))
        litigator = litigator or is_lit
        phones.append(phone_payload({"number": n["number"], "type": n.get("type") or "UNKNOWN", "tags": []}, t,
                                    [tag] + ([lit_tag] if is_lit else [])))
        # Unscored (no Trestle key, an Unknown score) cannot be judged, so it counts as a hit: the
        # waterfall must not keep paying the next source for a record that already has numbers.
        if not is_lit and (t in reached or t in ("", "Unknown")):
            callable_n += 1
    if subj.owner_uuid:
        ctx.change(subj.record_uuid, "add_numbers", None, [p["number"] for p in phones])
        ctx.write("add numbers", ctx.crm.upsert_phones, subj.owner_uuid, phones)
        if ctx.stage.get("count_as_traced", True):
            # Numbers from an outside source land through the phone route, which DataSift does not
            # count as a skip trace: the record keeps reading "never skip traced" and stays out of
            # every Ready to Call preset until the owner's counter says otherwise (plan 0.10).
            total = int((rec.get("owner") or {}).get("skiptrace_attempts") or 0) + 1
            ctx.change(subj.record_uuid, "skiptrace_attempts", total - 1, total)
            ctx.write("skip trace counter", ctx.crm.skiptrace_attempts, subj.owner_uuid, total)
    if litigator:
        suppress_litigator(ctx, rec)
    return callable_n


def run_retrace(ctx: Ctx) -> None:
    st = ctx.stage
    rows = stage_rows(ctx, extra=required_tags(ctx), cap=int(st.get("max_scan", 500)))
    ctx.out.found = len(rows)
    order = [s for s in _list(st.get("sources"))]
    max_records = int(st.get("max_records", 50))
    deceased_to = st.get("deceased", "deep_prospect")
    entity_to = st.get("entity", "after")
    new_tag = st.get("new_numbers_tag", "")
    queue: dict[str, dict] = {}
    for _, row in rows:
        if len(queue) + ctx.out.tally.get("to Diggs for a deep prospect", 0) >= max_records:
            ctx.out.tally["left for the next run"] += 1
            continue
        u = row["uuid"]
        if status_of(row) in DEAD_STATUSES:
            continue
        rec = ctx.crm.record(u)
        if status_of(rec) in DEAD_STATUSES:
            continue
        subj = subject_of(rec)
        if subj.kind in ("deceased", "probate") and deceased_to == "deep_prospect":
            _handoff(ctx, rec, subj, "%s owner" % subj.kind)
            continue
        if subj.kind == "entity":
            if entity_to == "deep_prospect":
                _handoff(ctx, rec, subj, "entity owner, needs its principal")
            else:
                ctx.out.tally["entity owners (need a principal)"] += 1
                if ctx.live:
                    _after(ctx, rec, subj, "entity owner, no person to trace")
            continue
        v = ctx.verdict(rec, "trace", row=row, allow={"deceased"} if deceased_to == "retrace" else None)
        if not v.ok:
            if v.code == "deceased" and deceased_to == "deep_prospect":
                # The gate reads death from more places than the owner flag (the Obituary list, the
                # Owner Deceased field), and a possible death goes to research, never to a blind trace.
                _handoff(ctx, rec, subj, "the owner may have died (%s)" % v.reasons[0][1])
            else:
                ctx.out.tally["held back: %s" % v.reason()] += 1
            continue
        rem = [s for s in order if s not in ledger.tried_sources(u)]
        if not rem:
            if ctx.live:
                _after(ctx, rec, subj, "every source already tried")
            else:
                ctx.out.tally["every source already tried"] += 1
            continue
        queue[u] = {"rec": rec, "subj": subj, "rem": rem, "hit": False}
    plan_round = 0
    for _ in range(len(order)):
        groups: dict[str, list[str]] = defaultdict(list)
        for u, q in queue.items():
            if q["rem"] and not q["hit"]:
                groups[q["rem"][0]].append(u)
        if not groups:
            break
        for name, us in groups.items():
            src = ctx.sources.get(name)
            if not src or not src.configured():
                ctx.out.problem("source %s has no key on this box, skipped" % name)
                for u in us:
                    queue[u]["rem"].pop(0)
                continue
            allowed, capped = [], ""
            for u in us:
                ok, why = ctx.budget(src.unit_usd * (len(allowed) + 1), u, record_usd=src.unit_usd)
                if not ok:
                    if why.startswith("record cap"):
                        ctx.out.tally["at the per-record cap"] += 1
                        queue[u]["rem"] = []
                        continue
                    capped = why
                    break
                allowed.append(u)
            if capped:
                left = [u for u in us if u not in allowed and queue[u]["rem"]]
                ctx.out.tally["not traced today (%s)" % capped] += len(left)
                for u in left:
                    queue[u]["rem"] = []
                    queue[u]["capped"] = True
            if not allowed:
                continue
            worst = round(src.unit_usd * len(allowed), 2)
            ctx.reserve(worst)
            if not ctx.live:
                ctx.out.line("%s %d record(s) via %s (up to $%.2f)" % (
                    "would trace" if plan_round == 0 else "misses would then go to", len(allowed), name, worst))
                ctx.out.tally["traces planned via %s" % name] += len(allowed)
                ctx.settle(worst, 0, name)
                for u in allowed:
                    queue[u]["rem"].pop(0)
                continue
            subjects = [queue[u]["subj"] for u in allowed]
            try:
                found, billed = src.trace(subjects)
            except Exception as exc:  # noqa: BLE001
                ctx.settle(worst, 0, name)
                ctx.out.problem("%s failed, nothing written: %s" % (name, str(exc)[:200]))
                for u in allowed:
                    queue[u]["rem"].pop(0)
                    queue[u]["failed"] = True
                continue
            ctx.settle(worst, billed, "%s x%d" % (name, len(subjects)))
            per = (billed / len(subjects)) if subjects else 0.0
            costs = getattr(src, "costs", None) or {}
            for u in allowed:
                q = queue[u]
                rec, subj = q["rec"], q["subj"]
                nums = [n for n in found.get(u, []) if n["number"] not in subj.existing]
                usd_u = costs.get(u, 0.0) if (costs and src.per == "match") else per
                ledger.event(ctx.run_id, st.id, u, "trace", {"source": name, "found": len(found.get(u, [])),
                                                            "new": len(nums)}, usd=round(usd_u, 4), now=ctx.now,
                             tz=ctx.policy.tz)
                q["rem"].pop(0)
                if not nums:
                    continue
                good = _write_numbers(ctx, rec, subj, nums, name)
                subj.existing |= {n["number"] for n in nums}
                if good:
                    ctx.out.tally["found callable numbers via %s" % name] += 1
                    ctx.out.line("%s: %d new number(s) via %s, %d callable" % (addr(rec), len(nums), name, good))
                    if new_tag and subj.attempts > 0:
                        ctx.write("new numbers tag", ctx.crm.add_tags, u, [new_tag], None)
                        ledger.set_fact(u, "new_numbers_at_attempts", subj.attempts)
                    q["hit"] = True
        plan_round += 1
    misses = [q for q in queue.values() if not q["hit"] and not q["rem"] and not q.get("capped") and not q.get("failed")]
    if ctx.live:
        for q in misses:
            _after(ctx, q["rec"], q["subj"], "no source found a callable number")
    elif misses:
        ctx.out.tally["if every source misses: %s" % st.get("after_sources", "mail_only")] += len(misses)
    hits = sum(1 for q in queue.values() if q["hit"])
    ctx.out.acted = hits + ctx.out.tally.get("to Diggs for a deep prospect", 0) + ctx.out.tally.get("moved to mail only", 0)
    if ctx.live:
        ctx.out.head = "Retraced %d record(s): %d found callable numbers, %d to Diggs, %d to mail only, $%.2f." % (
            len(queue), hits, ctx.out.tally.get("to Diggs for a deep prospect", 0), ctx.out.tally.get("moved to mail only", 0),
            ctx.out.usd)
    else:
        ctx.out.head = "Would retrace %d record(s) (up to $%.2f with scoring), %d to Diggs, %d entity owners." % (
            len(queue), ctx.out.would_usd, ctx.out.tally.get("to Diggs for a deep prospect", 0),
            ctx.out.tally.get("entity owners (need a principal)", 0))


# ---------------------------------------------------------------------- auction
def _custom_dates(ctx: Ctx, uuid: str, labels: list[str]) -> dict:
    r = ctx.crm.call("/api/internal/property/%s/custom-field/" % uuid)
    items = r if isinstance(r, list) else ((r or {}).get("results") or (r or {}).get("data") or [])
    want = {x.lower() for x in labels}
    out = {}
    for it in items:
        cf = it.get("custom_field") if isinstance(it.get("custom_field"), dict) else {}
        label = (cf.get("label") or cf.get("name") or cf.get("title") or "").strip()
        if label.lower() in want and it.get("value"):
            out[label] = str(it["value"])[:10]
    return out


def _as_date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def run_auction(ctx: Ctx) -> None:
    st = ctx.stage
    tag = st.get("tag", "Auction Soon")
    soon = int(st.get("soon_days", 21))
    fields = _list(st.get("date_fields")) or ["auction_date", "tax_auction_date"]
    custom = _list(st.get("custom_date_fields")) or ["Foreclosure Date"]
    trust_builtin_fc = bool(st.get("trust_builtin_foreclosure_date", False))
    refresh = timedelta(days=float(st.get("refresh_days", 3)))
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    tag_uuid = ctx.crm.tag_uuid(tag)
    if not tag_uuid:
        ctx.out.problem("there is no %r tag in this account; create it (the Hot Auction preset keys on it)" % tag)
    rows = stage_rows(ctx) if ctx.stage.presets else []
    cands = {r["uuid"]: r for _, r in rows}
    scope = _list(st.get("scope_tags"))
    if scope:
        ids = [ctx.crm.tag_uuid(t) for t in scope]
        if all(ids):
            # A record already in negotiation still has its sale date: the scope is the tag, not a
            # calling preset (those drop every record with a lead status).
            for r in ctx.crm.rows({"all_tags": ids, "must_not": {"any_property_status": GONE_STATUSES}},
                                  cap=int(st.get("max_scan", 3000))):
                cands.setdefault(r["uuid"], r)
        else:
            ctx.out.problem("scope_tags %s: one of those tags does not exist" % scope)
    tagged = {r["uuid"]: r for r in ctx.crm.rows({"all_tags": [tag_uuid]}, cap=2000)} if tag_uuid else {}
    for u, r in tagged.items():
        cands.setdefault(u, r)
    # A second, tighter tier (plan 1.8): hot inside soon_days, critical inside critical_days. The
    # critical tag is what a priority package or a door-knock route keys on.
    crit_tag = str(st.get("critical_tag") or "")
    crit_days = int(st.get("critical_days", 14))
    crit_uuid = ctx.crm.tag_uuid(crit_tag) if crit_tag else ""
    if crit_tag and not crit_uuid:
        ctx.out.problem("there is no %r tag in this account yet; create it and the critical tier starts" % crit_tag)
    crit_tagged = {r["uuid"] for r in ctx.crm.rows({"all_tags": [crit_uuid]}, cap=2000)} if crit_uuid else set()
    # The day after the sale every channel stops (decision D39): the record takes this status, which
    # the gate treats as finished. Unset, the stage only untags.
    after_status = str(st.get("after_sale_status") or "")
    after_window = int(st.get("after_sale_window_days", 60))
    ctx.out.found = len(cands)
    hot = []
    # Reading a sale date costs two requests a record (the record, then its custom fields), so one
    # run reads at most max_reads of them: records never read come first, then the stalest. The rest
    # are judged on the date already known, and a record whose date is not known yet waits a run.
    max_reads = int(st.get("max_reads", 400))
    reads = 0
    known = {u: ledger.fact(u, "auction_dates") for u in cands}
    order = sorted(cands, key=lambda u: (known[u] is not None, u in tagged))
    for u in order:
        row = cands[u]
        dates = ledger.fact(u, "auction_dates", max_age=refresh) if known[u] is not None else None
        if dates is None:
            if reads >= max_reads:
                if known[u] is None:
                    ctx.out.tally["sale date not read yet (read cap, next run)"] += 1
                    continue
                dates = known[u]
            else:
                reads += 1
                rec = ctx.crm.record(u)
                dates = {f: rec.get(f) for f in fields if rec.get(f)}
                if trust_builtin_fc and rec.get("foreclosure_date"):
                    dates["foreclosure_date"] = rec["foreclosure_date"]
                dates.update(_custom_dates(ctx, u, custom))
                ledger.set_fact(u, "auction_dates", dates)
        upcoming = sorted(d for d in (_as_date(v) for v in dates.values()) if d and d >= today)
        nxt = upcoming[0] if upcoming else None
        want = nxt is not None and (nxt - today).days <= soon
        has = u in tagged
        if want and not has:
            ctx.change(u, "add_tags", None, [tag])
            ctx.write("tag auction", ctx.crm.add_tags, u, [tag], None)
            ctx.out.tally["tagged %s" % tag] += 1
        elif has and not want:
            ctx.change(u, "remove_tags", [tag], None)
            ctx.write("untag auction", ctx.crm.remove_tags, u, [tag])
            ctx.out.tally["untagged (date passed or moved out)"] += 1
        if want:
            hot.append((nxt, row))
        if ctx.live and (want != has):
            ledger.event(ctx.run_id, st.id, u, "auction_tag", {"add": want, "date": str(nxt) if nxt else ""},
                         now=ctx.now, tz=ctx.policy.tz)
        if crit_uuid:
            want_c = nxt is not None and (nxt - today).days <= crit_days
            has_c = u in crit_tagged
            if want_c and not has_c:
                ctx.change(u, "add_tags", None, [crit_tag])
                ctx.write("tag critical", ctx.crm.add_tags, u, [crit_tag], None)
                ctx.out.tally["tagged %s" % crit_tag] += 1
            elif has_c and not want_c:
                ctx.change(u, "remove_tags", [crit_tag], None)
                ctx.write("untag critical", ctx.crm.remove_tags, u, [crit_tag])
                ctx.out.tally["untagged %s" % crit_tag] += 1
        if after_status and nxt is None:
            past = sorted(d for d in (_as_date(v) for v in dates.values()) if d and d < today)
            cur = status_of(row)
            if past and 1 <= (today - past[-1]).days <= after_window and cur in ("", "prospecting", "exhausted"):
                ctx.change(u, "status", cur or None, after_status)
                ctx.write("after sale", ctx.crm.set_status, u, after_status)
                ctx.out.tally["sale date passed: set %s" % after_status] += 1
                if ctx.live:
                    ledger.event(ctx.run_id, st.id, u, "after_sale", {"date": str(past[-1]), "status": after_status},
                                 now=ctx.now, tz=ctx.policy.tz)
    hot.sort(key=lambda x: x[0])
    for d, row in hot:
        ctx.out.line("%s (%d days): %s" % (d.strftime("%b %d"), (d - today).days, addr(row)))
    ctx.out.acted = sum(n for k, n in ctx.out.tally.items() if k.startswith(("tagged ", "untagged", "sale date passed")))
    critical = sum(1 for d, _ in hot if (d - today).days <= crit_days) if crit_tag else 0
    ctx.out.head = "%d sale(s) inside %d days%s%s." % (
        len(hot), soon, (", %d of them inside %d" % (critical, crit_days)) if crit_tag else "",
        "" if not ctx.out.acted else ", %s %d change(s)" % ("made" if ctx.live else "would make", ctx.out.acted))


# ---------------------------------------------------------------------- reactivate
def run_reactivate(ctx: Ctx) -> None:
    st = ctx.stage
    people = resolve_callers(ctx)
    rows = stage_rows(ctx)
    ctx.out.found = len(rows)
    active = {p["uuid"] for p in people}
    by_preset: Counter = Counter(t for t, _ in rows)
    moved = 0
    if st.get("assign", True) and people:
        load = {p["uuid"]: ctx.crm.count({"assigned_to": p["uuid"], "must_not": {"any_property_status": WORKLOAD_EXCLUDE}})
                for p in people}
        for title, row in rows[: int(st.get("max_per_run", 100))]:
            u = row["uuid"]
            if ledger.events(u, "reactivate", since=ctx.now - timedelta(days=float(st.get("every_days", 30)))):
                continue
            rec = ctx.crm.record(u)
            cur = assignee_of(rec)
            if cur in active:
                continue
            # The second-chance lane is the one place a Not Interested record may be touched.
            if not ctx.passes(rec, "assign", row=row, allow={"not_interested"}):
                continue
            who = balanced_pick(people, load)
            ctx.change(u, "assign", cur, who["uuid"])
            ctx.write("reassign", ctx.crm.assign, u, who["uuid"])
            if ctx.live:
                ledger.event(ctx.run_id, st.id, u, "reactivate", {"to": who["name"], "preset": title}, now=ctx.now,
                             tz=ctx.policy.tz)
            load[who["uuid"]] += 1
            moved += 1
            ctx.out.line("%s -> %s (%s)" % (addr(row), who["name"], title))
    ctx.out.acted = moved
    ctx.out.head = "%d record(s) are back from Not Interested (%s); %s %d to a caller." % (
        len(rows), ", ".join("%s %d" % kv for kv in by_preset.most_common()) or "none",
        "handed" if ctx.live else "would hand", moved)


# ---------------------------------------------------------------------- handoff
def run_handoff(ctx: Ctx) -> None:
    """Records in the stage's presets go to a teammate's workflow, a few a day, each record once.
    The workflow carries its own caps (a deep prospect spends inside AGENTS_RUN_CAP_USD and runs dry
    on a dry box), so this stage only decides who is next."""
    st = ctx.stage
    to, agent = str(st.get("workflow")), str(st.get("agent"))
    per_day = int(st.get("per_day", 3))
    rows = stage_rows(ctx, extra=required_tags(ctx))
    ctx.out.found = len(rows)
    d = ledger.day(ctx.now, ctx.policy.tz)
    today_n = sum(1 for e in ledger.events(action="handoff", stage=st.id) if e["day"] == d)
    enqueue = ctx.enqueue or store.enqueue
    sent = 0
    for _, row in rows:
        u = row["uuid"]
        if status_of(row) in DEAD_STATUSES or ledger.handed_off(u, to):
            continue
        if today_n + sent >= per_day:
            ctx.out.tally["waiting for a later day (%d a day)" % per_day] += 1
            continue
        if not ctx.passes(ctx.crm.record(u), "research", row=row):
            continue
        tid = ctx.write("handoff", enqueue, agent, to, u, addr(row), source="autopilot:%s" % st.id)
        if ctx.live:
            ledger.event(ctx.run_id, st.id, u, "handoff", {"to": to, "task": tid[0] if tid else 0}, now=ctx.now,
                         tz=ctx.policy.tz)
        sent += 1
        ctx.out.line("%s -> %s" % (addr(row), to.replace("_", " ")))
    ctx.out.acted = sent
    ctx.out.head = "%s %d record(s) to %s; %d in the presets." % ("Handed" if ctx.live else "Would hand", sent,
                                                                 to.replace("_", " "), len(rows))


# ---------------------------------------------------------------------- digest
def run_digest(ctx: Ctx) -> None:
    st = ctx.stage
    counts = {}
    for title in st.presets:
        try:
            must, _ = windows.resolve(ctx.crm.preset_must(title), ctx.now)
            counts[title] = ctx.crm.count(must)
        except KeyError as exc:
            ctx.out.problem(str(exc))
    prev = {}
    raw = store.kv_get("autopilot:digest:counts")
    if raw:
        try:
            prev = json.loads(raw)
        except ValueError:
            prev = {}
    if st.get("remember", True):
        store.kv_set("autopilot:digest:counts", json.dumps(counts))
    since = ctx.now - timedelta(hours=24)
    acts = ledger.counts_since(since)
    spent = sum(v[1] for v in acts.values())
    by = Counter()
    for (stage, action), (n, _) in acts.items():
        by[action] += n
    staged = ledger.batches(status="staged")
    ctx.out.found = sum(counts.values())
    parts = []
    for k, label in (("assign", "assigned"), ("trace", "retraced"), ("handoff", "to Diggs"),
                     ("auction_tag", "auction tags"), ("mail", "mailed"), ("score", "scored")):
        if by.get(k):
            parts.append("%d %s" % (by[k], label))
    ctx.out.head = "Last 24h: %s, $%.2f." % (", ".join(parts) or "nothing moved", spent)
    for title, n in counts.items():
        delta = n - prev[title] if title in prev else None
        ctx.out.line("%s: %d%s" % (title, n, "" if delta in (None, 0) else " (%+d)" % delta))
    if staged:
        ctx.out.line("Waiting on a person: %s" % ", ".join("batch %d (%d %s, $%.2f)" % (
            b["id"], b["count"], "counter corrections" if b["kind"] == "attempts" else "pieces", b["usd"]) for b in staged[:4]))
    try:
        from . import budget, capture
        for ln in capture.recent_lines(1)[:4]:
            ctx.out.line(ln)
        use = budget.usage()
        if use["minute_waits_today"] or use["hour_refusals_today"]:
            ctx.out.line("Request budget today: waited for a free minute %d time(s), refused for the hour %d time(s)" % (
                use["minute_waits_today"], use["hour_refusals_today"]))
    except Exception:  # noqa: BLE001  (the digest must post even when an extra line cannot be built)
        log.exception("digest extras failed")
    ctx.out.acted = 0
