"""Montgomery County (Ohio) Auditor property lookup.

Used to find a probate decedent's property when the court filing carries
only a name (no address). Wraps the public ASP.NET WebForms search at
``mcrealestate.org`` — no login, no CAPTCHA, just ViewState juggling.

Search flow
-----------
1. GET  ``/search/commonsearch.aspx?mode=owner`` to grab a fresh
   ``__VIEWSTATE`` / ``__EVENTVALIDATION`` pair.
2. POST the same URL with ``inpOwner=LASTNAME FIRSTNAME`` plus the
   hidden state. The site only matches ``LAST FIRST`` order — hence the
   name-variant fan-out below.
3. Parse the rendered ``<table>`` with ``class="SearchResults"`` rows.
   Three columns: parcel id, owner, parcel location.

The detail page (``mode=legaldesc``) requires a navigation that we don't
replicate — the result-row data alone covers what the enrichment pipeline
needs (parcel id, owner, street address). City/zip are not exposed on
the result page; callers can fill those from a separate geocode step.

Public API: a single ``async def search_by_owner_name(...)``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Endpoint ──────────────────────────────────────────────────────────
BASE_URL = "https://www.mcrealestate.org"
SEARCH_URL = f"{BASE_URL}/search/commonsearch.aspx?mode=owner"

# ── Behaviour knobs ───────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.0
REQUEST_DELAY_MAX = 3.0
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
MAX_RESULTS = 10
MIN_MATCH_SCORE = 0.4
PAGE_SIZE = 25

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Tokens to drop when comparing names.
_NAME_NOISE = {
    "jr", "sr", "ii", "iii", "iv", "v",
    "trustee", "trustees", "tr", "trust",
    "etal", "et", "al",
    "and", "or",
    "&",
}
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokens(name: str) -> set[str]:
    """Tokenise + lowercase + drop noise words."""
    return {
        tok.lower()
        for tok in _TOKEN_RE.findall(name or "")
        if tok.lower() not in _NAME_NOISE
    }


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _name_variants(
    name: str,
    last_name: Optional[str],
    first_name: Optional[str],
) -> list[str]:
    """Yield the search-string variants we'll try, in order.

    The Auditor matches ``LAST FIRST`` (e.g. ``LONG WILLIAM``). We always
    submit in that order. ``first_name``/``last_name`` overrides win
    when supplied; otherwise we infer from ``name``.
    """
    variants: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        v = re.sub(r"\s+", " ", v).strip().upper()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    raw_tokens = [t for t in _TOKEN_RE.findall(name or "") if t]
    if last_name and first_name:
        add(f"{last_name} {first_name}")
    elif last_name:
        add(last_name)

    if len(raw_tokens) >= 2:
        # Heuristic: if input is "FIRST LAST", flip to "LAST FIRST".
        first, *_, last = raw_tokens[0], *raw_tokens[1:-1], raw_tokens[-1]
        add(f"{last} {first}")
        # Strip a likely middle initial / middle name and try last+first only
        if len(raw_tokens) >= 3:
            add(f"{raw_tokens[-1]} {raw_tokens[0]}")
        # Also try the original ordering — cheap insurance if input was
        # already LAST FIRST.
        add(" ".join(raw_tokens))
        # Last name only, as a wildcard fallback
        add(f"{last}*")
    elif len(raw_tokens) == 1:
        add(raw_tokens[0])

    return variants


def _extract_hidden_fields(html: str) -> dict[str, str]:
    """Pull every ``<input type="hidden">`` name=value pair from a page."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for inp in soup.find_all("input", attrs={"type": "hidden"}):
        n = inp.get("name")
        if n:
            out[n] = inp.get("value", "") or ""
    return out


