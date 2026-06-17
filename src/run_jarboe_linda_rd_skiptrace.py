"""One-off deep skip trace for the 8207 Linda Rd / Jarboe estate deal.

Targets: Krystina Edlin (formerly Brenzel) and Alicia Decker.
Runs Tracerfy instant trace (real API, $0.10/hit) on name+address variants,
then Trestle phone_intel (real API, $0.015/phone + litigator add-on) on every
phone returned plus the two known numbers in play.

Tracerfy returns phones/emails as nested arrays: phones=[{number,type,dnc,
carrier,rank}], emails=[{email,rank}]. We parse those, surface DNC flags
(compliance), then Trestle-score every number for activity tier + litigator.

Prints raw API output. Fabricates nothing — a MISS is reported as a MISS.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests

import config as cfg
from phone_validator import call_trestle, assign_tier, clean_phone, DEFAULT_TIERS

TRACERFY_LOOKUP_URL = "https://tracerfy.com/v1/api/trace/lookup/"

# Each person: ordered (first, last, street, city, state, zip) variants.
# Stop after first HIT per person to avoid double-charging the same individual.
TARGETS = {
    "Krystina Edlin (formerly Brenzel)": [
        ("Krystina", "Edlin", "13900 Bearcamp Rd", "Louisville", "KY", "40272"),
        ("Krystina", "Edlin", "7900 Nottoway Cir", "Louisville", "KY", "40214"),
        ("Krystina", "Brenzel", "7900 Nottoway Cir", "Louisville", "KY", "40214"),
        ("Kristina", "Edlin", "13900 Bearcamp Rd", "Louisville", "KY", "40272"),
    ],
    "Alicia Decker": [
        ("Alicia", "Decker", "8207 Linda Rd", "Louisville", "KY", "40219"),
    ],
}

# Numbers already in play that we want scored regardless of Tracerfy result.
KNOWN_NUMBERS = {
    "5022953593": "prior 'best number' for Krystina (suspected brother John Edlin's line)",
}


def tracerfy_lookup(first, last, street, city, state, zip_code):
    try:
        resp = requests.post(
            TRACERFY_LOOKUP_URL,
            headers={
                "Authorization": f"Bearer {cfg.TRACERFY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "first_name": first, "last_name": last, "address": street,
                "city": city, "state": state, "zip": zip_code, "find_owner": False,
            },
            timeout=45,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:400]}
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def parse_person(person: dict):
    """Return (phones, emails) where phones is list of dicts with metadata."""
    phones = []
    raw_phones = person.get("phones")
    if isinstance(raw_phones, list):
        for p in raw_phones:
            if isinstance(p, dict) and p.get("number"):
                phones.append({
                    "number": clean_phone(p["number"]),
                    "type": p.get("type"),
                    "dnc": p.get("dnc"),
                    "carrier": p.get("carrier"),
                    "rank": p.get("rank"),
                })
    # Fallback: flat fields
    for f in ["primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
              "mobile_5", "landline_1", "landline_2", "landline_3"]:
        v = (person.get(f) or "").strip()
        if v and clean_phone(v) not in [x["number"] for x in phones]:
            phones.append({"number": clean_phone(v), "type": None, "dnc": None,
                           "carrier": None, "rank": None})

    emails = []
    raw_emails = person.get("emails")
    if isinstance(raw_emails, list):
        for e in raw_emails:
            if isinstance(e, dict) and e.get("email"):
                emails.append(e["email"])
            elif isinstance(e, str) and e:
                emails.append(e)
    for f in ["email_1", "email_2", "email_3", "email_4", "email_5"]:
        v = (person.get(f) or "").strip()
        if v and v not in emails:
            emails.append(v)
    return phones, emails


def main():
    print("=" * 80)
    print("DEEP SKIP TRACE -- 8207 Linda Rd / Jarboe estate")
    print("Tracerfy instant trace + Trestle phone_intel (LIVE APIs)")
    print("=" * 80)

    phone_owner = {}   # cleaned number -> person label
    phone_meta = {}    # cleaned number -> {type, dnc, carrier, rank}
    tracerfy_hits = 0

    for person_label, variants in TARGETS.items():
        print(f"\n### {person_label}")
        got_hit = False
        for (first, last, street, city, state, zc) in variants:
            print(f"  -> Tracerfy: {first} {last} @ {street}, {city}, {state} {zc}")
            data = tracerfy_lookup(first, last, street, city, state, zc)
            if data.get("error"):
                print(f"     ERROR: {data['error']} {data.get('detail','')}")
                continue
            if data.get("hit") and data.get("persons"):
                tracerfy_hits += 1
                got_hit = True
                print(f"     HIT -- {len(data['persons'])} record(s)")
                for i, person in enumerate(data["persons"], 1):
                    phones, emails = parse_person(person)
                    print(f"       person[{i}]: {person.get('full_name', person.get('first_name','?'))}"
                          f"  age={person.get('age','?')}  dob={person.get('dob','?')}"
                          f"  deceased={person.get('deceased','?')}"
                          f"  property_owner={person.get('property_owner','?')}"
                          f"  litigator={person.get('litigator','?')}")
                    mail = person.get("mailing_address") or {}
                    if mail:
                        print(f"         mailing: {mail.get('street','?')}, {mail.get('city','?')}, "
                              f"{mail.get('state','?')} {mail.get('zip','?')}")
                    addrs = person.get("addresses")
                    if isinstance(addrs, list) and addrs:
                        print(f"         addresses ({len(addrs)}):")
                        for a in addrs[:6]:
                            if isinstance(a, dict):
                                print(f"           - {a.get('street','?')}, {a.get('city','?')} "
                                      f"{a.get('state','')} {a.get('zip','')}")
                    rels = person.get("relatives") or person.get("associates")
                    if isinstance(rels, list) and rels:
                        names = [r.get("name") if isinstance(r, dict) else str(r) for r in rels]
                        print(f"         relatives/associates: {names[:12]}")
                    print(f"         PHONES ({len(phones)}):")
                    for ph in phones:
                        print(f"           {ph['number']}  type={ph['type']}  dnc={ph['dnc']}  "
                              f"rank={ph['rank']}  carrier={ph['carrier']}")
                        phone_owner.setdefault(ph["number"], person_label)
                        phone_meta[ph["number"]] = ph
                    print(f"         EMAILS ({len(emails)}): {emails}")
                break
            else:
                print("     MISS (no match)")
        if not got_hit:
            print(f"  (no Tracerfy hit for {person_label} on any variant)")

    for num, label in KNOWN_NUMBERS.items():
        phone_owner.setdefault(num, label)

    print("\n" + "=" * 80)
    print(f"Tracerfy hits: {tracerfy_hits}  |  unique phones to Trestle-score: {len(phone_owner)}")
    print("=" * 80)

    print("\n### Trestle phone_intel (activity score + line type + litigator)")
    scored = []
    for cleaned, owner in phone_owner.items():
        data = call_trestle(cleaned, cfg.TRESTLE_API_KEY, add_litigator=True)
        if data.get("error") and not data.get("is_valid"):
            print(f"  {cleaned}: ERROR {data.get('error')} {data.get('detail','')}")
            scored.append({"phone": cleaned, "owner": owner, "score": None, "tier": "ERROR",
                           "line_type": None, "carrier": None, "is_valid": None,
                           "is_prepaid": None, "lit": None})
            continue
        score = data.get("activity_score")
        addons = data.get("add_ons") or {}
        lit = (addons.get("litigator_checks") or {}).get("phone.is_litigator_risk")
        scored.append({
            "phone": cleaned, "owner": owner, "score": score,
            "tier": assign_tier(score, DEFAULT_TIERS),
            "line_type": data.get("line_type"), "carrier": data.get("carrier"),
            "is_valid": data.get("is_valid"), "is_prepaid": data.get("is_prepaid"),
            "lit": lit,
        })

    scored.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    print(f"\n{'PHONE':<12}{'SCORE':>6} {'TIER':<12}{'LINE':<10}{'VALID':<6}{'DNC':<5}{'LIT':<5}OWNER")
    print("-" * 80)
    for r in scored:
        dnc = phone_meta.get(r["phone"], {}).get("dnc")
        print(f"{r['phone']:<12}{str(r['score']):>6} {r['tier']:<12}"
              f"{str(r['line_type']):<10}{str(r['is_valid']):<6}{str(dnc):<5}{str(r['lit']):<5}{r['owner']}")
        print(f"            carrier={r['carrier']}  prepaid={r['is_prepaid']}")

    print("\n" + "=" * 80)
    print(f"Est. cost: Tracerfy {tracerfy_hits} x $0.10 = ${tracerfy_hits*0.10:.2f}  |  "
          f"Trestle {len(scored)} x ~$0.015 = ${len(scored)*0.015:.2f}")
    print("=" * 80)


if __name__ == "__main__":
    main()
