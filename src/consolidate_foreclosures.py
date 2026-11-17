"""Consolidate the last N months of foreclosure runs into a master list.

Pulls foreclosure records from the Apify dataset (the cloud daily runs) and,
optionally, the local output/ CSVs, dedupes them by notice ID, and removes any
whose foreclosure sale date ("option date" = auction_date) has already passed.
What remains is the master list of foreclosures still worth working, ready to
re-run through the pipeline to pull in notice screenshots.

Requires APIFY_TOKEN in .env to reach the cloud data. Without it, pass
--no-apify to consolidate only the local CSVs.

Run:
    python src/consolidate_foreclosures.py                 # Apify (3 mo) + local
    python src/consolidate_foreclosures.py --months 3      # window for run pull
    python src/consolidate_foreclosures.py --no-apify      # local CSVs only
    python src/consolidate_foreclosures.py --county Knox    # restrict county
"""

import argparse
import csv
import glob
import io
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402  (loads .env via load_dotenv at import)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"
DEFAULT_ACTOR = "tn-public-notice-scraper"

# Column aliases so records from different CSV shapes line up on the fields we need.
_ALIASES = {
    "auction_date": ["auction_date", "Foreclosure Date", "Tax Auction Date"],
    "notice_type": ["notice_type", "Notice Type"],
    "source_url": ["source_url", "Source URL"],
    "address": ["address", "Property Street Address"],
    "city": ["city", "Property City"],
    "county": ["county", "County"],
    "date_added": ["date_added", "Date Added"],
}


def _norm(rec: dict) -> dict:
    """Fill canonical keys from any known alias so downstream logic is uniform."""
    out = dict(rec)
    for canon, names in _ALIASES.items():
        cur = out.get(canon)
        is_empty = cur is None or (isinstance(cur, str) and not cur.strip())
        if is_empty:
            for name in names:
                v = rec.get(name)
                if v not in (None, ""):
                    out[canon] = v
                    break
    return out


def _notice_id(url: str) -> str:
    m = re.search(r"[?&]ID=(\d+)", url or "")
    return m.group(1) if m else ""


