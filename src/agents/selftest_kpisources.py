"""KPI sources selftest: the schema, connections, the four adapters (the DataSift log copy, the ledger, files and
the agency's kept files, the webhook receiver with its Kixie and Zapier mappings) and collect()'s dedupe and
counting rules. Zero network: the brain's own tables on a throwaway database, fake log rows, fake deliveries.
Every name, email, street, number and id here is invented; phone numbers sit on the 865-555, 865-000 and 865-001
exchanges.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc)          # Monday, 4:00 PM Eastern
T = datetime(2026, 11, 16, 19, 2, 11, tzinfo=timezone.utc)         # 2:02:11 PM Eastern
DAY_START = datetime(2026, 11, 16, 0, 0, tzinfo=ET)
DAY_END = datetime(2026, 11, 17, 0, 0, tzinfo=ET)
SELF = "auto.key@example.test"
PERSON = "pat.caller@example.test"
SECRET = "hook-secret-test"
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U5 = "55555555-5555-4555-8555-555555555555"
ENV_KEYS = ("AGENTS_KPI_HOOK_SECRET", "AGENTS_KPI_SIGN_TEST", "AGENTS_KPI_TZ", "AGENTS_KPI_AGREE",
            "AGENTS_KPI_CALL_MATCH_S", "AGENTS_KPI_TEXT_MATCH_S", "AGENTS_KPI_MARGIN_HOURS", "AGENTS_KPI_SILENT_MIN",
            "AGENTS_KPI_RAW_DAYS", "AGENTS_KPI_EVENT_DAYS", "AGENTS_KPI_ECHO_MINUTES", "AGENTS_KPI_LOG_SLACK_MINUTES",
            "AGENTS_KPI_HOOK_MAX_BYTES", "AGENTS_KPI_PARSE_BATCH", "AGENTS_AGENCY_RETURNS_DIR")


# ---------------------------------------------------------------------- fakes
def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def call_row(i, kind, owner, when, number, uuid, duration=0, inbound=False, caller="", source="kixie"):
    return {"id": "lc-%s" % i, "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": source, "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"destination_number": "8650000001" if inbound else number,
                                 "origin_number": number if inbound else "8650000001",
                                 "direction": "inbound" if inbound else "outbound", "duration": duration, "source": source,
                                 "uuid": uuid, "extra": {}, "user": {"first_name": caller}}}}


def sms_row(i, kind, owner, when, number, inbound=False):
    return {"id": "ls-%s" % i, "event_type": "owner.sms.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"sms": {"destination_number": "8650000001" if inbound else number,
                                "origin_number": number if inbound else "8650000001",
                                "direction": "inbound" if inbound else "outbound", "status": kind, "source": "kixie"}}}


def phone_row(i, owner, when, number, new, author="system", source="kixie"):
    return {"id": "lp-%s" % i, "event_type": "owner.phone.status.updated", "resource": owner, "resource_type": "owner",
            "source": source, "author": author, "timestamp": _ts(when),
            "payload": {"owner": {"phone": number, "status": ["UNKNOWN", new]}}}


def status_row(i, rec, when, old, new, author=PERSON, source="internal-api"):
    return {"id": "lt-%s" % i, "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": source, "author": author, "timestamp": _ts(when), "payload": {"property": {"status": [old, new]}}}


def task_row(i, rec, when, kind):
    return {"id": "lk-%s" % i, "event_type": "task.created", "resource": "task-%s" % i, "resource_type": "task",
            "source": "internal-api", "author": PERSON, "timestamp": _ts(when),
            "payload": {"task": {"assigned_to_property": rec, "title": "Walk the house", "event_type": kind}}}


class FakeCatalog:
    """The catalog's conversation definition and nothing else."""

    def __init__(self, min_seconds=60, texts=True, calls=True):
        self.d = {"min_seconds": min_seconds, "counts_inbound_text": texts, "counts_inbound_call": calls}

    def definition(self, name):
        assert name == "conversation", name
        return dict(self.d)


def _raises(fn) -> str:
    try:
        fn()
    except ValueError as exc:
        return str(exc) or "raised"
    return ""


# ---------------------------------------------------------------------- the section
def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DATA_DIR, config.DB_PATH)
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    config.DATA_DIR = Path(tmp) / "kpisources"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["AGENTS_AGENCY_RETURNS_DIR"] = str(config.DATA_DIR / "agency_returns")
    store.init()
    from .kpi_sources import registry
    saved_adapters = dict(registry.ADAPTERS)
    try:
        _schema(check)
        _connections(check)
        _datasift(check)
        _ledger(check)
        _files(check)
        _agency(check)
        _webhook(check)
        _merge(check)
        _collect(check)
        _conversations(check)
    finally:
        registry.ADAPTERS.clear()
        registry.ADAPTERS.update(saved_adapters)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.DATA_DIR, config.DB_PATH = keep


def _schema(check) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import file_import, registry, webhook
    print("  the schema")
    check("conversation" not in ks.KINDS and set(ks.KINDS) == {"call", "text", "phone_status", "mail", "stage", "spend"},
          "the event kinds are call, text, phone status, mail, stage and spend, and there is no conversation kind")
    said = _raises(lambda: ks.Event(kind="conversation", at=T, source="x", source_id="1"))
    check("catalog" in said, "an event of kind conversation is refused, with the reason that the catalog derives it")
    check(_raises(lambda: ks.Event(kind="call", at=datetime(2026, 11, 16, 14, 0), source="x", source_id="1")) != "",
          "an event whose time carries no zone is refused: each adapter normalizes its own clock")
    check(_raises(lambda: ks.Event(kind="stage", at=T, source="x", source_id="1")) != "",
          "a stage event with no stage named is refused")
    check(_raises(lambda: ks.Event(kind="call", at=T, source="", source_id="1")) != "",
          "an event with no source is refused")
    e = ks.Event(kind="call", at=T.astimezone(ET), source="x", source_id="1", phone="+1 (865) 555-0101",
                 direction="Outgoing", tool="Smarter-Contact")
    check(e.phone == "8655550101" and e.direction == "out" and e.at.tzinfo == timezone.utc and e.tool == "smartercontact",
          "an event keeps 10 digits, out or in, UTC and one spelling of the tool")
    check(ks.direction_of("INBOUND") == "in" and ks.direction_of("interested") == "",
          "a word that only starts like a direction is not one")
    check(e["kind"] == "call" and e.get("nothing", 7) == 7, "an event reads as a dict as well as an object")
    mine = ks.Event(kind="phone_status", at=T, source="x", source_id="2", phone="8655550101", outcome="CORRECT_DNC",
                    author="self", person_ref="Pat")
    check(mine.person_ref == "" and mine.outcome == "correct_dnc",
          "a row our own key wrote never carries a person, and a phone status reads in one spelling")
    back = ks.Event.from_dict(json.loads(json.dumps(e.to_dict())))
    check(back.phone == e.phone and back.at == e.at and back.members == e.members, "an event survives being stored")
    for word in ("conversations", "contacts", "connects", "Conversation Rate"):
        check(_raises(lambda w=word: ks.Tally(metric=w, period="2026-11", value=1, source="x")) != "",
              "a tally named %s is refused: it is the source's own word, kept as reported" % word)
    check(_raises(lambda: ks.Tally(metric="as_reported", period="2026-11", value=1, source="x")) != "",
          "a figure kept as reported must carry the source's own word for it")
    check(_raises(lambda: ks.Tally(metric="dials", period="2026-13", value=1, source="x")) != "",
          "a tally's period is a real day or month")
    t = ks.Tally(metric="as_reported", period="2026-11-16", value=30, source="x", definition="Connects")
    check(t.metric == "as_reported" and t.definition == "Connects", "a vendor's own figure is kept under its own word")
    check(_raises(lambda: ks.Provides("conversation", "webhook")) != "" and _raises(lambda: ks.Provides("call", "password")) != ""
          and _raises(lambda: ks.Provides("call", "file", frozenset({"secret"}))) != "",
          "a declaration naming a conversation, an unknown mode or a field an event lacks is refused")
    check(set(ks.MODES) == {"api_key", "oauth", "webhook", "cloud_login", "file"} and "never a password" in ks.MODES["cloud_login"]
          and "switches it on" in ks.MODES["cloud_login"],
          "the five ways in are declared, and a cloud login holds a session, never a password, off until switched on")
    check(registry.problems() == [], "every registered adapter loads and declares what it provides: %s" % (registry.problems() or "ok"))
    shipped = [registry.adapter(n) for n in registry.ADAPTERS]
    check(not any("cloud_login" in a.modes() for a in shipped), "no cloud login adapter ships: the owner decides those")
    check(all(p.kind in ks.KINDS for a in shipped for p in a.provides),
          "no adapter declares anything outside the six kinds")
    check(not any("convers" in w for w in file_import.KIND_WORDS) and not any("convers" in w for w in webhook.KIND_WORDS),
          "no file column and no webhook can name a conversation as a kind")
    at, exact = ks.parse_when("2026-11-16T14:02:11-05:00")
    check(at == T and exact, "an ISO time with its offset reads to the second")
    at, exact = ks.parse_when("11/16/2026 2:02:11 PM", "America/New_York")
    check(at == T and exact, "a dialer's local time reads in the zone the source declares")
    at, exact = ks.parse_when("Nov 16, 2026 2:02 PM", "America/New_York")
    check(at == T.replace(second=0) and exact, "a written-out date and time reads")
    at, exact = ks.parse_when("2026-11-16", "America/New_York")
    check(at is not None and not exact and ks.local_day(at, "America/New_York") == "2026-11-16",
          "a day with no time is placed on that local day and marked inexact, never moved across midnight")
    at, exact = ks.parse_when(int(T.timestamp() * 1000))
    check(at == T and exact, "epoch milliseconds read as UTC")
    check(ks.parse_when("soon")[0] is None and ks.parse_when(None)[0] is None, "something that is not a time reads as none")
    check(ks.seconds_of("1:15") == 75 and ks.seconds_of("00:01:15") == 75 and ks.seconds_of("1m 15s") == 75
          and ks.seconds_of("75") == 75 and ks.seconds_of("") is None and ks.seconds_of("about a minute") is None,
          "talk time reads from the shapes tools write, and an unknown length is none, never zero")
    check(ks.answered_of("voicemail") is True and ks.answered_of("no-answer") is False and ks.answered_of("Not Interested") is None,
          "a voicemail greeting counts as answered (the catalog), no answer is no, and a disposition cannot say")
    a = ks.Event(kind="call", at=T, source="kx", source_id="id:K1", direction="out", phone="8655550101", tool="kixie")
    b = ks.Event(kind="call", at=T + timedelta(seconds=80), source="kx", source_id="id:K1", seconds=75, answered=True,
                 outcome="Not Interested", person_ref=PERSON)
    c = ks.Event(kind="call", at=T + timedelta(seconds=90), source="kx", source_id="id:K1", outcome="Callback")
    f = ks.fold(ks.fold(a, b), c)
    check(f.at == T and f.phone == "8655550101" and f.seconds == 75 and f.answered is True and f.outcome == "callback"
          and f.person_ref == PERSON,
          "a call's later reports fill the same act: nothing filled goes back to empty, and the outcome is the latest")


