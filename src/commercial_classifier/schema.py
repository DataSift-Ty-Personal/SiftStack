"""Canonical record fields, value parsers and the row id.

Two rules here exist because the original pipeline broke on them:

* MISSING IS NOT ZERO. The old fnum() turned a blank cell into 0.0, so a file
  with no square-footage column made every parcel look unimproved and the
  no-improvement rule sent the whole file to Land. Parsers return None.
* NEVER JOIN BY POSITION. Every phase keys its output on `rid`, a hash of the
  county and parcel number, so a re-sorted or filtered file cannot mis-join.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re

IDENT = ["apn", "apn_seq", "fips", "county", "situs_address", "situs_city", "situs_state",
         "situs_zip", "lat", "lon"]
SIGNAL = ["use_code", "use_desc", "county_use_code", "county_use_desc", "property_class", "zoning",
          "zoning_desc", "structure_type", "vendor_type", "vendor_subtype", "lbcs_code", "lbcs_desc",
          "legal_desc", "subdivision", "business_name"]
PHYS = ["building_sqft", "building_count", "stories", "year_built", "lot_acres", "units_res",
        "units_comm", "quality", "condition", "construction"]
VALUE = ["mv_total", "mv_land", "mv_impr", "assessed_total", "vendor_avm", "sale_price", "sale_date"]
OWNER = ["owner1", "owner2", "owner3", "owner4", "owner_first", "owner_last", "owner_is_corp",
         "mail_address", "mail_city", "mail_state", "mail_zip"]
FIELDS = IDENT + SIGNAL + PHYS + VALUE + OWNER
# mapping-only fields: they feed a canonical field through a transform
EXTRA = ["geometry", "lot_sqft", "situs_house", "situs_street"]

NUMERIC = {"lat", "lon", "building_sqft", "building_count", "stories", "year_built", "lot_acres",
           "units_res", "units_comm", "mv_total", "mv_land", "mv_impr", "assessed_total",
           "vendor_avm", "sale_price", "lot_sqft"}
DATES = {"sale_date"}
CLASS_SIGNALS = ["use_code", "use_desc", "structure_type", "vendor_type", "county_use_code",
                 "county_use_desc", "property_class", "lbcs_code", "lbcs_desc", "zoning",
                 "zoning_desc", "business_name"]

_NULLS = {"", "NULL", "NONE", "NAN", "N/A", "NA", "-", "--", "#N/A", "NAT"}


def text(v):
    if v is None:
        return None
    s = str(v).strip()
    if s.upper() in _NULLS:
        return None
    return s


def num(v):
    """'$1,234.50' -> 1234.5, blank -> None. Never 0 for missing."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if v != v:  # NaN
            return None
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    if s.upper() in _NULLS:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def parse_date(v):
    """Any of 2023-08-31, 20230831, 08/31/2023, 2023-08-31T00:00:00, epoch ms,
    or a bare year -> 'YYYY-MM-DD' (a bare year becomes YYYY-01-01). None if
    it cannot be read."""
    if v is None:
        return None
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)):
        if v > 10 ** 11:  # epoch milliseconds (ArcGIS)
            try:
                return _dt.datetime.fromtimestamp(v / 1000.0, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                return None
        v = str(int(v))
    s = str(v).strip()
    if s.upper() in _NULLS:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
        if m:
            y, mo, d = map(int, m.groups())
        else:
            m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
            if m:
                mo, d, y = map(int, m.groups())
                if y < 100:
                    y += 2000 if y < 50 else 1900
            else:
                m = re.match(r"^(\d{4})$", s)
                if not m:
                    if re.match(r"^\d{11,13}$", s):
                        return parse_date(int(s))
                    return None
                y, mo, d = int(m.group(1)), 1, 1
    try:
        return _dt.date(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def norm_space(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def norm_apn(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def norm_addr(s):
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    return re.sub(r"\s+", " ", s).strip()


_UNIT_RX = re.compile(r"\b(STE|SUITE|APT|UNIT|RM|ROOM|FL|FLOOR|BLDG|LOT|TRLR|PMB)\b.*$|#.*$")


def addr_base(s):
    """Street address without its suite/unit tail."""
    return norm_space(_UNIT_RX.sub(" ", norm_addr(s)))


def county_key(fips=None, county=None, state=None, override=None):
    """The county a record belongs to: 5-digit FIPS when known, else
    STATE:COUNTY, else the --county-key override, else ''."""
    if override:
        return str(override).strip().upper()
    f = re.sub(r"\D", "", str(fips or ""))
    if f:
        return f.zfill(5)[-5:]
    c = norm_addr(county).replace(" COUNTY", "").strip()
    if c:
        return ("%s:%s" % ((state or "").upper(), c)).strip(":")
    return ""


def make_rid(ck, apn, apn_seq, situs, zip_, raw_items):
    """Stable 16-hex id. Keyed on county + parcel number; falls back to the
    situs address, then to the raw content of the row."""
    a = norm_apn(apn)
    if a:
        basis = "A|%s|%s|%s" % (ck, a, norm_apn(apn_seq))
    elif norm_addr(situs):
        basis = "S|%s|%s|%s" % (ck, norm_addr(situs), (zip_ or "")[:5])
    else:
        basis = "R|" + "|".join("%s=%s" % kv for kv in raw_items)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def content_hash(raw_items):
    return hashlib.sha1("|".join("%s=%s" % kv for kv in raw_items).encode("utf-8")).hexdigest()[:6]
