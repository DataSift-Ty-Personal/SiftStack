"""The cold email program's daily jobs. The agents brain runs them as stages (agents/autopilot/coldemail.py);
the command line below is the same code, for a run by hand.

    python src/cold_email/daily.py topup [--commit]   # nightly: keep about 3 sending days of leads queued
    python src/cold_email/daily.py health [--post]    # weekday mornings: post to the issues channel only if wrong
    python src/cold_email/daily.py stats [--post]     # Mondays: last week's numbers to the pipeline channel

topup reads the campaign's own counts from Instantly (leads loaded minus leads contacted), and when
fewer than three days of sending are left, builds the next list from the same preset (build_list.py,
ZeroBounce on, every guard) and loads it. It never loads an owner twice (ledger.json).
health checks what the course says to watch every morning: every inbox connected and warming, warmup
health in the 90s, bounces under 2%, the campaign running, leads left to send, and that the reply
watcher ran in the last half hour. A healthy morning posts nothing.

The work and the posting are apart: topup_run, health_run and stats_text return what they found, and
the brain's stages turn that into their own posts (the board, #issues). topup, health and stats below
post to Slack themselves, as the scheduled tasks on the PC did. A --commit or --post run happens only on
the install home.json names (coldstate.py).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coldstate  # noqa: E402
from instantly import ROOT, Instantly  # noqa: E402
from setup_accounts import env  # noqa: E402

ET = ZoneInfo("America/New_York")
CAMPAIGN = "Agency queue email 1"
PRESET = "Agency - 01 Queue"
DAYS_QUEUED = 3
MIN_BATCH = 100
BUILD_TIMEOUT_S = 3 * 3600
LOAD_TIMEOUT_S = 1800
STATE_DIR = coldstate.state_dir()
REPLIES_LOG = STATE_DIR / "replies.jsonl"
# The Windows task wrote the reply watcher's output here; a pass now writes its own heartbeat
# (coldstate.HEARTBEAT_FILE), and this file is only read when no heartbeat exists yet.
TASK_LOG = STATE_DIR / "replies_task.log"
STATE = STATE_DIR / "daily_state.json"
# A pause on purpose ({"until": "YYYY-MM-DD", "why": ..., "since": ...}): health stays quiet until the date,
# then says the campaign is still paused. campaign.py launch removes it.
HOLD = STATE_DIR / "hold.json"
# How long the reply watcher may go quiet in business hours before health says so (it runs every 5).
WATCHER_LATE_MIN = 30
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def slack(kind: str, text: str) -> bool:
    channel = env("AGENTS_SLACK_ISSUES_CHANNEL" if kind == "issues" else "AGENTS_SLACK_PIPELINE_CHANNEL") \
        or env("AGENTS_SLACK_CHANNEL")
    token = env("SLACK_BOT_TOKEN")
    if not (token and channel):
        print("(no Slack settings; not posted)")
        return False
    r = requests.post("https://slack.com/api/chat.postMessage", timeout=20, headers={"Authorization": f"Bearer {token}"},
                      json={"channel": channel, "text": text, "username": "Cold email", "unfurl_links": False})
    ok = bool(r.ok and r.json().get("ok"))
    if not ok:
        print("Slack refused:", r.text[:200])
    return ok


def campaign(api: Instantly, name: str = CAMPAIGN) -> dict:
    c = next((c for c in api.campaigns() if c.get("name") == name), None)
    if not c:
        raise SystemExit(f"no campaign named {name!r}")
    return c


def analytics(api: Instantly, cid: str, days: int | None = None) -> dict:
    # `ids`, not `id`: with id= the endpoint answers an empty list (read 2026-11-16), which read as
    # "0 leads queued" and would have made every nightly top-up build a full list again.
    p = {"ids": cid}
    if days:
        p["start_date"] = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
        p["end_date"] = datetime.now(ET).strftime("%Y-%m-%d")
    r = api.call("GET", "/campaigns/analytics", params=p)
    rows = r if isinstance(r, list) else (r.get("items") or [r])
    row = next((x for x in rows if x.get("campaign_id") == cid), None)
    if row is None:
        raise RuntimeError("Instantly returned no analytics for the campaign; refusing to guess a count")
    return row


def _tail(run, n: int = 300) -> str:
    """The end of a child's output for a person to read, with every email address taken out (a build's
    error can quote a homeowner's address, and this goes to Slack)."""
    t = ((getattr(run, "stderr", "") or "") or (getattr(run, "stdout", "") or ""))[-n:].strip()
    return _EMAIL_RX.sub("<email>", t)


def _ledger_size() -> int:
    try:
        return len((json.loads((coldstate.state_dir() / "ledger.json").read_text(encoding="utf-8")) or {}).get("owners") or {})
    except (OSError, ValueError, AttributeError):
        return 0


