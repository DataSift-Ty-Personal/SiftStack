"""Who wrote a log row: our own key, a person in the app, or a row stored before the two were told apart.

Measured on prod-account 2026-11-15: a person working in the DataSift app is logged as source internal-api with
their own email as the author, exactly like our scripts, which write through the API key under the key's
own user. The log copy used to store both as "user", so a note a person typed in the app never counted
as a person's note (lead clock, weekly lead tasks) and every status a person set in the app was thrown
out with our own (the neighborhood check). Zero network: a fake CRM, its own database. Every name,
email and id here is invented.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

SELF = "auto.key@example.test"
PERSON = "pat.caller@example.test"
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def _note(i, ts, resource, author, source="internal-api"):
    return {"id": "la%d" % i, "timestamp": ts, "event_type": "property.message.added", "resource": resource,
            "resource_type": "property", "source": source, "author": author, "payload": {}}


def _status(i, ts, resource, to, author, source="internal-api"):
    return {"id": "ls%d" % i, "timestamp": ts, "event_type": "property.status.updated", "resource": resource,
            "resource_type": "property", "source": source, "author": author,
            "payload": {"property": {"status": ["prospecting", to]}}}


class FakeCRM:
    """The two routes a log sync uses: the key's own user, and one page of the log."""

    def __init__(self, rows, email=SELF, can_call=True):
        self.rows, self.email, self.user_calls = rows, email, 0
        if not can_call:
            self.call = None

    def call(self, path, method="GET", body=None, headers=None):  # noqa: D401  (the CRM's own signature)
        assert path == "/api/internal/user/" and method == "GET", path
        self.user_calls += 1
        return {"email": self.email, "first_name": "Auto"}

    def logs(self, must, limit=100, cursor=""):
        types = set(must.get("any_event_type") or [])
        return {"results": [r for r in self.rows if r["event_type"] in types], "next_cursor": ""}


def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DATA_DIR, config.DB_PATH)
    config.DATA_DIR = Path(tmp) / "logauthor"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    try:
        _rules(check)
        _sync(check)
        _readers(check)
    finally:
        config.DATA_DIR, config.DB_PATH = keep


def _rules(check) -> None:
    from .autopilot import activity as a
    print("  who wrote it")
    check(a._author(SELF, SELF) == "self" and a._author("Auto.Key@Example.TEST", SELF) == "self",
          "our own key's email is stored as self, whatever its case")
    check(a._author(PERSON, SELF) == "person", "anyone else's email is a person")
    check(a._author(PERSON, "") == "user", "with our own email unknown, an email stays the old, ambiguous user")
    check(a._author("system", SELF) == "system" and a._author("", SELF) == "", "a non-email author is kept as it was")
    rows = [({"author": "person", "source": "internal-api"}, True), ({"author": "self", "source": "internal-api"}, False),
            ({"author": "user", "source": "internal-api"}, False), ({"author": "person", "source": "sequence"}, False),
            ({"author": "user", "source": "kixie"}, True), ({"author": "system", "source": "kixie"}, False),
            ({"author": "self", "source": "kixie"}, False)]
    check(all(a.by_person(e) is want for e, want in rows),
          "a person in the app counts; our own key, a sequence and a system row never do; a row stored before the split "
          "reads as before (internal-api was a machine)")
    check(a.by_person({"author": "user", "source": "internal-api"}, old_internal_api_is_person=True)
          and not a.by_person({"author": "self", "source": "internal-api"}, old_internal_api_is_person=True),
          "a stage that always counted internal-api as a person keeps that for old rows, but never for our own key")


def _sync(check) -> None:
    from .autopilot import activity as a
    from .autopilot import leadroutes as routes      # registers the notes group
    print("  the log copy")
    rows = [_note(1, "2026-11-16 13:00:00", "rec-1", SELF), _note(2, "2026-11-16 13:05:00", "rec-1", PERSON),
            _note(3, "2026-11-16 13:10:00", "rec-2", "system", source="sequence")]
    crm = FakeCRM(rows)
    a.sync(crm, "notes", now=NOW, max_calls=5)
    got = {e["id"]: e["author"] for e in a.events(routes.NOTE_EVENTS)}
    check(got == {"la1": "self", "la2": "person", "la3": "system"},
          "the sync stores our own key's note as self and a person's app note as person")
    a.sync(crm, "notes", now=NOW + timedelta(hours=1), max_calls=5)
    check(crm.user_calls == 1, "the key's own user is read once a day, not once a sync")
    from . import store
    store.kv_set(a.SELF_KV, "")
    blind = FakeCRM([_note(4, "2026-11-16 14:00:00", "rec-3", PERSON)], can_call=False)
    a.sync(blind, "notes", now=NOW + timedelta(hours=2), max_calls=5)
    check({e["id"]: e["author"] for e in a.events(routes.NOTE_EVENTS)}.get("la4") == "user",
          "a client that cannot say who the key is stores the old ambiguous user, and the sync does not fail")


def _readers(check) -> None:
    from .autopilot import activity as a
    from .autopilot import bizaudit, leadclock, leadtasks, suppression
    print("  the stages that read it")
    since = NOW - timedelta(days=1)
    check(leadtasks._note_since("rec-1", "", since) and not leadtasks._note_since("rec-2", "", since),
          "the weekly lead tasks see a note a person typed in the app, and not a sequence's")
    a._store([_note(5, "2026-11-16 13:20:00", "rec-9", SELF)], SELF)
    check(not leadtasks._note_since("rec-9", "", since), "a note our own key wrote is never a person's note")
    when, how = leadclock.last_conversation("", "rec-1", since=since)
    check(how == "note" and when is not None, "the lead clock counts a person's app note as a touch")
    check(bizaudit._person_note({"author": "person", "source": "internal-api"})
          and not bizaudit._person_note({"author": "self", "source": "internal-api"}),
          "the business audit counts a person's app note and never our own key's")
    a._store([_status(1, "2026-11-16 13:30:00", "rec-p", "Dead Lead", PERSON),
              _status(2, "2026-11-16 13:31:00", "rec-s", "Dead Lead", SELF)], SELF)
    a._store([_status(3, "2026-11-16 13:32:00", "rec-l", "Dead Lead", PERSON)], "")      # stored before the split
    out = suppression.outcomes(suppression.settings({}), since, NOW + timedelta(hours=3))
    check("rec-p" in out and out["rec-p"]["bad"] and "rec-s" not in out and "rec-l" not in out,
          "the neighborhood check counts a status a person set in the app, never one our key put back, and reads an "
          "old ambiguous row as before")
