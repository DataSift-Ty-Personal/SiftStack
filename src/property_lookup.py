"""Look up decedent property addresses from county assessor records.

For probate notices, the notice text contains the PR's mailing address but NOT
the decedent's property address. This module searches county property assessor
databases by the decedent's name to find their property address(es).

- Knox County: KGIS Maps (Playwright scrape — ArcGIS REST services require auth)
- Blount County: TPAD (TN Comptroller), JSON DataTables endpoint, needs a User-Agent
"""

import asyncio
import logging
import random
import re
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── TPAD (Blount County) ─────────────────────────────────────────────

TPAD_SEARCH_URL = "https://assessment.cot.tn.gov/TPAD/Search"
# The rows live behind the page's own DataTables call, not in the HTML.
TPAD_RESULTS_URL = "https://assessment.cot.tn.gov/TPAD/Search/GetSearchResults"
TPAD_BLOUNT_JUR = "005"  # Confirmed via scouting
# A User-Agent is REQUIRED: TPAD 403s anything that looks like a bare script.
TPAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": TPAD_SEARCH_URL,
}


def _tpad_normalize_address(raw: str) -> str:
    """TPAD prints the house number LAST: "LAKESHORE DR  5705" -> "5705 Lakeshore Dr".

    Left as-is, every Blount address fails downstream validation and Smarty
    standardization, so the record is dropped for a formatting reason rather
    than a data one.
    """
    raw = " ".join((raw or "").split())
    if not raw:
        return ""
    m = re.match(r"^(.*?)\s+(\d+[A-Za-z]?)$", raw)
    if m:
        street, number = m.group(1), m.group(2)
        raw = f"{number} {street}"
    return raw.title() if raw.isupper() else raw


def _tpad_lookup(name: str) -> list[dict]:
    """Search TPAD for properties owned by name in Blount County.

    Uses the DataTables JSON endpoint the page's own JS calls. Two things about
    this were silently broken, and together they meant NO Blount probate record
    could ever resolve an address:

      1. TPAD returns 403 to a request with no browser User-Agent. Bare
         `requests` was being refused outright on every single lookup.
      2. The HTML page only ships an EMPTY table shell (id `searchResultsTable`,
         not the `resultsTable` the parser looked for). Rows arrive from
         `POST /TPAD/Search/GetSearchResults`, which returns clean JSON.

    Both failures were quiet: the lookup returned [], the notice kept an empty
    address, and validation later dropped the record as "missing address".

    Args:
        name: Owner name to search (e.g. "SMITH JOHN")

    Returns:
        List of dicts with keys: owner, address, classification, parcel_id
    """
    try:
        resp = requests.post(
            TPAD_RESULTS_URL,
            data={
                "Jur": TPAD_BLOUNT_JUR,
                "Query": name,
                "SortBy": "",
                "PropertyType": "",
            },
            headers=TPAD_HEADERS,
            timeout=45,
        )
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as e:
        logger.warning("TPAD request failed for '%s': %s", name, e)
        return []
    except ValueError:
        logger.warning("TPAD returned non-JSON for '%s' (endpoint may have moved)", name)
        return []

    if not isinstance(rows, list):
        logger.warning("TPAD returned %s, expected a list, for '%s'", type(rows).__name__, name)
        return []

    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner = (row.get("owner") or "").strip()
        address = _tpad_normalize_address(row.get("propertyAddress") or "")
        if not owner or not address:
            continue
        results.append({
            "owner": owner,
            "address": address,
            "classification": (row.get("class") or "").strip(),
            "parcel_id": (row.get("parcelId") or "").strip(),
        })

    logger.debug("TPAD found %d properties for '%s'", len(results), name)
    return results


# ── KGIS (Knox County) ───────────────────────────────────────────────

KGIS_URL = "https://www.kgis.org/kgismaps/"
KGIS_OWNER_CARD_URL = "https://www.kgis.org/parcelreports/ownercard.aspx?id={parcel_id}"


