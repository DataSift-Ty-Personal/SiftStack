"""Sphere-of-influence intake: normalize LinkedIn + Facebook contact exports into one roster.

Stage 1 of the SOI pipeline (Columbus OH beta):
    intake (this) -> owner-of-record resolve (county auditor join / skip trace)
    -> property enrich (SiftMap) -> SOI priority score -> deliverable.

Input formats (as exported):
    LinkedIn: First Name, Last Name, Email   (last name often carries credentials: "Weatherford, CRS")
    Facebook: Name, Email                    (single name field, middle token often a maiden name)

Output: output/soi_contacts_normalized.csv + .json
    One row per unique person. Cross-source matches merged (both emails kept).

Usage:
    python src/soi_intake.py --linkedin "LInkedIn Contacts-Table 1.csv" --facebook "Facebook contacts-Table 1.csv"
"""

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "me.com", "icloud.com",
    "outlook.com", "live.com", "msn.com", "sbcglobal.net", "att.net", "mac.com",
    "comcast.net", "ymail.com", "protonmail.com", "roadrunner.com", "rr.com",
    "columbus.rr.com", "insight.rr.com", "wowway.com", "juno.com", "netzero.net",
}

# Suffixes that are part of the person's name, kept for owner-record matching.
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Credential / title tokens stripped from LinkedIn last-name fields ("Weatherford, CRS").
CREDENTIAL_TOKENS = {
    "crs", "jd", "mba", "ms", "msc", "phd", "cpa", "cfp", "cfa", "gri", "abr",
    "sres", "nrba", "broker", "realtor", "ceo", "cmo", "coo", "president",
    "psy", "pmp", "esq", "md", "dds", "rn", "lutcf", "chms", "e-pro", "sfr",
}

# Domain substrings that mark a real-estate-industry professional (agent/lender/title/inspector).
INDUSTRY_HINTS = (
    "realty", "realtor", "kw.com", "remax", "homes", "mortgage", "mtg",
    "lending", "lender", "bank", "title", "inspect", "apprais", "brokerage",
    "cbre", "staubach", "century21", "coldwell", "exprealty", "homefield",
)

# Domain substrings that hint the contact is in or tied to central Ohio.
GEO_HINTS = ("columbus", "ohio", "osu.edu", "muohio", "ohiohealth", "mchs.com", "osumc")

GENERIC_LOCALPARTS = {"info", "office", "contact", "admin", "team", "sales", "hello", "support"}