def _fake_login():
    """A cloud login adapter the selftest stands in, to prove the switch; no such adapter ships."""
    from .kpi_sources import Adapter, Provides

    class _Login(Adapter):
        id = "fake_login"
        provides = (Provides("call", "cloud_login", frozenset({"seconds"}), latency="daily"),)
        terms_note = "Its terms forbid robots without written consent."
    return _Login()


FAKE_LOGIN = None


def _connections(check) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import registry
    print("  connections")
    on = {c["id"]: c for c in ks.connections()}
    check({"datasift", "ledger", "agency"} <= set(on) and on["agency"]["settings"].get("preset") == "agency",
          "the log copy, the ledger and the agency's kept files are on with nothing to set up")
    check(ks.connect("kixie", "webhook", tool="Kixie", settings={"mapping": "kixie"}, now=datetime(2026, 11, 1, tzinfo=timezone.utc)) == [],
          "a Kixie webhook connects with its mapping")
    check(ks.connection("kixie")["tool"] == "kixie" and ks.connection("kixie")["enabled"], "the connection is saved and on")
    check(ks.connect("Bad Name", "webhook", settings={"mapping": "kixie"}) != [], "a connection's name is plain lower case")
    check(ks.connect("zz", "nothing_like_it") != [], "an adapter that is not registered is refused")
    said = ks.connect("zz", "webhook", settings={"mapping": "kixie", "password": "x"})
    check(said != [] and "password" in said[0] and ks.connection("zz") is None, "a connection never holds a password")
    check(ks.connect("zz", "webhook", settings={"mapping": "kixie", "auth": {"user_passwd": "x"}}) != [],
          "a password hidden one level down is found too")
    check(ks.connect("datasift", "webhook", settings={"mapping": "kixie"}) != [], "a built-in source's name cannot be taken")
    check(ks.connect("zz", "webhook", settings={"mapping": "nothing"}) != [], "a webhook needs a mapping that exists")
    check(ks.connect("zz", "webhook", settings={"mapping": "zapier",
                                                "signature": {"header": "X-Sig", "secret_env": "X", "secret": "abc"}}) != [],
          "a signature names the setting that holds its secret, never the secret itself")
    check(ks.connect("zz", "file_import", settings={"rows": "chats"}) != [] and
          ks.connect("zz", "file_import", settings={"columns": {"bogus": "x"}}) != [] and
          ks.connect("zz", "datasift_log", settings={"stages": {"won": {}}}) != [],
          "a file's kind, a column map and a stage map are checked when the connection is made")
    check(ks.disconnect("ledger") == [] and "ledger" not in {c["id"] for c in ks.connections()},
          "a built-in source can be switched off")
    check(ks.connect("ledger", "ledger_spend") == [] and "ledger" in {c["id"] for c in ks.connections()},
          "and switched back on")
    registry.ADAPTERS["fake_login"] = ("agents.selftest_kpisources", "FAKE_LOGIN")
    globals()["FAKE_LOGIN"] = _fake_login()
    said = ks.connect("crm_login", "fake_login", tool="crmx")
    check(said and "switched off" in said[0] and not ks.connection("crm_login")["enabled"]
          and "written consent" in said[0],
          "a cloud login is saved switched off, quoting the tool's automation clause")
    check(ks.connect("crm_login", "fake_login", tool="crmx", settings={"terms_seen": "2026-11-16"}) == []
          and ks.connection("crm_login")["enabled"],
          "it goes on only once the owner has read that clause")
    globals()["FAKE_LOGIN"].terms_note = ""
    check(any("automation clause" in p for p in registry.problems()),
          "a cloud login adapter with no automation clause fails the registry check")
    ks.disconnect("crm_login")
    del registry.ADAPTERS["fake_login"]


def _log(rows, through=None):
    from . import store
    from .autopilot import activity
    activity._store(rows, SELF)
    for g in ("calls", "texts", "state", "board"):
        store.kv_set("autopilot:log:%s:through" % g, (through or NOW).isoformat(timespec="seconds"))


def _change(at, stage, rec, what, after):
    from . import store
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_changes (at, run_id, stage, record_uuid, what, before, after) VALUES (?,?,?,?,?,?,?)",
                  (at.isoformat(timespec="seconds"), "run-x", stage, rec, what, "null", json.dumps(after)))


