"""Selftest: returned mail read from a scanned stack (agents/workflows/returned_mail.py). Zero network:
the page reader, the PDF splitter, the files on the thread and DataSift are all fakes, and every name,
street, ZIP and number here is invented (the 555 exchange is never assigned).
"""
from __future__ import annotations

import contextlib
import copy
import csv
import io
import json
import os
import types
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

ET = ZoneInfo("America/New_York")
EM, EN = chr(0x2014), chr(0x2013)
CHANNEL, THREAD = "C0RETURN", "1700000500.000100"

R_ONE = "bbbbbbbb-0000-4000-8000-000000000001"     # 12 Testwood Dr 37917, Pat Example
R_OTHER = "bbbbbbbb-0000-4000-8000-000000000002"   # 7 Maple Test Ct 37919, owned by somebody else
R_NORTH = "bbbbbbbb-0000-4000-8000-000000000003"   # 40 N Elm Test St 37920, Dana Testerson
R_FWD = "bbbbbbbb-0000-4000-8000-000000000004"     # 15 Birch Test Ave 37922, Kim Exemplar, already tagged
R_TWIN_A = "bbbbbbbb-0000-4000-8000-000000000005"  # 44 Twin Oak Ln 37918, one of two records there
R_TWIN_B = "bbbbbbbb-0000-4000-8000-000000000006"
R_CITY = "bbbbbbbb-0000-4000-8000-000000000007"    # 3 Cedar Test Way, Maryville 37801, Jo Mockley
R_NONAME = "bbbbbbbb-0000-4000-8000-000000000008"  # 60 Blank Test Rd 37924, an owner with no name


def _rec(u, street, zip_, first, last, city="Knoxville", tags=(), company=""):
    return {"uuid": u, "status": "prospecting", "tags": list(tags), "lists": [], "notes": "",
            "address": {"street": street, "city": city, "state": "TN", "postal_code": zip_, "zip5": zip_},
            "owner": {"uuid": "o-" + u[-4:], "first_name": first, "last_name": last, "company": company,
                      "deceased": False, "phones": [],
                      "address": {"street": "1 Mailbox Test Ln", "city": city, "state": "TN", "postal_code": zip_}}}


def records():
    return [_rec(R_ONE, "12 Testwood Dr", "37917", "Pat", "Example"),
            _rec(R_OTHER, "7 Maple Test Ct", "37919", "Morgan", "Otherly"),
            _rec(R_NORTH, "40 N Elm Test St", "37920", "Dana", "Testerson"),
            _rec(R_FWD, "15 Birch Test Ave", "37922", "Kim", "Exemplar", tags=["Return Mail"]),
            _rec(R_TWIN_A, "44 Twin Oak Ln", "37918", "Sam", "Twin"),
            _rec(R_TWIN_B, "44 Twin Oak Ln", "37918", "Sam", "Twin"),
            _rec(R_CITY, "3 Cedar Test Way", "37801", "Jo", "Mockley", city="Maryville"),
            _rec(R_NONAME, "60 Blank Test Rd", "37924", "", "")]


# ---------------------------------------------------------------------- fakes
class FakeCRM:
    """Reads and the two writes the workflow may make. Any other call (an owner change, a status, a
    phone) fails the test, which is how 'nothing else is touched' is proven."""

    def __init__(self, recs, tags=("Return Mail", "Verify Deceased")):
        self.recs = {r["uuid"]: copy.deepcopy(r) for r in recs}
        self.tags = set(tags)
        self.writes, self.searches, self.reads = [], [], []
        self.fail_note: set = set()
        self.extra_rows: list = []

    def search(self, must, limit=250, offset=0):
        self.searches.append(copy.deepcopy(must))
        q = str(must.get("search") or "").split(":", 1)[1].upper()
        rows = [{"uuid": u, "address": dict(r["address"]), "status": r.get("status")}
                for u, r in self.recs.items() if r["address"]["street"].upper().startswith(q)]
        rows += [row for row in self.extra_rows if row["address"]["street"].upper().startswith(q)]
        return len(rows), rows[:limit]

    def record(self, u):
        self.reads.append(u)
        return copy.deepcopy(self.recs.get(u) or {})

    def tag_uuid(self, title):
        return ("tag-" + title) if title in self.tags else ""

    def add_tags(self, u, add, rec=None):
        self.writes.append(("add_tags", u, list(add)))
        have = self.recs[u].setdefault("tags", [])
        for t in add:
            if t not in have:
                have.append(t)
        return have

    def message(self, u, text, pin=False):
        self.writes.append(("message", u, text))
        if u not in self.fail_note:
            self.recs[u]["notes"] = (self.recs[u].get("notes") or "") + "\n" + text
        return ""

    def __getattr__(self, name):
        raise AssertionError("returned mail called %s on DataSift" % name)


class FakePdf:
    """A 'PDF' whose file holds the list of page keys; each page renders as the bytes PAGE:<key>."""

    def __init__(self, path):
        self.keys = json.loads(Path(path).read_text(encoding="utf-8"))
        self.closed = False

    def __len__(self):
        return len(self.keys)

    def render(self, i):
        return ("PAGE:%s" % self.keys[i]).encode(), "image/jpeg"

    def close(self):
        self.closed = True


