"""Put back the courthouse sale dates the API uploader dropped (2026-08-14 to 2026-11-11).

datasift_api_upload.FIELD_MAP had no "Foreclosure Date", so every foreclosure uploaded through the
API landed with no sale date anywhere on the record. The scrape's own CSVs kept the date, so it is
recoverable: read the run CSVs, take each foreclosure's latest sale date (a postponed sale is
republished with a new one), find the record by address, and write the custom field.

Dry by default: it reads, matches and counts, and writes nothing. --commit writes the field, one
record at a time, and reads the first one back before it does the rest.

    python src/agents/cli.py autopilot backfill-sale-dates --csv "output/ftm_notices_2026-11-*.csv"
    python src/agents/cli.py autopilot backfill-sale-dates --csv "..." --commit
"""
from __future__ import annotations

import csv
import glob
import sys
from datetime import date, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def sale_dates(patterns: list[str]) -> dict:
    """(street, zip5) -> {"sale": latest ISO date, "city": city} from every foreclosure row."""
    from datasift_api_upload import iso_date
    out: dict = {}
    files = sorted({p for pat in patterns for p in glob.glob(pat)})
    for path in files:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if not (r.get("Notice Type") or "").lower().startswith("foreclos"):
                    continue
                sale = iso_date(r.get("Foreclosure Date") or "")
                street = " ".join((r.get("Property Street Address") or "").split())
                zip5 = str(r.get("Property ZIP Code") or "")[:5]
                if not (sale and street):
                    continue
                key = (street.upper(), zip5)
                if key not in out or sale > out[key]["sale"]:
                    out[key] = {"sale": sale, "street": street, "zip": zip5, "city": r.get("Property City") or ""}
    return {"files": files, "dates": out}


def find_record(crm, street: str, zip5: str) -> str:
    """The record at this address, or "". DataSift standardizes streets on ingest ("Street" is
    stored as "St"), so a prefix search on the raw street misses; search on the house number and
    the first street word, then keep only a candidate with the same house number, the same street
    name after abbreviations, and the same ZIP. Two candidates means no match, never a guess."""
    from .sources import same_street
    toks = street.split()
    if len(toks) < 2:
        return ""
    body = {"limit": 25, "offset": 0, "query": {"must": {"property_type": "clean",
                                                          "search": "address_prefix:%s %s" % (toks[0], toks[1])}}}
    r = crm.call("/api/internal/property/", "POST", body, headers={"x-http-method-override": "GET"})
    rows = (r.get("results") or r.get("data") or []) if isinstance(r, dict) else []
    hits = []
    for row in rows:
        a = row.get("address") or {}
        z = str(a.get("zip5") or a.get("postal_code") or "")[:5]
        if same_street(street, a.get("street") or "") and (not zip5 or not z or z == zip5):
            hits.append(row.get("uuid") or "")
    return hits[0] if len(hits) == 1 else ""


def _current(crm, uuid: str, label: str) -> str:
    r = crm.call("/api/internal/property/%s/custom-field/" % uuid)
    items = r if isinstance(r, list) else ((r or {}).get("results") or (r or {}).get("data") or [])
    for it in items:
        if ((it.get("custom_field") or {}).get("label") or "") == label:
            return str(it.get("value") or "")[:10]
    return ""


def run(patterns: list[str], commit: bool = False, crm=None, today: date | None = None, soon_days: int = 21) -> dict:
    from datasift_api_upload import field_index, find_property, write_fields
    if crm is None:
        from .crm import CRM
        crm = CRM()
    today = today or date.today()
    src = sale_dates(patterns)
    idx = field_index(crm.api)
    f = idx.get("Foreclosure Date")
    if not f:
        raise SystemExit("this account has no custom field named Foreclosure Date")
    out = {"files": len(src["files"]), "addresses": len(src["dates"]), "matched": 0, "unmatched": 0,
           "already_set": 0, "to_write": 0, "written": 0, "ahead": 0, "inside_window": 0, "problems": []}
    verified = False
    for key, d in sorted(src["dates"].items(), key=lambda kv: kv[1]["sale"]):
        uuid = (find_property(crm.api, {"street": d["street"], "postal_code": d["zip"]})
                or find_record(crm, d["street"], d["zip"]))
        if not uuid:
            out["unmatched"] += 1
            continue
        out["matched"] += 1
        sale = date.fromisoformat(d["sale"])
        if sale >= today:
            out["ahead"] += 1
            if sale <= today + timedelta(days=soon_days):
                out["inside_window"] += 1
        if _current(crm, uuid, "Foreclosure Date") == d["sale"]:
            out["already_set"] += 1
            continue
        out["to_write"] += 1
        if not commit:
            continue
        warn: set = set()
        if write_fields(crm.api, uuid, [{"field_uuid": f["uuid"], "value": d["sale"]}], warn):
            out["written"] += 1
        else:
            out["problems"].append("%s: %s" % (d["street"], sorted(warn)))
            continue
        if not verified:
            got = _current(crm, uuid, "Foreclosure Date")
            verified = True
            if got != d["sale"]:
                out["problems"].append("read-back on %s gave %r, not %s; stopped" % (d["street"], got, d["sale"]))
                break
    return out
