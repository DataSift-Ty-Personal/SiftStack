"""Deep prospecting v5 on one CRM record (build 1.0.58, 2026-10-27).

dream100_dp.py runs the v5 chain over the top of the Dream 100 ranking. This runs the same chain
on any single ty+2 record found by address, and adds the step an heir case needs: every person
the research names as a decision maker is bulk-traced through SmartSkip as a SUBJECT of their
own, at the address SmartSkip returned for them, so each heir comes back with their own numbers
rather than whatever rode along on the owner's relative list.

    python src/deep_prospect_record.py --address "123 Example St" --phase records
    python src/deep_prospect_record.py --address "123 Example St" --phase smartskip [--commit]
    python src/deep_prospect_record.py --address "123 Example St" --phase merge
        the researcher writes research/<slug>.json and analysis/<slug>.json
    python src/deep_prospect_record.py --address "123 Example St" --phase heirs [--commit]
    python src/deep_prospect_record.py --address "123 Example St" --phase merge
    python src/deep_prospect_record.py --address "123 Example St" --phase score [--commit]
    python src/deep_prospect_record.py --address "123 Example St" --phase verify [--commit]
    python src/deep_prospect_record.py --address "123 Example St" --phase build
    python src/deep_prospect_record.py --address "123 Example St" --phase writeback [--commit]

The phases are dream100_dp's own wherever they can be, so its v5 rules hold unchanged: SmartSkip
deaths are advisory, the spouse-obituary trap, the owner rule, no names in phone tags, phones
upserted by number. What differs, each on purpose:
  * the county tax lookup goes by street line. The CRM stores the parcel as "107gb031" and the
    county search is fuzzy: it answers that string with 22 unrelated parcels, the first of which
    would have been reported as the title owner.
  * heirs never traces a name without a street, because a name plus city search returns
    whichever namesake ranks first (see deal_buyer_dp).
  * heirs drops the obituary's middle names unless the provider is known to hold them: on
    the first run three of four children came back empty with their middle names and
    all three matched without. The owner matched WITH his middle name because
    the county title carries it.
  * verify runs Trestle Real Contact on the numbers that reach a decision maker, so the dial
    sheet says whose number it is rather than whose relative list it rode in on.
  * writeback also puts the fresh tier on numbers already on the record, sending the full phone
    object the way d4d_call_list does, since upsert-phones replaces by number.
  * the pack and the note claim no Dream 100 rank, deal size or D4D score, and the note carries
    the heir map, since the message board is the copy a caller actually reads.
  * build leaves heir_shares.json in the run folder: each heir's share when the owner has died
    and more than one person signs (an offer sheet reads it).

What may be written back is decided in agents/research/writeback.py, the same rules for this path
and for dream100_dp: Dial First and Second numbers with Third as a reserve, a decision maker only
when verified, and the newer writes (level, heir tag, counter, report, owner change) each behind
its own switch.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import dream100_dp as dp  # noqa: E402

TRACE_ROLES = {"owner", "signer", "decision maker"}
REAL_CONTACT = "https://api.trestleiq.com/1.1/real_contact"
SS_COLS = ["First Name", "Middle Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State",
           "Mailing Zip", "Property Address", "Property City", "Property State", "Property Zip"]
CFG = {"subject_address": "mailing"}

_PACK_MD, _BUILD_NOTE, _TAX = dp.pack_md, dp.build_note, dp._tax
_SS_ROWS, _ASSEMBLE, _WRITEBACK_PLAN = dp.smartskip_rows, dp.assemble, dp.writeback_plan


# ------------------------------------------------------------------- input --

def resolve(address: str) -> dict:
    """The CRM record for an address, shaped like a Dream 100 row. Exactly one match or stop."""
    crm = dp._crm_clients()[0]
    hits = crm.search_by_address(address, limit=5)
    dp.gate(len(hits) == 1, "%d CRM records match %r; pass the full street line" % (len(hits), address))
    return row_for(hits[0]["uuid"], crm)


def row_for(uuid: str, crm=None) -> dict:
    """The Dream 100 row shape for a record already known by uuid (a webhook hands us the uuid)."""
    crm = crm or dp._crm_clients()[0]
    rec = crm.get_record(uuid)
    a, ow = rec.get("address") or {}, rec.get("owner") or {}
    ma = ow.get("address") or {}
    street = a.get("street") or ""
    m = re.match(r"\s*(\d+)\s+(\S+)", street)
    return {"uuid": uuid, "dataflik_id": rec.get("dataflik_id"), "owner_uuid": ow.get("uuid"),
            "street": street, "city": a.get("city"), "zip": (a.get("postal_code") or "")[:5],
            "lat": a.get("latitude"), "lon": a.get("longitude"), "parcel_id": rec.get("parcel_id") or "",
            "tax_query": ("%s %s" % m.groups()).upper() if m else street.upper(),
            "owner": ow.get("company") or " ".join(x for x in (ow.get("first_name"), ow.get("last_name")) if x),
            "entity": bool(ow.get("company")),
            "mail_street": ma.get("street"), "mail_city": ma.get("city"), "mail_state": ma.get("state"),
            "mail_zip": (ma.get("postal_code") or "")[:5],
            "value": rec.get("estimate_value"), "equity_pct": float(rec.get("equity_percent") or 0),
            "beds": rec.get("bedrooms"), "baths": rec.get("bathrooms"), "sqft": rec.get("sqft"),
            "year": rec.get("year"),
            "lists": [li.get("title") if isinstance(li, dict) else li for li in rec.get("lists") or []]}


def patch(row: dict):
    """Point dream100_dp's phases at this one record."""
    dp.top_rows = lambda n: [row]

    def tax(q):
        if (q or "").strip().lower() == row["parcel_id"].strip().lower():
            q = row["tax_query"]
        return _TAX(q)

    dp._tax = tax
    dp.smartskip_rows = subject_rows
    dp.assemble = assemble
    dp.writeback_plan = writeback_plan
    dp.pack_md = pack_md
    dp.build_note = build_note


