"""Deep prospecting — full API-first heir waterfall for ONE deceased-owner record.

Generalizes the one-off Brice St scripts into a parameterized runner that follows
the deep-prospecting skill's Primary Path (Steps A-E):

  A. Person Search the DECEASED            -> relatives graph + date of death
  B. Derive REQUIRED SIGNERS               -> living children (surname + DOB)
  C. Person Search each SIGNER (name+DOB)  -> address + phones + their kids
  D. DEDUPE phones across signers          -> one unique set
  E. Trestle-score each unique phone       -> dial tiers + litigator risk

Heirs come from the provider graph (grounded — nothing inferred). A MISS is
printed as a MISS. Billing is per match; signer-gating (B) and phone-dedupe (D)
are the cost levers. Use this for a single high-value record; the bulk daily
pipeline uses only Step A via `python src/main.py daily --deep-heirs`.

USAGE
-----
  python src/run_deep_prospect.py --first Jewelline --last Willis \
      --street "2106 Brice St" --city Knoxville --state TN --zip 37917

  # Optional second known address (e.g. tax mailing) improves Tracerfy match:
  python src/run_deep_prospect.py --first Jewelline --last Willis \
      --street "2106 Brice St" --city Knoxville --state TN --zip 37917 \
      --mail-street "4412 W Sunset Rd" --mail-zip 37914

Requires ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD, TRACERFY_API_KEY,
TRESTLE_API_KEY in .env.
"""

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
import enformion_heir as enf
from phone_validator import call_trestle, assign_tier, clean_phone, DEFAULT_TIERS
from run_brice_st_skiptrace import tracerfy_lookup, parse_person

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

BAR = "=" * 84


def _enformion_phones(person: dict) -> list[dict]:
    """Pull phones off an Enformion person object."""
    out = []
    for p in person.get("phoneNumbers") or []:
        if not isinstance(p, dict):
            continue
        num = clean_phone(p.get("phoneNumber") or p.get("number") or "")
        if num:
            out.append({
                "number": num,
                "type": p.get("phoneType") or p.get("type"),
                "connected": p.get("isConnected"),
                "last_seen": p.get("lastReportedDate"),
            })
    return out


def _enformion_addresses(person: dict) -> list[str]:
    out = []
    for a in person.get("addresses") or []:
        if isinstance(a, dict):
            full = a.get("fullAddress") or a.get("AddressLine2") or ""
            if full:
                out.append(full)
    return out


