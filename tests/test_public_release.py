"""The public release (tools/public_release.py): what a release leaves out whatever the map says, the commit
message that names the lessons a release adds, and the shape of docs/lessons-learned.md, which the issue
fixer writes into with every fix it ships (Ty, 2026-11-24: "let's just do a release whenever a fix goes
out"). Zero network, no private map needed.

Run: python -m pytest tests/test_public_release.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import public_release as R  # noqa: E402

LESSONS = ROOT / "docs" / "lessons-learned.md"
FIELDS = ("**What happened:**", "**Why:**", "**The fix:**", "**Found by:**")


def test_private_is_left_out_whatever_the_map_says(tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "public_release.json").write_text("{}", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    gone = R.leave_out(tmp_path, R.ALWAYS_LEAVE_OUT + [])
    assert gone == ["private/public_release.json"]
    assert not (tmp_path / "private").exists() and (tmp_path / "docs" / "guide.md").exists()


def test_lesson_titles_are_the_second_level_headings(tmp_path):
    p = tmp_path / "lessons.md"
    p.write_text("# Lessons\n\nIntro.\n\n## 2026-11-24: A new one\n\n- **What happened:** x\n\n### not a lesson\n\n"
                 "## 2026-11-20: An old one\n", encoding="utf-8")
    assert R.lesson_titles(p) == ["2026-11-24: A new one", "2026-11-20: An old one"]
    assert R.lesson_titles(tmp_path / "missing.md") == []


def test_the_release_message_names_its_new_lessons():
    assert R.release_message("2026-11-24", []) == "Release 2026-11-24: the evergreen SiftStack"
    one = R.release_message("2026-11-24", ["2026-11-24: Tax feeds searched a single day"])
    assert one.splitlines()[0] == "Release 2026-11-24: Tax feeds searched a single day"
    assert "- 2026-11-24: Tax feeds searched a single day" in one
    two = R.release_message("2026-11-24", ["2026-11-24: First", "2026-11-23: Second"])
    assert two.splitlines()[0] == "Release 2026-11-24: First (and 1 more)" and "- 2026-11-23: Second" in two
    own = R.release_message("2026-11-24", ["2026-11-24: First"], message="Release by hand")
    assert own.splitlines()[0] == "Release by hand" and "- 2026-11-24: First" in own


def test_built_from_reads_the_source_commit_off_a_release_message():
    msg = ("Release 2026-11-23: x\n\nBuilt by tools/public_release.py from 0cd8db2a1b2c: 39 private file(s) left out")
    assert R.built_from(msg) == "0cd8db2a1b2c"
    assert R.built_from("a commit someone made by hand") == ""


def test_the_lessons_file_keeps_its_shape():
    text = LESSONS.read_text(encoding="utf-8")
    assert text.count("<!-- new lessons go here -->") == 1, "the fixer writes each new lesson under this line"
    marker = text.index("<!-- new lessons go here -->")
    titles = R.lesson_titles(LESSONS)
    assert titles, "there are lessons"
    assert marker < text.index("## " + titles[0]), "new lessons go above every old one"
    for t in titles:
        assert re.match(r"\d{4}-\d{2}-\d{2}: \S", t), "a lesson heading is '<date>: <title>': %r" % t
        assert len(t.split(": ", 1)[1]) <= 70, "a lesson title stays under 70 characters: %r" % t
    days = [t[:10] for t in titles]
    assert days == sorted(days, reverse=True), "newest first"
    blocks = re.split(r"^## ", text[marker:], flags=re.M)[1:]
    assert len(blocks) == len(titles)
    for b in blocks:
        for f in FIELDS:
            assert ("- " + f) in b, "%r is missing %s" % (b.splitlines()[0], f)
    assert chr(0x2014) not in text and chr(0x2013) not in text, "no long dashes"