def topup_run(commit: bool, *, name: str = CAMPAIGN, preset: str = PRESET, days_queued: int = DAYS_QUEUED,
              min_batch: int = MIN_BATCH, build_timeout_s: int = BUILD_TIMEOUT_S) -> dict:
    """The nightly top-up, and what happened, posting nothing:
        action    enough | low_credits | would_build | built | build_failed | load_failed | busy
        left      leads loaded and not yet emailed;  daily  the campaign's daily limit
        want      leads wanted tonight (days_queued x daily - left)
        credits   ZeroBounce credits before the build (0 when not read)
        loaded    owners added to the ledger tonight
        problems  sentences for a person (the command line prefixes "Cold email: " and posts them)
        code      the command line's exit code
    A committed top-up holds the `topup` lock: a second one on the same machine skips."""
    out = {"action": "", "left": 0, "daily": 0, "want": 0, "credits": 0, "loaded": 0, "problems": [], "code": 0,
           "slug": ""}
    lock = None
    if commit:
        lock = coldstate.RunLock("topup", coldstate.state_dir())
        if not lock.acquire():
            out["action"] = "busy"
            return out
    try:
        return _topup(commit, out, name, preset, days_queued, min_batch, build_timeout_s)
    finally:
        if lock is not None:
            lock.release()


