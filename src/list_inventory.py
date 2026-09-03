"""Inventory the Ty+2 DataSift lists against the County List Playbook framework.

Answers two questions Ty actually asked:
  1. Which framework segments am I marketing to right now, and which am I not?
  2. Of the ones I am not, which are Priority 2 for MY county (Knox 47093)?

The framework ranking is county-specific and lives in the playbook's own data
shard (learn.datasift.ai/county-data/<state_fips>.json), not in the page HTML.
Knox is evidence grade A: the ranking is measured off ty+2's own sold history,
so a "missing" row here is a segment this account has proven pays and is not
currently working.

Record counts come from the records-page search, POST /api/internal/property/
with an x-http-method-override: GET header. The plain GET ?lists=<uuid>
querystring is silently IGNORED and returns the whole account (50,769), which
would report every list as fully populated.

    python src/list_inventory.py                  # inventory to stdout + JSON
    python src/list_inventory.py --fips 47093     # another county
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datasift_api_upload import Api, BASE  # noqa: E402

PLAYBOOK = "https://learn.datasift.ai/county-data/%s.json"

# Framework segment key -> the list title(s) in this account that carry it.
# The playbook names segments by data flag; DataSift names lists by hand. This
# map is the join, and it is deliberately explicit: a fuzzy title match put
# "Judgements" (a typo duplicate) and "Judgments" on the same row and made an
# empty list look populated.
KEY_TO_LIST = {
    "is_absentee_owners":     ["Absentee Owners"],
    "is_free_and_clear":      ["Free & Clear"],
    "is_high_equity":         ["High Equity"],
    "is_vacant_property":     ["Vacant"],
    "is_senior_homeowners":   ["Senior Homeowners"],
    "is_low_income":          ["Low Income"],
    "is_bad_credit":          ["Low Credit Score"],
    "is_tired_landlord":      ["Tired Landlord"],
    "is_oos_owners":          ["Out-of-State"],         # created + pulled 2026-08-17
    "is_recent_deliquent":    ["Tax Delinquent"],
    "is_probate_properties":  ["Probate"],
    "is_pre_probate":         ["Pre-Probate/Deceased"],
    "is_estate_sales":        ["Estate Sales"],
    "is_bankruptcy_property": ["Bankruptcy"],
    "is_other_lien":          ["Liens"],
    "is_hoa_lien":            [],
    "is_judgment_liens":      ["Judgments", "Judgements"],
    "is_notice_of_default":   ["Pre-Foreclosures - Notice of Default"],
    "is_notice_of_foreclosure": ["Pre-Foreclosures - Notice of Foreclosure"],
    "is_lis_pendens":         ["Pre-Foreclosures - Lis Pendens"],
    "is_final_judgment":      ["Pre-Foreclosures - Final Judgement"],
    "is_zombie_property":     [],
    "is_obituary_property":   ["Obituary"],
    "investor_score":         [],                       # AI score, not a list
}


def fetch_shard(fips: str) -> dict:
    url = PLAYBOOK % fips[:2]
    with urllib.request.urlopen(url, timeout=60) as r:
        shard = json.loads(r.read())
    if fips not in shard:
        raise SystemExit("no playbook data for FIPS %s" % fips)
    return shard[fips]


def search_count(api: Api, must: dict) -> int:
    """Records matching a filter. POST + method override, like the records page."""
    body = json.dumps({"limit": 1, "offset": 0, "query": {"must": must}}).encode()
    req = urllib.request.Request(
        BASE + "/api/internal/property/", data=body, method="POST",
        headers={"Authorization": "Bearer " + api.token,
                 "Content-Type": "application/json",
                 "x-http-method-override": "GET"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read()).get("count", 0)
        except urllib.error.HTTPError as e:
            # /api/internal throttles hard; it tells you how long to wait.
            if e.code == 429 and attempt < 5:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError("HTTP %s: %s" % (e.code, e.read().decode()[:200]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fips", default="47093", help="county FIPS (default Knox TN)")
    ap.add_argument("--out", default="output/list_inventory.json")
    args = ap.parse_args()

    county = fetch_shard(args.fips)
    api = Api()

    lists = api.call("/api/internal/list/?limit=999")["results"]
    by_title = {(x.get("title") or ""): x["uuid"] for x in lists}

    # An empty filter is rejected ("Filter can't be empty"), so the account
    # total comes off the plain paginated GET instead.
    total = api.call("/api/internal/property/?limit=1").get("count", 0)
    print("Account total records: %s" % f"{total:,}")
    print("County: %s %s  |  %s  |  %s deals, baseline %s doors/deal"
          % (county["name"], county["state"], county["window"],
             county["deals"], county["baseline"]["dpd"]))
    print()

    # Count every list once, then reuse across rows (a segment can appear in
    # several combo rows and the endpoint is rate limited).
    counts: dict[str, int] = {}
    for title, uuid in sorted(by_title.items()):
        counts[title] = search_count(api, {"any_lists": [uuid]})
        print("  %-45s %8s" % (title[:45], f"{counts[title]:,}"))
        time.sleep(0.4)

    rows = []
    for r in county["rows"]:
        titles, present, missing = [], [], []
        for k in r["keys"]:
            for t in KEY_TO_LIST.get(k, []):
                titles.append(t)
                (present if counts.get(t, 0) > 0 else missing).append(t)
            if not KEY_TO_LIST.get(k):
                missing.append(k)
        rows.append({
            "seg": r["seg"], "type": r["type"], "priority": r["priority"],
            "keys": r["keys"], "dpd": r["dpd"], "lift": r["lift"],
            "deals": r["deals"], "list_size": r["list_size"],
            "share": r.get("share"), "plan": r.get("plan"),
            "conf": r.get("conf"), "caveats": r.get("caveats") or [],
            "lists": titles,
            "counts": {t: counts.get(t, 0) for t in titles},
            "covered": bool(titles) and not missing,
            "partial": bool(present) and bool(missing),
            "missing": missing,
        })

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"county": {k: v for k, v in county.items() if k != "rows"},
                   "list_counts": counts, "rows": rows}, f, indent=1)
    print("\nwrote %s (%d framework rows)" % (args.out, len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
