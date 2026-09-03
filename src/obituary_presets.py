"""Add the staged Obituary call and mail presets to folder 11 on ty+2.

WHAT ALREADY EXISTS (verified live, do not recreate):
  Folder `11. DEEP PROSPECTING (ALL TIERS)` has existed since 2026-06-19 and
  already holds `01 No / Bad Phone`, `02 Obituary / Deceased`, `03 Exhausted
  Call -> DP`, `04 Return Mail -> DP` and `05 Vacant -> DP`. The obituary gate
  Ty asked for is `02 Obituary / Deceased`, which already filters on
  `all_lists: [Obituary]` and already returns 854 records. Nothing to build there.

  An earlier pass in this build reported the folder as missing. That was wrong:
  `GET /api/internal/filter-preset-folder/` returns only 10 results unless you
  pass `limit=999`, and ty+2 has 21 folders. Every listing here passes it.

WHAT THIS ADDS:
  The staged call ladder the folder lacks, so the obituary cohort is workable
  rather than just filterable. Same 00-05 shape the Hottest / Strong / FTM
  folders use, gated on the Obituary LIST.

Two deliberate choices:

  The gate is the LIST, never the TAG. The Obituary tag covers only the 60
  records this build worked; the list holds 905. A tag gate would silently route
  a fraction of the cohort.

  The `must_not` block is deep-copied from `Hottest - 02 Ready to Call` rather
  than retyped, so every dead status, Mail Only, recently sold, Not Single
  Family, low/negative equity and dead-neighbourhood rule rides along and stays
  correct when Ty edits the Hottest presets later.

`one_per_owner` matches the house style of every other preset in the folder: an
owner holding six properties should be one row in a call queue, not six.

  python src/obituary_presets.py --dry
  python src/obituary_presets.py --commit
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obituary_opportunity import Reader  # noqa: E402

FOLDER = "11. DEEP PROSPECTING (ALL TIERS)"
PROTO_FOLDER = "01. HOTTEST - CALL"
PROTO_PRESET = "Hottest - 02 Ready to Call"


def aslist(r):
    if isinstance(r, dict):
        return r["results"] if "results" in r else []
    return r or []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    rd = Reader("datasift-apikey", gap=0.35)

    lists = {(l.get("title") or "").strip().lower(): l["uuid"]
             for l in aslist(rd._call("/api/internal/list/?limit=999"))}
    obit = lists.get("obituary")
    if not obit:
        sys.exit("No 'Obituary' list on this account")

    folders = aslist(rd._call("/api/internal/filter-preset-folder/?limit=999"))
    folder = next((f for f in folders if (f.get("title") or "").strip() == FOLDER), None)
    if not folder:
        sys.exit(f"Folder {FOLDER!r} not found among {len(folders)} folders")
    print(f"  folder: {FOLDER} -> {folder['uuid']}")

    src = next((f for f in folders if (f.get("title") or "").strip() == PROTO_FOLDER), None)
    proto = next((p for p in aslist(rd._call(
        f"/api/internal/filter-preset-folder/{src['uuid']}/filter-preset/?limit=999"))
        if (p.get("title") or "").strip() == PROTO_PRESET), None) if src else None
    if not proto:
        sys.exit(f"Could not read the prototype preset {PROTO_PRESET!r}")
    pmust = (rd._call(f"/api/internal/filter-preset/{proto['uuid']}/")
             .get("filters") or {}).get("must") or {}
    mustnot = copy.deepcopy(pmust.get("must_not") or {})
    print(f"  suppression cloned from {PROTO_PRESET!r}: "
          f"{len(mustnot.get('any_property_status') or [])} statuses, "
          f"{len(mustnot.get('any_tags') or [])} tags, "
          f"{len(mustnot.get('any_lists') or [])} lists, "
          f"{len(mustnot.get('any_neighborhood') or [])} neighbourhoods")

    existing = {(p.get("title") or "").strip(): p for p in aslist(rd._call(
        f"/api/internal/filter-preset-folder/{folder['uuid']}/filter-preset/?limit=999"))}
    print(f"  folder already holds {len(existing)}: {sorted(existing)}")

    base = {"all_lists": [obit], "must_not": mustnot,
            "ownerPropertiesOwned": {"show_properties": "one_per_owner"}}
    PRESETS = [
        ("06 Obituary - Ready to Call", {**base, "phone": 1, "skiptraced": 1,
                                         "predictivecall_attempts": [0, 0]}),
        ("07 Obituary - Call Attempt 1", {**base, "phone": 1,
                                          "predictivecall_attempts": [1, 1]}),
        ("08 Obituary - Call Attempt 2", {**base, "phone": 1,
                                          "predictivecall_attempts": [2, 2]}),
        ("09 Obituary - Call Attempt 3", {**base, "phone": 1,
                                          "predictivecall_attempts": [3, 3]}),
        ("10 Obituary - Ready to Mail", {**base, "directmail_attempts": [0, 0]}),
    ]

    def count(must):
        return rd._call("/api/internal/property/", method="POST", override="GET",
                        body={"limit": 1, "offset": 0, "query": {"must": must}}).get("count")

    print()
    for title, must in PRESETS:
        state = "EXISTS, would update" if title in existing else "new"
        print(f"    {title:32} {count(must):5} records   ({state})")

    if not args.commit or args.dry:
        print("\n  DRY RUN, nothing written. Rerun with --commit.")
        return

    print()
    for title, must in PRESETS:
        if title in existing:
            uuid = existing[title]["uuid"]
            rd._call(f"/api/internal/filter-preset/{uuid}/", method="PATCH",
                     body={"filters": {"must": must}})
            action = "updated"
        else:
            r = rd._call("/api/internal/filter-preset/", method="POST",
                         body={"title": title, "folder": folder["uuid"],
                               "quick_filter": False, "type": "properties",
                               "filters": {"must": must}})
            uuid = r.get("uuid")
            action = "created"
        # read back: a preset that saves but does not render is the silent failure
        back = (rd._call(f"/api/internal/filter-preset/{uuid}/").get("filters") or {}).get("must")
        same = json.dumps(back, sort_keys=True) == json.dumps(must, sort_keys=True)
        n = count(back or {})
        flag = "" if same else "   <-- READ-BACK DIFFERS FROM WHAT WE SENT"
        print(f"    {action:8} {title:32} {n:5} records{flag}")
        if n == 0:
            print("             WARNING: 0 records is a failure, not a quiet success")


if __name__ == "__main__":
    main()
