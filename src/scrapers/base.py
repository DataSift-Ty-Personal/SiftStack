"""Base class for Ohio county notice scrapers.

Each concrete scraper subclasses NoticeScraper, sets `county` + `notice_type`
as class attributes, and implements `scrape()`.

The async contract:
    records = await scraper.scrape(since_date=date(2026, 4, 17))

All scrapers return a list of `NoticeData` with at minimum these fields
populated: county, notice_type, source_url, date_added, raw_text, and
either (address, city, state, zip) or owner_name / decedent_name depending
on what the source exposes. The enrichment pipeline fills the rest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from models import NoticeData


class NoticeScraper(ABC):
    """Abstract base for all OH portal scrapers."""

    # Set by each concrete subclass
    county: str = ""
    notice_type: str = ""
    source_name: str = ""          # Human-readable name for logs ("Montgomery Probate")
    source_url: str = ""           # Root portal URL (for logs/preflight)
    requires_account: bool = False  # True if scraper needs credentials to function

    @abstractmethod
    async def scrape(self, since_date: date | None = None) -> list[NoticeData]:
        """Pull notices from this source.

        Args:
            since_date: Only return notices filed/published on or after this date.
                        If None, the scraper uses its own default window
                        (typically the last 7 days).

        Returns:
            List of NoticeData with `county`, `notice_type`, `source_url`,
            and `date_added` populated. Other fields filled opportunistically.
        """

    def required_credentials(self) -> list[str]:
        """Return list of config attribute names this scraper needs.

        Default: no credentials. Override for RealAuction-backed sources.
        """
        return []

    def __repr__(self) -> str:
        return f"<{type(self).__name__} county={self.county} type={self.notice_type}>"


def load_scraper(module_path: str) -> NoticeScraper:
    """Import and instantiate a scraper module by dotted path.

    The module must expose a `Scraper` class that subclasses NoticeScraper.
    Example: load_scraper("scrapers.oh_montgomery_probate") returns
    an instance of scrapers.oh_montgomery_probate.Scraper.
    """
    import importlib

    module = importlib.import_module(module_path)
    if not hasattr(module, "Scraper"):
        raise ImportError(
            f"Module {module_path} does not expose a `Scraper` class. "
            "Every scraper module must define `class Scraper(NoticeScraper): ...`"
        )
    instance = module.Scraper()
    if not isinstance(instance, NoticeScraper):
        raise TypeError(f"{module_path}.Scraper must subclass NoticeScraper")
    return instance


# ── ZIP → state correction ─────────────────────────────────────────────
# Canonical USPS ZIP-prefix → state mapping. Stable since the 1960s.
# Used to override clerk-typed wrong-state entries on probate fiduciary
# addresses (e.g. "GRAND ISLAND, TN 14072" — ZIP 14072 is unambiguously NY).
# Format: (start_prefix, end_prefix_inclusive, state_abbr)
_ZIP_PREFIX_RANGES: tuple[tuple[int, int, str], ...] = (
    (5, 9, "PR"),
    (10, 27, "MA"),
    (28, 29, "RI"),
    (30, 38, "NH"),
    (39, 49, "ME"),
    (50, 58, "VT"),
    (60, 69, "CT"),
    (70, 89, "NJ"),
    (100, 149, "NY"),
    (150, 196, "PA"),
    (197, 199, "DE"),
    (200, 205, "DC"),
    (206, 219, "MD"),
    (220, 246, "VA"),
    (247, 268, "WV"),
    (270, 289, "NC"),
    (290, 299, "SC"),
    (300, 319, "GA"),
    (320, 339, "FL"),
    (341, 349, "FL"),  # 340 reserved
    (350, 369, "AL"),
    (370, 385, "TN"),
    (386, 397, "MS"),
    (398, 399, "GA"),
    (400, 427, "KY"),
    (430, 459, "OH"),
    (460, 479, "IN"),
    (480, 499, "MI"),
    (500, 528, "IA"),
    (530, 549, "WI"),
    (550, 567, "MN"),
    (570, 577, "SD"),
    (580, 588, "ND"),
    (590, 599, "MT"),
    (600, 629, "IL"),
    (630, 658, "MO"),
    (660, 679, "KS"),
    (680, 693, "NE"),
    (700, 714, "LA"),
    (716, 729, "AR"),
    (730, 749, "OK"),
    (750, 799, "TX"),
    (800, 816, "CO"),
    (820, 831, "WY"),
    (832, 838, "ID"),
    (840, 847, "UT"),
    (850, 865, "AZ"),
    (870, 884, "NM"),
    (889, 898, "NV"),
    (900, 961, "CA"),
    (967, 968, "HI"),
    (970, 979, "OR"),
    (980, 994, "WA"),
    (995, 999, "AK"),
)


def state_from_zip(zip_code: str) -> str | None:
    """Return the canonical USPS state for a ZIP code's first 3 digits.

    Returns None for unrecognized prefixes (military APO/FPO, U.S. territories
    beyond PR, or malformed input).
    """
    if not zip_code:
        return None
    digits = str(zip_code).strip().split("-")[0]
    if not digits.isdigit() or len(digits) < 3:
        return None
    prefix = int(digits[:3])
    for start, end, state in _ZIP_PREFIX_RANGES:
        if start <= prefix <= end:
            return state
    return None


def correct_state_against_zip(state: str, zip_code: str) -> str:
    """If a parsed state disagrees with the ZIP prefix, the ZIP wins.

    Catches clerk data-entry errors on fiduciary mailing addresses (e.g.
    "GRAND ISLAND, TN 14072" — the ZIP is unambiguously NY) and stale
    TN-era defaults. When the ZIP prefix maps to a canonical state that
    differs from `state`, returns that canonical state. Otherwise returns
    `state` unchanged.

    If `state` is empty but ZIP is recognized, fills it in.
    """
    canonical = state_from_zip(zip_code)
    if not canonical:
        return state
    cur = (state or "").strip().upper()
    if not cur:
        return canonical
    if cur != canonical:
        return canonical
    return cur
