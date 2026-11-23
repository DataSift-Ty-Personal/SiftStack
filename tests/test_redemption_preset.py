"""The Tax Sale Redemption preset script: dry by default, one preset, read back, undone only as made.

Zero network: a fake account answers the five routes the script uses. Every id here is invented.

Run: python -m pytest tests/test_redemption_preset.py
"""
import copy
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("AGENTS_DATA_DIR", tempfile.mkdtemp(prefix="redemption_preset_"))   # never the real database

from agents.autopilot import redemption_preset as R  # noqa: E402

TAG_ID = "00000000-0000-4000-8000-0000000000aa"
LM = "00000000-0000-4000-8000-0000000000f1"
DP = "00000000-0000-4000-8000-0000000000f2"
ACCOUNT = "00000000-0000-4000-8000-0000000000ac"


class NotFound(Exception):
    code = 404


class Account:
    def __init__(self, tags=("Tax Sale Redemption",), presets=None):
        self.tags = [{"uuid": TAG_ID if t == "Tax Sale Redemption" else "t-%d" % i, "title": t} for i, t in enumerate(tags)]
        self.folders = [{"uuid": LM, "title": "15. Lead Management"}, {"uuid": DP, "title": "11. DEEP PROSPECTING (ALL TIERS)"}]
        self.presets = presets if presets is not None else {
            "p-1": {"uuid": "p-1", "title": "3. Leads Missing Tasks", "folder": LM,
                    "filters": {"must": {"tasks_incomplete": [0, 0]}, "account": ACCOUNT}}}
        self.sent, self.n = [], 0

    def call(self, path, method="GET", body=None, headers=None):
        p = path.split("?")[0]
        if method != "GET":
            self.sent.append((method, p, copy.deepcopy(body)))
        if p == "/api/internal/tag/":
            return {"results": self.tags}
        if p == "/api/internal/filter-preset-folder/":
            return {"results": self.folders}
        if p.startswith("/api/internal/filter-preset-folder/") and p.endswith("/filter-preset/"):
            fid = p.split("/")[4]
            return {"results": [copy.deepcopy(x) for x in self.presets.values() if x["folder"] == fid]}
        if p == "/api/internal/filter-preset/" and method == "POST":
            self.n += 1
            uid = "made-%d" % self.n
            self.presets[uid] = {"uuid": uid, "title": body["title"], "folder": body["folder"], "filters": body["filters"]}
            return {"uuid": uid}
        if p.startswith("/api/internal/filter-preset/"):
            uid = p.split("/")[4]
            if uid not in self.presets:
                raise NotFound(uid)
            if method == "DELETE":
                del self.presets[uid]
                return {}
            return copy.deepcopy(self.presets[uid])
        raise AssertionError("unexpected route %s %s" % (method, path))

    def count(self, must):
        return 3


def test_the_plan_reads_only_and_builds_the_tag_filter_in_the_lead_managers_folder():
    acc = Account()
    p = R.plan(acc)
    assert acc.sent == [] and p["must"] == {"any_tags": [TAG_ID]} and p["folder_uuid"] == LM and p["count"] == 3
    assert p["body"]["filters"]["account"] == ACCOUNT, "the account id is copied from a preset in the same folder"
    assert R.apply(acc, p, out=lambda s: None)["would"] and acc.sent == [], "without --commit nothing is sent"


def test_a_missing_tag_or_folder_stops_the_run():
    with pytest.raises(R.Refused):
        R.plan(Account(tags=("Something Else",)))
    with pytest.raises(R.Refused):
        R.plan(Account(), folder="99. No Such Folder")


def test_commit_makes_one_preset_reads_it_back_and_logs_it(tmp_path):
    acc = Account()
    done = R.apply(acc, R.plan(acc), commit=True, out=lambda s: None, out_dir=tmp_path)
    made = acc.presets[done["made"]]
    assert [m for m, _, _ in acc.sent] == ["POST"] and made["title"] == "Tax Sale Redemption" and made["folder"] == LM
    assert (tmp_path / os.path.basename(done["log"])).read_text(encoding="utf-8").count('"step": "preset"') == 1
    again = R.plan(acc)
    assert again["already"]["uuid"] == done["made"] and R.apply(acc, again, commit=True, out=lambda s: None)["already"]
    assert [m for m, _, _ in acc.sent] == ["POST"], "a second run finds it there and sends nothing"


def test_a_preset_of_that_title_with_another_filter_is_left_alone():
    taken = {"x": {"uuid": "x", "title": "Tax Sale Redemption", "folder": DP, "filters": {"must": {"any_tags": ["other"]}}}}
    with pytest.raises(R.Refused):
        R.plan(Account(presets=taken))


def test_undo_deletes_only_what_the_script_made(tmp_path):
    acc = Account()
    uid = R.apply(acc, R.plan(acc), commit=True, out=lambda s: None, out_dir=tmp_path)["made"]
    assert R.undo(acc, uid, out=lambda s: None) == "would" and uid in acc.presets, "dry until --commit"
    edited = copy.deepcopy(acc)
    edited.presets[uid]["filters"]["must"]["phone"] = 1
    with pytest.raises(R.Refused):
        R.undo(edited, uid, commit=True, out=lambda s: None, out_dir=tmp_path)
    assert R.undo(acc, uid, commit=True, out=lambda s: None, out_dir=tmp_path) == "deleted" and uid not in acc.presets
    assert R.undo(acc, uid, commit=True, out=lambda s: None, out_dir=tmp_path) == "gone"
