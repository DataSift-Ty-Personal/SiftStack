"""Source profiles: which header in a file feeds which canonical field.

A profile is detected from a header fingerprint, then every canonical field is
mapped by the profile's own header list first and a broad synonym list second.
`--map field=Header` overrides any of it. If no field that can carry a
classification signal maps, ingest stops and prints the headers it saw,
because a file that silently classifies nothing reads like an empty market.

Profiles we can specify from real files: fa (First American), datasift
(REISift export), regrid (published schema). loopnet_crexi and reonomy key on
the vendor's own property type. Anything else (PropStream, ATTOM, a raw county
export, an ArcGIS layer) runs as generic, and its codes are learned per county.
"""
from __future__ import annotations

import re

from . import schema


class ProfileError(RuntimeError):
    pass


def norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


# FA dictionary lookups (code -> plain label). The numbers are the vendor's
# codes; the labels are ordinary grade and condition words.
FA_QUALITY = {1: "A", 2: "A-", 3: "A+", 4: "B", 5: "B-", 6: "B+", 7: "C", 8: "C-", 9: "C+",
              10: "D", 11: "D-", 12: "D+", 13: "E", 14: "E-", 15: "E+"}
FA_CONDITION = {1: "Excellent", 2: "Fair", 3: "Good", 4: "Poor", 5: "Unsound", 6: "Average",
                7: "Very Good"}
FA_CONSTRUCTION = {1: "Adobe", 2: "Brick", 3: "Concrete", 4: "Concrete Block", 5: "Dome",
                   6: "Frame", 7: "Heavy", 8: "Light", 9: "Log", 10: "Manufactured", 11: "Other",
                   12: "Masonry", 13: "Metal", 14: "Steel", 15: "Stone", 16: "Tilt-up", 17: "Wood",
                   18: "Mixed", 19: "Bamboo"}


def _code_lookup(table):
    def f(v):
        n = schema.num(v)
        return table.get(int(n)) if n else None
    return f


def _fa_stories(v):
    n = schema.num(v)
    return round(n / 100.0, 2) if n else None


def _flag(v):
    s = (schema.text(v) or "").upper()
    if not s or s in ("N", "NO", "0", "FALSE", "F"):
        return None
    return "Y"


def _fa_code(v):
    s = schema.text(v)
    if not s:
        return None
    s = s.split(".")[0] if re.match(r"^\d+\.0+$", s) else s
    return s.zfill(4) if s.isdigit() else s.upper()