def _datasift(check) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import datasift_log
    print("  the DataSift log copy")
    conn = ks.connection("datasift")
    b = datasift_log.ADAPTER.pull(conn, DAY_START, DAY_END, ("call",), now=NOW)
    check(not b.complete and any("not been read yet" in p for p in b.problems),
          "a log copy never read is a problem, never a quiet day")
    rows = [
        call_row(1, "made", "own-1", T + timedelta(seconds=2), "8655550101", "c-1"),
        call_row(2, "answered", "own-1", T + timedelta(seconds=9), "8655550101", "c-1", duration=74, caller="Pat"),
        call_row(3, "made", "own-1", T + timedelta(minutes=8), "8655550102", "c-2"),
        call_row(4, "noanswer", "own-1", T + timedelta(minutes=8, seconds=40), "8655550102", "c-2-other"),
        call_row(5, "received", "own-3", T + timedelta(minutes=28), "8655550103", "c-3", duration=90, inbound=True,
                 caller="Pat"),
        {"id": "lx-1", "event_type": "property.predictivecall.attempts.increased", "resource": "rec-1",
         "resource_type": "property", "source": "sequence", "author": "system", "timestamp": _ts(T),
         "payload": {"property": {"predictivecall_attempts": [0, 1]}}},
        phone_row(1, "own-1", T + timedelta(seconds=40), "8655550101", "CORRECT"),
        sms_row(1, "sent", "own-2", T + timedelta(minutes=38), "8655550104"),
        sms_row(2, "delivered", "own-2", T + timedelta(minutes=39), "8655550104"),
        sms_row(3, "sent", "own-2", T + timedelta(minutes=50), "8655550106"),
        sms_row(4, "failed", "own-2", T + timedelta(minutes=51), "8655550106"),
        sms_row(5, "received", "own-2", T + timedelta(minutes=55), "8655550104", inbound=True),
        status_row(1, "rec-1", T, "prospecting", "new_lead"),
        status_row(2, "rec-1", T + timedelta(minutes=5), "new_lead", "Hot Lead"),
        status_row(3, "rec-2", T, "follow_up", "under_contract"),
        status_row(4, "rec-3", T, "transactions", "closed"),
        status_row(5, "rec-6", T, "prospecting", "offer_made"),
        task_row(1, "rec-4", T, "appointment"),
        task_row(2, "rec-4", T, "call"),
        status_row(6, U5, T + timedelta(minutes=1), "prospecting", "new_lead", author=SELF),
        phone_row(2, "own-5", T + timedelta(minutes=1), "8655550105", "CORRECT", author=SELF, source="internal-api"),
    ]
    _change(T, "agency_results", U5, "status", "new_lead")
    _change(T, "agency_results", U5, "phones", ["8655550105"])
    _log(rows, through=NOW - timedelta(hours=5))
    b = datasift_log.ADAPTER.pull(conn, DAY_START, DAY_END, ("call",), now=NOW)
    check(not b.complete and any("only through" in p for p in b.problems), "a log copy hours behind says how far it reaches")
    _log([], through=NOW)
    from .autopilot import activity
    old = T + timedelta(minutes=3)              # rows stored before the author split read "user", whoever wrote them
    activity._store([status_row(7, "rec-old", old, "prospecting", "new_lead", author=SELF),
                     status_row(8, "rec-old2", old, "prospecting", "new_lead", author=PERSON)], "")
    _change(old + timedelta(seconds=30), "agency_results", "rec-old", "status", "new_lead")
    b = datasift_log.ADAPTER.pull(conn, DAY_START, DAY_END, ("call", "text", "phone_status", "stage"), now=NOW)
    check(b.complete and b.problems == [], "a current log copy reads complete")
    olds = {e.record: e for e in b.events if e.kind == "stage" and e.record.startswith("rec-old")}
    check(olds["rec-old"].author == "self" and olds["rec-old"].via == "agency_results"
          and olds["rec-old2"].author == "user" and olds["rec-old2"].via == "",
          "an old row the ledger shows our key wrote is ours; one with no write of ours keeps its old reading")
    acts, _ = ks.merge(b.events, {"datasift": conn})
    calls = [a for a in acts if a.kind == "call"]
    one = [a for a in calls if a.phone == "8655550101"]
    check(len(one) == 1 and one[0].answered is True and one[0].seconds == 74 and one[0].person_ref == "Pat"
          and one[0].owner == "own-1" and one[0].tool == "kixie" and one[0].at == T + timedelta(seconds=2),
          "a dial and its answer are one call: answered, 74 seconds, the caller from the answer, timed at the dial")
    two = [a for a in calls if a.phone == "8655550102"]
    check(len(two) == 1 and two[0].answered is False,
          "an answer whose call id matches no dial joins the open dial to the same number, so one dial never reads as two")
    back = [a for a in calls if a.direction == "in"]
    check(len(back) == 1 and back[0].answered is True and back[0].seconds == 90, "an inbound call is a call in, answered")
    check(len(calls) == 3, "three calls from five call rows, and the attempt counter is never read as a dial")
    texts = [a for a in acts if a.kind == "text"]
    check(sorted((a.direction, a.status) for a in texts) == [("in", "received"), ("out", "delivered"), ("out", "failed")],
          "a text's delivery and its failure fold into the text sent: three texts, not five")
    ps = [a for a in acts if a.kind == "phone_status"]
    kx = [a for a in ps if a.phone == "8655550101"]
    check(len(kx) == 1 and kx[0].outcome == "correct" and kx[0].tool == "kixie",
          "a phone status keeps the tool that wrote it, so a number confirmed on a call is told apart")
    mine = [a for a in ps if a.phone == "8655550105"]
    check(len(mine) == 1 and mine[0].author == "self" and mine[0].via == "agency_results" and mine[0].person_ref == "",
          "a phone status our own key wrote is marked ours, with the workflow the ledger shows made it")
    stages = sorted((a.record, a.stage) for a in acts if a.kind == "stage")
    check(("rec-1", "lead") in stages and stages.count(("rec-1", "lead")) == 1,
          "a record entering a lead status from outside one is a lead once; New Lead to Hot Lead is the same lead")
    check(("rec-2", "contract") in stages and ("rec-2", "lead") not in stages,
          "under contract from a lead status is a contract, not a new lead")
    check(("rec-3", "closed") in stages and ("rec-4", "appointment") in stages
          and sum(1 for r, s in stages if r == "rec-4") == 1,
          "a close and an appointment task count; a task of another kind does not")
    check(("rec-6", "offer") not in stages and any("Offers are not counted" in n for n in b.notes),
          "offers count nothing until the owner names what an offer is, and the source says so")
    selfie = [a for a in acts if a.kind == "stage" and a.record == U5]
    check(len(selfie) == 1 and selfie[0].author == "self" and selfie[0].via == "agency_results",
          "a lead our own key set is marked ours and linked to the agency workflow's write through the ledger")
    conn2 = dict(conn, settings={"stages": {"offer": {"statuses": ["Offer Made"]}}})
    b2 = datasift_log.ADAPTER.pull(conn2, DAY_START, DAY_END, ("stage",), now=NOW)
    check(any(e.record == "rec-6" and e.stage == "offer" for e in b2.events) and not b2.notes,
          "once the owner names the offer status, it counts, compared as plain words")


def _ledger(check) -> None:
    from . import kpi_sources as ks, store
    from .autopilot import ledger
    from .kpi_sources import ledger_spend
    print("  the ledger")
    when = T.astimezone(ET)                        # a stage's clock in Eastern time: its rows carry that offset
    ledger.event("run-a", "ftm_retrace", "rec-9", "trace", {"source": "smartskip", "found": 1}, usd=0.15, now=when)
    ledger.event("run-a", "ftm_score", "rec-9", "score", {"numbers": 3}, usd=0.045, now=when)
    ledger.event("run-a", "ftm_mail", "rec-9", "mail", {"piece": "handwritten_letter", "vendor": "csv", "series": "ftm"},
                 usd=1.89, now=when)
    ledger.event("run-a", "ftm_assign", "rec-9", "assign", {"to": "Pat"}, usd=0, now=when)
    tid, _ = store.enqueue("research", "skip_trace", "rec-9")
    with store._LOCK, store._db() as c:
        for usd, what in ((0.42, "claude claude-opus-5-5"), (0.15, "autopilot ftm_retrace: smartskip x1"),
                          (0.10, "zillow sold pull"), (0.0, "nothing")):
            c.execute("INSERT INTO spend (at, day, task_id, usd, what) VALUES (?,?,?,?,?)",
                      (T.isoformat(timespec="seconds"), "2026-11-16", tid, usd, what))
    b = ledger_spend.ADAPTER.pull(ks.connection("ledger"), DAY_START, DAY_END, ("spend", "mail"), now=NOW)
    spend = [e for e in b.events if e.kind == "spend"]
    tools = sorted((e.tool, round(e.amount, 3)) for e in spend)
    check(tools == [("claude", 0.42), ("mail", 1.89), ("smartskip", 0.15), ("trestle", 0.045), ("zillow", 0.1)],
          "spend comes from the ledger and the brain's spend table, each vendor named: %s" % tools)
    check(round(sum(e.amount for e in spend), 3) == 2.605,
          "the autopilot's copy in the spend table is skipped, so every dollar counts once")
    mail = [e for e in b.events if e.kind == "mail"]
    check(len(mail) == 1 and mail[0].status == "sent" and mail[0].outcome == "handwritten_letter" and mail[0].amount is None
          and mail[0].record == "rec-9", "a piece handed to a printer is one mail event, its cost only on the spend side")
    check(all(e.person_ref == "" for e in b.events), "spend and mail name no person")
    b = ledger_spend.ADAPTER.pull(ks.connection("ledger"), DAY_END, DAY_END + timedelta(days=1), ("spend",), now=NOW)
    check(b.events == [], "a ledger row stamped with an Eastern offset stays on its own day")


