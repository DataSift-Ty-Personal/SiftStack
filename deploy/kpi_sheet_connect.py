"""Connect the KPI tracker to its Google Sheet: one Google sign-in, and the brain writes the sheet from then on.

Ty, 2026-11-23: the KPI tracker is a Google Sheet the brain rewrites three times a weekday (7:45 AM, 12:30 PM,
7:30 PM Eastern), laid out like the DataSift KPI tracker, with a Goals tab the owner types in. The brain
needs a Google sign-in that can edit that sheet. This script does the whole setup in one run:

  1. opens Google's sign-in page in your browser: pick the account that owns the sheet, then Allow;
  2. checks the new sign-in opens the sheet;
  3. saves the sign-in to this repo's .env (AGENTS_GOOGLE_CLIENT_ID, AGENTS_GOOGLE_CLIENT_SECRET,
     AGENTS_GOOGLE_REFRESH_TOKEN, AGENTS_KPI_SHEET_ID) and sets the same four as secrets on the brain's Fly
     app (Fly restarts the brain, about half a minute);
  4. runs the tracker once on the brain, so the sheet fills now instead of at the next scheduled run.

    python deploy/kpi_sheet_connect.py --sheet <the sheet's link or id> --client-env <a .env with an OAuth client>
    python deploy/kpi_sheet_connect.py --sheet <link> --client-id <id> --client-secret <secret>
    python deploy/kpi_sheet_connect.py --check        # the saved sign-in still opens the sheet (reads only)

The OAuth client is a Google Cloud "Desktop app" client (it accepts any local port). Its id and secret are read
from the .env you name (AGENTS_GOOGLE_CLIENT_ID / _SECRET, or GOOGLE_ADS_CLIENT_ID / _SECRET), or given on the
command line. Nothing secret is printed, and the Fly secrets go in on stdin, never on a command line. Scopes:
spreadsheets (to write the sheet) and drive.file. Revoke any time at https://myaccount.google.com/permissions.
Standard library only.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_APP = "siftstack-agents"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.file"
KEYS = ("AGENTS_GOOGLE_CLIENT_ID", "AGENTS_GOOGLE_CLIENT_SECRET", "AGENTS_GOOGLE_REFRESH_TOKEN", "AGENTS_KPI_SHEET_ID")
ID_RX = re.compile(r"/d/([A-Za-z0-9_-]{20,})")
RUN_LOG = "/data/kpi/connect_run.log"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def read_env(path: Path) -> dict:
    out = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def save_env(path: Path, values: dict) -> None:
    """Each key's line replaced where it stands, or added at the end; every other line kept as it was."""
    try:
        raw = path.read_bytes().decode("utf-8-sig")
    except OSError:
        raw = ""
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            lines[i] = "%s=%s" % (key, values[key])
            done.add(key)
    rest = [k for k in values if k not in done]
    if rest:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# KPI tracker: the Google Sheet the brain writes (deploy/kpi_sheet_connect.py)")
        lines += ["%s=%s" % (k, values[k]) for k in rest]
    path.write_bytes((nl.join(lines) + nl).encode("utf-8"))


def sheet_id(text: str) -> str:
    m = ID_RX.search(text or "")
    return m.group(1) if m else (text or "").strip()


def post_form(url: str, form: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except ValueError:
            body = {}
        raise SystemExit("Google refused the sign-in (%s): %s" % (exc.code, body.get("error_description") or body.get("error") or ""))


def get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read().decode()).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise SystemExit("Google answered %s opening the sheet: %s. Sign in with the account that owns it, or "
                         "share the sheet with that account as an editor." % (exc.code, msg))


