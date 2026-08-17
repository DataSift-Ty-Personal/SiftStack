#!/usr/bin/env python3
"""Deep prospecting — API-first heir waterfall for ONE deceased-owner record.

Self-contained reference implementation of the Primary Path (Steps A-E) in this
skill. Depends ONLY on `requests` + the Python standard library, so the whole REI
community can run it with their own API keys — no platform/repo required.

  A. Person Search the DECEASED            -> relatives graph + date of death
  B. Derive REQUIRED SIGNERS               -> living closest-kin children
  C. Person Search each SIGNER (name+DOB)  -> address + phones
  D. DEDUPE phones across signers          -> one unique set
  E. (optional) Trestle-score each phone   -> dial tiers + litigator risk

Heirs come straight from the provider's relationship graph — nothing is inferred
or fabricated. A MISS is printed as a MISS. Billing is per match (misses free);
signer-gating (B) and phone-dedupe (D) keep the cost down.

USAGE
-----
  export ENFORMION_AP_NAME=...        # Enformion Console -> API access profile
  export ENFORMION_AP_PASSWORD=...
  export TRESTLE_API_KEY=...          # optional; enables phone scoring (Step E)

  python enformion_person_search.py --first Jane --last Doe \
      --street "123 Oak St" --city Knoxville --state TN --zip 37918

  # Optional: a second known address (e.g. tax mailing) and the obituary DOD so
  # the script can flag a death-index vs obituary conflict:
  python enformion_person_search.py --first Jane --last Doe \
      --street "123 Oak St" --city Knoxville --state TN --zip 37918 \
      --mail-street "55 Pine Rd" --mail-zip 37914 --obit-dod 2026-03-21
"""

import argparse
import os
import re
import sys

import requests

PERSON_SEARCH_URL = "https://devapi.enformion.com/PersonSearch"
TRESTLE_URL = "https://api.trestleiq.com/3.0/phone_intel"
CLOSEST_KIN_LEVEL = "ab"
TIERS = [(81, 100, "Dial First"), (61, 80, "Dial Second"),
         (41, 60, "Dial Third"), (21, 40, "Dial Fourth"), (0, 20, "Drop")]
BAR = "=" * 84


# ── Enformion ──────────────────────────────────────────────────────────

