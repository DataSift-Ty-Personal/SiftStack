"""What research reads first: the deed records and the tax roll (build plan 3.3).

Before a research run pays for a trace it should know three things a county already publishes:
does the owner we hold still own the house, what is the debt, and who held title along the way.
The research engine lives on another box with no browser; the Register of Deeds needs one. This
module runs HERE, on the first-to-market box, and answers in plain JSON.

    python src/rod_lookup.py --street "123 Example St" --county Knox --owner "Pat Doe" \
        --notice-file notice.txt --json

WHAT IT CAN ANSWER, AND HOW IT KNOWS.

  Owner of record today (Knox AND Blount). The county tax roll, searched by street and accepted
  only when the row's own house number and street match (the search is fuzzy and its first
  result is often another house). "Still owns" is yes when that owner carries the name research
  holds. A no is NOT proof of a wrong house: a surviving spouse on the title, an estate that
  deeded the house to the heirs, and a sale all read the same way here, which is why the answer
  carries the roll's owner instead of a verdict alone.

  The debt, from the Register of Deeds (Knox only). A foreclosure notice cites the deed of trust
  it forecloses by instrument number, and an instrument number carries its own recording date. So
  each cited instrument is fetched from that ONE day's filings, under the deed of trust type and
  then the deed types, using only the search the recorder pull already proved (a document type
  and a date window). For a deed of trust that gives the borrower, the lender, the original
  amount, and whether a release naming that instrument is in our own release index.

  The chain of title, as far as the notice cites it. A notice usually cites the vesting deed too
  ("being the same property conveyed to ... by deed of record in Instrument No. ..."). Each cited
  deed found gives one link: who conveyed to whom, and when.

WHAT IT CANNOT ANSWER, said in the answer itself (`not_checked`). An instrument the notice does
not cite, a second loan taken later for example, can only be found by searching the recorder by
NAME. That search was never proven on this account's session, and the recorder returns nothing,
silently, for a request body it does not like. Rather than guess a field name and report "no
second loan" from an ignored filter, this says the second loan was not checked.

BLOUNT gets the tax roll half only. Blount County has no online recorder, so a Blount answer
carries the owner of record and the delinquent county tax, and says the deed records were not
read.

    ROD_LOOKUP_MAX_FETCHES=6      one-day recorder fetches for a single lookup
    KNOX_ROD_DOT_LABEL=TD         the deed of trust's label in the recorder's doc-type tree
    ROD_DEED_LABELS=WD,QCD,TRS    deed types tried for a cited instrument that is not a deed of trust
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_INSTRUMENT = re.compile(r"(?<!\d)(?:19|20)\d{13}(?!\d)")


def cited_instruments(notice_text: str) -> list[str]:
    """Every Register of Deeds instrument number a notice cites, in the order it cites them."""
    out = []
    for m in _INSTRUMENT.finditer(notice_text or ""):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


def deed_labels() -> list[str]:
    raw = os.environ.get("ROD_DEED_LABELS") or "WD,QCD,TRS"
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


def _max_fetches() -> int:
    try:
        return max(1, int(os.environ.get("ROD_LOOKUP_MAX_FETCHES") or 6))
    except ValueError:
        return 6


def released(instrument: str, scratch: str) -> bool | None:
    """True when a release naming this instrument is in our release index, False when the index
    exists and does not name it, None when there is no index to ask.

    False is weaker than it sounds: the index holds the releases the daily pull has collected
    (and the twelve months gathered in August 2026), not every release ever recorded."""
    path = os.path.join(scratch or "", "lien_releases.json")
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return None
    return instrument in set(got.get("released_instruments") or [])


def find_instrument(instrument: str, fetch_rows, labels: list[str], budget: list) -> dict | None:
    """One cited instrument, looked for under each doc type on the day its number names.

    `budget` is a one-item list holding the fetches left, shared across a lookup, so one notice
    citing six instruments cannot turn into eighteen requests."""
    import knox_appointments as ka

    day = ka.instrument_day(instrument)
    if not day:
        return None
    for label in labels:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        found = ka._parties(fetch_rows(label, day, day)).get(instrument)
        if found:
            return dict(found, doc_type=label)
    return None


def roll_owner(street: str, county: str = "Knox", search=None, bills=None) -> dict | None:
    """The tax roll's row for this street: owner, account, and what is truly delinquent.

    None when the county has no parcel AT this address. Knox and Blount (same vendor)."""
    from address_fallback import street_key
    from tax_enricher import TAX_API_BY_COUNTY, _roll_street_key, _street_queries

    base = TAX_API_BY_COUNTY.get((county or "").strip().lower())
    want = street_key((street or "").split(",")[0])
    if not base or not want[0]:
        return None
    if search is None:
        import requests
        from urllib.parse import quote

        def search(query):
            r = requests.get("%s/parcels/%s?detail_level=public&start=0&length=10" % (base, quote(query)), timeout=15)
            if r.status_code == 404:
                return []
            r.raise_for_status()
            return r.json().get("parcels", []) or []
    if bills is None:
        from tax_enricher import _knox_lookup_by_parcel

        def bills(account):
            return _knox_lookup_by_parcel(str(account), base)
    for query in _street_queries(street):
        try:
            parcels = search(query)
        except Exception:  # noqa: BLE001
            continue
        for p in parcels:
            if not p.get("account_number") or _roll_street_key(p.get("parcel_address") or "") != want:
                continue
            got = bills(p["account_number"]) or {}
            return {"owner": (p.get("owner") or "").strip(), "account": str(p["account_number"]),
                    "address": (p.get("parcel_address") or "").strip(),
                    "delinquent_amount": got.get("delinquent_amount"),
                    "delinquent_years": got.get("delinquent_year_list") or []}
    return None


def still_owns(asked_owner: str, roll: dict | None) -> tuple[bool | None, str]:
    """(verdict, why). None when it cannot be told."""
    if not roll:
        return None, "the tax roll has no parcel at this street"
    if not (asked_owner or "").strip():
        return None, "no owner name was given to compare"
    import knox_appointments as ka
    from property_lookup import _owner_matches_decedent

    owner = roll.get("owner") or ""
    if ka.name_in_owner(asked_owner, owner) or _owner_matches_decedent(asked_owner, owner):
        return True, "the tax roll's owner carries this name"
    return False, ("the tax roll shows %s. A surviving spouse on the title, an estate transfer and a sale "
                   "all read this way" % (owner or "no owner name"))


def lookup(street: str, county: str = "Knox", owner: str = "", notice_text: str = "", scratch: str = "",
           fetch_rows=None, roll=None, use_recorder: bool = True) -> dict:
    """Everything this box can read about one house before research spends anything."""
    county = (county or "Knox").strip().title()
    out: dict = {"street": street, "county": county, "asked_owner": owner, "read_on": date.today().isoformat(),
                 "deeds_of_trust": [], "chain": [], "not_checked": [], "sources": []}

    row = roll if roll is not None else roll_owner(street, county)
    out["roll"] = row
    out["still_owns"], out["still_owns_why"] = still_owns(owner, row)
    if row:
        out["sources"].append("%s County tax roll" % county)
    else:
        out["not_checked"].append("county tax: no parcel was found at this street on the %s County roll" % county)

    cited = cited_instruments(notice_text)
    out["cited_instruments"] = cited
    if county != "Knox":
        out["not_checked"].append("deed records: %s County has no online recorder, so the debt and the chain of "
                                  "title were not read" % county)
        return out
    if not use_recorder:
        out["not_checked"].append("deed records: the recorder was not asked on this run")
        return out
    if not cited:
        out["not_checked"].append("deed records: the notice text cites no instrument number, and the recorder "
                                  "can only be read here by instrument")
        return out

    import knox_appointments as ka

    if fetch_rows is None:
        import knox_rod

        fields, headers = knox_rod.capture_cached()

        def fetch_rows(label, d_from, d_to):
            return knox_rod.fetch_type(fields, headers, label, d_from, d_to, sleep=2.0)
    budget = [_max_fetches()]
    labels = [ka.dot_label()] + deed_labels()
    missed = []
    for instrument in cited:
        found = find_instrument(instrument, fetch_rows, labels, budget)
        if not found:
            missed.append(instrument)
            continue
        if found["doc_type"] == ka.dot_label():
            out["deeds_of_trust"].append({
                "instrument": instrument, "recorded": found.get("recorded") or "",
                "borrowers": found.get("grantors") or [], "lenders": found.get("grantees") or [],
                "amount": found.get("consideration"), "released": released(instrument, scratch),
                "parcel_ids": found.get("prp_ids") or []})
        else:
            out["chain"].append({
                "instrument": instrument, "doc_type": found["doc_type"], "recorded": found.get("recorded") or "",
                "from": found.get("grantors") or [], "to": found.get("grantees") or [],
                "amount": found.get("consideration")})
    out["chain"].sort(key=lambda link: link["recorded"])
    out["sources"].append("Knox County Register of Deeds index")
    open_loans = [d for d in out["deeds_of_trust"] if d["released"] is not True and d["amount"]]
    out["debt_total"] = round(sum(float(d["amount"]) for d in open_loans), 2) if open_loans else None
    if missed:
        out["not_checked"].append("%d cited instrument(s) were not found on the day their number names, or the "
                                  "lookup limit was reached: %s" % (len(missed), ", ".join(missed[:4])))
    out["not_checked"].append("a second loan or a later deed that the notice does not cite: the recorder can "
                              "only be read here by instrument number")
    if any(d["released"] is None for d in out["deeds_of_trust"]):
        out["not_checked"].append("releases: no release index is on this box, so a paid-off loan cannot be told apart")
    return out


def summary(answer: dict) -> str:
    """The answer as one short note for a record. Plain sentences, no bare dates on their own line."""
    bits = []
    roll = answer.get("roll") or {}
    if roll:
        bits.append("Owner of record on the %s County tax roll: %s." % (answer["county"], roll.get("owner") or "not named"))
        amount = roll.get("delinquent_amount")
        if amount:
            bits.append("Delinquent county tax $%s." % format(float(amount), ",.2f"))
    if answer.get("still_owns") is False:
        bits.append("This is not the name on the record (%s)." % answer.get("still_owns_why", ""))
    for d in answer.get("deeds_of_trust") or []:
        bits.append("Deed of trust %s recorded %s, borrower %s, lender %s%s%s." % (
            d["instrument"], d.get("recorded") or "date not read", "; ".join(d["borrowers"]) or "not named",
            "; ".join(d["lenders"]) or "not named",
            (", original amount $%s" % format(float(d["amount"]), ",.2f")) if d.get("amount") else "",
            {True: ", a release is on file", False: ", no release in our index", None: ""}[d.get("released")]))
    for link in answer.get("chain") or []:
        bits.append("%s %s recorded %s: %s to %s." % (
            link["doc_type"], link["instrument"], link.get("recorded") or "date not read",
            "; ".join(link["from"]) or "not named", "; ".join(link["to"]) or "not named"))
    if answer.get("not_checked"):
        bits.append("Not checked: " + "; ".join(answer["not_checked"]) + ".")
    return "Deed records check: " + " ".join(bits) if bits else ""


# ── the daily stage: put the answer on today's Knox foreclosures ──────


DONE = "rod_lookup_done.json"


def annotate(rows: list[dict], scratch: str, api=None, commit: bool = False, limit: int = 15,
             fetch_rows=None, roll_for=None, sleep=time.sleep) -> dict:
    """Run the lookup for the day's Knox foreclosure rows and put the answer on each record.

    `rows` are upload rows (the notice file's own columns). A row is done once: its Source URL is
    remembered, so tomorrow's run does not pay the recorder for it again. Writes a note and the
    debt field only with `commit`; a dry run reports what it would write.

    Needs the notice text on the row (FTM_NOTICE_TEXT=field or notes), because the instrument
    numbers are in the text and nowhere else."""
    import datasift_api_upload as up

    done_path = os.path.join(scratch, DONE)
    try:
        with open(done_path, encoding="utf-8") as fh:
            done = set(json.load(fh))
    except (OSError, ValueError):
        done = set()
    stats = {"candidates": 0, "looked_up": 0, "with_debt": 0, "written": 0, "no_text": 0, "not_found": 0,
             "left_for_tomorrow": 0, "failed": 0}
    todo = []
    for r in rows:
        if (r.get("Notice Type") or "").lower() != "foreclosure" or (r.get("County") or "").lower() != "knox":
            continue
        key = (r.get("Source URL") or r.get("Property Street Address") or "").strip()
        if not key or key in done:
            continue
        stats["candidates"] += 1
        if not (r.get("Notice Text") or "").strip():
            stats["no_text"] += 1
            continue
        todo.append((key, r))
    idx = None
    for key, r in todo:
        if stats["looked_up"] >= limit:
            stats["left_for_tomorrow"] += 1
            continue
        stats["looked_up"] += 1
        owner = ("%s %s" % (r.get("Owner First Name") or "", r.get("Owner Last Name") or "")).strip()
        try:
            answer = lookup(r.get("Property Street Address") or "", "Knox", owner, r.get("Notice Text") or "",
                            scratch, fetch_rows=fetch_rows, roll=roll_for(r) if roll_for else None)
        except Exception:  # noqa: BLE001  (left open: tomorrow's run asks again)
            stats["failed"] += 1
            continue
        note = summary(answer)
        debt = answer.get("debt_total")
        if debt:
            stats["with_debt"] += 1
        if not commit:
            continue                               # a dry run settles nothing and writes nothing
        api = api or up.Api()
        uuid = up.find_property(api, {"street": r.get("Property Street Address") or "",
                                      "postal_code": r.get("Property ZIP Code") or ""})
        if not uuid:
            stats["not_found"] += 1
            continue
        try:
            if note:
                api.call("/api/internal/property/%s/add-notes/" % uuid, "POST", {"notes": note[:1900]})
            if debt:
                idx = idx or up.field_index(api)
                label = up.field_map().get("Mortgage Debt")
                if label in idx:
                    up.write_fields(api, uuid, [{"field_uuid": idx[label]["uuid"], "value": str(debt)}], set())
            stats["written"] += 1
            done.add(key)
        except Exception:  # noqa: BLE001
            stats["failed"] += 1
        sleep(0.2)
    if commit:
        tmp = done_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(done), fh)
        os.replace(tmp, done_path)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--street", required=True)
    ap.add_argument("--county", default="Knox")
    ap.add_argument("--owner", default="")
    ap.add_argument("--notice-file", default="", help="a text file holding the notice, for its instrument numbers")
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    ap.add_argument("--no-recorder", action="store_true", help="tax roll only (no Register of Deeds login)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    text = ""
    if a.notice_file:
        with open(a.notice_file, encoding="utf-8") as fh:
            text = fh.read()
    answer = lookup(a.street, a.county, a.owner, text, a.scratch, use_recorder=not a.no_recorder)
    print(json.dumps(answer, indent=1) if a.json else (summary(answer) or "nothing found"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
