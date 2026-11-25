"""tools/check_docs.py: the DataSift login wording check. Zero network.

Users reach DataSift with their Open API key only. The check fails on any public skill,
plugin, setup doc or API doc text that tells someone to use a DataSift login, except in a
file marked staff-only. These tests build small trees in a temp folder, so they never
depend on what the repo holds today.

Run: python tests/test_check_docs_login.py   (or pytest)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import check_docs as C  # noqa: E402


def tree(files: dict) -> Path:
    d = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


def flagged(hits) -> list:
    return sorted({h.split(":")[0] for h in hits})


def test_catches_every_login_shape():
    root = tree({
        "skills/a/SKILL.md": "Set DATASIFT_EMAIL and DATASIFT_PASSWORD in .env.\n",
        "skills/b/SKILL.md": "1. Log into app.reisift.io. Open DevTools (F12) and copy the token.\n",
        "skills/c/scripts/run.py": '"""Run.\n\n    python run.py --target you@example.com --jwt <paste>\n"""\n',
        "skills/d/SKILL.md": "Your own DataSift login works here.\n",
        "plugins/p/skills/s/SKILL.md": "Copy it from any request to apiv2.reisift.io (the authorization: Bearer header).\n",
        "docs/setup/x.md": "kpi-engine needs REISIFT_TOKEN.\n",
        "docs/api/y.md": "python clone_account.py --email you@example.com --password '...'\n",
        ".env.skills.example": "REISIFT_TARGET_JWT=\n",
        "skills/e/scripts/mint.py": 'TOKEN_ROUTE = "/api/token/"\n',
        "skills/f/SKILL.md": "Enter your DataSift email and\npassword on the sign in page.\n",
    })
    got = flagged(C.check_login_wording(root, requires={}))
    assert got == [".env.skills.example", "docs/api/y.md", "docs/setup/x.md", "plugins/p/skills/s/SKILL.md",
                   "skills/a/SKILL.md", "skills/b/SKILL.md", "skills/c/scripts/run.py", "skills/d/SKILL.md",
                   "skills/e/scripts/mint.py", "skills/f/SKILL.md"], got


def test_staff_only_files_are_exempt_and_other_services_are_not_flagged():
    root = tree({
        "skills/a/scripts/staff.py": '"""Staff-only. A pasted JWT, or one minted from email + password."""\nJWT = 1\n',
        "skills/b/SKILL.md": "<!-- staff-only -->\nSet REISIFT_TOKEN.\n",
        "docs/api/county-data.md": "Open the county portal with DevTools open, then Copy as cURL.\n",
        "skills/c/SKILL.md": "SmartSkip: SMARTSKIP_EMAIL and SMARTSKIP_PASSWORD.\nYour SmartSkip password stays in .env.\n",
        "skills/d/SKILL.md": "Set REISIFT_API_KEY to your DataSift Open API key, the one DataSift credential.\n",
        "skills/manifest.json": '{"env": ["REISIFT_TOKEN"]}',
    })
    assert C.check_login_wording(root, requires={}) == []


def test_a_staff_marker_deep_in_a_file_does_not_exempt_it():
    body = "\n".join(["line %d" % i for i in range(30)]) + "\nstaff-only\nSet DATASIFT_PASSWORD.\n"
    root = tree({"skills/a/SKILL.md": body})
    assert flagged(C.check_login_wording(root, requires={})) == ["skills/a/SKILL.md"]


def test_the_requires_table_is_scanned_as_the_manifest_source():
    hits = C.check_login_wording(tree({}), requires={
        "x": {"env": ["DATASIFT_EMAIL", "DATASIFT_PASSWORD"], "note": "fine"},
        "y": {"env": ["REISIFT_API_KEY"], "note": "Runs on your DataSift Open API key."},
        "z": {"env": [], "note": "Mints its own token from your DataSift login."}})
    assert sorted(h.split("[")[1].split("]")[0] for h in hits) == ["'x'", "'z'"], hits


if __name__ == "__main__":
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok  " + n)
    print("%d tests passed" % len(names))
