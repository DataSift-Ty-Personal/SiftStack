"""Autopilot selftest: the clock, the playbook, the window converter and every stage, in plan and in
live mode, against a fake DataSift, fake skip trace sources, a fake scorer and the CSV mail vendor.
Zero network. The database is the selftest's throwaway file.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 11, 15, 10, tzinfo=ET).astimezone(timezone.utc)   # a Wednesday afternoon

ADRIANA = {"uuid": "u-adriana", "first_name": "Adriana", "is_active": True}
TINAA = {"uuid": "u-tinaa", "first_name": "Tinaa", "is_active": False}
MARCUS = {"uuid": "u-marcus", "first_name": "Marcus", "is_active": True}


def _rec(u, street, **kw):
    r = {"uuid": u, "address": {"street": street, "city": "Knoxville", "state": "TN", "postal_code": "37917"},
         "owner": {"uuid": "o-" + u, "first_name": "Pat", "last_name": "Example", "phones": [],
                   "address": {"street": "9 Mail Rd", "city": "Knoxville", "state": "TN", "postal_code": "37917"}},
         "tags": ["FTM", "Courthouse Data", "Foreclosure"], "status": None, "assigned_to": None,
         "predictivecall_attempts": 0, "directmail_attempts": 0, "skiptraced": True, "estimate_value": 150000}
    for k, v in kw.items():
        if k in ("first", "last", "company", "deceased", "phones", "owner_dnm"):
            key = {"first": "first_name", "last": "last_name", "owner_dnm": "do_not_mail_ever"}.get(k, k)
            r["owner"][key] = v
        else:
            r[k] = v
    return r


def _bad(p):
    return (p.get("status") or "UNKNOWN") in ("WRONG", "DEAD", "DNC")


PRESETS = {
    "FTM - 00 Needs Skipped": lambda r: "FTM" in r["tags"] and not r["owner"]["phones"] and not r["skiptraced"],
    "FTM - 01 Skipped No Numbers": lambda r: "FTM" in r["tags"] and not r["owner"]["phones"] and r["skiptraced"],
    "FTM - 02 Ready to Call": lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 0,
    "FTM - Mail 01 Handwritten Letter": lambda r: "FTM" in r["tags"] and r["directmail_attempts"] == 0,
    "FTM - Mail 02 Family Postcard": lambda r: "FTM" in r["tags"] and r["directmail_attempts"] == 1,
    "03 Exhausted Call -> DP": lambda r: r["predictivecall_attempts"] >= 3 and r["owner"]["phones"]
                                         and all(_bad(p) for p in r["owner"]["phones"]),
    "Not Interested - FTM Foreclosure (30d)": lambda r: r.get("status") == "not_interested",
}


class FakeCRM:
    def __init__(self, records, users=(ADRIANA, TINAA)):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in records}
        self.users_ = list(users)
        self.writes = []
        self.msgs = []
        self.native = 0
        self.stats = {"value_spent": 0.0, "saved_by_unlimited_skiptracing": 50.0}
        self.custom = {}
        self.calls = 0

    def presets(self):
        return {t: {"uuid": t, "folder": "", "must": {"_preset": t}} for t in PRESETS}

    def preset_must(self, title):
        if title not in PRESETS:
            raise KeyError("no preset titled %r in this account" % title)
        return {"_preset": title, "last_direct_mailed": ["36-months", "month"]} if "Mail 02" in title else {"_preset": title}

    def _match(self, r, must):
        p = must.get("_preset")
        if p and not PRESETS[p](r):
            return False
        if "assigned_to" in must:
            if must["assigned_to"] is None and r.get("assigned_to"):
                return False
            if must["assigned_to"] and r.get("assigned_to") != must["assigned_to"]:
                return False
        if "all_tags" in must:
            have = {t.lower() for t in r["tags"]}
            if not {x.replace("tag-", "") for x in must["all_tags"]} <= have:
                return False
        if "owners" in must and r["owner"]["uuid"] not in must["owners"]:
            return False
        if "any_tags" in must and not {x.replace("tag-", "") for x in must["any_tags"]} & {t.lower() for t in r["tags"]}:
            return False
        if "any_lists" in must and not {x.replace("list-", "") for x in must["any_lists"]} & {t.lower() for t in r.get("lists", [])}:
            return False
        if "any_property_status" in must and r.get("status") not in must["any_property_status"]:
            return False
        mn = must.get("must_not") or {}
        if r.get("status") in (mn.get("any_property_status") or []):
            return False
        return True

    def _row(self, r):
        return {"uuid": r["uuid"], "address": r["address"], "status": r.get("status"),
                "owner": {"uuid": r["owner"]["uuid"]}, "phone": r.get("row_phone"),
                "do_not_mail_ever": r.get("do_not_mail_ever", False), "owner_do_not_mail_ever": False}

    def search(self, must, limit=250, offset=0):
        rows = [self._row(r) for r in self.recs.values() if self._match(r, must)]
        return len(rows), rows[offset:offset + limit]

    def count(self, must):
        return self.search(must)[0]

    def rows(self, must, cap=10000):
        return self.search(must, limit=cap)[1]

    def record(self, u):
        return copy.deepcopy(self.recs.get(u) or {})

    def users(self):
        return self.users_

    def tag_uuid(self, title):
        return "tag-" + title.lower()

    def skiptrace_stats(self):
        return self.stats

    def skiptrace_all(self):
        self.native += 1
        return {"number_of_records": 1}

    scope_ok = True
    counter_broken = False
    quote_off = False

    def skiptrace_scoped(self, uuids, dry_run, tags=None, force_refresh=False):
        if dry_run:
            return {"number_of_records": len(uuids) if self.scope_ok else 66242, "cost": 0}
        self.native += 1
        self.writes.append(("skiptrace", tuple(uuids)))
        return {}

    fields = {"last mailed": "f-last-mailed"}

    def custom_field_uuid(self, label):
        return self.fields.get(label.strip().lower(), "")

    def set_custom_field(self, u, field_uuid, value):
        self.writes.append(("field", u, field_uuid, value))
        self.recs[u].setdefault("cf", {})[field_uuid] = value

    def directmail_attempts(self, u, total):
        self.writes.append(("dm_attempts", u, total))
        if not self.counter_broken:
            self.recs[u]["directmail_attempts"] = total

    def set_last_mailed(self, u, day):
        self.writes.append(("last_mailed", u, day))
        if not self.counter_broken:
            self.recs[u]["last_direct_mailed"] = day

    def mail_campaign(self, uuids, title, touches, estimate, campaign_type="single-touch"):
        if estimate:
            return {"number_of_records": len(uuids) + (5 if self.quote_off else 0), "cost": round(0.77 * len(uuids), 2)}
        self.writes.append(("campaign", tuple(uuids), touches[0]["template"]))
        for u in uuids:
            self.recs[u]["directmail_attempts"] = self.recs[u].get("directmail_attempts", 0) + 1
        return {"uuid": "camp-1"}

    def assign(self, u, who):
        self.writes.append(("assign", u, who))
        self.recs[u]["assigned_to"] = who

    def add_tags(self, u, add, rec=None):
        self.writes.append(("add_tags", u, tuple(add)))
        for t in add:
            if t not in self.recs[u]["tags"]:
                self.recs[u]["tags"].append(t)

    def remove_tags(self, u, drop):
        self.writes.append(("remove_tags", u, tuple(drop)))
        self.recs[u]["tags"] = [t for t in self.recs[u]["tags"] if t not in drop]

    def set_status(self, u, s):
        self.writes.append(("status", u, s))
        self.recs[u]["status"] = s

    # ---- phase 0: the documented counters, an owner's records, the logs
    def call_attempts(self, u, total):
        self.writes.append(("call_attempts", u, total))
        if not self.counter_broken:
            self.recs[u]["predictivecall_attempts"] = total

    def sms_attempts(self, u, total):
        self.writes.append(("sms_attempts", u, total))
        self.recs[u]["sms_attempts"] = total

    def skiptrace_attempts(self, owner_uuid, total):
        self.writes.append(("skiptrace_attempts", owner_uuid, total))
        for r in self.recs.values():
            if r["owner"]["uuid"] == owner_uuid:
                r["owner"]["skiptrace_attempts"] = total

    def owner(self, owner_uuid):
        for r in self.recs.values():
            if r["owner"]["uuid"] == owner_uuid:
                return copy.deepcopy(r["owner"])
        return {}

    def owner_records(self, owner_uuid, cap=250):
        return [self._row(r) for r in self.recs.values() if r["owner"]["uuid"] == owner_uuid]

    def custom_fields(self, u):
        return dict(self.custom.get(u, {}))

    def list_uuid(self, title):
        return "list-" + title.lower()

    log_rows = ()        # the account log, any order: dicts shaped like the real route's rows
    rec_logs = {}        # record uuid -> that record's own log rows

    def logs(self, must, limit=100, cursor="", include_owner=False):
        types = set(must.get("any_event_type") or [])
        lo, hi = (list(must.get("timestamp") or [None, None]) + [None, None])[:2]
        out = []
        for e in self.log_rows:
            ts = e["timestamp"].replace(" ", "T") + "Z"
            if types and e["event_type"] not in types:
                continue
            if (lo and ts < lo) or (hi and ts > hi):
                continue
            out.append(e)
        self.log_calls = getattr(self, "log_calls", 0) + 1
        return {"results": out, "next_cursor": None}

    def record_logs(self, u, event_types=None, limit=250):
        return [e for e in self.rec_logs.get(u, []) if not event_types or e["event_type"] in event_types]

    def upsert_phones(self, owner_uuid, phones):
        self.writes.append(("upsert", owner_uuid, len(phones)))
        rec = next(r for r in self.recs.values() if r["owner"]["uuid"] == owner_uuid)
        have = {p["number"]: p for p in rec["owner"]["phones"]}
        for p in phones:
            have[p["number"]] = dict(p)
        rec["owner"]["phones"] = list(have.values())

    def message(self, u, text, pin=False):
        self.msgs.append((u, text))
        self.writes.append(("message", u))
        return "m1"

    def call(self, path, method="GET", body=None, headers=None):
        m = re.search(r"/property/([^/]+)/custom-field/", path)
        if m:
            return {"results": [{"custom_field": {"label": k}, "value": v} for k, v in self.custom.get(m.group(1), {}).items()]}
        raise AssertionError("unexpected call %s" % path)


class FakeSource:
    def __init__(self, name, unit, per, hits, ok=True):
        self.name, self.unit_usd, self.per, self.hits, self.ok = name, unit, per, hits, ok
        self.calls = []

    def configured(self):
        return self.ok

    def trace(self, subjects):
        self.calls.append([s.record_uuid for s in subjects])
        found = {s.record_uuid: [{"number": n, "type": "MOBILE"} for n in self.hits[s.record_uuid]]
                 for s in subjects if self.hits.get(s.record_uuid)}
        billed = self.unit_usd * (len(subjects) if self.per == "row" else len(found))
        return found, round(billed, 4)


class FakeScorer:
    unit_usd = 0.015

    def __init__(self):
        self.calls = []

    def configured(self):
        return True

    def score(self, nums):
        self.calls += list(nums)
        out = {}
        for n in nums:
            if n.endswith("1"):
                out[n] = {"score": 92, "tier": "Dial First", "line_type": "Mobile", "litigator": False}
            elif n.endswith("9"):
                out[n] = {"score": 4, "tier": "Drop", "line_type": "Landline", "litigator": False}
            elif n.endswith("7"):
                out[n] = {"score": 60, "tier": "DNC", "line_type": "Mobile", "litigator": True}
            else:
                out[n] = {"score": 55, "tier": "Dial Third", "line_type": "Mobile", "litigator": False}
        return out


PLAYBOOK = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0},
    "people": {"callers": ["Adriana", "Tinaa"]},
    "stages": {
        "a_assign": {"kind": "assign", "owner": "analysis", "mode": "live", "schedule": "every 30 min 08:00-18:00 Mon-Fri",
                     "presets": ["FTM - 01 Skipped No Numbers", "FTM - 02 Ready to Call"]},
        "a_auction": {"kind": "auction", "owner": "analysis", "mode": "live", "schedule": "07:45 daily",
                      "scope_tags": ["FTM", "Courthouse Data"], "soon_days": 21},
        "r_native": {"kind": "native_trace", "owner": "research", "mode": "live", "schedule": "every 60 min",
                     "presets": ["FTM - 00 Needs Skipped"], "min_age_hours": 2, "every_hours": 12},
        "r_retrace": {"kind": "retrace", "owner": "research", "mode": "live", "schedule": "11:00 Mon-Fri",
                      "presets": ["FTM - 01 Skipped No Numbers"], "sources": ["tracerfy", "enformion"],
                      "deceased": "deep_prospect", "after_sources": "mail_only", "daily_usd": 10},
        "r_score": {"kind": "score", "owner": "research", "mode": "live", "schedule": "08:30 Mon-Fri",
                    "presets": ["FTM - 02 Ready to Call"]},
        "o_mail": {"kind": "mail", "owner": "ops", "mode": "live", "schedule": "10:00 Mon-Fri", "vendor": "csv",
                   "send": "staged", "daily_pieces": 10, "wait_days": 30, "offer_pct": 0,
                   "pieces": [{"preset": "FTM - Mail 01 Handwritten Letter", "piece": "handwritten_letter",
                               "copy": "Hi {first_name}, I would love to talk about {street}. {sender_name}"},
                              {"preset": "FTM - Mail 02 Family Postcard", "piece": "soft_offer_check"}]},
        "o_digest": {"kind": "digest", "owner": "ops", "mode": "live", "schedule": "08:00 Mon-Fri",
                     "presets": ["FTM - 02 Ready to Call"]},
        "r_handoff": {"kind": "handoff", "owner": "research", "mode": "live", "schedule": "13:00 Mon-Fri",
                      "presets": ["03 Exhausted Call -> DP"], "workflow": "deep_prospect", "agent": "research",
                      "per_day": 1},
    },
}
EMPLOYEES = {"analysis", "research", "sales", "ops"}


def run(check, tmp: Path) -> None:
    from . import scheduler, store, team
    from .autopilot import ledger, mail, policy, stages, sweep, windows
    # Own the caps: earlier sections fill the brain's daily spend on purpose (the cap tests), and the
    # brain cap is checked here on its own further down.
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    store.init()

    print("  clock")
    s = scheduler.parse("every 30 min 08:00-18:00 Mon-Fri")
    check(s.every_min == 30 and s.window == (480, 1080) and s.days == frozenset(range(5)), "every-N-min schedule parses")
    check(scheduler.parse("Mon 07:00 America/New_York").days == frozenset({0}), "a day before the time parses")
    check(scheduler.parse("1st 07:00").monthday == 1, "a monthly schedule parses")
    bad = 0
    for txt in ("every 0 min", "25:00", "08:00-18:00", "every 30 min 07:15", "07:15 Funday", ""):
        try:
            scheduler.parse(txt)
        except scheduler.ScheduleError:
            bad += 1
    check(bad == 6, "six malformed schedules are refused, none guessed")
    last = scheduler.last_slot(s, T0)
    check(last and last.astimezone(ET).strftime("%a %H:%M") == "Wed 15:00", "the latest slot before 15:10 ET is 15:00 ET")
    sat = datetime(2026, 11, 14, 12, 0, tzinfo=ET).astimezone(timezone.utc)
    check(scheduler.last_slot(s, sat).astimezone(ET).strftime("%a %H:%M") == "Fri 18:00", "a weekday schedule skips the weekend")

    class _WF:
        def __init__(self, name):
            self.name, self.owner, self.live = name, "ops", True

        def trigger_values(self, kind):
            return ["every 30 min 08:00-18:00 Mon-Fri"] if kind == "schedule" else []

    class _Roster:
        workflows = {"clock_test": _WF("clock_test")}

    queued = []

    def fake_enqueue(owner, wf, **kw):
        queued.append(wf)
        return len(queued), True

    f1 = scheduler.tick(_Roster, now=T0, enqueue=fake_enqueue)
    f2 = scheduler.tick(_Roster, now=T0 + timedelta(minutes=5), enqueue=fake_enqueue)
    check([x["action"] for x in f1] == ["queued"] and not f2, "a slot fires once and a second tick in the same slot does nothing")
    f3 = scheduler.tick(_Roster, now=sat, enqueue=fake_enqueue)   # Friday 18:00 was the last slot
    check([x["action"] for x in f3] == ["skipped_late"], "a slot missed by more than the grace window is not run late")
    tid, _ = store.enqueue("ops", "clock_test", subject="busy")
    mon = datetime(2026, 11, 16, 8, 5, tzinfo=ET).astimezone(timezone.utc)
    f4 = scheduler.tick(_Roster, now=mon, enqueue=fake_enqueue)
    check([x["action"] for x in f4] == ["skipped_busy"], "a slot is skipped while the last run is still queued")
    store.finish(tid, "done")

    print("  playbook")
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    check(len(pol.stages) == 8 and pol.stage("r_retrace").spend == "paid" and pol.stage("o_mail").spend == "outreach",
          "the playbook parses and every stage knows its spend class")
    broken = copy.deepcopy(PLAYBOOK)
    broken["stages"]["x"] = {"kind": "warp", "owner": "nobody"}
    broken["stages"]["r_retrace"]["sources"] = ["tracerfy", "psychic"]
    broken["stages"]["a_assign"]["schedule"] = "every 0 min"
    try:
        policy.parse(broken, EMPLOYEES)
        check(False, "a broken playbook is refused")
    except policy.PolicyError as exc:
        msg = str(exc)
        check(all(w in msg for w in ("warp", "nobody", "psychic", "every 0 min")),
              "every problem in a broken playbook is reported at once")
    wf = policy.as_workflow("r_retrace", PLAYBOOK["stages"]["r_retrace"])
    check(wf["status"] == "live" and wf["spend"] == "paid" and "schedule:11:00 Mon-Fri" in wf["triggers"],
          "a stage becomes a live roster workflow with its schedule")
    pb = tmp / "autopilot.yaml"
    import yaml
    pb.write_text(yaml.safe_dump(PLAYBOOK), encoding="utf-8")
    data, notes = team.with_autopilot({"workflows": {"r_score": {"owner": "research"}}}, pb)
    check("a_assign" in data["workflows"] and any("r_score" in n for n in notes),
          "the roster picks up the playbook and a name clash keeps the roster entry, out loud")

    print("  windows")
    m, notes = windows.resolve({"last_direct_mailed": ["36-months", "month"],
                                "last_updated_date": [{"field": "status", "options": ["36-months", "quarter"]}],
                                "phone": 1}, date(2026, 11, 11))
    check(m["last_direct_mailed"] == ["2023-10-11", "2026-10-11"],
          "mailed more than a month ago: the start counts back from the one-month point, as the app does")
    check(m["last_updated_date"][0]["options"] == ["2023-08-11", "2026-08-11"], "a status window converts too")
    m2, n2 = windows.resolve({"last_direct_mailed": ["fortnight-ish", "month"]}, date(2026, 11, 11))
    check("last_direct_mailed" not in m2 and any("dropped" in n for n in n2), "an unreadable window is dropped and said, not guessed")
    m3, _ = windows.resolve({"last_obituary_date": ["24-months", "quarter"], "any_property_status": ["sold", "closed"],
                             "predictivecall_attempts": [3, None], "investor_score": [95, 100]}, date(2026, 11, 11))
    check(m3["last_obituary_date"] == ["2024-08-11", "2026-08-11"] and m3["any_property_status"] == ["sold", "closed"]
          and m3["predictivecall_attempts"] == [3, None] and m3["investor_score"] == [95, 100],
          "any window converts by its value (the obituary preset's date) while statuses and number ranges stay as they are")

    print("  assign")
    config.MODE = "live"
    R1 = _rec("r1", "101 Elm St")
    R2 = _rec("r2", "202 Oak St", phones=[{"number": "8655550101", "status": "UNKNOWN", "tags": []}])
    crm = FakeCRM([R1, R2])
    s1 = sweep.run("a_assign", crm=crm, now=T0, pol=pol)
    check(s1.acted == 0 and not crm.writes and s1.tally.get("backlog left as is (set backlog: true to hand these out)") == 2,
          "the first run draws the line: records already waiting are left for the owner")
    check(any("Tinaa is inactive" in p for p in s1.problems), "an inactive caller is named, not silently skipped")
    crm.recs["r3"] = _rec("r3", "303 Pine St")
    s2 = sweep.run("a_assign", crm=crm, now=T0 + timedelta(minutes=30), pol=pol)
    check(s2.acted == 1 and crm.recs["r3"]["assigned_to"] == "u-adriana" and crm.recs["r1"]["assigned_to"] is None,
          "a record that lands after the line is assigned to the active caller; the backlog is untouched")
    pol_b = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_b.stage("a_assign").params["backlog"] = True
    crm_b = FakeCRM([R1, R2, _rec("r3", "303 Pine St")], users=(ADRIANA, MARCUS))
    pol_b.callers = ["Adriana", "Marcus"]
    store.kv_set("autopilot:baseline:a_assign", "")
    sweep.run("a_assign", crm=crm_b, now=T0, pol=pol_b)
    who = [crm_b.recs[u]["assigned_to"] for u in ("r1", "r2", "r3")]
    check(sorted(who) == ["u-adriana", "u-adriana", "u-marcus"] or sorted(who) == ["u-adriana", "u-marcus", "u-marcus"],
          "backlog: true hands everything out, balanced across the active callers")
    crm_p = FakeCRM([_rec("r9", "909 Ash St")])
    store.kv_set("autopilot:baseline:a_assign", "2000-01-01T00:00:00+00:00")
    sp = sweep.run("a_assign", crm=crm_p, now=T0, pol=pol, plan=True)
    check(sp.acted == 1 and not crm_p.writes and not sp.live, "plan mode works out the assignment and writes nothing")

    print("  native trace")
    batch = [_rec("n%02d" % i, "%d Trace Ln" % i, skiptraced=False) for i in range(20)]
    crm_n = FakeCRM(batch)
    a = sweep.run("r_native", crm=crm_n, now=T0, pol=pol)
    b = sweep.run("r_native", crm=crm_n, now=T0 + timedelta(hours=3), pol=pol)
    c = sweep.run("r_native", crm=crm_n, now=T0 + timedelta(hours=4), pol=pol)
    check(a.acted == 0 and b.acted == 20 and crm_n.native == 1 and c.acted == 0,
          "DataSift's trace waits for its own chance, then runs once, then not again inside the window")
    check(("skiptrace", tuple(r["uuid"] for r in batch)) in crm_n.writes, "the trace is scoped to exactly the waiting records")
    few = FakeCRM([_rec("w%d" % i, "%d Few Ln" % i, skiptraced=False) for i in range(3)])
    sweep.run("r_native", crm=few, now=T0, pol=pol)
    fw = sweep.run("r_native", crm=few, now=T0 + timedelta(hours=3), pol=pol)
    check(few.native == 0 and "20 or more" in fw.head,
          "under 20 records no trace job is created (the bulk policy counts small repeated jobs as abuse)")
    crm_x = FakeCRM([_rec("x%02d" % i, "%d Scope Ln" % i, skiptraced=False) for i in range(20)])
    crm_x.scope_ok = False
    sweep.run("r_native", crm=crm_x, now=T0, pol=pol)
    x = sweep.run("r_native", crm=crm_x, now=T0 + timedelta(hours=3), pol=pol)
    check(crm_x.native == 0 and any("dry run covers" in p for p in x.problems),
          "if DataSift's dry run does not cover exactly the records asked for, nothing is traced")
    pol_w = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_w.stage("r_native").params["scoped"] = False
    crm_m = FakeCRM([_rec("n2", "2 Trace Ln", skiptraced=False)])
    crm_m.stats = {"value_spent": 12.5, "saved_by_unlimited_skiptracing": 0}
    store.kv_set("autopilot:native_trace:last", "")
    sweep.run("r_native", crm=crm_m, now=T0, pol=pol_w)
    m3 = sweep.run("r_native", crm=crm_m, now=T0 + timedelta(hours=3), pol=pol_w)
    check(crm_m.native == 0 and any("metered" in p for p in m3.problems),
          "on a metered plan the account-wide trace is refused, because it would bill every record")

    print("  retrace")
    P1 = _rec("p1", "11 Miss Rd")
    P2 = _rec("p2", "12 Dead End Rd", deceased=True)
    P3 = _rec("p3", "13 Shell Co Rd", first="", last="", company="Acme Holdings LLC")
    P4 = _rec("p4", "14 Drop Ave")
    crm_r = FakeCRM([P1, P2, P3, P4])
    tracerfy = FakeSource("tracerfy", 0.02, "row", {"p4": ["8655550109"]})
    enformion = FakeSource("enformion", 0.10, "match", {"p1": ["8655550111", "8655550109"], "p4": ["8655550121"]})
    scorer = FakeScorer()
    handoffs = []

    def enq(agent, wf, u, subject, **kw):
        handoffs.append((agent, wf, u))
        return (len(handoffs), True)

    plan_r = sweep.run("r_retrace", crm=crm_r, now=T0, pol=pol, plan=True,
                       sources={"tracerfy": tracerfy, "enformion": enformion}, scorer=scorer, enqueue=enq)
    check(not tracerfy.calls and not enformion.calls and not crm_r.writes and plan_r.would_usd > 0 and not handoffs,
          "a planned retrace prices the waterfall and calls no vendor, hands off nothing")
    live_r = sweep.run("r_retrace", crm=crm_r, now=T0, pol=pol, sources={"tracerfy": tracerfy, "enformion": enformion},
                       scorer=scorer, enqueue=enq)
    check(tracerfy.calls and set(tracerfy.calls[0]) == {"p1", "p4"} and set(enformion.calls[0]) == {"p1", "p4"},
          "the waterfall tries the first source, then sends the misses to the next one")
    nums = {p["number"]: p for p in crm_r.recs["p1"]["owner"]["phones"]}
    check(set(nums) == {"8655550111", "8655550109"} and "Dial First" in nums["8655550111"]["tags"]
          and "Enformion" in nums["8655550111"]["tags"], "found numbers land once, tiered and tagged with their source")
    p4 = {p["number"]: p for p in crm_r.recs["p4"]["owner"]["phones"]}
    check("8655550121" in p4 and "Drop" in p4["8655550109"]["tags"],
          "a Drop-only find is written but is not a hit: the record kept going and found a callable number")
    check(handoffs == [("research", "deep_prospect", "p2")], "a deceased owner goes to Diggs, never a blind trace")
    check("Mail Only" in crm_r.recs["p3"]["tags"], "an entity owner with no person to trace moves to mail only")
    check(ledger.tried_sources("p1") == {"tracerfy", "enformion"} and ledger.spent(record_uuid="p1") > 0,
          "the ledger knows which sources each record has had and what it cost")
    again = sweep.run("r_retrace", crm=crm_r, now=T0 + timedelta(days=1), pol=pol,
                      sources={"tracerfy": tracerfy, "enformion": enformion}, scorer=scorer, enqueue=enq)
    check(len(handoffs) == 1 and len(tracerfy.calls) == 1, "a second day pays for nothing already done and hands off nobody twice")
    scored_before = len(scorer.calls)
    stages.score_numbers(stages.Ctx(policy=pol, stage=pol.stage("r_score"), crm=crm_r, run_id="x", now=T0, live=True,
                                    out=stages.Summary("r_score", "score", "research", "live", True), scorer=scorer),
                         ["8655550111"])
    check(len(scorer.calls) == scored_before, "a number scored once is never bought again")
    crm_cap = FakeCRM([_rec("c1", "21 Cap St")])
    pol_c = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_c.stage("r_retrace").params["daily_usd"] = 0.0
    capped = sweep.run("r_retrace", crm=crm_cap, now=T0, pol=pol_c, sources={"tracerfy": FakeSource("tracerfy", 0.02, "row", {}),
                       "enformion": FakeSource("enformion", 0.10, "match", {})}, scorer=scorer, enqueue=enq)
    check(any("not traced today" in k for k in capped.tally) and "Mail Only" not in crm_cap.recs["c1"]["tags"],
          "at the day cap a record waits for tomorrow; it is not written off as a miss")
    config.DAILY_CAP_USD = store.spent(day=datetime.now(timezone.utc).isoformat()[:10])
    brain = sweep.run("r_retrace", crm=FakeCRM([_rec("c2", "22 Brain St")]), now=T0, pol=pol,
                      sources={"tracerfy": FakeSource("tracerfy", 0.02, "row", {}),
                               "enformion": FakeSource("enformion", 0.10, "match", {})}, scorer=scorer, enqueue=enq)
    check(any("brain cap" in k for k in brain.tally), "the brain's own daily cap stops autopilot spend too")
    config.DAILY_CAP_USD = 100000.0

    from .autopilot.sources import SmartSkip, Subject
    sk = SmartSkip()
    subs = [Subject("ss1", "o1", "person", first="Pat", last="Example", street="11 Miss Rd"),
            Subject("ss2", "o2", "person", first="Lee", last="Sample", street="22 Other Rd")]
    got = sk.match(subs, [{"first": "Pat", "last": "Example", "property_address": "11 Miss Road", "has_results": True,
                           "subject_phones": [{"number": "(865) 555-0111", "type": "Wireless"}],
                           "relatives": [{"phones": [{"number": "8655550999"}]}]},
                          {"first": "Lee", "last": "Sample", "property_address": "22 Other Rd", "has_results": False,
                           "subject_phones": [], "relatives": []}])
    check(got == {"ss1": [{"number": "8655550111", "type": "MOBILE"}]} and sk.costs == {"ss1": 0.15},
          "SmartSkip results land on the right record; a living owner gets the subject's numbers, never a relative's; only hits are billed")

    print("  score")
    S1 = _rec("s1", "31 Score St", phones=[{"number": "8655550131", "status": "UNKNOWN", "tags": []},
                                           {"number": "8655550132", "status": "WRONG", "tags": []},
                                           {"number": "8655550137", "status": "UNKNOWN", "tags": ["Dial First"]}])
    crm_s = FakeCRM([S1])
    sc = sweep.run("r_score", crm=crm_s, now=T0, pol=pol, scorer=FakeScorer())
    ph = {p["number"]: p for p in crm_s.recs["s1"]["owner"]["phones"]}
    tiers = [t for t in ph["8655550131"]["tags"] if t in stages.TIER_NAMES]
    check(sc.acted == 1 and tiers == ["Dial First"], "an unscored number gets exactly one tier")
    check(ph["8655550132"]["tags"] == [] and ph["8655550137"]["tags"] == ["Dial First"],
          "a wrong number and an already-tiered number are left alone")

    print("  auction")
    A1 = _rec("a1", "41 Sale Rd")
    A2 = _rec("a2", "42 Past Rd", tags=["FTM", "Courthouse Data", "Auction Soon"])
    A3 = _rec("a3", "43 Later Rd")
    A4 = _rec("a4", "44 Bulk Rd", tags=["FTM", "Courthouse Data"], foreclosure_date="2026-11-20")
    crm_a = FakeCRM([A1, A2, A3, A4])
    crm_a.custom = {"a1": {"Foreclosure Date": "2026-11-21"}, "a2": {"Foreclosure Date": "2026-10-01"},
                    "a3": {"Foreclosure Date": "2027-02-01"}}
    au = sweep.run("a_auction", crm=crm_a, now=T0, pol=pol)
    check("Auction Soon" in crm_a.recs["a1"]["tags"] and "Auction Soon" not in crm_a.recs["a2"]["tags"]
          and "Auction Soon" not in crm_a.recs["a3"]["tags"], "inside the window gets the tag, a passed sale loses it, a far one waits")
    check("Auction Soon" not in crm_a.recs["a4"]["tags"], "the built-in foreclosure_date (a bulk artifact) is not trusted")
    check(au.lines and au.lines[0].startswith("Nov 21"), "the hot list reads by date")

    print("  mail")
    M1 = _rec("m1", "51 Letter Ln", first="E A", last="Henry")
    M2 = _rec("m2", "52 Nope Ln", owner_dnm=True)
    M3 = _rec("m3", "53 Check Ln", directmail_attempts=1)
    M4 = _rec("m4", "54 Gap Ln")
    M4["owner"]["address"] = {"street": "", "city": "", "state": "", "postal_code": ""}
    crm_m2 = FakeCRM([M1, M2, M3, M4])
    vend = mail.Vendor()
    pm = sweep.run("o_mail", crm=crm_m2, now=T0, pol=pol, mailer=vend, plan=True)
    check(pm.acted == 1 and not ledger.batches(), "a planned mail run prices the pieces and stages nothing")
    lm = sweep.run("o_mail", crm=crm_m2, now=T0, pol=pol, mailer=vend)
    b = ledger.batch(lm.batch) if lm.batch else None
    check(b and len(b["items"]) == 1 and b["items"][0]["record_uuid"] == "m1", "live staging builds one batch of what is due")
    check(b and b["items"][0]["first_name"] == "", "an initials-only owner gets no first name on the letter")
    check(any("do not mail" in k for k in lm.tally) and any("mailing address" in k for k in lm.tally)
          and any("offer_pct" in k for k in lm.tally), "do-not-mail, no address and a soft offer with no offer rule are all skipped, with the reason")
    check(Path(tmp / "mail" / ("batch_%d.csv" % lm.batch)).is_file(), "the batch is exported for the mail house")
    lm2 = sweep.run("o_mail", crm=crm_m2, now=T0 + timedelta(hours=1), pol=pol, mailer=vend)
    check(lm2.batch == 0, "a record already in a staged batch is not staged twice")
    try:
        mail.release(lm.batch, crm=crm_m2, pol=pol, commit=True)
        check(False, "a CSV batch is not marked mailed before it was uploaded")
    except SystemExit:
        check(True, "a CSV batch is not marked mailed before it was uploaded")
    rel = mail.release(lm.batch, crm=crm_m2, pol=pol, commit=True, uploaded=True, now=T0)
    check(rel["sent"] == 1 and crm_m2.msgs and ledger.events("m1", "mail"), "release logs the piece on the record and in the ledger")
    check("Mailed 2026-11" in crm_m2.recs["m1"]["tags"] and ("field", "m1", "f-last-mailed", "2026-11-11") in crm_m2.writes
          and crm_m2.recs["m1"]["directmail_attempts"] == 0,
          "an outside vendor's piece lands the documented month tag and Last Mailed date; the undocumented counter stays off")
    pol_k = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_k.stage("o_mail").params["undocumented_counter"] = True
    crm_ok = FakeCRM([_rec("k0", "70 Counter Ln")])
    ctx_ok = stages.Ctx(policy=pol_k, stage=pol_k.stage("o_mail"), crm=crm_ok, run_id="ok", now=T0, live=True,
                        out=stages.Summary("o_mail", "mail", "ops", "live", True), mailer=mail.Vendor())
    mail.deliver(ctx_ok, [mail.Piece("k0", "handwritten_letter", "x", 1, "Pat", "9 Mail Rd", "Knoxville", "TN", "37917",
                                     property_street="70 Counter Ln")], {}, "test", uploaded=True)
    check(crm_ok.recs["k0"]["directmail_attempts"] == 1 and crm_ok.recs["k0"]["last_direct_mailed"] == "2026-11-11",
          "with undocumented_counter on (our own accounts), the record's mail counter and date move too")
    crm_bk = FakeCRM([_rec("k1", "71 Broken Ln"), _rec("k2", "72 Broken Ln")])
    crm_bk.counter_broken = True
    ctx_bk = stages.Ctx(policy=pol_k, stage=pol_k.stage("o_mail"), crm=crm_bk, run_id="bk", now=T0, live=True,
                        out=stages.Summary("o_mail", "mail", "ops", "live", True), mailer=mail.Vendor())
    pcs = [mail.Piece(u, "handwritten_letter", "FTM - Mail 01 Handwritten Letter", 1, "Pat", "9 Mail Rd",
                      "Knoxville", "TN", "37917", property_street=s) for u, s in (("k1", "71 Broken Ln"), ("k2", "72 Broken Ln"))]
    mail.deliver(ctx_bk, pcs, {}, "test", uploaded=True)
    k2_writes = [w for w in crm_bk.writes if w[0] == "dm_attempts" and w[1] == "k2"]
    check(any("did not stick" in p for p in ctx_bk.out.problems) and not k2_writes,
          "a counter write that does not stick is caught on the first piece and not repeated for the rest")
    from .autopilot import mail_vendors
    sm = mail_vendors.SiftMail()
    pol_s = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_s.stage("o_mail").params["pieces"] = [{"preset": "FTM - Mail 01 Handwritten Letter", "piece": "family_postcard",
                                               "design": "tpl-1", "product_id": 13}]
    pol_s.stage("o_mail").params["min_batch"] = 1          # the mechanics; the 20-piece floor is checked below
    check(getattr(mail.vendor_for(pol.stage("o_mail")), "refused", "") == "" and
          "not in DataSift's published API" in getattr(mail.vendor_for(policy.parse({**copy.deepcopy(PLAYBOOK), "stages": {
              "m": {"kind": "mail", "owner": "ops", "schedule": "10:00", "vendor": "siftmail",
                    "pieces": [{"preset": "p", "piece": "postcard"}]}}}, EMPLOYEES).stage("m")), "refused", ""),
          "SiftMail is refused unless the stage opts in to internal routes (its campaign route is undocumented)")
    sender = {"name": "Pat", "street": "1 Return Rd", "city": "Knoxville", "state": "TN", "zip": "37917", "phone": "8655550100"}
    crm_sm = FakeCRM([_rec("q1", "81 Card St"), _rec("q2", "82 Card St")])
    ctx_sm = stages.Ctx(policy=pol_s, stage=pol_s.stage("o_mail"), crm=crm_sm, run_id="sm", now=T0, live=True,
                        out=stages.Summary("o_mail", "mail", "ops", "live", True), mailer=sm)
    q = [mail.Piece(u, "family_postcard", "FTM - Mail 01 Handwritten Letter", 1, "Pat", "9 Mail Rd", "Knoxville", "TN",
                    "37917", price=0.77) for u in ("q1", "q2")]
    sent, paid = mail.deliver(ctx_sm, q, sender, "test")
    check(sent == 2 and ("campaign", ("q1", "q2"), "tpl-1") in crm_sm.writes and abs(paid - 1.54) < 1e-6,
          "SiftMail launches one campaign for the step after a quote that covers exactly those records")
    crm_sm2 = FakeCRM([_rec("q3", "83 Card St")])
    crm_sm2.quote_off = True
    ctx_sm2 = stages.Ctx(policy=pol_s, stage=pol_s.stage("o_mail"), crm=crm_sm2, run_id="sm2", now=T0, live=True,
                         out=stages.Summary("o_mail", "mail", "ops", "live", True), mailer=sm)
    sent2, _ = mail.deliver(ctx_sm2, [mail.Piece("q3", "family_postcard", "x", 1, "Pat", "9 Mail Rd", "Knoxville", "TN",
                                                 "37917", price=0.77)], sender, "test")
    check(sent2 == 0 and not [w for w in crm_sm2.writes if w[0] == "campaign"] and ctx_sm2.out.problems,
          "a SiftMail quote that covers the wrong records launches nothing")
    _, why = sm.touch({"preset": "FTM - Mail 02"}, sender)
    check("design" in why, "a piece with no SiftMail design is refused before anything is quoted")
    pol_f = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_f.stage("o_mail").params["pieces"] = pol_s.stage("o_mail").params["pieces"]
    crm_f = FakeCRM([_rec("q8", "88 Card St"), _rec("q9", "89 Card St")])
    ctx_f = stages.Ctx(policy=pol_f, stage=pol_f.stage("o_mail"), crm=crm_f, run_id="f", now=T0, live=True,
                       out=stages.Summary("o_mail", "mail", "ops", "live", True), mailer=sm)
    sent_f, _ = mail.deliver(ctx_f, [mail.Piece(u, "family_postcard", "x", 1, "Pat", "9 Mail Rd", "Knoxville", "TN",
                                                "37917", price=0.67) for u in ("q8", "q9")], sender, "test")
    check(sent_f == 0 and any("20 or more" in p for p in ctx_f.out.problems),
          "a SiftMail step under 20 pieces waits; a campaign is a bulk job")
    _, prob = mail.render_copy("We help with foreclosure on {street}", mail.Piece("x", "letter", "p", 1, "N", "s", "c", "TN", "37917"), {})
    check(bool(prob), "copy that names the list is refused")

    print("  budgets")
    pol_o = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_o.daily_usd, pol_o.outreach_daily_usd = 0.05, 100.0
    ctx_o = stages.Ctx(policy=pol_o, stage=pol_o.stage("o_mail"), crm=FakeCRM([]), run_id="o", now=T0, live=True,
                       out=stages.Summary("o_mail", "mail", "ops", "live", True))
    ctx_d = stages.Ctx(policy=pol_o, stage=pol_o.stage("r_retrace"), crm=FakeCRM([]), run_id="d", now=T0, live=True,
                       out=stages.Summary("r_retrace", "retrace", "research", "live", True))
    check(ctx_o.budget(30.0)[0] and not ctx_d.budget(1.0)[0],
          "mail spends from its own outreach budget; it never competes with traces for the data cap")

    print("  handoff")
    dead = [{"number": "8655550141", "status": "WRONG", "tags": []}]
    crm_h = FakeCRM([_rec("h1", "61 Done Rd", predictivecall_attempts=3, phones=dead),
                     _rec("h2", "62 Done Rd", predictivecall_attempts=4, phones=dead)])
    sent = []

    def enq2(agent, wf, u, subject, **kw):
        sent.append((agent, wf, u))
        return (len(sent), True)

    h_day1 = sweep.run("r_handoff", crm=crm_h, now=T0, pol=pol, enqueue=enq2)
    h_again = sweep.run("r_handoff", crm=crm_h, now=T0 + timedelta(hours=1), pol=pol, enqueue=enq2)
    h_day2 = sweep.run("r_handoff", crm=crm_h, now=T0 + timedelta(days=1), pol=pol, enqueue=enq2)
    check(h_day1.acted == 1 and h_again.acted == 0 and h_day2.acted == 1 and len({u for _, _, u in sent}) == 2,
          "a handoff stage sends its daily quota, each record once, and the rest wait for the next day")

    print("  digest and the worker")
    dg = sweep.run("o_digest", crm=crm_r, now=T0 + timedelta(hours=2), pol=pol)
    check(dg.head.startswith("Last 24h:") and "retraced" in dg.head, "the digest reads the last 24 hours from the ledger")
    quiet = sweep.slack_text({"workflow": "r_native", "source": "schedule"},
                             {"kind": "native_trace", "acted": 0, "problems": [], "batch": 0})
    loud = sweep.slack_text({"workflow": "r_native", "source": "schedule"},
                            {"kind": "native_trace", "acted": 0, "problems": ["metered"], "batch": 0, "stage": "r_native"})
    check(quiet == "" and loud.startswith(":warning:"), "a quiet scheduled run posts nothing; a problem always posts")
    sched = {"workflow": "r_score", "source": "schedule"}
    would = {"kind": "score", "stage": "r_score", "acted": 78, "problems": [], "batch": 0, "head": "would score 78 numbers"}
    posts_was = config.PLAN_POSTS
    config.PLAN_POSTS = "digest"
    check(sweep.slack_text(sched, would, now=T0 + timedelta(days=39, hours=14)) == "",
          "by default a stage in plan mode posts nothing on its own: what it would do is read in the morning digest")
    check(sweep.slack_text(sched, dict(would, problems=["the scorer refused the key"]), now=T0 + timedelta(days=39, hours=15)) != "",
          "but a problem in a plan-mode stage is still posted the day it appears")
    check(sweep.slack_text(sched, dict(would, problems=["the scorer refused the key"]), now=T0 + timedelta(days=40, hours=15)) == ""
          and sweep.slack_text(sched, dict(would, problems=["12 records wait on the heir caller (D19)"]),
                               now=T0 + timedelta(days=40, hours=16)) != ""
          and sweep.slack_text(sched, dict(would, problems=["14 records wait on the heir caller (D19)"]),
                               now=T0 + timedelta(days=41, hours=16)) == "",
          "a standing problem is said once, not every morning, and a count inside it changing is not a new problem")
    check(sweep.slack_text({"workflow": "r_digest", "source": "schedule"},
                           {"kind": "digest", "stage": "r_digest", "acted": 0, "problems": [], "head": "Last 24h"},
                           now=T0 + timedelta(days=39, hours=8)) != "", "and the digest itself always posts")
    config.PLAN_POSTS = "off"
    check(sweep.slack_text(sched, dict(would, problems=["x"]), now=T0 + timedelta(days=39, hours=16)) == "",
          "with plan posts off, a plan-mode stage posts nothing at all")
    config.PLAN_POSTS = "daily"
    day1, day2 = T0 + timedelta(days=40, hours=14), T0 + timedelta(days=41, hours=14)
    first = sweep.slack_text(sched, would, now=day1)
    again = sweep.slack_text(sched, would, now=day1 + timedelta(minutes=30))
    check("plan: nothing written" in first and again == "",
          "a stage in plan mode says its plan once a day, not on every 30-minute run")
    worse = sweep.slack_text(sched, dict(would, problems=["the scorer refused the key"]), now=day1 + timedelta(hours=1))
    same = sweep.slack_text(sched, dict(would, problems=["the scorer refused the key"]), now=day1 + timedelta(hours=2))
    check(worse.startswith(":warning:") and same == "", "a new problem is said the day it appears, once")
    check(sweep.slack_text(sched, would, now=day2) != "", "the next business day the plan is said again")
    check(sweep.slack_text(sched, dict(would, live=True), now=day2 + timedelta(minutes=30)) != ""
          and sweep.slack_text(sched, dict(would, live=True), now=day2 + timedelta(minutes=60)) != "",
          "a live run that acted is always said")
    check(sweep.slack_text({"workflow": "r_score", "source": "slack"}, would, now=day2 + timedelta(minutes=90)) != "",
          "a plan somebody asked for in Slack is always answered")
    config.PLAN_POSTS = posts_was
    config.MODE = "dry"
    dry = sweep.run("a_assign", crm=FakeCRM([_rec("z1", "1 Dry St")]), now=T0 + timedelta(days=9), pol=pol)
    check(not dry.live, "a live stage on a dry box does not write: both switches must say live")
