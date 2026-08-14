"""Pull the FULL outstanding Knox County tax balance for properties in a Sift export.

Unlike export_tax_delinquency.py (which counts only bills flagged
delinquent=True AND paid=False), this captures the COMPLETE amount owed on
each parcel, broken down into:

  - full_amount_due : every unpaid bill (current-year + delinquent)  <- "full amount"
  - delinquent_due  : bills the county has flagged delinquent (unpaid)
  - current_due     : unpaid bills not yet flagged delinquent (current year)
  - turned_over_due : unpaid bills turned over to the Clerk & Master (tax-sale pipeline)

Knox County tax API (same endpoints used by src/tax_enricher.py):
  - Parcel lookup:  GET /api/v2/due/PPT/{parcel}?detail_level=public
  - Address search: GET /api/v2/parcels/{address}?detail_level=public

A paid bill reports due=0, so summing `due` over all bills == total outstanding.

The endpoint is slow (~7-8s/request) and throttles after a sustained concurrent
burst: once tripped it HANGS new connections until the load drops, then recovers.
To finish reliably this runner uses:
  - a circuit breaker: an error streak pauses ALL workers for a cooldown, then resumes
  - multi-pass retry: stragglers from a pass are re-queued after a cooldown
  - resume: already-fetched rows are reloaded from the output CSV and skipped

Usage:
  python src/export_tax_full_amounts.py "Tax delinquent check obituary data.csv"
  python src/export_tax_full_amounts.py "<csv>" --workers 4 --fresh
"""

import csv
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

# Windows redirects stdout as cp1252, which crashes on any non-Latin-1 glyph.
# Force UTF-8 so progress/owner names never kill the run mid-stream.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

BASE = "https://knox-tn.mygovonline.com/api/v2"
TIMEOUT = 25            # normal response ~7-8s; cut off hung (throttled) connections
WORKERS = 2             # the API serializes concurrent calls AND throttles bursts;
                        # 2 keeps throughput ~= sequential while minimizing trips
RETRIES = 2             # per-call attempts; the circuit breaker + passes do the rest
MAX_PASSES = 8          # re-queue stragglers up to this many times
OUT_PATH = "output/tax_full_amounts_results.csv"

# Circuit-breaker tuning
ERR_STREAK_TRIP = 3     # consecutive errors that trip the breaker (block detected)
COOLDOWNS = [45, 90, 150, 180]  # escalating pause (s) each time the breaker re-opens

COLS = ["row", "owner", "tax_api_owner", "address", "city", "zip",
        "raw_parcel", "parcel_used", "found", "error", "full_amount_due",
        "delinquent_due", "current_due", "turned_over_due",
        "years_owed_count", "years_owed", "delinquent_years",
        "n_bills", "est_value"]

# Sentinel: no definite answer (timeout / 5xx after retries). Distinct from None
# (HTTP 404 = parcel genuinely absent) so a throttled call is never recorded as "$0".
ERROR = object()

_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=WORKERS + 2,
                                       pool_maxsize=WORKERS + 2))


# ───────────────────────── circuit breaker ─────────────────────────
class Breaker:
    """Trip on consecutive errors; hold all workers until a cooldown elapses."""

    def __init__(self):
        self._lock = threading.Lock()
        self._consec = 0
        self._open_until = 0.0   # monotonic time
        self._opens = 0

    def wait_if_open(self):
        while True:
            with self._lock:
                remaining = self._open_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 2.0))

    def record_success(self):
        with self._lock:
            self._consec = 0

    def record_error(self):
        with self._lock:
            self._consec += 1
            if self._consec >= ERR_STREAK_TRIP and time.monotonic() >= self._open_until:
                cd = COOLDOWNS[min(self._opens, len(COOLDOWNS) - 1)]
                self._open_until = time.monotonic() + cd
                self._opens += 1
                self._consec = 0
                print(f"\n  [!] throttle detected, pausing all workers {cd}s "
                      f"(cooldown #{self._opens})\n", flush=True)


_breaker = Breaker()
_progress_lock = threading.Lock()
_done = 0
_all_results: dict[int, dict] = {}   # row -> rec, shared; checkpointed periodically
CHECKPOINT_EVERY = 25


