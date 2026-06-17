"""DISCOVERY pass -- deep prospect 1833 Laurans Ave, Knoxville TN 37915-2620.

Deceased / tax-delinquent record (4 years behind). Obituary tag present.
Owner of record per REISift export: JOYCE BROWN
  property/mailing: 1833 Laurans Ave, Knoxville, TN 37915-2620
On-file contacts (prior trace) imply relatives / heirs:
  emails: brownjoyce134@gmail.com, jbrown6657@att.net, brownjoyce134@ymail.com,
          thomaswynn4257@gmail.com, blackpride@password.spinway.com, plyn4kpz@live.com
  phones: 8654052624, 8653865557, 8652978632, 8659347161, 8654555534

This is the GROUNDED heir-discovery step. We use Tracerfy's INSTANT lookup
(/v1/api/trace/lookup/), which returns persons[] with the deceased flag,
relatives (the heir candidates), address history, and ranked phones. We dump the
raw persons[] so the heir set comes from the API, never inferred.

Run:  python src/run_laurans_brown_skiptrace.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
from run_brice_st_skiptrace import tracerfy_lookup, render_persons

OUT = Path(__file__).resolve().parent.parent / "output" / "laurans_brown_tracerfy_raw.json"

# find_owner surfaces the current owner / heir on the parcel.
FIND_OWNER_PROBES = [
    ("1833 Laurans Ave", "Knoxville", "TN", "37915"),
]

NAMED = [
    ("Joyce", "Brown", "1833 Laurans Ave", "Knoxville", "TN", "37915"),
]


def main():
    print("=" * 80)
    print("DISCOVERY -- 1833 Laurans Ave, Knoxville TN 37915 / Joyce Brown estate")
    print("Tracerfy instant lookup: pull relatives graph + deceased flag (LIVE)")
    print("=" * 80)

    phone_owner, phone_meta = {}, {}
    dump = {"find_owner": [], "named": []}

    print("\n### find_owner probe on the parcel")
    for (street, city, state, zc) in FIND_OWNER_PROBES:
        print(f"  -> Tracerfy find_owner @ {street}, {city}, {state} {zc}")
        data = tracerfy_lookup("", "", street, city, state, zc, find_owner=True)
        dump["find_owner"].append({"probe": street, "response": data})
        if data.get("error"):
            print(f"     ERROR: {data['error']} {data.get('detail','')}")
        elif data.get("hit") and data.get("persons"):
            print(f"     HIT -- {len(data['persons'])} record(s)")
            render_persons(data["persons"], f"owner@{street}", phone_owner, phone_meta)
        else:
            print("     MISS")

    print("\n### named lookup: Joyce Brown")
    for (first, last, street, city, state, zc) in NAMED:
        print(f"  -> Tracerfy: {first} {last} @ {street}, {city}, {state} {zc}")
        data = tracerfy_lookup(first, last, street, city, state, zc)
        dump["named"].append({"name": f"{first} {last}", "response": data})
        if data.get("error"):
            print(f"     ERROR: {data['error']} {data.get('detail','')}")
        elif data.get("hit") and data.get("persons"):
            print(f"     HIT -- {len(data['persons'])} record(s)")
            render_persons(data["persons"], f"{first} {last}", phone_owner, phone_meta)
        else:
            print("     MISS")

    OUT.write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    print(f"\nRaw Tracerfy JSON saved -> {OUT}")


if __name__ == "__main__":
    main()
