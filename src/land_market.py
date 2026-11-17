"""Knoxville region land market: how much land there is, which of it is real, who buys it, how to market it.

Ty, 2026-11-09: size the land market across Knoxville and the counties around it, verify the parcels the way the
Union OH vacant-land workflow did, find the end buyers for each county, and build the marketing plan. The model is
the one settled the same day: buy at farm price, entitle, and sell the approved land (no horizontal development);
plus the builder lot desk for finished and infill lots.

    python src/land_market.py --phase gis        # public layers: TN parcels, census roads, flood, Knox planning
    python src/land_market.py --phase tam        # every parcel in 11 counties, sized by lane and owner
    python src/land_market.py --phase screen     # Union-style verification, per lane
    python src/land_market.py --phase topo       # USGS 3DEP slope on the survivors
    python src/land_market.py --phase buyers     # end buyers per county (builder_land analysis + deeds)
    python src/land_market.py --phase score      # motivation, fit, tiers, offers
    python src/land_market.py --phase export     # workbook + mail lists + marketing plan
    python src/land_market.py --phase all
    python tests/test_land_market.py             # zero network

Sources, each used for what it is right about (read-only everywhere, nothing pulled into an account):
  * the opensearch MCP (via output/builder_land/<market>/) is the truth for the OWNER and the DEED: owner, mailing,
    distressors, sale history, builder purchases. Pulled by builder_land.py (markets knox_blount and knox_ring).
  * the TN Comptroller OLG land use layer (tnmap.tn.gov, public) is the truth for the PARCEL in 10 of the 11 counties:
    polygon, water/sewer service, building count, improvement value, share of the parcel in the 100-year flood.
  * Knox is not in that layer (it keeps its own GIS and sells the parcel polygons), so Knox parcels are verified from
    the cluster's own lot type, frontage and flood zone, the statewide DFIRM flood polygons, census roads, and the
    public Knoxville-Knox County-KUB GIS layers (growth policy plan, future land use, county zoning, the development
    pipeline). A Knox parcel's footprint is a circle of its own area around its point, and every Knox verdict says so.
  * USGS 3DEP 1 m lidar (free, keyless) for slope, census TIGER roads for access.

Contracts learned live 2026-11-09 (each one is silently wrong without it):
  * the Comptroller layer covers 86 counties and NOT Knox, Davidson, Shelby or Hamilton.
  * LU_CLASSIFICATION: 1 house under 5 ac, 2 house on 5+ ac, 51-53 vacant, 61-65 agricultural tract, 71-75 timber
    tract, 31/32 public and semi-public, 41 utility, 81-82 water, 91-93 road/rail right of way.
  * WATERSEWER reads "<water>/<sewer>" ("03 Public/Individual" = public water, septic). Sewer is the gate for
    density, so it is read as its own field.
  * a parcel can appear twice with the same GISLINK and different LANDVALUE (two value records); keep the higher.
  * LU_ACRES carries outliers (Anderson sums to 2.36 million acres); acreage comes from the polygon.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import csv
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "land_market"
GIS = OUT / "gis"
BL = ROOT / "output" / "builder_land"
MARKETS = ("knox_blount", "knox_ring")          # builder_land pulls that together cover the 11 counties
REGION_MARKET = "knoxville_region"              # the combined builder_land analysis (buy boxes across county lines)

COUNTIES = {
    "47093": {"name": "Knox", "olg": None},
    "47009": {"name": "Blount", "olg": "BLOUNT"},
    "47155": {"name": "Sevier", "olg": "SEVIER"},
    "47001": {"name": "Anderson", "olg": "ANDERSON"},
    "47105": {"name": "Loudon", "olg": "LOUDON"},
    "47145": {"name": "Roane", "olg": "ROANE"},
    "47089": {"name": "Jefferson", "olg": "JEFFERSON"},
    "47013": {"name": "Campbell", "olg": "CAMPBELL"},
    "47057": {"name": "Grainger", "olg": "GRAINGER"},
    "47173": {"name": "Union", "olg": "UNION"},
    "47129": {"name": "Morgan", "olg": "MORGAN"},
}
NAME_FIPS = {v["name"]: k for k, v in COUNTIES.items()}

OLG_URL = "https://tnmap.tn.gov/arcgis/rest/services/ENVIRONMENTAL/COMPTROLLER_OLG_LANDUSE/MapServer/2"
DFIRM_URL = "https://tnmap.tn.gov/arcgis/rest/services/ENVIRONMENTAL/DFIRM/MapServer/0"
TIGER_URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer"
TIGER_LAYERS = (2, 6, 8)                         # primary, secondary, local roads
KGIS = "https://services1.arcgis.com/QWaOgwdmpqI9HUzf/arcgis/rest/services"
KGIS_LAYERS = {
    # name: (path, outFields, geometry)
    "growth_policy": ("Knox_County_Growth_Policy_Plan/FeatureServer/0", "TYPE,PLAN_TYPE", True),
    "future_land_use": ("Knox_County_Future_Land_Use_Plan/FeatureServer/1", "PLACETYPE,GPP_COMPATIBILITY", True),
    "county_zoning": ("County_Zoning/FeatureServer/1", "ZONE1,PRDensityGroups,ZoningDistrictType", True),
    "dev_projects": ("DevelopmentProjects/FeatureServer/0",
                     "PROJECT,TYPE,ANNOUNCE_DATE,DESCRIPTION,LOCATION,MPC_FILENUM,SECTOR,OWNER,DEVELOPER,CONTRACTOR,"
                     "UNITS,PARCEL_SIZE,STATUS,STAGE,START_DATE,COMPLETION_DATE", True),
    "concept_lots": ("SubdivsLots/FeatureServer/1", "*", True),          # each layer carries its own fields
    "final_plat_lots": ("SubdivsLots/FeatureServer/2", "*", True),
    "detached_developments": ("DensityViewerDevelopments/FeatureServer/2", "*", True),
}

# candidate classes pulled from the Comptroller layer: houses on 5+ acres (the farmhouse tract), vacant, ag, timber
OLG_CLASSES = ("2", "51", "52", "53", "61", "62", "63", "64", "65", "71", "72", "73", "74", "75")
OLG_FIELDS = ("GISLINK,WATERSEWER,LU_ACRES,NUMBUILDINGS,NUMOFMH,IMPVALUE,LANDVALUE,APPRAISAL,YRBLT,LU_CLASSIFICATION,"
              "ADDRESS,SUBNAME,CITY,ZIP,COUNTY,FLOODPLAIN,HOUSEINFLOODPLAIN,ACRESINFLOOD,PEROFPARCELINFLOOD,"
              "INTERSTATEACCESSTENMINUTES,RETAILACCESSFIFTEENMINUTES,MAJORROADFRONTAGE,POPGROWTHAREA,EMPLOYMENTHUB,"
              "DEVELOPMENTOPPORTUNITIES,AADT_2018,LINK")

TODAY = dt.date.today()


# ----------------------------------------------------------------- plumbing

def _dump(obj, p) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, default=str)
    os.replace(tmp, p)


def _load(p, default=None):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return default


def log(msg: str) -> None:
    line = "%s %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "run.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def ags_pages(url: str, params: dict, page: int = 2000, sleep: float = 0.2):
    """Every feature of an ArcGIS layer query, paged by resultOffset on a stable OBJECTID order."""
    import requests
    offset = 0
    while True:
        p = {"f": "json", "resultOffset": offset, "resultRecordCount": page, "orderByFields": "OBJECTID", **params}
        for attempt in range(5):
            try:
                r = requests.get(url + "/query", params=p, timeout=240)
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise RuntimeError(str(data["error"])[:200])
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 4:
                    raise
                log("  retry %s offset %d: %s" % (url.rsplit("/", 3)[-3], offset, str(e)[:100]))
                time.sleep(5 * (attempt + 1))
        feats = data.get("features") or []
        yield from feats
        if not feats or (len(feats) < page and not data.get("exceededTransferLimit")):
            return
        offset += len(feats)
        time.sleep(sleep)


def _write_layer(dest: Path, feats) -> int:
    n = 0
    tmp = str(dest) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for f in feats:
            fh.write(json.dumps(f) + "\n")
            n += 1
    os.replace(tmp, dest)
    return n


def read_layer(dest: Path) -> list:
    if not Path(dest).exists():
        return []
    with open(dest, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


# ----------------------------------------------------------------- phase: gis

def county_bbox(fips: str) -> tuple | None:
    """(xmin, ymin, xmax, ymax) of a county's parcel points, from the builder_land pull on disk."""
    cache = _load(GIS / "bbox.json", {})
    if fips in cache:
        return tuple(cache[fips])
    xs, ys = [], []
    olg = GIS / ("olg_%s.jsonl" % fips)
    if olg.exists():                 # a complete county layer beats a pull that may still be running
        with open(olg, encoding="utf-8") as fh:
            for x in fh:
                for ring in (json.loads(x).get("geometry") or {}).get("rings") or []:
                    xs.extend(p[0] for p in ring[::8])
                    ys.extend(p[1] for p in ring[::8])
        if xs:
            bb = (min(xs) - 0.005, min(ys) - 0.005, max(xs) + 0.005, max(ys) + 0.005)
            cache[fips] = bb
            _dump(cache, GIS / "bbox.json")
            return bb
    for m in MARKETS:
        for f in sorted((BL / m).glob("props_*.jsonl")):
            with open(f, encoding="utf-8") as fh:
                for x in fh:
                    if '"fips": "%s"' % fips not in x:
                        continue
                    try:
                        r = json.loads(x)
                    except ValueError:
                        continue
                    mm = re.match(r"POINT \(([-\d.]+) ([-\d.]+)\)", r.get("location") or "")
                    if mm:
                        xs.append(float(mm.group(1)))
                        ys.append(float(mm.group(2)))
    if len(xs) < 100:
        return None
    xs.sort()
    ys.sort()
    k = max(1, len(xs) // 2000)                  # trim stray geocodes
    bb = (xs[k] - 0.01, ys[k] - 0.01, xs[-k] + 0.01, ys[-k] + 0.01)
    cache[fips] = bb
    _dump(cache, GIS / "bbox.json")
    return bb


def _tiles(bb, n=4):
    x0, y0, x1, y1 = bb
    dx, dy = (x1 - x0) / n, (y1 - y0) / n
    for i in range(n):
        for j in range(n):
            yield (x0 + i * dx, y0 + j * dy, x0 + (i + 1) * dx, y0 + (j + 1) * dy)


def gis_olg(fips: str, refresh: bool = False) -> int:
    c = COUNTIES[fips]
    dest = GIS / ("olg_%s.jsonl" % fips)
    if not c["olg"] or (dest.exists() and not refresh):
        return 0
    where = "COUNTY='%s' AND LU_ACRES>=0.05 AND LU_CLASSIFICATION IN (%s)" % (
        c["olg"], ",".join("'%s'" % k for k in OLG_CLASSES))
    params = {"where": where, "outFields": OLG_FIELDS, "returnGeometry": "true", "outSR": 4326,
              "geometryPrecision": 6, "maxAllowableOffset": 0.000004}
    n = _write_layer(dest, ags_pages(OLG_URL, params))
    log("gis olg %s: %d parcels" % (c["name"], n))
    return n


def gis_olg_attr(fips: str, refresh: bool = False) -> int:
    """Every parcel's state record, no geometry: a lot the cluster still calls vacant that the state roll codes as a
    house (class 1/5) or carries a building is built on."""
    c = COUNTIES[fips]
    dest = GIS / ("olg_attr_%s.jsonl" % fips)
    if not c["olg"] or (dest.exists() and not refresh):
        return 0
    params = {"where": "COUNTY='%s'" % c["olg"], "returnGeometry": "false",
              "outFields": "GISLINK,LU_CLASSIFICATION,NUMBUILDINGS,NUMOFMH,IMPVALUE,LANDVALUE,APPRAISAL,YRBLT,"
                           "WATERSEWER,LU_ACRES"}
    n = _write_layer(dest, ({"attributes": f["attributes"]} for f in ags_pages(OLG_URL, params)))
    log("gis olg attributes %s: %d parcels" % (c["name"], n))
    return n


STRUCT_URL = "https://services2.arcgis.com/FiaPA4ga0iQKduv3/ArcGIS/rest/services/USA_Structures_View/FeatureServer/0"


def gis_structures(fips: str, refresh: bool = False) -> int:
    """FEMA/ORNL USA Structures footprints (public), centroid and size only: a building on a parcel the rolls still
    call vacant. The only built-on check Knox has, since Knox is not in the state layer."""
    dest = GIS / ("structures_%s.jsonl" % fips)
    if dest.exists() and not refresh:
        return 0
    params = {"where": "FIPS='%s'" % fips, "returnGeometry": "false",
              "outFields": "OBJECTID,LATITUDE,LONGITUDE,SQFEET,OUTBLDG,OCC_CLS,IMAGE_DATE"}
    n = _write_layer(dest, ({"attributes": f["attributes"]} for f in ags_pages(STRUCT_URL, params)))
    log("gis structures %s: %d footprints" % (COUNTIES[fips]["name"], n))
    return n


def gis_roads(fips: str, refresh: bool = False) -> int:
    dest = GIS / ("roads_%s.jsonl" % fips)
    if dest.exists() and not refresh:
        return 0
    bb = county_bbox(fips)
    if not bb:
        log("gis roads %s: no bbox yet (pull not on disk)" % fips)
        return 0
    seen, feats = set(), []
    for lid in TIGER_LAYERS:
        for t in _tiles(bb, 3):
            params = {"where": "1=1", "outFields": "OBJECTID,NAME,MTFCC", "returnGeometry": "true", "outSR": 4326,
                      "geometry": "%.5f,%.5f,%.5f,%.5f" % t, "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                      "spatialRel": "esriSpatialRelIntersects", "geometryPrecision": 6}
            for f in ags_pages("%s/%d" % (TIGER_URL, lid), params, page=1000):
                k = (lid, f["attributes"].get("OBJECTID"))
                if k not in seen:
                    seen.add(k)
                    f["attributes"]["layer"] = lid
                    feats.append(f)
    n = _write_layer(dest, feats)
    log("gis roads %s: %d segments" % (COUNTIES[fips]["name"], n))
    return n


def gis_knox(refresh: bool = False) -> None:
    for name, (path, fields, geom) in KGIS_LAYERS.items():
        dest = GIS / ("kgis_%s.jsonl" % name)
        if dest.exists() and not refresh:
            continue
        params = {"where": "1=1", "outFields": fields, "returnGeometry": "true" if geom else "false", "outSR": 4326,
                  "geometryPrecision": 6}
        n = _write_layer(dest, ags_pages("%s/%s" % (KGIS, path), params, page=1000))
        log("gis kgis %s: %d features" % (name, n))
    dest = GIS / "dfirm_47093.jsonl"
    if not dest.exists() or refresh:
        bb = county_bbox("47093")
        if bb:
            feats = []
            seen = set()
            for t in _tiles(bb, 4):
                params = {"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": 4326,
                          "geometry": "%.5f,%.5f,%.5f,%.5f" % t, "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                          "spatialRel": "esriSpatialRelIntersects", "geometryPrecision": 6,
                          "maxAllowableOffset": 0.000004}
                for f in ags_pages(DFIRM_URL, params, page=1000):
                    k = f["attributes"].get("OBJECTID")
                    if k not in seen:
                        seen.add(k)
                        feats.append(f)
            n = _write_layer(dest, feats)
            log("gis dfirm Knox: %d flood polygons" % n)


def phase_gis(args) -> int:
    GIS.mkdir(parents=True, exist_ok=True)
    fips = [f for f in COUNTIES if not args.counties or COUNTIES[f]["name"] in args.counties]
    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda f: gis_olg(f, args.refresh), fips))
        list(ex.map(lambda f: gis_olg_attr(f, args.refresh), fips))
        list(ex.map(lambda f: gis_structures(f, args.refresh), fips))
    if "47093" in fips:
        gis_knox(args.refresh)
    for f in fips:
        try:
            gis_roads(f, args.refresh)
        except Exception as e:  # noqa: BLE001
            log("gis roads %s failed: %s" % (f, str(e)[:200]))
    return 0


# ----------------------------------------------------------------- owners

import builder_land as bl          # noqa: E402  (deed helpers, row tests)
import vacant_land as vl           # noqa: E402  (geometry, topo, owner classes, buildability)

_PUBLIC_TN = re.compile(
    r"\b(CITY|TOWN|COUNTY|STATE) OF\b|\b(%s) COUNTY\b|\bSTATE OF TENNESSEE\b|\bTENNESSEE (DEPT|DEPARTMENT|STATE|WILDLIFE)\b|"
    r"\bTVA\b|\bTENNESSEE VALLEY AUTH|\bUNITED STATES\b|\bUSA\b|\bU S A\b|\bNATIONAL PARK\b|\bBOARD OF EDUCATION\b|"
    r"\bHOUSING AUTHORITY\b|\bKCDC\b|\bUTILITY DISTRICT\b|\bUTILITIES BOARD\b|\bKNOXVILLE UTILITIES\b|\bKUB\b|"
    r"\bWATER AUTHORITY\b|\bINDUSTRIAL DEV|\bDEVELOPMENT BOARD\b|\bAIRPORT AUTHORITY\b|\bMETROPOLITAN\b|"
    r"\bUNIVERSITY OF TENNESSEE\b|\bLAND TRUST\b|\bLEGACY PARKS\b|\bCONSERVANCY\b|\bFOOTHILLS LAND\b|"
    r"\bGREAT SMOKY\b|\bDEPARTMENT OF\b" % "|".join(v["name"].upper() for v in COUNTIES.values()))
