"""Daily coverage audit and health digest for the first-to-market pull.

Answers one question every morning: is all of our FTM data actually being pulled,
and if not, which feed stopped. Posts one message a day, green or red, so silence
is never the signal.

    python src/ftm_health.py                 # the two-line daily summary
    python src/ftm_health.py --full          # the detailed report, on demand
    python src/ftm_health.py --days 14       # wider history window
    python src/ftm_health.py --post          # print and post to Slack
    python src/ftm_health.py --check         # one line, exit 1 if red (watchdog)
    python src/ftm_health.py --selftest      # assertions, no network, no writes

WHAT GETS SAID IS NOT WHAT GETS CHECKED. The daily post is deliberately two lines
(new records per DataSift list) plus one reason line when something is wrong. Every
check below still runs on every pass; a green day just has nothing worth saying.
The detailed version is `--full`.

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

FEED HEALTH (build plan 1.12). A feed that arrives a day or two after its filing is not first to
market, and nothing here used to measure that. Every pass now works out, per feed, from the
files already on the volume: the LAG of its new records (pull date minus filing date), its new
records this month and last, the last day it produced one, and the days left on the paid source
behind it. All of that is in `--full`. None of it is in the daily post, and none of it changes
the verdict, unless a switch below asks for an alarm: the post stays two lines, and a feed
finding reaches it only as the reason line of a red day.

Config:
  FTM_HEALTH_WEBHOOK         Slack incoming webhook; falls back to SLACK_WEBHOOK_URL
  FTM_FEED_STALE_DAYS        staleness threshold for the four notice feeds (default 7)
  FTM_HEALTH_WINDOW          default history window in days (default 14)
  FTM_FEED_LAG_ALARM_DAYS    off when unset. Set to 2 and a live feed whose new records run more
                             than 2 days behind their filings is a red finding
  FTM_HEALTH_ALARM_FEEDS     off when unset. Comma list of feed keys (knox_liens, knox_condemned)
                             that should also be judged by silence, on their own thresholds
  FTM_FEED_HEALTH=1          has the runner keep each paid source's expiry in ftm_sources.json
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

# The two dash characters no report may contain, built from their code points so this file
# does not hold either one itself.
_EM, _EN = chr(0x2014), chr(0x2013)

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

SWITCHED_OFF = "switched off"

# Sources that are not feeds of this pipeline: retired on purpose, or with nothing to pull.
# These never change with FTM_ARGS, so they are written down once.
_FIXED_SOURCES = [
    ("Trustee deeds (TRS)", RETIRED, "2026-08-25, duplicates the foreclosure scrape"),
    ("Evictions and tired landlords", RETIRED, "2026-08-25, docket keeps one week only"),
    ("Knox TN probate, direct from court", NO_UPSTREAM, "no online index; the notice scrape covers it"),
    ("Blount recorder and condemnations", NO_UPSTREAM, "no online source; Blount arrives through the notices only"),
]


def sources(stages: list[str] | None = None) -> list[tuple[str, str, str]]:
    """The full inventory, with each feed's state read off what the run is ASKED to do.

    This used to be a hand-written list, and it went stale: liens read "manual" for weeks after
    they became a daily stage. A feed is automated when its stage (and the stage that uploads
    its rows) is in FTM_ARGS, and switched off otherwise, so the inventory cannot disagree with
    the run. Falls back to the fixed rows alone when the feed table cannot be imported."""
    out: list[tuple[str, str, str]] = []
    try:
        import ftm_feeds
        live = {f.key for f in ftm_feeds.live_feeds(stages)}
        for f in ftm_feeds.FEEDS:
            if f.key in live:
                out.append((f.label, AUTOMATED, "stage:%s" % f.stage))
            else:
                out.append((f.label, SWITCHED_OFF, "add the %s stage to FTM_ARGS" % f.stage))
    except Exception:
        pass
    return out + list(_FIXED_SOURCES)


# Kept for anything that imported the old name. Built for the stages in today's FTM_ARGS.
SOURCES = sources()


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


def _feed_csvs() -> dict[str, list[str]]:
    """Upload files written by the notice feeds that run alone (tax_sale, tax_delinquent,
    public_notices). Empty until one of those stages is switched on."""
    out: dict[str, list[str]] = {}
    for p in sorted(glob.glob(str(config.OUTPUT_DIR / "ftm_feed_*.csv"))):
        m = re.search(r"_(\d{4}-\d{2}-\d{2})_\d{6}\.csv$", os.path.basename(p))
        if m:
            out.setdefault(m.group(1), []).append(p)
    return out


# ── feed health: lag, volume, last good day (build plan 1.12) ─────────


def _iso_day(raw: str) -> str:
    """YYYY-MM-DD from the two date forms the upload files carry, or ""."""
    s = (raw or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return ""
    return ""


def _notice_id(url: str) -> str:
    m = re.search(r"[?&]ID=(\d+)", url or "")
    return m.group(1) if m else ""


def _read_seen(name: str) -> dict:
    try:
        with open(config.STATE_DIR / name, "r", encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def feed_key(row: dict, family: str) -> str:
    """Which feed one upload row belongs to, read off the row itself.

    A notice row says its county and notice type. A county row says its list, and the list is
    matched against the feed table by NAME (the setting may have renamed it)."""
    try:
        import ftm_feeds
    except Exception:
        return ""
    county = (row.get("County") or "").strip().lower()
    ntype = (row.get("Notice Type") or "").strip().lower()
    if family in ("notices", "feeds"):
        want = ("ftm_notices",) if family == "notices" else None
        for f in ftm_feeds.FEEDS:
            if f.notice_type != ntype:
                continue
            if want and f.csv_prefix not in want:
                continue
            if not want and f.csv_prefix in ("ftm_notices", "knox_ftm_pull"):
                continue
            if not f.county or f.county.lower() == county:
                return f.key
        return ""
    lst = _row_list(row, "county").lower()
    for f in ftm_feeds.FEEDS:
        if f.csv_prefix == "knox_ftm_pull" and f.list_name.lower() == lst and f.notice_type == (ntype or f.notice_type):
            return f.key
    return ""


def filed_day(row: dict, family: str, seen: dict) -> str:
    """The day the county filed or published what this row carries, or "" when the file does not
    say. A notice's publication date is not a column: it is what the seen-id cache stores against
    the notice id in the row's Source URL. A county row carries it as Date Added."""
    if family == "county":
        return _iso_day(row.get("Date Added") or "")
    got = seen.get(_notice_id(row.get("Source URL") or ""), "")
    if got:
        return _iso_day(str(got))
    return _iso_day(row.get("Probate Open Date") or "")


