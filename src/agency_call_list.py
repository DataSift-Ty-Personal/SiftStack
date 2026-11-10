"""Calling-agency call list for a pool pulled with pull_by_ids.py: one row per owner, callable numbers only.

The same file the calling agency took in October (knox_blount_top15k.py phase export), for any pool and any
account: Record ID, owner and Owner 2, property and mailing address, Phone 1-10 with type (mobiles first),
Email 1-3, Segment, value / equity / specs, Vacant / Absentee / Deceased and the owner's other properties on
the list, then List Rank and both AI scores at the end so the agency's October column mapping still fits.
No Trestle and no DNC scrub on purpose: the agency scrubs the DNC registry and litigators itself. A number
comes off only for what the CRM already knows (invalid, a wrong / dead / DNC disposition, a prior litigator
tag, disconnected, a repeat). Owners the agency should not dial go to Held out with the reason: owner do not
call or opted out, an entity or nameless owner of record, a live lead, upside down.

  python src/agency_call_list.py --pool output/ai_pools/franklin/realtor.json \
      --list "Franklin OH - Realtor AI Top 20K" --target data+staging@dataflik.com --phase all --about "..."

harvest  reads every record once (resumable, about 3 a second): the full phone list is only on the record,
         a list search carries one representative number. Pool ids map to record uuids through the
         pull_by_ids state for that pool and account; a record that 404s is looked up again by id, because
         it was almost always replaced rather than deleted.
export   output/agency_call_lists/<list>_Call_List_<date>.csv, _no_phones.csv and .xlsx (Read me, Call
         list, No phones, Held out), rows in pool rank order. One row per person: the owner record, or the
         same first and last name with a number in common (one person split across two owner records).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datasift_api_upload import ApiError  # noqa: E402
import d4d_call_list as d4d  # noqa: E402
import knox_blount_top15k as kb  # noqa: E402
import pull_by_ids as pb  # noqa: E402

OUT = Path("output/agency_call_lists")
COLS = (["Record ID", "First Name", "Last Name", "Owner 2", "Property Address", "Property City",
         "Property State", "Property ZIP", "County", "Mailing Address", "Mailing City", "Mailing State",
         "Mailing ZIP"]
        + [c for n in range(1, kb.MAX_PHONES + 1) for c in ("Phone %d" % n, "Phone %d Type" % n)]
        + ["Email 1", "Email 2", "Email 3", "Segment", "Estimated Value", "Equity %", "Year Built", "Beds",
           "Baths", "SqFt", "Vacant", "Absentee Owner", "Owner Deceased", "Other Properties on This List",
           "List Rank", "Realtor AI Score", "Investor AI Score"])


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def uuid_map(args) -> dict:
    """dataflik id -> record uuid, from the pull_by_ids state for this pool and account."""
    p = Path(args.pool).resolve()
    account = args.key_env.lower() or args.target.split("@")[0].replace("+", "_")
    st = d4d._load(Path("output/pull_by_ids") / account / ("%s_%s" % (p.parent.name, p.stem)) / "state.json", {})
    m = {**(st.get("existing") or {}), **(st.get("new_uuids") or {})}
    d4d.gate(bool(m), "no pull_by_ids state for %s on this account; pull it first" % args.pool)
    return m


def phase_harvest(args, run: Path) -> None:
    api, m = pb.clients(args)
    ids = [str(x["dataflik_id"]) for x in json.loads(Path(args.pool).read_text(encoding="utf-8"))]
    um = uuid_map(args)
    cache = d4d._load(run / "records.json", {})
    todo = [um[i] for i in ids if i in um and um[i] not in cache]
    print("harvest: %d pool ids, %d with a record, %d cached, %d to read" % (
        len(ids), sum(1 for i in ids if i in um), len(cache), len(todo)), flush=True)
    t0 = time.time()
    for k, u in enumerate(todo, 1):
        try:
            cache[u] = kb._slim(d4d._call(api, "/api/internal/property/%s/" % u, min_gap=args.gap) or {})
        except ApiError as e:
            if e.code != 404:
                raise
            cache[u] = {"gone": True}  # deleted or merged since the pull: counted, never dialed
        if k % 250 == 0 or k == len(todo):
            d4d._save(run / "records.json", cache)
            rate = k / (time.time() - t0)
            print("  read %d/%d (%.1f/s, ~%d min left)" % (k, len(todo), rate, (len(todo) - k) / rate / 60),
                  flush=True)
    d4d._save(run / "records.json", cache)
    relink(args, api, m, ids, um, cache, run)


def relink(args, api, m, ids: list, um: dict, cache: dict, run: Path) -> None:
    """A record that 404s was usually replaced, not deleted (147 of 148 on the 2026-11-05 pools): look its id up
    again and read whichever record holds it now. Export follows the `now` pointer."""
    lost = [i for i in ids if i in um and (cache.get(um[i]) or {}).get("gone") and not cache[um[i]].get("now")]
    if not lost:
        return
    live, _ = pb.saved_uuids(m, lost)
    taken, found = set(um.values()), 0
    for i in lost:
        nu = live.get(i)
        if not nu or nu in taken:  # still the dead uuid, or another pool row's record: nothing new to read
            continue
        try:
            rec = kb._slim(d4d._call(api, "/api/internal/property/%s/" % nu, min_gap=args.gap) or {})
        except ApiError as e:
            if e.code != 404:
                raise
            continue
        if rec.get("dataflik_id") == i:
            cache[nu], cache[um[i]]["now"] = rec, nu
            found += 1
    d4d._save(run / "records.json", cache)
    print("harvest: %d records gone since the pull, %d found again under a new record id" % (len(lost), found),
          flush=True)


def people(r: dict, phones: list) -> set:
    """name|number keys: one person split across two DataSift owner records still has one name and a number."""
    first, last = (r.get("first") or "").strip().lower(), (r.get("last") or "").strip().lower()
    return {"%s|%s|%s" % (first, last, d) for d, _ in phones} if first and last else set()


def held_reason(r: dict) -> str:
    """knox_blount_top15k's hold rules, worded for whichever account the list lives in."""
    if r.get("dnc") or r.get("opt_out"):
        return "owner do not call / opted out"
    if not (r.get("first") and r.get("last")):
        co = r.get("company") or ""
        if not co:
            return "no owner name in the CRM"
        if kb._ENTITY.search(co) or co[:1].isdigit() or co.lower().startswith("the "):
            return "entity owner in the CRM: %s" % co
        return "person name stored as a company in the CRM: %s" % co
    if kb._church_as_person(r.get("first"), r.get("last")):
        return "entity owner in the CRM: %s %s" % (r["first"], r["last"])
    if r.get("status") in kb.LIVE_LEAD_STATUSES:
        return "live lead in the CRM (%s)" % r["status"]
    if r.get("equity") not in (None, "") and float(r["equity"]) < 0:
        return "upside down (equity under 0%)"
    if is_lot(r):
        return "a lot, not a house: no street number and no building on the record"
    return ""