def norm_key(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


def strip_possessive(token):
    return re.sub(r"['’]s$", "", token)


def classify_domain(email):
    domain = email.split("@", 1)[1].lower() if "@" in email else ""
    if not domain:
        return "", "unknown"
    if domain in PERSONAL_DOMAINS:
        return domain, "personal"
    if domain.endswith(".edu"):
        return domain, "edu"
    return domain, "corporate"


def parse_linkedin_name(first_raw, last_raw):
    """LinkedIn last-name fields carry credentials: 'Weatherford, CRS', 'Crum III, JD, MBA / CEO'."""
    last_part = re.split(r"[,/]", last_raw or "")[0].strip()
    tokens = last_part.split()
    suffix = ""
    kept = []
    for tok in tokens:
        tok = strip_possessive(tok)
        t = norm_key(tok)
        if t in NAME_SUFFIXES:
            suffix = tok
        elif t and t not in CREDENTIAL_TOKENS:
            kept.append(tok)
    last = " ".join(kept)
    # First-name field sometimes carries a middle initial ("Eric S.").
    ftoks = (first_raw or "").strip().split()
    first = ftoks[0] if ftoks else ""
    middle = " ".join(ftoks[1:])
    return first, middle, last, suffix


def parse_facebook_name(name_raw):
    """Single name field. Middle tokens are often maiden names, kept for matching."""
    tokens = (name_raw or "").strip().split()
    suffix = ""
    kept = []
    for tok in tokens:
        tok = strip_possessive(tok)
        t = norm_key(tok)
        if t in NAME_SUFFIXES:
            suffix = tok
        else:
            kept.append(tok)
    if not kept:
        return "", "", "", ""
    first = kept[0]
    last = kept[-1] if len(kept) > 1 else ""
    middle = " ".join(kept[1:-1])
    return first, middle, last, suffix


def flags_for(contact):
    email = contact["email"]
    domain = contact["email_domain"]
    flags = []
    local = email.split("@", 1)[0].lower() if email else ""
    if local in GENERIC_LOCALPARTS or not contact["last_name"]:
        flags.append("business_contact")
    if any(h in domain for h in INDUSTRY_HINTS):
        flags.append("re_industry")
    if any(h in domain for h in GEO_HINTS):
        flags.append("geo_hint_ohio")
    # A possessive or "Team" in the raw name means the export row is not a clean person.
    if "'s" in contact["raw_name"].lower() or re.search(r"\bteam\b", contact["raw_name"], re.I):
        flags.append("dirty_name")
    return flags


def load_contacts(linkedin_path, facebook_path):
    contacts = []
    if linkedin_path:
        with open(linkedin_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                first_raw = (row.get("First Name") or "").strip()
                last_raw = (row.get("Last Name") or "").strip()
                email = (row.get("Email") or "").strip().lower()
                if not (first_raw or last_raw or email):
                    continue
                first, middle, last, suffix = parse_linkedin_name(first_raw, last_raw)
                contacts.append({
                    "source": "linkedin", "raw_name": f"{first_raw} {last_raw}".strip(),
                    "first_name": first, "middle_name": middle, "last_name": last,
                    "suffix": suffix, "email": email,
                })
    if facebook_path:
        with open(facebook_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name_raw = (row.get("Name") or "").strip()
                email = ((row.get("Email") or row.get("Email ") or "")).strip().lower()
                if not (name_raw or email):
                    continue
                first, middle, last, suffix = parse_facebook_name(name_raw)
                contacts.append({
                    "source": "facebook", "raw_name": name_raw,
                    "first_name": first, "middle_name": middle, "last_name": last,
                    "suffix": suffix, "email": email,
                })
    for c in contacts:
        c["email_domain"], c["email_type"] = classify_domain(c["email"])
        c["flags"] = flags_for(c)
    return contacts


def merge(contacts):
    """Dedupe by email, then link the same person across sources by name.

    Cross-source match: same normalized (first, last), or same last name where one
    record's middle token (maiden name) equals the other's last name.
    """
    by_email = {}
    ordered = []
    for c in contacts:
        key = c["email"] or f"noemail:{norm_key(c['raw_name'])}:{c['source']}"
        if key in by_email:
            existing = by_email[key]
            existing["sources"].add(c["source"])
            existing["flags"] = sorted(set(existing["flags"]) | set(c["flags"]))
        else:
            c = dict(c)
            c["sources"] = {c.pop("source")}
            by_email[key] = c
            ordered.append(c)

    by_name = defaultdict(list)
    for c in ordered:
        if c["first_name"] and c["last_name"]:
            by_name[(norm_key(c["first_name"]), norm_key(c["last_name"]))].append(c)

    merged, consumed = [], set()
    for c in ordered:
        if id(c) in consumed:
            continue
        group = [c]
        key = (norm_key(c["first_name"]), norm_key(c["last_name"]))
        for other in by_name.get(key, []):
            if other is not c and id(other) not in consumed and other["sources"] != c["sources"]:
                group.append(other)
                consumed.add(id(other))
        primary = group[0]
        rec = dict(primary)
        rec["sources"] = sorted(set().union(*[g["sources"] for g in group]))
        rec["emails"] = sorted({g["email"] for g in group if g["email"]})
        rec["email"] = rec["emails"][0] if rec["emails"] else ""
        rec["flags"] = sorted(set().union(*[set(g["flags"]) for g in group]))
        # Prefer the record with a maiden/middle name for owner matching.
        rec["middle_name"] = next((g["middle_name"] for g in group if g["middle_name"]), primary["middle_name"])
        merged.append(rec)
    return merged


def main():
    ap = argparse.ArgumentParser(description="Normalize SOI contact exports")
    ap.add_argument("--linkedin", default="LInkedIn Contacts-Table 1.csv")
    ap.add_argument("--facebook", default="Facebook contacts-Table 1.csv")
    ap.add_argument("--out", default="output/soi_contacts_normalized")
    args = ap.parse_args()

    contacts = load_contacts(args.linkedin, args.facebook)
    merged = merge(contacts)

    out_base = Path(args.out)
    out_base.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = ["first_name", "middle_name", "last_name", "suffix", "email", "emails",
                  "email_domain", "email_type", "sources", "flags", "raw_name"]
    with open(f"{out_base}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for c in merged:
            row = dict(c)
            row["emails"] = "; ".join(c["emails"])
            row["sources"] = "; ".join(c["sources"])
            row["flags"] = "; ".join(c["flags"])
            w.writerow(row)
    with open(f"{out_base}.json", "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=1, default=list)

    both = sum(1 for c in merged if len(c["sources"]) > 1)
    stats = {
        "input_rows": len(contacts),
        "unique_people": len(merged),
        "in_both_sources": both,
        "linkedin_only": sum(1 for c in merged if c["sources"] == ["linkedin"]),
        "facebook_only": sum(1 for c in merged if c["sources"] == ["facebook"]),
        "personal_email": sum(1 for c in merged if c["email_type"] == "personal"),
        "corporate_email": sum(1 for c in merged if c["email_type"] == "corporate"),
        "edu_email": sum(1 for c in merged if c["email_type"] == "edu"),
        "re_industry": sum(1 for c in merged if "re_industry" in c["flags"]),
        "geo_hint_ohio": sum(1 for c in merged if "geo_hint_ohio" in c["flags"]),
        "business_or_dirty": sum(1 for c in merged if {"business_contact", "dirty_name"} & set(c["flags"])),
        "no_email": sum(1 for c in merged if not c["email"]),
    }
    for k, v in stats.items():
        print(f"{k}: {v}")
    print(f"\nwrote {out_base}.csv / .json")


if __name__ == "__main__":
    main()
