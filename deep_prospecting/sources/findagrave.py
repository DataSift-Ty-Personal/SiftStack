"""Find-A-Grave verification source.

Used by Phase 2.5 to verify whether a specific heir is deceased.

findagrave.com is Cloudflare-protected — direct HTTP returns 403. We
work around that by piggy-backing on the obit_search infrastructure
(DDGS via Google backend), which surfaces findagrave URLs as search
results. Presence of a findagrave URL in the results is enough signal
for our needs: it means a memorial exists, which means the person is
deceased.

When we *do* need to extract DOD/birth-date from a findagrave page, we
fall back to obit_fetch_page_text which routes through Firecrawl on
Cloudflare 403s. Optional — most Phase 2.5 calls only need the boolean.
"""

from __future__ import annotations

import asyncio
import logging
import re

from deep_prospecting._siftstack_bridge import obit_fetch_page_text, obit_search
from deep_prospecting.models import SourceState

logger = logging.getLogger(__name__)


_FINDAGRAVE_URL_RE = re.compile(r"https?://(?:www\.)?findagrave\.com/memorial/\d+", re.I)


def _looks_like_findagrave(url: str) -> bool:
    return bool(_FINDAGRAVE_URL_RE.match((url or "").strip()))


async def search_memorial(
    name: str,
    city: str,
    state_full: str,
) -> tuple[bool, list[str], SourceState]:
    """Return (memorial_found, urls, state).

    `memorial_found` is True when one or more findagrave.com memorial
    URLs appear in the search results for the person — a high-precision
    DECEASED signal.

    `urls` is the list of findagrave URLs we saw (most relevant first),
    so the caller can optionally fetch one for DOD/DOB extraction.
    """
    if not name:
        return False, [], SourceState(
            source="findagrave", status="SKIPPED",
            blocked_reason="empty name",
        )

    try:
        results = await asyncio.to_thread(
            obit_search, name, city or "", "find a grave OR memorial", state_full,
        )
    except Exception as e:
        logger.warning("findagrave search failed for %s: %s", name, e)
        return False, [], SourceState(
            source="findagrave", status="ERROR", blocked_reason=str(e)[:80],
        )

    urls = [r.get("url") for r in results if _looks_like_findagrave(r.get("url", ""))]
    state = SourceState(
        source="findagrave",
        status="HIT" if urls else "EMPTY",
    )
    return bool(urls), urls, state


async def extract_dod(url: str) -> str | None:
    """Best-effort DOD extraction from a Find-A-Grave memorial page.

    Returns YYYY-MM-DD-ish string or None. Findagrave's structured data
    includes "Death" date in a recognizable block; we regex it out
    rather than wiring a full parser. Optional helper for callers that
    want to fold DOD into the heir record.
    """
    if not url:
        return None
    try:
        text = await asyncio.to_thread(obit_fetch_page_text, url)
    except Exception as e:
        logger.debug("findagrave fetch failed for %s: %s", url, e)
        return None
    if not text:
        return None
    # Findagrave death-date formats seen: "Death 9 Jul 2023", "Died July 9, 2023"
    m = re.search(r"\bDe(?:ath|ied)\s+(\d{1,2}\s+\w+\s+\d{4})", text)
    if m:
        return m.group(1)
    m = re.search(r"\bDied\s+(\w+\s+\d{1,2},?\s+\d{4})", text)
    if m:
        return m.group(1)
    return None
