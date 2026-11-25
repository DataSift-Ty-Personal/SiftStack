"""Write the validated phone tags onto your DataSift owners, on your Open API key.

Takes phone_tags_for_datasift.csv (Phone Number, Phone Tag) from validate_phones.py and the
DataSift list the numbers came from. For every record on that list it reads the owner's phones,
and every phone whose number is in the CSV gets that tag as its ONE tier: any other tag from the
CSV's tier set (Dial First, Dial Second, Dial Third, Dial Fourth, Drop, plus any custom tier in
the file) comes off, so a re-scored phone never carries two tiers. Every other tag stays.

    python upload_phone_tags.py --csv phone_tags_for_datasift.csv --list "Probate"            # dry: what would change
    python upload_phone_tags.py --csv phone_tags_for_datasift.csv --list "Probate" --commit   # write, read a sample back
    python upload_phone_tags.py --csv phone_tags_for_datasift.csv --uuids <uuid>,<uuid> --commit
    python upload_phone_tags.py --selftest                                                    # the rules, no network

Writes go once per owner through POST /api/internal/owner/<uuid>/upsert-phones/ with each changed
phone's full object, then a sample of owners is read back; a phone without exactly the tag written
stops the run. Reads: the list lookup, the list's records (250 a request, up to 10,000) and one
read per record. DataSift allows 120 requests a minute per account, shared by every tool on it;
the script stays near 80.

Your key: REISIFT_API_KEY, your DataSift Open API key (see datasift_core.py). Without a key, upload
the CSV in the DataSift app instead: Upload, Update Data, "Tag phones by phone number".
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datasift_core import ApiError, DataSiftApi  # noqa: E402

DEFAULT_TIERS = ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")
LISTS = "/api/internal/list/?offset=0&limit=10000"
RECORDS = "/api/internal/property/"
WALL = 10000
PAGE = 250


def clean(n) -> str:
    d = re.sub(r"\D", "", str(n or ""))
    return d[-10:] if len(d) >= 10 else ""


def fold(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def load_tags(path) -> dict:
    """number -> tag from the CSV's "Phone Number" and "Phone Tag" columns (header case does not matter)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {fold(c): c for c in reader.fieldnames or []}
        num_col, tag_col = cols.get("phone number"), cols.get("phone tag")
        if not num_col or not tag_col:
            raise SystemExit("the CSV needs the columns Phone Number and Phone Tag (validate_phones.py writes them)")
        out = {}
        for row in reader:
            n, t = clean(row.get(num_col)), str(row.get(tag_col) or "").strip()
            if n and t:
                out[n] = t
    return out


def tag_names(p: dict) -> list:
    out = []
    for t in p.get("tags") or []:
        if isinstance(t, dict):
            t = t.get("title") or t.get("name") or t.get("tag")
        if t:
            out.append(str(t))
    return out


def payload(p: dict, tag: str, vocab: set) -> dict:
    """The FULL phone object upsert-phones needs, with exactly one tier on it."""
    tags = [t for t in tag_names(p) if t not in vocab]
    tags.append(tag)
    return {"number": clean(p.get("number")) or p.get("number"),
            "type": p.get("type") or "UNKNOWN",
            "tags": sorted(set(tags)),
            "status": (p.get("status") or "UNKNOWN").upper(),
            "is_connected": p.get("is_connected", True),
            "verified": p.get("verified", False)}


def list_uuid(api, title: str) -> str:
    r = api.get(LISTS)
    rows = r.get("results") if isinstance(r, dict) else r
    hit = next((x for x in rows or [] if fold(x.get("title")) == fold(title)), None)
    if not hit:
        raise SystemExit("there is no list called %r in this account" % title)
    return hit["uuid"]


def list_records(api, uuid: str) -> list:
    """Record ids on a list, by GET, up to the search's 10,000-row window."""
    out, offset = [], 0
    while offset < WALL:
        r = api.get(RECORDS, {"query": json.dumps({"must": {"any_lists": [uuid]}}), "limit": PAGE, "offset": offset})
        rows = (r or {}).get("results") or (r or {}).get("data") or []
        out += [x.get("uuid") for x in rows if x.get("uuid")]
        offset += len(rows)
        if not rows or offset >= int((r or {}).get("count") or 0):
            break
    if offset >= WALL:
        print("note: the list holds more than %d records; only the first %d are read" % (WALL, WALL))
    return out


