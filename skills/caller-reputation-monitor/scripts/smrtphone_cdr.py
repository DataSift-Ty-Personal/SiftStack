"""
smrtphone_cdr.py - L1 signal from SmrtPhone's OWN call log (no Telnyx needed).

The behavioral, ground-truth layer of the caller-reputation monitor, sourced from
SmrtPhone instead of Telnyx CDR - so it works for any SmrtPhone user with zero telecom
setup (no port, no account verification, no monthly fee). A number whose answer rate
craters is a labeled number; that is exactly what this measures.

Pulls the SmrtPhone call log (POST /logs/calls/filtered, the DataTables endpoint) over
a trailing window using the saved browser session (smrtphone_state.json,
captured by smrtphone_login.py - the same session pull_smrtphone_numbers.py uses),
groups OUTBOUND calls by the originating number (from_num), and computes per-DID:
  - dials       total outbound attempts
  - answered    calls that connected (duration > 0)
  - asr         answered / dials            (target >= 30%)
  - aloc        mean connected duration     (target >= 30s)
  - short_pct   share of answered under 6s  (target <= 15%)

Returns the SAME shape as telnyx_cdr.pull_day_metrics(), so it is a drop-in L1 source.
A trailing multi-day window (default 7) is used instead of a single day so each number
has a stable sample (~200+ dials) rather than a noisy ~35-dial single day.

Stdlib only (urllib). Reuses the proven form/field mapping from
a proven SmrtPhone call-log integration.
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("SMRTPHONE_STATE", str(Path(__file__).resolve().parent / "smrtphone_state.json")))
BASE = "https://phone.smrt.studio"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
PAGE_SIZE = 200
MAX_PAGES = 200                # safety valve (~40k calls)
SHORT_CALL_SEC = 6.0

COLUMNS = ["id", "user", "user_id", "created_at", "direction", "status",
           "disposition", "from_num", "to_num", "price", "duration",
           "podio_id", "recording_sid", "sid", "call_agent_id"]


def session_available() -> bool:
    return STATE.exists() and _cookie_header() != ""


def _cookie_header() -> str:
    if not STATE.exists():
        return ""
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    return "; ".join(f"{c['name']}={c['value']}" for c in st.get("cookies", [])
                     if "smrt.studio" in c.get("domain", ""))


def _digits10(raw: str) -> str:
    d = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


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


def _fetch_page(cookie: str, start: int, length: int, timeout: int) -> dict:
    """One call-log page. Raises PermissionError on an expired session."""
    data = urllib.parse.urlencode(_page_form(start, length)).encode()
    req = urllib.request.Request(BASE + "/logs/calls/filtered", data=data, method="POST",
        headers={
            "cookie": cookie, "user-agent": UA,
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest", "accept": "application/json, */*",
            "origin": BASE, "referer": BASE + "/logs/calls",
        })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    if body.lstrip().startswith("<"):
        raise PermissionError("SmrtPhone session expired (HTML response). "
                              "Re-run smrtphone_login.py.")
    return json.loads(body)


def _is_outbound(direction) -> bool:
    return "out" in str(direction or "").lower()


def pull_metrics(*, days: int = 7, pool: list[str] | None = None,
                 timeout: int = 60) -> dict:
    """Pull the last `days` of SmrtPhone outbound calls -> per-DID metrics.

    Returns (same shape as telnyx_cdr.pull_day_metrics):
      { "ok", "day" (window label), "window": {gte,lt}, "pages", "rows",
        "metrics": {"<10-digit>": {number,dials,answered,asr,aloc,short_pct,
                                   short_calls,talk_total_sec}},
        "truncated", "errors" }
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    window = {"gte": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "lt": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    label = f"last {days}d"

    cookie = _cookie_header()
    acc: dict[str, dict] = {}

    def bucket(n10: str) -> dict:
        return acc.setdefault(n10, {"number": n10, "dials": 0, "answered": 0,
                                    "short_calls": 0, "talk_total_sec": 0.0})

    if pool:
        for raw in pool:
            n10 = _digits10(raw)
            if len(n10) == 10:
                bucket(n10)

    if not cookie:
        return {"ok": False, "day": label, "window": window, "pages": 0, "rows": 0,
                "metrics": _finalize(acc), "truncated": False,
                "errors": ["no SmrtPhone session (smrtphone_state.json)"]}

    total_rows = 0
    pages = 0
    truncated = False
    errors: list[str] = []
    start = 0
    stop = False
    while pages < MAX_PAGES and not stop:
        try:
            page = _fetch_page(cookie, start, PAGE_SIZE, timeout)
        except PermissionError as e:
            errors.append(str(e))
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            errors.append(f"page {start}: {e}")
            break
        rows = page.get("data") or []
        recs_total = page.get("recordsTotal")
        pages += 1
        for row in rows:
            created = (row.get("created_at") or {}).get("date", "") or ""
            created19 = created[:19]
            if created19 and created19 < cutoff_str:
                stop = True
                break
            if not _is_outbound(row.get("direction")):
                continue
            n10 = _digits10((row.get("from_num") or {}).get("fromNum", ""))
            if len(n10) != 10:
                continue
            total_rows += 1
            b = bucket(n10)
            b["dials"] += 1
            dur = row.get("duration") or 0
            try:
                dur = float(dur)
            except (TypeError, ValueError):
                dur = 0.0
            if dur > 0:                       # connected/answered
                b["answered"] += 1
                b["talk_total_sec"] += dur
                if dur < SHORT_CALL_SEC:
                    b["short_calls"] += 1
        start += PAGE_SIZE
        if not rows or (recs_total is not None and start >= int(recs_total)):
            break
        time.sleep(0.35)
    else:
        if pages >= MAX_PAGES:
            truncated = True
            errors.append(f"hit MAX_PAGES={MAX_PAGES}")

    ok = not errors or total_rows > 0
    return {"ok": ok, "day": label, "window": window, "pages": pages,
            "rows": total_rows, "metrics": _finalize(acc),
            "truncated": truncated, "errors": errors}


def _finalize(acc: dict) -> dict:
    metrics = {}
    for n10, b in acc.items():
        dials, answered = b["dials"], b["answered"]
        metrics[n10] = {
            "number": n10,
            "dials": dials,
            "answered": answered,
            "asr": round(100.0 * answered / dials, 1) if dials else None,
            "aloc": round(b["talk_total_sec"] / answered, 1) if answered else None,
            "short_pct": round(100.0 * b["short_calls"] / answered, 1) if answered else None,
            "short_calls": b["short_calls"],
            "talk_total_sec": round(b["talk_total_sec"], 1),
        }
    return metrics


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys
    out = pull_metrics(days=int(sys.argv[1]) if len(sys.argv) > 1 else 7)
    # trim metrics for readability
    print(json.dumps({**out, "metrics": {k: v for k, v in list(out["metrics"].items())}},
                     indent=2))
