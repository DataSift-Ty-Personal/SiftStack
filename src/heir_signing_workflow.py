"""Cost-optimized heir + signing workflow for a deceased-owner property.

Two cost optimizations baked in:
  (1) SIGNER-GATED SEARCH -- the expensive per-person Enformion Person Search
      ($0.35/match) runs ONLY for people who would actually have to sign the deed
      (living children of the decedent = heirs at law). Distant relatives
      (cousins, in-laws, grandchildren) are flagged for manual verification but
      never trigger a paid search. One deceased search surfaces the whole heir
      set; we only "spend" on the signers.
  (2) DEDUPE BEFORE TRESTLE -- every phone across all signers is pooled into ONE
      unique set (shared family landlines collapse to a single number) and capped
      per signer, so Trestle ($0.015/lookup) scores each number exactly once
      instead of re-scoring the same household lines 4-5 times.

Pipeline:  Enformion Person Search (deceased) -> derive signers ->
           Enformion Person Search (each signer only) -> Tracerfy (emails) ->
           dedupe phones -> Trestle score -> report-ready JSON.

GROUNDING RULE: every name/address/phone/relationship comes from an API response.
Nothing inferred. Conflicts surfaced, not resolved.

Run from project root:  python src/heir_signing_workflow.py
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests

import config as cfg
from phone_validator import call_trestle, assign_tier, clean_phone, DEFAULT_TIERS
from run_brice_st_skiptrace import tracerfy_lookup, parse_person

PERSON_SEARCH_URL = "https://devapi.enformion.com/PersonSearch"
OUT = Path(__file__).resolve().parent.parent / "output"

# ---- Tunables -------------------------------------------------------------
MAX_PHONES_PER_SIGNER = 6     # cap each signer's phones before pooling (dedupe lever)
ENABLE_TRACERFY = True        # Tracerfy adds emails Enformion often omits; signer-gated
CHILD_LEVEL = "ab"            # Enformion relativeLevel for closest kin (children)

# ---- Subject: deceased owner + property anchor (with ZIP, required) -------
DECEASED = {
    "first": "Jewelline", "last": "Willis",
    "street": "2106 Brice St", "city": "Knoxville", "state": "TN", "zip": "37917",
    "label": "2106 Brice St, Knoxville TN 37917 (Willis estate)",
}


def headers():
    return {
        "galaxy-ap-name": cfg.ENFORMION_AP_NAME,
        "galaxy-ap-password": cfg.ENFORMION_AP_PASSWORD,
        "galaxy-search-type": "Person",
        "Content-Type": "application/json", "Accept": "application/json",
    }


def person_search(body):
    """POST PersonSearch. Enformion ALWAYS returns an 'error' object even on
    success, so we signal real failures via '_error'."""
    try:
        r = requests.post(PERSON_SEARCH_URL, headers=headers(), json=body, timeout=45)
        if r.status_code != 200:
            return {"_error": f"HTTP {r.status_code}", "_detail": r.text[:300]}
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def search_by_name_addr(first, last, city, state, zip_code):
    return person_search({"FirstName": first, "LastName": last,
                          "Addresses": [{"AddressLine2": f"{city}, {state} {zip_code}"}],
                          "Page": 1, "ResultsPerPage": 5})


def search_by_name_dob(first, last, dob_year):
    return person_search({"FirstName": first, "LastName": last, "Dob": str(dob_year),
                          "Page": 1, "ResultsPerPage": 5})


def year_of(dob):
    m = re.search(r"\b(19|20)\d{2}\b", dob or "")
    return int(m.group()) if m else None


def addr_str(a):
    if isinstance(a, dict):
        return a.get("fullAddress") or a.get("addressLine1") or ""
    return str(a or "")


def family_hints(person, anchor):
    """Tokens (zips, city, street words) used to disambiguate same-name matches."""
    hints = {anchor["city"].lower(), anchor["zip"], anchor["street"].split()[0].lower()}
    for a in (person.get("addresses") or []):
        s = addr_str(a).lower()
        for z in re.findall(r"\b\d{5}\b", s):
            hints.add(z)
    return {h for h in hints if h}


def pick_match(persons, last, byear, hints):
    """Surname + birth-year match, preferring a candidate anchored in the family area."""
    cands = []
    for p in persons or []:
        if ((p.get("name") or {}).get("lastName") or "").lower() != last.lower():
            continue
        age = p.get("age")
        if (str(byear) in (p.get("dob") or "")) or (age and abs((2026 - int(age)) - byear) <= 1):
            cands.append(p)
    for p in cands:
        blob = json.dumps(p.get("addresses") or []).lower()
        if any(h in blob for h in hints):
            return p
    return cands[0] if cands else None


def derive_signers(decedent_person, decedent_last):
    """REQUIRED SIGNERS = living children (relativeLevel 'ab') sharing the decedent's
    surname, with a usable birth year. Returns (signers, flagged_for_review)."""
    signers, flagged = [], []
    for r in (decedent_person.get("relativesSummary") or []):
        name = " ".join(x for x in [r.get("firstName"), r.get("middleName"),
                                    r.get("lastName"), r.get("suffix")] if x)
        last = (r.get("lastName") or "")
        lvl = r.get("relativeLevel")
        byear = year_of(r.get("dob"))
        is_child_surname = (lvl == CHILD_LEVEL and last.lower() == decedent_last.lower())
        if is_child_surname and not r.get("isDeceased") and byear:
            signers.append({"first": r.get("firstName"), "last": last,
                            "name": name, "byear": byear})
        elif is_child_surname and r.get("isDeceased"):
            flagged.append(f"{name} (b.{byear or '?'}) -- DECEASED child: their share passes "
                           f"to THEIR children per stirpes (probate layer, verify)")
        elif lvl == CHILD_LEVEL and last.lower() != decedent_last.lower() and byear:
            flagged.append(f"{name} (b.{byear}) -- close kin, different surname: "
                           f"possible married daughter / heir. VERIFY before relying on shares")
    return signers, flagged


def signer_phones(person, source):
    out = []
    for ph in (person.get("phoneNumbers") or []):
        num = clean_phone(ph.get("phoneNumber", ""))
        if num and num not in [x["number"] for x in out]:
            out.append({"number": num, "type": ph.get("phoneType"),
                        "last_seen": ph.get("lastReportedDate"), "src": source, "dnc": None})
    return out


def main():
    OUT.mkdir(exist_ok=True)
    d = DECEASED
    print("=" * 86)
    print(f"HEIR + SIGNING WORKFLOW -- {d['label']}")
    print("Signer-gated Enformion search + phone dedupe before Trestle")
    print("=" * 86)

    n_person_search = 0
    n_tracerfy = 0

    # --- Step 1: ONE Person Search on the deceased -> heir set ------------
    dec = search_by_name_addr(d["first"], d["last"], d["city"], d["state"], d["zip"])
    n_person_search += 1
    if dec.get("_error") or not dec.get("persons"):
        print(f"Deceased search failed: {dec.get('_error') or 'no match'}")
        return
    p0 = dec["persons"][0]
    hints = family_hints(p0, d)
    dod = p0.get("dod")
    print(f"\nDeceased match: {p0.get('fullName')}  DOD(Enformion)={dod}")
    print(f"Family-area hints: {sorted(hints)}")

    signers, flagged = derive_signers(p0, d["last"])
    print(f"\nREQUIRED SIGNERS (living children, surname {d['last']}): {len(signers)}")
    for s in signers:
        print(f"   - {s['name']}  b.{s['byear']}")
    if flagged:
        print("\nFLAGGED FOR MANUAL VERIFICATION (NOT auto-searched, $0 spent):")
        for f in flagged:
            print(f"   ! {f}")

    # --- Step 2: per-signer Person Search (ONLY signers) -----------------
    pool = {}   # number -> {meta, owners:set}  <-- global dedupe
    results = []
    for s in signers:
        print(f"\n--- resolving signer: {s['name']} ---")
        res = search_by_name_dob(s["first"], s["last"], s["byear"])
        n_person_search += 1
        p = pick_match(res.get("persons"), s["last"], s["byear"], hints) if not res.get("_error") else None
        rec = {"name": s["name"], "byear": s["byear"], "age": None, "address": None,
               "all_addresses": [], "emails": [], "children": [], "phones": []}
        if not p:
            print("   Enformion: no confident match -- FLAG for manual lookup")
            rec["note"] = "unresolved"
            results.append(rec)
            continue
        rec["age"] = p.get("age")
        addrs = [addr_str(a) for a in (p.get("addresses") or []) if addr_str(a)]
        rec["all_addresses"] = addrs[:4]
        rec["address"] = addrs[0] if addrs else None
        for c in (p.get("relativesSummary") or []):
            if c.get("relativeLevel") == CHILD_LEVEL and year_of(c.get("dob")) and year_of(c.get("dob")) >= 1975:
                rec["children"].append(" ".join(x for x in [c.get("firstName"), c.get("lastName")] if x))
        print(f"   address: {rec['address']}")

        phones = signer_phones(p, "enformion")

        # Tracerfy (signer-gated) for emails + any extra phones
        if ENABLE_TRACERFY and rec["address"]:
            street, city, state, zc = split_addr(rec["address"], d)
            tf = tracerfy_lookup(s["first"], s["last"], street, city, state, zc)
            n_tracerfy += 1
            if tf.get("hit") and tf.get("persons"):
                for person in tf["persons"]:
                    tphones, emails = parse_person(person)
                    rec["emails"] = emails
                    for tp in tphones:
                        if tp["number"] not in [x["number"] for x in phones]:
                            phones.append({"number": tp["number"], "type": tp["type"],
                                           "src": "tracerfy", "dnc": tp.get("dnc")})
                        else:
                            for x in phones:
                                if x["number"] == tp["number"]:
                                    x["dnc"] = tp.get("dnc")

        # CAP per signer, then add to global dedupe pool
        rec["phones"] = phones[:MAX_PHONES_PER_SIGNER]
        for ph in rec["phones"]:
            slot = pool.setdefault(ph["number"], {"meta": ph, "owners": set()})
            slot["owners"].add(s["name"].split()[0])
            if ph.get("dnc") is not None:
                slot["meta"]["dnc"] = ph["dnc"]
        results.append(rec)

    # --- Step 3: Trestle scores each UNIQUE number ONCE ------------------
    raw_phone_count = sum(len(r["phones"]) for r in results)
    print("\n" + "=" * 86)
    print(f"PHONE DEDUPE: {raw_phone_count} signer-phones -> {len(pool)} unique numbers to score")
    print("=" * 86)
    for num, slot in pool.items():
        t = call_trestle(num, cfg.TRESTLE_API_KEY, add_litigator=True)
        score = t.get("activity_score")
        addons = t.get("add_ons") or {}
        slot["score"] = score
        slot["tier"] = assign_tier(score, DEFAULT_TIERS)
        slot["line_type"] = t.get("line_type")
        slot["lit"] = (addons.get("litigator_checks") or {}).get("phone.is_litigator_risk")

    # attach scores back to each signer
    for r in results:
        for ph in r["phones"]:
            ph.update({k: pool[ph["number"]].get(k) for k in ("score", "tier", "line_type", "lit")})
        r["phones"].sort(key=lambda x: (x.get("score") is None, -(x.get("score") or 0)))

    out = {"deceased": {**d, "dod_enformion": dod}, "required_signers": results,
           "flagged_for_verification": flagged,
           "cost": {"enformion_person_search": n_person_search, "tracerfy_hits": n_tracerfy,
                    "trestle_unique_phones": len(pool), "trestle_phones_saved": raw_phone_count - len(pool)}}
    with open(OUT / "heir_signing_workflow.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)

    # --- Console signer cards + cost ------------------------------------
    print("\n" + "#" * 86)
    print("# REQUIRED SIGNERS")
    print("#" * 86)
    for r in results:
        print(f"\n{r['name']}  (age {r['age']})  -> {r['address'] or 'UNRESOLVED'}")
        if r["children"]:
            print(f"   children: {r['children']}")
        if r["emails"]:
            print(f"   emails: {r['emails']}")
        for ph in r["phones"]:
            print(f"   {ph['number']}  {ph.get('score')}/{ph.get('tier')}  "
                  f"{ph.get('line_type')}  dnc={ph.get('dnc')}  [{ph['src']}]")

    c = out["cost"]
    est = c["enformion_person_search"] * 0.35 + c["tracerfy_hits"] * 0.10 + c["trestle_unique_phones"] * 0.015
    print("\n" + "=" * 86)
    print(f"COST: {c['enformion_person_search']} Person Search x$0.35 = ${c['enformion_person_search']*0.35:.2f}  |  "
          f"{c['tracerfy_hits']} Tracerfy x$0.10 = ${c['tracerfy_hits']*0.10:.2f}  |  "
          f"{c['trestle_unique_phones']} Trestle x$0.015 = ${c['trestle_unique_phones']*0.015:.2f}")
    print(f"  saved {c['trestle_phones_saved']} duplicate Trestle lookups (~${c['trestle_phones_saved']*0.015:.2f})")
    print(f"  TOTAL ~= ${est:.2f}   (Enformion free under 2,000-request trial)")
    print(f"\nStructured output -> {OUT / 'heir_signing_workflow.json'}")


def split_addr(s, fallback):
    s = (s or "").replace(";", ",")
    parts = [p.strip() for p in s.split(",")]
    street = parts[0] if parts else fallback["street"]
    city = parts[1] if len(parts) > 1 else fallback["city"]
    st, zc = fallback["state"], fallback["zip"]
    if len(parts) > 2:
        tail = parts[2].split()
        if tail:
            st = tail[0]
            if len(tail) > 1:
                zc = tail[1].split("-")[0]
    return street, city, st, zc


if __name__ == "__main__":
    main()
