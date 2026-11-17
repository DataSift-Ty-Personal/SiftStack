"""Buildable vacant lots, 0.20 to 2.00 acres, in Union County OH (39159).

Finds every vacant parcel in the acreage window, screens out the ones that
cannot carry a house, ranks the owners who are left by motivation, and writes
the marketing plan. Two sources, each used for what it is right about:

  * Union County GIS (free, no auth) is the truth for the PARCEL: land-use
    class, acreage, geometry, road centerlines and right-of-way, the 100-year
    floodplain and floodway, zoning, recorded plats, permits and sales.
  * SiftMap is the truth for the OWNER: mailing address, tax status, active
    distressors, MLS status, and the dataflik id a DataSift pull needs.

    python src/vacant_land.py --phase probe          # LIVE API contract test, exit 3 on drift
    python src/vacant_land.py --phase county         # county layers (cached; --refresh to re-pull)
    python src/vacant_land.py --phase sweep          # every SiftMap land row in the county
    python src/vacant_land.py --phase topo           # USGS 3DEP slope + low-spot per lot
    python src/vacant_land.py --phase screen         # buildability verdict per parcel
    python src/vacant_land.py --phase hydrate        # SiftMap detail for the survivors
    python src/vacant_land.py --phase score          # motivation, segments, tiers
    python src/vacant_land.py --phase export         # workbook + mail list + marketing plan
    python src/vacant_land.py --phase all            # county..export
    python src/vacant_land.py --phase pull           # DataSift pull plan by tier (dry)
    python src/vacant_land.py --phase pull --commit --key-env TARGET_REISIFT_API_KEY

Contracts verified live 2026-11-05, every one of which fails silently:
  * `type_land: true` is the land filter (Union 40,017 rows -> 4,379).
    type_land_residential, type_vacant_land, type_lot, type_vacant,
    type_commercial, type_farm and type_agricultural all return the unfiltered
    40,017. There is still no lot-size filter, so the window is applied here.
  * type_land is RESIDENTIAL vacant land only: SiftMap files small ag-coded
    lots as "Rural/Agricultural-Vacant Land" and type_land never returns them.
    hydrate reaches those by searching with the parcel's own bounds as the
    map viewport, and the detail apn confirms the match.
  * a search row's `distressors` is {key: {"is_active": bool}} with EVERY key
    present on every row, so counting keys reports all 27 on all 4,379 rows.
  * the AVM means nothing on land: a 0.022 acre Laguna Cir sliver reads
    $1,249,000. Prices come from county land values and county lot sales.
  * the county class lags construction. Parcels still coded 500 (vacant) sold
    for $534,900 to $693,600 against land values of $35,350 to $126,480: those
    are new houses. Caught by sale/land ratio and the county dwelling layer.
  * USDA rates nearly every Union soil "Very limited" for septic absorption
    fields, so soil cannot separate one lot from another; sewer is the gate.
    No sewer layer is published, so the proxy is the size of nearby house lots
    (an engineered septic system does not fit on a 0.3 acre lot).
  * the detail `apn` digits equal the county CAMA number, so the SiftMap join
    is checked rather than trusted.
  * the county dwelling layer has a row for nearly every parcel; `age` is the
    year built and '0' means no house. Membership alone marks 1,035 of 1,046
    vacant lots in the window as built on.
  * USGS 3DEP elevation is free and keyless. computeStatisticsHistograms
    costs 7-8 s a call whatever the area; getSamples reads 1,000 points in
    ~11 s, so topo samples a grid and batches about ten lots per request.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ACRES_MIN = 0.20
ACRES_MAX = 2.00

COUNTIES = {
    "39159": {
        "fips": "39159", "name": "Union", "state": "OH", "label": "Union OH",
        "slug": "union_oh",
        "gis": "https://www7.co.union.oh.us/unioncountyohio/rest/services/",
        "layers": {
            # name: (path, outFields, geometry, page)
            "parcels": ("parcel/MapServer/0",
                        "camano,GISNO,Owner,OwnerCurrent,Acreage,ParcelClass,SUBD_NUMB,"
                        "Address,houseNumber,StreetName,city,ZipCode,township,schoolDistrict,"
                        "TIF,NeighborhoodCode,SaleMostRecent,PARCELTYPE,PropertyCard",
                        True, 1000),
            "roads": ("Roads/MapServer/27", "STR_FULL,STR_NAME,JURISDIC,FUNCTION_,L_TWP",
                      True, 1000),
            "row": ("Roads/MapServer/0", "OBJECTID", True, 1000),
            "flood": ("FloodPlain100/MapServer/0", "OBJECTID", True, 1000),
            "floodway": ("FloodWay/MapServer/0", "FLD_ZONE,FLOODWAY", True, 1000),
            "zoning": ("Zoning/MapServer/6", "ZoningName,zoning,definitions", True, 1000),
            "lots": ("Lots/MapServer/0", "CAMANo,GISNO,SUBD_NUMB,lotNumber,SUBDIVISION_NAME",
                     False, 1000),
            "subdivisions": ("Subdivision/MapServer/0", "Number,Name,Year,PlatBook,Acreage",
                             False, 1000),
            "sales": ("SaleInfo/MapServer/13",
                      "CAMANO,ownerName,saleDate,typeOfSale,salePrice,parcelClass,buildValue,"
                      "landValue,location,isValidSale,NumberofParcelsInSale",
                      False, 2000),
            "dwellings": ("Dwelling/MapServer/0", "CAMANO,GISNO,age", False, 1000),
            "permits": ("BuildingPermit/MapServer/0", "CAMANo,mYear", False, 2000),
        },
    },
}

# Ohio DTE land-use classes this workflow treats as vacant. 500/501 are
# residential vacant land; 100/110 are agricultural vacant land, kept because a
# small ag-coded parcel on a road can be a house lot. The meaning of each code
# is re-measured on every run against SiftMap's own label (QA crosstab) rather
# than trusted from a table.
RESIDENTIAL_VACANT = {"500", "501"}
AG_VACANT = {"100", "110"}
VACANT_CLASSES = RESIDENTIAL_VACANT | AG_VACANT
IMPROVED_RES = {"510", "511", "520", "530"}   # house lots, for the sewer proxy

# Screening thresholds. Defaults, not law: township zoning sets real frontage
# and lot-size minimums, and the county health district decides septic.
ROAD_REACH_FT = 80      # 60 ft right-of-way puts the centerline ~30 ft off the lot line
LANDLOCKED_FT = 150     # no centerline this close and no right-of-way line = no access
ROW_TOUCH_FT = 8
FRONTAGE_OK_FT = 60
FRONTAGE_MIN_FT = 40
WIDTH_FAIL_FT = 35      # narrower than this cannot hold a house plus side yards
FLOOD_FAIL = 0.50
FLOOD_FLAG = 0.10
FLOODWAY_FAIL = 0.25
SEWER_RADIUS_FT = 1000
SEWER_MIN_NEIGHBORS = 5
SEWER_SMALL_LOT_AC = 0.50   # houses on lots this small nearby = public sewer
SEPTIC_AREA_AC = 1.00       # houses this large nearby = on-site septic area
SEPTIC_MIN_AC = 1.00        # an engineered system plus replacement area, roughly
BUILT_SALE_RATIO = 3.0      # sold for 3x the land value = a house went up
BUILT_SALE_FLOOR = 150000
COMP_MONTHS = 36
RECENT_BUY_YEARS = 2
# Topography off USGS 3DEP (1 m lidar, free, no key). Union is a flat till
# plain, so the lot that hurts is usually not the steep one but the one that
# sits below its surroundings: that is where water goes.
DEP_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"
STEEP_GRADE = 0.15          # past a 15% grade a house pad needs real grading
LOT_POINTS = 60             # elevation samples on the lot
RING_POINTS = 40            # and on the ring around it
MIN_SPACING_FT = 15
SAMPLE_BATCH = 1000         # points per getSamples request
STEEP_FAIL = 0.50           # over half the lot that steep = not a house lot
STEEP_FLAG = 0.20
RELIEF_FLAG_FT = 20
RING_FT = 150               # the land around a lot it is compared against
LOW_FLAG_FT = 2.0           # lot median this far under the ring median = sits low
LOW_BAD_FT = 4.0

MARKETABLE = ("BUILDABLE", "LIKELY BUILDABLE", "VERIFY")
PAGE = 250
SPLIT_AT = 9000             # SiftMap reorders past a 10,000 row offset

OUT_ROOT = os.path.join(os.environ.get("SIFTSTACK_OUTPUT_DIR", "output"), "vacant_land")


# ----------------------------------------------------------------- plumbing

def out_dir(c: dict) -> str:
    return os.path.join(OUT_ROOT, c["slug"])


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _load(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_env() -> None:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def today() -> dt.date:
    return dt.date.today()


def digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def zip5(s) -> str:
    """Union's ZipCode field holds 'MARYSVILLE OH 43040', not a zip."""
    m = re.findall(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)", str(s or ""))
    return m[-1] if m else ""


# ------------------------------------------------------------------ ArcGIS

def arcgis_query(url: str, *, where: str = "1=1", fields: str = "*",
                 geometry: bool = False, page: int = 1000) -> list[dict]:
    """Every feature of a layer, paged by resultOffset. Geometry comes back in
    lon/lat (outSR 4326) so it lines up with SiftMap's points."""
    import requests
    out = []
    offset = 0
    while True:
        params = {"where": where, "outFields": fields, "f": "json",
                  "returnGeometry": "true" if geometry else "false",
                  "resultOffset": offset, "resultRecordCount": page}
        if geometry:
            params.update(outSR=4326, geometryPrecision=7)
        for attempt in range(4):
            try:
                r = requests.get(url + "/query", params=params, timeout=180)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise
                print("    %s offset %d: retry after %s" % (url.rsplit("/", 3)[-3], offset, str(e)[:80]))
                time.sleep(4 * (attempt + 1))
        if "error" in data:
            raise RuntimeError("%s: %s" % (url, data["error"]))
        feats = data.get("features") or []
        out.extend(feats)
        if len(feats) < page and not data.get("exceededTransferLimit"):
            break
        if not feats:
            break
        offset += len(feats)
        time.sleep(0.2)
    return out


def esri_to_shape(g: dict | None):
    """ArcGIS JSON geometry -> shapely, in whatever units the coordinates are."""
    from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
    from shapely.geometry import LinearRing
    if not g:
        return None
    if "x" in g and "y" in g:
        return Point(g["x"], g["y"])
    if "paths" in g:
        paths = [p for p in g["paths"] if len(p) >= 2]
        if not paths:
            return None
        return LineString(paths[0]) if len(paths) == 1 else MultiLineString(paths)
    if "rings" in g:
        shells, holes = [], []
        for ring in g["rings"]:
            if len(ring) < 4:
                continue
            # Esri winds outer rings clockwise and holes counter-clockwise.
            (holes if LinearRing(ring).is_ccw else shells).append(ring)
        if not shells:
            shells, holes = holes, []
        polys = []
        for s in shells:
            p = Polygon(s)
            mine = [h for h in holes if p.contains(Polygon(h).representative_point())]
            polys.append(Polygon(s, mine) if mine else p)
        if not polys:
            return None
        geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        return geom if geom.is_valid else geom.buffer(0)
    return None


# ---------------------------------------------------------------- projection

