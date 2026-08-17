"""
telnyx_cdr.py - Telnyx Detail Records (CDR) client -> per-DID call-outcome metrics.

This is Layer 1 (L1) of the caller-reputation stack: our OWN outbound-call outcomes,
the ground-truth behavioral signal that degrades BEFORE a carrier spam label lands.
Free (no per-query charge) and needs only API access to the Telnyx account that owns
the DIDs.

For each originating number (our DID) it computes, over a UTC day window:
  - dials       total outbound attempts
  - answered    calls with call_sec > 0
  - asr         answered / dials            (Answer-Seizure Ratio, target >= 30%)
  - aloc        mean talk time on answered  (target >= 30s)
  - short_pct   share of answered under 6s  (target <= 15%)

Stdlib only (urllib) - runs on the bare Python312 interpreter the _api cron uses,
same as ipqs.py. Every field is read defensively with .get(): Telnyx does not formally
type voice detail records in its OpenAPI spec, so a field rename must not crash us.

Contract: see TELNYX-API-CONTRACT.md (verified 2026-07-06 against Telnyx's live spec).

Endpoint:
  GET https://api.telnyx.com/v2/detail_records
  filter[record_type]=call-control|sip-trunking, filter[direction]=outbound,
  filter[created_at][gte/lt]=<UTC>, sort=-created_at, page[size]<=50
"""
from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://api.telnyx.com/v2/detail_records"
PAGE_SIZE = 50               # Telnyx hard cap
MAX_PAGES = 400              # safety valve (~20k rows/day); we alert if we hit it
MAX_RETRIES = 4
RETRY_BACKOFF = 1.5          # seconds, doubled each retry
SHORT_CALL_SEC = 6.0         # a "short call" is talk time strictly under this

# Outbound dialer traffic is call-control (Voice API) or sip-trunking (SIP trunk),
# never webrtc. We query both record types and union - whichever carries the traffic.
RECORD_TYPES = ("call-control", "sip-trunking")