class KgisSession:
    """One Chromium instance reused across many KGIS owner searches.

    THE REASON THIS EXISTS. `_kgis_lookup` used to launch a browser, navigate,
    search, and close for EVERY name. A probate record tries up to three name
    variants (full, shortened, maiden), so one record could pay three browser
    launches. On the Fly machine a launch is 10-15s, which made a single record
    take 40-60s and put the 12-month backfill's probate lookups at roughly 25
    hours of browser startup alone.

    Reusing the browser AND the page cuts that to one launch for the whole
    batch. The page stays on the KGIS map between searches; only the owner
    field is refilled.

    Use as an async context manager:

        async with KgisSession() as kgis:
            hits = await kgis.search("SMITH JOHN")
    """

    def __init__(self, headless: bool = True):
        self._pw = None
        self._browser = None
        self._page = None
        self._headless = headless
        self._dialog_hit = False

    async def __aenter__(self):
        await self._open()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _open(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        self._page = await self._browser.new_page()
        self._page.set_default_timeout(30000)

        async def handle_dialog(dialog):
            # KGIS reports an empty result set with a "Nothing found" alert.
            if "nothing found" in dialog.message.lower():
                self._dialog_hit = True
            await dialog.accept()

        self._page.on("dialog", handle_dialog)
        # domcontentloaded, not networkidle: map tiles never stop loading.
        await self._page.goto(KGIS_URL, wait_until="domcontentloaded")
        await self._page.wait_for_timeout(2000)

    async def close(self) -> None:
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass
        self._pw = self._browser = self._page = None

    async def _restart(self) -> None:
        """Recover from a wedged page without losing the whole batch."""
        logger.warning("KGIS session restarting")
        await self.close()
        await self._open()

    async def search(self, name: str, _retry: bool = True) -> list[dict]:
        """Search KGIS by owner name. Returns owner/address/parcel_id dicts."""
        if self._page is None:
            await self._open()

        results: list[dict] = []
        self._dialog_hit = False
        page = self._page

        try:
            # The owner field is only present once the Owner tab is selected.
            # After a previous search the tab is usually still active, so click
            # only when the field is missing.
            if not await page.locator("#txtOwnerName").is_visible():
                await page.get_by_role("cell", name="Owner", exact=True).click()
                await page.wait_for_selector("#txtOwnerName", state="visible", timeout=5000)

            await page.fill("#txtOwnerName", "")
            await page.fill("#txtOwnerName", name)
            await page.get_by_role("button", name="Search").click()
            await page.wait_for_timeout(3000)

            if self._dialog_hit:
                logger.debug("KGIS: no results for '%s'", name)
                return []

            # Results are innermost tables in the tabpanel: owner, address,
            # then parcel id plus action buttons.
            inner_tables = await page.locator("[role='tabpanel'] table table table").all()
            for tbl in inner_tables:
                rows = await tbl.locator("tr").all()
                if len(rows) >= 3:
                    owner_text = (await rows[0].inner_text()).strip()
                    address_text = (await rows[1].inner_text()).strip()
                    parcel_text = (await rows[2].locator("td").first.inner_text()).strip()
                    if owner_text and address_text:
                        results.append({
                            "owner": owner_text,
                            "address": address_text,
                            "parcel_id": parcel_text,
                        })
        except Exception as e:
            logger.warning("KGIS lookup failed for '%s': %s", name, e)
            if _retry:
                # A reused page can wedge (navigation, a stuck modal). Rebuild
                # once rather than failing every remaining record in the batch.
                await self._restart()
                return await self.search(name, _retry=False)

        logger.debug("KGIS found %d properties for '%s'", len(results), name)
        return results


async def _kgis_lookup(name: str, session: "KgisSession | None" = None) -> list[dict]:
    """Search KGIS for properties owned by name in Knox County.

    Pass `session` to reuse one browser across a batch; without it this opens
    and closes its own, which is correct but slow (see KgisSession).
    """
    if session is not None:
        return await session.search(name)
    async with KgisSession() as s:
        return await s.search(name)


async def _kgis_get_address_type(parcel_id: str) -> str:
    """Fetch the Address Type from the KGIS parcel details page.

    Returns the address type string (e.g. "DWELLING, SINGLE-FAMILY") or "".
    Uses requests (not Playwright) since the owner card page is static HTML.
    """
    url = KGIS_OWNER_CARD_URL.format(parcel_id=parcel_id)
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Look for "Address Type:" label
        label = soup.find(string=re.compile(r"Address Type", re.IGNORECASE))
        if label:
            # The value is in the next cell or sibling
            parent = label.find_parent("td")
            if parent:
                next_td = parent.find_next_sibling("td")
                if next_td:
                    return next_td.get_text(strip=True)
        return ""
    except Exception as e:
        logger.debug("KGIS owner card fetch failed for %s: %s", parcel_id, e)
        return ""


# ── Property selection logic ──────────────────────────────────────────

RESIDENTIAL_KEYWORDS = {
    "residential", "dwelling", "single-family", "single family",
    "duplex", "condo", "townhouse", "mobile home",
}


def _is_residential(classification: str) -> bool:
    """Check if a property classification indicates residential."""
    lower = classification.lower()
    return any(kw in lower for kw in RESIDENTIAL_KEYWORDS)


def _select_best_property(
    results: list[dict],
    pr_street: str = "",
) -> dict | None:
    """Select the best property match from search results.

    Priority:
    1. Filter to residential properties only
    2. Prefer where property address matches the PR's mailing address (owner-occupied)
    3. Take the first residential result if no mailing match
    """
    residential = [r for r in results if _is_residential(r.get("classification", "Residential"))]

    if not residential:
        # If no classification data (KGIS results), treat all as candidates
        residential = results

    if not residential:
        return None

    if len(residential) == 1:
        return residential[0]

    # Try to match PR's street address to find the primary residence
    if pr_street:
        pr_norm = re.sub(r"[^a-z0-9]", "", pr_street.lower())
        for prop in residential:
            prop_norm = re.sub(r"[^a-z0-9]", "", prop.get("address", "").lower())
            if pr_norm and prop_norm and (pr_norm in prop_norm or prop_norm in pr_norm):
                logger.info("Property matched PR mailing address: %s", prop["address"])
                return prop

    # No mailing match — take first residential
    return residential[0]


# ── Name formatting ───────────────────────────────────────────────────

def _format_name_for_search(name: str) -> str:
    """Convert 'JOHN SMITH' to 'SMITH JOHN' for county assessor search.

    County assessor databases index by "LAST FIRST" format.
    Handles common patterns:
      "JOHN SMITH"           → "SMITH JOHN"
      "JOHN A. SMITH"        → "SMITH JOHN A"
      "JOHN SMITH JR"        → "SMITH JOHN" (drop suffix)
      "JOHN AND JANE SMITH"  → "SMITH JOHN"  (drop second person)
    """
    if not name:
        return ""

    # Search on the primary name only. "<FIRST> <LAST> AKA <NICKNAME> <LAST>"
    # used to be sent to the assessor whole, which matches nothing.
    aliases = _split_aliases(name)
    if aliases:
        name = aliases[0]

    # Remove common suffixes
    clean = re.sub(r"\b(?:JR|SR|II|III|IV|M\.?D\.?)\b\.?", "", name, flags=re.IGNORECASE).strip()

    # Handle "FIRST AND SPOUSE LAST" — keep first name + last name
    spouse_match = re.match(r"(\w+)\s+(?:AND|&)\s+\w+\s+(\w+)$", clean, re.IGNORECASE)
    if spouse_match:
        clean = f"{spouse_match.group(1)} {spouse_match.group(2)}"
    else:
        # Fallback: remove "AND [name]..." entirely
        clean = re.sub(r"\s+(?:AND|&)\s+.*", "", clean, flags=re.IGNORECASE).strip()

    # Remove punctuation except spaces
    clean = re.sub(r"[.,']", "", clean).strip()

    parts = clean.split()
    if len(parts) < 2:
        return clean.upper()

    # Assume last token is the last name (most common pattern)
    last = parts[-1]
    firsts = parts[:-1]
    return f"{last} {' '.join(firsts)}".upper()


def _shorten_search_name(formatted_name: str) -> str | None:
    """Produce a shorter 'LAST FIRST' variant by dropping middle names.

    If the formatted name is already 'LAST FIRST' (2 parts), returns None.
    For 'LAST FIRST MIDDLE', returns 'LAST FIRST'.
    """
    parts = formatted_name.split()
    if len(parts) <= 2:
        return None  # Already short — no shorter variant
    return f"{parts[0]} {parts[1]}"


def _maiden_name_variant(decedent_name: str) -> str | None:
    """Try the penultimate name as surname for maiden+married name patterns.

    Many female decedents have names like "LULA ELIZABETH MASSIE JONES" where
    "Jones" is the married name and "Massie" is the maiden name. The property
    may be registered under the maiden name.

    For 4+ part names, returns 'PENULTIMATE FIRST' (e.g. 'MASSIE LULA').
    Returns None for shorter names or if the pattern doesn't apply.
    """
    if not decedent_name:
        return None

    aliases = _split_aliases(decedent_name)
    if aliases:
        decedent_name = aliases[0]

    # Work from the original decedent name (before _format_name_for_search)
    clean = re.sub(r"\b(?:JR|SR|II|III|IV)\b\.?", "", decedent_name, flags=re.IGNORECASE).strip()
    clean = re.sub(r"[.,'']", "", clean).strip()
    parts = clean.split()

    if len(parts) < 4:
        return None  # Need at least FIRST MIDDLE MAIDEN MARRIED

    first = parts[0]
    maiden = parts[-2]  # Penultimate = maiden name
    return f"{maiden} {first}".upper()


# ── Owner-of-record guard ─────────────────────────────────────────────
#
# THE BUG THIS CLOSES. The lookup used to take whatever the assessor search
# returned and keep the first residential row, with no check that the owner of
# record was the decedent. Both TPAD and KGIS match loosely, so a query built
# from a damaged name returned a stranger's house and it was uploaded as the
# estate's property. Measured on live Blount notices, 2026-11-11: a decedent
# whose name had been parsed with a line-wrap fragment in it ("<FIRST> <LAST>
# DE- CEASED") was searched as "DE- <FIRST>", and the assessor answered with a
# living couple whose surname merely began with "DE" and whose first name
# matched. Three of the four addresses "found" in that run were wrong houses.
#
# A missed lookup costs one lead. A wrong one sends a probate letter and a
# caller to an unrelated family. So a result is kept only when the owner of
# record carries the decedent's surname AND first name.

_ALIAS_SPLIT_RE = re.compile(
    r"\s+(?:A[./]?K[./]?A\.?|F[./]?K[./]?A\.?|N[./]?K[./]?A\.?"
    r"|ALSO\s+KNOWN\s+AS|FORMERLY\s+KNOWN\s+AS|NOW\s+KNOWN\s+AS)\s+",
    re.IGNORECASE,
)
_NAME_SUFFIX_TOKENS = {"JR", "SR", "II", "III", "IV"}
_FIRST_NAME_PREFIX_MIN = 4   # "JACK" covers "JACKY"; "ANN" must not cover "ANNETTE"


def _split_aliases(name: str) -> list[str]:
    """['ROBERTA ANN HALE', 'BOBBIE HALE'] from '... AKA ...'. Primary first."""
    return [p.strip(" ,") for p in _ALIAS_SPLIT_RE.split(name or "") if p.strip(" ,")]


def _person_tokens(name: str) -> list[str]:
    """Upper-case name tokens, punctuation and generational suffixes removed."""
    clean = re.sub(r"[^A-Za-z\s\-]", " ", name or "").upper().replace("-", " ")
    return [t for t in clean.split() if t not in _NAME_SUFFIX_TOKENS]


def _first_name_matches(first: str, owner_tokens: set) -> bool:
    for tok in owner_tokens:
        if tok == first:
            return True
        short, long_ = (first, tok) if len(first) <= len(tok) else (tok, first)
        if len(short) >= _FIRST_NAME_PREFIX_MIN and long_.startswith(short):
            return True
    return False


def _owner_matches_decedent(decedent_name: str, owner: str) -> bool:
    """True when the owner of record is plausibly the decedent.

    Surname: the decedent's last name, the maiden name of a four-part name, or
    the last name of any alias. First name: the decedent's, or any alias's.
    Both must appear among the owner's tokens.
    """
    names = _split_aliases(decedent_name)
    if not names:
        return False
    primary = _person_tokens(names[0])
    if len(primary) < 2:
        return False
    surnames = {primary[-1]}
    if len(primary) >= 4:
        surnames.add(primary[-2])
    firsts = {primary[0]}
    for alias in names[1:]:
        toks = _person_tokens(alias)
        if len(toks) >= 2:
            firsts.add(toks[0])
            surnames.add(toks[-1])

    owner_tokens = set(_person_tokens(owner))
    if not (surnames & owner_tokens):
        return False
    return any(_first_name_matches(f, owner_tokens) for f in firsts)


def _keep_owner_matches(results: list[dict], decedent_name: str) -> list[dict]:
    """Drop every result whose owner of record is not the decedent."""
    kept = [r for r in results if _owner_matches_decedent(decedent_name, r.get("owner", ""))]
    if results and not kept:
        logger.info(
            "  Rejected %d result(s): owner of record is not the decedent (first was %s)",
            len(results), (results[0].get("owner") or "?").strip(),
        )
    return kept


def _search_variants(decedent_name: str) -> list[tuple[str, str]]:
    """Ordered (label, query) pairs to try against the assessor, deduplicated.

    Full name, then without middle names, then the maiden name, then the same
    for each alias. The label is empty for the first attempt.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, query: str | None) -> None:
        if query and query not in seen:
            seen.add(query)
            out.append((label, query))

    full = _format_name_for_search(decedent_name)
    add("", full)
    add("shorter name", _shorten_search_name(full) if full else None)
    add("maiden name", _maiden_name_variant(decedent_name))
    for alias in _split_aliases(decedent_name)[1:]:
        alias_full = _format_name_for_search(alias)
        add("alias", alias_full)
        add("alias, shorter", _shorten_search_name(alias_full) if alias_full else None)
    return out


# ── TPAD address normalization ────────────────────────────────────────

def _normalize_tpad_address(raw_addr: str) -> str:
    """Normalize TPAD address format.

    TPAD returns addresses like "MADISON AVE 1605" (number after street).
    Convert to standard format "1605 MADISON AVE".
    """
    if not raw_addr:
        return ""

    # Check if it ends with a number (TPAD format: "STREET NAME 1234")
    match = re.match(r"^(.+?)\s+(\d+)\s*$", raw_addr.strip())
    if match:
        street = match.group(1).strip()
        number = match.group(2)
        return f"{number} {street}"

    return raw_addr.strip()


# ── Blount County city lookup ─────────────────────────────────────────

# Map of common Blount County areas/cities
BLOUNT_DEFAULT_CITY = "Maryville"


# ── Main entry point ──────────────────────────────────────────────────


async def lookup_decedent_properties(notices: list) -> None:
    """Look up property addresses for probate notices with decedent names.

    Modifies notices in-place, setting address/city/state/zip for each
    probate notice where the decedent's property can be found.

    Args:
        notices: List of NoticeData objects (probate only, with decedent_name set)
    """
    if not notices:
        return

    # One browser for the entire batch. Opened lazily so a Blount-only batch
    # (TPAD is plain HTTP) never pays for Chromium at all.
    kgis: KgisSession | None = None

    async def _kgis(name: str) -> list[dict]:
        nonlocal kgis
        if kgis is None:
            kgis = KgisSession()
            await kgis._open()
        return await kgis.search(name)

    try:
        return await _lookup_loop(notices, _kgis)
    finally:
        if kgis is not None:
            await kgis.close()


async def _lookup_loop(notices: list, _kgis) -> None:
    found = 0
    failed = 0
    skipped = 0

    for i, notice in enumerate(notices):
        if not notice.decedent_name:
            skipped += 1
            continue

        if notice.address:
            # Already has an address (shouldn't happen for probate, but skip)
            skipped += 1
            continue

        search_name = _format_name_for_search(notice.decedent_name)
        if not search_name:
            skipped += 1
            continue

        logger.info(
            "[%d/%d] Looking up property for %s (%s County)...",
            i + 1, len(notices), search_name, notice.county,
        )

        try:
            # Every variant goes through the owner-of-record guard, and a
            # variant whose results are all rejected falls through to the next
            # one, exactly as an empty result set does.
            variants = _search_variants(notice.decedent_name)
            results: list[dict] = []
            if notice.county.lower() == "knox":
                for n_try, (label, query) in enumerate(variants):
                    if label:
                        logger.info("  Retrying with %s: %s", label, query)
                    if n_try:
                        await asyncio.sleep(random.uniform(1.5, 2.5))
                    results = _keep_owner_matches(await _kgis(query), notice.decedent_name)
                    if results:
                        break
                # KGIS doesn't include classification in search results.
                # For single results, just take it. For multiple, check details.
                if len(results) > 1:
                    # Fetch address type for each to filter residential
                    for r in results:
                        addr_type = await _kgis_get_address_type(r["parcel_id"])
                        r["classification"] = addr_type
            elif notice.county.lower() == "blount":
                for label, query in variants:
                    if label:
                        logger.info("  Retrying with %s: %s", label, query)
                    results = _keep_owner_matches(_tpad_lookup(query), notice.decedent_name)
                    if results:
                        break
            else:
                logger.debug("Unsupported county for property lookup: %s", notice.county)
                skipped += 1
                continue

            if not results:
                logger.info("  No properties found for %s", search_name)
                failed += 1
            else:
                best = _select_best_property(results, notice.owner_street)
                if best:
                    raw_addr = best["address"]

                    if notice.county.lower() == "blount":
                        raw_addr = _normalize_tpad_address(raw_addr)

                    notice.address = raw_addr
                    notice.state = "TN"

                    # For Knox, KGIS results don't include city/zip in search list
                    # We'll rely on Smarty standardization to fill those in
                    if notice.county.lower() == "knox":
                        notice.city = "Knoxville"  # Default, Smarty will correct
                    elif notice.county.lower() == "blount":
                        notice.city = BLOUNT_DEFAULT_CITY

                    notice.parcel_id = best.get("parcel_id", "")
                    logger.info(
                        "  Found: %s (parcel %s)",
                        notice.address, notice.parcel_id or "?",
                    )
                    found += 1
                else:
                    logger.info("  No residential property found for %s", search_name)
                    failed += 1

        except Exception as e:
            logger.warning("  Property lookup failed for %s: %s", search_name, e)
            failed += 1

        # Be polite to county sites
        await asyncio.sleep(random.uniform(2.0, 3.0))

        if (i + 1) % 10 == 0:
            logger.info("Property lookup progress: %d/%d processed", i + 1, len(notices))

    logger.info(
        "Property lookup complete: %d found, %d not found, %d skipped (of %d total)",
        found, failed, skipped, len(notices),
    )