class Proj:
    """Local equirectangular projection to US feet around one latitude.

    Across a county (well under half a degree of latitude) the scale error is
    a fraction of a percent, which is noise against 40-150 ft thresholds, and
    it needs no projection library."""

    M_TO_FT = 3.280839895

    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = lon0, lat0
        p = math.radians(lat0)
        self.ky = (111132.954 - 559.822 * math.cos(2 * p) + 1.175 * math.cos(4 * p)) * self.M_TO_FT
        self.kx = (111412.84 * math.cos(p) - 93.5 * math.cos(3 * p)
                   + 0.118 * math.cos(5 * p)) * self.M_TO_FT

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def geom(self, g):
        import numpy as np
        import shapely
        if g is None:
            return None
        k = np.array([self.kx, self.ky])
        o = np.array([self.lon0, self.lat0])
        return shapely.transform(g, lambda c: (c - o) * k)

    def ll(self, x: float, y: float) -> tuple[float, float]:
        return x / self.kx + self.lon0, y / self.ky + self.lat0

    def lonlat(self, g):
        import numpy as np
        import shapely
        k = np.array([self.kx, self.ky])
        o = np.array([self.lon0, self.lat0])
        return shapely.transform(g, lambda c: c / k + o)


# -------------------------------------------------------------- topography

def grid_points(geom, spacing: float) -> list[tuple[int, int, float, float]]:
    """(i, j, x, y) for every grid cell centre inside `geom`, in feet. The i/j
    indices let slope come from each point's east and north neighbours."""
    import numpy as np
    import shapely
    x0, y0, x1, y1 = geom.bounds
    xs = np.arange(x0 + spacing / 2, x1, spacing)
    ys = np.arange(y0 + spacing / 2, y1, spacing)
    rp = geom.representative_point()
    if not len(xs) or not len(ys):
        return [(0, 0, rp.x, rp.y)]
    ii, jj = np.meshgrid(np.arange(len(xs)), np.arange(len(ys)), indexing="ij")
    gx, gy = xs[ii], ys[jj]
    shapely.prepare(geom)
    keep = shapely.contains_xy(geom, gx, gy)
    pts = [(int(i), int(j), float(x), float(y))
           for i, j, x, y in zip(ii[keep], jj[keep], gx[keep], gy[keep])]
    return pts or [(0, 0, rp.x, rp.y)]


def spacing_for(area_sqft: float, target: int) -> float:
    return max(MIN_SPACING_FT, math.sqrt(area_sqft / target)) if area_sqft > 0 else MIN_SPACING_FT


def dep_samples(points: list) -> list:
    """3DEP elevation in metres at each (lon, lat), None where unread. One
    request carries up to SAMPLE_BATCH points; the statistics route costs
    7-8 s a call whatever the area, so sampling is ~20x cheaper per lot."""
    import requests
    out: list = [None] * len(points)
    for k in range(0, len(points), SAMPLE_BATCH):
        chunk = points[k:k + SAMPLE_BATCH]
        p = {"geometry": json.dumps({"points": [list(pt) for pt in chunk],
                                     "spatialReference": {"wkid": 4326}}),
             "geometryType": "esriGeometryMultipoint", "returnFirstValueOnly": "true", "f": "json"}
        for attempt in range(4):
            try:
                r = requests.post(DEP_URL + "/getSamples", data=p, timeout=120)
                r.raise_for_status()
                d = r.json()
                if "error" in d:
                    raise RuntimeError(str(d["error"])[:120])
                for s in d.get("samples") or []:
                    try:
                        out[k + int(s["locationId"])] = float(s["value"])
                    except (KeyError, TypeError, ValueError):
                        pass
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    break
                time.sleep(3 * (attempt + 1))
    return out


def topo_from_samples(lot: dict, spacing_ft: float, ring: list) -> dict:
    """Steep share, mean slope, relief and how far the lot sits under its
    surroundings, from grid elevations in metres keyed (i, j)."""
    f = {"steep_share": None, "slope_mean_pct": None, "relief_ft": None,
         "sits_low_ft": None, "samples": len(lot)}
    z = {k: v * Proj.M_TO_FT for k, v in lot.items() if v is not None}
    if len(z) < 3:
        return f
    f["relief_ft"] = round(max(z.values()) - min(z.values()), 1)
    grades = []
    for (i, j), here in z.items():
        east, north = z.get((i + 1, j)), z.get((i, j + 1))
        if east is None or north is None:
            continue
        grades.append(math.hypot((east - here) / spacing_ft, (north - here) / spacing_ft))
    if grades:
        f["steep_share"] = round(sum(1 for g in grades if g > STEEP_GRADE) / len(grades), 3)
        f["slope_mean_pct"] = round(100 * statistics.mean(grades), 1)
    rz = [v * Proj.M_TO_FT for v in ring if v is not None]
    if len(rz) >= 3:
        f["sits_low_ft"] = round(statistics.median(rz) - statistics.median(z.values()), 1)
    return f


# ------------------------------------------------------------ owner classes

_PUBLIC = re.compile(
    r"\b(CITY|VILLAGE|TOWNSHIP|COUNTY|STATE) OF\b|\b(UNION|FRANKLIN|DELAWARE|MADISON|LOGAN|"
    r"CHAMPAIGN|MARION) COUNTY\b|\bCOMMISSIONERS\b|\bTWP\b|\bTOWNSHIP\b|\bBOARD OF\b|"
    r"\bSCHOOL DISTRICT\b|\bLOCAL SCHOOL\b|\bCITY SCHOOLS?\b|\bUNITED STATES\b|\bUSA\b|"
    r"\bPARK DISTRICT\b|\bMETRO PARKS\b|\bPORT AUTHORITY\b|\bLAND (BANK|REUTILIZATION)\b|"
    r"\bCOMMUNITY AUTHORITY\b|\bDEPARTMENT OF\b|\bODOT\b|\bHOUSING AUTHORITY\b|\bLIBRARY\b|"
    r"\bSTATE OF OHIO\b|\bOHIO (DEPT|DEPARTMENT)\b")
_HOA = re.compile(
    r"\b(HOME ?OWNERS?|OWNERS|CIVIC|COMMUNITY|CONDOMINIUM|CONDO|MASTER|PROPERTY OWNERS)\b.*"
    r"\b(ASSOCIATION|ASSN|ASSOC)\b|\bHOA\b|\bASSOCIATION\b|\bASSN\b")
_CEMETERY = re.compile(r"\b(CEMETERY|CEMETARY|BURIAL|MEMORIAL GARDENS)\b")
_CHURCH = re.compile(
    r"\b(CHURCH|MINISTRIES|MINISTRY|PARISH|DIOCESE|CONGREGATION|BAPTIST|METHODIST|LUTHERAN|"
    r"CATHOLIC|PRESBYTERIAN|TEMPLE|MOSQUE|SYNAGOGUE|CHAPEL|BIBLE|GOSPEL|TABERNACLE)\b")
_UTILITY = re.compile(
    r"\b(RAILROAD|RAILWAY|CSX|NORFOLK SOUTHERN|CONRAIL|ELECTRIC|POWER CO|EDISON|ENERGY|"
    r"COLUMBIA GAS|GAS CO|AEP|PIPE ?LINE|WATER (CO|COMPANY|DISTRICT)|SANITARY|SEWER|"
    r"TELEPHONE|TELECOM|COMMUNICATIONS|CELLULAR|WIRELESS|TOWER|COOPERATIVE|UTILITIES)\b")
# Production builders and lot developers active around Columbus. Their lots are
# inventory, not seller leads, and they are the exit for what we buy.
_BUILDER_NAMES = re.compile(
    r"\b(M ?/ ?I HOMES|M I HOMES|MI HOMES|PULTE|DEL WEBB|NVR|RYAN HOMES|ROCKFORD HOMES|DREES|"
    r"EPCON|FISCHER|SCHOTTENSTEIN|ROMANELLI|HOMEWOOD|TRUBERRY|DOMINION HOMES|BEAZER|"
    r"D ?R HORTON|LENNAR|TOLL BROTHERS|HOVNANIAN|ARCADIA|BOB WEBB|COASTAL RIDGE|CHARLES ROSS|"
    r"STANLEY MARTIN|MAPLE STREET HOMES|EASTERN HORIZON|JEROME VILLAGE|NORTHSTAR|MAJESTIC HOMES|"
    r"WAYNE HOMES|HOMES BY TRUBERRY|COVENTRY|CREEKSIDE DEVELOPMENT)\b")
_BUILDER_WORDS = re.compile(
    r"\b(HOMES|HOME BUILDERS?|BUILDERS?|BUILDING|CONSTRUCTION|DEVELOPMENT|DEVELOPERS?|"
    r"COMMUNITIES|LAND (CO|COMPANY|DEVELOPMENT|HOLDINGS))\b")
_BANK = re.compile(
    r"\b(BANK|SAVINGS|CREDIT UNION|MORTGAGE|FEDERAL NATIONAL|FEDERAL HOME LOAN|FANNIE MAE|"
    r"FREDDIE MAC|SECRETARY OF HOUSING|HUD|NATIONAL ASSOCIATION|LENDING)\b")
_TRUST_ESTATE = re.compile(
    r"\b(TRUST|TRUSTEE|TRUSTEES|TR|TRS|REVOCABLE|ESTATE|EST|HEIRS|ET AL|ETAL|LIFE ESTATE|"
    r"DECD|DECEASED)\b")
_ENTITY = re.compile(
    r"\b(LLC|L L C|INC|CORP|CORPORATION|CO|COMPANY|LTD|LP|LLP|PARTNERS|PARTNERSHIP|HOLDINGS|"
    r"INVESTMENTS?|GROUP|ENTERPRISES|PROPERTIES|REALTY|VENTURES|CAPITAL|FUND|ASSOCIATES)\b")

PUBLIC_CATS = {"public", "hoa", "cemetery", "church", "utility"}
CAT_LABEL = {"public": "public body", "hoa": "owners association or common area",
             "cemetery": "cemetery", "church": "church", "utility": "utility or railroad",
             "builder": "builder or developer", "bank": "bank or lender",
             "trust_estate": "trust, estate or heirs", "entity": "company",
             "person": "individual"}