def _csv(rows) -> bytes:
    return ("\n".join(rows) + "\n").encode("utf-8")


def _files(check) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import file_import
    print("  files")
    fi = file_import.ADAPTER
    check(ks.connect("dialer_csv", "file_import", tool="aircall", settings={"rows": "call", "tz": "America/New_York"}) == [],
          "a dialer's call history connects as a file with no column map at all")
    conn = ks.connection("dialer_csv")
    first = _csv(["Call ID,Date,Direction,From,To,Duration,Disposition,Agent Email",
                  "A-1,11/16/2026 2:02:11 PM,Outgoing,8650000001,8655550201,1:15,Not Interested,%s" % PERSON,
                  "A-2,11/16/2026 2:30 PM,Outgoing,8650000001,8655550202,0:00,No Answer,%s" % PERSON,
                  ",11/16/2026 3:00 PM,Incoming,8655550203,8650000001,45,,%s" % PERSON,
                  ",,Outgoing,8650000001,8655550204,10,,%s" % PERSON])
    b = fi.on_file(conn, "calls.csv", first, now=NOW)
    ev = {e.tool_id or e.phone: e for e in b.events}
    check(len(b.events) == 3 and any("no date" in p for p in b.problems),
          "each row is one call, and a row with no date is left out and said, never placed on the import day")
    a1 = ev.get("A-1")
    check(a1 is not None and a1.at == T and a1.direction == "out" and a1.phone == "8655550201" and a1.line == "8650000001"
          and a1.seconds == 75 and a1.outcome == "not interested" and a1.person_ref == PERSON and a1.grain == "export"
          and a1.tool == "aircall", "the columns are found by their usual names, and the times read in the file's zone")
    check(ev.get("A-2") is not None and ev["A-2"].answered is False and ev.get("8655550203") is not None
          and ev["8655550203"].direction == "in" and ev["8655550203"].phone == "8655550203",
          "no answer reads as not answered, and an inbound row's other party is its caller")
    again = fi.on_file(conn, "calls.csv", first, now=NOW)
    check(again.events == [] and again.notes and "read before" in again.notes[0], "the same file twice adds nothing")
    overlap = _csv(["Call ID,Date,Direction,From,To,Duration,Disposition,Agent Email",
                    "A-1,11/16/2026 2:02:11 PM,Outgoing,8650000001,8655550201,1:15,Not Interested,%s" % PERSON,
                    ",11/16/2026 3:00 PM,Incoming,8655550203,8650000001,45,,%s" % PERSON,
                    "A-3,11/16/2026 3:30 PM,Outgoing,8650000001,8655550205,0:20,Voicemail,%s" % PERSON])
    fi.on_file(conn, "calls week 2.csv", overlap, now=NOW)
    kept = ks.stored_events("dialer_csv")
    check(len(kept) == 4, "an export that overlaps the last one adds only its new rows (%d kept)" % len(kept))
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Report of calls"])
    ws.append(["Call ID", "Start Time", "Direction", "Number", "Talk Time"])
    ws.append(["X-1", datetime(2026, 11, 16, 14, 2, 11), "out", 8655550211, 61])
    ws.append(["X-2", datetime(2026, 11, 16), "out", 8655550212, 0])
    buf = io.BytesIO()
    wb.save(buf)
    check(ks.connect("xl", "file_import", tool="aircall", settings={"tz": "America/New_York"}) == [], "a workbook source connects")
    b = fi.on_file(ks.connection("xl"), "calls.xlsx", buf.getvalue(), now=NOW)
    xe = {e.tool_id: e for e in b.events}
    check(xe.get("X-1") is not None and xe["X-1"].at == T and xe["X-1"].exact and xe["X-1"].phone == "8655550211"
          and xe["X-1"].seconds == 61,
          "an Excel sheet with a title row reads: its date cells keep their time, a numeric phone keeps its digits")
    check(xe.get("X-2") is not None and not xe["X-2"].exact and ks.local_day(xe["X-2"].at) == "2026-11-16",
          "a date cell with no time is an inexact day")
    check(ks.connect("totals", "file_import", tool="aircall", settings={"rows": "tally"}) == [], "a totals report connects")
    tot = _csv(["Date,Agent,Calls,Answered,Connects,Contact Rate,Conversations,Leads",
                "2026-11-16,Pat,120,40,30,25%,9,2"])
    b = fi.on_file(ks.connection("totals"), "daily.csv", tot, now=NOW)
    got = {(t.metric, t.definition): t.value for t in b.tallies}
    check(got.get(("dials", "Calls")) == 120 and got.get(("answered", "Answered")) == 40 and got.get(("leads", "Leads")) == 2,
          "a totals report's figures land under our names with the report's own word beside them")
    check(got.get(("as_reported", "Connects")) == 30 and got.get(("as_reported", "Contact Rate")) == 25
          and got.get(("as_reported", "Conversations")) == 9,
          "connects, a rate and the report's own conversations are kept as reported, never as ours")
    check(not any(t.metric in ("conversations", "contacts", "connects") for t in b.tallies)
          and all(t.person_ref == "Pat" and t.period == "2026-11-16" for t in b.tallies),
          "no tally is ever called a conversation, and each carries its period and person")
    fixed = _csv(["Date,Agent,Calls", "2026-11-16,Pat,125"])
    fi.on_file(ks.connection("totals"), "daily fixed.csv", fixed, now=NOW)
    dials = [t for t in ks.stored_tallies("totals") if t.metric == "dials"]
    check(len(dials) == 1 and dials[0].value == 125, "a re-sent report for the same day replaces its figure, never adds")
    check(ks.connect("mixed", "file_import", tool="batchdialer", settings={"kind_column": "Activity", "tz": "America/New_York"}) == [],
          "a file that names each row's kind connects with that column named")
    mixed = _csv(["Activity,Date,Phone,Record ID",
                  "Lead,11/16/2026 1:00 PM,8655550221,%s" % U2,
                  "Conversation,11/16/2026 1:05 PM,8655550221,",
                  "Call,11/16/2026 1:10 PM,8655550222,"])
    b = fi.on_file(ks.connection("mixed"), "activity.csv", mixed, now=NOW)
    check(sorted((e.kind, e.stage) for e in b.events) == [("call", ""), ("stage", "lead")]
          and any("conversation" in p for p in b.problems),
          "a row that names a conversation is left out and said; the lead and the call are read")
    check(ks.Event.from_dict(b.events[0].to_dict()).record in ("", U2), "a record id is kept as written")
    plain = _csv(["Event,Date,Phone", "Inbound call,11/16/2026 1:00 PM,8655550231"])
    b = fi.on_file(conn, "events.csv", plain, now=NOW)
    check(all(e.kind == "call" for e in b.events), "a column that happens to be called Event never changes a call file's kind")
    agency = fi.on_file(ks.connection("agency"), "agency.csv", first, now=NOW)
    check(agency.events == [] and agency.problems and "Quinn" in agency.problems[0],
          "the agency's file is read by the agency results workflow, and this source says so instead of reading it twice")
    for cid in ("dialer_csv", "xl", "totals", "mixed"):
        ks.disconnect(cid)