class FakeVision:
    def __init__(self, pages: dict, fail=()):
        self.pages, self.fail, self.calls = pages, set(fail), []

    def __call__(self, image, media, label):
        key = image.decode().split(":", 1)[1]
        self.calls.append(key)
        if key in self.fail:
            raise RuntimeError("the reader is down")
        return dict(self.pages[key]), 1500, 120


def front(name, street, zip_, city="Knoxville", reason="none", text="", fwd="", readable=True):
    return {"side": "address", "recipient": name, "street": street, "city": city, "state": "TN", "zip": zip_,
            "reason": reason, "reason_text": text, "forwarding": fwd, "readable": readable}


def back(reason="none", text="", fwd=""):
    return {"side": "back", "recipient": "", "street": "", "city": "", "state": "", "zip": "", "reason": reason,
            "reason_text": text, "forwarding": fwd, "readable": True}


OTHER = {"side": "other", "recipient": "", "street": "", "city": "", "state": "", "zip": "", "reason": "none",
         "reason_text": "", "forwarding": "", "readable": True}
UNREAD = dict(OTHER, side="unreadable")
FWD = "99 Newplace Test Rd, Maryville TN 37801"

# The stack: front first, a cover sheet at the end, and a photo of a second envelope to the first house.
PAGES = {
    "p1": front("Pat Example", "12 Testwood Drive", "37917"), "p2": back("vacant", "VACANT"),
    "p3": front("Lee Sample", "7 Maple Test Ct", "37919"), "p4": back(),
    "p5": front("Dana Testerson", "40 North Elm Test Street", "37920", reason="deceased", text="DECEASED"), "p6": back(),
    "p7": front("Kim Exemplar", "15 Birch Test Ave", "37922", reason="forwarding_address", fwd=FWD), "p8": back(),
    "p9": front("Sam Twin", "44 Twin Oak Ln", "37918"), "p10": back(),
    "p11": front("Ray Nobody", "500 Missing Test Rd", "37923"), "p12": back(),
    "p13": UNREAD, "p14": front("Blurry Person", "", "37917", readable=False), "p15": OTHER,
    "x1": front("Pat Example (865) 555-0199", "12 Testwood Dr", "37917", reason="not_deliverable"),
}
PDF_KEYS = ["p%d" % i for i in range(1, 16)]


@contextlib.contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _no_phone(text: str) -> bool:
    """No phone number in the words. A link is taken out first: the signed file link carries a random
    signature, and ten digits in a row inside it are not a phone number (it failed the full suite
    once, 2026-11-15, when an earlier section left a public URL set)."""
    import re
    from .workflows import returned_mail as rm
    return not rm._PHONE.search(re.sub(r"https?://\S+", "", text or ""))


def _clean(text: str) -> bool:
    return EM not in (text or "") and EN not in (text or "") and "\x08" not in (text or "")


# ---------------------------------------------------------------------- the section
def run(check, tmp: Path) -> None:
    from . import store, workflows
    from .workflows import returned_mail as rm
    store.init()
    saved = {"ws": config.WORKSPACES_DIR, "mode": config.MODE, "key": config.ANTHROPIC_API_KEY,
             "cap": config.DAILY_CAP_USD}
    hooks = (rm.VISION, rm.SPLIT, rm.FILES, rm.CRM, rm.NOW, rm._pdfium)
    registry = dict(workflows.REGISTRY)
    config.WORKSPACES_DIR = tmp / "rm_workspaces"
    # Earlier sections book spend on the same throwaway day; this section tests its own caps, so the
    # brain's daily cap is moved out of the way except in the one check that is about it.
    config.MODE, config.ANTHROPIC_API_KEY, config.DAILY_CAP_USD = "dry", "", 100000.0
    rm.NOW = datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)
    keys = [k for k in os.environ if k.startswith("AGENTS_RETURNED_MAIL_")]
    env_saved = {k: os.environ.pop(k) for k in keys}
    try:
        _page_checks(check, rm)
        _envelope_checks(check, rm)
        _match_checks(check, rm)
        files = _stage_files(tmp)
        _dry_checks(check, rm, store, files)
        _live_checks(check, rm, store, files)
        _bound_checks(check, rm, store, files, tmp)
        _reader_checks(check, rm, tmp)
        _registry_checks(check, workflows)
        _hygiene_checks(check)
    finally:
        rm.VISION, rm.SPLIT, rm.FILES, rm.CRM, rm.NOW, rm._pdfium = hooks
        config.WORKSPACES_DIR, config.MODE = saved["ws"], saved["mode"]
        config.ANTHROPIC_API_KEY, config.DAILY_CAP_USD = saved["key"], saved["cap"]
        workflows.REGISTRY.clear()
        workflows.REGISTRY.update(registry)
        for k in [k for k in os.environ if k.startswith("AGENTS_RETURNED_MAIL_")]:
            os.environ.pop(k, None)
        os.environ.update(env_saved)