def subject_rows(recs: list[dict]) -> list[dict]:
    """dream100_dp's SmartSkip rows plus two things a single record needs: the full middle name
    off the county title (the Dream 100 parser keeps initials only), and the property as the
    trace address when the CRM mailing address is not a home."""
    rows = _SS_ROWS(recs)
    for r in rows:
        x = next(x for x in recs if x["slug"] == r["_slug"])
        toks = re.sub(r"[^A-Z ]", " ", (x.get("title_owner") or "").upper()).split()
        if (not r["Middle Name"] and len(toks) == 3 and toks[0] == r["Last Name"].upper()
                and toks[1] == r["First Name"].upper()):
            r["Middle Name"] = toks[2].title()
        if CFG["subject_address"] == "property":
            r.update({"Mailing Address": r["Property Address"], "Mailing City": r["Property City"],
                      "Mailing State": r["Property State"], "Mailing Zip": r["Property Zip"]})
    return rows


# ------------------------------------------------------------------ phases --

def phase_records(args, run, row):
    dp.phase_records(args, run)
    x = run.load("records.json")[0]
    n = len((x.get("tax") or {}).get("parcels") or [])
    if n != 1:
        print("WARNING: the county search for %r returned %d parcels; the title owner above is the "
              "first and may not be this property" % (row["tax_query"], n))


def heir_rows(run, row) -> tuple[list[dict], list[str]]:
    """One SmartSkip row per living decision maker the analysis names, at a known street."""
    slug = dp.slug_of(row["street"])
    an = dp._analysis(run, slug)
    dp.gate(bool(an), "write analysis/%s.json first: the heir pass traces who the research names" % slug)
    pidx = dp._people_index(run).get(slug) or {}
    rows, skipped = [], []
    for p in an.get("people") or []:
        if p.get("living") != "Living" or not (p.get("trace") or p.get("role") in TRACE_ROLES):
            continue
        sp = pidx.get(p.get("ss_key") or "") or {}
        if sp.get("is_subject"):
            continue                                  # already the subject of the owner pass
        addr = p.get("trace_address") or {"street": sp.get("street"), "city": sp.get("city"),
                                          "state": sp.get("state"), "zip": sp.get("zip")}
        if not addr.get("street"):
            skipped.append("%s (no street, never traced on name and city)" % p.get("name"))
            continue
        first = p.get("trace_first") or sp.get("first") or p["name"].split()[0]
        last = p.get("trace_last") or sp.get("last") or p["name"].split()[-1]
        rows.append({"First Name": first, "Middle Name": p.get("trace_middle") or "", "Last Name": last,
                     "Mailing Address": addr["street"], "Mailing City": addr.get("city") or "",
                     "Mailing State": addr.get("state") or "", "Mailing Zip": (addr.get("zip") or "")[:5],
                     "Property Address": row["street"], "Property City": row["city"], "Property State": "TN",
                     "Property Zip": row["zip"], "_key": p.get("ss_key") or ""})
    return rows, skipped