def feed_report(today: str, families: dict | None = None, seen: dict | None = None) -> dict:
    """Per feed: new records today, lag of those records, last day it produced one, and new
    records this month and last. Pure when `families` and `seen` are given (the selftest does).

    NEW is "first time this address showed up under this feed", replayed over every upload file
    on the volume, for the same reason the record ledger exists: the county stage re-sends the
    same rows every morning, and counting rows would call a re-send a fresh lead. Lag is measured
    on new records only, or a condemnation re-sent for a month would read a month late."""
    if families is None:
        families = {"notices": _notice_csvs(), "county": _county_csvs(), "feeds": _feed_csvs()}
        families = {name: {d: [r for p in paths for r in _read_csv(p)] for d, paths in table.items()}
                    for name, table in families.items()}
    if seen is None:
        seen = {}
        for name in ("seen_ids.json", "seen_ids_tax_sale.json", "seen_ids_tax_delinquent.json",
                     "seen_ids_public_notices.json"):
            seen.update(_read_seen(name))
    month = today[:7]
    y, m = int(today[:4]), int(today[5:7])
    last_month = "%04d-%02d" % ((y, m - 1) if m > 1 else (y - 1, 12))
    first_seen: dict[tuple[str, str], str] = {}
    out: dict[str, dict] = {}
    days = sorted({d for table in families.values() for d in table})
    for d in days:
        if d > today:
            continue
        for family, table in families.items():
            for row in table.get(d, []):
                key = feed_key(row, family)
                addr = _norm_addr(row.get("Property Street Address", ""), row.get("Property ZIP Code", ""))
                if not key or not addr or addr.startswith("|") or (key, addr) in first_seen:
                    continue
                first_seen[(key, addr)] = d
                rep = out.setdefault(key, {"new_today": 0, "last_new": "", "this_month": 0, "last_month": 0,
                                           "lags": [], "undated_today": 0})
                rep["last_new"] = d
                if d[:7] == month:
                    rep["this_month"] += 1
                elif d[:7] == last_month:
                    rep["last_month"] += 1
                if d == today:
                    rep["new_today"] += 1
                    filed = filed_day(row, family, seen)
                    if filed and filed <= d:
                        rep["lags"].append((date.fromisoformat(d) - date.fromisoformat(filed)).days)
                    else:
                        rep["undated_today"] += 1
    for rep in out.values():
        lags = sorted(rep.pop("lags"))
        rep["lag_median"] = lags[len(lags) // 2] if lags else None
        rep["lag_max"] = lags[-1] if lags else None
        rep["lag_measured"] = len(lags)
    out["_followed"] = _appointments_followed(first_seen)
    return out


def _appointments_followed(first_seen: dict) -> dict:
    """How many substitute trustee records later showed up as a foreclosure sale notice: the
    proof that the earlier signal really is earlier."""
    appt = {addr: d for (key, addr), d in first_seen.items() if key == "knox_substitute_trustee"}
    later = sum(1 for (key, addr), d in first_seen.items()
                if key.endswith("_foreclosure") and addr in appt and d > appt[addr])
    return {"appointments": len(appt), "later_a_sale_notice": later}


def feed_findings(today: str, report: dict, runs: list[dict], stages: list[str]) -> tuple[list[str], list[str]]:
    """(red, amber) findings from the feed report. Empty unless a switch asks for an alarm, or a
    feed from a NEWLY switched-on stage has gone silent past its own threshold."""
    red: list[str] = []
    amber: list[str] = []
    try:
        import ftm_feeds
    except Exception:
        return red, amber
    try:
        lag_alarm = int(os.getenv("FTM_FEED_LAG_ALARM_DAYS", "0") or 0)
    except ValueError:
        lag_alarm = 0
    asked = {x.strip() for x in os.getenv("FTM_HEALTH_ALARM_FEEDS", "").split(",") if x.strip()}
    today_d = date.fromisoformat(today)
    for f in ftm_feeds.live_feeds(stages):
        rep = report.get(f.key) or {}
        lag = rep.get("lag_median")
        if lag_alarm and lag is not None and lag > lag_alarm:
            red.append("%s is running %d days behind its filings (%d new today)."
                       % (f.label, lag, rep.get("new_today", 0)))
        # Silence. The four notice feeds keep the check they have always had (in analyze). A feed
        # from today's run is judged only when named; a feed from a newer stage is judged on its
        # own threshold from the day its stage first ran.
        judged = (f.key in asked) or (f.stage not in ftm_feeds.TODAY_STAGES and not f.core)
        if f.core or not judged or not f.stale_days:
            continue
        last = rep.get("last_new") or ""
        since = last or _first_run_day(runs, f.stage)
        if not since:
            continue
        gap = (today_d - date.fromisoformat(since)).days
        if gap >= f.stale_days:
            red.append("%s has produced nothing for %d days (%s)."
                       % (f.label, gap, ("last %s" % last) if last else "never since it was switched on"))
    return red, amber


def _first_run_day(runs: list[dict], stage: str) -> str:
    days = [_day_of(r) for r in runs if _stage(r, stage) is not None and _day_of(r)]
    return min(days) if days else ""


def paid_sources(today: str) -> list[dict]:
    """Days left on each paid source, from what the runner last read (ftm_sources.json).

    Empty when the runner is not keeping the file (FTM_FEED_HEALTH is not set). A reading goes
    stale: days left are recomputed from the day it was read, and the age is shown."""
    try:
        with open(config.STATE_DIR / "ftm_sources.json", "r", encoding="utf-8") as fh:
            held = json.load(fh)
    except Exception:
        return []
    out = []
    for name, d in sorted((held or {}).items()):
        if not isinstance(d, dict):
            continue
        read = _iso_day(str(d.get("read") or ""))
        age = (date.fromisoformat(today) - date.fromisoformat(read)).days if read else None
        left = d.get("days_left")
        if d.get("expires"):
            try:
                left = (date.fromisoformat(d["expires"]) - date.fromisoformat(today)).days
            except ValueError:
                pass
        elif left is not None and age is not None:
            left = int(left) - age
        out.append({"source": name, "days_left": left, "expires": d.get("expires") or "",
                    "read": read, "age_days": age, "state": d.get("state") or ""})
    return out


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


def _row_list(row: dict, src: str) -> str:
    """The DataSift list a row actually landed on, read off the CSV.

    Taken from the data rather than re-derived from a notice_type mapping,
    because the two mappings in this repo disagree: datasift_formatter sends
    code_violation to "Code Violation" while knox_ftm_pull sends it to
    "Condemned", and knox_ftm_pull is what writes those rows. The column is the
    only thing that knows where a record really went.
    """
    raw = (row.get("Lists") or "").strip()
    if raw:
        return raw.split(",")[0].strip()
    return src.capitalize()


def _addresses_for(day: str) -> list[tuple[str, str, str]]:
    """(key, source, list_name) for every row this day's pipelines wrote."""
    out: list[tuple[str, str, str]] = []
    # "feeds" holds the notice feeds that run alone; it is empty until one is switched on.
    for src, table in (("notices", _notice_csvs()), ("county", _county_csvs()), ("feeds", _feed_csvs())):
        for p in table.get(day, []):
            for r in _read_csv(p):
                k = _norm_addr(r.get("Property Street Address", ""),
                               r.get("Property ZIP Code", ""))
                if k and not k.startswith("|"):
                    out.append((k, src, _row_list(r, src)))
    return out


def _seed_ledger(led: dict, exclude_day: str) -> int:
    """First run only: absorb history so day one does not report 1,226 new records.

    Everything already on the volume is backdated into the ledger EXCEPT the day
    being reported, so today's number is still measured honestly.
    """
    days = sorted(set(_notice_csvs()) | set(_county_csvs()) | set(_feed_csvs()))
    n = 0
    for d in days:
        if d >= exclude_day:
            continue
        for k, _src, _lst in _addresses_for(d):
            if k not in led["addresses"]:
                led["addresses"][k] = d
                n += 1
    led["seeded"] = datetime.now().isoformat(timespec="seconds")
    return n


# ── analysis (pure, so the selftest can drive it) ─────────────────────


def analyze(runs: list[dict], today: str, *, feed_last: dict, feed_today: dict,
            new_by_source: dict, uploaded_by_source: dict,
            requested_stages: list[str], ledger_seeded: bool,
            stale_days: int = STALE_DAYS, feed_red: list | None = None,
            feed_amber: list | None = None) -> dict:
    """Turn the raw material into findings and a verdict. No IO.

    `feed_red` and `feed_amber` are the feed-health findings (lag, a newer feed gone silent).
    They are appended AFTER the run's own findings on purpose: the daily post carries only the
    first red finding, and "the stage failed" names the cause better than "the feed is late"."""
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

    red.extend(feed_red or [])
    amber.extend(feed_amber or [])

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
    new_by_list: Counter = Counter()
    uploaded_by_list: Counter = Counter()
    seen_this_run: set[str] = set()
    for key, src, lst in todays:
        uploaded_by_source[src] += 1
        uploaded_by_list[lst] += 1
        if key not in led["addresses"] and key not in seen_this_run:
            new_by_source[src] += 1
            new_by_list[lst] += 1
        seen_this_run.add(key)
    if update_ledger:
        for key, _src, _lst in todays:
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

    # Feed health. Computed on every pass and shown in --full; it adds a FINDING only when a
    # switch asks for one. A failure in here must never take the daily verdict down with it.
    feeds: dict = {}
    feed_red: list[str] = []
    feed_amber: list[str] = []
    try:
        feeds = feed_report(today)
        feed_red, feed_amber = feed_findings(today, feeds, runs, stages_req)
    except Exception as exc:
        logger.warning("feed health skipped: %s", exc)

    data = analyze(
        window, today,
        feed_last=feed_last, feed_today=dict(feed_today),
        new_by_source=dict(new_by_source),
        uploaded_by_source=dict(uploaded_by_source),
        requested_stages=stages_req, ledger_seeded=seeded_now,
        feed_red=feed_red, feed_amber=feed_amber,
    )
    data["window_days"] = days
    data["baseline"] = sorted(volumes)[len(volumes) // 2] if volumes else 0
    data["ledger_size"] = len(led["addresses"])
    data["history"] = window
    data["new_by_list"] = dict(new_by_list)
    data["uploaded_by_list"] = dict(uploaded_by_list)
    data["feeds"] = feeds
    data["stages_requested"] = stages_req
    try:
        data["paid_sources"] = paid_sources(today)
    except Exception:
        data["paid_sources"] = []
    return data


def _icon(verdict: str) -> str:
    return {"green": ":white_check_mark:", "amber": ":large_yellow_circle:",
            "red": ":rotating_light:"}[verdict]


# The lists the automated pipeline feeds. Always rendered, even at zero, so a
# list that stops producing shows as 0 rather than vanishing from the message.
EXPECTED_LISTS = ["Foreclosure", "Probate", "Condemned"]

REASON_LIMIT = 120


def _newer_live_lists(stages: list[str]) -> list[str]:
    """Lists fed by a stage that was switched on AFTER the three lists above were fixed.

    A list that stops producing has to show as 0, not vanish, and that holds for a new feed the
    day it goes live. Lists from today's run are deliberately not added (Liens would lengthen
    every post): only a stage somebody chose to switch on changes what the post shows."""
    try:
        import ftm_feeds
        out: list[str] = []
        for f in ftm_feeds.live_feeds(stages):
            if f.stage not in ftm_feeds.TODAY_STAGES and f.list_name not in out:
                out.append(f.list_name)
        return out
    except Exception:
        return []


def _short_reason(text: str, limit: int = REASON_LIMIT) -> str:
    """Trim a finding to one glanceable sentence.

    The runner writes long, useful detail strings (the empty-notices one runs to
    180 characters and ends in a command to paste). That belongs in --full and in
    ftm_runs.jsonl, not in a message whose whole purpose is being short. Cuts on a
    sentence boundary where there is one, so the line never ends mid-thought.
    """
    t = " ".join((text or "").split())
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)          # trailing command hints
    if len(t) <= limit:
        return t
    cut = t[:limit]
    for sep in (". ", ", ", " "):
        i = cut.rfind(sep)
        if i > 40:
            return cut[:i].rstrip(" ,.:;") + "."
    return cut.rstrip() + "."


def render_short(data: dict) -> str:
    """The daily post. Two lines when healthy, three when not.

    Every check in analyze() still runs; this only decides how much of the result
    is worth saying out loud. On a green day that is a tick and the counts. On a
    red day the top finding is appended, and it is the ONLY detail carried, so it
    has to be the one that names what broke.

    Because this reports new records only, a healthy quiet day prints all zeros
    and looks much like a broken one. The icon is what separates them, which is
    why the verdict is in the first character rather than buried.
    """
    new_by_list = data.get("new_by_list", {})
    total_new = sum(new_by_list.values())

    # Expected lists first in a stable order, then anything unexpected, so a new
    # notice type appears instead of being silently dropped.
    expected = EXPECTED_LISTS + [n for n in _newer_live_lists(data.get("stages_requested") or [])
                                 if n not in EXPECTED_LISTS]
    names = expected + sorted(k for k in new_by_list if k not in expected)
    counts = " · ".join(f"{n} {new_by_list.get(n, 0)}" for n in names)

    when = data["today"]
    try:
        when = date.fromisoformat(data["today"]).strftime("%b %-d")
    except (ValueError, TypeError):
        try:                                    # %-d is not portable to Windows
            when = date.fromisoformat(data["today"]).strftime("%b %d").replace(" 0", " ")
        except (ValueError, TypeError):
            pass

    # Two states only. Amber means nothing is broken and the note it carries is
    # exactly the noise being cut here, so it reads as the all-clear.
    icon = ":rotating_light:" if data["verdict"] == "red" else ":white_check_mark:"
    noun = "record" if total_new == 1 else "records"
    lines = [f"{icon} *FTM {when}* · {total_new} new {noun}", "", counts]

    if data["verdict"] == "red" and data["red"]:
        lines.append(_short_reason(data["red"][0]))
    return "\n".join(lines)


def _render_feed_health(data: dict) -> list[str]:
    """The feed health section of the full report: one line per live feed, then the paid
    sources and the request budget. Nothing here is in the daily post."""
    feeds = data.get("feeds") or {}
    lines: list[str] = []
    try:
        import ftm_feeds
        live = ftm_feeds.live_feeds(data.get("stages_requested"))
    except Exception:
        return lines
    if live:
        lo, hi = ftm_feeds.TEACHING_MONTHLY
        lines.append("")
        lines.append("*Feed health*  (lag = pull date minus filing date, on new records)")
        today_d = date.fromisoformat(data["today"])
        for f in live:
            rep = feeds.get(f.key) or {}
            last = rep.get("last_new") or ""
            if last:
                gap = (today_d - date.fromisoformat(last)).days
                when = "today" if gap == 0 else f"{last}, {gap}d ago"
            else:
                when = "never"
            if rep.get("lag_median") is None:
                lag = "no dated new record today"
            else:
                lag = f"lag {rep['lag_median']}d (max {rep['lag_max']}d over {rep['lag_measured']})"
            lines.append(f"  {f.label}: {rep.get('new_today', 0)} new today, {lag}, last good day {when}, "
                         f"{rep.get('this_month', 0)} this month, {rep.get('last_month', 0)} last month")
        lines.append(f"  The teaching expects {lo} to {hi} new records a month from one first-to-market list.")
        followed = feeds.get("_followed") or {}
        if followed.get("appointments"):
            lines.append(f"  Substitute trustee records that later ran as a sale notice: "
                         f"{followed['later_a_sale_notice']} of {followed['appointments']}.")

    paid = data.get("paid_sources") or []
    lines.append("")
    lines.append("*Paid sources*")
    if not paid:
        lines.append("  Days left are not being recorded (set FTM_FEED_HEALTH=1 on the runner).")
    for p in paid:
        left = "not read" if p["days_left"] is None else f"{p['days_left']} days left"
        until = f", expires {p['expires']}" if p.get("expires") else ""
        age = "" if not p.get("age_days") else f" (read {p['age_days']}d ago)"
        lines.append(f"  {p['source']}: {left}{until}{age}")

    try:
        import ftm_budget
        lines.append("")
        lines.append("*DataSift request budget*")
        for line in ftm_budget.summary_lines():
            lines.append("  " + line)
    except Exception:
        pass
    return lines


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

    lines.extend(_render_feed_health(data))

    # The inventory. Reported every day so a source that was never automated
    # cannot quietly disappear from the picture.
    lines.append("")
    lines.append("*Full source inventory*")
    for name, state, note in sources(data.get("stages_requested")):
        if state == AUTOMATED:
            continue
        lines.append(f"  {name}: {state} ({note})")

    lines.append("")
    lines.append(f"_Window {data['window_days']}d, typical notice day "
                 f"{data['baseline']} records._")
    return "\n".join(lines)


def run(days: int = WINDOW_DAYS, post: bool = False, full: bool = False) -> str:
    data = collect(days)
    text = render(data) if full else render_short(data)
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
    r.check("render has no em dash", _EM not in text and _EN not in text)
    r.check("render lists the inventory", "Full source inventory" in text)
    r.check("render shows new vs written", "New records today" in text)

    # The daily post. Shape matters as much as content: this is read at a glance.
    green = analyze([_mkrun(T, notices=("ok",), capture=("ok",), county=("ok",))],
                    T, **base)
    green.update(window_days=14, baseline=8, ledger_size=979, history=[],
                 new_by_list={"Foreclosure": 3, "Probate": 1})
    s = render_short(green)
    body = [ln for ln in s.split("\n") if ln.strip()]
    r.check("green post is two lines", len(body) == 2, repr(s))
    r.check("green post ticks", s.startswith(":white_check_mark:"))
    r.check("green post totals the lists", "4 new records" in s, s.split("\n")[0])
    r.check("green post shows every expected list",
            all(n in s for n in EXPECTED_LISTS), s)
    r.check("green post carries no reason line", "::" not in s)

    one = dict(green); one["new_by_list"] = {"Probate": 1}
    r.check("singular record reads right", "1 new record\n" in render_short(one) + "\n",
            render_short(one).split("\n")[0])

    zero = dict(green); zero["new_by_list"] = {}
    z = render_short(zero)
    r.check("a quiet day still lists all three", "Foreclosure 0" in z and "Probate 0" in z)
    r.check("a quiet day still ticks", z.startswith(":white_check_mark:"))

    redd = analyze([_mkrun(T, notices=("empty", "0 notices scraped."))], T, **base)
    redd.update(window_days=14, baseline=8, ledger_size=979, history=[],
                new_by_list={})
    rs = render_short(redd)
    rbody = [ln for ln in rs.split("\n") if ln.strip()]
    r.check("red post is three lines", len(rbody) == 3, repr(rs))
    r.check("red post sirens", rs.startswith(":rotating_light:"))
    r.check("red post names the cause", "returned nothing" in rs)
    r.check("red reason line stays short",
            len(rbody[-1]) <= REASON_LIMIT, f"{len(rbody[-1])} chars")
    r.check("red reason drops the pasted command", "list-searches" not in rs)

    verbose = ("Stage notices returned nothing. 0 notices scraped. Not a normal "
               "quiet day: check the notice gate, egress IP, login, and that the "
               "saved searches still exist (python src/main.py list-searches).")
    trimmed = _short_reason(verbose)
    r.check("long finding cuts on a sentence", trimmed.endswith("."), trimmed)
    r.check("long finding keeps the headline",
            trimmed.startswith("Stage notices returned nothing"), trimmed)
    short_in = "No run recorded today. The scheduler or the machine may be down."
    r.check("a short finding is left alone", _short_reason(short_in) == short_in)

    amber = analyze([_mkrun(T, notices=("ok",), county=("ok",))], T,
                    **{**base, "uploaded_by_source": {"county": 10},
                       "new_by_source": {"county": 0}})
    amber.update(window_days=14, baseline=8, ledger_size=979, history=[],
                 new_by_list={})
    r.check("amber reads as the all-clear",
            render_short(amber).startswith(":white_check_mark:"))
    r.check("amber stays two lines",
            len([x for x in render_short(amber).split("\n") if x.strip()]) == 2)

    r.check("short post has no em dash",
            _EM not in rs and _EN not in rs and _EM not in s)

    # An unexpected list must surface rather than be dropped.
    extra = dict(green); extra["new_by_list"] = {"Eviction": 2}
    r.check("unexpected list still shows", "Eviction 2" in render_short(extra))

    # List name comes from the data, not from a notice_type mapping.
    r.check("list read off the Lists column",
            _row_list({"Lists": "Condemned"}, "county") == "Condemned")
    r.check("multi-list cell takes the first",
            _row_list({"Lists": "Probate, Courthouse"}, "notices") == "Probate")
    r.check("missing Lists falls back to the source",
            _row_list({}, "notices") == "Notices")

    # Address key behavior.
    a = _norm_addr("2905 WASHINGTON PIKE", "37917")
    b = _norm_addr("2905 Washington Pike", "37917")
    r.check("address key is case stable", a == b, f"{a!r} vs {b!r}")
    r.check("address key includes zip", a.endswith("|37917"))
    r.check("blank address yields no key", _norm_addr("", "37917") == "")

    # Feed health (build plan 1.12). Invented rows; nothing is read from the volume.
    def _row(n, county="Knox", ntype="foreclosure"):
        return {"Property Street Address": "%d Sample Hollow Rd" % n, "Property ZIP Code": "37918",
                "County": county, "Notice Type": ntype, "Lists": ntype.capitalize(),
                "Source URL": "https://example.invalid/Details.aspx?ID=%d" % (7000 + n)}

    fams = {"notices": {"2026-08-20": [_row(1)], T: [_row(1), _row(2), _row(3)]}, "county": {}, "feeds": {}}
    rep = feed_report(T, fams, {"7002": "2026-08-27", "7003": "2026-08-24"})
    fc = rep.get("knox_foreclosure") or {}
    r.check("a row sent before is not new", fc.get("new_today") == 2, str(fc))
    r.check("lag is pull day minus filing day", fc.get("lag_median") == 4 and fc.get("lag_max") == 4, str(fc))
    r.check("last good day is the last NEW record", fc.get("last_new") == T)
    stages = ["notices", "capture", "liens", "county", "sold"]
    keep = {k: os.environ.pop(k, None) for k in ("FTM_FEED_LAG_ALARM_DAYS", "FTM_HEALTH_ALARM_FEEDS")}
    try:
        red, _amber = feed_findings(T, rep, [], stages)
        r.check("a late feed adds no finding unless the alarm is set", red == [], str(red))
        os.environ["FTM_FEED_LAG_ALARM_DAYS"] = "2"
        red, _amber = feed_findings(T, rep, [], stages)
        r.check("the lag alarm names the feed", len(red) == 1 and "4 days behind" in red[0], str(red))
    finally:
        os.environ.pop("FTM_FEED_LAG_ALARM_DAYS", None)
        for k, v in keep.items():
            if v is not None:
                os.environ[k] = v
    late = analyze([_mkrun(T, notices=("ok",))], T, **{**base, "requested_stages": ["notices"],
                                                      "feed_red": ["Foreclosure notices, Knox is running 4 days behind."]})
    late.update(window_days=14, baseline=8, ledger_size=979, history=[], new_by_list={})
    lbody = [ln for ln in render_short(late).split("\n") if ln.strip()]
    r.check("a feed finding is the reason line of a red post", len(lbody) == 3 and "4 days behind" in lbody[2])
    full = dict(green)
    full.update(feeds=rep, stages_requested=stages, paid_sources=[])
    ftext = render(full)
    r.check("the full report carries feed health", "Feed health" in ftext and "lag 4d" in ftext)
    r.check("feed health never reaches a green post", "lag" not in render_short(full).lower())
    r.check("the full report has no em dash", _EM not in ftext and _EN not in ftext)
    states = {name: state for name, state, _note in sources(stages)}
    r.check("the inventory follows the run", states.get("Liens, Knox") == AUTOMATED
            and states.get("Substitute trustee appointments, Knox") == SWITCHED_OFF)

    return r.report()


# ── entry point ───────────────────────────────────────────────────────


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="ftm_health",
        description="Daily FTM coverage audit and health digest.")
    p.add_argument("--days", type=int, default=WINDOW_DAYS,
                   help=f"History window (default {WINDOW_DAYS})")
    p.add_argument("--post", action="store_true", help="Post the report to Slack")
    p.add_argument("--full", action="store_true",
                   help="The detailed report (feeds, stages, source inventory) "
                        "instead of the two-line daily summary")
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

    text = render(data) if args.full else render_short(data)
    print(text)
    if args.post:
        ok = _post(text)
        print(f"\n[post {'sent' if ok else 'FAILED, no webhook or send error'}]")
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