def is_lot(r: dict) -> bool:
    """No street number and no building: the source parcel has no postal address (house number 0, the township
    standing in for the city), usually the owner's side lot. Its value is an AVM guess, which is how it cleared
    the buy box: 1,761 of the Franklin investor pool on 2026-11-06, 1 of the realtor pool."""
    num = re.match(r"\d+", (r.get("street") or "").strip())
    return not (num and int(num.group()) > 0) and not (r.get("sqft") or r.get("beds") or r.get("year"))


def line(x: dict) -> list:
    r, p = x["r"], x["pool"]
    ph = x["phones"][:kb.MAX_PHONES] + [("", "")] * (kb.MAX_PHONES - len(x["phones"][:kb.MAX_PHONES]))
    absentee = bool(r.get("m_street")) and (kb._addr_key(r.get("m_street")) != kb._addr_key(r.get("street"))
                                            or (r.get("m_zip") or r.get("zip")) != r.get("zip"))
    emails = [e if isinstance(e, str) else (e.get("email") or "") for e in r["emails"]][:3]
    emails += [""] * (3 - len(emails))
    return ([r["uuid"], kb._name(r.get("first")), kb._name(r.get("last")), kb._name(r.get("owner2")),
             r["street"], r["city"], r["state"], r["zip"], r["county"],
             r.get("m_street"), r.get("m_city"), r.get("m_state"), r.get("m_zip")]
            + [c for q in ph for c in q] + emails
            + [x["segment"], kb._num(r.get("value")), kb._num(r.get("equity"), 1), kb._num(r.get("year")),
               kb._num(r.get("beds")), kb._num(r.get("baths"), 1), kb._num(r.get("sqft")),
               "Yes" if r.get("vacant") else "", "Yes" if absentee else "", "Yes" if r.get("deceased") else "",
               "; ".join(x["others"]), p.get("rank"), p.get("realtor_score"), p.get("investor_off_market_score")])