# ───────────────────────── parcel / parsing ─────────────────────────
def parcel_candidates(raw: str) -> list[str]:
    """Generate Knox tax-API parcel-id formats to try, best first."""
    raw = (raw or "").strip().upper()
    if not raw:
        return []
    cands: list[str] = []
    if "-" in raw:
        cands.append(raw)
    if re.search(r"\s", raw):                       # '051  043' -> '051-043'
        cands.append(re.sub(r"\s+", "-", raw))
    m = re.match(r"^(\d{3}[A-Z]{0,2})(\d{2,5})$", raw)   # '123EE030' -> '123EE-030'
    if m:
        cands.append(f"{m.group(1)}-{m.group(2)}")
    m2 = re.match(r"^(\d{3}[A-Z]{0,2})(\d{2,5})([A-Z])$", raw)  # condo unit letter
    if m2:
        cands.append(f"{m2.group(1)}-{m2.group(2)}{m2.group(3)}")
        cands.append(f"{m2.group(1)}-{m2.group(2)} {m2.group(3)}")
    cands.append(raw)
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _get_json(url: str):
    """GET with breaker + retry. Returns JSON, None (real 404), or ERROR."""
    for attempt in range(RETRIES):
        _breaker.wait_if_open()
        try:
            r = _session.get(url, timeout=TIMEOUT)
            if r.status_code == 404:
                _breaker.record_success()   # a clean 404 means the server is healthy
                return None
            if r.status_code == 200:
                data = r.json()
                _breaker.record_success()
                return data
        except (requests.RequestException, ValueError):
            pass
        _breaker.record_error()
        if attempt < RETRIES - 1:
            time.sleep(1.0 * (attempt + 1))
    return ERROR


def fetch_ppt(parcel: str):
    data = _get_json(f"{BASE}/due/PPT/{quote(parcel)}?detail_level=public")
    if data is None or data is ERROR:
        return data
    return data.get("due", {}).get("PPT", {})


def lookup_account_by_address(addr: str):
    if not addr.strip():
        return None
    data = _get_json(f"{BASE}/parcels/{quote(addr)}?detail_level=public&start=0&length=5")
    if data is None or data is ERROR:
        return data
    parcels = data.get("parcels", [])
    return parcels[0].get("account_number") if parcels else None


def parse_amounts(ppt: dict) -> dict:
    """Split unpaid bills into full / delinquent / current / turned-over totals."""
    bills = ppt.get("bills", []) if ppt else []
    full = delinq = current = turned = 0.0
    years_full, years_delinq = [], []
    for b in bills:
        due = b.get("due") or 0
        if b.get("paid", True) or due <= 0:
            continue
        yr = b.get("tax_year") or b.get("taxYear")
        full += due
        years_full.append(yr)
        if b.get("delinquent"):
            delinq += due
            years_delinq.append(yr)
        else:
            current += due
        if b.get("turnedOver"):
            turned += due
    yrs_f = sorted({str(y) for y in years_full if y})
    yrs_d = sorted({str(y) for y in years_delinq if y})
    return {
        "full_amount_due": round(full, 2),
        "delinquent_due": round(delinq, 2),
        "current_due": round(current, 2),
        "turned_over_due": round(turned, 2),
        "years_owed": ", ".join(yrs_f),
        "years_owed_count": len(yrs_f),
        "delinquent_years": ", ".join(yrs_d),
        "n_bills": len(bills),
    }


def owner_from_ppt(ppt: dict) -> str:
    bills = ppt.get("bills", []) if ppt else []
    return bills[0].get("owner", "").strip() if bills else ""


