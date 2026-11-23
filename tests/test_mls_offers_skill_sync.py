"""skills/mls-offer-engine ships COPIES of the engine; they must not drift from src/ and data/.

Zero network. Line endings are compared normalized, because the skill copy is LF (skills/** is LF in
git) while a source may sit on disk with CRLF on a Windows checkout.

    python tests/test_mls_offers_skill_sync.py      (or pytest)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import sync_mls_offers_skill as sync  # noqa: E402

FIX = "run python tools/sync_mls_offers_skill.py"


def test_every_copy_matches_its_source():
    for src_rel, dst_rel in sync.FILES:
        src, dst = ROOT / src_rel, sync.SKILL / dst_rel
        if not src.is_file():
            assert not dst.is_file(), "%s is in the skill but %s does not exist: %s" % (dst_rel, src_rel, FIX)
            continue
        assert dst.is_file(), "%s is missing from the skill: %s" % (dst_rel, FIX)
        assert sync.lf(src.read_bytes()) == sync.lf(dst.read_bytes()), "%s drifted from %s: %s" % (dst_rel, src_rel, FIX)


def test_copies_are_lf():
    for _src, dst_rel in sync.FILES:
        dst = sync.SKILL / dst_rel
        if dst.is_file():
            assert b"\r\n" not in dst.read_bytes(), "%s has CRLF line endings: %s" % (dst_rel, FIX)


def test_skill_has_no_copy_the_tool_does_not_know():
    known = {dst for _src, dst in sync.FILES}
    for folder in ("scripts", "data"):
        base = sync.SKILL / folder
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                rel = p.relative_to(sync.SKILL).as_posix()
                assert rel in known, "%s is in the skill but no source feeds it; add it to %s or remove it" % (
                    rel, "tools/sync_mls_offers_skill.py")


def test_county_table_is_where_the_public_scan_skips_it():
    # Census county names collide with words the public scanner keeps out of the repo. The scanner
    # skips data/** inside an archive and the skill's own data/ folder in the tree, nowhere else.
    sys.path.insert(0, str(ROOT / "tools"))
    import json
    import public_scan
    skip = public_scan.matcher(json.loads((ROOT / "tools" / "public_scan_rules.json").read_text(encoding="utf-8"))["skip"])
    for _src, dst_rel in sync.FILES:
        if dst_rel.endswith(".csv"):
            assert skip(dst_rel), "%s inside the archive is not skipped by the public scan" % dst_rel
            tree = "skills/mls-offer-engine/" + dst_rel
            assert skip(tree), "%s is not skipped by the public scan; add skills/mls-offer-engine/data/** to tools/public_scan_rules.json" % tree


def test_skill_text_has_no_dashes_and_no_internal_route():
    bad = (chr(0x2014), chr(0x2013))
    for p in sync.SKILL.rglob("*.md"):
        text = p.read_text(encoding="utf-8")
        for ch in bad:
            assert ch not in text, "%s contains an em or en dash" % p.relative_to(ROOT).as_posix()
        assert "--source cluster" not in text, "%s names the staff-only cluster source" % p.relative_to(ROOT).as_posix()


if __name__ == "__main__":
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok  " + n)
    print("%d tests passed" % len(names))
