"""SOI stage 2c: reverse-search normalized contacts against the metro owners DB.

County rolls store people as "LAST FIRST M" (co-owners as "... & FIRST [LAST]").
The matcher scans all owner rows once, keeps only rows sharing a surname token with
the roster, then scores first-name/nickname/middle/suffix agreement per contact.

Same-name collisions are grouped by distinct owner-name string: one person owning
five parcels is a portfolio (good signal), five different "JOHN SMITH" strings is
ambiguity (needs email/skip-trace disambiguation before spending on stage 3).

Usage:
    python src/soi_owner_match.py
    python src/soi_owner_match.py --contacts output/soi_contacts_normalized.json --min-score 2.5
"""

import argparse
import csv
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path("output/soi/owners.db")

# Informal contact names vs legal names on deeds. Both directions are applied.
NICKNAMES = {
    "mike": ("michael",), "nick": ("nicholas",), "chuck": ("charles",),
    "charlie": ("charles",), "brad": ("bradley",), "zach": ("zachary",),
    "matt": ("matthew",), "dave": ("david",), "dan": ("daniel",), "danny": ("daniel",),
    "jim": ("james",), "jimmy": ("james",), "bob": ("robert",), "rob": ("robert",),
    "bobby": ("robert",), "bill": ("william",), "will": ("william",), "billy": ("william",),
    "tom": ("thomas",), "tony": ("anthony",), "steve": ("steven", "stephen"),
    "stephen": ("steven",), "jeff": ("jeffrey", "jeffery"), "jeffery": ("jeffrey",),
    "greg": ("gregory",), "ken": ("kenneth",), "kenny": ("kenneth",),
    "rick": ("richard",), "rich": ("richard",), "dick": ("richard",),
    "joe": ("joseph",), "joey": ("joseph",), "josh": ("joshua",),
    "chris": ("christopher", "christine", "christina"), "kate": ("katherine", "kathryn"),
    "katie": ("katherine", "kathryn"), "kathy": ("katherine", "kathleen"),
    "kat": ("katherine",), "beth": ("elizabeth",), "liz": ("elizabeth",),
    "betsy": ("elizabeth",), "libby": ("elizabeth",), "becky": ("rebecca",),
    "jen": ("jennifer",), "jenny": ("jennifer",), "jess": ("jessica",),
    "sam": ("samuel", "samantha"), "andy": ("andrew",), "drew": ("andrew",),
    "ed": ("edward",), "eddie": ("edward",), "ted": ("theodore", "edward"),
    "pat": ("patrick", "patricia"), "patty": ("patricia",), "trish": ("patricia",),
    "ron": ("ronald",), "ronnie": ("ronald",), "don": ("donald",), "donnie": ("donald",),
    "tim": ("timothy",), "pete": ("peter",), "frank": ("francis", "franklin"),
    "fred": ("frederick",), "walt": ("walter",), "ray": ("raymond",),
    "larry": ("lawrence",), "jerry": ("gerald", "jerome"), "gerry": ("gerald",),
    "terry": ("terrence", "terrance"), "stan": ("stanley",), "hank": ("henry",),
    "jack": ("john",), "johnny": ("john",), "jon": ("jonathan",),
    "abby": ("abigail",), "gabby": ("gabrielle",), "maggie": ("margaret",),
    "meg": ("margaret", "megan"), "peggy": ("margaret",), "sue": ("susan",),
    "suzie": ("susan", "suzanne"), "debbie": ("deborah", "debra"),
    "deb": ("deborah", "debra"), "cindy": ("cynthia",), "sandy": ("sandra",),
    "mandy": ("amanda",), "vicky": ("victoria",), "vicki": ("victoria",),
    "tori": ("victoria",), "angie": ("angela",), "carol": ("caroline", "carolyn"),
    "vince": ("vincent",), "gus": ("augustus",), "al": ("albert", "alan", "allen"),
    "bert": ("albert", "robert"), "cliff": ("clifford",), "doug": ("douglas",),
    "gene": ("eugene",), "herb": ("herbert",), "les": ("leslie", "lester"),
    "lou": ("louis",), "marty": ("martin",), "mel": ("melvin", "melissa"),
    "norm": ("norman",), "phil": ("philip", "phillip"), "russ": ("russell",),
    "sid": ("sidney",), "wes": ("wesley",), "kim": ("kimberly",),
    "brianna": ("briana",), "bri": ("briana", "brianna"), "alex": ("alexander", "alexandra"),
    "nate": ("nathan", "nathaniel"), "nathaniel": ("nathan",), "cam": ("cameron",),
    "ben": ("benjamin",), "max": ("maxwell",), "gabe": ("gabriel",),
    "leo": ("leonard", "leonardo"), "art": ("arthur",), "bernie": ("bernard",),
    "cal": ("calvin",), "dom": ("dominic",), "manny": ("manuel",),
    "mickey": ("michael",), "shelly": ("michelle",), "tammy": ("tamara",),
    "toni": ("antoinette",), "christy": ("christina", "christine"),
    "christie": ("christina", "christine"), "tina": ("christina", "christine"),
    "nikki": ("nicole", "nichole", "nicola"), "nicki": ("nicole", "nichole"),
    "missy": ("melissa",), "missie": ("melissa",), "jodi": ("jody", "joann"),
    "beckie": ("rebecca",), "randy": ("randall", "randolph"), "brandi": ("brandy",),
    "jamie": ("james",), "erin": ("erin",), "stana": ("stanislava",),
}

