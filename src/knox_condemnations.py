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

# THE CITY MOVED ITS WEBSITE, AND THE OLD FILES KEPT ANSWERING. Until
# 2026-11-11 this module read agenda_bbb.pdf / agenda_poh.pdf off the old
# civiclive CDN. Those URLs still return 200 and a valid PDF, and their
# Last-Modified header still changes, but the content has been the June 25 and
# 26, 2026 agendas ever since. So the capture "succeeded" every day for months
# on a dead file: "14 this cycle, 14 cumulative", the same ten rows uploaded
# again each morning, and the July, August and September cycles lost for good.
# Two guards came out of that: the links are now read off the City's live board
# page on every run (the document ids below are only the fallback), and
# `staleness_problems` fails the stage when the newest agenda is older than a
# cycle can explain.
CITY = "https://www.knoxvilletn.gov"
BOARD_PAGE = CITY + "/173/Better-Building-Board-Public-Officer-Hea"
AGENDAS = {
    "bbb": CITY + "/DocumentCenter/View/1253",   # Better Building Board, certification of blight
    "poh": CITY + "/DocumentCenter/View/1255",   # Public Officer hearing, repair/demolition orders
}
_LINK_HINT = {"bbb": "better building board", "poh": "public officer hearing"}

# Both boards meet monthly and the agenda posts about a week ahead, so a live
# "current agenda" is at most ~5 weeks old, ~9 when a month is canceled.
STALE_AGENDA_DAYS = 75
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

# The layout the City adopted with its new site (first seen on the October 30,
# 2026 Public Officer agenda). One "ADDRESS:" line opens each dossier and every
# field is a labelled line. The parcel is now "CLT NUMBER", fees are "CODE
# FEES", and the inspection and condemnation dates were renamed.
_V2_ADDRESS = re.compile(r"^[ \t]*ADDRESS:[ \t]*(\d[^\n]*?)[ \t]*$", re.I | re.M)
_FIELD_V2 = {
    "parcel": re.compile(r"CLT NUMBER:?[ \t]*([A-Z0-9]+)", re.I),
    "owner_since": re.compile(r"OWNER SINCE:?[ \t]*([0-9/]+)", re.I),
    "violations": re.compile(r"VIOLATIONS:?[ \t]*([^\n]+)", re.I),
    "fees": re.compile(r"CODE FEES:?[ \t]*([^\n]+)", re.I),
    "record": re.compile(r"RECORD NUMBER:?[ \t]*([A-Z0-9\-]+)", re.I),
    "inspected": re.compile(r"FIRST INSPECTION:?[ \t]*([0-9/]+)", re.I),
    "condemned": re.compile(r"CONDEMNATION:?[ \t]*([0-9/NA]+)", re.I),
    "city_tax": re.compile(r"CITY TAXES:?[ \t]*([^\n]+)", re.I),
    "cty_tax": re.compile(r"COUNTY TAXES:?[ \t]*([^\n]+)", re.I),
    "zoning": re.compile(r"ZONING:?[ \t]*([^\n]+)", re.I),
    "tax_sale": re.compile(r"TAX SALE:?[ \t]*([^\n]+)", re.I),
    "historic": re.compile(r"HISTORIC:?[ \t]*([^\n]+)", re.I),
}
_V2_PARTIES = re.compile(
    r"OWNERS AND OTHER INTERESTED PARTIES:?(.+?)(?=^[ \t]*[A-Z][A-Z ]{2,30}:|\Z)",
    re.I | re.S | re.M)
_CANCELED = re.compile(r"\bCANCEL+ED\b", re.I)
_ANY_DATE = re.compile(
    r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|"
    r"NOVEMBER|DECEMBER)\s+(\d{1,2}),\s+(\d{4})\b", re.I)
_DATE_LINE = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})\s*$")
_HOUSE_LINE = re.compile(r"^\s*(\d+\s+[A-Za-z][^\n]*?)\s*$")
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