def main():
    ap = argparse.ArgumentParser(description="Deep prospecting heir waterfall (one record)")
    ap.add_argument("--first", required=True, help="Deceased owner first name")
    ap.add_argument("--last", required=True, help="Deceased owner last name")
    ap.add_argument("--street", required=True, help="Property street address")
    ap.add_argument("--city", default="Knoxville")
    ap.add_argument("--state", default="TN")
    ap.add_argument("--zip", dest="zip_code", default="")
    ap.add_argument("--mail-street", default="", help="Optional 2nd known address (tax mailing)")
    ap.add_argument("--mail-city", default="")
    ap.add_argument("--mail-zip", default="")
    ap.add_argument("--max-signers", type=int, default=8, help="Cap paid per-signer searches")
    args = ap.parse_args()

    if not enf.is_configured():
        logger.error("Enformion not configured. Set ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD in .env")
        sys.exit(1)

    surname = args.last
    print(BAR)
    print(f"DEEP PROSPECTING -- {args.first} {args.last} | {args.street}, "
          f"{args.city}, {args.state} {args.zip_code}")
    print("Enformion heirs -> required signers -> per-signer search -> dedupe -> Trestle")
    print(BAR)

    # ── Step A: Person Search the decedent ────────────────────────────
    print("\n### Step A — Person Search the decedent (heir graph + DOD)")
    data = enf.person_search(args.first, args.last, city=args.city,
                             state=args.state, zip_code=args.zip_code)
    decedent = enf.first_match(data)
    if not decedent:
        print("  MISS — Enformion returned no match for the decedent. Try a 2nd address or check the name.")
        sys.exit(0)

    dod = enf.extract_dod(decedent)
    survivors = enf.relatives_to_survivors(decedent)
    print(f"  MATCH — DOD={dod or 'unknown'}  relatives={len(survivors)}")
    for s in survivors:
        flag = "DECEASED" if s["_deceased"] else "living"
        print(f"    - {s['name']:<28} {s['relationship'] or '?':<12} "
              f"level={s['_level'] or '?':<3} dob={s['_dob'] or '?':<6} "
              f"score={s['_score']:<4} {flag}")

    # ── Step B: Required signers (cost gate #1) ───────────────────────
    signers = enf.required_signers(survivors, surname)
    print(f"\n### Step B — Required signers (living closest-kin '{surname}' with DOB): {len(signers)}")
    if not signers:
        print("  No living closest-kin signers with a birth year. Flags to review:")
        for s in survivors:
            if s["_deceased"] and s["_level"] == enf.CLOSEST_KIN_LEVEL:
                print(f"    - {s['name']} is deceased -> per stirpes (check their children)")
        print("  Escalate per the skill (L4 / probate attorney) if no signer resolves.")
    for s in signers[:args.max_signers]:
        print(f"    * {s['name']} (dob {s['_dob']})")

    # ── Step C: Resolve each signer (signers only) ────────────────────
    print(f"\n### Step C — Resolve each signer (Person Search name+DOB, capped at {args.max_signers})")
    phone_entries: list[dict] = []   # {number, owner, type, connected}
    signer_cards = []
    mail_addr = (args.mail_street, args.mail_city or args.city, args.state, args.mail_zip)
    prop_addr = (args.street, args.city, args.state, args.zip_code)

    for s in signers[:args.max_signers]:
        parts = s["name"].split()
        sfirst, slast = parts[0], parts[-1]
        print(f"\n  -> {s['name']} (dob {s['_dob']})")
        sdata = enf.person_search(sfirst, slast, dob_year=s["_dob"])
        sp = enf.first_match(sdata)
        card = {"name": s["name"], "dob": s["_dob"], "address": "", "phones": [], "emails": []}
        if sp:
            addrs = _enformion_addresses(sp)
            if addrs:
                card["address"] = addrs[0]
                print(f"     address: {addrs[0]}")
            for ph in _enformion_phones(sp):
                ph["owner"] = s["name"]
                phone_entries.append(ph)
                card["phones"].append(ph["number"])
            print(f"     Enformion phones: {len(card['phones'])}")
        else:
            print("     Enformion MISS (no name+DOB match)")

        # Tracerfy enrichment (phones/emails) anchored to family addresses
        if cfg.TRACERFY_API_KEY:
            for (street, city, state, zc) in (prop_addr, mail_addr):
                if not street:
                    continue
                tdata = tracerfy_lookup(sfirst, slast, street, city, state, zc)
                if tdata.get("hit") and tdata.get("persons"):
                    for person in tdata["persons"]:
                        tphones, temails = parse_person(person)
                        for tp in tphones:
                            phone_entries.append({"number": tp["number"], "owner": s["name"],
                                                  "type": tp.get("type"), "connected": None})
                            card["phones"].append(tp["number"])
                        card["emails"].extend(temails)
                    print(f"     Tracerfy HIT @ {street}: +{len(tdata['persons'])} record(s)")
                    break
        signer_cards.append(card)

    # ── Step D: Dedupe phones (cost gate #2) ──────────────────────────
    unique = enf.dedupe_phones(phone_entries)
    owner_by_num: dict[str, str] = {}
    for p in phone_entries:
        num = re.sub(r"\D", "", str(p.get("number", "")))
        owner_by_num.setdefault(num, p.get("owner", "?"))
    print(f"\n### Step D — Dedupe phones: {len(phone_entries)} -> {len(unique)} unique")

    # ── Step E: Trestle-score each unique phone ───────────────────────
    print("\n### Step E — Trestle phone scoring (activity + line type + litigator)")
    scored = []
    for p in unique:
        num = p["number"]
        tdata = call_trestle(num, cfg.TRESTLE_API_KEY, add_litigator=True)
        score = tdata.get("activity_score")
        addons = tdata.get("add_ons") or {}
        lit = (addons.get("litigator_checks") or {}).get("phone.is_litigator_risk")
        scored.append({
            "phone": num, "owner": owner_by_num.get(num, "?"), "score": score,
            "tier": assign_tier(score, DEFAULT_TIERS) if score is not None else "ERROR",
            "line_type": tdata.get("line_type"), "is_valid": tdata.get("is_valid"),
            "lit": lit,
        })
    scored.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))

    # ── Master dial sheet ─────────────────────────────────────────────
    print("\n" + BAR)
    print(f"MASTER DIAL SHEET -- {args.street} -- {args.last} estate (deduped, best first)")
    print(BAR)
    print(f"{'PHONE':<12}{'SCORE':>6} {'TIER':<13}{'LINE':<10}{'VALID':<6}{'LIT':<5}REACHES")
    print("-" * 84)
    for r in scored:
        print(f"{r['phone']:<12}{str(r['score']):>6} {r['tier']:<13}"
              f"{str(r['line_type']):<10}{str(r['is_valid']):<6}{str(r['lit']):<5}{r['owner']}")

    print("\n### Signer contact cards")
    for c in signer_cards:
        print(f"  {c['name']} (dob {c['dob']}) — {c['address'] or 'address not found'}")
        if c["emails"]:
            print(f"     emails: {sorted(set(c['emails']))}")

    n_calls = 1 + len(signers[:args.max_signers])
    print("\n" + BAR)
    print(f"Est. cost: Enformion {n_calls} searches (~${n_calls*0.35:.2f}) | "
          f"Trestle {len(scored)} phones (~${len(scored)*0.015:.2f})")
    print("Grounding: every heir/phone above came from an API response. Verify "
          "signing parties via title/probate before treating as legal fact.")
    print(BAR)


if __name__ == "__main__":
    main()
