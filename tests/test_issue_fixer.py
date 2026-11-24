"""The issue fixer (tools/issue_fixer/fixer.py): folding the issues channel into a ledger, which box a
change reaches, the gates, the deploy window, approvals, and one whole run with every outside call
replaced by a fake. Zero network; invented streets only.

Run: python -m pytest tests/test_issue_fixer.py
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "issue_fixer"))

import fixer as F  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    # The fixer's own box sets ISSUE_FIXER=off, ISSUE_FIXER_OWN_CLONE=1 and the Slack keys in its
    # environment; seven of these tests failed there until the host's settings were cleared first.
    for k in list(os.environ):
        if k.startswith(("ISSUE_FIXER", "SLACK_", "AGENTS_SLACK", "SIFTSTACK_MAIN")):
            monkeypatch.delenv(k, raising=False)
    main = tmp_path / "main"
    (main / "output").mkdir(parents=True)
    (main / ".env").write_text("SLACK_BOT_TOKEN=x\nAGENTS_SLACK_ISSUES_CHANNEL=C1\n", encoding="utf-8")
    monkeypatch.setattr(F, "MAIN", main)
    monkeypatch.setattr(F, "STATE", main / "output" / "issue_fixer")
    monkeypatch.setattr(F, "WORKTREE", tmp_path / "wt")
    monkeypatch.setattr(F, "DEPLOY_TREE", tmp_path / "deploy")
    (tmp_path / "wt" / ".git").mkdir(parents=True)
    (tmp_path / "deploy" / ".git").mkdir(parents=True)
    monkeypatch.setattr(F, "now_utc", lambda: datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc))   # 10:00 ET
    return tmp_path


def post(ts, text, **kw):
    return dict({"ts": str(ts), "text": text, "username": "Quinn"}, **kw)


# ---------------------------------------------------------------------- shapes and the ledger
def test_a_daily_repeat_is_one_issue():
    a = "Call audit: 8: dialed an owner the gate holds: the house sold (for example 101 Example St, Knoxville)"
    b = "Call audit: 3: dialed an owner the gate holds: the house sold (for example 9 Sample Ave, Maryville)"
    c = "Call audit: 2: dialed an owner the gate holds: tagged Do Not Market (for example 1 Test Way, Alcoa)"
    assert F.shape_key(a) == F.shape_key(b)
    assert F.shape_key(a) != F.shape_key(c)
    d = "Check: the New Lead text follow-up.\nAll 11 texts it sent this week failed.\nExamples: Huron St and Croydon Rd."
    e = "Check: the New Lead text follow-up.\nAll 4 texts it sent this week failed.\nExamples: Elm St in Alcoa."
    assert F.shape_key(d) == F.shape_key(e)
    assert F.shape_key("Lead check: 1 new lead in the last 7 days has no outbound call") == \
        F.shape_key("Lead check: 11 new leads in the last 7 days have no outbound call")


def test_only_real_issue_posts_count():
    assert F.is_issue_post(post(1, "Lead check: 3 leads"))
    assert F.is_issue_post({"ts": "2", "text": "the county tax lookup looks broken", "user": "U1"})      # a person can report one
    assert not F.is_issue_post(post(3, "Shipped", username=F.USERNAME))
    assert not F.is_issue_post(post(4, "a reply", thread_ts="1"))
    assert not F.is_issue_post({"ts": "5", "text": "x joined", "subtype": "channel_join"})
    assert not F.is_issue_post({"ts": "6", "text": "broadcast", "subtype": "thread_broadcast", "thread_ts": "1"})


def test_fold_new_repeat_and_regression():
    led = F.empty_ledger()
    need = F.fold(led, [post(100, "Incomplete records: could not read an incomplete record")])
    assert len(need) == 1
    iid = need[0]
    assert F.fold(led, [post(200, "Incomplete records: could not read an incomplete record")]) == []
    assert led["issues"][iid]["seen"] == 2 and led["last_ts"] == "200"
    led["issues"][iid].update(status="fixed", fixed_at=datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc).isoformat())
    early = datetime(2026, 11, 18, 12, 10, tzinfo=timezone.utc).timestamp()
    assert F.fold(led, [post(early, "Incomplete records: could not read an incomplete record")]) == []
    late = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc).timestamp()
    assert F.fold(led, [post(late, "Incomplete records: could not read an incomplete record")]) == [iid]
    assert led["issues"][iid]["status"] == "regression"
    led["issues"][iid]["status"] = "fixed"
    F.fold(led, [post(late + 86400, "Incomplete records: could not read an incomplete record")])
    assert led["issues"][iid]["status"] == "needs_person", "a fix that fails twice goes to a person"


def test_seeding_marks_and_sorts_what_is_already_there():
    led = F.empty_ledger()
    F.fold(led, [post(1, "Pacing: the Top 1,000 stages are switched on"), post(2, "Something nobody has sorted")],
           seen_only=True)
    assert all(i["status"] == "seen" for i in led["issues"].values())
    verdicts = json.loads((ROOT / "tools" / "issue_fixer" / "seed_verdicts.json").read_text(encoding="utf-8"))
    assert F.apply_seed_verdicts(led, verdicts) == 1
    sorted_ = [i for i in led["issues"].values() if i["class"]]
    assert sorted_[0]["class"] == "by_design" and sorted_[0]["status"] == "closed"


def test_seed_verdicts_fixed_rows_carry_a_time():
    led = F.empty_ledger()
    F.fold(led, [post(1, "Incomplete records: could not read an incomplete record")], seen_only=True)
    F.apply_seed_verdicts(led, [{"match": "could not read an incomplete", "class": "bug", "status": "fixed"}])
    assert next(iter(led["issues"].values())).get("fixed_at")


# ---------------------------------------------------------------------- which box a change reaches
def test_apps_for_follows_imports_on_the_real_tree():
    assert "siftstack-agents" in F.apps_for(ROOT, ["src/agents/autopilot/limits.py"])
    assert "siftstack-ftm" in F.apps_for(ROOT, ["src/scraper.py"])
    assert "siftstack" in F.apps_for(ROOT, ["src/sms_agent/respond.py"])
    assert F.apps_for(ROOT, ["team/autopilot.yaml"]) == ["siftstack-agents"]
    assert F.apps_for(ROOT, ["tests/test_daily_window.py", "CLAUDE.md", "tools/issue_fixer/fixer.py"]) == []
    assert F.apps_for(ROOT, ["src/agents/selftest_complete.py"]) == [], "a selftest is not running code"
    assert "siftstack-ftm" in F.apps_for(ROOT, ["fly.ftm.toml"])


# ---------------------------------------------------------------------- the session's answer
def test_result_is_checked_against_what_was_given(tmp_path):
    p = tmp_path / "RESULT.json"
    p.write_text(json.dumps({"issues": [{"id": "a1", "class": "finding"}, {"id": "zz", "class": "bug"},
                                        {"id": "b2", "class": "nonsense"}]}), encoding="utf-8")
    out, problems = F.load_result(p, ["a1", "b2", "c3"])
    assert set(out["issues"]) == {"a1", "b2"} and out["issues"]["b2"]["class"] == "decision"
    assert any("zz" in x for x in problems) and any("c3" in x for x in problems)
    out, problems = F.load_result(tmp_path / "missing.json", ["a1"])
    assert out == {} and problems


def test_dashes_in_added_lines_are_found():
    diff = "+++ b/x.py\n+ok line\n+bad %s line\n- old %s line\n" % (chr(0x2014), chr(0x2013))
    assert len(F.dash_lines(diff)) == 1


def test_plain_text_for_slack():
    assert F.plain("a %s b %s c :fire: done" % (chr(0x2014), chr(0x2013))) == "a ,  b - c  done"


# ---------------------------------------------------------------------- a fake world
class FakeShell:
    """git, fly, Claude Code and the test suites, answered from a script of rules."""

    def __init__(self, **kw):
        self.calls = []
        self.kw = dict(selftest="5204 passed, 0 failed", pytest_rc=0, push_rc=0, deploy_rc=0, running=0, person=0,
                       doctor="stages beyond today's run: none\n  55 of 55 passed\n  all passed", result=None,
                       changed="src/scraper.py", image="registry.fly.io/siftstack-ftm:deployment-OLD")
        self.kw.update(kw)

    def run(self, cmd, cwd=None, timeout=1800, env=None, input_text=None):
        c = [str(x) for x in cmd]
        line = " ".join(c)
        self.calls.append(line)
        k = self.kw
        if os.path.basename(c[0]).lower() in ("git", "git.exe"):
            if "rev-parse" in c:
                return 0, "abc1234def\n"
            if "diff" in c and "--name-only" in c:
                return 0, k["changed"] + "\n"
            if "diff" in c:
                return 0, "+++ b/src/scraper.py\n+a plain line\n"
            if "push" in c:
                return k["push_rc"], "" if not k["push_rc"] else "rejected"
            return 0, ""
        if "claude" in c[0]:
            m = re.search(r"Read (\S+PROMPT\.md)", line)
            prompt = Path(m.group(1)).read_text(encoding="utf-8")
            res = re.search(r"Result file: (\S+)", prompt).group(1)
            Path(res).write_text(json.dumps(k["result"]), encoding="utf-8")
            return 0, json.dumps({"type": "result", "total_cost_usd": 3.5, "is_error": False, "permission_denials": []})
        if "sms_agent/cli.py" in line and "selftest" in line:
            return 0, "all 642 checks passed"
        if "selftest" in line and "cli.py" in line and "agents" in line:
            return 0, k["selftest"]
        if "pytest" in line:
            return k["pytest_rc"], "1125 passed" if not k["pytest_rc"] else "1 failed"
        if " ssh console " in " %s " % line:
            if "RUNNING" in line:
                return 0, "RUNNING %d" % k["running"]
            if "PERSON" in line:
                return 0, "PERSON %d" % k["person"]
            if "--doctor" in line:
                return 0, k["doctor"]
            return 0, ""
        if " status " in " %s " % line and "--json" in c:
            return 0, json.dumps({"Machines": [{"config": {"image": k["image"]}}]})
        if " releases " in " %s " % line:
            return 0, json.dumps([{"Version": 34}])
        if " deploy " in " %s " % line:
            return k["deploy_rc"], "deployed" if not k["deploy_rc"] else "Error: build failed"
        return 0, ""


class FakeSlack:
    def __init__(self, history=None, replies=None):
        self.history_rows, self.replies_rows, self.posts = history or [], replies or {}, []

    def history(self, channel, oldest):
        return [m for m in self.history_rows if not oldest or float(m["ts"]) > float(oldest)]

    def replies(self, channel, ts):
        return self.replies_rows.get(ts, [])

    def post(self, channel, text, thread_ts="", broadcast=False):
        self.posts.append({"text": text, "thread_ts": thread_ts, "broadcast": broadcast})
        return "999.1"


class Args:
    dry = False
    no_ship = False


def settings(**env):
    base = {"SLACK_BOT_TOKEN": "x", "AGENTS_SLACK_ISSUES_CHANNEL": "C1", "ISSUE_FIXER_APPROVERS": "UTY",
            "ISSUE_FIXER_MODEL": "claude-opus-5-5"}
    base.update(env)
    return F.Settings(base)


def seeded(slack, st):
    class A:
        hours = 72
    F.seed(A(), st=st, slack=slack)


# ---------------------------------------------------------------------- gates and windows
def test_gates_need_both_suites_and_the_texting_one_for_its_files():
    ok, lines = F.gates(FakeShell(), ["src/scraper.py"])
    assert ok and lines[0].startswith("agents selftest: 5204 passed, 0 failed")
    ok, _ = F.gates(FakeShell(selftest="5203 passed, 1 failed"), ["src/scraper.py"])
    assert not ok
    ok, _ = F.gates(FakeShell(pytest_rc=1), ["src/scraper.py"])
    assert not ok
    sh = FakeShell()
    F.gates(sh, ["src/sms_agent/respond.py"])
    assert any("sms_agent/cli.py selftest" in c for c in sh.calls)


def test_deploy_windows():
    assert not F.window(FakeShell(), "siftstack-ftm", now=datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc))[0]   # 7:00 ET
    assert F.window(FakeShell(), "siftstack-ftm", now=datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc))[0]       # 10:00 ET
    assert not F.window(FakeShell(running=1), "siftstack-ftm", now=datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc))[0]
    assert not F.window(FakeShell(person=1), "siftstack-agents")[0]
    assert F.window(FakeShell(), "siftstack-agents")[0]


# ---------------------------------------------------------------------- whole runs
def test_a_run_sorts_fixes_tests_and_ships(monkeypatch):
    old = post(datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc).timestamp(), "Pacing: the Top 1,000 stages are switched on")
    slack = FakeSlack([old])
    st = settings()
    seeded(slack, st)
    new_bug = post(datetime(2026, 11, 18, 15, 5, tzinfo=timezone.utc).timestamp(),
                   "First-to-market pull: Feed Blount/foreclosure has produced nothing for 9 days", username="SiftStack Team")
    finding = post(datetime(2026, 11, 18, 14, 5, tzinfo=timezone.utc).timestamp(), "Lead check: 4 leads with no open task")
    slack.history_rows += [new_bug, finding]
    bug_id, find_id = F.shape_key(new_bug["text"]), F.shape_key(finding["text"])
    result = {"issues": [
        {"id": bug_id, "class": "bug", "summary": "The window was wrong", "reply": "The feed missed new notices.",
         "fixed": True, "commits": ["abc1234"], "evidence": "53 in the window, 5 in the default search"},
        {"id": find_id, "class": "finding", "summary": "Four leads have no task", "reply": "Four leads have no task. Emerson should add one."}]}
    sh = FakeShell(result=result)
    monkeypatch.setattr(F, "apps_for", lambda repo, changed: ["siftstack-ftm"])
    assert F.run(Args(), st=st, sh=sh, slack=slack) == 0
    led = F.load_ledger()
    assert led["issues"][bug_id]["status"] == "fixed" and led["issues"][bug_id]["fix"]["commit"] == "abc1234def"
    assert led["issues"][find_id]["status"] == "for_people"
    assert any("deploy -c fly.ftm.toml --remote-only" in c for c in sh.calls), "the box the change reaches is deployed"
    assert any("push origin HEAD:agents-infra" in c for c in sh.calls)
    texts = [p["text"] for p in slack.posts]
    assert any(t.startswith("Shipped (abc1234)") and "first-to-market box v34" in t for t in texts)
    assert any("Emerson should add one" in t for t in texts)
    assert all(p["thread_ts"] for p in slack.posts), "every answer goes in the issue's own thread"
    assert F.spent_today("2026-11-18") == 3.5


def test_a_failed_test_ships_nothing(monkeypatch):
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    bug = post(datetime(2026, 11, 18, 15, 5, tzinfo=timezone.utc).timestamp(), "Research queue: it stopped partway")
    slack.history_rows.append(bug)
    bid = F.shape_key(bug["text"])
    sh = FakeShell(selftest="5203 passed, 1 failed", result={"issues": [
        {"id": bid, "class": "bug", "summary": "a crash", "reply": "It crashed.", "fixed": True, "commits": ["abc1234"]}]})
    monkeypatch.setattr(F, "apps_for", lambda repo, changed: ["siftstack-agents"])
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert F.load_ledger()["issues"][bid]["status"] == "needs_person"
    assert not any(" deploy " in " %s " % c for c in sh.calls) and not any("HEAD:agents-infra" in c for c in sh.calls)
    assert any("did not ship" in p["text"] and p["broadcast"] for p in slack.posts)


def test_a_box_that_fails_its_checks_goes_back(monkeypatch):
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    bug = post(datetime(2026, 11, 18, 15, 5, tzinfo=timezone.utc).timestamp(), "First-to-market pull: Feed Knox/foreclosure is stale")
    slack.history_rows.append(bug)
    bid = F.shape_key(bug["text"])
    sh = FakeShell(doctor="stages beyond today's run: tax_sale", result={"issues": [
        {"id": bid, "class": "bug", "summary": "x", "reply": "x", "fixed": True, "commits": ["abc1234"]}]})
    monkeypatch.setattr(F, "apps_for", lambda repo, changed: ["siftstack-ftm"])
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert any("--image registry.fly.io/siftstack-ftm:deployment-OLD" in c for c in sh.calls), "it rolls back to the old image"
    assert F.load_ledger()["issues"][bid]["status"] == "needs_person"


def test_the_texting_app_waits_for_an_ok_and_then_ships(monkeypatch):
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    bug = post(datetime(2026, 11, 18, 15, 5, tzinfo=timezone.utc).timestamp(), "Texting: a reply was blocked for no reason")
    slack.history_rows.append(bug)
    bid = F.shape_key(bug["text"])
    sh = FakeShell(changed="src/sms_agent/respond.py", result={"issues": [
        {"id": bid, "class": "bug", "summary": "x", "reply": "A reply was blocked.", "fixed": True, "commits": ["abc1234"]}]})
    monkeypatch.setattr(F, "apps_for", lambda repo, changed: ["siftstack"])
    monkeypatch.setattr(F, "checks", lambda sh, app: (True, ["health ok"]))
    F.run(Args(), st=st, sh=sh, slack=slack)
    iss = F.load_ledger()["issues"][bid]
    assert iss["status"] == "awaiting_ok" and not any("deploy -c fly.toml" in c for c in sh.calls)
    slack.replies_rows[bug["ts"]] = [{"ts": str(float(iss["asked_ts"]) + 5), "user": "USTRANGER", "text": "ok"}]
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert not any("deploy -c fly.toml" in c for c in sh.calls), "only an approver's ok counts"
    slack.replies_rows[bug["ts"]].append({"ts": str(float(iss["asked_ts"]) + 9), "user": "UTY", "text": "ok ship it"})
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert any("deploy -c fly.toml --remote-only" in c for c in sh.calls)
    assert F.load_ledger()["issues"][bid]["status"] == "fixed"


class Clock:
    """A clock a test moves: the runs of one morning, a quarter hour apart."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def morning_runs(clock, st, sh, slack, first, last):
    """F.run at every quarter hour from `first` to `last` (UTC), both included."""
    clock.t = first
    while clock.t <= last:
        F.run(Args(), st=st, sh=sh, slack=slack)
        clock.t += timedelta(minutes=15)


