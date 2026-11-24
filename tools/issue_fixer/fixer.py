"""The issue fixer: each new post in the SiftStack issues channel sorted, and a bug in our code fixed,
tested and shipped on its own (Ty, 2026-11-18: "Deploy on its own"). Staff-only: it ships the team's own
boxes and the public release; an install has no use for it.

It runs on Ty's PC from a scheduled task every 15 minutes, because that machine already holds the Fly
login, the keys and the repo. Each run:
  1. reads the issues channel since the last run with the brain's own bot token, and folds each post
     into the ledger by its shape (digits, examples and parentheses taken out), so a daily repeat is one
     issue, not a new one;
  2. carries out what a person approved in an issue's thread (an account change, a texting app deploy);
  3. hands the new issues to ONE Claude Code session in its own git worktree, which finds each cause,
     sorts it (bug, setup, finding, by design, transient, decision, duplicate) and, for one bug, writes
     the fix and its test and commits it (PLAYBOOK.md is its brief);
  4. checks the fix itself: the agents selftest and pytest must pass, or nothing ships;
  5. pushes to agents-infra, deploys each box the change reaches (worked out by following imports from
     each box's entry point), runs that box's own checks, and goes back to the previous image when a
     check fails;
  6. answers in each issue's thread, and broadcasts what it shipped or could not.

What it will not do on its own: change the DataSift account (a script is staged and dry run, and a
person replies ok in the thread), or deploy the texting app (a restart drops incoming texts). The session
is told to read production and never write; it can run Python, so that rule is an instruction, not a
wall. A spend cap per session and per day, one session at a time, and an off switch
(`python tools/issue_fixer/fixer.py off`, or ISSUE_FIXER=off in .env).

    python tools/issue_fixer/fixer.py run [--dry] [--no-ship] [--quiet]
    python tools/issue_fixer/fixer.py seed [--hours 72]      # mark what is already in the channel as seen
    python tools/issue_fixer/fixer.py status | off | on
    python tools/issue_fixer/fixer.py loop                   # the fixer's own Fly box (fly.fixer.toml)
    python tools/issue_fixer/fixer.py release [--force] [--dry]   # publish the newest shipped fix by hand

After every run (Ty, 2026-11-24: "let's just do a release whenever a fix goes out"): each fix that went out
is published to public SiftStack with tools/public_release.py (release_due), and its thread hears so. The
session writes the fix's lesson into docs/lessons-learned.md, which the release carries.

Where it runs (2026-11-18, Ty: off the desktop): its own Fly app, siftstack-fixer, signed in to Ty's Max
plan with CLAUDE_CODE_OAUTH_TOKEN, deploying with ISSUE_FIXER_FLY_TOKEN, pushing with a GitHub deploy key,
working in its own clone on the volume. The workstation path still works (the scheduled task is disabled).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
MAIN = Path(os.environ.get("SIFTSTACK_MAIN") or REPO)
STATE = Path(os.environ.get("ISSUE_FIXER_STATE") or (MAIN / "output" / "issue_fixer"))
WORKTREE = Path(os.environ.get("ISSUE_FIXER_WORKTREE") or "C:/pp/_work/issue_fixer")
DEPLOY_TREE = Path(os.environ.get("ISSUE_FIXER_DEPLOY_TREE") or "C:/pp/_work/fixer_deploy")
PY = Path(os.environ.get("ISSUE_FIXER_PYTHON") or (MAIN / ".venv" / "Scripts" / "python.exe"))
BRANCH, WORK_BRANCH = "agents-infra", "issue-fixer"
TZ = ZoneInfo("America/New_York")
USERNAME = "Issue fixer"
SESSION_TIMEOUT_S = 75 * 60
REGRESSION_GRACE = timedelta(minutes=30)
MAX_NEW_PER_SESSION = 12
HANDOVER_SETTLE_S = 60          # the box restarting after its staged secrets are deployed
LOCK_STALE = timedelta(hours=3)
ATTRIBUTION = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
CLASSES = ("bug", "setup", "finding", "by_design", "transient", "decision", "duplicate")
OPEN_FOR_PEOPLE = ("setup", "finding", "decision")          # said once, then left for a person
CLOSED = ("by_design", "transient", "duplicate")
SKIP_SUBTYPES = {"channel_join", "channel_leave", "thread_broadcast", "channel_topic", "channel_purpose",
                 "channel_name", "message_changed", "message_deleted", "pinned_item", "reminder_add"}
OK_RX = re.compile(r"^\s*(ok|okay|yes|ship|ship it|apply|approve|approved|do it|go)\b", re.I)

APPS = {
    # entries: where its running code starts (the import graph is followed from there); always: paths
    # that are in the image as they are; auto: may it be deployed without a person.
    "siftstack-agents": {"toml": "fly.agents.toml", "name": "the brain", "auto": True,
                         "entry_dirs": ["src/agents"], "entries": [],
                         "always": ["team/", "skills/", "data/master_materials_locked_37914.json",
                                    "data/deal_odds_model.json", "data/us_counties.csv", "deploy/Dockerfile.agents",
                                    "deploy/requirements-agents.txt", "fly.agents.toml"]},
    "siftstack-ftm": {"toml": "fly.ftm.toml", "name": "the first-to-market box", "auto": True,
                      "entry_dirs": [], "entries": ["src/ftm_schedule.py", "src/ftm_runner.py", "src/ftm_health.py",
                                                    "src/ftm_budget.py"],
                      "always": ["deploy/Dockerfile.ftm", "deploy/requirements-ftm.txt", "fly.ftm.toml"]},
    # The texting image holds src/sms_agent and model_registry.py only, so no import graph is needed.
    "siftstack": {"toml": "fly.toml", "name": "the texting app", "auto": False, "image_only": True,
                  "entry_dirs": [], "entries": [],
                  "always": ["src/sms_agent/", "src/model_registry.py", "deploy/Dockerfile",
                             "deploy/requirements-sms-agent.txt", "fly.toml"]},
    # The dialer is never deployed by the fixer: a restart mid-call drops the caller's phone page and
    # Twilio's callbacks for that call. Its image holds exactly these files (deploy/Dockerfile.dialer).
    "siftstack-dialer": {"toml": "fly.dialer.toml", "name": "the dialer", "auto": False, "image_only": True,
                         "entry_dirs": [], "entries": [],
                         "always": ["src/dialer/", "src/sms_agent/gate_rules.py", "src/agents/autopilot/windows.py",
                                    "src/datasift_links.py", "deploy/Dockerfile.dialer", "deploy/requirements-dialer.txt",
                                    "fly.dialer.toml"]},
}
ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git add:*)",
    "Bash(git commit:*)", "Bash(git rm:*)", "Bash(git mv:*)", "Bash(git grep:*)", "Bash(git blame:*)",
    "Bash(git ls-files:*)", "Bash(git checkout:*)", "Bash(git restore:*)", "Bash(git stash:*)",
    "Bash(python:*)", "Bash(fly ssh console:*)", "Bash(fly ssh sftp:*)", "Bash(fly logs:*)",
    "Bash(fly status:*)", "Bash(fly releases:*)", "Bash(curl -s:*)", "Bash(ls:*)", "Bash(cat:*)",
    "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(grep:*)", "Bash(sed -n:*)", "Bash(find:*)",
    "Bash(sort:*)", "Bash(uniq:*)", "Bash(cut:*)", "Bash(echo:*)", "Bash(mkdir:*)", "Bash(diff:*)",
]
DENIED_TOOLS = [
    "Bash(fly deploy:*)", "Bash(fly secrets:*)", "Bash(fly machine:*)", "Bash(fly scale:*)", "Bash(fly apps:*)",
    "Bash(fly volumes:*)", "Bash(fly auth:*)", "Bash(git push:*)", "Bash(git reset:*)", "Bash(git rebase:*)",
    "Bash(git merge:*)", "Bash(git clean:*)", "Bash(rm -rf:*)", "Bash(timeout:*)", "PowerShell", "Agent",
    "Workflow", "Artifact", "WebSearch", "WebFetch", "Skill", "Monitor", "ScheduleWakeup", "CronCreate",
    "RemoteTrigger", "SendMessage",
]


# ---------------------------------------------------------------------- settings
def read_env(path: Path) -> dict:
    out: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


class Settings:
    def __init__(self, env: dict | None = None):
        file_env = read_env(MAIN / ".env")
        merged = dict(file_env)
        merged.update({k: v for k, v in os.environ.items() if k.startswith(("ISSUE_FIXER", "SLACK_", "AGENTS_SLACK"))})
        merged.update(env or {})
        self.env = merged
        self.token = merged.get("SLACK_BOT_TOKEN", "")
        self.channel = merged.get("AGENTS_SLACK_ISSUES_CHANNEL", "")
        self.on = (merged.get("ISSUE_FIXER") or "on").strip().lower() not in ("off", "0", "false", "no")
        self.daily_usd = float(merged.get("ISSUE_FIXER_DAILY_USD") or 25)
        self.session_usd = float(merged.get("ISSUE_FIXER_SESSION_USD") or 12)
        self.approvers = {x.strip() for x in (merged.get("ISSUE_FIXER_APPROVERS") or "").split(",") if x.strip()}
        auto = merged.get("ISSUE_FIXER_AUTO_DEPLOY")
        self.auto_apps = ({a.strip() for a in auto.split(",") if a.strip()} if auto is not None
                          else {a for a, c in APPS.items() if c["auto"]})
        self.icon = merged.get("ISSUE_FIXER_ICON_URL", "")
        self.model = merged.get("ISSUE_FIXER_MODEL") or _registry_model()
        # Ty, 2026-11-24: "let's just do a release whenever a fix goes out". ISSUE_FIXER_RELEASE=off stops it.
        self.release = (merged.get("ISSUE_FIXER_RELEASE") or "on").strip().lower() not in ("off", "0", "false", "no")


def _registry_model() -> str:
    try:
        sys.path.insert(0, str(MAIN / "src"))
        import model_registry  # noqa: PLC0415
        return model_registry.model("issue_fixer")
    except Exception:  # noqa: BLE001
        return "claude-opus-5-5"


# ---------------------------------------------------------------------- small helpers
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_day(t: datetime | None = None) -> str:
    return (t or now_utc()).astimezone(TZ).date().isoformat()


def log(msg: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = "%s %s" % (now_utc().astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(STATE / "fixer.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def plain(text: str) -> str:
    """What goes into Slack: no em or en dash, no ellipsis character, no emoji code, trimmed."""
    t = str(text or "")
    t = t.replace(chr(0x2014), ", ").replace(chr(0x2013), "-").replace(chr(0x2026), "...")
    t = re.sub(r"(?<!\d):[a-z0-9_+\-]+:(?!\d)", "", t)
    return "\n".join(ln.rstrip() for ln in t.strip().splitlines())


def shape(text: str) -> str:
    """A post with what changes from day to day taken out: links, parentheses, examples, numbers."""
    lines = []
    for ln in str(text or "").lower().splitlines():
        ln = re.sub(r"<[^>]*>", " ", ln)
        ln = re.split(r"\bfor example\b|\bexamples?:|\be\.g\.", ln)[0]
        lines.append(ln)
    t = " ".join(lines)
    for _ in range(3):
        t = re.sub(r"\([^()]*\)", " ", t)
    t = re.sub(r"\d[\d,.:/]*", "#", t)
    t = re.sub(r"[^a-z#]+", " ", t)
    # "1 new lead has" and "11 new leads have" are the same problem: plurals and the verbs that follow
    # a count are folded together.
    t = re.sub(r"\b(has|have|was|were|is|are)\b", "be", t)
    t = re.sub(r"\b([a-z]{3,}?)(?:ies|es|s)\b", r"\1", t)
    return " ".join(t.split())[:400]


def shape_key(text: str) -> str:
    return hashlib.sha1(shape(text).encode("utf-8")).hexdigest()[:8]


def post_text(m: dict) -> str:
    text = str(m.get("text") or "")
    for b in m.get("blocks") or []:
        t = ((b.get("text") or {}).get("text") if isinstance(b, dict) else "") or ""
        if t and t not in text:
            text += "\n" + t
    return text.strip()


def is_issue_post(m: dict) -> bool:
    if m.get("subtype") in SKIP_SUBTYPES:
        return False
    if m.get("username") == USERNAME:
        return False
    if m.get("thread_ts") and m.get("thread_ts") != m.get("ts"):
        return False
    return bool(post_text(m))


def author(m: dict) -> str:
    if m.get("username"):
        return str(m["username"])
    if m.get("bot_profile"):
        return str((m.get("bot_profile") or {}).get("name") or "a bot")
    return "a person"


# ---------------------------------------------------------------------- the ledger
def empty_ledger() -> dict:
    return {"issues": {}, "last_ts": "", "seeded": False}


def load_ledger() -> dict:
    p = STATE / "ledger.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_ledger()


def save_ledger(led: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / "ledger.json.tmp"
    tmp.write_text(json.dumps(led, indent=1), encoding="utf-8")
    os.replace(tmp, STATE / "ledger.json")


def fold(led: dict, posts: list[dict], now: datetime | None = None, seen_only: bool = False) -> list[str]:
    """Each post into the ledger by its shape. Returns the ids that need a session (new or regressed)."""
    now = now or now_utc()
    need: list[str] = []
    for m in sorted(posts, key=lambda x: float(x.get("ts") or 0)):
        if not is_issue_post(m):
            continue
        text, ts = post_text(m), str(m["ts"])
        key = shape_key(text)
        iss = led["issues"].get(key)
        if iss is None:
            iss = led["issues"][key] = {"id": key, "first_ts": ts, "text": text[:1500], "author": author(m),
                                        "posts": [], "seen": 0, "status": "seen" if seen_only else "new",
                                        "class": "", "summary": "", "history": []}
            if not seen_only:
                need.append(key)
        iss["seen"] = int(iss.get("seen") or 0) + 1
        iss["last_ts"] = ts
        iss["posts"] = (iss.get("posts") or [])[-19:] + [ts]
        if not seen_only and iss.get("status") == "fixed" and iss.get("fixed_at"):
            fixed_at = datetime.fromisoformat(iss["fixed_at"])
            if datetime.fromtimestamp(float(ts), tz=timezone.utc) > fixed_at + REGRESSION_GRACE:
                iss["regressions"] = int(iss.get("regressions") or 0) + 1
                iss["status"] = "regression" if iss["regressions"] < 2 else "needs_person"
                iss["history"].append({"at": now.isoformat(), "what": "posted again after the fix (%s)" % ts})
                if iss["status"] == "regression" and key not in need:
                    need.append(key)
        led["last_ts"] = max(str(led.get("last_ts") or "0"), ts, key=float)
    return need


def apply_seed_verdicts(led: dict, verdicts: list[dict]) -> int:
    """A first pass's verdicts by a piece of wording: [{match, class, status, summary}]."""
    n = 0
    for iss in led["issues"].values():
        low = iss["text"].lower()
        for v in verdicts:
            if v["match"].lower() in low:
                iss.update({"class": v["class"], "status": v.get("status") or "closed", "summary": v.get("summary", "")})
                if iss["status"] == "fixed":
                    iss.setdefault("fixed_at", now_utc().isoformat())    # posted again from here on is a regression
                n += 1
                break
    return n


