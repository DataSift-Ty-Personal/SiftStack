"""Full end-to-end deep-prospecting workflow -- 2106 Brice St / Willis estate.

Resolves, for EACH of the 5 confirmed heirs:
  - current residential address (Enformion Person Search, most-recent address)
  - phone numbers (Enformion + Tracerfy, merged + de-duped)
  - emails (Tracerfy)
  - their OWN children (Enformion relativesSummary) -> feeds the family tree
Then Trestle-scores every phone into dial tiers + litigator risk.

Pipeline per heir:  Enformion (find address + phones + kids) -> Tracerfy (enrich
phones/emails at resolved address) -> Trestle (score).

GROUNDING RULE (obituary-hallucination guard): the family tree is built ONLY from
firstName/lastName/dob/relativeLevel that the APIs actually return. Nothing is
inferred or fabricated. A MISS is reported as a MISS.

Outputs:
  output/brice_workflow.json   -- structured result for every heir
  console                      -- per-heir cards + family tree + summaries

Cost: 5 Enformion searches + up to 5 Tracerfy hits + Trestle per phone.
Run from project root:  python src/run_brice_full_workflow.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests

import config as cfg
from phone_validator import call_trestle, assign_tier, clean_phone, DEFAULT_TIERS
from run_brice_st_skiptrace import tracerfy_lookup, parse_person

PERSON_SEARCH_URL = "https://devapi.enformion.com/PersonSearch"
OUT = Path(__file__).resolve().parent.parent / "output"

DECEASED = {"first": "Jewelline", "last": "Willis", "dod_enformion": "2/9/2005",
            "dod_sift_obit": "2026-03-21", "property": "2106 Brice St, Knoxville TN 37917",
            "mailing": "4412 W Sunset Rd, Knoxville TN 37914"}

# 5 living children confirmed by Enformion (relativeLevel 'ab'), with expected birth year.
HEIRS = [
    {"first": "Luther", "last": "Willis", "byear": 1953, "enf_score": 650, "note": "Jr"},
    {"first": "Letitia", "last": "Willis", "byear": 1966, "enf_score": 500, "note": "M"},
    {"first": "Norman", "last": "Willis", "byear": 1961, "enf_score": 450, "note": ""},
    {"first": "Darrell", "last": "Willis", "byear": 1955, "enf_score": 400, "note": "Eugene"},
    {"first": "Kenneth", "last": "Willis", "byear": 1960, "enf_score": 300, "note": ""},
]


def enformion_headers():
    return {
        "galaxy-ap-name": cfg.ENFORMION_AP_NAME,
        "galaxy-ap-password": cfg.ENFORMION_AP_PASSWORD,
        "galaxy-search-type": "Person",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def enformion_person(first, last, dob_year=None):
    """Person Search by Name + DOB year (Enformion's minimum-criteria combo).
    Returns persons[]. Name+city alone is rejected as insufficient criteria."""
    body = {"FirstName": first, "LastName": last, "Page": 1, "ResultsPerPage": 5}
    if dob_year:
        body["Dob"] = str(dob_year)
    # NOTE: Enformion ALWAYS returns an "error" object (inputErrors/warnings),
    # even on success -- so we signal our own failures via "_error" to avoid
    # colliding with that always-present field.
    try:
        r = requests.post(PERSON_SEARCH_URL, headers=enformion_headers(), json=body, timeout=45)
        if r.status_code != 200:
            return {"_error": f"HTTP {r.status_code}", "_detail": r.text[:300]}
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


FAMILY_HINTS = ("Knoxville", "37917", "37914", "Brice", "Sunset")


def _in_family_area(p):
    blob = json.dumps(p.get("addresses") or []).lower()
    return any(h.lower() in blob for h in FAMILY_HINTS)


def pick_match(persons, last, byear):
    """Choose the person whose surname + birth year match, preferring the one whose
    address history is in the Willis family area (disambiguates same-name/same-age).
    Grounded, no guessing."""
    candidates = []
    for p in persons or []:
        nm = p.get("name") or {}
        if (nm.get("lastName") or "").lower() != last.lower():
            continue
        age = p.get("age")
        dob = p.get("dob") or ""
        yr_ok = (str(byear) in dob) or (age and abs((2026 - int(age)) - byear) <= 1)
        if yr_ok:
            candidates.append(p)
    if not candidates:
        return None
    # prefer a candidate anchored in the family area
    for p in candidates:
        if _in_family_area(p):
            return p
    return candidates[0]


def addr_str(a):
    if not isinstance(a, dict):
        return str(a)
    return a.get("fullAddress") or a.get("addressLine1") or json.dumps(a)


def enformion_phones(p):
    out = []
    for ph in (p.get("phoneNumbers") or []):
        num = clean_phone(ph.get("phoneNumber", ""))
        if num:
            out.append({"number": num, "type": ph.get("phoneType"),
                        "last_seen": ph.get("lastReportedDate"), "src": "enformion"})
    return out


def heir_children(p, sibling_lastnames):
    """Return this heir's OWN children: relativeLevel 'ab', younger generation.
    Grounded entirely in returned relativesSummary fields."""
    kids = []
    for r in (p.get("relativesSummary") or []):
        if r.get("relativeLevel") != "ab":
            continue
        dob = r.get("dob") or ""
        # crude generation filter: born 1975+ are likely children, not siblings/parents
        yr = next((int(t) for t in dob.replace("/", " ").split() if t.isdigit() and len(t) == 4), None)
        if yr and yr >= 1975:
            kids.append({
                "name": " ".join(x for x in [r.get("firstName"), r.get("middleName"),
                                             r.get("lastName"), r.get("suffix")] if x),
                "dob": dob, "deceased": r.get("isDeceased"),
            })
    return kids


def main():
    OUT.mkdir(exist_ok=True)
    results = []
    phone_owner = {}   # cleaned -> heir label
    phone_meta = {}    # cleaned -> meta (dnc etc from tracerfy)

    for h in HEIRS:
        label = f"{h['first']} {h['last']} {h['note']}".strip()
        print("\n" + "=" * 84)
        print(f"HEIR: {label}  (expected b.{h['byear']}, Enformion score {h['enf_score']})")
        print("=" * 84)

        rec = {"heir": label, "byear": h["byear"], "enf_score": h["enf_score"],
               "address": None, "all_addresses": [], "phones": [], "emails": [],
               "children": [], "age": None, "dob": None, "property_owner": None,
               "litigator": None}

        # --- Step 1: Enformion (Name + DOB year) -> address + phones + kids
        data = enformion_person(h["first"], h["last"], dob_year=h["byear"])
        if data.get("_error"):
            print(f"  Enformion ERROR: {data['_error']} {data.get('_detail','')}")
            p = None
        else:
            p = pick_match(data.get("persons"), h["last"], h["byear"])

        if p:
            with open(OUT / f"enformion_{h['first']}_{h['last']}.json", "w", encoding="utf-8") as f:
                json.dump(p, f, indent=2)
            rec["age"] = p.get("age")
            rec["dob"] = p.get("dob")
            addrs = [addr_str(a) for a in (p.get("addresses") or []) if addr_str(a)]
            rec["all_addresses"] = addrs[:5]
            rec["address"] = addrs[0] if addrs else None
            for ph in enformion_phones(p):
                if ph["number"] not in [x["number"] for x in rec["phones"]]:
                    rec["phones"].append(ph)
            rec["children"] = heir_children(p, {"Willis"})
            print(f"  Enformion match: {p.get('fullName')}  age={p.get('age')}  dob={p.get('dob')}")
            print(f"    CURRENT ADDRESS: {rec['address']}")
            for a in addrs[1:4]:
                print(f"    prior address:  {a}")
            print(f"    Enformion phones: {[x['number'] for x in rec['phones']]}")
            print(f"    own children (relativeLevel ab, b.1975+): "
                  f"{[c['name'] + ' ' + c['dob'] for c in rec['children']]}")
        else:
            print("  Enformion: NO confident match (surname+birthyear).")

        # --- Step 2: Tracerfy at resolved address -> enrich phones/emails -
        anchor = None
        if rec["address"]:
            # crude split of "Street; City, ST ZIP" or "Street, City, ST ZIP"
            anchor = rec["address"]
        tf_targets = []
        if rec["address"]:
            tf_targets.append(_split_addr(rec["address"]))
        # always also try the family property + mailing as fallback anchors
        tf_targets += [("2106 Brice St", "Knoxville", "TN", "37917"),
                       ("4412 W Sunset Rd", "Knoxville", "TN", "37914")]
        for (street, city, state, zc) in tf_targets:
            if not street:
                continue
            d = tracerfy_lookup(h["first"], h["last"], street, city, state, zc)
            if d.get("error"):
                print(f"  Tracerfy ERROR @ {street}: {d['error']}")
                continue
            if d.get("hit") and d.get("persons"):
                print(f"  Tracerfy HIT @ {street}, {city}")
                for person in d["persons"]:
                    phones, emails = parse_person(person)
                    for ph in phones:
                        if ph["number"] not in [x["number"] for x in rec["phones"]]:
                            rec["phones"].append({"number": ph["number"], "type": ph["type"],
                                                  "src": "tracerfy"})
                        phone_meta[ph["number"]] = ph
                    for e in emails:
                        if e not in rec["emails"]:
                            rec["emails"].append(e)
                    if person.get("property_owner"):
                        rec["property_owner"] = True
                    if person.get("litigator"):
                        rec["litigator"] = True
                    mail = person.get("mailing_address") or {}
                    if mail and not rec["address"]:
                        rec["address"] = f"{mail.get('street')}, {mail.get('city')}, {mail.get('state')} {mail.get('zip')}"
                break
            print(f"  Tracerfy MISS @ {street}, {city}")

        print(f"  EMAILS: {rec['emails']}")
        for ph in rec["phones"]:
            phone_owner.setdefault(ph["number"], label)
        results.append(rec)

    # --- Step 3: Trestle score every collected phone ---------------------
    print("\n" + "=" * 84)
    print(f"Trestle scoring {len(phone_owner)} unique phones")
    print("=" * 84)
    phone_score = {}
    for cleaned, owner in phone_owner.items():
        d = call_trestle(cleaned, cfg.TRESTLE_API_KEY, add_litigator=True)
        score = d.get("activity_score")
        addons = d.get("add_ons") or {}
        lit = (addons.get("litigator_checks") or {}).get("phone.is_litigator_risk")
        phone_score[cleaned] = {
            "score": score, "tier": assign_tier(score, DEFAULT_TIERS),
            "line_type": d.get("line_type"), "is_valid": d.get("is_valid"),
            "dnc": (phone_meta.get(cleaned) or {}).get("dnc"), "lit": lit,
        }

    # attach scores to each heir's phones
    for rec in results:
        for ph in rec["phones"]:
            ph.update(phone_score.get(ph["number"], {}))
        rec["phones"].sort(key=lambda x: (x.get("score") is None, -(x.get("score") or 0)))

    with open(OUT / "brice_workflow.json", "w", encoding="utf-8") as f:
        json.dump({"deceased": DECEASED, "heirs": results}, f, indent=2)

    # --- Final: per-heir cards ------------------------------------------
    print("\n" + "#" * 84)
    print("# PER-HEIR CARDS")
    print("#" * 84)
    for rec in results:
        print(f"\n--- {rec['heir']}  (age {rec['age']}, dob {rec['dob']}) ---")
        print(f"    Address: {rec['address'] or 'NOT RESOLVED'}")
        print(f"    Property owner flag: {rec['property_owner']}   Litigator: {rec['litigator']}")
        if rec["children"]:
            print(f"    Children: {[c['name'] for c in rec['children']]}")
        print(f"    Emails: {rec['emails']}")
        print("    Phones (best first):")
        for ph in rec["phones"]:
            print(f"      {ph['number']}  score={ph.get('score')}  {ph.get('tier')}  "
                  f"{ph.get('line_type')}  dnc={ph.get('dnc')}  lit={ph.get('lit')}  [{ph['src']}]")

    print(f"\nStructured output -> {OUT / 'brice_workflow.json'}")


def _split_addr(s):
    """Best-effort split of an Enformion fullAddress into (street, city, state, zip)."""
    s = s.replace(";", ",")
    parts = [p.strip() for p in s.split(",")]
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 1 else "Knoxville"
    st, zc = "TN", ""
    if len(parts) > 2:
        tail = parts[2].split()
        if tail:
            st = tail[0]
            if len(tail) > 1:
                zc = tail[1].split("-")[0]
    return (street, city, st, zc)


if __name__ == "__main__":
    main()