def test_a_deploy_that_waits_for_its_window_is_said_once_and_shipped_once(monkeypatch):
    # 2026-11-20: a fix found at 6:45 waited for the first-to-market box's 6:00 to 9:15 window, and every run
    # until 9:00 broadcast "Shipped (...). the first-to-market box waits" again: ten top-level posts.
    clock = Clock(datetime(2026, 11, 18, 11, 30, tzinfo=timezone.utc))             # 6:30 business time
    monkeypatch.setattr(F, "now_utc", clock)
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    a = post(datetime(2026, 11, 18, 11, 40, tzinfo=timezone.utc).timestamp(), "First-to-market run: liens stage failed")
    b = post(datetime(2026, 11, 18, 11, 41, tzinfo=timezone.utc).timestamp(), "Lead clock: it stopped partway")
    slack.history_rows += [a, b]
    aid, bid = F.shape_key(a["text"]), F.shape_key(b["text"])
    sh = FakeShell(result={"issues": [
        {"id": aid, "class": "bug", "summary": "x", "reply": "The recorder dropped the line.", "fixed": True, "commits": ["abc1234"]},
        {"id": bid, "class": "bug", "summary": "y", "reply": "Same cause.", "fixed": True, "commits": ["abc1234"]}]})
    monkeypatch.setattr(F, "apps_for", lambda repo, changed: ["siftstack-ftm"])
    tried = []
    real_window = F.window
    monkeypatch.setattr(F, "window", lambda sh_, app, now=None: tried.append(clock.t) or real_window(sh_, app, now))
    morning_runs(clock, st, sh, slack, datetime(2026, 11, 18, 11, 45, tzinfo=timezone.utc),       # 6:45 to 9:00
                 datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc))
    waits = [p for p in slack.posts if "waits to deploy" in p["text"]]
    assert len(waits) == 1 and not waits[0]["broadcast"], "the wait is said once, in the thread"
    assert "the first-to-market box waits: the first-to-market run and its 8:00 digest" in waits[0]["text"]
    assert not any(p["text"].startswith("Shipped") for p in slack.posts), "nothing is shipped before the box takes it"
    assert len(tried) == 10, "a waiting deploy is tried again on every run"
    led = F.load_ledger()
    assert led["issues"][aid]["status"] == "waiting_to_deploy" and led["issues"][bid]["status"] == "waiting_to_deploy"
    morning_runs(clock, st, sh, slack, datetime(2026, 11, 18, 14, 30, tzinfo=timezone.utc),       # 9:30 and 9:45
                 datetime(2026, 11, 18, 14, 45, tzinfo=timezone.utc))
    shipped = [p for p in slack.posts if p["text"].startswith("Shipped")]
    assert len(shipped) == 1 and shipped[0]["broadcast"] and "first-to-market box v34" in shipped[0]["text"]
    assert sum("deploy -c fly.ftm.toml --remote-only" in c for c in sh.calls) == 1
    led = F.load_ledger()
    assert led["issues"][aid]["status"] == "fixed" and led["issues"][aid]["fixed_at"]
    assert led["issues"][bid]["status"] == "fixed", "the issue that shipped with the fix moves with it"