def _task(inputs=None, source="agent:ops", thread=THREAD):
    from . import store
    tid, _ = store.enqueue("ops", "returned_mail", "", subject="", force=True,
                           payload={"inputs": inputs or {}, "details": ""}, source=source,
                           slack_channel=CHANNEL if thread else "", slack_thread=thread)
    return store.task(tid)


def _stage_files(tmp: Path) -> dict:
    """The stack as the employee's turn saved it: a PDF and a photo in Quinn's workspace inbox."""
    ws = Path(config.WORKSPACES_DIR) / "ops"
    folder = ws / "inbox" / ("%s-%s" % (CHANNEL, THREAD))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01-stack.pdf").write_text(json.dumps(PDF_KEYS), encoding="utf-8")
    (folder / "02-extra.jpg").write_bytes(b"PAGE:x1")
    (folder / "03-boundary.kml").write_text("<kml/>", encoding="utf-8")
    rel = "inbox/%s-%s" % (CHANNEL, THREAD)
    rows = [{"name": "stack.pdf", "kind": "pdf", "path": rel + "/01-stack.pdf", "why": ""},
            {"name": "extra.jpg", "kind": "photo", "path": rel + "/02-extra.jpg", "why": ""},
            {"name": "boundary.kml", "kind": "kmz", "path": rel + "/03-boundary.kml", "why": ""}]
    return {"rows": rows, "folder": folder, "ws": ws}


def _setup(rm, files, crm=None, vision=None, rows=None):
    rm.SPLIT = FakePdf
    rm.FILES = (lambda task, ws, _rows=rows if rows is not None else files["rows"]: [dict(r) for r in _rows])
    rm.CRM = crm or FakeCRM(records())
    rm.VISION = vision or FakeVision(PAGES)
    return rm.CRM, rm.VISION


# ---------------------------------------------------------------------- one page
def _page_checks(check, rm) -> None:
    print("  reading one page")
    check(rm.parse_reply('{"side": "address"}') == {"side": "address"}
          and rm.parse_reply('Here it is:\n```json\n{"side": "back"}\n```') == {"side": "back"}
          and rm.parse_reply("I could not read this page.") == {},
          "the reader's JSON is found whole, inside a fence or a sentence, and nothing is invented when there is none")
    c = rm.conform({"side": "address", "recipient": "Pat Example (865) 555-0101", "street": "12 Testwood Dr",
                    "city": "Knoxville", "state": "tn", "zip": "37917-1234", "reason": "weird",
                    "reason_text": "", "forwarding": "", "readable": True})
    check(c["recipient"] == "Pat Example" and c["zip"] == "37917" and c["state"] == "TN" and c["reason"] == "none",
          "a phone number is taken out of the page's text, a ZIP+4 keeps its five digits and an unknown reason with no words is none")
    check(rm.conform({"side": "address", "street": "1 Test Rd", "zip": "3791", "state": "Tenn", "reason": "odd",
                      "reason_text": "MOVED LEFT NO ADDRESS", "readable": True})["zip"] == ""
          and rm.conform({"state": "Tenn"})["state"] == ""
          and rm.conform({"reason": "odd", "reason_text": "MOVED LEFT NO ADDRESS"})["reason"] == "other",
          "a ZIP that is not five digits is dropped, never padded, and an unknown reason with words is other")
    check(rm.conform({"side": "address", "street": "1 Test Rd", "forwarding": "2 New Test Rd", "readable": True})["reason"]
          == "forwarding_address", "a forwarding address with no reason given is a forwarding address")
    check(rm.conform({"side": "address", "street": "", "readable": True})["readable"] is False
          and rm.conform({"side": "sideways"})["side"] == "unreadable" and rm.conform("not a dict")["side"] == "unreadable",
          "an address side with no street, an unknown side and an answer that is not an object all read as unreadable")
    check(rm.clean_text("Moved%sleft%sno address" % (EM, EN)) == "Moved-left-no address",
          "a long or short dash in the page's text becomes a plain hyphen")


# ---------------------------------------------------------------------- pages into envelopes
def _rows(*fields):
    return [{"n": i, "file": "s.pdf", "page": i, "fields": rm_conform(f), "error": ""} for i, f in enumerate(fields, 1)]


def rm_conform(f):
    from .workflows import returned_mail as rm
    return rm.conform(f)