def links_from_board_page(html: str) -> dict:
    """{kind: url} for the 'current agenda' links on the City's board page.

    Matched on the link TEXT, not the document id, so a re-issued id does not
    strand the capture on a dead file the way the old fixed URL did.
    """
    out = {}
    for href, label in re.findall(
            r'<a\b[^>]*href="([^"]*/DocumentCenter/View/\d+[^"]*)"[^>]*>(.*?)</a>',
            html or "", re.I | re.S):
        text = _clean(re.sub(r"<[^>]+>", " ", label)).lower()
        if "agenda" not in text:
            continue
        for kind, hint in _LINK_HINT.items():
            if hint in text and kind not in out:
                out[kind] = href if href.startswith("http") else CITY + href
    return out


SCRAPFLY_API = "https://api.scrapfly.io/scrape"


def _via_scrapfly(url: str, key: str, timeout: int = 150) -> bytes:
    """One fetch through Scrapfly's residential pool. Returns the raw bytes."""
    import base64

    import requests

    r = requests.get(SCRAPFLY_API, timeout=timeout, params={
        "key": key, "url": url, "country": "us",
        "proxy_pool": os.environ.get("SCRAPFLY_PROXY_POOL") or "public_residential_pool"})
    res = (r.json() or {}).get("result") or {}
    if res.get("status_code") != 200:
        raise RuntimeError(f"residential fetch answered {res.get('status_code')} (api {r.status_code})")
    body = res.get("content") or ""
    return base64.b64decode(body) if res.get("format") == "binary" else body.encode("utf-8")


