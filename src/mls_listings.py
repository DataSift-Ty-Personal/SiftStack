"""Which addresses are active on the MLS right now, from Zillow agent listings.

Sift's MLS field lags: one Knox house read "Off Market" in Sift while Zillow
had it agent-listed at $750,000 for 58 days (2026-10-30), and the owner answered
our text with her list price. Anything we market to is checked here instead.

One price-banded /search per zip pulls every agent listing in it. The search
defaults to listing_type BY_AGENT, so a for-sale-by-owner is not counted (it is
not on the MLS and is worth the call). That is a few hundred calls for a whole
county rather than one per address.

    listings = ActiveListings()
    hit = listings.lookup("100 Example Rd", "37932")   # {'price', 'status', 'days', 'url'} or None
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests

import config
from zillow_market_api import MAX_RETRIES, MIN_BAND_WIDTH, RESULT_CAP, SEARCH_ENDPOINT

logger = logging.getLogger(__name__)

BANDS = ((0, 150_000), (150_001, 300_000), (300_001, 500_000), (500_001, 900_000), (900_001, 100_000_000))
SUFFIX = {"road": "rd", "drive": "dr", "avenue": "ave", "av": "ave", "street": "st", "lane": "ln", "circle": "cir",
          "court": "ct", "boulevard": "blvd", "parkway": "pkwy", "place": "pl", "trail": "trl", "terrace": "ter",
          "highway": "hwy", "pike": "pike", "way": "way", "cove": "cv", "point": "pt", "square": "sq", "run": "run"}
DIRS = {"north": "n", "south": "s", "east": "e", "west": "w"}


_UNIT = re.compile(r"\s(?:#|(?:apt|unit|ste|suite|lot)\b)\s*([a-z0-9-]*)")


def address_key(street: str, zip_code: str) -> Optional[tuple[str, str]]:
    """House number + street name (suffix dropped) + unit + zip5, or None when
    the street has no house number to match on. The unit stays in: without it
    one listed condo matched three units of 804 Olde Pioneer Trl."""
    s = (street or "").lower()
    m = _UNIT.search(s)
    unit = m.group(1) if m else ""
    s = s[:m.start()] if m else s
    toks = [DIRS.get(t, SUFFIX.get(t, t)) for t in re.sub(r"[^a-z0-9 ]", " ", s).split()]
    if not toks or not toks[0].isdigit():
        return None
    if len(toks) > 2 and toks[-1] in SUFFIX.values():
        toks = toks[:-1]
    return " ".join(toks) + (f" #{unit}" if unit else ""), str(zip_code or "")[:5]


class ActiveListings:
    """Agent listings by address, pulled per zip on first lookup and cached."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or config.OPENWEBNINJA_API_KEY
        if not self.api_key:
            raise ValueError("OPENWEBNINJA_API_KEY is not configured")
        self.calls = 0
        self._zips: set[str] = set()
        self._by_key: dict[tuple[str, str], dict] = {}

    def lookup(self, street: str, zip_code: str) -> Optional[dict]:
        k = address_key(street, zip_code)
        if not k or len(k[1]) != 5:
            return None
        if k[1] not in self._zips:
            self.pull_zip(k[1])
        return self._by_key.get(k)

    def pull_zip(self, zip5: str) -> int:
        """Every agent listing in one zip. Raises rather than return a partial
        pull, because a missed listing reads exactly like an off-market house."""
        before = len(self._by_key)
        for lo, hi in BANDS:
            self._band(zip5, lo, hi)
        self._zips.add(zip5)
        return len(self._by_key) - before

    def _band(self, zip5: str, lo: int, hi: int) -> None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(SEARCH_ENDPOINT, headers={"x-api-key": self.api_key}, timeout=60,
                                    params={"location": zip5, "home_status": "FOR_SALE",
                                            "min_price": lo, "max_price": hi})
                self.calls += 1
                resp.raise_for_status()
                data = resp.json().get("data") or []
                break
            except (requests.RequestException, ValueError) as exc:
                logger.warning("listing search %s %d-%d: %s (attempt %d)", zip5, lo, hi, exc, attempt)
                time.sleep(2 * attempt)
        else:
            raise RuntimeError(f"listing search failed for {zip5} {lo}-{hi}")
        if len(data) >= RESULT_CAP and hi - lo > MIN_BAND_WIDTH:
            mid = (lo + hi) // 2
            self._band(zip5, lo, mid)
            self._band(zip5, mid + 1, hi)
            return
        for x in data:
            # A zip search also returns listings just over the line.
            if str(x.get("addressZipcode") or x.get("zipcode") or "")[:5] != zip5:
                continue
            k = address_key(x.get("streetAddress") or x.get("addressStreet") or "", zip5)
            if k:
                self._by_key[k] = {"price": x.get("unformattedPrice") or x.get("price"),
                                   "status": x.get("statusText") or x.get("homeStatus"),
                                   "days": x.get("daysOnZillow"), "url": x.get("detailUrl")}
        time.sleep(0.3)