def _envelope_checks(check, rm) -> None:
    print("  pages into envelopes")
    envs, other = rm.envelopes(_rows(front("A One", "1 Test Rd", "37917"), back("vacant", "VACANT"),
                                     front("B Two", "2 Test Rd", "37917", reason="deceased"), back("refused")))
    check(len(envs) == 2 and envs[0]["pages"] == [1, 2] and envs[0]["reason"] == "vacant" and envs[1]["pages"] == [3, 4]
          and envs[1]["reason"] == "deceased" and other == 0,
          "a stack scanned front first pairs each back with the front before it, and the back lends its reason only when the front has none")
    envs, _ = rm.envelopes(_rows(back("vacant"), front("A One", "1 Test Rd", "37917"), back(), front("B Two", "2 Test Rd", "37917")))
    check(len(envs) == 2 and envs[0]["pages"] == [1, 2] and envs[0]["reason"] == "vacant" and envs[1]["pages"] == [3, 4],
          "a stack scanned back first pairs each back with the front after it")
    envs, other = rm.envelopes(_rows(front("A One", "1 Test Rd", "37917"), back(), OTHER, back("deceased", "DECEASED"),
                                     front("C Three", "3 Test Rd", "37917"), back()))
    c3 = [e for e in envs if e.get("street") == "3 Test Rd"][0]
    stray = [e for e in envs if e.get("why") == "a back page with no front beside it"]
    check(c3["reason"] == "none" and c3["pages"] == [5, 6] and len(stray) == 1 and stray[0]["result"] == "unreadable"
          and other == 1,
          "after a misread page a back with no front of its own stands alone for a person, and its deceased stamp is never lent to the next envelope")
    envs, _ = rm.envelopes(_rows(front("A One", "1 Test Rd", "37917", reason="refused"), back("vacant")))
    check(envs[0]["reason"] == "refused", "the front's own reason wins over the back's")
    rows = _rows(UNREAD, front("Blurry", "4 Test Rd", "37917", readable=False), OTHER)
    rows.append({"n": 4, "file": "s.pdf", "page": 4, "fields": None, "error": "past the 3-page limit for one ask"})
    envs, other = rm.envelopes(rows)
    check([e["result"] for e in envs] == ["unreadable", "unreadable", "not_read"] and other == 1,
          "an unreadable page and an address the reader was not sure of are each a row for a person; a page not read is kept as not read; a cover sheet is only counted")


# ---------------------------------------------------------------------- an envelope to its record
def _env_of(name, street, zip_, city="Knoxville"):
    return {"recipient": name, "street": street, "zip": zip_, "city": city, "state": "TN", "result": "", "why": "",
            "record": "", "pages": [1], "file": "s.pdf", "reason": "none", "reason_text": "", "forwarding": ""}


def _match_checks(check, rm) -> None:
    print("  an envelope to its record")
    crm = FakeCRM(records())
    reads = rm.Reads(100)
    rows, kind, _ = rm.candidates(crm, _env_of("Pat Example", "12 Testwood Drive", "37917"), reads)
    check(not kind and [r["uuid"] for r in rows] == [R_ONE] and crm.searches[-1] == {"property_type": "clean",
                                                                                    "search": "address_prefix:12 Testwood"},
          "a record is found on the house number and the first street word, and Drive on the envelope matches Dr on the record")
    rows, kind, why = rm.candidates(crm, _env_of("Sam Twin", "44 Twin Oak Ln", "37918"), reads)
    check(kind == "two_matches" and not rows and "2 records" in why, "two records at one address is no match, never a guess")
    _, kind, _ = rm.candidates(crm, _env_of("Pat Example", "12 Testwood Dr", "37999"), reads)
    check(kind == "no_match", "the same street in another ZIP is not the record")
    rows, kind, _ = rm.candidates(crm, _env_of("Jo Mockley", "3 Cedar Test Way", "", city="Maryville"), reads)
    _, kind2, why2 = rm.candidates(crm, _env_of("Jo Mockley", "3 Cedar Test Way", "", city=""), reads)
    check(not kind and rows[0]["uuid"] == R_CITY and kind2 == "unreadable" and "no ZIP and no city" in why2,
          "with no ZIP the city must agree, and an address with neither is never matched")
    before = len(crm.searches)
    rows, kind, _ = rm.candidates(crm, _env_of("Dana Testerson", "40 North Elm Test Street", "37920"), reads)
    check(not kind and rows[0]["uuid"] == R_NORTH and len(crm.searches) == before + 2,
          "North on the envelope finds a record stored as N, by searching both spellings")
    before = len(crm.searches)
    _, kind, why = rm.candidates(crm, _env_of("Pat Example", "PO Box 12", "37917"), reads)
    _, kind2, why2 = rm.candidates(crm, _env_of("Pat Example", "Testwood Dr", "37917"), reads)
    check(kind == "no_match" and "PO box" in why and kind2 == "unreadable" and "house number" in why2
          and len(crm.searches) == before, "a PO box and a street with no house number are never searched")
    crowd = FakeCRM(records())
    crowd.extra_rows = [{"uuid": "cccccccc-0000-4000-8000-%012d" % i,
                         "address": {"street": "88 Crowded Test Rd", "postal_code": "37917", "zip5": "37917"}}
                        for i in range(100)]
    _, kind, why = rm.candidates(crowd, _env_of("Pat Example", "88 Crowded Test Rd", "37917"), rm.Reads(100))
    check(kind == "two_matches" and "more records start with" in why,
          "a search that comes back full cannot prove it found the only record, so it is no match")

    rec = records()[0]
    check(rm.surname_fits("Pat Example", rec)[0] == "yes" and rm.surname_fits("The Estate of Pat Example", rec)[0] == "yes"
          and rm.surname_fits("Chris Example-Ross", rec)[0] == "yes" and rm.surname_fits("EXAMPLE PAT", rec)[0] == "yes",
          "the envelope fits when it carries the owner's surname, written any way round, with an estate's words or a hyphenated name")
    obrien = dict(rec, owner=dict(rec["owner"], last_name="O'Brien"))
    vanb = dict(rec, owner=dict(rec["owner"], last_name="Van Buren"))
    check(rm.surname_fits("Pat OBrien", obrien)[0] == "yes" and rm.surname_fits("Pat Van Buren", vanb)[0] == "yes"
          and rm.surname_fits("Pat Vance", vanb)[0] == "no",
          "an apostrophe and a surname particle do not break a match, and a particle alone never makes one")
    check(rm.surname_fits("Lee Sample", rec)[0] == "no" and rm.surname_fits("Pat Examples", rec)[0] == "no",
          "a different surname is a different person, even one letter longer")
    check(rm.surname_fits("Current Resident", rec)[0] == "unknown" and rm.surname_fits("", rec)[0] == "unknown"
          and rm.surname_fits("Pat Example", records()[7])[0] == "unknown",
          "an envelope that names nobody, and an owner with no name, are for a person to check")

    crm = FakeCRM(records())
    reads, cache = rm.Reads(100), {}
    e1, e2 = _env_of("Pat Example", "12 Testwood Dr", "37917"), _env_of("Pat Example", "12 Testwood Drive", "37917")
    rm.match(crm, e1, reads, cache)
    rm.match(crm, e2, reads, cache)
    check(e1["record"] == e2["record"] == R_ONE and not e1["result"] and crm.reads.count(R_ONE) == 1,
          "two envelopes to one house read its record once")
    e3 = _env_of("Lee Sample", "7 Maple Test Ct", "37919")
    rm.match(crm, e3, reads, cache)
    check(e3["result"] == "different_name" and "Morgan Otherly" in e3["why"] and e3["record"] == R_OTHER,
          "a matched record owned by someone else is listed as a different name, with both names for the person")
    tight = rm.Reads(1)
    try:
        rm.match(crm, _env_of("Pat Example", "12 Testwood Dr", "37917"), tight, {})
        capped = False
    except rm.ReadCap:
        capped = True
    check(capped, "one ask's DataSift reads are capped, and the cap stops the lookups instead of being passed")