def _enf_headers():
    name = os.environ.get("ENFORMION_AP_NAME")
    pw = os.environ.get("ENFORMION_AP_PASSWORD")
    if not name or not pw:
        sys.exit("Set ENFORMION_AP_NAME and ENFORMION_AP_PASSWORD in your environment.")
    return {
        "galaxy-ap-name": name,
        "galaxy-ap-password": pw,
        "galaxy-search-type": "Person",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def person_search(first, last, *, city="", state="", zip_code="", dob_year=""):
    """One Person Search POST. Returns parsed JSON, or {} on failure.

    Failure is detected by HTTP status — NOT the always-present `error` object.
    """
    body = {"FirstName": first, "LastName": last, "Page": 1, "ResultsPerPage": 5}
    addr2 = " ".join(p for p in [f"{city}," if city else "", state, zip_code] if p).strip()
    if addr2:
        body["Addresses"] = [{"AddressLine2": addr2}]
    if dob_year:
        body["Dob"] = str(dob_year)
    try:
        r = requests.post(PERSON_SEARCH_URL, headers=_enf_headers(), json=body, timeout=45)
    except requests.RequestException as e:
        print(f"     (request error: {e})")
        return {}
    if r.status_code != 200:
        print(f"     (HTTP {r.status_code}: {r.text[:160]})")
        return {}
    return r.json()


def first_match(data):
    persons = data.get("persons") or data.get("people") or data.get("results") or []
    return persons[0] if persons else None


# ── Schema helpers ─────────────────────────────────────────────────────

def full_name(rel):
    parts = [rel.get("firstName", ""), rel.get("middleName", ""), rel.get("lastName", "")]
    name = " ".join(p.strip() for p in parts if p and p.strip())
    if name:
        return name
    raw = rel.get("rawNames") or rel.get("name")
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        return (raw.get("fullName") or "").strip()
    return (raw or "").strip()


def year_of(value):
    """Recover a 4-digit year from a possibly-masked/dict date (e.g. '9/XX/1955')."""
    if isinstance(value, dict):
        value = value.get("year") or value.get("dob") or value.get("dod") or ""
    m = re.search(r"(19|20)\d{2}", str(value))
    return m.group(0) if m else ""


def extract_dod(person):
    """Date of death as YYYY-MM-DD (best effort, year-floor for masked), or ''."""
    candidates = []
    if person.get("dod"):
        candidates.append(person["dod"])
    for d in person.get("datesOfDeath") or []:
        candidates.append(d.get("dod") if isinstance(d, dict) else d)
    for raw in candidates:
        if isinstance(raw, dict):
            raw = raw.get("dod") or raw.get("year") or ""
        raw = str(raw).strip()
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m:
            return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            return m.group(0)
        y = year_of(raw)
        if y:
            return f"{y}-01-01"
    return ""


def is_deceased(rel):
    v = rel.get("isDeceased")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1")


def relatives(person):
    """relativesSummary -> [{name, type, level, dob, score, deceased, lastname}], closest first."""
    out = []
    for rel in person.get("relativesSummary") or person.get("relatives") or []:
        if not isinstance(rel, dict):
            continue
        name = full_name(rel)
        if not name:
            continue
        try:
            score = int(rel.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        out.append({
            "name": name,
            "type": (rel.get("relativeType") or rel.get("relationship") or "").strip(),
            "level": (rel.get("relativeLevel") or "").strip().lower(),
            "dob": year_of(rel.get("dob") or rel.get("dateOfBirth") or ""),
            "score": score,
            "deceased": is_deceased(rel),
            "lastname": (rel.get("lastName") or "").strip(),
        })
    out.sort(key=lambda r: (r["level"] or "zz", -r["score"]))
    return out


def surname_matches(name, lastname, surname):
    """Whole-last-name-token match (NOT a substring: 'Maxwell' != surname 'Well')."""
    sur = surname.lower()
    if lastname and lastname.lower() == sur:
        return True
    tokens = name.lower().split()
    return bool(tokens) and tokens[-1] == sur


def required_signers(rels, surname):
    """Living closest-kin children = the cost gate for Step C.

    Prefer relativeType (Son/Daughter/Child). If blank, fall back to a whole-token
    surname match — but flag it UNVERIFIED rather than asserting 'child'.
    """
    sur = surname.lower()
    out = []
    for r in rels:
        if r["deceased"] or r["level"] != CLOSEST_KIN_LEVEL or not r["dob"]:
            continue
        t = r["type"].lower()
        is_child = t in ("son", "daughter", "child")
        if is_child:
            out.append({**r, "verified": True})
        elif not t and surname_matches(r["name"], r["lastname"], sur):
            out.append({**r, "verified": False})  # surname-only guess -> verify
    return out


def enf_phones(person):
    out = []
    for p in person.get("phoneNumbers") or []:
        if isinstance(p, dict):
            num = re.sub(r"\D", "", str(p.get("phoneNumber") or p.get("number") or ""))
            if len(num) == 11 and num.startswith("1"):
                num = num[1:]
            if len(num) == 10:
                out.append(num)
    return out


def enf_addresses(person):
    out = []
    for a in person.get("addresses") or []:
        if isinstance(a, dict):
            full = a.get("fullAddress") or a.get("AddressLine2") or ""
            if full:
                out.append(full)
    return out


# ── Trestle (optional) ─────────────────────────────────────────────────

def trestle_score(phone):
    key = os.environ.get("TRESTLE_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(
            TRESTLE_URL, params={"phone": phone, "add_ons": "litigator_checks"},
            headers={"x-api-key": key, "Accept": "application/json"}, timeout=15,
        )
        if r.status_code != 200:
            return {"score": None, "tier": f"HTTP {r.status_code}", "line": None, "lit": None}
        d = r.json()
        score = d.get("activity_score")
        lit = (d.get("add_ons") or {}).get("litigator_checks", {}).get("phone.is_litigator_risk")
        tier = "Unknown"
        if isinstance(score, int):
            tier = next((t for lo, hi, t in TIERS if lo <= score <= hi), "Unknown")
        return {"score": score, "tier": tier, "line": d.get("line_type"), "lit": lit}
    except requests.RequestException:
        return {"score": None, "tier": "ERROR", "line": None, "lit": None}


# ── Waterfall ──────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Deep prospecting heir waterfall (one record)")
    ap.add_argument("--first", required=True)
    ap.add_argument("--last", required=True)
    ap.add_argument("--street", required=True)
    ap.add_argument("--city", default="")
    ap.add_argument("--state", default="TN")
    ap.add_argument("--zip", dest="zip_code", default="")
    ap.add_argument("--mail-street", default="")
    ap.add_argument("--mail-city", default="")
    ap.add_argument("--mail-zip", default="")
    ap.add_argument("--obit-dod", default="", help="Obituary DOD (YYYY-MM-DD) to flag conflicts")
    ap.add_argument("--max-signers", type=int, default=8)
    args = ap.parse_args()
    surname = args.last

    print(BAR)
    print(f"DEEP PROSPECTING -- {args.first} {args.last} | {args.street}, "
          f"{args.city}, {args.state} {args.zip_code}")
    print(BAR)

    # Step A — decedent
    print("\n### Step A — Person Search the decedent (heir graph + DOD)")
    decedent = first_match(person_search(
        args.first, args.last, city=args.city, state=args.state, zip_code=args.zip_code))
    if not decedent:
        print("  MISS — no match. Try the mailing address or check the name spelling.")
        return
    dod = extract_dod(decedent)
    rels = relatives(decedent)
    print(f"  MATCH — DOD={dod or 'unknown'}  relatives={len(rels)}")
    for r in rels:
        flag = "DECEASED" if r["deceased"] else "living"
        print(f"    - {r['name']:<26} {r['type'] or '(unlabeled)':<14} "
              f"level={r['level'] or '?':<3} dob={r['dob'] or '?':<6} "
              f"score={r['score']:<4} {flag}")
    if args.obit_dod and dod and dod[:4] != args.obit_dod[:4]:
        print(f"  ** DOD CONFLICT: death index {dod} vs obituary {args.obit_dod} — "
              f"possible second household death. Surface, do not resolve. **")

    # Step B — required signers
    signers = required_signers(rels, surname)
    print(f"\n### Step B — Required signers (living closest-kin children): {len(signers)}")
    if not signers:
        print("  No confirmed living child signers. Review per stirpes / spouse / L4 escalation:")
        for r in rels:
            if r["deceased"] and r["level"] == CLOSEST_KIN_LEVEL:
                print(f"    - {r['name']} deceased -> check their children (per stirpes)")
    for s in signers[:args.max_signers]:
        tag = "" if s["verified"] else "  [UNVERIFIED relationship — confirm before treating as signer]"
        print(f"    * {s['name']} (dob {s['dob']}){tag}")

    # Step C — resolve each signer
    print(f"\n### Step C — Resolve each signer (Person Search name+DOB, cap {args.max_signers})")
    phone_owner = {}        # number -> signer name
    cards = []
    for s in signers[:args.max_signers]:
        parts = s["name"].split()
        print(f"\n  -> {s['name']} (dob {s['dob']})")
        sp = first_match(person_search(parts[0], parts[-1], dob_year=s["dob"]))
        card = {"name": s["name"], "dob": s["dob"], "address": "", "phones": []}
        if sp:
            addrs = enf_addresses(sp)
            if addrs:
                card["address"] = addrs[0]
                print(f"     address: {addrs[0]}")
            for num in enf_phones(sp):
                phone_owner.setdefault(num, s["name"])
                card["phones"].append(num)
            print(f"     phones: {len(card['phones'])}")
        else:
            print("     MISS (no name+DOB match)")
        cards.append(card)

    # Step D — dedupe
    unique = list(phone_owner.keys())
    print(f"\n### Step D — Dedupe phones: {sum(len(c['phones']) for c in cards)} -> {len(unique)} unique")

    # Step E — Trestle scoring (optional) + master dial sheet
    print("\n" + BAR)
    print(f"MASTER DIAL SHEET -- {args.street} -- {surname} estate (deduped, best first)")
    print(BAR)
    scored = []
    for num in unique:
        s = trestle_score(num)
        scored.append((num, s))
    # Sort by score desc when available
    scored.sort(key=lambda x: (x[1] is None, -(x[1]["score"] if x[1] and isinstance(x[1].get("score"), int) else -1)))
    if any(s for _, s in scored):
        print(f"{'PHONE':<12}{'SCORE':>6} {'TIER':<13}{'LINE':<10}{'LIT':<5}REACHES")
        print("-" * 84)
        for num, s in scored:
            if s:
                print(f"{num:<12}{str(s['score']):>6} {s['tier']:<13}"
                      f"{str(s['line']):<10}{str(s['lit']):<5}{phone_owner.get(num, '?')}")
            else:
                print(f"{num:<12}{'--':>6} {'(no TRESTLE_API_KEY)':<28}{phone_owner.get(num, '?')}")
    else:
        for num in unique:
            print(f"  {num}  -> {phone_owner.get(num, '?')}   (set TRESTLE_API_KEY to score)")

    print("\n### Signer contact cards")
    for c in cards:
        print(f"  {c['name']} (dob {c['dob']}) — {c['address'] or 'address not found'} "
              f"— {len(c['phones'])} phone(s)")

    n_calls = 1 + len(signers[:args.max_signers])
    print("\n" + BAR)
    print(f"Est. Enformion cost: {n_calls} searches (~${n_calls * 0.35:.2f}). Billed per match.")
    print("Grounding: every heir/phone above came from an API response. Verify the "
          "signing parties via title/probate before treating them as legal fact.")
    print(BAR)


if __name__ == "__main__":
    main()