# ---------------------------------------------------------------------- Slack
class Slack:
    def __init__(self, token: str, icon: str = ""):
        self.token, self.icon = token, icon

    def call(self, method: str, body: dict | None = None, **params) -> dict:
        url = "https://slack.com/api/%s" % method
        headers = {"Authorization": "Bearer " + self.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode("utf-8"))
            if out.get("ok") or out.get("error") != "ratelimited":
                return out
            time.sleep(3 * (attempt + 1))
        return out

    def history(self, channel: str, oldest: str) -> list[dict]:
        msgs, cursor = [], None
        while True:
            kw = {"channel": channel, "limit": 200}
            if oldest:
                kw["oldest"] = oldest
            if cursor:
                kw["cursor"] = cursor
            r = self.call("conversations.history", **kw)
            if not r.get("ok"):
                raise RuntimeError("Slack history: %s" % r.get("error"))
            msgs += r.get("messages") or []
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return [m for m in msgs if not oldest or float(m.get("ts") or 0) > float(oldest)]

    def replies(self, channel: str, ts: str) -> list[dict]:
        r = self.call("conversations.replies", channel=channel, ts=ts, limit=200)
        return (r.get("messages") or [])[1:] if r.get("ok") else []

    def post(self, channel: str, text: str, thread_ts: str = "", broadcast: bool = False) -> str:
        body = {"channel": channel, "text": plain(text), "username": USERNAME, "unfurl_links": False}
        if self.icon:
            body["icon_url"] = self.icon
        if thread_ts:
            body["thread_ts"] = thread_ts
            body["reply_broadcast"] = bool(broadcast)
        r = self.call("chat.postMessage", body)
        if not r.get("ok"):
            raise RuntimeError("Slack post: %s" % r.get("error"))
        return str(r.get("ts") or "")


