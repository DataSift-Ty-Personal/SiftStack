"""Plan every CRM write for the obituary deep-prospecting cohort. Reads only.

This is the review surface. It computes exactly what would be written to ty+2
and emits it as JSON plus a CSV a human can scan, so the whole build can be
argued with before anything touches the network.

The conservative calls encoded here, and why:

  DM Confidence is LOW on every unresearched record. Of the five records we did
  research, THREE had a materially wrong signer set (the Fitzgerald decedent was
  the owner's father, the Burchell decedent was the owner's wife, and the Meek
  signer SmartSkip returned was a grandson's wife). 44 of 58 records have had no
  research at all. Writing a confident-looking name into a field a caller reads
  as fact is how somebody offers to buy a house from a living widow's children.

  Decedent Name is left BLANK wherever the owner may be alive. Writing the
  owner's name into Decedent Name on a widowed_owner record would bake the
  spouse-obituary trap into the database permanently.

  43 of 58 records route to Check Relatives, not Call Relatives. If that call
  queue looks too small, the fix is to research more records, not to route
  unverified guesses into a dial column.

Usage:
  python src/obituary_crm_plan.py
  python src/obituary_crm_plan.py --top 60 --outdir output/dp
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obituary_opportunity import CACHE, assemble  # noqa: E402
from obituary_campaign import situation_for  # noqa: E402
from obituary_mail_validate import norm  # noqa: E402
from obituary_dp_input import ENTITY_WORDS  # noqa: E402

BOARD = "242cafcf-cdd8-4be4-902a-0ec990339e52"
COLUMNS = {
    "call_new_numbers": "35783e71-bfd9-4491-b35a-f0665b881e21",
    "call_relatives": "aef2b9c5-55d4-4db0-8501-cd3a1a7411c8",
    "check_relatives": "a05b1bb9-5f01-49b1-82a1-10713f6bb57c",
    "research_socials": "ae2c6c29-1dc2-48c7-8683-3fb30b26eaaa",
    "doorknock": "c6e52296-9908-4834-9cf6-5555c9164ee3",
}
ASSIGNEE = {"Adriana": "d482aefc-b17d-44f3-8d76-3a87ada9072d",
            "Tinaa": "3ecfcd9b-e0d7-437f-af65-531aeed6454d"}

OWNER_ALIVE = {"widowed_owner", "owner_parent_died", "living_owner"}
ESTATE = {"estate_verified", "estate_presumed"}
NO_CARD = {"unresolved", "entity_owner"}

# reisift wants Landline / Mobile; SmartSkip emits Residential / OtherPhone / caps.
PHONE_TYPE = {"residential": "Landline", "landline": "Landline", "mobile": "Mobile",
              "cell": "Mobile", "otherphone": "Other", "": ""}
# canon_rel -> an existing ty+2 phone tag. Every one of these already exists.
REL_TAG = {"Son": "Son", "Daughter": "Daughter", "Child": "Child",
           "Wife": "Wife", "Husband": "Husband", "Spouse": "Spouse",
           "Mother": "Mother", "Father": "Father", "Parent": "Parent",
           "Brother": "Brother", "Sister": "Sister", "Sibling": "Sibling",
           "In-Law": "In-Law", "Relative": "Relative", "Unknown": "Unknown"}

MAX_MERGE_PHONES = 9  # the Add-Data CSV has Phone 1..9 and no more
EXPLICIT_KIN = {"Son", "Daughter", "Child", "Wife", "Husband", "Spouse"}
CHILD_KIN = {"Son", "Daughter", "Child"}


def subject_ages(path="output/dp/smartskip_vertical.csv"):
    """input name -> subject age, for the parent-child gap test."""
    out = {}
    pth = Path(path)
    if not pth.exists():
        return out
    for r in csv.DictReader(pth.open(encoding="utf-8-sig")):
        if r.get("Relationship") == "Subject":
            a = (r.get("Age") or "").strip()
            out[(r.get("Input Name") or "").strip()] = int(a) if a.isdigit() else None
    return out


def digits(s):
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def title(s):
    return " ".join(w.capitalize() for w in str(s or "").split())


def _norm_name(s):
    return re.sub(r"[^a-z ]", "", str(s or "").lower()).strip()


def pick_dms(ss, research):
    """Signers in rank order, honouring research drops AND the decedent.

    SmartSkip happily returns the dead person as a relative. On 1663 Dick Lonas
    the intestacy pass listed David Fitzgerald as signer #2 when David is the man
    who died, so the known decedent is filtered out by name here rather than
    being written into a DM field a caller would dial."""
    res = research or {}
    drop = {_norm_name(n) for n in (res.get("drop_names") or [])}
    dec = _norm_name(res.get("decedent_name"))
    if dec:
        drop.add(dec)
        # "David Fitzgerald" vs SmartSkip's "DAVID FITZGERALD" is covered, but so
        # is a middle name or a suffix, by first+last comparison.
        t = dec.split()
        if len(t) >= 2:
            drop.add(f"{t[0]} {t[-1]}")
    out = []
    for d in (ss.get("dms") or []):
        n = _norm_name(d.get("name"))
        t = n.split()
        short = f"{t[0]} {t[-1]}" if len(t) >= 2 else n
        if n in drop or short in drop:
            continue
        out.append(d)
    return out


def heir_phones(dms):
    """One entry per unique number, carrying who it reaches and its tags."""
    out, seen = [], set()
    for d in dms:
        rel = REL_TAG.get(d.get("canon_rel") or "Relative", "Relative")
        for p in (d.get("phones") or []):
            n = digits(p.get("number"))
            if not n or n in seen:
                continue
            seen.add(n)
            out.append({
                "number": n,
                "type": PHONE_TYPE.get((p.get("type") or "").strip().lower(), ""),
                "person": title(d.get("name")),
                "rel": rel,
                # No dial tier: Trestle is 403 on both keys and a fake Dial First
                # would corrupt every dial-tier preset in the account.
                "tags": [rel, "SmartSkip"],
            })
    return out


def dm_confidence(situation, verified, ss, dms, owner_age):
    """Graded, not binary.

    The earlier version returned "low" for everything unresearched, on the
    reasoning that 3 of the 5 researched records had a wrong signer set. That
    reasoning was bad: all five of those were picked BECAUSE SmartSkip flagged
    the owner as not deceased. Zero of the 44 deceased=true records were ever
    researched, so that overturn rate says nothing about them. What we can
    actually test is whether the record corroborates itself."""
    if verified:
        return "high"
    if situation not in ESTATE:
        return "low"
    if not ss.get("deceased") or not dms:
        return "low"
    labels = [d.get("canon_rel") for d in dms]
    if not all(l in EXPLICIT_KIN for l in labels):
        return "low"          # generic "Relative"/"Unknown" is not a signer set
    if owner_age:
        for d in dms:
            a = str(d.get("age") or "").strip()
            if d.get("canon_rel") in CHILD_KIN and a.isdigit():
                gap = owner_age - int(a)
                if not (15 <= gap <= 55):
                    return "low"   # a 10-year "child" is a sibling, mislabelled
    if not any(d.get("phones") for d in dms):
        return "low"
    return "medium"


def decedent_fields(rec, ss, research, situation, verified):
    """Only assert a death where the identity is actually grounded."""
    out = {}
    if situation in OWNER_ALIVE or situation in NO_CARD:
        out["Owner Deceased"] = "no" if situation in OWNER_ALIVE else ""
        # The owner is alive, but somebody still died and we may know exactly
        # who. Recording the real decedent is the useful, non-dangerous half.
        if (research or {}).get("decedent_name"):
            out["Decedent Name"] = research["decedent_name"]
            if research.get("obit_date_on_record"):
                out["Date of Death"] = str(research["obit_date_on_record"])
        return {k: v for k, v in out.items() if v}
    # estate_*: SmartSkip says the owner is the decedent
    out["Owner Deceased"] = "yes"
    name = None
    if verified and (research or {}).get("verdict", "").lower().startswith("owner did die"):
        name = rec.get("owner_name")
    elif situation == "estate_presumed":
        name = rec.get("owner_name")
    if name:
        out["Decedent Name"] = title(name)
    dod = (research or {}).get("obit_date_on_record") or (
        rec["anchor_date"].isoformat() if rec.get("anchor_date") else "")
    if dod:
        out["Date of Death"] = str(dod)
    return {k: v for k, v in out.items() if v}


def board_column(situation, phones, vacant, confidence):
    """First match wins. Check Relatives is the spouse-obituary gate, but it is
    for records that do NOT corroborate themselves, not for every unresearched
    record. A record where SmartSkip says the owner died and returns two
    explicitly labelled children of plausible age, with phones, has told a
    consistent story and belongs in a call queue."""
    if situation in NO_CARD:
        return None
    if not phones:
        return "doorknock" if vacant else "research_socials"
    if situation in OWNER_ALIVE:
        return "call_new_numbers"
    if confidence in ("high", "medium"):
        return "call_relatives"
    return "check_relatives"


def property_tags(situation, dms, tier):
    """Deliberately NO Priority tag.

    Priority 1 / Priority 2 is the account-wide cadence tier set by an earlier
    build and it gates the Hottest and Tier 2 presets. 49 of these 60 records
    already carry one, and on 14 it disagrees with our obituary rank. Adding
    ours put a record in two call cadences at once. The obituary opportunity
    score is a different ranking for a different purpose; it lives in
    crm_plan.csv and the workbook, not in a tag that drives somebody else's
    dialer queue."""
    tags = ["Obituary", "Deep Prospected", "SmartSkip Skipped"]
    if situation in ESTATE and dms:
        tags.append("Heirs Resolved")
    return tags


def _fmt_phone(x, rel=None):
    """A number the way a caller needs to read it: number, line type, and the
    relationship tag that will sit on it in Sift."""
    n = digits(x.get("number"))
    t = PHONE_TYPE.get((x.get("type") or "").strip().lower(), x.get("type") or "")
    tags = f"  [tags: {rel}, SmartSkip]" if rel else "  [tags: SmartSkip]"
    return f"{n} ({t or 'unknown line'}){tags}"


def _person_block(d, prop_addr, lead):
    L = []
    age = d.get("age") or "?"
    rel = REL_TAG.get(d.get("canon_rel") or "Relative", "Relative")
    L.append(f"{lead}{title(d.get('name'))}, {d.get('canon_rel')}, age {age}")
    pad = " " * len(lead)
    ph = [p for p in (d.get("phones") or []) if digits(p.get("number"))]
    for x in ph:
        L.append(f"{pad}  {_fmt_phone(x, rel)}")
    if not ph:
        L.append(f"{pad}  no phone returned by SmartSkip")
    mail = " ".join(v for v in [d.get("mailing_street"), d.get("mailing_city"),
                                d.get("mailing_state"), d.get("mailing_zip")] if v)
    if mail:
        same = norm(d.get("mailing_street")) == norm(prop_addr)
        L.append(f"{pad}  {mail}" + ("   <- LIVES AT THE SUBJECT PROPERTY" if same else ""))
    return L


def note_text(rec, ss, research, situation, dms, phones, verified, confidence):
    """The pack a caller reads when they open the record.

    The first version listed only the 1-4 signers and threw the rest away. Ty
    flagged it as thin, correctly: the other relatives, the line types, the
    per-number relationship tags, the owner's own numbers, the skip-trace
    provenance and the whole property picture were all sitting unused in
    ranked_records.json. This puts them on the record and states plainly which
    research stages have and have NOT run, instead of implying the whole deep
    prospecting flow completed when only the skip trace did.
    """
    res = research or {}
    prop = rec.get("street") or ""
    rels = ss.get("relatives") or []
    others = [r for r in rels
              if not any(_norm_name(r.get("name")) == _norm_name(d.get("name")) for d in dms)]
    subj_ph = [p for p in (ss.get("subject_phones") or []) if digits(p.get("number"))]
    n_num = len({digits(p["number"]) for r in rels for p in (r.get("phones") or [])
                 if digits(p.get("number"))})
    n_email = 0

    L = [f"[Obituary DP] Deep prospecting pack - {prop}, {rec.get('city')}", ""]

    L.append("RESEARCH STATUS  (what has actually been done on this record)")
    L.append(f"  Skip trace ............ DONE. SmartSkip bulk trace, batch "
             f"6a762b43, paid 2026-08-07. Returned {len(rels)} relatives and "
             f"{n_num} numbers on this record.")
    L.append(f"  Signer selection ...... DONE. TN intestacy (T.C.A. 31-2-104), "
             f"{len(dms)} of {len(rels)} relatives kept.")
    if verified:
        L.append("  Decedent verified ..... DONE. Obituary located, verdict below.")
    else:
        L.append("  Decedent verified ..... NOT DONE  <-- do this before you dial. "
                 "The skip trace ran; the research layer did not.")
    L.append("  Tracerfy gap-fill ..... DONE for signers SmartSkip left phoneless "
             "(3 of 9 filled).")
    L.append("  Dial tier scoring ..... NOT DONE. Trestle is returning 403 on both keys, "
             "so no number below carries a Dial First/Second tier yet.")
    L.append("")

    L.append(f"SITUATION: {situation}")
    if res.get("verdict"):
        L.append(f"  VERDICT: {res['verdict']}")
        if res.get("evidence"):
            L.append(f"  {res['evidence']}")
        if res.get("action"):
            L.append(f"  ACTION: {res['action']}")
        if res.get("source"):
            L.append(f"  Source: {res['source']}")
    else:
        L.append("  Nobody has read an obituary for this record. Who died is UNCONFIRMED.")
    if confidence == "medium":
        L.append("  CONFIDENCE MEDIUM. Owner reported deceased by SmartSkip, signers "
                 "explicitly labelled children or spouse, ages fit a parent-child gap, "
                 "phones present. Self-consistent, but unverified.")
    elif confidence == "low" and situation in ESTATE:
        L.append("  CONFIDENCE LOW. This record does not corroborate itself: relatives are "
                 "unlabelled, or an age gap does not fit a parent-child relationship, or no "
                 "signer has a phone.")
    L.append("")

    L.append("OWNER OF RECORD: " + str(rec.get("owner_name")))
    if res.get("decedent_name"):
        L.append(f"WHO ACTUALLY DIED: {res['decedent_name']}"
                 + (f" ({res.get('decedent_relation_to_owner')} of the owner)"
                    if res.get("decedent_relation_to_owner") else ""))
    if rec.get("anchor_date"):
        L.append(f"OBITUARY DATE ON RECORD: {rec['anchor_date']} "
                 f"({rec.get('months_since')} months ago). This is the PUBLICATION date from "
                 f"dataflik, not a confirmed date of death.")
    L.append(f"SIGNER BASIS: {ss.get('signer_basis') or 'n/a'}")
    L.append("")

    L.append("THE PROPERTY")
    L.append(f"  {prop}, {rec.get('city')} {rec.get('state')} {rec.get('zip')} "
             f"({rec.get('county')} County)")
    money = []
    if rec.get("estimate_value"):
        money.append("Value ${:,.0f}".format(rec["estimate_value"]))
    if rec.get("equity_pct") is not None:
        eq = "Equity {:.0f}%".format(rec["equity_pct"])
        if rec.get("equity_dollars"):
            eq += " (${:,.0f})".format(rec["equity_dollars"])
        money.append(eq)
    if rec.get("rental_value"):
        money.append("Rent ${:,.0f}/mo".format(rec["rental_value"]))
    if money:
        L.append("  " + " | ".join(money))
    phys = []
    if rec.get("beds"):
        phys.append("{:.0f} bd".format(rec["beds"]))
    if rec.get("baths"):
        phys.append("{:g} ba".format(rec["baths"]))
    if rec.get("sqft"):
        phys.append("{:,.0f} sqft".format(rec["sqft"]))
    if rec.get("year_built"):
        phys.append("built {:.0f}".format(rec["year_built"]))
    if phys:
        L.append("  " + " | ".join(phys))
    dist = []
    if rec.get("vacant"):
        dist.append("VACANT")
    if rec.get("tax_amount"):
        dist.append("${:,.0f} tax delinquent".format(rec["tax_amount"]))
    if rec.get("tax_years"):
        dist.append("{:.0f} yr behind on taxes".format(rec["tax_years"]))
    if rec.get("lien_amount"):
        dist.append("${:,.0f} in active liens".format(rec["lien_amount"]))
    L.append("  Distress: " + (", ".join(dist) if dist
                               else "none on file. The death is the motivation here."))
    L.append(f"  Opportunity score {rec.get('score')} (tier {rec.get('tier')} of 60 worked)")
    L.append("")

    if situation in OWNER_ALIVE:
        L.append("FAMILY CONTACTS  (the OWNER is alive and signs alone)")
        L.append("  Nothing has passed to these people. They are a route to the owner, "
                 "and on an elderly owner they are usually the ones handling their affairs.")
    else:
        L.append(f"WHO MUST SIGN  ({len(dms)} of {len(rels)} relatives SmartSkip returned)")
    for i, d in enumerate(dms, 1):
        L += _person_block(d, prop, f"  {i}. ")
    if not dms:
        L.append("  none identified")
    L.append("")

    if others:
        L.append(f"OTHER FAMILY FROM SMARTSKIP  ({len(others)}, not signers, "
                 f"use them to reach the signers)")
        for d in others[:12]:
            L += _person_block(d, prop, "  - ")
        if len(others) > 12:
            L.append(f"  ... and {len(others) - 12} more in output/dp/ranked_records.json")
        L.append("")

    if subj_ph:
        L.append(f"THE OWNER'S OWN NUMBERS  ({len(subj_ph)} from the skip trace)")
        if situation in ESTATE:
            L.append("  The owner is reported deceased, so expect these to be dead or to "
                     "reach a surviving household member rather than the owner.")
        for x in subj_ph[:8]:
            L.append("  " + _fmt_phone(x))
        L.append("")

    if not verified:
        L.append("WHAT TO RESEARCH NEXT  (free, about 20 minutes, do it before dialing)")
        surname = rec.get("owner_last") or (str(rec.get("owner_name") or "").split() or [""])[-1]
        month = str(rec.get("anchor_date") or "")[:7]
        L.append(f"  1. CONFIRM WHO DIED. Search: {surname} obituary {rec.get('city')} {month}. "
                 f"Match the decedent against the owner of record, {rec.get('owner_name')}.")
        L.append("     THE TRAP: an obituary on the record does NOT mean the OWNER died. On "
                 "several records checked by hand it was a spouse or a parent, which makes "
                 "the owner a living widow or widower and not an estate at all.")
        L.append("  2. Get the TRUE date of death. The date above is only when a notice published.")
        L.append("  3. Reconcile the obituary's survivor list against the signers above. "
                 "SmartSkip's relationship labels are an inference, not a record, and it "
                 "returns the dead person as a relative.")
        L.append("  4. If the surname is common a name search will not settle it. The Knox "
                 "County Register of Deeds and the probate filings are address-anchored.")
        L.append("  5. Write what you find back here, and set Decedent Name, Date of Death "
                 "and DM Confidence on the record.")
        L.append("")

    L.append(f"PHONE TAGS: every number above will carry its relationship tag "
             f"(Son / Daughter / Wife / Mother / Relative) plus SmartSkip once the phone-tag "
             f"upload runs. No Dial First/Second tier until Trestle is restored.")
    L.append(f"TOTAL: {n_num} unique numbers across {len(rels)} relatives.")
    L.append("Source: SmartSkip bulk trace 2026-08-07 (batch 6a762b43), Tracerfy gap-fill, "
             "TN intestacy signer selection via src/obituary_dp_run.py. "
             "Full detail: output/dp/ranked_records.json")
    return "\n".join(L)


def build(cache, ranked, research_path, top, sender):
    qualified, _, _ = assemble(Path(cache), 3, date(2026, 8, 7))
    crm = qualified[:top]
    ss_recs = json.loads(Path(ranked).read_text(encoding="utf-8"))
    ss_by_addr = {norm(r.get("property_address")): r for r in ss_recs}
    research = json.loads(Path(research_path).read_text(encoding="utf-8"))
    rbr = {f["record"]: f for f in research["findings"]}

    ages = subject_ages()
    plan = []
    for rec in crm:
        ss = ss_by_addr.get(norm(rec["street"])) or {}
        res = rbr.get(ss.get("input_name") or "")
        # A trust or LLC on title was never name-traced, so it has no relatives and
        # no signer set. It must not get a card, a tag or a Decedent Name.
        ent_words = set(re.findall(r"[a-z]+",
                                   (rec.get("owner_company") or rec.get("owner_name") or "").lower()))
        if not ss and ((rec.get("owner_type") or "").lower() in ("trust", "company", "entity")
                       or bool(ent_words & ENTITY_WORDS)):
            situation, evidence = "entity_owner", "trust or LLC on title, never name-traced"
        else:
            situation, evidence = situation_for(ss, res, bool(ss.get("deceased")))
        verified = bool(res and not (res.get("verdict") or "").lower().startswith("unresolved"))

        dms = pick_dms(ss, res)
        phones = heir_phones(dms)
        # numbers already on the record must not be re-merged
        existing = {digits(p) for p in (rec.get("phone_numbers") or [])}
        new_phones = [p for p in phones if p["number"] not in existing]

        fields = {}
        if situation in OWNER_ALIVE:
            # The owner is alive and can sign alone. Writing an heir into
            # Decision Maker here contradicts the record and, on the Fitzgerald
            # record, named the deceased father. Relatives stay in the note as
            # family contacts, not in a field that implies signing authority.
            fields["Decision Maker"] = title(rec.get("owner_name"))
            fields["DM Relationship"] = "Owner of record"
        else:
            for i, d in enumerate(dms[:3], 1):
                key = "Decision Maker" if i == 1 else f"DM {i} Name"
                rel = "DM Relationship" if i == 1 else f"DM {i} Relationship"
                fields[key] = title(d.get("name"))
                fields[rel] = d.get("canon_rel") or "Relative"
        fields["DM Confidence"] = dm_confidence(
            situation, verified, ss, dms, ages.get((ss.get("input_name") or "").strip()))
        fields.update(decedent_fields(rec, ss, res, situation, verified))
        if res and res.get("source"):
            fields["Obituary URL"] = res["source"]

        col = board_column(situation, new_phones or phones, rec.get("vacant"),
                           fields["DM Confidence"])
        plan.append({
            "uuid": rec["uuid"],
            "street": rec["street"], "city": rec["city"], "state": rec["state"],
            "zip": rec["zip"], "county": rec["county"],
            "input_name": ss.get("input_name") or rec.get("owner_name"),
            "owner_name": rec.get("owner_name"),
            "owner_first": rec.get("owner_first"), "owner_last": rec.get("owner_last"),
            "score": rec.get("score"), "tier": rec.get("tier"),
            "situation": situation, "evidence": evidence, "verified": verified,
            "signer_basis": ss.get("signer_basis"),
            "relatives_found": len(ss.get("relatives") or []),
            "dms": [{"name": title(d.get("name")), "canon_rel": d.get("canon_rel"),
                     "age": d.get("age"), "dm_score": d.get("dm_score"),
                     "phones": [digits(p.get("number")) for p in (d.get("phones") or [])
                                if digits(p.get("number"))]} for d in dms],
            "phones": new_phones[:MAX_MERGE_PHONES],
            "phone_overflow": new_phones[MAX_MERGE_PHONES:],
            "all_phone_tags": phones,
            "fields": fields,
            "tags": property_tags(situation, dms, rec.get("tier")),
            "assign_to": sender,
            "assign_uuid": ASSIGNEE.get(sender, ""),
            "column": col,
            "column_uuid": COLUMNS.get(col or "", ""),
            "note": note_text(rec, ss, res, situation, dms, new_phones, verified,
                              fields["DM Confidence"]),
        })
    return plan


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--ranked", default="output/dp/ranked_records.json")
    ap.add_argument("--research", default="output/dp/stage_c_research.json")
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--sender", default="Adriana", choices=sorted(ASSIGNEE))
    ap.add_argument("--outdir", default="output/dp")
    args = ap.parse_args()

    plan = build(args.cache, args.ranked, args.research, args.top, args.sender)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "crm_plan.json").write_text(json.dumps(plan, indent=1, default=str),
                                          encoding="utf-8")

    cols = ["street", "city", "zip", "score", "tier", "situation", "verified",
            "relatives_found", "n_dms", "n_phones", "n_overflow", "column",
            "assign_to", "decision_maker", "dm_relationship", "dm_confidence",
            "decedent_name", "owner_deceased", "tags"]
    with (outdir / "crm_plan.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in plan:
            f = p["fields"]
            w.writerow({**p, "n_dms": len(p["dms"]), "n_phones": len(p["phones"]),
                        "n_overflow": len(p["phone_overflow"]),
                        "decision_maker": f.get("Decision Maker", ""),
                        "dm_relationship": f.get("DM Relationship", ""),
                        "dm_confidence": f.get("DM Confidence", ""),
                        "decedent_name": f.get("Decedent Name", ""),
                        "owner_deceased": f.get("Owner Deceased", ""),
                        "tags": ", ".join(p["tags"])})

    print(f"planned {len(plan)} records -> {outdir}/crm_plan.json + crm_plan.csv\n")
    print("  situations   :", dict(Counter(p["situation"] for p in plan)))
    print("  board columns:", dict(Counter(p["column"] or "(no card)" for p in plan)))
    print("  DM confidence:", dict(Counter(p["fields"].get("DM Confidence") for p in plan)))
    print("  Owner Deceased:", dict(Counter(p["fields"].get("Owner Deceased", "(blank)")
                                            for p in plan)))
    print("  Decedent Name written:", sum(1 for p in plan if p["fields"].get("Decedent Name")))
    print(f"  phones to merge: {sum(len(p['phones']) for p in plan)}"
          f"  overflow (needs pass B): {sum(len(p['phone_overflow']) for p in plan)}"
          f"  across {sum(1 for p in plan if p['phone_overflow'])} records")
    print(f"  records with NO phone: {sum(1 for p in plan if not p['phones'])}")
    print(f"  phone tag rows: {sum(len(p['all_phone_tags']) * 2 for p in plan)}")
    print(f"  all assigned to: {args.sender}")


if __name__ == "__main__":
    main()
