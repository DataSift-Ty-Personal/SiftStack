"""Daily coverage audit and health digest for the first-to-market pull.

Answers one question every morning: is all of our FTM data actually being pulled,
and if not, which feed stopped. Posts one message a day, green or red, so silence
is never the signal.

    python src/ftm_health.py                 # print today's report
    python src/ftm_health.py --days 14       # wider history window
    python src/ftm_health.py --post          # print and post to Slack
    python src/ftm_health.py --check         # one line, exit 1 if red (watchdog)
    python src/ftm_health.py --selftest      # assertions, no network, no writes

WHY THIS EXISTS. `ftm_runner._notify` fires only when a run COMPLETES. If the
machine stops or the scheduler dies, nothing is emitted at all, and a dead box
looks exactly like a quiet day. Worse, the runner reports the notices stage as one
aggregate number, so a run where both probate feeds silently returned nothing
still reads as healthy: 2026-08-28 uploaded 19 records and every one was
foreclosure. Per-feed is the only altitude at which "is everything being pulled"
is answerable.

THREE COUNTING RULES, each learned from real data on this volume:

1. UPLOADED IS NOT NEW. `POST /property/` is upsert by address, and the county
   stage re-uploads the same 10 condemnation rows every single day (verified: the
   address column of knox_ftm_pull_*.csv hashes identically across 08-22, 08-25
   and 08-28). A "new records" figure built on the uploaded count would report 10
   fresh leads a day forever. New is measured against a persisted address ledger.

2. A ZERO DAY IS NOT A DEAD FEED. Each saved search publishes in bursts: Knox
   probate landed on 08-24 and 08-25 and nothing either side. Alerting on a single
   quiet day would cry wolf daily. The metric is DAYS SINCE the feed last produced
   a record, against FTM_FEED_STALE_DAYS.

3. A SKIPPED STAGE IS NOT A HEALTHY STAGE. `StageResult.ok` counts "skipped" as
   OK, so if the county stage reverts to skipping (it needs the SiftMap client) the
   run still exits 0 and Slack still shows green. The audit treats a stage that
   `FTM_ARGS` asked for and did not get as a finding. Deliberately checked here
   rather than by changing the runner's exit contract, which the scheduler rides on.

Config:
  FTM_HEALTH_WEBHOOK    Slack incoming webhook; falls back to SLACK_WEBHOOK_URL
  FTM_FEED_STALE_DAYS   per-feed staleness threshold (default 7)
  FTM_HEALTH_WINDOW     default history window in days (default 14)
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

logger = logging.getLogger("ftm_health")

RUNS_FILE = "ftm_runs.jsonl"
LEDGER_FILE = "ftm_record_ledger.json"

STALE_DAYS = int(os.getenv("FTM_FEED_STALE_DAYS", "7") or 7)
WINDOW_DAYS = int(os.getenv("FTM_HEALTH_WINDOW", "14") or 14)

# The four saved searches the cloud run actually scrapes. Anything here that goes
# quiet for STALE_DAYS is a finding, because these are supposed to be automatic.
FEEDS = [
    ("Knox", "foreclosure"),
    ("Blount", "foreclosure"),
    ("Knox", "probate"),
    ("Blount", "probate"),
]

# Source states. Only AUTOMATED is ever alerted on; the rest are reported so that
# "all of our first market data" is answerable rather than quietly incomplete.
AUTOMATED = "automated"
MANUAL = "manual"
NOT_BUILT = "not built"
RETIRED = "retired"
NO_UPSTREAM = "no upstream"
EXCLUDED = "excluded"

# The full inventory. Every FTM source lives here with an explicit state, so a
# source that was never automated stays visible instead of being absent.
SOURCES = [
    ("Foreclosure notices, Knox", AUTOMATED, "feed:Knox/foreclosure"),
    ("Foreclosure notices, Blount", AUTOMATED, "feed:Blount/foreclosure"),
    ("Probate notices, Knox", AUTOMATED, "feed:Knox/probate"),
    ("Probate notices, Blount", AUTOMATED, "feed:Blount/probate"),
    ("Condemnations, captured to volume", AUTOMATED, "stage:capture"),
    ("Condemnations, into DataSift", AUTOMATED, "stage:county"),
    ("General liens (LEN)", MANUAL, "knox_rod.py, then knox_lien_resolve.py"),
    ("State tax liens (STL)", MANUAL, "knox_rod.py, no dollar amount at source"),
    ("Federal tax liens (FTL)", MANUAL, "knox_rod.py"),
    ("Lien releases", MANUAL, "knox_rod.py, filter feed not a lead source"),
    ("tnpn public sales and orders", NOT_BUILT, "loader is live, nothing writes its JSON"),
    ("Trustee deeds (TRS)", RETIRED, "2026-08-25, duplicates the foreclosure scrape"),
    ("Evictions and tired landlords", RETIRED, "2026-08-25, docket keeps one week only"),
    ("Knox TN probate, direct from court", NO_UPSTREAM, "no online index; the notice scrape covers it"),
    ("Tax sale", EXCLUDED, "2026-07-26"),
]


# ── small local utilities ─────────────────────────────────────────────
#
# Deliberately dependency-light. A health check that cannot run because the thing
# it monitors is broken is worthless, so every import that could fail is optional
# and has a local fallback.


def _webhook() -> str:
    return (os.getenv("FTM_HEALTH_WEBHOOK", "").strip()
            or getattr(config, "SLACK_WEBHOOK_URL", "")
            or os.getenv("SLACK_WEBHOOK_URL", "").strip())


def _post(text: str, webhook_url: str | None = None) -> bool:
    """Post to Slack. Prefers the repo helper, falls back to a local sender."""
    url = webhook_url or _webhook()
    if not url:
        return False
    try:
        from slack_notifier import _send_webhook
        return _send_webhook(text, url)
    except Exception:
        pass
    try:
        import requests
        r = requests.post(url, json={"text": text},
                          headers={"Content-Type": "application/json"}, timeout=15)
        return r.status_code in (200, 204)
    except Exception as exc:
        logger.warning("health post failed: %s", exc)
        return False


_UNIT_RE = re.compile(r"\s+(?:APT|UNIT|STE|SUITE|#)\s*\S+$", re.IGNORECASE)


def _norm_addr(street: str, zipcode: str = "") -> str:
    """Canonical ledger key for a property.

    Prefers knox_ftm_pull.normalize_address so the repo keeps one address
    normalizer, but never lets its import failure take down the health report.
    The zip is part of the key because the same street name recurs across cities.
    """
    s = (street or "").strip()
    if not s:
        return ""
    try:
        from knox_ftm_pull import normalize_address
        s = normalize_address(s)
    except Exception:
        s = _UNIT_RE.sub("", " ".join(s.replace(".", " ").split()))
    z = (zipcode or "").strip()[:5]
    return f"{s.upper().strip(' ,')}|{z}"


def _today() -> str:
    try:
        return config.run_date()
    except Exception:
        return date.today().isoformat()


def _read_runs() -> list[dict]:
    path = config.STATE_DIR / RUNS_FILE
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("could not read %s: %s", path, exc)
    return rows


def _day_of(run: dict) -> str:
    return str(run.get("started", ""))[:10]


def _stage(run: dict | None, name: str) -> dict | None:
    for s in (run or {}).get("stages", []) or []:
        if s.get("name") == name:
            return s
    return None


def _read_csv(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    except Exception:
        return []


def _csvs_by_day(prefix: str) -> dict[str, list[str]]:
    """Map YYYY-MM-DD to EVERY CSV that day wrote.

    A day can hold more than one file: the runner stamps each run, and the
    12-month backfill wrote 48 of them. Keeping only the last path per day (the
    first version of this) silently dropped every earlier run's records, which
    undercounted the ledger seed by an order of magnitude.
    """
    out: dict[str, list[str]] = {}
    for p in sorted(glob.glob(str(config.OUTPUT_DIR / f"{prefix}_*.csv"))):
        if p.endswith("_upside_down.csv") or p.endswith("_rejected.csv"):
            continue
        m = re.search(rf"{re.escape(prefix)}_(\d{{4}}-\d{{2}}-\d{{2}})",
                      os.path.basename(p))
        if m:
            out.setdefault(m.group(1), []).append(p)
    return out


def _notice_csvs() -> dict[str, list[str]]:
    return _csvs_by_day("ftm_notices")


def _county_csvs() -> dict[str, list[str]]:
    return _csvs_by_day("knox_ftm_pull")


# ── the ledger: what makes "new" mean new ─────────────────────────────


def _load_ledger() -> dict:
    try:
        with open(config.STATE_DIR / LEDGER_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
            if isinstance(d, dict) and isinstance(d.get("addresses"), dict):
                return d
    except Exception:
        pass
    return {"addresses": {}, "seeded": ""}


def _save_ledger(led: dict) -> bool:
    try:
        path = config.STATE_DIR / LEDGER_FILE
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(led, fh)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.warning("could not write ledger: %s", exc)
        return False


def _addresses_for(day: str) -> list[tuple[str, str]]:
    """(key, source) for every row this day's pipelines wrote."""
    out: list[tuple[str, str]] = []
    for src, table in (("notices", _notice_csvs()), ("county", _county_csvs())):
        for p in table.get(day, []):
            for r in _read_csv(p):
                k = _norm_addr(r.get("Property Street Address", ""),
                               r.get("Property ZIP Code", ""))
                if k and not k.startswith("|"):
                    out.append((k, src))
    return out


