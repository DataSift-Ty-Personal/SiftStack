"""Reusable OpenWeb Ninja Realtime Zillow Data /search client.

Replaces the retired ``similar-sale-homes`` endpoint (404 as of 2026-07) with
the still-live ``/search`` endpoint, plus the workarounds needed to make it
useful for comping:

API contract gotchas (verified live 2026-07-21):
- ``home_status`` must be the exact enum ``RECENTLY_SOLD`` / ``FOR_SALE``
  (``SOLD``, ``RecentlySold`` etc. return HTTP 400).
- Each search returns AT MOST 41 results and ``totalPages`` is always 1, so a
  bare RECENTLY_SOLD query only reaches back ~5 weeks in an active zip. Deeper
  history requires partitioning: ``min_price`` / ``max_price`` ARE honored
  (echoed in ``parameters``), while ``price_min`` / ``price_max`` are silently
  ignored -- always confirm a filter via the echoed ``parameters`` object.
- ``listing_type`` defaults to BY_AGENT; BY_OWNER_AND_OTHER adds nothing for
  sold history (returns 0), so off-market/auction transfers never appear --
  county records remain the source of truth for non-MLS sales.
- ``dateSold`` is epoch milliseconds. ``soldPrice`` is a display string
  ("$92,500"); use ``unformattedPrice``.
- ``homeType`` can be LOT for a distressed house sold at land value, and for
  new construction the listing may carry no sqft/beds -- verify anything
  surprising against the county card before using it as a comp.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

import config

logger = logging.getLogger(__name__)

API_BASE = "https://api.openwebninja.com/realtime-zillow-data"
SEARCH_ENDPOINT = f"{API_BASE}/search"

RESULT_CAP = 41          # hard per-search cap observed on /search
MIN_BAND_WIDTH = 4000    # stop splitting price bands narrower than this
REQUEST_DELAY = 0.8
MAX_RETRIES = 3

# Default price bands for the adaptive sold pull. Any band that comes back
# saturated (>= RESULT_CAP) is split in half recursively.
DEFAULT_BANDS = [
    (1_000, 100_000), (100_001, 160_000), (160_001, 210_000),
    (210_001, 250_000), (250_001, 290_000), (290_001, 340_000),
    (340_001, 420_000), (420_001, 3_000_000),
]


@dataclass
class MarketListing:
    """One normalized /search result (sold or active)."""
    zpid: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    beds: int = 0
    baths: float = 0.0
    sqft: int = 0
    home_type: str = ""
    home_status: str = ""
    price: float = 0.0          # sold price for sold, list price for active
    sold_date: str = ""         # YYYY-MM-DD, empty for active
    zestimate: float = 0.0
    rent_zestimate: float = 0.0
    days_on_zillow: int = 0
    lot_acres: float = 0.0
    detail_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def ppsf(self) -> float:
        return round(self.price / self.sqft, 2) if self.sqft else 0.0


def _num(value) -> float:
    if isinstance(value, str):
        value = "".join(c for c in value if c.isdigit() or c == ".") or 0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sold_date(item: dict) -> str:
    v = item.get("dateSold")
    if isinstance(v, (int, float)) and v:
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(v or "")[:10]


def normalize(item: dict) -> MarketListing:
    lot_acres = 0.0
    lot_val = item.get("lotAreaValue")
    if lot_val:
        unit = (item.get("lotAreaUnit") or "").lower()
        lot_acres = _num(lot_val) if "acre" in unit else _num(lot_val) / 43560
    return MarketListing(
        zpid=str(item.get("zpid") or ""),
        address=item.get("streetAddress") or item.get("address") or "",
        city=item.get("city") or item.get("addressCity") or "",
        state=item.get("state") or item.get("addressState") or "",
        zip_code=str(item.get("zipcode") or item.get("addressZipcode") or ""),
        latitude=_num(item.get("latitude")),
        longitude=_num(item.get("longitude")),
        beds=int(_num(item.get("beds") or item.get("bedrooms"))),
        baths=_num(item.get("baths") or item.get("bathrooms")),
        sqft=int(_num(item.get("livingArea") or item.get("area"))),
        home_type=item.get("homeType") or "",
        home_status=item.get("homeStatus") or "",
        price=_num(item.get("unformattedPrice") or item.get("price")),
        sold_date=_sold_date(item),
        zestimate=_num(item.get("zestimate")),
        rent_zestimate=_num(item.get("rentZestimate")),
        days_on_zillow=int(_num(item.get("daysOnZillow"))),
        lot_acres=round(lot_acres, 3),
        detail_url=item.get("detailUrl") or "",
        raw=item,
    )


class ZillowMarketAPI:
    """Thin client over /search with the band-partition workaround built in."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or config.OPENWEBNINJA_API_KEY
        if not self.api_key:
            raise ValueError("OPENWEBNINJA_API_KEY is not configured")

    def search(self, location: str, home_status: str = "FOR_SALE", **params) -> list[dict]:
        query = {"location": location, "home_status": home_status, **params}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(SEARCH_ENDPOINT, params=query,
                                    headers={"x-api-key": self.api_key}, timeout=60)
                resp.raise_for_status()
                body = resp.json()
                echoed = body.get("parameters") or {}
                dropped = [k for k in params if k not in echoed]
                if dropped:
                    logger.warning("/search silently ignored params %s (echoed: %s)",
                                   dropped, sorted(echoed))
                return body.get("data") or []
            except (requests.RequestException, ValueError) as exc:
                logger.warning("/search error: %s (attempt %d/%d)", exc, attempt, MAX_RETRIES)
                time.sleep(attempt)
        return []

    def pull_active(self, location: str, houses_only: bool = True) -> list[MarketListing]:
        items = [normalize(i) for i in self.search(location, "FOR_SALE")]
        if houses_only:
            items = [i for i in items if i.home_type in ("SINGLE_FAMILY", "")]
        return items

    def pull_sold(self, location: str, months_back: int = 12,
                  houses_only: bool = True) -> list[MarketListing]:
        """Pull sold history via adaptive price-band partitioning.

        A single RECENTLY_SOLD search caps at 41 rows; banding by price and
        splitting saturated bands recovers roughly the last 2-3 years in a
        typical Knox County zip.
        """
        collected: dict[str, dict] = {}

        def pull_band(lo: int, hi: int, depth: int = 0) -> None:
            data = self.search(location, "RECENTLY_SOLD", min_price=lo, max_price=hi)
            logger.debug("%sband %d-%d: %d rows", "  " * depth, lo, hi, len(data))
            if len(data) >= RESULT_CAP and hi - lo > MIN_BAND_WIDTH:
                mid = (lo + hi) // 2
                pull_band(lo, mid, depth + 1)
                pull_band(mid + 1, hi, depth + 1)
            else:
                for item in data:
                    collected[str(item.get("zpid"))] = item
            time.sleep(REQUEST_DELAY)

        for lo, hi in DEFAULT_BANDS:
            pull_band(lo, hi)

        cutoff = (datetime.now() - timedelta(days=months_back * 30)).strftime("%Y-%m-%d")
        listings = [normalize(i) for i in collected.values()]
        listings = [l for l in listings if l.sold_date >= cutoff]
        if houses_only:
            listings = [l for l in listings if l.home_type == "SINGLE_FAMILY"]
        listings.sort(key=lambda l: l.sold_date, reverse=True)
        logger.info("Pulled %d sold listings for %s (last %d months)",
                    len(listings), location, months_back)
        return listings


# ── Boundary filters ──────────────────────────────────────────────────

def filter_bbox(listings: list[MarketListing], lat_min: float, lat_max: float,
                lon_min: float, lon_max: float) -> list[MarketListing]:
    return [l for l in listings
            if lat_min <= l.latitude <= lat_max and lon_min <= l.longitude <= lon_max]


def filter_streets(listings: list[MarketListing], street_pattern) -> list[MarketListing]:
    """Keep listings whose street matches a compiled regex (drawn-boundary proxy)."""
    return [l for l in listings if street_pattern.search(l.address)]
