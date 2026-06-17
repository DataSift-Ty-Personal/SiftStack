"""Phase-0b: richer Tracerfy named lookup on the owner of record.

find_owner returned a slim record (no DOB/relatives/deceased). A named lookup
(first+last anchored to the property + a couple name variants) returns the full
person record: DOB/age, deceased flag, relatives (heir graph), and address
history. This is the living-vs-deceased branch decision.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
from run_brice_st_skiptrace import tracerfy_lookup, parse_person

CITY, STATE, ZIP = "Knoxville", "TN", "37923"
PROP = "1437 Overton Ln"
BAR = "=" * 84

VARIANTS = [
    ("Nicholas", "Chessher", PROP, CITY, STATE, ZIP),
    ("Nick", "Chessher", PROP, CITY, STATE, ZIP),
]


def main():
    print(BAR)
    print("PHASE-0b -- richer named lookup: Nicholas Chessher @ 1437 Overton Ln")
    print(BAR)
    for (f, l, street, city, state, zc) in VARIANTS:
        print(f"\n### Tracerfy named: {f} {l} @ {street}")
        data = tracerfy_lookup(f, l, street, city, state, zc)
        if data.get("error"):
            print(f"  ERROR {data['error']} {data.get('detail','')}")
            continue
        print(f"  hit={data.get('hit')} persons={len(data.get('persons') or [])} "
              f"credits={data.get('credits_deducted')}")
        for i, p in enumerate(data.get("persons") or [], 1):
            phones, emails = parse_person(p)
            print(f"  person[{i}]: {p.get('full_name')}  age={p.get('age')} dob={p.get('dob')} "
                  f"DECEASED={p.get('deceased')} owner={p.get('property_owner')} "
                  f"litigator={p.get('litigator')}")
            mail = p.get("mailing_address") or {}
            if mail:
                print(f"     mailing: {mail}")
            addrs = p.get("addresses")
            if isinstance(addrs, list) and addrs:
                print(f"     addresses ({len(addrs)}):")
                for a in addrs[:10]:
                    if isinstance(a, dict):
                        print(f"       - {a.get('street')}, {a.get('city')} {a.get('state')} "
                              f"{a.get('zip')}  ({a.get('date_first')}..{a.get('date_last')})")
            rels = p.get("relatives") or []
            if rels:
                print(f"     relatives ({len(rels)}):")
                for r in rels[:20]:
                    if isinstance(r, dict):
                        print(f"       - {r.get('name')}  {r.get('relationship','')} "
                              f"age={r.get('age','')} deceased={r.get('deceased','')}")
                    else:
                        print(f"       - {r}")
            print(f"     phones ({len(phones)}): "
                  f"{[(x['number'], x.get('type'), x.get('dnc'), x.get('rank')) for x in phones]}")
            if emails:
                print(f"     emails: {emails}")
        # Dump the first person fully so we don't miss any schema field
        if data.get("persons"):
            print("\n  --- person[1] raw ---")
            print(json.dumps(data["persons"][0], indent=2, default=str)[:5000])


if __name__ == "__main__":
    main()
