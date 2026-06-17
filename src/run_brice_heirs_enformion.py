"""Deep prospecting -- 2106 Brice St / Willis estate, heirs from ENFORMION.

Pipeline (deep-prospecting waterfall, no browser automation):
  1. Enformion Person Search already ran -> output/enformion_Jewelline_Willis.json
     Confirmed 5 living children (relativeLevel 'ab', surname Willis):
       Luther Willis Jr (1953), Letitia M Willis (1966), Norman Willis (1961),
       Darrell Eugene Willis (1955), Kenneth Willis (1960).
  2. Tracerfy INSTANT lookup on each heir -> current phones + emails.
  3. Trestle phone_intel on every phone -> activity tier + litigator risk.

We anchor each heir to the two known family addresses (property + Jewelline's
mailing) so Tracerfy resolves the right individual. Stop after first HIT per heir
to avoid double-charging. Fabricates nothing -- a MISS is printed as a MISS.

NOTE ON DOD CONFLICT: Enformion's death index gives Jewelline's DOD as 2/9/2005,
while Sift's obituary data shows 2026-03-21. Both agree she is deceased (the only
fact that matters for heir contact). The date conflict is surfaced, not resolved.

Run from project root:  python src/run_brice_heirs_enformion.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
from phone_validator import call_trestle, assign_tier, clean_phone, DEFAULT_TIERS
from run_brice_st_skiptrace import tracerfy_lookup, render_persons

PROP = ("2106 Brice St", "Knoxville", "TN", "37917")
MAIL = ("4412 W Sunset Rd", "Knoxville", "TN", "37914")

# 5 living children confirmed by Enformion (relativeLevel 'ab'), ranked by score.
HEIRS = [
    ("Luther", "Willis", "Jr  b.1953  Enformion score 650"),
    ("Letitia", "Willis", "M   b.1966  Enformion 500 / on-file letitiaw@sbcglobal.net"),
    ("Norman", "Willis", "b.1961  Enformion 450 / on-file normanwillis14@gmail.com"),
    ("Darrell", "Willis", "Eugene b.1955  Enformion 400"),
    ("Kenneth", "Willis", "b.1960  Enformion 300"),
]

# Phones Enformion already returned on Jewelline's record -- score them too.
ENFORMION_PHONES = {
    "8655401564": "Jewelline record (LandLine, last seen 5/2026)",
    "4236376517": "Jewelline record (Wireless, 2016)",
    "8655235434": "Jewelline record (LandLine, 2016)",
    "8656376517": "Jewelline record (LandLine, 2016)",
}


def main():
    print("=" * 84)
    print("DEEP PROSPECTING -- 2106 Brice St, Knoxville TN 37917 / Willis estate")
    print("Heirs from Enformion -> Tracerfy skip trace -> Trestle scoring (LIVE APIs)")
    print("Deceased owner (Jewelline Y Willis) | 4 yrs tax delinquent")
    print("=" * 84)

    phone_owner = {}
    phone_meta = {}
    tracerfy_hits = 0

    for first, last, note in HEIRS:
        label = f"{first} {last} ({note})"
        print(f"\n### {label}")
        got = False
        for (street, city, state, zc) in (PROP, MAIL):
            print(f"  -> Tracerfy: {first} {last} @ {street}, {city}, {state} {zc}")
            data = tracerfy_lookup(first, last, street, city, state, zc)
            if data.get("error"):
                print(f"     ERROR: {data['error']} {data.get('detail','')}")
                continue
            if data.get("hit") and data.get("persons"):
                tracerfy_hits += 1
                got = True
                print(f"     HIT -- {len(data['persons'])} record(s)")
                render_persons(data["persons"], label, phone_owner, phone_meta)
                break
            print("     MISS (no match)")
        if not got:
            print(f"  (no Tracerfy hit for {label})")

    for num, lbl in ENFORMION_PHONES.items():
        phone_owner.setdefault(num, lbl)

    print("\n" + "=" * 84)
    print(f"Tracerfy hits: {tracerfy_hits}  |  unique phones to Trestle-score: {len(phone_owner)}")
    print("=" * 84)

    print("\n### Trestle phone_intel (activity score + line type + litigator)")
    scored = []
    for cleaned, owner in phone_owner.items():
        data = call_trestle(cleaned, cfg.TRESTLE_API_KEY, add_litigator=True)
        if data.get("error") and not data.get("is_valid"):
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
    print(f"\n{'PHONE':<12}{'SCORE':>6} {'TIER':<13}{'LINE':<10}{'VALID':<6}{'LIT':<5}OWNER")
    print("-" * 84)
    for r in scored:
        print(f"{r['phone']:<12}{str(r['score']):>6} {r['tier']:<13}"
              f"{str(r['line_type']):<10}{str(r['is_valid']):<6}{str(r['lit']):<5}{r['owner']}")

    print("\n" + "=" * 84)
    print(f"Est. cost: Tracerfy {tracerfy_hits} hits  |  Trestle {len(scored)} phones scored")
    print("=" * 84)


if __name__ == "__main__":
    main()