def _parse_results(html: str) -> list[dict]:
    """Return [{parcel_id, owner_name, address}, ...] from a results page."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    for row in soup.find_all("tr", class_="SearchResults"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        parcel_id = cells[0].get_text(strip=True)
        owner = cells[1].get_text(strip=True)
        address = cells[2].get_text(strip=True)
        if not parcel_id:
            continue
        results.append({
            "parcel_id": parcel_id,
            "owner_name": owner,
            "address": address,
        })
    return results


def _polite_sleep() -> None:
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    **kwargs,
) -> Optional[requests.Response]:
    """Issue a request with exponential backoff. Returns None on persistent fail."""
    backoff = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            # Server-side errors → retry; client errors → bail
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} server error")
            return resp
        except (requests.RequestException, requests.HTTPError) as e:
            if attempt == MAX_RETRIES:
                logger.warning("Montgomery Auditor %s %s failed after %d attempts: %s",
                               method, url, attempt, e)
                return None
            logger.debug("Montgomery Auditor %s attempt %d failed (%s); sleeping %.1fs",
                         method, attempt, e, backoff)
            time.sleep(backoff)
            backoff *= 2
    return None


def _search_one_variant(
    session: requests.Session,
    query: str,
) -> list[dict]:
    """Hit the Auditor once with a single owner string. Returns parsed rows."""
    # GET to refresh ViewState — the form is single-use.
    r = _request_with_retries(session, "GET", SEARCH_URL)
    if r is None or r.status_code != 200:
        return []

    hidden = _extract_hidden_fields(r.text)
    form = dict(hidden)
    form.update({
        "inpOwner": query,
        "btSearch": "Search",
        "selSortBy": "PARID",
        "selSortDir": "asc",
        "selPageSize": str(PAGE_SIZE),
        "PageSize": str(PAGE_SIZE),
        "PageNum": "1",
        "hdAction": "Search",
        "mode": "OWNER",
    })

    _polite_sleep()
    r2 = _request_with_retries(
        session,
        "POST",
        SEARCH_URL,
        data=form,
        headers={
            "Referer": SEARCH_URL,
            "Origin": BASE_URL,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    if r2 is None or r2.status_code != 200:
        return []

    # Encoding: site is cp1252-ish; requests usually nails it from headers,
    # but be defensive.
    if r2.encoding is None or r2.encoding.lower() == "iso-8859-1":
        r2.encoding = r2.apparent_encoding or "utf-8"

    return _parse_results(r2.text)


def _sync_search(
    name: str,
    last_name: Optional[str],
    first_name: Optional[str],
) -> list[dict]:
    """Synchronous core. ``search_by_owner_name`` wraps this in a thread."""
    if not name and not last_name:
        return []

    variants = _name_variants(name or "", last_name, first_name)
    if not variants:
        return []

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Aggregate across variants, dedup by parcel_id, keep best score.
    by_parcel: dict[str, dict] = {}
    score_target = name or f"{first_name or ''} {last_name or ''}".strip()

    for variant in variants:
        rows = _search_one_variant(session, variant)
        logger.debug("Montgomery Auditor %r → %d rows", variant, len(rows))
        for row in rows:
            score = _jaccard(score_target, row["owner_name"])
            if score < MIN_MATCH_SCORE:
                continue
            existing = by_parcel.get(row["parcel_id"])
            if existing and existing["match_score"] >= score:
                continue
            by_parcel[row["parcel_id"]] = {
                "parcel_id": row["parcel_id"],
                "owner_name": row["owner_name"],
                "address": row["address"],
                # Default city = Dayton (Montgomery County seat — ~70% of parcels).
                # Smarty will correct to actual suburb on standardization if
                # the address is in Kettering / Vandalia / Centerville / etc.
                # Without this default, every Montgomery probate record failed
                # validation ("missing zip") because Smarty needs city to
                # standardize and infer ZIP.
                "city": "Dayton",
                "state": "OH",
                "zip": "",
                "assessed_value": "",
                "match_score": round(score, 3),
            }
        # If we already have a strong match, no need to keep querying.
        if any(r["match_score"] >= 0.8 for r in by_parcel.values()):
            break

    ranked = sorted(
        by_parcel.values(),
        key=lambda r: r["match_score"],
        reverse=True,
    )
    return ranked[:MAX_RESULTS]


async def search_by_owner_name(
    name: str,
    last_name: Optional[str] = None,
    first_name: Optional[str] = None,
) -> list[dict]:
    """Search Montgomery County Auditor for parcels owned by ``name``.

    Returns a list of candidate parcels (up to ``MAX_RESULTS``) sorted by
    Jaccard token-overlap match score against the input name. Empty list
    on no match or persistent failure.

    Each dict carries: ``parcel_id``, ``owner_name``, ``address``,
    ``city`` (empty — not exposed on results page), ``state="OH"``,
    ``zip`` (empty), ``assessed_value`` (empty — same reason),
    ``match_score`` (0.0–1.0).
    """
    return await asyncio.to_thread(_sync_search, name, last_name, first_name)


# ── CLI test harness ──────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Search Montgomery County (OH) Auditor by owner name."
    )
    parser.add_argument("name", help="Owner name (e.g. 'WILLIAM LONG')")
    parser.add_argument("--last", default=None, help="Optional last name override")
    parser.add_argument("--first", default=None, help="Optional first name override")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    results = asyncio.run(search_by_owner_name(args.name, args.last, args.first))
    print(json.dumps(results, indent=2))
    print(f"\n{len(results)} matches")
