"""The issue fixer's box keeps watch over the other three boxes, on time (Ty, 2026-11-20).

Two jobs, in their own thread beside the fixer's loop (fixer.loop starts it), so a fixer run that takes an
hour never delays a check. Each tick (every 5 minutes) runs in a fresh process with a time limit.

1. THE WATCHDOGS. The three outside checks ran on GitHub, and GitHub throttles a public repo's schedules:
   the daily checks fired about 8 hours late and the every-5-minute texting check about three times a day.
   A heartbeat that fires 8 hours late is not a heartbeat. The same checks and the same verdicts run here:
     ftm     the first-to-market box's own verdict line, `ftm_health.py --check --no-ledger-update` over
             fly ssh, read off the printed line and never off the remote exit code; daily from 9:30
             business time (the 6:30 run and the 8:00 digest are done by then). --no-ledger-update keeps
             a check that runs before the digest from eating the digest's "new records" count.
     agents  the brain's health page, judged by deploy/agents_heartbeat.py, every 15 minutes
     sms     the texting app's /alive, every 5 minutes
   A box that cannot be reached is red. A bad verdict must be seen twice in a row, 5 minutes apart, before
   anyone hears (a deploy's restart is not an outage), and each watchdog posts at most once a business day.
   Posts go through the brain's relay (ISSUE_FIXER_RELAY_URL, or AGENTS_RELAY_SECRET), which keeps one post
   per watchdog a day whoever sends it (this box, or the GitHub workflows, which stay as a late backup) and
   one first-to-market problem a day across the run report, the digest and the watchdog. When the relay
   cannot be reached (the brain is the box that is down), the post goes straight to the issues channel with
   the bot token, after a look at today's channel for the same watchdog's post; the relay makes the same
   look before it lets a later one through. So the pair never says the same red twice in one day.

2. THE LOG WATCH. A failure caught and logged, never posted, never reached the fixer, which reads the
   issues channel only (cold email replies on a retired route for hours, a report of 0 dials three nights
   running, notes refused on a retired route). Every quarter hour the log watch reads each box's new log
   lines from the Fly logs API, paged from where it stopped, keeps error-level lines and tracebacks, masks
   secrets, tokens, phone numbers, emails, record ids and street addresses, and folds them by shape the way
   the fixer folds issues (fixer.shape). A shape reaches the issues channel when it is a crash (a traceback)
   or repeats (3 times in one day), at most once a business day, at most LOG_MAX_POSTS_DAY shapes a day, and
   never when the fixer already sorted that post as anything but a fixed bug: a fixed bug that comes back
   after its fix is a regression and is said again.

    python tools/issue_fixer/watch.py tick [--dry] [--force] [--only ftm,agents,sms,logs]
    python tools/issue_fixer/watch.py status
    python tools/issue_fixer/watch.py relay-check      # the relay URL and the brain's version, dry
    python tools/issue_fixer/watch.py off | on

Switches: ISSUE_FIXER_WATCH and ISSUE_FIXER_LOGWATCH (on, quiet: check and log but post nothing, off), and
the file WATCH_OFF in the fixer's state folder (both off). The fixer's own off switch does not stop the
watch: the boxes are watched while the fixer is paused.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import fixer as F  # noqa: E402

TICK_MIN = 5
TICK_TIMEOUT_S = 290
CONFIRM = 2                       # bad verdicts in a row before anyone hears
FTM_AT = (9, 30)                  # business time: the 6:30 run and its 8:00 digest are done
FTM_CHECK = "python /app/src/ftm_health.py --check --no-ledger-update"
FTM_SSH_TIMEOUT_S = 120
AGENTS_URL = "https://siftstack-agents.fly.dev"
SMS_URL = "https://siftstack.fly.dev/alive"
WATCH_NAME = "Watchdog"           # the username of a direct watchdog post; never the fixer's own name
LOG_NAME = "Log watch"            # the username of a log watch post; never the fixer's own name

LOG_APPS = ("siftstack-agents", "siftstack-ftm", "siftstack")
APP_NAMES = {"siftstack-agents": "the agents brain", "siftstack-ftm": "the first-to-market box",
             "siftstack": "the texting app"}
LOG_EVERY_MIN = 15
LOG_REPEAT = 3                    # an error line is an issue at this many in one day; a crash at one
LOG_MAX_PAGES = 30                # 100 lines a page, per box per read
LOG_BUDGET_S = 90                 # a slow logs API never holds a tick past its limit
LOG_GAP_HOURS = 20                # a stopping point this old means the box was down: read the last hour only
LOG_MAX_POSTS_DAY = 6             # a bad deploy that throws twenty new errors is one morning, not twenty posts
LOG_FAILS_TO_SAY = 3              # reads of a box's logs that fail in a row before the channel hears
LOG_HITS_KEPT = 50
LOGS_API = "https://api.fly.io/api/v1/apps/%s/logs?%s"

# Each watchdog: the head its message carries (the relay and the GitHub workflows use the same words), the
# marker its post in the channel starts with, and what the relay calls it. The first-to-market marker is the
# whole family: one first-to-market problem a day is said, whichever job saw it first. The order is the order
# of a tick: the quick checks first, so a fly ssh that hangs never costs them.
WATCHDOGS = {
    "sms": {"app": "siftstack", "head": "SMS agent", "marker": "Texting app watchdog:", "subject": "the app",
            "lead": "Texting app watchdog:"},
    "agents": {"app": "siftstack-agents", "head": "Agents brain", "marker": "Agents brain watchdog:",
               "subject": "the brain", "lead": "Agents brain watchdog:"},
    "ftm": {"app": "siftstack-ftm", "head": "FTM pull", "marker": "First-to-market", "subject": "the pull",
            "lead": "First-to-market watchdog:"},
}
BAD = ("red", "unreachable", "unreadable", "stale", "unconfigured")

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FTM_LINE = re.compile(r"^(GREEN|AMBER|RED) ")


# ---------------------------------------------------------------------- masking
def _long_token(m: re.Match) -> str:
    s = m.group(0)
    return "[secret]" if re.search(r"\d", s) and re.search(r"[A-Za-z]", s) else s


# Secrets first, so a token inside a URL or a key=value pair goes whole; then the things that vary from one
# line to the next (record ids, emails, phone numbers, street addresses), so one problem keeps one shape.
MASKS = (
    (re.compile(r"https://hooks\.slack\.com/services/\S+"), "https://hooks.slack.com/services/[secret]"),
    (re.compile(r"(/hooks/[a-z]+/)[^/\s?#]+"), r"\1[secret]"),
    (re.compile(r"\bxox[a-z]-[A-Za-z0-9-]+"), "[secret]"),
    (re.compile(r"\bxapp-[A-Za-z0-9-]+"), "[secret]"),
    (re.compile(r"\b(?:fm1|fm2|fo1)_[A-Za-z0-9_+/=,-]+"), "[secret]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[secret]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[secret]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[secret]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[secret]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[secret]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[secret]"),
    (re.compile(r"(?i)\b(bearer|basic|api-key|flyv1)[:\s]+[A-Za-z0-9._~+/=,:-]{8,}"), r"\1 [secret]"),
    # key=value anywhere, and "key": value only with the key quoted (a dict or JSON), so "KeyError: 'x'"
    # keeps its words.
    (re.compile(r"(?i)(\b[\w-]*(?:key|token|secret|password|passwd|pwd|signature|auth)[\w-]*\s*=\s*['\"]?)"
                r"([^\s'\",&;)}\]]{4,})"), r"\1[secret]"),
    (re.compile(r"(?i)(['\"][\w-]*(?:key|token|secret|password|passwd|pwd|signature|auth)[\w-]*['\"]\s*:\s*['\"]?)"
                r"([^\s'\",&;)}\]]{4,})"), r"\1[secret]"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "[uuid]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), _long_token),
    (re.compile(r"(?<![\w.])(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w.])"), "[phone]"),
    # A house number, one to three capitalised words and a street suffix (the public scan's own shape).
    (re.compile(r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][A-Za-z'.-]*\s+){1,3}(?i:St|Street|Rd|Road|Dr|Drive|Ave|"
                r"Avenue|Ln|Lane|Way|Ct|Court|Pike|Blvd|Boulevard|Cir|Circle|Pl|Place|Trl|Trail|Hwy|Highway|Pkwy|"
                r"Parkway|Ter|Terrace)\b\.?"), "[address]"),
)


def mask(text: str) -> str:
    """A log line with nothing in it that should not reach Slack or the state file."""
    t = ANSI.sub("", str(text or ""))
    for rx, repl in MASKS:
        t = rx.sub(repl, t)
    return t


def short(exc, limit: int = 120) -> str:
    return mask(str(exc)).replace("\n", " ")[:limit]


# ---------------------------------------------------------------------- the verdicts (pure)
def ftm_verdict(rc: int, out: str) -> tuple[str, str]:
    """The first-to-market box's own verdict, read off its printed line (GREEN, AMBER or RED). flyctl does not
    reliably pass a remote exit code back, and a watchdog that silently always passes is worse than none."""
    lines = [ANSI.sub("", ln).strip("\r") for ln in str(out or "").splitlines()]
    for ln in lines:
        m = FTM_LINE.match(ln)
        if m:
            return ("red" if m.group(1) == "RED" else "ok"), ln.strip()
    last = next((mask(ln.strip()) for ln in reversed(lines) if ln.strip()), "")
    return "unreachable", ("The machine did not answer (flyctl exit %d%s). It is most likely stopped, out of credit, or "
                           "mid-deploy. No FTM data is being pulled while this is true." % (rc, (": " + last[:120]) if last else ""))


def sms_verdict(code: int, body: str) -> tuple[str, str]:
    """The texting app's /alive: 200 is a worker that is passing; anything else is a worker that stopped."""
    if code == 200:
        return "ok", "The worker is passing."
    m = re.search(r'"minutes_ago"\s*:\s*([0-9.]+)', str(body or ""))
    return "stale", ("The receiver answered but the worker has not run a pass in %s minutes. Replies are landing in "
                     "the queue and nothing is draining it." % (m.group(1) if m else "?"))