# ---------------------------------------------------------------------- the run, dry
def _dry_checks(check, rm, store, files) -> None:
    print("  a dry run")
    crm, vision = _setup(rm, files)
    t = _task({"scan_date": "2026-11-12"})
    r = rm.run(t)
    c = r.get("counts") or {}
    check(r["ok"] and not r["live"] and r["pages"] == 16 and r["read"] == 16 and len(vision.calls) == 16,
          "a dry box asked in Slack reads every page, so the plan says what a live run would flag")
    check(crm.writes == [], "a dry run writes nothing to DataSift")
    check(r["envelopes"] == 9 and c["would_flag"] == 4 and r["records"] == 3 and c["different_name"] == 1
          and c["two_matches"] == 1 and c["no_match"] == 1 and c["unreadable"] == 2 and r["matched"] == 5
          and r["other_pages"] == 1,
          "the stack reads as 9 envelopes: 4 would flag on 3 records, 1 different name, 1 shared address, 1 with no record, 2 unreadable, the cover sheet skipped")
    check(r["deceased"] == 1 and r["forwarding"] == 1, "the deceased stamp and the forwarding label are counted")
    path = Path(r["run_dir"]) / "files" / rm.CSV_NAME
    text = path.read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    check(r["files"] == [rm.CSV_NAME] and len(rows) == 9 and rows[0]["Result"] == "would flag"
          and any(x["Result"] == "a different name" and x["Owner of record"] == "Morgan Otherly" for x in rows),
          "the CSV has one row per envelope with what happened and, for a different name, the owner of record")
    check(_no_phone(text) and "555" not in text and _clean(text),
          "the CSV carries no phone number, even one printed on an envelope, and no long dash")
    usd = store.spent(task_id=int(t["id"]))
    check(abs(usd - 16 * 0.0002) < 0.0006 and r["usd"] > 0, "every page read is booked against the task from the reader's own token counts")
    text = rm.render(t, r)
    check("dry run, nothing written" in text and "Would flag 4 envelopes on 3 records" in text
          and "Needs a person: 5" in text and _no_phone(text) and _clean(text),
          "the dry answer says it wrote nothing, what it would flag and how many need a person, with no phone number")
    check("Reading cost under a cent." in text and "$0.00" not in text,
          "a read that cost a fraction of a cent says so instead of claiming it cost nothing")
    check("/files/%d/" % t["id"] in text and text.rstrip().endswith("returned_mail.csv"),
          "the CSV is linked in full, the signed link untouched by the phone scrub")
    nofile = FakeCRM(records(), tags=())
    rm.CRM, rm.VISION = nofile, FakeVision(PAGES)
    r2 = rm.run(_task({"scan_date": "2026-11-12"}))
    check(r2["counts"]["would_flag"] == 4 and nofile.writes == []
          and any("Return Mail and Verify Deceased do not exist in this account yet" in p for p in r2["problems"]),
          "a dry run says when the tags a live run needs are missing from the account")