def _seed_ledger(led: dict, exclude_day: str) -> int:
    """First run only: absorb history so day one does not report 1,226 new records.

    Everything already on the volume is backdated into the ledger EXCEPT the day
    being reported, so today's number is still measured honestly.
    """
    days = sorted(set(_notice_csvs()) | set(_county_csvs()))
    n = 0
    for d in days:
        if d >= exclude_day:
            continue
        for k, _src in _addresses_for(d):
            if k not in led["addresses"]:
                led["addresses"][k] = d
                n += 1
    led["seeded"] = datetime.now().isoformat(timespec="seconds")
    return n


# ── analysis (pure, so the selftest can drive it) ─────────────────────


def analyze(runs: list[dict], today: str, *, feed_last: dict, feed_today: dict,
            new_by_source: dict, uploaded_by_source: dict,
            requested_stages: list[str], ledger_seeded: bool,
            stale_days: int = STALE_DAYS) -> dict:
    """Turn the raw material into findings and a verdict. No IO."""
    today_runs = [r for r in runs if _day_of(r) == today]
    run = today_runs[-1] if today_runs else None

    red: list[str] = []
    amber: list[str] = []

    if run is None:
        red.append("No run recorded today. The scheduler or the machine may be down.")
    else:
        if run.get("blocked"):
            red.append("EGRESS BLOCKED. The site refused notice pages from this "
                       "run's IP. Retrying will not help; rotate the proxy.")
        for s in run.get("stages", []) or []:
            nm, st = s.get("name"), s.get("status")
            if st == "failed":
                red.append(f"Stage {nm} FAILED: {s.get('detail', '')[:160]}")
            elif st == "empty":
                red.append(f"Stage {nm} returned nothing. {s.get('detail', '')[:160]}")
            elif st == "skipped" and nm in requested_stages:
                red.append(f"Stage {nm} was requested but SKIPPED: "
                           f"{s.get('detail', '')[:160]}")
            for w in (s.get("warnings") or [])[:3]:
                amber.append(f"{nm} warning: {str(w)[:140]}")

    # Per-feed staleness. This is the check the aggregate number cannot make.
    for county, ntype in FEEDS:
        key = f"{county}/{ntype}"
        last = feed_last.get(key)
        if last is None:
            amber.append(f"Feed {key} has produced no record in the visible history.")
            continue
        gap = (date.fromisoformat(today) - date.fromisoformat(last)).days
        if gap >= stale_days:
            red.append(f"Feed {key} has produced nothing for {gap} days "
                       f"(last {last}).")

    # Consecutive empty notice days, which is what a broken gate looks like.
    streak = 0
    for r in sorted(runs, key=lambda x: x.get("started", ""), reverse=True):
        s = _stage(r, "notices")
        if s is None:
            continue
        if s.get("status") in ("empty", "failed"):
            streak += 1
        else:
            break
    if streak >= 2:
        red.append(f"{streak} consecutive runs produced no notices.")

    # The county stage re-upserting the same rows is the current steady state.
    if uploaded_by_source.get("county") and not new_by_source.get("county"):
        amber.append(
            f"County stage re-uploaded {uploaded_by_source['county']} row(s) that "
            "were already known. No new condemnation records today.")

    if ledger_seeded:
        amber.append("Record ledger was seeded from history on this run, so the "
                     "new-record count starts being meaningful tomorrow.")

    verdict = "red" if red else ("amber" if amber else "green")
    return {
        "today": today,
        "run": run,
        "runs_today": len(today_runs),
        "red": red,
        "amber": amber,
        "verdict": verdict,
        "feed_today": feed_today,
        "feed_last": feed_last,
        "new_by_source": new_by_source,
        "uploaded_by_source": uploaded_by_source,
        "empty_streak": streak,
    }