def test_a_fix_that_reaches_two_boxes_ships_where_it_can_and_says_the_wait_once(monkeypatch):
    clock = Clock(datetime(2026, 11, 18, 11, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(F, "now_utc", clock)
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    bug = post(datetime(2026, 11, 18, 11, 40, tzinfo=timezone.utc).timestamp(), "Research queue: it stopped partway")
    slack.history_rows.append(bug)
    bid = F.shape_key(bug["text"])
    sh = FakeShell(result={"issues": [{"id": bid, "class": "bug", "summary": "x", "reply": "It crashed.", "fixed": True,
                                       "commits": ["abc1234"]}]})
    monkeypatch.setattr(F, "apps_for", lambda repo, changed: ["siftstack-agents", "siftstack-ftm"])
    monkeypatch.setattr(F, "checks", lambda sh_, app: (True, ["checks pass"]))
    morning_runs(clock, st, sh, slack, datetime(2026, 11, 18, 11, 45, tzinfo=timezone.utc),
                 datetime(2026, 11, 18, 14, 45, tzinfo=timezone.utc))
    shipped = [p["text"] for p in slack.posts if p["text"].startswith("Shipped")]
    assert len(shipped) == 2
    assert "the brain v34" in shipped[0] and "the first-to-market box waits" in shipped[0]
    assert "the first-to-market box v34" in shipped[1] and "waits" not in shipped[1]
    assert sum("deploy -c fly.agents.toml --remote-only" in c for c in sh.calls) == 1
    assert sum("deploy -c fly.ftm.toml --remote-only" in c for c in sh.calls) == 1
    assert F.load_ledger()["issues"][bid]["status"] == "fixed"


def test_an_account_change_is_dry_run_staged_and_run_on_ok(monkeypatch, tmp_state):
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    issue = post(datetime(2026, 11, 18, 15, 5, tzinfo=timezone.utc).timestamp(), "Lead weekly: the preset filters on a status nobody uses")
    slack.history_rows.append(issue)
    iid = F.shape_key(issue["text"])
    run_scripts = F.STATE / "runs" / "x"
    run_scripts.mkdir(parents=True)
    script = run_scripts / "fix_preset.py"
    script.write_text("print('would change 1 preset')\n", encoding="utf-8")
    sh = FakeShell(result={"issues": [{"id": iid, "class": "setup", "summary": "the preset stores a label",
                                       "reply": "The preset uses the wrong spelling.",
                                       "account_change": {"script": str(script), "what": "fix one preset value"}}]})
    F.run(Args(), st=st, sh=sh, slack=slack)
    iss = F.load_ledger()["issues"][iid]
    assert iss["status"] == "awaiting_ok" and iss["waiting_for"]["sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
    assert not any("--commit" in c for c in sh.calls), "nothing is written before the ok"
    slack.replies_rows[issue["ts"]] = [{"ts": str(float(iss["asked_ts"]) + 5), "user": "UTY", "text": "ok"}]
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert any(c.endswith("fix_preset.py --commit") for c in sh.calls)
    assert F.load_ledger()["issues"][iid]["status"] == "fixed"


def test_the_off_switch_and_the_daily_cap(monkeypatch):
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    F.switch(False)
    sh = FakeShell(result={"issues": []})
    assert F.run(Args(), st=st, sh=sh, slack=slack) == 0 and sh.calls == []
    F.switch(True)
    F.record_run({"day": "2026-11-18", "usd": 25})
    slack.history_rows.append(post(datetime(2026, 11, 18, 15, 5, tzinfo=timezone.utc).timestamp(), "Something new broke"))
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert not any("claude" in c for c in sh.calls), "past the day's cap no session starts"


def test_a_deferred_bug_keeps_its_tries(monkeypatch):
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    a = post(datetime(2026, 11, 18, 15, 5, tzinfo=timezone.utc).timestamp(), "Lead clock: it stopped partway")
    slack.history_rows.append(a)
    aid = F.shape_key(a["text"])
    sh = FakeShell(result={"issues": [{"id": aid, "class": "bug", "summary": "x", "reply": "x", "fixed": False, "deferred": True}]})
    for _ in range(4):
        F.run(Args(), st=st, sh=sh, slack=slack)
    iss = F.load_ledger()["issues"][aid]
    assert iss["status"] == "new" and not iss.get("attempts"), "waiting its turn costs no try"
    sh.kw["result"] = {"issues": [{"id": aid, "class": "bug", "summary": "x", "reply": "x", "fixed": False}]}
    for _ in range(3):
        F.run(Args(), st=st, sh=sh, slack=slack)
    assert F.load_ledger()["issues"][aid]["status"] == "needs_person"
    assert sum("in line for a fix" in p["text"] for p in slack.posts) == 1, "the thread hears it once"


def test_the_box_runs_at_each_quarter_hour():
    t = datetime(2026, 11, 18, 15, 7, 31, tzinfo=timezone.utc)
    assert F.next_quarter(t) == datetime(2026, 11, 18, 15, 15, tzinfo=timezone.utc)
    assert F.next_quarter(datetime(2026, 11, 18, 15, 45, tzinfo=timezone.utc)) == datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc)


def test_the_box_token_becomes_fly_login_and_the_api_key_never_reaches_claude(monkeypatch):
    monkeypatch.setenv("FLY_API_TOKEN", "scoped-to-one-app")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.delenv("ISSUE_FIXER_FLY_TOKEN", raising=False)
    env = F.plain_env()
    assert "FLY_API_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env, "the workstation uses Ty's own fly login"
    monkeypatch.setenv("ISSUE_FIXER_FLY_TOKEN", "deploy-tokens")
    env = F.plain_env()
    assert env["FLY_API_TOKEN"] == "deploy-tokens" and "ISSUE_FIXER_FLY_TOKEN" not in env


def test_the_loop_refreshes_its_own_clone_and_runs(monkeypatch):
    monkeypatch.setenv("ISSUE_FIXER_OWN_CLONE", "1")
    sh, ran = FakeShell(), []
    assert F.loop(sh=sh, once=True, runner=lambda: ran.append(1) or 0) == 0
    assert ran == [1] and any("reset --hard origin/agents-infra" in c for c in sh.calls)
    monkeypatch.delenv("ISSUE_FIXER_OWN_CLONE")
    sh = FakeShell()
    F.loop(sh=sh, once=True, runner=lambda: 0)
    assert not any("reset --hard" in c for c in sh.calls), "the workstation's checkout is never reset"


def test_a_worktree_needs_no_env_file_on_the_box(tmp_state):
    (F.MAIN / ".env").unlink()
    F.prepare_worktree(FakeShell())
    assert not (F.WORKTREE / ".env").exists()


class HandoverShell(FakeShell):
    def __init__(self, names, claude_ok=True, staged=False):
        super().__init__(result={"issues": []})
        self.names, self.claude_ok, self.staged = names, claude_ok, staged

    def run(self, cmd, cwd=None, timeout=1800, env=None, input_text=None):
        line = " ".join(str(c) for c in cmd)
        if " secrets list " in " %s " % line:
            self.calls.append(line)
            return 0, json.dumps([{"name": n, "status": "Staged" if n == "CLAUDE_CODE_OAUTH_TOKEN" and self.staged
                                   else "Deployed"} for n in self.names])
        if " secrets deploy " in " %s " % line:
            self.calls.append(line)
            self.staged = False
            return 0, "deployed"
        if "claude -p" in line and " ssh console " in " %s " % line:
            self.calls.append(line)
            return (0, '{"type": "result", "is_error": false, "result": "ok"}') if self.claude_ok else (1, "auth failed")
        return super().run(cmd, cwd, timeout, env, input_text)


def test_a_staged_sign_in_is_deployed_before_the_box_is_tried(monkeypatch):
    monkeypatch.setattr(F, "HANDOVER_SETTLE_S", 0)
    st = settings(ISSUE_FIXER_HANDOVER_TO="siftstack-fixer")
    slack = FakeSlack([])
    seeded(slack, st)
    sh = HandoverShell(["CLAUDE_CODE_OAUTH_TOKEN"], staged=True)
    F.run(Args(), st=st, sh=sh, slack=slack)
    deploy_at = next(i for i, c in enumerate(sh.calls) if " secrets deploy " in " %s " % c)
    claude_at = next(i for i, c in enumerate(sh.calls) if "claude -p" in c)
    assert deploy_at < claude_at and (F.STATE / "OFF").exists()


def test_the_workstation_hands_over_only_when_the_box_can_work():
    st = settings(ISSUE_FIXER_HANDOVER_TO="siftstack-fixer")
    slack = FakeSlack([])
    seeded(slack, st)
    sh = HandoverShell(["SLACK_BOT_TOKEN"])
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert not (F.STATE / "OFF").exists(), "without the Claude sign-in on the box the workstation keeps working"
    sh = HandoverShell(["SLACK_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"], claude_ok=False)
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert not (F.STATE / "OFF").exists(), "a box where Claude Code does not answer does not take over"
    sh = HandoverShell(["SLACK_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
    assert F.run(Args(), st=st, sh=sh, slack=slack) == 0
    assert any("sftp put" in c and "/data/state/ledger.json" in c for c in sh.calls), "the ledger goes first"
    assert any("secrets set ISSUE_FIXER=on" in c for c in sh.calls)
    assert any("schtasks /change /tn SiftStack Issue Fixer /disable" in c for c in sh.calls)
    assert (F.STATE / "OFF").exists(), "and the workstation's fixer is off"
    sh = HandoverShell(["CLAUDE_CODE_OAUTH_TOKEN"])
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert not sh.calls, "after the handover the workstation does nothing"


def test_no_dash_in_the_fixer_sources():
    for name in ("fixer.py", "PLAYBOOK.md", "seed_verdicts.json"):
        text = (ROOT / "tools" / "issue_fixer" / name).read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, name


# ---------------------------------------------------------------------- the public release after a fix
C1, C2, C3, C5, C9 = ("c1c1c1c1c1c1", "c2c2c2c2c2c2", "c3c3c3c3c3c3", "c5c5c5c5c5c5", "c9c9c9c9c9c9")   # commits: hex, as public main's message carries them


class ReleaseShell:
    """git and tools/public_release.py, for release_due: `source` is the commit public main says it was
    built from, `ancestors` the (older, newer) pairs in one history, `outs` what each release prints."""

    def __init__(self, source=C1, ancestors=(), outs=None, tip=C9):
        self.calls, self.source, self.ancestors, self.tip = [], source, set(ancestors), tip
        self.outs = list(outs or [(0, "RESULT released 1234567890ab from c2c2c2c2c2c2, 1 new lesson(s)")])

    def run(self, cmd, cwd=None, timeout=1800, env=None, input_text=None):
        c = [str(x) for x in cmd]
        line = " ".join(c)
        self.calls.append(line)
        if "public_release.py" in line:
            rc, out = self.outs.pop(0) if len(self.outs) > 1 else self.outs[0]
            return rc, "export c2\nscan: 0 finding(s)\n" + out + "\n"
        if os.path.basename(c[0]).lower() in ("git", "git.exe"):
            if "log" in c and "--format=%B" in c:
                return 0, ("Release 2026-11-23: x\n\nBuilt by tools/public_release.py from %s: 39 private file(s) left out"
                           % self.source)
            if "merge-base" in c:
                older, newer = c[-2], c[-1]
                return (0 if older == newer or (older, newer) in self.ancestors else 1), ""
            if "rev-parse" in c:
                return 0, self.tip + "\n"
        return 0, ""

    def releases(self):
        return [c for c in self.calls if "public_release.py" in c]


def shipped(led, iid, commit, ts="100"):
    led["issues"][iid] = {"id": iid, "text": "issue %s" % iid, "first_ts": ts, "last_ts": ts, "seen": 1, "history": [],
                          "status": "fixed", "fix": {"commit": commit, "run": "r", "tests": [], "apps": []}}
    return led["issues"][iid]


@pytest.fixture
def release_tool():
    (F.MAIN / "tools").mkdir(parents=True, exist_ok=True)
    (F.MAIN / "tools" / "public_release.py").write_text("# stands in for the release tool\n", encoding="utf-8")


def test_a_shipped_fix_is_released_and_its_thread_hears(release_tool):
    led, slack = F.empty_ledger(), FakeSlack([])
    iss = shipped(led, "i1", C2)
    sh = ReleaseShell(source=C1, ancestors={(C1, C2)})
    what = F.release_due(sh, slack, settings(), led)
    assert sh.releases() and "publish --push --ref " + C2 in sh.releases()[0]
    assert iss["released_in"] == "1234567890ab" and "released c2c2c2c" in what
    assert [p["text"] for p in slack.posts] == ["Public SiftStack has this fix now (release 1234567)."]
    assert slack.posts[0]["thread_ts"] == "100" and not slack.posts[0]["broadcast"], "a quiet note in the fix's own thread"
    assert F.release_due(sh, slack, settings(), led) == "" and len(sh.releases()) == 1, "released once"


def test_a_fix_the_last_release_carries_is_marked_without_a_release(release_tool):
    led, slack = F.empty_ledger(), FakeSlack([])
    iss = shipped(led, "i1", C1)
    sh = ReleaseShell(source=C5, ancestors={(C1, C5)})
    F.release_due(sh, slack, settings(), led)
    assert not sh.releases() and iss["released_in"] == "built from " + C5 and not slack.posts


def test_two_fixes_go_out_in_one_release_of_the_newest(release_tool):
    led, slack = F.empty_ledger(), FakeSlack([])
    a, b = shipped(led, "i1", C2, ts="100"), shipped(led, "i2", C3, ts="200")
    sh = ReleaseShell(source=C1, ancestors={(C2, C3), (C1, C2), (C1, C3)})
    F.release_due(sh, slack, settings(), led)
    assert len(sh.releases()) == 1 and "--ref " + C3 in sh.releases()[0]
    assert a["released_in"] == b["released_in"] == "1234567890ab" and len(slack.posts) == 2


def test_a_fix_waiting_for_its_box_is_not_released_yet(release_tool):
    led, slack = F.empty_ledger(), FakeSlack([])
    iss = shipped(led, "i1", C2)
    iss["status"] = "waiting_to_deploy"
    sh = ReleaseShell(source=C1)
    assert F.release_due(sh, slack, settings(), led) == "" and not sh.calls
    iss["status"] = "fixed"                        # the box took it
    F.release_due(sh, slack, settings(), led)
    assert sh.releases()


def test_a_scan_stop_is_said_once_and_the_next_fix_tries_again(release_tool):
    led, slack = F.empty_ledger(), FakeSlack([])
    shipped(led, "i1", C2)
    stop = (1, "RESULT stopped: the scan found 2 private thing(s) (in docs/lessons-learned.md) and 0 file(s) did not regenerate")
    sh = ReleaseShell(source=C1, outs=[stop])
    F.release_due(sh, slack, settings(), led)
    assert len(slack.posts) == 1 and slack.posts[0]["broadcast"]
    assert "public release stopped: the scan found 2 private thing(s)" in slack.posts[0]["text"]
    assert "waits for the next fix" in F.release_due(sh, slack, settings(), led)
    assert len(sh.releases()) == 1 and len(slack.posts) == 1, "not tried again, not said again"
    shipped(led, "i2", C3, ts="300")
    sh.ancestors |= {(C2, C3)}
    sh.outs = [(0, "RESULT released abcdef123456 from c3c3c3c3c3c3, 2 new lesson(s)")]
    F.release_due(sh, slack, settings(), led)
    assert "--ref " + C3 in sh.releases()[-1] and led["issues"]["i1"]["released_in"] == "abcdef123456"
    assert "failed" not in led["release"]


def test_a_failure_with_no_stated_reason_is_tried_three_times_then_said(release_tool):
    led, slack = F.empty_ledger(), FakeSlack([])
    shipped(led, "i1", C2)
    sh = ReleaseShell(source=C1, outs=[(1, "git push public HEAD:main failed: Connection reset by peer")])
    for _ in range(2):
        F.release_due(sh, slack, settings(), led)
        assert not slack.posts
    F.release_due(sh, slack, settings(), led)
    assert len(sh.releases()) == 3 and len(slack.posts) == 1 and "Connection reset by peer" in slack.posts[0]["text"]
    F.release_due(sh, slack, settings(), led)
    assert len(sh.releases()) == 3, "after three tries it waits for the next fix"


def test_the_release_switch_and_a_missing_tool(release_tool):
    led, slack = F.empty_ledger(), FakeSlack([])
    shipped(led, "i1", C2)
    sh = ReleaseShell(source=C1)
    assert F.release_due(sh, slack, settings(ISSUE_FIXER_RELEASE="off"), led) == "" and not sh.calls
    (F.MAIN / "tools" / "public_release.py").unlink()
    assert "no public release" in F.release_due(sh, slack, settings(), led) and not sh.calls


def test_force_releases_the_tip_with_nothing_due(release_tool):
    led, slack = F.empty_ledger(), FakeSlack([])
    sh = ReleaseShell(source=C1, tip=C9)
    F.release_due(sh, slack, settings(), led, force=True)
    assert "--ref " + C9 in sh.releases()[0] and not slack.posts
    sh2 = ReleaseShell(source=C9, tip=C9)
    assert "already carries" in F.release_due(sh2, slack, settings(), led, force=True) and not sh2.releases()


def test_a_run_that_ships_a_fix_then_releases_it(monkeypatch):
    slack = FakeSlack([])
    st = settings()
    seeded(slack, st)
    bug = post(datetime(2026, 11, 18, 15, 5, tzinfo=timezone.utc).timestamp(), "Research queue: it stopped partway")
    slack.history_rows.append(bug)
    bid = F.shape_key(bug["text"])
    sh = FakeShell(result={"issues": [
        {"id": bid, "class": "bug", "summary": "a crash", "reply": "It crashed.", "fixed": True, "commits": ["abc1234"]}]})
    monkeypatch.setattr(F, "apps_for", lambda repo, changed: [])
    seen = []
    monkeypatch.setattr(F, "release_due", lambda sh_, slack_, st_, led: seen.append(led["issues"][bid]["status"]) or "")
    F.run(Args(), st=st, sh=sh, slack=slack)
    assert seen == ["fixed"], "the release step runs once, after the fix has gone out"
    seen.clear()

    class NoShip(Args):
        no_ship = True
    F.run(NoShip(), st=st, sh=FakeShell(result={"issues": []}), slack=slack)
    assert not seen, "--no-ship releases nothing"