# production builders and lot developers active around Knoxville (deeds, 2021-2026). Their land is inventory, not a
# seller lead, and they are the exit for what we entitle.
_BUILDERS_TN = re.compile(
    r"\b(D ?R HORTON|HORTON|BALL HOMES|SMITHBILT|TURNER HOMES|MESANA|HOMESTEAD LAND|CLAYTON (PROPERTIES|HOMES)|"
    r"PALMETTO|WESTLAND CREEK|EAGLE BEND DEV|LENNAR|NVR|RYAN HOMES|DAN RYAN|SCHUMACHER|STANLEY MARTIN|AMBERWOOD|"
    r"HARMONY HOMES|PULTE|CENTEX|SMITH ?BUILT|ROCKWOOD HOMES|FALCON HOMES|BRADBURY|SAGEBROOK|CAPITAL CITY HOMES|"
    r"VICTORY HOMES|ADAMS HOMES|MERITAGE|KB HOME|TAYLOR MORRISON|DREAM FINDERS|CENTURY COMMUNITIES|LGI HOMES|"
    r"SOUTHERN HOMES|GREAT SOUTHERN HOMES|KNOX TL LOT|CMH)\b")
# industrial and extraction owners hold land for operations, not for sale to a land desk
_INDUSTRIAL_TN = re.compile(
    r"\b(ARCONIC|ALCOA|INDUSTRIES|INDUSTRIAL|MANUFACTURING|CEMENT|QUARRY|MINING|MINERALS|MATERIALS|RAILROAD|RAILWAY|"
    r"CHEMICAL|STEEL|AGGREGATES|VULCAN|ROGERS GROUP|MARTIN MARIETTA)\b")


def owner_cat(name, r=None) -> str:
    """public | hoa | cemetery | church | utility | builder | bank | trust_estate | entity | person | unknown."""
    s = vl._clean(name)
    if not s:
        return "unknown"
    if _PUBLIC_TN.search(s):
        return "public"
    cat = vl.owner_category(name)
    if cat in vl.PUBLIC_CATS or cat == "bank":
        return cat
    if _BUILDERS_TN.search(s) or (r and r.get("is_builder_owned")):
        return "builder"
    if _INDUSTRIAL_TN.search(s):
        return "industrial"
    return cat


OWNER_LABEL = {**vl.CAT_LABEL, "unknown": "no owner name", "industrial": "industrial or mining company"}
NOT_SELLERS = {"public", "hoa", "cemetery", "church", "utility", "unknown", "industrial"}


# ----------------------------------------------------------------- lanes

LANES = [
    # key, label, who buys it
    ("lot", "Builder lots (0.06-1 ac, vacant)", "small builders (the Builder Land Desk)"),
    ("homesite", "Homesites (1-5 ac, vacant)", "homesite buyers and small builders on septic"),
    ("small_tract", "Small tracts (5-15 ac)", "small subdivision builders and homesite splitters"),
    ("ent_tract", "Entitlement tracts (15-150 ac)", "production builders and developers, once approved"),
    ("large_tract", "Large tracts (150+ ac)", "developers, land funds and timber buyers"),
]
LANE_LABEL = {k: v for k, v, _ in LANES}
LANE_BUYER = {k: b for k, _, b in LANES}


def status_of(r: dict) -> str:
    """vacant | ag | house | mobile | other, from the cluster row."""
    lu = r.get("land_use")
    ptg = str(r.get("property_type_group") or "")
    pt = str(r.get("property_type") or "")
    if lu == "Vacant" or ptg.startswith("land"):
        return "vacant"
    if lu == "Agricultural" or "Agricultural" in pt or "Timber" in pt or "Farm" in pt:
        return "ag"
    if ptg == "mobile_home":
        return "mobile"
    if ptg == "single_family_residence" or "Residen" in pt:
        return "house"
    return "other"


def lane_of(status: str, acres) -> str | None:
    if not acres or acres <= 0:
        return None
    if status == "vacant":
        if bl.MIN_LOT_ACRES <= acres < 1:
            return "lot"
        if 1 <= acres < 5:
            return "homesite"
    if status in ("vacant", "ag", "house", "mobile") and acres >= 5:
        if acres < 15:
            return "small_tract"
        if acres < 150:
            return "ent_tract"
        return "large_tract"
    return None


# ----------------------------------------------------------------- phase: tam

LAND_FIELDS = ("dataflik_id", "fips", "parcel_number", "property_address", "property_city", "property_zip",
               "full_name", "full_name_2", "mailing_full_address", "mailing_address_state", "mailing_address_city",
               "owner_age", "area_lot_acres", "lot_frontage_feet", "lot_type", "flood_zone", "assessed_value",
               "assessed_land_value", "property_value", "last_sale_date", "last_sale_price", "is_last_sale_intrafamily",
               "is_builder_owned", "is_corporate_owned", "is_trust_owned", "subdivision_name", "neighborhood",
               "property_type", "land_use", "property_type_group", "year_built", "is_active_listing",
               "has_postal_address", "lat", "lon")


def iter_rows():
    """Every pulled parcel once (both builder_land markets), with lat/lon; broken last lines skipped."""
    for m in MARKETS:
        seen = set()
        for f in sorted((BL / m).glob("props_*.jsonl")):
            with open(f, encoding="utf-8") as fh:
                for x in fh:
                    try:
                        r = json.loads(x)
                    except ValueError:
                        continue
                    if r["dataflik_id"] in seen or r.get("fips") not in COUNTIES:
                        continue
                    seen.add(r["dataflik_id"])
                    r["lat"], r["lon"] = bl.latlon(r)
                    yield r


def load_sales_all():
    by = {}
    for m in MARKETS:
        if (BL / m / "sales").exists():
            by.update(bl.load_sales(BL / m))
    return by


ACRE_BANDS = [(0.06, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 15), (15, 50), (50, 150), (150, 100000)]


def acre_band(a: float) -> str:
    for lo, hi in ACRE_BANDS:
        if lo <= a < hi:
            return "%g-%g ac" % (lo, hi) if hi < 100000 else "%g+ ac" % lo
    return "under 0.06 ac"


def _band_stats(v: list) -> dict:
    amts = sorted(x[0] for x in v)
    per = sorted(x[1] for x in v if x[1])
    return {"n": len(v), "median_price": round(statistics.median(amts)),
            "median_per_acre": round(statistics.median(per)) if per else None,
            "p25_per_acre": round(bl.q(per, .25)) if len(per) >= 4 else None,
            "p75_per_acre": round(bl.q(per, .75)) if len(per) >= 4 else None}


def land_prices(land_rows: dict, sales: dict) -> dict:
    """Median $/acre and $/parcel of priced arm's-length sales of VACANT land since 2021, by county and acre band.
    A small parcel resold at $150K+ and 3x its appraisal is a house going up, not a land price."""
    by = collections.defaultdict(list)
    for i, r in land_rows.items():
        if r["status"] != "vacant":
            continue
        a = r.get("area_lot_acres") or 0
        appr = (r.get("assessed_value") or 0) * 4
        for s in sales.get(i, []):
            amt = s["sale_amount"]
            if s["sale_date"] < bl.SINCE_MS or amt < 3000 or bl.family(s):
                continue
            if amt >= 150000 and appr and amt >= 3 * appr and a < 5:
                continue
            by[(r["fips"], acre_band(a))].append((amt, amt / a if a else None))
    out = {"%s|%s" % k: _band_stats(v) for k, v in by.items()}
    reg = collections.defaultdict(list)
    for (f, b), v in by.items():
        reg[b].extend(v)
    out.update({"region|%s" % b: _band_stats(v) for b, v in reg.items()})
    return out


TURNOVER_YEARS = (2022, 2023, 2024)       # full years; 2025 deeds are still being recorded
T0_MS = dt.datetime(TURNOVER_YEARS[0], 1, 1).timestamp() * 1000
T1_MS = dt.datetime(TURNOVER_YEARS[-1] + 1, 1, 1).timestamp() * 1000


def turnover(land_rows: dict, sales: dict) -> dict:
    """Arm's-length sales of vacant land a year (2022-24 average), by county and lane: the dollars that actually
    change hands, which is the market a land desk competes for."""
    by = collections.defaultdict(lambda: [0, 0])
    for i, r in land_rows.items():
        if r["status"] != "vacant":
            continue
        a = r.get("area_lot_acres") or 0
        appr = (r.get("assessed_value") or 0) * 4
        for s in sales.get(i, []):
            if not (T0_MS <= (s["sale_date"] or 0) < T1_MS):     # a pre-1970 deed date cannot be converted on Windows
                continue
            amt = s["sale_amount"]
            if amt < 3000 or bl.family(s):
                continue
            if amt >= 150000 and appr and amt >= 3 * appr and a < 5:
                continue
            k = "%s|%s" % (r["fips"], r["lane"])
            by[k][0] += 1
            by[k][1] += amt
    n = len(TURNOVER_YEARS)
    return {k: {"sales_per_year": round(v[0] / n), "dollars_per_year": round(v[1] / n)} for k, v in by.items()}


def price_for(prices: dict, fips: str, acres: float) -> dict | None:
    """The county's own band when it holds 8+ sales; else the county's next band down (land gets cheaper per acre
    as it gets bigger, so this leans high, never absurdly low); only then the 11-county band. The region's 150+
    acre band is mostly Morgan and Campbell timber at under $1,000 an acre, which would price a Knox farm at a
    tenth of its value."""
    b = acre_band(acres)
    p = prices.get("%s|%s" % (fips, b))
    if p and p["n"] >= 8:
        return {**p, "basis": "%s %s (%d sales)" % (COUNTIES[fips]["name"], b, p["n"])}
    names = ["%g-%g ac" % (lo, hi) if hi < 100000 else "%g+ ac" % lo for lo, hi in ACRE_BANDS]
    if b in names:
        for down in reversed(names[:names.index(b)]):
            q = prices.get("%s|%s" % (fips, down))
            if q and q["n"] >= 8 and q.get("median_per_acre"):
                # the county's price level, discounted by the region's own drop from that band to this one
                ra, rd = prices.get("region|%s" % b) or {}, prices.get("region|%s" % down) or {}
                k = (ra.get("median_per_acre") or 0) / rd["median_per_acre"] if rd.get("median_per_acre") else 1
                k = max(0.3, min(1.0, k))
                per = round(q["median_per_acre"] * k)
                return {**q, "median_per_acre": per, "median_price": round(per * acres), "p25_per_acre": None,
                        "p75_per_acre": None,
                        "basis": "%s %s at $%s/ac x the region's %s-to-%s discount (%.2f); too few %s sales" % (
                            COUNTIES[fips]["name"], down, format(q["median_per_acre"], ","), down, b, k, b)}
    p = prices.get("region|%s" % b)
    if p:
        return {**p, "basis": "11-county %s (%d sales)" % (b, p["n"])}
    return None


def is_seller(row: dict) -> bool:
    return row["owner_cat"] not in NOT_SELLERS and row["owner_cat"] != "builder"


def phase_tam(args) -> int:
    t = time.time()
    agg = collections.defaultdict(collections.Counter)
    owners = collections.defaultdict(set)
    land, houses, newbuilds = {}, collections.defaultdict(list), []
    total = collections.Counter()
    for r in iter_rows():
        f = r["fips"]
        total[f] += 1
        st = status_of(r)
        a = r.get("area_lot_acres") or 0
        if bl.is_house(r) and r.get("lat") is not None:
            houses[f].append((round(r["lat"], 6), round(r["lon"], 6), round(a, 3)))
            if (r.get("year_built") or 0) >= 2021:
                newbuilds.append((f, round(r["lat"], 6), round(r["lon"], 6), r["year_built"],
                                  r.get("property_value") or 0, r.get("subdivision_name") or ""))
        ln = lane_of(st, a)
        if not ln:
            continue
        cat = owner_cat(r.get("full_name"), r)
        tenure, mflags, distressed = bl.owner_flags(r, "TN")
        row = {k: r.get(k) for k in LAND_FIELDS}
        row.update(status=st, lane=ln, owner_cat=cat, tenure=tenure, mflags=mflags, distressed=distressed,
                   distress=[k for k in bl.DISTRESS if bl.flag(r, k)])
        land[r["dataflik_id"]] = row
        c = agg[(f, ln)]
        c["parcels"] += 1
        c["acres"] += a
        c["status_" + st] += 1
        c["owner_" + cat] += 1
        if not is_seller(row):
            continue
        c["addressable"] += 1
        c["addressable_acres"] += a
        okey = vl.owner_key(r.get("full_name"))
        if okey:
            owners[(f, ln)].add(okey)
        for m in mflags:
            c["flag_" + m] += 1
        if distressed:
            c["flag_distressed"] += 1
        if r.get("lot_type") == "Landlocked":
            c["landlocked"] += 1
        if r.get("flood_zone") in ("A", "AE", "AO", "AH", "VE"):
            c["flood_point"] += 1
        if r.get("is_active_listing"):
            c["listed"] += 1
        if tenure is not None and tenure < 2:
            c["bought_2y"] += 1
    log("tam: %d parcels read, %d in a land lane, %.0fs" % (sum(total.values()), len(land), time.time() - t))
    sales = load_sales_all()
    prices = land_prices(land, sales)
    turn = turnover(land, sales)
    for k, c in agg.items():
        c["owners"] = len(owners[k])
        c.update(turn.get("%s|%s" % k, {}))
    for row in land.values():
        if not is_seller(row):
            continue
        a = row.get("area_lot_acres") or 0
        p = price_for(prices, row["fips"], a)
        if p and p.get("median_per_acre"):
            # lots and homesites trade per parcel, tracts per acre
            v = p["median_price"] if row["lane"] in ("lot", "homesite") else p["median_per_acre"] * a
            agg[(row["fips"], row["lane"])]["value_at_market"] += v
    _dump({"built": TODAY.isoformat(), "parcels_by_county": dict(total),
           "by_county_lane": {"%s|%s" % k: dict(v) for k, v in agg.items()}, "prices": prices}, OUT / "tam.json")
    with open(OUT / "land_parcels.jsonl", "w", encoding="utf-8") as fh:
        for row in land.values():
            fh.write(json.dumps(row) + "\n")
    _dump(dict(houses), OUT / "house_pts.json")
    _dump(newbuilds, OUT / "newbuild_pts.json")
    tot = collections.Counter()
    for (f, ln), c in agg.items():
        tot[ln] += c["addressable"]
    log("tam: addressable parcels by lane %s" % dict(tot))
    return 0


# ----------------------------------------------------------------- phase: screen

SFHA = {"A", "AE", "AO", "AH", "A99", "AR", "V", "VE"}
BUILT_CLASSES = {"1": "a house", "2": "a house on 5+ acres", "3": "a duplex", "4": "multifamily", "5": "a mobile home",
                 "6": "a mobile home park", "7": "resort residential", "96": "an improved parcel",
                 "97": "an improved parcel"}
NONRES_ZONES = {"CA", "CB", "CH", "CN", "CR", "BP", "I", "LI", "EC", "OA", "OB", "OC", "SC", "T", "HZ", "PC", "CAC"}
INDUSTRIAL_ZONES = {"I", "LI", "HZ", "EC"}
STATE_NOT_LAND = {"21": "light industrial or warehousing", "22": "heavy industrial", "31": "public use",
                  "32": "semi-public use", "41": "utility", "81": "water", "82": "water", "91": "road or rail",
                  "92": "road or rail", "93": "road or rail"}
MILE_FT = 5280.0
NB_NEAR_FT = 1 * MILE_FT
NB_FAR_FT = 3 * MILE_FT


def water_sewer(ws) -> tuple:
    """'03 Public/Individual' -> ('public', 'individual'); anything unreadable -> (None, None)."""
    m = re.match(r"\s*\d*\s*([A-Za-z]+)\s*/\s*([A-Za-z]+)", ws or "")
    return (m.group(1).lower(), m.group(2).lower()) if m else (None, None)


def pkey(s) -> str:
    """Join key for a parcel id: the state's GISLINK is the county code plus the cluster's parcel number, so both
    sides are compared upper-cased with every space removed."""
    return re.sub(r"\s+", "", str(s or "").upper())


_DASHED = re.compile(r"^(\d{3}[A-Z]?)-(?:([A-Z])-)?(\d{3})\.(\d{2})$")


def gis_key(code: str, parcel_number) -> str:
    """The state GISLINK for a cluster parcel number. Most counties ship the state's own layout ('001m A 00400' ->
    '005' + '001MA00400'); Anderson, Jefferson, Loudon and Sevier ship it dashed ('019-028.00' is map 019, parcel
    028.00, so '001' + '019' + '02800')."""
    s = str(parcel_number or "").upper().strip()
    m = _DASHED.match(s)
    if m:
        return code + m.group(1) + (m.group(2) or "") + m.group(3) + m.group(4)
    return pkey(code + s)


def iter_land(fips: str | None = None):
    with open(OUT / "land_parcels.jsonl", encoding="utf-8") as fh:
        for x in fh:
            r = json.loads(x)
            if fips is None or r["fips"] == fips:
                yield r


def _county_code(attrs: dict) -> str:
    c = collections.Counter(k[:3] for k in list(attrs)[:5000])
    return c.most_common(1)[0][0] if c else ""


def sewer_label(sewer: str | None) -> str:
    """The state's sewer word in the vocabulary vacant_land.buildability scores."""
    return {"public": "sewer likely", "private": "mixed", "individual": "septic area"}.get(sewer or "", "unknown")


