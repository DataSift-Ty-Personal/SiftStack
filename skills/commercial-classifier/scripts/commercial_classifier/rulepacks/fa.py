"""First American rule pack.

* Code 2038 is "Casino" nationally, but at least one county (Knox, TN) maps its
  commercial-condo-unit code onto it. A county with a pile of "casinos" is that
  miscode, so the rule only fires when a county has CONDO_GATE or more 2038
  parcels; a county with one real casino keeps it.
* FA codes 1101/1102/1103 are duplex/triplex/fourplex: they imply a unit count
  when the unit field is a placeholder.
"""
from __future__ import annotations

import re

CONDO_GATE = 10
CASINO_CODE = "2038"

# NAIP-validated in Knox 2026-07: APT-named condo complexes are converted
# apartment buildings; transport/salvage names are industrial sites.
CONDO_RULES = [
    (re.compile(r"APARTMENT|\bAPTS?\b|\bFLATS\b"), "Multifamily", "", 0.7),
    (re.compile(r"THEATRE|THEATER"), "Sports & Entertainment", "Theater/Concert Hall", 0.7),
    (re.compile(r"SALVAGE|RECYCL|TRANSPORT\b|PETROLEUM|TRUCKING"), "Industrial", "Service", 0.7),
    (re.compile(r"MEDICAL|DENTAL|HEALTH|CLINIC"), "Office", "Medical", 0.7),
    (re.compile(r"OFFICE|OFC|PROF\b|PROFESSIONAL|CORPORATE|EXECUTIVE|BUS(INESS)? PARK"), "Office", "", 0.7),
    (re.compile(r"STORAGE|WAREHOUSE|MINI[- ]?WHSE|BOAT"), "Specialty", "Self-Storage", 0.7),
    (re.compile(r"INDUSTRIAL|FLEX"), "Industrial", "Flex", 0.7),
    (re.compile(r"RETAIL|SHOP(PE|PING|S)?|PLAZA|MARKET|COMMONS"), "Retail", "Storefront", 0.7),
]

CODE_UNITS = {"1101": 2, "1102": 3, "1103": 4}


def condo(text):
    """Reclassify a 2038 commercial condo by the words around it.
    Returns (class, sub, confidence, method)."""
    t = (text or "").upper()
    for rx, ac, st, conf in CONDO_RULES:
        if rx.search(t):
            return ac, st, conf, "tier1-condo-keyword"
    return "Office", "", 0.5, "tier1-condo-default"