# ---- generic synonyms (normalized header names) -----------------------------
GENERIC = {
    "apn": ["apn", "parcelnumber", "parcelid", "parcelno", "parcelnum", "parcelnumb", "parcel", "pin",
            "parid", "assessorparcelnumber", "apnformatted", "folio", "folionumber", "taxid",
            "accountnumber", "account", "mapparcel", "gisparcelid", "parcelidentifier", "propertyid"],
    "apn_seq": ["apnseqnbr", "apnseq"],
    "fips": ["fips", "fipscode", "countyfips", "stcntyfips", "geoid"],
    "county": ["county", "countyname", "propertycounty", "situscounty", "cntyname"],
    "situs_address": ["situsfullstreetaddress", "situsaddress", "siteaddress", "siteaddr",
                      "propertyaddress", "address", "propaddress", "locationaddress", "fulladdress",
                      "streetaddress", "physicaladdress", "propertylocation", "location", "situs", "addr"],
    "situs_city": ["situscity", "sitecity", "propertycity", "city", "scity", "sitepostalcity",
                   "cityname", "physicalcity", "municipality"],
    "situs_state": ["situsstate", "sitestate", "propertystate", "state", "state2", "stateabbr"],
    "situs_zip": ["situszip5", "situszip", "sitezip", "sitepostalzip", "propertyzip5", "propertyzip",
                  "zip", "zipcode", "zip5", "szip", "szip5", "postalcode", "sitezipcode"],
    "lat": ["situslatitude", "latitude", "lat", "centroidlat", "ycoord", "pointy", "y"],
    "lon": ["situslongitude", "longitude", "lon", "lng", "long", "centroidlon", "xcoord", "pointx", "x"],
    "use_code": ["landusecode", "usecode", "propertyusecode", "luc", "landusecd", "usecd", "propuse",
                 "propertyuse", "classcd", "stdlandusecode", "standardizedlandusecode", "landusecodes"],
    "use_desc": ["landusedesc", "landusedescription", "usedesc", "usedescription", "propertyusedesc",
                 "propertyusedescription", "landuse", "landusetype", "lucdesc", "classdesc",
                 "usecodedescription", "propusedesc", "occupancy", "occupancydescription",
                 "buildingusedescription", "propertyusestandardized", "propertyusegroup"],
    "county_use_code": ["countylandusecode", "countyusecode", "localusecode"],
    "county_use_desc": ["countylandusedesc", "countylandusedescription", "localusedesc"],
    "property_class": ["propertyclassid", "propertyclass", "classcode", "propclass", "taxclass",
                       "parcelclass", "landclass", "class"],
    "zoning": ["zoning", "zoningcode", "zone", "zonecode", "landzoning", "zoningclass"],
    "zoning_desc": ["zoningdesc", "zoningdescription", "zonedesc", "zonedescription"],
    "structure_type": ["structuretype", "buildingtype", "bldgtype", "improvementtype", "structuredesc"],
    "vendor_type": ["propertytype", "proptype", "assettype", "assetclass", "assetcategory",
                    "propertycategory"],
    "vendor_subtype": ["propertysubtype", "subtype", "propertysubcategory", "assetsubtype",
                       "secondarytype"],
    "lbcs_code": ["lbcsfunction", "lbcsactivity", "lbcsstructure"],
    "lbcs_desc": ["lbcsfunctiondesc", "lbcsactivitydesc", "lbcsstructuredesc"],
    "legal_desc": ["legaldescription", "legaldesc", "legal", "legal1"],
    "subdivision": ["subdivisionname", "subdivision", "subdiv"],
    "business_name": ["businessname", "tenantname", "tenant", "tenants", "dba", "companyname",
                      "occupant", "business"],
    "building_sqft": ["sumbuildingsqft", "buildingarea", "buildingsqft", "bldgsqft", "buildingsize",
                      "buildingsf", "grossbuildingarea", "gba", "squarefeet", "sqft", "livingarea",
                      "livingareasqft", "totalsqft", "improvementsqft", "rentablebuildingarea", "rba",
                      "grosssqft", "finishedsqft", "bldgarea", "totalbuildingarea",
                      "buildingsquarefeet", "recrdareano"],
    "building_count": ["sumbuildingsnbr", "buildingcount", "numbuildings", "nbrbuildings", "structno",
                       "numberofbuildings", "buildings"],
    "stories": ["stories", "numstories", "storeys", "nostories", "floors", "numberofstories",
                "storycount", "numfloors"],
    "year_built": ["yearbuilt", "yrbuilt", "yearbuiltactual", "builtyear", "yearconstructed",
                   "effectiveyearbuilt", "year"],
    "lot_acres": ["lotsizeacres", "acres", "taxacres", "gisacres", "lotacres", "acreage", "llgisacre", "gisacre",
                  "deededacres", "calcacres", "totalacres", "lotsize", "lotsizeac"],
    "lot_sqft": ["lotsizesqft", "lotsqft", "lotsizesquarefeet", "landsqft", "lotarea", "landsf",
                 "lotsf", "landarea"],
    "units_res": ["sumresidentialunits", "residentialunits", "numunits", "numberofunits", "unitcount",
                  "totalunits", "nounits", "livingunits", "dwellingunits", "units"],
    "units_comm": ["sumcommercialunits", "commercialunits"],
    "quality": ["quality", "qualitycode", "buildingquality", "constructionquality", "bldgquality",
                "grade", "buildinggrade"],
    "condition": ["condition", "conditioncode", "buildingcondition", "bldgcondition"],
    "construction": ["constructiontype", "construction", "constructionclass"],
    "mv_total": ["markettotalvalue", "totalmarketvalue", "marketvalue", "markettotal", "appraisedvalue",
                 "totalappraisedvalue", "appraisedtotal", "apprtotal", "totalvalue", "parval",
                 "justvalue", "fairmarketvalue", "mkttotval", "totappr", "totalapprvalue",
                 "markettot"],
    "mv_land": ["marketvalueland", "marketlandvalue", "landvalue", "landval", "marketland", "appraisedland", "apprland",
                "landmarketvalue"],
    "mv_impr": ["marketvalueimprovement", "marketimpvalue", "marketimprovementvalue", "improvementvalue", "improvval", "bldgvalue", "buildingvalue",
                "apprimpr", "improvementsvalue", "improvementmarketvalue"],
    "assessed_total": ["assdtotalvalue", "assessedvalue", "totalassessedvalue", "assessedtotal",
                       "asmttotal", "taxablevalue", "netassessed"],
    "vendor_avm": ["estimatedvalue", "estvalue", "avm", "estimatedmarketvalue", "valueestimate"],
    "sale_price": ["currentsalesprice", "saleprice", "lastsaleprice", "lastsaleamount", "saleamount",
                   "saleamnt", "salesprice", "t1saleamount", "recentsaleprice", "lastsoldprice",
                   "deedamount", "considerationamount", "soldprice"],
    "sale_date": ["currentsalerecordingdate", "saledate", "lastsaledate", "lastsold",
                  "lastsalerecordingdate", "recordingdate", "saledt", "t1date", "deeddate", "solddate",
                  "lastsolddate", "transferdate", "datesold"],
    "owner1": ["ownername1full", "ownername", "owner", "owner1", "owner1name", "owner1fullname",
               "ownerfullname", "taxpayername", "ownernames", "currentowner", "ownercurrent",
               "recordowner", "mailname1", "reportedowner", "name"],
    "owner2": ["ownername2full", "owner2", "owner2name", "owner2fullname", "coowner", "mailname2"],
    "owner3": ["ownername3full", "owner3", "owner3name"],
    "owner4": ["ownername4full", "owner4", "owner4name"],
    "owner_first": ["owner1firstname", "ownerfirstname", "firstname", "ownfrst", "owner1first"],
    "owner_last": ["owner1lastname", "ownerlastname", "lastname", "ownlast", "owner1last"],
    "owner_is_corp": ["owner1corpind", "corporateowned", "corpind", "iscorporate", "ownercorporate"],
    "mail_address": ["mailingfullstreetaddress", "mailingaddress", "mailaddress", "mailadd", "mailaddr",
                     "mailaddr1", "owneraddress", "owneraddr", "taxpayeraddress", "mailaddress1",
                     "mailingaddress1", "owneraddress1", "owneradd1"],
    "mail_city": ["mailingcity", "mailcity", "ownercity", "taxpayercity"],
    "mail_state": ["mailingstate", "mailstate", "mailstate2", "ownerstate", "taxpayerstate"],
    "mail_zip": ["mailingzip5", "mailingzip", "mailzip", "ownerzip", "taxpayerzip", "mailzip5"],
    "geometry": ["parcelboundary", "geometry", "wkt", "geom", "shape", "thegeom", "geometrywkt",
                 "wktgeometry", "geometrywkt4326"],
    "situs_house": ["situshousenbr", "housenumber", "houseno", "addrnumber", "stnum", "housenum"],
    "situs_street": ["situsstreet", "streetname", "stname"],
}