def heartbeat():
    """deploy/agents_heartbeat.py, the GitHub workflow's own verdict on the brain, loaded from the image."""
    for p in (HERE.parent.parent / "deploy" / "agents_heartbeat.py", F.MAIN / "deploy" / "agents_heartbeat.py"):
        if p.exists():
            spec = importlib.util.spec_from_file_location("agents_heartbeat", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("deploy/agents_heartbeat.py is not on this box")


# ---------------------------------------------------------------------- the messages
def raw_message(name: str, status: str, detail: str) -> str:
    """What goes to the relay: the GitHub workflows' own shape, so the relay reads both the same way."""
    wd = WATCHDOGS[name]
    return ("*%s is %s*\n%s\n\nThis came from the watchdog on the issue fixer's box, so it also fires when the box "
            "itself goes quiet.\nCheck: `fly status -a %s` then `fly logs -a %s`" % (
                wd["head"], status, str(detail or "").strip(), wd["app"], wd["app"]))


def direct_text(name: str, status: str, detail: str) -> str:
    """What goes straight to the channel when the relay cannot be reached: the words the relay would post,
    starting with the watchdog's marker, so the relay and this box both find it later the same day."""
    wd = WATCHDOGS[name]
    said = [ln.split("::", 1)[1].strip() if "::" in ln else ln.strip() for ln in str(detail or "").splitlines()
            if ln.strip()]
    return F.plain("%s %s is %s. %s\nThe brain's relay did not answer, so this came straight from the watchdog on the "
                   "issue fixer's box.\nCheck: fly status -a %s, then fly logs -a %s" % (
                       wd["lead"], wd["subject"], status, " ".join(said), wd["app"], wd["app"]))


def said_today(texts: list[str], marker: str) -> bool:
    return any(str(t or "").lstrip(" *_").startswith(marker) for t in texts)


# ---------------------------------------------------------------------- the outside world
def http_get(url: str, timeout: float = 25.0) -> tuple[int, str]:
    """(status code, body). An HTTP error is an answer; only a box that does not answer raises."""
    req = urllib.request.Request(url, headers={"User-Agent": "siftstack-watch"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return int(e.code), body


def fly_auth(sh: F.Shell | None = None) -> str:
    """The Authorization header for the Fly API: the box's deploy tokens (ISSUE_FIXER_FLY_TOKEN), or on the
    workstation Ty's own fly login, read when asked and never printed or kept."""
    tok = (os.environ.get("ISSUE_FIXER_FLY_TOKEN") or "").strip()
    if not tok:
        rc, out = (sh or F.Shell()).run([F.tool("fly"), "auth", "token"], timeout=60, env=F.plain_env())
        tok = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith(("FlyV1 ", "fm1_", "fm2_", "fo1_"))), "")
    if tok.startswith("FlyV1 "):
        return tok
    if tok.startswith(("fm1_", "fm2_")):
        return "FlyV1 " + tok
    return ("Bearer " + tok) if tok else ""


class World:
    """Everything the watch does outside this process, in one place, so a test can stand in for all of it."""

    def __init__(self, st: F.Settings, sh: F.Shell | None = None, slack=None):
        self.st, self.sh = st, sh or F.Shell()
        self.slack = slack if slack is not None else (F.Slack(st.token, st.icon) if st.token else None)
        self._auth = None

    # the three checks
    def check_ftm(self) -> tuple[str, str]:
        rc, out = self.sh.run([F.tool("fly"), "ssh", "console", "-a", WATCHDOGS["ftm"]["app"], "-C", FTM_CHECK],
                              timeout=FTM_SSH_TIMEOUT_S, env=F.plain_env())
        return ftm_verdict(rc, out)

    def check_agents(self) -> tuple[str, str]:
        hb = heartbeat()
        page, failed = hb.fetch(self.st.env.get("ISSUE_FIXER_AGENTS_URL") or AGENTS_URL)
        return failed if failed else hb.verdict(page)

    def check_sms(self) -> tuple[str, str]:
        try:
            code, body = http_get(self.st.env.get("ISSUE_FIXER_SMS_URL") or SMS_URL, 25)
        except Exception as exc:  # noqa: BLE001  (unreachable is worse than stale: the whole box is gone)
            return "unreachable", ("The box did not respond at all (%s). It is most likely stopped, out of credit, or "
                                   "mid-deploy." % short(exc, 80))
        return sms_verdict(code, body)

    def check(self, name: str) -> tuple[str, str]:
        return {"ftm": self.check_ftm, "agents": self.check_agents, "sms": self.check_sms}[name]()

    # the relay and the channel
    def relay_url(self) -> str:
        url = (self.st.env.get("ISSUE_FIXER_RELAY_URL") or "").strip()
        if url:
            return url
        secret = (os.environ.get("AGENTS_RELAY_SECRET") or self.st.env.get("AGENTS_RELAY_SECRET") or "").strip()
        base = (self.st.env.get("ISSUE_FIXER_AGENTS_URL") or AGENTS_URL).rstrip("/")
        return "%s/hooks/slack/%s/watchdog" % (base, secret) if secret else ""

    def relay(self, url: str, text: str) -> tuple[bool, str]:
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "siftstack-watch"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200 <= int(r.status) < 300, "HTTP %d" % r.status
        except urllib.error.HTTPError as e:
            return False, "HTTP %d" % e.code
        except Exception as exc:  # noqa: BLE001
            return False, short(exc, 80)

    def relay_dry(self, url: str, text: str) -> dict:
        """How the relay would route a message, asked with dry=1: it answers in JSON and posts nothing."""
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(url + ("&" if "?" in url else "?") + "dry=1", data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "siftstack-watch"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def today_texts(self, now: datetime) -> list[str]:
        """The words of today's top-level posts in the issues channel (business day)."""
        if not (self.slack and self.st.channel):
            return []
        start = now.astimezone(F.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.slack.history(self.st.channel, "%.6f" % start.timestamp())
        return [F.post_text(m) for m in rows if not (m.get("thread_ts") and m.get("thread_ts") != m.get("ts"))]

    def post(self, text: str, username: str) -> str:
        body = {"channel": self.st.channel, "text": F.plain(text), "username": username, "unfurl_links": False}
        if self.st.icon:
            body["icon_url"] = self.st.icon
        r = self.slack.call("chat.postMessage", body)
        if not r.get("ok"):
            raise RuntimeError("Slack post: %s" % r.get("error"))
        return str(r.get("ts") or "")

    # the logs
    def logs_page(self, app: str, token: str) -> tuple[list[dict], str]:
        """Up to 100 log entries after `token` (a nanosecond time, as the API hands it back), oldest first."""
        if self._auth is None:
            self._auth = fly_auth(self.sh)
        if not self._auth:
            raise RuntimeError("no Fly token on this box")
        url = LOGS_API % (app, urllib.parse.urlencode({"next_token": token or ""}))
        req = urllib.request.Request(url, headers={"Authorization": self._auth, "Accept": "application/json",
                                                   "User-Agent": "siftstack-watch"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        rows = [x.get("attributes") or {} for x in d.get("data") or [] if isinstance(x, dict)]
        return rows, str((d.get("meta") or {}).get("next_token") or token or "")

    def fixer_ledger(self) -> dict:
        return F.load_ledger()


# ---------------------------------------------------------------------- state
def state_path() -> Path:
    return F.STATE / "watch.json"


def load_state() -> dict:
    try:
        d = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("watchdogs", {})
            d.setdefault("logs", {"apps": {}, "shapes": {}, "noise": {}, "posts": {}})
            return d
    except (OSError, ValueError):
        pass
    return {"watchdogs": {}, "logs": {"apps": {}, "shapes": {}, "noise": {}, "posts": {}}}


def save_state(state: dict) -> None:
    F.STATE.mkdir(parents=True, exist_ok=True)
    tmp = F.STATE / "watch.json.tmp"
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, state_path())


class WatchLock:
    """One tick at a time (a tick run by hand must not race the thread's)."""

    def __init__(self):
        self.path = F.STATE / "watch.lock"

    def __enter__(self):
        F.STATE.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                at = datetime.fromisoformat(json.loads(self.path.read_text(encoding="utf-8"))["at"])
                if F.now_utc() - at < timedelta(seconds=TICK_TIMEOUT_S + 60):
                    raise RuntimeError("another tick holds the watch lock since %s" % at.isoformat(timespec="seconds"))
            except (ValueError, KeyError):
                pass
        self.path.write_text(json.dumps({"pid": os.getpid(), "at": F.now_utc().isoformat()}), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


LOGTIME_RX = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$")


def _when(iso: str) -> datetime | None:
    """A time the state file or the logs API wrote: ISO, with or without a zone, with up to nine digits of
    fraction (the logs API writes nanoseconds, which datetime does not read)."""
    s = str(iso or "").strip()
    m = LOGTIME_RX.match(s)
    if m:
        s = m.group(1) + ("." + m.group(2)[:6] if m.group(2) else "") + ("+00:00" if m.group(3) in (None, "Z")
                                                                         else m.group(3))
    try:
        t = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def mode(st: F.Settings, key: str) -> str:
    if (F.STATE / "WATCH_OFF").exists():
        return "off"
    v = (st.env.get(key) or os.environ.get(key) or "on").strip().lower()
    if v in ("off", "0", "false", "no"):
        return "off"
    return "quiet" if v == "quiet" else "on"


# ---------------------------------------------------------------------- the watchdogs
def due(name: str, ws: dict, now: datetime) -> bool:
    """Is this watchdog's check due on this tick? Times are kept, never the clock's minute, so a late tick
    still runs what is due."""
    last = _when(ws.get("last_at"))
    since = (now - last).total_seconds() / 60.0 if last else 1e9
    if name == "ftm":
        local = now.astimezone(F.TZ)
        if (local.hour, local.minute) < FTM_AT or ws.get("settled_day") == local.date().isoformat():
            return False
        return since >= TICK_MIN - 0.5
    pending = int(ws.get("streak") or 0) > 0
    every = TICK_MIN if (name == "sms" or pending) else 15
    return since >= every - 0.5


def deliver(world: World, name: str, status: str, detail: str, now: datetime, quiet: bool) -> str:
    """Say one watchdog's red: through the relay, else straight to the channel unless the channel already has
    it today. Returns how it went out ("relay", "direct", "already", "quiet"), or "" when it could not."""
    if quiet:
        F.log("watch: would say %s is %s: %s" % (name, status, detail[:200].replace("\n", " ")))
        return "quiet"
    url = world.relay_url()
    if url:
        ok, why = world.relay(url, raw_message(name, status, detail))
        if ok:
            return "relay"
        F.log("watch: the relay did not take the %s watchdog's post (%s); it goes straight to the channel" % (name, why))
    if not (world.slack and world.st.channel):
        F.log("watch: no Slack token or issues channel, so the %s watchdog's red was not said" % name)
        return ""
    try:
        texts = world.today_texts(now)
    except Exception as exc:  # noqa: BLE001  (a missed alarm costs more than a second one)
        F.log("watch: today's posts could not be read (%s); the %s watchdog's red goes out" % (short(exc), name))
        texts = []
    if said_today(texts, WATCHDOGS[name]["marker"]):
        return "already"
    try:
        world.post(direct_text(name, status, detail), WATCH_NAME)
        return "direct"
    except Exception as exc:  # noqa: BLE001  (the next tick tries again)
        F.log("watch: could not post the %s watchdog's red: %s" % (name, short(exc)))
        return ""


def run_watchdogs(world: World, state: dict, now: datetime, quiet: bool, force: bool = False,
                  only: tuple = ()) -> list[str]:
    lines: list[str] = []
    day = F.local_day(now)
    for name in WATCHDOGS:
        if only and name not in only:
            continue
        ws = state["watchdogs"].setdefault(name, {})
        if not (force or due(name, ws, now)):
            continue
        try:
            status, detail = world.check(name)
        except Exception as exc:  # noqa: BLE001  (a check that cannot run is a box nobody can see)
            status, detail = "unreachable", "The check itself could not run (%s)." % short(exc)
        bad = status in BAD
        was = ws.get("status")
        ws.update(last_at=now.isoformat(), status=status, detail=str(detail)[:600])
        ws["streak"] = (int(ws.get("streak") or 0) + 1) if bad else 0
        if was != status:
            F.log("watch: %s is %s: %s" % (name, status, str(detail)[:200].replace("\n", " ")))
        said = "%s %s: %s" % (name, status, str(detail)[:160].replace("\n", " "))
        if not bad:
            if name == "ftm":
                ws["settled_day"] = day
            lines.append(said)
            continue
        if ws["streak"] < CONFIRM:
            lines.append(said + " (seen once: checked again in 5 minutes before anyone hears)")
            continue
        if ws.get("posted_day") == day:
            if name == "ftm":
                ws["settled_day"] = day
            lines.append(said + " (already said today)")
            continue
        how = deliver(world, name, status, str(detail), now, quiet)
        if how:
            ws.update(posted_day=day, posted_how=how, posted_at=now.isoformat())
            if name == "ftm":
                ws["settled_day"] = day
            if how != "quiet":
                F.log("watch: said %s is %s (%s)" % (name, status, how))
        lines.append(said + (" (said: %s)" % how if how else " (NOT said yet: tried again next tick)"))
    return lines


# ---------------------------------------------------------------------- the log watch: reading
TB_HEAD = "Traceback (most recent call last):"
CHAIN = ("During handling of the above exception", "The above exception was the direct cause")
FRAME_RX = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
LEVEL_RX = re.compile(r"^\s*(?:\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[,.]\d+)?(?:Z|[+-]\d\d:?\d\d)?\s+)?"
                      r"(?:\[(?:ERROR|CRITICAL|FATAL)\]|(?:ERROR|CRITICAL|FATAL)\b:?)\s*")
# Our own code's printed failures, which carry no level: the uploader's "NOTE NOT POSTED", "FAIL" and
# "STOPPED" (a refused note on a retired route printed one of these per record for days) and the sold
# guard's "STOPPED: ... did not read back". Upper case at the start of a line only, so the run report's
# "[FAILED]" (already said through the relay) never matches. The marker stays in the words.
PRINT_RX = re.compile(r"^\s*(NOTE NOT POSTED|FAIL|STOPPED)\b:?\s*")
# A line that starts a new log record: a logging timestamp, uvicorn's "INFO:", or Fly's init (" INFO ...").
# Anything else that follows an error line within CONTINUE_S is the rest of that record: an exception's
# message can carry a whole HTML page (DataSift's retired add-notes route answered one, 2026-11-18) between
# "task failed" and its traceback.
NEW_RECORD_RX = re.compile(r"^(?:\s*\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d|(?:DEBUG|INFO|WARNING|ERROR|CRITICAL):|"
                           r"\s(?:DEBUG|INFO|WARN|ERROR)\s)")
CONTINUE_S = 2.0
CRASH_NOISE = ("KeyboardInterrupt", "SystemExit")
# Lines that are expected, each with why. The fixer's own verdicts do the rest: a post it sorts as anything
# but a fixed bug is never posted again.
NOISE = (
    ("scrapfly", "the Scrapfly SDK logs every refused scrape at CRITICAL; its caller retries or falls back",
     re.compile(r"^scrapfly\.api\b")),
    ("smarty", "Smarty has been lapsed since September and the pipeline no longer needs it for a ZIP",
     re.compile(r"Smarty batch API error: Active subscription required")),
)
PRE_NOISE = (
    ("task_failed", "the brain posts every failed task itself (issues, or the asker's thread)",
     re.compile(r"^agents\.worker: task \S+ failed")),
)


def level_text(msg: str) -> str:
    """The words after an error level ("ERROR name: words"), the words of a printed failure with its marker
    ("NOTE NOT POSTED: words"), or "" for a line that is neither."""
    m = LEVEL_RX.match(msg)
    if m:
        return msg[m.end():].strip()
    m = PRINT_RX.match(msg)
    return ("%s: %s" % (m.group(1), msg[m.end():].strip())).strip(": ") if m else ""


def _frame(lines: list[str]) -> tuple[str, str, str]:
    frames = [m.groups() for m in (FRAME_RX.match(ln) for ln in lines) if m]
    ours = [f for f in frames if "site-packages" not in f[0] and "/lib/python" not in f[0]]
    f = (ours or frames or [("", "", "")])[-1]
    path = re.sub(r"^/app/", "", f[0])
    path = re.sub(r"^.*/site-packages/", "site-packages/", path)
    return path, f[1], f[2]


def _new_open(at: str, pre: str) -> dict:
    return {"lines": [TB_HEAD], "exc": "", "after": False, "at": at, "pre": pre}


def _keep_line(tb: dict, msg: str) -> None:
    """A traceback's lines, the last 80 only: the innermost frames are the ones that name the code, and a deep
    recursion must not grow the state file."""
    tb["lines"].append(msg[:500])
    del tb["lines"][:-80]


def scan(rows: list[dict], keep: dict) -> list[dict]:
    """The error-level lines and tracebacks in log rows, in order, one event each. A traceback is one event
    (its last exception, after any chained ones), and the error line logged with it rides along as its
    context. `keep` carries per machine what a page or a tick boundary cut in two."""
    events: list[dict] = []
    open_ = keep.setdefault("open", {})
    pend = keep.setdefault("pending", {})
    for r in rows:
        if (((r.get("meta") or {}).get("event") or {}).get("provider") or "app") != "app":
            continue
        inst = str(r.get("instance") or "")
        msg = ANSI.sub("", str(r.get("message") or "")).rstrip("\r\n")
        at = str(r.get("timestamp") or "")
        tb = open_.get(inst)
        if tb is not None:
            if msg.startswith(TB_HEAD):
                tb.update(after=False, exc="")
                _keep_line(tb, msg)
                continue
            if not tb["after"]:
                if not msg.strip() or msg[:1] in (" ", "\t"):
                    _keep_line(tb, msg)
                    continue
                tb.update(exc=msg.strip(), after=True)
                _keep_line(tb, msg)
                continue
            if not msg.strip() or msg.startswith(CHAIN):
                continue
            events.append(_crash(open_.pop(inst)))
        p = pend.get(inst)
        near = bool(p) and _near(p["at"], at)
        if msg.startswith(TB_HEAD):
            pend.pop(inst, None)
            if p and not near:
                events.append({"kind": "error", "text": p["text"], "at": p["at"]})
            open_[inst] = _new_open(at, p["text"] if near else "")      # the error it was logged with rides along
            continue
        text = level_text(msg)
        if p and near and not text and not NEW_RECORD_RX.match(msg):
            continue                                  # the rest of the pending error's record
        if p:
            pend.pop(inst, None)
            events.append({"kind": "error", "text": p["text"], "at": p["at"]})
        if text:
            pend[inst] = {"text": text, "at": at}
    return events


def _near(a: str, b: str) -> bool:
    ta, tb = _when(a), _when(b)
    return bool(ta and tb) and abs((tb - ta).total_seconds()) <= CONTINUE_S


def _crash(tb: dict) -> dict:
    path, line, func = _frame(tb["lines"][-60:])
    return {"kind": "crash", "exc": tb["exc"] or "a traceback that never named its exception", "at": tb["at"],
            "file": path, "line": line, "func": func, "pre": tb.get("pre", "")}


def flush(keep: dict, now: datetime, force: bool = False) -> list[dict]:
    """What waited at the end of a read: an error line no traceback followed, a traceback that never got
    another line after its exception. Kept a little while, since the next line may come on the next page."""
    events: list[dict] = []
    for inst, p in list((keep.get("pending") or {}).items()):
        at = _when(p.get("at"))
        if force or not at or (now - at).total_seconds() > 30:
            events.append({"kind": "error", "text": p["text"], "at": p["at"]})
            keep["pending"].pop(inst)
    for inst, tb in list((keep.get("open") or {}).items()):
        at = _when(tb.get("at"))
        age = (now - at).total_seconds() if at else 1e9
        if force or (tb["after"] and age > 30) or age > 600:
            events.append(_crash(keep["open"].pop(inst)))
    return events


# ---------------------------------------------------------------------- the log watch: shapes and posts
def noise_of(ev: dict) -> str:
    """Why an event is expected, or "" when it is not."""
    if ev["kind"] == "crash":
        if ev["exc"].split(":", 1)[0].strip() in CRASH_NOISE:
            return "restart"
        for name, _why, rx in PRE_NOISE:
            if ev.get("pre") and rx.search(mask(ev["pre"])):
                return name
        return ""
    for name, _why, rx in NOISE + PRE_NOISE:
        if rx.search(mask(ev["text"])):
            return name
    return ""


def canonical(ev: dict) -> str:
    if ev["kind"] == "crash":
        return mask("%s in %s %s" % (ev["exc"], ev["file"], ev["func"])).strip()
    return mask(ev["text"]).strip()


def fold_events(lw: dict, app: str, events: list[dict], now: datetime) -> int:
    """Each event into the log watch's ledger by shape, the way the fixer folds issues."""
    day = F.local_day(now)
    if lw.get("noise_day") != day:
        lw["noise"], lw["noise_day"] = {}, day
    n = 0
    for ev in events:
        why = noise_of(ev)
        if why:
            lw["noise"][why] = int(lw["noise"].get(why) or 0) + 1
            continue
        text = canonical(ev)
        key = F.shape_key("%s %s" % (app, text))
        s = lw["shapes"].get(key)
        if s is None:
            s = lw["shapes"][key] = {"id": key, "app": app, "kind": ev["kind"], "text": text[:600], "seen": 0,
                                     "first_at": ev["at"], "day": day, "hits": [], "posted_day": "", "posts": []}
        if ev["kind"] == "crash" and "where" not in s:      # the first one seen, so the post keeps one shape
            s["where"] = "%s line %s, in %s" % (ev["file"], ev["line"], ev["func"]) if ev["file"] else ""
            s["pre"] = mask(ev.get("pre") or "")[:300]
        if s.get("day") != day:
            s["day"], s["hits"] = day, []
        s["hits"] = (s["hits"] + [ev["at"]])[-LOG_HITS_KEPT:]
        s["seen"] = int(s.get("seen") or 0) + 1
        s["last_at"] = ev["at"]
        n += 1
    return n


def _clock(at: str) -> str:
    t = _when(at)
    return t.astimezone(F.TZ).strftime("%H:%M") if t else ""


def post_text_for(s: dict, hits: list[str]) -> str:
    """The issue post for one shape. The counts sit in parentheses and the line numbers are digits, so one
    problem keeps one shape in the fixer's ledger from day to day."""
    name = APP_NAMES.get(s["app"], s["app"])
    n = len(hits)
    times = "%d time%s today, last at %s" % (n, "" if n == 1 else "s", _clock(hits[-1]) if hits else "")
    if s["kind"] == "crash":
        lines = ["Log watch: %s logged a crash (%s)." % (name, times), s["text"]]
        if s.get("where"):
            lines.append("Where: %s" % s["where"])
        if s.get("pre"):
            lines.append("Logged with: %s" % s["pre"])
    else:
        lines = ["Log watch: %s keeps logging the same error (%s)." % (name, times), s["text"]]
    lines.append("Read more: fly logs -a %s --no-tail (only about the last 100 lines stay there)." % s["app"])
    return F.plain("\n".join(lines))


def hits_that_count(s: dict, led: dict, text: str) -> tuple[list[str], str]:
    """Today's hits that still count, and why none do: the fixer's verdict on this post decides. A post it
    sorted as anything but a fixed bug is never said again; after a fix only what came after it counts."""
    iss = (led.get("issues") or {}).get(F.shape_key(text))
    if not iss:
        return list(s.get("hits") or []), ""
    status = iss.get("status") or ""
    if status not in ("fixed", "fixed_with"):
        return [], "the fixer has it (%s)" % status
    fixed = _when(iss.get("fixed_at") or "")
    if not fixed:
        return [], "the fixer has it (fixed, no time)"
    cutoff = fixed + F.REGRESSION_GRACE
    later = [h for h in s.get("hits") or [] if (_when(h) or cutoff) > cutoff]
    return later, ("" if later else "fixed, and not seen since the fix")


def surface(world: World, lw: dict, now: datetime, quiet: bool) -> list[str]:
    """Post the shapes that qualify: a crash, or the same error LOG_REPEAT times today. Once a business day each."""
    day = F.local_day(now)
    lines: list[str] = []
    try:
        led = world.fixer_ledger()
    except Exception:  # noqa: BLE001
        led = {"issues": {}}
    posted_today = lw["posts"].get(day, [])
    for key, s in sorted(lw["shapes"].items(), key=lambda kv: str(kv[1].get("last_at") or "")):
        if s.get("day") != day or not s.get("hits") or s.get("posted_day") == day:
            continue
        probe = post_text_for(s, s["hits"])
        hits, why = hits_that_count(s, led, probe)
        enough = len(hits) >= (1 if s["kind"] == "crash" else LOG_REPEAT)
        if not enough:
            if why:
                s["held"] = why
            continue
        if len(posted_today) >= LOG_MAX_POSTS_DAY:
            lines.append("%s waits: %d log posts were made today already" % (key, len(posted_today)))
            continue
        text = post_text_for(s, hits)
        if quiet:
            F.log("watch: the log watch would post: %s" % text.replace("\n", " | ")[:300])
            s["posted_day"] = day
            continue
        if not (world.slack and world.st.channel):
            lines.append("no Slack token or issues channel: %s not posted" % key)
            continue
        try:
            world.post(text, LOG_NAME)
        except Exception as exc:  # noqa: BLE001  (tried again next read)
            F.log("watch: could not post log shape %s: %s" % (key, short(exc)))
            continue
        s["posted_day"] = day
        s["posts"] = (s.get("posts") or [])[-19:] + [now.isoformat()]
        posted_today = posted_today + [key]
        lw["posts"][day] = posted_today
        F.log("watch: posted log shape %s from %s" % (key, s["app"]))
        lines.append("posted %s (%s, %d today)" % (key, s["app"], len(hits)))
    for d in [d for d in lw["posts"] if d < (now - timedelta(days=7)).astimezone(F.TZ).date().isoformat()]:
        lw["posts"].pop(d, None)
    return lines


def ns(t: datetime) -> str:
    return str(int(t.timestamp() * 1_000_000_000))


def read_app(world: World, la: dict, app: str, now: datetime, deadline: float | None = None) -> list[dict]:
    """A box's log lines since the last read, page by page, until they run out or the read's time is up (the
    rest waits for the next read: the stopping point is kept)."""
    token = str(la.get("token") or "")
    if not token:
        token = ns(now)                     # the first look starts now: nothing from before the watch existed
    elif token.isdigit() and (now.timestamp() - int(token) / 1e9) > LOG_GAP_HOURS * 3600:
        F.log("watch: %s's logs were last read %s; reading the last hour only" % (app, token))
        token = ns(now - timedelta(hours=1))
    rows: list[dict] = []
    deadline = deadline or (time.monotonic() + LOG_BUDGET_S)
    for _ in range(LOG_MAX_PAGES):
        page, nxt = world.logs_page(app, token)
        if not page:
            break
        rows += page
        token = nxt or token
        if time.monotonic() > deadline:
            break
    la["token"] = token
    return rows


def run_logs(world: World, state: dict, now: datetime, quiet: bool, force: bool = False) -> list[str]:
    lw = state["logs"]
    last = _when(lw.get("last_at"))
    if not force and last and (now - last).total_seconds() < (LOG_EVERY_MIN - 0.5) * 60:
        return []
    lw["last_at"] = now.isoformat()
    lines: list[str] = []
    apps = [a.strip() for a in (world.st.env.get("ISSUE_FIXER_LOG_APPS") or ",".join(LOG_APPS)).split(",") if a.strip()]
    day = F.local_day(now)
    deadline = time.monotonic() + LOG_BUDGET_S          # one budget for every box's read in this tick
    for app in apps:
        la = lw["apps"].setdefault(app, {})
        try:
            rows = read_app(world, la, app, now, deadline)
        except Exception as exc:  # noqa: BLE001
            la["fails"] = int(la.get("fails") or 0) + 1
            la["error"] = short(exc)
            lines.append("%s: logs not read (%s)" % (app, la["error"]))
            if la["fails"] >= LOG_FAILS_TO_SAY and la.get("said_day") != day and not quiet and world.slack:
                try:
                    world.post("Log watch: could not read the logs of %s (%s), so an error it logs never reaches "
                               "this channel.\nThe issue fixer's box reads them with its Fly token "
                               "(ISSUE_FIXER_FLY_TOKEN)." % (APP_NAMES.get(app, app), la["error"]), LOG_NAME)
                    la["said_day"] = day
                except Exception as exc2:  # noqa: BLE001
                    F.log("watch: could not post that %s's logs are unread: %s" % (app, short(exc2)))
            continue
        la.update(fails=0, error="", read_at=now.isoformat())
        events = scan(rows, la) + flush(la, now)
        n = fold_events(lw, app, events, now)
        if rows or n:
            lines.append("%s: %d new line(s), %d error(s) kept" % (app, len(rows), n))
    lines += surface(world, lw, now, quiet)
    for key in [k for k, s in lw["shapes"].items() if (_when(s.get("last_at")) or now) < now - timedelta(days=30)]:
        lw["shapes"].pop(key, None)
    return lines


# ---------------------------------------------------------------------- the tick and the thread
def tick(dry: bool = False, force: bool = False, only: tuple = (), now: datetime | None = None,
         st: F.Settings | None = None, world: World | None = None) -> list[str]:
    now = now or F.now_utc()
    st = st or F.Settings()
    wmode, lmode = mode(st, "ISSUE_FIXER_WATCH"), mode(st, "ISSUE_FIXER_LOGWATCH")
    if wmode == "off" and lmode == "off":
        return ["the watch is off"]
    world = world or World(st)
    out: list[str] = []
    with WatchLock():
        state = load_state()
        if wmode != "off" and (not only or set(only) & set(WATCHDOGS)):
            out += run_watchdogs(world, state, now, quiet=dry or wmode == "quiet", force=force, only=only)
            if not dry:
                save_state(state)          # a red said is kept before the log read, which may run long
        if lmode != "off" and (not only or "logs" in only):
            out += run_logs(world, state, now, quiet=dry or lmode == "quiet", force=force)
            if not dry:
                save_state(state)
    return out


def seconds_to_next(now: datetime, every_min: int = TICK_MIN, offset_s: int = 60) -> float:
    """Seconds to the next tick: every 5 minutes on the clock, a minute after the mark, so a tick never starts
    on the same second as the fixer's own quarter-hour run."""
    base = now.replace(second=0, microsecond=0)
    nxt = base - timedelta(minutes=base.minute % every_min) + timedelta(seconds=offset_s)
    while (nxt - now).total_seconds() < 30:
        nxt += timedelta(minutes=every_min)
    return (nxt - now).total_seconds()


def start(run_tick=None, stop: threading.Event | None = None) -> threading.Thread:
    """The watch's own thread in the fixer's loop: a tick in a fresh process every 5 minutes, with a time
    limit, so a hung fly ssh costs one tick and never the loop."""
    run_tick = run_tick or (lambda: subprocess.call([sys.executable, "-u", str(Path(__file__).resolve()), "tick"],
                                                    timeout=TICK_TIMEOUT_S))
    stop = stop or threading.Event()

    def body():
        while not stop.is_set():
            try:
                run_tick()
            except subprocess.TimeoutExpired:
                F.log("watch: a tick went past %ds and was stopped" % TICK_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                F.log("watch: a tick could not start: %s" % short(exc))
            stop.wait(seconds_to_next(F.now_utc()))

    t = threading.Thread(target=body, name="watch", daemon=True)
    t.start()
    return t


def status(_args=None) -> int:
    st = F.Settings()
    state = load_state()
    w = World(st, slack=False)
    print("watchdogs: %s; log watch: %s; relay %s" % (mode(st, "ISSUE_FIXER_WATCH"), mode(st, "ISSUE_FIXER_LOGWATCH"),
                                                      "set" if w.relay_url() else "NOT set (posts go straight to the channel)"))
    for name in WATCHDOGS:
        ws = state["watchdogs"].get(name) or {}
        print("  %-7s %-12s %-26s said %-10s %s" % (name, ws.get("status") or "-", ws.get("last_at", "-")[:19],
                                                     ws.get("posted_day") or "-", str(ws.get("detail") or "")[:90]))
    lw = state["logs"]
    print("log watch: last read %s; noise today so far %s" % (str(lw.get("last_at") or "-")[:19], lw.get("noise") or {}))
    for app, la in (lw.get("apps") or {}).items():
        print("  %-17s read %s, %s" % (app, str(la.get("read_at") or "-")[:19],
                                       ("failing: %s" % la.get("error")) if la.get("fails") else "ok"))
    day = F.local_day()
    for s in sorted(lw.get("shapes", {}).values(), key=lambda s: str(s.get("last_at") or ""), reverse=True)[:12]:
        print("  %s %-17s %-5s today %-3s seen %-4s posted %-10s %s" % (
            s["id"], s["app"], s["kind"], len(s.get("hits") or []) if s.get("day") == day else 0, s.get("seen"),
            s.get("posted_day") or "-", s["text"][:80]))
    return 0


def relay_check(world: World | None = None) -> int:
    """Ask the brain's relay, dry (it posts nothing), how it reads a watchdog's message: the cutover check that
    the relay URL is right and the brain is new enough to keep one post per watchdog a day."""
    world = world or World(F.Settings(), slack=False)
    url = world.relay_url()
    if not url:
        print("no relay URL: set ISSUE_FIXER_RELAY_URL (or AGENTS_RELAY_SECRET); posts go straight to the channel")
        return 2
    try:
        got = world.relay_dry(url, raw_message("agents", "red", "A relay check from the watch. Nothing is posted."))
    except urllib.error.HTTPError as e:
        print("the relay answered HTTP %d: %s" % (e.code, "a wrong secret in the URL" if e.code == 404 else "see the brain's logs"))
        return 1
    except Exception as exc:  # noqa: BLE001
        print("the relay did not answer: %s" % short(exc))
        return 1
    posts = got.get("posts") or []
    if got.get("what") != "agents_watchdog" or not posts or posts[0].get("group") != "watchdog_agents":
        print("the brain's relay reads a watchdog message as %r: deploy the brain first (src/agents/relay.py)" % got.get("what"))
        return 1
    print("relay ok: a watchdog's red goes to %s, one a day (%s)" % (posts[0].get("kind"), posts[0].get("group")))
    return 0


def switch(on: bool) -> int:
    F.STATE.mkdir(parents=True, exist_ok=True)
    p = F.STATE / "WATCH_OFF"
    if on:
        try:
            p.unlink()
        except OSError:
            pass
    else:
        p.write_text(F.now_utc().isoformat(), encoding="utf-8")
    print("the watch is", "on" if on else "off")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The issue fixer's box watches the other boxes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--dry", action="store_true", help="check and read, post nothing, keep nothing")
    t.add_argument("--force", action="store_true", help="run every check now, whatever its schedule")
    t.add_argument("--only", default="", help="comma list of ftm, agents, sms, logs")
    sub.add_parser("status")
    sub.add_parser("relay-check", help="ask the brain's relay, dry, how it reads a watchdog's message")
    sub.add_parser("off")
    sub.add_parser("on")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "tick":
            only = tuple(x.strip() for x in args.only.split(",") if x.strip())
            for line in tick(dry=args.dry, force=args.force, only=only):
                if args.dry or args.force:
                    print(line)
            return 0
        if args.cmd == "status":
            return status(args)
        if args.cmd == "relay-check":
            return relay_check()
        return switch(args.cmd == "on")
    except Exception as exc:  # noqa: BLE001  (a crash is logged with its cause; the next tick starts clean)
        F.log("watch: crashed: %s" % short(exc, 400))
        return 1


if __name__ == "__main__":
    sys.exit(main())
