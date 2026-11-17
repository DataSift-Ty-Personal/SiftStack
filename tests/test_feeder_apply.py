"""feeder_apply: an approved feeder change list is sent with two switches, a read first, a read
back and a log of what was there. Zero network: the wire is a fake. Names and ids are invented.

    python tests/test_feeder_apply.py
"""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "agents" / "autopilot"))

import feeder_apply as fa  # noqa: E402

ADDR = [{"state": "TN", "county": "Example", "searchType": "county"}]


def feeder(fid, name, tags, auto=True, crit=None, active=True):
    return {"id": fid, "name": name, "is_active": active, "auto_add_enabled": auto, "tags": list(tags), "lists": [],
            "filter_data": {"filters": dict(crit or {"type_single_family": True}), "addresses": copy.deepcopy(ADDR)}}


class FakeWire(fa.Wire):
    def __init__(self, feeders, allowed=False, ignore_tags=False):
        super().__init__("test-key", allowed=allowed, gap=0, sleep=lambda s: None)
        self.store = {f["id"]: copy.deepcopy(f) for f in feeders}
        self.patches = []
        self.ignore_tags = ignore_tags

    def _send(self, method, url, body=None):
        fid = int(url.rstrip("/").split("/")[-1])
        self.requests += 1
        if method == "GET":
            return copy.deepcopy(self.store[fid])
        self.patches.append((fid, copy.deepcopy(body)))
        for k, v in body.items():
            if k == "tags" and self.ignore_tags:
                continue
            self.store[fid][k] = copy.deepcopy(v)
        return copy.deepcopy(self.store[fid])


def plan_of(feeders, changes, never=()):
    return {"feeders": [{"id": f["id"], "title": f["name"], "tags": list(f["tags"]), "auto_add": f["auto_add_enabled"]} for f in feeders],
            "changes": changes, "never_on": [{"id": i} for i in never]}


F1 = feeder(101, "Example - Retired - Vacant", ["Old Tier"])
F2 = feeder(102, "Example - Priority 1 - Absentee", ["Old Tier", "Priority 1"])
F3 = feeder(103, "Example Obituary", [], crit={"type_single_family": True, "distressors_include": "is_obituary_property"})
CH1 = {"group": "retired", "id": 101, "title": F1["name"], "do": "Switch auto-add off", "set": {"auto_add_enabled": False}}
CH2 = {"group": "tag", "id": 102, "title": F2["name"], "do": "Stop adding the tag Old Tier", "set": {"tags": ["Priority 1"]}}
CH3 = {"group": "obituary", "id": 103, "title": F3["name"], "do": "Gain a value cap",
       "set": {"filters": {"value_max": 700000, "extra_equity_percent_min": 30}, "tags": ["Obituary"]}}
PLAN = plan_of([F1, F2, F3], [CH1, CH2, CH3])


def test_both_switches_or_nothing():
    os.environ.pop(fa.ALLOW_ENV, None)
    assert not fa.may_write(True) and not fa.may_write(False)
    os.environ[fa.ALLOW_ENV] = "1"
    try:
        assert fa.may_write(True) and not fa.may_write(False)
    finally:
        os.environ.pop(fa.ALLOW_ENV, None)


def test_a_dry_run_reads_and_sends_nothing():
    w = FakeWire([F1, F2, F3])
    done = fa.apply_plan(w, PLAN, out=lambda s: None)
    assert len(done["would"]) == 3 and not w.patches and not done["changed"]
    try:
        w.patch(101, {"auto_add_enabled": False})
        raise AssertionError("a wire that may not write accepted a write")
    except fa.Refused:
        pass


