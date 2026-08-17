"""
telnyx_reputation.py - Telnyx Number Reputation (L2) read + remediation client.

Layer 2 of the caller-reputation stack: the carrier-grade spam signal (Hiya-fed,
all engines fused into one aggregate score) plus the remediation API to dispute a
flag hands-off.

Two responsibilities:
  1. READ reputation for the monitored DIDs. A free cached list-sweep each run;
     a BILLABLE fresh recheck only on DIDs a cheaper layer already flagged as degraded.
  2. REMEDIATE. Submit a flagged DID for reputation remediation and poll the result
     buckets.

Stdlib only (urllib), defensive .get() reads. Enterprise-scoped: needs
TELNYX_API_KEY + TELNYX_ENTERPRISE_ID. One-time enterprise/LOA/number-association
setup lives in telnyx_setup.py.

Contract: TELNYX-API-CONTRACT.md (verified 2026-07-06). Key facts encoded here:
  - reputation_data is AGGREGATE only (spam_risk / spam_category + 4 scores); there is
    NO per-carrier/per-engine breakdown from Telnyx.
  - The FIRST read of a never-scored DID auto-bills a fresh check; only post-cache
    reads are free. read_all_cached() therefore reports which DIDs are still unscored
    so the caller can avoid a fresh storm.
  - Remediation is async: POST -> 202 + data.id, then poll GET-by-id for 5 buckets.
  - Encode the leading + of every E.164 in the path as %2B.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.telnyx.com/v2"
LIST_PAGE_SIZE = 250          # max for the reputation numbers list
MAX_LIST_PAGES = 50
MAX_RETRIES = 4
RETRY_BACKOFF = 1.5

# spam_risk -> our status vocabulary
RISK_STATUS = {"low": "Clean", "medium": "Watch", "high": "Flagged"}


def _e164(raw: str) -> str:
    """Coerce any US number format to +1NPANXXXXXX."""
    d = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(d) == 10:
        d = "1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    # already E.164-ish or non-US; return best effort with a leading +
    return raw if str(raw).startswith("+") else "+" + d


def _enc(e164: str) -> str:
    """Path-encode an E.164 number (leading + becomes %2B)."""
    return urllib.parse.quote(e164, safe="")


def _request(method: str, url: str, api_key: str, *, body: dict | None = None,
             timeout: int = 30) -> dict:
    """One HTTP call with retry/backoff. Returns
    {"ok": True, "status": int, "json": {...}} or {"ok": False, "error": "...",
    "status": int|None} - never raises."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    last_err = None
    last_status = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                parsed = json.loads(raw) if raw.strip() else {}
                return {"ok": True, "status": resp.status, "json": parsed}
        except urllib.error.HTTPError as e:
            last_status = e.code
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            # 409 (in-flight remediation) and 4xx are terminal; surface the body.
            last_err = f"HTTP {e.code} {body_txt[:400]}".strip()
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            return {"ok": False, "status": e.code, "error": last_err,
                    "body": body_txt}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = str(e)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
    return {"ok": False, "status": last_status, "error": last_err or "unknown"}


def _rep_base(enterprise_id: str) -> str:
    return f"{API_BASE}/enterprises/{urllib.parse.quote(enterprise_id)}/reputation"


# ---- status mapping ----
def classify_reputation(rep_data: dict, th: dict | None = None) -> str:
    """Map a reputation_data dict to Clean / Watch / Flagged.

    Rule (carrier-grade, from the fusion spec): spam_category non-null -> Flagged
    (a label is present); spam_risk high -> Flagged; medium -> Watch; low -> Clean.
    Unknown/None spam_risk with no category -> Pending (not yet scored).
    """
    if not isinstance(rep_data, dict):
        return "Pending"
    if rep_data.get("spam_category"):           # non-null, non-empty => a label exists
        return "Flagged"
    risk = (rep_data.get("spam_risk") or "").lower()
    if risk in RISK_STATUS:
        return RISK_STATUS[risk]
    return "Pending"