def _write_csv(records: list[dict]) -> None:
    os.makedirs("output", exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(sorted(records, key=lambda r: r["row"]))


# ───────────────────────── per-record work ─────────────────────────
def process_row(args):
    idx, row, total = args
    addr = (row.get("Property address") or "").strip()
    owner = " ".join(x for x in [(row.get("First Name") or "").strip(),
                                 (row.get("Last Name") or "").strip()] if x)
    raw_parcel = (row.get("Parcel id") or row.get("Apn") or "").strip()

    ppt, used_parcel, errored = None, "", False
    for cand in parcel_candidates(raw_parcel):
        got = fetch_ppt(cand)
        if got is ERROR:
            errored = True
            continue
        if got and got.get("bills"):
            ppt, used_parcel, errored = got, cand, False
            break

    if not ppt:                                   # fall back to address search
        acct = lookup_account_by_address(addr)
        if acct is ERROR:
            errored = True
        elif acct:
            got = fetch_ppt(acct)
            if got is ERROR:
                errored = True
            elif got and got.get("bills"):
                ppt, used_parcel, errored = got, acct, False

    rec = {
        "row": idx, "owner": owner, "tax_api_owner": owner_from_ppt(ppt or {}),
        "address": addr, "city": (row.get("Property city") or "").strip(),
        "zip": (row.get("Property zip") or "").strip(),
        "raw_parcel": raw_parcel, "parcel_used": used_parcel,
        "est_value": (row.get("Estimated value") or "").strip(),
        "found": bool(ppt), "error": errored and not ppt,
        **parse_amounts(ppt or {}),
    }

    global _done
    with _progress_lock:
        _done += 1
        n = _done
        _all_results[rec["row"]] = rec
        if _done % CHECKPOINT_EVERY == 0:
            _write_csv(list(_all_results.values()))
    flag = "  [LOOKUP ERROR]" if rec["error"] else ("" if ppt else "  [NO API MATCH]")
    print(f"[{n}/{total}] {addr:<30.30} parcel={used_parcel or '-':<12} "
          f"full=${rec['full_amount_due']:>10,.2f} "
          f"(delinq ${rec['delinquent_due']:>9,.2f}){flag}", flush=True)
    return rec


# ───────────────────────── orchestration ─────────────────────────
def _load_existing() -> dict[int, dict]:
    """Reload prior results; keep only rows with a definite answer (no error)."""
    if not os.path.exists(OUT_PATH):
        return {}
    done = {}
    with open(OUT_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("error", "").strip().lower() == "true":
                continue
            try:
                r["row"] = int(r["row"])
            except (TypeError, ValueError):
                continue
            for k in ("full_amount_due", "delinquent_due", "current_due", "turned_over_due"):
                r[k] = float(r.get(k) or 0)
            for k in ("years_owed_count", "n_bills"):
                r[k] = int(float(r.get(k) or 0))
            r["found"] = str(r.get("found")).strip().lower() == "true"
            r["error"] = False
            done[r["row"]] = r
    return done


def run(csv_path: str, fresh: bool = False) -> None:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    total = len(rows)

    done = {} if fresh else _load_existing()
    if done:
        print(f"Resuming: {len(done)} rows already fetched, "
              f"{total - len(done)} to go.\n")
    else:
        print(f"Loaded {total} records from {csv_path}\n")

    todo = [(i, row, total) for i, row in enumerate(rows, 1) if i not in done]
    _all_results.update(done)

    for p in range(1, MAX_PASSES + 1):
        if not todo:
            break
        if p > 1:
            cd = COOLDOWNS[min(p - 2, len(COOLDOWNS) - 1)]
            print(f"\n-- pass {p}: retrying {len(todo)} stragglers after {cd}s cooldown --\n",
                  flush=True)
            time.sleep(cd)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            batch = list(ex.map(process_row, todo))
        _write_csv(list(_all_results.values()))     # checkpoint after every pass
        todo = [(rec["row"], rows[rec["row"] - 1], total) for rec in batch if rec["error"]]

    _write_csv(list(_all_results.values()))
    _summary(list(_all_results.values()))


def _summary(results: list[dict]) -> None:
    owes = [r for r in results if r["full_amount_due"] > 0]
    delinq = [r for r in results if r["delinquent_due"] > 0]
    turned = [r for r in results if r["turned_over_due"] > 0]
    not_found = [r for r in results if not r["found"] and not r["error"]]
    errors = [r for r in results if r["error"]]
    g_full = sum(r["full_amount_due"] for r in results)
    g_delq = sum(r["delinquent_due"] for r in results)
    g_curr = sum(r["current_due"] for r in results)
    g_turn = sum(r["turned_over_due"] for r in results)

    print("\n" + "=" * 72)
    print(f"  Records processed:                {len(results)}")
    print(f"  Matched in Knox tax API:          {len(results) - len(not_found) - len(errors)}")
    print(f"  No API match (parcel not found):  {len(not_found)}")
    print(f"  Lookup errors (re-run to fill):   {len(errors)}")
    print(f"  Properties owing anything:        {len(owes)}")
    print(f"  Properties with delinquent bills: {len(delinq)}")
    print(f"  Properties in tax-sale pipeline:  {len(turned)}")
    print("  " + "-" * 60)
    print(f"  FULL amount owed (all unpaid):    ${g_full:,.2f}")
    print(f"    of which delinquent:            ${g_delq:,.2f}")
    print(f"    of which current-year:          ${g_curr:,.2f}")
    print(f"    of which turned over (tax sale):${g_turn:,.2f}")
    print("  " + "-" * 60)
    print(f"  Results CSV:                      {OUT_PATH}")
    if errors:
        print(f"  NOTE: {len(errors)} lookups still errored. Re-run the same command to resume.")
    print("=" * 72)

    top = sorted(owes, key=lambda r: r["full_amount_due"], reverse=True)[:15]
    if top:
        print("\n  Top 15 by full amount owed:")
        for r in top:
            print(f"    ${r['full_amount_due']:>11,.2f}  {r['years_owed_count']:>2} yr  "
                  f"{r['address'][:34]:<34}  {r['owner']}")


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:]]
    fresh = "--fresh" in argv
    argv = [a for a in argv if a != "--fresh"]
    if "--workers" in argv:
        i = argv.index("--workers")
        WORKERS = int(argv[i + 1])
        del argv[i:i + 2]
    path = argv[0] if argv else "Tax delinquent check obituary data.csv"
    run(path, fresh=fresh)
