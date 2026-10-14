"""The classification taxonomy: 13 listing asset classes, 76 sub-types, plus
three non-listing buckets (Residential, Civic/Exempt, Other) so every parcel
in a file gets a class.

Every label that enters the system (a learned crosswalk row, a review verdict,
a place category map) is validated here, so a typo cannot create a class the
rest of the pipeline has never heard of.
"""
from __future__ import annotations

import re

CLASSES = {
    "Office": ["Industrial Live/Work", "Loft/Creative Space", "Medical", "Office Live/Work Unit",
               "Office/Residential", "Office/Storefront Retail"],
    "Industrial": ["Data Center", "Distribution", "Flex", "Manufacturing", "R&D",
                   "Refrigeration/Cold Storage", "Service", "Showroom", "Telecom Facility",
                   "Truck Terminal", "Warehouse", "Other Industrial"],
    "Retail": ["Bank", "Day Care Facility/Nursery", "Drug Store", "Freestanding",
               "Grocery/Convenience Store", "Service/Gas Station", "Showroom", "Storefront",
               "Storefront Retail/Office", "Storefront Retail/Residential", "Vehicle Related",
               "Other Retail"],
    "Restaurant": [],
    "Shopping Center": ["Airport Retail", "Community Center", "Lifestyle Center",
                        "Neighborhood Center", "Outlet Center", "Power Center", "Regional Mall",
                        "Strip Center", "Super Regional Mall", "Theme/Festival Center"],
    "Multifamily": ["Dormitory", "Garden", "High-Rise", "Low-Rise",
                    "Manufactured Housing/Mobile Home", "Mid-Rise"],
    "Specialty": ["Car Wash", "Marina", "Parking", "Religious Facility", "Schools", "Self-Storage",
                  "Other Specialty"],
    "Health Care": ["Assisted Living", "Congregate Senior Housing",
                    "Continuing Care Retirement Community", "Hospital", "Rehabilitation Center",
                    "Skilled Nursing Facility"],
    "Hospitality": ["Bed and Breakfast", "Hostel", "Hotel", "Serviced Apartment"],
    "Sports & Entertainment": ["Amusement Park", "Baseball Field", "Casino", "Golf / Driving Range",
                               "Horse Stables", "Race Track", "Skating Rink", "Swimming Pool",
                               "Theater/Concert Hall"],
    "Lab": [],
    "Land": ["Agricultural", "Commercial", "Industrial", "Residential/Multifamily"],
    "Residential Income": [],
    "Residential": [],
    "Civic/Exempt": [],
    "Other": [],
}

LISTING_CLASSES = [c for c in CLASSES if c not in ("Residential", "Civic/Exempt", "Other")]

BUILDING_CLASSES = {"Office", "Retail", "Industrial", "Restaurant", "Shopping Center", "Specialty",
                    "Health Care", "Hospitality", "Sports & Entertainment", "Lab", "Multifamily"}


def _key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_CLASS_BY_KEY = {_key(c): c for c in CLASSES}
_CLASS_ALIASES = {
    "multifamily": "Multifamily", "apartments": "Multifamily", "apartment": "Multifamily",
    "sportsentertainment": "Sports & Entertainment", "sportsandentertainment": "Sports & Entertainment",
    "healthcare": "Health Care", "medical": "Health Care", "hotel": "Hospitality",
    "civic": "Civic/Exempt", "exempt": "Civic/Exempt", "civicexempt": "Civic/Exempt",
    "government": "Civic/Exempt", "residentialincome": "Residential Income", "lab": "Lab",
    "laboratory": "Lab", "selfstorage": "Specialty", "specialpurpose": "Specialty",
    "vacantland": "Land", "land": "Land",
}
_SUB_BY_KEY = {}
for _c, _subs in CLASSES.items():
    for _s in _subs:
        _SUB_BY_KEY[(_c, _key(_s))] = _s
_SUB_ALIASES = {
    "golfdrivingrange": "Golf / Driving Range", "selfstorage": "Self-Storage",
    "ministorage": "Self-Storage", "gasstation": "Service/Gas Station",
    "servicegasstation": "Service/Gas Station", "convenience": "Grocery/Convenience Store",
    "grocery": "Grocery/Convenience Store", "mobilehomepark": "Manufactured Housing/Mobile Home",
    "manufacturedhousing": "Manufactured Housing/Mobile Home", "daycare": "Day Care Facility/Nursery",
    "coldstorage": "Refrigeration/Cold Storage", "randd": "R&D", "rd": "R&D",
    "religious": "Religious Facility", "church": "Religious Facility", "school": "Schools",
    "theater": "Theater/Concert Hall", "theatre": "Theater/Concert Hall",
    "highrise": "High-Rise", "midrise": "Mid-Rise", "lowrise": "Low-Rise",
    "residentialmultifamily": "Residential/Multifamily", "skillednursing": "Skilled Nursing Facility",
}


class TaxonomyError(ValueError):
    pass


def canon_class(s):
    """'sports and entertainment' -> 'Sports & Entertainment'. None when unknown."""
    k = _key(s)
    if not k:
        return None
    return _CLASS_BY_KEY.get(k) or _CLASS_ALIASES.get(k)


def canon_sub(asset_class, s):
    """Sub-type for a class, tolerant of case and punctuation. '' when blank,
    None when the sub-type does not belong to the class."""
    k = _key(s)
    if not k:
        return ""
    hit = _SUB_BY_KEY.get((asset_class, k))
    if hit:
        return hit
    alias = _SUB_ALIASES.get(k)
    if alias and alias in CLASSES.get(asset_class, []):
        return alias
    return None


def validate(asset_class, sub_type=""):
    """Return the canonical (class, sub) pair or raise TaxonomyError."""
    ac = canon_class(asset_class)
    if not ac:
        raise TaxonomyError("unknown asset class %r (allowed: %s)" % (asset_class, ", ".join(CLASSES)))
    st = canon_sub(ac, sub_type)
    if st is None:
        allowed = ", ".join(CLASSES[ac]) or "none (leave blank)"
        raise TaxonomyError("sub-type %r does not belong to %s (allowed: %s)" % (sub_type, ac, allowed))
    return ac, st


def commercial_target(asset_class, sub_type="", target_hint="", property_class="", cre_class_ids=()):
    """Separate genuine commercial real estate from residential leakage while
    keeping income-producing residential, which is a real target."""
    if target_hint == "income-residential" or asset_class in ("Multifamily", "Residential Income"):
        return "income-residential"
    if target_hint == "residential-leak" or asset_class == "Residential":
        return "residential-leak"
    if asset_class == "Land":
        return "land"
    if asset_class == "Civic/Exempt":
        return "civic"
    if asset_class in BUILDING_CLASSES or (property_class and property_class in cre_class_ids):
        return "commercial-building"
    return "other"