# headers whose unit (acres vs square feet) has to be sniffed from the values
AMBIGUOUS_LOT = {"lotsize", "lotarea", "landarea"}


class Profile:
    def __init__(self, name, label, confidence, fingerprint, min_score, fields, use_system,
                 use_national, transforms=None, cre_class_ids=()):
        self.name = name
        self.label = label
        self.confidence = confidence
        self.fingerprint = fingerprint
        self.min_score = min_score
        self.fields = fields
        self.use_system = use_system
        self.use_national = use_national
        self.transforms = transforms or {}
        self.cre_class_ids = set(cre_class_ids)


PROFILES = {
    "fa": Profile(
        "fa", "First American assessor export", "high",
        ["landusecode", "countylandusecode", "propertyclassid", "storiesnbrcode", "apnseqnbr",
         "situsfullstreetaddress", "ownername1full", "sumbuildingsqft"], 0.6,
        {"apn": ["apn"], "apn_seq": ["apnseqnbr"], "fips": ["fips"],
         "situs_address": ["situsfullstreetaddress"], "situs_city": ["situscity"],
         "situs_state": ["situsstate"], "situs_zip": ["situszip5"], "lat": ["situslatitude"],
         "lon": ["situslongitude"], "use_code": ["landusecode"],
         "county_use_code": ["countylandusecode"], "property_class": ["propertyclassid"],
         "zoning": ["zoning"], "legal_desc": ["legaldescription"], "subdivision": ["subdivisionname"],
         "building_sqft": ["sumbuildingsqft", "buildingarea"], "building_count": ["sumbuildingsnbr"],
         "stories": ["storiesnbrcode"], "year_built": ["yearbuilt"], "lot_acres": ["lotsizeacres"],
         "lot_sqft": ["lotsizesqft"], "units_res": ["sumresidentialunits"],
         "units_comm": ["sumcommercialunits"], "quality": ["buildingqualitycode"],
         "condition": ["buildingconditioncode"], "construction": ["constructiontypecode"],
         "mv_total": ["markettotalvalue"], "mv_land": ["marketvalueland"],
         "mv_impr": ["marketvalueimprovement"], "assessed_total": ["assdtotalvalue"],
         "sale_price": ["currentsalesprice"], "sale_date": ["currentsalerecordingdate"],
         "owner1": ["ownername1full"], "owner2": ["ownername2full"], "owner3": ["ownername3full"],
         "owner4": ["ownername4full"], "owner_first": ["owner1firstname"],
         "owner_last": ["owner1lastname"], "owner_is_corp": ["owner1corpind"],
         "mail_address": ["mailingfullstreetaddress"], "mail_city": ["mailingcity"],
         "mail_state": ["mailingstate"], "mail_zip": ["mailingzip5"], "geometry": ["parcelboundary"],
         "situs_house": ["situshousenbr"], "situs_street": ["situsstreet"]},
        "fa", True,
        {"stories": _fa_stories, "quality": _code_lookup(FA_QUALITY),
         "condition": _code_lookup(FA_CONDITION), "construction": _code_lookup(FA_CONSTRUCTION),
         "owner_is_corp": _flag, "use_code": _fa_code},
        cre_class_ids=("C", "O", "I", "F")),
    "datasift": Profile(
        "datasift", "DataSift (REISift) export", "high",
        ["exportedfromreisiftio", "structuretype", "buildingusecode", "landzoning", "storeys",
         "propertyaddress", "mailingaddress", "liststack"], 0.5,
        {"apn": ["apn", "parcelid"], "situs_address": ["propertyaddress"],
         "situs_city": ["propertycity"], "situs_state": ["propertystate"],
         "situs_zip": ["propertyzip5", "propertyzip"], "county": ["propertycounty"],
         "structure_type": ["structuretype"], "use_desc": ["buildingusecode"],
         "zoning": ["landzoning"], "building_sqft": ["sqft"], "stories": ["storeys"],
         "year_built": ["year"], "lot_acres": ["lotsize"], "units_res": ["numberofunits"],
         "vendor_avm": ["estimatedvalue"], "sale_price": ["lastsaleprice"], "sale_date": ["lastsold"],
         "legal_desc": ["legaldescription"],
         "owner1": [("first_of", "businessname", ("join", "firstname", "lastname"))],
         "owner_first": ["firstname"], "owner_last": ["lastname"],
         "mail_address": ["mailingaddress"], "mail_city": ["mailingcity"],
         "mail_state": ["mailingstate"], "mail_zip": ["mailingzip5", "mailingzip"],
         "business_name": []},
        "datasift_use", True),
    "regrid": Profile(
        "regrid", "Regrid parcel export", "medium-high",
        ["parcelnumb", "usecode", "usedesc", "lbcsactivity", "lbcsfunction", "mailadd", "llgisacre",
         "lluuid", "szip"], 0.5,
        {"apn": ["parcelnumb", "parcelnumbnoformatting"], "fips": ["geoid"], "county": ["county"],
         "situs_address": ["address"], "situs_city": ["scity"], "situs_state": ["state2"],
         "situs_zip": ["szip", "szip5"], "lat": ["lat"], "lon": ["lon"], "use_code": ["usecode"],
         "use_desc": ["usedesc"], "zoning": ["zoning"],
         "zoning_desc": ["zoningdescription", "zoningdesc"],
         "lbcs_code": ["lbcsfunction", "lbcsactivity"],
         "lbcs_desc": ["lbcsfunctiondesc", "lbcsactivitydesc"], "building_count": ["structno"],
         "year_built": ["yearbuilt"], "stories": ["numstories"], "units_res": ["numunits"],
         "mv_total": ["parval"], "mv_land": ["landval"], "mv_impr": ["improvval"],
         "sale_price": ["saleprice"], "sale_date": ["saledate"], "owner1": ["owner"],
         "owner2": ["owner2"], "owner_first": ["ownfrst"], "owner_last": ["ownlast"],
         "mail_address": ["mailadd"], "mail_city": ["mailcity"], "mail_state": ["mailstate2"],
         "mail_zip": ["mailzip"], "lot_acres": ["llgisacre", "gisacre", "deededacres"],
         "building_sqft": ["recrdareano"], "geometry": ["geom", "wkt", "geometry"]},
        "regrid_use", False),
    "loopnet_crexi": Profile(
        "loopnet_crexi", "Listing export (LoopNet, Crexi, CoStar style)", "medium",
        ["propertytype", "propertysubtype", "buildingsize", "caprate", "askingprice", "listingurl"], 0.5,
        {"vendor_type": ["propertytype", "assettype"], "vendor_subtype": ["propertysubtype", "subtype"],
         "building_sqft": ["buildingsize", "buildingsf", "rba", "rentablebuildingarea"]},
        "vendor_type", True),
    "reonomy": Profile(
        "reonomy", "Reonomy export", "medium",
        ["reonomyid", "assetcategory", "assettype", "reportedowner"], 0.5,
        {"vendor_type": ["assetcategory"], "vendor_subtype": ["assettype"],
         "owner1": ["reportedowner", "reportedownername"]},
        "vendor_type", True),
    "arcgis": Profile(
        "arcgis", "ArcGIS parcel layer", "fields vary by county",
        ["objectid", "shapearea", "shapelength", "globalid"], 0.25, {}, "use", False),
    "generic": Profile("generic", "Generic parcel file", "learned per county", [], 1.1, {}, "use", False),
}