def _agency(check) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import file_import
    from .workflows import agency_results as ar
    print("  the agency's kept files")
    rows_u1 = [{"row": 2, "file": "a.csv", "disposition": "Hot Lead", "class": "lead", "phone": "8655550121",
                "notes": "", "callback": "", "call_date": "2026-11-16", "reached": True, "last": "", "address": "",
                "agent": "Sam"}]
    rows_u2 = [{"row": 3, "file": "a.csv", "disposition": "Wrong Number", "class": "wrong", "phone": "8655550122",
                "notes": "", "callback": "", "call_date": "2026-11-15", "reached": None, "last": "", "address": "",
                "agent": "Sam"},
               {"row": 4, "file": "a.csv", "disposition": "No Answer", "class": "no_contact", "phone": "8655550123",
                "notes": "", "callback": "", "call_date": "", "reached": None, "last": "", "address": "", "agent": ""}]
    rows_u5 = [{"row": 5, "file": "a.csv", "disposition": "Interested", "class": "lead", "phone": "8655550105",
                "notes": "", "callback": "", "call_date": "2026-11-16", "reached": True, "last": "", "address": "",
                "agent": "Sam"}]
    base = {"kind": "records", "received": "2026-11-16T15:00:00+00:00", "day": "2026-11-16", "month": "2026-11",
            "records": {U1: {"top": "lead", "phones": {"8655550121": "correct"}, "deceased": False, "rows": rows_u1},
                        U2: {"top": "", "phones": {"8655550122": "wrong"}, "deceased": False, "rows": rows_u2},
                        U5: {"top": "lead", "phones": {"8655550105": "correct"}, "deceased": False, "rows": rows_u5}}}
    ar.save_return(dict(base, id="ret000000001"))
    corrected = json.loads(json.dumps(base))
    corrected["records"][U1]["rows"][0]["disposition"] = "Warm Lead"
    corrected["received"] = "2026-11-16T16:00:00+00:00"
    ar.save_return(dict(corrected, id="ret000000002"))
    ar.save_return({"id": "pdf000000001", "kind": "pdf", "received": "2026-11-03T15:00:00+00:00", "day": "2026-11-03",
                    "month": "2026-10", "numbers": [
                        {"label": "Total Dials", "value": 12000, "kind": "count"},
                        {"label": "Connects", "value": 900, "kind": "count"},
                        {"label": "Leads", "value": 14, "kind": "count"},
                        {"label": "Dials to Connect", "value": 857.1, "kind": "count"},
                        {"label": "Invoice", "value": 3995, "kind": "money"},
                        {"label": "Connect Rate", "value": 7.5, "kind": "percent"},
                        {"label": "Talk Hours", "value": 41, "kind": "count"}]})
    conn = ks.connection("agency")
    b = file_import.ADAPTER.pull(conn, DAY_START - timedelta(days=15), DAY_END, ("call", "phone_status", "stage"), now=NOW)
    calls = [e for e in b.events if e.kind == "call"]
    check(len(calls) == 4 and all(e.grain == "export" and e.tool == "agency" and e.direction == "out" for e in calls),
          "each number the agency dialed is one call, from the newest file for the month (a corrected file is not new calls)")
    lead_call = [e for e in calls if e.record == U1]
    check(len(lead_call) == 1 and lead_call[0].outcome == "warm lead" and lead_call[0].answered is True
          and lead_call[0].person_ref == "Sam" and not lead_call[0].exact,
          "the newest file's wording wins, a lead was a call someone answered, and the agency's caller is the person")
    blank = [e for e in calls if e.phone == "8655550123"]
    check(len(blank) == 1 and blank[0].answered is None and ks.local_day(blank[0].at) == "2026-11-16",
          "a no answer cannot say whether a machine answered, and a row with no call date sits in its month")
    ps = sorted((e.phone, e.outcome) for e in b.events if e.kind == "phone_status")
    check(ps == [("8655550105", "correct"), ("8655550121", "correct"), ("8655550122", "wrong")],
          "a number the agency reached is correct and a wrong one is wrong")
    leads = sorted(e.record for e in b.events if e.kind == "stage")
    check(leads == sorted([U1, U5]), "a lead the agency passed is a lead stage on its record")
    b = file_import.ADAPTER.pull(conn, datetime(2026, 10, 1, tzinfo=ET), datetime(2026, 11, 1, tzinfo=ET),
                                 ("call", "phone_status", "stage"), now=NOW)
    got = {t.definition: t.metric for t in b.tallies}
    check(got.get("Total Dials") == "dials" and got.get("Leads") == "leads" and got.get("Invoice") == "spend",
          "the summary PDF's dials, leads and fee keep our names, with the agency's word as the definition")
    check(got.get("Connects") == "as_reported" and got.get("Dials to Connect") == "as_reported"
          and got.get("Connect Rate") == "as_reported" and got.get("Talk Hours") == "as_reported",
          "its connects, ratios, rates and talk time are kept as reported")
    check(file_import.pdf_metric({"label": "Conversations", "value": 9, "kind": "count"}) == "as_reported",
          "an agency's own conversations are never ours")


