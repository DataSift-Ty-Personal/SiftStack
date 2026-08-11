"""Resolve Knox lien debtors to parcels.

Liens (LEN), state tax liens (STL) and federal tax liens (FTL) are indexed
against the PERSON, not the parcel: 0% of rows carry a `PrpId:`. So the only
route to a property is the debtor name, resolved through the open Knox County
tax API (knox-tn.mygovonline.com), which searches by owner name and returns the
parcel address plus unpaid tax years.

    python src/knox_lien_resolve.py --sample 500
    python src/knox_lien_resolve.py --all --workers 6
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

TAXAPI = ("https://knox-tn.mygovonline.com/api/v2/parcels/%s"
          "?detailLevel=public&start=0&length=25")

# Business-looking debtors rarely map to single-family stock, but they are kept
# and flagged rather than dropped so the hit rate can be measured per class.
ENTITY_RE = re.compile(
    r"\b(LLC|L\.L\.C|INC|CORP|CO|COMPANY|TRUST|PROPERTIES|PARTNERS|LP|L\.P|"
    r"ASSOCIATION|BANK|GROUP|ENTERPRISES|SERVICES|CENTER|HOSPITAL|MEDICAL|"
    r"CHURCH|FOUNDATION|SYSTEMS|SOLUTIONS|HOLDINGS)\b", re.I)


def txt(x) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(x or "")).split())


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^A-Z ]", " ", (s or "").upper()).split())


def load_debtors(scratch: str) -> list[dict]:
    """Grantor side of each lien instrument is the debtor; Grantee is the creditor."""
    out: dict[str, dict] = {}
    for code in ("LEN", "STL", "FTL"):
        path = os.path.join(scratch, "rod2_%s.json" % code)
        if not os.path.exists(path):
            continue
        d = json.load(open(path, encoding="utf-8"))
        for r in d.get("aaData") or []:
            if len(r) < 11:
                continue
            if not txt(r[6]).lower().startswith("grantor"):
                continue
            name = (txt(r[4]) + " " + txt(r[5])).strip()
            if not name or len(name.split()) < 2:
                continue          # a bare surname matches far too much
            rec = out.setdefault(name, {
                "name": name, "types": set(), "instruments": set(),
                "creditors": set(), "recorded": set(),
            })
            m = re.search(r"Recorded:\s*([\d/]+)", txt(r[3]) if len(r) > 3 else "")
            if m:
                rec["recorded"].add(m.group(1))
            rec["types"].add(code)
            rec["instruments"].add(txt(r[10]))
            if txt(r[7]):
                rec["creditors"].add(txt(r[7]))
    for r in out.values():
        r["types"] = sorted(r["types"])
        r["n_liens"] = len(r["instruments"])
        r["instruments"] = sorted(r["instruments"])[:8]
        r["creditors"] = sorted(r["creditors"])[:5]
        r["is_entity"] = bool(ENTITY_RE.search(r["name"]))
        # normalise M/D/YYYY -> YYYY-MM-DD and keep the span
        iso = []
        for d in r.pop("recorded", set()):
            try:
                mm, dd, yy = d.split("/")
                iso.append("%04d-%02d-%02d" % (int(yy), int(mm), int(dd)))
            except Exception:
                pass
        iso.sort()
        r["filed_first"] = iso[0] if iso else ""
        r["filed_last"] = iso[-1] if iso else ""
    return list(out.values())


def resolve(rec: dict, timeout: int = 40) -> dict:
    url = TAXAPI % urllib.parse.quote(rec["name"])
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:50])
        rec["n_parcels"] = 0
        return rec

    toks = set(norm(rec["name"]).split())
    hits = []
    for p in (d.get("parcels") or []):
        owner_toks = set(norm(p.get("owner")).split())
        # every token must appear: the API is relevance-ranked and over-matches
        if toks and toks.issubset(owner_toks):
            hits.append({"account": p.get("account_number"),
                         "owner": p.get("owner"),
                         "address": p.get("parcel_address"),
                         "amount_due": p.get("amount_due")})
    rec["n_parcels"] = len(hits)
    rec["parcels"] = hits[:20]
    rec["tax_unpaid_years"] = sorted(
        {b.get("taxYear") for b in (d.get("bills") or [])
         if b.get("paid") is False and b.get("taxYear")})
    # A debtor holding this many parcels is an institution, not a lead.
    rec["institutional"] = len(hits) > 15
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=".")
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--out", default="output/knox_lien_parcels.json")
    a = ap.parse_args()

    debtors = load_debtors(a.scratch)
    if not debtors:
        print("no lien data found in", a.scratch)
        return 1
    print("unique lien debtors: %d (individuals %d, entities %d)"
          % (len(debtors), sum(1 for d in debtors if not d["is_entity"]),
             sum(1 for d in debtors if d["is_entity"])))

    # Sample the highest-lien-count debtors first: more liens is more distress.
    debtors.sort(key=lambda d: -d["n_liens"])
    target = debtors if a.all else debtors[:a.sample]
    print("resolving %d names with %d workers ...\n" % (len(target), a.workers))

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(resolve, r): r for r in target}
        for f in as_completed(futs):
            done += 1
            if done % 50 == 0:
                hit = sum(1 for r in target if r.get("n_parcels"))
                print("   %d/%d  resolved=%d  %.0fs"
                      % (done, len(target), hit, time.time() - t0), flush=True)

    hits = [r for r in target if r.get("n_parcels")]
    leads = [r for r in hits if not r["institutional"]]
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(target, open(a.out, "w", encoding="utf-8"), indent=1, default=list)

    print("\n%-34s %d" % ("names tried", len(target)))
    print("%-34s %d (%.0f%%)" % ("matched >=1 parcel", len(hits),
                                 100 * len(hits) / max(1, len(target))))
    print("%-34s %d" % ("after dropping institutional", len(leads)))
    print("%-34s %d" % ("total parcels", sum(r["n_parcels"] for r in leads)))
    print("%-34s %d" % ("errors", sum(1 for r in target if r.get("error"))))
    print("%-34s %.0fs" % ("elapsed", time.time() - t0))
    print("\nby lien type:", dict(Counter(t for r in leads for t in r["types"])))
    print("with unpaid county tax too:",
          sum(1 for r in leads if r.get("tax_unpaid_years")))
    print("\ntop leads (most liens):")
    for r in sorted(leads, key=lambda x: -x["n_liens"])[:10]:
        p = r["parcels"][0]
        print("  %-28s %d liens %-10s %-28s %s"
              % (r["name"][:28], r["n_liens"], "/".join(r["types"]),
                 (p["address"] or "")[:28], r.get("tax_unpaid_years") or ""))
    print("\nwrote", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
