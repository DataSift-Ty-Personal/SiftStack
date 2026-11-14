"""Phase 0 selftest: the suppression gate (one check per rule), the lane table, the request budget,
the shared activity log, the attempt keeper, the death screen, the owner-lane check, revocation,
the sequence watchdog, the touch audit and undo. Zero network: the same fake DataSift as the
autopilot selftest, with a fake account log. Every name, street and number here is invented.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import ADRIANA, EMPLOYEES, ET, PRESETS, T0, FakeCRM, FakeScorer, FakeSource, _rec

MOBILE1 = {"number": "8655550101", "type": "MOBILE", "tags": ["Dial First"], "status": "UNKNOWN"}
MOBILE2 = {"number": "8655550102", "type": "MOBILE", "tags": ["Dial Second"], "status": "UNKNOWN"}
LAND = {"number": "8655550103", "type": "LANDLINE", "tags": ["Dial First"], "status": "UNKNOWN"}
DROP = {"number": "8655550109", "type": "MOBILE", "tags": ["Drop"], "status": "UNKNOWN"}

PLAYBOOK = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0},
    "people": {"callers": ["Adriana"]},
    "gate": {"read_fields": True},
    "lanes": {"ftm": {"tags": ["FTM"]}},
    "stages": {
        "k_assign": {"kind": "assign", "owner": "analysis", "mode": "live", "schedule": "every 30 min", "lane": "ftm",
                     "presets": ["FTM - 02 Ready to Call"], "backlog": True},
        "k_retrace": {"kind": "retrace", "owner": "research", "mode": "live", "schedule": "11:00", "lane": "ftm",
                      "presets": ["FTM - 01 Skipped No Numbers"], "sources": ["tracerfy"], "after_sources": "none"},
        "k_score": {"kind": "score", "owner": "research", "mode": "live", "schedule": "08:30", "lane": "ftm",
                    "presets": ["FTM - 02 Ready to Call"]},
        "k_mail": {"kind": "mail", "owner": "ops", "mode": "live", "schedule": "10:00", "vendor": "csv", "lane": "ftm",
                   "send": "staged", "daily_pieces": 10,
                   "pieces": [{"preset": "FTM - Mail 01 Handwritten Letter", "piece": "handwritten_letter"}]},
        "k_attempts": {"kind": "attempts", "owner": "ops", "mode": "live", "schedule": "every 60 min", "lane": "ftm",
                       "presets": ["FTM - 02 Ready to Call", "FTM - 03 Call Attempt 1", "FTM - 04 Call Attempt 2"]},
        "k_screen": {"kind": "screen", "owner": "research", "mode": "live", "schedule": "every 30 min", "lane": "ftm",
                     "presets": ["FTM - 02 Ready to Call", "FTM - 01 Skipped No Numbers"]},
        "k_lanes": {"kind": "lanes", "owner": "ops", "mode": "plan", "schedule": "02:30 daily", "skip": ["top1000"]},
        "k_revoke": {"kind": "revoke", "owner": "sales", "mode": "live", "schedule": "every 60 min", "smarter_contact": False},
        "k_watch": {"kind": "watchdog", "owner": "ops", "mode": "plan", "schedule": "07:20 daily", "expect": [
            {"name": "Sold tag to Sold", "when": {"event": "property.tags.added", "tag": "recently sold"}, "then": {"status": "sold"}},
            {"name": "Call New Lead", "when": {"event": "property.status.updated", "to": "new_lead"},
             "then": {"assigned": True, "task": True, "card": True}}]},
        "k_audit": {"kind": "audit", "owner": "ops", "mode": "plan", "schedule": "07:30 daily"},
        "k_second": {"kind": "reactivate", "owner": "analysis", "mode": "live", "schedule": "Mon 08:15",
                     "presets": ["Not Interested - FTM Foreclosure (30d)"]},
    },
}


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _et(month: int, day: int, hour: int = 11, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ET)


def dial(i, owner, when, number="8655550101", kind="made", inbound=False, caller="Adriana"):
    return {"id": "call-%s" % i, "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"destination_number": "8655550000" if inbound else number,
                                 "origin_number": number if inbound else "8655550000",
                                 "direction": "inbound" if inbound else "outbound", "duration": 40, "source": "kixie",
                                 "owner": owner, "uuid": "c-%s" % i, "extra": {}, "user": {"first_name": caller, "uuid": "u-adriana"}}}}


def status_ev(i, rec, when, old, new, source="internal-api"):
    return {"id": "st-%s" % i, "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": source, "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"property": {"status": [old, new]}}}


def tag_ev(i, rec, when, tags, added=True):
    return {"id": "tg-%s" % i, "event_type": "property.tags.%s" % ("added" if added else "removed"), "resource": rec,
            "resource_type": "property", "source": "bulk", "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"tags": list(tags), "tags_uuid": []}}


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import (activity, attempts, audit, budget, gate, lanecheck, lanes, ledger, mail, policy, revoke,
                            screen, stages, sweep, undo)
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    config.MODE = "live"
    store.init()
    saved_presets = dict(PRESETS)
    PRESETS["FTM - 03 Call Attempt 1"] = lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 1
    PRESETS["FTM - 04 Call Attempt 2"] = lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 2
    try:
        _run(check, tmp, store, activity, attempts, audit, budget, gate, lanecheck, lanes, ledger, mail, policy, revoke,
             screen, stages, sweep, undo)
    finally:
        PRESETS.clear()
        PRESETS.update(saved_presets)
        screen.lookup_index = None
        config.MODE = "dry"


def _run(check, tmp, store, activity, attempts, audit, budget, gate, lanecheck, lanes, ledger, mail, policy, revoke,
         screen, stages, sweep, undo) -> None:
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    # This section runs last, on the selftest's throwaway file. Start from an empty autopilot memory so a
    # record id an earlier section also used (its mail, its changes) cannot leak into these checks.
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_log", "ap_phones"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")

    # ------------------------------------------------------------------ the gate, one rule at a time
    print("  gate")
    base = _rec("g0", "10 Gate St", phones=[dict(MOBILE1), dict(LAND)])
    base["lists"] = ["Foreclosure"]
    ok = gate.check(base, "call")
    check(ok.ok and [n["number"] for n in ok.numbers] == ["8655550101", "8655550103"],
          "a clean record passes, and a call may use its top-tier numbers")
    check([n["number"] for n in gate.check(base, "text").numbers] == ["8655550101"], "a text may use only a mobile")

    def held(change, channel="assign", **kw):
        r = copy.deepcopy(base)
        change(r)
        return gate.check(r, channel, **kw)

    v = held(lambda r: r["tags"].append("Do Not Market"), "research")
    check(not v.ok and v.code == "do_not_market", "Do Not Market stops everything, research included")
    check(not held(lambda r: r["tags"].append("do not market"), "mail", allow=gate.RULES).ok,
          "nothing a stage is allowed lifts Do Not Market, and the tag matches in any case")
    check(held(lambda r: r["tags"].append("Litigator"), "mail").code == "litigator", "a Litigator tag on the record stops mail too")
    check(held(lambda r: r["owner"]["phones"][0]["tags"].append("Litigator")).code == "litigator",
          "a Litigator tag on one phone holds the whole record")
    check(gate.check(base, "assign", litigators={"8655550103"}).code == "litigator",
          "a number the scoring cache knows as a litigator's holds the record")
    check(held(lambda r: r["owner"].update(opt_out=True), "mail").code == "opted_out", "an opted-out owner gets no mail either")
    check(held(lambda r: r["owner"].update(dnc=True), "call").code == "opted_out", "the owner's do-not-call flag holds the record")
    check(held(lambda r: r["lists"].append("DNC"), "text").code == "opted_out", "the DNC list holds the record")
    check(held(lambda r: r.update(status="opt_out"), "trace").code == "opted_out", "an opt-out status holds a paid trace")
    check(held(lambda r: r["tags"].append("recently sold"), "research").code == "sold", "a sold house is not researched")
    check(held(lambda r: r.update(status="sold")).code == "sold", "status sold holds the record")

    d1 = held(lambda r: r["owner"].update(deceased=True))
    d2 = held(lambda r: r["tags"].append("deceased"), "call")
    d3 = held(lambda r: r["lists"].append("Obituary"), "mail")
    d4 = gate.check(base, "text", custom={"Owner Deceased": "Yes"})
    check(all(x.code == "deceased" for x in (d1, d2, d3, d4)),
          "a death is read from all four places: the owner flag, the tag, the Obituary list and the Owner Deceased field")
    check("Obituary list" in d3.reason() and "research first" in d3.reason(), "the reason says where the death was recorded")
    check(held(lambda r: r["lists"].append("Obituary"), "research").ok, "a possible death does not stop research: research is how it is settled")
    check(held(lambda r: r["lists"].extend(["Obituary", "Probate"]), "call").ok,
          "a probate record is not held by the Obituary list: its owner is the court's representative")
    check(not held(lambda r: (r["lists"].append("Probate"), r["owner"].update(deceased=True)), "call").ok,
          "an explicit deceased flag still holds a probate record")
    check(not held(lambda r: r["owner"].update(deceased=True), "call", allow=("heir",)).ok
          and held(lambda r: (r["owner"].update(deceased=True), r["tags"].append("Heir Identified")), "call", allow=("heir",)).ok,
          "the heir lane may work a dead owner's record only once research tagged a verified heir")

    check(held(lambda r: r.update(status="Hot Lead")).code == "lead" and held(lambda r: r.update(status="new_lead"), "trace").code == "lead",
          "a lead is not touched by a prospecting stage")
    check(held(lambda r: r.update(status="follow_up"), "research").ok, "research may still run beside a lead")
    check(held(lambda r: r.update(status="Hot Lead"), "task", allow=("lead",)).ok, "a lead-management stage may act on a lead")
    ni = held(lambda r: r.update(status="not_interested"))
    check(ni.code == "finished" and "not_interested" in ni.reason(), "Not Interested is finished for every cold stage")
    check(held(lambda r: r.update(status="not_interested"), allow=("not_interested",)).ok
          and not held(lambda r: r.update(status="Dead Lead"), allow=("not_interested",)).ok,
          "only the second-chance lane lifts Not Interested, and it lifts nothing else")
    check(held(lambda r: r.update(status="Auction Date Passed"), "mail").code == "finished", "a passed auction stops every channel")
    check(held(lambda r: r.update(status="Brand New Status")).code == "unknown_status", "a status the gate has never seen holds the record")
    pol_w = policy.parse({**copy.deepcopy(PLAYBOOK), "gate": {"working_statuses": ["Brand New Status"]}}, EMPLOYEES)
    check(held(lambda r: r.update(status="Brand New Status"), cfg=pol_w.gate).ok, "an install can name its own working statuses")
    check(held(lambda r: r.update(status="Deep Prospecting"), "call").code == "in_research"
          and held(lambda r: r.update(status="Deep Prospecting"), "research").ok, "a record held for research is not called, and is researched")
    check(held(lambda r: r.update(status="Exhausted"), "call").code == "exhausted" and held(lambda r: r.update(status="Exhausted"), "mail").ok,
          "an exhausted record is not called again and still gets mail")
    check(held(lambda r: r.update(status="Return Mail"), "mail").code == "return_mail" and held(lambda r: r.update(status="Return Mail"), "call").ok,
          "returned mail stops mail, not calls")
    check(held(lambda r: r.update(mls="Listed"), "text").code == "listed" and held(lambda r: r.update(mls="Listed"), "research").ok,
          "a listed house gets no cold outreach")
    check(held(lambda r: r.update(mls="Sold"), "call").ok, "an MLS value of Sold alone holds nothing: it mirrors the owner's own purchase")
    check(held(lambda r: r["tags"].append("Not Single Family"), "mail").code == "not_single_family", "a record tagged Not Single Family is held")
    check(held(lambda r: r["tags"].append("Mail Only"), "call").code == "mail_only" and held(lambda r: r["tags"].append("Mail Only"), "mail").ok,
          "Mail Only stops calls and texts and lets mail through")
    check(held(lambda r: r.update(do_not_mail_ever=True), "mail").code == "do_not_mail"
          and held(lambda r: r["owner"].update(do_not_mail_ever=True), "mail").code == "do_not_mail"
          and held(lambda r: r.update(do_not_mail_ever=True), "call").ok, "do-not-mail on the record or the owner stops mail only")

    n1 = held(lambda r: r["owner"].update(phones=[dict(DROP)]), "call")
    check(n1.code == "no_number", "a record whose only number is Drop tier has nothing to call")
    wrong = dict(MOBILE1, status="WRONG")
    check(held(lambda r: r["owner"].update(phones=[wrong]), "text").code == "no_number", "a number marked wrong is not usable")
    correct = dict(DROP, status="CORRECT")
    check(held(lambda r: r["owner"].update(phones=[correct]), "call").ok, "a number a person confirmed is usable whatever its tier")
    flagged_row = {"phone": {"number": "8655550101", "doNotCall": True}}
    check([n["number"] for n in gate.check(base, "call", row=flagged_row).numbers] == ["8655550103"]
          and gate.check(base, "text", row=flagged_row).code == "no_number",
          "the number the do-not-call registry flagged is left out, where the search row shows the flag")
    check(gate.check({}, "assign").code == "unreadable", "a record that cannot be read is held")
    try:
        gate.check(base, "fax")
        check(False, "an unknown channel is refused")
    except ValueError:
        check(True, "an unknown channel is refused")

    row = {"uuid": "g9", "status": "not_interested", "do_not_mail_ever": False, "owner_do_not_mail_ever": True}
    check(not gate.check_row(row, "trace").ok and gate.check_row(row, "assign", allow=("not_interested",)).ok
          and gate.check_row({"status": None, "owner_do_not_mail_ever": True}, "mail").code == "do_not_mail",
          "the row check answers what a search row can: its status and the do-not-mail flags")
    own = {"lane": "lead", "records": {"other": "lead"}}
    ol = gate.check(base, "call", lane="ftm", owner=own)
    check(ol.code == "owner_lane" and "lead lane" in ol.reason(), "an owner who is a lead on another record is held here")
    check(gate.check(base, "call", lane="ftm", owner={"lane": "hot_auction", "records": {"g0": "hot_auction"}}).ok,
          "the record that puts the owner in the higher lane is not held by its own lane")
    check(gate.check(base, "research", lane="ftm", owner=own).ok, "research is not a lane: it runs beside the owner's lane")
    try:
        policy.parse({**copy.deepcopy(PLAYBOOK), "gate": {"do_not_market_tags": []}}, EMPLOYEES)
        check(False, "a playbook that empties the Do Not Market tag is refused")
    except policy.PolicyError:
        check(True, "a playbook that empties the Do Not Market tag is refused")

    # ------------------------------------------------------------------ lanes and the timeline
    print("  lanes")
    L = pol.lanes
    check(lanes.lane_of(dict(base, status="Hot Lead")) == "lead" and lanes.lane_of(dict(base, tags=["FTM", "Auction Soon"])) == "hot_auction"
          and lanes.lane_of(base) == "ftm" and lanes.lane_of(dict(base, tags=["Top 1000"])) == "top1000"
          and lanes.lane_of(dict(base, tags=[], lists=["Obituary"])) == "obituary" and lanes.lane_of(dict(base, tags=[], lists=[])) == "agency",
          "a record's lane comes from its status, tags and lists, highest first")
    check(lanes.top_lane(["agency", "ftm", "top1000"]) == "ftm" and lanes.rank("lead") < lanes.rank("heirs") < lanes.rank("agency"),
          "the highest lane wins")
    fri, mon = _et(11, 6).date(), _et(11, 9).date()
    check(lanes.business_days(fri, fri) == 0 and lanes.business_days(fri, mon) == 1 and lanes.business_days(_et(11, 2).date(), _et(11, 12).date()) == 8,
          "days in a lane are business days: a Friday record is on day 1 on Monday")
    co = lambda ch, day, counter=0, facts=None, wd=2: lanes.channel_open(L, "ftm", ch, day, counter, facts, wd)[0]  # noqa: E731
    check(co("text", 0) and co("text", 2) and not co("text", 3) and not co("call", 0) and co("call", 1) and co("call", 5, 4),
          "the opening texts run on days 0 to 2 and calls start the day after a record lands")
    check(not co("call", 3, 5) and not co("call", 2, 0, None, 5), "the call round ends at 5 attempts, and call days are weekdays")
    check(not co("mail", 3, 2) and co("mail", 3, 5) and co("mail", 8, 0), "mail opens when the round ends or on day 8, whichever is first")
    check(co("mail", 0, 0, {"foreclosure": True}) and co("mail", 1, 0, {"no_number": True}),
          "a foreclosure and a record with no number are mailed at once")
    check(not co("followup_text", 4, 2) and co("followup_text", 9, 5), "follow-up texts wait for the round to end")
    check(lanes.channel_open(L, "top1000", "mail", 0)[0], "a lane with no written timeline is not restricted by the table")
    check(len(lanes.describe(L, "ftm")) == 5 and "counter reads 5" in " ".join(lanes.describe(L, "ftm")), "the timeline prints in plain lines")
    try:
        policy.parse({**copy.deepcopy(PLAYBOOK), "lanes": {"warp": {"tags": ["X"]}}}, EMPLOYEES)
        check(False, "an unknown lane is refused")
    except policy.PolicyError:
        check(True, "an unknown lane is refused")
    bad = copy.deepcopy(PLAYBOOK)
    bad["stages"]["k_assign"]["lane"] = "fastlane"
    bad["stages"]["k_second"]["gate_allow"] = ["everything"]
    bad["stages"]["k_watch"]["expect"] = [{"when": "tag"}]
    try:
        policy.parse(bad, EMPLOYEES)
        check(False, "a bad lane, a bad allow word and a malformed expectation are refused")
    except policy.PolicyError as exc:
        check(all(k in str(exc) for k in ("fastlane", "everything", "expect 1")), "a bad lane, a bad allow word and a malformed expectation are refused")

    # ------------------------------------------------------------------ the request budget
    print("  budget")
    clock = {"t": 1794500010.0}                          # a daytime moment, ten seconds into a minute
    while budget.is_night(clock["t"]) or budget.is_quiet(clock["t"]) or budget.is_quiet(clock["t"] + 120):
        clock["t"] += 3600
    clock["t"] -= clock["t"] % 60
    clock["t"] += 10
    slept = []

    def fake_sleep(s):
        slept.append(s)
        clock["t"] += s

    cap = budget.per_min(clock["t"])
    for _ in range(int(cap * (1 - budget.RESERVE))):
        budget.acquire(name="test-a", clock=lambda: clock["t"], sleep=fake_sleep)
    check(not slept, "requests inside the minute's share do not wait")
    budget.acquire(name="test-a", priority=True, clock=lambda: clock["t"], sleep=fake_sleep)
    check(not slept, "priority work may use the part of the minute that ordinary work may not")
    w = budget.acquire(name="test-a", clock=lambda: clock["t"], sleep=fake_sleep)
    check(slept and 49 <= w <= 51, "the next ordinary request waits for the next minute")
    check(budget.usage("test-a", clock["t"])["minute"] == 1 and budget.usage("test-b", clock["t"])["minute"] == 0,
          "each app counts its own share, across every process that uses the same file")
    import os
    os.environ["DATASIFT_RATE_PER_HOUR"], os.environ["DATASIFT_RATE_NIGHT_PER_HOUR"] = "3", "3"
    try:
        hit = 0
        for _ in range(5):
            try:
                budget.acquire(name="test-h", priority=True, clock=lambda: clock["t"], sleep=fake_sleep)
            except budget.BudgetExhausted:
                hit += 1
        check(hit == 2, "past the hour's share a request is refused out loud instead of sleeping the hour away")
    finally:
        os.environ.pop("DATASIFT_RATE_PER_HOUR"), os.environ.pop("DATASIFT_RATE_NIGHT_PER_HOUR")
    night = clock["t"]
    while not budget.is_night(night):
        night += 3600
    check(budget.per_min(night) > budget.per_min(clock["t"]), "the night share is larger than the day share")
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zone
    run_time = _dt(2026, 11, 13, 6, 40, tzinfo=_Zone(budget.TZ)).timestamp()      # the first-to-market box is mid-run
    after = _dt(2026, 11, 13, 8, 0, tzinfo=_Zone(budget.TZ)).timestamp()
    check(budget.is_quiet(run_time) and not budget.is_quiet(after) and budget.per_min(run_time) < budget.per_min(after) / 3,
          "while the first-to-market box makes its morning run this app drops to a trickle")
    os.environ["DATASIFT_RATE_QUIET"] = "off"
    try:
        check(not budget.is_quiet(run_time), "an install with no morning run switches the quiet window off")
        os.environ["DATASIFT_RATE_QUIET"] = "23:30-00:30"
        late = _dt(2026, 11, 13, 0, 10, tzinfo=_Zone(budget.TZ)).timestamp()
        check(budget.is_quiet(late) and not budget.is_quiet(run_time), "a quiet window may cross midnight")
        os.environ["DATASIFT_RATE_QUIET"] = "sometime"
        check(budget.is_quiet(run_time), "an unreadable window falls back to the default, never to no window")
    finally:
        os.environ.pop("DATASIFT_RATE_QUIET", None)

    # ------------------------------------------------------------------ the shared log
    print("  activity log")
    activity.reset()

    class PagedLog:
        """A log that pages like the real one: newest first, 100 rows a page, a cursor between pages."""
        def __init__(self, rows):
            self.rows, self.calls = rows, 0

        def logs(self, must, limit=100, cursor="", include_owner=False):
            self.calls += 1
            lo, hi = (list(must.get("timestamp") or [None, None]) + [None, None])[:2]
            types = set(must.get("any_event_type") or [])
            hit = [e for e in self.rows if (not types or e["event_type"] in types)
                   and not (lo and e["timestamp"].replace(" ", "T") + "Z" < lo)
                   and not (hi and e["timestamp"].replace(" ", "T") + "Z" > hi)]
            hit.sort(key=lambda e: e["timestamp"], reverse=True)
            i = int(cursor or 0)
            return {"results": hit[i:i + limit], "next_cursor": str(i + limit) if i + limit < len(hit) else None}

    big_day = _et(11, 10, 9)                    # one busy day: 450 dials, a few seconds apart
    paged = PagedLog([dial("big%d" % i, "o-big%d" % (i % 7), big_day + timedelta(seconds=20 * i)) for i in range(450)])
    sync_now = big_day + timedelta(hours=20)
    store.kv_set("autopilot:log:calls:through", (big_day - timedelta(hours=1)).astimezone(timezone.utc).isoformat(timespec="seconds"))
    rounds, done_sync = 0, False
    while rounds < 12 and not done_sync:
        done_sync = activity.sync(paged, "calls", now=sync_now, floor_days=45, max_calls=2)["complete"]
        rounds += 1
    stored = len(activity.events(activity.DIAL, since=big_day - timedelta(hours=1), until=big_day + timedelta(hours=12)))
    check(done_sync and stored == 450 and 3 <= rounds <= 6,
          "a day too big for one run is finished over the next runs, from where the last one stopped, not from the top")
    activity.reset()
    e_call = dial(1, "o-a1", _et(11, 10, 14))
    kind, data = activity.slim(e_call)
    check(kind == "o-a1" and data["number"] == "8655550101" and data["direction"] == "outbound" and data["caller"] == "Adriana",
          "a dial is filed under the OWNER with the number, the direction and who dialed")
    check(activity.slim(dial(2, "o-a1", _et(11, 10), inbound=True))[1]["direction"] == "inbound"
          and activity.slim(dial(2, "o-a1", _et(11, 10), inbound=True))[1]["number"] == "8655550101",
          "an inbound call keeps the prospect's number, not ours")
    check(activity.slim(status_ev(1, "r-x", _et(11, 10), None, "sold"))[1] == {"from": "", "to": "sold", "by": ""}
          and activity.slim(tag_ev(1, "r-x", _et(11, 10), ["FTM"]))[1] == {"tags": ["FTM"]}, "status and tag changes keep what changed")
    card = {"id": "cd1", "event_type": "card.created", "resource": "card-1", "resource_type": "card", "source": "sequence",
            "author": "system", "timestamp": _ts(_et(11, 10)),
            "payload": {"card": {"prop": "r-x"}, "extra_info": {"card": {"prop": "r-x", "column": ["New Lead"], "board": "Leads"},
                                                                 "source": {"title": "Call New Lead"}}}}
    res, cd = activity.slim(card)
    check(res == "r-x" and cd["column"] == "New Lead" and cd["by"] == "Call New Lead", "a card event is filed under its property, with the sequence that made it")
    crm_l = FakeCRM([])
    crm_l.log_rows = [dial(i, "o-a1", _et(11, 3 + i, 11)) for i in range(6)] + [dial(9, "o-a1", _et(11, 5, 11), kind="answered")]
    s1 = activity.sync(crm_l, "calls", now=T0, floor_days=20, max_calls=4)
    check(not s1["complete"] and s1["calls"] == 4, "a sync that hits its request cap stops and says it is not complete")
    s2 = activity.sync(crm_l, "calls", now=T0, floor_days=20, max_calls=400)
    check(s2["complete"] and len(activity.events(activity.DIAL)) == 6, "the next sync resumes at the same day and reads the rest, each event once")
    days = activity.dial_days("o-a1", since=_et(11, 5, 0))
    check(sorted(days) == ["2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"], "dial days are counted since a moment, in business-local dates")
    check(activity.owners_dialed(_et(11, 1)) == {"o-a1"}, "the owners dialed since a date come from the stored log")

    # ------------------------------------------------------------------ the gate inside the stages
    print("  stages behind the gate")
    store.kv_set("autopilot:baseline:k_assign", "")
    a_ok = _rec("s1", "11 Clear St", phones=[dict(MOBILE1)])
    a_dead = _rec("s2", "12 Late St", phones=[dict(MOBILE1)])
    a_dead["lists"] = ["Obituary"]
    a_dnm = _rec("s3", "13 Quiet St", phones=[dict(MOBILE1)])
    a_dnm["tags"].append("Do Not Market")
    crm_s = FakeCRM([a_ok, a_dead, a_dnm])
    sa = sweep.run("k_assign", crm=crm_s, now=T0, pol=pol)
    check(crm_s.recs["s1"]["assigned_to"] == "u-adriana" and crm_s.recs["s2"]["assigned_to"] is None and crm_s.recs["s3"]["assigned_to"] is None,
          "the assign stage hands out the clean record and holds a possible death and a Do Not Market record")
    check(any("held back: tagged Do Not Market" in k for k in sa.tally) and any("research first" in k for k in sa.tally),
          "each held record is counted under its reason")
    ch = ledger.changes(record_uuid="s1")
    check(len(ch) == 1 and ch[0]["what"] == "assign" and ch[0]["before"] == "" and ch[0]["after"] == "u-adriana",
          "a live write records the value it replaced")
    r_ob = _rec("t1", "21 Trace St")
    r_ob["lists"] = ["Obituary"]
    r_ok = _rec("t2", "22 Trace St")
    r_cf = _rec("t3", "23 Trace St")
    crm_t = FakeCRM([r_ob, r_ok, r_cf])
    crm_t.custom = {"t3": {"Owner Deceased": "Yes"}}
    src = FakeSource("tracerfy", 0.02, "row", {"t2": ["8655550141"]})
    queued = []

    def fake_enqueue(agent, wf, record_uuid="", subject="", **kw):
        queued.append((wf, record_uuid))
        return len(queued), True

    st_r = sweep.run("k_retrace", crm=crm_t, now=T0, pol=pol, sources={"tracerfy": src}, scorer=FakeScorer(), enqueue=fake_enqueue)
    check(src.calls == [["t2"]] and sorted(queued) == [("deep_prospect", "t1"), ("deep_prospect", "t3")],
          "a possible death (the Obituary list, the Owner Deceased field) goes to research, never to a blind trace")
    check(("skiptrace_attempts", "o-t2", 1) in crm_t.writes and crm_t.recs["t2"]["owner"]["skiptrace_attempts"] == 1,
          "numbers from an outside source also raise the owner's skip trace counter, so the record reads as traced")
    check(st_r.problems == [], "the retrace ran clean")
    lit_a = _rec("l1", "31 Suit St", phones=[{"number": "8655550167", "type": "MOBILE", "tags": [], "status": "UNKNOWN"}])
    lit_b = _rec("l2", "32 Suit St", phones=[dict(MOBILE1)])
    lit_b["owner"]["uuid"] = "o-l1"
    crm_x = FakeCRM([lit_a, lit_b])
    sweep.run("k_score", crm=crm_x, now=T0, pol=pol, scorer=FakeScorer())
    check({"Litigator", "Do Not Market"} <= set(crm_x.recs["l1"]["tags"]) and {"Litigator", "Do Not Market"} <= set(crm_x.recs["l2"]["tags"]),
          "a litigator's number tags the record AND the owner's other record Litigator and Do Not Market")
    check("Litigator" in crm_x.recs["l1"]["owner"]["phones"][0]["tags"] and any(m[0] == "l2" for m in crm_x.msgs),
          "the phone carries the tag and each record gets the note")
    check(not gate.check(crm_x.record("l2"), "mail").ok, "after that, the gate holds the owner's other house on every channel")

    print("  mail behind the gate and the timeline")
    vend = mail.vendor_for(pol.stage("k_mail"))
    early = _rec("m1", "41 Early St", phones=[dict(MOBILE1)])
    early["tags"] = ["FTM", "Courthouse Data"]
    done_r = _rec("m2", "42 Round St", phones=[dict(MOBILE1)], predictivecall_attempts=5)
    done_r["tags"] = ["FTM", "Courthouse Data"]
    nonum = _rec("m3", "43 Silent St")
    nonum["tags"] = ["FTM", "Courthouse Data"]
    fc = _rec("m4", "44 Notice St", phones=[dict(MOBILE1)])
    crm_m = FakeCRM([early, done_r, nonum, fc])
    crm_m.rec_logs = {u: [{"event_type": "property.created", "timestamp": _ts(T0 - timedelta(days=1)), "payload": {}}]
                      for u in ("m1", "m2", "m3", "m4")}
    sm = sweep.run("k_mail", crm=crm_m, now=T0, pol=pol, mailer=vend)
    staged = {it["record_uuid"] for it in ledger.batch(sm.batch)["items"]} if sm.batch else set()
    check(staged == {"m2", "m3", "m4"},
          "on day 1 mail goes only to the record whose call round is over, the one with no number and the foreclosure")
    check(any("mail starts on day 8" in k for k in sm.tally), "the record still inside its call round is held, with the reason")
    crm_m.recs["m3"]["tags"].append("Do Not Market")
    crm_m.recs["m2"]["status"] = "new_lead"
    rel = mail.release(sm.batch, crm=crm_m, pol=pol, commit=True, uploaded=False, now=T0) if not vend.export_only else None
    out2 = stages.Summary("k_mail", "mail", "ops", "live", True)
    ctx2 = stages.Ctx(policy=pol, stage=pol.stage("k_mail"), crm=crm_m, run_id="rel", now=T0, live=True, out=out2, mailer=mail.Vendor())
    pieces = [mail.Piece(**{k: v for k, v in it.items() if k in mail.Piece.__dataclass_fields__}) for it in ledger.batch(sm.batch)["items"]]
    kept_n, _ = mail.deliver(ctx2, pieces, {}, "release test")
    check(rel is None and sum(n for k, n in out2.tally.items() if k.startswith("held back at release")) == 2,
          "at release the gate runs again: the owner who opted out and the one who became a lead since staging get nothing")
    # the day's mail budget: once it refuses a piece, the rest of the lane is counted, not read
    price = vend.price("handwritten_letter")
    pol_b = policy.parse({**copy.deepcopy(PLAYBOOK), "caps": {"daily_usd": 25, "record_usd": 1.0,
                                                               "outreach_daily_usd": round(price * 1.5, 2)}}, EMPLOYEES)
    lane6 = []
    for i in range(6):
        r = _rec("b%d" % i, "%d Budget St" % (60 + i))
        r["tags"] = ["FTM", "Courthouse Data"]
        lane6.append(r)
    crm_b = FakeCRM(lane6)
    crm_b.rec_logs = {r["uuid"]: [{"event_type": "property.created", "timestamp": _ts(T0 - timedelta(days=1)), "payload": {}}]
                      for r in lane6}
    reads = []
    real_record = crm_b.record
    crm_b.record = lambda u: (reads.append(u), real_record(u))[1]
    sb = sweep.run("k_mail", crm=crm_b, now=T0, pol=pol_b, mailer=vend, plan=True)
    check(sb.acted == 1 and len(reads) == 2 and sb.tally.get("left for tomorrow (the day's mail budget is used)") == 4,
          "once the day's mail budget refuses a piece, the rest of the lane is counted without one more record read")

    # the scoring stage reads a bounded number of records a run, and takes up where it left off
    lane5 = [_rec("sc%d" % i, "%d Tier St" % (70 + i), phones=[dict(MOBILE1, number="86555502%02d" % i)]) for i in range(5)]
    crm_sc = FakeCRM(lane5)
    pol_sc = policy.parse({**copy.deepcopy(PLAYBOOK), "stages": {**copy.deepcopy(PLAYBOOK["stages"]), "k_score": {
        **copy.deepcopy(PLAYBOOK["stages"]["k_score"]), "mode": "plan", "max_reads": 2}}}, EMPLOYEES)
    read_log = []
    real_rec = crm_sc.record
    crm_sc.record = lambda u: (read_log.append(u), real_rec(u))[1]
    s1 = sweep.run("k_score", crm=crm_sc, now=T0, pol=pol_sc)
    first = list(read_log)
    s2 = sweep.run("k_score", crm=crm_sc, now=T0 + timedelta(hours=1), pol=pol_sc)
    second = read_log[len(first):]
    check(len(first) == 2 and s1.tally.get("not read this run (read cap 2)") == 3 and len(second) == 2
          and not set(first) & set(second),
          "the scoring stage reads a capped number of records a run and the next run reads the ones it left")

    # ------------------------------------------------------------------ the attempt keeper
    print("  attempt keeper")
    activity.reset()
    k1 = _rec("a1", "51 Dial St", phones=[dict(MOBILE1), dict(MOBILE2)])
    k2 = _rec("a2", "52 Dial St", phones=[dict(MOBILE1)], predictivecall_attempts=1)
    k3 = _rec("a3", "53 Dial St", phones=[dict(MOBILE1)])
    k4 = _rec("a4", "54 Dial St", phones=[dict(MOBILE1)], predictivecall_attempts=2)
    crm_a = FakeCRM([k1, k2, k3, k4])
    crm_a.rec_logs = {"a1": [{"event_type": "property.tags.added", "timestamp": _ts(_et(11, 3, 7)), "payload": {"tags": ["FTM"]}},
                             {"event_type": "property.created", "timestamp": _ts(_et(8, 1)), "payload": {}}]}
    crm_a.log_rows = [
        dial("a", "o-a1", _et(10, 20, 10)),                                # before the record entered the FTM lane
        dial("b", "o-a1", _et(11, 5, 10)), dial("c", "o-a1", _et(11, 9, 10)),
        dial("d", "o-a1", _et(11, 9, 15), number="8655550102"),            # the same day, a second number
        dial("e", "o-a1", _et(11, 10, 10)), dial("f", "o-a1", _et(11, 10, 10, 5), kind="answered"),
        dial("g", "o-a1", _et(11, 10, 16), inbound=True),                  # the owner calling back is not an attempt
        dial("h", "o-a2", _et(11, 10, 10)),                                # a2 already reads 1: nothing to write
        dial("i", "o-a4", _et(11, 9, 10)),                                 # a4 reads 2 with one call day: never lowered
    ]
    sp = sweep.run("k_attempts", crm=crm_a, now=T0, pol=pol, plan=True)
    check(sp.acted == 1 and not crm_a.writes and "0 to 3" in sp.head,
          "the plan finds the one record whose counter is behind: three call days since it entered the lane")
    check(any("Nov 5, Nov 9 and Nov 10" in ln for ln in sp.lines), "each proposed change shows the days it was dialed, as evidence")
    s_first = sweep.run("k_attempts", crm=crm_a, now=T0, pol=pol)
    check(s_first.batch and not [w for w in crm_a.writes if w[0] == "call_attempts"] and "staged as batch" in s_first.head,
          "the first live run stages its list and writes nothing")
    s_again = sweep.run("k_attempts", crm=crm_a, now=T0 + timedelta(hours=1), pol=pol)
    check(not s_again.batch and "still staged" in s_again.head, "it is not staged twice while it waits on a person")
    dry = attempts.release(s_first.batch, crm=crm_a, pol=pol)
    check(dry.get("dry") and dry["records"] == 1 and not [w for w in crm_a.writes if w[0] == "call_attempts"], "releasing without commit shows the list and writes nothing")
    rel_a = attempts.release(s_first.batch, crm=crm_a, pol=pol, commit=True, now=T0)
    check(crm_a.recs["a1"]["predictivecall_attempts"] == 3 and rel_a["results"] == {"moved": 1}, "the approved list is written, each counter read back")
    crm_a.log_rows.append(dial("j", "o-a3", T0 + timedelta(minutes=90)))
    s_live = sweep.run("k_attempts", crm=crm_a, now=T0 + timedelta(hours=2), pol=pol)
    check(crm_a.recs["a3"]["predictivecall_attempts"] == 1 and s_live.acted == 1 and not s_live.batch,
          "after the first approval the stage writes on its own")
    check(crm_a.recs["a4"]["predictivecall_attempts"] == 2 and crm_a.recs["a2"]["predictivecall_attempts"] == 1,
          "a counter is never lowered, and one that already matches is left alone")
    crm_a.log_rows.append({"id": "low1", "event_type": "property.predictivecall.attempts.decreased", "resource": "a1",
                           "resource_type": "property", "source": "web", "author": "person@example.com",
                           "timestamp": _ts(T0 + timedelta(minutes=150)), "payload": {"property": {"predictivecall_attempts": [3, 0]}}})
    crm_a.recs["a1"]["predictivecall_attempts"] = 0
    s_reset = sweep.run("k_attempts", crm=crm_a, now=T0 + timedelta(hours=3), pol=pol)
    check(crm_a.recs["a1"]["predictivecall_attempts"] == 0 and any("lowered by a person" in k for k in s_reset.tally),
          "a person lowering the counter is a reset: the old call days are not written back")
    store.kv_set(attempts._approved_key("k_attempts"), "")
    activity.reset()
    k5 = _rec("a5", "55 Dial St", phones=[dict(MOBILE1)])
    crm_k = FakeCRM([k5])
    crm_k.counter_broken = True
    crm_k.log_rows = [dial("k", "o-a5", _et(11, 10, 10))]
    store.kv_set(attempts._approved_key("k_attempts"), "x")
    s_bad = sweep.run("k_attempts", crm=crm_k, now=T0, pol=pol)
    check(any("did not stick" in p for p in s_bad.problems), "a counter write that does not stick is caught on the first record")

    # ------------------------------------------------------------------ undo
    print("  undo")
    run_id = [c for c in ledger.changes(record_uuid="a3")][0]["run_id"]
    plan_u = undo.run(run_id, crm=crm_a)
    check(plan_u.get("dry") and plan_u["would_undo"] == 1 and crm_a.recs["a3"]["predictivecall_attempts"] == 1, "undo shows its steps and changes nothing without commit")
    done_u = undo.run(run_id, crm=crm_a, commit=True)
    check(done_u["undone"] == 1 and crm_a.recs["a3"]["predictivecall_attempts"] == 0 and undo.run(run_id, crm=crm_a).get("steps") == 0,
          "undo puts the counter back and a run is undone only once")
    arun = ledger.changes(record_uuid="s1")[0]["run_id"]
    lrun = ledger.changes(record_uuid="l2")[0]["run_id"]
    undo.run(lrun, crm=crm_x, commit=True)
    check("Litigator" not in crm_x.recs["l2"]["tags"] and "unassign" in str(undo.run(arun, crm=crm_s)),
          "added tags come off again, and what has no documented way back is said, not pretended")

    # ------------------------------------------------------------------ the death screen
    print("  death screen")
    idx = ("Example, Pat Q | age 71 | x | obituary date 10/02/2026\n"
           "Example, Patricia | age 44 | x | obituary date 05/01/2026\n"
           "Example, Pat | age 80 | x | obituary date 03/03/2019")
    check([h["age"] for h in screen.index_matches(idx, "Pat", "Example", T0)] == ["71"],
          "the index matches the same first name within three years: not Patricia, not the 2019 death")
    check(screen.index_matches(idx, "P", "Example", T0) == [] and screen.index_matches(idx, "Pat", "Example", T0, owner_age=40) == [],
          "an initial is not a match, and an age far from the owner's is a stranger")
    looked = []

    def fake_index(last, first):
        looked.append((last, first))
        return idx if last == "Example" and first == "Pat" else "No obituary in the index for %s %s." % (first, last)

    screen.lookup_index = fake_index
    d_idx = _rec("d1", "61 Check St", phones=[dict(MOBILE1)])
    d_idx["address"]["county"] = "Knox"
    d_note = _rec("d2", "62 Check St", first="Sam", last="Sample", phones=[dict(MOBILE1)])
    d_note["notes"] = "Trustee sale. Executed by the Estate of Sam Sample."
    d_note["address"]["county"] = "Blount"
    d_prob = _rec("d3", "63 Check St", phones=[dict(MOBILE1)])
    d_prob["tags"].append("Probate")
    d_clear = _rec("d4", "64 Check St", first="Lee", last="Nobody", phones=[dict(MOBILE1)])
    d_clear["address"]["county"] = "Knox"
    crm_d = FakeCRM([d_idx, d_note, d_prob, d_clear])
    pl = sweep.run("k_screen", crm=crm_d, now=T0, pol=pol, plan=True)
    check(pl.acted == 2 and not crm_d.writes, "the plan names the two possible deaths and writes nothing")
    lv = sweep.run("k_screen", crm=crm_d, now=T0, pol=pol)
    check("Verify Deceased" in crm_d.recs["d1"]["tags"] and "Verify Deceased" in crm_d.recs["d2"]["tags"]
          and "Verify Deceased" not in crm_d.recs["d3"]["tags"] and "Verify Deceased" not in crm_d.recs["d4"]["tags"],
          "an index hit and a notice that names an estate are marked for research; a probate and a clear record are not")
    check(("Sample", "Sam") not in looked, "a Blount record is not looked up in the Knox index")
    check(not gate.check(crm_d.record("d1"), "text").ok and gate.check(crm_d.record("d1"), "research").ok,
          "the mark holds the record out of texts and calls until research settles it")
    n_look = len(looked)
    sweep.run("k_screen", crm=crm_d, now=T0 + timedelta(hours=1), pol=pol)
    check(len(looked) == n_look and lv.problems == [], "a screened record is not looked up again")

    # ------------------------------------------------------------------ one owner, one lane
    print("  owner lanes")
    o_lead = _rec("w1", "71 Two St", phones=[dict(MOBILE1)], status="Warm Lead")
    o_lead["tags"] = ["Priority 1"]
    o_ftm = _rec("w2", "72 Two St", phones=[dict(MOBILE1)])
    o_ftm["owner"]["uuid"] = "o-w1"
    o_solo = _rec("w3", "73 One St", phones=[dict(MOBILE1)])
    crm_w = FakeCRM([o_lead, o_ftm, o_solo])
    sl = sweep.run("k_lanes", crm=crm_w, now=T0, pol=pol)
    check(sl.acted == 1 and lanes.owner_lane("o-w1")["lane"] == "lead" and lanes.owner_lane("o-w3")["lane"] == "ftm",
          "the nightly check finds the one owner sitting in two lanes and remembers each owner's highest lane")
    check(len(lanes.owners_in_two_lanes()) == 1 and "lead and ftm" in " ".join(sl.lines), "the report names the pair of lanes")
    store.kv_set("autopilot:baseline:k_assign", "")
    sa2 = sweep.run("k_assign", crm=crm_w, now=T0, pol=pol)
    check(crm_w.recs["w2"]["assigned_to"] is None and crm_w.recs["w3"]["assigned_to"] == "u-adriana"
          and any("lead lane on another record" in k for k in sa2.tally),
          "the gate then holds that owner's first-to-market record, and works the other owner's")
    crm_w.recs["w1"]["status"] = None
    sweep.run("k_lanes", crm=crm_w, now=T0 + timedelta(days=1), pol=pol)
    freed = lanes.owner_lane("o-w1")
    check(freed["lane"] == "ftm" and gate.check(crm_w.record("w2"), "call", lane="ftm", owner=freed).ok
          and gate.check(crm_w.record("w1"), "call", lane="top1000", owner=freed).code == "owner_lane",
          "the map is rebuilt each night: when the lead ends, the first-to-market record is workable and the Top 1,000 record is the one held")

    # ------------------------------------------------------------------ revocation
    print("  revocation")
    activity.reset()
    store.kv_set(revoke.KV, "")
    v1 = _rec("v1", "81 Stop St", phones=[dict(MOBILE1)], predictivecall_attempts=5)
    v2 = _rec("v2", "82 Stop St", phones=[dict(MOBILE1)], predictivecall_attempts=5)
    crm_v = FakeCRM([v1, v2])
    bid = ledger.batch_create("k_mail", "mail", [{"record_uuid": "v1", "price": 1.89}, {"record_uuid": "v2", "price": 1.89}], 3.78)
    crm_v.log_rows = [tag_ev("v", "v1", T0 - timedelta(hours=2), ["Do Not Market"]),
                      status_ev("v", "v2", T0 - timedelta(hours=30), None, "not_interested")]
    rp = sweep.run("k_revoke", crm=crm_v, now=T0, pol=pol, plan=True)
    check(rp.found == 1 and len(ledger.batch(bid)["items"]) == 2, "a plan run names the owner who just opted out and pulls nothing")
    store.kv_set(revoke.KV, "")
    rv = sweep.run("k_revoke", crm=crm_v, now=T0, pol=pol)
    left = ledger.batch(bid)
    check([it["record_uuid"] for it in left["items"]] == ["v2"] and abs(left["usd"] - 1.89) < 1e-6 and rv.acted == 1,
          "a live run takes that owner's piece out of the staged batch before anyone releases it")
    check(sweep.run("k_revoke", crm=crm_v, now=T0 + timedelta(minutes=30), pol=pol).found == 0, "each change is handled once")
    ledger.batch_set(bid, "cancelled")

    # ------------------------------------------------------------------ the sequence watchdog
    print("  sequence watchdog")
    activity.reset()
    q1 = _rec("q1", "91 Fire St", status="sold")
    q2 = _rec("q2", "92 Fire St")
    q3 = _rec("q3", "93 Fire St", status="new_lead", assigned_to="u-adriana")
    q4 = _rec("q4", "94 Fire St", status="new_lead")
    crm_q = FakeCRM([q1, q2, q3, q4])
    t = T0 - timedelta(hours=5)
    task = {"id": "tk1", "event_type": "task.created", "resource": "task-1", "resource_type": "task", "source": "sequence",
            "author": "system", "timestamp": _ts(t + timedelta(minutes=1)),
            "payload": {"task": {"title": "Call New Lead", "assigned_to_property": "q3"}}}
    card3 = copy.deepcopy(card)
    card3.update(id="cd3", timestamp=_ts(t + timedelta(minutes=1)))
    card3["payload"]["card"]["prop"] = "q3"
    card3["payload"]["extra_info"]["card"]["prop"] = "q3"
    crm_q.log_rows = [tag_ev("q1", "q1", t, ["recently sold"]), tag_ev("q2", "q2", t, ["recently sold"]),
                      status_ev("q3", "q3", t, None, "new_lead"), status_ev("q4", "q4", t, None, "new_lead"), task, card3,
                      tag_ev("q9", "q2", T0 - timedelta(minutes=5), ["recently sold"])]
    sw = sweep.run("k_watch", crm=crm_q, now=T0, pol=pol)
    check(sw.found == 4 and sw.acted == 2, "of four triggers in the last day, the two that produced nothing are counted")
    check(any("Sold tag to Sold missed 1 of 2" in p for p in sw.problems) and any("Call New Lead missed 1 of 2" in p and "assignee" in p for p in sw.problems),
          "each sequence that missed is named with what it failed to produce")
    check(sw.tally.get("Sold tag to Sold: fired") == 1, "a record that already had the status counts as fired, read off the record")

    # ------------------------------------------------------------------ the touch audit
    print("  touch audit")
    activity.reset()
    y = (T0.astimezone(ET) - timedelta(days=1)).replace(hour=11, minute=0)
    u_ok = _rec("x1", "101 Audit St", phones=[dict(MOBILE1)])
    u_dnm = _rec("x2", "102 Audit St", phones=[dict(MOBILE1)])
    u_dnm["tags"].append("Do Not Market")
    u_ni = _rec("x3", "103 Audit St", phones=[dict(MOBILE1)], status="not_interested")
    u_late = _rec("x4", "104 Audit St", phones=[dict(MOBILE1)])
    crm_u = FakeCRM([u_ok, u_dnm, u_ni, u_late])
    crm_u.log_rows = [
        dial("x1", "o-x1", y), dial("x2", "o-x2", y), dial("x3", "o-x3", y),
        tag_ev("x2", "x2", y - timedelta(days=3), ["Do Not Market"]),           # tagged before the dial: a violation
        status_ev("x3", "x3", y + timedelta(minutes=4), None, "not_interested"),  # the call is why: not a violation
        dial("x4", "o-x4", y.replace(hour=20, minute=15)),                        # after calling hours
        dial("x5", "o-x1", y + timedelta(minutes=1)), dial("x6", "o-x1", y + timedelta(minutes=2)),   # three dials, one number
        {"id": "ps1", "event_type": "owner.phone.status.updated", "resource": "o-x4", "resource_type": "owner", "source": "kixie",
         "author": "system", "timestamp": _ts(y - timedelta(days=2)), "payload": {"owner": {"phone": "8655550101", "status": ["UNKNOWN", "WRONG"]}}},
    ]
    au = sweep.run("k_audit", crm=crm_u, now=T0, pol=pol)
    check(au.found == 6 and any("tagged Do Not Market" in p and "102 Audit St" in p for p in au.problems),
          "the dial to an owner tagged Do Not Market three days earlier is named")
    check(not any("not_interested" in p for p in au.problems), "the owner who said no ON that call is not a violation: the call is why")
    check(any("outside calling hours" in p for p in au.problems) and any("already marked wrong" in p for p in au.problems)
          and any("3 or more times" in p for p in au.problems),
          "a dial after hours, a dial to a number already marked wrong and a number dialed three times are each counted")
    check("should not have happened" in au.head and not crm_u.writes, "the audit reads only, and says how many touches should not have happened")
    check(au.tally.get("caller ID lines used") == 1 and au.tally.get("most dials on one line in a day") == 6
          and not any("caller ID line" in p for p in au.problems), "dials are counted per caller ID line, and six on one line is under the cap")
    pol_l = policy.parse({**copy.deepcopy(PLAYBOOK), "stages": {**copy.deepcopy(PLAYBOOK["stages"]), "k_audit": {
        **copy.deepcopy(PLAYBOOK["stages"]["k_audit"]), "line_cap": 3}}}, EMPLOYEES)
    au_l = sweep.run("k_audit", crm=crm_u, now=T0, pol=pol_l)
    check(any("more than 3 dials in a day" in p and "ending 0000 made 6" in p for p in au_l.problems),
          "a line worked past its daily cap is named by its last four digits, with its count")

    # ------------------------------------------------------------------ the daily capture
    print("  daily capture")
    from .autopilot import capture
    activity.reset()
    pol_c = policy.parse({**copy.deepcopy(PLAYBOOK), "stages": {**copy.deepcopy(PLAYBOOK["stages"]), "k_capture": {
        "kind": "capture", "owner": "ops", "mode": "plan", "schedule": "19:15 Mon-Fri", "floors": {"dials": 150},
        "dials_without_a_lead": 5}}}, EMPLOYEES)
    evening = T0.astimezone(ET).replace(hour=19, minute=20).astimezone(timezone.utc)
    day0 = T0.astimezone(ET).replace(hour=10, minute=0)
    crm_c = FakeCRM([])
    made = [dial("m%d" % i, "o-c%d" % (i % 4), day0 + timedelta(minutes=i)) for i in range(8)]
    for e in made:
        e["payload"]["call"]["user"] = None            # the dialer names the caller on the answer, not on the dial
    ans = [dial("n1", "o-c0", day0 + timedelta(minutes=1), kind="answered"), dial("n2", "o-c1", day0 + timedelta(minutes=2), kind="answered")]
    ans[0]["payload"]["call"]["duration"] = 15
    ans[1]["payload"]["call"]["duration"] = 185
    crm_c.log_rows = made + ans + [dial("in1", "o-c0", day0 + timedelta(minutes=30), inbound=True),
                                   status_ev("ld", "rec-c", day0 - timedelta(days=3), None, "new_lead")]
    sc = sweep.run("k_capture", crm=crm_c, now=evening, pol=pol_c)
    today = T0.astimezone(ET).date().isoformat()
    row = [r for r in capture.rows(since_day=today) if r["caller"] == "Adriana"]
    check(row and row[0]["dials"] == 8 and row[0]["owners"] == 4 and row[0]["answered"] == 2 and row[0]["conversations"] == 1
          and row[0]["talk_s"] == 200, "the day is written per caller: dials, owners, answers, and only the long answer is a conversation")
    check("8 dials" in sc.head and not crm_c.writes, "the capture reads the log and writes nothing to the CRM")
    check(any("under the floor of 150" in p for p in sc.problems) and any("since the last new lead" in p for p in sc.problems),
          "a day far under the dial floor and a run of dials with no new lead are each called out, with what to check")
    check(any("8 dials" in ln for ln in capture.recent_lines(1)), "the morning digest can quote the last captured day")
    sweep.run("k_capture", crm=crm_c, now=evening + timedelta(minutes=5), pol=pol_c)
    check(len([r for r in capture.rows(since_day=today) if r["caller"] == "Adriana"]) == 1, "running it twice rewrites the day, it does not double it")

    # ------------------------------------------------------------------ the lead check
    print("  lead check")
    activity.reset()
    pol_l = policy.parse({**copy.deepcopy(PLAYBOOK), "stages": {**copy.deepcopy(PLAYBOOK["stages"]), "k_leads": {
        "kind": "leadcheck", "owner": "sales", "mode": "plan", "schedule": "08:10 Mon-Fri"}}}, EMPLOYEES)
    l_ok = _rec("ld1", "121 Lead St", status="Hot Lead", assigned_to="u-adriana", phones=[dict(MOBILE1, status="CORRECT")])
    l_ok["tasks_open"] = 1
    l_bare = _rec("ld2", "122 Lead St", status="new_lead", phones=[dict(MOBILE1)])
    l_cold = _rec("ld3", "123 Lead St", phones=[dict(MOBILE1)])            # not a lead: never read
    crm_ld = FakeCRM([l_ok, l_bare, l_cold])
    base_row = crm_ld._row
    crm_ld._row = lambda r: {**base_row(r), "tasks": {"total": r.get("tasks_open", 0), "complete": 0, "incomplete": r.get("tasks_open", 0)}}
    crm_ld.log_rows = [status_ev("ld2", "ld2", T0 - timedelta(days=3), None, "new_lead")]
    sld = sweep.run("k_leads", crm=crm_ld, now=T0, pol=pol_l)
    check(sld.found == 2 and sld.tally.get("leads with no open task") == 1 and sld.tally.get("leads with no assignee") == 1
          and sld.tally.get("leads with no confirmed number") == 1 and sld.tally.get("leads with still New Lead after a day") == 1,
          "the lead with no task, no assignee, no confirmed number and three days as a new lead is named four ways")
    check(not crm_ld.writes and all("121 Lead St" not in p for p in sld.problems), "the worked lead is not named, and nothing is written")
    crm_ld.recs["ld1"]["lists"] = ["Liens"]
    crm_ld.log_rows += [status_ev("sp1", "ld1", T0 - timedelta(hours=5), None, "new_lead"),
                        dial("sp1", "o-ld1", T0 - timedelta(hours=5) + timedelta(minutes=12))]
    sld2 = sweep.run("k_leads", crm=crm_ld, now=T0 + timedelta(minutes=10), pol=pol_l)
    check(any("gained Liens" in p for p in sld2.problems), "a lead whose record gained a list since the last check is named: that is the day to call")
    check(sld2.tally.get("speed to lead, median minutes") == 12 and any("no outbound call" in p for p in sld2.problems),
          "speed to lead is measured from the status change to the first dial, and a new lead nobody dialed is counted on its own")
    pol_cap = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    pol_cap.stage("k_assign").params["caller_cap"] = 1
    crm_cap = FakeCRM([_rec("cp1", "141 Full St", phones=[dict(MOBILE1)], assigned_to="u-adriana"),
                       _rec("cp2", "142 Full St", phones=[dict(MOBILE1)], assigned_to="u-adriana"),
                       _rec("cp3", "143 Full St", phones=[dict(MOBILE1)])])
    store.kv_set("autopilot:baseline:k_assign", "")
    scap = sweep.run("k_assign", crm=crm_cap, now=T0, pol=pol_cap)
    check(any("over the cap of 1" in p for p in scap.problems) and crm_cap.recs["cp3"]["assigned_to"] == "u-adriana",
          "a caller past the cap is said out loud, and a first-to-market record is still assigned")

    # ------------------------------------------------------------------ the full-attempt list
    print("  full attempt")
    activity.reset()
    pol_f = policy.parse({**copy.deepcopy(PLAYBOOK), "stages": {**copy.deepcopy(PLAYBOOK["stages"]), "k_full": {
        "kind": "fullattempt", "owner": "sales", "mode": "plan", "schedule": "16:30 Mon-Fri"}}}, EMPLOYEES)
    f_short = _rec("fa1", "151 Try St", phones=[dict(MOBILE1), dict(MOBILE2), dict(DROP)])
    f_full = _rec("fa2", "152 Try St", phones=[dict(MOBILE1)])
    f_held = _rec("fa3", "153 Try St", phones=[dict(MOBILE1)], status="not_interested")
    crm_f = FakeCRM([f_short, f_full, f_held])
    noon = T0.astimezone(ET).replace(hour=12, minute=0)
    crm_f.log_rows = [dial("f1", "o-fa1", noon), dial("f2", "o-fa2", noon), dial("f3", "o-fa3", noon),
                      dial("f0", "o-fa1", noon - timedelta(days=1), number="8655550102")]      # yesterday's dial does not count today
    sf = sweep.run("k_full", crm=crm_f, now=T0, pol=pol_f)
    check(sf.found == 3 and sf.tally["got a full attempt"] == 1 and sf.tally["one or two numbers short"] == 1
          and sf.tally["dialed although the record is held"] == 1,
          "of three owners dialed today one got a full attempt, one is a number short and one should not have been dialed")
    check(any("151 Try St" in ln and "0102" in ln and "8655550102" not in ln for ln in sf.lines) and not crm_f.writes,
          "the list shows the last four digits of the untried number, and writes nothing")

    # ------------------------------------------------------------------ tier calibration
    print("  tier calibration")
    from .autopilot import calibration
    activity.reset()
    cal = _rec("cal1", "161 Tier St", phones=[dict(MOBILE1, status="CORRECT"), dict(MOBILE2, status="WRONG"), dict(DROP, status="WRONG")])

    def mark(i, number, to, when):
        return {"id": "pm%s" % i, "event_type": "owner.phone.status.updated", "resource": "o-cal1", "resource_type": "owner",
                "source": "kixie", "author": "system", "timestamp": _ts(when), "payload": {"owner": {"phone": number, "status": ["UNKNOWN", to]}}}

    crm_cal = FakeCRM([cal])
    crm_cal.log_rows = [mark(1, "8655550101", "WRONG", T0 - timedelta(days=3)), mark(2, "8655550101", "CORRECT", T0 - timedelta(days=2)),
                        mark(3, "8655550102", "WRONG", T0 - timedelta(days=2)), mark(4, "8655550109", "WRONG", T0 - timedelta(days=1))]
    os_dir = config.DATA_DIR
    res = calibration.run(crm_cal, days=10, now=T0)
    by_tier = {r["tier"]: r for r in res["by_tier"]}
    check(res["numbers_marked"] == 3 and by_tier["Dial First"]["correct"] == 1 and by_tier["Dial First"]["correct_share"] == 100.0
          and by_tier["Dial Second"]["wrong"] == 1 and by_tier["Drop"]["correct_share"] == 0.0,
          "each marked number is counted once, by its latest mark, under the tier it carries")
    check("correct share" in calibration.text(res) and (Path(os_dir) / "calibration").is_dir() and not crm_cal.writes,
          "the calibration table is written to the box and nothing is written to the CRM")

    # ------------------------------------------------------------------ the two auction tiers
    print("  auction tiers")
    pol_a = policy.parse({**copy.deepcopy(PLAYBOOK), "stages": {**copy.deepcopy(PLAYBOOK["stages"]), "k_auction": {
        "kind": "auction", "owner": "analysis", "mode": "live", "schedule": "07:45 daily", "scope_tags": ["FTM", "Courthouse Data"],
        "soon_days": 45, "critical_tag": "Auction Critical", "critical_days": 14, "after_sale_status": "Auction Date Passed"}}},
        EMPLOYEES)
    today_et = T0.astimezone(ET).date()
    au_far = _rec("au1", "131 Sale St")
    au_near = _rec("au2", "132 Sale St")
    au_past = _rec("au3", "133 Sale St")
    au_lead = _rec("au4", "134 Sale St", status="Hot Lead")
    crm_au = FakeCRM([au_far, au_near, au_past, au_lead])
    crm_au.custom = {"au1": {"Foreclosure Date": (today_et + timedelta(days=30)).isoformat()},
                     "au2": {"Foreclosure Date": (today_et + timedelta(days=9)).isoformat()},
                     "au3": {"Foreclosure Date": (today_et - timedelta(days=1)).isoformat()},
                     "au4": {"Foreclosure Date": (today_et - timedelta(days=2)).isoformat()}}
    sau = sweep.run("k_auction", crm=crm_au, now=T0, pol=pol_a)
    check("Auction Soon" in crm_au.recs["au1"]["tags"] and "Auction Critical" not in crm_au.recs["au1"]["tags"]
          and {"Auction Soon", "Auction Critical"} <= set(crm_au.recs["au2"]["tags"]),
          "a sale 30 days out is hot; a sale 9 days out is hot and critical")
    check(crm_au.recs["au3"]["status"] == "Auction Date Passed" and crm_au.recs["au4"]["status"] == "Hot Lead",
          "the day after the sale the record is closed out, and a lead is left to the lead manager")
    check("2 sale(s) inside 45 days, 1 of them inside 14" in sau.head and not gate.check(crm_au.record("au3"), "call").ok,
          "the summary counts both tiers, and the gate then stops every channel on the passed sale")

    # ------------------------------------------------------------------ one-record proofs
    print("  one-record proofs")
    import os
    from .autopilot import proofs
    os.environ["AGENTS_DATA_DIR"] = str(tmp)
    try:
        pr = _rec("pr1", "111 Proof St", phones=[dict(MOBILE1)], predictivecall_attempts=2)
        crm_p = FakeCRM([pr])
        dry = proofs.run(crm_p, "pr1")
        check(not [w for w in crm_p.writes] and all(r["ok"] is not False for r in dry), "a dry proof run writes nothing")
        live = proofs.run(crm_p, "pr1", commit=True)
        by = {r["proof"]: r for r in live}
        check(all(by[k]["ok"] for k in ("call_counter", "sms_counter", "skiptrace_counter", "tag_round_trip", "status_round_trip",
                                         "scoped_trace_estimate")),
              "each proof writes one value, reads it back and restores it")
        after = crm_p.recs["pr1"]
        check(after["predictivecall_attempts"] == 2 and "Proof Test" not in after["tags"] and after["status"] is None
              and after["owner"].get("skiptrace_attempts") == 0, "the record is left exactly as it was")
        crm_p.counter_broken = True
        check(proofs.run(crm_p, "pr1", commit=True, only=["call_counter"])[0]["ok"] is False, "a write that does not stick fails its proof")
        crm_p.counter_broken = False
        crm_p.scope_ok = False
        check(proofs.run(crm_p, "pr1", commit=True, only=["scoped_trace_estimate"])[0]["ok"] is False,
              "a skip trace estimate that covers more than the one record fails its proof")
        check(proofs.main(["run", "--key-env", "NO_SUCH_KEY_NAME", "--record", "pr1"]) == 2, "a proof run needs the target account's key named by its variable")
        check((tmp / "proofs").is_dir() and any((tmp / "proofs").iterdir()), "every proof result is written to the proof log")
    finally:
        os.environ.pop("AGENTS_DATA_DIR", None)

    # ------------------------------------------------------------------ the health page and its outside watchdog
    print("  health page and outside watchdog")
    import importlib.util
    import json as _json
    from datetime import datetime as _dtm, timezone as _tz
    from . import brain
    pub = brain.public_last({"at": "2026-11-13T12:20:00+00:00", "live": False, "found": 9, "acted": 2, "usd": 0.0,
                             "would_usd": 0.0, "head": "9 lead(s), for example 12 Invented St",
                             "problems": ["no confirmed number at 12 Invented St"]})
    check("head" not in pub and pub["problems"] == 1 and pub["found"] == 9 and "Invented" not in _json.dumps(pub),
          "the public health page carries a run's counts, never its sentences")
    hb_path = Path(__file__).resolve().parents[2] / "deploy" / "agents_heartbeat.py"
    if hb_path.exists():                                    # the Fly image ships no deploy folder
        spec = importlib.util.spec_from_file_location("agents_heartbeat_under_test", hb_path)
        hb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hb)
        at_now = _dtm(2026, 11, 13, 14, 30, tzinfo=_tz.utc)

        def page(**over):
            base = {"ok": True, "worker_alive": True, "clock_alive": True,
                    "autopilot": {"_requests": {"hour_refusals_today": 0},
                                  "stage_a": {"mode": "plan", "last": {"at": "2026-11-13T12:20:00+00:00", "found": 3}},
                                  "stage_b": {"mode": "plan", "last": {}},
                                  "stage_off": {"mode": "off", "last": {}}}}
            base.update(over)
            return base

        v = hb.verdict(page(), at_now)
        check(v[0] == "ok" and "1 of 2 stages" in v[1], "the watchdog passes a live brain and leaves switched-off stages out of the count")
        check(hb.verdict(page(worker_alive=False), at_now)[0] == "red" and hb.verdict(page(clock_alive=False), at_now)[0] == "red",
              "a page that answers while its worker or its clock is dead is red")
        old_page = page()
        old_page["autopilot"]["stage_a"]["last"]["at"] = "2026-11-11T12:20:00+00:00"
        check(hb.verdict(old_page, at_now) == ("red", "No stage has run in the last 30 hours."), "no stage run in 30 hours is red")
        never = page()
        never["autopilot"]["stage_a"]["last"] = {}
        check(hb.verdict(never, at_now)[0] == "red" and "ever run" in hb.verdict(never, at_now)[1], "a box whose stages never ran is red")
        check(hb.verdict(page(autopilot={"error": "playbook missing"}), at_now)[0] == "red"
              and hb.verdict(["not", "a", "page"], at_now)[0] == "unreadable" and hb.verdict(page(ok=False), at_now)[0] == "red",
              "a playbook that did not load, a page that is not an object and a not-ok page are never a pass")
        live_shape = page()
        live_shape["autopilot"]["stage_a"]["last"] = brain.public_last({"at": "2026-11-13T12:20:00+00:00", "head": "x", "problems": []})
        check(hb.verdict(live_shape, at_now)[0] == "ok", "the watchdog reads the same shape the health page really serves")