# ---------------------------------------------------------------------- shell
class Shell:
    """Every command the fixer runs, in one place, so a test can stand in for all of them."""

    def run(self, cmd: list, cwd: Path | None = None, timeout: int = 1800, env: dict | None = None,
            input_text: str | None = None) -> tuple[int, str]:
        # No console window: the scheduled task runs every 15 minutes while Ty works.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            p = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=env, input=input_text,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               creationflags=flags)
        except subprocess.TimeoutExpired as exc:
            return 124, "timed out after %ds: %s" % (timeout, str(exc)[:200])
        except OSError as exc:
            return 127, str(exc)
        return p.returncode, (p.stdout or "") + (p.stderr or "")


TOOL_HOMES = {"fly": [Path.home() / ".fly" / "bin" / "fly.exe"], "claude": [Path.home() / ".local" / "bin" / "claude.exe"],
              "git": [Path("C:/Program Files/Git/cmd/git.exe")]}


def tool(name: str) -> str:
    """A tool's full path: a scheduled task's PATH can be shorter than a terminal's."""
    found = shutil.which(name) or shutil.which(name + ".exe")
    if found:
        return found
    for p in TOOL_HOMES.get(name, []):
        if p.exists():
            return str(p)
    return name


def plain_env() -> dict:
    """The environment for fly and Claude Code. No API key, so Claude Code uses its own sign-in (Ty's Max
    plan: his login on the workstation, CLAUDE_CODE_OAUTH_TOKEN on the fixer's box). FLY_API_TOKEN: on
    the workstation the .env one is scoped to one app, so it is taken out and Ty's own fly login is used;
    on the fixer's box ISSUE_FIXER_FLY_TOKEN carries the three apps' deploy tokens and becomes it. No path
    rewriting by Git Bash."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("FLY_API_TOKEN", "ANTHROPIC_API_KEY", "ISSUE_FIXER_FLY_TOKEN")}
    if os.environ.get("ISSUE_FIXER_FLY_TOKEN"):
        env["FLY_API_TOKEN"] = os.environ["ISSUE_FIXER_FLY_TOKEN"]
    env["MSYS_NO_PATHCONV"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def git(sh: Shell, *args, cwd: Path = WORKTREE, timeout: int = 300) -> tuple[int, str]:
    return sh.run([tool("git"), "-c", "core.autocrlf=false", *args], cwd=cwd, timeout=timeout)


# ---------------------------------------------------------------------- which box a change reaches
def module_index(src: Path) -> dict:
    idx = {}
    for p in src.rglob("*.py"):
        rel = p.relative_to(src).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            idx[".".join(parts)] = p
    return idx


def imports_of(path: Path, src: Path, idx: dict) -> set:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return set()
    rel = path.relative_to(src).with_suffix("")
    pkg = list(rel.parts[:-1]) if rel.parts[-1] != "__init__" else list(rel.parts[:-1])
    found = set()

    def add(name: str):
        while name:
            if name in idx:
                found.add(idx[name])
                return
            name = name.rpartition(".")[0]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                base_name = ".".join(base + ([node.module] if node.module else []))
            else:
                base_name = node.module or ""
            for a in node.names:
                add((base_name + "." + a.name) if base_name else a.name)
            add(base_name)
    return found


def closure(repo: Path, app: str) -> set:
    """Every file under src/ an app's running code can import, as repo-relative posix paths."""
    cfg = APPS[app]
    src = repo / "src"
    idx = module_index(src)
    todo = [repo / e for e in cfg["entries"]]
    for d in cfg["entry_dirs"]:
        todo += [p for p in (repo / d).rglob("*.py") if not p.name.startswith("selftest")]
    seen = set()
    while todo:
        p = todo.pop()
        if p in seen or not p.exists():
            continue
        seen.add(p)
        todo += [q for q in imports_of(p, src, idx) if q not in seen]
    return {p.relative_to(repo).as_posix() for p in seen}


def runtime_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return not (path.startswith(("tests/", "docs/", "output/", "tools/")) or name.startswith("selftest")
                or name.endswith(".md") and not path.startswith(("team/", "skills/")))


def apps_for(repo: Path, changed: list[str]) -> list[str]:
    out = []
    for app, cfg in APPS.items():
        if any(c.startswith(tuple(cfg["always"])) for c in changed):
            out.append(app)
            continue
        if cfg.get("image_only"):
            continue
        reach = closure(repo, app)
        if any(c in reach and runtime_file(c) for c in changed):
            out.append(app)
    return out


# ---------------------------------------------------------------------- the session
def prepare_worktree(sh: Shell) -> str:
    if not (WORKTREE / ".git").exists():
        WORKTREE.parent.mkdir(parents=True, exist_ok=True)
        rc, out = git(sh, "worktree", "add", "-B", WORK_BRANCH, str(WORKTREE), "origin/" + BRANCH, cwd=MAIN)
        if rc:
            raise RuntimeError("could not make the worktree: %s" % out[-300:])
    for args in (("fetch", "origin", BRANCH), ("checkout", "-B", WORK_BRANCH, "origin/" + BRANCH),
                 ("reset", "--hard", "origin/" + BRANCH), ("clean", "-fdq")):
        rc, out = git(sh, *args)
        if rc:
            raise RuntimeError("git %s: %s" % (args[0], out[-300:]))
    if (MAIN / ".env").exists():          # the workstation; on the fixer's box the keys are in the environment
        shutil.copyfile(MAIN / ".env", WORKTREE / ".env")
    return git(sh, "rev-parse", "HEAD")[1].strip()


def known_summary(led: dict, limit: int = 60) -> list[dict]:
    rows = [i for i in led["issues"].values() if i.get("class") or i.get("status") in ("fixed", "seen")]
    rows.sort(key=lambda i: float(i.get("last_ts") or 0), reverse=True)
    return [{"id": i["id"], "class": i.get("class", ""), "status": i.get("status", ""), "summary": i.get("summary", ""),
             "text": i["text"][:220]} for i in rows[:limit]]


def build_prompt(led: dict, ids: list[str], run_dir: Path, result: Path) -> str:
    play = (HERE / "PLAYBOOK.md").read_text(encoding="utf-8")
    play = play.replace("{PY}", PY.as_posix()).replace("{RUN_DIR}", run_dir.as_posix()).replace("{RESULT}", result.as_posix())
    new = []
    for i in ids:
        iss = led["issues"][i]
        row = {"id": i, "posted_by": iss.get("author"), "first_posted": _when(iss["first_ts"]),
               "times_seen": iss.get("seen"), "text": iss["text"]}
        if iss.get("status") == "regression":
            row["was_fixed"] = iss.get("fix") or {}
            row["note"] = "This was fixed and shipped, and posted again after the fix: the fix did not hold."
        new.append(row)
    return "%s\n\n## This run\nRun folder: %s\nResult file: %s\nNow: %s (America/New_York)\n\n## NEW\n%s\n\n## KNOWN\n%s\n" % (
        play, run_dir.as_posix(), result.as_posix(), now_utc().astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
        json.dumps(new, indent=1), json.dumps(known_summary(led), indent=1))


