"""Phone numbers: the one format we store, the time zones a number may be in, and its state.

libphonenumber (Google's, through the `phonenumbers` package) is the source. It knows every US area
code and many exchanges, and it is updated as numbering changes. One gap matters for calling hours: it
answers ONE zone even where an area code straddles two (it puts a 423 exchange in Central when most of
423 is Eastern). So `zones()` adds the other zone for the area codes known to straddle a line, and the
hours check requires the call to be inside the window in EVERY candidate zone. That errs toward not
calling, which is the direction a calling-hours rule must err.
"""
from __future__ import annotations

import re
from zoneinfo import ZoneInfo

try:
    import phonenumbers
    from phonenumbers import geocoder as _geo
    from phonenumbers import timezone as _tz
except ImportError:  # pragma: no cover - the image installs it; this keeps the helpers importable
    phonenumbers = None

# Area codes whose territory crosses a time zone line, with every zone they reach. Checked against the
# North American Numbering Plan's area code maps; the cost of a wrong extra zone is a call that waits an
# hour, the cost of a missing one is a call outside the law.
SPLIT_AREA_CODES = {
    "270": ("America/Chicago", "America/New_York"),      # Kentucky
    "364": ("America/Chicago", "America/New_York"),      # Kentucky overlay of 270
    "606": ("America/New_York", "America/Chicago"),      # Kentucky
    "423": ("America/New_York", "America/Chicago"),      # Tennessee
    "931": ("America/Chicago", "America/New_York"),      # Tennessee
    "850": ("America/New_York", "America/Chicago"),      # Florida panhandle
    "812": ("America/New_York", "America/Chicago"),      # southern Indiana
    "930": ("America/New_York", "America/Chicago"),      # overlay of 812
    "219": ("America/Chicago", "America/New_York"),      # northwest Indiana
    "574": ("America/New_York", "America/Chicago"),      # northern Indiana
    "906": ("America/New_York", "America/Chicago"),      # Michigan's Upper Peninsula
    "334": ("America/Chicago", "America/New_York"),      # Alabama, Phenix City keeps Eastern
    "229": ("America/New_York", "America/Chicago"),      # southwest Georgia
    "308": ("America/Chicago", "America/Denver"),        # western Nebraska
    "605": ("America/Chicago", "America/Denver"),        # South Dakota
    "701": ("America/Chicago", "America/Denver"),        # North Dakota
    "785": ("America/Chicago", "America/Denver"),        # Kansas
    "620": ("America/Chicago", "America/Denver"),        # Kansas
    "806": ("America/Chicago", "America/Denver"),        # Texas panhandle
    "915": ("America/Denver", "America/Chicago"),        # El Paso
    "432": ("America/Chicago", "America/Denver"),        # west Texas
    "208": ("America/Boise", "America/Los_Angeles"),     # Idaho
    "986": ("America/Boise", "America/Los_Angeles"),     # overlay of 208
    "541": ("America/Los_Angeles", "America/Boise"),     # Oregon, Malheur County keeps Mountain
    "458": ("America/Los_Angeles", "America/Boise"),     # overlay of 541
    "775": ("America/Los_Angeles", "America/Denver"),    # Nevada, West Wendover keeps Mountain
    "928": ("America/Phoenix", "America/Denver"),        # Arizona, the Navajo Nation keeps daylight time
}

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington d.c.": "DC", "florida": "FL",
    "washington state": "WA",           # libphonenumber's name for 206, 253, 360, 425, 509, 564
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}
_CODES = set(STATES.values())


def digits(raw) -> str:
    """Ten digits for a US number, '' for anything else."""
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def _parsed(raw):
    d = digits(raw)
    if not d or phonenumbers is None:
        return None
    try:
        return phonenumbers.parse(d, "US")
    except Exception:  # noqa: BLE001 - libphonenumber raises its own NumberParseException
        return None


def valid(raw) -> bool:
    """A number that can be dialed: ten digits, an area code and exchange that cannot start with 0 or 1,
    no N11 service code for an area code, no premium (900, 976) lines, and in the United States. Canada and
    the Caribbean share the +1 plan and parse as ordinary ten-digit numbers; 809 and 876 are the classic
    toll-fraud codes, and nobody here is buying a house there."""
    d = digits(raw)
    if not d or d[0] in "01" or d[3] in "01" or d[1:3] == "11" or d[:3] in ("900", "976"):
        return False
    p = _parsed(d)
    if p is None:
        return True   # without the library, the shape checks above are what we have
    if not phonenumbers.is_possible_number(p):
        return False
    return phonenumbers.region_code_for_number(p) in ("US", None)


def e164(raw) -> str:
    d = digits(raw)
    return "+1" + d if d else ""


def pretty(raw) -> str:
    d = digits(raw)
    return "(%s) %s-%s" % (d[:3], d[3:6], d[6:]) if d else str(raw or "")


def last4(raw) -> str:
    d = digits(raw)
    return d[-4:] if d else ""


def zones(raw) -> list[str]:
    """Every IANA zone this number may be in, most likely first."""
    d = digits(raw)
    if not d:
        return []
    out: list[str] = []
    p = _parsed(d)
    if p is not None:
        for z in _tz.time_zones_for_number(p) or ():
            if z and z != "Etc/Unknown" and z not in out:
                out.append(z)
    for z in SPLIT_AREA_CODES.get(d[:3], ()):
        if z not in out:
            out.append(z)
    good = []
    for z in out:
        try:
            ZoneInfo(z)
            good.append(z)
        except Exception:  # noqa: BLE001 - an unknown zone name is dropped, never guessed
            continue
    return good


def state(raw) -> str:
    """The two-letter state a number's area code belongs to, or ''."""
    p = _parsed(raw)
    if p is None:
        return ""
    desc = (_geo.description_for_number(p, "en") or "").strip()
    if not desc:
        return ""
    if "," in desc:
        tail = desc.rsplit(",", 1)[1].strip().upper()
        if tail in _CODES:
            return tail
    return STATES.get(desc.lower(), "")


def state_code(name: str) -> str:
    """'Tennessee', 'TN' or 'tn' -> 'TN'."""
    t = (name or "").strip()
    if t.upper() in _CODES:
        return t.upper()
    return STATES.get(t.lower(), "")
