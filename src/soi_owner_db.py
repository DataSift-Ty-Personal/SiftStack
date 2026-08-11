"""SOI stage 2b: build the unified Columbus-metro owners database.

Loads six county sources (output/soi/raw/) into one SQLite table:
    Franklin  franklin_parcel.csv  (auditor bulk CSV, owner + mailing + values + transfer)
    Fairfield fairfield_cama.zip   (nightly iasWorld CAMA dump: OWNDAT/PARDAT/APRVAL/DWELL)
    Delaware/Licking/Pickaway/Union  {county}.jsonl from soi_county_pull.py

Usage:
    python src/soi_owner_db.py                 # load whatever raw files exist
    python src/soi_owner_db.py --counties fairfield,delaware
"""

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

RAW = Path("output/soi/raw")
DB_PATH = Path("output/soi/owners.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY,
    county TEXT, parcel_id TEXT, owner1 TEXT, owner2 TEXT,
    mail_name TEXT, mail_addr TEXT, mail_csz TEXT,
    situs_addr TEXT, situs_city TEXT, situs_zip TEXT,
    market_total REAL, sale_date TEXT, sale_price REAL,
    year_built INTEGER, sqft REAL, beds INTEGER,
    land_use TEXT, owner_occupied INTEGER, link TEXT
);
CREATE INDEX IF NOT EXISTS idx_owners_county ON owners(county);
"""


def epoch_ms(v):
    if not v:
        return ""
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def dmy(v):
    """iasWorld '14-MAR-06' style dates."""
    if not v:
        return ""
    try:
        d = datetime.strptime(v.strip(), "%d-%b-%y")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return ""


ADDR_NOISE = re.compile(r"[^A-Z0-9 ]")


def addr_key(addr):
    """Loose street key: leading number + first street word."""
    a = ADDR_NOISE.sub(" ", (addr or "").upper()).split()
    if not a:
        return ""
    num = a[0] if a[0].isdigit() else ""
    words = [t for t in a if not t.isdigit()]
    return f"{num} {words[0]}" if num and words else ""


def occ(mail_addr, situs_addr):
    mk, sk = addr_key(mail_addr), addr_key(situs_addr)
    return 1 if mk and mk == sk else 0


def load_delaware(cur):
    n = 0
    for line in open(RAW / "delaware.jsonl", encoding="utf-8"):
        d = json.loads(line)
        o1 = (d.get("OWNER1") or "").strip()
        if not o1 or o1 == "0":
            continue
        o2 = (d.get("OWNER2") or "").strip()
        mail1 = (d.get("MAILADDR1") or "").strip()
        cur.execute("INSERT INTO owners VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "delaware", str(d.get("OBJECTID") or ""), o1, "" if o2 == "0" else o2,
            (d.get("MAILNAME1") or "").strip(), mail1, (d.get("MAILADDR2") or "").strip(),
            (d.get("ADDR1") or "").strip(), (d.get("ADDR2") or "").split(",")[0].strip(),
            (d.get("ADDR2") or "").rsplit(" ", 1)[-1] if d.get("ADDR2") else "",
            d.get("MARKET_TOT") or 0, epoch_ms(d.get("SALE_DATE")), d.get("SALE_AMNT") or 0,
            d.get("YRBUILT") or 0, d.get("SQFT") or 0, d.get("BDROOMS") or 0,
            str(d.get("CLASS") or ""), occ(mail1, d.get("ADDR1")), "",
        ))
        n += 1
    return n


def load_licking(cur):
    n = 0
    for line in open(RAW / "licking.jsonl", encoding="utf-8"):
        d = json.loads(line)
        o1 = (d.get("OwnerName") or "").strip()
        if not o1:
            continue
        mail = (d.get("OwnerAddress") or "").strip()
        situs = (d.get("SiteAddress") or "").strip()
        csz = " ".join(str(d.get(k) or "").strip() for k in ("OwnerCity", "OwnerState", "OwnerZip"))
        cur.execute("INSERT INTO owners VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "licking", (d.get("Parcel") or "").strip(), o1, "",
            "", mail, csz.strip(),
            situs, (d.get("SitePostalCity") or "").strip(), str(d.get("SitePostalZip") or "").strip(),
            d.get("MarketTotalValue") or 0, epoch_ms(d.get("T1Date")), d.get("T1SaleAmount") or 0,
            d.get("YearBuilt") or 0, d.get("LivingAreaSqFt") or 0, 0,
            (d.get("Landuse") or "").strip(), occ(mail, situs), "",
        ))
        n += 1
    return n


def load_pickaway(cur):
    n = 0
    seen = set()
    for line in open(RAW / "pickaway.jsonl", encoding="utf-8"):
        d = json.loads(line)
        pid = (d.get("PARCELID") or "").strip()
        o1 = (d.get("OWNER") or "").strip()
        if not o1 or pid in seen:
            continue
        seen.add(pid)
        mail = (d.get("PPOwnerAdd") or "").strip()
        situs = (d.get("ADDRESS") or "").strip()
        cur.execute("INSERT INTO owners VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "pickaway", pid, o1, "",
            "", mail, (d.get("PPOwnerA_2") or "").strip(),
            situs, "", "",
            0, "", 0, 0, 0, 0, "", occ(mail, situs), (d.get("Link") or "").strip(),
        ))
        n += 1
    return n


def load_union(cur):
    n = 0
    seen = set()
    for line in open(RAW / "union.jsonl", encoding="utf-8"):
        d = json.loads(line)
        o1 = ((d.get("OwnerCurrent") or d.get("Owner")) or "").strip()
        acct = (d.get("ACCOUNT") or "").strip()
        if not o1 or not acct or acct in seen:
            continue
        seen.add(acct)
        situs = (d.get("Address") or "").strip()
        cur.execute("INSERT INTO owners VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "union", acct, o1, "",
            "", "", "",
            situs, (d.get("city") or "").strip(),
            str(d.get("ZipCode") or "").rsplit(" ", 1)[-1],
            0, epoch_ms(d.get("SaleDate")), 0, 0, 0, 0,
            str(d.get("ParcelClass") or ""), 0, "",
        ))
        n += 1
    return n


def _cama_rows(zf, name):
    with zf.open(name) as f:
        # iasWorld dumps have a blank line after the header row.
        text = io.TextIOWrapper(f, "latin-1")
        reader = csv.reader(text)
        header = next(reader)
        header = [h.strip() for h in header]
        for row in reader:
            if not row or all(not c for c in row):
                continue
            yield dict(zip(header, row))


def load_fairfield(cur):
    zf = zipfile.ZipFile(RAW / "fairfield_cama.zip")
    par, apr, dwl = {}, {}, {}
    for d in _cama_rows(zf, "PARDAT.DAT"):
        if d.get("DEACTIVAT"):
            continue
        situs = " ".join(filter(None, [d.get("ADRNO"), d.get("ADRDIR"), d.get("ADRSTR"), d.get("ADRSUF")]))
        par[d["PARID"]] = (situs, d.get("CITYNAME") or "", d.get("ZIP1") or "", d.get("LUC") or "")
    for d in _cama_rows(zf, "APRVAL.DAT"):
        if not d.get("DEACTIVAT"):
            apr[d["PARID"]] = d.get("APRTOT") or 0
    for d in _cama_rows(zf, "DWELL.DAT"):
        if not d.get("DEACTIVAT"):
            dwl[d["PARID"]] = (d.get("YRBLT") or 0, d.get("SFLA") or 0, d.get("RMBED") or 0)
    n = 0
    for d in _cama_rows(zf, "OWNDAT.DAT"):
        if d.get("DEACTIVAT"):
            continue
        pid = d["PARID"]
        o1 = (d.get("OWN1") or "").strip()
        if not o1:
            continue
        situs, city, zip1, luc = par.get(pid, ("", "", "", ""))
        yb, sfla, beds = dwl.get(pid, (0, 0, 0))
        mail = (d.get("PADDR1") or "").strip()
        csz = " ".join(filter(None, [(d.get("PADDR2") or "").strip(), (d.get("PADDR3") or "").strip()]))
        cur.execute("INSERT INTO owners VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "fairfield", pid, o1, (d.get("OWN2") or "").strip(),
            "", mail, csz, situs, city, zip1,
            apr.get(pid, 0), "", 0, yb, sfla, beds, luc, occ(mail, situs), "",
        ))
        n += 1
    return n


def load_franklin(cur):
    path = RAW / "franklin_parcel.csv"
    with open(path, newline="", encoding="latin-1") as f:
        reader = csv.reader(f)
        header = [h.strip().lstrip("﻿").lstrip("ï»¿") for h in next(reader)]
        ix = {h: i for i, h in enumerate(header)}

        def g(row, name):
            i = ix.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""

        n = 0
        for row in reader:
            o1 = g(row, "NAME1")
            if not o1:
                continue
            # MAILAD1-4 are free-form lines: name line(s), street, city/state/zip last.
            mail_lines = [g(row, f"MAILAD{i}") for i in range(1, 5)]
            mail_lines = [m for m in mail_lines if m]
            mail_csz = mail_lines[-1] if len(mail_lines) >= 2 else ""
            mail_addr = mail_lines[-2] if len(mail_lines) >= 3 else (g(row, "OWNER_ADD1") or "")
            situs = g(row, "STADDR")
            trandt = g(row, "TRANDT")
            try:
                trandt = datetime.strptime(trandt, "%m/%d/%Y").strftime("%Y-%m-%d") if trandt else ""
            except ValueError:
                trandt = ""
            cur.execute("INSERT INTO owners VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "franklin", g(row, "PARCEL ID"), o1, g(row, "NAME2"),
                mail_lines[0] if mail_lines else "", mail_addr, mail_csz,
                situs, g(row, "USPS_CITY"), g(row, "ZIPCODE"),
                float(g(row, "APPRTOT") or 0), trandt, float(g(row, "PRICE") or 0),
                int(float(g(row, "YEARBLT") or 0)), float(g(row, "AREA_A") or 0),
                int(float(g(row, "BEDRMS") or 0)),
                g(row, "LANDUSE"), occ(mail_addr, situs), "",
            ))
            n += 1
    return n


LOADERS = {
    "delaware": load_delaware,
    "licking": load_licking,
    "pickaway": load_pickaway,
    "union": load_union,
    "fairfield": load_fairfield,
    "franklin": load_franklin,
}

SOURCES = {
    "delaware": "delaware.jsonl", "licking": "licking.jsonl",
    "pickaway": "pickaway.jsonl", "union": "union.jsonl",
    "fairfield": "fairfield_cama.zip", "franklin": "franklin_parcel.csv",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--counties", help="comma list; default = all with raw files present")
    args = ap.parse_args()
    targets = args.counties.split(",") if args.counties else [
        c for c, src in SOURCES.items() if (RAW / src).exists()
    ]
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    for county in targets:
        con.execute("DELETE FROM owners WHERE county = ?", (county,))
        n = LOADERS[county](con)
        con.commit()
        print(f"{county}: {n} owner rows", flush=True)
    total = con.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
    print(f"total owners: {total} -> {DB_PATH}")
    con.close()


if __name__ == "__main__":
    main()
