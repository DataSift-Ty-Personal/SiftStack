"""Cold email replies are never lost (2026-11-19). Two replies failed on a retired notes route, the
watermark moved past them, and they were never read again; a hand raise would have been lost the same
way, because the CRM note was written before the Slack post. Zero network: Instantly, the CRM, the
classifier and Slack are fakes. Every name, street and address here is invented."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "cold_email"))
sys.path.insert(0, str(ROOT / "src"))

import replies  # noqa: E402


class FakeInstantly:
    def __init__(self, items, block_fails=0):
        self.items, self.block_fails, self.blocked, self.calls = items, block_fails, [], []

    def call(self, method, path, params=None, **kw):
        self.calls.append(dict(params or {}))
        since = (params or {}).get("min_timestamp_created") or ""
        return {"items": [e for e in self.items if e["timestamp_created"] >= since]}

    def block(self, emails):
        if self.block_fails:
            self.block_fails -= 1
            raise RuntimeError("Instantly answered 502")
        self.blocked += list(emails)


class FakeCRM:
    def __init__(self, note_fails=False, dnc_fails=0):
        self.note_fails, self.dnc_fails, self.notes, self.lists = note_fails, dnc_fails, [], []

    def record(self, uuid):
        return {"address": {"street": "100 Example St", "city": "Knoxville", "state": "TN"},
                "owner": {"first_name": "Pat"}}

    def message(self, uuid, text, pin=False):
        if self.note_fails:
            raise RuntimeError("HTTP 404 on POST /api/internal/property/x/add-notes/")
        self.notes.append(text)
        return "m1"

    def add_lists(self, uuid, lists):
        if self.dnc_fails:
            self.dnc_fails -= 1
            raise RuntimeError("HTTP 503")
        self.lists += list(lists)


def reply(i, text, ts, sender="owner%d@example.com"):
    return {"id": "e%d" % i, "timestamp_created": ts, "lead": sender % i, "body": {"text": text}}


def setup(tmp_path, monkeypatch, items, crm, api=None, kinds=None):
    monkeypatch.setattr(replies, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(replies, "LOG", tmp_path / "replies.jsonl")
    ledger = {"owners": {}, "emails": {}}
    for e in items:
        ledger["emails"][e["lead"]] = "o-" + e["id"]
        ledger["owners"]["o-" + e["id"]] = {"record": "r-" + e["id"]}
    (tmp_path / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(replies, "LEDGER", tmp_path / "ledger.json")
    (tmp_path / "state.json").write_text(json.dumps({"seen": [], "since": "2026-03-05T14:00:00.000Z"}), encoding="utf-8")
    api = api or FakeInstantly(items)
    monkeypatch.setattr(replies, "Instantly", lambda: api)
    posts = []
    monkeypatch.setattr(replies, "post", lambda text: posts.append(text) or True)
    # A committed pass refuses to start without the text leads channel; this is an invented one.
    monkeypatch.setattr(replies, "webhook", lambda: "https://hooks.example.test/text-leads")
    kinds = kinds or {}

    def fake_classify(text, street):
        if replies.STOP_RX.search(text or ""):
            return "stop", "asked to stop", 1.0
        return kinds.get(text, "not_interested"), "test", 0.9
    monkeypatch.setattr(replies, "classify", fake_classify)
    real_handle = replies.handle

    def handle(e, ledger, api_, crm_box, commit):
        crm_box["crm"] = crm                       # the fake CRM, never the real client
        return real_handle(e, ledger, api_, crm_box, commit)
    monkeypatch.setattr(replies, "handle", handle)
    return api, posts


def state(tmp_path):
    return json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


def logged(tmp_path):
    p = tmp_path / "replies.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def test_a_hand_raise_is_posted_even_when_the_note_cannot_be_written(tmp_path, monkeypatch):
    items = [reply(1, "yes, make me an offer", "2026-03-05T15:00:00.000Z")]
    crm = FakeCRM(note_fails=True)
    api, posts = setup(tmp_path, monkeypatch, items, crm, kinds={"yes, make me an offer": "interested"})
    assert replies.run_once(commit=True) == 1
    assert len(posts) == 1 and "Call within 5 minutes" in posts[0]
    acts = logged(tmp_path)[0]["acts"]
    assert "posted to text leads" in acts and any(a.startswith("note FAILED") for a in acts)
    assert "e1" in state(tmp_path)["seen"]


def test_a_reply_that_fails_is_read_again_and_the_watermark_waits_for_it(tmp_path, monkeypatch):
    items = [reply(1, "No, sorry.", "2026-03-05T15:24:00.000Z"),
             reply(2, "not for sale", "2026-03-05T15:49:00.000Z")]
    crm = FakeCRM()
    api = FakeInstantly(items, block_fails=1)        # the first block fails once
    api, posts = setup(tmp_path, monkeypatch, items, crm, api=api)
    assert replies.run_once(commit=True) == 1        # the second reply is handled, the first is not
    s = state(tmp_path)
    assert "e1" not in s["seen"] and "e2" in s["seen"] and s["tries"] == {"e1": 1}
    assert s["since"] == "2026-03-05T15:23:59.000Z", "the watermark must not move past a reply still to handle"
    assert replies.run_once(commit=True) == 1        # the next pass reads it again and handles it
    s = state(tmp_path)
    assert "e1" in s["seen"] and s["tries"] == {} and api.blocked.count("owner1@example.com") == 1
    assert [x["email_id"] for x in logged(tmp_path)] == ["e2", "e1"]


def test_a_reply_that_keeps_failing_goes_to_a_person(tmp_path, monkeypatch):
    items = [reply(1, "No thanks", "2026-03-05T15:00:00.000Z")]
    crm = FakeCRM()
    api = FakeInstantly(items, block_fails=99)
    api, posts = setup(tmp_path, monkeypatch, items, crm, api=api)
    for _ in range(replies.MAX_TRIES):
        replies.run_once(commit=True)
    assert len(posts) == 1 and "could not be handled on its own" in posts[0] and "No thanks" in posts[0]
    s = state(tmp_path)
    assert "e1" in s["seen"] and s["tries"] == {}
    assert logged(tmp_path)[-1]["kind"] == "gave_up"


def test_a_stop_reaches_the_dnc_list_or_is_tried_again(tmp_path, monkeypatch):
    items = [reply(1, "Do not notify me again.", "2026-03-05T15:00:00.000Z")]
    crm = FakeCRM(dnc_fails=1)
    api, posts = setup(tmp_path, monkeypatch, items, crm)
    assert replies.run_once(commit=True) == 0 and crm.lists == []
    assert replies.run_once(commit=True) == 1 and crm.lists == ["DNC"]
    assert logged(tmp_path)[-1]["kind"] == "stop" and posts == []


def test_stop_words_cover_notify_and_text():
    for t in ("Do not notify me again.", "don't text me", "Stop notifying me", "please do not reach out"):
        assert replies.STOP_RX.search(t), t
    assert not replies.STOP_RX.search("Not right now, maybe next year")


def test_an_auto_reply_is_skipped_and_dry_runs_write_nothing(tmp_path, monkeypatch):
    items = [dict(reply(1, "I am out of the office", "2026-03-05T15:00:00.000Z"), is_auto_reply=True),
             reply(2, "No", "2026-03-05T15:05:00.000Z")]
    crm = FakeCRM()
    api, posts = setup(tmp_path, monkeypatch, items, crm)
    replies.run_once(commit=False)
    assert state(tmp_path)["seen"] == [] and api.blocked == [] and crm.notes == []
    replies.run_once(commit=True)
    assert set(state(tmp_path)["seen"]) == {"e1", "e2"} and crm.notes and api.blocked == ["owner2@example.com"]