SUFFIX_TOKENS = {"JR", "SR", "II", "III", "IV", "V"}
NON_ALPHA = re.compile(r"[^A-Z ]")


def name_tokens(s):
    return [t for t in NON_ALPHA.sub(" ", (s or "").upper()).split() if t]


def variants(first):
    f = first.lower()
    out = {f}
    out.update(NICKNAMES.get(f, ()))
    for k, formals in NICKNAMES.items():
        if f in formals:
            out.add(k)
    return {v.upper() for v in out}


def load_contacts(path):
    contacts = json.load(open(path, encoding="utf-8"))
    out = []
    for c in contacts:
        if {"business_contact", "dirty_name"} & set(c.get("flags", [])):
            continue
        if not c.get("first_name") or not c.get("last_name"):
            continue
        surnames = {c["last_name"].upper().replace(" ", "")}
        surnames |= {t for t in name_tokens(c["last_name"]) if len(t) >= 3}
        # A Facebook middle token is often a maiden name: search it as a surname too.
        for t in name_tokens(c.get("middle_name", "")):
            if len(t) >= 4:
                surnames.add(t)
        c["_surnames"] = surnames
        c["_firsts"] = variants(c["first_name"])
        c["_middles"] = set(name_tokens(c.get("middle_name", "")))
        c["_suffix"] = c.get("suffix", "").upper().replace(".", "")
        out.append(c)
    return out