# ---------------------------------------------------------------------- the run, live
def _live_checks(check, rm, store, files) -> None:
    from .autopilot import ledger, undo
    print("  a live run")
    config.MODE = "live"
    try:
        crm, _ = _setup(rm, files)
        owner_before = copy.deepcopy(crm.recs[R_FWD]["owner"])
        t = _task({"scan_date": "2026-11-13"})
        r = rm.run(t)
        c = r["counts"]
        check(r["live"] and c["flagged"] == 4 and r["records"] == 3, "a live run flags the 4 envelopes that fit, on 3 records")
        tagged = {w[1]: w[2] for w in crm.writes if w[0] == "add_tags"}
        notes = {w[1]: w[2] for w in crm.writes if w[0] == "message"}
        check(tagged.get(R_ONE) == ["Return Mail"] and "Return Mail" in crm.recs[R_ONE]["tags"],
              "a flagged record is tagged Return Mail")
        check(set(notes) == {R_ONE, R_NORTH, R_FWD} and [w[1] for w in crm.writes if w[0] == "message"].count(R_ONE) == 1,
              "each flagged record gets one note, even when two envelopes came back from it")
        n1 = notes[R_ONE]
        check("scanned 2026-11-13" in n1 and "vacant" in n1 and "not deliverable as addressed" in n1
              and "12 Testwood Drive" in n1 and "stack.pdf" in n1 and "extra.jpg" in n1,
              "the note carries the scan date, every reason the post office gave and where in the scan each envelope is")
        check(_no_phone(n1) and "555" not in n1 and _clean(n1), "the note carries no phone number and no long dash")
        check(tagged.get(R_NORTH) == ["Return Mail", "Verify Deceased"] and "not marked dead" in notes[R_NORTH]
              and crm.recs[R_NORTH]["owner"]["deceased"] is False,
              "a deceased stamp adds Verify Deceased and nothing more: the owner is never marked dead")
        check(FWD.split(",")[0] in notes[R_FWD] and "nothing on the owner was changed" in notes[R_FWD]
              and crm.recs[R_FWD]["owner"] == owner_before,
              "a forwarding address goes in the note for a person and is never written into the owner's mailing address")
        check(R_FWD not in tagged, "a record already tagged Return Mail is not tagged again; it still gets the note")
        check(not any(w[1] in (R_OTHER, R_TWIN_A, R_TWIN_B) for w in crm.writes),
              "nothing is written to a record whose owner has another name, or to either record at a shared address")
        changes = ledger.changes(run_id=r["run_id"])
        steps = undo.plan(r["run_id"])
        check(len(changes) == 2 and all(ch["what"] == "add_tags" for ch in changes)
              and sorted(s["args"] for s in steps if s["do"] == "remove_tags") == [["Return Mail"], ["Return Mail", "Verify Deceased"]],
              "every tag added is remembered with its run, and undo takes exactly those tags off")
        text = rm.render(t, r)
        check("Flagged 4 envelopes on 3 records" in text and ("autopilot undo %s" % r["run_id"]) in text
              and "notes stay" in text and _no_phone(text) and _clean(text),
              "the live answer says what was flagged and how to take the tags off, and that a note stays")
        before = len(crm.writes)
        r2 = rm.run(_task({"scan_date": "2026-11-13"}))
        check(r2["counts"]["already"] == 4 and r2["counts"]["flagged"] == 0 and len(crm.writes) == before,
              "the same scan posted again writes nothing: each record says already flagged from this scan")
        r3 = rm.run(_task({"scan_date": "2026-11-14"}))
        check(r3["counts"]["flagged"] == 4 and len([w for w in crm.writes[before:] if w[0] == "add_tags"]) == 0,
              "a later scan of the same house adds a new note but no second tag")

        # Scans of other days from here on: the runs above already flagged these houses from the 13th and 14th.
        missing = FakeCRM(records(), tags=("Verify Deceased",))
        rm.CRM, rm.VISION = missing, FakeVision(PAGES)
        r4 = rm.run(_task({"scan_date": "2026-11-10"}))
        check(missing.writes == [] and r4["counts"]["no_tag"] == 4
              and any("The tag Return Mail does not exist" in p and "create it in DataSift" in p for p in r4["problems"]),
              "a live run writes nothing while the Return Mail tag is missing from the account, and says to create it")
        flaky = FakeCRM(records())
        flaky.fail_note = {R_ONE}
        rm.CRM, rm.VISION = flaky, FakeVision(PAGES)
        r5 = rm.run(_task({"scan_date": "2026-11-09"}))
        check(r5["counts"]["write_failed"] == 2 and r5["counts"]["flagged"] == 2,
              "a note that does not read back on the record is reported as written but not read back, never as flagged")
        only_failed = FakeCRM(records())
        only_failed.fail_note = {R_ONE, R_NORTH, R_FWD}
        rm.CRM, rm.VISION = only_failed, FakeVision(PAGES)
        r6 = rm.run(_task({"scan_date": "2026-11-08"}))
        check(r6["counts"]["flagged"] == 0 and r6.get("run_id") and any(
            s["do"] == "remove_tags" for s in undo.plan(r6["run_id"])),
              "a run whose notes all failed to read back still names its run, so the tags it did add can come off")
    finally:
        config.MODE = "dry"


