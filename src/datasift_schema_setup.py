"""Create the DataSift custom fields, select options and lists this pull needs.

Idempotent and dry-run by default. Every step checks for an existing object
first, so re-running never duplicates. Run with --commit to actually write.

    python src/datasift_schema_setup.py            # show the plan
    python src/datasift_schema_setup.py --commit   # apply it

Discovery notes that shaped this (verified live 2026-08-03):
  * "Tax delinquency amount" (money) and "Number of years tax-linked" (number)
    ALREADY EXIST. Reused, not recreated.
  * "Notice Type" is a SELECT whose only options are foreclosure, tax_sale,
    tax_delinquent, probate. None of this pull's types exist, so the values
    would silently fail to stick without adding the options first.
  * Lien records route to the existing "Liens" list (Ty), condemnations get a
    new "Condemned" list, and there is NO pool list: first-to-market routing is
    by the "Courthouse Data" tag into the niche sequential presets.
"""
from __future__ import annotations

import argparse
import os
import sys

_API = r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api"
sys.path.insert(0, _API)
sys.path.insert(0, os.path.join(_API, "clients"))
os.environ.setdefault("REISIFT_ACCOUNT", "datasift-apikey")

from clients.crm_api import CRMClient  # noqa: E402

# label -> field_type. Only what this pull needs and the account lacks.
NEW_FIELDS = [
    ("Outstanding Lien Amount", "money"),    # active liens only, releases removed
    ("Total Delinquency", "money"),          # liens + county tax owed
    ("Est Equity", "money"),                 # AVM minus mortgage on this parcel
    ("Debt vs Equity Pct", "number"),
    ("Upside Down", "select"),
    ("Liens Active vs Total", "text"),
    ("Lien Type", "select"),
]
FIELD_OPTIONS = {
    "Upside Down": ["yes", "no"],
    "Lien Type": ["LEN", "STL", "FTL"],
}
# Values this pull writes into the existing Notice Type select.
NOTICE_TYPE_OPTIONS = ["lien", "trustee_deed", "eviction",
                       "code_violation", "public_sale", "order"]
NEW_LISTS = ["Condemned", "Trustee Deed", "Public Sale", "Court Order"]


def fetch(c, path, **params):
    r = c._request(path, method="GET", params={"limit": 999, **params})
    return r.get("results") or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    tag = "COMMIT" if a.commit else "DRY RUN"
    c = CRMClient()

    fields = fetch(c, "/api/internal/custom-fields/")
    by_label = {f.get("label"): f for f in fields}
    lists = fetch(c, "/api/internal/list/")
    list_titles = {str(x.get("title")) for x in lists}
    # group_id is REQUIRED on create but is not returned by the field list, so
    # resolve it from the groups endpoint. The FTM fields belong in the existing
    # "TN Public Notice" group alongside Notice Type / County / Source URL.
    groups = fetch(c, "/api/internal/custom-fields/group/")
    gmap = {str(g.get("title") or g.get("label")): g.get("id") for g in groups}
    group_id = gmap.get("TN Public Notice") or gmap.get("Misc.")
    if not group_id:
        print("ERROR: could not resolve a custom-field group id; aborting")
        return 1
    print("[%s] %d existing fields, %d existing lists, group_id=%s\n"
          % (tag, len(fields), len(lists), group_id))

    print("--- CUSTOM FIELDS ---")
    for label, ftype in NEW_FIELDS:
        if label in by_label:
            print("  exists   %-26s (%s)" % (label, by_label[label].get("field_type")))
            continue
        if not a.commit:
            print("  CREATE   %-26s %s" % (label, ftype))
            continue
        body = {"label": label, "field_type": ftype,
                "entity_type": "property"}
        if group_id:
            body["group_id"] = group_id
        # A select/multiselect is rejected without options at creation time:
        # {"options": ["Options are required and must contain at least 1 item."]}
        if ftype in ("select", "multiselect"):
            body["options"] = [{"label": o, "value": o}
                               for o in FIELD_OPTIONS.get(label, [])]
        r = c._request("/api/internal/custom-fields/", method="POST", body=body)
        by_label[label] = r
        print("  created  %-26s %s -> %s" % (label, ftype, r.get("uuid") or r.get("id")))

    print("\n--- SELECT OPTIONS ---")
    wanted = dict(FIELD_OPTIONS)
    wanted["Notice Type"] = NOTICE_TYPE_OPTIONS
    for label, opts in wanted.items():
        f = by_label.get(label)
        if not f:
            print("  SKIP     %-26s (field not created yet)" % label)
            continue
        have = {str(o.get("label") or o.get("value")) for o in (f.get("options") or [])}
        missing = [o for o in opts if o not in have]
        if not missing:
            print("  ok       %-26s all %d options present" % (label, len(opts)))
            continue
        fid = f.get("id") or f.get("uuid")
        for o in missing:
            if not a.commit:
                print("  ADD OPT  %-26s %s" % (label, o))
                continue
            c._request(f"/api/internal/custom-fields/{fid}/option/",
                       method="POST", body={"label": o, "value": o})
            print("  added    %-26s %s" % (label, o))

    print("\n--- LISTS ---")
    for title in NEW_LISTS:
        if title in list_titles:
            print("  exists   %s" % title)
            continue
        if not a.commit:
            print("  CREATE   %s" % title)
            continue
        r = c._request("/api/internal/list/", method="POST", body={"title": title})
        print("  created  %-20s -> %s" % (title, r.get("uuid")))

    print("\nReused, NOT recreated: 'Liens' list, 'Tax delinquency amount', "
          "'Number of years tax-linked'.")
    print("No pool list: first-to-market routing is the 'Courthouse Data' tag.")
    if not a.commit:
        print("\nNothing was written. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