def tract_fit(f: dict) -> tuple:
    """(verdict, score 0-100, hard reasons, flags, path) for a 5+ acre tract and the entitle-and-sell exit.

    Score: utilities 25, builder demand 25, access 20, developable acres 15, growth 10, slope 5. The path is what
    the land can be approved as: a sewer subdivision (3-5 homes an acre), septic lots (about an acre each, the
    rural-county norm), or homesite splits (2-5 acre tracts sold one at a time)."""
    flags, hard = [], []
    cat = f["owner_cat"]
    if cat in vl.PUBLIC_CATS or cat == "public":
        return "PUBLIC OR COMMON", 0, ["owner is a %s" % OWNER_LABEL.get(cat, cat)], flags, None
    acres = f.get("acres") or 0
    fl = f.get("flood_pct") or 0
    steep = f.get("steep_share")
    # half the land over a 15% grade still holds roads and pads on the gentler half of each slope
    dev = acres * (1 - min(fl, 1)) * (1 - 0.5 * (steep or 0)) - (2 if f.get("with_house") else 0)
    mean = f.get("slope_mean_pct")
    f["dev_acres"] = round(dev, 1)
    sewer, water = f.get("sewer_state"), f.get("water_state")
    knox = f["fips"] == "47093"
    if sewer == "public" or (knox and f.get("sewer") == "sewer likely" and f.get("gpp") in ("UGB", "PGA", "FAR_UGB")):
        path, pts = "sewer subdivision", 25
    elif water == "public" or (knox and f.get("gpp") in ("UGB", "PGA", "FAR_UGB")):
        path, pts = "septic lots", 12
        flags.append("public water, septic: lots near an acre each unless sewer is extended")
    elif knox:
        path, pts = "septic lots", 8
        flags.append("Knox rural area: septic, check the utility district for water")
    else:
        path, pts = "homesite splits", 4
        flags.append("no public water on the state roll")
    nb3, nb1 = f.get("nb_3mi") or 0, f.get("nb_1mi") or 0
    pts += 25 if nb3 >= 200 else 20 if nb3 >= 75 else 14 if nb3 >= 25 else 8 if nb3 >= 8 else 2
    fr = f.get("frontage_ft") or 0
    pts += 20 if fr >= 200 else 15 if fr >= 100 else 10 if fr >= 50 else 0
    pts += 15 if dev >= 40 else 12 if dev >= 20 else 9 if dev >= 10 else 5 if dev >= 5 else 0
    if knox:
        pts += {"PGA": 10, "UGB": 10, "FAR_UGB": 10, "RURAL": 2}.get(f.get("gpp"), 4)
    else:
        pts += 4 + (3 if f.get("interstate_10min") == "YES" else 0) + (
            3 if f.get("dev_opportunity") in ("DO", "CDO") else 0)
    pts += 3 if mean is None else 5 if mean < 12 else 3 if mean < TRACT_SLOPE_FLAG else 0
    if f.get("landlocked"):
        hard.append("no road within %d ft" % vl.LANDLOCKED_FT)
    if fl >= 0.60:
        hard.append("%d%% in the 100-year floodplain" % round(fl * 100))
    elif fl >= 0.10:
        flags.append("%d%% in the 100-year floodplain" % round(fl * 100))
    if mean is not None and mean >= TRACT_SLOPE_FAIL:
        hard.append("average grade %d%%: a mountainside, not a subdivision" % mean)
    elif mean is not None and mean >= TRACT_SLOPE_FLAG:
        flags.append("average grade %d%%: lot yield will be lower than the acreage says" % mean)
    if dev < 4:
        hard.append("under 4 developable acres after flood and slope")
    cls = str(f.get("state_class") or "")
    if cls in STATE_NOT_LAND:
        hard.append("state roll: %s" % STATE_NOT_LAND[cls])
    elif cls in ("11", "12", "13", "14", "15", "16"):
        flags.append("state roll: commercial use")
    if knox and f.get("zoning") in INDUSTRIAL_ZONES:
        hard.append("zoned %s (industrial)" % f["zoning"])
    elif knox and f.get("zoning") in NONRES_ZONES:
        flags.append("zoned %s (commercial or office)" % f["zoning"])
    if f.get("with_house"):
        flags.append("house on the tract: carve it out or sell it with the land")
    pts = max(0, min(100, pts))
    if cat == "builder":
        return "BUILDER INVENTORY", pts, hard, flags, path
    if hard:
        return "NOT A FIT", pts, hard, flags, path
    if path == "sewer subdivision" and pts >= 60 and nb3 >= 25 and dev >= 8:
        v = "ENTITLEMENT: SEWER"
    elif path != "homesite splits" and pts >= 50 and nb3 >= 10 and dev >= 10:
        v = "ENTITLEMENT: SEPTIC LOTS" if path == "septic lots" else "ENTITLEMENT: SEWER"
    elif dev >= 5 and fr >= 50:
        v = "HOMESITE SPLIT"
    else:
        v = "VERIFY"
    if f["lane"] == "large_tract" and v.startswith("ENTITLEMENT"):
        v = "LARGE: " + v
    return v, pts, hard, flags, path


def _built_reasons(r: dict, st: dict | None) -> list:
    """Why a parcel the cluster files as vacant has a house on it."""
    out = []
    if st:
        cls = str(st.get("LU_CLASSIFICATION") or "")
        if cls in BUILT_CLASSES:
            out.append("state roll codes it as %s" % BUILT_CLASSES[cls])
        if (st.get("NUMBUILDINGS") or 0) > 0 and cls not in ("61", "62", "63", "64", "65", "71", "72", "73", "74", "75"):
            out.append("state roll counts %d building(s)" % st["NUMBUILDINGS"])
        if (st.get("IMPVALUE") or 0) >= 30000:
            out.append("state roll carries $%s of improvements" % format(int(st["IMPVALUE"]), ","))
    sold, price = r.get("last_sale_date") or 0, r.get("last_sale_price") or 0
    appr = (r.get("assessed_value") or 0) * 4
    if sold >= bl.SINCE_MS and price >= vl.BUILT_SALE_FLOOR and appr and price >= vl.BUILT_SALE_RATIO * appr:
        out.append("sold for $%s, %.0fx its appraisal" % (format(int(price), ","), price / appr))
    return out


def screen_county(fips: str) -> list:
    import shapely
    from shapely.geometry import Point
    rows = list(iter_land(fips))
    if not rows:
        return []
    pts = [(r["lon"], r["lat"]) for r in rows if r.get("lat") is not None]
    P = vl.Proj(statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))

    def ft(g):
        return P.geom(g) if g is not None else None

    roads = [g for g in (ft(vl.esri_to_shape(f.get("geometry"))) for f in read_layer(GIS / ("roads_%s.jsonl" % fips)))
             if g is not None and not g.is_empty]
    road_tree = shapely.STRtree(roads) if roads else None
    polys = {}
    for f in read_layer(GIS / ("olg_%s.jsonl" % fips)):
        a = f["attributes"]
        k = pkey(a.get("GISLINK"))
        if k in polys and (polys[k][1].get("APPRAISAL") or 0) >= (a.get("APPRAISAL") or 0):
            continue
        polys[k] = (ft(vl.esri_to_shape(f.get("geometry"))), a)
    pkeys = [k for k, v in polys.items() if v[0] is not None]
    ptree = shapely.STRtree([polys[k][0] for k in pkeys]) if pkeys else None
    attrs = {}
    for f in read_layer(GIS / ("olg_attr_%s.jsonl" % fips)):
        a = f["attributes"]
        k = pkey(a.get("GISLINK"))
        if k not in attrs or (a.get("APPRAISAL") or 0) > (attrs[k].get("APPRAISAL") or 0):
            attrs[k] = a
    code = _county_code(attrs) if attrs else ""
    knox = {}
    if fips == "47093":
        for name in ("growth_policy", "future_land_use", "county_zoning"):
            feats = [(ft(vl.esri_to_shape(f.get("geometry"))), f["attributes"]) for f in read_layer(GIS / ("kgis_%s.jsonl" % name))]
            feats = [x for x in feats if x[0] is not None]
            knox[name] = (shapely.STRtree([g for g, _ in feats]), feats)
        fl = [(ft(vl.esri_to_shape(f.get("geometry"))), f["attributes"]) for f in read_layer(GIS / "dfirm_47093.jsonl")]
        sf = [g for g, a in fl if g is not None and a.get("FLD_ZONE") in SFHA]
        fw = [g for g, a in fl if g is not None and "FLOODWAY" in str(a.get("FLOODWAY") or "").upper()]
        knox["sfha"] = (shapely.STRtree(sf), sf) if sf else None
        knox["floodway"] = (shapely.STRtree(fw), fw) if fw else None
        hp = (_load(OUT / "house_pts.json", {}) or {}).get(fips, [])
        hxy = [P.xy(lon, lat) for lat, lon, _ in hp]
        knox["houses"] = (shapely.STRtree([Point(x, y) for x, y in hxy]), [a for _, _, a in hp]) if hp else None
    sp = []
    for f in read_layer(GIS / ("structures_%s.jsonl" % fips)):
        a = f["attributes"]
        if a.get("LONGITUDE") is None or (a.get("SQFEET") or 0) < 400 or str(a.get("OUTBLDG") or "").upper() in ("Y", "YES", "1"):
            continue
        sp.append((Point(*P.xy(a["LONGITUDE"], a["LATITUDE"])), a.get("SQFEET"), a.get("IMAGE_DATE")))
    s_tree = shapely.STRtree([p for p, _, _ in sp]) if sp else None
    nbs = _load(OUT / "newbuild_pts.json", []) or []
    nb_pts = [Point(*P.xy(lon, lat)) for _, lat, lon, *_ in nbs]
    nb_tree = shapely.STRtree(nb_pts) if nb_pts else None

    def inside(name, pt):
        t = knox.get(name)
        if not t:
            return None
        for i in t[0].query(pt):
            if t[1][i][0].contains(pt):
                return t[1][i][1]
        return None

    out = []
    for r in rows:
        f = {k: r.get(k) for k in ("dataflik_id", "fips", "lane", "status", "owner_cat", "tenure", "mflags",
                                   "distressed", "distress", "full_name", "property_address", "property_city",
                                   "property_zip", "parcel_number", "lot_type", "subdivision_name", "area_lot_acres",
                                   "lat", "lon")}
        if r.get("lat") is None:
            f.update(verdict="VERIFY", score=0, hard=[], flags=["no map point"], geom_src="none")
            out.append(f)
            continue
        x, y = P.xy(r["lon"], r["lat"])
        pt = Point(x, y)
        k = gis_key(code, r.get("parcel_number")) if code else None
        poly, pa = polys.get(k, (None, None)) if k else (None, None)
        if poly is None and ptree is not None:
            best = None
            for i in ptree.query(pt):
                g, a = polys[pkeys[i]]
                if g.contains(pt):
                    ratio = abs(math.log(max(g.area / 43560, 0.01) / max(r.get("area_lot_acres") or 0.01, 0.01)))
                    if best is None or ratio < best[0]:
                        best = (ratio, g, a)
            if best and best[0] < 1.2:
                _, poly, pa = best
                f["matched_by"] = "point in polygon"
        st = attrs.get(k) if k else None
        st = st or pa
        f["geom_src"] = "state polygon" if poly is not None else "circle of the cluster acreage"
        acres = poly.area / 43560 if poly is not None else (r.get("area_lot_acres") or 0)
        f["acres"] = round(acres, 3)
        shape = poly if poly is not None else pt.buffer(math.sqrt(max(acres, 0.01) * 43560 / math.pi), 16)
        f["state_class"] = (st or {}).get("LU_CLASSIFICATION")
        f["water_state"], f["sewer_state"] = water_sewer((st or {}).get("WATERSEWER"))
        f["state_appraisal"] = (st or {}).get("APPRAISAL")
        f["state_land_value"] = (st or {}).get("LANDVALUE")
        f["gislink"] = (st or {}).get("GISLINK")
        # access and shape
        if poly is not None and road_tree is not None:
            f.update(vl.road_access(poly, road_tree, roads, None, []))
            f.update(vl.shape_metrics(poly))
        else:
            rad = math.sqrt(max(acres, 0.01) * 43560 / math.pi)
            edge = None
            if road_tree is not None:
                idx, dist = road_tree.query_nearest(pt, return_distance=True)
                edge = max(0.0, float(min(dist)) - rad) if len(idx) else None
            f["road_ft"] = None if edge is None else round(edge, 1)
            f["landlocked"] = (edge is None or edge > vl.LANDLOCKED_FT) or (r.get("lot_type") == "Landlocked" and edge > 30)
            fr = r.get("lot_frontage_feet") or 0
            f["frontage_ft"] = fr if fr else (0 if f["landlocked"] else None)
            f["width_ft"] = fr if fr else round(math.sqrt(acres * 43560), 1)
            f["compactness"] = None
        # flood
        if pa is not None and pa.get("FLOODPLAIN") is not None:
            f["flood_pct"] = (pa.get("PEROFPARCELINFLOOD") or 0) / 100 if pa.get("FLOODPLAIN") == "YES" else 0.0
        elif knox.get("sfha"):
            f["flood_pct"] = vl.area_share(shape, knox["sfha"][0], knox["sfha"][1])
            if knox.get("floodway"):
                f["floodway_pct"] = vl.area_share(shape, knox["floodway"][0], knox["floodway"][1])
        else:
            f["flood_pct"] = 0.5 if r.get("flood_zone") in SFHA else 0.0
            f["flood_from"] = "cluster point"
        # sewer and planning
        if fips == "47093":
            near = []
            if knox.get("houses"):
                t, acs = knox["houses"]
                near = [acs[i] for i in t.query(pt.buffer(vl.SEWER_RADIUS_FT)) if acs[i]]
            f["sewer"], f["sewer_basis"] = vl.sewer_proxy(near, bool(r.get("subdivision_name")), acres)
            g = inside("growth_policy", pt)
            f["gpp"] = (g or {}).get("TYPE") or "CITY"
            u = inside("future_land_use", pt)
            f["future_land_use"] = (u or {}).get("PLACETYPE")
            z = inside("county_zoning", pt)
            f["zoning"] = (z or {}).get("ZONE1") or "city"
            f["pr_density"] = (z or {}).get("PRDensityGroups")
        else:
            f["sewer"] = sewer_label(f["sewer_state"])
            f["sewer_basis"] = "state roll: %s" % ((st or {}).get("WATERSEWER") or "no record")
            for a, b in (("POPGROWTHAREA", "pop_growth"), ("INTERSTATEACCESSTENMINUTES", "interstate_10min"),
                         ("RETAILACCESSFIFTEENMINUTES", "retail_15min"), ("MAJORROADFRONTAGE", "major_road"),
                         ("DEVELOPMENTOPPORTUNITIES", "dev_opportunity"), ("AADT_2018", "aadt")):
                f[b] = (pa or {}).get(a)
        if nb_tree is not None:
            f["nb_1mi"] = len(nb_tree.query(pt.buffer(NB_NEAR_FT)))
            f["nb_3mi"] = len(nb_tree.query(pt.buffer(NB_FAR_FT)))
        f["with_house"] = r["status"] in ("house", "mobile") or f.get("state_class") in ("1", "2", "5")
        f["built"] = _built_reasons(r, st) if r["lane"] in ("lot", "homesite") else []
        if s_tree is not None:
            # a circle is only an estimate of a Knox lot, so its inner 80% is tested, to keep the neighbors' houses out
            zone = poly if poly is not None else pt.buffer(0.8 * math.sqrt(max(acres, 0.01) * 43560 / math.pi), 16)
            hits = [i for i in s_tree.query(zone) if zone.contains(sp[i][0])]
            f["structures"] = len(hits)
            if hits and r["lane"] in ("lot", "homesite"):
                big = max(sp[i][1] or 0 for i in hits)
                f["built"].append("building footprint on the lot (%d sq ft, USA Structures)" % big)
        f["platted"] = bool(r.get("subdivision_name") or (pa or {}).get("SUBNAME"))
        f["zoning_nonres"] = r["lane"] in ("lot", "homesite") and (
            f.get("zoning") in NONRES_ZONES or r.get("property_type") == "Commercial-Vacant Land")
        verdict_for(f)
        out.append(f)
    return out


# East Tennessee builds on hills: Union's "half the lot over a 15% grade fails" rule (a flat Ohio county) would
# reject 70% of the builder-fit lots here, most of them Sevier cabin lots that cabin builders buy every week. Sevier's
# own rules allow 30-50% slopes on 2 acre lots. So a lot fails only at a 45% AVERAGE grade, and 15-45% is priced.
LOT_SLOPE_FAIL, LOT_SLOPE_STEEP, LOT_SLOPE_SOME = 45, 25, 15
TRACT_SLOPE_FAIL, TRACT_SLOPE_FLAG = 35, 20


def lot_slope(f: dict, score: int, hard: list, flags: list) -> tuple:
    """Apply the East Tennessee slope rule to a lot verdict; returns (score, hard, flags)."""
    mean = f.get("slope_mean_pct")
    if mean is None:
        return score, hard, flags
    if mean >= LOT_SLOPE_FAIL:
        hard = hard + ["average grade %d%%: too steep to build without major grading" % mean]
    elif mean >= LOT_SLOPE_STEEP:
        score -= 8
        flags = flags + ["steep: average grade %d%%, a hillside or cabin builder's lot; price the grading" % mean]
    elif mean >= LOT_SLOPE_SOME:
        score -= 3
        flags = flags + ["sloped: average grade %d%%" % mean]
    return max(0, score), hard, flags