def _digits10(raw: str) -> str:
    """Strip to a 10-digit US number (drop a leading country-code 1)."""
    d = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def _parse_iso(ts) -> datetime.datetime | None:
    """Parse a Telnyx ISO-8601 timestamp (…Z or +00:00). Returns None on junk."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        # Some rows carry fractional seconds with >6 digits; trim to microseconds.
        try:
            if "." in s:
                head, _, tail = s.partition(".")
                frac = "".join(ch for ch in tail if ch.isdigit())[:6]
                off = tail[len(frac):] if len(tail) > len(frac) else ""
                return datetime.datetime.fromisoformat(f"{head}.{frac}{off}")
        except ValueError:
            pass
    return None


def _talk_seconds(row: dict) -> float | None:
    """Fractional talk time for an answered row, from finished_at - started_at.

    Falls back to the integer call_sec if timestamps are missing. billed_sec is
    deliberately NOT used (it rounds up to the billing increment and overstates
    short calls).
    """
    start = _parse_iso(row.get("started_at"))
    end = _parse_iso(row.get("finished_at"))
    if start and end:
        delta = (end - start).total_seconds()
        if delta >= 0:
            return delta
    cs = row.get("call_sec")
    if isinstance(cs, (int, float)):
        return float(cs)
    return None


def _is_answered(row: dict) -> bool:
    """Answered = connected duration > 0. The real-time CDR JSON has no answer flag."""
    cs = row.get("call_sec")
    if isinstance(cs, (int, float)) and cs > 0:
        return True
    # Defensive fallback if call_sec is absent but a positive talk delta exists.
    t = _talk_seconds(row)
    return bool(t and t > 0)


def _fetch_page(api_key: str, record_type: str, gte: str, lt: str,
                page: int, timeout: int) -> dict:
    """Fetch one page. Returns {"ok": True, "data": [...], "meta": {...}} or
    {"ok": False, "error": "..."} - never raises."""
    qs = urllib.parse.urlencode({
        "filter[record_type]": record_type,
        "filter[direction]": "outbound",
        "filter[created_at][gte]": gte,
        "filter[created_at][lt]": lt,
        "sort": "-created_at",
        "page[number]": page,
        "page[size]": PAGE_SIZE,
    })
    url = f"{ENDPOINT}?{qs}"
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            return {"ok": True, "data": data.get("data", []) or [],
                    "meta": data.get("meta", {}) or {}}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            last_err = f"HTTP {e.code} {body}".strip()
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            return {"ok": False, "error": last_err}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = str(e)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
    return {"ok": False, "error": last_err or "unknown"}


def _day_window(day: datetime.date) -> tuple[str, str]:
    """UTC [00:00:00, next 00:00:00) window strings for one calendar day."""
    gte = datetime.datetime.combine(day, datetime.time.min,
                                    tzinfo=datetime.timezone.utc)
    lt = gte + datetime.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return gte.strftime(fmt), lt.strftime(fmt)


def pull_day_metrics(api_key: str, *, day: datetime.date | None = None,
                     pool: list[str] | None = None, timeout: int = 30) -> dict:
    """Pull one UTC day of outbound CDRs and return per-DID metrics.

    Args:
      api_key: Telnyx v2 API key.
      day:     UTC calendar day to pull (default: yesterday UTC).
      pool:    optional list of DID numbers (any format) to restrict/seed the
               result. DIDs in the pool with no traffic get a zero-dial record so
               the caller can see "dead day" (a spam signal). Numbers dialed FROM
               outside the pool are still returned (surfaces unexpected senders).

    Returns:
      {
        "ok": bool,
        "day": "YYYY-MM-DD",
        "window": {"gte": ..., "lt": ...},
        "pages": int, "rows": int,
        "metrics": { "<10-digit>": {number, dials, answered, asr, aloc,
                                    short_pct, short_calls, talk_total_sec} },
        "truncated": bool,       # hit MAX_PAGES on some record_type
        "errors": [ "..." ],     # per-record-type fetch errors (partial data possible)
      }
    """
    if day is None:
        day = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)
    gte, lt = _day_window(day)

    # accumulators keyed by 10-digit originating number
    acc: dict[str, dict] = {}
    seen_call_ids: set = set()   # de-dupe across record_type unions

    def bucket(n10: str) -> dict:
        return acc.setdefault(n10, {
            "number": n10, "dials": 0, "answered": 0,
            "short_calls": 0, "talk_total_sec": 0.0,
        })

    # seed pool DIDs so a zero-traffic day is visible
    if pool:
        for raw in pool:
            n10 = _digits10(raw)
            if len(n10) == 10:
                bucket(n10)

    total_rows = 0
    total_pages = 0
    truncated = False
    errors: list[str] = []

    for record_type in RECORD_TYPES:
        page = 1
        while page <= MAX_PAGES:
            res = _fetch_page(api_key, record_type, gte, lt, page, timeout)
            if not res["ok"]:
                errors.append(f"{record_type} p{page}: {res['error']}")
                break
            rows = res["data"]
            total_pages += 1
            for row in rows:
                cid = row.get("call_id") or row.get("id") or row.get("telnyx_leg_id")
                if cid and cid in seen_call_ids:
                    continue
                if cid:
                    seen_call_ids.add(cid)
                n10 = _digits10(row.get("cli") or row.get("caller_number") or "")
                if len(n10) != 10:
                    continue
                total_rows += 1
                b = bucket(n10)
                b["dials"] += 1
                if _is_answered(row):
                    b["answered"] += 1
                    talk = _talk_seconds(row) or 0.0
                    b["talk_total_sec"] += talk
                    if talk < SHORT_CALL_SEC:
                        b["short_calls"] += 1

            meta = res["meta"]
            tp = meta.get("total_pages")
            if isinstance(tp, int) and tp > 0:
                if page >= tp:
                    break
            elif not rows:
                break
            page += 1
        else:
            truncated = True
            errors.append(f"{record_type}: hit MAX_PAGES={MAX_PAGES}, data truncated")

    # finalize rates
    metrics: dict[str, dict] = {}
    for n10, b in acc.items():
        dials = b["dials"]
        answered = b["answered"]
        asr = round(100.0 * answered / dials, 1) if dials else None
        aloc = round(b["talk_total_sec"] / answered, 1) if answered else None
        short_pct = round(100.0 * b["short_calls"] / answered, 1) if answered else None
        metrics[n10] = {
            "number": n10,
            "dials": dials,
            "answered": answered,
            "asr": asr,               # percent, or None if no dials
            "aloc": aloc,             # seconds, or None if no answers
            "short_pct": short_pct,   # percent of answered, or None if no answers
            "short_calls": b["short_calls"],
            "talk_total_sec": round(b["talk_total_sec"], 1),
        }

    ok = not errors or total_rows > 0
    return {
        "ok": ok,
        "day": day.isoformat(),
        "window": {"gte": gte, "lt": lt},
        "pages": total_pages,
        "rows": total_rows,
        "metrics": metrics,
        "truncated": truncated,
        "errors": errors,
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import os
    import sys

    key = os.environ.get("TELNYX_API_KEY")
    if not key:
        print("Set TELNYX_API_KEY to smoke-test.")
        sys.exit(2)
    out = pull_day_metrics(key)
    print(json.dumps(out, indent=2))