def reputation_reasons(rep_data: dict) -> list[str]:
    out = []
    if not isinstance(rep_data, dict):
        return out
    if rep_data.get("spam_category"):
        out.append(f"spam_category={rep_data.get('spam_category')}")
    risk = (rep_data.get("spam_risk") or "").lower()
    if risk in ("medium", "high"):
        out.append(f"spam_risk={risk}")
    return out


def _extract_rep(number_obj: dict) -> dict:
    """Pull reputation_data out of a numbers-list/get item, reading defensively."""
    rd = (number_obj or {}).get("reputation_data") or {}
    return {
        "phone_number": number_obj.get("phone_number"),
        "spam_risk": rd.get("spam_risk"),
        "spam_category": rd.get("spam_category"),
        "maturity_score": rd.get("maturity_score"),
        "connection_score": rd.get("connection_score"),
        "engagement_score": rd.get("engagement_score"),
        "sentiment_score": rd.get("sentiment_score"),
        "last_refreshed_at": rd.get("last_refreshed_at"),
        "scored": rd.get("spam_risk") is not None or rd.get("last_refreshed_at") is not None,
    }


# ---- READ ----
def read_all_cached(api_key: str, enterprise_id: str, *, timeout: int = 30) -> dict:
    """Free cached sweep of every monitored DID via the reputation numbers list.

    Returns {"ok", "by_number": {"+1...": {rep fields}}, "unscored": ["+1..."],
    "count": int, "error": str|None}. `unscored` DIDs have no cached score yet -
    a fresh read on them would AUTO-BILL, so the caller should refrain (they light
    up for free once check_frequency populates the cache).
    """
    by_number: dict[str, dict] = {}
    unscored: list[str] = []
    page = 1
    while page <= MAX_LIST_PAGES:
        qs = urllib.parse.urlencode({"page[number]": page, "page[size]": LIST_PAGE_SIZE})
        url = f"{_rep_base(enterprise_id)}/numbers?{qs}"
        res = _request("GET", url, api_key, timeout=timeout)
        if not res["ok"]:
            return {"ok": False, "by_number": by_number, "unscored": unscored,
                    "count": len(by_number), "error": res.get("error")}
        payload = res["json"]
        items = payload.get("data", []) or []
        for obj in items:
            rep = _extract_rep(obj)
            pn = rep.get("phone_number")
            if not pn:
                continue
            by_number[pn] = rep
            if not rep["scored"]:
                unscored.append(pn)
        meta = payload.get("meta", {}) or {}
        tp = meta.get("total_pages")
        if isinstance(tp, int) and tp > 0:
            if page >= tp:
                break
        elif not items:
            break
        page += 1
    return {"ok": True, "by_number": by_number, "unscored": unscored,
            "count": len(by_number), "error": None}


def read_one(api_key: str, enterprise_id: str, number: str, *,
             fresh: bool = False, timeout: int = 30) -> dict:
    """Read one DID's reputation. fresh=True forces a BILLABLE live check.

    NOTE: a plain (fresh=False) read on a never-scored DID still auto-bills on
    Telnyx's side - only reads after the cache is warm are free. Prefer
    read_all_cached() for the routine sweep; use this for a single number.
    """
    e = _e164(number)
    url = f"{_rep_base(enterprise_id)}/numbers/{_enc(e)}"
    if fresh:
        url += "?fresh=true"
    res = _request("GET", url, api_key, timeout=timeout)
    if not res["ok"]:
        return {"ok": False, "number": e, "error": res.get("error")}
    obj = (res["json"] or {}).get("data", {}) or {}
    rep = _extract_rep(obj)
    return {"ok": True, "number": e, **rep}


