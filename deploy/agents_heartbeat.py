"""The outside watchdog's verdict on the agents brain (build plan 0.15).

Run once a day by .github/workflows/agents-heartbeat.yml, from OUTSIDE the box. The brain's own
morning digest covers what a run can see about itself. It cannot cover the failure that matters
most: a stopped machine reports nothing, and a missing digest looks exactly like a quiet morning.

The verdict is read off the health page's own fields, never off the HTTP status alone: a page that
answers 200 while its worker thread is dead is the failure this exists to catch.

    python deploy/agents_heartbeat.py https://<app>.fly.dev

Prints one line, `<status>|<detail>`, where status is ok, red, unreachable or unreadable, and
writes the same two values to $GITHUB_OUTPUT when a workflow runs it. The exit code is 0 for every
verdict (the workflow decides what a red verdict does); 2 means the command itself was misused.

Standard library only, so the workflow needs no install step, and `verdict()` is a pure function
that the selftest drives with invented pages.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

STALE_HOURS = 30.0


def _age_hours(at: str, now: datetime) -> float | None:
    """Hours since an ISO timestamp, or None when there is none or it cannot be read."""
    if not at:
        return None
    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() / 3600.0


def verdict(page, now: datetime | None = None, stale_hours: float = STALE_HOURS) -> tuple[str, str]:
    """(status, detail) for one parsed health page."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(page, dict):
        return "unreadable", "The health page answered but not with the expected JSON object."
    if page.get("ok") is not True:
        return "red", "The health page says it is not ok."
    if page.get("worker_alive") is not True:
        return "red", "The worker thread is dead, so nothing queued will run."
    if page.get("clock_alive") is not True:
        return "red", "The clock thread is dead, so no stage will fire."
    auto = page.get("autopilot")
    if not isinstance(auto, dict):
        return "red", "The health page carries no autopilot block."
    if auto.get("error"):
        return "red", "The playbook did not load on the box: %s" % str(auto["error"])[:160]
    stages = {k: v for k, v in auto.items()
              if not k.startswith("_") and isinstance(v, dict) and v.get("mode") != "off"}
    if not stages:
        return "ok", "The brain is up; the playbook has no stage switched on."
    fresh, never = 0, 0
    for st in stages.values():
        age = _age_hours((st.get("last") or {}).get("at") or "", now)
        if age is None:
            never += 1
        elif age < stale_hours:
            fresh += 1
    if fresh == 0:
        if never == len(stages):
            return "red", "None of the %d stages has ever run on this box." % len(stages)
        return "red", "No stage has run in the last %d hours." % int(stale_hours)
    detail = "%d of %d stages ran in the last %d hours" % (fresh, len(stages), int(stale_hours))
    req = auto.get("_requests") or {}
    if int(req.get("hour_refusals_today") or 0) > 0:
        detail += "; the request budget refused work %d time(s) today" % int(req["hour_refusals_today"])
    return "ok", detail + "."


def fetch(url: str, timeout: float = 30.0):
    """The parsed health page, or (None, why) when it could not be fetched or parsed."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            raw = r.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, ("unreachable", "The health page did not answer (%s). The machine is most likely stopped, out of "
                                     "credit or mid-deploy. No stage is running while this is true." % str(exc)[:120])
    try:
        return json.loads(raw.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError):
        return None, ("unreadable", "The health page answered but could not be read as JSON.")


def main(argv: list[str]) -> int:
    url = (argv[1] if len(argv) > 1 else os.environ.get("AGENTS_URL") or "").strip()
    if not url:
        print("usage: agents_heartbeat.py https://<app>.fly.dev   (or set AGENTS_URL)")
        return 2
    page, failed = fetch(url)
    status, detail = failed if failed else verdict(page)
    print("%s|%s" % (status, detail))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("status=%s\ndetail=%s\n" % (status, detail.replace("\n", " ")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
