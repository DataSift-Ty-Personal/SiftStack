"""The cold email cutover (deploy/cold_email_cutover.py, 2026-11-20): the jobs move from the PC to the agents
brain in one step and back again. Zero network and no real command: a fake shell answers for schtasks and
fly, and plays the box from a temporary folder (its remote scripts run against that folder). Every name and
address here is invented."""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "cold_email"))
_spec = importlib.util.spec_from_file_location("cold_email_cutover", ROOT / "deploy" / "cold_email_cutover.py")
cut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut)
import coldstate  # noqa: E402

APP = "siftstack-agents"


class FakeShell:
    def __init__(self, box: Path, secrets=("INSTANTLY_API_KEY", "ZEROBOUNCE_API_KEY", "SMS_AGENT_SLACK_WEBHOOK"),
                 live="ftm_texts,research_queue", code=True, running=(), fail_put=None, fail_disable=False):
        self.box, self.secrets, self.live, self.code = box, list(secrets), live, code
        self.running, self.fail_put, self.fail_disable = set(running), fail_put, fail_disable
        self.cmds: list[list] = []

    def run(self, args, timeout=300):
        args = [str(a) for a in args]
        self.cmds.append(args)
        if args[0] == "schtasks":
            if "/Query" in args:
                name = args[args.index("/TN") + 1]
                return 0, '"\\%s","N/A","%s"' % (name, "Running" if name in self.running else "Ready")
            if "/DISABLE" in args and self.fail_disable:
                return 1, "ERROR: access denied"
            return 0, "SUCCESS"
        if args[:3] == ["fly", "secrets", "list"]:
            return 0, json.dumps([{"Name": n, "Digest": "x"} for n in self.secrets])
        if args[:3] == ["fly", "secrets", "set"]:
            return 0, "Secrets are staged"
        if args[:4] == ["fly", "ssh", "sftp", "put"]:
            local, remote = Path(args[4]), Path(args[5])
            if self.fail_put and local.name == self.fail_put:
                return 1, "Error: connection reset"
            remote.parent.mkdir(parents=True, exist_ok=True)
            if remote.exists():
                return 1, "Error: file exists"
            shutil.copyfile(local, remote)
            return 0, "%d bytes written" % remote.stat().st_size
        if args[:4] == ["fly", "ssh", "sftp", "get"]:
            remote, local = Path(args[4]), Path(args[5])
            shutil.copyfile(remote, local)
            return 0, "ok"
        if args[:3] == ["fly", "ssh", "console"]:
            code = base64.b64decode(re.search(r"b64decode\('([^']+)'\)", args[-1]).group(1)).decode("utf-8")
            if '"code": os.path.exists' in code:          # the box read: answer as the deployed box would
                home = {}
                if (self.box / "home.json").exists():
                    home = json.loads((self.box / "home.json").read_text(encoding="utf-8"))
                hb = {}
                if (self.box / "replies_heartbeat.json").exists():
                    hb = json.loads((self.box / "replies_heartbeat.json").read_text(encoding="utf-8"))
                files = sorted(p.name for p in self.box.glob("*") if p.suffix in (".json", ".jsonl")) if self.box.is_dir() else []
                return 0, "Connecting...\n" + json.dumps({"code": self.code, "home": home.get("home", ""), "files": files,
                                                          "heartbeat": hb, "app": APP, "live_stages": self.live,
                                                          "data_dir": "/data"})
            import contextlib
            import io
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    exec(code, {})
            except SystemExit as exc:
                return 1, str(exc)
            return 0, buf.getvalue()
        raise AssertionError("unexpected command %r" % args)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("COLD_EMAIL_INSTALL", "workstation")   # this PC, whatever machine runs the test
    monkeypatch.delenv("COLD_EMAIL_STATE_DIR", raising=False)
    pc, box = tmp_path / "pc", tmp_path / "box"
    pc.mkdir()
    monkeypatch.setattr(cut, "REMOTE", str(box).replace("\\", "/"))
    (pc / "ledger.json").write_text(json.dumps({"owners": {"o1": {"record": "r1"}}, "emails": {"pat@example.com": "o1"}}),
                                    encoding="utf-8")
    (pc / "replies_state.json").write_text(json.dumps({"seen": ["e1"], "since": "2026-11-19T10:00:00.000Z"}), encoding="utf-8")
    (pc / "rejected.json").write_text(json.dumps({"r9": "2026-11-18"}), encoding="utf-8")
    (pc / "replies.jsonl").write_text(json.dumps({"kind": "stop", "at": "2026-11-19T10:00:00+00:00"}) + "\n", encoding="utf-8")
    return pc, box