def scan_owners(needed_surnames):
    """One pass over the DB; keep rows sharing any needed surname token."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    hits = defaultdict(list)  # surname -> [row dict]
    for row in con.execute("SELECT * FROM owners"):
        toks = set(name_tokens(row["owner1"])) | set(name_tokens(row["owner2"]))
        common = toks & needed_surnames
        if not common:
            continue
        rec = dict(row)
        rec["_tokens"] = toks
        rec["_first_tok"] = name_tokens(row["owner1"])[:1]
        for s in common:
            hits[s].append(rec)
    con.close()
    return hits


def score_candidate(contact, row):
    toks = row["_tokens"]
    first_hit = contact["_firsts"] & toks
    if not first_hit:
        return 0.0, ""
    score = 2.0 if contact["first_name"].upper() in first_hit else 1.5
    reason = ["first_exact" if contact["first_name"].upper() in first_hit else "first_nickname"]
    if contact["_middles"] & toks:
        score += 0.75
        reason.append("middle")
    else:
        # middle initial: county rolls store it as a single letter token
        for m in contact["_middles"]:
            if m[:1] in toks and len(m) > 1:
                score += 0.4
                reason.append("middle_initial")
                break
    if contact["_suffix"] and contact["_suffix"] in SUFFIX_TOKENS and contact["_suffix"] in toks:
        score += 0.5
        reason.append("suffix")
    if row["_first_tok"] and row["_first_tok"][0] in contact["_surnames"]:
        score += 0.5
        reason.append("surname_leads")
    if row["owner_occupied"]:
        score += 1.0
        reason.append("owner_occupied")
    return score, "+".join(reason)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contacts", default="output/soi_contacts_normalized.json")
    ap.add_argument("--min-score", type=float, default=2.5)
    ap.add_argument("--out", default="output/soi_owner_matches")
    args = ap.parse_args()

    contacts = load_contacts(args.contacts)
    needed = set().union(*[c["_surnames"] for c in contacts])
    print(f"contacts: {len(contacts)}, distinct surname tokens: {len(needed)}")
    hits = scan_owners(needed)
    print(f"owner rows sharing a roster surname: {sum(len(v) for v in hits.values())}")

    # Collect above a low working floor first; a household-pair boost may lift
    # near-misses (a spouse on the same deed as another roster contact).
    floor = min(2.0, args.min_score)
    all_cands = []
    for ci, c in enumerate(contacts):
        cands = {}
        for s in c["_surnames"]:
            for row in hits.get(s, []):
                sc, why = score_candidate(c, row)
                if sc >= floor and (row["id"] not in cands or cands[row["id"]][0] < sc):
                    cands[row["id"]] = (sc, why, row)
        all_cands.append(cands)

    row_contacts = defaultdict(set)
    for ci, cands in enumerate(all_cands):
        for rid in cands:
            row_contacts[rid].add(ci)
    for ci, cands in enumerate(all_cands):
        for rid, (sc, why, row) in list(cands.items()):
            others = {o for o in row_contacts[rid]
                      if o != ci and contacts[o]["first_name"].upper() != contacts[ci]["first_name"].upper()}
            if others:
                cands[rid] = (sc + 0.75, why + "+household_pair", row)

    results = []
    for ci, c in enumerate(contacts):
        ranked = sorted((v for v in all_cands[ci].values() if v[0] >= args.min_score),
                        key=lambda x: -x[0])
        distinct_names = {r["owner1"] for _, _, r in ranked}
        if not ranked:
            status = "none"
        elif len(distinct_names) == 1:
            status = "unique"
        elif len(distinct_names) <= 4:
            status = "ambiguous"
        else:
            status = "too_common"
        results.append({
            "contact": {k: c[k] for k in ("first_name", "middle_name", "last_name", "suffix",
                                          "email", "emails", "sources", "flags")},
            "status": status,
            "n_parcels": len(ranked),
            "n_distinct_owner_names": len(distinct_names),
            "candidates": [{
                "score": sc, "why": why,
                **{k: r[k] for k in ("county", "parcel_id", "owner1", "owner2", "situs_addr",
                                     "situs_city", "situs_zip", "market_total", "sale_date",
                                     "sale_price", "year_built", "land_use", "owner_occupied")},
            } for sc, why, r in ranked[:8]],
        })

    out_base = Path(args.out)
    with open(f"{out_base}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)

    with open(f"{out_base}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["first_name", "last_name", "email", "sources", "status", "n_parcels",
                    "n_distinct_owner_names", "best_score", "why", "county", "owner1", "owner2",
                    "situs_addr", "situs_city", "situs_zip", "market_total", "sale_date",
                    "sale_price", "year_built", "owner_occupied"])
        for r in results:
            c = r["contact"]
            best = r["candidates"][0] if r["candidates"] else {}
            w.writerow([c["first_name"], c["last_name"], c["email"], "; ".join(c["sources"]),
                        r["status"], r["n_parcels"], r["n_distinct_owner_names"],
                        best.get("score", ""), best.get("why", ""), best.get("county", ""),
                        best.get("owner1", ""), best.get("owner2", ""), best.get("situs_addr", ""),
                        best.get("situs_city", ""), best.get("situs_zip", ""),
                        best.get("market_total", ""), best.get("sale_date", ""),
                        best.get("sale_price", ""), best.get("year_built", ""),
                        best.get("owner_occupied", "")])

    counts = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1
    print(dict(counts))
    oo = sum(1 for r in results if r["candidates"] and r["candidates"][0]["owner_occupied"])
    print(f"best candidate owner-occupied: {oo}")
    print(f"wrote {out_base}.csv / .json")


if __name__ == "__main__":
    main()