def detect(headers):
    """Best profile for these headers: (name, score, scores-by-profile)."""
    have = {norm(h) for h in headers}
    scores = {}
    for name, p in PROFILES.items():
        if not p.fingerprint:
            continue
        scores[name] = sum(1 for f in p.fingerprint if f in have) / float(len(p.fingerprint))
    ranked = sorted(((s, n) for n, s in scores.items() if s >= PROFILES[n].min_score), reverse=True)
    if ranked:
        return ranked[0][1], ranked[0][0], scores
    return "generic", 0.0, scores


def _resolve(spec, hnorm):
    """A spec is a header name, ('join', a, b, ...) or ('first_of', a, b, ...)."""
    if isinstance(spec, tuple):
        parts = [_resolve(p, hnorm) for p in spec[1:]]
        parts = [p for p in parts if p]
        if not parts:
            return None
        return (spec[0],) + tuple(parts)
    return hnorm.get(norm(spec))


def build_mapping(profile_name, headers, overrides=None, aliases=None):
    """{field: header-or-compose-spec}. Each header feeds at most one field,
    the first in canonical order, unless an override says otherwise."""
    p = PROFILES[profile_name]
    hnorm = {}
    for h in headers:
        hnorm.setdefault(norm(h), h)
    for alias, real in (aliases or {}).items():
        hnorm.setdefault(norm(alias), real)
    mapping, used = {}, set()
    overrides = dict(overrides or {})
    for field, header in overrides.items():
        if field not in schema.FIELDS and field not in schema.EXTRA:
            raise ProfileError("--map: unknown field %r. Fields: %s" % (field, ", ".join(schema.FIELDS + schema.EXTRA)))
        real = hnorm.get(norm(header))
        if not real:
            raise ProfileError("--map %s=%s: no such header. Headers seen: %s" % (field, header, ", ".join(headers)))
        mapping[field] = real
        used.add(real)
    for field in schema.FIELDS + schema.EXTRA:
        if field in mapping:
            continue
        cands = list(p.fields.get(field, []))
        if field not in p.fields or p.fields.get(field):
            cands += GENERIC.get(field, [])
        for c in cands:
            got = _resolve(c, hnorm)
            if not got:
                continue
            if isinstance(got, str):
                if got in used:
                    continue
                used.add(got)
            mapping[field] = got
            break
    return mapping


def check_mapping(mapping, headers):
    """Raise when nothing in the file can carry a classification signal."""
    if not any(f in mapping for f in schema.CLASS_SIGNALS):
        raise ProfileError(
            "no column that can classify a property was found (looked for a land use code or "
            "description, structure or property type, zoning, property class, or business name). "
            "Point one at the right header with --map, e.g. --map use_desc=\"Land Use\". "
            "Headers seen: %s" % ", ".join(headers))


def header_of(spec):
    if isinstance(spec, tuple):
        return "%s(%s)" % (spec[0], ", ".join(header_of(s) for s in spec[1:]))
    return spec


def read_value(row, spec):
    """Pull a raw value out of a row by spec."""
    if spec is None:
        return None
    if isinstance(spec, tuple):
        op, parts = spec[0], spec[1:]
        if op == "join":
            vals = [schema.text(read_value(row, s)) for s in parts]
            vals = [v for v in vals if v]
            return " ".join(vals) if vals else None
        for s in parts:
            v = schema.text(read_value(row, s))
            if v:
                return v
        return None
    return row.get(spec)
