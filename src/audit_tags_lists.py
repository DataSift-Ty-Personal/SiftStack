"""Audit (and repair) the FTM tag and the notice-type list on every record.

WHY THIS IS SEPARATE FROM THE UPLOAD CHECK. Verifying the CSV we SENT is not the
same as verifying what DataSift KEPT. The upload CSV carried Courthouse Data +
FTM + the list on 1262/1262 rows, and that told us nothing about whether the
CRM actually applied them. This reads the live records.

What every record must carry:
    tag  "FTM"              first-to-market marker
    tag  "Courthouse Data"  routes niche presets vs bulk data
    tag  <notice_type>      probate / foreclosure
    list "Probate"          probate records only
    list "Foreclosure"      foreclosure records only

    python src/audit_tags_lists.py                 # report only
    python src/audit_tags_lists.py --fix           # add whatever is missing

Tags MERGE on write (read-modify-write), so a repair never clears existing tags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datasift_api_upload import Api  # noqa: E402

REQUIRED_TAGS = ("FTM", "Courthouse Data")
LIST_FOR_TYPE = {"probate": "Probate", "foreclosure": "Foreclosure"}


def names(items) -> list[str]:
    out = []
    for x in items or []:
        if isinstance(x, dict):
            out.append((x.get("name") or x.get("title") or "").strip())
        else:
            out.append(str(x).strip())
    return [x for x in out if x]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default="output/enriched_audit.json")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="output/tag_list_audit.json")
    a = ap.parse_args()

    rows = json.load(open(a.audit, encoding="utf-8"))
    if a.limit:
        rows = rows[:a.limit]

    api = Api()
    report, fixed, failed = [], 0, 0
    miss_tag = miss_list = miss_type_tag = 0

    for i, r in enumerate(rows, 1):
        try:
            d = api.call(f"/api/internal/property/{r['uuid']}/")
        except Exception as exc:
            failed += 1
            if failed <= 5:
                print(f"  read fail {r['address']}: {str(exc)[:70]}")
            continue

        tags = names(d.get("tags"))
        lists = names(d.get("lists"))
        low_tags = {t.lower() for t in tags}
        low_lists = {x.lower() for x in lists}

        want_list = LIST_FOR_TYPE.get(r["type"], "")
        missing_tags = [t for t in REQUIRED_TAGS if t.lower() not in low_tags]
        if r["type"] and r["type"].lower() not in low_tags:
            missing_tags.append(r["type"])
            miss_type_tag += 1
        needs_list = bool(want_list) and want_list.lower() not in low_lists

        if any(t.lower() == "ftm" for t in missing_tags):
            miss_tag += 1
        if needs_list:
            miss_list += 1

        rec = {"uuid": r["uuid"], "address": r["address"], "type": r["type"],
               "tags": tags, "lists": lists,
               "missing_tags": missing_tags, "missing_list": want_list if needs_list else ""}

        if a.fix and (missing_tags or needs_list):
            try:
                if missing_tags:
                    # MERGE: send existing + missing so nothing is cleared.
                    api.call(f"/api/internal/property/{r['uuid']}/tags/", "POST",
                             {"tags": sorted(set(tags) | set(missing_tags))})
                if needs_list:
                    api.call(f"/api/internal/property/{r['uuid']}/lists/", "POST",
                             {"lists": sorted(set(lists) | {want_list})})
                time.sleep(0.4)
                back = api.call(f"/api/internal/property/{r['uuid']}/")
                bt = {t.lower() for t in names(back.get("tags"))}
                bl = {x.lower() for x in names(back.get("lists"))}
                ok = all(t.lower() in bt for t in REQUIRED_TAGS) and \
                     (not want_list or want_list.lower() in bl)
                rec["fixed"] = ok
                fixed += ok
                if not ok:
                    print(f"  FIX DID NOT STICK {r['address'][:28]}")
            except Exception as exc:
                rec["fix_error"] = str(exc)[:120]
                if failed <= 8:
                    print(f"  fix fail {r['address'][:26]}: {str(exc)[:80]}")
                failed += 1

        report.append(rec)
        if i % 100 == 0:
            print(f"   {i}/{len(rows)} missing_FTM={miss_tag} missing_list={miss_list} "
                  f"fixed={fixed} failed={failed}", flush=True)
            json.dump(report, open(a.out, "w"))
        time.sleep(a.sleep)

    json.dump(report, open(a.out, "w"))
    n = len(report)
    print(f"\n=== {n} records ===")
    print(f"missing FTM tag             : {miss_tag}")
    print(f"missing Courthouse Data tag : {sum(1 for x in report if any(t.lower()=='courthouse data' for t in x['missing_tags']))}")
    print(f"missing notice-type tag     : {miss_type_tag}")
    print(f"missing its list            : {miss_list}")
    if a.fix:
        print(f"repaired + verified         : {fixed}")
    print(f"failed                      : {failed}")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