def _when(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def run_session(sh: Shell, st: Settings, prompt: str, run_dir: Path, budget: float) -> dict:
    (run_dir / "PROMPT.md").write_text(prompt, encoding="utf-8")
    cmd = [tool("claude"), "-p", "Read %s and do exactly what it says. Write the result file it names, then stop." %
           (run_dir / "PROMPT.md").as_posix(),
           "--output-format", "json", "--model", st.model, "--permission-mode", "acceptEdits",
           "--max-budget-usd", "%.2f" % budget, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "project", "--add-dir", run_dir.as_posix(), "--no-session-persistence",
           "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DENIED_TOOLS]
    rc, out = sh.run(cmd, cwd=WORKTREE, timeout=SESSION_TIMEOUT_S, env=plain_env())
    (run_dir / "session_output.txt").write_text(out, encoding="utf-8")
    meta: dict = {"rc": rc}
    try:
        meta.update(json.loads(out[out.index("{"):out.rindex("}") + 1]))
    except ValueError:
        meta["parse_error"] = out[-500:]
    return meta


def load_result(path: Path, ids: list[str]) -> tuple[dict, list[str]]:
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, ["the session wrote no readable result (%s)" % exc]
    out = {}
    for row in data.get("issues") or []:
        i = str(row.get("id") or "")
        if i not in ids:
            problems.append("the result names an issue it was not given: %r" % i)
            continue
        if row.get("class") not in CLASSES:
            problems.append("issue %s has no known class: %r" % (i, row.get("class")))
            row["class"] = "decision"
        out[i] = row
    for i in ids:
        if i not in out:
            problems.append("the result says nothing about issue %s" % i)
    return {"issues": out, "notes": data.get("notes", "")}, problems


# ---------------------------------------------------------------------- the gates
SELFTEST_RX = re.compile(r"(\d+) passed, (\d+) failed")


def dash_lines(diff: str) -> list[str]:
    bad = [chr(0x2014), chr(0x2013)]
    return [ln for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++") and any(b in ln for b in bad)]


def gates(sh: Shell, changed: list[str], cwd: Path = WORKTREE) -> tuple[bool, list[str]]:
    lines: list[str] = []
    rc, out = sh.run([PY, "src/agents/cli.py", "selftest"], cwd=cwd, timeout=3600)
    m = SELFTEST_RX.findall(out)
    passed, failed = (int(m[-1][0]), int(m[-1][1])) if m else (0, -1)
    lines.append("agents selftest: %d passed, %d failed" % (passed, failed) if m else "agents selftest: no result line (exit %d)" % rc)
    ok = rc == 0 and bool(m) and failed == 0
    rc, out = sh.run([PY, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"], cwd=cwd, timeout=3600)
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or ["(no output)"]
    lines.append("pytest: %s" % tail[0][:120])
    ok = ok and rc == 0
    if any(c.startswith("src/sms_agent/") or c == "src/model_registry.py" for c in changed):
        rc, out = sh.run([PY, "src/sms_agent/cli.py", "selftest"], cwd=cwd, timeout=1800)
        good = rc == 0 and "checks passed" in out and "checks failed" not in out
        lines.append("texting selftest: %s" % ("passed" if good else "FAILED"))
        ok = ok and good
    if any(c.startswith(("skills/", "plugins/", "dist/")) for c in changed):
        rc, out = sh.run([PY, "tools/build_skills.py", "--verify"], cwd=cwd, timeout=900)
        rc2, out2 = sh.run([PY, "tools/check_docs.py"], cwd=cwd, timeout=900)
        good = rc == 0 and "dist matches source" in out and rc2 == 0
        lines.append("skill packages: %s" % ("match their source" if good else "FAILED"))
        ok = ok and good
    rc, diff = git(sh, "diff", "origin/%s...HEAD" % BRANCH, cwd=cwd)
    bad = dash_lines(diff)
    if bad:
        lines.append("dashes: %d added line(s) carry an em or en dash" % len(bad))
        ok = False
    return ok, lines


# ---------------------------------------------------------------------- deploy
def box(sh: Shell, app: str, command: str, timeout: int = 300) -> tuple[int, str]:
    return sh.run([tool("fly"), "ssh", "console", "-a", app, "-C", command], timeout=timeout, env=plain_env())


def window(sh: Shell, app: str, now: datetime | None = None) -> tuple[bool, str]:
    """Is it a safe moment to restart this box?"""
    local = (now or now_utc()).astimezone(TZ)
    if app == "siftstack-ftm":
        minutes = local.hour * 60 + local.minute
        if 6 * 60 <= minutes < 9 * 60 + 15:
            return False, "the first-to-market run and its 8:00 digest run from 6:00 to 9:15"
        probe = ("python -c \"import os;me=str(os.getpid());n=[p for p in os.listdir('/proc') if p.isdigit() and p!=me "
                 "and (b'ftm_'+b'runner') in open('/proc/%s/cmdline'%p,'rb').read()];print('RUNNING',len(n))\"")
        rc, out = box(sh, app, probe)
        m = re.search(r"RUNNING (\d+)", out)
        if not m:
            return False, "could not tell whether a run is going on the box"
        if int(m.group(1)):
            return False, "a first-to-market run is going on the box"
    if app == "siftstack-agents":
        probe = ("python -c \"import sqlite3;c=sqlite3.connect('/data/agents.db');"
                 "print('PERSON',c.execute(\\\"select count(*) from tasks where status='running' and source!='schedule'\\\").fetchone()[0])\"")
        rc, out = box(sh, app, probe)
        m = re.search(r"PERSON (\d+)", out)
        if not m:
            return False, "could not tell whether a person's task is running"
        if int(m.group(1)):
            return False, "a task a person started is running"
    return True, ""


def current_image(sh: Shell, app: str) -> str:
    rc, out = sh.run([tool("fly"), "status", "-a", app, "--json"], timeout=120, env=plain_env())
    try:
        d = json.loads(out[out.index("{"):out.rindex("}") + 1])
        return str(((d.get("Machines") or [{}])[0].get("config") or {}).get("image") or "")
    except (ValueError, IndexError, AttributeError):
        return ""


def release_version(sh: Shell, app: str) -> str:
    rc, out = sh.run([tool("fly"), "releases", "-a", app, "--json"], timeout=120, env=plain_env())
    try:
        d = json.loads(out[out.index("["):out.rindex("]") + 1])
        return "v%s" % d[0]["Version"]
    except (ValueError, IndexError, KeyError, TypeError):
        return ""


def checks(sh: Shell, app: str) -> tuple[bool, list[str]]:
    """Each box's own post-deploy checks (the ones CLAUDE.md lists)."""
    if app == "siftstack-agents":
        rc, out = box(sh, app, "sh -c 'cd /app && python -m agents.sandbox --check 2>&1 | tail -1; "
                               "python src/agents/cli.py autopilot check --live 2>&1 | tail -1'", timeout=600)
        sandbox_ok = "all passed" in out
        presets_ok = bool(re.search(r"\b0 named by the playbook are missing", out))
        health = {}
        for _ in range(6):
            try:
                with urllib.request.urlopen("https://siftstack-agents.fly.dev/health", timeout=20) as r:
                    health = json.loads(r.read().decode("utf-8"))
                if health.get("worker_alive") and health.get("people_worker_alive"):
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(10)
        health_ok = bool(health.get("ok") and health.get("worker_alive") and health.get("people_worker_alive"))
        return sandbox_ok and presets_ok and health_ok, [
            "sandbox check %s" % ("passed" if sandbox_ok else "FAILED"),
            "presets %s" % ("all found" if presets_ok else "MISSING or unread"),
            "health %s" % ("ok, both workers up" if health_ok else "NOT ok")]
    if app == "siftstack-ftm":
        rc, out = box(sh, app, "sh -c 'cd /app && python src/ftm_runner.py --doctor 2>&1 | tail -25; "
                               "python src/ftm_health.py --selftest 2>&1 | tail -1; "
                               "python src/ftm_budget.py --selftest 2>&1 | tail -1'", timeout=600)
        doctor_ok = "stages beyond today's run: none" in out
        m = re.search(r"(\d+) of (\d+) passed", out)
        health_ok = bool(m and m.group(1) == m.group(2))
        budget_ok = "all passed" in out
        return doctor_ok and health_ok and budget_ok, [
            "doctor %s" % ("clean" if doctor_ok else "NOT clean"),
            "health selftest %s" % (m.group(0) if m else "no result"),
            "budget selftest %s" % ("passed" if budget_ok else "FAILED")]
    if app == "siftstack":
        try:
            with urllib.request.urlopen("https://siftstack.fly.dev/health", timeout=20) as r:
                health = json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return False, ["health did not answer (%s)" % str(exc)[:80]]
        ok = bool(health.get("ok", True)) and "phase" in health
        return ok, ["health phase %s, dry run %s" % (health.get("phase"), health.get("dry_run"))]
    return False, ["no checks known for %s" % app]


def sync_deploy_tree(sh: Shell, sha: str) -> None:
    if not (DEPLOY_TREE / ".git").exists():
        DEPLOY_TREE.parent.mkdir(parents=True, exist_ok=True)
        rc, out = git(sh, "worktree", "add", "--detach", str(DEPLOY_TREE), sha, cwd=MAIN)
        if rc:
            raise RuntimeError("could not make the deploy tree: %s" % out[-300:])
    for args in (("fetch", "origin", BRANCH), ("checkout", "-f", "--detach", sha), ("checkout", "-f", "--", ".")):
        rc, out = git(sh, *args, cwd=DEPLOY_TREE)
        if rc:
            raise RuntimeError("deploy tree git %s: %s" % (args[0], out[-300:]))
    crlf = [p for p in DEPLOY_TREE.rglob("*") if p.is_file() and ".git" not in p.parts
            and p.suffix in (".py", ".sh", ".toml", ".yaml", ".yml", ".json", ".txt") and b"\r\n" in p.read_bytes()]
    if crlf:
        raise RuntimeError("the deploy tree has Windows line endings in %d file(s), e.g. %s" % (len(crlf), crlf[0]))


def deploy(sh: Shell, app: str, sha: str) -> tuple[bool, list[str], str]:
    cfg = APPS[app]
    prev = current_image(sh, app)
    sync_deploy_tree(sh, sha)
    rc, out = sh.run([tool("fly"), "deploy", "-c", cfg["toml"], "--remote-only"], cwd=DEPLOY_TREE, timeout=2400,
                     env=plain_env())
    if rc:
        return False, ["the deploy failed: %s" % out.strip().splitlines()[-1][:200] if out.strip() else "the deploy failed"], ""
    ok, lines = checks(sh, app)
    version = release_version(sh, app)
    if ok:
        return True, lines, version
    lines.append("checks failed, going back to the previous image")
    if prev:
        rc, out = sh.run([tool("fly"), "deploy", "-c", cfg["toml"], "--image", prev], cwd=DEPLOY_TREE, timeout=1800,
                         env=plain_env())
        back_ok, back_lines = checks(sh, app) if rc == 0 else (False, ["the rollback deploy failed"])
        lines.append("rolled back to %s: %s" % (prev.rsplit(":", 1)[-1][:24], "checks pass" if back_ok else "; ".join(back_lines)))
    else:
        lines.append("the previous image was not known, so it was not rolled back")
    return False, lines, version


# ---------------------------------------------------------------------- shipping one fix
def changed_files(sh: Shell) -> list[str]:
    rc, out = git(sh, "diff", "--name-only", "origin/%s...HEAD" % BRANCH)
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []


def land(sh: Shell) -> tuple[bool, str, list[str]]:
    """Rebase the fix on the newest agents-infra, gate it, push it. Returns (ok, sha, lines)."""
    lines: list[str] = []
    for attempt in range(2):
        git(sh, "fetch", "origin", BRANCH)
        rc, out = git(sh, "rebase", "origin/" + BRANCH)
        if rc:
            git(sh, "rebase", "--abort")
            return False, "", ["the fix no longer applies on top of agents-infra (someone changed the same lines)"]
        changed = changed_files(sh)
        ok, glines = gates(sh, changed)
        lines = glines
        if not ok:
            return False, "", lines
        rc, out = git(sh, "push", "origin", "HEAD:" + BRANCH, timeout=300)
        if rc == 0:
            return True, git(sh, "rev-parse", "HEAD")[1].strip(), lines
        if "non-fast-forward" not in out and "rejected" not in out and "fetch first" not in out:
            return False, "", lines + ["the push failed: %s" % out.strip()[-200:]]
    return False, "", lines + ["agents-infra kept moving while the fix was tested; it waits for the next run"]


def keep_branch(sh: Shell, iid: str) -> str:
    name = "issue-fixer/%s" % iid
    rc, _ = git(sh, "push", "-f", "origin", "HEAD:refs/heads/%s" % name, timeout=300)
    return name if rc == 0 else ""


# ---------------------------------------------------------------------- the run
def spent_today(day: str) -> float:
    total = 0.0
    try:
        for line in (STATE / "runs.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("day") == day:
                total += float(row.get("usd") or 0)
    except (OSError, ValueError):
        pass
    return total


def record_run(row: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "runs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


class Lock:
    def __init__(self):
        self.path = STATE / "run.lock"

    def __enter__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                at = datetime.fromisoformat(json.loads(self.path.read_text(encoding="utf-8"))["at"])
                if now_utc() - at < LOCK_STALE:
                    raise RuntimeError("another run holds the lock since %s" % at.isoformat(timespec="seconds"))
            except (ValueError, KeyError):
                pass
        self.path.write_text(json.dumps({"pid": os.getpid(), "at": now_utc().isoformat()}), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


QUIET = False          # --quiet: every post is logged instead of sent (a first live test)


def thread_of(iss: dict) -> str:
    """The thread an answer goes in: the issue's newest post, so it sits under what people see today."""
    return str(iss.get("last_ts") or iss["first_ts"])


def say(slack, st: Settings, iss: dict, text: str, broadcast: bool = False, dry: bool = False) -> None:
    if dry or QUIET or not slack:
        log("would post on %s: %s" % (iss["id"], text.replace("\n", " | ")))
        return
    try:
        slack.post(st.channel, text, thread_ts=thread_of(iss), broadcast=broadcast)
    except Exception as exc:  # noqa: BLE001  (a reply that fails is logged; the ledger still moves on)
        log("could not post on %s: %s" % (iss["id"], exc))


def handle_approvals(sh: Shell, slack, st: Settings, led: dict, dry: bool) -> None:
    for iss in led["issues"].values():
        if iss.get("status") != "awaiting_ok" or not slack:
            continue
        asked_at = str(iss.get("asked_ts") or iss.get("first_ts"))
        oks = [r for r in slack.replies(st.channel, str(iss.get("asked_thread") or thread_of(iss)))
               if float(r.get("ts") or 0) > float(asked_at) and r.get("user") in st.approvers and OK_RX.match(str(r.get("text") or ""))]
        if not oks:
            continue
        what = iss.get("waiting_for") or {}
        if dry:
            log("would carry out the approved %s on %s" % (what.get("kind"), iss["id"]))
            continue
        if what.get("kind") == "account_change":
            script = Path(what["script"])
            if not script.exists() or hashlib.sha256(script.read_bytes()).hexdigest() != what.get("sha256"):
                say(slack, st, iss, "The change script moved since it was checked, so it was not run. A person should look at it.")
                iss["status"] = "needs_person"
                continue
            rc, out = sh.run([PY, script, "--commit"], cwd=MAIN, timeout=1800)
            tail = "\n".join(out.strip().splitlines()[-6:])
            iss["status"] = "fixed" if rc == 0 else "needs_person"
            iss["fixed_at"] = now_utc().isoformat()
            iss["history"].append({"at": now_utc().isoformat(), "what": "account change run, exit %d" % rc})
            say(slack, st, iss, ("Done. The account change ran:\n%s" if rc == 0 else
                                 "The account change failed, nothing more was tried:\n%s") % tail[:800], broadcast=True)
        elif what.get("kind") == "deploy":
            ship_apps(sh, slack, st, iss, what.get("sha"), [what["app"]], force=True)


def ship_apps(sh: Shell, slack, st: Settings, iss: dict, sha: str, apps: list[str], force: bool = False,
              dry: bool = False) -> None:
    results, waits, waiting, failed = [], [], [], []
    for app in apps:
        cfg = APPS[app]
        if not force and app not in st.auto_apps:
            waiting.append(app)
            continue
        ok, why = window(sh, app)
        if not ok:
            iss.setdefault("deploy_waiting", {})[app] = sha
            waits.append((app, "%s waits: %s" % (cfg["name"], why)))
            continue
        if dry:
            results.append("would deploy %s" % cfg["name"])
            continue
        good, lines, version = deploy(sh, app, sha)
        (results if good else failed).append("%s%s: %s" % (cfg["name"], (" " + version) if version else "", "; ".join(lines)))
        if good:
            (iss.get("deploy_waiting") or {}).pop(app, None)
    if waiting:
        iss["status"] = "awaiting_ok"
        iss["asked_ts"] = str(time.time())
        iss["asked_thread"] = thread_of(iss)
        iss["waiting_for"] = {"kind": "deploy", "app": waiting[0], "sha": sha}
        say(slack, st, iss, "The fix is in the code (%s). %s is not shipped on its own: a restart drops incoming texts "
                            "for about a minute. Reply ok in this thread to ship it." % (sha[:7], APPS[waiting[0]]["name"].capitalize()),
            dry=dry)
    if failed:
        iss["status"] = "needs_person"
        say(slack, st, iss, "The fix (%s) did not pass its checks on the box:\n%s" % (sha[:7], "\n".join(failed)), broadcast=True, dry=dry)
    elif (results or waits) and not waiting:
        if iss.get("deploy_waiting"):
            iss["status"] = "waiting_to_deploy"
        else:
            iss["status"] = "fixed"
            iss["fixed_at"] = now_utc().isoformat()
        # A box that waits for its window is said once per fix, in the thread, and the run that finds it
        # still closed says nothing (2026-11-20: one waiting deploy broadcast "Shipped" ten times from 6:49 to
        # 9:00). "Shipped" means a box took the fix, and is said when it does.
        told = iss.setdefault("said_wait", {})
        news = [text for app, text in waits if told.get(app) != sha]
        told.update({app: sha for app, _ in waits})
        later = " ".join(w.rstrip(".") + "." for w in news)
        if results:
            say(slack, st, iss, "Shipped (%s). %s%s" % (sha[:7], " ".join(r.rstrip(".") + "." for r in results),
                                                        (" " + later + " It deploys there at the next safe moment.") if news else ""),
                broadcast=True, dry=dry)
        elif news:
            say(slack, st, iss, "The fix (%s) is pushed and waits to deploy. %s It deploys at the next safe moment, and this "
                                "thread hears when it does." % (sha[:7], later), dry=dry)


def retry_waiting_deploys(sh: Shell, slack, st: Settings, led: dict, dry: bool) -> None:
    """Every run tries a deploy that waits for its window again; ship_apps says nothing while it still waits."""
    for iss in led["issues"].values():
        waiting = iss.get("deploy_waiting") or {}
        if iss.get("status") != "waiting_to_deploy" or not waiting:
            continue
        for app, sha in list(waiting.items()):
            ship_apps(sh, slack, st, iss, sha, [app], dry=dry)
        if iss.get("status") != "waiting_to_deploy":
            # The issues that shipped with this fix took its status when it began to wait; they move with it.
            for other in led["issues"].values():
                if other.get("fixed_with") == iss["id"] and other.get("status") == "waiting_to_deploy":
                    other["status"], other["fixed_at"] = iss["status"], iss.get("fixed_at")


# ---------------------------------------------------------------------- the public release
PUBLIC_TOOL = Path("tools") / "public_release.py"
RELEASE_TIMEOUT_S = 90 * 60          # build, every check inside the release, push: about 20 minutes on this box
RELEASE_TRIES = 3                    # a release that failed for no stated reason (a network drop) is tried again
RESULT_RX = re.compile(r"^RESULT (\w+)(.*)$", re.M)
BUILT_FROM_RX = re.compile(r"Built by tools/public_release\.py from ([0-9a-f]{7,40})")


def public_remote() -> str:
    return os.environ.get("PUBLIC_RELEASE_REMOTE") or "public"


def public_release_source(sh: Shell) -> str:
    """The agents-infra commit the newest public release was built from, read off public main's message."""
    rc, out = git(sh, "fetch", public_remote(), "main", cwd=MAIN, timeout=300)
    if rc:
        return ""
    rc, out = git(sh, "log", "-1", "--format=%B", "%s/main" % public_remote(), cwd=MAIN)
    m = BUILT_FROM_RX.search(out or "")
    return m.group(1) if (rc == 0 and m) else ""


def is_ancestor(sh: Shell, older: str, newer: str) -> bool:
    """True when `older` is in `newer`'s history (or is it). An unknown commit is not."""
    if not older or not newer:
        return False
    rc, _ = git(sh, "merge-base", "--is-ancestor", older, newer, cwd=MAIN)
    return rc == 0


def newest(sh: Shell, shas: list[str]) -> str:
    """The commit that carries all the others (agents-infra is one line of history)."""
    for c in shas:
        if all(o == c or is_ancestor(sh, o, c) for o in shas):
            return c
    return shas[-1]


def release_due(sh: Shell, slack, st: Settings, led: dict, force: bool = False, dry: bool = False) -> str:
    """Ty, 2026-11-24: "let's just do a release whenever a fix goes out". A fix has gone out when it is
    pushed and every box it reaches took it (status fixed). The newest such commit is published to public
    SiftStack (tools/public_release.py publish --push), which carries every fix before it, so each fix
    reaches users within the run that ships it. A fix the last public release already holds is marked and
    left. A release the scan stops (something private in the tree) is said once and waits for the next fix;
    any other failure is tried again on the next runs, three times, then said once. `force` releases
    agents-infra's tip with no fix due. Returns a line for the log ("" when there was nothing to do)."""
    if not st.release:
        return ""
    if not (MAIN / PUBLIC_TOOL).exists():
        return "no %s in %s, so no public release" % (PUBLIC_TOOL.as_posix(), MAIN)
    due = [i for i in led["issues"].values()
           if i.get("status") == "fixed" and (i.get("fix") or {}).get("commit") and not i.get("released_in")]
    if not due and not force:
        return ""
    source = public_release_source(sh)
    if not source:
        return "could not read public main (the %s remote), so no release this run" % public_remote()
    waiting = []
    for iss in due:
        if is_ancestor(sh, iss["fix"]["commit"], source):
            iss["released_in"] = "built from %s" % source[:12]      # an earlier release already carried it
        else:
            waiting.append(iss)
    if force:
        git(sh, "fetch", "origin", BRANCH, cwd=MAIN)
        target = git(sh, "rev-parse", "origin/" + BRANCH, cwd=MAIN)[1].strip()
    elif waiting:
        target = newest(sh, [i["fix"]["commit"] for i in waiting])
    else:
        return "every shipped fix is already in public main (built from %s)" % source[:12]
    if is_ancestor(sh, target, source):
        return "public main already carries %s (built from %s)" % (target[:7], source[:12])
    rel = led.setdefault("release", {})
    failed = rel.get("failed") or {}
    if not force and failed.get("sha") == target and (failed.get("said") or int(failed.get("tries") or 0) >= RELEASE_TRIES):
        return "the release of %s stopped before (%s); it waits for the next fix" % (target[:7], failed.get("why", ""))
    if dry:
        return "would release %s to public main (%d fix(es) waiting)" % (target[:7], len(waiting))
    env = {k: v for k, v in plain_env().items() if k != "FLY_API_TOKEN"}
    rc, out = sh.run([PY, MAIN / PUBLIC_TOOL, "publish", "--push", "--ref", target, "--python", PY], cwd=MAIN,
                     timeout=RELEASE_TIMEOUT_S, env=env)
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "release.log", "a", encoding="utf-8") as fh:
        fh.write("==== %s release of %s, exit %d\n%s\n" % (now_utc().isoformat(timespec="seconds"), target[:12], rc,
                                                           "\n".join(out.strip().splitlines()[-80:])))
    results = RESULT_RX.findall(out or "")
    kind, rest = (results[-1][0], results[-1][1].strip(" :")) if results else ("", "")
    carried = [i for i in waiting if is_ancestor(sh, i["fix"]["commit"], target)]
    if rc == 0 and kind in ("released", "unchanged"):
        public_sha = (rest.split() or [""])[0]
        for iss in carried:
            iss["released_in"] = public_sha
            say(slack, st, iss, "Public SiftStack has this fix now (release %s)." % public_sha[:7])
        rel.pop("failed", None)
        rel["last"] = {"sha": target, "public": public_sha, "at": now_utc().isoformat(), "fixes": [i["id"] for i in carried]}
        return "released %s to public main as %s" % (target[:7], public_sha[:7])
    why = rest if kind == "stopped" else ((out or "").strip().splitlines() or ["no output"])[-1][:200]
    tries = (int(failed.get("tries") or 0) if failed.get("sha") == target else 0) + 1
    final = (kind == "stopped" and why.startswith("the scan found")) or tries >= RELEASE_TRIES
    rel["failed"] = {"sha": target, "why": why, "tries": tries, "at": now_utc().isoformat(), "said": final}
    if final and carried:
        newest_iss = max(carried, key=lambda i: float(i.get("last_ts") or 0))
        say(slack, st, newest_iss, "This fix is live on our boxes, but the public release stopped: %s. A person needs to "
                                   "look at it; public SiftStack gets it with the next release that passes." % why,
            broadcast=True)
    return "the release of %s failed (try %d): %s" % (target[:7], tries, why)


def publish_fixes(sh: Shell, slack, st: Settings, led: dict) -> None:
    """After every run: what went out goes public (release_due). A release that crashes costs that release,
    never the run's ledger."""
    try:
        what = release_due(sh, slack, st, led)
        if what:
            log(what)
    except Exception as exc:  # noqa: BLE001
        log("the public release crashed: %s\n%s" % (exc, traceback.format_exc()[-800:]))
    save_ledger(led)


def release_cmd(args, st: Settings | None = None, sh: Shell | None = None, slack=None) -> int:
    """`fixer.py release [--force] [--dry]`: the same step by hand, under the run's lock."""
    st = st or Settings()
    sh = sh or Shell()
    if slack is None and st.token and st.channel:
        slack = Slack(st.token, st.icon)
    with Lock():
        if os.environ.get("ISSUE_FIXER_OWN_CLONE") == "1" and not refresh_clone(sh):
            return 1
        led = load_ledger()
        what = release_due(sh, slack, st, led, force=args.force, dry=args.dry)
        if not args.dry:
            save_ledger(led)
    log(what or "nothing to release")
    return 0


def handover(sh: Shell, app: str) -> bool:
    """The workstation hands the fixer to its own box once the box can do the work: the box holds Ty's
    Claude sign-in (the secret CLAUDE_CODE_OAUTH_TOKEN) and Claude Code answers there. Then the ledger is
    copied over, the box is switched on, and this machine switches its fixer and its scheduled task off,
    so the two never answer the same issue. True when the box has taken over."""
    rc, out = sh.run([tool("fly"), "secrets", "list", "-a", app, "--json"], timeout=120, env=plain_env())
    try:
        rows = json.loads(out[out.index("["):out.rindex("]") + 1])
        status = {str(x.get("name") or x.get("Name")): str(x.get("status") or x.get("Status") or "") for x in rows}
    except ValueError:
        return False
    if "CLAUDE_CODE_OAUTH_TOKEN" not in status:
        return False
    if status["CLAUDE_CODE_OAUTH_TOKEN"].lower() != "deployed":
        # Saved staged: the running box does not hold it until the staged secrets are deployed. The box is
        # still switched off, so the restart this causes costs nothing.
        rc, out = sh.run([tool("fly"), "secrets", "deploy", "-a", app], timeout=900, env=plain_env())
        if rc:
            log("could not apply the box's staged secrets (%s); this machine keeps the work" % out.strip()[-160:])
            return False
        time.sleep(HANDOVER_SETTLE_S)
    rc, out = box(sh, app, "claude -p 'Reply with the single word ok.' --output-format json", timeout=300)
    if rc or '"is_error":false' not in out.replace(" ", "") or "ok" not in out.lower():
        log("the box has the Claude sign-in, but Claude Code did not answer there yet; this machine keeps the work")
        return False
    for name in ("ledger.json", "runs.jsonl"):
        src = STATE / name
        if src.exists():
            rc, out = sh.run([tool("fly"), "ssh", "sftp", "put", str(src), "/data/state/%s" % name, "-a", app],
                             timeout=300, env=plain_env())
            if rc and "already exists" not in out:
                log("could not copy %s to the box (%s); this machine keeps the work" % (name, out.strip()[-160:]))
                return False
    rc, out = sh.run([tool("fly"), "secrets", "set", "ISSUE_FIXER=on", "-a", app], timeout=600, env=plain_env())
    if rc:
        log("could not switch the box on (%s); this machine keeps the work" % out.strip()[-160:])
        return False
    (STATE / "OFF").write_text("handed over to %s at %s" % (app, now_utc().isoformat()), encoding="utf-8")
    sh.run(["schtasks", "/change", "/tn", "SiftStack Issue Fixer", "/disable"], timeout=60)
    log("handed the fixer over to %s: the ledger is there, the box is on, this machine's fixer is off" % app)
    return True


def run(args, st: Settings | None = None, sh: Shell | None = None, slack=None) -> int:
    st = st or Settings()
    sh = sh or Shell()
    if not st.on or (STATE / "OFF").exists():
        log("the issue fixer is off")
        return 0
    target = st.env.get("ISSUE_FIXER_HANDOVER_TO", "")
    if target and os.environ.get("ISSUE_FIXER_OWN_CLONE") != "1" and not args.dry and handover(sh, target):
        return 0
    if not (st.token and st.channel):
        log("no Slack token or issues channel in .env; nothing to read")
        return 2
    slack = slack or Slack(st.token, st.icon)
    with Lock():
        led = load_ledger()
        if not led.get("seeded"):
            log("not seeded yet: run `fixer.py seed` first, so what is already in the channel is not worked again")
            return 2
        rc = work(args, st, sh, slack, led)
        if not args.dry and not getattr(args, "no_ship", False):
            publish_fixes(sh, slack, st, led)
    return rc


def work(args, st: Settings, sh: Shell, slack, led: dict) -> int:
    """One run's work on the ledger, inside run()'s lock: read the channel, carry out approvals, retry
    waiting deploys, hand the new issues to a session, then sort, fix and ship what it found."""
    for iss in led["issues"].values():
        if iss.get("status") == "working":     # a run that died mid-session (this run holds the lock)
            iss["status"] = "new"
    posts = slack.history(st.channel, str(led.get("last_ts") or ""))
    need = fold(led, posts)
    need += [i for i, iss in led["issues"].items() if iss.get("status") in ("new", "regression") and i not in need]
    log("read %d post(s); %d issue(s) need a look" % (len(posts), len(need)))
    handle_approvals(sh, slack, st, led, args.dry)
    retry_waiting_deploys(sh, slack, st, led, args.dry)
    if not args.dry:
        save_ledger(led)
    if not need:
        return 0
    day = local_day()
    left = st.daily_usd - spent_today(day)
    if left < 1.0:
        log("the day's budget is spent ($%.2f of $%.2f); %d issue(s) wait" % (st.daily_usd - left, st.daily_usd, len(need)))
        return 0
    ids = need[:MAX_NEW_PER_SESSION]
    if args.dry:
        for i in ids:
            log("would work on %s: %s" % (i, led["issues"][i]["text"][:160].replace("\n", " | ")))
        return 0
    run_id = now_utc().astimezone(TZ).strftime("%Y%m%d_%H%M%S")
    run_dir = STATE / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "RESULT.json"
    for i in ids:
        led["issues"][i]["status"] = "working"
    save_ledger(led)
    base = prepare_worktree(sh)
    try:
        meta = run_session(sh, st, build_prompt(led, ids, run_dir, result_path), run_dir, min(st.session_usd, left))
    finally:
        try:
            (WORKTREE / ".env").unlink()
        except OSError:
            pass
    usd = float(meta.get("total_cost_usd") or 0)
    record_run({"run": run_id, "day": day, "usd": usd, "issues": ids, "rc": meta.get("rc"),
                "is_error": meta.get("is_error"), "denials": len(meta.get("permission_denials") or [])})
    result, problems = load_result(result_path, ids)
    for p in problems:
        log("result: %s" % p)
    rows = (result or {}).get("issues") or {}
    fixed_ids = [i for i, r in rows.items() if r.get("class") == "bug" and r.get("fixed") and r.get("commits")]
    for i in ids:
        iss, row = led["issues"][i], rows.get(i)
        if not row:
            iss["status"] = "new"            # the next session looks again
            continue
        iss.update({"class": row["class"], "summary": row.get("summary", ""), "evidence": row.get("evidence", "")})
        iss["history"].append({"at": now_utc().isoformat(), "what": "sorted as %s" % row["class"], "run": run_id})
        if row["class"] == "bug" and i in fixed_ids:
            continue                          # shipped below
        if row.get("account_change"):
            stage_account_change(sh, slack, st, iss, row, args.dry)
        elif row["class"] == "bug":
            # Not fixed this session (one bug a session, or it ran out): the next session takes it, twice
            # more at most, and the thread hears once now and once if it is handed to a person.
            if not row.get("deferred"):
                iss["attempts"] = int(iss.get("attempts") or 0) + 1
            if not iss.get("said_bug"):
                iss["said_bug"] = True
                say(slack, st, iss, (row.get("reply") or row.get("summary") or "This looks like a bug.")
                    + "\nIt is in line for a fix.")
            if int(iss.get("attempts") or 0) >= 3:
                iss["status"] = "needs_person"
                say(slack, st, iss, "Three tries did not fix this, so it needs a person. What was found: %s" % (
                    row.get("summary") or row.get("evidence") or "see the fixer's run folder"), broadcast=True)
            else:
                iss["status"] = "new"
        elif row["class"] == "duplicate":
            iss["status"] = "closed"
            iss["duplicate_of"] = row.get("duplicate_of", "")
        else:
            iss["status"] = "for_people" if row["class"] in OPEN_FOR_PEOPLE else "closed"
            say(slack, st, iss, row.get("reply") or row.get("summary") or "")
    save_ledger(led)
    if fixed_ids and args.no_ship:
        for i in fixed_ids:
            led["issues"][i]["status"] = "new"
            led["issues"][i]["history"].append({"at": now_utc().isoformat(), "what": "fixed in the session, not shipped (--no-ship)"})
    elif fixed_ids:
        ship_fixes(sh, slack, st, led, rows, fixed_ids, base, run_id)
    save_ledger(led)
    log("run %s done: $%.2f, %d sorted, %d fixed" % (run_id, usd, len(rows), len(fixed_ids)))
    return 0


def ship_fixes(sh: Shell, slack, st: Settings, led: dict, rows: dict, fixed_ids: list[str], base: str, run_id: str) -> None:
    """Everything the session fixed lands, is tested and ships together: the commits share one branch."""
    first = led["issues"][fixed_ids[0]]
    ok, sha, lines = land(sh)
    if not ok:
        branch = keep_branch(sh, fixed_ids[0])
        for i in fixed_ids:
            iss, row = led["issues"][i], rows[i]
            iss["status"] = "needs_person"
            say(slack, st, iss, "%s\nI wrote a fix, but it did not ship:\n%s%s" % (
                row.get("reply") or row.get("summary") or "", "\n".join(lines),
                ("\nThe change is kept on the branch %s." % branch) if branch else ""), broadcast=(iss is first))
        return
    apps = apps_for(WORKTREE, changed_since(sh, base, sha))
    for i in fixed_ids:
        iss, row = led["issues"][i], rows[i]
        iss["fix"] = {"commit": sha, "run": run_id, "tests": lines, "apps": apps}
        note = row.get("reply") or row.get("summary") or ""
        if not apps:
            iss["status"] = "fixed"
            iss["fixed_at"] = now_utc().isoformat()
            say(slack, st, iss, "%s\nFixed in %s (no server needed a deploy). %s" % (note, sha[:7], "; ".join(lines)),
                broadcast=True)
            continue
        if iss is not first:
            iss["status"] = "fixed_with"
            iss["fixed_with"] = fixed_ids[0]
            say(slack, st, iss, "%s\nFixed in %s, shipping with issue %s." % (note, sha[:7], fixed_ids[0]))
    if apps:
        say(slack, st, first, "%s\nFix %s passed every test (%s). Deploying now." % (
            rows[fixed_ids[0]].get("reply") or rows[fixed_ids[0]].get("summary") or "", sha[:7], "; ".join(lines)))
        ship_apps(sh, slack, st, first, sha, apps)
        for i in fixed_ids[1:]:
            iss = led["issues"][i]
            iss["status"], iss["fixed_at"] = first["status"], first.get("fixed_at")


def changed_since(sh: Shell, base: str, sha: str) -> list[str]:
    rc, out = git(sh, "diff", "--name-only", "%s..%s" % (base, sha))
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []


def stage_account_change(sh: Shell, slack, st: Settings, iss: dict, row: dict, dry: bool) -> None:
    ac = row["account_change"] or {}
    script = Path(str(ac.get("script") or ""))
    if not script.is_absolute():
        script = WORKTREE / script
    if not script.exists() or STATE.resolve() not in script.resolve().parents:
        iss["status"] = "needs_person"
        say(slack, st, iss, "%s\nAn account change would fix it, but its script is not in the fixer's run folder, so it "
                            "was not staged." % (row.get("reply") or ""), dry=dry)
        return
    rc, out = sh.run([PY, script], cwd=MAIN, timeout=900)
    tail = "\n".join(out.strip().splitlines()[-8:])
    if rc:
        iss["status"] = "needs_person"
        say(slack, st, iss, "%s\nThe account change failed its dry run, so it was not staged:\n%s" % (row.get("reply") or "", tail[:600]), dry=dry)
        return
    iss["status"] = "awaiting_ok"
    iss["asked_ts"] = str(time.time())
    iss["asked_thread"] = thread_of(iss)
    iss["waiting_for"] = {"kind": "account_change", "script": str(script),
                          "sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "what": ac.get("what", "")}
    say(slack, st, iss, "%s\nThe fix is an account change: %s\nDry run:\n%s\nReply ok in this thread to apply it." % (
        row.get("reply") or "", ac.get("what", ""), tail[:700]), dry=dry)


# ---------------------------------------------------------------------- seed, status, switches
SEED_VERDICTS = HERE / "seed_verdicts.json"


def seed(args, st: Settings | None = None, slack=None) -> int:
    st = st or Settings()
    slack = slack or Slack(st.token, st.icon)
    led = load_ledger()
    oldest = str((now_utc() - timedelta(hours=args.hours)).timestamp())
    posts = slack.history(st.channel, oldest)
    fold(led, posts, seen_only=True)
    n = 0
    if SEED_VERDICTS.exists():
        n = apply_seed_verdicts(led, json.loads(SEED_VERDICTS.read_text(encoding="utf-8")))
    led["seeded"] = True
    if not led.get("last_ts"):
        led["last_ts"] = str(now_utc().timestamp())
    save_ledger(led)
    log("seeded from %d post(s): %d issue shape(s), %d with a first-pass verdict" % (len(posts), len(led["issues"]), n))
    return 0


def status(_args) -> int:
    led = load_ledger()
    from collections import Counter
    c = Counter(i.get("status") for i in led["issues"].values())
    print("issues by status:", dict(c))
    print("spent today: $%.2f" % spent_today(local_day()))
    print("off" if (STATE / "OFF").exists() else "on")
    for i in sorted(led["issues"].values(), key=lambda x: float(x.get("last_ts") or 0), reverse=True)[:12]:
        print("  %s %-16s %-10s x%-3s %s" % (i["id"], i.get("status"), i.get("class") or "-", i.get("seen"),
                                            i["text"][:90].replace("\n", " | ")))
    return 0


def switch(on: bool) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    if on:
        try:
            (STATE / "OFF").unlink()
        except OSError:
            pass
    else:
        (STATE / "OFF").write_text(now_utc().isoformat(), encoding="utf-8")
    print("the issue fixer is", "on" if on else "off")
    return 0


def next_quarter(t: datetime) -> datetime:
    """The next quarter hour after t: a run every 15 minutes, on the clock."""
    base = t.replace(second=0, microsecond=0)
    return base + timedelta(minutes=15 - base.minute % 15)


def refresh_clone(sh: Shell) -> bool:
    """The fixer's own clone on its box, which no person edits, put back to agents-infra before a run.
    Never called on the workstation, where MAIN is Ty's own checkout."""
    for args in (("fetch", "origin", BRANCH), ("checkout", "-B", BRANCH, "origin/" + BRANCH),
                 ("reset", "--hard", "origin/" + BRANCH)):
        rc, out = git(sh, *args, cwd=MAIN)
        if rc:
            log("could not refresh the clone (git %s): %s" % (args[0], out.strip()[-200:]))
            return False
    return True


def start_watch():
    """The watch (watch.py): the other boxes' outside checks on time and their logs read, in its own thread,
    so a fixer run that takes an hour never delays a check. A watch that cannot start is logged, and the
    fixer runs on."""
    try:
        import watch  # noqa: PLC0415  (its own module; it imports this one)
        return watch.start()
    except Exception as exc:  # noqa: BLE001
        log("the watch did not start: %s" % str(exc)[:300])
        return None


def loop(_args=None, sh: Shell | None = None, once: bool = False, runner=None) -> int:
    """The fixer's own box: a run in a fresh process at every quarter hour, for as long as the box is up.
    A run that crashes or hangs costs that run, never the loop."""
    sh = sh or Shell()
    runner = runner or (lambda: subprocess.call([sys.executable, "-u", str(Path(__file__).resolve()), "run"],
                                                timeout=3 * 3600))
    if not once:
        start_watch()
    while True:
        if os.environ.get("ISSUE_FIXER_OWN_CLONE") == "1":
            refresh_clone(sh)
        try:
            rc = runner()
        except subprocess.TimeoutExpired:
            log("a run went past three hours and was stopped")
            rc = 124
        if once:
            return rc
        time.sleep(max(30.0, (next_quarter(now_utc()) - now_utc()).total_seconds()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The SiftStack issue fixer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry", action="store_true", help="read and sort nothing for real: no session, no post, no ship")
    r.add_argument("--no-ship", action="store_true", help="run the session, but push and deploy nothing")
    r.add_argument("--quiet", action="store_true", help="post nothing to Slack; log what would be posted")
    s = sub.add_parser("seed")
    s.add_argument("--hours", type=float, default=72)
    sub.add_parser("status")
    sub.add_parser("off")
    sub.add_parser("on")
    sub.add_parser("loop", help="the fixer's own box: a run at every quarter hour")
    rl = sub.add_parser("release", help="publish the newest shipped fix to public SiftStack now")
    rl.add_argument("--force", action="store_true", help="release agents-infra's tip even with no fix waiting")
    rl.add_argument("--dry", action="store_true", help="say what would be released, release nothing")
    args = ap.parse_args(argv)
    global QUIET
    QUIET = bool(getattr(args, "quiet", False))
    try:
        if args.cmd == "loop":
            return loop(args)
        if args.cmd == "run":
            return run(args)
        if args.cmd == "release":
            return release_cmd(args)
        if args.cmd == "seed":
            return seed(args)
        if args.cmd == "status":
            return status(args)
        return switch(args.cmd == "on")
    except Exception as exc:  # noqa: BLE001  (a crash is logged with its cause, and the next run starts clean)
        log("crashed: %s\n%s" % (exc, traceback.format_exc()[-1500:]))
        return 1


if __name__ == "__main__":
    sys.exit(main())
