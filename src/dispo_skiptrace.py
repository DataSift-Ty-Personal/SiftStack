"""Three-source dispo skip-trace waterfall with a per-contact audit matrix.

Built from the 158 Old State dispo run. For every contact it runs:
  Source 1  Enformion Person Search (devapi PersonSearch, address-anchored)
  Source 2  Tracerfy batch trace ($0.02/record)
  Source 3  web people-search cross-check  (MANUAL: this module leaves a slot
            and merges results you drop in; the aggregators bot-block, so the
            web pass is run by an agent/browser, not here)
then de-dupes the union, Trestle-scores every number, and emits an AUDIT so you
can see at a glance which numbers are single-source vs cross-confirmed and which
source missed a given contact (the landline question: "when did we skip-trace
this at Tracerfy AND Enformion?").

Dial tiers (phone_validator standard): 81-100 first, 61-80 second, 41-60 third,
<=40 drop.

Input JSON: list of contacts
  [{"label": "...", "first": "..", "last": "..",
    "address": "..", "city": "..", "state": "TN", "zip": ".."}, ...]
(entities with no person: give "label" only and resolve the principal first via
buyer_sweep / enformion_business.)

Usage:
  python src/dispo_skiptrace.py --contacts contacts.json --out output/skiptrace_run
  python src/dispo_skiptrace.py --contacts contacts.json --web web_crosscheck.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import config as cfg

logger = logging.getLogger(__name__)

TRACERFY_TRACE = "https://tracerfy.com/v1/api/trace/"
TRACERFY_QUEUE = "https://tracerfy.com/v1/api/queue/"
TRACERFY_PHONES = ["primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
                   "mobile_5", "landline_1", "landline_2", "landline_3"]
TRACERFY_EMAILS = ["email_1", "email_2", "email_3", "email_4", "email_5"]


def digits(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    return d[-10:] if len(d) >= 10 else d


def fmt_phone(d: str) -> str:
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else d


# ── Source 1: Enformion ───────────────────────────────────────────────

def enformion_pass(contacts: list[dict]) -> dict[str, dict]:
    from enformion_heir import person_search
    out: dict[str, dict] = {}
    for c in contacts:
        if not (c.get("first") and c.get("last")):
            continue
        data = person_search(c["first"], c["last"], city=c.get("city", ""),
                             state=c.get("state", "TN"), zip_code=c.get("zip", ""))
        best = _best_person(data.get("persons") or [], c)
        phones, emails = {}, set()
        if best:
            for ph in best.get("phoneNumbers") or []:
                d = digits(ph.get("phoneNumber", ""))
                if d:
                    phones[d] = {"type": ph.get("phoneType"), "seen": ph.get("lastReportedDate")}
            emails = {e.get("emailAddress") or "" for e in best.get("emailAddresses") or []}
        out[c["label"]] = {"phones": phones, "emails": {e for e in emails if e},
                           "age": (best or {}).get("age"),
                           "address": ((best or {}).get("addresses") or [{}])[0].get("fullAddress", "")}
        time.sleep(0.6)
    return out


def _best_person(persons: list[dict], contact: dict) -> dict | None:
    """Anchor on the contact's zip/city to beat common-name collisions."""
    if not persons:
        return None
    zc, city = contact.get("zip", ""), (contact.get("city") or "").upper()
    for p in persons:
        for a in p.get("addresses") or []:
            addr = (a.get("fullAddress") or "").upper()
            if (zc and zc in addr) or (city and city in addr):
                return p
    return persons[0]


# ── Source 2: Tracerfy ────────────────────────────────────────────────

def tracerfy_pass(contacts: list[dict]) -> dict[str, dict]:
    people = [c for c in contacts if c.get("first") and c.get("last")]
    if not people or not cfg.TRACERFY_API_KEY:
        return {}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["first_name", "last_name", "address", "city", "state", "zip",
                "mail_address", "mail_city", "mail_state"])
    for c in people:
        w.writerow([c["first"], c["last"], c.get("address", ""), c.get("city", ""),
                    c.get("state", "TN"), c.get("zip", ""), "", "", ""])
    try:
        resp = requests.post(TRACERFY_TRACE, headers={"Authorization": f"Bearer {cfg.TRACERFY_API_KEY}"},
                             data={f"{k}_column": k for k in ("first_name", "last_name", "address",
                                   "city", "state", "zip", "mail_address", "mail_city", "mail_state")}
                             | {"mailing_zip_column": "zip"},
                             files={"csv_file": ("batch.csv", buf.getvalue(), "text/csv")}, timeout=30)
    except requests.RequestException as e:
        logger.warning("Tracerfy submit failed: %s", e)
        return {}
    if resp.status_code == 402:
        logger.error("Tracerfy 402 INSUFFICIENT CREDITS")
        return {}
    if resp.status_code != 200:
        logger.warning("Tracerfy HTTP %s: %s", resp.status_code, resp.text[:200])
        return {}
    qid = resp.json().get("queue_id")
    records = []
    for _ in range(60):
        time.sleep(5)
        try:
            d = requests.get(f"{TRACERFY_QUEUE}{qid}",
                            headers={"Authorization": f"Bearer {cfg.TRACERFY_API_KEY}"}, timeout=15).json()
        except (requests.RequestException, ValueError):
            continue
        if isinstance(d, list):
            records = d
            break
        if isinstance(d, dict):
            if d.get("status") == "failed":
                return {}
            if d.get("status") == "completed":
                records = d.get("records", [])
                break
    # Match records back to contacts by (first,last) order.
    out: dict[str, dict] = {}
    by_name = {(r.get("first_name", "").upper(), r.get("last_name", "").upper()): r for r in records}
    for c in people:
        rec = by_name.get((c["first"].upper(), c["last"].upper()))
        if not rec:
            out[c["label"]] = {"phones": {}, "emails": set()}
            continue
        phones = {digits(rec[f]): {"type": None, "seen": None}
                  for f in TRACERFY_PHONES if (rec.get(f) or "").strip()}
        emails = {rec[f] for f in TRACERFY_EMAILS if (rec.get(f) or "").strip()}
        out[c["label"]] = {"phones": {k: v for k, v in phones.items() if k}, "emails": emails}
    return out


