"""Code violations from a file a person exported (build plan 1.10). Knox only.

WHY A FILE AND NOT A SCRAPER. The City of Knoxville's record search is not a public API. Its
front end exposes the credentials of the City's own permitting service, and its 200 row cap
exists to stop exactly the kind of bulk pull a scheduled job would make. Automating against it
is not something this pipeline does. The same search has an ordinary "Export to Excel" button,
and a person using it by hand gets the hidden parcel and latitude and longitude columns as well.

So this feed is fed by the assistant: export the search, drop the file in the inbox on the
volume, and the next morning's run stages it for the county upload (buy box, true tax
delinquency, upside-down check, all the same rules a lien passes).

    <scratch>/inbox/code_violations/   .csv, .tsv, .txt, or the export's .xls / .html table

WHAT IS KEPT. Only the record types that mean distress, plus the two statuses the search hides
behind "closed": an Expired permit (a rehab somebody walked away from) and "Lien Owed - Closed"
(the City abated a nuisance and put a lien on the parcel). Everything else in an export is
routine permitting.

A FEED THAT DEPENDS ON A PERSON MUST SAY WHEN THE PERSON STOPPED. With the stage on, a morning
with no new file is fine for a while and then it is a failure: after CODE_VIOLATION_STALE_DAYS
(10) without a new file the stage fails by name, because a list that quietly stops arriving
looks exactly like a list with nothing on it.

A file that cannot be read (no address column, no rows) fails the stage and names the file and
the columns it did find. It is never skipped silently, and nothing in the inbox is ever moved or
deleted: a file is remembered by its content, so dropping the same export twice does nothing.

    CODE_VIOLATION_TYPES="board structure complaint,ce structure complaint,structure complaint,
                          irc/ibc demolition permit,dirty lot"
    CODE_VIOLATION_STATUSES="expired,lien owed - closed"
    CODE_VIOLATION_STALE_DAYS=10      0 = never fail for a missing file
    CODE_VIOLATION_MAX_AGE_DAYS=120   rows opened longer ago than this are not staged

    python src/code_violations.py --scratch output/knox     # read the inbox, stage, upload nothing
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from datetime import date, datetime
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ftm_staging  # noqa: E402

FEED = "code_violations"
FEED_KEY = "knox_code_violations"
SOURCE_URL = "https://knoxvilleworksearch.knoxvilletn.gov/advsearch.html"
FILES = "code_violation_files.json"
EXTENSIONS = (".csv", ".tsv", ".txt", ".xls", ".html", ".htm")

DEFAULT_TYPES = ("board structure complaint", "ce structure complaint", "structure complaint",
                 "irc/ibc demolition permit", "dirty lot")
DEFAULT_STATUSES = ("expired", "lien owed - closed")

# header (lowercased, punctuation dropped) -> our field. First match per field wins.
_COLUMNS = {
    "address": ("address", "site address", "location", "property address", "street address"),
    "record": ("record number", "record id", "record", "case number", "permit number", "number"),
    "type": ("record type", "type", "case type", "permit type"),
    "status": ("status", "record status"),
    "opened": ("opened", "opened date", "open date", "date opened", "file date", "date", "created"),
    "parcel": ("parcel", "parcel number", "parcel id", "parcel no"),
    "description": ("description", "project name", "short notes", "name"),
}


def _setting(name: str, default: tuple) -> set[str]:
    raw = os.environ.get(name)
    items = raw.split(",") if raw else list(default)
    return {" ".join(x.lower().split()) for x in items if x.strip()}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def inbox(scratch: str) -> str:
    return os.path.join(scratch, "inbox", "code_violations")


class _Table(HTMLParser):
    """The first table in an HTML export, as rows of cell text. The City's "Excel" export is a
    rendered HTML table with an .xls name."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def read_table(content: bytes) -> list[list[str]]:
    """Rows of cells from an export, whatever it calls itself. Raises ValueError when it is
    neither a table nor delimited text."""
    text = content.decode("utf-8-sig", "replace")
    head = text[:2000].lower()
    if "<table" in head or "<tr" in head or "<html" in head:
        p = _Table()
        p.feed(text)
        if not p.rows:
            raise ValueError("an HTML file with no table rows in it")
        return p.rows
    if content[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"):
        raise ValueError("a real Excel workbook, which this box cannot open. Save it as CSV and drop that instead")
    sample = text[:4000]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    rows = [[" ".join(c.split()) for c in r] for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ValueError("an empty file")
    return rows


def map_columns(header: list[str]) -> dict:
    """our field -> column index, for the headers this export uses."""
    norm = [" ".join(re.sub(r"[^a-z0-9 ]", " ", (h or "").lower()).split()) for h in header]
    out = {}
    for field, names in _COLUMNS.items():
        for name in names:
            if name in norm:
                out[field] = norm.index(name)
                break
    return out


def _iso(raw: str) -> str:
    s = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return ""
    return ""


def is_distress(kind: str, status: str, types: set[str], statuses: set[str]) -> bool:
    k, s = " ".join((kind or "").lower().split()), " ".join((status or "").lower().split())
    return k in types or s in statuses


def rows_from_table(table: list[list[str]], today: str) -> tuple[list[dict], dict]:
    """(staged rows, counts) for one export. Raises ValueError when the export has no address
    column, which is the one thing a record cannot be built without."""
    cols = map_columns(table[0])
    if "address" not in cols:
        raise ValueError("no address column. Its columns are: %s" % ", ".join(c for c in table[0] if c)[:200])
    types, statuses = _setting("CODE_VIOLATION_TYPES", DEFAULT_TYPES), _setting("CODE_VIOLATION_STATUSES", DEFAULT_STATUSES)
    max_age = _env_int("CODE_VIOLATION_MAX_AGE_DAYS", 120)
    counts = {"rows": 0, "kept": 0, "routine": 0, "no_address": 0, "too_old": 0}
    out = []

    def cell(row, field):
        i = cols.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    for row in table[1:]:
        counts["rows"] += 1
        address = cell(row, "address")
        if not re.match(r"^\d+\s+\S", address):
            counts["no_address"] += 1
            continue
        kind, status = cell(row, "type"), cell(row, "status")
        if not is_distress(kind, status, types, statuses):
            counts["routine"] += 1
            continue
        opened = _iso(cell(row, "opened"))
        if opened and max_age and (date.fromisoformat(today) - date.fromisoformat(opened)).days > max_age:
            counts["too_old"] += 1
            continue
        record = cell(row, "record")
        # The street line only: an export prints "123 Example St, Knoxville TN 37917".
        street = address.split(",")[0].strip()
        parcel = re.sub(r"[^0-9A-Za-z.-]", "", cell(row, "parcel"))
        key = record or "%s|%s|%s" % (street.upper(), kind.lower(), opened)
        counts["kept"] += 1
        out.append({
            "_key": key,
            "source": "knoxville_code_file", "feed": FEED_KEY, "notice_type": "code_violation",
            "address": street, "owner_raw": "", "case_number": record,
            "event_date": opened, "filed_date": opened,
            "tax_account": parcel,
            "notes": ("City of Knoxville record %s: %s, status %s%s.%s%s"
                      % (record or "(no number)", kind or "type not stated", status or "not stated",
                         (", opened %s" % opened) if opened else "",
                         (" %s." % cell(row, "description")[:160]) if cell(row, "description") else "",
                         (" Parcel %s." % parcel) if parcel else "")),
            "source_url": SOURCE_URL,
        })
    return out, counts


def _load_files(scratch: str) -> dict:
    try:
        with open(os.path.join(scratch, FILES), encoding="utf-8") as fh:
            got = json.load(fh)
            return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_files(scratch: str, files: dict) -> None:
    path = os.path.join(scratch, FILES)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(files, fh, indent=1)
    os.replace(tmp, path)


def prepare(scratch: str, today: str | None = None) -> dict:
    """Read every file in the inbox and stage the distress rows that have not been uploaded.

    The inbox is read whole every morning rather than "new files only": the staging ledger is
    what makes a row upload once, and re-reading means a row whose upload failed yesterday is
    staged again today without anyone dropping the file a second time."""
    today = today or date.today().isoformat()
    box = inbox(scratch)
    os.makedirs(box, exist_ok=True)
    files = _load_files(scratch)
    problems, rows, keys = [], [], []
    stats = {"files": 0, "new_files": 0, "rows": 0, "staged": 0, "routine": 0, "no_address": 0,
             "too_old": 0, "already_uploaded": 0}
    for name in sorted(os.listdir(box)):
        path = os.path.join(box, name)
        if not os.path.isfile(path) or not name.lower().endswith(EXTENSIONS):
            continue
        with open(path, "rb") as fh:
            content = fh.read()
        sha = hashlib.sha256(content).hexdigest()[:12]
        stats["files"] += 1
        try:
            staged, counts = rows_from_table(read_table(content), today)
        except ValueError as exc:
            problems.append("%s could not be used: %s" % (name, exc))
            continue
        if sha not in files:
            files[sha] = {"name": name, "first_seen": today, "rows": counts["rows"], "kept": counts["kept"]}
            stats["new_files"] += 1
        for k in ("rows", "routine", "no_address", "too_old"):
            stats[k] += counts[k]
        for r in staged:
            key = r.pop("_key")
            if key in keys:
                continue
            if not ftm_staging.is_new(scratch, FEED, key):
                stats["already_uploaded"] += 1
                continue
            keys.append(key)
            rows.append(r)
    _save_files(scratch, files)
    stats["staged"] = len(rows)
    ftm_staging.stage(scratch, FEED, rows, keys)

    last = max((f.get("first_seen") or "" for f in files.values()), default="")
    stats["last_file"] = last
    stats["days_since_file"] = (date.fromisoformat(today) - date.fromisoformat(last)).days if last else None
    stale = _env_int("CODE_VIOLATION_STALE_DAYS", 10)
    if stale > 0:
        if not last:
            problems.append("no code violation file has ever been dropped in %s. Export the City's search and "
                            "drop the file there, or take this stage out of the run." % box)
        elif stats["days_since_file"] > stale:
            problems.append("no new code violation file for %d days (the last arrived %s). This list is fed by "
                            "hand and has stopped arriving." % (stats["days_since_file"], last))
    stats["problems"] = problems
    return stats


def describe(stats: dict) -> str:
    if not stats["files"]:
        return "no file in the code violation inbox"
    said = "%d file(s) read (%d new), %d distress record(s) staged of %d rows" % (
        stats["files"], stats["new_files"], stats["staged"], stats["rows"])
    extra = ["%d %s" % (stats[k], label) for k, label in (
        ("already_uploaded", "already uploaded"), ("routine", "routine permits left out"),
        ("too_old", "too old"), ("no_address", "with no street address")) if stats[k]]
    return said + ((" (" + ", ".join(extra) + ")") if extra else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    a = ap.parse_args()
    stats = prepare(a.scratch)
    print(describe(stats))
    for p in stats["problems"]:
        print("  problem:", p)
    return 1 if stats["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