def phase_heirs(args, run, row):
    """The second SmartSkip bulk pass: each decision maker as a subject of their own."""
    rows, skipped = heir_rows(run, row)
    # Passes accumulate. Anyone a prior heir order already matched is not paid for again, so a
    # retry (say without the obituary's middle names, which the provider may hold as initials)
    # only re-sends the misses.
    prior = run.load("smartskip_heirs.json", []) or []
    keys = run.load("smartskip_heirs_keys.json", {}) or {}
    hit = {keys.get(p["input_name"].strip().lower()) for p in prior if p.get("has_results")}
    skipped += ["%s %s (matched in an earlier heir order)" % (r["First Name"], r["Last Name"])
                for r in rows if r["_key"] and r["_key"] in hit]
    rows = [r for r in rows if not (r["_key"] and r["_key"] in hit)]
    for s in skipped:
        print("  skip %s" % s)
    dp.gate(bool(rows), "no living decision maker left to trace")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SS_COLS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
        print("  %-12s %-8s %-14s | %s, %s %s" % (r["First Name"], r["Middle Name"], r["Last Name"],
                                                 r["Mailing Address"], r["Mailing City"], r["Mailing State"]))
    body = buf.getvalue()
    digest = hashlib.sha1(body.encode()).hexdigest()[:12]
    csv_path = run.p("ss_heirs_input.csv")
    csv_path.write_text(body, encoding="utf-8")
    # SmartSkip names its rows "First Last"; this is how a married daughter traced under
    # her married name folds back onto the maiden-name record the owner pass found her under.
    keys.update({("%s %s" % (r["First Name"], r["Last Name"])).strip().lower(): r["_key"] for r in rows if r["_key"]})
    run.save("smartskip_heirs_keys.json", keys)
    ss, ps = dp._ss_module(run)
    sess = ss.Session()
    state = run.load("smartskip_heirs_state.json", {})
    if state.get("digest") != digest or not state.get("bulkSkipId"):
        entry = ss.submit(sess, str(csv_path), {})
        state = {"digest": digest, "bulkSkipId": entry["bulkSkipId"], "entities": entry.get("entities"),
                 "rows": len(rows), "paid": False, "submitted": datetime.now().isoformat(timespec="seconds")}
        run.save("smartskip_heirs_state.json", state)
    bid = state["bulkSkipId"]
    print("heirs: bulkSkipId %s, billable entities %s of %d rows" % (bid, state.get("entities"), len(rows)))
    dp.gate((state.get("entities") or 0) <= len(rows), "calculate returned more entities than rows")
    if not args.commit:
        print("DRY: nothing billed. Re-run with --commit to pay (max $%.2f)." % (len(rows) * dp.SMARTSKIP_PER_HIT))
        return
    if not state.get("paid"):
        dp.gate(ss.pay(sess, bid), "SmartSkip payment did not go through (3DS or decline); finish it in the app")
        state["paid"] = True
        run.save("smartskip_heirs_state.json", state)
    t0 = time.time()
    while True:
        items = ss.fetch_status(sess, bid)
        st = ((items[0].get("status") if items else "") or "").lower()
        print("  status=%s" % (st or "?"), flush=True)
        if st == "completed":
            break
        dp.gate(st not in ("error", "failed"), "bulk skip ended in status %s" % st)
        dp.gate(time.time() - t0 < 900, "timed out waiting; re-run later, the order is paid and saved")
        time.sleep(15)
    vert = run.p("ss_heirs_vertical_%s.csv" % bid)
    ss.download(sess, bid, str(vert), "vertical")
    ss.download(sess, bid, str(run.p("ss_heirs_horizontal_%s.csv" % bid)), "horizontal")
    parsed = ps.parse(str(vert))
    hits = sum(1 for p in parsed if p.get("has_results"))
    merged = {p["input_name"].strip().lower(): p for p in prior}
    for p in parsed:
        old = merged.get(p["input_name"].strip().lower())
        if p.get("has_results") or not (old and old.get("has_results")):
            merged[p["input_name"].strip().lower()] = p
    run.save("smartskip_heirs.json", list(merged.values()))
    run.ledger("smartskip_heirs_%s" % bid[-6:], hits * dp.SMARTSKIP_PER_HIT,
               "%d hits of %d rows, order %s" % (hits, len(rows), bid))
    for p in parsed:
        print("  %-26s own phones %2d | relatives %2d | deceased flag %s" % (
            p["input_name"][:26], len(p["subject_phones"]), len(p["relatives"]), p["deceased"]))
    print("heirs: %d hits, about $%.2f. Run --phase merge next." % (hits, hits * dp.SMARTSKIP_PER_HIT))


