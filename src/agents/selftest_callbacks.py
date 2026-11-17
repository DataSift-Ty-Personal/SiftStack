"""The morning callback list (the lead manager's day starts by clearing every missed call, voicemail
and text). Zero network: events seeded into the log copy, a fake CRM, its own database. Every
number, street and name here is invented; the numbers use the never-assigned 865-000 exchange.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES

NOW = datetime(2026, 11, 16, 13, 15, tzinfo=timezone.utc)          # Monday 8:15am Eastern


def _ev(i, ts, kind, owner, direction="inbound", duration=0, recording=""):
    if kind.startswith("owner.sms."):
        payload = {"sms": {"direction": direction, "origin_number": "8650000001", "destination_number": "8650000099"}}
    else:
        payload = {"call": {"direction": direction, "origin_number": "8650000001", "destination_number": "8650000099",
                            "duration": duration, "recording": recording}}
    return {"id": "cb%d" % i, "timestamp": ts, "event_type": kind, "resource": owner, "payload": payload}


class FakeCRM:
    def users(self):
        return [{"uuid": "u1", "first_name": "Emerson"}]

    def owner_records(self, owner, cap=5):
        return [{"uuid": "r-" + owner}]

    def record(self, u):
        return {"uuid": u, "address": {"street": "1%s Example St" % u[-1], "city": "Knoxville"}, "assigned_to": "u1"}


def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DATA_DIR, config.DB_PATH)
    config.DATA_DIR = Path(tmp) / "callbacks"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    try:
        _checks(check)
    finally:
        config.DATA_DIR, config.DB_PATH = keep


def _checks(check) -> None:
    from .autopilot import activity, callbacks, policy, sweep
    start = callbacks.window_start(NOW, "America/New_York", (540, 1080), {0, 1, 2, 3, 4})
    check(start.isoformat() == "2026-11-13T18:00:00-05:00", "on a Monday the window opens at the end of Friday's hours")
    activity._store([
        _ev(1, "2026-11-16 00:42:00", "owner.call.missed", "own-a"),                       # Sunday 7:42pm
        _ev(2, "2026-11-16 12:05:00", "owner.sms.received", "own-b"),                      # Monday 7:05am
        _ev(3, "2026-11-14 15:00:00", "owner.call.missed", "own-c"),                       # Saturday, then called back
        _ev(4, "2026-11-16 12:30:00", "owner.call.made", "own-c", direction="outbound"),
        _ev(5, "2026-11-14 16:00:00", "owner.call.received", "own-d", duration=120),       # somebody picked up
        _ev(6, "2026-11-13 22:00:00", "owner.call.missed", "own-e"),                       # Friday 5pm, inside hours
        _ev(7, "2026-11-15 23:10:00", "owner.call.missed", "own-f", recording="rec"),      # Sunday 6:10pm voicemail
    ])
    waiting = callbacks.waiting(start, NOW)
    check(sorted(waiting) == ["own-a", "own-b", "own-f"],
          "the list holds missed calls, voicemails and texts nobody answered; a called-back owner, an answered call "
          "and anything from inside Friday's hours are left off")
    pb = {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
          "caps": {"daily_usd": 25, "record_usd": 1.0}, "people": {"callers": ["Emerson"]},
          "stages": {"cb": {"kind": "callbacks", "owner": "sales", "mode": "plan", "schedule": ["08:15 Mon-Fri"], "sync": False}}}
    out = sweep.run("cb", crm=FakeCRM(), now=NOW, pol=policy.parse(pb, EMPLOYEES))
    text = "\n".join([out.head] + out.lines)
    check(out.head.startswith("Call back first: 3 owners") and "Friday 6pm" in out.head,
          "the head says how many are waiting and since when")
    check("missed call 7:42pm, Emerson" in text and "text 7:05am" in text and "voicemail 6:10pm" in text,
          "each line has the house, what came in and when, and who has the record")
    posts = sweep.posts({"id": 1, "workflow": "cb", "source": "schedule"}, out.as_dict())
    check([p["kind"] for p in posts] == ["pipeline"], "the callback list goes to the pipeline channel")
    quiet = sweep.run("cb", crm=FakeCRM(), now=datetime(2026, 11, 10, 13, 15, tzinfo=timezone.utc), pol=policy.parse(pb, EMPLOYEES))
    check(sweep.posts({"id": 2, "workflow": "cb", "source": "schedule"}, quiet.as_dict()) == [],
          "a quiet morning posts nothing")