# ---------------------------------------------------------------------- the cost bounds and the refusals
def _bound_checks(check, rm, store, files, tmp) -> None:
    print("  the cost bounds and the refusals")
    with _env(AGENTS_RETURNED_MAIL_ASK_PAGES=10):
        crm, vision = _setup(rm, files)
        t = _task({})
        r = rm.run(t)
        text = rm.render(t, r)
        check(r["ok"] and r["held"] == "needs_go" and vision.calls == [] and crm.searches == []
              and "Say go" in text and "$0.02" in text and "16 pages" in text,
              "a stack longer than the pages read without asking gets the page count and the estimate, and nothing is read")
        with _env(AGENTS_RETURNED_MAIL_MAX_PAGES=12):
            r = rm.run(_task({"go": "yes"}))
        check(r["read"] == 12 and r["counts"]["not_read"] >= 1 and len(vision.calls) == 12,
              "with go the stack is read, never past the page limit for one ask; the rest is kept as not read")
    with _env(AGENTS_RETURNED_MAIL_RUN_USD="0.0005", AGENTS_RETURNED_MAIL_PAGE_USD="0.0003"):
        _, vision = _setup(rm, files)
        t = _task({})
        r = rm.run(t)
        check(0 < r["read"] < 16 and any("spending cap" in p for p in r["problems"])
              and store.spent(task_id=int(t["id"])) <= 0.0006,
              "the dollar cap for one ask is checked before each page, and the read stops there")
    config.DAILY_CAP_USD = 0.0
    try:
        _, vision = _setup(rm, files)
        r = rm.run(_task({}))
        check(r["read"] == 0 and vision.calls == [], "the brain's daily cap stops the read too")
    finally:
        config.DAILY_CAP_USD = 100000.0
    with _env(AGENTS_RETURNED_MAIL_DRY_READ="0"):
        _, vision = _setup(rm, files)
        t = _task({})
        r = rm.run(t)
        check(r["held"] == "dry" and vision.calls == [] and "switched off" in rm.render(t, r),
              "reading on a dry box can be switched off, and then nothing is read")
    _, vision = _setup(rm, files)
    r = rm.run(_task({}, source="autopilot:somewhere", thread=""))
    check(r["held"] == "dry" and vision.calls == [], "on a dry box only a person's ask in Slack starts a read")
    rm.VISION = None
    r = rm.run(_task({}))
    check(not r["ok"] and "No Claude key" in r["say"], "with no Claude key on the box nothing is read, and it says so")

    _setup(rm, files, rows=[])
    r = rm.run(_task({}))
    check(not r["ok"] and "Post the scan here" in r["say"], "a thread with no scan gets a plain sentence on what to post")
    big = [{"name": "huge.pdf", "kind": "pdf", "path": "", "why": "larger than 20 MB"}]
    _setup(rm, files, rows=big)
    r = rm.run(_task({}))
    check(not r["ok"] and "huge.pdf: larger than 20 MB" in r["say"] and "black and white" in r["say"],
          "a scan too big to save is named with the reason and how to send a smaller one")
    crm, vision = _setup(rm, files)
    r = rm.run(_task({"files": "extra.jpg"}))
    check(r["ok"] and r["pages"] == 1 and vision.calls == ["x1"], "the ask can name the one file to read")
    r = rm.run(_task({"files": "nothere.pdf"}))
    check(not r["ok"] and "no file named nothere.pdf" in r["say"].lower(), "a file the thread does not hold is refused by name")
    outside = tmp / "elsewhere_scan.pdf"
    outside.write_text(json.dumps(["p1"]), encoding="utf-8")
    r = rm.run(_task({"files": "../../elsewhere_scan.pdf"}))
    check(not r["ok"] and "outside the workspace" in r["say"] and vision.calls == ["x1"],
          "a path outside the employee's workspace is never opened")
    r = rm.run(_task({"files": "inbox/%s-%s/02-extra.jpg" % (CHANNEL, THREAD)}))
    check(r["ok"] and r["pages"] == 1, "a path to a file in the workspace is read")
    check(not rm.run(_task({"scan_date": "last week"}))["ok"]
          and "in the future" in rm.run(_task({"scan_date": "2999-01-01"}))["say"],
          "a scan date that cannot be read, or one in the future, is refused before anything is read")
    t = _task({"scan_date": "11/12/2026", "files": "extra.jpg"})
    check(rm.scan_date(t, "11/12/2026")[:2] == ("2026-11-12", "scanned 2026-11-12"),
          "a scan date written month first is read too")
    day = datetime.fromisoformat(t["created"]).astimezone(ET).date().isoformat()
    check(rm.scan_date(t, "")[:2] == (day, "from the scan posted %s" % day),
          "with no scan date the note names the day the scan was posted, and says posted")
    with _env(AGENTS_RETURNED_MAIL_MAX_READS=3):
        _setup(rm, files)
        r = rm.run(_task({}))
    check(r["counts"]["not_looked_up"] > 0 and any("Stopped looking up" in p for p in r["problems"]),
          "past the read limit for one ask the rest of the envelopes are not looked up, and the answer says so")
    _setup(rm, files, vision=FakeVision(PAGES, fail={"p1", "p2", "p3"}))
    r = rm.run(_task({}))
    check(r["read"] == 0 and any("3 pages in a row" in p for p in r["problems"]),
          "a reader that fails three pages running stops the read instead of paying for the rest")


