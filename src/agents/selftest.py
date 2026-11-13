"""Agents selftest: zero network, throwaway database, every check an assertion.

    python src/agents/cli.py selftest
"""
from __future__ import annotations

import importlib
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


def _ds_event(event_id: str, uuid: str) -> dict:
    """A delivery in the shape DataSift publishes (developers.datasift.ai/guides/webhooks)."""
    return {"event_type": "sequence.action.triggered", "event_id": event_id,
            "event_datetime": "2026-11-11T15:00:00Z", "sequence": {"uuid": U1, "name": "Send to Deep Prospecting"},
            "property": {"uuid": uuid, "address": {"street": "1 Example Rd", "city": "Knoxville", "state": "TN"},
                         "owner": {"first_name": "Pat", "last_name": "Example"}, "tags": [], "lists": []}}


def _webhook_signer(check):
    """Generate a throwaway Ed25519 key, park its public half in the signature module's cache (so
    nothing is fetched), unit-test verify(), and return a function that signs a body the way
    DataSift does. None when the cryptography package is missing, which is itself a failure."""
    import base64
    import time
    from . import webhook_sig
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        check(False, "webhook signatures need the cryptography package: %s" % exc)
        return None
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    keys = {"test-key": pem}
    webhook_sig._CACHE.update({"keys": dict(keys), "at": time.time()})

    def sign(body, age: int = 0, tamper: bool = False, kid: str = "test-key"):
        raw = json.dumps(body, separators=(",", ":")).encode()
        ts = int(time.time()) - age
        sig = base64.b64encode(key.sign(("%d." % ts).encode() + raw)).decode()
        if tamper:
            raw = raw.replace(b"Knoxville", b"Maryville")
        return raw, {"Content-Type": "application/json", "X-Sift-Webhook-Signature": "v1," + sig,
                     "X-Sift-Webhook-Timestamp": str(ts), "X-Sift-Webhook-Key-Id": kid}

    raw, h = sign(_ds_event("u-1", U1))
    stub = lambda: dict(keys)  # noqa: E731  (a fetch that keeps the test key in the cache)
    check(webhook_sig.verify(h, raw, fetch=stub) == (True, ""), "a body signed with DataSift's scheme verifies")
    check(webhook_sig.verify({k.lower(): v for k, v in h.items()}, raw, fetch=stub)[0],
          "header names are matched case-insensitively")
    check(webhook_sig.verify(h, raw + b" ", fetch=stub)[1] == "signature does not match",
          "one byte of difference fails: the raw body is what is signed, never re-serialised JSON")
    raw2, h2 = sign(_ds_event("u-2", U1), kid="rotated")
    check(webhook_sig.verify(h2, raw2, fetch=stub)[1].startswith("unknown key id"), "an unknown key id fails")
    check(webhook_sig.verify({}, raw, fetch=stub)[1] == "unsigned", "a delivery with no signature is unsigned")
    check(webhook_sig.verify(dict(h, **{"X-Sift-Webhook-Signature": "v2," + h["X-Sift-Webhook-Signature"][3:]}),
                             raw, fetch=stub)[1].startswith("unknown signature version"), "only v1 signatures are read")
    raw3, h3 = sign(_ds_event("u-3", U1), age=4000)
    check(webhook_sig.verify(h3, raw3, fetch=stub)[1].startswith("stale"), "an old signature is stale")
    check(webhook_sig.mode() == "require", "signatures default to require")
    return sign


EXTRA_SECTIONS: list[tuple[str, str]] = [
    ("data, lists and hygiene", "selftest_data"),
    ("releasing a batch from slack", "selftest_release"),
    ("text lane", "selftest_textlane"),
    ("lead management", "selftest_leads"),
    ("lanes", "selftest_lanes"),
    ("kpi, alarms and measurement", "selftest_kpi"),
    ("research lane", "selftest_research"),
    ("research write-back", "selftest_writeback"),
    ("files dropped in slack", "selftest_files"),
    ("quinn's operations workflows", "selftest_wf_ops"),
    ("people workflows", "selftest_wf_people"),
    ("atlas's data workflows", "selftest_wf_data"),
    ("roma's deal workflows", "selftest_wf_deals"),
    ("query tools", "selftest_query"),
]