def http_get(url: str, timeout: int = 60, direct=None, residential=None) -> bytes:
    """The page's bytes. Direct first, then a residential address.

    The City's site sits behind Cloudflare, which answers 403 to a datacenter
    address whatever the headers say (measured from the Fly box 2026-11-12:
    direct and through the Apify datacenter proxy both 403, a residential
    address 200). A workstation is served directly, so the paid fetch only
    happens where the free one is refused.
    """
    import requests

    def _direct(u):
        r = requests.get(u, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        return r.content

    try:
        return (direct or _direct)(url)
    except Exception as exc:
        key = os.environ.get("SCRAPFLY_KEY", "")
        if not (residential or key):
            raise
        print(f"  direct fetch refused ({type(exc).__name__}: {str(exc)[:70]}); "
              "retrying from a residential address")
        return (residential or (lambda u: _via_scrapfly(u, key)))(url)


def discover_agenda_urls(timeout: int = 30) -> dict:
    """Live agenda URLs off the board page, falling back to the known ids."""
    urls = dict(AGENDAS)
    try:
        found = links_from_board_page(http_get(BOARD_PAGE, timeout).decode("utf-8", "replace"))
        for kind, url in found.items():
            if url != urls.get(kind):
                print(f"  {kind}: board page now links {url}")
            urls[kind] = url
        missing = [k for k in AGENDAS if k not in found]
        if missing:
            print(f"  board page shows no agenda link for {', '.join(missing)}; "
                  "using the last known document id")
    except Exception as exc:
        print(f"  board page unreadable ({type(exc).__name__}: {exc}); "
              "using the last known document ids")
    return urls


def fetch(scratch: str, timeout: int = 60) -> dict:
    """Download both agendas, archive by content hash, return {kind: path}."""
    arc = os.path.join(scratch, "condemnation_archive")
    os.makedirs(arc, exist_ok=True)
    out = {}
    for kind, url in discover_agenda_urls().items():
        fname = url.rsplit("/", 1)[-1]
        try:
            content = http_get(url, timeout)
        except Exception as exc:
            print(f"  fetch failed {kind} ({url}): {type(exc).__name__}: {exc}")
            continue
        if not content.startswith(b"%PDF-"):
            print(f"  {kind} ({fname}): not a PDF ({len(content)} bytes), skipping")
            continue
        h = hashlib.sha256(content).hexdigest()[:12]
        # Content hash, not date. The City re-uploads the same agenda repeatedly
        # and a date-keyed name would make a dozen copies of one cycle.
        path = os.path.join(arc, f"{kind}_{h}.pdf")
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(content)
            print(f"  NEW snapshot {kind}: {os.path.basename(path)} ({len(content):,} bytes)")
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
    return parse_agenda_text(_text(path), kind, os.path.basename(path))


def meeting_date(txt: str) -> str:
    """ISO meeting date from the agenda header, '' when there is none."""
    m = _MEETING_DATE.search(txt or "")
    if not m:
        return ""
    try:
        return datetime.strptime(_clean(m.group(1)), "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return _clean(m.group(1))


def is_canceled_text(txt: str) -> bool:
    """True for the one-paragraph notice the City posts when a meeting is off.

    A canceled month is a real answer, not a parse failure: there is no agenda
    to read, so zero entries is correct and must not raise an alert.
    """
    txt = txt or ""
    return bool(_CANCELED.search(txt)) and not _MEETING_DATE.search(txt) and len(txt) < 1500


def is_canceled(path: str) -> bool:
    return is_canceled_text(_text(path))


def newest_date_text(txt: str) -> str:
    """Latest 'MONTH D, YYYY' anywhere in the document, ISO, '' when none."""
    best = ""
    for m in _ANY_DATE.finditer(txt or ""):
        try:
            iso = datetime.strptime(
                f"{m.group(1).title()} {int(m.group(2))}, {m.group(3)}",
                "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        best = max(best, iso)
    return best


def staleness_problems(paths: dict, today: datetime | None = None) -> list[str]:
    """Problems when the agendas are too old to be the live ones.

    The check the old fixed URL needed and never had. If the newest date across
    every fetched document is more than STALE_AGENDA_DAYS behind today, the
    City has moved or stopped updating the files and the capture is reading a
    fossil, however healthy the HTTP response looks.
    """
    today = today or datetime.now()
    newest = ""
    for path in (paths or {}).values():
        try:
            newest = max(newest, newest_date_text(_text(path)))
        except Exception:
            continue
    if not newest:
        return ["condemnation agendas carry no readable date, cannot tell if they are current"]
    age = (today - datetime.strptime(newest, "%Y-%m-%d")).days
    if age > STALE_AGENDA_DAYS:
        return [f"condemnation agendas look STALE: newest date in them is {newest}, "
                f"{age} days ago. The City has probably moved the files: check {BOARD_PAGE}"]
    return []


def _parse_petitions_v2(txt: str, kind: str, meeting: str, agenda: str) -> list[dict]:
    """Dossiers in the labelled-line layout: one 'ADDRESS:' line opens each."""
    marks = list(_V2_ADDRESS.finditer(txt))
    out = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(txt)
        chunk = txt[mark.start():end]
        rec = {"source": "condemnation", "kind": kind, "address": _clean(mark.group(1)),
               "hearing": meeting, "agenda": agenda}
        for key, rx in _FIELD_V2.items():
            g = rx.search(chunk)
            rec[key] = _clean(g.group(1)) if g else ""
        parties = []
        pm = _V2_PARTIES.search(chunk)
        if pm:
            for line in pm.group(1).splitlines():
                # Each party is a bulleted line; the bullet is a non-ascii glyph
                # that sometimes lands on a line of its own.
                p = _clean(re.sub(r"^[^A-Za-z0-9]+", "", line))
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


def parse_agenda_text(txt: str, kind: str, agenda: str = "") -> list[dict]:
    """parse_agenda on text already extracted. Handles both City layouts."""
    meeting = meeting_date(txt)

    marks = [mm.start() for mm in re.finditer(r"PROPERTY IDENTIFICATION NO", txt, re.I)]
    if not marks:
        # No old-layout anchor at all: this is the labelled-line layout.
        return _parse_petitions_v2(txt, kind, meeting, agenda)
    path = agenda
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
    return parse_boardings_text(_text(path))


def _boarding(addr: str, boarded: str) -> dict:
    when = f" on {boarded}" if boarded else ""
    return {"source": "condemnation", "kind": "boarding",
            "address": addr, "boarded": boarded, "hearing": boarded,
            "parties": [], "owner_raw": "", "parcel": "", "record": "",
            "notes": (f"Boarded by the City{when}. Vacant and "
                      "secured, on the Public Officer docket.")}


def parse_boardings_text(txt: str) -> list[dict]:
    """parse_boardings on text already extracted. Handles both City layouts."""
    start = txt.upper().find("BOARDING APPROVAL")
    if start == -1:
        return []
    ends = [e for e in (txt.find("PUBLIC OFFICER REPAIR", start),
                        txt.upper().find("PETITION DETAILS", start)) if e != -1]
    blob = txt[start: min(ends) if ends else start + 2500]
    out = []
    for m in _BOARDED.finditer(blob):
        addr = _clean(m.group(1))
        if not re.match(r"^\d", addr):
            continue
        out.append(_boarding(addr, m.group(2)))
    if out:
        return out

    # New layout: the table is extracted column by column, every address under
    # an ADDRESS header and then every date under DATE BOARDED. The two lists
    # pair by position, and only when they are the same length: a date attached
    # to the wrong house is worse than no date.
    addrs, dates = [], []
    for line in blob.splitlines():
        d = _DATE_LINE.match(line)
        if d:
            dates.append(d.group(1))
            continue
        h = _HOUSE_LINE.match(line)
        if h:
            addrs.append(_clean(h.group(1)))
    paired = len(addrs) == len(dates)
    return [_boarding(a, dates[i] if paired else "") for i, a in enumerate(addrs)]


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


def collect(paths: dict, today: datetime | None = None) -> tuple[list[dict], list[str]]:
    """Parse every fetched agenda. Returns (entries, problems).

    One code path for the CLI and the scheduled capture stage, so the two can
    never disagree about what counts as a failure. A problem is anything that
    means a cycle may be slipping away: an agenda that was not fetched, one
    that parsed to nothing without being a cancellation notice, or a set of
    documents too old to be the live ones.
    """
    fresh: list[dict] = []
    problems: list[str] = []
    for kind in AGENDAS:
        path = (paths or {}).get(kind)
        if not path:
            problems.append(f"{kind} agenda was not fetched")
            continue
        try:
            txt = _text(path)
        except Exception as exc:
            problems.append(f"{kind} agenda unreadable: {type(exc).__name__}: {exc}")
            continue
        if is_canceled_text(txt):
            print(f"  {kind}: meeting canceled, nothing to capture this cycle")
            continue
        got = parse_agenda_text(txt, kind, os.path.basename(path))
        boards = parse_boardings_text(txt) if kind == "poh" else []
        print(f"  {kind}: {len(got)} propert(ies)"
              + (f", {len(boards)} boarding(s)" if kind == "poh" else ""))
        if not got and not boards:
            problems.append(f"{kind} agenda parsed 0 entries (the layout may have changed)")
        fresh.extend(got)
        fresh.extend(boards)
    problems.extend(staleness_problems(paths, today))
    return fresh, problems


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

    fresh, problems = collect(paths)
    for p in problems:
        # Loud on purpose: a silent empty here is a permanently lost cycle.
        print(f"  PROBLEM: {p}")
    if not fresh:
        if problems:
            print("Nothing parsed. The PDFs are archived so nothing is lost, but "
                  "this needs a look.")
            return 1
        print("No hearing this cycle (canceled). Nothing to add.")
        return 0

    allrec = merge(a.scratch, fresh)
    out = a.out or os.path.join(a.scratch, "condemnations.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(allrec, fh, indent=1)
    print(f"\nthis cycle: {len(fresh)}   cumulative: {len(allrec)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