def sign_in(client_id: str, client_secret: str, port: int = 0) -> str:
    """Google's consent page in the browser, the answer caught on this machine; the refresh token."""
    state, got = secrets.token_urlsafe(16), {}

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802  (the stdlib's name)
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and go back to the terminal.")

        def log_message(self, *args):
            return

    # The redirect your Google Sheets setup already uses (hubspot google-sheets-oauth-setup.py): localhost on
    # port 8766, the listener on 127.0.0.1. A desktop OAuth client takes any loopback port, so a busy 8766
    # falls back to a free one.
    try:
        srv = http.server.HTTPServer(("127.0.0.1", port or 8766), Back)
    except OSError:
        srv = http.server.HTTPServer(("127.0.0.1", 0), Back)
    srv.timeout = 300
    redirect = "http://localhost:%d" % srv.server_address[1]
    url = AUTH_URL + "?" + urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
                                                   "scope": SCOPES, "access_type": "offline", "prompt": "consent",
                                                   "state": state})
    say("Opening Google's sign-in page. Pick the account that owns the sheet, then Allow.")
    say("If Google says the app isn't verified: Advanced, then Go to the app. If no browser opens, paste this:\n%s\n" % url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    srv.handle_request()
    srv.server_close()
    if got.get("state") != state or not got.get("code"):
        raise SystemExit("No sign-in came back (%s). Run it again." % (got.get("error") or "timed out after 5 minutes"))
    data = post_form(TOKEN_URL, {"code": got["code"], "client_id": client_id, "client_secret": client_secret,
                                 "redirect_uri": redirect, "grant_type": "authorization_code"})
    if not data.get("refresh_token"):
        raise SystemExit("Google gave no refresh token. Remove the app at https://myaccount.google.com/permissions, "
                         "then run this again.")
    return data["refresh_token"]


def access(client_id: str, client_secret: str, refresh: str) -> str:
    return post_form(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh,
                                 "grant_type": "refresh_token"})["access_token"]


def fly_env() -> dict:
    """flyctl with your own login: this repo's .env FLY_API_TOKEN is scoped to another app."""
    env = dict(os.environ)
    env.pop("FLY_API_TOKEN", None)
    env["MSYS_NO_PATHCONV"] = "1"
    return env


def fly(args: list, stdin: str | None = None, timeout: int = 240) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["fly"] + args, input=stdin, capture_output=True, text=True, timeout=timeout, env=fly_env())
    except FileNotFoundError:
        raise SystemExit("flyctl is not on PATH: install it, run fly auth login, then run this again.")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["fly"] + args, 124, "", "flyctl did not answer within %d seconds" % timeout)


def wait_healthy(app: str, seconds: int = 180) -> bool:
    url = "https://%s.fly.dev/health" % app
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001  (restarting: keep asking)
            pass
        time.sleep(5)
    return False