def _heir_meta(run) -> dict:
    out = {}
    for f in sorted(run.dir.glob("ss_heirs_vertical*.csv")):
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            key = (dp._nm(r.get("First Name")), dp._nm(r.get("Last Name")))
            m = out.setdefault(key, {"emails": [], "age": None})
            if r.get("Relationship") == "Subject" and r.get("Age"):
                m["age"] = r["Age"]
            for i in range(1, 6):
                e = (r.get("Email %d" % i) or "").lower()
                if e and e not in m["emails"]:
                    m["emails"].append(e)
    return out


def _add_phones(person: dict, phones: list, crm: dict, own: bool):
    for q in phones or []:
        n = dp.digits(q.get("number"))
        if not n:
            continue
        have = next((ph for ph in person["phones"] if ph["number"] == n), None)
        if not have:
            c = crm.get(n) or {}
            have = {"number": n, "ss_type": q.get("type") or "", "sources": ["SmartSkip"], "on_crm": bool(c),
                    "crm_status": c.get("status"), "crm_tags": c.get("tags") or []}
            person["phones"].append(have)
        if own:
            have["own_trace"] = True


def fold_heirs(run, row):
    """Each heir's own trace into people.json: their numbers onto them, their relatives added."""
    parsed = run.load("smartskip_heirs.json")
    if not parsed:
        return
    slug = dp.slug_of(row["street"])
    groups = run.load("people.json", [])
    g = next(g for g in groups if g["slug"] == slug)
    x = run.load("records.json")[0]
    crm = dp._crm_phones(x)
    meta = _heir_meta(run)
    owner_addr = ((x["record"].get("owner") or {}).get("address") or {}).get("street") or ""
    key_for = run.load("smartskip_heirs_keys.json", {}) or {}
    by_key = {v["key"]: v for v in g["people"]}

    def _age(v):
        try:
            return int(str(v).strip().lstrip("~").split("-")[0])
        except (TypeError, ValueError):
            return None

    def find(first, last, street="", age=None):
        # Same name at the same street can be two people: a father and the son named for him
        # ("Thomas Ward" 71 and 49 at one house). Ages more than 3 years apart never merge.
        c = [v for v in g["people"] if dp._nm(v["first"]) == dp._nm(first) and dp._nm(v["last"]) == dp._nm(last)
             and not (_age(age) and _age(v.get("age")) and abs(_age(age) - _age(v.get("age"))) > 3)]
        same = [v for v in c if street and dp._na(v["street"]) == dp._na(street)]
        return (same or c or [None])[0]

    def new_person(first, last, **kw):
        p = dp._person(first, last, crm=crm, owner_addr=owner_addr, prop_addr=row["street"],
                       owner_last=row["owner"].split()[-1], prop_state="TN", is_subject=False, **kw)
        if any(v["key"] == p["key"] for v in g["people"]):
            p["key"] += "|%s" % (_age(kw.get("age")) or len(g["people"]))
        g["people"].append(p)
        return p

    added = 0
    for p in parsed:
        m = meta.get((dp._nm(p["first"]), dp._nm(p["last"]))) or {}
        person = by_key.get(key_for.get(p["input_name"].strip().lower(), "")) or find(
            p["first"], p["last"], p["mailing_address"]) or new_person(
            p["first"], p["last"], age=m.get("age"), label="Traced as a decision maker", canon="",
            street=p["mailing_address"], city=p["mailing_city"], state=p["mailing_state"], zipc=p["mailing_zip"],
            phones=[], ss_deceased=p["deceased"], via=[p["input_name"]], emails=[])
        person["own_trace"] = True
        person["own_trace_deceased_flag"] = p["deceased"]
        if person.get("age") is None and str(m.get("age") or "").isdigit():
            person["age"] = int(m["age"])
        _add_phones(person, p["subject_phones"], crm, own=True)
        person["emails"] = sorted(set(person.get("emails") or []) | set(m.get("emails") or []))
        for rl in p["relatives"]:
            have = find(rl["first"], rl["last"], rl.get("mailing_street"), rl.get("age"))
            if have:
                _add_phones(have, rl["phones"], crm, own=False)
                have["via"] = sorted(set(have["via"]) | {p["input_name"]})
                continue
            q = new_person(rl["first"], rl["last"], age=rl.get("age"),
                           label="%s of %s" % (rl.get("type_raw") or "Relative", p["first"]),
                           canon=rl.get("canon_rel"), street=rl.get("mailing_street"), city=rl.get("mailing_city"),
                           state=rl.get("mailing_state"), zipc=rl.get("mailing_zip"), phones=rl.get("phones"),
                           ss_deceased=rl.get("deceased"), via=[p["input_name"]], emails=[])
            q["score_worthy"] = False
            added += 1
    run.save("people.json", groups)
    print("merge: folded %d heir traces, %d new people from their relative lists, %d people total" % (
        len(parsed), added, len(g["people"])))