class NoNetwork:
    """Stands where `requests` stands until a section installs its own fake. This machine's .env can
    hold a real Slack app; a selftest that reached for it would post in a real channel."""

    def post(self, *a, **k):
        raise AssertionError("the selftest tried to reach Slack")

    get = post


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agents_selftest_"))
    config.DB_PATH, config.RUNS_DIR = tmp / "agents.db", tmp / "runs"
    config.WEBHOOK_SECRET, config.PUBLIC_URL = "test-secret", "https://agents.example.test"
    config.SLACK_WEBHOOK, config.INLINE_WORKER = "", False
    # Every Slack setting this machine's own .env may carry is blanked, and the door is shut.
    config.SLACK_BOT_TOKEN = config.SLACK_SIGNING_SECRET = config.SLACK_APP_TOKEN = config.SLACK_CHANNEL = ""
    from . import slack_app, slack_socket
    slack_app._http = slack_socket._http = NoNetwork()
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

    print("webhook signatures")
    signer = _webhook_signer(check)

    print("brain")
    try:
        from fastapi.testclient import TestClient
        from . import brain
        c = TestClient(brain.app)
        hook = "/hooks/datasift/test-secret/"
        check(c.post("/hooks/datasift/wrong/deep-prospect", json={"property_uuid": U1}).status_code == 404,
              "a wrong secret is a 404")
        U2 = "99999999-2222-3333-4444-555555555555"
        if signer:
            def post(workflow, body, **kw):
                raw, headers = signer(body, **kw)
                return c.post(hook + workflow, content=raw, headers=headers)

            check(c.post(hook + "deep-prospect", json={"property": {"uuid": U2}}).status_code == 401,
                  "an unsigned delivery is refused (signatures default to require)")
            check(post("deep-prospect", _ds_event("e-forged", U2), tamper=True).status_code == 401,
                  "a body changed after signing is refused")
            check(post("deep-prospect", _ds_event("e-old", U2), age=4000).status_code == 401,
                  "a delivery signed over half an hour ago is refused (no replay)")
            r1 = post("deep-prospect", _ds_event("e-1", U2)).json()
            r2 = post("deep-prospect", _ds_event("e-1", U2)).json()
            r2b = post("deep-prospect", _ds_event("e-2", U2)).json()
            check(r1.get("queued") is True, "a signed delivery in the documented shape queues the deep prospect")
            check(r2.get("duplicate") is True, "DataSift's retry of the same event_id is a duplicate")
            check(r2b.get("queued") is False, "a second event for a record already queued does not queue it twice")
            rt = post("deep-prospect", {"event_type": "webhook.test", "event_id": "t-1"}).json()
            check(rt.get("test") is True and store.kv_get("datasift:webhook_test:deep-prospect"),
                  "the app's Test button is recorded, and that receipt is the setup proof")
            check(c.get("/health").json().get("datasift_webhook_tests", {}).get("deep-prospect"),
                  "health reports when each webhook last passed DataSift's test")
            r3 = post("deep-prospect", {"event_id": "e-3", "nothing": 1}).json()
            check(r3.get("queued") is False and r3.get("reason") == "no record id", "a body with no record id runs nothing")
            check(post("unknown", {"event_id": "e-4"}).status_code == 404, "an unknown workflow is a 404")
            import os
            os.environ["AGENTS_WEBHOOK_SIGNATURES"] = "log"
            try:
                U3 = "77777777-2222-3333-4444-555555555555"
                r4 = c.post(hook + "deep-prospect", json=_ds_event("e-5", U3)).json()
                check(r4.get("queued") is True, "log mode accepts an unsigned delivery (for a first install only)")
            finally:
                os.environ.pop("AGENTS_WEBHOOK_SIGNATURES", None)
        check(c.get("/health").json()["ok"] is True, "health answers")
        check(c.get("/files/1/badsig/x.pdf").status_code == 404, "a bad file signature is a 404")
    except ImportError as exc:
        check(False, "brain tests need fastapi + httpx: %s" % exc)

    print("datasift setup: the test delivery")
    from . import datasift_setup

    class _HookCRM:
        def __init__(self, answer):
            self.answer, self.calls = answer, []

        def _request(self, path, method="GET", body=None, params=None):
            self.calls.append((method, path))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    real_receipt, receipts = datasift_setup.test_receipt, []
    datasift_setup.test_receipt = lambda workflow="deep-prospect": (receipts.pop(0) if len(receipts) > 1 else receipts[0],
                                                                    "the running brain")
    try:
        crm_ok = _HookCRM({"success": True, "status_code": 200})
        receipts[:] = ["", "", "2026-01-02T03:04:05+00:00"]
        said = datasift_setup.send_test(crm_ok, "hook-1", sleep=lambda s: None)
        check(said.startswith("received by the running brain at 2026-01-02") and
              crm_ok.calls == [("POST", "/api/internal/integration/webhook/%s/test/" % "hook-1")],
              "the setup asks DataSift for its Test delivery and waits for the brain's own receipt")
        receipts[:] = ["2026-01-01T00:00:00+00:00"]
        check(datasift_setup.send_test(crm_ok, "hook-1", wait_s=4, sleep=lambda s: None).startswith("sent, but"),
              "an older receipt is not taken for this test: no new receipt is said plainly")
        receipts[:] = [""]
        check(datasift_setup.send_test(_HookCRM({"success": False, "status_code": 401}), "hook-1", sleep=lambda s: None)
              == "sent, and the brain answered 401", "a delivery the brain refused is reported with its status")
        from sms_agent.crm_standalone import StandaloneCRMError
        check(datasift_setup.send_test(_HookCRM(StandaloneCRMError("HTTP 404")), "hook-1", sleep=lambda s: None)
              .startswith("not sent"), "a route that refuses the test is reported, never raised")
    finally:
        datasift_setup.test_receipt = real_receipt

    print("team")
    from . import selftest_team
    try:
        selftest_team.run(check, tmp)
    except Exception as exc:  # noqa: BLE001  (a crash in the team checks is a failure, not an abort)
        import traceback
        traceback.print_exc()
        check(False, "team checks crashed: %s: %s" % (type(exc).__name__, exc))

    print("autopilot")
    from . import selftest_autopilot
    try:
        selftest_autopilot.run(check, tmp)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(False, "autopilot checks crashed: %s: %s" % (type(exc).__name__, exc))
    finally:
        config.MODE = "dry"

    print("phase 0")
    from . import selftest_phase0
    try:
        selftest_phase0.run(check, tmp)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(False, "phase 0 checks crashed: %s: %s" % (type(exc).__name__, exc))
    finally:
        config.MODE = "dry"

    # Sections added after the first build: (title, module with run(check, tmp)). One line each.
    for title, module in EXTRA_SECTIONS:
        print(title)
        try:
            importlib.import_module("%s.%s" % (__package__, module)).run(check, tmp)
        except Exception as exc:  # noqa: BLE001  (a crash in one section is a failure, not an abort)
            import traceback
            traceback.print_exc()
            check(False, "%s checks crashed: %s: %s" % (title, type(exc).__name__, exc))
        finally:
            config.MODE = "dry"

    print("registries")
    from .autopilot import registry
    from . import workflows
    check(registry.problems() == [], "every registered stage kind loads: %s" % (registry.problems() or "ok"))
    check(workflows.problems() == [], "every registered workflow loads: %s" % (workflows.problems() or "ok"))

    print("source hygiene")
    src = Path(__file__).resolve().parent
    check(all("\x08" not in f.read_text(encoding="utf-8") for f in src.rglob("*.py")), "no backspace byte in the agents source")

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0
