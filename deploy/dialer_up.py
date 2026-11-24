"""One command from a Twilio account to a working SiftStack dialer on Fly. Dry by default.

    python deploy/dialer_up.py                  # preflight and the plan; changes nothing
    python deploy/dialer_up.py --commit         # Twilio setup, the Fly app, secrets, deploy, callers, links

Every step checks first and skips itself when it is already done, so a rerun picks up where the last one
stopped.

  1 preflight  the dialer selftest is green; .env has TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and
               REISIFT_API_KEY; flyctl is signed in (the .env FLY_API_TOKEN is scoped to one other app
               and is stripped for these commands)
  2 twilio     python -m dialer.cli setup: the TwiML App, an API key, the numbers in DIALER_NUMBERS routed
               to the dialer for call-backs; the new values are appended to .env
  3 tokens     DIALER_SECRET, DIALER_HANDRAISE_TOKEN and DIALER_TEXTING_TOKEN generated once into .env
  4 app        fly apps create, and the dialer_data volume
  5 secrets    the dialer's settings from .env, staged in one call (values are never printed)
  6 deploy     fly deploy -c fly.dialer.toml
  7 health     GET /health until it answers
  8 callers    each caller in DIALER_CALLERS (.env, JSON) is added on the box, and their sign-in link printed

What it does NOT do: buy a Twilio number (a purchase; one click on the console's Buy a Number page), and
deploy the texting app. The texting app takes the dialer's calls only once it is redeployed with the two
tokens set (SMS_AGENT_DIALER_TOKEN = DIALER_TEXTING_TOKEN, SMS_AGENT_DIALER_HANDRAISE_TOKEN =
DIALER_HANDRAISE_TOKEN, SMS_AGENT_DIALER_URL = the dialer's address); step 9 prints that, and it is the
owner's go, because a texting deploy restarts the box that takes seller replies.

DIALER_CALLERS example: [{"identity": "casey", "name": "Casey", "number": "8655550100"},
                         {"identity": "ty", "name": "Ty", "number": "8655550101", "role": "manager"}]
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "deploy"))
from sync_ftm_secrets import load_env, mask  # noqa: E402

DEFAULT_APP = "siftstack-dialer"
VOLUME = "dialer_data"
SECRET_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET",
               "TWILIO_TWIML_APP_SID", "TWILIO_WEBHOOK_KEY", "REISIFT_API_KEY", "DIALER_SECRET", "DIALER_TEST_NUMBERS",
               "DIALER_TEXTING_URL", "DIALER_TEXTING_TOKEN", "DIALER_HANDRAISE_TOKEN", "DIALER_KPI_URL",
               "DIALER_SLACK_WEBHOOK", "DIALER_GATE", "DIALER_CALLS", "DIALER_WRITEBACK")


def fly_env() -> dict:
    env = dict(os.environ)
    env.pop("FLY_API_TOKEN", None)       # scoped to another app; the person's own login is used
    env["MSYS_NO_PATHCONV"] = "1"        # Git Bash would rewrite /data paths in remote commands
    return env


def sh(cmd: list[str], timeout: int = 300, stdin: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO, env=fly_env(), input=stdin)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "%s is not on PATH" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout


def py(args: list[str], timeout: int = 600) -> tuple[int, str]:
    exe = REPO / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    exe = str(exe) if exe.exists() else sys.executable
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, cwd=REPO / "src")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def append_env(values: dict) -> list[str]:
    path = REPO / ".env"
    have = load_env(path)
    lines = ["%s=%s" % (k, v) for k, v in values.items() if v and k not in have]
    if lines:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        with path.open("a", encoding="utf-8") as f:
            f.write(("\n" if text and not text.endswith("\n") else "") + "# SiftStack dialer (deploy/dialer_up.py)\n" + "\n".join(lines) + "\n")
    return [line.split("=", 1)[0] for line in lines]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=DEFAULT_APP)
    ap.add_argument("--region", default="iad")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    url = "https://%s.fly.dev" % a.app
    env = load_env(REPO / ".env")
    stop = []

    print("1 preflight")
    rc, out = py(["-m", "dialer.selftest"])
    print("  dialer selftest:", out.strip().splitlines()[-1] if out.strip() else "no output")
    if rc:
        stop.append("the dialer selftest is not green")
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "REISIFT_API_KEY"):
        print("  %s: %s" % (k, mask(env.get(k, ""))))
        if not env.get(k):
            stop.append("%s is not in .env" % k)
    rc, out = sh(["fly", "auth", "whoami"], timeout=60)
    print("  fly login:", out.strip().splitlines()[-1] if rc == 0 and out.strip() else "NOT signed in (run: fly auth login)")
    if rc:
        stop.append("flyctl is not signed in")
    if stop:
        print("\nStopped before changing anything:\n  - " + "\n  - ".join(stop))
        return 2

    print("2 twilio")
    args = ["-m", "dialer.cli", "setup", "--public-url", url, "--numbers", env.get("DIALER_NUMBERS", "")]
    rc, out = py(args + (["--commit", "--write-env"] if a.commit else []))
    print("  " + out.strip().replace("\n", "\n  "))
    if rc:
        return rc

    print("3 tokens")
    new = {k: secrets.token_urlsafe(32) for k in ("DIALER_SECRET", "DIALER_HANDRAISE_TOKEN", "DIALER_TEXTING_TOKEN")
           if not env.get(k)}
    new.setdefault("DIALER_TEXTING_URL", "" if env.get("DIALER_TEXTING_URL") else "https://siftstack.fly.dev")
    if a.commit:
        print("  added to .env:", ", ".join(append_env(new)) or "nothing new")
    else:
        print("  would add to .env:", ", ".join(k for k, v in new.items() if v) or "nothing new")
    env = load_env(REPO / ".env")

    print("4 app")
    rc, out = sh(["fly", "apps", "list", "--json"], timeout=90)
    exists = rc == 0 and any(x.get("Name") == a.app for x in json.loads(out or "[]"))
    print("  %s: %s" % (a.app, "exists" if exists else "would be created" if not a.commit else "creating"))
    if not exists and a.commit:
        rc, out = sh(["fly", "apps", "create", a.app])
        if rc:
            print(out)
            return rc
    rc, out = sh(["fly", "volumes", "list", "-a", a.app, "--json"], timeout=90)
    has_vol = rc == 0 and any(v.get("name") == VOLUME for v in json.loads(out or "[]"))
    print("  volume %s: %s" % (VOLUME, "exists" if has_vol else "would be created" if not a.commit else "creating"))
    if not has_vol and a.commit:
        rc, out = sh(["fly", "volumes", "create", VOLUME, "--size", "1", "--region", a.region, "-a", a.app, "--yes"])
        if rc:
            print(out)
            return rc

    print("5 secrets")
    vals = {k: env[k] for k in SECRET_KEYS if env.get(k)}
    vals["DIALER_PUBLIC_URL"] = url
    for k in sorted(vals):
        print("  %s: %s" % (k, mask(vals[k])))
    if a.commit:
        body = "".join("%s=%s\n" % (k, v) for k, v in vals.items())
        rc, out = sh(["fly", "secrets", "import", "-a", a.app, "--stage"], stdin=body)
        if rc:
            print(out)
            return rc

    print("6 deploy")
    if a.commit:
        rc, out = sh(["fly", "deploy", "-c", "fly.dialer.toml", "-a", a.app, "--remote-only"], timeout=1800)
        print("  " + "\n  ".join(out.strip().splitlines()[-6:]))
        if rc:
            return rc
    else:
        print("  would deploy fly.dialer.toml to", a.app)

    print("7 health")
    if a.commit:
        import urllib.request
        for _ in range(30):
            try:
                with urllib.request.urlopen(url + "/health", timeout=10) as r:
                    print("  " + r.read().decode("utf-8")[:200])
                    break
            except Exception:  # noqa: BLE001
                time.sleep(5)
        else:
            print("  the app did not answer /health")
            return 3

    print("8 callers")
    try:
        people = json.loads(env.get("DIALER_CALLERS") or "[]")
    except ValueError:
        people = []
        print("  DIALER_CALLERS is not valid JSON")
    for p in people:
        ident, name = p.get("identity", ""), p.get("name", "")
        cmd = "python -m dialer.cli add-caller %s %s --number %s --role %s" % (ident, json.dumps(name), p.get("number", ""),
                                                                               p.get("role", "caller"))
        if not a.commit:
            print("  would add %s (%s)" % (name, ident))
            continue
        rc, out = sh(["fly", "ssh", "console", "-a", a.app, "-C", "sh -c 'cd /app && %s && python -m dialer.cli link %s'" % (cmd, ident)])
        print("  " + out.strip().splitlines()[-1] if out.strip() else "  (no output)")

    print("9 texting app (your go)")
    print("  The texting app takes the dialer's calls once it is redeployed with these secrets on siftstack:")
    print("  SMS_AGENT_DIALER_TOKEN = DIALER_TEXTING_TOKEN, SMS_AGENT_DIALER_HANDRAISE_TOKEN = DIALER_HANDRAISE_TOKEN,")
    print("  SMS_AGENT_DIALER_URL = %s. Until then the dialer shows no text thread and hears no hand raises." % url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
