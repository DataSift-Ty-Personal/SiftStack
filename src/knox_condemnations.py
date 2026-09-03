"""Knox City condemnation docket: BBB blight certifications + Public Officer orders.

WHY THIS RUNS ON A SCHEDULE. The City publishes both agendas at fixed URLs and
OVERWRITES them each cycle. There is no public back archive: the IQM2 portal
registers the board but publishes no BBB meetings. So a cycle that is not
captured while it is live is gone permanently, unlike foreclosure and probate
where a 12 month archive can be backfilled at any time.

    python src/knox_condemnations.py                 # fetch, archive, parse
    python src/knox_condemnations.py --no-fetch      # re-parse what is archived

Output is `condemnations.json` in the scratch dir, in the exact shape
`knox_ftm_pull.load_condemnations` already expects, plus richer fields it
ignores. Snapshots accumulate under `<scratch>/condemnation_archive/` keyed by
content hash, so re-running mid cycle never duplicates and a changed agenda is
kept as a new snapshot rather than clobbering the old one.

Each entry is a full dossier: parcel id, every interested party with a mailing
address, owner-since date, violations, lien and bill totals, delinquent taxes,
the Accela record number and inspection dates. Deceased owners and unknown-heir
blocks appear verbatim, which is why heirs surface here before anywhere else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CDN = ("https://cdnsm5-hosted.civiclive.com/UserFiles/Servers/Server_109478"
       "/File/Boards/betterbuilding/")
AGENDAS = {
    "bbb": "agenda_bbb.pdf",   # Better Building Board, certification of blight
    "poh": "agenda_poh.pdf",   # Public Officer hearing, repair/demolition orders
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# "A.   2905 WASHINGTON PIKE" / "B.  1620 JEFFERSON AVENUE INCLUDING ..."
_ENTRY = re.compile(r"^\s*([A-Z])\.\s{1,12}([0-9][^\n]*?)\s*$", re.M)
_FIELD = {
    "parcel": re.compile(r"PROPERTY IDENTIFICATION NO:?\s*([A-Z0-9]+)", re.I),
    "owner_since": re.compile(r"OWNER SINCE:?\s*([0-9/]+)", re.I),
    "violations": re.compile(
        r"VIOLATIONS:?\s*(.+?)(?=\n\s*\n|FEES:|HISTORIC:|RECORD NUMBER:)", re.I | re.S),
    "fees": re.compile(
        r"FEES:?\s*(.+?)(?=\n\s*\n|HISTORIC:|RECORD NUMBER:|OWNER SINCE:)", re.I | re.S),
    "record": re.compile(r"RECORD NUMBER:?\s*([A-Z0-9\-]+)", re.I),
    "inspected": re.compile(r"DATE INSPECTED:?\s*([0-9/]+)", re.I),
    "condemned": re.compile(r"DATE CONDEMNED:?\s*([0-9/NA]+)", re.I),
    "city_tax": re.compile(r"CITY TAXES:?\s*([^\n]+)", re.I),
    "cty_tax": re.compile(r"CTY TAXES:?\s*([^\n]+)", re.I),
    "zoning": re.compile(r"ZONING:?\s*([^\n]+)", re.I),
    "tax_sale": re.compile(r"TAX SALE:?\s*([^\n]+)", re.I),
    "historic": re.compile(r"HISTORIC:?\s*(.+?)(?=\n\s*\n|RECORD NUMBER:)", re.I | re.S),
}
_MEETING_DATE = re.compile(r"MEETING AGENDA\s*\n\s*([A-Z]+ \d{1,2}, \d{4})", re.I)
# "2904 Browning Ave - 03/05/2026" in Boarding Approvals. The City uses a
# non-ascii bullet as the separator, so match any single non-word character.
_BOARDED = re.compile(r"^\s*(\d+[^\n]*?)\s*[^\w\s]\s*(\d{2}/\d{2}/\d{4})\s*$", re.M)


# Party blocks in the agenda are separated by a blank line, and each block keeps
# its own mailing address attached to the name above it.
_BLANK_LINE = re.compile(r"\n\s*\n")


def _clean(s: str) -> str:
    return " ".join((s or "").split())


# The dossier header is an item letter followed by the property address, either
# on the same line ("B.   1620 JEFFERSON AVENUE") or with the address on a
# following line ("A." then a blank line then the address).
_ITEM_LETTER = re.compile(r"^[ \t]*([A-Z])\.[ \t]*(.*)$")


def _address_from_head(head: str) -> str:
    """Property address from the text between the item letter and the parcel id.

    Scoped to AFTER the last item letter on purpose. Everything before that
    letter belongs to the previous dossier and ends in its parties' mailing
    addresses, which is how a Dallas law firm once became a condemned property.
    """
    lines = head.splitlines()
    last_letter = -1
    inline = ""
    for i, line in enumerate(lines):
        m = _ITEM_LETTER.match(line)
        if m:
            last_letter = i
            inline = m.group(2).strip()
    if last_letter == -1:
        return ""
    if re.match(r"^\d+\s+\S", inline):
        return _clean(inline)
    for line in lines[last_letter + 1:]:
        s = line.strip()
        if re.match(r"^\d+\s+\S", s):
            return _clean(s)
    return ""


def fetch(scratch: str, timeout: int = 60) -> dict:
    """Download both agendas, archive by content hash, return {kind: path}."""
    import requests

    arc = os.path.join(scratch, "condemnation_archive")
    os.makedirs(arc, exist_ok=True)
    out = {}
    for kind, fname in AGENDAS.items():
        try:
            r = requests.get(CDN + fname, headers={"User-Agent": UA}, timeout=timeout)
            r.raise_for_status()
        except Exception as exc:
            print(f"  fetch failed {fname}: {type(exc).__name__}: {exc}")
            continue
        if not r.content.startswith(b"%PDF-"):
            print(f"  {fname}: not a PDF ({len(r.content)} bytes), skipping")
            continue
        h = hashlib.sha256(r.content).hexdigest()[:12]
        # Content hash, not date. The City re-uploads the same agenda repeatedly
        # and a date-keyed name would make a dozen copies of one cycle.
        path = os.path.join(arc, f"{kind}_{h}.pdf")
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(r.content)
            print(f"  NEW snapshot {kind}: {os.path.basename(path)} ({len(r.content):,} bytes)")
        else:
            print(f"  unchanged {kind} ({h})")
        out[kind] = path
    return out


def _text(path: str) -> str:
    from pdfminer.high_level import extract_text
    return extract_text(path)


def parse_agenda(path: str, kind: str) -> list[dict]:
    """Split one agenda into per-property dossiers.

    Anchored on PROPERTY IDENTIFICATION NO rather than the "A." item letter.
    The letter and the address sit on the same line in some agendas and are
    separated by a blank line in others, so anchoring on the letter parsed zero
    entries off a live BBB agenda while happily reporting success.
    """
    txt = _text(path)
    meeting = ""
    m = _MEETING_DATE.search(txt)
    if m:
        try:
            meeting = datetime.strptime(_clean(m.group(1)), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            meeting = _clean(m.group(1))

    marks = [mm.start() for mm in re.finditer(r"PROPERTY IDENTIFICATION NO", txt, re.I)]
    out = []
    for i, at in enumerate(marks):
        prev = marks[i - 1] if i else 0
        head = txt[prev:at]                                   # address lives here
        end = marks[i + 1] if i + 1 < len(marks) else len(txt)
        chunk = txt[at:end]                                   # dossier fields here

        # The property address sits between the item letter and the parcel
        # marker. Do NOT simply take the last house-numbered line before the
        # marker: the preceding dossier ends with its parties' MAILING
        # addresses, so that heuristic returned a law firm in Dallas and three
        # owners' home addresses as if they were the condemned properties.
        addr = _address_from_head(head)
        if not addr:
            continue

        rec = {"source": "condemnation", "kind": kind, "address": addr,
               "hearing": meeting, "agenda": os.path.basename(path)}
        for key, rx in _FIELD.items():
            g = rx.search(chunk)
            rec[key] = _clean(g.group(1)) if g else ""

        pm = re.search(r"OWNERS AND OTHER INTERESTED PARTIES:?(.+?)(?=OWNER SINCE:)",
                       chunk, re.I | re.S)
        parties = []
        if pm:
            # Blank-line separated blocks, kept whole so each mailing address
            # travels with the name it belongs to.
            for part in _BLANK_LINE.split(pm.group(1)):
                p = _clean(part)
                if len(p) > 3:
                    parties.append(p)
        rec["parties"] = parties
        rec["owner_raw"] = parties[0] if parties else ""
        rec["deceased_parties"] = sum(1 for p in parties if "DECEASED" in p.upper())
        rec["has_unknown_heirs"] = any(
            "UNBORN" in p.upper() or "UNKNOWN" in p.upper() for p in parties)
        rec["notes"] = _build_note(rec)
        out.append(rec)
    return out


def parse_boardings(path: str) -> list[dict]:
    """The Boarding Approvals block: address plus the date it was boarded."""
    txt = _text(path)
    start = txt.upper().find("BOARDING APPROVAL")
    if start == -1:
        return []
    end = txt.find("PUBLIC OFFICER REPAIR", start)
    blob = txt[start: end if end != -1 else start + 2500]
    out = []
    for m in _BOARDED.finditer(blob):
        addr = _clean(m.group(1))
        if not re.match(r"^\d", addr):
            continue
        out.append({"source": "condemnation", "kind": "boarding",
                    "address": addr, "boarded": m.group(2), "hearing": m.group(2),
                    "parties": [], "owner_raw": "", "parcel": "", "record": "",
                    "notes": (f"Boarded by the City on {m.group(2)}. Vacant and "
                              "secured, on the Public Officer docket.")})
    return out


def _build_note(r: dict) -> str:
    """Human-readable summary for the DataSift Notes column."""
    label = ("Certified blight (Better Building Board)" if r.get("kind") == "bbb"
             else "Public Officer repair/demolition order")
    bits = [label + (f", hearing {r['hearing']}" if r.get("hearing") else "")]
    if r.get("parcel"):
        bits.append(f"Parcel {r['parcel']}")
    if r.get("violations"):
        bits.append(f"Violations: {r['violations']}")
    if r.get("fees") and r["fees"].upper() != "NONE":
        bits.append(f"Fees: {r['fees']}")
    if r.get("owner_since"):
        bits.append(f"Owner since {r['owner_since']}")
    for key, lab in (("city_tax", "City taxes"), ("cty_tax", "County taxes")):
        if r.get(key):
            bits.append(f"{lab}: {r[key]}")
    if r.get("record"):
        bits.append(f"Record {r['record']}")
    if r.get("deceased_parties"):
        bits.append(f"{r['deceased_parties']} deceased party/parties named")
    if r.get("has_unknown_heirs"):
        bits.append("Unknown or unborn heirs named on the petition")
    return ". ".join(bits) + "."


def merge(scratch: str, fresh: list[dict]) -> list[dict]:
    """Accumulate across cycles, keyed on address + hearing date + kind."""
    path = os.path.join(scratch, "condemnations.json")
    prior = []
    if os.path.exists(path):
        try:
            prior = json.load(open(path, encoding="utf-8"))
        except Exception:
            prior = []
    seen = {}
    for r in prior + fresh:
        k = (r.get("address", "").upper(), r.get("hearing", ""), r.get("kind", ""))
        seen[k] = r          # later wins, so a re-parse refreshes a cycle
    return list(seen.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    ap.add_argument("--no-fetch", action="store_true",
                    help="parse the newest archived snapshots, download nothing")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    os.makedirs(a.scratch, exist_ok=True)
    print(f"Knox condemnation docket -> {a.scratch}")

    if a.no_fetch:
        arc = os.path.join(a.scratch, "condemnation_archive")
        paths = {}
        if os.path.isdir(arc):
            for kind in AGENDAS:
                cands = sorted((os.path.join(arc, f) for f in os.listdir(arc)
                                if f.startswith(kind + "_")),
                               key=os.path.getmtime, reverse=True)
                if cands:
                    paths[kind] = cands[0]
    else:
        paths = fetch(a.scratch)

    if not paths:
        print("no agendas available")
        return 1

    fresh = []
    for kind, p in paths.items():
        try:
            got = parse_agenda(p, kind)
            fresh.extend(got)
            print(f"  {kind}: {len(got)} propert(ies)")
            if kind == "poh":
                b = parse_boardings(p)
                fresh.extend(b)
                print(f"  boardings: {len(b)}")
        except Exception as exc:
            print(f"  parse failed {p}: {type(exc).__name__}: {exc}")

    if not fresh:
        # Loud on purpose: a silent empty here is a permanently lost cycle.
        print("PARSED ZERO ENTRIES. The agenda layout may have changed. The PDFs "
              "are archived so nothing is lost, but the parser needs a look.")
        return 1

    allrec = merge(a.scratch, fresh)
    out = a.out or os.path.join(a.scratch, "condemnations.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(allrec, fh, indent=1)
    print(f"\nthis cycle: {len(fresh)}   cumulative: {len(allrec)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
