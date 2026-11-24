"""The step-by-step account guide (docs/account-structure.md) is generated from the blueprint the
account-blueprint skill ships and from the autopilot playbook. These checks need no network.

    python tests/test_render_account_structure.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import render_account_structure as R  # noqa: E402


def test_every_folder_with_presets_has_its_section():
    bp_path = R.newest_blueprint()
    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    text = R.render(bp_path)
    for f in bp["preset_folders"]:
        if f.get("presets"):
            assert "### %s" % f["title"] in text, f["title"]
            for p in f["presets"]:
                assert "**%s**" % p["title"] in text, p["title"]


def test_guide_carries_no_secret_and_no_dash():
    text = R.render(R.newest_blueprint())
    assert "/hooks/" not in text, "a webhook receiver's address never reaches the guide"
    assert "—" not in text and "–" not in text


def test_committed_guide_is_current():
    assert R.main(["--check"]) == 0, "run: python tools/render_account_structure.py"


def test_filters_read_as_words():
    shared = R.Shared([])
    p = {"title": "T", "filters": {"must": {
        "phone": 1, "skiptraced": 1, "sms_attempts": [0, 0], "predictivecall_attempts": [1, 1],
        "ownerPropertiesOwned": {"show_properties": "one_per_owner"},
        "last_direct_mailed": ["36-months", "week"],
        "assigned_to": {"$ref": "user", "title": "Caller"},
        "must_not": {"any_tags": [{"$ref": "tag", "title": "Do Not Market"}], "any_neighborhood": ["A", "B"]}}}}
    lines = R.filter_lines(p, shared)
    assert "has a phone number" in lines and "texts sent: 0" in lines and "dialer call attempts: 1" in lines
    assert "shows one property per owner" in lines and "is assigned to Caller" in lines
    assert "last mailed from 36 months before a week ago up to a week ago" in lines
    assert "does not have the tags: Do Not Market" in lines
    assert any("leaves out 2 neighborhoods" in ln for ln in lines)
    assert not any("{" in ln for ln in lines), lines


if __name__ == "__main__":
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok  " + n)
    print("%d tests passed" % len(names))