def read_owners(api, uuids: list, out=print) -> dict:
    """owner uuid -> {"record": one record uuid, "phones": [...]}. An owner on several records is read once."""
    owners = {}
    for i, u in enumerate(dict.fromkeys(uuids), 1):
        try:
            rec = api.get("/api/internal/property/%s/" % u) or {}
        except ApiError as e:
            out("  could not read record %s: %s" % (u, str(e)[:100]))
            continue
        ow = rec.get("owner") or {}
        if ow.get("uuid") and ow["uuid"] not in owners:
            owners[ow["uuid"]] = {"record": u, "phones": ow.get("phones") or []}
        if i % 100 == 0:
            out("  read %d of %d records" % (i, len(uuids)))
    return owners


def plan(owners: dict, tags: dict) -> dict:
    """owner -> {"record", "phones": [payload...], "expect": {number: tag}}, plus the counts."""
    vocab = set(DEFAULT_TIERS) | set(tags.values())
    changes, already, seen = {}, 0, set()
    for owner, v in owners.items():
        changed, expect = [], {}
        for p in v["phones"]:
            n = clean(p.get("number"))
            if n not in tags:
                continue
            seen.add(n)
            want = tags[n]
            if [t for t in tag_names(p) if t in vocab] == [want]:
                already += 1
                continue
            changed.append(payload(p, want, vocab))
            expect[n] = want
        if changed:
            changes[owner] = {"record": v["record"], "phones": changed, "expect": expect}
    return {"owners": changes, "already": already, "numbers_found": len(seen),
            "numbers_in_csv": len(tags), "vocab": sorted(vocab)}


def write(api, p: dict, readback: int = 10, out=print) -> dict:
    """Write once per owner, then read a sample back. A mismatch stops the run."""
    failed = 0
    for owner, v in p["owners"].items():
        try:
            api.post("/api/internal/owner/%s/upsert-phones/" % owner, {"phones": v["phones"]})
        except ApiError as e:
            failed += 1
            out("  write failed for owner %s: %s" % (owner, str(e)[:120]))
    vocab, bad = set(p["vocab"]), 0
    sample = random.sample(sorted(p["owners"]), min(readback, len(p["owners"])))
    for owner in sample:
        v = p["owners"][owner]
        rec = api.get("/api/internal/property/%s/" % v["record"]) or {}
        phones = {clean(x.get("number")): x for x in (rec.get("owner") or {}).get("phones") or []}
        for num, want in v["expect"].items():
            got = [t for t in tag_names(phones.get(num) or {}) if t in vocab]
            if got != [want]:
                bad += 1
                out("  read-back: the number ending %s on record %s carries %s, not [%s]" % (num[-4:], v["record"], got, want))
    if bad:
        raise SystemExit("%d phones did not read back with the tag written; the run stopped" % bad)
    return {"owners": len(p["owners"]), "phones": sum(len(v["phones"]) for v in p["owners"].values()),
            "failed": failed, "read_back": len(sample), "mismatches": bad}


# ---- selftest: invented records, no network ----
class FakeApi:
    def __init__(self):
        self.calls = []
        self.owners = {
            "o1": {"uuid": "o1", "phones": [
                {"number": "5550000101", "type": "MOBILE", "tags": [{"title": "Dial Third"}, {"title": "dispo"}],
                 "status": "unknown"},
                {"number": "5550000102", "type": "LANDLINE", "tags": ["Dial First"], "status": "CORRECT"},
                {"number": "5550000199", "type": "MOBILE", "tags": [], "status": "UNKNOWN"}]},
            "o2": {"uuid": "o2", "phones": [{"number": "(555) 000-0103", "type": "MOBILE", "tags": []}]},
        }
        self.records = {"r1": "o1", "r2": "o1", "r3": "o2"}

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        if path == LISTS:
            return {"count": 1, "results": [{"uuid": "l1", "title": "Probate"}]}
        if path == RECORDS:
            rows = [{"uuid": u} for u in self.records]
            off = int((params or {}).get("offset") or 0)
            return {"count": len(rows), "results": rows[off:off + PAGE]}
        m = re.match(r"^/api/internal/property/([^/]+)/$", path)
        if m:
            return {"uuid": m.group(1), "owner": json.loads(json.dumps(self.owners[self.records[m.group(1)]]))}
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(("POST", path))
        owner = re.match(r"^/api/internal/owner/([^/]+)/upsert-phones/$", path).group(1)
        for new in body["phones"]:
            for old in self.owners[owner]["phones"]:
                if clean(old["number"]) == clean(new["number"]):
                    old.update(new)
        return {}


