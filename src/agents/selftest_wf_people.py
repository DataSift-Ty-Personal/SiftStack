"""Diggs's people workflows (wave 3): skip_trace, phone_score, entity_principals, probate_property and
buyer_registry, plus the five shared fixes in the modules under them (buyer_sweep, enformion_business,
dispo_buyers). Zero network: the autopilot selftest's fake DataSift (extended for an address search
and a field read-back), fake trace sources and scorer, a fake SiftMap that runs the real MapClient
matching code, a fake business search, the real property_lookup guard over fake county rolls, and a
fake dispo_buyers for the nightly job. Every name, street, company and number here is invented.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, FakeCRM, FakeScorer, FakeSource, _rec

SRC = Path(__file__).resolve().parents[1]
PHONE_RX = re.compile(r"\d{3}[-. ]?\d{3}[-. ]?\d{4}")

PLAYBOOK = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0},
    "people": {"callers": ["Adriana"]},
    "gate": {"read_fields": True},
    "lanes": {"ftm": {"tags": ["FTM"]}},
    "stages": {
        "ftm_retrace": {"kind": "retrace", "owner": "research", "mode": "plan", "schedule": "11:00", "lane": "ftm",
                        "presets": ["FTM - 01 Skipped No Numbers"], "sources": ["smartskip", "tracerfy"],
                        "callable_tiers": ["Dial First", "Dial Second"], "entity": "principals",
                        "after_sources": "mail_only", "write_tiers": ["Dial First", "Dial Second"],
                        "reserve_tiers": ["Dial Third"], "keep_numbers": 3, "drop_status": "DEAD",
                        "write_line_type": True, "fixup_queue": 100, "fixup_tag": "Needs Fix-Up"},
        "ftm_score": {"kind": "score", "owner": "research", "mode": "plan", "schedule": "08:30", "lane": "ftm",
                      "presets": ["FTM - 02 Ready to Call"], "drop_status": "DEAD", "write_line_type": True,
                      "recheck_days": 7},
    },
}


class PeopleCRM(FakeCRM):
    """The autopilot's fake DataSift plus the two things these workflows read that it lacks: a
    street-line search (address_prefix) and custom fields that read back what was written."""
    fields = {"decision maker": "f-dm", "dm relationship": "f-rel", "owner deceased": "f-od"}
    LABELS = {"f-dm": "Decision Maker", "f-rel": "DM Relationship", "f-od": "Owner Deceased"}

    def __init__(self, records):
        super().__init__(records)
        self.custom = {}
        self.searches = []

    def search(self, must, limit=250, offset=0):
        q = str((must or {}).get("search") or "")
        if q.startswith("address_prefix:"):
            want = q.split(":", 1)[1].strip().lower()
            self.searches.append(want)
            rows = [self._row(r) for r in self.recs.values() if r["address"]["street"].lower().startswith(want)]
            return len(rows), rows[offset:offset + limit]
        return super().search(must, limit, offset)

    def set_custom_field(self, u, field_uuid, value):
        super().set_custom_field(u, field_uuid, value)
        self.custom.setdefault(u, {})[self.LABELS.get(field_uuid, field_uuid)] = value


class SubjectSource(FakeSource):
    """A fake source that also keeps who it was asked to find (name and mailing street)."""

    def trace(self, subjects):
        self.subjects = getattr(self, "subjects", []) + [(s.record_uuid, s.first, s.last, s.mail_street) for s in subjects]
        return super().trace(subjects)


def _task(store, workflow, record="", inputs=None, source="agent:research", details=""):
    tid, _ = store.enqueue("research", workflow, record, subject="selftest", force=True, source=source,
                           payload={"inputs": inputs or {}, "details": details}, thread_id="thread-people")
    return store.task(tid)


def _people_records():
    return [
        _rec("sp1", "1 Trace St"),
        _rec("sp2", "2 Trace St", tags=["FTM", "Courthouse Data", "Foreclosure", "Do Not Market"]),
        _rec("sp3", "3 Trace St", deceased=True),
        _rec("sp4", "4 Trace St", phones=[{"number": "8655550401", "type": "MOBILE", "tags": ["Dial First"], "status": "UNKNOWN"}]),
        _rec("sp5", "5 Trace St", first=""),
        _rec("sp6", "6 Trace St", first="", last="", company="Invented Holdings LLC"),
        _rec("sp7", "7 Trace St", tags=["FTM", "Courthouse Data", "Probate"]),
        _rec("sp8", "8 Trace St"),
        _rec("sp9", "9 Twin Ave"),
        _rec("sp10", "9 Twin Avenue Rear"),
    ]


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import ledger, policy
    saved_env = {k: os.environ.get(k) for k in (
        "AGENTS_SKIP_TRACE_RUN_USD", "AGENTS_PHONE_SCORE_RUN_USD", "AGENTS_PRINCIPALS_RUN_USD", "AGENTS_WF_PRESET_MAX",
        "AGENTS_BUYER_REGISTRY_DETAILS", "AGENTS_BUYER_REGISTRY_UNMASK", "AGENTS_BUYER_REGISTRY_RUN_USD",
        "AGENTS_BUYER_REGISTRY_COLD", "AGENTS_BUYER_REGISTRY_MINUTES", "AGENTS_BUYER_REGISTRY_DIR")}
    saved_cfg = (config.DATA_DIR, config.DAILY_CAP_USD, config.MODE)
    import enformion_business as eb
    saved_configured = eb.is_configured
    config.DATA_DIR, config.DAILY_CAP_USD = tmp, 100000.0
    store.init()
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_phones", "ap_owner_lane"):
            c.execute("DELETE FROM " + table)
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    try:
        _modules(check)
        _skip_trace(check, store, ledger, pol)
        _phone_score(check, store, ledger, pol)
        _entity(check, store, ledger, pol)
        _probate(check, store)
        _registry(check, store, ledger, tmp)
        _hygiene(check, tmp)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.DATA_DIR, config.DAILY_CAP_USD, config.MODE = saved_cfg
        eb.is_configured = saved_configured


# ---------------------------------------------------------------------- the modules underneath
def _modules(check) -> None:
    print("  the modules underneath")
    import buyer_sweep as bs
    import dispo_buyers as D
    import enformion_business as eb

    check(bs.drop_unit("9 Example Pkwy Ste 4, Knoxville, TN 37917") == "9 Example Pkwy, Knoxville, TN 37917",
          "dropping a unit keeps the city and ZIP, so the search cannot match the same street in another county")
    check(bs.street_of("4300 Example Rd; Knoxville, TN 37919") == "4300 Example Rd",
          "a semicolon ends the street, the way Enformion writes an address")
    rows = [{"id": "a", "address": "12 Oak St, Otherville, TN 37000"}, {"id": "b", "address": "12 Oak St, Knoxville, TN 37917"}]
    check(bs.match_row(rows, "12 Oak St, Knoxville, TN 37917")["id"] == "b",
          "a search row counts only at the same house number, street and ZIP")
    check(bs.match_row(rows[:1], "12 Oak St, Knoxville, TN 37917") is None,
          "the same street in another ZIP is a stranger's house, never a match")

    class Opener:
        def __init__(self):
            self.reqs, self.n = [], 0

        def __call__(self, req, timeout=0):
            self.reqs.append(req)
            self.n += 1
            if self.n == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, io.BytesIO(b"{}"))

            class R:
                def __enter__(self_):
                    return self_

                def __exit__(self_, *a):
                    return False

                def read(self_):
                    return json.dumps({"data": [{"id": "x9", "address": "5 Map Rd, Knoxville, TN 37917"}]}).encode()
            return R()
    op = Opener()
    mc = bs.MapClient(api_key="test-key", min_interval=0, opener=op, sleep=lambda s: None)
    hit = mc.find_address("5 Map Rd, Knoxville, TN 37917")
    check(hit and hit["id"] == "x9" and op.n == 2 and op.reqs[-1].full_url.endswith("/properties/search/")
          and op.reqs[-1].get_header("Authorization") == "Api-Key test-key",
          "the map client uses the documented search with the Open API key and waits out a 429 instead of failing")
    try:
        bs.MapClient(api_key="").search({})
        check(False, "a map client with no key refuses instead of logging in some other way")
    except bs.MapError:
        check(True, "a map client with no key refuses instead of logging in some other way")
    try:
        D.KeyOnlySearch()._bearer()
        check(False, "the registry's search never falls back to a password token")
    except RuntimeError as exc:
        check("password" in str(exc), "the registry's search never falls back to a password token")

    class Map(bs.MapClient):
        def __init__(self, rows, details):
            super().__init__(api_key="test-key", min_interval=0, sleep=lambda s: None)
            self.rows, self.details, self.log = rows, details, []

        def _request(self, path, method="GET", body=None, timeout=45.0):
            self.log.append(path)
            if path == "/properties/search/":
                return {"data": self.rows.get(body["addresses"][0]["search"], [])}
            return self.details.get(path.split("/")[-2], {})
    m = Map({"7 Home Rd, Knoxville, TN 37917": [{"id": "h7", "address": "7 Home Rd, Knoxville, TN 37917"}]},
            {"h7": {"owner_info": {"owner_name": "ROBIN TESTER"}}})
    got = bs.reverse_address("7 Home Rd, Knoxville, TN 37917", m)
    check(got["principal"] == "ROBIN TESTER" and got["source"] == "siftmap-reverse-address",
          "the free check names the person who owns the house a company's mail goes to")
    check(bs.reverse_address("PO Box 44, Knoxville, TN 37917", m)["miss"] == "PO box mailing address" and len(m.log) == 2,
          "a PO box is never looked up: it is not anybody's house")
    m2 = Map({"7 Home Rd, Knoxville, TN 37917": [{"id": "h8", "address": "7 Home Rd, Knoxville, TN 37917"}]},
             {"h8": {"owner_info": {"owner_name": "SAMPLE HOLDINGS LLC", "secondary_owner_names": ["JORDAN SAMPLE"]}}})
    check(bs.reverse_address("7 Home Rd, Knoxville, TN 37917", m2)["source"] == "siftmap-secondary-owner",
          "a company on the title with a person beside it names the person")
    check(eb.rank_officers("Rivet Works LLC", [{"name": "Quinn Faraway", "title": "MEMBER", "address": "1 Far Rd; Dover, DE 19901"}], "TN") == [],
          "an officer outside the company's state is a different person, so is dropped")
    check(eb.principal_confidence("Sample Ventures LLC", "Lee Counsel", "REGISTERED AGENT") == "low"
          and eb.principal_confidence("Rivet Works LLC", "Rowan Rivet", "REGISTERED AGENT") == "high",
          "a registered agent who shares no name with the company is usually its lawyer")
    check(eb.first_last_for("RIVET ROWAN", "Rivet Works LLC") == ("Rowan", "Rivet"),
          "a surname the company name shares is moved last (county filings write the surname first)")
    check(eb.first_last_for("Morales Family Trust") == ("", "") and eb.first_last_for("E Bourgeois") == ("", ""),
          "a trust word is not a surname and an initial is not a first name")
    calls = []
    offs, billed = eb.search_officers("Rivet Works LLC", "Knoxville, TN", search=lambda n, cs: calls.append(n) or [])
    check(offs == [] and billed is False, "a business search that returns no record is not billed")
    check(D._is_human is eb._is_human and D.split_mail is eb.split_mail and D.rank_officers is eb.rank_officers,
          "the registry and the principal finder read one copy of the principal rules")


# ---------------------------------------------------------------------- skip_trace
def _skip_trace(check, store, ledger, pol) -> None:
    print("  skip trace")
    from .workflows import skip_trace as st

    crm = PeopleCRM(_people_records())
    now = __import__("datetime").datetime(2026, 11, 13, 15, 0, tzinfo=__import__("datetime").timezone.utc)
    t = _task(store, "skip_trace", inputs={"target": "1 Trace St"})
    check(st.resolve_target(t, crm, now) == {"kind": "record", "uuid": "sp1"}, "a street line that matches one record names it")
    t2 = _task(store, "skip_trace", inputs={"target": "9 Twin"})
    r2 = st.resolve_target(t2, crm, now)
    check(r2["kind"] == "" and "Several records match" in r2["say"], "two matching records are never guessed between")
    t3 = _task(store, "skip_trace", inputs={"target": "ftm - 01 skipped no numbers"})
    check(st.resolve_target(t3, crm, now)["kind"] == "preset", "a saved preset is named by its title, whatever the case")

    def srcs():
        return {"smartskip": SubjectSource("smartskip", 0.15, "match", {"sp1": ["8655550111"], "sp6": ["8655550161"]}),
                "tracerfy": SubjectSource("tracerfy", 0.02, "row", {})}

    config.MODE = "dry"
    s = srcs()
    dry = st.run(_task(store, "skip_trace", "sp1"), crm=crm, pol=pol, sources=s, scorer=FakeScorer(), now=now, enqueue=lambda *a, **k: None)
    check(not s["smartskip"].calls and not crm.writes and not dry["live"] and dry["would_usd"] > 0,
          "a dry run calls no source, writes nothing and says what the trace would cost")
    check("plan" in st.render({}, dry) and not PHONE_RX.search(st.render({}, dry)), "a dry run says it wrote nothing")

    config.MODE = "live"
    queued = []
    s = srcs()
    task = _task(store, "skip_trace", "sp1")
    live = st.run(task, crm=crm, pol=pol, sources=s, scorer=FakeScorer(), now=now, enqueue=lambda *a, **k: queued.append(a) or (1, True))
    numbers = {p["number"] for p in crm.recs["sp1"]["owner"]["phones"]}
    check(s["smartskip"].calls == [["sp1"]] and "8655550111" in numbers,
          "a live run traces the record through the playbook's waterfall and the number lands on the owner")
    check(("skiptrace_attempts", "o-sp1", 1) in crm.writes, "the owner's skip trace counter is written as the new total")
    check(any(w[0] == "upsert" and w[1] == "o-sp1" for w in crm.writes), "numbers land through the owner's upsert-phones route")
    check(live["read_back"] == 1 and not live["problems"], "every record a live run changed is read back")
    check(store.spent(task_id=task["id"]) > 0, "what the run paid is recorded against its task, so the day's cap sees it")
    check(not PHONE_RX.search(st.render(task, live)), "the Slack text carries no full phone number")

    def one(uuid_, **kw):
        crm.writes.clear()
        s_ = srcs()
        q_ = []
        r = st.run(_task(store, "skip_trace", uuid_), crm=crm, pol=pol, sources=s_, scorer=FakeScorer(), now=now,
                   enqueue=lambda *a, **k: q_.append(a) or (1, True), **kw)
        return r, s_, q_
    r, s_, _ = one("sp2")
    check(not s_["smartskip"].calls and "Do Not Market" in r["held"][0]["why"], "a record the gate holds is never sent to a paid source")
    r, s_, q_ = one("sp3")
    check(not s_["smartskip"].calls and "deep prospect" in r["held"][0]["why"] and not q_,
          "a dead owner is never traced blind, and no paid research is queued on a skip trace ask")
    r, s_, q_ = one("sp7")
    check(not s_["smartskip"].calls and "probate" in r["held"][0]["why"], "a probate record goes to research, not a trace")
    r, s_, _ = one("sp4")
    check(not s_["smartskip"].calls and "callable" in r["held"][0]["why"], "a record that already has a callable number is not paid for again")
    r, s_, _ = one("sp5")
    check(not s_["smartskip"].calls and "no full name" in r["held"][0]["why"], "a record with half a name never reaches a paid source")
    r, s_, q_ = one("sp6")
    check(not s_["smartskip"].calls and q_ and q_[0][:3] == ("research", "entity_principals", "sp6"),
          "a company owner goes to the principal finder on the same thread, not to a trace")
    r, s_, _ = one("sp8")
    check(s_["smartskip"].calls == [["sp8"]] and "Mail Only" not in crm.recs["sp8"]["tags"],
          "a miss on every source is reported and never tagged Mail Only on a person's ask")

    os.environ["AGENTS_SKIP_TRACE_RUN_USD"] = "0.10"
    crm.recs["sp9"]["address"]["street"] = "19 Cap Ln"
    r, s_, _ = one("sp9")
    check(not s_["smartskip"].calls and any("not traced today" in k for k in r["tally"]),
          "a source that would pass the run's cap is not called")
    os.environ.pop("AGENTS_SKIP_TRACE_RUN_USD", None)
    keep_cap = config.DAILY_CAP_USD
    config.DAILY_CAP_USD = store.spent(day=store.today(), research=False) + 0.05
    crm.recs["sp10"]["address"]["street"] = "20 Cap Ln"
    r, s_, _ = one("sp10")
    check(not s_["smartskip"].calls and any("brain cap" in k for k in r["tally"]), "the brain's daily cap stops a trace too")
    config.DAILY_CAP_USD = keep_cap

    os.environ["AGENTS_WF_PRESET_MAX"] = "2"
    config.MODE = "dry"
    pr = st.run(_task(store, "skip_trace", inputs={"target": "FTM - 01 Skipped No Numbers"}), crm=crm, pol=pol, sources=srcs(),
                scorer=FakeScorer(), now=now, enqueue=lambda *a, **k: None)
    check(pr["scope"] == "preset" and pr["taken"] == 2 and pr["preset_total"] > 2 and "holds" in st.render({}, pr),
          "a preset ask takes a handful of records and says how many the preset holds")
    os.environ.pop("AGENTS_WF_PRESET_MAX", None)
    config.MODE = "dry"


# ---------------------------------------------------------------------- phone_score
def _phone_score(check, store, ledger, pol) -> None:
    print("  phone score")
    from .workflows import phone_score as ps

    def ph(n):
        return {"number": n, "type": "UNKNOWN", "tags": [], "status": "UNKNOWN"}
    recs = [_rec("ps1", "1 Score St", phones=[ph("8655550151"), ph("8655550159")]),
            _rec("ps2", "2 Score St", phones=[ph("8655550151")]),
            _rec("ps3", "3 Score St", phones=[ph("8655550157")]),
            _rec("ps3b", "33 Score St"),
            _rec("ps4", "4 Score St", phones=[ph("8655550141")], tags=["FTM", "Do Not Market"]),
            _rec("ps5", "5 Score St", phones=[ph("8655550121")])]
    recs[2]["owner"]["uuid"] = recs[3]["owner"]["uuid"] = "o-shared"
    crm = PeopleCRM(recs)
    now = __import__("datetime").datetime(2026, 11, 13, 15, 0, tzinfo=__import__("datetime").timezone.utc)

    config.MODE = "dry"
    sc = FakeScorer()
    dry = ps.run(_task(store, "phone_score", "ps1"), crm=crm, pol=pol, scorer=sc, now=now)
    check(not sc.calls and not crm.writes and abs(dry["would_usd"] - 0.03) < 1e-6,
          "a dry run prices the unscored numbers and buys and writes nothing")
    config.MODE = "live"
    sc = FakeScorer()
    task = _task(store, "phone_score", "ps1")
    live = ps.run(task, crm=crm, pol=pol, scorer=sc, now=now)
    tiers = {p["number"]: [t for t in p["tags"] if t in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")]
             for p in crm.recs["ps1"]["owner"]["phones"]}
    check(sorted(sc.calls) == ["8655550151", "8655550159"] and tiers == {"8655550151": ["Dial First"], "8655550159": ["Drop"]},
          "a live run scores every unscored number and each phone carries exactly one tier")
    check({p["number"]: p["status"] for p in crm.recs["ps1"]["owner"]["phones"]}["8655550159"] == "DEAD",
          "a Drop number is marked dead, as the playbook's scoring stage does")
    check(ledger.phone("8655550151")["tier"] == "Dial First" and live["read_back"] == 1 and not live["problems"],
          "every score is cached by number and the written tiers are read back")
    again = ps.run(_task(store, "phone_score", "ps1"), crm=crm, pol=pol, scorer=sc, now=now)
    check(len(sc.calls) == 2 and "last 7 days" in again["held"][0]["why"], "a record scored this week is not scored again")
    sc2 = FakeScorer()
    ps.run(_task(store, "phone_score", "ps2"), crm=crm, pol=pol, scorer=sc2, now=now)
    check(not sc2.calls and "Dial First" in crm.recs["ps2"]["owner"]["phones"][0]["tags"],
          "a number already scored on another record is not bought again")
    ps.run(_task(store, "phone_score", "ps3"), crm=crm, pol=pol, scorer=FakeScorer(), now=now)
    check({"Litigator", "Do Not Market"} <= set(crm.recs["ps3"]["tags"]) and {"Litigator", "Do Not Market"} <= set(crm.recs["ps3b"]["tags"]),
          "a litigator's number suppresses every record the owner holds")
    sc4 = FakeScorer()
    held = ps.run(_task(store, "phone_score", "ps4"), crm=crm, pol=pol, scorer=sc4, now=now)
    check(not sc4.calls and any("Do Not Market" in h["why"] for h in held["held"]), "a record the gate holds is not scored")
    os.environ["AGENTS_PHONE_SCORE_RUN_USD"] = "0.01"
    sc5 = FakeScorer()
    capped = ps.run(_task(store, "phone_score", "ps5"), crm=crm, pol=pol, scorer=sc5, now=now)
    check(not sc5.calls and any("left unscored" in k for k in capped["tally"]), "a score that would pass the run's cap is not bought")
    os.environ.pop("AGENTS_PHONE_SCORE_RUN_USD", None)
    check(not PHONE_RX.search(ps.render({}, live)), "the Slack text carries no full phone number")
    config.MODE = "dry"


# ---------------------------------------------------------------------- entity_principals
def _entity(check, store, ledger, pol) -> None:
    print("  entity principals")
    import buyer_sweep as bs
    from .workflows import entity_principals as ep
    from .workflows import skip_trace as st

    class Map(bs.MapClient):
        def __init__(self, rows, details):
            super().__init__(api_key="test-key", min_interval=0, sleep=lambda s: None)
            self.rows, self.details, self.log = rows, details, []

        def _request(self, path, method="GET", body=None, timeout=45.0):
            self.log.append(path)
            if path == "/properties/search/":
                return {"data": self.rows.get(body["addresses"][0]["search"], [])}
            return self.details.get(path.split("/")[-2], {})

    def biz(officers, company):
        def f(name, city_state=""):
            f.calls.append(name)
            return [{"usCorpFilings": [{"name": company.upper(), "officers": officers}]}] if officers else []
        f.calls = []
        return f

    def company(u, street, name, mail_street="9 Mail Rd", zip_="37917", tags=None):
        r = _rec(u, street, first="", last="", company=name)
        r["owner"]["address"] = {"street": mail_street, "city": "Knoxville", "state": "TN", "postal_code": zip_}
        if tags:
            r["tags"] = list(r["tags"]) + tags
        return r
    recs = [_rec("ep1", "1 Owner St"),
            company("ep2", "2 Company St", "Invented Holdings LLC"),
            company("ep5", "5 Company St", "Sample Ventures LLC", mail_street="55 Nobody Rd"),
            company("ep6", "6 Company St", "Rivet Works LLC", mail_street="66 Nobody Rd"),
            company("ep7", "7 Company St", "Pat Example Revocable Trust", mail_street="77 Nobody Rd"),
            company("ep8", "8 Company St", "Box Holdings LLC", mail_street="PO Box 12"),
            company("ep9", "9 Company St", "House Mail LLC", mail_street="9 Company St"),
            company("ep10", "10 Company St", "Wrong Zip LLC", mail_street="9 Mail Rd", zip_="37917"),
            company("ep11", "11 Company St", "Invented Holdings LLC"),
            company("ep12", "12 Company St", "Held Holdings LLC", tags=["Do Not Market"]),
            company("ep13", "13 Company St", "Cap Holdings LLC", mail_street="133 Nobody Rd")]
    crm = PeopleCRM(recs)
    crm.custom["ep11"] = {"Decision Maker": "Jo Researched"}
    rows = {"9 Mail Rd, Knoxville, TN 37917": [{"id": "h9", "address": "9 Mail Rd, Knoxville, TN 37917"}]}
    details = {"h9": {"owner_info": {"owner_name": "PAT QUINCY EXAMPLE"}}}
    now = __import__("datetime").datetime(2026, 11, 13, 15, 0, tzinfo=__import__("datetime").timezone.utc)

    def go(u, source="agent:research", officers=None, comp="", mp=None):
        f = biz(officers, comp) if officers is not None else biz([], comp)
        m = mp or Map(rows, details)
        r = ep.run(_task(store, "entity_principals", u, source=source), crm=crm, pol=pol, map_client=m, officer_search=f, now=now)
        return r, m, f

    config.MODE = "dry"
    r, m, f = go("ep1")
    check("not a company" in r["head"] and not m.log, "a person-owned record has nobody behind it to find")
    r, m, f = go("ep12")
    check(r.get("held") and not m.log and not f.calls, "a record the gate holds is never looked up or sent to a paid source")
    r, m, f = go("ep2")
    fact = ledger.fact("ep2", "entity_principal") or {}
    check(r["person"] == "Pat Quincy Example" and not crm.writes and fact.get("named") is False and "Would name" in r["head"],
          "a dry run finds the person for free, writes nothing to the record and remembers what it found")
    check(ep.principal_subject(crm.record("ep2")) is None, "a person found in a dry run is not traced: nothing is named on the record yet")
    r, m, f = go("ep8")
    check(any("PO box" in x for x in r["misses"]) and not f.calls and r.get("would_usd") == 0.10,
          "a PO box is not looked up, and a dry run only prices the business search")
    r, m, f = go("ep9")
    check(any("goes to the house itself" in x for x in r["misses"]), "a company whose mail goes to the house names nobody else")
    m10 = Map({"9 Mail Rd, Knoxville, TN 37917": [{"id": "z1", "address": "9 Mail Rd, Otherville, TN 37000"}]},
              {"z1": {"owner_info": {"owner_name": "STRANGER PERSON"}}})
    r, m, f = go("ep10", mp=m10)
    check(not r.get("person") and "/properties/detail/z1/" not in m.log,
          "the same street in another ZIP is never taken as the company's mailing house")

    config.MODE = "live"
    crm.writes.clear()
    r, m, f = go("ep2")
    fact = ledger.fact("ep2", "entity_principal") or {}
    check(crm.custom["ep2"].get("Decision Maker") == "Pat Quincy Example" and crm.custom["ep2"].get("DM Relationship") == "Principal of Invented Holdings LLC",
          "a live run names the person in Decision Maker and DM Relationship")
    check(r["read_back"] == "ok" and r["named"] and fact.get("named") and (ledger.fact("ep2", "entity_pending") or {}).get("named"),
          "the write is read back before the record counts as named")
    check(any(u == "ep2" and "Person behind Invented Holdings LLC" in txt and not PHONE_RX.search(txt) for u, txt in crm.msgs),
          "one note says how the person was found, with no phone number in it")
    ch = [c for c in ledger.changes(record_uuid="ep2") if c["what"] == "custom_field"]
    check(len(ch) == 2 and ch[0]["before"]["value"] == "" and ch[0]["before"]["field_uuid"] == "f-dm",
          "each field written records the value it replaced, so undo can put it back")
    subj = ep.principal_subject(crm.record("ep2"))
    named = ep.as_principal_record(crm.record("ep2"))
    check(subj and (subj.first, subj.last, subj.mail_street) == ("Pat", "Example", "9 Mail Rd") and subj.owner_uuid == "o-ep2",
          "the named person is traced at the house the company's mail goes to, and the numbers land on the company's owner")
    check(named["owner"]["uuid"] == "o-ep2" and named["owner"]["first_name"] == "Pat" and not named["owner"]["company"],
          "the record shown to the retrace keeps the owner's uuid and flags; only the name and address change")
    m_before = len(m.log)
    r2, m2, _ = go("ep2", mp=m)
    check(r2["named"] and "nothing to do" in r2["head"] and len(m.log) == m_before,
          "a person already named on the record is not looked up again")
    r, m, f = go("ep11")
    check(r.get("kept") == "Jo Researched" and crm.custom["ep11"]["Decision Maker"] == "Jo Researched",
          "a decision maker a person or the research named is never overwritten")

    lawyer = [{"name": {"nameRaw": "QUINN FARAWAY"}, "title": "MEMBER", "address": {"fullAddress": "1 Far Rd; Dover, DE 19901"}},
              {"name": {"nameRaw": "LEE COUNSEL"}, "title": "REGISTERED AGENT", "address": {"fullAddress": "5 Law Sq; Knoxville, TN 37902"}}]
    t5 = _task(store, "entity_principals", "ep5")
    f5 = biz(lawyer, "Sample Ventures LLC")
    r = ep.run(t5, crm=crm, pol=pol, map_client=Map(rows, details), officer_search=f5, now=now)
    check(f5.calls == ["Sample Ventures LLC"] and not r.get("person") and r.get("low_confidence", {}).get("person") == "LEE COUNSEL",
          "the paid search runs live only after the free check, and a lawyer-shaped agent is reported, never named")
    check("Decision Maker" not in crm.custom.get("ep5", {}) and abs(store.spent(task_id=t5["id"]) - 0.10) < 1e-9,
          "an out-of-state officer is dropped, and a search that returned records is charged to its task")
    member = [{"name": {"nameRaw": "RIVET ROWAN"}, "title": "MEMBER", "address": {"fullAddress": "40 Officer Way; Knoxville, TN 37919"}}]
    r, m, f = go("ep6", officers=member, comp="Rivet Works LLC")
    s6 = ep.principal_subject(crm.record("ep6"))
    check(r["person"] == "Rowan Rivet" and crm.custom["ep6"]["DM Relationship"] == "Member of Rivet Works LLC"
          and s6 and (s6.first, s6.last, s6.mail_street, s6.mail_zip) == ("Rowan", "Rivet", "40 Officer Way", "37919"),
          "an in-state member is named, in first-last order, and traced at the officer's own address")
    r, m, f = go("ep7", officers=member, comp="Pat Example Revocable Trust")
    check(not f.calls and any("trustee" in x for x in r["misses"]), "a trust is never sent to the business search; its trustee is research")
    os.environ["AGENTS_PRINCIPALS_RUN_USD"] = "0.05"
    r, m, f = go("ep13", officers=member, comp="Cap Holdings LLC")
    check(not f.calls and any("cap" in x for x in r["misses"]), "a business search that would pass the run's cap is not bought")
    os.environ.pop("AGENTS_PRINCIPALS_RUN_USD", None)
    quiet_r, _, _ = go("ep13", source="autopilot:ftm_retrace")
    check(ep.render({}, quiet_r) == "", "a handoff from the autopilot that named nobody posts nothing")
    check(ep.render({}, r) != "" and not PHONE_RX.search(ep.render({}, r)), "an ask from Slack always gets an answer, with no phone number")

    # The loop the retrace hands off: once named, a skip trace traces the PERSON, onto the company's owner.
    s = {"smartskip": SubjectSource("smartskip", 0.15, "match", {"ep2": ["8655550221"]}),
         "tracerfy": SubjectSource("tracerfy", 0.02, "row", {})}
    crm.writes.clear()
    st.run(_task(store, "skip_trace", "ep2"), crm=crm, pol=pol, sources=s, scorer=FakeScorer(), now=now, enqueue=lambda *a, **k: None)
    check(s["smartskip"].subjects == [("ep2", "Pat", "Example", "9 Mail Rd")] and any(w[0] == "upsert" and w[1] == "o-ep2" for w in crm.writes),
          "once a person is named, the skip trace looks for that person and the numbers land on the company's owner")
    config.MODE = "dry"


# ---------------------------------------------------------------------- probate_property
def _probate(check, store) -> None:
    print("  probate property")
    import property_lookup as pl
    from .workflows import probate_property as pp

    knox_roll = {
        "EXAMPLE DORA MAY": [{"owner": "EXAMPLE DORA M", "parcel_address": "12 Probate Ln", "account_number": "K1"},
                             {"owner": "EXAMPLE PAT", "parcel_address": "14 Probate Ln", "account_number": "K2"},
                             {"owner": "EXAMPLE DORA", "parcel_address": None, "account_number": "B9"}],
        "SAMPLE IVY ROSE": [{"owner": "SAMPLE IVAN", "parcel_address": "50 Other Rd", "account_number": "K5"}],
        "SAMPLE JO": [{"owner": "SAMPLE JO", "parcel_address": "30 Exec Rd", "account_number": "K30"},
                      {"owner": "SAMPLE IVY ROSE & SAMPLE JO", "parcel_address": "31 Shared Rd", "account_number": "K31"}],
    }
    asked = []

    def knox(q):
        asked.append(q)
        return knox_roll.get(q, [])

    def tpad(q):
        return [{"owner": "UPTON NED", "address": "LAKE VIEW DR 5705", "classification": "Residential", "parcel_id": "B1"}] \
            if q.startswith("UPTON NED") else []

    class Map:
        def __init__(self):
            self.calls = 0

        def search(self, body):
            self.calls += 1
            return {"data": [{"id": "x1", "address": "12 Probate Ln, Knoxville, TN 37917", "county": "Blount", "state": "TN"},
                             {"id": "x0", "address": "16 Probate Ln, Powell, TN 37849", "county": "Knox", "state": "TN"},
                             {"id": "x2", "address": "12 Probate Ln, Powell, TN 37849", "county": "Knox", "state": "TN"}]}

    noop = lambda: None  # noqa: E731
    k = pp.find("Dora May Example", "Knox", pl=pl, knox=knox, map_client=Map(), sleep=noop)
    f = k["found"] or {}
    check(f.get("street") == "12 Probate Ln" and f.get("parcel_id") == "K1" and k["rejected"] == 2,
          "a house counts only when the decedent is on the title; a stranger and a no-street account are turned away")
    check(f.get("city") == "Powell" and f.get("zip") == "37849",
          "the city and ZIP come from the same house number and street inside the same county, never a neighbor or another county")
    e = pp.find("Ivy Rose Sample", "Knox", "Jo Sample", pl=pl, knox=knox, sleep=noop)
    check((e["found"] or {}).get("street") == "31 Shared Rd" and e["found"]["tier"] == "executor"
          and any(c["street"] == "30 Exec Rd" and not c["kept"] for c in e["candidates"]),
          "the executor's search keeps a house the decedent co-owns and never the executor's own")
    b = pp.find("Ned Upton", "Blount", pl=pl, tpad=tpad, sleep=noop)
    check((b["found"] or {}).get("street") == "5705 LAKE VIEW DR", "a Blount address printed number-last is turned around")
    n = pp.find("Zed Nobody", "Knox", pl=pl, knox=knox, sleep=noop)
    check(n["found"] is None and "no property found" in n["miss"], "nothing found is said honestly, never guessed")
    s = pp.find("Ivy Rose Sample", "Knox", pl=pl, knox=knox, sleep=noop)
    check(s["found"] is None and "1 county result" in s["miss"], "a miss says how many county results were turned away")
    t = _task(store, "probate_property", inputs={"decedent": "Dora May Example", "county": "Knox"})
    res = pp.run(t, pl=pl, knox=knox, tpad=tpad, map_client=Map(), sleep=noop)
    out = Path(res["run_dir"]) / "files" / res["file"]
    check(res["ok"] and "12 Probate Ln" in res["head"] and out.is_file() and len(json.loads(out.read_text())["results"][0]["candidates"]) == 3,
          "the answer names the house and a file lists every candidate with why it was kept or turned away")
    both = pp.run(_task(store, "probate_property", inputs={"decedent": "Dora May Example", "county": "both"}),
                  pl=pl, knox=knox, tpad=tpad, sleep=noop)
    check([r["county"] for r in both["results"]] == ["Knox", "Blount"], "an unknown county searches both rolls")
    bad = pp.run(_task(store, "probate_property", inputs={"decedent": "Dora May Example", "county": "Sevier"}), pl=pl)
    check(not bad["ok"] and "Knox and Blount" in bad["say"], "a county the workflow has no roll for is refused in words")
    ex = pp.run(_task(store, "probate_property", inputs={"decedent": "Ivy Rose Sample", "county": "Knox"},
                      details="Personal representative: Jo Sample"), pl=pl, knox=knox, sleep=noop)
    check(ex["executor"] == "Jo Sample" and ex["results"][0]["found"]["tier"] == "executor",
          "an executor named in the details is used for the co-owned search")


# ---------------------------------------------------------------------- buyer_registry
class FakeDispo:
    """The parts of dispo_buyers the nightly job drives, with counters instead of SiftMap."""

    def __init__(self, details_left=150, unmask=80, principals_todo=4, trace_rows=3):
        self.OUT = Path(".")
        self.STATE_PATH = Path("state.json")
        self.details_left, self.unmask, self.principals_todo, self.trace_n = details_left, unmask, principals_todo, trace_rows
        self.calls = []

    def use_dir(self, path):
        self.OUT = Path(path)
        self.STATE_PATH = self.OUT / "state.json"
        self.OUT.mkdir(parents=True, exist_ok=True)
        return self.OUT

    def _load(self, p, default):
        p = Path(p)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    def _save(self, p, obj):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(json.dumps(obj), encoding="utf-8")

    def state(self):
        return self._load(self.STATE_PATH, {})

    def mark_done(self, phase, **facts):
        s = self.state()
        s[phase] = facts
        self._save(self.STATE_PATH, s)

    def hydrate_todo(self):
        return ["p%d" % i for i in range(self.details_left)]

    def unmask_left(self, floor=0):
        return self.unmask

    def phase_sweep(self, args):
        self.calls.append(("sweep",))
        (self.OUT / "rows.json").write_text("{}", encoding="utf-8")
        self.mark_done("sweep", rows=10)

    def phase_recent(self, args):
        self.calls.append(("recent",))
        self.mark_done("recent", buyers=12)

    def phase_hydrate(self, args):
        self.calls.append(("hydrate", args.limit, args.workers))
        self.details_left = max(0, self.details_left - args.limit)

    def phase_aggregate(self, args):
        self.calls.append(("aggregate",))

    def phase_unmask(self, args):
        self.calls.append(("unmask", args.limit, args.unmask_floor))
        self.unmask = max(0, self.unmask - args.limit)

    def phase_principals(self, args):
        self.calls.append(("principals", args.commit, args.limit))
        if not args.commit:
            self.mark_done("principals_plan", todo=self.principals_todo, usd=round(self.principals_todo * 0.1, 2))
            return
        k = min(args.limit, self.principals_todo)
        self.principals_todo -= k
        self.mark_done("principals", attempted=k, resolved=k, billed=k, usd=round(k * 0.10, 2))

    def phase_profiles(self, args):
        self.calls.append(("profiles",))

    def phase_crm_phones(self, args):
        self.calls.append(("crm_phones", args.limit, args.skip_read_days))
        args.crm.get_record("rb1")
        self.mark_done("crm_phones", records=1, left_over=0)

    def phase_skipinput(self, args):
        self.calls.append(("skipinput",))
        if not self.trace_n:
            raise SystemExit("GATE FAILED: skipinput produced no traceable people")
        with (self.OUT / "smartskip_input.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["buyer_key", "First Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip"])
            for i in range(self.trace_n):
                w.writerow(["BUYER%d" % i, "Casey", "Buyer%d" % i, "%d Buyer Rd" % (i + 1), "Knoxville", "TN", "37917"])

    def phase_score(self, args):
        self.calls.append(("score", args.commit, args.score_limit))
        phones = self._load(self.OUT / "phones.json", {})
        cache = self._load(self.OUT / "phone_scores.json", {})
        nums = sorted({p["number"] for v in phones.values() for p in v["phones"]})
        todo = [n for n in nums if n not in cache][:args.score_limit]
        for n_, r in args.scorer.score(todo).items():
            cache[n_] = {"score": r["score"], "tier": r["tier"], "litigator": r["litigator"], "line_type": r["line_type"]}
        self._save(self.OUT / "phone_scores.json", cache)
        self.mark_done("score_registry", newly_scored=len(todo))


class TraceSource:
    name, unit_usd = "smartskip", 0.15

    def __init__(self):
        self.subjects = []

    def configured(self):
        return True

    def trace(self, subjects):
        self.subjects += [(s.record_uuid, s.first, s.last, s.mail_street) for s in subjects]
        return {s.record_uuid: [{"number": "86555503%02d" % (i * 10 + 1), "type": "MOBILE"}] for i, s in enumerate(subjects)}, \
            round(0.15 * len(subjects), 2)


def _registry(check, store, ledger, tmp) -> None:
    print("  buyer registry")
    import enformion_business as eb
    from .workflows import buyer_registry as br
    from datetime import datetime, timedelta, timezone

    os.environ.update({"AGENTS_BUYER_REGISTRY_DETAILS": "100", "AGENTS_BUYER_REGISTRY_UNMASK": "60",
                       "AGENTS_BUYER_REGISTRY_RUN_USD": "0.60", "AGENTS_BUYER_REGISTRY_COLD": "1000"})
    eb.is_configured = lambda: True
    crm = PeopleCRM([_rec("rb1", "1 Buyer Rd")])
    now = datetime(2026, 11, 13, 7, 15, tzinfo=timezone.utc)
    d = FakeDispo()
    config.MODE = "dry"
    sched = lambda: _task(store, "buyer_registry", source="schedule")  # noqa: E731
    first = br.run(sched(), dispo=d, crm=crm, now=now)
    check(Path(d.OUT) == Path(config.DATA_DIR) / "dispo_buyers", "the registry lives on the brain's volume, not in the image")
    check(("hydrate", 100, 1) in d.calls and ("aggregate",) not in d.calls and not first["finished"],
          "a night reads at most its cap of deeds, one worker at a time, and stops there")
    check(br.render({"source": "schedule"}, first) == "", "a scheduled night that only moved a capped step forward posts nothing")
    d.calls.clear()
    second = br.run(sched(), dispo=d, crm=crm, now=now + timedelta(days=1))
    check(("sweep",) not in d.calls and ("hydrate", 100, 1) in d.calls and ("unmask", 60, 2) in d.calls,
          "the next night resumes where the last one stopped, and the free check takes repeat buyers only")
    check(not any(c[0] == "principals" and c[1] for c in d.calls) and second["usd"] == 0,
          "a dry box runs every free step and buys nothing")
    d.calls.clear()
    third = br.run(sched(), dispo=d, crm=crm, now=now + timedelta(days=2))
    check(third["finished"] and third["would_usd"] > 0 and "would" in br.render({"source": "schedule"}, third),
          "a finished dry refresh says what the paid steps would cost")
    quiet = br.run(sched(), dispo=d, crm=crm, now=now + timedelta(days=3))
    check(quiet.get("quiet") and br.render({"source": "schedule"}, quiet) == "", "a finished refresh waits a month before the next")

    d2 = FakeDispo(details_left=0, unmask=0, principals_todo=4, trace_rows=3)
    os.environ["AGENTS_BUYER_REGISTRY_DIR"] = str(tmp / "registry_live")
    phones = {"OLD": {"phones": [{"number": "8655550501"}, {"number": "8655550511"}, {"number": "8655550521"}]}}
    d2.use_dir(tmp / "registry_live")
    d2._save(d2.OUT / "phones.json", phones)
    d2._save(d2.OUT / "phone_scores.json", {"8655550501": {"score": 90, "tier": "Dial First"}})
    ledger.set_phone("8655550511", 88, "Dial First", "Mobile", False)
    config.MODE = "live"
    ts = TraceSource()
    sc = FakeScorer()
    task = _task(store, "buyer_registry", source="agent:research")
    live = br.run(task, dispo=d2, crm=crm, now=now, trace_source=ts, scorer=sc)
    check(("principals", True, 4) in d2.calls and abs(store.spent(task_id=task["id"]) - live["usd"]) < 1e-6,
          "on a live box the paid steps run, and every dollar is charged to the task")
    check(live["usd"] <= 0.60 + 1e-9, "the paid steps together stay inside the run's cap")
    check(len(ts.subjects) <= 1 and any(s == "trace" and "wait" in note for s, note in live["tonight"]),
          "a paid step that runs out of money leaves the rest for tomorrow")
    check(("profiles",) in d2.calls and ("crm_phones", 150, 30) in d2.calls and sc.calls,
          "a paid step out of money does not hold up the steps after it (the trace ran short and scoring still ran)")
    check("8655550511" not in sc.calls and d2._load(d2.OUT / "phone_scores.json", {})["8655550511"]["from"] == "autopilot cache",
          "a number the autopilot already scored is copied in for free, never bought twice")
    check(ledger.phone("8655550501") is None, "an older registry score taken without the litigator check never enters the shared cache")
    check((ledger.phone("8655550521") or {}).get("tier") == "Dial First",
          "a number scored tonight, with the litigator check, joins the shared cache so no stage buys it again")
    d3 = FakeDispo(details_left=5000)
    os.environ["AGENTS_BUYER_REGISTRY_DIR"] = str(tmp / "registry_cold")
    cold = br.run(_task(store, "buyer_registry", source="agent:research"), dispo=d3, crm=crm, now=now)
    check(not any(c[0] == "hydrate" for c in d3.calls) and any("Seed the cache" in p for p in cold["problems"]),
          "a box with no deed cache refuses to read thousands of deeds and says to seed it")
    d4 = FakeDispo(details_left=0, unmask=0)
    os.environ["AGENTS_BUYER_REGISTRY_DIR"] = str(tmp / "registry_clock")
    ticks = iter([0, 0, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000])
    late = br.run(_task(store, "buyer_registry", source="agent:research"), dispo=d4, crm=crm, now=now, clock=lambda: next(ticks))
    check(any(s == "time" for s, _ in late["tonight"]) and not late["finished"], "a night that runs out of time stops and resumes next time")
    eb.is_configured = lambda: False
    d5 = FakeDispo(details_left=0, unmask=0)
    os.environ["AGENTS_BUYER_REGISTRY_DIR"] = str(tmp / "registry_nokey")
    d5.use_dir(tmp / "registry_nokey")
    d5.mark_done("principals", attempted=9, resolved=9, billed=9, usd=0.9)
    t5 = _task(store, "buyer_registry", source="agent:research")
    br.run(t5, dispo=d5, crm=crm, now=now, trace_source=TraceSource(), scorer=FakeScorer())
    check(not any(c[0] == "principals" for c in d5.calls) and store.spent(task_id=t5["id"]) < 0.9,
          "with no business search key nothing is looked up, and an old result is never charged again")
    for k in ("AGENTS_BUYER_REGISTRY_DETAILS", "AGENTS_BUYER_REGISTRY_UNMASK", "AGENTS_BUYER_REGISTRY_RUN_USD",
              "AGENTS_BUYER_REGISTRY_COLD", "AGENTS_BUYER_REGISTRY_DIR"):
        os.environ.pop(k, None)
    config.MODE = "dry"


# ---------------------------------------------------------------------- hygiene
MINE = ("agents/workflows/skip_trace.py", "agents/workflows/phone_score.py", "agents/workflows/entity_principals.py",
        "agents/workflows/probate_property.py", "agents/workflows/buyer_registry.py", "agents/selftest_wf_people.py",
        "buyer_sweep.py", "enformion_business.py", "dispo_buyers.py")


def _hygiene(check, tmp) -> None:
    print("  hygiene")
    texts = {p: (SRC / p).read_text(encoding="utf-8") for p in MINE}
    bad = [p for p, t in texts.items() if "\u2014" in t or "\u2013" in t or "\x08" in t]
    check(not bad, "no em dash, en dash or backspace byte in these files: %s" % (bad or "ok"))
    # The API audit's own scan, so these checks mean exactly what its grades mean (prose about what
    # was removed is not a dependency; code is).
    audit_path = SRC.parent / "tools" / "datasift_api_audit.py"
    if audit_path.is_file():
        import importlib.util
        spec = importlib.util.spec_from_file_location("datasift_api_audit_selftest", audit_path)
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        scans = {p: audit.scan_module(SRC / p) for p in MINE if not p.startswith("agents/selftest")}
        check(not any({"deal_room", "deal_room_optional", "desktop_path", "jwt", "password", "browser"} & set(s["auth"])
                      for s in scans.values()),
              "none of these modules needs a Deal Room checkout, a path on one machine, a password or a browser: %s"
              % ({p: s["auth"] for p, s in scans.items() if {"deal_room", "desktop_path", "jwt", "password", "browser"} & set(s["auth"])}
                 or "ok"))
        check(not any("siftmap_autocomplete" in s["risks"] for p, s in scans.items()),
              "none of these modules calls SiftMap's autocomplete")
    check(not any(".autocomplete(" in texts[p] for p in ("buyer_sweep.py", "dispo_buyers.py")),
          "neither the sweep nor the registry calls SiftMap's autocomplete")
    code = ("import importlib.abc, sys\n"
            "class Block(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in ('playwright', 'bs4'):\n"
            "            raise ImportError('not on this box: ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            "sys.path.insert(0, %r)\n"
            "import buyer_sweep, enformion_business, dispo_buyers\n"
            "from agents.workflows import skip_trace, phone_score, entity_principals, buyer_registry, probate_property\n"
            "pl, why = probate_property.lookup_module()\n"
            "assert pl is None and 'beautifulsoup4' in why, why\n"
            "print('loaded')\n") % str(SRC)
    env = dict(os.environ, AGENTS_DATA_DIR=str(tmp / "subproc"), AGENTS_MODE="dry")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180, env=env)
    check(r.returncode == 0 and "loaded" in r.stdout,
          "every people workflow loads on a box with no browser; the probate lookup says what it lacks: %s"
          % ((r.stderr or r.stdout).strip().splitlines()[-1:] if r.returncode else "ok"))
