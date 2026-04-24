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
