"""Stages B to E of the obituary deep-prospecting v5 run.

SmartSkip hands back everybody it can find: on the measured Knox benchmark, 100
owners returned 682 relatives. You cannot mail 682 people to buy 60 houses, and
you should not pay to score 1,661 phone numbers either. This module decides WHO
of those people is a plausible decision maker, and only they go on to Tracerfy
gap-fill and Trestle scoring.

  B  rank    score every relative as a potential signer, keep the top few
  D  gap     Tracerfy, only for ranked DMs that SmartSkip left phoneless
  E  score   Trestle, only on DM phone numbers
  R  report  contacts per record, which is the number that sizes a mail budget

Stage C (obituary and web research: who actually died, date of death, the true
survivor list) is free, mandatory and NOT automatable here. It is what converts
a ranked guess into a known signer set, and it is the only thing that catches
the spouse-obituary trap. Run it on the records this module puts at the top.

Usage:
  python src/obituary_dp_run.py --smartskip output/dp/smartskip_vertical.csv --rank-only
  python src/obituary_dp_run.py --smartskip output/dp/smartskip_vertical.csv --tracerfy --trestle
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "Skills for REI" / "improved" / "deep-prospecting-v5" / "scripts"))

TRACERFY_TRACE_URL = "https://tracerfy.com/v1/api/trace/"
TRACERFY_QUEUE_URL = "https://tracerfy.com/v1/api/queue/"
TRESTLE_ENDPOINT = "https://api.trestleiq.com/3.0/phone_intel"

# Dial tiers, identical to phone_validator / the phone-validator skill.
TIERS = [("Dial First", 81, 100), ("Dial Second", 61, 80), ("Dial Third", 41, 60),
         ("Dial Fourth", 21, 40), ("Drop", 0, 20)]

# Relationship base scores. Tennessee vests real property in the heirs at death
# (T.C.A. 31-2-103), so the signer set is the surviving spouse and the children
# first, then parents, then siblings.
REL_BASE = {
    "Husband": 100, "Wife": 100, "Spouse": 100,
    "Son": 90, "Daughter": 90, "Child": 90,
    "Mother": 55, "Father": 55, "Parent": 55,
    "Brother": 50, "Sister": 50, "Sibling": 50,
    "In-Law": 30,
    "Relative": 40,   # 63% of SmartSkip labels land here, so the signals below decide
}


def tier_for(score):
    if score is None:
        return "Unknown"
    for name, lo, hi in TIERS:
        if lo <= score <= hi:
            return name
    return "Unknown"


def norm_addr(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ── Stage B: who is plausibly a decision maker ────────────────────────

def score_dm(rel, rec):
    """Score one relative as a potential signer. Returns (score, reasons).

    SmartSkip's 'Possible Type' came back generic on 63% of a 100-record batch,
    so the label alone cannot carry this. Surname, shared address, locality and
    age do most of the work when the label says nothing.
    """
    canon = rel.get("canon_rel") or "Relative"
    score = float(REL_BASE.get(canon, 40))
    why = []
    if canon != "Relative":
        why.append(canon.lower())

    if rel.get("deceased"):
        return -1, ["deceased, cannot sign"]

    subj_last = (rec.get("last") or "").strip().lower()
    if subj_last and (rel.get("last") or "").strip().lower() == subj_last:
        score += 15
        why.append("surname matches the owner")

    # Lived at the owner's address: strongest available proxy for immediate family.
    if rel.get("mailing_street") and rec.get("mailing_address") \
            and norm_addr(rel["mailing_street"]) == norm_addr(rec["mailing_address"]):
        score += 12
        why.append("shared the owner's address")

    if rel.get("mailing_state") and rec.get("property_state") \
            and rel["mailing_state"].upper() == rec["property_state"].upper():
        score += 8
        why.append("in state")

    n_ph = len(rel.get("phones") or [])
    if n_ph:
        score += min(10, 4 + 2 * n_ph)
    else:
        why.append("no phone from SmartSkip")

    try:
        age = int(str(rel.get("age") or "").strip())
    except ValueError:
        age = None
    if age is not None:
        if age < 25:
            score -= 25
            why.append(f"age {age}, too young to be handling an estate")
        elif 45 <= age <= 80:
            score += 8
            why.append(f"age {age}")
    return score, why


SPOUSE = {"Husband", "Wife", "Spouse"}
CHILDREN = {"Son", "Daughter", "Child"}
PARENTS = {"Mother", "Father", "Parent"}
SIBLINGS = {"Brother", "Sister", "Sibling"}


def rank_record(rec, max_generic, min_score):
    """Pick the signer set by Tennessee intestacy, not by a flat score.

    T.C.A. 31-2-104: the surviving spouse and the children take. If there are
    children, EVERY child must sign, so this does not cap them at some tidy
    number: six children means six signatures and six letters. Only when no
    spouse, child, parent or sibling is identifiable does it fall back to the
    best-scoring generic relatives.

    In-laws are 28% of what SmartSkip returns here and are almost never signers.
    They are kept separately as dial channels: the way you reach a daughter who
    does not answer is often her husband.
    """
    scored = []
    for rel in rec.get("relatives") or []:
        s, why = score_dm(rel, rec)
        scored.append({**rel, "dm_score": round(s, 1), "dm_why": "; ".join(why)})
    scored.sort(key=lambda x: -x["dm_score"])

    def cls(names):
        return [x for x in scored if x["canon_rel"] in names and not x.get("deceased")]

    spouse, kids = cls(SPOUSE), cls(CHILDREN)
    parents, sibs = cls(PARENTS), cls(SIBLINGS)
    inlaws = [x for x in scored if x["canon_rel"] == "In-Law"]
    generic = [x for x in scored if x["canon_rel"] == "Relative"]

    if kids:
        signers, basis = spouse + kids, "spouse and children (all children must sign)"
    elif spouse:
        signers, basis = spouse, "surviving spouse takes"
    elif parents:
        signers, basis = parents, "no spouse or children, parents take"
    elif sibs:
        signers, basis = sibs, "no spouse, children or parents, siblings take"
    else:
        signers = [x for x in generic if x["dm_score"] >= min_score][:max_generic]
        basis = ("no labelled immediate family, using the best-scoring unlabelled "
                 "relatives, VERIFY before mailing")

    keys = {id(x) for x in signers}
    for x in scored:
        x["is_dm"] = id(x) in keys
    rec["ranked"] = scored
    rec["dms"] = signers
    rec["signer_basis"] = basis
    rec["channels"] = [x for x in inlaws + generic if id(x) not in keys and x.get("phones")]
    return rec


# ── Stage D: Tracerfy gap-fill, DMs with no phone only ────────────────

def tracerfy_gapfill(dms, api_key, poll_s=10, max_polls=30):
    """Batch-trace only the ranked DMs SmartSkip left without a number.

    Returns {dm_key: [numbers]}. Best effort: any failure returns what it has,
    because a gap-fill miss must never take down the run.
    """
    targets = [d for d in dms if not d.get("phones")]
    if not targets or not api_key:
        return {}, {"attempted": len(targets), "reason": "no api key" if not api_key else ""}

    # Verified contract (mirrors tracerfy_skip_tracer.batch_skip_trace): a
    # multipart CSV upload with Bearer auth and explicit column mapping, then
    # GET {queue}/{id} to poll. Not a JSON body, and not an x-api-key header.
    import requests  # noqa: E402  (only needed on this path)

    buf = io.StringIO()
    w = csv.writer(buf)
    # The mail_* columns are NOT optional: omitting them 400s the upload even
    # when every value is blank. Verified live 2026-08-07.
    w.writerow(["first_name", "last_name", "address", "city", "state", "zip",
                "mail_address", "mail_city", "mail_state"])
    for d in targets:
        w.writerow([d.get("first", ""), d.get("last", ""), d.get("mailing_street", ""),
                    d.get("mailing_city", ""), d.get("mailing_state", "") or "TN",
                    d.get("mailing_zip", ""), "", "", ""])

    try:
        resp = requests.post(
            TRACERFY_TRACE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            data={"first_name_column": "first_name", "last_name_column": "last_name",
                  "address_column": "address", "city_column": "city",
                  "state_column": "state", "zip_column": "zip",
                  "mail_address_column": "mail_address", "mail_city_column": "mail_city",
                  "mail_state_column": "mail_state", "mailing_zip_column": "zip"},
            files={"csv_file": ("gapfill.csv", buf.getvalue(), "text/csv")},
            timeout=60)
        if resp.status_code == 402:
            return {}, {"attempted": len(targets), "error": "402 INSUFFICIENT CREDITS"}
        resp.raise_for_status()
        qid = resp.json().get("queue_id")
        if not qid:
            return {}, {"attempted": len(targets), "error": "no queue_id returned"}
    except Exception as e:
        return {}, {"attempted": len(targets), "error": str(e)[:200]}

    records = None
    for _ in range(max_polls):
        time.sleep(poll_s)
        try:
            r = requests.get(f"{TRACERFY_QUEUE_URL}{qid}",
                             headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            return {}, {"attempted": len(targets), "error": str(e)[:200]}
        if isinstance(d, list):
            records = d
            break
        if d.get("status") == "failed":
            return {}, {"attempted": len(targets), "error": "job failed"}
        if d.get("status") == "completed":
            records = d.get("records", [])
            break
    if records is None:
        return {}, {"attempted": len(targets), "error": "timed out",
                    "cost": round(0.02 * len(targets), 2)}

    # Tracerfy returns phones as FLAT columns, not a list: primary_phone,
    # mobile_1..5, landline_1..3, email_1..5. Reading rec["phones"] silently
    # yields nothing and the run looks like a 0% hit rate when it is not.
    phone_fields = (["primary_phone"] + [f"mobile_{i}" for i in range(1, 6)]
                    + [f"landline_{i}" for i in range(1, 4)])
    out = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        key = (f"{(rec.get('first_name') or '').strip()} "
               f"{(rec.get('last_name') or '').strip()}").lower()
        nums, types = [], {}
        for f in phone_fields:
            n = re.sub(r"\D", "", str(rec.get(f) or ""))
            if len(n) == 11 and n.startswith("1"):
                n = n[1:]
            if len(n) == 10 and n not in nums:
                nums.append(n)
                types[n] = ("MOBILE" if f.startswith("mobile")
                            else "LANDLINE" if f.startswith("landline")
                            else (rec.get("primary_phone_type") or "").upper() or "UNKNOWN")
        emails = [rec.get(f"email_{i}") for i in range(1, 6) if rec.get(f"email_{i}")]
        if nums or emails:
            out[key] = {"numbers": nums, "types": types, "emails": emails}
    return out, {"attempted": len(targets), "filled": len(out),
                 "cost": round(0.02 * len(targets), 2)}


# ── Stage E: Trestle scoring, DM numbers only ─────────────────────────

def trestle_score(numbers, api_key, litigator=False):
    """Score each unique DM number. Returns {number: {...}} and a cost line."""
    out = {}
    if not api_key:
        return out, {"scored": 0, "reason": "no api key"}
    for n in numbers:
        params = {"phone": n}
        if litigator:
            params["add_ons"] = "litigator_checks"
        url = TRESTLE_ENDPOINT + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"x-api-key": api_key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read() or b"{}")
        except Exception as e:
            out[n] = {"error": str(e)[:120]}
            continue
        score = d.get("activity_score")
        out[n] = {
            "activity_score": score, "tier": tier_for(score),
            "line_type": d.get("line_type"), "is_valid": d.get("is_valid"),
            "carrier": (d.get("carrier") or ""),
            "litigator": ((d.get("litigator_checks") or {}).get("is_litigator")
                          if litigator else None),
        }
        time.sleep(0.12)
    return out, {"scored": len(numbers), "cost": round(0.015 * len(numbers), 2)}


# ── Report: how many people do we actually have to reach ──────────────

def build_report(recs, trestle, outdir, costs):
    """The contacts-per-record numbers that size a mail and dial budget."""
    outdir.mkdir(parents=True, exist_ok=True)

    per_record, all_dm_rows = [], []
    for rec in recs:
        dms = rec.get("dms") or []
        dm_nums, mailable = set(), set()
        for d in dms:
            for p in (d.get("phones") or []):
                dm_nums.add(p["number"])
            if d.get("mailing_street"):
                mailable.add((norm_addr(d["mailing_street"]), d.get("mailing_zip") or ""))
            all_dm_rows.append({
                "input_name": rec.get("input_name"),
                "property_address": rec.get("property_address"),
                "dm_name": d.get("name"), "relationship": d.get("canon_rel"),
                "type_raw": d.get("type_raw"), "age": d.get("age"),
                "dm_score": d.get("dm_score"), "why": d.get("dm_why"),
                "mailing": " ".join(x for x in [d.get("mailing_street"), d.get("mailing_city"),
                                                d.get("mailing_state"), d.get("mailing_zip")] if x),
                "phones": ", ".join(p["number"] for p in (d.get("phones") or [])),
                "best_tier": _best_tier(d, trestle),
            })
        per_record.append({
            "input_name": rec.get("input_name"),
            "property_address": rec.get("property_address"),
            "hit": rec.get("has_results"),
            "relatives_returned": len(rec.get("relatives") or []),
            "dms_kept": len(dms),
            "signer_basis": rec.get("signer_basis"),
            "channels": len(rec.get("channels") or []),
            "dm_phone_numbers": len(dm_nums),
            "mailable_addresses": len(mailable),
            "subject_phones": len(rec.get("subject_phones") or []),
        })

    hits = [p for p in per_record if p["hit"]]
    n = max(len(per_record), 1)
    tot_rel = sum(p["relatives_returned"] for p in per_record)
    tot_dm = sum(p["dms_kept"] for p in per_record)
    tot_num = sum(p["dm_phone_numbers"] for p in per_record)
    tot_mail = sum(p["mailable_addresses"] for p in per_record)

    tiers = Counter(r["best_tier"] for r in all_dm_rows)
    summary = {
        "records_submitted": len(per_record),
        "records_with_a_hit": len(hits),
        "hit_rate_pct": round(100 * len(hits) / n, 1),
        "relatives_returned_total": tot_rel,
        "relatives_per_record": round(tot_rel / n, 1),
        "decision_makers_kept_total": tot_dm,
        "decision_makers_per_record": round(tot_dm / n, 1),
        "dm_phone_numbers_total": tot_num,
        "dm_phone_numbers_per_record": round(tot_num / n, 1),
        "mailable_dm_addresses_total": tot_mail,
        "mailable_dm_addresses_per_record": round(tot_mail / n, 1),
        "filtering_kept_pct_of_relatives": round(100 * tot_dm / max(tot_rel, 1), 1),
        "dial_tier_mix": dict(tiers),
        "costs": costs,
    }

    with (outdir / "dm_dial_sheet.csv").open("w", newline="", encoding="utf-8") as fh:
        cols = ["input_name", "property_address", "dm_name", "relationship", "type_raw",
                "age", "dm_score", "best_tier", "phones", "mailing", "why"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(all_dm_rows, key=lambda r: (r["input_name"], -(r["dm_score"] or 0))))

    with (outdir / "per_record.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(per_record[0].keys()) if per_record else ["input_name"])
        w.writeheader()
        w.writerows(per_record)

    (outdir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def _best_tier(dm, trestle):
    best, rank = "Unscored", 99
    order = {name: i for i, (name, _, _) in enumerate(TIERS)}
    for p in (dm.get("phones") or []):
        t = (trestle.get(p["number"]) or {}).get("tier")
        if t and order.get(t, 99) < rank:
            rank, best = order[t], t
    return best


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smartskip", required=True, help="SmartSkip export CSV (vertical or horizontal)")
    ap.add_argument("--outdir", default="output/dp")
    ap.add_argument("--max-dms", type=int, default=4,
                    help="cap on FALLBACK generic relatives when no family is labelled")
    ap.add_argument("--min-score", type=float, default=45.0,
                    help="minimum DM score to keep (default 45)")
    ap.add_argument("--rank-only", action="store_true", help="stage B only, spend nothing")
    ap.add_argument("--tracerfy", action="store_true", help="stage D gap-fill, $0.02 per DM traced")
    ap.add_argument("--trestle", action="store_true", help="stage E scoring, $0.015 per number")
    ap.add_argument("--litigator", action="store_true", help="add Trestle litigator checks")
    args = ap.parse_args()

    from parse_smartskip import parse  # the verified Stage 0 normalizer

    recs = parse(args.smartskip)
    print(f"parsed {len(recs)} records from {args.smartskip}")
    for rec in recs:
        rank_record(rec, args.max_dms, args.min_score)

    tot_rel = sum(len(r.get("relatives") or []) for r in recs)
    tot_dm = sum(len(r.get("dms") or []) for r in recs)
    print(f"  relatives returned : {tot_rel}")
    print(f"  ranked as DMs      : {tot_dm}  ({100*tot_dm/max(tot_rel,1):.0f}% of relatives)")

    costs = {"smartskip_note": "billed at upload time, $0.15 per hit"}
    trestle = {}

    if not args.rank_only:
        all_dms = [d for r in recs for d in (r.get("dms") or [])]
        if args.tracerfy:
            filled, meta = tracerfy_gapfill(all_dms, os.environ.get("TRACERFY_API_KEY", ""))
            costs["tracerfy"] = meta
            print(f"  tracerfy gap-fill  : {meta}")
            for r in recs:
                for d in (r.get("dms") or []):
                    key = f"{d.get('first','')} {d.get('last','')}".strip().lower()
                    if not d.get("phones") and key in filled:
                        f = filled[key]
                        d["phones"] = [{"number": n, "type": f["types"].get(n, "TRACERFY")}
                                       for n in f["numbers"]]
                        d["emails"] = f["emails"]
                        d["source"] = "tracerfy"
        if args.trestle:
            nums = sorted({p["number"] for d in all_dms for p in (d.get("phones") or [])})
            trestle, meta = trestle_score(nums, os.environ.get("TRESTLE_API_KEY", ""),
                                          litigator=args.litigator)
            costs["trestle"] = meta
            print(f"  trestle scoring    : {meta}")

    outdir = Path(args.outdir)
    summary = build_report(recs, trestle, outdir, costs)
    (outdir / "ranked_records.json").write_text(json.dumps(recs, indent=1, default=str),
                                                encoding="utf-8")

    print("\n== how many people you actually have to reach ==")
    for k in ("records_submitted", "records_with_a_hit", "hit_rate_pct",
              "relatives_per_record", "decision_makers_per_record",
              "dm_phone_numbers_per_record", "mailable_dm_addresses_per_record",
              "filtering_kept_pct_of_relatives"):
        print(f"  {k:34} {summary[k]}")
    print(f"  dial tier mix                      {summary['dial_tier_mix']}")
    print(f"\nwrote {outdir}/dm_dial_sheet.csv, per_record.csv, summary.json")


if __name__ == "__main__":
    main()
