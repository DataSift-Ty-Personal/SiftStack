"""Move the cold email jobs from the PC to the agents brain in one step, or back. Dry by default.

    python deploy/cold_email_cutover.py                    # the plan: every step and command, nothing run
    python deploy/cold_email_cutover.py --check            # read only: is the box ready (secrets, code, no state yet)
    python deploy/cold_email_cutover.py --commit           # do it
    python deploy/cold_email_cutover.py --commit --set-live   # ...and add the top-up to AGENTS_LIVE_STAGES (restarts the box)
    python deploy/cold_email_cutover.py --verify           # read only: the box's reply watcher has run since
    python deploy/cold_email_cutover.py --back [--commit]  # undo: the PC runs cold email again

Run it from the checkout whose output/cold_email holds the live state (the PC's), or pass --state-dir.

The order is the point: each job stops on the PC in the same step it starts on the box, because two
reply watchers post every hand raise twice and two top-ups race the ledger.
  1. preflight (read only): the state here, this PC is still the home, no top-up running here, the
     box has the cold email code and its secrets, and the box holds no cold email state yet
  2. stop the PC: disable the four scheduled tasks, wait out a run in progress (the task's own status
     and the reply and top-up locks), then name the box the home in the PC's own state folder, so a run
     by hand on the PC refuses from here on (coldstate.require)
  3. copy the state to the box: into /data/cold_email/incoming, checked by hash, then moved into place,
     and the box named the home there LAST, so nothing acts on half a copy
  4. the top-up loads people into a campaign that emails them (outreach), so on the box's free mode it
     acts only once its id is in AGENTS_LIVE_STAGES: the exact command is printed with the stages already
     there kept (--set-live runs it; it restarts the box)
After step 3 the replies, health and stats stages act at their next slot: they spend nothing. A failure
in step 3 puts step 2 back (the PC is the home again and its tasks are on), so replies are never left
with nobody reading them.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "cold_email"))
import coldstate  # noqa: E402

APP = "siftstack-agents"
REMOTE = "/data/cold_email"
TASKS = ("SiftStack Cold Email Replies", "SiftStack Cold Email Top-up", "SiftStack Cold Email Health",
         "SiftStack Cold Email Stats")
REQUIRED_FILES = ("ledger.json", "replies_state.json")
OPTIONAL_FILES = ("rejected.json", "replies.jsonl", "hold.json", "daily_state.json")
SECRETS = ("INSTANTLY_API_KEY", "ZEROBOUNCE_API_KEY")
WEBHOOKS = ("SMS_AGENT_SLACK_WEBHOOK", "COLD_EMAIL_SLACK_WEBHOOK")    # either one carries the hand raises
STAGES = ("cold_email_replies", "cold_email_topup", "cold_email_health", "cold_email_stats")
LIVE_STAGE = "cold_email_topup"
WAIT_RUNNING_S = 600


class Refused(RuntimeError):
    """Preflight said no: nothing was changed."""


class Shell:
    """Runs a command (schtasks, fly). fly is run without the FLY_API_TOKEN a .env may carry (it is scoped
    to another app) and with MSYS_NO_PATHCONV=1, so a remote /data path is never rewritten on Windows."""

    def __init__(self, echo=print):
        self.echo = echo

    def run(self, args: list, timeout: int = 300) -> tuple[int, str]:
        env = dict(os.environ)
        env.pop("FLY_API_TOKEN", None)
        env["MSYS_NO_PATHCONV"] = "1"
        try:
            p = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout, env=env)
        except FileNotFoundError as exc:
            return 127, "not found: %s" % exc
        except subprocess.TimeoutExpired:
            return 124, "timed out after %ds" % timeout
        return p.returncode, (p.stdout or "") + (p.stderr or "")


# ---------------------------------------------------------------------- commands, as lists
def remote_py(code: str, app: str = APP) -> list:
    """A Python snippet run on the box. Sent base64 so no quote or newline in it meets a shell."""
    b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    return ["fly", "ssh", "console", "-a", app, "-C", 'python -c "import base64;exec(base64.b64decode(\'%s\'))"' % b64]


def disable(task: str) -> list:
    return ["schtasks", "/Change", "/TN", task, "/DISABLE"]


def enable(task: str) -> list:
    return ["schtasks", "/Change", "/TN", task, "/ENABLE"]


def query(task: str) -> list:
    return ["schtasks", "/Query", "/TN", task, "/FO", "CSV", "/NH"]


def put(local: Path, remote: str, app: str = APP) -> list:
    return ["fly", "ssh", "sftp", "put", str(local), remote, "-a", app]


def get(remote: str, local: Path, app: str = APP) -> list:
    return ["fly", "ssh", "sftp", "get", remote, str(local), "-a", app]


def secrets_list(app: str = APP) -> list:
    return ["fly", "secrets", "list", "-a", app, "--json"]


BOX_READ = """
import json, os
d = %(remote)r
def j(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
home = j(os.path.join(d, "home.json"))
hb = j(os.path.join(d, "replies_heartbeat.json"))
print(json.dumps({"code": os.path.exists("/app/src/cold_email/coldstate.py") and os.path.exists("/app/src/agents/autopilot/coldemail.py"),
                  "home": (home or {}).get("home", ""), "files": sorted(f for f in (os.listdir(d) if os.path.isdir(d) else []) if f.endswith((".json", ".jsonl"))),
                  "heartbeat": hb or {}, "app": os.environ.get("FLY_APP_NAME", ""),
                  "live_stages": os.environ.get("AGENTS_LIVE_STAGES", ""), "data_dir": os.environ.get("AGENTS_DATA_DIR", "")}))
"""

BOX_CLEAN_INCOMING = """
import os
d = os.path.join(%(remote)r, "incoming")
os.makedirs(d, exist_ok=True)
for f in os.listdir(d):
    os.remove(os.path.join(d, f))
print("CLEAN")
"""

BOX_INSTALL = """
import hashlib, json, os
from datetime import datetime, timezone
d = %(remote)r
inc = os.path.join(d, "incoming")
want = %(want)r
for name, digest in want.items():
    got = hashlib.sha256(open(os.path.join(inc, name), "rb").read()).hexdigest()
    if got != digest:
        raise SystemExit("HASH DIFFERS: " + name)
for name in want:
    os.replace(os.path.join(inc, name), os.path.join(d, name))
home = {"home": %(home)r, "since": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": "cold_email_cutover", "note": %(note)r}
tmp = os.path.join(d, "home.json.tmp")
open(tmp, "w", encoding="utf-8").write(json.dumps(home, indent=1))
os.replace(tmp, os.path.join(d, "home.json"))
print("INSTALLED " + str(len(want)))
"""

BOX_CLAIM = """
import json, os
from datetime import datetime, timezone
d = %(remote)r
os.makedirs(d, exist_ok=True)
home = {"home": %(home)r, "since": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": "cold_email_cutover", "note": %(note)r}
tmp = os.path.join(d, "home.json.tmp")
open(tmp, "w", encoding="utf-8").write(json.dumps(home, indent=1))
os.replace(tmp, os.path.join(d, "home.json"))
print("CLAIMED " + home["home"])
"""


def sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _json_tail(out: str) -> dict:
    """The last line of a command's output that is a JSON object (fly prints its own lines first)."""
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def merged_live(current: str, add: str = LIVE_STAGE) -> str:
    ids = [s.strip() for s in (current or "").split(",") if s.strip()]
    if add not in ids:
        ids.append(add)
    return ",".join(ids)


def without_live(current: str, drop: str = LIVE_STAGE) -> str:
    return ",".join(s.strip() for s in (current or "").split(",") if s.strip() and s.strip() != drop)


# ---------------------------------------------------------------------- the cutover
class Cutover:
    def __init__(self, state: Path, shell=None, app: str = APP, echo=print, sleep=time.sleep, wait_s: int = WAIT_RUNNING_S):
        self.state, self.app, self.echo, self.sleep, self.wait_s = Path(state), app, echo, sleep, wait_s
        self.shell = shell or Shell(echo)
        self.done: list[str] = []

    # ------------------------------------------------------------------ read only
    def local_files(self) -> list[Path]:
        missing = [n for n in REQUIRED_FILES if not (self.state / n).is_file()]
        if missing:
            raise Refused("the state folder %s has no %s: this is not the install that ran cold email"
                          % (self.state, " or ".join(missing)))
        return [self.state / n for n in REQUIRED_FILES + OPTIONAL_FILES if (self.state / n).is_file()]

    def box(self) -> dict:
        code, out = self.shell.run(remote_py(BOX_READ % {"remote": REMOTE}, self.app), timeout=120)
        info = _json_tail(out)
        if code != 0 or not info:
            raise Refused("could not read the box (%s): %s" % (self.app, out.strip()[-300:]))
        return info

    def box_secrets(self) -> set:
        code, out = self.shell.run(secrets_list(self.app), timeout=120)
        if code != 0:
            raise Refused("could not list the box's secrets: %s" % out.strip()[-300:])
        try:
            start = out.index("[")
            rows = json.loads(out[start:])
        except ValueError:
            raise Refused("could not read the box's secret list")
        return {str(r.get("Name") or r.get("name") or "") for r in rows if isinstance(r, dict)}

    def task_running(self, task: str) -> bool:
        code, out = self.shell.run(query(task), timeout=60)
        return code == 0 and '"Running"' in out

    def preflight(self, replace_box_state: bool = False, old_pc_code: bool = False) -> dict:
        files = self.local_files()
        ok, why = coldstate.owns(self.state)
        if not ok:
            raise Refused("this PC is not cold email's home any more (%s): nothing to move" % why)
        # The Windows tasks run the checkout that holds this state (<repo>/output/cold_email). Without the home
        # rule there, a run by hand after the cutover would still act and post every hand raise a second time.
        repo = self.state.parent.parent
        if (repo / "src" / "cold_email").is_dir() and not (repo / "src" / "cold_email" / "coldstate.py").is_file() \
                and not old_pc_code:
            raise Refused("the checkout at %s runs cold email code without the home rule: update it (git pull) first, "
                          "so a run by hand there refuses after the cutover (--old-pc-code goes on anyway)" % repo)
        top = coldstate.RunLock("topup", self.state)
        if not top.acquire():
            raise Refused("a top-up is running on this PC; run the cutover once it finishes")
        top.release()
        if self.task_running(TASKS[1]):
            raise Refused("the scheduled top-up is running on this PC; run the cutover once it finishes")
        names = self.box_secrets()
        missing = [s for s in SECRETS if s not in names] + ([] if any(w in names for w in WEBHOOKS) else [WEBHOOKS[0]])
        if missing:
            raise Refused("the box is missing %s. Stage them with python deploy/sync_agents_secrets.py --commit, then "
                          "deploy (fly deploy -c fly.agents.toml)" % ", ".join(missing))
        info = self.box()
        if not info.get("code"):
            raise Refused("the box does not have the cold email stages yet: deploy this code first")
        if info.get("home") and info.get("home") != coldstate.WORKSTATION:
            raise Refused("the box already names %s as cold email's home" % info["home"])
        there = [f for f in info.get("files") or [] if f in REQUIRED_FILES]
        if there and not replace_box_state:
            raise Refused("the box already holds %s in %s: a cutover would overwrite it. Look before going on "
                          "(after a --back, the PC's copy is the newer one: --replace-box-state)" % (", ".join(there), REMOTE))
        return {"files": files, "box": info}

    # ------------------------------------------------------------------ the steps
    def stop_pc(self) -> None:
        for t in TASKS:
            code, out = self.shell.run(disable(t), timeout=60)
            if code != 0:
                raise RuntimeError("could not disable %r: %s" % (t, out.strip()[-200:]))
            self.done.append("disabled " + t)
        waited = 0
        while any(self.task_running(t) for t in TASKS):
            if waited >= self.wait_s:
                raise RuntimeError("a cold email task is still running on this PC after %d seconds" % self.wait_s)
            self.sleep(10)
            waited += 10
        locks = [coldstate.RunLock(n, self.state) for n in ("replies", "topup")]
        try:
            for lk in locks:
                if not lk.acquire(wait_s=self.wait_s, poll_s=5):
                    raise RuntimeError("a cold email run on this PC still holds its %s lock" % lk.path.stem)
            coldstate.claim(self.app, self.state, by="cold_email_cutover", note="moved to the agents brain")
            self.done.append("named %s the home on this PC" % self.app)
        finally:
            for lk in locks:
                lk.release()

    def copy_to_box(self, files: list[Path]) -> None:
        code, out = self.shell.run(remote_py(BOX_CLEAN_INCOMING % {"remote": REMOTE}, self.app), timeout=120)
        if code != 0 or "CLEAN" not in out:
            raise RuntimeError("could not make the incoming folder on the box: %s" % out.strip()[-200:])
        want = {}
        for f in files:
            code, out = self.shell.run(put(f, "%s/incoming/%s" % (REMOTE, f.name), self.app), timeout=300)
            if code != 0:
                raise RuntimeError("could not copy %s to the box: %s" % (f.name, out.strip()[-200:]))
            want[f.name] = sha256(f)
        code, out = self.shell.run(remote_py(BOX_INSTALL % {"remote": REMOTE, "want": want, "home": self.app,
                                                            "note": "moved from the workstation"}, self.app), timeout=120)
        if code != 0 or "INSTALLED" not in out:
            raise RuntimeError("the box did not take the state: %s" % out.strip()[-300:])
        self.done.append("copied %d files to %s and named the box the home there" % (len(want), REMOTE))

    def put_pc_back(self) -> list[str]:
        """Undo step 2 after a failure in step 3: the PC is the home again and its tasks are on."""
        said = []
        try:
            coldstate.claim(coldstate.WORKSTATION, self.state, by="cold_email_cutover", note="cutover failed, put back")
            said.append("named the workstation the home again on this PC")
        except OSError as exc:
            said.append("COULD NOT name the workstation the home again (%s): fix %s by hand" % (exc, self.state / coldstate.HOME_FILE))
        for t in TASKS:
            code, out = self.shell.run(enable(t), timeout=60)
            said.append(("enabled " + t) if code == 0 else ("COULD NOT enable %s: %s" % (t, out.strip()[-120:])))
        return said

    def live_command(self, current: str) -> list:
        return ["fly", "secrets", "set", "AGENTS_LIVE_STAGES=%s" % merged_live(current), "-a", self.app]

    def run(self, set_live: bool = False, replace_box_state: bool = False, old_pc_code: bool = False) -> int:
        pre = self.preflight(replace_box_state, old_pc_code)
        self.echo("preflight passed: %d state files to move; the box holds none yet" % len(pre["files"]))
        try:
            self.stop_pc()
        except Exception as exc:  # noqa: BLE001  (a task that would not disable, a run that would not end)
            self.echo("FAILED stopping the PC: %s" % exc)
            for line in self.put_pc_back():
                self.echo("  " + line)
            self.echo("Nothing moved; the PC runs cold email as before.")
            return 1
        self.echo("the PC has stopped: %s" % "; ".join(self.done))
        try:
            self.copy_to_box(pre["files"])
        except Exception as exc:  # noqa: BLE001  (step 2 is put back so replies are never left unread)
            self.echo("FAILED moving the state: %s" % exc)
            for line in self.put_pc_back():
                self.echo("  " + line)
            self.echo("Nothing runs on the box (it is not the home); the PC runs cold email as before.")
            return 1
        self.echo(self.done[-1])
        cmd = self.live_command(pre["box"].get("live_stages") or "")
        if set_live:
            code, out = self.shell.run(cmd, timeout=600)
            self.echo(("set " if code == 0 else "COULD NOT set ") + cmd[3] + ("" if code == 0 else ": " + out.strip()[-200:]))
            if code != 0:
                return 1
        else:
            self.echo("Last step, by a person (it restarts the box), so the top-up acts there:\n  " + " ".join(cmd))
        self.echo("Then: python deploy/cold_email_cutover.py --verify (the box's reply watcher runs every 5 minutes)")
        return 0

    # ------------------------------------------------------------------ the other direction
    def back(self) -> int:
        info = self.box()
        if info.get("home") != self.app:
            raise Refused("the box is not cold email's home (it names %r): nothing to move back" % (info.get("home") or "nobody"))
        # The box stops first: the PC must never start while the box still acts.
        code, out = self.shell.run(remote_py(BOX_CLAIM % {"remote": REMOTE, "home": coldstate.WORKSTATION,
                                                          "note": "moved back to the workstation"}, self.app), timeout=120)
        if code != 0 or "CLAIMED" not in out:
            raise RuntimeError("could not name the workstation the home on the box: %s" % out.strip()[-200:])
        self.echo("the box has stopped (it names the workstation the home)")
        incoming = self.state / ("from_box_%s" % time.strftime("%Y%m%d_%H%M%S"))
        incoming.mkdir(parents=True, exist_ok=True)
        got = []
        for name in REQUIRED_FILES + OPTIONAL_FILES:
            if name not in (info.get("files") or []):
                continue
            code, out = self.shell.run(get("%s/%s" % (REMOTE, name), incoming / name, self.app), timeout=300)
            if code != 0 or not (incoming / name).is_file():
                raise RuntimeError("could not copy %s back from the box: %s (the box has stopped; the PC has not "
                                   "started: finish by hand)" % (name, out.strip()[-200:]))
            got.append(name)
        keep = self.state / ("before_back_%s" % time.strftime("%Y%m%d_%H%M%S"))
        keep.mkdir(parents=True, exist_ok=True)
        for name in got:
            old = self.state / name
            if old.exists():
                os.replace(old, keep / name)
            os.replace(incoming / name, self.state / name)
        coldstate.claim(coldstate.WORKSTATION, self.state, by="cold_email_cutover", note="moved back from the agents brain")
        for t in TASKS:
            code, out = self.shell.run(enable(t), timeout=60)
            self.echo(("enabled " + t) if code == 0 else ("COULD NOT enable %s: %s" % (t, out.strip()[-120:])))
        self.echo("the PC runs cold email again (%d files back; the PC's old copies are in %s)" % (len(got), keep))
        self.echo("Take the top-up off the box's live list:\n  fly secrets set AGENTS_LIVE_STAGES=%s -a %s"
                  % (without_live(info.get("live_stages") or ""), self.app))
        return 0


def plan_text(state: Path, app: str = APP) -> str:
    """The whole cutover in order, with every command, for a person to read before --commit."""
    rows = ["Cold email cutover: the PC (%s) to %s, in this order." % (state, app), "",
            "1. preflight, read only:",
            "   the state here holds %s (and %s when present)" % (", ".join(REQUIRED_FILES), ", ".join(OPTIONAL_FILES)),
            "   this PC is the home (home.json absent or naming the workstation); no top-up running here",
            "   the box has %s and one of %s:   %s" % (", ".join(SECRETS), " / ".join(WEBHOOKS), " ".join(secrets_list(app))),
            "   the box has the cold email code and holds no cold email state yet (fly ssh console, a read)",
            "2. stop the PC:"]
    rows += ["   %s" % " ".join('"%s"' % a if " " in a else a for a in disable(t)) for t in TASKS]
    rows += ["   wait for a running task and for the reply and top-up locks, then write %s naming %s"
             % (state / coldstate.HOME_FILE, app),
             "3. copy the state to the box, then name the box the home there, last:",
             "   fly ssh console -a %s -C (clear %s/incoming)" % (app, REMOTE)]
    names = [n for n in REQUIRED_FILES + OPTIONAL_FILES if n in REQUIRED_FILES or (state / n).is_file()]
    rows += ["   %s" % " ".join(put(state / n, "%s/incoming/%s" % (REMOTE, n), app)) for n in names]
    rows += ["   fly ssh console -a %s -C (check every hash, move the files into %s, write home.json naming %s)"
             % (app, REMOTE, app),
             "   (a failure here puts step 2 back: the PC is the home again and its tasks are on)",
             "4. the top-up is outreach, so it acts on the free box only once listed (restarts the box):",
             "   fly secrets set AGENTS_LIVE_STAGES=<the stages there now>,%s -a %s   (--set-live runs it)" % (LIVE_STAGE, app),
             "",
             "Stages that act on the box from step 3 on: %s (%s waits for step 4)." % (", ".join(STAGES), LIVE_STAGE),
             "Undo: python deploy/cold_email_cutover.py --back --commit"]
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Move the cold email jobs from the PC to the agents brain (dry by default)")
    ap.add_argument("--commit", action="store_true", help="do it (with --back: move back)")
    ap.add_argument("--check", action="store_true", help="read only: run the preflight against the box")
    ap.add_argument("--verify", action="store_true", help="read only: the box's home and its reply watcher's last pass")
    ap.add_argument("--back", action="store_true", help="the other direction: the PC runs cold email again")
    ap.add_argument("--set-live", action="store_true", help="with --commit: also add the top-up to AGENTS_LIVE_STAGES")
    ap.add_argument("--replace-box-state", action="store_true",
                    help="the box already holds cold email state (a cutover after a --back): replace it with this PC's")
    ap.add_argument("--old-pc-code", action="store_true",
                    help="go on although the PC's checkout lacks the home rule (its tasks are still disabled)")
    ap.add_argument("--state-dir", default="", help="the PC's cold email state folder (default: this checkout's)")
    ap.add_argument("--app", default=APP)
    a = ap.parse_args(argv)
    state = Path(a.state_dir) if a.state_dir else coldstate.state_dir()
    cut = Cutover(state, app=a.app)
    try:
        if a.verify:
            info = cut.box()
            hb = info.get("heartbeat") or {}
            print("box home: %s | reply watcher last pass: %s (%s new, %s handled)" % (
                info.get("home") or "none", hb.get("at") or "never", hb.get("new", "-"), hb.get("handled", "-")))
            return 0 if info.get("home") == a.app and hb.get("at") else 2
        if a.back:
            if not a.commit:
                print("Undo, in this order: the box names the workstation the home (it stops), the state comes back "
                      "from %s by fly ssh sftp get, the PC names itself the home, the four tasks are enabled, and "
                      "%s comes off AGENTS_LIVE_STAGES. Re-run with --back --commit." % (REMOTE, LIVE_STAGE))
                return 0
            return cut.back()
        if a.check:
            pre = cut.preflight(a.replace_box_state, a.old_pc_code)
            print("ready: %d state files to move (%s); the box's live stages now: %s" % (
                len(pre["files"]), ", ".join(f.name for f in pre["files"]), pre["box"].get("live_stages") or "none"))
            return 0
        if not a.commit:
            print(plan_text(state, a.app))
            print("\nDRY RUN: nothing was changed. --check runs the read-only preflight; --commit does it.")
            return 0
        return cut.run(set_live=a.set_live, replace_box_state=a.replace_box_state, old_pc_code=a.old_pc_code)
    except Refused as exc:
        print("REFUSED, nothing changed: %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