def verdict_for(f: dict) -> None:
    """Fill verdict, score, hard reasons and flags in place, by lane. Re-run after topo adds slope."""
    if f["lane"] in ("lot", "homesite"):
        g = dict(f)
        g["owner_cat"] = {"unknown": "person"}.get(f["owner_cat"], f["owner_cat"])
        g["class"] = None
        g["steep_share"] = 0.0 if f.get("slope_mean_pct") is not None else None    # slope is scored below instead
        v, s, hard, flags = vl.buildability(g)
        if v not in ("PUBLIC OR COMMON", "BUILT SINCE REAPPRAISAL", "BUILDER INVENTORY"):
            s, hard, flags = lot_slope(f, s, hard, flags)
            septic = any(x.startswith(("septic risk", "sewer unknown")) for x in flags)
            v = ("NOT BUILDABLE" if hard else "BUILDABLE" if s >= 75 and not septic else
                 "LIKELY BUILDABLE" if s >= 55 else "VERIFY")
        elif v == "BUILDER INVENTORY":
            s, hard, flags = lot_slope(f, s, hard, flags)
        if f["owner_cat"] == "public":
            v, s, hard = "PUBLIC OR COMMON", 0, ["owner is a public body"]
        if f.get("geom_src") != "state polygon" and v not in ("PUBLIC OR COMMON", "BUILT SINCE REAPPRAISAL"):
            flags = flags + ["Knox: footprint estimated from the cluster acreage, no county polygon"]
        f.update(verdict=v, score=s, hard=hard, flags=flags, path=None)
    else:
        v, s, hard, flags, path = tract_fit(f)
        f.update(verdict=v, score=s, hard=hard, flags=flags, path=path)


def phase_screen(args) -> int:
    fips = [f for f in COUNTIES if not args.counties or COUNTIES[f]["name"] in args.counties]
    t = time.time()
    res = {}
    with cf.ProcessPoolExecutor(min(6, len(fips))) as ex:
        futs = {ex.submit(screen_county, f): f for f in fips}
        for fu in cf.as_completed(futs):
            f = futs[fu]
            res[f] = fu.result()
            c = collections.Counter(x["verdict"] for x in res[f])
            log("screen %s: %d parcels %s (%.0fs)" % (COUNTIES[f]["name"], len(res[f]), dict(c.most_common()), time.time() - t))
    prev = {}
    if args.counties and (OUT / "screen.jsonl").exists():
        for x in read_layer(OUT / "screen.jsonl"):
            if x["fips"] not in res:
                prev.setdefault(x["fips"], []).append(x)
    res.update(prev)
    with open(OUT / "screen.jsonl", "w", encoding="utf-8") as fh:
        for f in COUNTIES:
            for x in res.get(f, []):
                fh.write(json.dumps(x, default=str) + "\n")
    return 0


# ----------------------------------------------------------------- phase: topo

LOT_PTS, LOT_RING_PTS, TRACT_PTS = 24, 16, 60
TOPO_SKIP = {"PUBLIC OR COMMON", "BUILT SINCE REAPPRAISAL", "BUILDER INVENTORY", "NOT BUILDABLE", "NOT A FIT"}


def topo_candidates(screen: list, fits: dict | None = None) -> list:
    """Parcels that can reach a mail list and whose verdict slope can still change: sellable owners, not ruled out,
    and either a tract that passed the entitlement screen or a lot that fits a known builder. Sampling all ~100,000
    survivors would take about four hours of 3DEP calls; the rest are marked "slope not checked"."""
    fits = fits if fits is not None else (_load(OUT / "fits.json", {}) or {})
    out = []
    for x in screen:
        if x.get("lat") is None or x["verdict"] in TOPO_SKIP or not is_seller(x):
            continue
        if x["lane"] in ("lot", "homesite"):
            if str(x["dataflik_id"]) in fits and x["verdict"] in vl.MARKETABLE:
                out.append(x)
        elif "ENTITLEMENT" in x["verdict"]:
            out.append(x)
    return out


def _topo_county(fips: str, cands: list, cache: dict) -> dict:
    """3DEP grid samples per parcel (plus a ring around lots), batched about ten lots a request."""
    from shapely.geometry import Point
    todo = [x for x in cands if str(x["dataflik_id"]) not in cache]
    if not todo:
        return {}
    P = vl.Proj(statistics.median(x["lon"] for x in todo), statistics.median(x["lat"] for x in todo))
    polys = {}
    for f in read_layer(GIS / ("olg_%s.jsonl" % fips)):
        k = pkey(f["attributes"].get("GISLINK"))
        if k not in polys:
            polys[k] = f.get("geometry")
    items = []
    for x in todo:
        g = vl.esri_to_shape(polys.get(pkey(x.get("gislink")))) if x.get("geom_src") == "state polygon" else None
        gf = P.geom(g) if g is not None else Point(*P.xy(x["lon"], x["lat"])).buffer(
            math.sqrt(max(x.get("acres") or 0.05, 0.05) * 43560 / math.pi), 16)
        tract = x["lane"] not in ("lot", "homesite")
        s = vl.spacing_for(gf.area, TRACT_PTS if tract else LOT_PTS)
        lp = vl.grid_points(gf, s)
        rp = [] if tract else vl.grid_points(gf.buffer(vl.RING_FT).difference(gf),
                                             vl.spacing_for(gf.buffer(vl.RING_FT).difference(gf).area, LOT_RING_PTS))
        items.append((str(x["dataflik_id"]), s, lp, rp))
    batches, cur, size = [], [], 0
    for it in items:
        n = len(it[2]) + len(it[3])
        if cur and size + n > vl.SAMPLE_BATCH:
            batches.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += n
    if cur:
        batches.append(cur)

    def work(batch):
        z = vl.dep_samples([P.ll(x, y) for _, _, lp, rp in batch for (_, _, x, y) in lp + rp])
        res, k = {}, 0
        for pid, s, lp, rp in batch:
            lz = {(i, j): z[k + n] for n, (i, j, _, _) in enumerate(lp)}
            k += len(lp)
            res[pid] = vl.topo_from_samples(lz, s, z[k:k + len(rp)])
            k += len(rp)
        return res

    got = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(6) as ex:
        for n, res in enumerate(ex.map(work, batches), 1):
            got.update(res)
            if n % 25 == 0:
                cache.update(got)
                _dump(cache, OUT / "topo.json")
                log("  topo %s: %d / %d requests (%.0fs)" % (COUNTIES[fips]["name"], n, len(batches), time.time() - t0))
    log("topo %s: %d parcels in %d requests, %.0fs" % (COUNTIES[fips]["name"], len(items), len(batches), time.time() - t0))
    return got


def phase_topo(args) -> int:
    screen = read_layer(OUT / "screen.jsonl")
    cache = {} if args.refresh else (_load(OUT / "topo.json", {}) or {})
    cands = topo_candidates(screen)
    by = collections.defaultdict(list)
    for x in cands:
        if not args.counties or COUNTIES[x["fips"]]["name"] in args.counties:
            by[x["fips"]].append(x)
    log("topo: %d candidates, %d cached" % (len(cands), sum(1 for x in cands if str(x["dataflik_id"]) in cache)))
    for f in sorted(by, key=lambda k: len(by[k])):
        cache.update(_topo_county(f, by[f], cache))
        _dump(cache, OUT / "topo.json")
    return 0


# ----------------------------------------------------------------- phase: buyers

def _region_args() -> list:
    return ["--market", REGION_MARKET, "--fips", ",".join(COUNTIES), "--name", "Knoxville region (11 counties), TN"]


def run_builder_land(phase: str) -> int:
    """builder_land on the combined region (sources.json points it at both county pulls, read in place)."""
    import subprocess
    d = BL / REGION_MARKET
    d.mkdir(parents=True, exist_ok=True)
    if not (d / "sources.json").exists():
        _dump(list(MARKETS), d / "sources.json")
    meta = _load(d / "meta.json", {}) or {}
    meta.update({"market": REGION_MARKET, "fips": list(COUNTIES), "name": "Knoxville region (11 counties), TN",
                 "county_names": {f: v["name"] for f, v in COUNTIES.items()}})
    _dump(meta, d / "meta.json")
    cmd = [sys.executable, str(ROOT / "src" / "builder_land.py"), *_region_args(), "--phase", phase]
    return subprocess.call(cmd, cwd=str(ROOT))


def _takedown_units(deals: list) -> None:
    """A deed that carried 3+ of one builder's lots on one day is priced per lot."""
    cnt = collections.Counter((d["builder_key"], d["acq_date"], d["acq_price"]) for d in deals)
    for d in deals:
        k = cnt[(d["builder_key"], d["acq_date"], d["acq_price"])]
        d["unit"] = int(d["acq_price"] / k) if k >= 3 else d["acq_price"]
        d["deed_lots"] = k


def phase_buyers(args) -> int:
    if not args.skip_builder_land:
        rc = run_builder_land("analyze")
        if rc:
            return rc
    region = BL / REGION_MARKET
    an = _load(region / "analysis.json", {}) or {}
    rows = bl.load(region)
    sales = bl.load_sales(region)
    log("buyers: %d parcels, %d with sales history" % (len(rows), len(sales)))
    lat0 = statistics.median([r["lat"] for r in rows if r.get("lat") is not None])
    grid = bl.Grid(lat0)
    classify = bl.area_index(rows, grid)
    nb = []
    for r in rows:
        if bl.is_house(r) and r["year_built"] >= bl.NEW_BUILD_YEAR:
            r["setting"] = classify(r)[0]
            nb.append(r)
    deals = bl.build_deals(nb, sales)
    for d in deals:
        d["niche"] = bl.niche_of(d)
    recent = [d for d in deals if d["acq_date"] >= bl.SINCE_MS]
    _takedown_units(recent)
    by_id = {r["dataflik_id"]: r for r in rows}

    county = {}
    for f, meta in COUNTIES.items():
        homes = [r for r in nb if r["fips"] == f]
        yb = collections.Counter(r["year_built"] for r in homes if r["year_built"] >= 2019)
        vals = [r.get("property_value") for r in homes if r["year_built"] >= 2021 and r.get("property_value")]
        ds = [d for d in recent if d["fips"] == f]
        prod = [d for d in ds if d["niche"] == "production" and d["builder_entity"]]
        small = [d for d in ds if d["niche"] != "production" and d["builder_entity"]]
        # the developer who sold the finished lot: the seller on the builder's lot deed
        devs = collections.Counter()
        for d in prod:
            acq = next((s for s in sales.get(d["id"], []) if s["sale_date"] == d["acq_date"]
                        and s["sale_amount"] == d["acq_price"]), None)
            if acq and bl.is_entity(acq.get("seller_full_name")):
                devs[bl.norm(acq.get("seller_full_name"))] += 1
        pb = collections.defaultdict(list)
        for d in prod:
            pb[d["builder"]].append(d)
        prod_rank = sorted(pb.items(), key=lambda kv: -len(kv[1]))
        # tract buyers: entities that bought 5+ acres of land here since 2021
        tb = collections.defaultdict(list)
        for i, ss in sales.items():
            r = by_id.get(i)
            if not r or r["fips"] != f or (r.get("area_lot_acres") or 0) < 5 or not bl.is_land(r):
                continue
            for s in ss:
                if s["sale_date"] >= bl.SINCE_MS and s["sale_amount"] >= 20000 and not bl.family(s) \
                        and bl.is_entity(s.get("buyer_full_name")):
                    tb[bl.norm(s.get("buyer_full_name"))].append((s["sale_amount"], r.get("area_lot_acres"), bl.year(s["sale_date"])))
        tract_rank = sorted(tb.items(), key=lambda kv: (-len(kv[1]), -sum(x[0] for x in kv[1])))
        units = [d["unit"] for d in prod if d.get("unit") and 5000 <= d["unit"] < 400000]
        county[f] = {
            "name": meta["name"],
            "new_homes_by_year": dict(sorted(yb.items())),
            "new_home_value_med": round(statistics.median(vals)) if vals else None,
            "production_lot_buys": len(prod), "production_builder_count": len(pb),
            "small_builder_lot_buys": len(small),
            "finished_lot_med": round(statistics.median(units)) if units else None,
            "production_builders": [{"builder": b, "lots": len(v),
                                     "lot_med": bl.med(x["unit"] for x in v if x.get("unit") and x["unit"] < 400000),
                                     "home_med": bl.med(x["exit_price"] for x in v if x.get("exit_price")),
                                     "last": max(bl.day(x["acq_date"]) for x in v),
                                     "subdivisions": [s for s, _ in collections.Counter(
                                         (by_id.get(x["id"]) or {}).get("subdivision_name") for x in v
                                         if (by_id.get(x["id"]) or {}).get("subdivision_name")).most_common(3)]}
                                    for b, v in prod_rank[:15]],
            "lot_developers": devs.most_common(12),
            "tract_buyers": [{"buyer": b, "buys": len(v), "spent": sum(x[0] for x in v),
                              "acres": round(sum(x[1] or 0 for x in v), 1),
                              "per_acre_med": bl.med(x[0] / x[1] for x in v if x[1]),
                              "years": sorted({x[2] for x in v})} for b, v in tract_rank[:15]],
            "small_builders": [{"name": b["name"], "type": b["type"], "buys": b["buys"], "per_year": b.get("per_year"),
                                "niche": b.get("main_niche"), "reach": b.get("reach"), "last_buy": b.get("last_buy"),
                                "counties": b.get("counties"), "matched_lots": b.get("matched_lots"),
                                "matched_tracts": b.get("matched_tracts"),
                                "lot_price_med": ((b.get("classes") or {}).get("lot") or {}).get("price_med"),
                                "sells_for": b.get("sells_for")}
                               for b in an.get("boxes", []) if meta["name"] in (b.get("counties") or [])],
        }
        log("buyers %s: %d production lot buys by %d builders, %d small-builder buys, %d tract buyers, %d boxes" % (
            meta["name"], len(prod), len(pb), len(small), len(tb), len(county[f]["small_builders"])))
    dev = read_layer(GIS / "kgis_dev_projects.jsonl")
    pipe = [f["attributes"] for f in dev if re.search(r"resid|subdiv|single|home|lot", str(f["attributes"].get("TYPE")) + " "
                                                       + str(f["attributes"].get("DESCRIPTION")), re.I)]
    _dump({"built": TODAY.isoformat(), "county": county, "box_count": len(an.get("boxes", [])),
           "knox_pipeline": pipe, "region_summary": an.get("summary"), "lots_matched": len(an.get("lots", [])),
           "tracts_matched": len(an.get("tracts", []))}, OUT / "buyers.json")
    # every matched lot and tract, keyed by dataflik id, so the mail list can name the builders it fits
    fits = {}
    for x in an.get("lots", []) + an.get("tracts", []):
        i = x.get("id") or x.get("dataflik_id")
        if i:
            fits[str(i)] = {"builders": [m.get("name") for m in x.get("matches") or []], "n_fit": x.get("n_fit"),
                            "offer": x.get("offer"), "ref": x.get("ref"), "ref_basis": x.get("ref_basis"),
                            "per_acre_ref": x.get("per_acre_ref"), "production_builders": x.get("production_builders"),
                            "production_homes_2km": x.get("production_homes_2km")}
    _dump(fits, OUT / "fits.json")
    return 0


# ----------------------------------------------------------------- phase: score

DECEASED_KEYS = {"is_estate_sales", "is_probate_properties", "is_heirs_property", "is_obituary_property",
                 "is_inheritance_property", "is_pre_probate"}
MAIL_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
SEG_LABEL = {**dict(vl.SEGMENTS), "aged": "Owner 75 or older"}
STRONG = {"tax", "estate", "oos", "liens"}
SIDE_LOT_M = 122                       # 400 ft: the owner's own house is next door
ENT_COST_PER_LOT, ENT_COST_FIXED = 2500, 15000      # engineering, concept plan, legal (estimate, Knox 2026)
SPLIT_COST_EACH = 3000                 # survey and plat per homesite split (estimate)
PAPER_LOT_RATIO = 0.42                 # approved paper lot / finished lot: Knox measured ~$30K against ~$69-75K
SEPTIC_LOT_RATIO = 0.40                # approved septic lot / a 1-2 acre homesite sale in the same county
DENSITY = {"sewer subdivision": 2.0, "septic lots": 0.7}   # net homes per developable acre after roads and open space
HOMESITE_AC = 3.0
PHASE_AC = 150                         # spread math stops at a first phase: a builder takes a big farm in pieces
OFFER_LO, OFFER_HI = 0.50, 0.65        # lots and homesites, of the reference price (Union default)
TRACT_LO, TRACT_HI = 0.85, 1.00        # tracts are bought at farm price; the approval is the margin


def motivation(x: dict, county_zips: set, owner_land: int) -> tuple:
    tags, pts = set(), 0
    dist = set(x.get("distress") or [])
    if "is_recent_deliquent" in dist:
        tags.add("tax")
        pts += 20
    if dist & DECEASED_KEYS or x.get("owner_cat") == "trust_estate":
        tags.add("estate")
        pts += 20
    ms = (x.get("mailing_address_state") or "").upper()
    m = MAIL_ZIP.search(x.get("mailing_full_address") or "")
    if ms and ms != "TN":
        tags.add("oos")
        pts += 25
    elif m and m.group(1) not in county_zips:
        tags.add("ooc")
        pts += 12
    if "is_notice_of_default" in dist:
        tags.add("liens")
        pts += 10
    held = x.get("tenure")
    if held is not None:
        if held >= 20:
            tags.add("longhold")
            pts += 15
        elif held >= 10:
            pts += 8
        elif held < 2:
            pts -= 15
    if owner_land >= 3:
        tags.add("multi")
        pts += 5
    if x.get("side_lot"):
        tags.add("side")
    age = x.get("owner_age") or 0
    if age >= 75:
        tags.add("aged")
        pts += 8
    elif "senior" in (x.get("mflags") or []):
        pts += 5
    if not tags:
        tags.add("local")
    return max(0, min(100, pts)), tags