# ── collect / render / run ────────────────────────────────────────────


def collect(days: int = WINDOW_DAYS, *, update_ledger: bool = True) -> dict:
    today = _today()
    runs = _read_runs()
    cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    window = [r for r in runs if _day_of(r) >= cutoff]

    # Per-feed history straight off the CSVs, which carry County and Notice Type.
    notice_csvs = _notice_csvs()
    feed_last: dict[str, str] = {}
    feed_today: Counter = Counter()
    volumes: list[int] = []
    for d in sorted(notice_csvs):
        rows = [r for p in notice_csvs[d] for r in _read_csv(p)]
        if d < cutoff:
            # Still useful for staleness, just not for the volume baseline.
            for r in rows:
                feed_last[f"{r.get('County')}/{r.get('Notice Type')}"] = d
            continue
        volumes.append(len(rows))
        for r in rows:
            key = f"{r.get('County')}/{r.get('Notice Type')}"
            feed_last[key] = d
            if d == today:
                feed_today[key] += 1

    # New records, measured against the ledger rather than the upload count.
    led = _load_ledger()
    seeded_now = False
    if not led["addresses"] and not led.get("seeded"):
        _seed_ledger(led, exclude_day=today)
        seeded_now = True

    todays = _addresses_for(today)
    new_by_source: Counter = Counter()
    uploaded_by_source: Counter = Counter()
    seen_this_run: set[str] = set()
    for key, src in todays:
        uploaded_by_source[src] += 1
        if key not in led["addresses"] and key not in seen_this_run:
            new_by_source[src] += 1
        seen_this_run.add(key)
    if update_ledger:
        for key, _src in todays:
            led["addresses"].setdefault(key, today)
        _save_ledger(led)

    requested = [s.strip() for s in
                 os.getenv("FTM_ARGS", "").replace("=", " ").split()
                 if s.strip()]
    stages_req: list[str] = []
    if "--stages" in requested:
        i = requested.index("--stages")
        if i + 1 < len(requested):
            stages_req = [x for x in requested[i + 1].split(",") if x]

    data = analyze(
        window, today,
        feed_last=feed_last, feed_today=dict(feed_today),
        new_by_source=dict(new_by_source),
        uploaded_by_source=dict(uploaded_by_source),
        requested_stages=stages_req, ledger_seeded=seeded_now,
    )
    data["window_days"] = days
    data["baseline"] = sorted(volumes)[len(volumes) // 2] if volumes else 0
    data["ledger_size"] = len(led["addresses"])
    data["history"] = window
    return data


def _icon(verdict: str) -> str:
    return {"green": ":white_check_mark:", "amber": ":large_yellow_circle:",
            "red": ":rotating_light:"}[verdict]


def render(data: dict) -> str:
    v = data["verdict"]
    new_total = sum(data["new_by_source"].values())
    up_total = sum(data["uploaded_by_source"].values())

    lines = [f"{_icon(v)} *FTM daily check, {data['today']}*"]

    if v == "green":
        lines.append("Everything is working correctly. All automated sources ran "
                     "and reported in.")
    elif v == "amber":
        lines.append("Running, with notes below. Nothing is broken.")
    else:
        lines.append("Something needs attention. Details below.")

    lines.append("")
    lines.append(f"*New records today: {new_total}*  (rows written {up_total}; "
                 f"the gap is re-upserts of properties we already had)")
    if data["new_by_source"] or data["uploaded_by_source"]:
        parts = []
        for src in ("notices", "county"):
            if src in data["uploaded_by_source"]:
                parts.append(f"{src} {data['new_by_source'].get(src, 0)} new "
                             f"of {data['uploaded_by_source'][src]}")
        if parts:
            lines.append("  " + ", ".join(parts))
    lines.append(f"  Ledger now holds {data['ledger_size']} distinct properties.")

    # Per-feed table. The aggregate number cannot show a dead saved search.
    lines.append("")
    lines.append("*Automated feeds*")
    today_d = date.fromisoformat(data["today"])
    for county, ntype in FEEDS:
        key = f"{county}/{ntype}"
        n = data["feed_today"].get(key, 0)
        last = data["feed_last"].get(key)
        if last:
            gap = (today_d - date.fromisoformat(last)).days
            age = "today" if gap == 0 else f"last {last}, {gap}d ago"
            stale = gap >= STALE_DAYS
        else:
            age, stale = "never seen", True
        flag = " :warning:" if stale else ""
        lines.append(f"  {key}: {n} today ({age}){flag}")

    run = data["run"]
    lines.append("")
    if run:
        lines.append("*Stages*")
        for s in run.get("stages", []) or []:
            mark = {"ok": "OK", "failed": "FAILED", "empty": "EMPTY",
                    "skipped": "SKIPPED"}.get(s.get("status"), s.get("status"))
            lines.append(f"  {s.get('name')} [{mark}] {str(s.get('detail',''))[:150]}")
    else:
        lines.append("*Stages*  no run recorded today")

    if data["red"]:
        lines.append("")
        lines.append("*Needs attention*")
        for f in data["red"][:8]:
            lines.append(f"  {f}")
    if data["amber"]:
        lines.append("")
        lines.append("*Notes*")
        for f in data["amber"][:6]:
            lines.append(f"  {f}")

    # The inventory. Reported every day so a source that was never automated
    # cannot quietly disappear from the picture.
    lines.append("")
    lines.append("*Full source inventory*")
    for name, state, note in SOURCES:
        if state == AUTOMATED:
            continue
        lines.append(f"  {name}: {state} ({note})")

    lines.append("")
    lines.append(f"_Window {data['window_days']}d, typical notice day "
                 f"{data['baseline']} records._")
    return "\n".join(lines)


def run(days: int = WINDOW_DAYS, post: bool = False) -> str:
    text = render(collect(days))
    if post:
        _post(text)
    return text


# ── selftest ──────────────────────────────────────────────────────────


class _R:
    def __init__(self):
        self.fail = 0
        self.n = 0

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.n += 1
        if cond:
            print(f"  [pass] {name}")
        else:
            self.fail += 1
            print(f"  [FAIL] {name}" + (f"  {detail}" if detail else ""))
        return bool(cond)

    def report(self) -> int:
        print(f"\n  {self.n - self.fail} of {self.n} passed")
        return 1 if self.fail else 0


def _mkrun(day: str, **stages) -> dict:
    return {
        "started": f"{day}T06:30:00", "finished": f"{day}T06:50:00",
        "committed": True, "blocked": stages.pop("blocked", False),
        "stages": [dict(name=k, status=v[0], detail=v[1] if len(v) > 1 else "",
                        records=0, uploaded=0, seconds=1.0, csv_path="",
                        warnings=[])
                   for k, v in stages.items()],
    }


def selftest() -> int:
    """Assertions over synthetic runs. No network, no files, no posting."""
    r = _R()
    T = "2026-08-28"
    fresh = {f"{c}/{t}": T for c, t in FEEDS}
    base = dict(feed_last=fresh, feed_today={}, new_by_source={},
                uploaded_by_source={}, requested_stages=["notices", "capture", "county"],
                ledger_seeded=False)

    d = analyze([_mkrun(T, notices=("ok",), capture=("ok",), county=("ok",))], T, **base)
    r.check("all green", d["verdict"] == "green", str(d["red"] + d["amber"]))

    d = analyze([], T, **base)
    r.check("no run today is red", d["verdict"] == "red")
    r.check("no run says machine may be down",
            any("may be down" in x for x in d["red"]))

    d = analyze([_mkrun(T, notices=("empty", "0 notices"))], T, **base)
    r.check("empty notices is red", d["verdict"] == "red")

    blocked = _mkrun(T, notices=("failed", "EGRESS BLOCKED"))
    blocked["blocked"] = True
    d = analyze([blocked], T, **base)
    r.check("egress block is red", d["verdict"] == "red")
    r.check("egress block names the cause",
            any("EGRESS" in x.upper() for x in d["red"]))

    # The defect the runner's own exit code cannot see.
    d = analyze([_mkrun(T, notices=("ok",), county=("skipped", "SiftMap absent"))],
                T, **base)
    r.check("requested-but-skipped stage is red", d["verdict"] == "red")
    d2 = analyze([_mkrun(T, notices=("ok",), county=("skipped", "x"))], T,
                 **{**base, "requested_stages": ["notices"]})
    r.check("unrequested skip is not red", d2["verdict"] == "green")

    # Staleness, the check the aggregate number cannot make.
    stale = dict(fresh)
    stale["Knox/probate"] = "2026-08-01"
    d = analyze([_mkrun(T, notices=("ok",))], T, **{**base, "feed_last": stale})
    r.check("stale feed is red", d["verdict"] == "red")
    r.check("stale feed is named",
            any("Knox/probate" in x for x in d["red"]))

    near = dict(fresh)
    near["Knox/probate"] = (date.fromisoformat(T) - timedelta(days=3)).isoformat()
    d = analyze([_mkrun(T, notices=("ok",))], T, **{**base, "feed_last": near})
    r.check("a 3 day quiet feed is not an alarm", d["verdict"] == "green")

    # Consecutive empties.
    runs = [_mkrun("2026-08-26", notices=("empty",)),
            _mkrun("2026-08-27", notices=("empty",)),
            _mkrun(T, notices=("empty",))]
    d = analyze(runs, T, **base)
    r.check("empty streak counted", d["empty_streak"] == 3, str(d["empty_streak"]))

    runs = [_mkrun("2026-08-27", notices=("empty",)), _mkrun(T, notices=("ok",))]
    d = analyze(runs, T, **base)
    r.check("streak resets on a good run", d["empty_streak"] == 0)

    # Re-upsert accounting.
    d = analyze([_mkrun(T, notices=("ok",), county=("ok",))], T,
                **{**base, "uploaded_by_source": {"county": 10},
                   "new_by_source": {"county": 0}})
    r.check("re-upsert is amber not red", d["verdict"] == "amber")
    r.check("re-upsert is explained",
            any("already known" in x for x in d["amber"]))

    # Render must not raise and must carry the all-clear.
    d = analyze([_mkrun(T, notices=("ok",), capture=("ok",), county=("ok",))], T, **base)
    d.update(window_days=14, baseline=8, ledger_size=940, history=[])
    text = render(d)
    r.check("green render says it is working",
            "working correctly" in text)
    r.check("render has no em dash", "—" not in text and "–" not in text)
    r.check("render lists the inventory", "Full source inventory" in text)
    r.check("render shows new vs written", "New records today" in text)

    # Address key behavior.
    a = _norm_addr("2905 WASHINGTON PIKE", "37917")
    b = _norm_addr("2905 Washington Pike", "37917")
    r.check("address key is case stable", a == b, f"{a!r} vs {b!r}")
    r.check("address key includes zip", a.endswith("|37917"))
    r.check("blank address yields no key", _norm_addr("", "37917") == "")

    return r.report()


# ── entry point ───────────────────────────────────────────────────────


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="ftm_health",
        description="Daily FTM coverage audit and health digest.")
    p.add_argument("--days", type=int, default=WINDOW_DAYS,
                   help=f"History window (default {WINDOW_DAYS})")
    p.add_argument("--post", action="store_true", help="Post the report to Slack")
    p.add_argument("--check", action="store_true",
                   help="One line verdict, exit 1 if red. For the watchdog.")
    p.add_argument("--no-ledger-update", action="store_true",
                   help="Report without recording today's records as seen")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout)

    if args.selftest:
        return selftest()

    data = collect(args.days, update_ledger=not args.no_ledger_update)

    if args.check:
        run_ = data["run"]
        when = run_.get("started", "never") if run_ else "no run today"
        first = (data["red"] or ["all clear"])[0]
        print(f"{data['verdict'].upper()} {data['today']} last_run={when} :: {first}")
        return 1 if data["verdict"] == "red" else 0

    text = render(data)
    print(text)
    if args.post:
        ok = _post(text)
        print(f"\n[post {'sent' if ok else 'FAILED, no webhook or send error'}]")
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