def make(pc, box, **kw):
    shell = FakeShell(box, **kw)
    said: list[str] = []
    return cut.Cutover(pc, shell=shell, app=APP, echo=said.append, sleep=lambda s: None, wait_s=0), shell, said


def index_of(cmds, pred) -> list[int]:
    return [i for i, c in enumerate(cmds) if pred(c)]


def test_the_cutover_moves_the_state_and_names_the_box_the_home(rig):
    pc, box = rig
    c, shell, said = make(pc, box)
    assert c.run() == 0
    for name in ("ledger.json", "replies_state.json", "rejected.json", "replies.jsonl"):
        assert (box / name).read_bytes() == (pc / name).read_bytes(), name
    assert not list((box / "incoming").iterdir()), "the incoming folder is emptied into place"
    assert json.loads((box / "home.json").read_text(encoding="utf-8"))["home"] == APP
    assert coldstate.home_name(pc) == APP and not coldstate.owns(pc)[0], "a run by hand on the PC now refuses"
    disables = index_of(shell.cmds, lambda c: "/DISABLE" in c)
    puts = index_of(shell.cmds, lambda c: c[:4] == ["fly", "ssh", "sftp", "put"])
    assert len(disables) == 4 and len(puts) == 4 and max(disables) < min(puts), "the PC stops before the state is copied"
    assert any("fly secrets set AGENTS_LIVE_STAGES=ftm_texts,research_queue,cold_email_topup -a siftstack-agents" in s
               for s in said), "the top-up's switch is printed with the stages already there kept"
    assert not any(c[:3] == ["fly", "secrets", "set"] for c in shell.cmds), "without --set-live nothing restarts the box"


def test_set_live_adds_the_top_up_to_the_live_stages(rig):
    pc, box = rig
    c, shell, said = make(pc, box)
    assert c.run(set_live=True) == 0
    sets = [x for x in shell.cmds if x[:3] == ["fly", "secrets", "set"]]
    assert sets == [["fly", "secrets", "set", "AGENTS_LIVE_STAGES=ftm_texts,research_queue,cold_email_topup", "-a", APP]]


def test_a_failed_copy_puts_the_pc_back(rig):
    pc, box = rig
    c, shell, said = make(pc, box, fail_put="rejected.json")
    assert c.run() == 1
    assert coldstate.home_name(pc) == "workstation" and coldstate.owns(pc)[0], "the PC is the home again"
    assert len(index_of(shell.cmds, lambda c: "/ENABLE" in c)) == 4, "and its four tasks are on again"
    assert not (box / "home.json").exists(), "the box was never named the home, so nothing acts there"


def test_a_task_that_will_not_disable_puts_the_pc_back(rig):
    pc, box = rig
    c, shell, said = make(pc, box, fail_disable=True)
    assert c.run() == 1
    assert coldstate.owns(pc)[0] and len(index_of(shell.cmds, lambda c: "/ENABLE" in c)) == 4
    assert not any(x[:4] == ["fly", "ssh", "sftp", "put"] for x in shell.cmds), "nothing was copied"


