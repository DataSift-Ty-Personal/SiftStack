"""City and ZIP for a record that has a street address and nothing after it.

WHY THIS EXISTS. The county lookups that resolve a probate decedent's house
(TPAD for Blount, KGIS for Knox) return a street line only. The city was a
guess ("Maryville", "Knoxville") and the ZIP was left for Smarty to fill in.
When the Smarty subscription lapsed in September 2026 every one of those
records reached validation with no ZIP and was dropped, silently, for two
months: the run still said OK because the foreclosures (whose notices print
their own ZIP) kept coming through.

So the ZIP no longer depends on a paid vendor being up. SiftMap's address
autocomplete is free, needs no auth, and is the same address base DataSift
standardizes against on ingest, so the street it returns is the street the CRM
record will carry.

A hit is only trusted when ALL of these hold, because an address-only search
will happily return the same street name in another part of the state:
  * state is TN
  * the house number is identical
  * the street name tokens are identical after suffix/directional normalizing
  * the ZIP belongs to the record's county (Knox and Blount are listed; any
    other county accepts a TN hit)

Stdlib plus siftmap_standalone. Never raises: a record it cannot resolve is
left exactly as it was.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# ZIPs that actually sit in each county we work. Blount is enumerated. Knox is
# every 379xx plus the outlying post offices that deliver inside the county.
BLOUNT_ZIPS = {
    "37701",                              # Alcoa
    "37737",                              # Friendsville
    "37742",                              # Greenback
    "37777",                              # Louisville
    "37801", "37802", "37803", "37804",   # Maryville
    "37853",                              # Rockford
    "37865",                              # Seymour
    "37878",                              # Tallassee
    "37882",                              # Townsend
    "37886",                              # Walland
}
KNOX_OUTLYING_ZIPS = {
    "37721",   # Corryton
    "37754",   # Heiskell
    "37764",   # Kodak
    "37779",   # Luttrell
    "37806",   # Mascot
    "37849",   # Powell
    "37871",   # Strawberry Plains
}


def zip_in_county(zip5: str, county: str) -> bool:
    """True when the ZIP can belong to the county. Unknown counties pass."""
    c = (county or "").strip().lower()
    if c == "blount":
        return zip5 in BLOUNT_ZIPS
    if c == "knox":
        return zip5.startswith("379") or zip5 in KNOX_OUTLYING_ZIPS
    return True


_SUFFIX = {
    "street": "st", "str": "st", "road": "rd", "drive": "dr", "drv": "dr",
    "avenue": "ave", "av": "ave", "lane": "ln", "court": "ct", "circle": "cir",
    "boulevard": "blvd", "highway": "hwy", "place": "pl", "trail": "trl",
    "terrace": "ter", "parkway": "pkwy", "point": "pt", "square": "sq",
    "crossing": "xing", "heights": "hts", "ridge": "rdg", "hollow": "holw",
    "mountain": "mtn", "pike": "pike", "way": "way",
}
_DIRECTION = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}
_UNIT_RE = re.compile(r"\s+(?:apt|unit|ste|suite|lot|#)\s*\.?\s*[\w-]+\s*$", re.IGNORECASE)
_FULL_ADDR_RE = re.compile(r"^\s*(.+?),\s*([^,]+?),\s*([A-Za-z]{2})\s+(\d{5})")


def street_key(street_line: str) -> tuple[str, frozenset]:
    """(house number, normalized street-name tokens) for comparing two lines."""
    line = _UNIT_RE.sub("", (street_line or "").strip())
    line = re.sub(r"[^A-Za-z0-9\s]", " ", line).lower()
    parts = line.split()
    if not parts or not parts[0].isdigit():
        return "", frozenset()
    tokens = []
    for tok in parts[1:]:
        tok = _DIRECTION.get(tok, tok)
        tok = _SUFFIX.get(tok, tok)
        tokens.append(tok)
    return parts[0], frozenset(tokens)


def split_full_address(full: str) -> tuple[str, str, str, str] | None:
    """'100 Example Rd, Maryville, TN 37803' -> (street, city, state, zip)."""
    m = _FULL_ADDR_RE.match(full or "")
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip(), m.group(3).upper(), m.group(4)


def pick_hit(hits: list, address: str, county: str) -> dict | None:
    """The first autocomplete hit that is provably this street in this county."""
    want = street_key(address)
    if not want[0] or not want[1]:
        return None
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        parsed = split_full_address(hit.get("address") or hit.get("title") or "")
        if not parsed:
            continue
        street, city, state, zip5 = parsed
        if state != "TN":
            continue
        if street_key(street) != want:
            continue
        if not zip_in_county(zip5, county):
            continue
        return {"address": street, "city": city, "state": state, "zip": zip5}
    return None


def resolve_city_zip(client, address: str, city: str = "", county: str = "") -> dict | None:
    """Ask SiftMap for the city and ZIP of one street line. None when unsure."""
    queries = []
    if city:
        queries.append(f"{address}, {city}, TN")
    queries.append(f"{address} TN")
    for query in queries:
        try:
            hits = client.autocomplete(query, limit=8)
        except Exception as exc:  # noqa: BLE001 - never break the pipeline
            logger.debug("SiftMap autocomplete failed for %r: %s", query, exc)
            continue
        found = pick_hit(hits, address, county)
        if found:
            return found
    return None


def fill_missing_city_zip(notices: list, client=None) -> int:
    """Fill city + ZIP on every record that has a street but no ZIP.

    Returns how many were filled. Modifies the notices in place. A record that
    cannot be resolved with certainty is left alone and falls to validation,
    which is the honest outcome: no ZIP is better than a wrong one.
    """
    todo = [n for n in notices
            if (getattr(n, "address", "") or "").strip()
            and not (getattr(n, "zip", "") or "").strip()]
    if not todo:
        return 0
    if client is None:
        if not os.getenv("REISIFT_API_KEY"):
            logger.info("  ZIP fallback skipped: REISIFT_API_KEY not set (%d records without a ZIP)",
                        len(todo))
            return 0
        try:
            from siftmap_standalone import SiftMapClient
            client = SiftMapClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning("  ZIP fallback unavailable: %s", exc)
            return 0

    filled = 0
    for notice in todo:
        found = resolve_city_zip(client, notice.address, getattr(notice, "city", ""),
                                 getattr(notice, "county", ""))
        if not found:
            logger.info("  ZIP fallback: no certain match for %s (%s County)",
                        notice.address, getattr(notice, "county", "?"))
            continue
        notice.address = found["address"]
        notice.city = found["city"]
        notice.state = found["state"]
        notice.zip = found["zip"]
        filled += 1
    logger.info("  ZIP fallback (SiftMap): filled %d of %d records that had no ZIP",
                filled, len(todo))
    return filled
