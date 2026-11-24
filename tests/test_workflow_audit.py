"""tools/workflow_audit.py: the repo passes, and each shape of the fault that shipped before is caught."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import workflow_audit as W  # noqa: E402

DESK = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "Desktop" + "\\" + "clients"   # built, so this file holds no such path


def test_the_repo_has_no_workflow_only_one_machine_can_run():
    found = W.audit(ROOT)
    assert not found, "\n".join("%s %s:%d %s" % (f["check"], f["file"], f["line"], f["what"]) for f in found)


def tree(tmp_path: Path, files: dict, agents: list | None = None) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    if agents is not None:
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "agents.json").write_text(json.dumps({"departments": [{"agents": agents}]}), encoding="utf-8")
    return tmp_path


def checks(found):
    return sorted((f["check"], f["file"]) for f in found)


def test_the_shapes_that_shipped_before_are_caught(tmp_path):
    root = tree(tmp_path, {
        "src/logo.py": 'LOGO = r"%s"\n' % DESK,
        "src/home.py": "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path.home() / 'x'))\n",
        "src/needs.py": "import crm_api\n",
        "src/pool.py": "from opensearch_mcp import OpenSearchMCP\n",
    }, agents=[{"id": "pool", "role": "Builds pools.", "status": "live", "source": "src/pool.py"}])
    got = checks(W.audit(root))
    assert ("machine-path", "src/logo.py") in got and ("machine-path", "src/home.py") in got
    assert ("import", "src/needs.py") in got and ("import", "src/pool.py") in got
    assert ("staff", "src/pool.py") in got


def test_what_is_not_a_fault_is_left_alone(tmp_path):
    root = tree(tmp_path, {
        "src/documented.py": '"""Once read %s, now it reads the key."""\nimport requests\n' % DESK.replace("\\", "/"),
        "src/optional.py": "try:\n    import crm_api\nexcept ImportError:\n    crm_api = None\n",
        "src/marked.py": "# staff-only: the opt-in checkout\nimport crm_api\n",
        "src/staffmod.py": '"""Staff-only: the team\'s own workstation."""\nfrom opensearch_mcp import X\nP = r"%s"\n' % DESK,
        "src/labelled.py": "from opensearch_mcp import OpenSearchMCP  # noqa\n",
        "src/sibling.py": "import helper\n", "src/helper.py": "X = 1\n",
    }, agents=[{"id": "lab", "role": "Staff only: matches lots.", "status": "gated", "source": "src/labelled.py"},
               {"id": "old", "role": "Retired.", "status": "retired", "source": "src/labelled.py"}])
    found = W.audit(root)
    assert [f for f in found if f["file"] != "src/labelled.py"] == []
    # the staff source in labelled.py is fine for a staff-labelled agent; its import is still nothing a user installs
    assert checks(found) == [("import", "src/labelled.py")]


def test_selftests_and_tests_are_not_shipped_workflows(tmp_path):
    root = tree(tmp_path, {"src/selftest_x.py": 'P = r"%s"\n' % DESK, "src/test_y.py": "import crm_api\n"})
    assert W.audit(root) == []


def test_the_command_line_exits_on_a_finding(tmp_path, capsys):
    root = tree(tmp_path, {"src/bad.py": 'P = r"%s"\n' % DESK})
    assert W.main([str(root)]) == 1
    assert "1 finding" in capsys.readouterr().out
    assert W.main([str(tree(tmp_path / "clean", {"src/ok.py": "X = 1\n"}))]) == 0