def phase_export(args, run: Path) -> None:
    pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    um = uuid_map(args)
    cache = d4d._load(run / "records.json", {})
    # Pools are disjoint by property, not by owner: an owner with one house on each Franklin list would
    # be dialed twice. --not-on holds anyone already on another list's call sheet (exported first), matched
    # by owner record or by name plus a number in common.
    elsewhere, twins_elsewhere = {}, {}
    for other in args.not_on:
        for fn, into in (("owners.json", elsewhere), ("people.json", twins_elsewhere)):
            f = OUT / slug(other) / fn
            d4d.gate(f.exists(), "export %r first: --not-on needs its %s" % (other, fn))
            for k in json.loads(f.read_text(encoding="utf-8")):
                into.setdefault(k, other)
    calls, quiet, held_rows, held, drops = {}, {}, [], {}, {}
    person, alias, done = {}, {}, set()
    missing = gone = moved = merged = shared = same_address = 0
    for p in pool:  # pool rank order, so a portfolio owner is called about their best-ranked property
        u = um.get(str(p["dataflik_id"]))
        r = cache.get(u) if u else None
        if not r:
            missing += 1
            continue
        if r.get("now") in cache:  # replaced since the pull, see relink
            r, moved = cache[r["now"]], moved + 1
        if r.get("gone"):
            gone += 1
            continue
        if r["uuid"] in done:  # two pool ids landed on one record (one address): it is counted once
            shared += 1
            continue
        done.add(r["uuid"])
        seg = ("P%s %s" % (p["priority"], p["rung"])) if p.get("priority") else (p.get("rung") or "")
        key = r.get("owner_uuid") or r["uuid"]
        key = alias.get(key, key)
        phones, why_drop = kb.clean_phones(r)
        twins = people(r, phones)
        other = elsewhere.get(key) or next((twins_elsewhere[t] for t in sorted(twins) if t in twins_elsewhere), "")
        why = ("owner already on the %s call list" % other) if other else held_reason(r)
        if why:
            cat = why.split(":")[0].split(" (")[0]
            held[cat] = held.get(cat, 0) + 1
            held_rows.append({"r": r, "phones": phones, "segment": seg, "others": [], "pool": p, "why": why})
            continue
        same = None
        if key not in calls and key not in quiet:
            same = next((person[t] for t in sorted(twins) if t in person), None)
        if same:  # one person on two owner records: their numbers join the row they are already on
            alias[key], key, merged = same, same, merged + 1
            row = calls[same]["phones"]
            row += [q for q in phones if q[0] not in {d for d, _ in row}]
            row.sort(key=lambda q: q[1] != "Mobile")
            for t in twins:
                person.setdefault(t, same)
        if key in calls or key in quiet:
            x = calls.get(key) or quiet[key]
            at = "%s, %s" % (r["street"], r["city"])
            if at.lower() == ("%s, %s" % (x["r"]["street"], x["r"]["city"])).lower() \
                    or at.lower() in (o.lower() for o in x["others"]):
                same_address += 1  # a second parcel at an address the row already names
            else:
                x["others"].append(at)
            continue
        for k, n in why_drop.items():
            drops[k] = drops.get(k, 0) + n
        x = {"r": r, "phones": phones, "segment": seg, "others": [], "pool": p}
        (calls if phones else quiet)[key] = x
        for t in twins:
            person.setdefault(t, key)

    base = OUT / ("%s_Call_List_%s" % (slug(args.list_name), time.strftime("%Y-%m-%d")))
    for path, rows in ((base.with_suffix(".csv"), calls.values()),
                       (Path(str(base) + "_no_phones.csv"), quiet.values())):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            for x in rows:
                w.writerow(line(x))
    shown = [x["phones"][:kb.MAX_PHONES] for x in calls.values()]
    summary = {"list": args.list_name, "pool_ids": len(pool), "not_harvested": missing, "gone": gone,
               "found_under_new_record_id": moved, "pool_ids_on_a_shared_record": shared,
               "same_person_merged": merged, "second_parcel_same_address": same_address, "rows": len(calls),
               "properties_covered": sum(1 + len(x["others"]) for x in calls.values()),
               "no_phone_rows": len(quiet), "held": held, "phones": sum(len(s) for s in shown),
               "mobiles": sum(1 for s in shown for q in s if q[1] == "Mobile"), "phone_drops": drops,
               "rows_over_phone_cap": sum(1 for x in calls.values() if len(x["phones"]) > kb.MAX_PHONES),
               "deceased_rows": sum(1 for x in calls.values() if x["r"].get("deceased")),
               "file": str(base.with_suffix(".csv"))}
    d4d._save(run / "export_summary.json", summary)
    d4d._save(run / "owners.json", sorted(set(calls) | set(alias)))
    d4d._save(run / "people.json", sorted(person))
    write_xlsx(base.with_suffix(".xlsx"), args, [line(x) for x in calls.values()],
               [line(x) for x in quiet.values()], [[x["why"]] + line(x) for x in held_rows], summary)
    print(json.dumps(summary, indent=1))


