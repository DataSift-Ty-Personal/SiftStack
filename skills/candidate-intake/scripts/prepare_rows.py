#!/usr/bin/env python3
"""Dedupe scored candidates against the master sheet and build the paste block.

Takes the scored JSON array plus a CSV export of the current master sheet,
drops anyone already present (matched on normalized email, phone, or profile
handle), and prints a tab-separated block ready to paste into Google Sheets,
in the exact column order defined in references/sheet-schema.md. Stdlib only.

Usage:
    python prepare_rows.py --scored scored.json --sheet-csv current_sheet.csv \
        --out rows.tsv
Use --sheet-csv "" (or omit the file) on the very first run with an empty sheet.
"""
import argparse
import csv
import json
import re
import sys

COLUMNS = [
    ("date_received", "Date Received"),
    ("full_name", "Full Name"),
    ("first_name", "First Name"),
    ("channel", "Channel"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("location", "Location"),
    ("profile_url", "Profile URL"),
    ("resume_url", "Resume URL"),
    ("years_experience", "Years Experience"),
    ("english_level", "English Level"),
    ("score", "Score"),
    ("tier", "Tier"),
    ("score_reason", "Score Reason"),
    ("role_fit_notes", "Role Fit Notes"),
    ("screener_answers", "Screener Answers"),
    ("outreach_status", "Outreach Status"),
]


def norm_email(v):
    v = str(v or "").strip().lower()
    return v if "@" in v else ""


def norm_phone(v):
    digits = re.sub(r"\D", "", str(v or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def norm_profile(v):
    v = str(v or "").strip().lower()
    if not v:
        return ""
    v = re.sub(r"^https?://(www\.)?", "", v)
    return v.rstrip("/")


def keys_for(email, phone, profile):
    keys = set()
    if norm_email(email):
        keys.add("e:" + norm_email(email))
    if norm_phone(phone):
        keys.add("p:" + norm_phone(phone))
    if norm_profile(profile):
        keys.add("u:" + norm_profile(profile))
    return keys


def existing_keys(sheet_csv):
    keys = set()
    try:
        f = open(sheet_csv, encoding="utf-8-sig", newline="")
    except (OSError, TypeError):
        return keys
    with f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {str(k or "").strip().lower(): v for k, v in row.items()}
            keys |= keys_for(row.get("email"), row.get("phone"),
                             row.get("profile url"))
    return keys


def clean_cell(value):
    """Tabs and newlines break the paste block; flatten them."""
    s = str(value if value is not None else "")
    return re.sub(r"[\t\r\n]+", " | ", s).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scored", default="scored.json")
    ap.add_argument("--sheet-csv", default="current_sheet.csv")
    ap.add_argument("--out", default="rows.tsv")
    args = ap.parse_args()

    with open(args.scored, encoding="utf-8-sig") as f:
        candidates = json.load(f)

    seen = existing_keys(args.sheet_csv)
    if not seen and args.sheet_csv:
        print("Note: no existing rows read from %r (empty or missing sheet "
              "export); nothing will be skipped." % args.sheet_csv,
              file=sys.stderr)

    added, skipped = [], []
    for cand in candidates:
        ckeys = keys_for(cand.get("email"), cand.get("phone"),
                         cand.get("profile_url"))
        if ckeys & seen:
            skipped.append(cand)
            continue
        if not ckeys:
            # No dedupe key at all; add, but flag it so the user knows.
            print("Warning: %s has no email/phone/profile key; cannot dedupe."
                  % cand.get("full_name", "?"), file=sys.stderr)
        seen |= ckeys
        cand.setdefault("outreach_status", "Not contacted")
        added.append(cand)

    lines = []
    for cand in added:
        lines.append("\t".join(clean_cell(cand.get(field, ""))
                               for field, _header in COLUMNS))
    block = "\n".join(lines)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(block + ("\n" if block else ""))

    if block:
        print(block)

    print("", file=sys.stderr)
    print("Added %d, skipped %d duplicate(s)." % (len(added), len(skipped)),
          file=sys.stderr)
    for cand in skipped:
        print("  skipped: %s (%s)" % (cand.get("full_name", "?"),
              cand.get("channel", "?")), file=sys.stderr)
    if added and args.out:
        print("Paste block saved to %s; also printed above. Paste into the "
              "first empty cell of column A." % args.out, file=sys.stderr)


if __name__ == "__main__":
    main()
