"""Validate every mailing address on the obituary deep-prospecting mail list.

SmartSkip's campaign export gives a mailing address per person, but it is not
CASS validated and it is not checked for deliverability. Mailing it raw means
paying postage on addresses the USPS will not deliver to, and worse, mailing an
estate letter to a vacant house or to a person who has moved.

This runs every signer address through the Smarty US Street API and grades it:

  deliverable   DPV confirmed, safe to mail
  no_secondary  building confirmed but the apartment or unit number is missing
                or wrong (DPV code D or S). Will often not deliver.
  vacant        USPS has the address flagged vacant. Do not mail an estate
                letter here, it is the wrong signal to send.
  undeliverable no DPV match. Do not mail.
  unverified    Smarty returned no candidate at all.

Also flags duplicates, because two children who live at the same address should
get one letter each by name, not two identical letters to one door, and a
signer whose mailing address IS the subject property (the decedent's own house)
is called out separately: nobody is reading that mailbox.

Usage:
  python src/obituary_mail_validate.py --dry-run
  python src/obituary_mail_validate.py --commit
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_env(path=".env"):
    env = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def grade(c):
    """Map a Smarty candidate onto a mail decision."""
    if c is None:
        return "unverified", "Smarty returned no candidate"
    a = c.analysis
    dpv = (getattr(a, "dpv_match_code", "") or "").upper()
    vacant = (getattr(a, "dpv_vacant", "") or "").upper()
    active = (getattr(a, "active", "") or "").upper()
    if vacant == "Y":
        return "vacant", "USPS flags this address vacant"
    if active == "N":
        return "undeliverable", "USPS flags this address inactive"
    if dpv == "Y":
        return "deliverable", "DPV confirmed"
    if dpv in ("D", "S"):
        return "no_secondary", f"DPV {dpv}: unit or apartment number missing or wrong"
    return "undeliverable", f"DPV match code {dpv or 'none'}"


def collect_people(recs, include_channels=False):
    """One row per person we intend to mail."""
    out = []
    for rec in recs:
        for d in rec.get("dms") or []:
            out.append({
                "record": rec.get("input_name"),
                "property_address": rec.get("property_address"),
                "property_zip": rec.get("property_zip"),
                "signer_basis": rec.get("signer_basis"),
                "owner_flagged_deceased": rec.get("deceased"),
                "name": d.get("name"), "first": d.get("first"), "last": d.get("last"),
                "relationship": d.get("canon_rel"), "type_raw": d.get("type_raw"),
                "age": d.get("age"),
                "street": d.get("mailing_street"), "city": d.get("mailing_city"),
                "state": d.get("mailing_state"), "zip": d.get("mailing_zip"),
                "phones": ", ".join(p["number"] for p in (d.get("phones") or [])),
                "role": "signer",
            })
        if include_channels:
            for d in rec.get("channels") or []:
                out.append({
                    "record": rec.get("input_name"),
                    "property_address": rec.get("property_address"),
                    "name": d.get("name"), "first": d.get("first"), "last": d.get("last"),
                    "relationship": d.get("canon_rel"),
                    "street": d.get("mailing_street"), "city": d.get("mailing_city"),
                    "state": d.get("mailing_state"), "zip": d.get("mailing_zip"),
                    "phones": ", ".join(p["number"] for p in (d.get("phones") or [])),
                    "role": "channel",
                })
    return out


def validate(people, auth_id, auth_token, batch_size=100):
    from smartystreets_python_sdk import BasicAuthCredentials, Batch, ClientBuilder
    from smartystreets_python_sdk.us_street import Lookup
    from smartystreets_python_sdk.us_street.match_type import MatchType

    client = ClientBuilder(BasicAuthCredentials(auth_id, auth_token)).build_us_street_api_client()
    idx = [i for i, p in enumerate(people) if p.get("street")]
    for start in range(0, len(idx), batch_size):
        chunk = idx[start:start + batch_size]
        batch = Batch()
        for i in chunk:
            p = people[i]
            lk = Lookup()
            lk.street = p["street"]
            lk.city = p.get("city") or ""
            lk.state = p.get("state") or "TN"
            lk.zipcode = p.get("zip") or ""
            lk.candidates = 1
            lk.match = MatchType.INVALID  # return a candidate even when it does not match
            batch.add(lk)
        try:
            client.send_batch(batch)
        except Exception as e:
            for i in chunk:
                people[i]["mail_status"] = "error"
                people[i]["mail_reason"] = str(e)[:140]
            continue
        for slot, i in enumerate(chunk):
            cands = batch[slot].result
            c = cands[0] if cands else None
            status, reason = grade(c)
            people[i]["mail_status"] = status
            people[i]["mail_reason"] = reason
            if c:
                comps, meta = c.components, c.metadata
                people[i].update({
                    "usps_street": c.delivery_line_1,
                    "usps_lastline": c.last_line,
                    "usps_zip": f"{comps.zipcode}-{comps.plus4_code}" if comps.plus4_code else comps.zipcode,
                    "rdi": getattr(meta, "rdi", ""),
                    "county": getattr(meta, "county_name", ""),
                    "dpv_footnotes": getattr(c.analysis, "dpv_footnotes", ""),
                })
    for p in people:
        p.setdefault("mail_status", "unverified")
        p.setdefault("mail_reason", "no street address on the record")
    return people


def annotate(people):
    """Flag duplicate doors and letters addressed to the decedent's own house."""
    doors = defaultdict(list)
    for p in people:
        key = (norm(p.get("usps_street") or p.get("street")), (p.get("usps_zip") or p.get("zip") or "")[:5])
        if key[0]:
            doors[key].append(p)
    for key, group in doors.items():
        if len(group) > 1:
            names = ", ".join(sorted({g["name"] for g in group}))
            for g in group:
                g["shared_door_with"] = names
                g["shared_door_count"] = len(group)
    for p in people:
        if p.get("property_address") and norm(p.get("street")) == norm(p["property_address"]):
            p["mails_to_subject_property"] = "YES"
    return people


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ranked", default="output/dp/ranked_records.json")
    ap.add_argument("--outdir", default="output/dp")
    ap.add_argument("--include-channels", action="store_true",
                    help="also validate in-law and generic relative addresses")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    recs = json.loads(Path(args.ranked).read_text(encoding="utf-8"))
    people = collect_people(recs, args.include_channels)
    with_addr = sum(1 for p in people if p.get("street"))
    print(f"{len(people)} people to mail, {with_addr} carry a street address")

    env = load_env()
    aid, tok = env.get("SMARTY_AUTH_ID", ""), env.get("SMARTY_AUTH_TOKEN", "")
    if not args.commit or args.dry_run:
        print(f"DRY RUN. Smarty creds present: {bool(aid and tok)}. "
              f"Rerun with --commit to validate {with_addr} addresses.")
        return
    if not (aid and tok):
        print("No SMARTY_AUTH_ID / SMARTY_AUTH_TOKEN in .env, nothing validated.")
        return

    validate(people, aid, tok)
    annotate(people)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cols = ["record", "property_address", "name", "relationship", "type_raw", "age", "role",
            "mail_status", "mail_reason", "street", "city", "state", "zip",
            "usps_street", "usps_lastline", "usps_zip", "rdi", "county", "dpv_footnotes",
            "shared_door_with", "shared_door_count", "mails_to_subject_property",
            "owner_flagged_deceased", "signer_basis", "phones"]
    with (outdir / "mail_list_validated.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(people)

    mix = Counter(p["mail_status"] for p in people)
    mailable = [p for p in people if p["mail_status"] == "deliverable"]
    print(f"\n  {dict(mix)}")
    print(f"  SAFE TO MAIL          : {len(mailable)}")
    print(f"  shared-door people    : {sum(1 for p in people if p.get('shared_door_count'))}")
    print(f"  mailing to the subject property: "
          f"{sum(1 for p in people if p.get('mails_to_subject_property'))}")
    covered = {p["record"] for p in mailable}
    allrec = {p["record"] for p in people}
    print(f"  records with at least one mailable signer: {len(covered)} of {len(allrec)}")
    if allrec - covered:
        print(f"  RECORDS WITH NOBODY MAILABLE: {sorted(allrec - covered)}")
    (outdir / "mail_validation_summary.json").write_text(json.dumps({
        "people": len(people), "status_mix": dict(mix), "safe_to_mail": len(mailable),
        "records_covered": len(covered), "records_total": len(allrec),
        "records_uncovered": sorted(allrec - covered)}, indent=1), encoding="utf-8")
    print(f"\nwrote {outdir}/mail_list_validated.csv")


if __name__ == "__main__":
    main()