def write_xlsx(path: Path, args, rows: list, no_phone: list, held: list, s: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    head = Font(bold=True, color=kb.NAVY)
    fill = PatternFill("solid", start_color=kb.SOFT, end_color=kb.SOFT)
    rule = Border(bottom=Side(style="thin", color=kb.LINE))
    wb = Workbook()
    ws = wb.active
    ws.title = "Read me"
    lines = [
        ("%s: call list for the calling agency" % args.list_name, True),
        ("Built %s. %s" % (time.strftime("%Y-%m-%d"), args.about), False),
        ("", False),
        ("Call list rows (one per owner)", s["rows"]),
        ("Properties those rows cover", s["properties_covered"]),
        ("Phone numbers on the list", s["phones"]),
        ("  of which mobile", s["mobiles"]),
        ("Rows with no callable number (separate sheet)", s["no_phone_rows"]),
        ("Rows held out (separate sheet, reason on each)", sum(s["held"].values())),
        ("Owner possibly deceased (flagged, still on the list)", s["deceased_rows"]),
        ("", False),
        ("Not scrubbed for litigators or the DNC registry: the agency runs that scrub before dialing.", False),
        ("Not Trestle scored on purpose. Phones are mobiles first, then DataSift's order.", False),
        ("Removed per number: wrong, dead and DNC dispositions already in the CRM, numbers DataSift marks "
         "disconnected, earlier litigator flags, invalid numbers, repeats.", False),
        ("One row per owner: a portfolio owner is called once, about their best-ranked property, with the rest "
         "in Other Properties on This List. Two owner records with the same name and a number in common count "
         "as one owner.", False),
        ("Dial in List Rank order: 1 is the strongest record on the list.", False),
        ("Record ID is the DataSift record id: send it back with each disposition.", False),
    ]
    for text, v in lines:
        if isinstance(v, bool):
            ws.append([text])
            if v:
                ws.cell(ws.max_row, 1).font = Font(bold=True, size=14, color=kb.NAVY)
        else:
            ws.append([text, v])
            ws.cell(ws.max_row, 2).number_format = "#,##0"
    ws.column_dimensions["A"].width = 95
    ws.column_dimensions["B"].width = 12
    for row in ws.iter_rows():
        row[0].alignment = Alignment(wrap_text=True, vertical="top")

    def sheet(title, header, data):
        w = wb.create_sheet(title)
        w.append(header)
        for c in w[1]:
            c.font, c.fill, c.border = head, fill, rule
        for d in data:
            w.append(d)
        w.freeze_panes = "B2"
        for i, h in enumerate(header, 1):
            w.column_dimensions[w.cell(1, i).column_letter].width = max(10, min(32, len(h) + 4))

    sheet("Call list", COLS, rows)
    sheet("No phones", COLS, no_phone)
    sheet("Held out", ["Why held"] + COLS, held)
    tmp = path.with_name(path.stem + "_PENDING_.xlsx")
    wb.save(tmp)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pool", required=True, help="pool JSON from ai_pools.py (rank order, dataflik_id)")
    ap.add_argument("--list", dest="list_name", required=True, help="the DataSift list the pool was pulled onto")
    ap.add_argument("--about", default="", help="one sentence for the Read me: what the pool is, its buy box")
    ap.add_argument("--target", default="", help="allowlisted account email, token in REISIFT_TARGET_JWT_FILE")
    ap.add_argument("--key-env", default="", help="env var holding another account's Open API key")
    ap.add_argument("--expect-email", default="")
    ap.add_argument("--phase", required=True, choices=["harvest", "export", "all"])
    ap.add_argument("--gap", type=float, default=0.3, help="harvest: minimum seconds between record reads")
    ap.add_argument("--not-on", action="append", default=[],
                    help="export: hold owners already on this list's call sheet (repeatable; export it first)")
    args = ap.parse_args()
    run = OUT / slug(args.list_name)
    run.mkdir(parents=True, exist_ok=True)
    if args.phase in ("harvest", "all"):
        phase_harvest(args, run)
    if args.phase in ("export", "all"):
        phase_export(args, run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
