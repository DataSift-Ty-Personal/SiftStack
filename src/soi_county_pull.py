"""SOI stage 2a: pull Columbus-metro county parcel/owner rolls to local files.

ArcGIS counties (Delaware, Licking, Pickaway, Union) paginate to output/soi/raw/{county}.jsonl.
Franklin + Fairfield come from bulk zips downloaded separately (see soi_owner_db.py loader).

Usage:
    python src/soi_county_pull.py                # pull all four ArcGIS counties
    python src/soi_county_pull.py --county union
"""

import argparse
import json
import time
from pathlib import Path

import requests

RAW_DIR = Path("output/soi/raw")

ARCGIS = {
    "delaware": {
        "url": "https://services2.arcgis.com/ziXVKVy3BiopMCCU/arcgis/rest/services/Parcel/FeatureServer/0/query",
        "fields": "OBJECTID,OWNER1,OWNER2,MAILNAME1,MAILNAME2,MAILADDR1,MAILADDR2,ADDR1,ADDR2,"
                  "MARKET_TOT,SALE_DATE,SALE_AMNT,YRBUILT,CLASS,SQFT,BDROOMS",
        "page": 2000,
    },
    "licking": {
        "url": "https://gis.lickingcounty.gov/server/rest/services/Auditor/ParcelsSearch/MapServer/0/query",
        "fields": "OBJECTID,Parcel,OwnerName,SiteAddress,SitePostalCity,SitePostalZip,Landuse,Class,"
                  "YearBuilt,LivingAreaSqFt,MarketTotalValue,OwnerAddress,OwnerCity,OwnerState,OwnerZip,"
                  "T1From,T1To,T1Date,T1SaleAmount,T1Valid",
        "page": 2000,
    },
    "pickaway": {
        "url": "https://services6.arcgis.com/FhJ42byMw3LmPYCN/arcgis/rest/services/Parcels_Search/FeatureServer/1/query",
        "fields": "OBJECTID,PARCELID,OWNER,ADDRESS,PPOwnerAdd,PPOwnerA_2,Link",
        "page": 2000,
    },
    "union": {
        "url": "https://www7.co.union.oh.us/unioncountyohio/rest/services/parcel/MapServer/0/query",
        "fields": "OBJECTID,ACCOUNT,Owner,OwnerCurrent,Address,houseNumber,StreetName,city,ZipCode,"
                  "ParcelClass,SaleDate,SaleMostRecent,PARCELTYPE",
        "page": 1000,
    },
}


def pull_county(name, cfg):
    out_path = RAW_DIR / f"{name}.jsonl"
    offset = 0
    total = 0
    with open(out_path, "w", encoding="utf-8") as out:
        while True:
            params = {
                "where": "1=1",
                "outFields": cfg["fields"],
                "returnGeometry": "false",
                "f": "json",
                "resultOffset": offset,
                "resultRecordCount": cfg["page"],
            }
            for attempt in range(3):
                try:
                    r = requests.get(cfg["url"], params=params, timeout=120)
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    print(f"  {name} offset {offset}: retry after {e}")
                    time.sleep(5)
            feats = data.get("features", [])
            if "error" in data:
                raise RuntimeError(f"{name}: {data['error']}")
            for feat in feats:
                out.write(json.dumps(feat.get("attributes", {})) + "\n")
            total += len(feats)
            print(f"  {name}: {total} rows", flush=True)
            if len(feats) < cfg["page"] and not data.get("exceededTransferLimit"):
                break
            offset += len(feats)
            time.sleep(0.3)
    print(f"{name}: DONE {total} rows -> {out_path}")
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", choices=sorted(ARCGIS), help="pull one county only")
    args = ap.parse_args()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    targets = [args.county] if args.county else sorted(ARCGIS)
    for name in targets:
        pull_county(name, ARCGIS[name])


if __name__ == "__main__":
    main()
