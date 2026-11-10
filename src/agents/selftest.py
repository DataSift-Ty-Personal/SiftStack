"""Agents selftest: zero network, throwaway database, every check an assertion.

    python src/agents/cli.py selftest
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from . import config

PASS, FAIL = [], []


def check(cond, msg: str) -> None:
    (PASS if cond else FAIL).append(msg)
    print(("  ok    " if cond else "  FAIL  ") + msg)


class FakeRun:
    def __init__(self, files: dict):
        self.files = files

    def load(self, name, default=None):
        return self.files.get(name, default)

    def save(self, name, obj):
        self.files[name] = obj


U1 = "11111111-2222-3333-4444-555555555555"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agents_selftest_"))
    config.DB_PATH, config.RUNS_DIR = tmp / "agents.db", tmp / "runs"
    config.WEBHOOK_SECRET, config.PUBLIC_URL = "test-secret", "https://agents.example.test"
    config.SLACK_WEBHOOK, config.INLINE_WORKER = "", False
    config.RUN_CAP_USD, config.DAILY_CAP_USD, config.MODE = 5.0, 25.0, "dry"
    from . import router, slack, store
    from .research import deep_prospect, researcher

    print("store")
    store.init()
    t1, c1 = store.enqueue("research", "deep_prospect", U1)
    t2, c2 = store.enqueue("research", "deep_prospect", U1)
    check(c1 and not c2 and t1 == t2, "a record already queued is not queued twice")
    t3, c3 = store.enqueue("research", "deep_prospect", U1, force=True)
    check(c3 and t3 != t1, "force queues a fresh run")
    claimed = store.claim_next()
    check(claimed and claimed["id"] == t1 and claimed["status"] == "running", "oldest task is claimed first")
    store.finish(t1, "done", {"x": 1})
    _, c4 = store.enqueue("research", "deep_prospect", U1)
    check(not c4, "a record finished inside the dedupe window is not re-run")
    store.claim_next()
    check(store.requeue_stale() == 1, "a task left running by a restart is requeued once")
    store.add_spend(t1, 1.25, "smartskip")
    store.add_spend(t1, 0, "nothing")
    check(abs(store.spent(task_id=t1) - 1.25) < 1e-9, "spend sums per task and ignores zero")

    print("router")
    shapes = [({"property": {"uuid": U1}}, U1), ({"property_uuid": U1}, U1),
              ({"data": {"property": {"uuid": U1}}}, U1), ({"uuid": U1, "address": {"street": "x"}}, U1),
              ({"card": {"property": U1}}, U1), ({"uuid": U1}, ""), ({}, ""), ("not a dict", "")]
    for payload, want in shapes:
        check(router.record_uuid(payload) == want, "record id from %s" % json.dumps(payload)[:60])
    check(router.route("deep-prospect", {})["workflow"] == "deep_prospect", "deep-prospect routes to research")

    print("slack")
    check(slack.sign(7, "a.pdf") == slack.sign(7, "a.pdf") != slack.sign(8, "a.pdf"), "file signatures are per task")
    check(slack.file_url(7, "a.pdf").endswith("/files/7/%s/a.pdf" % slack.sign(7, "a.pdf")), "signed file URL")
    check(slack.post("hello") is False, "no webhook set: post logs and returns False, never raises")

    print("researcher guards")
    for name, schema in (("research", researcher.RESEARCH_SCHEMA), ("analysis", researcher.ANALYSIS_SCHEMA)):
        bad = []

        def walk(s, path):
            if s.get("type") == "object":
                if s.get("additionalProperties") is not False or set(s.get("required", [])) != set(s.get("properties", {})):
                    bad.append(path)
                for k, v in s.get("properties", {}).items():
                    walk(v, path + "." + k)
            elif s.get("type") == "array":
                walk(s["items"], path + "[]")
        walk(schema, name)
        check(not bad, "%s schema is strict everywhere %s" % (name, bad[:2]))
    r = researcher.enforce_sources({
        "identity_proof": [{"claim": "real", "url": "https://obits.example/a/", "quote": "q"},
                           {"claim": "invented", "url": "https://nowhere.example/z", "quote": "q"}],
        "decedents": [{"name": "Pat Example", "obituary_url": "https://nowhere.example/obit"}],
        "people": [{"name": "Kim", "source_url": "https://nowhere.example/k"}], "unresolved": []},
        {"https://obits.example/a?ref=1"})
    check([e["claim"] for e in r["identity_proof"]] == ["real"], "a claim whose URL was never visited is dropped")
    check(any("invented" in u for u in r["unresolved"]), "the dropped claim is kept in unresolved")
    check(r["decedents"][0]["obituary_url"] == "", "an obituary never opened is not cited")

    people = [{"slug": "s", "people": [{"key": "k1", "phones": [{"number": "8655550101"}]}]}]
    base = {"slug": "s", "people": [
        {"ss_key": "k1", "name": "A", "trace_address": {"street": "", "city": "", "state": "", "zip": ""},
         "trace_first": "", "tag": ""},
        {"ss_key": "bogus", "name": "B", "trace_address": {"street": "1 Main St", "city": "K", "state": "TN", "zip": "1"}}],
        "call_order": [{"who": "A", "number": "(865) 555-0101", "why": "x"}, {"who": "Z", "number": "8655559999", "why": "x"}],
        "number_notes": [{"number": "(865) 555-0101", "note": "mobile"}], "wrong_party": [],
        "verify_extra": [], "also_reaches": [{"number": "8655550101", "names": ["A", "C"]}],
        "crm_fields": {"Owner Deceased": "Yes", "Date of Death": ""},
        "death": {"decedent": "Robert Example", "dod": "2026-01-01", "obituary_url": "https://obits.example/a",
                  "owner_deceased": True, "confirmed": True}}
    run = FakeRun({"people.json": people, "research/s_urls.json": ["https://obits.example/a"]})
    a = researcher.enforce_analysis(json.loads(json.dumps(base)), run, {"owner_of_record": "Mary Example"})
    check(a["people"][0]["ss_key"] == "k1" and a["people"][1]["ss_key"] == "", "an unknown ss_key is cleared")
    check("trace_address" not in a["people"][0], "an empty trace address is dropped, never overrides the real one")
    check("trace_address" in a["people"][1], "a real trace address is kept")
    check("trace_first" not in a["people"][0] and "tag" not in a["people"][0], "empty optional fields are dropped")
    check([c["who"] for c in a["call_order"]] == ["A"], "a call-order number on nobody is dropped")
    check(a["number_notes"] == {"8655550101": "mobile"} and a["also_reaches"] == {"8655550101": ["A", "C"]},
          "number-keyed lists become number-keyed objects")
    check(a["death"]["owner_deceased"] is False and "Owner Deceased" not in a["crm_fields"],
          "spouse-obituary trap: a different first name does not mark the owner deceased")
    check("Date of Death" not in a["crm_fields"], "empty CRM fields are never written")
    a2 = researcher.enforce_analysis(json.loads(json.dumps(base)), run, {"owner_of_record": "Robert J Example"})
    check(a2["death"]["owner_deceased"] is True, "a visited obituary naming the owner does mark him deceased")
    run2 = FakeRun({"people.json": people, "research/s_urls.json": []})
    a3 = researcher.enforce_analysis(json.loads(json.dumps(base)), run2, {"owner_of_record": "Robert Example"})
    check(a3["death"]["owner_deceased"] is False, "an obituary not opened this run does not mark a death")

    print("budget")
    b = deep_prospect.Budget(t1)
    check(b.allows(3.0, "x") and not b.allows(4.0, "y"), "the run cap counts what the task already spent")
    check(b.skipped and "cap" in b.skipped[-1], "a capped phase is recorded as skipped")

    print("slack text")
    txt = deep_prospect.slack_text({"id": 9}, {
        "street": "1 Example Rd", "live": False, "headline": "Owner died 2026; daughter signs",
        "owner_status": "Deceased", "must_sign": [{"who": "Kim Example", "short": "Kim (daughter)"}],
        "call_order": [{"who": "Kim", "number": "(865) 555-0101"}], "pdf_name": "1-example-rd.pdf",
        "spent_usd": 1.5, "written_back": False, "skipped": [], "open_items": []})
    check("555-0101" not in txt and "ends 0101" in txt, "Slack shows the last 4 of a number, never the whole thing")
    check("—" not in txt and "–" not in txt, "no em or en dash in the Slack post")
    check("/files/9/" in txt, "the property file is linked")

    print("brain")
    try:
        from fastapi.testclient import TestClient
        from . import brain
        c = TestClient(brain.app)
        check(c.post("/hooks/datasift/wrong/deep-prospect", json={"property_uuid": U1}).status_code == 404,
              "a wrong secret is a 404")
        U2 = "99999999-2222-3333-4444-555555555555"
        r1 = c.post("/hooks/datasift/test-secret/deep-prospect", json={"property": {"uuid": U2}}).json()
        r2 = c.post("/hooks/datasift/test-secret/deep-prospect", json={"property": {"uuid": U2}}).json()
        check(r1["queued"] and not r2["queued"], "the webhook queues once and dedupes a repeat")
        r3 = c.post("/hooks/datasift/test-secret/deep-prospect", json={"nothing": 1}).json()
        check(r3["queued"] is False and r3.get("reason") == "no record id", "a body with no record id runs nothing")
        check(c.post("/hooks/datasift/test-secret/unknown", json={}).status_code == 404, "an unknown workflow is a 404")
        check(c.get("/health").json()["ok"] is True, "health answers")
        check(c.get("/files/1/badsig/x.pdf").status_code == 404, "a bad file signature is a 404")
    except ImportError as exc:
        check(False, "brain tests need fastapi + httpx: %s" % exc)

    print("source hygiene")
    src = Path(__file__).resolve().parent
    check(all("\x08" not in f.read_text(encoding="utf-8") for f in src.rglob("*.py")), "no backspace byte in the agents source")

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0