def tier_for(x: dict) -> str | None:
    strong = bool(STRONG & set(x.get("tags") or []))
    s, m = x.get("score") or 0, x.get("motivation") or 0
    if x["lane"] in ("lot", "homesite"):
        if x["verdict"] not in vl.MARKETABLE:
            return None
        if not x.get("n_fit"):
            return "R"          # buildable, but no builder buys here: retail or later, not the lot desk
        return "A" if s >= 65 and (strong or m >= 30) else "B" if s >= 55 and m >= 12 else "C"
    if "ENTITLEMENT" in x["verdict"]:
        return "A" if s >= 70 and (strong or m >= 25) else "B" if s >= 55 and m >= 12 else "C"
    if x["verdict"] == "HOMESITE SPLIT":
        return "B" if (strong or m >= 25) else "C"
    return None


def mail_exclusion(x: dict) -> str | None:
    if not is_seller(x):
        return "owner is a %s" % OWNER_LABEL.get(x["owner_cat"], x["owner_cat"])
    if not x.get("tier"):
        return "not marketable: %s" % x["verdict"].lower()
    if x.get("is_active_listing"):
        return "listed on the MLS"
    if x.get("tenure") is not None and x["tenure"] < 2:
        return "bought in the last 2 years"
    if not (x.get("mailing_full_address") or "").strip():
        return "no mailing address"
    return None


def _county_lot_ref(buyers: dict, fips: str) -> int | None:
    c = (buyers.get("county") or {}).get(fips) or {}
    if c.get("finished_lot_med") and c.get("production_lot_buys", 0) >= 10:
        return c["finished_lot_med"]
    vals = [v["finished_lot_med"] for v in (buyers.get("county") or {}).values()
            if v.get("finished_lot_med") and v.get("production_lot_buys", 0) >= 10]
    return round(statistics.median(vals)) if vals else None


COMP_RINGS = ((5, 0.33, 3.0), (10, 0.25, 4.0))      # (miles, size ratio low, high), tried in order
COMP_MIN = 5


class TractComps:
    """Priced arm's-length sales of vacant 5+ acre land since 2021, searched by distance and size. A county median
    cannot tell a sewered farm beside a new subdivision from a ridge two valleys over; the sales next to it can."""

    def __init__(self, land: dict, sales: dict):
        self.cells = collections.defaultdict(list)
        n = 0
        for i, r in land.items():
            a = r.get("area_lot_acres") or 0
            if r["status"] != "vacant" or a < 5 or r.get("lat") is None:
                continue
            for s in sales.get(i, []):
                if s["sale_date"] >= bl.SINCE_MS and s["sale_amount"] >= 20000 and not bl.family(s):
                    self.cells[(int(r["lat"] * 10), int(r["lon"] * 10))].append((r["lat"], r["lon"], a,
                                                                                 s["sale_amount"] / a))
                    n += 1
        self.n = n

    def near(self, lat, lon, acres):
        for miles, lo, hi in COMP_RINGS:
            m = miles * 1609.34
            got = []
            ci, cj = int(lat * 10), int(lon * 10)
            span = int(math.ceil(miles / 6.0)) + 1
            for di in range(-span, span + 1):
                for dj in range(-span, span + 1):
                    for a, b, ac, per in self.cells.get((ci + di, cj + dj), ()):
                        if lo <= ac / max(acres, 0.1) <= hi and bl.meters(lat, lon, a, b) <= m:
                            got.append((per, ac))
            if len(got) >= COMP_MIN:
                pers = sorted(p for p, _ in got)
                return {"median_per_acre": round(statistics.median(pers)), "n": len(got),
                        "basis": "%d tract sales within %d mi since 2021 (%g-%g ac), median $%s/ac" % (
                            len(got), miles, round(min(a for _, a in got)), round(max(a for _, a in got)),
                            format(round(statistics.median(pers)), ","))}
        return None


