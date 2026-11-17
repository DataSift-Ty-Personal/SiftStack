"""Pull live Knox County tax-delinquency data for the properties in a Sift export.

Reuses the Knox County tax API workflow from tax_enricher.py:
  - Parcel lookup:  GET /api/v2/due/PPT/{parcel}?detail_level=public
  - Address search: GET /api/v2/parcels/{address}?detail_level=public
A bill counts as delinquent when delinquent=True AND paid=False AND due>0.

Usage:
  python src/export_tax_delinquency.py "Tax Enrich Test.csv"
"""

import csv
import re
import sys
import time
from urllib.parse import quote

import requests

BASE = "https://knox-tn.mygovonline.com/api/v2"
TIMEOUT = 15
DELAY = 0.5  # polite pause between lookups


def parcel_candidates(raw: str) -> list[str]:
    """Generate Knox tax-API parcel-id formats to try, best first."""
    raw = (raw or "").strip().upper()
    if not raw:
        return []
    cands: list[str] = []
    if "-" in raw:
        cands.append(raw)
    # Map+parcel format with internal spaces -> single dash (e.g. '051  043' -> '051-043')
    if re.search(r"\s", raw):
        cands.append(re.sub(r"\s+", "-", raw))
    # Alnum format -> dash before trailing digits (e.g. '123EE030' -> '123EE-030')
    m = re.match(r"^(\d{3}[A-Z]{0,2})(\d{2,5})$", raw)
    if m:
        cands.append(f"{m.group(1)}-{m.group(2)}")
    # Trailing unit letter (condos: '058PA01600E' -> '058PA-01600E' / with space)
    m2 = re.match(r"^(\d{3}[A-Z]{0,2})(\d{2,5})([A-Z])$", raw)
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


def fetch_ppt(parcel: str) -> dict | None:
    url = f"{BASE}/due/PPT/{quote(parcel)}?detail_level=public"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json().get("due", {}).get("PPT", {})
    except (requests.RequestException, ValueError):
        return None


def parse_delinquency(ppt: dict) -> tuple[float, list, int]:
    """Return (delinquent_amount, [delinquent_tax_years], total_bill_count)."""
    bills = ppt.get("bills", []) if ppt else []
    amount = 0.0
    years: list = []
    for b in bills:
        if b.get("delinquent") and not b.get("paid", True) and (b.get("due") or 0) > 0:
            amount += b.get("due", 0)
            years.append(b.get("tax_year") or b.get("taxYear"))
    years = sorted({str(y) for y in years if y})
    return round(amount, 2), years, len(bills)


def lookup_account_by_address(addr: str) -> str | None:
    if not addr.strip():
        return None
    url = f"{BASE}/parcels/{quote(addr)}?detail_level=public&start=0&length=5"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        parcels = r.json().get("parcels", [])
        return parcels[0].get("account_number") if parcels else None
    except (requests.RequestException, ValueError):
        return None


def owner_from_ppt(ppt: dict) -> str:
    bills = ppt.get("bills", []) if ppt else []
    return bills[0].get("owner", "").strip() if bills else ""


def run(csv_path: str) -> None:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    results = []
    for i, row in enumerate(rows, 1):
        addr = (row.get("Property address") or "").strip()
        if not addr:
            continue
        city = (row.get("Property city") or "").strip()
        state = (row.get("Property state") or "").strip()
        zipc = (row.get("Property zip") or "").strip()
        county = (row.get("Property county") or "").strip()
        owner = " ".join(x for x in [(row.get("First Name") or "").strip(),
                                     (row.get("Last Name") or "").strip()] if x)
        raw_parcel = (row.get("Parcel id") or row.get("Apn") or "").strip()
        est_val = (row.get("Estimated value") or "").strip()

        ppt = None
        used_parcel = ""
        for cand in parcel_candidates(raw_parcel):
            ppt_try = fetch_ppt(cand)
            time.sleep(DELAY)
            if ppt_try and ppt_try.get("bills"):
                ppt, used_parcel = ppt_try, cand
                break

        # Fallback: search by address to get the canonical account number
        if not ppt:
            acct = lookup_account_by_address(addr)
            time.sleep(DELAY)
            if acct:
                ppt_try = fetch_ppt(acct)
                time.sleep(DELAY)
                if ppt_try and ppt_try.get("bills"):
                    ppt, used_parcel = ppt_try, acct

        amount, years, n_bills = parse_delinquency(ppt or {})
        tax_owner = owner_from_ppt(ppt or {})

        results.append({
            "address": addr, "city": city, "state": state, "zip": zipc,
            "county": county, "owner": owner, "tax_api_owner": tax_owner,
            "parcel_used": used_parcel, "raw_parcel": raw_parcel,
            "est_value": est_val, "delinquent_amount": amount,
            "delinquent_years_count": len(years),
            "delinquent_years": ", ".join(years), "found": bool(ppt),
        })
        flag = "" if ppt else "  [NO API MATCH]"
        print(f"[{i}/{len(rows)}] {addr:<32} parcel={used_parcel or '-':<14} "
              f"${amount:>10,.2f}  {len(years)} yr(s){flag}", flush=True)

    # Write results CSV
    out_path = "output/tax_delinquency_results.csv"
    import os
    os.makedirs("output", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    # Summary
    delinq = [r for r in results if r["delinquent_amount"] > 0]
    total = sum(r["delinquent_amount"] for r in results)
    not_found = [r for r in results if not r["found"]]
    print("\n" + "=" * 70)
    print(f"Properties processed:      {len(results)}")
    print(f"With tax delinquency owed: {len(delinq)}")
    print(f"Total delinquent (all):    ${total:,.2f}")
    print(f"No API match:              {len(not_found)}")
    print(f"Results CSV:               {out_path}")
    print("=" * 70)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "Tax Enrich Test.csv"
    run(path)
