"""SOI stage 2d: Enformion resolve for contacts the county-roll join missed.

For each no-match contact: Person Search anchored to the Columbus metro
(name + city/state is accepted; verified live 2026-08-11). Identity is grounded
by EMAIL: a person whose emailAddresses contain the contact's exported email is
the right person, full stop. Name-only candidates are kept but flagged weaker.

The resolved current address is then cross-checked against output/soi/owners.db:
  owns_here      surname appears in the owner-of-record at that address
                 (the roll join missed it: name variation, trust, recent deed)
  other_owner    someone else owns the address (renter, spouse-titled, LLC)
  out_of_metro   current address is outside the six pulled counties

Billing: per MATCH (~$0.10 DataSift rate), misses free. Resumable via
output/soi_enformion_state.json.

Usage:
    python src/soi_enformion.py --limit 10      # spot check
    python src/soi_enformion.py                 # all status=none contacts
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

STATE_PATH = Path("output/soi_enformion_state.json")
DB_PATH = Path("output/soi/owners.db")
METRO_COUNTIES = {"franklin", "delaware", "licking", "fairfield", "pickaway", "union"}

ADDR_NOISE = re.compile(r"[^A-Z0-9 ]")


def addr_key(addr):
    a = ADDR_NOISE.sub(" ", (addr or "").upper()).split()
    if not a:
        return ""
    num = a[0] if a[0].isdigit() else ""
    words = [t for t in a if not t.isdigit()]
    return f"{num} {words[0]}" if num and words else ""


def name_tokens(s):
    return {t for t in re.sub(r"[^A-Z ]", " ", (s or "").upper()).split() if len(t) >= 3}


def current_address(person):
    addrs = person.get("addresses") or []
    cur = [a for a in addrs if a.get("addressOrder") == 1] or addrs[:1]
    return cur[0] if cur else {}


def pick_person(contact, persons):
    """Email-verified first; else exact-name candidate flagged weaker."""
    emails = {e.lower() for e in contact.get("emails", []) if e}
    for p in persons:
        p_emails = {((e.get("emailAddress") if isinstance(e, dict) else e) or "").lower()
                    for e in (p.get("emailAddresses") or [])}
        if emails & p_emails:
            return p, "email_verified"
    from soi_owner_match import variants
    firsts = variants(contact["first_name"])
    ln = contact["last_name"].upper()
    for p in persons:
        nm = p.get("name") or {}
        first_up = (nm.get("firstName") or "").upper()
        if (first_up in firsts or (nm.get("middleName") or "").upper() in firsts) \
                and (nm.get("lastName") or "").upper() == ln:
            addr = current_address(p)
            if (addr.get("state") or "").upper() == "OH":
                return p, "name_metro"
    return None, ""


def ownership_check(con, addr, surnames):
    key = addr_key(addr.get("fullAddress", "").split(";")[0])
    county = (addr.get("county") or "").lower()
    if county not in METRO_COUNTIES:
        return "out_of_metro", ""
    if not key:
        return "no_addr_key", ""
    num = key.split()[0]
    rows = con.execute(
        "SELECT owner1, owner2, situs_addr FROM owners WHERE county=? AND situs_addr LIKE ?",
        (county, f"{num}%")).fetchall()
    for o1, o2, situs in rows:
        if addr_key(situs) == key:
            if name_tokens(f"{o1} {o2}") & surnames:
                return "owns_here", o1
            return "other_owner", o1
    return "addr_not_in_roll", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", default="output/soi_owner_matches.json")
    ap.add_argument("--status", default="none")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="output/soi_enformion_resolved")
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    import enformion_heir as eh
    if not eh.is_configured():
        sys.exit("ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD missing in .env")

    statuses = set(args.status.split(","))
    targets = [r for r in json.load(open(args.matches, encoding="utf-8"))
               if r["status"] in statuses]
    if args.limit:
        targets = targets[:args.limit]

    state = json.load(open(STATE_PATH, encoding="utf-8")) if STATE_PATH.exists() else {}
    con = sqlite3.connect(DB_PATH)
    done = 0
    for rec in targets:
        c = rec["contact"]
        key = c["email"] or f"{c['first_name']}|{c['last_name']}"
        if key in state:
            continue
        data = eh.person_search(c["first_name"], c["last_name"], city="Columbus", state="OH")
        persons = data.get("persons") or []
        person, confidence = pick_person(c, persons)
        if not person:
            state[key] = {"status": "no_person", "n_persons": len(persons)}
        else:
            addr = current_address(person)
            surnames = name_tokens(c["last_name"]) | name_tokens(c.get("middle_name", ""))
            own_status, roll_owner = ownership_check(con, addr, surnames)
            phones = [(p.get("phoneNumber"), p.get("phoneType"))
                      for p in (person.get("phoneNumbers") or [])[:3] if isinstance(p, dict)]
            state[key] = {
                "status": "resolved", "confidence": confidence,
                "full_name": person.get("fullName"),
                "address": (addr.get("fullAddress") or "").replace(";", ","),
                "county": addr.get("county"), "state": addr.get("state"),
                "zip": addr.get("zip"),
                "ownership": own_status, "roll_owner": roll_owner,
                "is_property_owner_flag": person.get("isCurrentPropertyOwner"),
                "phones": phones,
            }
        done += 1
        if done % 10 == 0:
            print(f"{done}/{len(targets)}", flush=True)
            STATE_PATH.write_text(json.dumps(state, indent=0), encoding="utf-8")
        time.sleep(args.delay)
    STATE_PATH.write_text(json.dumps(state, indent=0), encoding="utf-8")

    rows = []
    for rec in targets:
        c = rec["contact"]
        key = c["email"] or f"{c['first_name']}|{c['last_name']}"
        e = state.get(key, {})
        rows.append({
            "first_name": c["first_name"], "last_name": c["last_name"], "email": c["email"],
            "sources": "; ".join(c["sources"]), "resolve_status": e.get("status", "pending"),
            "confidence": e.get("confidence", ""), "enf_name": e.get("full_name", ""),
            "address": e.get("address", ""), "county": e.get("county", ""),
            "addr_state": e.get("state", ""), "zip": e.get("zip", ""),
            "ownership": e.get("ownership", ""), "roll_owner": e.get("roll_owner", ""),
            "enf_owner_flag": e.get("is_property_owner_flag", ""),
            "phone1": (e.get("phones") or [("", "")])[0][0],
        })
    out = Path(f"{args.out}.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    print(Counter(r["resolve_status"] for r in rows))
    print("confidence:", Counter(r["confidence"] for r in rows if r["confidence"]))
    print("ownership:", Counter(r["ownership"] for r in rows if r["ownership"]))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