def refresh_degraded(api_key: str, enterprise_id: str, numbers: list[str], *,
                     timeout: int = 30) -> dict:
    """Force a BILLABLE fresh recheck on a batch of degraded DIDs (<=100).

    POST .../numbers/refresh. Returns the raw per-item results plus a convenience
    `refreshed` list. Call read_all_cached() again afterward to pick up new scores.
    """
    numbers = [_e164(n) for n in numbers][:100]
    if not numbers:
        return {"ok": True, "refreshed": [], "results": [], "note": "no numbers"}
    url = f"{_rep_base(enterprise_id)}/numbers/refresh"
    res = _request("POST", url, api_key, body={"phone_numbers": numbers}, timeout=timeout)
    if not res["ok"]:
        return {"ok": False, "error": res.get("error"), "requested": numbers}
    data = (res["json"] or {}).get("data", {}) or {}
    results = data.get("results", []) or []
    refreshed = [r.get("phone_number") for r in results if r.get("success")]
    return {"ok": True, "refreshed": refreshed, "results": results,
            "total_requested": data.get("total_requested"),
            "total_successful": data.get("total_successful"),
            "total_failed": data.get("total_failed")}


# ---- REMEDIATE ----
def submit_remediation(api_key: str, enterprise_id: str, numbers: list[str], *,
                       call_purpose: str, contact_email: str | None = None,
                       webhook_url: str | None = None, timeout: int = 30) -> dict:
    """Submit DIDs for reputation remediation. Async -> returns a request id.

    Returns {"ok", "request_id", "status", "submitted", "ineligible", "count",
    "conflict": bool, "error"}. conflict=True means a 409 (a number already has an
    in-flight request) - back off and poll the existing one instead of resubmitting.
    """
    nums = [_e164(n) for n in numbers][:2000]
    if not nums:
        return {"ok": False, "error": "no numbers"}
    body = {"phone_numbers": nums, "call_purpose": call_purpose[:2000]}
    if contact_email:
        body["contact_email"] = contact_email[:255]
    if webhook_url:
        body["webhook_url"] = webhook_url[:2048]
    url = f"{_rep_base(enterprise_id)}/remediation"
    res = _request("POST", url, api_key, body=body, timeout=timeout)
    if not res["ok"]:
        return {"ok": False, "conflict": res.get("status") == 409,
                "status": res.get("status"), "error": res.get("error"),
                "requested": nums}
    data = (res["json"] or {}).get("data", {}) or {}
    return {
        "ok": True,
        "request_id": data.get("id"),
        "status": data.get("status"),
        "count": data.get("phone_numbers_count"),
        "submitted": data.get("phone_numbers_submitted"),
        "ineligible": data.get("phone_numbers_ineligible"),
        "requested": nums,
    }


def poll_remediation(api_key: str, enterprise_id: str, request_id: str, *,
                     timeout: int = 30) -> dict:
    """Poll a remediation request. Returns status + the 5 result buckets (or None
    while pending)."""
    url = f"{_rep_base(enterprise_id)}/remediation/{urllib.parse.quote(request_id)}"
    res = _request("GET", url, api_key, timeout=timeout)
    if not res["ok"]:
        return {"ok": False, "error": res.get("error"), "request_id": request_id}
    data = (res["json"] or {}).get("data", {}) or {}
    results = data.get("results")  # null until ready; then all 5 buckets present
    buckets = {}
    if isinstance(results, dict):
        for key in ("remediated", "not_flagged", "requires_review",
                    "ineligible", "refused"):
            buckets[key] = results.get(key, []) or []
    status = data.get("status")
    return {
        "ok": True,
        "request_id": request_id,
        "status": status,
        "terminal": status in ("completed", "failed", "cancelled"),
        "results": buckets or None,
        "tier1_completed_at": data.get("tier1_completed_at"),
        "tier2_completed_at": data.get("tier2_completed_at"),
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import os
    import sys

    key = os.environ.get("TELNYX_API_KEY")
    ent = os.environ.get("TELNYX_ENTERPRISE_ID")
    if not (key and ent):
        print("Set TELNYX_API_KEY and TELNYX_ENTERPRISE_ID to smoke-test.")
        sys.exit(2)
    print(json.dumps(read_all_cached(key, ent), indent=2))