def selftest() -> int:
    results = []

    def check(label, got, want):
        results.append((label, got == want, got, want))

    quiet = lambda *_: None  # noqa: E731
    tags = {"5550000101": "Dial First", "5550000102": "Dial First", "5550000103": "Drop", "5550000777": "Dial Second"}
    api = FakeApi()
    owners = read_owners(api, list_records(api, list_uuid(api, "probate")), out=quiet)
    check("an owner on two records is read once", sorted(owners), ["o1", "o2"])
    p = plan(owners, tags)
    o1 = p["owners"]["o1"]["phones"]
    check("only numbers in the CSV change", [x["number"] for x in o1], ["5550000101"])
    check("the old tier comes off and other tags stay", o1[0]["tags"], ["Dial First", "dispo"])
    check("a phone already carrying its tag is left alone", p["already"], 1)
    check("a number written with punctuation still matches", p["owners"]["o2"]["expect"], {"5550000103": "Drop"})
    check("a number on no owner is counted, not guessed", (p["numbers_found"], p["numbers_in_csv"]), (3, 4))
    check("the status is sent in its stored case", o1[0]["status"], "UNKNOWN")
    before = len([c for c in api.calls if c[0] == "POST"])
    res = write(api, p, readback=5, out=quiet)
    check("one write per owner", len([c for c in api.calls if c[0] == "POST"]) - before, 2)
    check("every written phone reads back with one tier", (res["mismatches"], res["read_back"]), (0, 2))
    again = plan(read_owners(api, ["r1", "r3"], out=quiet), tags)
    check("a second run finds nothing to change", (again["owners"], again["already"]), ({}, 3))
    bad = 0
    for label, ok, got, want in results:
        bad += 0 if ok else 1
        print("%s %s" % ("ok  " if ok else "FAIL", label) + ("" if ok else ": %r (want %r)" % (got, want)))
    print("%d of %d passed" % (len(results) - bad, len(results)))
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write validated phone tags onto DataSift owners, on your Open API key "
                                             "(dry until --commit)")
    ap.add_argument("--csv", help="phone_tags_for_datasift.csv from validate_phones.py")
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--list", dest="list_name", help="the DataSift list the numbers came from")
    who.add_argument("--uuids", help="comma list of record uuids")
    ap.add_argument("--commit", action="store_true", help="write the tags (dry without it)")
    ap.add_argument("--readback", type=int, default=10, help="owners read back after the write")
    ap.add_argument("--selftest", action="store_true", help="check the rules on invented records, no network")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.csv or not (args.list_name or args.uuids):
        ap.error("--csv and one of --list or --uuids are required")
    tags = load_tags(args.csv)
    api = DataSiftApi()
    try:
        uuids = (list_records(api, list_uuid(api, args.list_name)) if args.list_name
                 else [u.strip() for u in args.uuids.split(",") if u.strip()])
        print("%d records, %d numbers in the CSV; reading each record's owner..." % (len(uuids), len(tags)))
        p = plan(read_owners(api, uuids), tags)
        n = sum(len(v["phones"]) for v in p["owners"].values())
        print("%d of the CSV's numbers are on these owners: %d phones to retag on %d owners, %d already right"
              % (p["numbers_found"], n, len(p["owners"]), p["already"]))
        if not args.commit:
            print("DRY: nothing sent. Add --commit to write the tags.")
            return 0
        res = write(api, p, args.readback)
        print("wrote %d phones on %d owners, %d failed; read back %d owners, %d mismatches"
              % (res["phones"], res["owners"], res["failed"], res["read_back"], res["mismatches"]))
    except ApiError as e:
        print("DataSift refused a request, so the run stopped: %s" % e)
        return 1
    finally:
        print("(%d requests)" % api.calls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