# ── Trestle scoring ───────────────────────────────────────────────────

def trestle_score(all_digits: set[str]) -> dict[str, dict]:
    from phone_validator import call_trestle
    scores: dict[str, dict] = {}
    key = cfg.TRESTLE_API_KEY
    if not key:
        return scores
    for d in sorted(all_digits):
        try:
            r = call_trestle(d, key)
            scores[d] = {"score": r.get("activity_score"), "line": r.get("line_type"),
                         "carrier": r.get("carrier")}
        except Exception:  # noqa: BLE001
            scores[d] = {"score": None, "line": None, "carrier": None}
    return scores


def tier(score) -> str:
    if score is None:
        return "unscored"
    if score >= 81:
        return "1-DIAL FIRST"
    if score >= 61:
        return "2-second"
    if score >= 41:
        return "3-third"
    return "4-drop"


# ── Merge + audit ─────────────────────────────────────────────────────

def merge(contacts, enf, trac, web) -> list[dict]:
    merged = []
    all_digits: set[str] = set()
    for c in contacts:
        label = c["label"]
        sources = {"enformion": enf.get(label, {}), "tracerfy": trac.get(label, {}),
                   "web": web.get(label, {})}
        phone_sources: dict[str, set[str]] = {}
        emails: set[str] = set()
        for sname, sdata in sources.items():
            for d in (sdata.get("phones") or {}):
                phone_sources.setdefault(d, set()).add(sname)
            emails |= set(sdata.get("emails") or [])
        all_digits |= set(phone_sources)
        merged.append({"label": label, "contact": c, "phone_sources": phone_sources,
                       "emails": sorted(e for e in emails if e), "sources_run": sources})
    scores = trestle_score(all_digits)
    for m in merged:
        phones = []
        for d, srcs in m["phone_sources"].items():
            s = scores.get(d, {})
            phones.append({"phone": fmt_phone(d), "digits": d, "score": s.get("score"),
                           "tier": tier(s.get("score")), "line": s.get("line"),
                           "carrier": s.get("carrier"), "sources": sorted(srcs),
                           "confirm_count": len(srcs)})
        phones.sort(key=lambda p: (p["score"] if p["score"] is not None else -1,
                                   p["confirm_count"]), reverse=True)
        m["phones"] = phones
        m["best"] = phones[0] if phones else None
        # Audit: which sources returned ANYTHING for this contact.
        m["audit"] = {s: bool(m["sources_run"][s].get("phones") or m["sources_run"][s].get("emails"))
                      for s in ("enformion", "tracerfy", "web")}
        del m["phone_sources"], m["sources_run"]
    return merged


def run(contacts: list[dict], web: dict | None = None) -> list[dict]:
    web = web or {}
    logger.info("Skip-trace: %d contacts", len(contacts))
    enf = enformion_pass(contacts)
    logger.info("  Enformion done")
    trac = tracerfy_pass(contacts)
    logger.info("  Tracerfy done")
    return merge(contacts, enf, trac, web)


def main() -> int:
    ap = argparse.ArgumentParser(description="3-source dispo skip-trace with audit")
    ap.add_argument("--contacts", required=True, help="JSON list of contacts")
    ap.add_argument("--web", help="Optional web-crosscheck JSON: {label: {phones:{digits:{}}, emails:[]}}")
    ap.add_argument("--out", help="Output stem")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    contacts = json.load(open(args.contacts, encoding="utf-8"))
    web = json.load(open(args.web, encoding="utf-8")) if args.web else {}
    # Normalize web phones to digit keys.
    web = {lbl: {"phones": {digits(k): v for k, v in (d.get("phones") or {}).items()},
                 "emails": set(d.get("emails") or [])} for lbl, d in web.items()}

    merged = run(contacts, web)

    stem = args.out or str(cfg.OUTPUT_DIR / f"skiptrace_{datetime.now():%Y%m%d_%H%M%S}")
    Path(stem).parent.mkdir(parents=True, exist_ok=True)
    json.dump(merged, open(f"{stem}.json", "w", encoding="utf-8"), indent=1, default=str)
    with open(f"{stem}.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["contact", "best_phone", "best_score", "best_tier", "all_phones",
                    "emails", "enformion", "tracerfy", "web", "single_source_flag"])
        for m in merged:
            best = m["best"] or {}
            single = any(p["confirm_count"] == 1 for p in m["phones"])
            w.writerow([
                m["label"], best.get("phone", ""), best.get("score", ""), best.get("tier", ""),
                " | ".join(f"{p['phone']} ({p['score']},{'+'.join(s[:3] for s in p['sources'])})"
                           for p in m["phones"]),
                "; ".join(m["emails"]),
                "Y" if m["audit"]["enformion"] else "MISS",
                "Y" if m["audit"]["tracerfy"] else "MISS",
                "Y" if m["audit"]["web"] else "-",
                "SINGLE-SOURCE" if single else ""])
    print(f"traced {len(merged)} contacts -> {stem}.json + .csv")
    miss = [m["label"] for m in merged if not m["audit"]["tracerfy"] or not m["audit"]["enformion"]]
    if miss:
        print(f"source gaps (enformion or tracerfy missed): {', '.join(miss)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