def _webhook(check) -> None:
    from . import kpi_sources as ks, store
    from .kpi_sources import webhook
    print("  webhooks")
    kx = json.dumps({"event": "start_call", "data": {"callid": "K-81", "calldate": "2026-11-16T19:02:11Z",
                                                     "calltype": "outgoing", "fromnumber": "+18650000001",
                                                     "tonumber": "+18655550101", "email": PERSON}}).encode("utf-8")
    check(webhook.receive(SECRET, "kixie", {}, kx, now=NOW)[0] == 404, "with no secret set the route does not exist")
    os.environ["AGENTS_KPI_HOOK_SECRET"] = SECRET
    check(webhook.receive("wrong", "kixie", {}, kx, now=NOW)[0] == 404, "a wrong secret is a 404, never a hint")
    check(webhook.receive(SECRET, "nobody", {}, kx, now=NOW)[0] == 404, "an unknown connection is a 404")
    check(webhook.receive(SECRET, "ledger", {}, kx, now=NOW)[0] == 404, "a connection that is not a webhook is a 404")
    check(webhook.receive(SECRET, "../etc", {}, kx, now=NOW)[0] == 404, "a malformed connection name is a 404")
    big = b"{" + b'"a":"' + b"x" * (300 * 1024) + b'"}'
    check(webhook.receive(SECRET, "kixie", {}, big, now=NOW)[0] == 413, "a delivery that is too large is refused")
    check(webhook.receive(SECRET, "kixie", {}, b"  ", now=NOW)[0] == 400, "an empty delivery is refused")
    status, body = webhook.receive(SECRET, "kixie", {"Content-Type": "application/json", "Authorization": "Bearer x"},
                                   kx, now=NOW)
    check(status == 200 and body == {"ok": True}, "a Kixie start call is kept and answered at once")
    check(webhook.receive(SECRET, "kixie", {}, kx, now=NOW)[1].get("duplicate") is True,
          "a byte-identical retry is one delivery")
    end = json.dumps({"event": "end_call", "data": {"callid": "K-81", "calldate": "2026-11-16T19:02:11Z",
                                                   "calltype": "outgoing", "fromnumber": "+18650000001",
                                                   "tonumber": "+18655550101", "email": PERSON, "duration": 75,
                                                   "callstatus": "answered", "disposition": "Not Interested",
                                                   "answerdate": "2026-11-16T19:02:20Z"}}).encode("utf-8")
    webhook.receive(SECRET, "kixie", {}, end, now=NOW)
    sms = json.dumps({"data": {"messageid": "M-1", "direction": "outgoing", "from": "+18650000001",
                               "to": "+18655550104", "message": "Is the house on Example Road still yours?",
                               "date": "2026-11-16T19:40:00Z", "email": PERSON}}).encode("utf-8")
    webhook.receive(SECRET, "kixie", {}, sms, now=NOW)
    with store._LOCK, store._db() as c:
        raws = c.execute("SELECT parsed, headers, body FROM kpisrc_raw WHERE source='kixie' ORDER BY id").fetchall()
    check(len(raws) == 3 and all(r["parsed"] == "" for r in raws),
          "the route only keeps a delivery; reading it waits for the next count")
    check("Example Road" not in raws[2]["body"] and "[removed]" in raws[2]["body"] and "authorization" not in raws[0]["headers"].lower(),
          "a message's words and any credential header are never kept")
    b = webhook.ADAPTER.pull(ks.connection("kixie"), DAY_START, DAY_END, ("call", "text"), now=NOW)
    calls = [e for e in b.events if e.kind == "call"]
    check(len(calls) == 1 and calls[0].tool_id == "K-81" and calls[0].seconds == 75 and calls[0].answered is True
          and calls[0].outcome == "not interested" and calls[0].person_ref == PERSON and calls[0].direction == "out"
          and calls[0].phone == "8655550101" and calls[0].line == "8650000001" and calls[0].at == T,
          "Kixie's start and end deliveries are one call: its id, 75 seconds, answered, its wording and its agent")
    texts = [e for e in b.events if e.kind == "text"]
    check(len(texts) == 1 and texts[0].direction == "out" and texts[0].phone == "8655550104" and texts[0].tool_id == "M-1",
          "a Kixie text is a text out to the owner's number")
    with store._LOCK, store._db() as c:
        check(c.execute("SELECT COUNT(*) AS n FROM kpisrc_raw WHERE source='kixie' AND parsed=''").fetchone()["n"] == 0,
              "the count reads every kept delivery once")
    check(ks.connect("zap", "webhook", tool="batchdialer", settings={"mapping": "zapier", "tz": "America/New_York"}) == [],
          "a Zapier trigger connects with no code")
    zj = json.dumps({"kind": "call", "id": "Z-1", "at": "11/16/2026 1:00 PM", "direction": "out",
                     "phone": "865-555-0301", "seconds": "61", "answered": "yes", "agent": "Lee"}).encode("utf-8")
    zf = b"kind=lead&record=rec-z1&at=2026-11-16T18%3A00%3A00Z"
    zc = json.dumps({"kind": "Conversation", "id": "Z-9", "at": "2026-11-16T18:00:00Z"}).encode("utf-8")
    zu = json.dumps({"kind": "voodoo", "id": "Z-8"}).encode("utf-8")
    for body in (zj, zf, zc, zu):
        webhook.receive(SECRET, "zap", {}, body, now=NOW)
    b = webhook.ADAPTER.pull(ks.connection("zap"), DAY_START, DAY_END, ks.KINDS, now=NOW)
    zcall = [e for e in b.events if e.kind == "call"]
    check(len(zcall) == 1 and zcall[0].tool == "batchdialer" and zcall[0].seconds == 61 and zcall[0].answered is True
          and zcall[0].person_ref == "Lee" and zcall[0].phone == "8655550301"
          and zcall[0].at == datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc),
          "a Zap's call reads in our own field names, its time in the zone the connection declares")
    check(any(e.kind == "stage" and e.stage == "lead" and e.record == "rec-z1" for e in b.events),
          "a Zap posting a form with a stage word is a lead")
    check(not any(e.kind not in ks.KINDS for e in b.events) and len(b.events) == 2,
          "a Zap that names a conversation or an unknown kind adds nothing")
    check(any("could not be read" in p for p in b.problems), "and the deliveries it could not read are said")
    with store._LOCK, store._db() as c:
        said = [r["problem"] for r in c.execute("SELECT problem FROM kpisrc_raw WHERE source='zap' AND parsed='problem'")]
    check(any("conversation" in p for p in said), "the refusal says a conversation is counted from calls and texts")
    check(ks.connect("kx2", "webhook", tool="kixie", settings={"mapping": "zapier"}) == [],
          "a webhook can be connected with the wrong mapping by mistake")
    webhook.receive(SECRET, "kx2", {}, end, now=NOW)
    b = webhook.ADAPTER.pull(ks.connection("kx2"), DAY_START, DAY_END, ks.KINDS, now=NOW)
    check(b.events == [] and any("could not be read" in p for p in b.problems), "a delivery the mapping cannot read is said")
    ks.connect("kx2", "webhook", tool="kixie", settings={"mapping": "kixie"})
    check(webhook.reparse("kx2") == 1, "once the mapping is fixed, the kept delivery is read again")
    b = webhook.ADAPTER.pull(ks.connection("kx2"), DAY_START, DAY_END, ks.KINDS, now=NOW)
    check(len(b.events) == 1 and b.events[0].tool_id == "K-81" and not b.problems,
          "and it becomes the call it always was, with nothing left over from the wrong reading")
    ks.disconnect("kx2")
    os.environ["AGENTS_KPI_SIGN_TEST"] = "sign-secret-test"
    check(ks.connect("signed", "webhook", tool="phoneburner", settings={
        "mapping": "zapier", "signature": {"header": "X-Sig", "secret_env": "AGENTS_KPI_SIGN_TEST", "prefix": "sha256="}}) == [],
        "a tool that signs its deliveries connects with the header and the name of its secret")
    sb = json.dumps({"kind": "call", "id": "P-1", "at": "2026-11-16T18:30:00Z", "direction": "out",
                     "phone": "8655550401"}).encode("utf-8")
    mac = hmac.new(b"sign-secret-test", sb, hashlib.sha256).digest()
    check(webhook.receive(SECRET, "signed", {}, sb, now=NOW)[0] == 401, "an unsigned delivery is refused")
    check(webhook.receive(SECRET, "signed", {"X-Sig": "sha256=00"}, sb, now=NOW)[0] == 401, "a wrong signature is refused")
    check(webhook.receive(SECRET, "signed", {"x-sig": "sha256=" + mac.hex()}, sb, now=NOW)[0] == 200,
          "a signature in hex is accepted")
    sb2 = sb.replace(b"P-1", b"P-2")
    mac2 = hmac.new(b"sign-secret-test", sb2, hashlib.sha256).digest()
    check(webhook.receive(SECRET, "signed", {"X-Sig": base64.b64encode(mac2).decode()}, sb2, now=NOW)[0] == 200,
          "and in base64")
    ks.disconnect("signed")
    check(webhook.receive(SECRET, "signed", {"x-sig": "sha256=" + mac.hex()}, sb, now=NOW)[0] == 404,
          "a connection switched off stops answering")


def E(kind, at, source, sid, **kw):
    from . import kpi_sources as ks
    return ks.Event(kind=kind, at=at, source=source, source_id=sid, **kw)


CONNS = {
    "datasift": {"id": "datasift", "adapter": "datasift_log", "tool": "", "settings": {}, "builtin": True, "created": ""},
    "kixie": {"id": "kixie", "adapter": "webhook", "tool": "kixie", "settings": {"mapping": "kixie"}, "builtin": False,
              "created": "2026-11-01T00:00:00+00:00"},
    "kixie_csv": {"id": "kixie_csv", "adapter": "file_import", "tool": "kixie", "settings": {"rows": "call"},
                  "builtin": False, "created": ""},
    "agency": {"id": "agency", "adapter": "file_import", "tool": "agency", "builtin": True, "created": "",
               "settings": {"preset": "agency", "echoes": ["agency_results"]}},
}


def _calls(src, n, start, number_of=lambda i: "86555503%02d" % i, step=600, shift=lambda i: 0, **kw):
    out = []
    for i in range(n):
        out.append(E("call", start + timedelta(seconds=i * step + shift(i)), src, "%s-%d" % (src, i), direction="out",
                     phone=number_of(i), tool="kixie", **kw))
    return out


