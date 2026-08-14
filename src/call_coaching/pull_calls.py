"""pull_calls.py - pull the SmrtPhone call log and download call recordings.

Authenticates with the saved browser-session cookies (SiftStack/smrtphone_state.json,
captured by _api/smrtphone_login.py). The call log is a DataTables server-side
endpoint (POST /logs/calls/filtered) that returns duration, disposition, caller,
the reisift record link, and a DIRECT recording URL on rec.smrtphone.io (no auth
needed once you have the URL). Verified live 2026-07-06 (tenant 42564).

USAGE (run from SiftStack root with the venv python):
  python src/call_coaching/pull_calls.py --list                      # survey: log only, no downloads
  python src/call_coaching/pull_calls.py --min-seconds 60            # download all recordings >= 60s
  python src/call_coaching/pull_calls.py --min-seconds 60 --days 14  # only last 14 days
  python src/call_coaching/pull_calls.py --max-calls 40              # cap downloads

Output:
  output/call_coaching/call_log.json            full call log (all rows pulled)
  output/call_coaching/calls_to_review.json     the filtered set (>= min seconds, has recording)
  output/call_coaching/recordings/{call_id}.mp3
If the session is expired the script exits 2 with a clear message
(re-run: .venv/Scripts/python.exe "..Desktop/Deal Room Coaching Call/_api/smrtphone_login.py").
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "smrtphone_state.json"
OUT_DIR = ROOT / "output" / "call_coaching"
REC_DIR = OUT_DIR / "recordings"
BASE = "https://phone.smrt.studio"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
PAGE_SIZE = 200

COLUMNS = ["id", "user", "user_id", "created_at", "direction", "status",
           "disposition", "from_num", "to_num", "price", "duration",
           "podio_id", "recording_sid", "sid", "call_agent_id"]


def _cookie_header() -> str:
    if not STATE.exists():
        return ""
    st = json.loads(STATE.read_text(encoding="utf-8"))
    return "; ".join(f"{c['name']}={c['value']}" for c in st.get("cookies", [])
                     if "smrt.studio" in c.get("domain", ""))


def _page_form(start: int, length: int) -> dict:
    form = {"draw": "1", "start": str(start), "length": str(length),
            "order[0][column]": "3", "order[0][dir]": "desc",
            "search[value]": "", "search[regex]": "false"}
    for i, col in enumerate(COLUMNS):
        form[f"columns[{i}][data]"] = col
        form[f"columns[{i}][name]"] = col
        form[f"columns[{i}][searchable]"] = "true"
        form[f"columns[{i}][orderable]"] = "true"
        form[f"columns[{i}][search][value]"] = ""
        form[f"columns[{i}][search][regex]"] = "false"
    return form


def fetch_page(cookie: str, start: int, length: int) -> dict:
    data = urllib.parse.urlencode(_page_form(start, length)).encode()
    req = urllib.request.Request(BASE + "/logs/calls/filtered", data=data, method="POST", headers={
        "cookie": cookie, "user-agent": UA,
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-requested-with": "XMLHttpRequest", "accept": "application/json, */*",
        "origin": BASE, "referer": BASE + "/logs/calls",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode("utf-8", "replace")
    if body.lstrip().startswith("<"):
        raise PermissionError("HTML response - SmrtPhone session expired. "
                              "Re-run _api/smrtphone_login.py in the Deal Room Coaching Call project.")
    return json.loads(body)


def normalize(row: dict) -> dict:
    rec = row.get("recording_sid") or {}
    created = (row.get("created_at") or {}).get("date", "")
    return {
        "call_id": row.get("id"),
        "created_at_utc": created[:19],
        "caller": (row.get("user") or {}).get("name"),
        "direction": row.get("direction"),
        "status": row.get("status"),
        "disposition": row.get("disposition"),
        "contact_name": (row.get("from_num") or {}).get("contactName")
                        or (row.get("to_num") or {}).get("contactName"),
        "from_num": (row.get("from_num") or {}).get("fromNum"),
        "to_num": (row.get("to_num") or {}).get("toNum"),
        "duration_seconds": row.get("duration") or 0,
        "reisift_record_url": row.get("podio_id"),
        "recording_url": rec.get("hasRec") if isinstance(rec, dict) else None,
        "call_detail_url": BASE + rec.get("viewRoute", "") if isinstance(rec, dict) and rec.get("viewRoute") else None,
    }


def pull_log(days: int | None, quiet: bool = False) -> list[dict]:
    cookie = _cookie_header()
    if not cookie:
        print("ERROR: no smrtphone_state.json session file", file=sys.stderr)
        sys.exit(2)
    cutoff = None
    if days:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    calls: list[dict] = []
    start, total = 0, None
    while True:
        page = fetch_page(cookie, start, PAGE_SIZE)
        rows = page.get("data") or []
        total = page.get("recordsTotal")
        for row in rows:
            c = normalize(row)
            if cutoff and c["created_at_utc"] and c["created_at_utc"] < cutoff:
                return calls
            calls.append(c)
        if not quiet:
            print(f"  pulled {len(calls)}/{total} calls...", flush=True)
        start += PAGE_SIZE
        if not rows or (total is not None and start >= int(total)):
            return calls
        time.sleep(0.4)


def download_recording(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"user-agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            dest.write_bytes(r.read())
        return dest.stat().st_size > 4096
    except Exception as e:
        print(f"    download failed {url}: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Pull SmrtPhone call log + recordings")
    ap.add_argument("--min-seconds", type=int, default=60, help="minimum call duration (default 60)")
    ap.add_argument("--include-dispositions", help="comma-separated dispositions to include "
                    "regardless of --min-seconds (still requires >= 15s and a recording), "
                    'e.g. "Correct Number"')
    ap.add_argument("--days", type=int, help="only calls from the last N days")
    ap.add_argument("--max-calls", type=int, help="cap number of downloads (newest first)")
    ap.add_argument("--list", action="store_true", help="survey only - write the log, no downloads")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Pulling SmrtPhone call log ({'last %d days' % args.days if args.days else 'all'})...")
    calls = pull_log(args.days)
    (OUT_DIR / "call_log.json").write_text(json.dumps(calls, indent=1), encoding="utf-8")
    print(f"Call log: {len(calls)} calls -> {OUT_DIR / 'call_log.json'}")

    dispo_include = {d.strip().lower() for d in (args.include_dispositions or "").split(",") if d.strip()}
    keep = [c for c in calls
            if c["recording_url"]
            and ((c["duration_seconds"] or 0) >= args.min_seconds
                 or (dispo_include and (c["disposition"] or "").lower() in dispo_include
                     and (c["duration_seconds"] or 0) >= 15))]
    if args.max_calls:
        keep = keep[: args.max_calls]
    (OUT_DIR / "calls_to_review.json").write_text(json.dumps(keep, indent=1), encoding="utf-8")
    print(f"Qualifying (>= {args.min_seconds}s with recording): {len(keep)}")
    if args.list:
        return 0

    REC_DIR.mkdir(parents=True, exist_ok=True)
    got = skipped = 0
    for i, c in enumerate(keep, 1):
        dest = REC_DIR / f"{c['call_id']}.mp3"
        if dest.exists() and dest.stat().st_size > 4096:
            skipped += 1
            continue
        ok = download_recording(c["recording_url"], dest)
        got += 1 if ok else 0
        if i % 10 == 0:
            print(f"  downloaded {i}/{len(keep)}...", flush=True)
        time.sleep(0.25)
    print(f"Recordings: {got} downloaded, {skipped} already present -> {REC_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