# ---------------------------------------------------------------------- the PDF reader and the model call
def _reader_checks(check, rm, tmp) -> None:
    print("  the PDF reader and the model call")
    files = _stage_files(tmp)
    _setup(rm, files)
    rm.SPLIT = None

    def no_pdf():
        raise rm.NoPdfReader("this box cannot split a PDF into pages yet (the pypdfium2 package is not installed); "
                             "post photos of the envelopes instead")
    saved = rm._pdfium
    rm._pdfium = no_pdf
    try:
        r = rm.run(_task({}))
    finally:
        rm._pdfium = saved
    check(r["ok"] and r["pages"] == 1 and any("cannot split a PDF" in x["why"] for x in r["skipped"]),
          "a box without the PDF library says so for the PDF and still reads the photos")
    try:
        import pypdfium2  # noqa: F401
        from PIL import Image
        have = True
    except ImportError:
        have = False
    if have:
        pdf = tmp / "rm_probe.pdf"
        pages = [Image.new("RGB", (684, 297), "white") for _ in range(2)]
        pages[0].save(pdf, "PDF", save_all=True, append_images=pages[1:], resolution=72)
        pp = rm.PdfPages(pdf)
        data, media = pp.render(0)
        size = Image.open(io.BytesIO(data)).size
        n = len(pp)
        pp.close()
        check(n == 2 and media == "image/jpeg" and data[:2] == b"\xff\xd8" and max(size) == rm.LONG_SIDE,
              "a real PDF splits into JPEG pages rendered at the long edge the reader sees in full")
    else:
        print("  (pypdfium2 is not installed here: the real PDF split is checked where it is)")

    import anthropic
    calls = []

    class _Msg:
        def __init__(self, text, stop="end_turn"):
            self.content = [types.SimpleNamespace(type="text", text=text)]
            self.stop_reason = stop
            self.usage = types.SimpleNamespace(input_tokens=1400, output_tokens=90)

    replies = []

    class _Client:
        def __init__(self, api_key=None, **kw):
            self.messages = types.SimpleNamespace(create=self._create)

        def _create(self, **kw):
            calls.append(kw)
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    saved_client = anthropic.Anthropic
    anthropic.Anthropic = _Client
    try:
        replies[:] = [_Msg(json.dumps(PAGES["p1"]))]
        fields, tin, tout = rm.ask_claude(b"\xff\xd8jpeg", "image/jpeg", "page 1 of private-name.pdf")
        kw = calls[-1]
        content = kw["messages"][0]["content"]
        check(fields["street"] == "12 Testwood Drive" and (tin, tout) == (1400, 90) and kw["model"] == "claude-haiku-5-5"
              and kw["thinking"] == {"type": "disabled"} and kw["output_config"]["effort"] == "low"
              and kw["output_config"]["format"]["type"] == "json_schema",
              "each page goes to Claude Haiku 5.5 with thinking off, low effort and the strict JSON schema")
        check(content[0]["type"] == "image" and content[0]["source"]["type"] == "base64"
              and content[0]["source"]["media_type"] == "image/jpeg" and "private-name" not in json.dumps(content),
              "the page is sent as an image block, and the file's name is not sent to the model")
        replies[:] = [_Msg('{"side": "address"', stop="max_tokens")]
        check(rm.ask_claude(b"x", "image/jpeg", "p")[0] == {"side": "unreadable"},
              "a reply cut off by the token limit is an unreadable page, never half an address")
        try:
            import httpx
            bad = anthropic.BadRequestError("schema refused", response=httpx.Response(
                400, request=httpx.Request("POST", "https://api.example.test/v1/messages")), body=None)
        except Exception:  # noqa: BLE001  (an SDK whose error cannot be built here skips this one check)
            bad = None
        if bad is not None:
            calls.clear()
            replies[:] = [bad, _Msg("Sure: " + json.dumps(PAGES["p2"]))]
            fields, _, _ = rm.ask_claude(b"x", "image/jpeg", "p")
            check(fields["side"] == "back" and len(calls) == 2 and "format" not in calls[1]["output_config"],
                  "a model that refuses the schema is asked again without it, and the JSON is read out of the plain reply")
    finally:
        anthropic.Anthropic = saved_client


# ---------------------------------------------------------------------- registry and hygiene
def _registry_checks(check, workflows) -> None:
    print("  registry")
    workflows.REGISTRY["returned_mail"] = "returned_mail"
    problems = [p for p in workflows.problems() if p.startswith("returned_mail")]
    found = workflows.handler("returned_mail")
    check(not problems and found is not None and all(callable(f) for f in found),
          "returned_mail loads as a workflow with run and render")


def _hygiene_checks(check) -> None:
    print("  hygiene")
    here = Path(__file__).resolve().parent
    for p in (here / "workflows" / "returned_mail.py", here / "selftest_returnmail.py"):
        text = p.read_text(encoding="utf-8")
        check(_clean(text), "%s carries no long dash and no backspace byte" % p.name)