def fill_now(app: str) -> None:
    """One tracker run on the brain, started apart from this connection so a dropped ssh cannot stop it."""
    # The shell's own output goes to the log before anything runs: a shell still holding the ssh session's
    # terminal keeps `fly ssh console` open until the run ends.
    script = ("exec > %s 2>&1 < /dev/null; cd /app && python -u /app/src/agents/cli.py autopilot run kpi_tracker; "
              "echo \"== EXIT $?\"" % RUN_LOG)
    r = fly(["ssh", "console", "-a", app, "-C", "setsid -f sh -c '%s'" % script], timeout=120)
    if r.returncode != 0:
        say("Could not start the tracker on the brain (%s). It fills at the next scheduled run." % (r.stderr or r.stdout).strip()[:200])
        return
    say("The tracker is running on the brain (a minute or three)...")
    end = time.time() + 900
    tail = ""
    while time.time() < end:
        time.sleep(15)
        r = fly(["ssh", "console", "-a", app, "-C", "tail -n 12 %s" % RUN_LOG], timeout=90)
        tail = (r.stdout or "").strip()
        if "== EXIT" in tail:
            break
    lines = [ln for ln in tail.splitlines() if ln.strip() and not ln.startswith("Connecting to")]
    say("\n".join("  " + ln for ln in lines[-10:]) if lines else "  (no output yet; it keeps running on the brain)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Connect the KPI tracker to its Google Sheet")
    ap.add_argument("--sheet", help="the Google Sheet's link or id (default: AGENTS_KPI_SHEET_ID in .env)")
    ap.add_argument("--client-env", help="a .env holding the OAuth client id and secret")
    ap.add_argument("--client-id")
    ap.add_argument("--client-secret")
    ap.add_argument("--app", default=DEFAULT_APP, help="the brain's Fly app (default %s)" % DEFAULT_APP)
    ap.add_argument("--env-file", default=str(REPO / ".env"), help="where the sign-in is saved (this repo's .env)")
    ap.add_argument("--port", type=int, default=0, help="the local port Google answers on (default: any free one)")
    ap.add_argument("--no-push", action="store_true", help="save to .env only; do not touch the Fly app")
    ap.add_argument("--no-fill", action="store_true", help="do not run the tracker on the brain now")
    ap.add_argument("--check", action="store_true", help="only check the saved sign-in opens the sheet")
    args = ap.parse_args()

    env_file = Path(args.env_file)
    saved = read_env(env_file)
    sid = sheet_id(args.sheet or saved.get("AGENTS_KPI_SHEET_ID", ""))
    if not sid:
        raise SystemExit("Which sheet? Pass --sheet with its link.")
    if args.check:
        need = [k for k in KEYS[:3] if not saved.get(k)]
        if need:
            raise SystemExit("Not signed in yet: %s missing from %s." % (", ".join(need), env_file))
        tok = access(saved["AGENTS_GOOGLE_CLIENT_ID"], saved["AGENTS_GOOGLE_CLIENT_SECRET"], saved["AGENTS_GOOGLE_REFRESH_TOKEN"])
        info = get_json("%s/%s?fields=properties.title,sheets.properties.title" % (SHEETS, sid), tok)
        say("The sign-in works and opens \"%s\" (tabs: %s)." % (
            info.get("properties", {}).get("title", ""), ", ".join(s["properties"]["title"] for s in info.get("sheets", []))))
        return 0

    client = read_env(Path(args.client_env)) if args.client_env else {}
    cid = args.client_id or client.get("AGENTS_GOOGLE_CLIENT_ID") or client.get("GOOGLE_ADS_CLIENT_ID") or saved.get("AGENTS_GOOGLE_CLIENT_ID")
    csec = (args.client_secret or client.get("AGENTS_GOOGLE_CLIENT_SECRET") or client.get("GOOGLE_ADS_CLIENT_SECRET")
            or saved.get("AGENTS_GOOGLE_CLIENT_SECRET"))
    if not cid or not csec:
        raise SystemExit("No OAuth client: pass --client-env with a .env holding GOOGLE_ADS_CLIENT_ID and "
                         "GOOGLE_ADS_CLIENT_SECRET, or --client-id and --client-secret.")

    refresh = sign_in(cid, csec, args.port)
    info = get_json("%s/%s?fields=properties.title,sheets.properties.title" % (SHEETS, sid), access(cid, csec, refresh))
    title = info.get("properties", {}).get("title", "")
    say("Signed in. The sign-in opens \"%s\"." % title)

    values = {"AGENTS_GOOGLE_CLIENT_ID": cid, "AGENTS_GOOGLE_CLIENT_SECRET": csec, "AGENTS_GOOGLE_REFRESH_TOKEN": refresh,
              "AGENTS_KPI_SHEET_ID": sid}
    save_env(env_file, values)
    say("Saved to %s (four AGENTS_ lines)." % env_file)
    if args.no_push:
        say("Not pushed to Fly (--no-push).")
        return 0
    r = fly(["secrets", "import", "-a", args.app], stdin="".join("%s=%s\n" % kv for kv in values.items()))
    if r.returncode != 0:
        out = (r.stderr or r.stdout or "").strip()
        for v in values.values():
            out = out.replace(v, "***")
        raise SystemExit("Fly refused the secrets: %s\nRun fly auth login, then run this again." % out[:300])
    say("Set on the brain (%s). Fly is restarting it..." % args.app)
    if not wait_healthy(args.app):
        say("The brain did not come back within three minutes; the sheet fills at the next scheduled run.")
        return 0
    if not args.no_fill:
        fill_now(args.app)
    say("\nThe KPI sheet: https://docs.google.com/spreadsheets/d/%s/edit" % sid)
    say("It refreshes at 7:45 AM, 12:30 PM and 7:30 PM Eastern on weekdays. Type your goals, costs and deal fees on "
        "its Goals tab; the next refresh uses them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