def _topup(commit: bool, out: dict, name: str, preset: str, days_queued: int, min_batch: int,
           build_timeout_s: int) -> dict:
    api = Instantly()
    c = campaign(api, name)
    a = analytics(api, c["id"])
    daily = int(c.get("daily_limit") or 250)
    left = int(a.get("leads_count") or 0) - int(a.get("contacted_count") or 0)
    want = days_queued * daily - left
    out.update(left=left, daily=daily, want=max(0, want))
    print(f"{left} leads not yet emailed, {daily} a day; {max(0, want)} wanted")
    if want < min_batch:
        print("enough queued; nothing to do")
        out["action"] = "enough"
        return out
    from zerobounce import ZeroBounce
    credits = ZeroBounce().credits()
    out["credits"] = credits
    if credits < want * 2:
        out.update(action="low_credits", code=2)
        out["problems"].append(f"ZeroBounce is down to {credits:,} credits, about {want * 2:,} are needed tonight. "
                               "The list was not topped up.")
        return out
    if not commit:
        print(f"dry run: would build about {want} leads from {preset!r} and load them")
        out["action"] = "would_build"
        return out
    # Its own folder name per night: on 11-16 the top-up reused the first list's folder (same date),
    # found that list finished and loaded 0 new leads. A re-run the same night still resumes.
    slug = "agency_queue_topup_" + datetime.now(ET).strftime("%Y%m%d")
    out["slug"] = slug
    py = sys.executable
    try:
        run = subprocess.run([py, "-u", str(HERE / "build_list.py"), "--preset", preset, "--target", str(want), "--verify",
                              "--slug", slug], cwd=str(ROOT), capture_output=True, text=True, timeout=build_timeout_s)
    except subprocess.TimeoutExpired:
        out.update(action="build_failed", code=2)
        out["problems"].append("tonight's list build ran past %d minutes and was stopped, so no new leads were added. "
                               "The next top-up picks up where it left off." % (build_timeout_s // 60))
        return out
    print((run.stdout or "")[-3000:])
    leads = coldstate.state_dir() / "lists" / slug / "leads.json"
    if run.returncode not in (0,) or not leads.exists():
        out.update(action="build_failed", code=2)
        out["problems"].append(f"tonight's list build failed, so no new leads were added. It tries again tomorrow night. "
                               f"({_tail(run)})")
        return out
    before = _ledger_size()
    try:
        load = subprocess.run([py, "-u", str(HERE / "campaign.py"), "load", "--name", name, "--leads", str(leads)],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=LOAD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        out.update(action="load_failed", code=2)
        out["problems"].append("the new leads were built but did not load into Instantly in %d minutes. "
                               "The next top-up loads them." % (LOAD_TIMEOUT_S // 60))
        return out
    print((load.stdout or "")[-2000:])
    out["loaded"] = max(0, _ledger_size() - before)
    if load.returncode != 0:
        out.update(action="load_failed", code=2)
        out["problems"].append(f"the new leads were built but did not load into Instantly. ({_tail(load)})")
        return out
    out["action"] = "built"
    return out


def topup(commit: bool) -> int:
    r = topup_run(commit)
    for p in r["problems"]:
        slack("issues", "Cold email: " + p)
    return r["code"]


def _watcher_age_min() -> float | None:
    """Minutes since the reply watcher's last committed pass: its heartbeat, else (a workstation that
    has not run the new code yet) the Windows task's log. None when neither exists."""
    hb = coldstate.heartbeat(STATE_DIR)
    if hb.get("at"):
        try:
            t = datetime.fromisoformat(str(hb["at"]).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - t).total_seconds() / 60
        except ValueError:
            pass
    if TASK_LOG.exists():
        return (time.time() - TASK_LOG.stat().st_mtime) / 60
    return None


def health_run(name: str = CAMPAIGN) -> list[str]:
    """What is wrong this morning, as sentences (an empty list on a healthy morning). Posts nothing."""
    api = Instantly()
    problems = []
    accs = api.accounts()
    bad = [a["email"] for a in accs if a.get("status") != 1]
    if bad:
        problems.append(f"{len(bad)} of {len(accs)} inboxes are not connected ({', '.join(sorted(bad)[:3])})")
    cold = [a["email"] for a in accs if a.get("warmup_status") != 1]
    if cold:
        problems.append(f"warmup is off on {len(cold)} inboxes")
    try:
        wa = api.warmup_analytics([a["email"] for a in accs]).get("aggregate_data") or {}
        low = [e for e, v in wa.items() if isinstance(v, dict) and v.get("health_score") is not None
               and float(v["health_score"]) < 90]
        if low:
            problems.append(f"warmup health is under 90 on {len(low)} inboxes")
    except Exception as exc:
        problems.append(f"could not read warmup health ({str(exc)[:80]})")
    c = campaign(api, name)
    if c.get("status") != 1:
        hold = json.loads(HOLD.read_text(encoding="utf-8-sig")) if HOLD.exists() else {}   # -sig: a PowerShell-written file
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if hold and today <= hold.get("until", ""):
            print(f"paused on purpose until {hold['until']}: {hold.get('why', '')}")
        elif hold:
            problems.append(f"the campaign is still paused ({hold.get('why', 'on hold')}, since {hold.get('since', '?')}); "
                            "say go to resume it")
        else:
            problems.append(f"the campaign is not running (status {c.get('status')})")
    week = analytics(api, c["id"], days=7)
    sent, bounced = int(week.get("emails_sent_count") or 0), int(week.get("bounced_count") or 0)
    if sent >= 50 and bounced / sent > 0.02:
        problems.append(f"bounces are {bounced / sent:.1%} this week ({bounced} of {sent}); the course line is 2%")
    a = analytics(api, c["id"])
    left = int(a.get("leads_count") or 0) - int(a.get("contacted_count") or 0)
    if left <= 0:
        problems.append("no leads left to email; the nightly top-up did not add any")
    now = datetime.now(ET)
    age = _watcher_age_min()
    if age is None:
        problems.append("the reply watcher has never run")
    elif now.weekday() < 5 and 8 <= now.hour < 20 and age > WATCHER_LATE_MIN:
        problems.append(f"the reply watcher last ran {age:.0f} minutes ago (it runs every 5 minutes)")
    print("\n".join(problems) or "all healthy")
    return problems


def health(post: bool) -> list[str]:
    problems = health_run()
    if post and problems:
        day = datetime.now(ET).strftime("%Y-%m-%d")
        st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
        key = "|".join(sorted(p.split(" (")[0] for p in problems))
        if st.get("health_posted", {}).get(day) != key:
            slack("issues", "Cold email needs a look:\n" + "\n".join(f"- {p}" for p in problems))
            st.setdefault("health_posted", {})[day] = key
            coldstate.write_json(STATE, st)
    return problems


def stats_text(name: str = CAMPAIGN) -> str:
    """Last week's numbers, two lines: the sends and the replies, then what the replies said and what is left."""
    api = Instantly()
    c = campaign(api, name)
    w = analytics(api, c["id"], days=7)
    a = analytics(api, c["id"])
    since = datetime.now(timezone.utc) - timedelta(days=7)
    kinds = Counter()
    if REPLIES_LOG.exists():
        for line in REPLIES_LOG.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if datetime.fromisoformat(r["at"]) >= since:
                    kinds[r["kind"]] += 1
            except (ValueError, KeyError, TypeError):
                continue
    sent = int(w.get("emails_sent_count") or 0)
    replies = sum(kinds.values())
    left = int(a.get("leads_count") or 0) - int(a.get("contacted_count") or 0)
    from zerobounce import ZeroBounce
    text = (f"Cold email last week: {sent:,} sent, {replies} replies"
            + (f" ({replies / sent:.1%})" if sent else "") + f", {kinds['interested']} interested, "
            f"{int(w.get('bounced_count') or 0)} bounced.\n"
            f"{kinds['stop']} asked to stop, {kinds['not_interested']} said no, {kinds['wrong_person']} wrong person, "
            f"{kinds['needs_reply'] + kinds['sensitive']} needed an answer. {left:,} leads queued, "
            f"{ZeroBounce().credits():,} ZeroBounce credits left.")
    print(text)
    return text


def stats(post: bool) -> str:
    text = stats_text()
    if post:
        slack("pipeline", text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["topup", "health", "stats"])
    ap.add_argument("--commit", action="store_true", help="topup: build and load for real")
    ap.add_argument("--post", action="store_true", help="health/stats: post to Slack")
    a = ap.parse_args()
    if a.commit or a.post:
        coldstate.require("cold email %s" % a.job, STATE_DIR)
    try:
        if a.job == "topup":
            return topup(a.commit)
        if a.job == "health":
            health(a.post)
            return 0
        stats(a.post)
        return 0
    except Exception as exc:
        if a.commit or a.post:
            slack("issues", f"Cold email {a.job} crashed: {str(exc)[:300]}. It runs again at its next scheduled time.")
        raise


if __name__ == "__main__":
    sys.exit(main())