def price_parcel(x: dict, prices: dict, fits: dict, buyers: dict, comps: "TractComps | None" = None) -> None:
    """Reference price, offer band and, for tracts, the entitlement math. Every figure is labeled with its basis."""
    a = x.get("acres") or 0
    fit = fits.get(str(x["dataflik_id"])) or {}
    p = price_for(prices, x["fips"], a)
    if x["lane"] in ("lot", "homesite"):
        if fit.get("ref"):
            ref, basis = fit["ref"], "what fitting builders paid nearby (%s)" % (fit.get("ref_basis") or "builder buys")
        elif p:
            ref, basis = p["median_price"], "recent vacant sales, %s" % p["basis"]
        else:
            ref, basis = None, "no comparable sales"
        x.update(ref_price=ref, ref_basis=basis, offer_lo=round(ref * OFFER_LO, -2) if ref else None,
                 offer_hi=round(ref * OFFER_HI, -2) if ref else None, builders_fit=fit.get("builders") or [],
                 n_fit=fit.get("n_fit") or 0)
        return
    near = comps.near(x["lat"], x["lon"], a) if comps and x.get("lat") is not None else None
    if near:
        per_ac, basis = near["median_per_acre"], near["basis"]
    else:
        per_ac = p.get("median_per_acre") if p else None
        basis = "county band: %s" % p["basis"] if p else "no comparable sales"
    farm = round(per_ac * a, -3) if per_ac else None
    x.update(farm_per_acre=per_ac, farm_ref=farm, ref_basis=basis, offer_lo=round(farm * TRACT_LO, -3) if farm else None,
             offer_hi=round(farm * TRACT_HI, -3) if farm else None,
             builders_fit=fit.get("builders") or [], production_builders=fit.get("production_builders"),
             production_homes_2km=fit.get("production_homes_2km"))
    dev = x.get("dev_acres") or 0
    if "ENTITLEMENT" in x["verdict"]:
        lots = int(min(dev, PHASE_AC) * DENSITY.get(x.get("path") or "", 0.7))
        if dev > PHASE_AC:
            x["phase_note"] = "first %d acres only" % PHASE_AC
        if x.get("path") == "sewer subdivision":
            fin = _county_lot_ref(buyers, x["fips"])
            lot_val = round(PAPER_LOT_RATIO * fin, -2) if fin else None
            lb = "%.0f%% of the county's finished lot median ($%s)" % (PAPER_LOT_RATIO * 100, format(fin or 0, ","))
        else:
            hp = price_for(prices, x["fips"], 1.5)
            lot_val = round(SEPTIC_LOT_RATIO * hp["median_price"], -2) if hp else None
            lb = "%.0f%% of a 1-2 acre homesite sale (%s)" % (SEPTIC_LOT_RATIO * 100, hp["basis"] if hp else "n/a")
        approved = lots * lot_val if lot_val else None
        cost = ENT_COST_FIXED + ENT_COST_PER_LOT * lots
        x.update(est_lots=lots, paper_lot_value=lot_val, paper_lot_basis=lb, approved_value=approved,
                 entitle_cost=cost, est_spread=(approved - (farm or 0) - cost) if approved else None)
    elif x["verdict"] == "HOMESITE SPLIT":
        n = max(2, int(dev // HOMESITE_AC))
        hp = price_for(prices, x["fips"], HOMESITE_AC)
        val = n * hp["median_price"] if hp else None
        x.update(est_lots=n, paper_lot_value=hp["median_price"] if hp else None,
                 paper_lot_basis="2-5 acre homesite sales (%s)" % hp["basis"] if hp else "n/a",
                 approved_value=val, entitle_cost=SPLIT_COST_EACH * n,
                 est_spread=(val - (farm or 0) - SPLIT_COST_EACH * n) if val else None)


def phase_score(args) -> int:
    screen = read_layer(OUT / "screen.jsonl")
    topo = _load(OUT / "topo.json", {}) or {}
    land = {r["dataflik_id"]: r for r in iter_land()}
    tam = _load(OUT / "tam.json", {}) or {}
    prices = tam.get("prices") or {}
    fits = _load(OUT / "fits.json", {}) or {}
    buyers = _load(OUT / "buyers.json", {}) or {}
    zips = collections.defaultdict(set)
    owner_land = collections.Counter()
    for r in land.values():
        if r.get("property_zip"):
            zips[r["fips"]].add(str(r["property_zip"])[:5])
        k = vl.owner_key(r.get("full_name"))
        if k:
            owner_land[(r["fips"], k)] += 1
    # side lots: the lot's owner also owns a house within 400 ft
    want = {vl.owner_key(land[x["dataflik_id"]].get("full_name")) for x in screen
            if x["lane"] in ("lot", "homesite") and x["dataflik_id"] in land}
    want.discard("")
    comps = TractComps(land, load_sales_all())
    log("score: %d tract sales indexed for nearby comps" % comps.n)
    homes = collections.defaultdict(list)
    for r in iter_rows():
        if bl.is_house(r) and r.get("lat") is not None:
            k = vl.owner_key(r.get("full_name"))
            if k in want:
                homes[k].append((r["lat"], r["lon"]))
    out = []
    for x in screen:
        r = land.get(x["dataflik_id"]) or {}
        for k in ("mailing_full_address", "mailing_address_state", "mailing_address_city", "owner_age",
                  "is_active_listing", "full_name_2", "last_sale_date", "last_sale_price", "property_type"):
            x[k] = r.get(k)
        t = topo.get(str(x["dataflik_id"]))
        if t:
            x.update(steep_share=t.get("steep_share"), slope_mean_pct=t.get("slope_mean_pct"),
                     relief_ft=t.get("relief_ft"), sits_low_ft=t.get("sits_low_ft"), topo="3DEP")
            verdict_for(x)
        else:
            x["topo"] = "slope not checked"
        k = vl.owner_key(r.get("full_name"))
        if x["lane"] in ("lot", "homesite") and k and x.get("lat") is not None:
            x["side_lot"] = any(bl.meters(x["lat"], x["lon"], a, b) <= SIDE_LOT_M for a, b in homes.get(k, ()))
        x["mflags"], x["distress"], x["tenure"] = r.get("mflags"), r.get("distress"), r.get("tenure")
        m, tags = motivation(x, zips[x["fips"]], owner_land[(x["fips"], k)] if k else 1)
        x["motivation"], x["tags"] = m, sorted(tags)
        x["segment"] = next((SEG_LABEL[t] for t, _ in vl.SEGMENTS + [("aged", "")] if t in tags), "Local owner")
        x["n_fit"] = (fits.get(str(x["dataflik_id"])) or {}).get("n_fit") or 0
        x["tier"] = tier_for(x)
        price_parcel(x, prices, fits, buyers, comps)
        if "ENTITLEMENT" in x["verdict"] and x["tier"] in ("A", "B") and x.get("est_spread") is not None \
                and x["est_spread"] <= 0:
            # nearby land already sells at or above what the approval would make it worth: no margin at farm price
            x["tier"] = "C"
            x["flags"] = (x.get("flags") or []) + ["nearby land already sells at development prices: no entitlement margin"]
        x["mail_exclude"] = mail_exclusion(x)
        out.append(x)
    with open(OUT / "scored.jsonl", "w", encoding="utf-8") as fh:
        for x in out:
            fh.write(json.dumps(x, default=str) + "\n")
    c = collections.Counter((x["lane"], x["tier"]) for x in out if not x["mail_exclude"])
    log("score: mailable by lane and tier %s" % dict(sorted(c.items(), key=str)))
    return 0


# ----------------------------------------------------------------- county rules (researched 2026-11-09)

# From county planning sites, zoning and subdivision regulations and local news, read 2026-11-09. Verify the line
# that matters before a contract depends on it: these change, and two counties publish almost nothing.
COUNTY_RULES = {
    "Knox": {"zoning": "yes", "body": "Knoxville-Knox County Planning; County Commission votes on rezonings",
             "rules": "Concept plan for 6+ lots. PR zoning sets 'up to X du/ac' (2-4 in the suburbs). Growth Policy "
                      "Plan Rural Area capped at 2 du/ac.",
             "sewer": "Utility districts (First Utility, Hallsdale-Powell, West Knox, Knox-Chapman, KUB in the city).",
             "climate": "2026 state law makes the county share road-upgrade costs; staff recommended denying the "
                        "121-lot Carter Valley plan (Aug 2026) because the county cannot fund its share. 2025: "
                        "1,905 new lots approved, unincorporated residential rezoning acreage down over 60%.",
             "aft": "none", "status": "open, but only inside the Planned Growth Area on roads that need no upgrade",
             "risk": 1,
             "sources": "knoxplanning.org/cases/3-B-26-DP; compassknox.com/2026/08/13/new-law-could-slow-development; "
                        "knoxplanning.org/news/2026/0609/855"},
    "Blount": {"zoning": "yes (county-wide since 2000)", "body": "Blount County Regional Planning Commission",
               "rules": "Major subdivision = 5+ lots or a new road; preliminary plat valid 24 months. S zone 0.75 ac "
                        "on septic, 0.5 ac on sewer; PUD in S up to 6.2 du/ac on sewer (15 ac minimum). Needs an "
                        "18 ft paved road or the developer widens it.",
               "sewer": "Alcoa, Maryville, South Blount UD, Knox-Chapman UD, Friendsville, Tuckaleechee UD.",
               "climate": "Oct 2026: commissioners passed a resolution recommending a 12-month moratorium on "
                          "subdivisions over 10 lots and multifamily, referred to the planning commission (not in "
                          "effect). Exempt: 10 lots or fewer, and projects with preliminary approval. 2021 denials: "
                          "Pate Farms (190 lots), Best Farms (224).",
               "aft": "none", "status": "PAUSE RISK for 11+ lots; small plats and already-approved land unaffected",
               "risk": 3,
               "sources": "blounttn.gov zoning + subdivision regs 2023; wate.com blount-county-housing-moratorium "
                          "(verified 2026-11-09)"},
    "Loudon": {"zoning": "yes (outside cities)", "body": "Loudon County Regional Planning Commission",
               "rules": "A-1 5 ac minimum since May 2024; A-2 2.5 ac; A-3 1 ac; R-1 0.5 ac; cluster option 15,000 "
                        "sq ft lots at 2 du/ac on public sewer only.",
               "sewer": "Tellico Area Services (TASS), Lenoir City UB, Loudon UB.",
               "climate": "Reported Dec 2025: approvals suspended for 10+ lot subdivisions on county roads under "
                          "20 ft until new road standards pass; the Home Builders Association sued over notice. "
                          "PUD moratorium since Oct 2021. (Article now offline; confirm with the county.)",
               "aft": "$1.00 per sq ft", "status": "RESTRICTED: road pause reported, PUDs on hold", "risk": 3,
               "sources": "planningandcodes.loudoncounty-tn.gov subdivision regs 2024, zoning art. V; county packet "
                          "2025-12-15"},
    "Sevier": {"zoning": "yes", "body": "Sevier County Regional Planning Commission",
               "rules": "Concept plan required above 10 lots (it serves as the preliminary plat). A-1 30,000 sq ft; "
                        "R-2 7,500 sq ft. Lots on 30-50% slopes need 2 ac, 50%+ need 3 ac.",
               "sewer": "City systems (Sevierville, Pigeon Forge, Gatlinburg), case by case.",
               "climate": "No moratorium found; May 2025 rezonings passed 23-0.", "aft": "none",
               "status": "open; sewer only from the cities", "risk": 1,
               "sources": "seviercountytn.gov planning; subdivision regs 2023; zoning resolution Dec 2025"},
    "Jefferson": {"zoning": "yes", "body": "Jefferson County Regional Planning Commission (joint with BZA)",
                  "rules": "Preliminary plat above 4 lots. A-1 40,000 sq ft with septic approval and 50 ft of "
                           "county road frontage.",
                  "sewer": "No county system: Jefferson City, Dandridge, White Pine, Tennessee Wastewater Systems.",
                  "climate": "No moratorium found.", "aft": "about $1.10 per sq ft (raised 10% in 2024)",
                  "status": "open; septic acre lots are the norm", "risk": 1,
                  "sources": "jeffersoncountytn.gov zoning + subdivision regs 2025"},
    "Anderson": {"zoning": "yes", "body": "Anderson County Regional Planning Commission",
                 "rules": "Minimum lot cut proposed from 22,000 to 20,000 sq ft (Nov 2025). Subdivision rule "
                          "amendments heard Apr 2026.",
                 "sewer": "Hallsdale-Powell UD in part; others not found.",
                 "climate": "No moratorium found; county workshop on a growth plan Nov 2025.", "aft": "none",
                 "status": "open; thin public record", "risk": 2, "sources": "andersoncountytn.gov public notices"},
    "Roane": {"zoning": "yes (outside cities)", "body": "Roane County Regional Planning Commission",
              "rules": "Major subdivision above 2 lots. A-1 1 ac; A-2 0.5 ac (2 du/ac); PUD 2 du/ac.",
              "sewer": "Harriman Utility Board, Rockwood, a county plant.",
              "climate": "Rezonings pass with road studies deferred to site plan (May 2026); a commercial one failed "
                         "over access and private utilities (Jul 2026).", "aft": "none",
              "status": "open; low density caps", "risk": 2,
              "sources": "roanecountytn.gov zoning 2013 + subdivision regs 2024"},
    "Union": {"zoning": "NO county zoning outside cities", "body": "Union County Planning Commission",
              "rules": "Only subdivision regulations apply; no rezoning step exists.",
              "sewer": "Hallsdale-Powell UD reaches in from north Knox; Maynardville system.",
              "climate": "Zoning debated in 2024, outcome not found.", "aft": "none",
              "status": "open; no rezoning needed, but sewer in pockets only", "risk": 2,
              "sources": "ctas.tennessee.edu; historicunioncounty.com"},
    "Grainger": {"zoning": "NO county zoning found", "body": "Grainger County Planning Commission",
                 "rules": "A division where every tract is over 5 ac is not a subdivision. Lots 10,000 sq ft with "
                          "water and sewer, 20,000 with water only, 30,000 with neither.",
                 "sewer": "not found", "climate": "not found", "aft": "none",
                 "status": "open; 5+ ac splits need no approval at all", "risk": 2,
                 "sources": "graingercountytn.com planning"},
    "Campbell": {"zoning": "not found", "body": "Campbell County Regional Planning Commission",
                 "rules": "Handles minor and major subdivisions; a 2023 proposal cut the minimum lot to 12,500 sq "
                          "ft where private utilities serve.",
                 "sewer": "Caryville-Jacksboro Utility Board, LaFollette Utilities; drip-field systems allowed.",
                 "climate": "not found", "aft": "none", "status": "unclear; verify locally", "risk": 2,
                 "sources": "1450wlaf.com (search snippets only, verify)"},
    "Morgan": {"zoning": "not found", "body": "not found", "rules": "not found",
               "sewer": "Harriman Utility Board serves part of the county.", "climate": "not found", "aft": "none",
               "status": "unknown; verify locally", "risk": 2, "sources": "hub-tn.com"},
}
STATEWIDE_RULES = [
    ("Public Chapter 1044 (from Jan 1, 2027)", "Staff must comment on a preliminary plat within 30 business days; a "
     "plat not acted on within 60 business days is deemed approved. Entitlement timelines shorten from 2027.",
     "ips.tennessee.edu Policy Guide, Public Chapter 1044"),
    ("Road cost sharing (2026)", "Counties must pay a proportionate share of road upgrades a development needs; Knox "
     "staff are already recommending denials where the county cannot fund its share. Prefer tracts on roads that need "
     "no widening.", "compassknox.com 2026-08-13"),
    ("Adequate facilities tax", "Only Jefferson (~$1.10/sq ft) and Loudon ($1.00/sq ft) in this region; cap $1.50.",
     "TACIR 2023; CTAS"),
]


# ----------------------------------------------------------------- phase: export

def big_money(x) -> str:
    """About $15.8 billion, not $15,750,033,863: a total this rough should not look precise."""
    x = float(x or 0)
    for div, word in ((1e9, "billion"), (1e6, "million")):
        if x >= div:
            return "about $%.1f %s" % (x / div, word)
    return "about $%s" % format(int(round(x, -3)), ",")


def _tract_type(name: str) -> str:
    s = bl.norm(name)
    if _BUILDERS_TN.search(s):
        return "production builder"
    if re.search(r"\b(HOMES|HOME BUILDERS|BUILDERS?|BUILT|CONSTRUCTION|DEVELOPMENT|DEVELOPERS?|COMMUNITIES)\b", s):
        return "builder or developer"
    if re.search(r"\b(TIMBER|FOREST|RANCH|FARMS?|LAND MANAGEMENT)\b", s):
        return "farm or timber"
    if re.search(r"\b(PROPERTY OWN|POA|CLUB|ASSOCIATION|CHURCH|MINISTR|INDUSTRIES|MARINA|RAILWAY)\b", s):
        return "not a land buyer"
    return "investor or holding company"


def _mail_parts(addr: str | None) -> dict:
    try:
        return vl.mail_parts(addr)
    except Exception:  # noqa: BLE001
        return {"street": addr, "city": None, "state": None, "zip": None}


def _split_name(name: str | None) -> tuple:
    if not name:
        return "", "", ""
    if bl.is_entity(name) or vl.owner_category(name) in ("trust_estate", "entity", "builder", "bank"):
        return "", "", name
    toks = name.split()
    return (toks[0], toks[-1], "") if len(toks) >= 2 else ("", name, "")


CSV_COLS = ["DataSift id", "Lane", "Tier", "Verdict", "County", "Property Street", "Property City", "Property State",
            "Property ZIP", "Owner First Name", "Owner Last Name", "Owner Company", "Owner", "Mailing Street",
            "Mailing City", "Mailing State", "Mailing ZIP", "Acres", "Developable acres", "Path", "Est lots",
            "Build or fit score", "Motivation", "Segment", "Reason tags", "Builders this fits", "Offer low",
            "Offer high", "Reference price", "Price basis", "Est approved value", "Est spread", "Sewer", "Flood %",
            "Frontage ft", "Slope", "Flags", "Parcel number", "State GISLINK", "Lists", "Tags"]


def csv_row(x: dict) -> list:
    m = _mail_parts(x.get("mailing_full_address"))
    fn, ln, co = _split_name(x.get("full_name"))
    lane_tag = {"lot": "Land - Builder Lot", "homesite": "Land - Homesite", "small_tract": "Land - Small Tract",
                "ent_tract": "Land - Entitlement Tract", "large_tract": "Land - Large Tract"}[x["lane"]]
    slope = ("%d%% over 15%% grade" % round(100 * x["steep_share"])) if x.get("steep_share") is not None else x.get("topo")
    return [x["dataflik_id"], LANE_LABEL[x["lane"]], x.get("tier"), x["verdict"], COUNTIES[x["fips"]]["name"],
            x.get("property_address"), x.get("property_city"), "TN", x.get("property_zip"), fn, ln, co,
            x.get("full_name"), m.get("street"), m.get("city"), m.get("state"), m.get("zip"), x.get("acres"),
            x.get("dev_acres"), x.get("path"), x.get("est_lots"), x.get("score"), x.get("motivation"),
            x.get("segment"), ", ".join(SEG_LABEL.get(t, t) for t in x.get("tags") or []),
            ", ".join(bl.pretty(b) for b in (x.get("builders_fit") or [])[:4]), x.get("offer_lo"), x.get("offer_hi"),
            x.get("ref_price") or x.get("farm_ref"), x.get("ref_basis"), x.get("approved_value"), x.get("est_spread"),
            x.get("sewer_basis"), round(100 * (x.get("flood_pct") or 0)), x.get("frontage_ft"), slope,
            "; ".join((x.get("hard") or []) + (x.get("flags") or [])), x.get("parcel_number"), x.get("gislink"),
            lane_tag, "Land, %s, %s, Tier %s" % (COUNTIES[x["fips"]]["name"], lane_tag, x.get("tier"))]


def _county_scorecard(scored, tam, buyers, an) -> list:
    fit_by_county = collections.Counter(x.get("county") for x in an.get("lots", []) if x.get("n_fit"))
    rows = []
    for f, meta in COUNTIES.items():
        name = meta["name"]
        b = (buyers.get("county") or {}).get(f) or {}
        yrs = b.get("new_homes_by_year") or {}
        per_year = round(statistics.mean([yrs.get(str(y), yrs.get(y, 0)) or 0 for y in (2022, 2023, 2024)]))
        mine = [x for x in scored if x["fips"] == f and not x["mail_exclude"]]
        lots_ab = sum(1 for x in mine if x["lane"] in ("lot", "homesite") and x["tier"] in ("A", "B"))
        ent_ab = sum(1 for x in mine if "ENTITLEMENT" in x["verdict"] and x["tier"] in ("A", "B"))
        splits = sum(1 for x in mine if x["verdict"] == "HOMESITE SPLIT")
        rules = COUNTY_RULES.get(name, {})
        p = (tam.get("prices") or {}).get("%s|15-50 ac" % f) or {}
        lot_score = min(40, fit_by_county[name] / 50) + min(30, b.get("small_builder_lot_buys", 0) / 6) + min(30, lots_ab / 100)
        ent_score = (min(35, b.get("production_lot_buys", 0) / 8) + min(30, per_year / 60) + min(20, ent_ab / 40)
                     - 10 * (rules.get("risk", 2) - 1))
        rows.append({"fips": f, "county": name, "new_homes_per_year": per_year,
                     "new_home_value_med": b.get("new_home_value_med"), "finished_lot_med": b.get("finished_lot_med"),
                     "production_lot_buys": b.get("production_lot_buys", 0),
                     "production_builders": b.get("production_builder_count", len(b.get("production_builders") or [])),
                     "small_builder_lot_buys": b.get("small_builder_lot_buys", 0),
                     "lots_fitting_a_builder": fit_by_county[name], "lots_ab": lots_ab, "ent_ab": ent_ab,
                     "splits": splits, "tract_per_acre": p.get("median_per_acre"), "tract_sales": p.get("n"),
                     "rules_status": rules.get("status"), "risk": rules.get("risk"),
                     "lot_score": round(lot_score), "ent_score": round(ent_score)})
    for key in ("lot_score", "ent_score"):
        for i, r in enumerate(sorted(rows, key=lambda r: -r[key]), 1):
            r[key.replace("score", "rank")] = i
    return rows


def phase_export(args) -> int:
    scored = read_layer(OUT / "scored.jsonl")
    tam = _load(OUT / "tam.json", {}) or {}
    buyers = _load(OUT / "buyers.json", {}) or {}
    an = _load(BL / REGION_MARKET / "analysis.json", {}) or {}
    if not scored or not tam:
        raise SystemExit("run --phase tam, screen, buyers and score first")
    stamp = TODAY.strftime("%Y%m%d")
    wb, sheet = bl._book()
    mail = [x for x in scored if not x["mail_exclude"]]
    cards = _county_scorecard(scored, tam, buyers, an)
    cnt = collections.Counter((x["lane"], x["tier"]) for x in mail)
    byc = tam.get("by_county_lane") or {}
    tot = collections.Counter()
    for k, c in byc.items():
        ln = k.split("|")[1]
        tot["sales_per_year"] += c.get("sales_per_year", 0)
        tot["dollars_per_year"] += c.get("dollars_per_year", 0)
        tot[ln + "_parcels"] += c.get("addressable", 0)
        tot[ln + "_acres"] += c.get("addressable_acres", 0)
        tot[ln + "_value"] += c.get("value_at_market", 0)
    lot_top = sorted(cards, key=lambda r: r["lot_rank"])[:3]
    ent_top = sorted(cards, key=lambda r: r["ent_rank"])[:3]
    # 1. overview
    ov = [
        ("Question", "How big is the land market around Knoxville, which of it is real, who buys it, and how do we "
                     "market it? Model: buy at farm price, entitle, sell the approved land (no site work), plus the "
                     "builder lot desk."),
        ("Counties", "11: Knox, Blount, Sevier, Anderson, Loudon, Roane, Jefferson, Campbell, Grainger, Union, Morgan "
                     "(the Knoxville metro plus Sevier and Jefferson)."),
        ("Parcels read", "%s parcels; %s sit in a land lane (vacant 0.06+ ac, or any parcel of 5+ ac)." % (
            bl.n_(sum((tam.get("parcels_by_county") or {}).values())), bl.n_(len(scored)))),
        ("Market size (privately owned, not builder inventory)",
         "Builder lots %s; homesites %s; small tracts 5-15 ac %s; entitlement tracts 15-150 ac %s (%s acres); large "
         "tracts %s. At recent vacant-land prices that land is worth %s." % (
             bl.n_(tot["lot_parcels"]), bl.n_(tot["homesite_parcels"]), bl.n_(tot["small_tract_parcels"]),
             bl.n_(tot["ent_tract_parcels"]), bl.n_(tot["ent_tract_acres"]), bl.n_(tot["large_tract_parcels"]),
             big_money(sum(v for k, v in tot.items() if k.endswith("_value"))))),
        ("Market size (what trades)", "Vacant land changing hands in arm's-length sales: about %s sales and %s a "
                                      "year (2022-24 average). Sales of farm tracts that carry a house are not in this "
                                      "count, so it understates the tract lane." % (
             bl.n_(tot["sales_per_year"]), big_money(tot["dollars_per_year"]))),
        ("After verification", "Mailable today: %s builder lots and homesites (A %s, B %s), %s entitlement tracts "
                               "(A %s, B %s), %s homesite-split tracts. Everything else is ruled out with its reason "
                               "on the Funnel and Not marketable sheets." % (
             bl.n_(sum(v for (ln, t), v in cnt.items() if ln in ("lot", "homesite"))),
             bl.n_(cnt[("lot", "A")] + cnt[("homesite", "A")]), bl.n_(cnt[("lot", "B")] + cnt[("homesite", "B")]),
             bl.n_(sum(1 for x in mail if "ENTITLEMENT" in x["verdict"])),
             bl.n_(sum(1 for x in mail if "ENTITLEMENT" in x["verdict"] and x["tier"] == "A")),
             bl.n_(sum(1 for x in mail if "ENTITLEMENT" in x["verdict"] and x["tier"] == "B")),
             bl.n_(sum(1 for x in mail if x["verdict"] == "HOMESITE SPLIT")))),
        ("End buyers", "%s active small-builder buy boxes across the region; %s production lot purchases by builders "
                       "since 2021; the named exits for approved tracts are on the End buyers sheet per county." % (
             bl.n_(buyers.get("box_count")), bl.n_(sum(c["production_lot_buys"] for c in cards)))),
        ("Where to start: lot desk", ", ".join("%s (%s lots fit a builder)" % (r["county"], bl.n_(r["lots_fitting_a_builder"]))
                                               for r in lot_top)),
        ("Where to start: entitlement", ", ".join("%s (%s production lot buys, %s new homes a year)" % (
            r["county"], bl.n_(r["production_lot_buys"]), bl.n_(r["new_homes_per_year"])) for r in ent_top)),
        ("Rules that move the plan", "Blount: a 12-month pause on 11+ lot subdivisions is recommended and with its "
                                     "planning commission (not in effect). Loudon: road pause reported for 10+ lots. "
                                     "Knox: 2026 road cost-sharing law is producing denials on narrow roads. Union and "
                                     "Grainger have no county zoning, so there is no rezoning step. From 2027 a "
                                     "preliminary plat not acted on in 60 business days is deemed approved."),
        ("Read before relying", "Prices are medians of recorded sales, not appraisals. Approved-value and spread figures "
                                "are estimates built from measured lot prices and labeled ratios; the Economics sheet "
                                "response rates are placeholders. Knox parcels have no county polygon: their access, "
                                "flood and slope come from an estimated footprint. Nothing was pulled into a DataSift "
                                "account."),
    ]
    sheet("Overview", ["Item", "Answer"], ov, widths={"Item": 34, "Answer": 150}, wrap=("Answer",), first=True)
    # 2. market size
    hdr = ["County", "Lane", "Parcels", "Acres", "Private owners (not builders)", "Their acres", "Owners",
           "Value at recent sale prices", "Vacant sales a year", "Vacant sale dollars a year", "Out of state", "Estate or heirs", "Held 20+ yrs", "Distress flag",
           "Landlocked (cluster)", "Flood point", "Bought last 2 yrs", "Listed", "Builder inventory", "Public or HOA"]
    rows = []
    for f, meta in COUNTIES.items():
        for ln, label, _ in LANES:
            c = byc.get("%s|%s" % (f, ln)) or {}
            if not c:
                continue
            rows.append([meta["name"], label, c.get("parcels"), round(c.get("acres", 0)), c.get("addressable"),
                         round(c.get("addressable_acres", 0)), c.get("owners"), round(c.get("value_at_market", 0)),
                         c.get("sales_per_year"), c.get("dollars_per_year"), c.get("flag_out_of_state"), c.get("flag_estate"), c.get("flag_long_tenure"),
                         c.get("flag_distressed"), c.get("landlocked"), c.get("flood_point"), c.get("bought_2y"),
                         c.get("listed"), c.get("owner_builder"),
                         sum(c.get("owner_" + k, 0) for k in ("public", "hoa", "cemetery", "church", "utility"))])
    for ln, label, _ in LANES:
        rr = [r for r in rows if r[1] == label]
        rows.append(["All 11 counties", label] + [sum((r[i] or 0) for r in rr) for i in range(2, len(hdr))])
    sheet("Market size", hdr, rows, widths={"County": 16, "Lane": 30},
          num=tuple(hdr[2:7]) + tuple(hdr[8:9]) + tuple(hdr[10:]),
          money=("Value at recent sale prices", "Vacant sale dollars a year"))
    # 3. county scorecard
    hdr = ["County", "Lot desk rank", "Entitlement rank", "New homes a year (2022-24)", "New home median",
           "Finished lot median", "Production lot buys since 2021", "Production builders", "Small-builder lot buys",
           "Lots that fit a builder", "Mailable lots A+B", "Mailable entitlement tracts A+B", "Homesite-split tracts",
           "Raw land $/ac (15-50 ac)", "Sales behind it", "Rules status"]
    rows = [[r["county"], r["lot_rank"], r["ent_rank"], r["new_homes_per_year"], r["new_home_value_med"],
             r["finished_lot_med"], r["production_lot_buys"], r["production_builders"], r["small_builder_lot_buys"],
             r["lots_fitting_a_builder"], r["lots_ab"], r["ent_ab"], r["splits"], r["tract_per_acre"],
             r["tract_sales"], r["rules_status"]] for r in sorted(cards, key=lambda r: r["ent_rank"])]
    sheet("County scorecard", hdr, rows, widths={"County": 14, "Rules status": 60}, wrap=("Rules status",),
          money=("New home median", "Finished lot median", "Raw land $/ac (15-50 ac)"),
          num=tuple(h for h in hdr if h not in ("County", "Rules status", "New home median", "Finished lot median",
                                                 "Raw land $/ac (15-50 ac)")))
    # 4. funnel
    hdr = ["County", "Lane", "Parcels", "Not a seller (public, builder, no name)", "Built on", "Ruled out (land)",
           "Verify", "Marketable", "Bought in 2 yrs", "Listed", "No mailing address", "Mailable", "Tier A", "Tier B",
           "Tier C", "Lots with no builder fit (R)"]
    rows = []
    for f, meta in COUNTIES.items():
        for ln, label, _ in LANES:
            xs = [x for x in scored if x["fips"] == f and x["lane"] == ln]
            if not xs:
                continue
            ex = collections.Counter((x["mail_exclude"] or "").split(":")[0] for x in xs)
            rows.append([meta["name"], label, len(xs), sum(1 for x in xs if not is_seller(x)),
                         sum(1 for x in xs if x["verdict"] == "BUILT SINCE REAPPRAISAL"),
                         sum(1 for x in xs if x["verdict"] in ("NOT BUILDABLE", "NOT A FIT") and is_seller(x)),
                         sum(1 for x in xs if x["verdict"] == "VERIFY" and is_seller(x)),
                         sum(1 for x in xs if x.get("tier") and is_seller(x)), ex["bought in the last 2 years"],
                         ex["listed on the MLS"], ex["no mailing address"],
                         sum(1 for x in xs if not x["mail_exclude"]),
                         *[sum(1 for x in xs if not x["mail_exclude"] and x["tier"] == t) for t in "ABCR"]])
    sheet("Funnel", hdr, rows, widths={"County": 14, "Lane": 30}, num=tuple(hdr[2:]))
    # 5. end buyers by county
    hdr = ["County", "Buyer type", "Buyer", "Count", "Measure", "Detail"]
    rows = []
    fitc = collections.defaultdict(collections.Counter)
    for x in an.get("lots", []):
        for m in x.get("matches") or []:
            fitc[x.get("county")][m.get("name")] += 1
    boxes = {b["name"]: b for b in an.get("boxes", [])}
    for f, meta in COUNTIES.items():
        c = (buyers.get("county") or {}).get(f) or {}
        for p in c.get("production_builders", [])[:12]:
            rows.append([meta["name"], "Builder buying finished lots", bl.pretty(p["builder"]), p["lots"],
                         "lots since 2021", "lot median %s, home median %s, last %s; %s" % (
                             bl.money(p["lot_med"] if (p.get("lot_med") or 0) >= 10000 else None),
                             bl.money(p.get("home_med")), p.get("last"), ", ".join(p.get("subdivisions") or []))])
        for d, n in c.get("lot_developers", [])[:8]:
            rows.append([meta["name"], "Lot developer (sold finished lots to builders)", bl.pretty(d), n,
                         "lots sold since 2021", "a seller of approved land and a likely buyer or partner for ours"])
        for t in c.get("tract_buyers", [])[:12]:
            ty = _tract_type(t["buyer"])
            if ty == "not a land buyer":
                continue
            rows.append([meta["name"], "Tract buyer: %s" % ty, bl.pretty(t["buyer"]), t["buys"], "5+ ac buys since 2021",
                         "%s acres, %s total, median %s/ac, %s" % (t["acres"], bl.money(t["spent"]),
                                                                    bl.money(t.get("per_acre_med")),
                                                                    "-".join(str(y) for y in t["years"][:1] + t["years"][-1:]))])
        for nm, n in fitc[meta["name"]].most_common(12):
            b = boxes.get(nm) or {}
            rows.append([meta["name"], "Small builder (buy box)", bl.pretty(nm), n, "lots here fit this builder",
                         "%s buys, %s a year, lot median %s, sells for %s, %s" % (
                             b.get("buys"), b.get("per_year"),
                             bl.money(((b.get("classes") or {}).get("lot") or {}).get("price_med")),
                             bl.money(b.get("sells_for")), b.get("reach") or "no phone on file")])
    sheet("End buyers", hdr, rows, widths={"County": 12, "Buyer type": 36, "Buyer": 34, "Measure": 24, "Detail": 90},
          wrap=("Detail",), num=("Count",))
    # 6. builder buy boxes (region)
    hdr = ["Builder", "Type", "Counties", "Buys since 2021", "Per year", "Bought last 12 mo", "Main niche",
           "Lot size (core)", "Lot price p25", "Lot price median", "Lot price p75", "Builds", "Sells for",
           "Bought from individuals", "Lots fitting now", "Tracts fitting", "Reach", "Last buy"]
    rows = []
    for b in sorted(an.get("boxes", []), key=lambda b: -(b.get("matched_lots") or 0)):
        lc = (b.get("classes") or {}).get("lot") or {}
        rows.append([bl.pretty(b["name"]), b.get("type"), ", ".join(b.get("counties") or []), b.get("buys"),
                     b.get("per_year"), b.get("buys_12m"), b.get("main_niche"),
                     ("%.2f-%.2f ac" % (lc["core_lo"], lc["core_hi"])) if lc.get("core_lo") else None,
                     lc.get("price_p25"), lc.get("price_med"), lc.get("price_p75"), b.get("builds"), b.get("sells_for"),
                     b.get("from_person_share"), b.get("matched_lots"), b.get("matched_tracts"), b.get("reach"),
                     b.get("last_buy")])
    sheet("Builder buy boxes", hdr, rows, widths={"Builder": 34, "Counties": 30},
          money=("Lot price p25", "Lot price median", "Lot price p75", "Sells for"), pct=("Bought from individuals",),
          num=("Buys since 2021", "Bought last 12 mo", "Builds", "Lots fitting now", "Tracts fitting"))
    # 7-9. mail lists
    order = {"A": 0, "B": 1, "C": 2}
    lots = sorted([x for x in mail if x["lane"] in ("lot", "homesite") and x["tier"] in ("A", "B")],
                  key=lambda x: (x["fips"] != "47093", COUNTIES[x["fips"]]["name"], order[x["tier"]],
                                 -(x.get("n_fit") or 0), -(x.get("motivation") or 0)))
    tracts = sorted([x for x in mail if "ENTITLEMENT" in x["verdict"]],
                    key=lambda x: (order[x["tier"]], -(x.get("est_spread") or 0)))
    splits = sorted([x for x in mail if x["verdict"] == "HOMESITE SPLIT" and x["tier"] == "B"],
                    key=lambda x: -(x.get("est_spread") or 0))
    money_cols = ("Offer low", "Offer high", "Reference price", "Est approved value", "Est spread")
    for title, xs in (("Lot list (A+B)", lots), ("Entitlement tracts", tracts), ("Homesite splits (B)", splits)):
        sheet(title, CSV_COLS, [csv_row(x) for x in xs],
              widths={"Owner": 28, "Property Street": 26, "Flags": 60, "Price basis": 40, "Builders this fits": 40,
                      "Sewer": 30, "Reason tags": 30},
              money=money_cols, num=("Est lots",))
    # 10. pricing
    hdr = ["County", "Acre band", "Sales since 2021", "Median price", "Median $/ac", "p25 $/ac", "p75 $/ac"]
    rows = []
    pr = tam.get("prices") or {}
    for k in sorted(pr, key=lambda k: (k.split("|")[0] != "region", k)):
        f, b = k.split("|")
        v = pr[k]
        rows.append(["All 11 counties" if f == "region" else COUNTIES[f]["name"], b, v["n"], v["median_price"],
                     v["median_per_acre"], v["p25_per_acre"], v["p75_per_acre"]])
    sheet("Land prices", hdr, rows, money=("Median price", "Median $/ac", "p25 $/ac", "p75 $/ac"),
          num=("Sales since 2021",))
    # 11. rules
    hdr = ["County", "Status for entitle-and-sell", "County zoning", "Who approves", "Rules that set lot yield",
           "Sewer", "Climate 2023-2026", "Adequate facilities tax", "Sources (read 2026-11-09)"]
    rows = [[n, r["status"], r["zoning"], r["body"], r["rules"], r["sewer"], r["climate"], r["aft"], r["sources"]]
            for n, r in COUNTY_RULES.items()]
    rows += [["Statewide", a, "", "", b, "", "", "", c] for a, b, c in STATEWIDE_RULES]
    sheet("County rules", hdr, rows, widths={h: 34 for h in hdr} | {"County": 12, "Rules that set lot yield": 60,
                                                                     "Climate 2023-2026": 60},
          wrap=tuple(hdr))
    marketing_plan(wb, sheet, cards, mail, cnt, buyers, an)
    econ_sheet(wb, mail, an)
    # 14. not marketable
    hdr = ["Lane", "Verdict", "Reason", "Parcels"]
    nm = collections.Counter()
    for x in scored:
        if x["mail_exclude"]:
            why = (x.get("hard") or [x["mail_exclude"]])[0] if x["verdict"] in ("NOT BUILDABLE", "NOT A FIT") else x["mail_exclude"]
            why = re.sub(r"\d[\d,.]*", "N", why)
            nm[(LANE_LABEL[x["lane"]], x["verdict"], why)] += 1
    sheet("Not marketable", hdr, [list(k) + [v] for k, v in nm.most_common()], widths={"Lane": 30, "Verdict": 26,
                                                                                       "Reason": 70}, num=("Parcels",))
    sheet("Sources and caveats", ["Topic", "Note"], caveats_rows(), widths={"Topic": 30, "Note": 150}, wrap=("Note",))
    path = OUT / ("Knoxville_Region_Land_Market_%s.xlsx" % stamp)
    saved = bl._save(wb, path)
    for name, xs in (("mail_lots", [x for x in mail if x["lane"] in ("lot", "homesite")]),
                     ("mail_entitlement_tracts", tracts),
                     ("mail_homesite_splits", [x for x in mail if x["verdict"] == "HOMESITE SPLIT"])):
        with open(OUT / ("%s_%s.csv" % (name, stamp)), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLS)
            for x in xs:
                w.writerow(csv_row(x))
    log("export: %s (%d lots A+B, %d entitlement tracts, %d splits B)" % (saved, len(lots), len(tracts), len(splits)))
    return 0


def marketing_plan(wb, sheet, cards, mail, cnt, buyers, an) -> None:
    """The plan as steps (buyers first), a per-county volume table, a 90-day calendar and draft copy."""
    lot_cty = [r["county"] for r in sorted(cards, key=lambda r: r["lot_rank"])[:4]]
    ent_cty = [r["county"] for r in sorted(cards, key=lambda r: r["ent_rank"]) if r["risk"] == 1][:3]
    ent_watch = [r["county"] for r in sorted(cards, key=lambda r: r["ent_rank"]) if r["risk"] == 3]
    split_cty = [n for n in ("Grainger", "Union", "Jefferson", "Campbell", "Morgan")]

    def n_lots(ctys, tier, fit=True):
        return sum(1 for x in mail if x["lane"] in ("lot", "homesite") and x["tier"] == tier
                   and COUNTIES[x["fips"]]["name"] in ctys and (x.get("n_fit") or not fit))

    def n_ent(ctys, tier):
        return sum(1 for x in mail if "ENTITLEMENT" in x["verdict"] and x["tier"] == tier
                   and COUNTIES[x["fips"]]["name"] in ctys)

    la, lb = n_lots(lot_cty, "A"), n_lots(lot_cty, "B")
    ea, eb = n_ent(ent_cty, "A"), n_ent(ent_cty, "B")
    sb = sum(1 for x in mail if x["verdict"] == "HOMESITE SPLIT" and x["tier"] == "B"
             and COUNTIES[x["fips"]]["name"] in split_cty[:2])
    hdr = ["Step", "Lane", "Counties", "Who", "What we say", "Channel and cadence", "Records", "Touches",
           "Cost per touch", "Cost (first cycle)", "Target by day 90"]
    rows = [
        ["1. Buyers before sellers", "All", "All 11",
         "The small builders with the most fitting lots (Builder buy boxes), and per county the production builders, "
         "lot developers and tract buyers (End buyers).",
         "We find land that fits what you build. Tell us your price, lot size, areas and how many a year, and we bring "
         "you lots and approved tracts before they hit the market.",
         "Phone first, then a one-page buy box by email; lunch with the top 10. Re-confirm every quarter.",
         min(60, buyers.get("box_count") or 60), 2, 0, 0,
         "40 confirmed buy boxes, at least 3 per priority county, and 5 production builders or developers who say "
         "what an approved tract is worth to them per lot."],
        ["2. Lot desk, tier A", "Builder lots and homesites", ", ".join(lot_cty),
         "Owners of tier A lots that fit 1+ confirmed builder (strong reason to sell, buildable 65+).",
         "I'm looking to buy lots on your street. We pay cash, as is, close in 30 days and cover closing. "
         "Here is a number; call or text if it works.",
         "Letter with a price (day 1), text (day 3), call (day 5), postcard (day 21), second letter (day 45), call "
         "(day 50).", la, 6, 0.65, round(la * 6 * 0.65),
         "Every tier A owner touched twice; first 10 lots under contract and assigned."],
        ["3. Lot desk, tier B", "Builder lots and homesites", ", ".join(lot_cty),
         "Owners of tier B lots that fit a builder.",
         "Same letter, softer: I'm looking to buy lots like yours.",
         "Letter (day 1), text (day 7), letter (day 45), text (day 60).", lb, 4, 0.65, round(lb * 4 * 0.65),
         "One full cycle; response rate measured by county."],
        ["4. Entitlement tracts, tier A", "Entitlement tracts 15-150 ac", ", ".join(ent_cty),
         "Owners of tier A tracts: sewer or public water, road frontage, builders active within 3 miles, a reason "
         "to sell.",
         "We buy land at today's price and do the zoning and plat work ourselves, at our cost. You pick the closing "
         "date; a longer close can pay you more.",
         "Personal letter, no price (monthly x6); call 1 week after each letter; door knock the top 50; follow up "
         "every month for a year. A tract seller decides slowly.", ea, 8, 1.25, round(ea * 8 * 1.25),
         "50 conversations, 5 signed contracts or options, each pre-sold to a builder before engineering is spent."],
        ["5. Entitlement tracts, tier B", "Entitlement tracts 15-150 ac", ", ".join(ent_cty),
         "Owners of tier B tracts.", "Same letter.", "Letter every 6 weeks, call after the second.", eb, 4, 1.25,
         round(eb * 4 * 1.25), "One cycle; tracts that answer move to tier A follow-up."],
        ["6. Pre-sell every tract", "Entitlement", "Where a tract is under contract",
         "The 3-5 production builders and developers active within 3 miles (End buyers; production builders column).",
         "Here is a tract we control: acres, sewer, frontage, likely lot count. What would you pay per approved lot, "
         "and how many a year would you take?",
         "One-page site sheet by email, then a call; ask for a letter of intent before spending on a concept plan.",
         None, None, None, None, "A builder LOI on every tract before the first engineering dollar."],
        ["7. Homesite splits (test)", "Small tracts", ", ".join(split_cty[:2]),
         "Owners of tier B split tracts in the two counties with no county zoning (no rezoning step; in Grainger a "
         "division into 5+ acre tracts is not even a subdivision).",
         "We buy land as is and pay cash; no repairs, no realtor, you choose the date.",
         "Letter (monthly x4), call after letter 2.", sb, 4, 0.65, round(sb * 4 * 0.65),
         "Decide by day 90 whether splits are worth a lane; exit is retail homesite buyers, not builders."],
        ["8. Inbound", "All", "All 11", "Land owners searching to sell.",
         "Sell your land in East Tennessee: cash, as is, we handle the paperwork.",
         "One landing page, Google search ads on 'sell land Knoxville / Blount / Sevier' at about $1,500 a month, "
         "and the Google Business profile.", None, None, None, 4500, "Cost per inbound lead measured against mail."],
        ["9. Data operations", "All", "All 11",
         "The mail lists in this workbook (DataSift id on every row).",
         "Not applicable.",
         "Pull into the chosen DataSift account by id (ask Ty which account first), skip trace (unlimited plan), "
         "Trestle-score mobiles, DNC scrub, one list per lane and tag per tier, presets per lane and tier, and the "
         "Sold cleanup sequence.", None, None, None, None, "Lists live, numbers scored, presets showing counts."],
        ["10. Measure and reset", "All", "All 11", "Every touch and answer.",
         "Not applicable.",
         "Day 30, 60 and 90: answer rate, contract rate and cost per contract by county and lane. Replace the "
         "placeholder rates on the Economics sheet with the measured ones and move budget to what is converting.",
         None, None, None, None, "Economics sheet runs on measured rates by day 90."],
    ]
    sheet("Marketing plan", hdr, rows, widths={"Step": 24, "Lane": 22, "Counties": 24, "Who": 44,
                                               "What we say": 52, "Channel and cadence": 52, "Target by day 90": 44},
          wrap=("Who", "What we say", "Channel and cadence", "Target by day 90", "Counties"),
          money=("Cost per touch", "Cost (first cycle)"), num=("Records", "Touches"))
    hdr = ["County", "Lot desk rank", "Entitlement rank", "Lots A (fit a builder)", "Lots B (fit a builder)",
           "Lots with no builder fit (R)", "Entitlement tracts A", "Entitlement tracts B", "Homesite splits B",
           "First cycle cost (plan cadence)", "Recommendation"]
    rows = []
    for r in sorted(cards, key=lambda r: r["ent_rank"]):
        c = r["county"]
        a1, b1 = n_lots([c], "A"), n_lots([c], "B")
        nf = n_lots([c], "R", fit=False)
        e1, e2 = n_ent([c], "A"), n_ent([c], "B")
        s2 = sum(1 for x in mail if x["verdict"] == "HOMESITE SPLIT" and x["tier"] == "B" and COUNTIES[x["fips"]]["name"] == c)
        cost = a1 * 6 * 0.65 + b1 * 4 * 0.65 + e1 * 8 * 1.25 + e2 * 4 * 1.25
        rec = recommendation(r, c in lot_cty, c in ent_cty, c in ent_watch, c in split_cty[:2])
        rows.append([c, r["lot_rank"], r["ent_rank"], a1, b1, nf, e1, e2, s2, round(cost), rec])
    sheet("County volumes", hdr, rows, widths={"County": 14, "Recommendation": 70}, wrap=("Recommendation",),
          money=("First cycle cost (plan cadence)",), num=tuple(hdr[3:9]))
    cal = [
        ("Week 1", "Call the top 25 builders by fitting lots; confirm boxes. Ask Ty which DataSift account takes the "
                   "lists.", "Ty or acquisitions lead", "25 calls made, 15 boxes written"),
        ("Week 1", "Meet planning staff in the priority entitlement counties: rezoning calendar, sewer contacts, road "
                   "standards, the Blount and Loudon status.", "Ty", "Notes per county on the County rules sheet"),
        ("Week 2", "Pull the tier A and B lists by DataSift id; skip trace; Trestle; DNC scrub; presets and tags.",
         "Ops", "Lists live with scored phones"),
        ("Week 2", "Retain a civil engineer and a land-use attorney for concept plans and rezonings (quote per "
                   "tract).", "Ty", "Two quotes in hand"),
        ("Week 3", "First drop: tier A lots in the lot-desk counties; texts and calls follow.", "Ops", "Drop mailed"),
        ("Week 3", "First tract letters: tier A in the entitlement counties.", "Ops", "Letters mailed"),
        ("Weeks 4-6", "Work responses: price lots from the builder's box minus the fee; send each signed lot to "
                      "its builder within 48 hours.", "Acquisitions", "First assignments"),
        ("Weeks 4-8", "Tract calls and door knocks; option or contract with a long close, contingent on approval; "
                      "pre-sell each to builders before engineering.", "Ty", "First tract LOIs"),
        ("Week 7", "Second lot letter and tract letter 2; tier B lots begin.", "Ops", "Drop mailed"),
        ("Weeks 8-12", "Concept plan or rezoning filed on the first 1-2 pre-sold tracts; splits test letters in "
                       "Grainger and Union.", "Ty and engineer", "Filings made"),
        ("Day 90", "Measured rates replace placeholders on the Economics sheet; reallocate the budget by county "
                   "and lane.", "Ty", "Plan v2"),
    ]
    sheet("90-day calendar", ["When", "Task", "Owner", "Done when"], cal,
          widths={"When": 12, "Task": 100, "Owner": 22, "Done when": 40}, wrap=("Task", "Done when"))
    copy = [
        ("Lot letter (tier A)", "Hi {first name}, I'm looking to buy lots on {street}, "
         "and yours at {address} is one I would like to buy. I can pay {offer} cash, as is, and cover the closing "
         "costs, and we can close in about 30 days. If that works, or if you would want a different number, call or "
         "text me at {phone}. {sender}"),
        ("Tract letter (tier A)", "Hi {first name}, I buy land around {county} County and I am interested in your "
         "property on {street}. I pay a fair price for land as it is today, and I take care of the zoning, survey "
         "and plat work myself, at my cost. You choose the closing date, and a longer closing can mean a better "
         "price. I would like to talk about what you would want for it. {phone}. {sender}"),
        ("Builder intake opener", "I run a land desk that finds lots and approved tracts for builders in {county}. "
         "Before I go looking, I want to know exactly what you buy: what you pay, lot size, which areas, and how many "
         "a year. If I bring you one that fits, how fast can you decide?"),
        ("Builder pre-sell (tract)", "I control {acres} acres on {road} in {county}: {sewer}, {frontage} ft of "
         "frontage, about {lots} homes at the density the county has approved nearby. What would you pay per approved "
         "lot, and how many a year would you take? I want your letter of intent before I spend on the concept plan."),
    ]
    sheet("Draft copy", ["Piece", "Draft (fill the braces; no prices on tract letters)"], copy,
          widths={"Piece": 26, "Draft (fill the braces; no prices on tract letters)": 150},
          wrap=("Draft (fill the braces; no prices on tract letters)",))


def recommendation(r: dict, lot_p1: bool, ent_p1: bool, watch: bool, split_test: bool) -> str:
    """One line per county, from its ranks and its rules."""
    if watch:
        return ("Highest builder demand after Knox, but 11+ lot approvals are paused or at risk: contract tracts that "
                "plat at 10 lots or fewer, or that already hold preliminary approval (exempt), and keep tier A owners "
                "warm until the rules clear. Lots as builders confirm boxes.")
    if lot_p1 and ent_p1:
        return "Priority 1 for both lanes."
    if ent_p1:
        return "Priority 1 for entitlement; lots as builders confirm boxes here."
    if lot_p1:
        return "Priority 1 for the lot desk; entitlement in phase 2 once the priority counties produce approvals."
    if split_test:
        return ("No county zoning, so no rezoning step: test homesite splits here; lots only where a builder "
                "confirms a box.")
    return "Phase 2: mail after the priority counties show measured rates."


def econ_sheet(wb, mail, an) -> list:
    """Live formulas: change a blue input and the outputs move. The answer, contract and approval rates are
    placeholders to replace with the first 90 days of results, not measurements."""
    from openpyxl.styles import Alignment, Font
    ws = wb.create_sheet("Economics")
    blue, bold = Font(color="316AFF"), Font(bold=True, color="0A1130")
    fit_ab = sum(1 for x in mail if x["lane"] in ("lot", "homesite") and x["tier"] in ("A", "B") and x.get("n_fit"))
    demand = sum(b.get("buys_12m") or 0 for b in an.get("boxes", []))
    ent = [x for x in mail if "ENTITLEMENT" in x["verdict"] and x["tier"] in ("A", "B")]
    spreads = sorted(x["est_spread"] for x in ent if (x.get("est_spread") or 0) > 0)
    costs = sorted(x["entitle_cost"] for x in ent if x.get("entitle_cost"))
    med_spread = round(statistics.median(spreads)) if spreads else 0
    p75_spread = round(statistics.quantiles(spreads, n=4)[2]) if len(spreads) >= 4 else med_spread
    med_cost = round(statistics.median(costs)) if costs else 30000
    rows = [
        ("LOT DESK", None, None, None),
        ("Measured: lots A+B that fit a builder", fit_ab, "From the Lot list sheet.", "#,##0"),
        ("Measured: lots the box builders bought in the last 12 months", demand, "From the Builder buy boxes sheet.", "#,##0"),
        ("Lots marketed a month", min(fit_ab, 600), "Input. First touches a month.", "#,##0"),
        ("Touches per lot", 5, "Input. Mail, text and call over about 60 days.", "0"),
        ("Cost per touch", 0.65, "Input. Blended mail and text.", '"$"#,##0.00'),
        ("Owners who answer", 0.03, "Placeholder. Measure it.", "0.0%"),
        ("Answers that sign", 0.15, "Placeholder.", "0%"),
        ("Signed lots a builder takes", 0.70, "Placeholder; the rest fail sewer, survey or title.", "0%"),
        ("Fee per lot", bl.FEE, "Input. Knox quick lot flips cleared a median $15,000 (middle half $9-26K).", '"$"#,##0'),
        ("Lots closed a month", "={l_marketed}*{l_answer}*{l_sign}*{l_take}", None, "0.0"),
        ("Fee income a month", "={l_closed}*{l_fee}", None, '"$"#,##0'),
        ("Marketing a month", "={l_marketed}*{l_touches}*{l_cost}", None, '"$"#,##0'),
        ("Net a year before people", "=({l_income}-{l_mkt})*12", None, '"$"#,##0'),
        ("Share of the builders' yearly buying we would supply", '=IF({l_demand}>0,{l_closed}*12/{l_demand},"")',
         "Above about 25% the plan outruns the demand.", "0%"),
        ("ENTITLEMENT", None, None, None),
        ("Measured: entitlement tracts A+B (all counties)", len(ent), "From the Entitlement tracts sheet.", "#,##0"),
        ("Estimate: median spread per tract with a positive spread (approved value less farm price and cost)",
         med_spread,
         "Built from measured lot prices and labeled ratios; treat as an upper bound until builders quote.",
         '"$"#,##0'),
        ("Tracts marketed a month", min(100, max(1, round(len(ent) / 12))),
         "Input. First touches a month; one person can work about 100 tract owners a month.", "#,##0"),
        ("Touches per tract", 8, "Input.", "0"),
        ("Cost per touch", 1.25, "Input. Personal letters and calls.", '"$"#,##0.00'),
        ("Owners who answer", 0.04, "Placeholder.", "0.0%"),
        ("Answers that sign an option or contract", 0.08, "Placeholder.", "0%"),
        ("Signed tracts a builder pre-commits to (LOI)", 0.60,
         "Placeholder. No LOI, no engineering: the option is dropped and only the earnest money is lost.", "0%"),
        ("Pre-sold tracts that get approved", 0.80, "Placeholder.", "0%"),
        ("Share of the estimated spread we actually keep", 0.50, "Input. Haircut for builder negotiation.", "0%"),
        ("Entitlement cost per tract", med_cost, "Estimate: engineering, concept plan, legal.", '"$"#,##0'),
        ("Earnest money lost per dropped tract", 5000, "Input.", '"$"#,##0'),
        ("Months from contract to approval", 12, "Input. Shorter from 2027 under Public Chapter 1044.", "0"),
        ("Estimate: 75th percentile spread", p75_spread, "The tracts worth chasing first.", '"$"#,##0'),
        ("Tracts signed a year", "={t_marketed}*12*{t_answer}*{t_sign}", None, "0.0"),
        ("Tracts pre-sold a year", "={t_signed}*{t_loi}", None, "0.0"),
        ("Tracts approved and sold a year", "={t_presold}*{t_approved}", None, "0.0"),
        ("Gross a year", "={t_sold}*{t_spread}*{t_keep}", None, '"$"#,##0'),
        ("Marketing a year", "={t_marketed}*{t_touches}*{t_cost}*12", None, '"$"#,##0'),
        ("Entitlement spend a year (pre-sold tracts only)", "={t_presold}*{t_entcost}", None, '"$"#,##0'),
        ("Earnest money lost a year", "=({t_signed}-{t_presold})*{t_earnest}", None, '"$"#,##0'),
        ("Net a year before people", "={t_gross}-{t_mkt}-{t_spend}-{t_lost}", None, '"$"#,##0'),
        ("Net a year at the 75th percentile spread", "={t_sold}*{t_p75}*{t_keep}-{t_mkt}-{t_spend}-{t_lost}", None,
         '"$"#,##0'),
        ("Capital at risk (entitlement in flight)", "={t_presold}*{t_entcost}*{t_months}/12", None, '"$"#,##0'),
    ]
    rows = econ_resolve(rows)
    ws.append(["Line", "Value", "Note"])
    for i, (label, val, note, fmt) in enumerate(rows, 2):
        ws.append([label, val, note])
        if val is None:
            ws.cell(i, 1).font = bold
        elif fmt:
            ws.cell(i, 2).number_format = fmt
        if note and (note.startswith("Input") or note.startswith("Placeholder")):
            ws.cell(i, 2).font = blue
    for c in ws[1]:
        c.font = bold
    ws.column_dimensions["A"].width = 70
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 80
    for row in ws.iter_rows(min_row=2):
        row[2].alignment = Alignment(wrap_text=True, vertical="top")
    return rows


# which row each formula input lives on, found by the start of its label inside its own block
ECON_KEYS = {
    "LOT DESK": {"l_fit": "Measured: lots A+B", "l_demand": "Measured: lots the box", "l_marketed": "Lots marketed",
                 "l_touches": "Touches per lot", "l_cost": "Cost per touch", "l_answer": "Owners who answer",
                 "l_sign": "Answers that sign", "l_take": "Signed lots a builder", "l_fee": "Fee per lot",
                 "l_closed": "Lots closed", "l_income": "Fee income", "l_mkt": "Marketing a month"},
    "ENTITLEMENT": {"t_spread": "Estimate: median spread", "t_marketed": "Tracts marketed", "t_touches": "Touches per",
                    "t_cost": "Cost per touch", "t_answer": "Owners who answer", "t_sign": "Answers that sign",
                    "t_loi": "Signed tracts a builder", "t_approved": "Pre-sold tracts that get",
                    "t_keep": "Share of the estimated", "t_entcost": "Entitlement cost per",
                    "t_earnest": "Earnest money lost per", "t_months": "Months from", "t_p75": "Estimate: 75th",
                    "t_signed": "Tracts signed", "t_presold": "Tracts pre-sold", "t_sold": "Tracts approved",
                    "t_gross": "Gross a year", "t_mkt": "Marketing a year", "t_spend": "Entitlement spend",
                    "t_lost": "Earnest money lost a"},
}


def econ_resolve(rows: list) -> list:
    """Turn {name} placeholders into cell references. The first version hard-coded B-numbers and every formula
    pointed one row too high (lots closed a month multiplied the builders' yearly demand), so references are now
    looked up by label and the test recalculates the sheet."""
    ref, block = {}, None
    for i, (label, val, *_r) in enumerate(rows, 2):
        if val is None:
            block = label
            continue
        for k, start in ECON_KEYS.get(block, {}).items():
            if label.startswith(start) and k not in ref:
                ref[k] = "B%d" % i
    out = []
    for label, val, note, fmt in rows:
        if isinstance(val, str) and val.startswith("="):
            val = val.format(**ref)
        out.append((label, val, note, fmt))
    return out


def caveats_rows() -> list:
    return [
        ("Property universe", "DataSift property index through the opensearch MCP (read-only), pulled by "
         "builder_land.py into output/builder_land/knox_blount and knox_ring (2026-11-06 and 2026-11-09). Condos, "
         "townhouses, warehouses and commercial multifamily are left out."),
        ("Lanes", "Builder lots: vacant 0.06-1 ac. Homesites: vacant 1-5 ac. Small tracts 5-15 ac, entitlement tracts "
         "15-150 ac and large tracts 150+ ac include farms, timber and parcels with a house (a farmhouse on 40 acres is "
         "a tract, and the house is carved out)."),
        ("Parcel truth (10 counties)", "TN Comptroller OLG land use layer (tnmap.tn.gov, public): polygon, water and "
         "sewer service, building count, improvement value, flood share. Joined on the state GISLINK (county code plus "
         "the cluster parcel number; dashed numbers are rebuilt), 97-99.6% joined; the rest by point in polygon."),
        ("Knox", "Not in the state layer. Footprint is a circle of the cluster acreage at its point. Access from census "
         "TIGER roads plus the cluster lot type and frontage; flood from statewide DFIRM polygons; sewer from the lot "
         "sizes of houses within 1,000 ft; growth area, place type and county zoning from the public "
         "Knoxville-Knox County-KUB GIS layers."),
        ("Built on", "A parcel the cluster calls vacant is built on if the state roll codes it as a house or counts a "
         "building, if a FEMA/ORNL USA Structures footprint (imagery 2014-2025) sits on it, or if it sold for $150K+ "
         "at 3x its appraisal since 2021."),
        ("Lot verdicts", "The Union OH buildability rules (vacant_land.buildability): access 40, utilities 20, flood "
         "15, shape 10, slope 10, zoning 5; hard fails for landlocked, strips under 35 ft, 50%+ flood, 25%+ floodway, "
         "50%+ steeper than 15%, non-residential zoning."),
        ("Tract verdicts", "Entitlement fit: utilities 25 (public sewer, public water, neither), builder demand 25 "
         "(houses built since 2021 within 3 miles), access 20, developable acres 15 (net of flood, slope and 2 acres "
         "for a house), growth 10, slope 5. Hard fails: no road within 150 ft, 60%+ flood, under 4 developable acres, "
         "industrial zoning, state roll public, utility or right of way."),
        ("Slope", "USGS 3DEP 1 m lidar, grid samples per parcel; run on the parcels that can reach a mail list "
         "(entitlement tracts and builder-fit lots). Everything else says 'slope not checked'."),
        ("Prices", "Medians of priced, non-family sales of vacant land since 2021 by county and acre band; a county "
         "band with under 8 sales uses the 11-county band. A small parcel resold at $150K+ and 3x its appraisal is a "
         "house, not a land price."),
        ("Approved value (estimate)", "Sewer subdivision: 2.0 homes per developable acre x 42% of the county's "
         "finished lot median (Knox measured about $30K paper against $69-75K finished). Septic lots: 0.7 per acre x "
         "55% of a 1-2 acre homesite sale. Less $15,000 + $2,500 a lot of entitlement cost. Upper bound until a "
         "builder quotes."),
        ("Offers", "Lots and homesites: 50-65% of what fitting builders paid nearby, else of recent sales. Tracts: "
         "85-100% of farm price (recent sales x acres); the approval is the margin."),
        ("End buyers", "builder_land.py on the combined 11-county market (deed chains, buy boxes, fits) plus deed "
         "reads per county: production lot purchases since 2021, the developers who sold them, and entity buyers of "
         "5+ acres. Knox's development pipeline is the public KGIS DevelopmentProjects layer."),
        ("County rules", "Read from county planning pages, regulations and local news on 2026-11-09. Blount and the Knox "
         "road law were checked against the source article; the Loudon road-pause article is offline. Verify the rule "
         "a contract depends on with the county before signing."),
        ("Not done", "Nothing was pulled into a DataSift account. No owner phones were read. Response, contract and "
         "approval rates on the Economics sheet are placeholders."),
    ]


# ----------------------------------------------------------------- main

PHASES = {"gis": phase_gis, "tam": phase_tam, "screen": phase_screen, "topo": phase_topo, "buyers": phase_buyers,
          "score": phase_score, "export": phase_export}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", default="all", choices=list(PHASES) + ["all"])
    ap.add_argument("--counties", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    help="comma-separated county names (default: all 11)")
    ap.add_argument("--refresh", action="store_true", help="re-download cached public layers")
    ap.add_argument("--skip-builder-land", action="store_true", help="buyers: reuse the region's analysis.json")
    args = ap.parse_args(argv)
    order = list(PHASES) if args.phase == "all" else [args.phase]
    for ph in order:
        t = time.time()
        rc = PHASES[ph](args)
        log("phase %s: rc %s, %.0fs" % (ph, rc, time.time() - t))
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