def apply_tracerfy(run):
    """Tracerfy numbers onto the people they were bought for; re-applied after every merge."""
    tf = run.load("tracerfy.json")
    if not tf:
        return
    keys = {t["key"] for t in tf.get("targets") or []}
    groups = run.load("people.json", [])
    for g in groups:
        for p in g["people"]:
            hit = (tf.get("found") or {}).get(("%s %s" % (p["first"], p["last"])).lower())
            if p["key"] not in keys or not hit:
                continue
            for n in hit["numbers"]:
                if not any(q["number"] == n for q in p["phones"]):
                    p["phones"].append({"number": n, "ss_type": hit["types"].get(n, ""), "sources": ["Tracerfy"],
                                        "on_crm": False, "crm_status": None, "crm_tags": []})
            p["emails"] = sorted(set(p.get("emails") or []) | {e.lower() for e in hit.get("emails") or []})
    run.save("people.json", groups)


def phase_merge(args, run, row):
    dp.phase_merge(args, run)
    fold_heirs(run, row)
    apply_tracerfy(run)


def phase_gapfill(args, run, row):
    """Tracerfy for any living decision maker with no LIVE number, not only one with no number
    at all (dream100_dp's rule): a lone landline at activity 30 is no way to reach someone."""
    from obituary_dp_run import tracerfy_gapfill  # noqa: E402
    env = dp.load_env()
    slug = dp.slug_of(row["street"])
    an = dp._analysis(run, slug)
    scores = run.load("scores.json", {})
    dp.gate(bool(an and scores), "run --phase score --commit first: gap-fill needs tiers")
    pidx = dp._people_index(run).get(slug) or {}
    targets = []
    for ap in an.get("people") or []:
        p = pidx.get(ap.get("ss_key") or "")
        if ap.get("role") not in TRACE_ROLES or ap.get("living") != "Living" or not (p and p.get("street")):
            continue
        if any((scores.get(q["number"]) or {}).get("tier") in dp.WRITE_TIERS for q in p["phones"]):
            continue
        targets.append({"slug": slug, "key": p["key"], "first": p["first"], "last": p["last"],
                        "mailing_street": p["street"], "mailing_city": p["city"], "mailing_state": p["state"],
                        "mailing_zip": p["zip"], "phones": []})
    print("gapfill: %d decision makers with no live number: %s" % (
        len(targets), ", ".join("%s %s" % (t["first"], t["last"]) for t in targets) or "none"))
    if not targets or not args.commit:
        if targets:
            print("DRY: re-run with --commit (max $%.2f)." % (len(targets) * dp.TRACERFY_PER_REC))
        return
    found, stats = tracerfy_gapfill(targets, env.get("TRACERFY_API_KEY", ""))
    run.save("tracerfy.json", {"found": found, "stats": stats, "targets": targets})
    run.ledger("tracerfy", stats.get("cost", len(targets) * dp.TRACERFY_PER_REC), json.dumps(stats))
    apply_tracerfy(run)
    for t in targets:
        hit = found.get(("%s %s" % (t["first"], t["last"])).lower()) or {}
        print("  %s %s: %s" % (t["first"], t["last"], ", ".join(dp.fmt_phone(n) for n in hit.get("numbers") or []) or "no hit"))
    print("gapfill: %s. Run --phase score and --phase verify for the new numbers." % stats)


def _real_contact(key: str, name: str, phone: str) -> dict:
    """deal_buyer_dp's call shape. Trestle answers the browser User-Agent the rest of
    dream100_dp sends with 403 AUTHENTICATION_FAILED, which reads exactly like a bad key."""
    import requests
    h = {"x-api-key": key, "Accept": "application/json"}
    params = {"name": name, "phone": phone, "add_ons": "litigator_checks"}
    for attempt in range(4):
        try:
            r = requests.get(REAL_CONTACT, headers=h, params=params, timeout=20)
        except requests.RequestException as e:
            if attempt == 3:
                return {"error": str(e)[:200]}
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        return {"error": "HTTP %s %s" % (r.status_code, r.text[:200])}
    return {"error": "rate limited"}