def test_an_allowed_run_sends_exactly_the_list_and_logs_what_was_there():
    w = FakeWire([F1, F2, F3], allowed=True)
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "applied.jsonl"
        done = fa.apply_plan(w, PLAN, log_path=log, out=lambda s: None)
        assert len(done["changed"]) == 3 and not done["stopped"], done
        assert w.patches[0] == (101, {"auto_add_enabled": False})
        assert w.patches[1] == (102, {"tags": ["Priority 1"]})
        sent = w.patches[2][1]
        # the new criteria are merged in: what was there stays, and the addresses block is untouched
        assert sent["filter_data"]["filters"] == {"type_single_family": True, "distressors_include": "is_obituary_property",
                                                  "value_max": 700000, "extra_equity_percent_min": 30}
        assert sent["filter_data"]["addresses"] == ADDR and sent["tags"] == ["Obituary"]
        rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        assert [r["id"] for r in rows] == [101, 102, 103]
        assert rows[0]["before"] == {"auto_add_enabled": True} and rows[1]["before"] == {"tags": ["Old Tier", "Priority 1"]}
        assert rows[2]["before"]["filter_data"]["filters"] == F3["filter_data"]["filters"]
        # undo sends those values back, newest first
        back = fa.undo(w, log, out=lambda s: None)
        assert len(back["restored"]) == 3 and not back["stopped"]
        assert w.store[101]["auto_add_enabled"] is True and w.store[102]["tags"] == ["Old Tier", "Priority 1"]
        assert w.store[103]["filter_data"] == F3["filter_data"] and w.store[103]["tags"] == []


def test_a_feeder_that_changed_since_the_list_is_skipped():
    moved = copy.deepcopy(F2)
    moved["tags"] = ["Priority 1", "Something New"]
    w = FakeWire([F1, moved, F3], allowed=True)
    done = fa.apply_plan(w, PLAN, out=lambda s: None)
    assert any("102" in s and "tags changed" in s for s in done["skipped"]) and len(done["changed"]) == 2
    assert all(fid != 102 for fid, _ in w.patches)


def test_a_deleted_feeder_and_a_never_touch_feeder_are_left_alone():
    gone = copy.deepcopy(F1)
    gone["is_active"] = False
    w = FakeWire([gone, F2, F3], allowed=True)
    done = fa.apply_plan(w, plan_of([F1, F2, F3], [CH1, CH2, CH3], never=[103]), out=lambda s: None)
    assert any("101" in s and "deleted" in s for s in done["skipped"])
    assert any("103" in s and "never-touch" in s for s in done["skipped"])
    assert [fid for fid, _ in w.patches] == [102]


def test_a_write_that_does_not_read_back_stops_the_run():
    w = FakeWire([F2, F3], allowed=True, ignore_tags=True)
    done = fa.apply_plan(w, plan_of([F2, F3], [CH2, CH3]), out=lambda s: None)
    assert "did not read back" in done["stopped"] and "102" in done["stopped"]
    assert [fid for fid, _ in w.patches] == [102], "nothing more is sent after the first write that did not stick"


def test_already_as_listed_is_not_sent_again():
    done_already = copy.deepcopy(F1)
    done_already["auto_add_enabled"] = False
    w = FakeWire([done_already], allowed=True)
    plan = plan_of([F1], [CH1])
    plan["feeders"][0]["auto_add"] = False          # the list was written about it as it stands
    done = fa.apply_plan(w, plan, out=lambda s: None)
    assert done["already"] and not w.patches


def test_auto_add_is_never_switched_on_and_unknown_fields_are_refused():
    for bad in ({"auto_add_enabled": True}, {"replace_owners_enabled": True}):
        try:
            fa.body_for({"id": 101, "set": bad}, F1)
            raise AssertionError("accepted %s" % bad)
        except fa.FeederError:
            pass


def test_only_filter_addresses_are_reachable():
    w = fa.Wire("k", allowed=True, gap=0, sleep=lambda s: None)
    for url in ("https://example.com/filters/1/", fa.MAP + "/properties/search/"):
        try:
            w._send("GET", url)
            raise AssertionError("reached %s" % url)
        except fa.FeederError:
            pass


def test_the_module_holds_no_long_dash_or_backspace_byte():
    raw = (ROOT / "src" / "agents" / "autopilot" / "feeder_apply.py").read_bytes()
    assert b"\x08" not in raw and "—".encode() not in raw and "–".encode() not in raw


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  %s" % name)
            n += 1
    print("%d tests passed" % n)