def _merge(check) -> None:
    from . import kpi_sources as ks
    print("  joining reports into acts")
    acts, _ = ks.merge([E("stage", T, "agency", "lead:r1", stage="lead", record="r1", tool="agency", grain="export"),
                        E("stage", T + timedelta(days=1), "datasift", "row:1:lead", stage="lead", record="R1",
                          author="person", tool="datasift")], CONNS)
    check(len(acts) == 1 and acts[0].sources == ("agency", "datasift"), "a lead told by two sources is one lead (record, stage)")
    acts, _ = ks.merge([E("phone_status", T, "datasift", "row:1", phone="8655550101", owner="o1", outcome="CORRECT"),
                        E("phone_status", T, "agency", "phone:r1", phone="8655550101", record="r1", outcome="correct",
                          grain="export"),
                        E("phone_status", T, "datasift", "row:2", phone="8655550101", owner="o2", outcome="CORRECT")], CONNS)
    check(len(acts) == 2 and sorted(len(a.sources) for a in acts) == [1, 2],
          "a number marked correct by two sources is one fact, while the same number on two owners stays two")
    acts, _ = ks.merge([E("call", T, "kixie", "id:K-81", tool="kixie", tool_id="K-81", direction="out", phone="8655550101"),
                        E("call", T + timedelta(minutes=9), "kixie_csv", "id:K-81", tool="kixie", tool_id="K-81",
                          direction="out", phone="8655550101", grain="export")], CONNS)
    check(len(acts) == 1, "two sources carrying the tool's own call id are one call, however far apart their clocks")
    kx = E("call", T, "kixie", "id:K-81", tool="kixie", tool_id="K-81", direction="out", phone="8655550101", seconds=75,
           answered=True, outcome="Not Interested", person_ref=PERSON)
    ds = E("call", T + timedelta(seconds=2), "datasift", "call:D-19", tool="kixie", direction="out", phone="8655550101",
           seconds=74, answered=True, owner="own-1", person_ref="Pat")
    acts, _ = ks.merge([kx, ds], CONNS)
    a = acts[0] if len(acts) == 1 else None
    check(a is not None and a.sources == ("datasift", "kixie"),
          "the worked example: Kixie's call and DataSift's, two seconds apart, are one call")
    check(a is not None and a.seconds == 75 and a.outcome == "not interested" and a.person_ref == PERSON
          and a.owner == "own-1" and a.at == T,
          "the tool's own talk time, wording, agent and clock win; DataSift gives the owner")
    far = E("call", T + timedelta(minutes=3), "datasift", "call:D-20", tool="kixie", direction="out", phone="8655550101")
    check(len(ks.merge([kx, far], CONNS)[0]) == 2, "three minutes apart are two calls")
    other = E("call", T + timedelta(seconds=2), "datasift", "call:D-21", tool="kixie", direction="in", phone="8655550101")
    check(len(ks.merge([kx, other], CONNS)[0]) == 2, "a call out and a call in are never one")
    elsewhere = E("call", T + timedelta(seconds=2), "datasift", "call:D-22", tool="kixie", direction="out",
                  phone="8655550199")
    check(len(ks.merge([kx, elsewhere], CONNS)[0]) == 2, "calls to two numbers are never one")
    someone = E("call", T + timedelta(seconds=2), "kixie_csv", "row:1", tool="kixie", direction="out", phone="8655550101",
                person_ref="lee.caller@example.test", grain="export")
    check(len(ks.merge([kx, someone], CONNS)[0]) == 2, "two different agents' emails are two calls")
    smrt = E("call", T + timedelta(seconds=2), "datasift", "call:D-23", tool="smrtphone", direction="out",
             phone="8655550101")
    check(len(ks.merge([kx, smrt], CONNS)[0]) == 2, "two tools' calls are never one")
    texts = [E("text", T, "kixie", "id:M1", tool="kixie", direction="out", phone="8655550104"),
             E("text", T + timedelta(seconds=40), "datasift", "row:t1", tool="kixie", direction="out", phone="8655550104")]
    check(len(ks.merge(texts, CONNS)[0]) == 2, "texts join only within 30 seconds")
    k2 = _calls("kixie", 2, T, number_of=lambda i: "8655550101", step=60)
    d2 = _calls("datasift", 2, T + timedelta(seconds=2), number_of=lambda i: "8655550101", step=59)
    acts, _ = ks.merge(k2 + d2, CONNS)
    check(len(acts) == 2 and all(set(x.sources) == {"kixie", "datasift"} for x in acts)
          and sorted(x.members for x in acts) == [("kixie:kixie-0", "datasift:datasift-0"),
                                                   ("kixie:kixie-1", "datasift:datasift-1")],
          "a quick redial pairs one to one, nearest in time first")
    # agreement, per tool and day
    k10 = _calls("kixie", 10, T)
    d10 = _calls("datasift", 9, T + timedelta(seconds=2)) + [E("call", T + timedelta(hours=1), "datasift", "datasift-x",
                                                              direction="out", phone="8655550399", tool="kixie")]
    acts, issues = ks.reconcile(ks.merge(k10 + d10, CONNS)[0], CONNS)
    check(len(acts) == 11 and issues == [], "sources that agree on 9 of 10 are counted together: 11 calls, no issue")
    bad = _calls("datasift", 10, T + timedelta(seconds=2), shift=lambda i: 0 if i < 5 else 3600)
    acts, issues = ks.reconcile(ks.merge(k10 + bad, CONNS)[0], CONNS)
    check(len(acts) == 10 and all("kixie" in x.sources for x in acts) and len(issues) == 1,
          "sources that agree on only 5 of 10 are not added: the larger counts alone (the tool's own on a tie), with one issue")
    check(issues and issues[0]["text"].startswith("Kixie calls on Nov 16: ") and "5 of 10 matched" in issues[0]["text"]
          and issues[0]["level"] == "problem",
          "the issue opens with the same words every run and says how many matched")
    k20 = _calls("kixie", 20, T, step=300)
    d8 = _calls("datasift", 8, T + timedelta(seconds=2), step=300)
    acts, issues = ks.reconcile(ks.merge(k20 + d8, CONNS)[0], CONNS)
    check(len(acts) == 20 and issues == [],
          "the smaller source is the divisor: DataSift's 8 on-record calls all inside Kixie's 20 is agreement")
    d12 = _calls("datasift", 12, T, step=300)
    acts, issues = ks.reconcile(ks.merge(d12, CONNS)[0], CONNS)
    check(len(acts) == 12 and any("reported none" in i["text"] and i.get("silent") == "kixie" for i in issues),
          "a connected webhook that sent nothing while DataSift shows 12 of its calls is an issue, never a quiet day")
    later = dict(CONNS, kixie=dict(CONNS["kixie"], created="2026-11-20T00:00:00+00:00"))
    check(ks.reconcile(ks.merge(d12, later)[0], later)[1] == [], "a day before the webhook was connected is not silence")
    # our own write-backs
    self_lead = E("stage", T, "datasift", "row:9:lead", stage="lead", record=U5, author="self", via="agency_results",
                  tool="datasift")
    agency_lead = E("stage", T - timedelta(hours=3), "agency", "lead:%s" % U5, stage="lead", record=U5, tool="agency",
                    grain="export", exact=False)
    acts, info = ks.merge([self_lead, agency_lead], CONNS)
    check(len(acts) == 1 and acts[0].sources == ("agency",) and info["echoes"] == {"agency": 1},
          "a lead our key set for the agency's lead is an echo: counted once, as the agency's")
    alone = {k: v for k, v in CONNS.items() if k != "agency"}
    acts, info = ks.merge([self_lead], alone)
    check(len(acts) == 1 and acts[0].author == "self" and acts[0].person_ref == "" and info["echoes"] == {},
          "with the agency's source off, our own write stands as a fact but is no person's work")
    mixed = [E("call", T, "datasift", "call:1", tool="kixie", direction="out", phone="8655550101", author="self",
               person_ref="Pat"),
             E("call", T + timedelta(seconds=1), "kixie", "id:K9", tool="kixie", direction="out", phone="8655550101",
               person_ref=PERSON)]
    acts, _ = ks.merge(mixed, CONNS)
    check(len(acts) == 1 and acts[0].person_ref == PERSON and acts[0].author != "self",
          "an act with a tool's report takes its person from the tool, never from our own key")


