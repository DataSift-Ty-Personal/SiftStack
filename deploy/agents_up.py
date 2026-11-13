"""One command from an account to a working AI team on Fly. Dry by default: the plan changes nothing.

    python deploy/agents_up.py                               # preflight + the plan
    python deploy/agents_up.py --commit                      # create, sync, deploy, wire, verify
    python deploy/agents_up.py --app acme-team --region iad --commit

Every step checks first and skips itself when it is already done, so a rerun picks up exactly where
the last one stopped (the Slack step deliberately stops the first run: see step 7).

  1 preflight  roster valid and TEAM.md current, selftest green, keys present, flyctl signed in
  2 app        fly apps create <app>
  3 volume     fly volumes create agents_data (the queue, the ledger, sessions, workspaces)
  4 secrets    deploy/sync_agents_secrets.py --commit (+ AGENTS_PUBLIC_URL for a custom app name)
  5 deploy     fly deploy -c fly.agents.toml -a <app>
  6 health     GET /health until the worker reports alive
  7 slack      no Slack app yet: prints the one-click create link (scopes, events and URL filled
               in); paste the bot token and signing secret into .env and rerun
  8 datasift   setup-datasift: the webhook is made in the DataSift app (an app-only operation per
               developers.datasift.ai), so this prints the URL and steps and waits for DataSift's
               own Test delivery to reach the brain; --staff-api wires our own accounts by API
  9 hello      POST /team/hello/<secret>: the team introduces itself in #<team channel>

Keys: the DataSift Open API key and an Anthropic key. No DataSift password is ever read or sent.
The .env FLY_API_TOKEN is scoped to one app and cannot create apps; sign in with `fly auth login`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "deploy"))

from sync_ftm_secrets import load_env, mask  # noqa: E402

DEFAULT_APP = "siftstack-agents"
VOLUME = "agents_data"
REQUIRED_KEYS = ("REISIFT_API_KEY", "ANTHROPIC_API_KEY")


def sh(cmd: list[str], timeout: int = 180, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO,
                           env={**os.environ, **(env or {})})
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "%s not found on PATH" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout


class Plan:
    def __init__(self, commit: bool):
        self.commit, self.blocked, self.rows = commit, "", []

    def say(self, step: str, state: str, detail: str = "") -> None:
        self.rows.append((step, state))
        print("  %-10s %-9s %s" % (step, state, detail))

    def block(self, step: str, why: str) -> None:
        self.blocked = self.blocked or "%s: %s" % (step, why)
        self.say(step, "BLOCKED", why)


def preflight(p: Plan, env: dict, fly: str) -> None:
    from agents import team
    try:
        roster = team.load()
        md = (REPO / "team" / "TEAM.md").read_text(encoding="utf-8")
        fresh = md.endswith(team.render_team_md(roster))
        p.say("roster", "ok" if fresh else "BLOCKED",
              "%d employees, %d workflows, %d live" % (len(roster.employees), len(roster.workflows),
                                                       sum(w.live for w in roster.workflows.values()))
              + ("" if fresh else "; TEAM.md is stale, run: python src/agents/cli.py team"))
        if not fresh:
            p.blocked = p.blocked or "roster: TEAM.md is stale"
    except Exception as exc:  # noqa: BLE001
        p.block("roster", str(exc).splitlines()[0])
    code, out = sh([sys.executable, "src/agents/cli.py", "selftest"], timeout=600, env={"AGENTS_USE_LLM": "0"})
    m = re.search(r"(\d+) passed, (\d+) failed", out)
    if code == 0 and m:
        p.say("selftest", "ok", "%s checks, zero network" % m.group(1))
    else:
        p.block("selftest", m.group(0) if m else out.strip().splitlines()[-1][:160] if out.strip() else "did not run")
    missing = [k for k in REQUIRED_KEYS if not env.get(k)]
    if missing:
        p.block("keys", "missing in .env: %s" % ", ".join(missing))
    else:
        p.say("keys", "ok", ", ".join("%s %s" % (k, mask(env[k])) for k in REQUIRED_KEYS))
    if not fly:
        p.block("flyctl", "install it: https://fly.io/docs/flyctl/install/")
        return
    code, out = sh([fly, "auth", "whoami"], timeout=30, env={"FLY_API_TOKEN": ""})
    if code != 0:
        p.block("flyctl", "not signed in; run: fly auth login")
    else:
        p.say("flyctl", "ok", "signed in as %s" % out.strip().splitlines()[-1])


def fly_json(fly: str, args: list[str]):
    code, out = sh([fly] + args + ["--json"], timeout=60, env={"FLY_API_TOKEN": ""})
    if code != 0:
        return None
    try:
        return json.loads(out[out.find("[") if out.find("[") >= 0 else out.find("{"):])
    except ValueError:
        return None


def get(url: str, timeout: int = 15) -> dict:
    import requests
    try:
        return requests.get(url, timeout=timeout).json()
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Stand up the AI team on Fly, end to end")
    ap.add_argument("--app", default=DEFAULT_APP)
    ap.add_argument("--region", default="iad")
    ap.add_argument("--commit", action="store_true", help="do it (default: plan only, changes nothing)")
    ap.add_argument("--staff-api", action="store_true",
                    help="our own accounts: wire the DataSift webhook + sequence by API instead of the app steps")
    a = ap.parse_args()

    env = load_env(REPO / ".env")
    env.update({k: v for k, v in os.environ.items() if k in REQUIRED_KEYS or k.startswith(("SLACK_", "AGENTS_"))})
    fly = shutil.which("fly") or shutil.which("flyctl") or ""
    url = "https://%s.fly.dev" % a.app
    p = Plan(a.commit)
    print("\nAI team -> Fly app %s (%s)%s\n" % (a.app, url, "" if a.commit else "  [PLAN ONLY, nothing changes]"))
    preflight(p, env, fly)
    if p.blocked:
        print("\nStopped before touching anything. %s" % p.blocked)
        print("Once that is fixed, the same command creates the app and its volume if missing, syncs the secrets,\n"
              "deploys, waits for health, gives you the one-click Slack app link, wires the DataSift sequence,\n"
              "and has the team introduce itself in Slack.\n")
        return 2

    from agents import team
    roster = team.load()
    apps = fly_json(fly, ["apps", "list"]) or []
    exists = any((x.get("Name") or x.get("name")) == a.app for x in apps if isinstance(x, dict))
    if exists:
        p.say("app", "done", "exists")
    elif not a.commit:
        p.say("app", "would", "fly apps create %s" % a.app)
    else:
        code, out = sh([fly, "apps", "create", a.app], env={"FLY_API_TOKEN": ""})
        p.say("app", "created" if code == 0 else "FAILED", out.strip().splitlines()[-1][:160] if out.strip() else "")
        if code:
            return 1

    vols = fly_json(fly, ["volumes", "list", "-a", a.app]) if exists or a.commit else []
    if any((v.get("Name") or v.get("name")) == VOLUME for v in vols or [] if isinstance(v, dict)):
        p.say("volume", "done", VOLUME)
    elif not a.commit:
        p.say("volume", "would", "fly volumes create %s --size 1 --region %s -a %s" % (VOLUME, a.region, a.app))
    else:
        code, out = sh([fly, "volumes", "create", VOLUME, "--size", "1", "--region", a.region, "-a", a.app, "--yes"],
                       env={"FLY_API_TOKEN": ""})
        p.say("volume", "created" if code == 0 else "FAILED", "" if code == 0 else out.strip()[-160:])
        if code:
            return 1

    slack_ready = bool(env.get("SLACK_BOT_TOKEN") and env.get("SLACK_SIGNING_SECRET"))
    if not a.commit:
        p.say("secrets", "would", "python deploy/sync_agents_secrets.py --app %s --commit%s" % (
            a.app, "" if a.app == DEFAULT_APP else " (+ AGENTS_PUBLIC_URL=%s)" % url))
        p.say("deploy", "would", "fly deploy -c fly.agents.toml -a %s --remote-only" % a.app)
    else:
        code, out = sh([sys.executable, "deploy/sync_agents_secrets.py", "--app", a.app, "--commit"],
                       env={"FLY_API_TOKEN": ""})
        p.say("secrets", "staged" if code == 0 else "FAILED", "" if code == 0 else out.strip()[-200:])
        if code:
            return 1
        env = load_env(REPO / ".env")     # the sync may have just generated AGENTS_WEBHOOK_SECRET
        if a.app != DEFAULT_APP:
            sh([fly, "secrets", "set", "-a", a.app, "--stage", "AGENTS_PUBLIC_URL=%s" % url], env={"FLY_API_TOKEN": ""})
        code, out = sh([fly, "deploy", "-c", "fly.agents.toml", "-a", a.app, "--remote-only"], timeout=1800,
                       env={"FLY_API_TOKEN": ""})
        p.say("deploy", "done" if code == 0 else "FAILED", "" if code == 0 else out.strip()[-240:])
        if code:
            return 1
        health = {}
        for _ in range(18):
            health = get(url + "/health")
            if health.get("worker_alive"):
                break
            time.sleep(10)
        p.say("health", "ok" if health.get("worker_alive") else "FAILED",
              "mode %s, slack %s, claude %s, team %s" % (health.get("mode"), health.get("slack"), health.get("claude"),
                                                         ", ".join((health.get("team") or {}).values())))
        if not health.get("worker_alive"):
            return 1

    if not slack_ready:
        p.say("slack", "YOUR TURN", "one click creates the Slack app with every scope and event filled in:")
        print("\n    %s\n" % team.manifest_url(team.slack_manifest(url)))
        print("    Install it to your workspace, then add to .env and rerun this command:\n"
              "      SLACK_BOT_TOKEN=xoxb-...      (OAuth & Permissions)\n"
              "      SLACK_SIGNING_SECRET=...      (Basic Information)\n"
              "    Create #%s plus each employee's channel (%s) and invite the app.\n" % (
                  roster.team_channel, ", ".join("#" + e.slack_channel for e in roster.employees.values())))
    else:
        p.say("slack", "ok", "app keys present (synced with the secrets)")

    # Webhooks are created in the DataSift app (developers.datasift.ai: an app-only operation), so
    # this step hands the person the exact URL and steps, then confirms through DataSift's own Test
    # delivery reaching the brain. --staff-api keeps the old API route for our own accounts.
    ds_env = {"AGENTS_PUBLIC_URL": url, "AGENTS_WEBHOOK_SECRET": env.get("AGENTS_WEBHOOK_SECRET", "")}
    if not ds_env["AGENTS_WEBHOOK_SECRET"]:
        p.say("datasift", "would", "python src/agents/cli.py setup-datasift (app steps, then DataSift's Test)")
    else:
        cmd = [sys.executable, "src/agents/cli.py", "setup-datasift"]
        if a.staff_api:
            cmd += ["--staff-api"] + (["--commit"] if a.commit else [])
        code, out = sh(cmd, timeout=300, env=ds_env)
        if code == 0:
            p.say("datasift", "wired", "DataSift's Test delivery reached the brain" if not a.staff_api else "")
        elif code == 2:
            p.say("datasift", "YOUR TURN", "in the DataSift app (steps below), then rerun this command")
            try:
                for i, step in enumerate(json.loads(out).get("steps") or [], 1):
                    print("    %d. %s" % (i, step))
                print("    Webhook URL: %s\n" % json.loads(out).get("webhook_url", ""))
            except ValueError:
                print(out.strip()[-600:])
        else:
            p.say("datasift", "FAILED", out.strip()[-240:])

    if a.commit and slack_ready:
        import requests
        try:
            r = requests.post("%s/team/hello/%s" % (url, env.get("AGENTS_WEBHOOK_SECRET", "")), timeout=20).json()
        except Exception as exc:  # noqa: BLE001
            r = {"error": str(exc)}
        p.say("hello", "sent" if r.get("ok") else "FAILED", "watch #%s: everyone introduces themselves" % roster.team_channel)
    else:
        p.say("hello", "would" if not a.commit else "waiting", "POST /team/hello/<secret> once Slack is set")

    done = sum(1 for _, s in p.rows if s in ("ok", "done", "created", "staged", "wired", "sent"))
    print("\n%d steps done, %d waiting%s.\n" % (done, len(p.rows) - done,
                                                "" if a.commit else "; rerun with --commit to do it"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
