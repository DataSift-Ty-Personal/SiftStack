"""The texting app's Smarter Contact loader (src/sms_agent/sc_load.py), against a fake Smarter Contact.

Zero network: FakeSC answers the GraphQL documents the loader sends, by the operation in them.
Every name, street and number here is invented (555 numbers).
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SMS_AGENT_DATA_DIR", tempfile.mkdtemp(prefix="sc_load_test_"))

from sms_agent import sc_client, sc_load  # noqa: E402

NOW = datetime(2026, 11, 19, 13, 30, tzinfo=timezone.utc)      # 8:30am Eastern


class FakeSC:
    def __init__(self, *, blocked: bool = False, approved: bool = True, templates=None, existing=None, suppressed=(),
                 groups=None):
        self.phones = [{"id": "p%d" % i, "status": "APPROVED" if approved else "PENDING", "number": "+1865555%04d" % i,
                        "isBlocked": blocked, "messagingEnabled": True} for i in range(1, 4)]
        self.templates = [{"id": "t-" + n, "name": n} for n in (templates if templates is not None else
                                                                 ["P1 T1 Named 01", "P1 T1 Named 02", "P1 T1 No Name 01"])]
        self.existing = dict(existing or {})          # ten digits -> contact id
        self.suppressed = list(suppressed)
        self.groups = dict(groups or {})              # name -> {id, contacts, campaigns}
        self.calls: list[tuple[str, dict]] = []
        self.created, self.campaigns = [], []
        self.lookups: list[str] = []                  # numbers looked up by phone
        self.dnc: list[str] = []                      # contact ids put on the do-not-contact list
        self.lookup_fail: set[str] = set()            # ten digits whose lookup raises
        self.dnc_fail: set[str] = set()               # contact ids whose do-not-contact write raises
        self.dnc_already: set[str] = set()            # contact ids Smarter Contact says are already on its list

    def contact_id_for(self, phone):
        n = sc_load.digits10(phone)
        self.lookups.append(n)
        if n in self.lookup_fail:
            raise sc_client.SCError("HTTP 503 from https://api.example/gql: b'busy'")
        return self.existing.get(n, "")

    def add_contact_dnc(self, contact_id):
        if contact_id in self.dnc_fail:
            raise sc_client.SCError("HTTP 500 from https://api.example/gql: b'oops'")
        if contact_id in self.dnc_already:
            return "already"
        self.dnc.append(contact_id)
        return "DNC"

    def thread(self, contact_id, limit=12):
        return [{"dir": "out", "body": "an old text"}] if contact_id in getattr(self, "texted", set()) else []

    def gql(self, query, variables=None):
        v = variables or {}
        op = query.split("{", 2)[1] if "{" in query else query
        self.calls.append((query.split("(")[0], v))
        if "findOwnPhones" in query:
            return {"findOwnPhones": {"items": self.phones}}
        if "findMessageTemplates" in query:
            return {"findMessageTemplates": {"items": self.templates, "hasNext": False}}
        if "findFields" in query:
            return {"findFields": [{"id": "f-addr", "key": "ADDRESS", "name": "Address"},
                                   {"id": "f-sift", "key": None, "name": sc_client.SIFT_RECORD_FIELD}]}
        if "findGroups" in query:
            return {"findGroups": {"items": [dict(g, name=n) for n, g in self.groups.items()], "hasNext": False}}
        if "findSuppressions" in query:
            return {"findSuppressions": {"items": [{"number": "+1" + n} for n in self.suppressed], "hasNext": False}}
        if "createContact" in query:
            cid = "c-%d" % (len(self.created) + 1)
            self.created.append(v["input"])
            self.existing[sc_load.digits10(v["input"]["phone"])] = cid
            return {"createContact": {"id": cid, "phone": v["input"]["phone"]}}
        if "createGroup" in query:
            name = v["input"]["name"]
            self.groups[name] = {"id": "g-" + name, "contacts": 0, "campaigns": []}
            return {"createGroup": {"id": "g-" + name, "name": name}}
        if "addGroupToContacts" in query:
            for g in self.groups.values():
                if g["id"] == v["input"]["groupId"]:
                    g["contacts"] += len(v["input"]["ids"])
            return {"addGroupToContacts": {"success": True}}
        if "createCampaign" in query:
            self.campaigns.append(v["input"])
            return {"createCampaign": {"id": "k-%d" % len(self.campaigns), "name": v["input"]["name"]}}
        if "startCampaign" in query:
            return {"startCampaign": {"success": True}}
        if "getCampaign" in query:
            return {"getCampaign": {"id": v["id"], "state": {"status": "SCHEDULED"}}}
        raise AssertionError("unexpected query: %s" % op[:60])


def a_load(batch=11):
    group = {"name": "FTM 1119 Named East", "series": "opening", "step": 1, "audience": "named", "tz": "east",
             "template_set": "p1", "touch": 1, "templates": ["P1 T1 Named 01", "P1 T1 Named 02"],
             "start": "2026-11-19T14:15:00+00:00", "contacts": 2}
    contacts = [{"group": group["name"], "phone": "8655550101", "record": "r1", "first_name": "Pat", "last_name": "Example",
                 "values": {"ADDRESS": "11 Example St", "SIFT_RECORD": "r1"}},
                {"group": group["name"], "phone": "8655550102", "record": "r1", "first_name": "Pat", "last_name": "Example",
                 "values": {"ADDRESS": "11 Example St", "SIFT_RECORD": "r1"}}]
    return {"batch": batch, "groups": [group], "contacts": contacts}


def writes(sc):
    return [c for c, _ in sc.calls if c.startswith("mutation")]


def test_no_approved_number_refuses_before_any_write():
    sc = FakeSC(approved=False)
    out = sc_load.run(a_load(), commit=True, sc=sc, now=NOW, pause=0)
    assert out["ok"] is False and "no Smarter Contact number can send" in out["refused"] and "3 not approved" in out["refused"]
    assert writes(sc) == [] and out["loaded"] == []


def test_the_isblocked_flag_alone_does_not_stop_a_send():
    # 2026-11-18: every number read isBlocked while the app showed them Approved and replies went out from them.
    sc = FakeSC(blocked=True)
    out = sc_load.run(a_load(), commit=True, sc=sc, now=NOW, pause=0)
    assert out["ok"] and sorted(out["loaded"]) == ["8655550101", "8655550102"] and sc.campaigns[0]["ownPhones"] == ["p1", "p2", "p3"]


def test_a_dry_run_with_no_approved_number_still_plans_the_rest():
    out = sc_load.run(a_load(), commit=False, sc=FakeSC(approved=False), now=NOW, pause=0)
    assert "refused" not in out and any("a real load would be refused here" in p for p in out["problems"])
    assert out["groups"][0]["state"].startswith("would create")


def test_missing_template_refuses_before_any_write():
    sc = FakeSC(templates=["P1 T1 Named 01"])
    out = sc_load.run(a_load(), commit=True, sc=sc, now=NOW, pause=0)
    assert "P1 T1 Named 02" in out["refused"] and writes(sc) == []


def test_dry_run_plans_and_writes_nothing():
    sc = FakeSC()
    out = sc_load.run(a_load(), commit=False, sc=sc, now=NOW, pause=0)
    assert out["dry"] and writes(sc) == [] and out["contacts_new"] == 2 and out["loaded"] == []
    assert out["groups"][0]["state"].startswith("would create")


def test_commit_creates_contacts_group_and_one_started_campaign():
    sc = FakeSC(existing={"8655550102": "old-7"})
    out = sc_load.run(a_load(), commit=True, sc=sc, now=NOW, pause=0)
    assert out["ok"] and out["contacts_new"] == 1 and out["contacts_found"] == 1
    assert sorted(out["loaded"]) == ["8655550101", "8655550102"]
    new = sc.created[0]
    assert new["phone"] == "+18655550101" and {"fieldId": "f-sift", "value": "r1"} in new["attrs"]
    assert sc.groups["FTM 1119 Named East"]["contacts"] == 2
    camp = sc.campaigns[0]
    seq, sched = camp["sequences"][0], camp["scheduleOptions"]
    assert camp["type"] == "STANDARD" and camp["groups"] == ["g-FTM 1119 Named East"] and camp["ownPhones"] == ["p1", "p2", "p3"]
    assert seq["templates"] == ["t-P1 T1 Named 01", "t-P1 T1 Named 02"] and seq["days"] == [1, 2, 3, 4, 5]
    assert seq["timeRange"] == {"from": {"hour": 9, "minute": 0}, "to": {"hour": 18, "minute": 0}}
    assert sched["startAt"] == "2026-11-19T14:15:00.000Z" and sched["perBatch"] == 6 and sched["frequency"] == "MINUTELY"
    assert out["groups"][0]["campaign"] == "k-1" and out["groups"][0]["state"] == "SCHEDULED"


def test_other_time_zone_window_opens_at_noon_and_a_past_start_moves_to_now():
    g = dict(a_load()["groups"][0], tz="other", start="2026-11-19T10:00:00+00:00")
    inp = sc_load.campaign_input(g, "g1", ["p1"], ["t1"], NOW)["input"]
    assert inp["sequences"][0]["timeRange"]["from"]["hour"] == 12
    assert inp["scheduleOptions"]["startAt"] == "2026-11-19T13:30:00.000Z"


def test_suppressed_number_and_a_group_already_sent_are_left_out():
    sc = FakeSC(suppressed=["8655550101"])
    out = sc_load.run(a_load(), commit=True, sc=sc, now=NOW, pause=0)
    assert out["loaded"] == ["8655550102"] and out["skipped"] == {"on Smarter Contact's own do-not-text list": 1}
    sc2 = FakeSC(groups={"FTM 1119 Named East": {"id": "g-x", "contacts": 2, "campaigns": [{"name": "FTM 1119 Named East"}]}})
    out2 = sc_load.run(a_load(), commit=True, sc=sc2, now=NOW, pause=0)
    assert out2["loaded"] == [] and sc2.campaigns == [] and "a load sent twice" in next(iter(out2["skipped"]))


def test_a_bad_load_is_refused():
    assert sc_load.check({"batch": 3, "groups": []}) == ["the load names no group"]
    assert "the load has no batch number" in sc_load.check({"groups": [{"name": "x"}]})
    out = sc_load.run({"batch": 3, "groups": []}, commit=True, sc=FakeSC(), now=NOW, pause=0)
    assert out["refused"] == "the load names no group"


def test_group_lookup_is_by_exact_name_and_paging_cannot_loop():
    sc = FakeSC(groups={"FTM 1119 Named East": {"id": "g-a", "contacts": 2, "campaigns": []},
                        "FTM 1119 Named East T2": {"id": "g-b", "contacts": 1, "campaigns": []}})
    assert sc_load.find_group("FTM 1119 Named East", sc=sc)["id"] == "g-a"
    assert sc_load.find_group("FTM 1120 Named East", sc=sc) == {}

    class Stuck(FakeSC):
        def gql(self, query, variables=None):
            self.calls.append(("q", variables or {}))
            return {"findSuppressions": {"items": [{"number": "+18655550101"}], "hasNext": True, "nextId": "same"}}
    stuck = Stuck()
    rows = sc_load._pages(sc_load.Q_SUPP, "findSuppressions", sc=stuck)
    assert len(rows) == 1 and len(stuck.calls) == 2      # the second page brought nothing new: stop


def test_the_load_route_wants_the_bearer_token_in_a_header(monkeypatch):
    from fastapi.testclient import TestClient
    from sms_agent import config, receiver
    monkeypatch.setattr(config, "SC_LOAD_SECRET", "tok-example-123")
    monkeypatch.setattr(config, "INLINE_WORKER", False)
    with TestClient(receiver.app) as client:
        assert client.post("/hooks/scload", json=a_load()).status_code == 404
        assert client.post("/hooks/scload", json=a_load(), headers={"authorization": "Bearer nope"}).status_code == 404
        assert client.post("/hooks/scload", json={"batch": 1}, headers={"authorization": "Bearer tok-example-123"}).status_code == 400
        assert client.get("/hooks/scload/7", headers={"authorization": "Bearer tok-example-123"}).json()["status"] == "unknown"
        assert client.post("/hooks/tok-example-123/scload", json=a_load()).status_code in (404, 405)


def test_text_1_skips_a_number_already_texted_in_smarter_contact():
    sc = FakeSC(existing={"8655550102": "old-7", "8655550101": "old-8"})
    sc.texted = {"old-7"}                      # an October campaign reached this one
    out = sc_load.run(a_load(), commit=True, sc=sc, now=NOW, pause=0)
    assert out["loaded"] == ["8655550101"] and sc.groups["FTM 1119 Named East"]["contacts"] == 1
    assert any("texted before in Smarter Contact" in k for k in out["skipped"])
    assert out["texted_before"] == ["8655550102"]       # named, so the brain can remember it and never stage it again
    later = a_load(12)
    later["groups"][0].update(name="FTM 1119 Named East T2", step=2)
    for c in later["contacts"]:
        c["group"] = "FTM 1119 Named East T2"
    out2 = sc_load.run(later, commit=True, sc=sc, now=NOW, pause=0)
    assert sorted(out2["loaded"]) == ["8655550101", "8655550102"]      # text 2 follows its own thread
    assert out2["texted_before"] == []


def test_only_the_texted_before_skip_is_named_as_texted_before():
    sc = FakeSC(existing={"8655550102": "old-7"}, suppressed=["8655550101"])
    sc.texted = {"old-7"}
    out = sc_load.run(a_load(), commit=True, sc=sc, now=NOW, pause=0)
    # the suppressed number is left out for its own reason and is not named as texted before
    assert out["texted_before"] == ["8655550102"] and out["loaded"] == []
    assert out["skipped"]["on Smarter Contact's own do-not-text list"] == 1


def test_accept_never_loads_one_batch_twice(monkeypatch, tmp_path):
    monkeypatch.setattr(sc_load, "STATE_DIR", tmp_path)
    sc_load._save(21, {"batch": 21, "status": "done"})
    assert sc_load.accept(a_load(21)) == {"accepted": True, "already": "done", "batch": 21}
    assert sc_load.accept({"batch": "x"})["accepted"] is False


# ---------------------------------------------------------------------- the revocation stage's jobs
JOB_NO = 1_763_560_000_123          # the brain numbers its jobs in milliseconds since 1970


def a_job(batch=JOB_NO, job="j-example-1"):
    """One owner who opted out (two numbers in Smarter Contact, one already on its list, one it has
    never seen) and one owner who just became a lead."""
    return {"action": "revoke", "batch": batch, "job": job,
            "owners": [{"record": "r1", "dnc": True,
                        "numbers": ["8655550101", "(865) 555-0102", "8655550104", "8655550105"]},
                       {"record": "r2", "dnc": False, "numbers": ["8655550103"]}]}


def outcomes(out):
    return {row["number"]: row["outcome"] for row in out["numbers"]}


def revoke_sc(**kw):
    return FakeSC(existing={"8655550101": "c-1", "8655550102": "c-2", "8655550103": "c-3", "8655550104": "c-4"},
                  suppressed=["8655550104"], **kw)


def test_a_revoke_job_marks_every_number_and_only_looks_up_a_lead():
    sc = revoke_sc()
    out = sc_load.revoke_run(a_job(), commit=True, sc=sc, pause=0)
    assert out["ok"] and out["dry"] is False and out["action"] == "revoke" and out["job"] == "j-example-1"
    assert outcomes(out) == {"8655550101": "marked", "8655550102": "marked", "8655550104": "already",
                             "8655550105": "not_in_sc", "8655550103": "lead"}
    assert sc.dnc == ["c-1", "c-2"]                       # the lead's contact is never put on the list
    assert "8655550104" not in sc.lookups                 # already on the list: no lookup and no write
    assert out["counts"] == {"marked": 2, "already": 1, "not_in_sc": 1, "lead": 1}
    assert {row["record"] for row in out["numbers"] if row["number"] == "8655550103"} == {"r2"}
    assert writes(sc) == []                               # no campaign, group or contact is created


def test_a_dry_revoke_job_looks_up_and_marks_nothing():
    sc = revoke_sc()
    out = sc_load.revoke_run(a_job(), commit=False, sc=sc, pause=0)
    assert out["ok"] and out["dry"] is True and sc.dnc == []
    assert outcomes(out)["8655550101"] == "would_mark" and outcomes(out)["8655550103"] == "lead"


def test_a_number_on_two_records_is_marked_once():
    sc = revoke_sc()
    job = a_job()
    job["owners"].append({"record": "r3", "dnc": True, "numbers": ["8655550101"]})
    out = sc_load.revoke_run(job, commit=True, sc=sc, pause=0)
    assert sc.dnc == ["c-1", "c-2"] and [r["outcome"] for r in out["numbers"] if r["record"] == "r3"] == ["already"]


def test_a_failed_number_is_reported_and_three_failures_in_a_row_stop_the_job():
    sc = revoke_sc()
    sc.dnc_fail = {"c-1"}
    sc.dnc_already = {"c-2"}
    out = sc_load.revoke_run(a_job(), commit=True, sc=sc, pause=0)
    assert out["ok"] is False and outcomes(out)["8655550101"] == "failed" and "HTTP 500" in out["numbers"][0]["detail"]
    assert outcomes(out)["8655550102"] == "already" and outcomes(out)["8655550105"] == "not_in_sc"   # one failure is not a stop
    down = revoke_sc()
    down.lookup_fail = {"8655550101", "8655550102", "8655550105"}
    out = sc_load.revoke_run(a_job(), commit=True, sc=down, pause=0)
    assert any("stopped after 3 failures in a row" in p for p in out["problems"])
    assert outcomes(out)["8655550103"] == "failed" and "not tried" in out["numbers"][-1]["detail"]
    assert "8655550103" not in down.lookups and down.dnc == []


def test_an_unreadable_do_not_text_list_still_looks_every_number_up():
    class NoList(FakeSC):
        def gql(self, query, variables=None):
            if "findSuppressions" in query:
                raise sc_client.SCError("HTTP 502 from https://api.example/gql: b'bad gateway'")
            return super().gql(query, variables)
    sc = NoList(existing={"8655550104": "c-4"})
    sc.dnc_already = {"c-4"}
    job = {"action": "revoke", "batch": JOB_NO, "job": "j-1", "owners": [{"record": "r1", "numbers": ["8655550104"]}]}
    out = sc_load.revoke_run(job, commit=True, sc=sc, pause=0)
    assert out["ok"] and outcomes(out) == {"8655550104": "already"} and "8655550104" in sc.lookups
    assert any("could not read Smarter Contact's do-not-text list" in p for p in out["problems"])


def test_a_revoke_job_is_never_a_load():
    # A texting app from before this change runs check() on it, and refuses it instead of building a campaign.
    assert sc_load.check(a_job()) == ["the load names no group"]
    assert sc_load.check_revoke(a_job()) == []
    assert any("at least" in p for p in sc_load.check_revoke(a_job(batch=21)))      # a text batch's number
    assert any("no id" in p for p in sc_load.check_revoke(a_job(job="")))
    assert any("no groups or contacts" in p for p in sc_load.check_revoke(dict(a_job(), groups=[{"name": "x"}])))
    assert any("names no owner" in p for p in sc_load.check_revoke(dict(a_job(), owners=[])))
    sc = revoke_sc()
    out = sc_load.revoke_run(dict(a_job(), owners=[]), commit=True, sc=sc, pause=0)
    assert out["ok"] is False and "names no owner" in out["refused"] and sc.lookups == [] and sc.calls == []


def test_accept_never_mixes_a_revoke_job_with_a_load(monkeypatch, tmp_path):
    monkeypatch.setattr(sc_load, "STATE_DIR", tmp_path)
    sc_load._save(JOB_NO, {"batch": JOB_NO, "status": "done"})                       # a text load's document
    assert "holds a text load" in sc_load.accept(a_job())["why"]
    sc_load._save(JOB_NO + 1, {"batch": JOB_NO + 1, "action": "revoke", "job": "j-old", "status": "done"})
    assert "another do-not-contact job" in sc_load.accept(a_job(JOB_NO + 1))["why"]
    assert sc_load.accept(a_job(JOB_NO + 1, job="j-old")) == {"accepted": True, "already": "done",
                                                               "batch": JOB_NO + 1, "action": "revoke"}
    assert "holds a do-not-contact job" in sc_load.accept(a_load(JOB_NO + 1))["why"]
    assert sc_load.accept(dict(a_job(), owners=[]))["accepted"] is False


def test_dry_answers_kept_in_memory_are_capped(monkeypatch):
    import time as _time
    monkeypatch.setattr(sc_load, "_DRY", {})
    monkeypatch.setattr(sc_load, "DRY_KEEP", 2)
    monkeypatch.setattr(sc_load, "revoke_run", lambda job, commit=False: {"ok": True, "dry": not commit, "numbers": []})
    for i in range(3):
        assert sc_load.accept(a_job(JOB_NO + 20 + i, job="j-%d" % i), dry=True)["accepted"]
        for _ in range(300):
            if sc_load.dry_status(JOB_NO + 20 + i).get("status") == "done":
                break
            _time.sleep(0.01)
    assert len(sc_load._DRY) == 2 and JOB_NO + 20 not in sc_load._DRY
    assert sc_load.dry_status(JOB_NO + 22)["status"] == "done" and sc_load.dry_status(JOB_NO + 22)["job"] == "j-2"


def _wait_done(client, path, head, tries=200):
    import time as _time
    doc = {}
    for _ in range(tries):
        doc = client.get(path, headers=head).json()
        if doc.get("status") in ("done", "failed"):
            return doc
        _time.sleep(0.02)
    return doc


def test_the_load_route_takes_a_revoke_job_and_reports_each_number(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from sms_agent import config, receiver
    sc = revoke_sc()
    real = sc_load.revoke_run
    monkeypatch.setattr(sc_load, "revoke_run", lambda job, commit=False: real(job, commit=commit, sc=sc, pause=0))
    monkeypatch.setattr(sc_load, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "SC_LOAD_SECRET", "tok-example-123")
    monkeypatch.setattr(config, "INLINE_WORKER", False)
    monkeypatch.setattr(config, "DRY_RUN", False)
    head = {"authorization": "Bearer tok-example-123"}
    with TestClient(receiver.app) as client:
        assert client.post("/hooks/scload", json=a_job()).status_code == 404               # no token, no route
        r = client.post("/hooks/scload?dry=1", json=a_job(JOB_NO + 7, job="j-dry"), headers=head)
        assert r.status_code == 200 and r.json()["accepted"] and r.json()["action"] == "revoke"
        doc = _wait_done(client, "/hooks/scload/%d?dry=1" % (JOB_NO + 7), head)
        assert doc["status"] == "done" and doc["job"] == "j-dry" and doc["result"]["dry"] is True and sc.dnc == []
        assert outcomes(doc["result"])["8655550101"] == "would_mark"
        r = client.post("/hooks/scload", json=a_job(JOB_NO + 8, job="j-live"), headers=head)
        assert r.status_code == 200 and r.json() == {"accepted": True, "batch": JOB_NO + 8, "dry": False, "action": "revoke"}
        doc = _wait_done(client, "/hooks/scload/%d" % (JOB_NO + 8), head)
        assert doc["status"] == "done" and doc["action"] == "revoke" and doc["job"] == "j-live"
        assert outcomes(doc["result"])["8655550101"] == "marked" and sc.dnc == ["c-1", "c-2"]
        assert (tmp_path / ("batch_%d.json" % (JOB_NO + 8))).exists()                    # the result outlives a restart
        again = client.post("/hooks/scload", json=a_job(JOB_NO + 8, job="j-live"), headers=head).json()
        assert again["already"] == "done" and sc.dnc == ["c-1", "c-2"]                   # sent twice, run once
        assert client.post("/hooks/scload", json=a_job(JOB_NO + 9, job=""), headers=head).status_code == 400