def _collect(check) -> None:
    from . import kpi_sources as ks
    from .kpi_sources import registry
    print("  collect")
    check(_raises(lambda: ks.collect(DAY_START, DAY_END, kinds="conversation", now=NOW)) != "",
          "asking collect for conversations is refused: they are derived, never collected")
    check(_raises(lambda: ks.collect(datetime(2026, 11, 16), DAY_END, now=NOW)) != "", "a window with no zone is refused")
    only = ["datasift", "kixie", "ledger", "agency"]
    out = ks.collect(DAY_START, DAY_END, now=NOW, only=only)
    ev, tallies, health = out["events"], out["tallies"], out["health"]
    k81 = [a for a in ev if a.kind == "call" and a.phone == "8655550101" and a.direction == "out"]
    check(len(k81) == 1 and set(k81[0].sources) == {"datasift", "kixie"} and k81[0].seconds == 75,
          "Kixie's webhook and the DataSift log tell the same call: one act, Kixie's 75 seconds")
    check(not any(a.kind == "conversation" for a in ev) and not any(t.metric == "conversations" for t in tallies),
          "nothing collect returns reports a conversation")
    u5 = [a for a in ev if a.kind == "stage" and a.record == U5]
    check(len(u5) == 1 and u5[0].sources == ("agency",), "the agency's lead counts once, as the agency's, not as our own write")
    check(any(h.get("source") == "merge" and h.get("level") == "note" and "repeat what" in h["text"] for h in health),
          "and health says how many of our own rows were echoes")
    k_issue = [h for h in health if h.get("source") == "merge" and h.get("level") == "problem" and h.get("tool") == "kixie"]
    check(k_issue == [], "DataSift's Kixie calls and Kixie's webhook agree, so there is no issue: %s" % [h["text"] for h in k_issue])
    rows = {h["source"]: h for h in health if h.get("source") != "merge"}
    check(set(only) <= set(rows) and rows["datasift"]["complete"] and rows["kixie"]["level"] == "ok",
          "every source has a health row, and a current one reads ok")
    check(not any(set(a.sources) & {"dialer_csv", "zap"} for a in ev), "only the sources asked for are read")
    spend = ks.collect(DAY_START, DAY_END, kinds=("spend",), now=NOW, only=only)
    check(spend["events"] and all(a.kind == "spend" for a in spend["events"]), "collect returns only the kinds asked for")
    check(ks.connect("kx_totals", "file_import", tool="kixie", settings={"rows": "tally"}) == [], "a tool's totals connect")
    from .kpi_sources import file_import
    file_import.ADAPTER.on_file(ks.connection("kx_totals"), "kixie day.csv",
                                _csv(["Date,Calls,Connects", "2026-11-16,3,1"]), now=NOW)
    out = ks.collect(DAY_START, DAY_END, now=NOW, only=only + ["kx_totals"])
    tl = {t.metric: t for t in out["tallies"] if t.source == "kx_totals"}
    check(tl.get("dials") is not None and tl["dials"].covered and not tl["as_reported"].covered,
          "a tally for a day the events already count is marked covered; a vendor's own figure never is")
    check(not any(a.source == "kx_totals" for a in out["events"]) and len(out["events"]) == len(
        ks.collect(DAY_START, DAY_END, now=NOW, only=only)["events"]),
          "a tally is never added to the events")
    ks.disconnect("kx_totals")
    october = ks.collect(datetime(2026, 10, 1, tzinfo=ET), datetime(2026, 11, 1, tzinfo=ET), now=NOW, only=["agency"])
    dials = [t for t in october["tallies"] if t.metric == "dials"]
    check(len(dials) == 1 and dials[0].value == 12000 and not dials[0].covered and dials[0].definition == "Total Dials",
          "a month only the agency's PDF covers keeps the PDF's dials, under its own word")
    edge = [call_row(90, "made", "own-9", DAY_START - timedelta(seconds=2), "8655550191", "c-90"),
            call_row(91, "answered", "own-9", DAY_START + timedelta(seconds=10), "8655550191", "c-90", duration=70,
                     caller="Pat")]
    _log(edge)
    today = ks.collect(DAY_START, DAY_END, kinds=("call",), now=NOW, only=["datasift"])["events"]
    yesterday = ks.collect(DAY_START - timedelta(days=1), DAY_START, kinds=("call",), now=NOW, only=["datasift"])["events"]
    check(not any(a.phone == "8655550191" for a in today)
          and sum(1 for a in yesterday if a.phone == "8655550191" and a.answered) == 1,
          "a call dialed before midnight and answered after it belongs to the day it was dialed, once")

    class Broken(ks.Adapter):
        id = "broken"
        provides = (ks.Provides("call", "api_key"),)

        def pull(self, conn, since, until, kinds, now=None):
            raise RuntimeError("the vendor is down")

    globals()["BROKEN"] = Broken()
    registry.ADAPTERS["broken"] = ("agents.selftest_kpisources", "BROKEN")
    ks.connect("vendor_x", "broken", tool="vendorx")
    out = ks.collect(DAY_START, DAY_END, now=NOW, only=["vendor_x", "datasift"])
    bad = [h for h in out["health"] if h.get("source") == "vendor_x"]
    check(bad and bad[0]["level"] == "problem" and "could not be read" in bad[0]["problems"][0]
          and any(a.kind == "call" for a in out["events"]),
          "a source that cannot be read is a problem in its row, and the others still count")
    ks.disconnect("vendor_x")
    del registry.ADAPTERS["broken"]
    ks.connect("aircall_hook", "webhook", tool="aircall", settings={"mapping": "zapier"},
               now=datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc))
    late = ks.collect(DAY_START, DAY_END, now=NOW, only=["aircall_hook"])["health"][0]
    check(late["level"] == "problem" and "nothing has arrived since it was connected" in late["problems"][0],
          "a webhook that never delivered is late by the afternoon of a business day")
    morning = datetime(2026, 11, 16, 15, 30, tzinfo=timezone.utc)      # 10:30 AM Eastern, Monday
    early = ks.collect(DAY_START, DAY_END, now=morning, only=["aircall_hook"])["health"][0]
    check(early["level"] == "ok", "a weekend is not silence: on Monday morning the clock starts at 9:00")
    ks.disconnect("aircall_hook")
    ks.prune(NOW + timedelta(days=20))
    from . import store
    with store._LOCK, store._db() as c:
        check(c.execute("SELECT COUNT(*) AS n FROM kpisrc_raw").fetchone()["n"] == 0
              and c.execute("SELECT COUNT(*) AS n FROM kpisrc_event WHERE source='kixie'").fetchone()["n"] > 0,
              "raw deliveries are kept 14 days; the events read from them stay")
    pub = ks.public_health()
    check(set(pub) == {"sources_on", "webhooks_on", "deliveries_kept", "deliveries_waiting"}
          and all(isinstance(v, int) for v in pub.values()),
          "the public health line carries counts only")


def _conversations(check) -> None:
    from . import kpi_sources as ks
    print("  conversations, from the catalog only")
    acts = [E("call", T, "kx", "1", direction="out", phone="8655550101", owner="o1", answered=True, seconds=60),
            E("call", T + timedelta(minutes=5), "kx", "2", direction="out", phone="8655550102", owner="o2", answered=True,
              seconds=59),
            E("call", T + timedelta(minutes=6), "kx", "3", direction="out", phone="8655550103", owner="o3", answered=True),
            E("call", T + timedelta(minutes=7), "kx", "4", direction="out", phone="8655550104", owner="o4", answered=True,
              seconds=20, outcome="voicemail"),
            E("text", T + timedelta(minutes=8), "kx", "5", direction="in", phone="8655550101", owner="o1"),
            E("text", T + timedelta(minutes=9), "kx", "6", direction="in", phone="8655550105", owner="o5"),
            E("text", T + timedelta(minutes=10), "kx", "7", direction="in", phone="8655550105", owner="o5"),
            E("call", T + timedelta(minutes=11), "kx", "8", direction="in", phone="8655550106", owner="o6", answered=True,
              seconds=90),
            E("text", T + timedelta(minutes=12), "kx", "9", direction="out", phone="8655550107", owner="o7")]
    got = ks.conversations(acts, FakeCatalog())
    kinds = sorted((x["kind"], x["party"]) for x in got["items"])
    check(kinds == [("call", "o1"), ("call back", "o6"), ("text", "o5")],
          "a minute answered is a conversation, 59 seconds and a voicemail are not, a text in counts once per owner a "
          "day and not for an owner already talked to: %s" % kinds)
    check(got["length_unknown"] == 1, "an answered call no source gave a length for is length unknown, never a conversation")
    strict = ks.conversations(acts, FakeCatalog(min_seconds=90, texts=False, calls=False))
    check([x["kind"] for x in strict["items"]] == [],
          "the catalog's own figures decide: a 90 second floor with texts and call backs switched off counts none here")
