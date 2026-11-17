"""The cold email program's daily jobs, run by Windows scheduled tasks until the agents brain owns them.

    python src/cold_email/daily.py topup [--commit]   # nightly: keep about 3 sending days of leads queued
    python src/cold_email/daily.py health [--post]    # weekday mornings: post to the issues channel only if wrong
    python src/cold_email/daily.py stats [--post]     # Mondays: last week's numbers to the pipeline channel

topup reads the campaign's own counts from Instantly (leads loaded minus leads contacted), and when
fewer than three days of sending are left, builds the next list from the same preset (build_list.py,
ZeroBounce on, every guard) and loads it. It never loads an owner twice (ledger.json).
health checks what the course says to watch every morning: every inbox connected and warming, warmup
health in the 90s, bounces under 2%, the campaign running, leads left to send, and that the reply
watcher ran in the last half hour. A healthy morning posts nothing.
"""
from __future__ import annotations

import argparse
import json
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
from instantly import ROOT, Instantly  # noqa: E402
from setup_accounts import env  # noqa: E402

ET = ZoneInfo("America/New_York")
CAMPAIGN = "Agency queue email 1"
PRESET = "Agency - 01 Queue"
DAYS_QUEUED = 3
MIN_BATCH = 100
REPLIES_LOG = ROOT / "output" / "cold_email" / "replies.jsonl"
TASK_LOG = ROOT / "output" / "cold_email" / "replies_task.log"
STATE = ROOT / "output" / "cold_email" / "daily_state.json"
# A pause on purpose ({"until": "YYYY-MM-DD", "why": ..., "since": ...}): health stays quiet until the date,
# then says the campaign is still paused. campaign.py launch removes it.
HOLD = ROOT / "output" / "cold_email" / "hold.json"


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


def campaign(api: Instantly) -> dict:
    c = next((c for c in api.campaigns() if c.get("name") == CAMPAIGN), None)
    if not c:
        raise SystemExit(f"no campaign named {CAMPAIGN!r}")
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


def topup(commit: bool) -> int:
    api = Instantly()
    c = campaign(api)
    a = analytics(api, c["id"])
    daily = int(c.get("daily_limit") or 250)
    left = int(a.get("leads_count") or 0) - int(a.get("contacted_count") or 0)
    want = DAYS_QUEUED * daily - left
    print(f"{left} leads not yet emailed, {daily} a day; {max(0, want)} wanted")
    if want < MIN_BATCH:
        print("enough queued; nothing to do")
        return 0
    from zerobounce import ZeroBounce
    credits = ZeroBounce().credits()
    if credits < want * 2:
        slack("issues", f"Cold email: ZeroBounce is down to {credits:,} credits, about {want * 2:,} are needed tonight. "
                        "The list was not topped up.")
        return 2
    if not commit:
        print(f"dry run: would build about {want} leads from {PRESET!r} and load them")
        return 0
    # Its own folder name per night: on 11-16 the top-up reused the first list's folder (same date),
    # found that list finished and loaded 0 new leads. A re-run the same night still resumes.
    slug = "agency_queue_topup_" + datetime.now(ET).strftime("%Y%m%d")
    py = sys.executable
    run = subprocess.run([py, "-u", str(HERE / "build_list.py"), "--preset", PRESET, "--target", str(want), "--verify",
                          "--slug", slug], cwd=str(ROOT), capture_output=True, text=True, timeout=3 * 3600)
    print(run.stdout[-3000:])
    leads = ROOT / "output" / "cold_email" / "lists" / slug / "leads.json"
    if run.returncode not in (0,) or not leads.exists():
        slack("issues", "Cold email: tonight's list build failed, so no new leads were added. "
                        f"It tries again tomorrow night. ({(run.stderr or run.stdout)[-300:].strip()})")
        return 2
    load = subprocess.run([py, "-u", str(HERE / "campaign.py"), "load", "--name", CAMPAIGN, "--leads", str(leads)],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=1800)
    print(load.stdout[-2000:])
    if load.returncode != 0:
        slack("issues", "Cold email: the new leads were built but did not load into Instantly. "
                        f"({(load.stderr or load.stdout)[-300:].strip()})")
        return 2
    return 0


def health(post: bool) -> list[str]:
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
    c = campaign(api)
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
    if TASK_LOG.exists():
        age = (time.time() - TASK_LOG.stat().st_mtime) / 60
        if now.weekday() < 5 and 8 <= now.hour < 20 and age > 30:
            problems.append(f"the reply watcher last ran {age:.0f} minutes ago (it runs every 5; is this PC on?)")
    else:
        problems.append("the reply watcher has never run")
    print("\n".join(problems) or "all healthy")
    if post and problems:
        day = now.strftime("%Y-%m-%d")
        st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
        key = "|".join(sorted(p.split(" (")[0] for p in problems))
        if st.get("health_posted", {}).get(day) != key:
            slack("issues", "Cold email needs a look:\n" + "\n".join(f"- {p}" for p in problems))
            st.setdefault("health_posted", {})[day] = key
            STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    return problems


def stats(post: bool) -> str:
    api = Instantly()
    c = campaign(api)
    w = analytics(api, c["id"], days=7)
    a = analytics(api, c["id"])
    since = datetime.now(timezone.utc) - timedelta(days=7)
    kinds = Counter()
    if REPLIES_LOG.exists():
        for line in REPLIES_LOG.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if datetime.fromisoformat(r["at"]) >= since:
                kinds[r["kind"]] += 1
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
    if post:
        slack("pipeline", text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["topup", "health", "stats"])
    ap.add_argument("--commit", action="store_true", help="topup: build and load for real")
    ap.add_argument("--post", action="store_true", help="health/stats: post to Slack")
    a = ap.parse_args()
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