def _clean(name: str | None) -> str:
    s = (name or "").upper().replace("&", " & ")
    s = re.sub(r"[^A-Z0-9&/ ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def owner_category(name: str | None) -> str:
    s = _clean(name)
    if not s:
        return "person"
    for cat, rx in (("public", _PUBLIC), ("hoa", _HOA), ("cemetery", _CEMETERY),
                    ("church", _CHURCH), ("utility", _UTILITY)):
        if rx.search(s):
            return cat
    if _BUILDER_NAMES.search(s):
        return "builder"
    if _BANK.search(s):
        return "bank"
    if _BUILDER_WORDS.search(s) and _ENTITY.search(s):
        return "builder"
    if _TRUST_ESTATE.search(s):
        return "trust_estate"
    if _ENTITY.search(s):
        return "entity"
    return "person"


_SUFFIX = {"LLC", "L", "C", "INC", "CO", "CORP", "LTD", "THE", "LP", "LLP"}


def owner_key(name: str | None) -> str:
    """Same-owner key across county rows. County deeds write persons LAST FIRST
    M, so surname + first name identifies a household; co-owners after & are
    dropped; an entity keeps its whole name minus its legal suffix."""
    s = _clean(name)
    if not s:
        return ""
    first = re.split(r" & | AND ", s)[0]
    toks = [t for t in first.split() if t not in _SUFFIX]
    if not toks:
        return ""
    cat = owner_category(name)
    if cat == "person" or cat == "trust_estate":
        toks = [t for t in toks if not _TRUST_ESTATE.fullmatch(t)]
        return " ".join(toks[:2])
    return " ".join(toks)


# --------------------------------------------------------- geometry metrics

def shape_metrics(poly) -> dict:
    """Width along the long axis (area / long side of the minimum rotated
    rectangle, so a flag lot reads as narrow as it really is) and compactness."""
    import shapely
    area = poly.area
    perim = poly.length
    mrr = shapely.minimum_rotated_rectangle(poly)
    xs = list(mrr.exterior.coords) if mrr.geom_type == "Polygon" else []
    sides = []
    for a, b in zip(xs, xs[1:]):
        sides.append(math.hypot(b[0] - a[0], b[1] - a[1]))
    long_side = max(sides) if sides else math.sqrt(area)
    width = area / long_side if long_side else 0.0
    compact = 4 * math.pi * area / (perim * perim) if perim else 0.0
    return {"width_ft": round(width, 1), "depth_ft": round(long_side, 1),
            "compactness": round(compact, 3)}


def road_access(poly, road_tree, roads: list, row_tree, rows: list) -> dict:
    """Distance to the nearest centerline, and the length of lot line that faces
    a road (within ROAD_REACH_FT of a centerline or touching a right-of-way
    line). A corner lot counts both streets."""
    from shapely.ops import unary_union
    near_d = None
    idx, dist = road_tree.query_nearest(poly, max_distance=LANDLOCKED_FT * 4,
                                        return_distance=True)
    if len(idx):
        near_d = float(min(dist))
    row_d = None
    if row_tree is not None:
        ridx, rdist = row_tree.query_nearest(poly, max_distance=LANDLOCKED_FT,
                                             return_distance=True)
        if len(ridx):
            row_d = float(min(rdist))
    edge = poly.boundary
    front = 0.0
    close = road_tree.query(poly.buffer(ROAD_REACH_FT), predicate="intersects")
    if len(close):
        corridor = unary_union([roads[i].buffer(ROAD_REACH_FT) for i in close])
        front = edge.intersection(corridor).length
    if row_tree is not None:
        rclose = row_tree.query(poly.buffer(ROW_TOUCH_FT), predicate="intersects")
        if len(rclose):
            corridor = unary_union([rows[i].buffer(ROW_TOUCH_FT) for i in rclose])
            front = max(front, edge.intersection(corridor).length)
    landlocked = ((near_d is None or near_d > LANDLOCKED_FT)
                  and (row_d is None or row_d > ROW_TOUCH_FT))
    return {"road_ft": None if near_d is None else round(near_d, 1),
            "frontage_ft": round(front, 1), "landlocked": landlocked}


def area_share(poly, tree, geoms: list) -> float:
    hits = tree.query(poly, predicate="intersects")
    if not len(hits) or not poly.area:
        return 0.0
    a = sum(poly.intersection(geoms[i]).area for i in hits)
    return round(min(1.0, a / poly.area), 3)


def sewer_proxy(acres_nearby: list, platted: bool = False, acres: float = 0.0) -> tuple[str, str]:
    """('sewer likely' | 'septic area' | 'mixed' | 'unknown', basis).

    Houses nearby decide it when there are enough of them. Without them: an
    unplatted lot with almost no houses around is rural, so septic; a platted
    lot under half an acre was approved on public sewer, since an engineered
    septic system and its replacement area do not fit on one."""
    n = len(acres_nearby)
    if n >= SEWER_MIN_NEIGHBORS:
        med = statistics.median(acres_nearby)
        basis = "%d houses within %d ft, median lot %.2f ac" % (n, SEWER_RADIUS_FT, med)
        if med < SEWER_SMALL_LOT_AC:
            return "sewer likely", basis
        if med >= SEPTIC_AREA_AC:
            return "septic area", basis
        return "mixed", basis
    if platted and acres < SEWER_SMALL_LOT_AC:
        return "sewer likely", "platted lot under %.2f ac" % SEWER_SMALL_LOT_AC
    if not platted:
        return "septic area", "rural: %d houses within %d ft" % (n, SEWER_RADIUS_FT)
    return "unknown", "platted, %d houses within %d ft" % (n, SEWER_RADIUS_FT)


def dwelling_year(age) -> int | None:
    """The dwelling layer's `age` is a year built; '0' or blank is no house."""
    try:
        y = int(str(age or "").strip()[:4])
    except ValueError:
        return None
    return y if 1700 <= y <= today().year + 1 else None


def built_since_reappraisal(sale: dict | None, dwelling_built: int | None, permit_year: int | None,
                            as_of: dt.date | None = None) -> list[str]:
    """Evidence that a structure stands on a parcel the county still codes vacant."""
    as_of = as_of or today()
    why = []
    if dwelling_built:
        why.append("county dwelling record, built %d" % dwelling_built)
    if sale and sale.get("salePrice") and sale.get("saleDate"):
        sold = dt.date.fromtimestamp(sale["saleDate"] / 1000)
        price = float(sale["salePrice"])
        land = float(sale.get("landValue") or 0)
        if ((as_of - sold).days <= 4 * 365 and int(sale.get("NumberofParcelsInSale") or 1) == 1
                and price >= BUILT_SALE_FLOOR and land > 0 and price >= BUILT_SALE_RATIO * land):
            why.append("sold %s for $%s against a $%s land value" % (
                sold.isoformat(), f"{price:,.0f}", f"{land:,.0f}"))
    if permit_year and permit_year >= as_of.year - 3:
        why.append("building permit on file %d" % permit_year)
    return why


_NONRES = re.compile(r"COMMERCIAL|INDUSTR|MANUFACTUR|BUSINESS|OFFICE|GOVERNMENT|CIVIC|"
                     r"HOSPITAL|INSTITUTION|OPEN SPACE|CONSERVATION|PARK")


def zoning_nonres(definition: str | None) -> bool:
    d = (definition or "").upper()
    return bool(d) and bool(_NONRES.search(d)) and "RESIDENTIAL" not in d


# ------------------------------------------------------------- the verdict

def buildability(f: dict) -> tuple[str, int, list[str], list[str]]:
    """(verdict, score 0-100, hard reasons, soft flags) for one parcel's facts.

    Score: access 40 (platted 20 + frontage 20), utilities 20, flood 15,
    shape 10, topography 10, zoning 5. Hard fails make a lot NOT BUILDABLE whatever it
    scores; public and common-area owners and built-on parcels are their own
    verdicts so nothing is mislabeled as a lead."""
    flags: list[str] = []
    if f["owner_cat"] in PUBLIC_CATS:
        return "PUBLIC OR COMMON", 0, ["owner is a %s" % CAT_LABEL[f["owner_cat"]]], flags
    if f.get("built"):
        return "BUILT SINCE REAPPRAISAL", 0, list(f["built"]), flags

    pts = 0
    platted = bool(f.get("platted"))
    if platted:
        pts += 20
    front = f.get("frontage_ft") or 0
    if front >= 100:
        pts += 20
    elif front >= FRONTAGE_OK_FT:
        pts += 15
    elif front >= FRONTAGE_MIN_FT:
        pts += 8
        flags.append("short frontage %d ft" % front)
    elif not f.get("landlocked"):
        pts += 4
        flags.append("road access but under %d ft of frontage" % FRONTAGE_MIN_FT)

    w = f.get("width_ft") or 0
    pts += 10 if w >= 80 else 8 if w >= 60 else 5 if w >= 50 else 2
    if (f.get("compactness") or 1) < 0.20:
        pts -= 5
        flags.append("irregular shape")

    fl = f.get("flood_pct") or 0
    pts += 15 if fl == 0 else 10 if fl < FLOOD_FLAG else 5 if fl < 0.25 else 0
    if fl >= FLOOD_FLAG:
        flags.append("%d%% in the 100-year floodplain" % round(fl * 100))

    sewer = f.get("sewer") or "unknown"
    ac = f.get("acres") or 0
    if sewer == "sewer likely":
        pts += 20
    elif ac >= SEPTIC_MIN_AC:
        pts += 12 if sewer == "septic area" else 10
        if sewer == "septic area":
            flags.append("septic area: engineered system, health district sign-off")
    elif sewer == "septic area":
        flags.append("septic risk: under %.1f acre where houses are on septic" % SEPTIC_MIN_AC)
    else:
        pts += 5
        flags.append("sewer unknown on a lot under %.1f acre" % SEPTIC_MIN_AC)

    steep, low = f.get("steep_share"), f.get("sits_low_ft")
    if steep is None and low is None:
        pts += 7        # no elevation read: neither credit nor penalty in full
    else:
        t = 10
        if steep is not None and steep >= STEEP_FLAG:
            t -= 5
            flags.append("%d%% of the lot steeper than a 15%% grade" % round(steep * 100))
        if low is not None and low >= LOW_BAD_FT:
            t -= 6
            flags.append("sits %.0f ft below the land around it: drainage and fill" % low)
        elif low is not None and low >= LOW_FLAG_FT:
            t -= 3
            flags.append("sits %.0f ft low: check drainage" % low)
        if (f.get("relief_ft") or 0) >= RELIEF_FLAG_FT:
            flags.append("%d ft of relief across the lot" % f["relief_ft"])
        pts += max(0, t)

    if not f.get("zoning_nonres"):
        pts += 5
    if f.get("class") in AG_VACANT:
        flags.append("agricultural class: confirm the township allows a house")
    if f.get("lot_type") and "LANDLOCK" in str(f["lot_type"]).upper() and not f.get("landlocked"):
        flags.append("DataSift calls it landlocked; county roads say it fronts a road")
    pts = max(0, min(100, pts))

    hard = []
    if f.get("landlocked") and not platted and not f.get("assemblage_frontage"):
        hard.append("landlocked: no road within %d ft" % LANDLOCKED_FT)
    if w < WIDTH_FAIL_FT:
        hard.append("strip: %d ft wide" % w)
    if fl >= FLOOD_FAIL:
        hard.append("%d%% in the 100-year floodplain" % round(fl * 100))
    if (f.get("floodway_pct") or 0) >= FLOODWAY_FAIL:
        hard.append("%d%% in the floodway" % round(f["floodway_pct"] * 100))
    if steep is not None and steep >= STEEP_FAIL:
        hard.append("%d%% of the lot steeper than a 15%% grade" % round(steep * 100))
    if f.get("zoning_nonres"):
        hard.append("zoned %s" % (f.get("zoning") or "non-residential"))
    if f["owner_cat"] == "builder":
        return "BUILDER INVENTORY", pts, hard, flags
    if hard:
        return "NOT BUILDABLE", pts, hard, flags
    if f.get("landlocked") and platted:
        flags.append("platted lot, street not in the county centerline yet")
    if pts >= 75 and not any(x.startswith(("septic risk", "sewer unknown")) for x in flags):
        return "BUILDABLE", pts, hard, flags
    if pts >= 55:
        return "LIKELY BUILDABLE", pts, hard, flags
    return "VERIFY", pts, hard, flags


# --------------------------------------------------------------- SiftMap

def county_address(c: dict) -> dict:
    """The rich county object plus the thin search/type pair the search route
    needs. `search` must be the BARE county name: "Union County, OH" returns 0."""
    title = "%s County, %s" % (c["name"], c["state"])
    return {"state": c["state"], "title": title, "value": title, "county": c["name"],
            "searchType": "county", "counties": [{"fips": c["fips"], "county_name": c["name"]}],
            "search": c["name"], "type": "county"}


def search_body(c: dict, filters: dict, result_index: int = 1) -> dict:
    from siftmap_pull import WORLD
    return {"result_index": result_index, "with_boundaries": False, "filters": filters,
            "addresses": [county_address(c)], "polygon": WORLD}


def active_distressors(d) -> list[str]:
    """Keys whose is_active is true. Search rows and details both ship every
    key on every record, so presence means nothing."""
    out = []
    for k, v in (d or {}).items():
        on = v.get("is_active") if isinstance(v, dict) else bool(v)
        if on:
            out.append(k)
    return sorted(out)


PROBE_KEYS = ["type_land_residential", "type_vacant_land", "type_lot", "type_vacant",
              "type_commercial", "type_farm"]
ROW_KEYS = ("id", "lotAcres", "propertyUse", "latitude", "longitude", "distressors",
            "presets", "absenteeOwner", "corporateOwned", "saved")


def probe_failures(counts: dict, rows: list) -> tuple[list[str], list[str]]:
    """(failures, notes) from a probe run. A failure means the contract this
    workflow rests on has moved and nothing downstream can be trusted."""
    fails, notes = [], []
    base = counts.get("none", 0)
    land = counts.get("type_land", 0)
    if base <= 0:
        fails.append("the county query returned 0 rows: the address shape broke")
    if counts.get("bogus") != base:
        fails.append("a bogus key moved the count (%s vs %s): unknown keys are no longer "
                     "ignored, so every filter has to be proven again" % (counts.get("bogus"), base))
    if not 0 < land < base:
        fails.append("type_land did not filter (%s of %s)" % (land, base))
    for k in PROBE_KEYS:
        if k in counts and counts[k] != base:
            notes.append("%s now changes the count (%s): a new filter key exists" % (k, counts[k]))
    if rows:
        vac = sum(1 for r in rows if "VACANT" in str(r.get("propertyUse") or "").upper())
        if vac / len(rows) < 0.95:
            fails.append("only %d of %d type_land rows read as vacant land" % (vac, len(rows)))
        miss = [k for k in ROW_KEYS if k not in rows[0]]
        if miss:
            fails.append("rows lost fields this workflow reads: %s" % ", ".join(miss))
        d = rows[0].get("distressors")
        if not isinstance(d, dict) or not all(isinstance(v, dict) for v in d.values()):
            fails.append("distressors are no longer {key: {is_active}}; active_distressors() needs updating")
    else:
        fails.append("type_land returned no rows to inspect")
    return fails, notes


def _sm():
    _load_env()
    from siftmap_standalone import SiftMapClient
    return SiftMapClient(min_interval=0.3, max_retries=6)


def phase_probe(c: dict, args) -> int:
    sm = _sm()
    counts = {}

    def n(label, f):
        r = sm._request("/properties/search/", "POST", search_body(c, f), timeout=120)
        counts[label] = int(r.get("total_results") or 0)
        print("  %-28s %s" % (label, f"{counts[label]:,}"))
        return r

    print("== %s: land filter proof by count delta" % c["label"])
    n("none", {})
    n("bogus", {"zz_not_a_filter": True})
    land = n("type_land", {"type_land": True})
    n("type_single_family", {"type_single_family": True})
    for k in PROBE_KEYS:
        n(k, {k: True})
    rows = land.get("data") or []
    fails, notes = probe_failures(counts, rows)
    for x in notes:
        print("  note:", x)
    res = {"run": dt.datetime.now().isoformat(timespec="seconds"), "counts": counts,
           "row_keys": sorted(rows[0].keys()) if rows else [],
           "property_use": _counter(r.get("propertyUse") for r in rows),
           "fails": fails, "notes": notes}
    _save(os.path.join(out_dir(c), "probe.json"), res)
    if fails:
        for x in fails:
            print("  FAIL:", x)
        return 3
    print("  PASS: type_land is the land filter (%s of %s rows), bogus keys are ignored"
          % (f"{counts['type_land']:,}", f"{counts['none']:,}"))
    return 0


def _counter(it) -> dict:
    out: dict = {}
    for x in it:
        out[str(x)] = out.get(str(x), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def phase_sweep(c: dict, args) -> int:
    sm = _sm()
    f = {"type_land": True}
    rows: dict = {}
    idx, total = 1, None
    while True:
        r = sm._request("/properties/search/", "POST", search_body(c, f, idx), timeout=120)
        total = int(r.get("total_results") or 0)
        if total > SPLIT_AT:
            # Land rows often carry no value, so the value bisection the SFR
            # sweeps use would silently drop them. Split by zip before running.
            print("FAIL: %s land rows is past the %s offset wall; split the sweep by zip_codes"
                  % (f"{total:,}", f"{SPLIT_AT:,}"))
            return 3
        data = r.get("data") or []
        for x in data:
            rows[str(x["id"])] = x
        idx += PAGE
        if not data or idx > total:
            break
    _save(os.path.join(out_dir(c), "siftmap_land.json"), list(rows.values()))
    print("sweep: %s land rows of %s (unique ids)" % (f"{len(rows):,}", f"{total:,}"))
    if len(rows) != total:
        print("  WARN: unique ids %d != total_results %d" % (len(rows), total))
    return 0


# ---------------------------------------------------------------- county

def phase_county(c: dict, args) -> int:
    d = os.path.join(out_dir(c), "layers")
    for name, (path, fields, geom, page) in c["layers"].items():
        dest = os.path.join(d, name + ".json")
        if os.path.exists(dest) and not args.refresh:
            print("  %-13s cached (%s)" % (name, f"{len(_load(dest)):,}"))
            continue
        t0 = time.time()
        feats = arcgis_query(c["gis"] + path, fields=fields, geometry=geom, page=page)
        _save(dest, feats)
        print("  %-13s %s features (%.0fs)" % (name, f"{len(feats):,}", time.time() - t0))
    return 0


def _layer(c: dict, name: str) -> list[dict]:
    feats = _load(os.path.join(out_dir(c), "layers", name + ".json"))
    if feats is None:
        raise SystemExit("layer %s missing: run --phase county first" % name)
    return feats


def _proj_for(parcels: list) -> Proj:
    lons, lats = [], []
    for p in parcels[:4000]:
        for ring in (p.get("geometry") or {}).get("rings", [])[:1]:
            lons.append(ring[0][0])
            lats.append(ring[0][1])
    return Proj(statistics.median(lons), statistics.median(lats))


def in_window(cls: str, acres: float) -> bool:
    return cls in VACANT_CLASSES and ACRES_MIN <= acres <= ACRES_MAX


# ------------------------------------------------------------------- topo

def phase_topo(c: dict, args) -> int:
    """3DEP elevation on a grid over every vacant-class lot in the window plus
    a RING_FT ring around it, batched so one request covers about ten lots.
    Resumable; free; no key."""
    from concurrent.futures import ThreadPoolExecutor
    parcels = _layer(c, "parcels")
    proj = _proj_for(parcels)
    path = os.path.join(out_dir(c), "topo.json")
    cache = {} if args.refresh else _load(path, {})
    lots = []
    for ft in parcels:
        a = ft.get("attributes") or {}
        cls = str(a.get("ParcelClass") or "")
        if cls not in VACANT_CLASSES:
            continue
        g = esri_to_shape(ft.get("geometry"))
        if g is None or g.is_empty:
            continue
        gf = proj.geom(g)
        if not in_window(cls, float(a.get("Acreage") or 0) or gf.area / 43560.0):
            continue
        cama = (a.get("camano") or "").strip()
        if cama in cache:
            continue
        s = spacing_for(gf.area, LOT_POINTS)
        ring = gf.buffer(RING_FT).difference(gf)
        lots.append((cama, s, grid_points(gf, s), grid_points(ring, spacing_for(ring.area, RING_POINTS))))
    if args.limit:
        lots = lots[:args.limit]
    batches, cur, size = [], [], 0
    for lot in lots:
        n = len(lot[2]) + len(lot[3])
        if cur and size + n > SAMPLE_BATCH:
            batches.append(cur)
            cur, size = [], 0
        cur.append(lot)
        size += n
    if cur:
        batches.append(cur)
    print("topo: %d lots in %d 3DEP requests, %d cached" % (len(lots), len(batches), len(cache)))

    def work(batch):
        pts = [proj.ll(x, y) for _, _, lp, rp in batch for (_, _, x, y) in lp + rp]
        z = dep_samples(pts)
        out, k = {}, 0
        for cama, s, lp, rp in batch:
            lz = {(i, j): z[k + n] for n, (i, j, _, _) in enumerate(lp)}
            k += len(lp)
            out[cama] = topo_from_samples(lz, s, z[k:k + len(rp)])
            k += len(rp)
        return out

    t0 = time.time()
    with ThreadPoolExecutor(4) as ex:
        for n, res in enumerate(ex.map(work, batches), 1):
            cache.update(res)
            if n % 10 == 0:
                _save(path, cache)
                print("  %d / %d requests (%.0fs)" % (n, len(batches), time.time() - t0), flush=True)
    _save(path, cache)
    vals = list(cache.values())
    miss = sum(1 for v in vals if v.get("relief_ft") is None)
    steep = sum(1 for v in vals if (v.get("steep_share") or 0) >= STEEP_FLAG)
    low = sum(1 for v in vals if (v.get("sits_low_ft") or 0) >= LOW_FLAG_FT)
    print("topo: %d lots, %d steep (%d%%+ over a 15%% grade), %d sit %.0f+ ft low, %d unread"
          % (len(vals), steep, STEEP_FLAG * 100, low, LOW_FLAG_FT, miss))
    return 0


# ---------------------------------------------------------------- screen

def phase_screen(c: dict, args) -> int:
    from shapely import STRtree
    from shapely.geometry import Point

    parcels = _layer(c, "parcels")
    proj = _proj_for(parcels)
    topo = _load(os.path.join(out_dir(c), "topo.json"), {})

    def shapes(name):
        out, attrs = [], []
        for ft in _layer(c, name):
            g = proj.geom(esri_to_shape(ft.get("geometry")))
            if g is not None and not g.is_empty:
                out.append(g)
                attrs.append(ft.get("attributes") or {})
        return out, attrs

    print("screen: projecting %s parcels" % f"{len(parcels):,}")
    pg, pa = shapes("parcels")
    roads, road_attrs = shapes("roads")
    rows, _ = shapes("row")
    flood, _ = shapes("flood")
    fway, _ = shapes("floodway")
    zones, zone_attrs = shapes("zoning")
    ptree, rtree = STRtree(pg), STRtree(roads)
    rowtree = STRtree(rows) if rows else None
    ftree, fwtree = STRtree(flood), STRtree(fway) if fway else None
    ztree = STRtree(zones) if zones else None

    lots = {}
    for ft in _layer(c, "lots"):
        a = ft["attributes"]
        if a.get("CAMANo"):
            lots[a["CAMANo"].strip()] = a
    plat_year = {str(ft["attributes"].get("Number") or "").strip(): ft["attributes"].get("Year")
                 for ft in _layer(c, "subdivisions")}
    sales = {}
    for ft in _layer(c, "sales"):
        a = ft["attributes"]
        k = (a.get("CAMANO") or "").strip()
        if k and (k not in sales or (a.get("saleDate") or 0) > (sales[k].get("saleDate") or 0)):
            sales[k] = a
    # The dwelling layer has a row for nearly EVERY parcel; `age` is the year
    # built and '0' means no house. Membership alone flagged 1,035 of 1,046
    # vacant lots in the window as built on.
    dwell = {}
    for ft in _layer(c, "dwellings"):
        a = ft["attributes"]
        y = dwelling_year(a.get("age"))
        if y:
            dwell[(a.get("CAMANO") or "").strip()] = y
    permits: dict = {}
    for ft in _layer(c, "permits"):
        a = ft["attributes"]
        k = (a.get("CAMANo") or "").strip()
        try:
            y = int(str(a.get("mYear") or "")[:4])
        except ValueError:
            continue
        permits[k] = max(permits.get(k, 0), y)

    # house lots for the sewer proxy
    hp, hac = [], []
    for g, a in zip(pg, pa):
        if str(a.get("ParcelClass")) in IMPROVED_RES and (a.get("Acreage") or 0) > 0:
            hp.append(g.representative_point())
            hac.append(float(a["Acreage"]))
    htree = STRtree(hp)

    # SiftMap land rows: point in parcel
    sm_rows = _load(os.path.join(out_dir(c), "siftmap_land.json"), [])
    sm_by_parcel: dict = {}
    sm_unplaced = 0
    for r in sm_rows:
        if r.get("longitude") is None:
            continue
        pt = Point(*proj.xy(r["longitude"], r["latitude"]))
        hit = ptree.query(pt, predicate="intersects")
        if not len(hit):
            sm_unplaced += 1
            continue
        sm_by_parcel.setdefault(int(hit[0]), []).append(r)

    keys = [owner_key(a.get("OwnerCurrent") or a.get("Owner")) for a in pa]
    classes = [str(a.get("ParcelClass") or "") for a in pa]

    def acres_of(i):
        a = pa[i].get("Acreage")
        return float(a) if a else round(pg[i].area / 43560.0, 3)

    cand = [i for i in range(len(pg)) if in_window(classes[i], acres_of(i))]
    in_window_owner = {}
    for i in cand:
        in_window_owner[keys[i]] = in_window_owner.get(keys[i], 0) + 1
    print("screen: %s vacant-class parcels in %.2f-%.2f ac; %s SiftMap land rows placed"
          % (f"{len(cand):,}", ACRES_MIN, ACRES_MAX,
             f"{sum(len(v) for v in sm_by_parcel.values()):,}"))

    out = []
    for n, i in enumerate(cand, 1):
        a, g = pa[i], pg[i]
        cama = (a.get("camano") or "").strip()
        owner = (a.get("OwnerCurrent") or a.get("Owner") or "").strip()
        cat = owner_category(owner)
        acres = acres_of(i)
        f = {"camano": cama, "gisno": (a.get("GISNO") or "").strip(), "class": classes[i],
             "acres": round(acres, 3), "owner": re.sub(r"\s+", " ", owner), "owner_cat": cat,
             "owner_key": keys[i], "address": (a.get("Address") or "").strip(),
             "house_number": a.get("houseNumber") or 0, "city": (a.get("city") or "").strip(),
             "zip": zip5(a.get("ZipCode")), "township": a.get("township") or "",
             "school": a.get("schoolDistrict") or "", "card": a.get("PropertyCard") or ""}
        f.update(shape_metrics(g))
        f.update(road_access(g, rtree, roads, rowtree, rows))
        f["flood_pct"] = area_share(g, ftree, flood)
        f["floodway_pct"] = area_share(g, fwtree, fway) if fwtree else 0.0
        f.update(topo.get(cama) or {})
        if ztree is not None:
            zi = ztree.query(g.representative_point(), predicate="intersects")
            if len(zi):
                za = zone_attrs[int(zi[0])]
                f["zoning"] = "%s %s" % (za.get("ZoningName") or "", za.get("zoning") or "")
                f["zoning_def"] = za.get("definitions") or ""
                f["zoning_nonres"] = zoning_nonres(za.get("definitions"))
        lot = lots.get(cama)
        subd = str((lot or {}).get("SUBD_NUMB") or a.get("SUBD_NUMB") or "").strip()
        f["platted"] = bool(lot and (lot.get("lotNumber") or lot.get("SUBDIVISION_NAME")))
        f["subdivision"] = (lot or {}).get("SUBDIVISION_NAME") or ""
        f["lot_number"] = (lot or {}).get("lotNumber") or ""
        f["plat_year"] = plat_year.get(subd)
        near = htree.query(g.centroid.buffer(SEWER_RADIUS_FT), predicate="intersects")
        nac = [hac[j] for j in near]
        f["houses_nearby"] = len(nac)
        f["house_lot_median_ac"] = round(statistics.median(nac), 2) if nac else None
        f["sewer"], f["sewer_basis"] = sewer_proxy(nac, f["platted"], acres)
        sale = sales.get(cama)
        f["built"] = built_since_reappraisal(sale, dwell.get(cama), permits.get(cama))
        if sale:
            f["last_sale_date"] = (dt.date.fromtimestamp(sale["saleDate"] / 1000).isoformat()
                                   if sale.get("saleDate") else None)
            f["last_sale_price"] = sale.get("salePrice")
            f["land_value"] = sale.get("landValue")
        # neighbours: same owner next door
        side, assem, assem_front = [], [], False
        for j in ptree.query(g.buffer(3), predicate="intersects"):
            j = int(j)
            if j == i or not keys[i] or keys[j] != keys[i]:
                continue
            if classes[j] in VACANT_CLASSES:
                assem.append(acres_of(j))
                if road_access(pg[j], rtree, roads, rowtree, rows)["frontage_ft"] >= FRONTAGE_MIN_FT:
                    assem_front = True
            elif classes[j] in IMPROVED_RES or classes[j].startswith("5"):
                side.append((pa[j].get("Address") or "").strip())
        f["side_lot_of"] = side[0] if side else ""
        f["assemblage_acres"] = round(acres + sum(assem), 3) if assem else None
        f["assemblage_frontage"] = assem_front
        f["owner_lots_in_window"] = in_window_owner.get(keys[i], 1)
        sm = sm_by_parcel.get(i) or []
        if sm:
            best = min(sm, key=lambda r: abs((r.get("lotAcres") or 0) - acres))
            f["sid"] = str(best["id"])
            f["sm_use"] = best.get("propertyUse")
            f["sm_acres"] = best.get("lotAcres")
            f["absentee"] = bool(best.get("absenteeOwner"))
            f["corporate"] = bool(best.get("corporateOwned"))
            f["mls_active"] = bool(best.get("mlsActive") or best.get("mlsPending"))
            f["presets"] = sorted(k for k, v in (best.get("presets") or {}).items() if v)
            f["distress"] = active_distressors(best.get("distressors"))
            f["saved"] = bool(best.get("saved"))
        v, score, hard, flags = buildability(f)
        f.update(verdict=v, build_score=score, hard=hard, flags=flags)
        out.append(f)
        if n % 500 == 0:
            print("  %d / %d" % (n, len(cand)))

    # class meaning, measured: county class x SiftMap label for every placed row
    xtab: dict = {}
    for i, rs in sm_by_parcel.items():
        for r in rs:
            k = "%s | %s" % (classes[i] or "none", r.get("propertyUse"))
            xtab[k] = xtab.get(k, 0) + 1
    meta = {"run": dt.datetime.now().isoformat(timespec="seconds"),
            "parcels": len(pg), "candidates": len(cand), "sm_rows": len(sm_rows),
            "sm_unplaced": sm_unplaced,
            "sm_on_candidates": sum(1 for f in out if f.get("sid")),
            "class_x_siftmap": dict(sorted(xtab.items(), key=lambda kv: -kv[1])),
            "verdicts": _counter(f["verdict"] for f in out),
            "class_counts": _counter(classes[i] for i in cand)}
    _save(os.path.join(out_dir(c), "screen.json"), out)
    _save(os.path.join(out_dir(c), "screen_meta.json"), meta)
    print("screen: verdicts %s" % meta["verdicts"])
    print("        %d of %d candidates carry a SiftMap id; %d SiftMap land rows sit on no parcel"
          % (meta["sm_on_candidates"], len(out), sm_unplaced))
    return 0


# ---------------------------------------------------------------- hydrate

def mail_parts(addr: str | None) -> dict:
    """'100 Example Rd, Plain City, OH 43064' -> street/city/state/zip."""
    parts = [p.strip() for p in (addr or "").split(",") if p.strip()]
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not parts:
        return out
    last = parts[-1].split()
    if last and re.fullmatch(r"\d{5}(-\d{4})?", last[-1]):
        out["zip"] = last[-1][:5]
        last = last[:-1]
    if last and re.fullmatch(r"[A-Z]{2}", last[-1].upper()):
        out["state"] = last[-1].upper()
        last = last[:-1]
    rest = parts[:-1] + ([" ".join(last)] if last else [])
    if rest:
        out["street"] = rest[0]
        out["city"] = rest[1] if len(rest) > 1 else ""
    return out


def flatten_detail(d: dict) -> dict:
    oi = d.get("owner_info") or {}
    sh = d.get("sale_history") or []
    last = sh[0] if sh else {}
    return {
        "apn": d.get("apn"), "owner_name": oi.get("owner_name"),
        "co_owners": oi.get("secondary_owner_names") or [],
        "mail": oi.get("owner_mail_address"), "owner_props": oi.get("total_properties"),
        "tax_delinquent": d.get("tax_delinquent"), "tax_delinquent_year": d.get("tax_delinquent_year"),
        "tax_amount": d.get("tax_amount"), "tax_lien": d.get("tax_lien"),
        "assessed_land": d.get("assessed_land_value"), "lot_type": d.get("lot_type"),
        "lot_frontage_feet": d.get("lot_frontage_feet"), "zoning": d.get("zoning"),
        "flood_zone_type": d.get("flood_zone_type"), "legal": d.get("legal_description"),
        "sale_date": last.get("sale_date"), "sale_price": last.get("sale_price"),
        "seller": last.get("seller_name"), "obituaries": len(d.get("obituary_history") or []),
        "distress": active_distressors(d.get("distressor_data")),
        "presets": sorted(k for k, v in (d.get("presets") or {}).items() if v),
    }


def viewport_find(sm, c: dict, geom_ll) -> dict | None:
    """The SiftMap row whose point falls inside this parcel, found by handing
    the parcel's own bounds in as the map viewport. This is how a lot that
    type_land does not return is reached: type_land is RESIDENTIAL vacant land
    only, and SiftMap files small ag-coded lots as Rural/Agricultural-Vacant."""
    from shapely.geometry import Point
    x0, y0, x1, y1 = geom_ll.bounds
    pad = 0.0003
    poly = [{"lon": x0 - pad, "lat": y1 + pad}, {"lon": x0 - pad, "lat": y0 - pad},
            {"lon": x1 + pad, "lat": y0 - pad}, {"lon": x1 + pad, "lat": y1 + pad}]
    r = sm._request("/properties/search/", "POST", {
        "result_index": 1, "with_boundaries": False, "filters": {},
        "addresses": [county_address(c)], "polygon": poly}, timeout=120)
    zone = geom_ll.buffer(0.00005)
    inside = [x for x in r.get("data") or [] if x.get("longitude") is not None
              and zone.contains(Point(x["longitude"], x["latitude"]))]
    if not inside:
        return None
    ctr = geom_ll.centroid
    return min(inside, key=lambda x: Point(x["longitude"], x["latitude"]).distance(ctr))


def phase_hydrate(c: dict, args) -> int:
    screen = _load(os.path.join(out_dir(c), "screen.json"))
    if screen is None:
        raise SystemExit("run --phase screen first")
    path = os.path.join(out_dir(c), "details.json")
    cache = _load(path, {})
    sm = _sm()

    rpath = os.path.join(out_dir(c), "rescued.json")
    rescued = _load(rpath, {})
    miss = [f for f in screen if f["verdict"] in MARKETABLE and not f.get("sid")
            and f["camano"] not in rescued]
    if miss:
        geo = {(p["attributes"].get("camano") or "").strip(): p.get("geometry")
               for p in _layer(c, "parcels")}
        print("hydrate: finding %d marketable lots type_land does not return" % len(miss))
        for n, f in enumerate(miss, 1):
            g = esri_to_shape(geo.get(f["camano"]))
            row = viewport_find(sm, c, g) if g is not None else None
            rescued[f["camano"]] = None if row is None else {
                "sid": str(row["id"]), "sm_use": row.get("propertyUse"), "sm_acres": row.get("lotAcres"),
                "absentee": bool(row.get("absenteeOwner")), "corporate": bool(row.get("corporateOwned")),
                "mls_active": bool(row.get("mlsActive") or row.get("mlsPending")),
                "presets": sorted(k for k, x in (row.get("presets") or {}).items() if x),
                "distress": active_distressors(row.get("distressors")), "saved": bool(row.get("saved"))}
            if n % 50 == 0:
                _save(rpath, rescued)
        _save(rpath, rescued)
        print("  found %d of %d" % (sum(1 for f in miss if rescued.get(f["camano"])), len(miss)))

    sids = [f["sid"] for f in screen if f.get("sid") and f["verdict"] in MARKETABLE]
    sids += [r["sid"] for r in rescued.values() if r]
    todo = [s for s in dict.fromkeys(sids) if s not in cache]
    if args.limit:
        todo = todo[:args.limit]
    print("hydrate: %d to fetch, %d cached" % (len(todo), len(cache)))
    errs = 0
    for n, sid in enumerate(todo, 1):
        try:
            cache[sid] = flatten_detail(sm.get_detail(sid))
        except Exception as e:  # noqa: BLE001
            errs += 1
            print("  %s: %s" % (sid, str(e)[:100]))
            if errs > 25 and errs > n * 0.2:
                _save(path, cache)
                raise SystemExit("hydrate: too many errors (%d of %d), stopping" % (errs, n))
        if n % 50 == 0:
            _save(path, cache)
            print("  %d / %d" % (n, len(todo)), flush=True)
    _save(path, cache)
    print("hydrate: %d details cached, %d errors" % (len(cache), errs))
    return 0


# ------------------------------------------------------------------ score

DECEASED_KEYS = {"is_probate_properties", "is_pre_probate", "is_obituary_property",
                 "is_heirs_property", "is_inheritance_property", "is_estate_sales"}
LIEN_KEYS = {"is_other_lien", "is_judgment_liens", "is_federal_lien", "is_state_tax",
             "is_hoa_lien", "is_final_judgment", "is_lis_pendens", "is_court_order"}
TAX_KEYS = {"is_recent_deliquent"}

SEGMENTS = [   # priority order: a lot's primary segment is the first that fits
    ("tax", "Tax delinquent"),
    ("estate", "Estate, heirs or deceased owner"),
    ("oos", "Out-of-state owner"),
    ("liens", "Liens or judgments"),
    ("longhold", "Held 20+ years"),
    ("ooc", "Out-of-county owner"),
    ("multi", "Holds 3+ lots in the window"),
    ("side", "Neighbor side lot"),
    ("local", "Local owner"),
]


def motivation(f: dict, det: dict | None, county_zips: set, as_of: dt.date | None = None) -> dict:
    as_of = as_of or today()
    det = det or {}
    m = mail_parts(det.get("mail"))
    dist = set(det.get("distress") or f.get("distress") or [])
    presets = set(det.get("presets") or f.get("presets") or [])
    tags = set()
    pts = 0
    if det.get("tax_delinquent") or det.get("tax_delinquent_year") or dist & TAX_KEYS \
            or "tax_lien" in presets:
        tags.add("tax")
        pts += 20
    if dist & DECEASED_KEYS or f["owner_cat"] == "trust_estate" or det.get("obituaries"):
        tags.add("estate")
        pts += 20
    if m["state"] and m["state"] != "OH":
        tags.add("oos")
        pts += 25
    elif m["zip"] and m["zip"] not in county_zips:
        tags.add("ooc")
        pts += 12
    if dist & LIEN_KEYS:
        tags.add("liens")
        pts += 10
    sold = det.get("sale_date") or f.get("last_sale_date")
    held = None
    if sold:
        try:
            held = (as_of - dt.date.fromisoformat(str(sold)[:10])).days / 365.25
        except ValueError:
            held = None
    if held is not None:
        if held >= 20:
            tags.add("longhold")
            pts += 15
        elif held >= 10:
            pts += 8
        elif held < RECENT_BUY_YEARS:
            pts -= 15
    if f.get("owner_lots_in_window", 1) >= 3:
        tags.add("multi")
        pts += 5
    if f.get("side_lot_of"):
        tags.add("side")
    if "is_senior_homeowners" in dist:
        pts += 5
    if "free_clear" in presets:
        pts += 3
    if f["owner_cat"] == "bank":
        pts += 10
    seg = next((label for key, label in SEGMENTS if key in tags), "Local owner")
    if not tags:
        tags.add("local")
    return {"motivation": max(0, min(100, pts)), "segment": seg,
            "segment_tags": [label for key, label in SEGMENTS if key in tags],
            "years_held": None if held is None else round(held, 1),
            "mail_street": m["street"], "mail_city": m["city"], "mail_state": m["state"],
            "mail_zip": m["zip"], "recent_buy": held is not None and held < RECENT_BUY_YEARS}


STRONG_TAGS = {"Tax delinquent", "Estate, heirs or deceased owner", "Out-of-state owner",
               "Liens or judgments"}


def tier(f: dict) -> str:
    """A: a solid lot with a strong reason to sell. B: a decent lot with any
    reason. C: everything else marketable. Rules, not a blended cutoff: on
    Union's 2026-11-05 run buildability topped out near 82 and motivation near
    35, so a blended threshold of 70 put nobody in A."""
    strong = bool(STRONG_TAGS & set(f.get("segment_tags") or []))
    if f["build_score"] >= 65 and (strong or f["motivation"] >= 30):
        return "A"
    if f["build_score"] >= 55 and f["motivation"] >= 12:
        return "B"
    return "C"


def phase_score(c: dict, args) -> int:
    screen = _load(os.path.join(out_dir(c), "screen.json"))
    det = _load(os.path.join(out_dir(c), "details.json"), {})
    rescued = _load(os.path.join(out_dir(c), "rescued.json"), {})
    for f in screen:
        r = rescued.get(f["camano"])
        if r and not f.get("sid"):
            f.update(r)
            f["sid_via"] = "viewport"
    county_zips = {f["zip"] for f in screen if f.get("zip")}
    for p in _layer(c, "parcels"):
        z = zip5((p.get("attributes") or {}).get("ZipCode"))
        if z:
            county_zips.add(z)
    scored, mismatch, reclass = [], 0, 0
    for f in screen:
        if f["verdict"] not in MARKETABLE:
            continue
        d = det.get(f.get("sid") or "")
        if d and d.get("apn") and digits(d["apn"]) != digits(f["camano"]):
            # The point fell in this parcel but SiftMap's record is another one.
            mismatch += 1
            f["sid_note"] = "SiftMap apn %s is not this parcel" % d["apn"]
            f["sid"], d = None, None
        if d:
            # The county and SiftMap can name different owners (a transfer, or
            # a common area the county still carries under the developer).
            # Either one being an HOA, a public body or a builder ends it.
            cat2 = owner_category(d.get("owner_name"))
            if cat2 in PUBLIC_CATS or cat2 == "builder":
                reclass += 1
                f["verdict"] = "BUILDER INVENTORY" if cat2 == "builder" else "PUBLIC OR COMMON"
                f["hard"] = list(f["hard"]) + ["DataSift owner %s is a %s" % (d.get("owner_name"), CAT_LABEL[cat2])]
                continue
        use = str(f.get("sm_use") or "")
        if use and "VACANT" not in use.upper():
            # SiftMap thinks a structure is there; the county has not caught up,
            # or SiftMap is wrong. Either way it is not a lot to mail as buildable.
            note = "DataSift lists it as %s: confirm it is still vacant" % use
            if note not in f["flags"]:
                f["flags"] = list(f["flags"]) + [note]
            if f["verdict"] != "VERIFY":
                f["verdict"] = "VERIFY"
        f.update(motivation(f, d, county_zips))
        if d:
            f["owner_mail_name"] = d.get("owner_name")
            f["tax_delinquent_year"] = d.get("tax_delinquent_year")
            f["dm_lot_type"] = d.get("lot_type")
            if d.get("assessed_land"):
                # Ohio assesses at 35% of appraised value.
                f["land_value_est"] = round(float(d["assessed_land"]) / 0.35)
        f["priority"] = round(0.55 * f["build_score"] + 0.45 * f["motivation"], 1)
        f["tier"] = tier(f)
        f["mailable"] = bool(f.get("mail_street")) and not f.get("mls_active") and not f.get("recent_buy")
        scored.append(f)
    scored.sort(key=lambda f: (f["tier"], -f["priority"]))
    _save(os.path.join(out_dir(c), "scored.json"), scored)
    _save(os.path.join(out_dir(c), "screen.json"), screen)   # carries the reclassified verdicts
    print("score: %d marketable lots, %d mailable, %d SiftMap ids rejected on apn, "
          "%d reclassified on the DataSift owner"
          % (len(scored), sum(f["mailable"] for f in scored), mismatch, reclass))
    print("       tiers %s" % _counter(f["tier"] for f in scored if f["mailable"]))
    print("       segments %s" % _counter(f["segment"] for f in scored if f["mailable"]))
    return 0


# ------------------------------------------------------------------ comps

def lot_comps(c: dict, screen_all: list | None = None, as_of: dt.date | None = None) -> tuple[list, dict]:
    """Valid single-parcel sales of vacant-class parcels in the window over the
    last COMP_MONTHS, minus any that sold with a house on them."""
    as_of = as_of or today()
    layer = _layer(c, "parcels")
    proj = _proj_for(layer)
    parcels = {(p["attributes"].get("camano") or "").strip(): p for p in layer}

    def acres(p) -> float:
        # Platted lots carry no county Acreage; measure the polygon instead.
        a = float(p["attributes"].get("Acreage") or 0)
        if a:
            return a
        g = esri_to_shape(p.get("geometry"))
        return round(proj.geom(g).area / 43560.0, 3) if g is not None else 0.0

    cut = as_of - dt.timedelta(days=COMP_MONTHS * 30.44)
    comps = []
    for ft in _layer(c, "sales"):
        a = ft["attributes"]
        if str(a.get("parcelClass")) not in VACANT_CLASSES or a.get("isValidSale") != 1:
            continue
        if int(a.get("NumberofParcelsInSale") or 1) != 1 or not a.get("saleDate"):
            continue
        sold = dt.date.fromtimestamp(a["saleDate"] / 1000)
        price = float(a.get("salePrice") or 0)
        land = float(a.get("landValue") or 0)
        if sold < cut or price < 5000:
            continue
        if price >= BUILT_SALE_FLOOR and land and price >= BUILT_SALE_RATIO * land:
            continue
        pf = parcels.get((a.get("CAMANO") or "").strip())
        if pf is None:
            continue
        ac = acres(pf)
        p = pf["attributes"]
        if not ACRES_MIN <= ac <= ACRES_MAX:
            continue
        comps.append({"camano": a.get("CAMANO"), "sold": sold.isoformat(), "price": price,
                      "acres": ac, "per_acre": round(price / ac) if ac else None,
                      "township": p.get("township") or "", "address": (p.get("Address") or "").strip(),
                      "buyer": (a.get("ownerName") or "").strip(), "deed": a.get("typeOfSale"),
                      "land_value": land, "buyer_cat": owner_category(a.get("ownerName"))})
    by: dict = {}
    for x in comps:
        by.setdefault(x["township"] or "UNKNOWN", []).append(x)
    by["COUNTY"] = comps

    def band(xs):
        ps = sorted(x["price"] for x in xs)
        pa = sorted(x["per_acre"] for x in xs if x["per_acre"])
        q = statistics.quantiles(ps, n=4) if len(ps) >= 4 else [ps[0], ps[len(ps) // 2], ps[-1]] if ps else [0, 0, 0]
        return {"n": len(xs), "median": statistics.median(ps) if ps else None,
                "p25": q[0], "p75": q[-1], "median_per_acre": statistics.median(pa) if pa else None}

    for x in comps:
        by.setdefault("COUNTY " + acre_band(x["acres"]), []).append(x)
    return comps, {k: band(v) for k, v in by.items()}


def acre_band(ac: float) -> str:
    """Lots price per LOT inside a size class, not per acre: a 0.25 acre platted
    lot and a 1.5 acre road-front lot are different products."""
    return "0.20-0.49 ac" if ac < 0.50 else "0.50-0.99 ac" if ac < 1.00 else "1.00+ ac"


SUBDIVISION_MARKET_MIN = 20   # a township with this many lot sales is a new-subdivision market
CAUV_CLASSES = {"110"}        # farm-use valuation: land value $29-$400 on Union house-size lots
LAND_VALUE_FLOOR = 5000


def market_ratios(comps: list) -> dict:
    """Median sale price over auditor land value: per township with 5+ sales,
    and `_rural` for the townships that are not subdivision markets. Measured
    2026-11-05: Jerome and Washington/Dublin lots sell at 1.20x land value,
    everywhere else at 1.95x, so one county ratio under-prices every rural lot."""
    counts: dict = {}
    for x in comps:
        counts[x["township"]] = counts.get(x["township"], 0) + 1
    by: dict = {}
    rural = []
    for x in comps:
        if not x.get("land_value"):
            continue
        r = x["price"] / x["land_value"]
        by.setdefault(x["township"], []).append(r)
        if counts[x["township"]] < SUBDIVISION_MARKET_MIN:
            rural.append(r)
    out = {t: round(statistics.median(v), 2) for t, v in by.items() if len(v) >= 5}
    out["_rural"] = round(statistics.median(rural), 2) if len(rural) >= 10 else None
    out["_counts"] = counts
    return out


def comp_reference(f: dict, comps: list, min_n: int = 5,
                   ratios: dict | None = None) -> tuple[float | None, str]:
    """Value for this lot, in order:
      1. the median of same-township, same-size lot sales;
      2. the lot's own auditor land value times its township's sale-to-land
         ratio (the rural ratio where the township is thin), because the land
         value already carries the location. Not on CAUV farm parcels, whose
         land value is a farm-use figure, and not under LAND_VALUE_FLOOR;
      3. rural lot sales in the same size band (county band inside a
         subdivision market), then the township, then the county."""
    ratios = ratios or {}
    counts = ratios.get("_counts") or {}
    b = acre_band(f["acres"])
    tw = f.get("township") or ""
    xs = [x["price"] for x in comps if x["township"] == tw and acre_band(x["acres"]) == b]
    if len(xs) >= min_n:
        return statistics.median(xs), "%s %s (%d sales)" % (tw.title(), b, len(xs))
    lv = float(f.get("land_value_est") or f.get("land_value") or 0)
    if lv >= LAND_VALUE_FLOOR and f.get("class") not in CAUV_CLASSES:
        r, src = (ratios[tw], tw.title()) if tw in ratios else (ratios.get("_rural"), "rural")
        if r:
            return round(lv * r), "land value $%s x %.2f %s sale-to-land" % (f"{lv:,.0f}", r, src)
    suburban = counts.get(tw, 0) >= SUBDIVISION_MARKET_MIN
    for scope, pick in (
            ("rural %s" % b, lambda x: not suburban and counts.get(x["township"], 0) < SUBDIVISION_MARKET_MIN
             and acre_band(x["acres"]) == b),
            ("county %s" % b, lambda x: acre_band(x["acres"]) == b),
            (tw.title(), lambda x: x["township"] == tw),
            ("county", lambda x: True)):
        xs = [x["price"] for x in comps if pick(x)]
        if len(xs) >= min_n:
            return statistics.median(xs), "%s (%d sales)" % (scope, len(xs))
    return None, "no comps"


# ----------------------------------------------------------------- export

PLAN = {
    # segment: (channel sequence, message angle, offer approach)
    "Tax delinquent": (
        "Letter, postcard at day 21, offer letter at day 45; skip trace then text and call tier A",
        "A clean cash sale on their schedule; amounts owed on the parcel are settled at closing",
        "Offer letter with a firm number; the tax figure is in the file, so the math is known"),
    "Estate, heirs or deceased owner": (
        "Soft letter to the mailing address, then a second letter; call only after the research pass",
        "Patience and help with paperwork; we work with whoever handles the property",
        "Range, not a number, until the signer is confirmed (deep prospect before any offer)"),
    "Out-of-state owner": (
        "Letter, postcard at day 21, offer letter at day 45; skip trace then text tier A and B",
        "No trip needed: remote closing through a local title company, we order the survey",
        "Offer letter with a number off the township lot comps"),
    "Liens or judgments": (
        "Letter, then offer letter at day 30; call after skip trace",
        "Cash, as-is, title company clears what is recorded against it",
        "Offer net of recorded liens; confirm payoff at the title search"),
    "Held 20+ years": (
        "Value letter with nearby lot sales, then offer letter at day 30",
        "Lots near them have sold well recently; here is what they brought",
        "Offer letter with the comp table attached"),
    "Out-of-county owner": (
        "Letter, postcard at day 21, offer letter at day 45",
        "Easy remote sale, we handle the legwork in Union County",
        "Offer letter with a number off the township lot comps"),
    "Holds 3+ lots in the window": (
        "Personal letter, then a phone call; one conversation covers every lot",
        "A single buyer for several lots, one closing",
        "Bulk offer by phone; price each lot, then the package"),
    "Neighbor side lot": (
        "One soft letter a quarter; never a blast",
        "If the extra lot next to the house ever comes up, we are a simple buyer",
        "No number in writing; talk first"),
    "Local owner": (
        "Letter, then postcard at day 30",
        "Local cash buyer for the lot, no fees, close when it suits them",
        "Interest letter first; number on the call"),
}


def phase_export(c: dict, args) -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    d = out_dir(c)
    screen = _load(os.path.join(d, "screen.json"))
    scored = _load(os.path.join(d, "scored.json"))
    meta = _load(os.path.join(d, "screen_meta.json"), {})
    probe = _load(os.path.join(d, "probe.json"), {})
    if screen is None or scored is None:
        raise SystemExit("run --phase screen and --phase score first")
    comps, bands = lot_comps(c)
    lo_pct, hi_pct = args.offer_low, args.offer_high
    ratios = market_ratios(comps)
    for f in scored:
        ref, basis = comp_reference(f, comps, ratios=ratios)
        f["comp_median"], f["comp_basis"] = ref, basis
        if ref:
            f["offer_low"] = round(ref * lo_pct / 500) * 500
            f["offer_high"] = round(ref * hi_pct / 500) * 500

    NAVY, BORDER = "0A1130", Side(style="thin", color="C9CED8")
    wb = Workbook()
    head_font = Font(bold=True, color=NAVY)

    def sheet(title, header, rows, widths=None, money=(), pct=(), first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(header)
        for cell in ws[1]:
            cell.font = head_font
            cell.border = Border(bottom=Side(style="medium", color=NAVY))
            cell.alignment = Alignment(wrap_text=True, vertical="bottom")
        for r in rows:
            ws.append([("" if v is None else v) for v in r])
        for ci, h in enumerate(header, 1):
            col = get_column_letter(ci)
            ws.column_dimensions[col].width = (widths or {}).get(h, min(48, max(10, len(h) + 2)))
            for cell in ws[col][1:]:
                if h in money:
                    cell.number_format = '"$"#,##0'
                elif h in pct:
                    cell.number_format = "0%"
                cell.border = Border(bottom=BORDER)
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        return ws

    mail = [f for f in scored if f["mailable"]]
    v = _counter(f["verdict"] for f in screen)
    seg_n = _counter(f["segment"] for f in mail)
    overview = [
        ("Run", dt.date.today().isoformat()),
        ("County", "%s County, %s (FIPS %s)" % (c["name"], c["state"], c["fips"])),
        ("Acreage window", "%.2f to %.2f acres (county Acreage)" % (ACRES_MIN, ACRES_MAX)),
        ("County parcels", meta.get("parcels")),
        ("Vacant-class parcels in the window", len(screen)),
    ] + [("  " + k, n) for k, n in v.items()] + [
        ("Marketable lots (buildable, likely, verify)", len(scored)),
        ("Mailable now (mailing address, not listed, not bought in 2 years)", len(mail)),
    ] + [("  Tier " + k, n) for k, n in sorted(_counter(f["tier"] for f in mail).items())] + [
        ("SiftMap land rows in the county", meta.get("sm_rows")),
        ("SiftMap rows matched to a candidate parcel", meta.get("sm_on_candidates")),
        ("Lot comps used (valid vacant sales, %d months)" % COMP_MONTHS, len(comps)),
        ("County median lot sale", bands["COUNTY"].get("median")),
        ("API probe", "PASS" if probe and not probe.get("fails") else "not run or FAILED"),
    ]
    ws = sheet("Overview", ["Item", "Value"], overview, {"Item": 62, "Value": 46}, first=True)
    for cell in ws["B"][1:]:
        if isinstance(cell.value, (int, float)):
            cell.number_format = "#,##0"

    cols = ["Rank", "Tier", "Priority", "Buildability", "Motivation", "Verdict", "Segment",
            "Also", "Flags", "Parcel (CAMA)", "Property", "City", "Township", "Acres",
            "Platted lot", "Subdivision", "Frontage ft", "Width ft", "Flood %", "Sewer proxy",
            "Steep share", "Sits low ft", "Relief ft", "Zoning", "Owner (county)", "Owner (mailing)", "Mail street", "Mail city",
            "Mail state", "Mail zip", "Owner type", "Years held", "Tax delinquent year",
            "Distress (DataSift)", "Side lot of", "Owner lots in window", "Land value est",
            "Lot comp median", "Offer low", "Offer high", "Comp basis", "DataSift id",
            "Property card"]
    rows = []
    for n, f in enumerate(mail, 1):
        rows.append([n, f["tier"], f["priority"], f["build_score"], f["motivation"], f["verdict"],
                     f["segment"], "; ".join(s for s in f["segment_tags"] if s != f["segment"]),
                     "; ".join(f["flags"]), f["camano"], f["address"], f["city"], f["township"],
                     f["acres"], "yes" if f["platted"] else "", f["subdivision"], f["frontage_ft"],
                     f["width_ft"], f["flood_pct"], f["sewer"], f.get("steep_share"),
                     f.get("sits_low_ft"), f.get("relief_ft"), f.get("zoning", ""), f["owner"],
                     f.get("owner_mail_name"), f["mail_street"], f["mail_city"], f["mail_state"],
                     f["mail_zip"], CAT_LABEL[f["owner_cat"]], f["years_held"],
                     f.get("tax_delinquent_year"), ", ".join(x.replace("is_", "") for x in f.get("distress") or []),
                     f["side_lot_of"], f["owner_lots_in_window"], f.get("land_value_est"),
                     f.get("comp_median"), f.get("offer_low"), f.get("offer_high"),
                     f.get("comp_basis"), f.get("sid"), f["card"]])
    sheet("Mail list", cols, rows, {"Flags": 40, "Property": 28, "Owner (county)": 30,
                                    "Owner (mailing)": 26, "Mail street": 26, "Segment": 24},
          money=("Land value est", "Lot comp median", "Offer low", "Offer high"),
          pct=("Flood %", "Steep share"))

    plan_rows = []
    for key, label in SEGMENTS:
        mine = [f for f in mail if f["segment"] == label]
        ch, angle, offer = PLAN[label]
        t = _counter(f["tier"] for f in mine)
        plan_rows.append([label, len(mine), t.get("A", 0), t.get("B", 0), t.get("C", 0),
                          ch, angle, offer])
    sheet("Marketing plan", ["Segment", "Lots", "Tier A", "Tier B", "Tier C", "Channel and cadence",
                             "Message angle (never name the situation)", "Offer approach"],
          plan_rows, {"Segment": 30, "Channel and cadence": 52,
                      "Message angle (never name the situation)": 52, "Offer approach": 44})
    ws = wb["Marketing plan"]
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    notes = [
        "", "How to run it",
        "1. Mail is the primary channel for land: nobody lives on a vacant lot, so the owner's mailing address is the only reach that works on day one.",
        "2. Pull the mailable lots into DataSift (--phase pull) into one list, tagged by tier and segment; DataSift's skip trace runs on the pull.",
        "3. Tier A gets every touch plus text and call after skip trace; tier B gets the mail sequence; tier C gets the interest letter and the quarterly drip.",
        "4. Copy never names the situation (taxes, estate, liens): the owner should feel found, not targeted. Mention the lot by street, never by parcel trouble.",
        "5. Offers come off the township lot comps on the Lot comps tab at the offer band set on this run (%d%% to %d%% of the comp); move the band with --offer-low/--offer-high." % (lo_pct * 100, hi_pct * 100),
        "6. Exit: the Lot buyers tab lists who bought vacant lots in the window over the last %d months. Builders on the Builder inventory tab are buyers, not sellers." % COMP_MONTHS,
        "7. Before any contract: township zoning frontage and lot-size minimum, Union County Health District sign-off where the sewer proxy is not 'sewer likely', and a title search.",
    ]
    for line in notes:
        ws.append([line])

    nb = [f for f in screen if f["verdict"] not in MARKETABLE]
    sheet("Not marketable", ["Verdict", "Reasons", "Parcel (CAMA)", "Property", "Township", "Acres",
                             "Owner", "Owner type", "Frontage ft", "Width ft", "Flood %", "Build score"],
          [[f["verdict"], "; ".join(f["hard"]), f["camano"], f["address"], f["township"], f["acres"],
            f["owner"], CAT_LABEL[f["owner_cat"]], f["frontage_ft"], f["width_ft"], f["flood_pct"],
            f["build_score"]] for f in sorted(nb, key=lambda f: f["verdict"])],
          {"Reasons": 60, "Owner": 34, "Property": 28}, pct=("Flood %",))

    inv: dict = {}
    for f in screen:
        if f["verdict"] == "BUILDER INVENTORY":
            g = inv.setdefault(f["owner"], {"n": 0, "acres": 0.0, "subs": set()})
            g["n"] += 1
            g["acres"] += f["acres"]
            if f["subdivision"]:
                g["subs"].add(f["subdivision"])
    sheet("Builder inventory", ["Builder or developer", "Lots in window", "Acres", "Subdivisions"],
          [[k, g["n"], round(g["acres"], 2), ", ".join(sorted(g["subs"]))[:250]]
           for k, g in sorted(inv.items(), key=lambda kv: -kv[1]["n"])],
          {"Builder or developer": 44, "Subdivisions": 70})

    band_rows = [[k, b["n"], b["median"], b["p25"], b["p75"], b["median_per_acre"]]
                 for k, b in sorted(bands.items(), key=lambda kv: -kv[1]["n"])]
    ws = sheet("Lot comps", ["Township", "Sales", "Median", "25th pct", "75th pct", "Median per acre"],
               band_rows, {"Township": 22}, money=("Median", "25th pct", "75th pct", "Median per acre"))
    ws.append([])
    ws.append(["Sold", "Price", "Acres", "Per acre", "Township", "Property", "Buyer", "Buyer type", "Deed"])
    for x in sorted(comps, key=lambda x: x["sold"], reverse=True):
        ws.append([x["sold"], x["price"], x["acres"], x["per_acre"], x["township"], x["address"],
                   x["buyer"], CAT_LABEL[x["buyer_cat"]], x["deed"]])

    buyers: dict = {}
    for x in comps:
        if x["buyer_cat"] in ("person", "trust_estate") or not x["buyer"]:
            continue
        b = buyers.setdefault(x["buyer"], {"n": 0, "spend": 0.0, "last": "", "cat": x["buyer_cat"],
                                           "towns": set()})
        b["n"] += 1
        b["spend"] += x["price"]
        b["last"] = max(b["last"], x["sold"])
        b["towns"].add(x["township"])
    sheet("Lot buyers", ["Buyer", "Type", "Lots bought", "Total spend", "Average", "Last buy", "Townships"],
          [[k, CAT_LABEL[b["cat"]], b["n"], b["spend"], round(b["spend"] / b["n"]), b["last"],
            ", ".join(sorted(t for t in b["towns"] if t))]
           for k, b in sorted(buyers.items(), key=lambda kv: -kv[1]["n"])],
          {"Buyer": 44, "Townships": 40}, money=("Total spend", "Average"))

    src = [
        ("Parcel truth", "Union County GIS parcel layer (class, Acreage, geometry, plats via the Lots layer)"),
        ("Roads", "Union County road centerlines (View Road Range) and right-of-way lines"),
        ("Flood", "Union County 100-year floodplain and floodway layers"),
        ("Zoning", "Union County zoning layer (Leesburg, Liberty, Marysville, Millcreek, Paris, Taylor only; "
                   "every other township is unmapped and scored as not disqualifying)"),
        ("Sales and land value", "Union County most-recent-sale-per-parcel layer"),
        ("Owner and distress", "SiftMap (DataSift) search rows and property detail, Open API key"),
        ("Land filter", "type_land: true, proven by count delta on every --phase probe run"),
        ("Sewer", "No sewer layer is published. Proxy: median lot size of houses within %d ft "
                  "(under %.2f ac = sewer likely, %.1f ac or more = septic area). Verify before contract."
         % (SEWER_RADIUS_FT, SEWER_SMALL_LOT_AC, SEPTIC_AREA_AC)),
        ("Septic", "USDA rates nearly every Union soil Very limited for conventional septic fields, "
                   "so an unsewered lot needs an engineered system and health district approval"),
        ("Built since reappraisal", "Vacant-class parcels with a dwelling record, a sale at %.0fx land value "
                                    "and $%s or more, or a permit in the last 3 years"
         % (BUILT_SALE_RATIO, f"{BUILT_SALE_FLOOR:,}")),
        ("Topography", "USGS 3DEP 1 m lidar: share of the lot steeper than a 15%% grade (fail at %d%%, "
                       "flag at %d%%), relief, and how far the lot's median elevation sits under a "
                       "%d ft ring around it (flag at %.0f ft, strong at %.0f ft). Union is flat, so "
                       "sitting low (drainage, fill) matters more than slope. County 2 ft contours "
                       "and spot elevations are in its topography service for a visual check."
         % (STEEP_FAIL * 100, STEEP_FLAG * 100, RING_FT, LOW_FLAG_FT, LOW_BAD_FT)),
        ("AVM", "Not used: SiftMap's estimated value is meaningless on land"),
        ("Thresholds", "landlocked %d ft, frontage %d ft (minimum %d), strip under %d ft wide, "
                       "floodplain fail %d%%, flag %d%%" % (LANDLOCKED_FT, FRONTAGE_OK_FT, FRONTAGE_MIN_FT,
                                                            WIDTH_FAIL_FT, FLOOD_FAIL * 100, FLOOD_FLAG * 100)),
        ("Class meaning, measured", "; ".join("%s: %s" % kv for kv in list(meta.get("class_x_siftmap", {}).items())[:12])),
    ]
    sheet("Sources and caveats", ["Topic", "Detail"], src, {"Topic": 26, "Detail": 120})
    ws = wb["Sources and caveats"]
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    name = "%s_Buildable_Lots_%s.xlsx" % (c["label"].replace(" ", "_"), dt.date.today().strftime("%Y%m%d"))
    path = os.path.join(d, name)
    tmp = os.path.join(d, "_PENDING_" + name)
    wb.save(tmp)
    os.replace(tmp, path)
    with open(os.path.join(d, "mail_list.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    _save(os.path.join(d, "comps.json"), {"bands": bands, "comps": comps})
    print("export: %s (%d mailable lots, %d comps)" % (path, len(mail), len(comps)))
    return 0


# ------------------------------------------------------------------- pull

def pull_plan(scored: list, label: str) -> dict:
    """Tier -> dataflik ids for the mailable lots, plus the list and tags."""
    plan = {"list": "%s - Buildable Lots" % label, "tiers": {}}
    for f in scored:
        if f.get("mailable") and f.get("sid") and not f.get("saved"):
            plan["tiers"].setdefault(f["tier"], []).append(f["sid"])
    return plan


def phase_pull(c: dict, args) -> int:
    scored = _load(os.path.join(out_dir(c), "scored.json"))
    if scored is None:
        raise SystemExit("run --phase score first")
    plan = pull_plan(scored, c["label"])
    total = sum(len(v) for v in plan["tiers"].values())
    print("pull: %d ids into list '%s' by tier %s"
          % (total, plan["list"], {k: len(v) for k, v in sorted(plan["tiers"].items())}))
    _save(os.path.join(out_dir(c), "pull_plan.json"), plan)
    if not args.commit:
        print("  dry run: nothing sent. --commit --key-env <VAR> sends it to the account that key belongs to")
        return 0
    key = os.environ.get(args.key_env or "", "")
    if not key:
        raise SystemExit("--commit needs --key-env naming an env var that holds the TARGET account's Open API key")
    _load_env()
    from siftmap_pull import WORLD
    from siftmap_standalone import SiftMapClient
    m = SiftMapClient(key, min_interval=0.5)
    for t, ids in sorted(plan["tiers"].items()):
        for i in range(0, len(ids), 250):
            chunk = ids[i:i + 250]
            r = m._request("/properties/add-properties-by-query/", "POST", {
                "auto_add_enabled": False, "lists": [plan["list"]],
                "tags": ["Buildable Lot", "Lot Tier %s" % t], "replace_owners": False,
                "query": {"result_index": 1, "with_boundaries": False, "filters": {},
                          "addresses": [{"search": s, "type": "ids"} for s in chunk],
                          "polygon": WORLD}}, timeout=240)
            print("  tier %s chunk %d: %d ids -> %s" % (t, i // 250 + 1, len(chunk), str(r)[:80]))
    print("  sent; adds are async, read the list count back before trusting it")
    return 0


# ------------------------------------------------------------------- main

PHASES = {"probe": phase_probe, "county": phase_county, "sweep": phase_sweep,
          "topo": phase_topo, "screen": phase_screen, "hydrate": phase_hydrate, "score": phase_score,
          "export": phase_export, "pull": phase_pull}


def main(argv=None) -> int:
    global ACRES_MIN, ACRES_MAX
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", required=True, choices=sorted(PHASES) + ["all"])
    ap.add_argument("--fips", default="39159", choices=sorted(COUNTIES))
    ap.add_argument("--acres-min", type=float, default=ACRES_MIN)
    ap.add_argument("--acres-max", type=float, default=ACRES_MAX)
    ap.add_argument("--refresh", action="store_true", help="re-pull cached county layers")
    ap.add_argument("--limit", type=int, default=0, help="hydrate at most N details")
    ap.add_argument("--offer-low", type=float, default=0.50, help="offer band low, share of the lot comp")
    ap.add_argument("--offer-high", type=float, default=0.65, help="offer band high, share of the lot comp")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--key-env", help="env var holding the target account's Open API key (pull)")
    args = ap.parse_args(argv)
    ACRES_MIN, ACRES_MAX = args.acres_min, args.acres_max
    c = COUNTIES[args.fips]
    if args.phase == "all":
        for p in ("county", "sweep", "topo", "screen", "hydrate", "score", "export"):
            print("\n#### %s" % p)
            rc = PHASES[p](c, args)
            if rc:
                return rc
        return 0
    return PHASES[args.phase](c, args)


if __name__ == "__main__":
    raise SystemExit(main())