def _parse_date(s: str):
    """Return a date, None (blank), or 'BAD' (present but unparseable)."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return "BAD"


def _auction_sort_key(rec: dict):
    """Pick the most current notice of a property: the latest sale date wins."""
    d = _parse_date(rec.get("auction_date"))
    return d if isinstance(d, date) else date.min


# ── Apify pull ────────────────────────────────────────────────────────


def _apify_get(path: str, token: str, params: dict | None = None):
    import requests

    params = dict(params or {})
    params["token"] = token
    resp = requests.get(f"{APIFY_BASE}/{path}", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _apify_get_text(path: str, token: str, params: dict | None = None) -> str:
    import requests

    params = dict(params or {})
    params["token"] = token
    resp = requests.get(f"{APIFY_BASE}/{path}", params=params, timeout=60)
    resp.raise_for_status()
    return resp.text


def fetch_apify_records(token: str, actor: str, months: int) -> list[dict]:
    """Pull foreclosure-era records from every actor run in the last `months`."""
    # Resolve the actor id by name from the token owner's actor list.
    acts = _apify_get("acts", token, {"my": "1", "limit": 1000}).get("data", {}).get("items", [])
    act = next((a for a in acts if a.get("name") == actor), None)
    if not act:
        names = ", ".join(a.get("name", "?") for a in acts) or "(none)"
        raise SystemExit(f"Actor '{actor}' not found for this token. Available: {names}")
    act_id = act["id"]

    cutoff = (date.today() - timedelta(days=months * 31)).isoformat()
    runs = _apify_get(
        f"acts/{act_id}/runs", token, {"desc": "1", "limit": 1000}
    ).get("data", {}).get("items", [])
    recent = [
        r for r in runs
        if (r.get("startedAt") or "")[:10] >= cutoff and r.get("defaultKeyValueStoreId")
    ]
    logger.info("Apify: %d runs total, %d in last %d months", len(runs), len(recent), months)

    # Each run stores its records as output.csv in its key-value store (the
    # default dataset is unused). Pull and parse each run's output.csv.
    records: list[dict] = []
    runs_with_data = 0
    for r in recent:
        kv_id = r["defaultKeyValueStoreId"]
        try:
            txt = _apify_get_text(f"key-value-stores/{kv_id}/records/output.csv", token)
        except Exception:
            continue  # run had no output.csv (empty or failed mid-run)
        rows = list(csv.DictReader(io.StringIO(txt)))
        if rows:
            runs_with_data += 1
        records.extend(rows)
    logger.info("Apify: pulled %d rows from output.csv across %d/%d runs",
                len(records), runs_with_data, len(recent))
    return records


# ── Local pull ────────────────────────────────────────────────────────


def load_local_records() -> list[dict]:
    """Read foreclosure-bearing CSVs already in output/."""
    paths = sorted(set(
        glob.glob(str(config.OUTPUT_DIR / "master_all_records.csv"))
        + glob.glob(str(config.OUTPUT_DIR / "tn_notices*.csv"))
    ))
    records: list[dict] = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                records.append(row)
    logger.info("Local: %d raw records from %d CSV file(s)", len(records), len(paths))
    return records


# ── Consolidate ───────────────────────────────────────────────────────


def consolidate(records: list[dict], today: date, county: str | None):
    """Dedupe foreclosures by notice ID and split by sale-date status."""
    fc = []
    for r in records:
        r = _norm(r)
        if (r.get("notice_type") or "").strip().lower() != "foreclosure":
            continue
        if county and (r.get("county") or "").strip().lower() != county.lower():
            continue
        fc.append(r)

    # Property-level dedupe: a foreclosure is published several times (each a
    # separate notice ID), so collapse all notices for the same address into one
    # record, keeping the LATEST sale date so a rescheduled/postponed sale wins
    # over an earlier passed one.
    best: dict[str, dict] = {}
    for r in fc:
        addr = re.sub(r"\s+", " ", (r.get("address") or "").strip().lower())
        city = re.sub(r"\s+", " ", (r.get("city") or "").strip().lower())
        if addr:
            key = f"{addr}|{city}"
        else:
            key = "noaddr:" + (_notice_id(r.get("source_url")) or f"row{id(r)}")
        prev = best.get(key)
        if prev is None or _auction_sort_key(r) > _auction_sort_key(prev):
            best[key] = r
    uniq = list(best.values())

    passed, future, blank, bad = [], [], [], []
    for r in uniq:
        d = _parse_date(r.get("auction_date"))
        if d is None:
            blank.append(r)
        elif d == "BAD":
            bad.append(r)
        elif d < today:
            passed.append(r)
        else:
            future.append(r)

    master = future + blank + bad  # keep everything except a confirmed-passed sale date
    return {
        "raw_fc": len(fc), "unique": len(uniq),
        "passed": passed, "future": future, "blank": blank, "bad": bad, "master": master,
    }


def write_master_csv(master: list[dict], out_path: Path) -> None:
    """Write the master list, preserving every column seen (union of keys)."""
    preferred = [
        "date_added", "date_published", "auction_date", "address", "city", "state",
        "zip", "owner_name", "notice_type", "county", "source_url",
    ]
    keys = list(preferred)
    for r in master:
        for k in r:
            if k not in keys and not k.startswith("_"):
                keys.append(k)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in master:
            w.writerow(r)


def _month_hist(rows: list[dict]) -> str:
    from collections import Counter
    c: Counter = Counter()
    for r in rows:
        d = _parse_date(r.get("auction_date"))
        c[d.strftime("%Y-%m") if isinstance(d, date) else "(none)"] += 1
    return ", ".join(f"{m}:{n}" for m, n in sorted(c.items()))


def _county_hist(rows: list[dict]) -> str:
    from collections import Counter
    c = Counter((r.get("county") or "(none)").strip() or "(none)" for r in rows)
    return ", ".join(f"{k}:{n}" for k, n in sorted(c.items()))


def main() -> None:
    ap = argparse.ArgumentParser(description="Consolidate foreclosure runs into a master list")
    ap.add_argument("--months", type=int, default=3, help="How many months of Apify runs to pull")
    ap.add_argument("--actor", default=DEFAULT_ACTOR, help="Apify actor name")
    ap.add_argument("--county", default=None, help="Restrict to one county (e.g. Knox)")
    ap.add_argument("--no-apify", action="store_true", help="Skip Apify, use local CSVs only")
    ap.add_argument("--no-local", action="store_true", help="Skip local CSVs, use Apify only")
    ap.add_argument("--require-sale-date", action="store_true",
                    help="Drop records with no parsed sale date (stricter active list)")
    ap.add_argument("--out", default=None, help="Output CSV path")
    args = ap.parse_args()

    today = date.today()
    logger.info("Today: %s | window: last %d months | county: %s",
                today, args.months, args.county or "all")

    records: list[dict] = []
    if not args.no_apify:
        token = os.getenv("APIFY_TOKEN", "").strip()
        if not token:
            logger.warning("APIFY_TOKEN not set. Add it to .env, or pass --no-apify. Using local only.")
        else:
            records += fetch_apify_records(token, args.actor, args.months)
    if not args.no_local:
        records += load_local_records()

    if not records:
        logger.error("No records gathered. Add APIFY_TOKEN to .env or ensure local CSVs exist.")
        sys.exit(1)

    s = consolidate(records, today, args.county)
    if args.require_sale_date:
        s["master"] = s["future"]

    logger.info("")
    logger.info("=== Foreclosure consolidation ===")
    logger.info("  raw foreclosure rows      : %d", s["raw_fc"])
    logger.info("  unique properties         : %d", s["unique"])
    logger.info("  PASSED sale date (REMOVE) : %d", len(s["passed"]))
    logger.info("  FUTURE sale date (keep)   : %d", len(s["future"]))
    logger.info("  no date (keep)            : %d", len(s["blank"]))
    logger.info("  unparseable date (keep)   : %d", len(s["bad"]))
    logger.info("  --> MASTER LIST           : %d", len(s["master"]))
    logger.info("")
    logger.info("  future sale months: %s", _month_hist(s["future"]) or "(none)")
    logger.info("  master by county  : %s", _county_hist(s["master"]))

    out = Path(args.out) if args.out else (config.OUTPUT_DIR / f"foreclosure_master_active_{today}.csv")
    write_master_csv(s["master"], out)
    logger.info("")
    logger.info("Master list written: %s (%d records)", out, len(s["master"]))


if __name__ == "__main__":
    main()
