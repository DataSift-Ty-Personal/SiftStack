"""Selftest: SiftStack's command-line scripts from Slack (agents/commands.py, workflows/run_script.py).

Zero network and no process is started: the scripts are invented files in a throwaway repo, read by
the catalog and never imported (one of them exits the moment it is imported); the process runner is
a fake, and so are Popen and Slack. Invented names, streets, phones (the never-assigned 865-000
exchange) and secrets only.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DASH, NDASH = chr(0x2014), chr(0x2013)

REPO = {
    "deploy/requirements-agents.txt": "requests>=2.31\nopenpyxl>=3.1   # workbooks\npyyaml>=6\n",
    "src/config.py": 'HOST = "https://api.trestleiq.com/3.0"\n',
    "src/cmdt_helper_free.py": "def f():\n    return 1\n",
    "src/not_a_cli.py": "def f():\n    return 1\n",
    "src/cmdt_report.py": '''"""Report on invented records, read only."""
import argparse
import config
import cmdt_helper_free

COUNTIES = ("Knox", "Blount")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", choices=COUNTIES, default="Knox", help="the county {DASH} one of two")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--out", default="output/report.csv")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--names", nargs="+")
    ap.add_argument("--since")
    ap.add_argument("--url")
    ap.parse_args()


if __name__ == "__main__":
    main()
''',
    "src/cmdt_writer.py": '''"""Writes when told to."""
import argparse

PHASES = ["plan", "apply"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--run", default="", help="run folder")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--phase", choices=PHASES + ["all"], default="plan")
    ap.add_argument("--list", required=True)
    ap.parse_args()


if __name__ == "__main__":
    main()
''',
    "src/cmdt_dryswitch.py": '''"""Writes unless told not to."""
import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    client = None
    if not a.dry_run:
        client.add_tags("u", ["x"])


if __name__ == "__main__":
    main()
''',
    "src/cmdt_always.py": '''"""Tags every record it sees."""


def main(client):
    client.add_tags("u", ["x"])


if __name__ == "__main__":
    main(None)
''',
    "src/cmdt_sub.py": '''"""Subcommands made in a loop."""
import argparse


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "repair"):
        p = sub.add_parser(name)
        p.add_argument("--commit", action="store_true")
        p.add_argument("--limit", type=int, default=0)
    ap.parse_args()


if __name__ == "__main__":
    main()
''',
    "src/cmdt_shared.py": '''"""Options a helper adds."""
import argparse


def add_common(p):
    p.add_argument("--zip")


def main():
    ap = argparse.ArgumentParser()
    add_common(ap)
    ap.add_argument("--top", type=int)
    ap.parse_args()


if __name__ == "__main__":
    main()
''',
    "src/cmdt_helper_browser.py": "from playwright.sync_api import sync_playwright\n",
    "src/cmdt_browser.py": '"""Drives a page."""\nimport cmdt_helper_browser\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_lazybrowser.py": '"""Uploads through the app."""\n\n\ndef main():\n    import datasift_core\n    return datasift_core\n\n\nif __name__ == "__main__":\n    main()\n',
    "src/cmdt_optional.py": '''"""A browser it can do without."""
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

if __name__ == "__main__":
    pass
''',
    "src/cmdt_dealroom.py": '"""Needs the Deal Room."""\nfrom crm_api import CRMClient\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_desktop.py": '''"""Reads a folder on the PC."""
from pathlib import Path

BASE = Path.home() / "OneDrive" / "Desktop" / "Deal Room Coaching Call"

if __name__ == "__main__":
    print(BASE)
''',
    "src/cmdt_desktop_doc.py": '''"""Names C:/Users/someone/Desktop in prose only."""
# The Deal Room Coaching Call folder is named in a comment, never read.

if __name__ == "__main__":
    print("ok")
''',
    "src/cmdt_desktop_fallback.py": '''"""Prefers the Deal Room, falls back to the key."""
from pathlib import Path

BASE = Path.home() / "OneDrive" / "Desktop" / "Deal Room Coaching Call"


def client():
    import crm_key_client
    return crm_key_client


if __name__ == "__main__":
    print(BASE)
''',
    "src/cmdt_pandas.py": '"""Frames."""\nimport pandas\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_pandas_lazy.py": '"""Frames, sometimes."""\n\n\ndef f():\n    import pandas\n    return pandas\n\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_requests.py": '"""Installed packages."""\nimport requests\nimport openpyxl\nimport yaml\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_paid.py": '''"""Scores numbers."""
import argparse

URL = "https://api.trestleiq.com/3.0/phone_intel"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--top", type=int, default=5)
    ap.parse_args()


if __name__ == "__main__":
    main()
''',
    "src/cmdt_helper_vendor.py": 'URL = "https://tracerfy.com/v1/api/trace/"\n',
    "src/cmdt_paid_helper.py": '"""Traces through a helper."""\nimport cmdt_helper_vendor\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_helper_lazy_sdk.py": "def ask():\n    import anthropic\n    return anthropic\n",
    "src/cmdt_lazy_sdk_helper.py": '"""A helper that asks a model sometimes."""\nimport cmdt_helper_lazy_sdk\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_own_sdk.py": '"""Asks a model."""\n\n\ndef ask():\n    import anthropic\n    return anthropic\n\n\nif __name__ == "__main__":\n    ask()\n',
    "src/cmdt_detail.py": '"""Reads property detail."""\n\n\ndef main(sm):\n    return sm.get_detail("1")\n\n\nif __name__ == "__main__":\n    main(None)\n',
    "src/cmdt_argv.py": '"""Reads its own argv."""\nimport sys\n\nif __name__ == "__main__":\n    if "--selftest" in sys.argv:\n        print("ok")\n',
    "src/cmdt_none.py": '"""Takes nothing."""\n\nif __name__ == "__main__":\n    print("hi")\n',
    "src/cmdt_pkg/__init__.py": "",
    "src/cmdt_pkg/cli.py": '''import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("plan", "verify"))
    ap.parse_args()
''',
    "src/cmdt_delegate.py": '"""Delegates its command line."""\nfrom cmdt_pkg.cli import main\n\nif __name__ == "__main__":\n    main()\n',
    "src/cmdt_explodes.py": '''raise SystemExit("this module was imported")
import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--x")
''',
    "src/cmdt_retired.py": '"""An old one-off."""\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_wired.py": '"""A workflow runs this."""\n\nif __name__ == "__main__":\n    pass\n',
    "src/cmdt_planned.py": '"""A planned workflow lists this."""\n\nif __name__ == "__main__":\n    pass\n',
}


def _write_repo(root: Path) -> None:
    for rel, text in REPO.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text.replace("{DASH}", DASH), encoding="utf-8")
    (root / "output").mkdir(exist_ok=True)
    (root / "output" / "old.csv").write_text("a,b\n1,2\n", encoding="utf-8")


def _fake_roster():
    from . import team
    emps = {eid: {"display_name": n, "role": r, "slack_channel": c, "model": "m", "job": "j", "introduction": "i"}
            for eid, n, r, c in (("analysis", "Atlas", "Analysis", "a"), ("research", "Diggs", "Research", "b"),
                                 ("sales", "Roma", "Sales", "c"), ("ops", "Quinn", "Operations", "d"))}
    wfs = {"cmdt_live": {"owner": "analysis", "summary": "s", "status": "live", "spend": "free", "triggers": ["slack"],
                         "sources": ["cmdt_wired", "agents.workflows.cmdt_live"]},
           "cmdt_plan": {"owner": "analysis", "summary": "s", "status": "planned", "spend": "free", "sources": ["cmdt_planned"]},
           "run_script": {"owner": "ops", "summary": "s", "status": "live", "spend": "free", "sources": ["cmdt_report"]}}
    return team.parse({"employees": emps, "workflows": wfs})


def _text_of(out: dict) -> str:
    return "".join(c.get("text", "") for c in out.get("content") or [])


class _Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


class _Slack:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None, headers=None):
        if url.endswith("chat.postMessage"):
            self.posts.append(dict(json or {}))
        return _Resp({"ok": True, "ts": "1900000000.%06d" % len(self.posts)})

    def get(self, url, params=None, timeout=None, headers=None):
        return _Resp({"ok": False})


class _Runner:
    """Stands where the process starts: records each call, writes the log and the files a script
    would leave, and answers with the exit code it was given."""

    def __init__(self, root: Path):
        self.root, self.calls = root, []
        self.output, self.rc, self.timed_out, self.files = "", 0, False, {}

    def __call__(self, argv, cwd, env, timeout, log_path):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env), "timeout": timeout, "log": Path(log_path)})
        Path(log_path).write_text(self.output, encoding="utf-8")
        for rel, data in self.files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return {"rc": self.rc, "timed_out": self.timed_out, "seconds": 1.5}


def run(check, tmp) -> None:
    from . import commands, config, store
    print("  the catalog, read from invented scripts")
    root = Path(tmp) / "cmdrepo"
    _write_repo(root)
    saved_retired = dict(commands.RETIRED)
    saved_free, saved_notpaid = dict(commands.FREE_DRY_RUN), dict(commands.NOT_PAID)
    env_keys = ("AGENTS_SCRIPT_ROOT", "AGENTS_SCRIPT_TIMEOUT_MIN", "AGENTS_SCRIPT_TOOLS", "AGENTS_SCRIPT_MAX_FILES",
                "AGENTS_SCRIPT_FILE_MB", "AGENTS_SCRIPT_LOG_MB", "AGENTS_SCRIPT_SLACK_CHARS", "SIFTSTACK_OUTPUT_DIR",
                "REISIFT_API_KEY", "CMDTEST_API_KEY", "CMDTEST_TRESTLE_API_KEY", "SLACK_CMDTEST_TOKEN")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    from . import workflows
    had_line = "run_script" in workflows.REGISTRY
    workflows.REGISTRY.setdefault("run_script", "run_script")    # the orchestrator's line, until it lands
    try:
        commands.RETIRED["cmdt_retired"] = ("an invented one-off from the selftest", "the cmdt_live workflow (Atlas)")
        commands.FREE_DRY_RUN["cmdt_paid"] = "the selftest's own vetted dry run"
        commands.NOT_PAID["cmdt_paid_helper"] = "the selftest's own reviewed exception"
        _catalog(check, commands, root)
        _real_tree(check, commands, config)
        for k in env_keys:
            os.environ.pop(k, None)
        os.environ["AGENTS_SCRIPT_ROOT"] = str(root)
        commands.reset()
        _decide(check, commands, config)
        _tools(check, commands, store)
        _worker(check, commands, config, store, root)
        _release_flow(check, commands, config, store, root)
        _popen(check, root)
    finally:
        commands.RETIRED.clear()
        commands.RETIRED.update(saved_retired)
        commands.FREE_DRY_RUN.clear()
        commands.FREE_DRY_RUN.update(saved_free)
        commands.NOT_PAID.clear()
        commands.NOT_PAID.update(saved_notpaid)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        commands.reset()
        from .workflows import run_script
        run_script.RUNNER = None
        config.MODE = "dry"
        if not had_line:
            workflows.REGISTRY.pop("run_script", None)
    _hygiene(check)


# ------------------------------------------------------------------------------------ the builder

def _catalog(check, commands, root: Path) -> None:
    roster = _fake_roster()
    cat = commands.build(root, roster=roster)
    S = cat["scripts"]
    names = set(S)
    check({"cmdt_report", "cmdt_delegate", "cmdt_explodes", "cmdt_argv", "cmdt_none"} <= names and "not_a_cli" not in names
          and "config" not in names and "cmdt_helper_free" not in names,
          "a script is a file with a main block or an argument parser; a plain module is not in the catalog")
    check(S["cmdt_explodes"]["class"] == "runnable" and S["cmdt_explodes"]["args"][0]["flags"] == ["--x"]
          and "cmdt_explodes" not in sys.modules,
          "a script is read, never imported: one that exits on import is still in the catalog with its option")
    rep = {a["flags"][0]: a for a in S["cmdt_report"]["args"]}
    check(rep["--county"]["choices"] == ["Knox", "Blount"] and rep["--county"]["default"] == "Knox"
          and rep["--limit"]["type"] == "int" and rep["--lon"]["type"] == "float" and rep["--verbose"]["kind"] == "switch"
          and rep["--verbose"]["flags"] == ["--verbose", "-v"] and rep["--names"]["nargs"] == "+",
          "options are read with their kind, choices from a module constant, type, default and nargs")
    check(rep["--county"]["help"] == "the county , one of two" or ("one of two" in rep["--county"]["help"]
                                                                  and DASH not in rep["--county"]["help"]),
          "a long dash in a help text is taken out of the catalog")
    wr = {a["flags"][0]: a for a in S["cmdt_writer"]["args"]}
    check(wr["--list"].get("required") is True and wr["--phase"]["choices"] == ["plan", "apply", "all"],
          "a required option and choices built by adding two lists are read")
    check(S["cmdt_writer"]["write_flags"] == ["--commit"] and S["cmdt_writer"]["writes"] == "commit",
          "a --commit switch is a write flag; --run <folder> (a value) and --no-upload (a guard) are not")
    check(S["cmdt_dryswitch"]["writes"] == "default" and S["cmdt_dryswitch"]["dry_flags"] == ["--dry-run"],
          "a script with only a dry switch writes by default")
    check(S["cmdt_always"]["writes"] == "always" and S["cmdt_always"]["write_calls"],
          "a script whose own code writes with no switch at all writes on every run")
    check(S["cmdt_report"]["writes"] == "" and not S["cmdt_report"].get("write_flags"), "a report writes nothing")
    sub = S["cmdt_sub"]
    check(set(sub["subcommands"]) == {"audit", "repair"} and sub["subcommand_required"] is True
          and {a["flags"][0] for a in sub["subcommands"]["repair"]["args"]} == {"--commit", "--limit"},
          "subcommands made in a loop over a literal tuple each carry the loop's options")
    check({a["flags"][0] for a in S["cmdt_shared"]["args"]} == {"--zip", "--top"},
          "an option a helper adds to the parser is kept, so it is not refused as undeclared")
    check([a["flags"][0] for a in S["cmdt_delegate"]["args"]] == ["--phase"] and S["cmdt_delegate"].get("cli_via") == "cmdt_pkg.cli",
          "a script that hands its command line to a package's main is read from there")
    check(S["cmdt_argv"]["cli"] == "argv" and S["cmdt_argv"]["args"][0]["flags"] == ["--selftest"],
          "a script with no argparse that compares sys.argv to a literal gets that flag as a switch")
    check(S["cmdt_none"]["cli"] == "none", "a script that reads no arguments is marked so")

    check(S["cmdt_browser"]["class"] == "browser" and "playwright" in S["cmdt_browser"]["browser"],
          "a helper that imports playwright at module scope makes its script a browser script")
    check(S["cmdt_lazybrowser"]["class"] == "browser" and "datasift_core" in S["cmdt_lazybrowser"]["browser"],
          "the script's own function-scope import of the DataSift web app driver is a browser script too")
    check(S["cmdt_optional"]["class"] == "runnable", "a playwright import guarded by except ImportError is not a browser need")
    check(S["cmdt_dealroom"]["class"] == "local" and "crm_api" in " ".join(S["cmdt_dealroom"]["local"]),
          "a module-scope import of a Deal Room module is local to the owner's PC")
    check(S["cmdt_desktop"]["class"] == "local", "a Desktop path in code, with no key-only fallback, is local")
    check(S["cmdt_desktop_doc"]["class"] == "runnable", "a Desktop path named only in a docstring or a comment is not local")
    check(S["cmdt_desktop_fallback"]["class"] == "runnable",
          "a file that names a key-only client beside the Deal Room path falls back to it, so it is not local")
    check(S["cmdt_pandas"]["class"] == "package" and S["cmdt_pandas"]["needs"] == ["pandas"],
          "a module-scope import of a package the brain image does not install is refused as package")
    check(S["cmdt_pandas_lazy"]["class"] == "runnable" and S["cmdt_requests"]["class"] == "runnable",
          "a lazy import of a missing package, and packages the image installs (pyyaml as yaml), are fine")

    check(S["cmdt_paid"].get("paid") == ["Trestle"] and S["cmdt_paid"].get("free_dry_run"),
          "a vendor host in the script's own code makes it paid, and a vetted free dry run is recorded")
    check(not S["cmdt_report"].get("paid"), "a module named config.py declares hosts and makes nobody paid")
    check(S["cmdt_paid_helper"].get("paid") is None and "Tracerfy" in (S["cmdt_paid_helper"].get("paid_deeper") or [])
          and S["cmdt_paid_helper"].get("not_paid"),
          "a reviewed exception takes a vendor off a script, kept as a caution with the reason")
    check(not S["cmdt_lazy_sdk_helper"].get("paid") and "Claude" in (S["cmdt_lazy_sdk_helper"].get("paid_deeper") or []),
          "a helper's function-scope SDK import is a caution, not a paid class")
    check(S["cmdt_own_sdk"].get("paid") == ["Claude"], "the script's own function-scope SDK import makes it paid")
    check(S["cmdt_detail"].get("siftmap_reads") == ["get_detail"], "SiftMap detail reads in the script's own code are recorded")

    check(S["cmdt_retired"]["class"] == "retired" and S["cmdt_retired"]["replacement"].startswith("the cmdt_live"),
          "a script on the retired list is retired, with its replacement")
    check(S["cmdt_wired"]["class"] == "wired" and S["cmdt_wired"]["workflows"] == ["cmdt_live"]
          and "Atlas" in S["cmdt_wired"]["why"], "a script a live workflow lists in sources is wired, naming the workflow and owner")
    check(S["cmdt_planned"]["class"] == "runnable", "a planned workflow's sources are not wired")
    check(S["cmdt_report"]["class"] == "runnable", "run_script's own roster sources never make a script wired")

    again = commands.build(root, roster=roster)
    text = commands.render(cat)
    check(text == commands.render(again) and all(ord(c) < 128 for c in text),
          "two builds of one tree render the same, and the catalog is pure ASCII")
    tally: dict = {}
    for e in S.values():
        tally[e["class"]] = tally.get(e["class"], 0) + 1
    check(all(cat["counts"][k] == v for k, v in tally.items()), "the counts match the classes")


def _real_tree(check, commands, config) -> None:
    print("  the committed catalog against this source tree")
    from . import team
    roster = team.load(config.ROOT / "team" / "roster.yaml")
    fresh = json.loads(commands.render(commands.build(config.ROOT, roster=roster)))   # as written: emails masked
    p = config.ROOT / "team" / "commands.json"
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    bad = sorted(n for n in set(fresh["scripts"]) | set(old.get("scripts") or {})
                 if fresh["scripts"].get(n) != (old.get("scripts") or {}).get(n))
    check(not bad, "team/commands.json matches src/ (stale for %s; run python src/agents/commands.py --write)"
          % (", ".join(bad[:8]) or "nothing"))
    S = fresh["scripts"]
    keys = set(commands.RETIRED) - {"cmdt_retired"}
    check(keys <= set(S), "every retired name is a script in src/ (missing: %s)" % sorted(keys - set(S)))
    vetted = (set(commands.FREE_DRY_RUN) | set(commands.NOT_PAID)) - {"cmdt_paid", "cmdt_paid_helper"}
    check(vetted <= set(S), "every vetted dry run and paid exception names a script in src/")
    check(not [n for n, e in S.items() if e["class"] == "broken"], "every script in src/ parses")
    check(S.get("sold_guard", {}).get("class") == "runnable" and S.get("main", {}).get("class") == "browser",
          "the real tree reads as expected: the sold guard runs here, the main scraper needs a browser")


# ------------------------------------------------------------------------------------ one ask

def _e(**kw) -> dict:
    base = {"class": "runnable", "cli": "argparse", "complete": True, "writes": "", "summary": "x"}
    base.update(kw)
    return base


def _decide(check, commands, config) -> None:
    print("  what one ask becomes")
    D = commands.decide
    A = lambda *fl, **kw: dict({"flags": list(fl), "kind": kw.pop("kind", "value")}, **kw)  # noqa: E731
    rep = _e(args=[A("--county", choices=["Knox", "Blount"]), A("--limit", type="int"), A("--lon", type="float"),
                   A("--out"), A("--verbose", "-v", kind="switch"), A("--names", nargs="+"), A("--since"), A("--url"),
                   A("--uuid", required=True)])
    rep_noreq = dict(rep, args=[a for a in rep["args"] if a["flags"] != ["--uuid"]])
    d = D("cmdt_x", ["--county", "Knox", "--limit", "5", "--verbose"], rep_noreq)
    check(d.kind == "run" and d.argv == ["--county", "Knox", "--limit", "5", "--verbose"]
          and d.line == "python src/cmdt_x.py --county Knox --limit 5 --verbose", "a read-only command line runs as given")
    check(D("cmdt_x", "--county Blount --names 'Oak St' \"Elm Rd\"", rep_noreq).argv == ["--county", "Blount", "--names", "Oak St", "Elm Rd"],
          "a string is split like a terminal would, and nargs + takes several values")
    check(D("cmdt_x", "--county 'Knox", rep_noreq).kind == "refuse", "unbalanced quotes are refused")
    check(D("cmdt_x", ["--county=Knox"], rep_noreq).kind == "run", "the --flag=value form is read")
    check(D("cmdt_x", ["--lon", "-83.91"], rep_noreq).kind == "run", "a negative number is a value, not a flag")
    r = D("cmdt_x", ["--countie", "Knox"], rep_noreq)
    check(r.kind == "refuse" and "does not take --countie" in r.say and "--county" in r.say,
          "an undeclared flag is refused, with the near one named")
    w = _e(args=[A("--commit", kind="switch"), A("--list")], writes="commit", write_flags=["--commit"])
    r = D("cmdt_w", ["--comm"], w)
    check(r.kind == "refuse", "an abbreviation is refused: argparse would run --comm as --commit")
    for tok in ("a;b", "x|y", "$HOME", "`id`", "out>f", "a&b", "a\nb", "*.csv", "a\\b"):
        check(D("cmdt_x", ["--since", tok], rep_noreq).kind == "refuse", "a shell character is refused: %r" % tok)
    check(D("cmdt_x", ["--since", "O'Neal Rd 12"], rep_noreq).kind == "run", "an apostrophe and spaces in a value pass")
    check(D("cmdt_x", ["--county", "Sevier"], rep_noreq).say.startswith("--county takes one of Knox, Blount"),
          "a value outside the choices is refused, naming the choices")
    check(D("cmdt_x", ["--limit", "five"], rep_noreq).kind == "refuse" and D("cmdt_x", ["--lon", "west"], rep_noreq).kind == "refuse",
          "a number option refuses a word")
    check(D("cmdt_x", ["--verbose=1"], rep_noreq).kind == "refuse", "a switch given a value is refused")
    check(D("cmdt_x", ["--limit"], rep_noreq).say == "--limit needs a value.", "a value option with no value is refused")
    check(D("cmdt_x", ["--county", "Knox"], rep).say.endswith("needs --uuid."), "a required option that is missing is refused")
    check(D("cmdt_x", ["--", "--county"], rep_noreq).kind == "refuse", "the bare -- marker is refused")
    check(D("cmdt_x", ["loose"], rep_noreq).kind == "refuse", "a loose value with no flag is refused")
    check(D("cmdt_x", ["--since", "x" * 500], rep_noreq).kind == "refuse" and D("cmdt_x", ["-v"] * 70, rep_noreq).kind == "refuse",
          "a very long value and a very long command line are refused")

    print("  file and address values")
    for bad in ("/data/agents.db", "../secrets.csv", "output/../src/agents/worker.py", "report.csv", "~/notes.md",
                "C:/Users/someone/x.csv", "http://example.com/x", "https://localhost/x", "https://10.0.0.1/x",
                "https://box.internal/x"):
        check(D("cmdt_x", ["--out", bad], rep_noreq).kind == "refuse", "a value that names a file outside output/ is refused: %s" % bad)
    run_file = (Path(config.RUNS_DIR) / "run_script-1" / "files" / "x.csv").as_posix()
    for good, label in (("output/report.csv", "a file under output/"), ("output/sub/dir/a.json", "a nested file under output/"),
                        (run_file, "a file inside a run folder on the box"), ("https://example.com/page", "a public https address"),
                        ("11/01/2026", "a date written with slashes"), ("3/2", "beds over baths"),
                        (".5", "a number written with a leading point")):
        flag = "--since" if good[0].isdigit() else "--out"
        check(D("cmdt_x", [flag, good], rep_noreq).kind == "run", "allowed: %s" % label)

    print("  subcommands")
    subs = _e(subcommands={"audit": {"args": [A("--commit", kind="switch")]}, "repair": {"args": [A("--commit", kind="switch"),
                                                                                                    A("--limit", type="int")]}},
              subcommand_required=True, args=[], writes="commit", write_flags=["--commit"])
    check(D("cmdt_s", [], subs).kind == "refuse" and "needs one of audit, repair" in D("cmdt_s", [], subs).say,
          "a missing subcommand is refused, naming them")
    check(D("cmdt_s", ["undo"], subs).kind == "refuse", "an unknown subcommand is refused")
    check(D("cmdt_s", ["--limit", "3", "repair"], subs).kind == "refuse" and D("cmdt_s", ["repair", "--limit", "3"], subs).kind == "run",
          "a subcommand's options are read after the subcommand, the way argparse reads them")
    check(D("cmdt_s", ["audit", "--limit", "3"], subs).kind == "refuse", "an option another subcommand owns is refused")

    print("  refused classes")
    check(D("cmdt_x", ["--help"], rep).kind == "run", "--help alone runs: it prints the usage and stops")
    r = D("cmdt_missing_script", [])
    check(r.kind == "refuse" and "no script named cmdt_missing_script" in r.say, "an unknown script is refused")
    r = D("cmdt_old", [], _e(**{"class": "retired", "why": "an invented one-off", "replacement": "the x workflow (Atlas)"}))
    check(r.kind == "refuse" and "is retired: an invented one-off" in r.say and "Use the x workflow (Atlas) instead" in r.say,
          "a retired script is refused, naming its replacement")
    r = D("cmdt_old", [], _e(**{"class": "retired", "why": "gone", "replacement": ""}))
    check("Nothing replaces it" in r.say, "a retired script with no replacement says so")
    r = D("cmdt_w2", [], _e(**{"class": "wired", "workflows": ["idle_records"], "why": "runs inside idle_records (Atlas)"}))
    check(r.kind == "refuse" and "already runs inside idle_records (Atlas)" in r.say and "caps" in r.say,
          "a wired script is refused with the workflow that already does it, so its caps apply")
    for cls, why in (("browser", "drives a browser (playwright), and the brain has none"),
                     ("local", "needs the Deal Room checkout (crm_api), which only the owner's PC has"),
                     ("package", "imports pandas, which the brain image does not install")):
        r = D("cmdt_b", ["--phase", "plan"], _e(**{"class": cls, "why": why}))
        check(r.kind == "refuse" and r.say.endswith("On the workstation: python src/cmdt_b.py --phase plan") and why in r.say,
              "a %s script is refused with where it runs and the command line to type there" % cls)

    print("  writes and spend")
    check(D("cmdt_w", ["--list", "Probate"], w).kind == "run", "a write-capable script without its write flag is a dry run and runs")
    r = D("cmdt_w", ["--list", "Probate", "--commit"], w)
    check(r.kind == "stage" and "it writes (--commit)" in r.say, "with its write flag the command is staged for a person")
    dflt = _e(args=[A("--dry-run", kind="switch")], writes="default", dry_flags=["--dry-run"])
    check(D("cmdt_d", [], dflt).kind == "stage" and D("cmdt_d", ["--dry-run"], dflt).kind == "run",
          "a script that writes by default is staged unless its dry switch is given")
    check(D("cmdt_a", [], _e(writes="always", write_calls=["add_tags"])).kind == "stage", "a script that always writes is always staged")
    paid = _e(args=[A("--commit", kind="switch"), A("--top", type="int")], writes="commit", write_flags=["--commit"], paid=["Trestle"])
    r = D("cmdt_p", ["--top", "3"], paid)
    check(r.kind == "stage" and "it pays Trestle" in r.say and r.quote is None,
          "a paid script is staged even without a write flag, and without a vetted dry run nothing prices it")
    vet = dict(paid, free_dry_run="vetted")
    check(D("cmdt_p", ["--top", "3"], vet).kind == "run", "a vetted free dry run of a paid script runs")
    r = D("cmdt_p", ["--top", "3", "--commit"], vet)
    check(r.kind == "stage" and r.quote == ["--top", "3"], "the paid run itself is staged, priced by the same command minus its write flag")
    check(D("cmdt_p", ["--help"], paid).kind == "run", "--help never spends, even on a paid script")
    sm = _e(siftmap_reads=["get_detail"])
    r = D("cmdt_m", [], sm)
    check(r.kind == "stage" and "acceptable use" in r.say, "a script that reads SiftMap property by property is staged for a person")
    check(D("cmdt_n", ["--x"], _e(cli="none", args=[])).kind == "refuse" and D("cmdt_n", [], _e(cli="none", args=[])).kind == "run",
          "a script that reads no arguments runs only as it is")


# ------------------------------------------------------------------------------------ the tools

def _tools(check, commands, store) -> None:
    print("  the two tools")
    from . import query, team, tools
    store.init()
    roster = team.load()
    parent_id, _ = store.enqueue("sales", "ask", "", subject="ask", mode="agent", source="slack",
                                 slack_channel="CTEAM", slack_thread="1900.1")
    parent = store.task(parent_id)
    store.finish(parent_id, "done")
    ctx = tools.RunContext(task=parent, roster=roster, employee=roster.employees["sales"])
    built = tools.build(ctx)
    names = [t.name for t in built]
    i = names.index("spend_report")
    check(names[:4] == ["handoff", "ask_operator", "thread_history", "record_lookup"] and names[i + 1:i + 5] == list(query.ORDER),
          "the team tools come first and the query tools still follow spend report")
    check(names[i + 5:i + 7] == ["script_catalog", "run_script"], "script_catalog and run_script follow the query tools")
    by = {t.name: t for t in built}
    check(by["script_catalog"].read_only and not by["run_script"].read_only
          and by["run_script"].schema["additionalProperties"] is False and by["run_script"].schema["required"] == ["script"],
          "the catalog is read-only; run_script takes only script, args and details")
    for emp in roster.employees.values():
        got = [t.name for t in tools.build(tools.RunContext(task=parent, roster=roster, employee=emp))]
        check("run_script" in got and "script_catalog" in got, "%s can ask for a script" % emp.display_name)
    wf = team.Workflow(name="run_script", owner="ops", summary="s", status="live", spend="free", triggers=["slack"],
                       inputs={"script": "x", "args": "y"})
    r2 = team.Roster(team_channel=roster.team_channel, employees=roster.employees,
                     workflows=dict(roster.workflows, run_script=wf), shared_tools=roster.shared_tools)
    got = [t.name for t in tools.build(tools.RunContext(task=parent, roster=r2, employee=r2.employees["ops"]))]
    check(got.count("run_script") == 1, "the roster's run_script workflow never adds a second tool with the same name")
    os.environ["AGENTS_SCRIPT_TOOLS"] = "0"
    try:
        check(not {"run_script", "script_catalog"} & {t.name for t in tools.build(ctx)}, "AGENTS_SCRIPT_TOOLS=0 takes both away")
    finally:
        os.environ.pop("AGENTS_SCRIPT_TOOLS", None)

    out = asyncio.run(by["script_catalog"].handler({"query": "cmdt_writer"}))
    res = json.loads(_text_of(out))
    check(res["script"] == "cmdt_writer" and any(o["flag"] == "--commit" for o in res["options"]) and res["writes"] == "commit",
          "the catalog answers a script's options and how it writes")
    res = json.loads(_text_of(asyncio.run(by["script_catalog"].handler({"query": "subcommands loop"}))))
    check([m["script"] for m in res["matches"]] == ["cmdt_sub"], "the catalog searches names and summaries")
    res = json.loads(_text_of(asyncio.run(by["script_catalog"].handler({}))))
    check("cmdt_report" in {x["script"] for x in res["runs_here"]} and "cmdt_browser" not in {x["script"] for x in res["runs_here"]},
          "asked nothing, the catalog lists the scripts that run here")

    print("  asking for a script")
    from . import workflows
    n0 = len(store.thread(parent["thread_id"]))
    had = workflows.REGISTRY.pop("run_script", None)
    try:
        out = asyncio.run(by["run_script"].handler({"script": "cmdt_report", "args": []}))
        check(out.get("is_error") and "not wired into the worker" in _text_of(out)
              and len(store.thread(parent["thread_id"])) == n0,
              "with no registry line the ask is refused: the worker would read the task as a paid employee turn")
    finally:
        workflows.REGISTRY["run_script"] = had or "run_script"
    out = asyncio.run(by["run_script"].handler({"script": "cmdt_browser", "args": []}))
    check(out.get("is_error") and "On the workstation" in _text_of(out) and len(store.thread(parent["thread_id"])) == n0,
          "a refused ask is an error the employee can repeat, and nothing is queued")
    out = asyncio.run(by["run_script"].handler({"script": "cmdt_report", "args": ["--county", "Knox"], "details": "for the call"}))
    tid = ctx.tickets[-1] if ctx.tickets else 0
    t = store.task(tid) or {}
    p = store.payload_of(t)
    check(not out.get("is_error") and "Queued `python src/cmdt_report.py --county Knox` as task %d" % tid in _text_of(out)
          and t.get("workflow") == "run_script" and t.get("mode") == "direct" and t.get("agent") == "ops"
          and t.get("thread_id") == parent["thread_id"] and t.get("slack_thread") == "1900.1"
          and p == {"requested_by": "Roma", "script": "cmdt_report", "args": ["--county", "Knox"], "action": "run",
                    "details": "for the call"},
          "a runnable ask queues one direct run_script task on the same thread, under Quinn, with the exact command")
    out = asyncio.run(by["run_script"].handler({"script": "cmdt_report", "args": []}))
    check(out.get("is_error") and "One script at a time" in _text_of(out) and len(ctx.tickets) == 1,
          "while one script waits or runs, a second ask is refused: the brain has one worker")
    store.finish(tid, "done")
    said, refused, tid2 = commands.queue_ask(parent, roster.employees["sales"], roster,
                                             {"script": "python src/cmdt_writer.py --list Probate --commit"})
    t2 = store.task(tid2) or {}
    check(not refused and store.payload_of(t2).get("action") == "stage" and "stage" in said and "release batch" in said,
          "the whole command line sent as the script is split, and a write is queued to be staged, not run")
    store.finish(tid2, "done")


# ------------------------------------------------------------------------------------ the worker's half

def _ask(store, script: str, args: list, action: str = "run", thread: str = "1900.2") -> dict:
    tid, _ = store.enqueue("ops", "run_script", "", subject=script, payload={"requested_by": "Roma", "script": script,
                                                                             "args": args, "action": action},
                           mode="direct", source="agent:sales", slack_channel="CTEAM", slack_thread=thread)
    return store.task(tid)


def _worker(check, commands, config, store, root: Path) -> None:
    print("  running one command")
    from .workflows import run_script
    runner = _Runner(root)
    run_script.RUNNER = runner
    os.environ["AGENTS_SCRIPT_TIMEOUT_MIN"] = "2"
    os.environ["REISIFT_API_KEY"] = "rk-0000-cmdtest-not-real"
    os.environ["CMDTEST_TRESTLE_API_KEY"] = "tr-0000-cmdtest-not-real"
    os.environ["SLACK_CMDTEST_TOKEN"] = "xoxb-0000-cmdtest-not-real"
    os.environ["CMDTEST_API_KEY"] = "sk-0000-cmdtest-not-real-secret"
    saved_data = config.DATA_DIR
    config.DATA_DIR = root / "output" / "agents"
    try:
        runner.output = ("rows 12\nCall 865-000-0101 or write pat@example.test\nkey sk-0000-cmdtest-not-real-secret\n"
                         "done " + DASH + " wrote output/new/report.csv\n")
        runner.files = {"output/new/report.csv": "street,value\n12 Example Ln,100\n", "output/agents/brain.json": "{}",
                        "output/big.bin": b"x" * (2 * 1024 * 1024)}
        os.environ["AGENTS_SCRIPT_FILE_MB"] = "1"
        task = _ask(store, "cmdt_report", ["--county", "Knox"])
        res = run_script.run(task)
        c = runner.calls[-1]
        check(c["argv"] == [sys.executable, "src/cmdt_report.py", "--county", "Knox"] and c["cwd"] == root
              and c["timeout"] == 120.0, "the script runs as a list (no shell) from the repo root, with the time limit setting")
        env = c["env"]
        check("CMDTEST_TRESTLE_API_KEY" not in env and "SLACK_CMDTEST_TOKEN" not in env and env.get("AGENTS_MODE") == "dry"
              and env.get("SMS_AGENT_DRY_RUN") == "1" and env.get("REISIFT_API_KEY") == "rk-0000-cmdtest-not-real"
              and env.get("PYTHONUNBUFFERED") == "1",
              "a run nobody released has no vendor key and no Slack token, runs dry, and keeps the DataSift key")
        tail = res["tail"]
        check("[phone]" in tail and "[email]" in tail and "[secret]" in tail and "865-000-0101" not in tail
              and "sk-0000-cmdtest" not in tail, "the posted output hides phone numbers, emails and secret values")
        log_text = (Path(res["run_dir"]) / "files" / res["log"]).read_text(encoding="utf-8")
        check("sk-0000-cmdtest" not in log_text and "865-000-0101" in log_text,
              "the full log keeps the numbers behind its signed link but never a secret value")
        check("text" not in res and "865-000-0101" not in res["summary"] and res["summary"].startswith("python src/cmdt_report.py"),
              "the stored result carries a masked summary for the thread, never the raw log")
        check(res["files"] == ["new__report.csv"] and (Path(res["run_dir"]) / "files" / "new__report.csv").is_file()
              and not any("old" in f or "brain" in f for f in res["files"])
              and any("big.bin" in s for s in res["skipped_files"]),
              "a file the run left under output/ is copied and linked; an unchanged one, the brain's own data and a "
              "file over the size limit are not")
        txt = run_script.render(task, res)
        check(txt.startswith(":white_check_mark: `python src/cmdt_report.py --county Knox` finished") and "```" in txt
              and "/files/%d/" % task["id"] in txt and "new__report.csv" in txt and "Not copied" in txt,
              "the post says it finished, shows the output and links the log and the files")
        store.finish(task["id"], "done", result=res)

        runner.files, runner.rc = {}, 2
        task = _ask(store, "cmdt_report", [])
        res = run_script.run(task)
        check(run_script.render(task, res).startswith(":x: `python src/cmdt_report.py` exited 2"), "a failed run says its exit code")
        store.finish(task["id"], "done", result=res)
        runner.rc, runner.timed_out = 0, True
        task = _ask(store, "cmdt_report", [])
        res = run_script.run(task)
        check(":hourglass:" in run_script.render(task, res) and "time limit (2 minutes)" in run_script.render(task, res),
              "a run stopped at the limit says so")
        store.finish(task["id"], "done", result=res)
        runner.timed_out = False
        os.environ["AGENTS_SCRIPT_LOG_MB"] = "0.01"
        runner.output = "line\n" * 5000
        task = _ask(store, "cmdt_report", [])
        res = run_script.run(task)
        size = (Path(res["run_dir"]) / "files" / res["log"]).stat().st_size
        check(res["log_cut"] and size < 12000 and "bytes cut" in (Path(res["run_dir"]) / "files" / res["log"]).read_text(),
              "a runaway log is cut to its head and tail")
        os.environ.pop("AGENTS_SCRIPT_LOG_MB", None)
        store.finish(task["id"], "done", result=res)

        print("  the worker decides again")
        n = len(runner.calls)
        task = _ask(store, "cmdt_report", ["--county", "Knox; rm -rf output"])
        res = run_script.run(task)
        check(res.get("refused") and len(runner.calls) == n and run_script.render(task, res).startswith(":no_entry: Did not run"),
              "a payload that skipped the tool is checked again and refused; nothing runs")
        store.finish(task["id"], "done", result=res)
        task = _ask(store, "cmdt_writer", ["--list", "Probate", "--commit"], action="run")
        res = run_script.run(task)
        from .autopilot import ledger
        b = ledger.batch(res.get("staged") or 0) or {}
        check(res.get("staged") and len(runner.calls) == n and b.get("kind") == "command" and b.get("status") == "staged"
              and b["items"][0]["command"] == "python src/cmdt_writer.py --list Probate --commit"
              and b["items"][0]["slack_thread"] == "1900.2",
              "a write asked as a run is staged instead: one command batch holding the exact line and its thread")
        txt = run_script.render(task, res)
        check("Staged as batch %d" % b["id"] in txt and "release batch %d" % b["id"] in txt and "confirm %d" % b["id"] in txt,
              "the post names the batch and how a person releases it")
        store.finish(task["id"], "done", result=res)
        ledger.batch_set(b["id"], "cancelled", {})

        print("  a paid script priced by its own dry run")
        runner.output = "would score 4 numbers, est $0.06\n"
        task = _ask(store, "cmdt_paid", ["--top", "3", "--commit"])
        res = run_script.run(task)
        q = runner.calls[-1]
        b = ledger.batch(res.get("staged") or 0) or {}
        check(q["argv"][2:] == ["--top", "3"] and "CMDTEST_TRESTLE_API_KEY" not in q["env"] and b.get("usd") == 0.06
              and "about $0.06" in run_script.render(task, res),
              "the vetted dry run runs once without the write flag or the vendor keys, and its price rides on the batch")
        store.finish(task["id"], "done", result=res)
        ledger.batch_set(b["id"], "cancelled", {})
        check(run_script.quote("x", "est $1,204.50 for 9 numbers") == 1204.5 and run_script.quote("x", "nothing priced") is None
              and run_script.quote("obituary_owner_phones", "Rerun with --commit to score 40 numbers.") == 0.6,
              "a price is read from the dry run's own words, or counted at the unit price it was vetted with")
        n = len(runner.calls)
        task = _ask(store, "cmdt_paid", ["--top", "3"])
        res = run_script.run(task)
        check(not res.get("staged") and len(runner.calls) == n + 1 and "CMDTEST_TRESTLE_API_KEY" not in runner.calls[-1]["env"],
              "the vetted dry run asked for on its own runs straight away, keys removed")
        store.finish(task["id"], "done", result=res)
    finally:
        config.DATA_DIR = saved_data
        for k in ("AGENTS_SCRIPT_FILE_MB", "AGENTS_SCRIPT_LOG_MB"):
            os.environ.pop(k, None)


# ------------------------------------------------------------------------------------ releasing

def _release_flow(check, commands, config, store, root: Path) -> None:
    print("  a person releases a staged command")
    from . import release, slack_app, worker
    from .autopilot import ledger
    from .workflows import run_script
    runner = run_script.RUNNER
    fake = _Slack()
    door, token, who = slack_app._http, config.SLACK_BOT_TOKEN, config.RELEASERS
    slack_app._http, config.SLACK_BOT_TOKEN, config.RELEASERS = fake, "xoxb-test", "UBOSS"
    t0 = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)
    try:
        check(release.release_fn("command") is run_script.release, "the release table sends a command batch to run_script")
        runner.output, runner.files = "tagged 3 records\n", {}
        n = len(runner.calls)
        stage_task = _ask(store, "cmdt_writer", ["--list", "Probate", "--commit"], action="stage", thread="1900.5")
        worker.run_task(stage_task)
        res = store.result_of(store.task(stage_task["id"]))
        bid = int(res.get("staged") or 0)
        check(bid and len(runner.calls) == n and "Staged as batch %d" % bid in fake.posts[-1]["text"]
              and fake.posts[-1]["thread_ts"] == "1900.5" and fake.posts[-1]["username"] == "Quinn",
              "the worker stages the command, runs nothing, and Quinn posts the batch in the asking thread")
        check(run_script.release(bid, commit=False).get("dry") and ledger.batch(bid)["status"] == "staged",
              "a release without commit only says what would run")
        config.MODE = "dry"
        r = run_script.release(bid, commit=True)
        check(ledger.batch(bid)["status"] == "staged" and not store.open_task("run_script") and "dry mode" in json.dumps(r),
              "on a dry brain a release queues nothing and the batch stays staged")
        config.MODE = "live"
        release.slack_command(("release", bid, False), "UBOSS", "CREL", "1950.1", now=t0)
        r = release.slack_command(("confirm", bid, False), "UBOSS", "CREL", "1950.1", now=t0 + timedelta(minutes=1))
        rel_task = store.task(r.get("task") or 0) or {}
        worker.run_task(rel_task)
        b = ledger.batch(bid)
        run_tid = int((b.get("result") or {}).get("task") or 0)
        run_task = store.task(run_tid) or {}
        check(b["status"] == "released" and run_task.get("workflow") == "run_script" and run_task.get("slack_thread") == "1900.5"
              and store.payload_of(run_task) == {"action": "release_run", "batch": bid}
              and "queued as task %d" % run_tid in fake.posts[-1]["text"] and len(runner.calls) == n,
              "a confirm on a live brain queues the command as its own task in the asking thread; the release itself runs nothing")
        worker.run_task(run_task)
        c = runner.calls[-1]
        b = ledger.batch(bid)
        check(len(runner.calls) == n + 1 and c["argv"][2:] == ["--list", "Probate", "--commit"]
              and c["env"].get("CMDTEST_TRESTLE_API_KEY") == "tr-0000-cmdtest-not-real" and b["status"] == "done"
              and fake.posts[-1]["thread_ts"] == "1900.5" and "(released batch %d)" % bid in fake.posts[-1]["text"],
              "the released command runs once with the box's own environment, marks the batch done and posts in the thread")
        store.mark(run_tid, "queued")
        worker.run_task(store.task(run_tid))
        check(len(runner.calls) == n + 1 and "runs once" in fake.posts[-1]["text"],
              "run again (a restart mid-run retries a task), a released command does not run twice")

        bid2 = ledger.batch_create("run_script", "command", [{"script": "cmdt_writer", "args": ["--list", "X", "--commit"],
                                                              "command": "python src/cmdt_writer.py --list X --commit"}], 0)
        forged = _ask(store, "x", [], action="release_run")
        store.finish(forged["id"], "done")
        forged = dict(forged, payload=json.dumps({"action": "release_run", "batch": bid2}))
        check(run_script.run(forged).get("refused") and len(runner.calls) == n + 1,
              "a release_run task the release did not queue cannot run a staged batch")
        r = run_script.release(bid2, commit=True)
        tid3 = int(r.get("task") or 0)
        config.MODE = "dry"
        out = run_script.run(store.task(tid3))
        check(out.get("refused") and ledger.batch(bid2)["status"] == "staged" and len(runner.calls) == n + 1,
              "put back in dry mode between the release and the run, nothing runs and the batch is staged again")
        store.finish(tid3, "done")
        config.MODE = "live"
        commands.RETIRED["cmdt_writer"] = ("retired by the selftest after staging", "")
        commands.reset()
        r = run_script.release(bid2, commit=True)
        check(ledger.batch(bid2)["status"] == "refused" and "retired" in json.dumps(r),
              "a script retired after it was staged is refused at release")
    finally:
        commands.RETIRED.pop("cmdt_writer", None)
        commands.reset()
        slack_app._http, config.SLACK_BOT_TOKEN, config.RELEASERS = door, token, who
        config.MODE = "dry"


# ------------------------------------------------------------------------------------ the real runner, faked at Popen

class _Popen:
    made: list = []
    hang = False

    def __init__(self, argv, stdout=None, **kw):
        self.argv, self.kw, self.pid, self.waits, self.killed = argv, kw, 4242, 0, False
        _Popen.made.append(self)
        stdout.write(b"hello from the fake\n")

    def wait(self, timeout=None):
        self.waits += 1
        if _Popen.hang and self.waits == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return 0

    def kill(self):
        self.killed = True


def _popen(check, root: Path) -> None:
    print("  the process, started without a shell")
    from .workflows import run_script
    real_popen = run_script.subprocess.Popen
    real_killpg = getattr(run_script.os, "killpg", None)
    killed = []
    run_script.subprocess.Popen = _Popen
    if real_killpg is not None:
        run_script.os.killpg = lambda pid, sig: killed.append((pid, sig))
    try:
        log = root / "popen-log.txt"
        out = run_script._run_process(["python", "src/x.py"], root, {"A": "1"}, 30.0, log)
        p = _Popen.made[-1]
        check(out["rc"] == 0 and not out["timed_out"] and p.kw.get("shell") is False and p.kw.get("stdin") == subprocess.DEVNULL
              and p.kw.get("stderr") == subprocess.STDOUT and p.kw.get("cwd") == str(root) and p.argv == ["python", "src/x.py"]
              and log.read_bytes() == b"hello from the fake\n",
              "the process gets the argument list, no shell, no stdin, and one log for both streams")
        check(p.kw.get("start_new_session") is True if os.name == "posix" else "creationflags" in p.kw,
              "the process starts in its own group, so a timeout can stop everything it started")
        _Popen.hang = True
        out = run_script._run_process(["python", "src/x.py"], root, {}, 5.0, log)
        p = _Popen.made[-1]
        check(out["timed_out"] and (killed if os.name == "posix" else p.killed),
              "at the time limit the whole group is stopped")
    finally:
        _Popen.hang = False
        run_script.subprocess.Popen = real_popen
        if real_killpg is not None:
            run_script.os.killpg = real_killpg


def _hygiene(check) -> None:
    here = Path(__file__).resolve().parent
    files = [here / "commands.py", here / "selftest_cmd.py", here / "tools.py", here / "release.py",
             here / "workflows" / "run_script.py", here.parents[1] / "team" / "commands.json"]
    for f in files:
        text = f.read_text(encoding="utf-8")
        check(DASH not in text and NDASH not in text and "\x08" not in text,
              "%s has no em dash, no en dash and no backspace byte" % f.name)