@pytest.mark.parametrize("why, kw, setup", [
    ("a secret is missing", {"secrets": ("INSTANTLY_API_KEY", "SMS_AGENT_SLACK_WEBHOOK")}, None),
    ("no hand raise channel", {"secrets": ("INSTANTLY_API_KEY", "ZEROBOUNCE_API_KEY")}, None),
    ("the box has old code", {"code": False}, None),
    ("the top-up runs on the PC", {"running": ("SiftStack Cold Email Top-up",)}, None),
    ("the box is already the home", {}, "box_home"),
    ("the box holds state", {}, "box_state"),
    ("the PC is not the home", {}, "pc_moved"),
])
def test_the_preflight_refuses_and_changes_nothing(rig, why, kw, setup):
    pc, box = rig
    if setup == "box_home":
        box.mkdir()
        (box / "home.json").write_text(json.dumps({"home": APP}), encoding="utf-8")
    if setup == "box_state":
        box.mkdir()
        (box / "ledger.json").write_text("{}", encoding="utf-8")
    if setup == "pc_moved":
        coldstate.claim(APP, pc)
    c, shell, said = make(pc, box, **kw)
    with pytest.raises(cut.Refused):
        c.run()
    assert not any("/DISABLE" in x for x in shell.cmds), why
    assert not any(x[:4] == ["fly", "ssh", "sftp", "put"] for x in shell.cmds), why


def test_a_pc_checkout_without_the_home_rule_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("COLD_EMAIL_INSTALL", "workstation")
    repo = tmp_path / "SiftStack"
    pc, box = repo / "output" / "cold_email", tmp_path / "box"
    pc.mkdir(parents=True)
    (repo / "src" / "cold_email").mkdir(parents=True)                 # old code: no coldstate.py there
    monkeypatch.setattr(cut, "REMOTE", str(box).replace("\\", "/"))
    for name in ("ledger.json", "replies_state.json"):
        (pc / name).write_text("{}", encoding="utf-8")
    c, shell, said = make(pc, box)
    with pytest.raises(cut.Refused, match="without the home rule"):
        c.run()
    assert not any("/DISABLE" in x for x in shell.cmds)
    (repo / "src" / "cold_email" / "coldstate.py").write_text("# the home rule\n", encoding="utf-8")
    c2, shell2, _ = make(pc, box)
    assert c2.run() == 0, "with the home rule in the PC's checkout the cutover goes ahead"


def test_a_cutover_after_a_back_may_replace_the_box_state(rig):
    pc, box = rig
    box.mkdir()
    (box / "ledger.json").write_text("{}", encoding="utf-8")
    c, shell, said = make(pc, box)
    assert c.run(replace_box_state=True) == 0
    assert (box / "ledger.json").read_bytes() == (pc / "ledger.json").read_bytes()


def test_back_stops_the_box_first_and_brings_the_newer_state_home(rig):
    pc, box = rig
    c, shell, said = make(pc, box)
    assert c.run() == 0
    newer = {"owners": {"o1": {"record": "r1"}, "o2": {"record": "r2"}}, "emails": {}}
    (box / "ledger.json").write_text(json.dumps(newer), encoding="utf-8")
    shell.cmds.clear()
    assert c.back() == 0
    assert json.loads((box / "home.json").read_text(encoding="utf-8"))["home"] == "workstation"
    assert json.loads((pc / "ledger.json").read_text(encoding="utf-8")) == newer
    assert coldstate.owns(pc)[0] and len(index_of(shell.cmds, lambda c: "/ENABLE" in c)) == 4
    claim = index_of(shell.cmds, lambda c: c[:3] == ["fly", "ssh", "console"])
    gets = index_of(shell.cmds, lambda c: c[:4] == ["fly", "ssh", "sftp", "get"])
    assert claim and gets and min(claim) < min(gets), "the box stops before the state comes back"
    assert list(pc.glob("before_back_*/ledger.json")), "the PC's old copy is kept"


def test_the_dry_plan_runs_nothing(rig, capsys):
    pc, box = rig
    assert cut.main(["--state-dir", str(pc)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and out.index("/DISABLE") < out.index("sftp put") < out.index("AGENTS_LIVE_STAGES=")
    assert not (pc / "home.json").exists() and not box.exists()
    assert "\u2014" not in out and "\u2013" not in out


def test_the_live_list_is_merged_not_replaced():
    assert cut.merged_live("") == "cold_email_topup"
    assert cut.merged_live("a, b") == "a,b,cold_email_topup"
    assert cut.merged_live("a,cold_email_topup") == "a,cold_email_topup"
    assert cut.without_live("cold_email_topup") == ""
