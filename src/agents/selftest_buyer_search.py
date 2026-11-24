"""Buyer search selftest: Roma's buyer_search workflow end to end on a fake SiftMap.

What is checked: a DataSift record is searched from its own map point; a street line that is not a record
is placed by the geocoder and found on the map; an unknown house and a missing house number are said plainly;
the deed reads are charged to the deal workflows' daily budget and a spent budget stops the run; the lender
is set aside with its reason and never dropped; going past the top ones (start and limit) and growing the
list (a second ask for the same house reads only what the first left unread); the Slack text names the house,
the buyers and the files, carries no dash or phone number, and says nothing was written; the workbook and CSV
are written. Zero network: buyer_search.fakes.sample_siftmap() and a fake geocoder (invented names and ids).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import config, store


def _task(inputs: dict, record: str = "") -> dict:
    tid, _ = store.enqueue("sales", "buyer_search", record, subject="", force=True,
                           payload={"inputs": inputs, "details": ""}, source="agent:sales",
                           slack_channel="C0TEST", slack_thread="1700000000.000100")
    return store.task(tid)


RECORD = {"uuid": "0b0b0b0b-1111-4222-8333-444444444444", "dataflik_id": "5000", "bedrooms": 3, "bathrooms": 1,
          "sqft": 1300, "year": 1960, "estimate_value": 200000,
          "address": {"street": "100 Example St", "city": "Sampleton", "state": "TN", "postal_code": "37999",
                      "county": "Sample", "latitude": 36.0, "longitude": -84.0}}


def run(check, tmp: Path) -> None:
    import deal_buyer_match as dbm
    from buyer_search import fakes, open_api
    from .workflows import buyer_search as wf
    from .workflows import quick_screen as qs

    saved = {"siftmap": qs.SIFTMAP, "locate": wf.locate, "geocode": open_api.census_geocode, "reg": dbm.REG_DIR,
             "reads": os.environ.get("AGENTS_BUYER_SEARCH_READS")}
    houses = Path(config.RUNS_DIR) / "buyer_search_houses"
    try:
        dbm.REG_DIR = tmp / "buyer_search_no_registry"
        open_api.census_geocode = lambda address: (36.0, -84.0)
        shutil.rmtree(houses, ignore_errors=True)

        # 1. a DataSift record: its own map point, no geocoding
        qs.SIFTMAP = fakes.sample_siftmap()
        wf.locate = lambda task, inputs: (dict(RECORD), {}, "", False)
        left = qs.deed_reads_left()
        t = _task({"address": RECORD["uuid"]})
        r = wf.run(t)
        check(r["ok"], "a DataSift record is searched from its own map point: %s" % (r.get("why") or "ok"))
        names = [b["buyer_full_name"] for b in r.get("top") or []]
        check("EXAMPLE HOLDINGS LLC" in names[:2] and "DEMO MORTGAGE LLC" not in names,
              "the buyers to call lead, and the lender is not among them: %s" % names)
        check((r.get("marked") or {}).get("lender or servicer") == 1, "the lender is set aside with its reason")
        check(r.get("reads") == 9 and qs.deed_reads_left() == left - 9,
              "the 9 deed reads are charged to the deal workflows' daily budget")
        text = wf.render(t, r)
        check("Buyers for 100 Example St" in text and "Top 4 of 4 to call:" in text and "set aside" in text,
              "the Slack text names the house, how many are listed and what was set aside")
        check("11 or more" in text and "How it works:" in text, "it says 11 or more at the ceiling and how it works")
        check("TEST FLIPS LLC" in text and "holdings not read" in text,
              "a flip's seller, whose own houses were never read, says holdings not read (never a count of 1)")
        check((r.get("subject") or {}).get("lot_sf") == round(0.24 * 43560),
              "a record's lot size comes from its own SiftMap row, so both paths apply the same lot band")
        check("would pay about $128,000 (paid 65% of worth over 2 deeds)" in text
              and "would pay about $107,000 (bought for $110,000, resold for $245,000)" in text,
              "each buyer's line carries their price guess and the deal it rests on")
        check("about $125,000 here" in text and "applied to this house's $200,000 estimate" in text
              and "one more deal" in text and "Ask with an ARV so the flippers' deals price against it" in text,
              "one line says what the buyers to call paid, what each kind of deal is priced against, and the ARV")
        import inspect
        import re
        src = inspect.getsource(wf._guess) + inspect.getsource(wf.render)
        used = {x for pair in re.findall(r'b\.get\("(\w+)"\)|b\["(\w+)"\]', src) for x in pair if x}
        check(len(used) >= 10 and used <= set(wf.TOP_KEYS),
              "every field a buyer's Slack line reads is carried in the listed buyers (missing: %s)"
              % sorted(used - set(wf.TOP_KEYS)))
        no_price = wf._guess({"likely_price": 196000, "priced_deeds": [], "unpriced": "their deed covered several houses"})
        check(no_price == "would pay about $196,000 (their deed covered several houses, so the area's typical)",
              "a buyer with no usable price says why and gets the area's typical: %s" % no_price)
        r_arv = wf.run(_task({"address": RECORD["uuid"], "arv": "$300,000"}))
        t_arv = wf.render(_task({}), r_arv)
        check("applied to your ARV, $300,000" in t_arv and "would pay about $134,000 (bought for $110,000" in t_arv
              and "would pay about $131,000" in t_arv and "Ask with an ARV" not in t_arv,
              "an ARV in the ask prices the flips against it and leaves the holders on the estimate")
        check(chr(0x2014) not in text and chr(0x2013) not in text and "nothing written to DataSift" in text,
              "no dash, and it says nothing was written")
        folder = wf.files_dir(t, wf.NAME)
        check((folder / "Buyers.xlsx").is_file() and (folder / "buyers.csv").is_file(),
              "every buyer is in a workbook and a CSV")

        # 2. past the top ones: start and limit page through the ranked list (the deeds are kept, no reads)
        qs.SIFTMAP = fakes.sample_siftmap()
        r = wf.run(_task({"address": RECORD["uuid"], "start": "2", "limit": "2"}))
        ranks = [b["target_rank"] for b in r.get("top") or []]
        text = wf.render(_task({}), r)
        check(ranks == [2, 3] and "Buyers 2 to 3 of 4 to call:" in text and "ask for start 4" in text,
              "start 2, limit 2 lists buyers 2 and 3 and says how to see the rest: %s" % ranks)
        check(r.get("reads") == 0, "the house's deeds are kept between asks, so paging reads nothing")
        r = wf.run(_task({"address": RECORD["uuid"], "start": "9"}))
        check(r["ok"] and not r.get("top") and "start of 4 or lower" in wf.render(_task({}), r),
              "a start past the end says where the list ends")

        # 3. growing the list: a capped ask leaves houses unread, the next ask for the same house reads only those
        shutil.rmtree(houses, ignore_errors=True)
        qs.SIFTMAP = fakes.sample_siftmap()
        r = wf.run(_task({"address": RECORD["uuid"], "reads": "3"}))
        text = wf.render(_task({}), r)
        check(r.get("reads") == 3 and (r.get("counts") or {}).get("deeds_unread") == 6 and "still unread" in text,
              "a capped ask reads the nearest 3 and says 6 are still unread")
        qs.SIFTMAP = fakes.sample_siftmap()
        r2 = wf.run(_task({"address": RECORD["uuid"]}))
        check(r2.get("reads") == 6 and (r2.get("counts") or {}).get("deeds_unread") == 0
              and (r2.get("shown_of") or 0) > (r.get("shown_of") or 0),
              "asking again reads only the 6 left unread, and the list grows (%s to %s)"
              % (r.get("shown_of"), r2.get("shown_of")))

        # 4. a street line that is not a record: the geocoder places it, the map finds the parcel
        qs.SIFTMAP = fakes.sample_siftmap()
        wf.locate = lambda task, inputs: (None, {}, "no Sift record matches that street line", True)
        r = wf.run(_task({"address": "100 Example St, Sampleton, TN 37999", "radius": "3"}))
        check(r["ok"] and (r.get("params") or {}).get("radius_miles") == 3.0,
              "a street line that is not a record is found on the map; the radius is passed: %s"
              % (r.get("why") or "ok"))
        r = wf.run(_task({"address": "104 Example St, Sampleton, TN 37999"}))
        check(not r["ok"] and "104" in (r.get("why") or ""), "an unknown house is said plainly: %s" % r.get("why"))
        r = wf.run(_task({"address": "Example St"}))
        check(not r["ok"], "a street line without a house number is refused")
        check("Not run" in wf.render(_task({}), r), "and the Slack text says it was not run")

        # 5. a spent budget: a house already searched answers from the deeds kept for it; a new one is not run
        os.environ["AGENTS_BUYER_SEARCH_READS"] = "0"
        qs.SIFTMAP = fakes.sample_siftmap()
        r = wf.run(_task({"address": "100 Example St, Sampleton, TN 37999", "arv": "300000"}))
        t5 = wf.render(_task({}), r)
        check(r["ok"] and r.get("reads") == 0 and r.get("budget_spent") and "answers from the" in t5
              and "applied to your ARV, $300,000" in t5,
              "with no reads left, a house already searched still answers (an ARV re-ask) from its kept deeds")
        shutil.rmtree(houses, ignore_errors=True)
        qs.SIFTMAP = fakes.sample_siftmap()
        r = wf.run(_task({"address": "100 Example St, Sampleton, TN 37999"}))
        check(not r["ok"] and "deed reads are used" in r["why"], "a house never searched, with no reads left, is not run")
    finally:
        qs.SIFTMAP, wf.locate, open_api.census_geocode, dbm.REG_DIR = (saved["siftmap"], saved["locate"],
                                                                       saved["geocode"], saved["reg"])
        if saved["reads"] is None:
            os.environ.pop("AGENTS_BUYER_SEARCH_READS", None)
        else:
            os.environ["AGENTS_BUYER_SEARCH_READS"] = saved["reads"]
