"""Phase-0 scout for deep prospecting 1437 Overton Ln, Knoxville, TN 37923.

Gating facts before the full waterfall:
  1. Tracerfy find_owner probe @ the parcel  -> owner of record + deceased flag
     + relatives (heir graph) + ranked phones (DNC) in ONE instant lookup.
  2. Tracerfy name lookup at the property      -> address history / co-owners.
  3. Zillow (OpenWeb Ninja) property-details   -> value / beds-baths / last sale.

Prints full raw JSON so the branch (living vs deceased owner) is grounded in the
actual API responses. Reuses the production helpers; fabricates nothing.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
from run_brice_st_skiptrace import tracerfy_lookup, parse_person
import property_enricher as pe

STREET, CITY, STATE, ZIP = "1437 Overton Ln", "Knoxville", "TN", "37923"
BAR = "=" * 84


def dump(label, obj):
    print(f"\n----- {label} -----")
    print(json.dumps(obj, indent=2, default=str)[:6000])


def summarize_persons(persons):
    for i, p in enumerate(persons, 1):
        phones, emails = parse_person(p)
        print(f"  person[{i}]: {p.get('full_name') or p.get('first_name','?')} "
              f"{p.get('last_name','')}  age={p.get('age','?')} dob={p.get('dob','?')} "
              f"DECEASED={p.get('deceased','?')} property_owner={p.get('property_owner','?')} "
              f"litigator={p.get('litigator','?')}")
        mail = p.get("mailing_address") or {}
        if mail:
            print(f"     mailing: {mail.get('street','?')}, {mail.get('city','?')}, "
                  f"{mail.get('state','?')} {mail.get('zip','?')}")
        rels = p.get("relatives") or []
        if rels:
            names = [r.get("name") if isinstance(r, dict) else str(r) for r in rels]
            print(f"     relatives ({len(rels)}): {names[:15]}")
        print(f"     phones ({len(phones)}): "
              f"{[(x['number'], x.get('type'), x.get('dnc')) for x in phones]}")
        if emails:
            print(f"     emails: {emails}")


def main():
    print(BAR)
    print(f"SCOUT -- {STREET}, {CITY}, {STATE} {ZIP}")
    print(f"Tracerfy key set: {bool(cfg.TRACERFY_API_KEY)} | Trestle: {bool(cfg.TRESTLE_API_KEY)} "
          f"| OpenWebNinja: {bool(cfg.OPENWEBNINJA_API_KEY)}")
    print(BAR)

    # 1) find_owner probe on the parcel
    print("\n### Tracerfy find_owner @ property")
    fo = tracerfy_lookup("", "", STREET, CITY, STATE, ZIP, find_owner=True)
    if fo.get("error"):
        print(f"  ERROR: {fo['error']} {fo.get('detail','')}")
    else:
        print(f"  hit={fo.get('hit')}  persons={len(fo.get('persons') or [])}")
        if fo.get("persons"):
            summarize_persons(fo["persons"])
        dump("find_owner raw", fo)

    # 3) Zillow property facts
    print("\n### Zillow (OpenWeb Ninja) property-details")
    try:
        data = pe._fetch_property(STREET, CITY, STATE, ZIP, cfg.OPENWEBNINJA_API_KEY)
        if data:
            keys = ["zpid", "homeStatus", "price", "zestimate", "rentZestimate",
                    "livingArea", "bedrooms", "bathrooms", "yearBuilt", "lotSize",
                    "homeType", "lastSoldPrice", "dateSold", "address", "taxAssessedValue"]
            print("  " + json.dumps({k: data.get(k) for k in keys}, default=str))
            ph = data.get("priceHistory")
            if isinstance(ph, list) and ph:
                print("  priceHistory (recent):")
                for e in ph[:6]:
                    print(f"    {e.get('date')}  {e.get('event')}  ${e.get('price')}")
        else:
            print("  Zillow: no data returned")
    except Exception as e:
        print(f"  Zillow error: {e}")

    print("\n" + BAR)
    print("Scout done. Branch on the DECEASED flag + property_owner above.")
    print(BAR)


if __name__ == "__main__":
    main()