def _plain(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", " ", name or "")).strip()


def verify_pairs(run, row) -> list[tuple[str, str]]:
    """(number, name) for every number that reaches a living decision maker, tested against
    the LEGAL name (a nickname like "Peggy" never matches the carrier's "Margaret"); every CRM
    number against the owner; and a CRM number nobody in the family holds against the
    candidates the analysis lists for it."""
    d = _ASSEMBLE(run)[0]
    an = d["an"]
    legal = {p.get("name"): p.get("rc_name") or _plain(p.get("name")) for p in an.get("people") or []}
    living = {p.get("name") for p in an.get("people") or [] if p.get("living") == "Living"}
    pairs = set()
    for r in d["dial"]:
        for z in r["reaches"]:
            if z["role"] in TRACE_ROLES and z["name"] in living:
                pairs.add((r["number"], legal.get(z["name"]) or _plain(z["name"])))
        if r["on_crm"]:
            pairs.add((r["number"], _plain(row["owner"])))
            if not r["reaches"]:
                for nm in an.get("crm_candidates") or []:
                    pairs.add((r["number"], _plain(nm)))
    for n, names in (an.get("verify_extra") or {}).items():
        for nm in names:
            pairs.add((dp.digits(n), _plain(nm)))
    return sorted(pairs)


def phase_verify(args, run, row):
    """Trestle Real Contact: does this number belong to someone with this name."""
    import os
    pairs = verify_pairs(run, row)
    ver = run.load("verify.json", {}) or {}
    todo = [(n, nm) for n, nm in pairs if "%s|%s" % (n, nm) not in ver]
    print("verify: %d pairs, %d cached, %d to query" % (len(pairs), len(pairs) - len(todo), len(todo)))
    if not args.commit:
        for n, nm in todo:
            print("  %s  %s" % (dp.fmt_phone(n), nm))
        print("DRY: re-run with --commit to query.")
        return
    key = os.environ.get("TRESTLE_PAID_API_KEY")
    dp.gate(bool(key), "TRESTLE_PAID_API_KEY missing")
    errors = 0
    for n, nm in todo:
        res = _real_contact(key, nm, n)
        if res.get("error"):
            errors += 1
            print("  %s %s error: %s" % (dp.fmt_phone(n), nm, res["error"][:100]))
            continue
        lit = ((res.get("add_ons") or {}).get("litigator_checks") or {}).get("phone.is_litigator_risk")
        ver["%s|%s" % (n, nm)] = {"number": n, "name": nm, "match": bool(res.get("phone.name_match")),
                                  "grade": res.get("phone.contact_grade") or "",
                                  "activity": res.get("phone.activity_score"),
                                  "linetype": res.get("phone.linetype") or "", "valid": res.get("phone.is_valid"),
                                  "litigator": bool(lit), "when": datetime.now().isoformat(timespec="seconds")}
        run.save("verify.json", ver)
    run.ledger("trestle_real_contact", 0.0, "%d Real Contact queries, %d errors (rate not published here)"
               % (len(todo) - errors, errors))
    for k in sorted(ver):
        v = ver[k]
        print("  %s  %-24s match %-5s grade %-2s activity %-4s %s" % (
            dp.fmt_phone(v["number"]), v["name"][:24], v["match"], v["grade"], v["activity"], v["linetype"]))


ROLE_RANK = {"owner": 0, "decision maker": 1, "signer": 1, "channel": 2}


def assemble(run) -> list[dict]:
    """dream100_dp's assembly plus three analysis overrides and the Real Contact verdict:
    wrong_party {number: holder} is never dialed or tiered (a CRM number scoring 100 for a
    stranger otherwise lands at the top as Dial First); also_reaches {number: [names]} adds a
    person Trestle confirmed on a line SmartSkip filed under someone else; and a number whose
    note the analysis did not write gets the name check and own-trace provenance."""
    out = _ASSEMBLE(run)
    ver = run.load("verify.json", {}) or {}
    own = set()
    for g in run.load("people.json", []):
        own |= {q["number"] for p in g["people"] for q in p["phones"] if q.get("own_trace")}
    for d in out:
        an = d["an"]
        wrong = an.get("wrong_party") or {}
        named = {p.get("name"): p for p in an.get("people") or []}
        for r in d["dial"]:
            for nm in (an.get("also_reaches") or {}).get(r["number"], []):
                ap = named.get(nm)
                if ap and not any(z["name"] == nm for z in r["reaches"]):
                    r["reaches"].append({"name": nm, "relationship": dp._short_rel(ap), "tag": ap.get("tag") or "",
                                         "role": ap.get("role"), "owner_like": False})
                    r["reaches_label"] = "; ".join(sorted({"%s (%s)" % (z["name"], z["relationship"])
                                                           if z["relationship"] else z["name"] for z in r["reaches"]}))
            if r["number"] in wrong:
                r["do_not_dial"] = True
                r["wrong_party"] = wrong[r["number"]]
        # Within a tier the analysis's call order wins, so the pack's "best number" per person is
        # the number the call order names, not the lowest of several equal mobiles.
        first = {dp.digits(c.get("number")) for c in an.get("call_order") or []}
        d["dial"].sort(key=lambda r: (r["do_not_dial"], dp.TIER_ORDER.get(r["tier"], 6), r["number"] not in first,
                                      0 if r["reaches"] else 1, -(r["score"] or 0), 0 if r["type"] == "MOBILE" else 1,
                                      r["number"]))
        for r in d["dial"]:
            hits = [v for k, v in ver.items() if k.split("|")[0] == r["number"]]
            matched = [v for v in hits if v["match"]]
            r["name_match"] = ", ".join(v["name"] for v in matched)
            if r["note"]:
                continue
            bits = []                 # short: the dial sheet's note column holds 60 characters
            if matched:
                bits.append("Trestle: %s (%s)" % (r["name_match"], matched[0]["grade"] or "?"))
            elif hits:
                bits.append("Trestle: not %s" % ", ".join(v["name"] for v in hits))
            if r["number"] in own:
                bits.append("own trace")
            if bits:
                r["note"] = "; ".join(bits)
    return out


def writeback_plan(d: dict) -> dict:
    """dream100_dp's plan, with the relationship tag taken from the highest-ranked person on a
    shared line (a decision maker before a channel) rather than the alphabetically first one."""
    plan = _WRITEBACK_PLAN(d)
    by_num = {r["number"]: r for r in d["dial"]}
    for p in plan["phones"]:
        r = by_num[p["number"]]
        if any(x["owner_like"] for x in r["reaches"]):
            continue
        rel = sorted((ROLE_RANK.get(x["role"], 3), x["tag"]) for x in r["reaches"] if x["tag"])
        if rel:
            p["tags"] = [rel[0][1]] + [t for t in p["tags"] if t == r["tier"] or t in r["sources"]]
    return plan


def pack_md(d: dict) -> str:
    """dream100_dp's pack without the Dream 100 claims: no rank, no deal size, no D4D score."""
    ctx = (d["x"]["row"].get("context") or "ty+2 record").rstrip(".")
    txt = re.sub(r"Dream 100 rank \d+ of 100\. ", ctx + ". ", _PACK_MD(d))
    txt = txt.replace("D4D visual distress None of 100. ", "")
    return "\n".join(ln for ln in txt.split("\n") if not ln.startswith("| Deal size (discount room) |"))


def build_note(d: dict) -> str:
    """The board post: dream100_dp's note with the heir map and who must sign folded in ahead of
    the call order, since the message board is what a caller reads before dialing."""
    an = d["an"]
    lines = _BUILD_NOTE(d).replace(" and the Dream 100 top 5 workbook", "").split("\n")
    extra = []
    if an.get("note_tree"):
        extra += ["Heir map:"] + ["  " + t for t in an["note_tree"]]
    if an.get("must_sign"):
        extra += ["Who must sign:"] + ["  - %s: %s. %s" % (m.get("who", ""), m.get("status", ""),
                                                          m.get("short") or m.get("why", ""))
                                      for m in an["must_sign"]]
    at = next((i for i, ln in enumerate(lines) if ln == "Call order:"), len(lines) - 1)
    return dp.no_dash("\n".join(lines[:at] + extra + lines[at:]))


def phase_build(args, run, row):
    data = dp.assemble(run)
    dp.gate(all(d["an"] for d in data), "analysis missing")
    for d in data:
        md = run.p("packs", d["slug"] + ".md")
        md.write_text(dp.pack_md(d), encoding="utf-8")
        pdf = dp.render_pdf(md, md.with_suffix(".pdf"), photo=run.p("img", d["slug"] + ".jpg"))
        note = dp.build_note(d)
        run.p("packs", d["slug"] + "_board_note.txt").write_text(note, encoding="utf-8")
        for what, text in (("pack", md.read_text(encoding="utf-8")), ("board note", note)):
            dp.gate(chr(0x2014) not in text and chr(0x2013) not in text, "%s carries an em or en dash" % what)
        # Each heir's share when more than one person signs, for an offer sheet (plan 3.9). Saved
        # even when there is none, so a file from an earlier run never outlives its research.
        run.save("heir_shares.json", dp.decision(run, d)["shares"])
        print("  pack %s\n  note %s (%d lines)" % (dp.rel(pdf),
                                                 dp.rel(run.p("packs", d["slug"] + "_board_note.txt")),
                                                 note.count("\n") + 1))


def existing_tier_writes(run) -> list[dict]:
    """Full phone objects for numbers already on the record whose fresh tier is not on them yet."""
    import d4d_call_list as d4d  # noqa: E402
    x = run.load("records.json")[0]
    scores = run.load("scores.json", {})
    wrong = dp._analysis(run, x["slug"]).get("wrong_party") or {}
    items = []
    for n, c in dp._crm_phones(x).items():
        s = scores.get(n) or {}
        tier = d4d.LITIGATOR_TAG if s.get("litigator") else s.get("tier")
        if not tier or c["status"] in dp.BAD_STATUSES or n in wrong:
            continue
        keep = [t for t in c["tags"] if t not in d4d.TIER_NAMES and t != d4d.LITIGATOR_TAG]
        if sorted(keep + [tier]) == sorted(c["tags"]):
            continue
        raw = c["raw"]
        items.append({"number": n, "type": c["type"], "tags": sorted(keep + [tier]), "status": c["status"],
                      "is_connected": raw.get("is_connected", True), "verified": raw.get("verified", False)})
    return items


def phase_writeback(args, run, row):
    dp.phase_writeback(args, run)
    items = existing_tier_writes(run)
    for p in items:
        print("  ~ %s tags -> %s (already on the record)" % (dp.fmt_phone(p["number"]), ", ".join(p["tags"])))
    if not (args.commit and items):
        return
    from datasift_api_upload import Api  # noqa: E402
    api = Api()
    # The row was read in the records phase. If the record's owner has changed since (an owner
    # change, a merge), these tags would land on someone no longer attached to this house.
    held = (api.call("/api/internal/property/%s/" % row["uuid"]) or {}).get("owner") or {}
    if held.get("uuid") and row.get("owner_uuid") and held["uuid"] != row["owner_uuid"]:
        print("  existing numbers not re-tagged: the record's owner changed since it was read. Run the records phase again.")
        return
    ok, dropped = dp._upsert_full(api, row["owner_uuid"], [dict(p) for p in items])
    time.sleep(dp.READBACK_PAUSE_S)
    rec = api.call("/api/internal/property/%s/" % row["uuid"])
    have = {dp.digits(p.get("number")): {t if isinstance(t, str) else (t.get("title") or t.get("name"))
                                         for t in p.get("tags") or []}
            for p in (rec.get("owner") or {}).get("phones") or []}
    good = sum(1 for p in items if set(p["tags"]) <= have.get(p["number"], set()))
    audit = run.load("writeback_audit.json", {}) or {}
    audit.setdefault(dp.slug_of(row["street"]), {})["existing_tiers"] = {
        "sent": len(items), "dropped": dropped, "read_back_ok": good, "ok": ok}
    run.save("writeback_audit.json", audit)
    print("  existing numbers: %d sent, %d dropped, %d of %d read back with the new tier" % (
        len(items), dropped, good, len(items)))


PHASES = {"records": phase_records, "smartskip": lambda a, r, w: dp.phase_smartskip(a, r),
          "merge": phase_merge, "heirs": phase_heirs, "gapfill": phase_gapfill,
          "score": lambda a, r, w: dp.phase_score(a, r), "verify": phase_verify, "build": phase_build,
          "writeback": phase_writeback}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deep prospecting v5 on one CRM record")
    ap.add_argument("--address", required=True, help="street line of the CRM record")
    ap.add_argument("--phase", required=True, choices=sorted(PHASES))
    ap.add_argument("--run", default="", help="run folder (default output/deep_prospect/<address slug>)")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--context", default="", help="records: what the pack header says this record is")
    ap.add_argument("--subject-address", choices=("mailing", "property"), default="",
                    help="trace the owner at the CRM mailing address or at the property (when the mailing "
                         "address is not a home)")
    ap.set_defaults(top=1, only="")
    args = ap.parse_args(argv)
    dp.load_env()
    run = dp.Run(args.run or "output/deep_prospect/" + dp.slug_of(args.address))
    row = run.load("row.json")
    if args.phase == "records" or not row:
        prior = row or {}
        row = resolve(args.address)
        row["context"] = args.context or prior.get("context") or "ty+2 record"
        row["subject_address"] = prior.get("subject_address") or "mailing"
    if args.subject_address:
        row["subject_address"] = args.subject_address
    run.save("row.json", row)
    CFG["subject_address"] = row["subject_address"]
    patch(row)
    PHASES[args.phase](args, run, row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
