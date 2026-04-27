"""Enrich notices with obituary-confirmed deceased owner data.

Searches for obituaries matching each property owner, parses them with
Claude Haiku to confirm identity and extract survivors, then identifies
the decision-maker (heir/executor) for each deceased owner.

This catches deceased owners the county tax API hasn't flagged yet.
"""

import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from datetime import datetime

import llm_client
from models import NoticeData

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024
SEARCH_DELAY_MIN = 0.5
SEARCH_DELAY_MAX = 1.0
PARALLEL_WORKERS = 6  # Concurrent heir verifications
FETCH_TIMEOUT = 20
MAX_OBITUARY_TEXT = 6000
MAX_ADDRESS_TEXT = 15000  # Larger limit for people search pages (CBC has 250+ results)

# Maximum years between DOD and notice filing date to accept an obituary match.
# Probate is typically filed within 1-2 years of death. 3 years gives margin.
MAX_DOD_GAP_YEARS = 3


def _dod_sanity_check(dod_str: str, notice: "NoticeData") -> bool:
    """Reject obituary matches where DOD is implausibly far from the notice date.

    Returns True if the DOD is plausible, False if it should be rejected.
    """
    if not dod_str or not dod_str.strip():
        return True  # No DOD to check — let other signals decide

    reference_date = notice.date_added or ""
    if not reference_date:
        return True  # No filing date to compare against

    try:
        dod = datetime.strptime(dod_str.strip(), "%Y-%m-%d")
        ref = datetime.strptime(reference_date.strip(), "%Y-%m-%d")
        gap_years = (ref - dod).days / 365.25
        if gap_years > MAX_DOD_GAP_YEARS:
            logger.warning(
                "  DOD sanity check FAILED: DOD %s is %.1f years before filing date %s "
                "(max %d years) — likely wrong person",
                dod_str, gap_years, reference_date, MAX_DOD_GAP_YEARS,
            )
            return False
        if gap_years < -0.5:
            # DOD is in the future relative to notice — clearly wrong
            logger.warning(
                "  DOD sanity check FAILED: DOD %s is after filing date %s — invalid match",
                dod_str, reference_date,
            )
            return False
    except ValueError:
        return True  # Unparseable date — don't block on format issues

    return True


# Domains that always return HTTP 403 — skip direct fetch, go straight to Firecrawl
KNOWN_403_DOMAINS = {
    "dignitymemorial.com",
    "tributearchive.com",
    "echovita.com",
    "funeralhomes.com",
}

# Known obituary domains — filter search results to these
OBITUARY_DOMAINS = {
    "legacy.com",
    "dignitymemorial.com",
    "echovita.com",
    "tributearchive.com",
    "findagrave.com",
    "obituaries.com",
    "funeralhomes.com",
    "meaningfulfunerals.net",
    "mem.com",
    "forevermissed.com",
    "knoxnews.com",
    "thedailytimes.com",
}

# Legal suffixes to strip from tax API owner names before searching
_SUFFIX_RE = re.compile(
    r"\s*\(?\s*(?:LIFE\s+EST(?:ATE)?|PERSONAL\s+REP(?:RESENTATIVE)?|"
    r"TRUSTEE|TRUST|ETAL|ET\s+AL)\s*\)?\s*$",
    re.IGNORECASE,
)

# Suffixes that appear in notice owner_name but not tax API names
_NOTICE_SUFFIX_RE = re.compile(
    r",?\s*(?:Tenants?\s+By\s+The\s+Entirety|As\s+Joint\s+Tenants?|Joint\s+Tenants?)"
    r"|,?\s*(?:An?\s+)?(?:Unmarried|Married|Single)\s*(?:Person|Woman|Man)?"
    r"|,?\s*(?:Husband\s+And\s+Wife|Wife\s+And\s+Husband)"
    r"|\s+(?:Et\s+Al|Etal)\b",
    re.IGNORECASE,
)

# Business entity / trust / estate patterns — imported from shared config
import config as _cfg
_BUSINESS_RE = _cfg.BUSINESS_RE
_TRUST_NAME_RE = _cfg.TRUST_NAME_RE
_ESTATE_OF_RE = _cfg.ESTATE_OF_RE

# Common nickname ↔ formal name pairs for search fallback
_NICKNAME_MAP: dict[str, list[str]] = {
    "robert": ["bob", "bobby", "rob", "robbie"],
    "william": ["bill", "billy", "will", "willie", "willy"],
    "richard": ["rick", "dick", "rich"],
    "james": ["jim", "jimmy", "jamie"],
    "john": ["jack", "johnny"],
    "charles": ["chuck", "charlie"],
    "thomas": ["tom", "tommy"],
    "joseph": ["joe", "joey"],
    "michael": ["mike", "mikey"],
    "david": ["dave", "davey"],
    "daniel": ["dan", "danny"],
    "edward": ["ed", "eddie", "ted", "teddy"],
    "kenneth": ["ken", "kenny"],
    "ronald": ["ron", "ronnie"],
    "donald": ["don", "donnie"],
    "steven": ["steve", "stephen"],
    "stephen": ["steve", "steven"],
    "patrick": ["pat", "patty"],
    "raymond": ["ray"],
    "timothy": ["tim", "timmy"],
    "anthony": ["tony"],
    "lawrence": ["larry"],
    "gerald": ["jerry"],
    "eugene": ["gene"],
    "margaret": ["maggie", "peggy", "marge"],
    "elizabeth": ["liz", "beth", "betty", "eliza"],
    "patricia": ["pat", "patty", "trish"],
    "barbara": ["barb", "barbie"],
    "catherine": ["cathy", "kate", "kathy"],
    "katherine": ["kathy", "kate", "cathy"],
    "dorothy": ["dot", "dotty"],
    "virginia": ["ginny"],
    "deborah": ["debbie", "deb"],
    "sandra": ["sandy"],
}

# Build reverse map: nickname → [formal names]
_NICKNAME_REVERSE: dict[str, list[str]] = {}
for _formal, _nicks in _NICKNAME_MAP.items():
    for _nick in _nicks:
        _NICKNAME_REVERSE.setdefault(_nick, []).append(_formal)


def _get_name_variants(first_name: str) -> list[str]:
    """Return alternate first names (nicknames/formal) for search fallback."""
    key = first_name.lower().strip()
    variants = set()
    # Formal → nicknames
    if key in _NICKNAME_MAP:
        variants.update(_NICKNAME_MAP[key])
    # Nickname → formal names
    if key in _NICKNAME_REVERSE:
        variants.update(_NICKNAME_REVERSE[key])
    variants.discard(key)
    return list(variants)


def _extract_personal_from_trust_estate(raw_name: str) -> str | None:
    """Extract a personal name from trust/estate ownership patterns.

    Examples:
        "JOHN DOE TRUST" → "JOHN DOE"
        "THE JOHN DOE REVOCABLE TRUST" → "JOHN DOE"
        "ESTATE OF MARY SMITH" → "MARY SMITH"
        "JOHN DOE LIVING TRUST" → "JOHN DOE"
        "FIRST TENNESSEE BANK TRUST" → None (business entity)
    """
    name = raw_name.strip()

    for pattern in (_TRUST_NAME_RE, _ESTATE_OF_RE):
        m = pattern.match(name)
        if m:
            extracted = m.group(1).strip()
            if _BUSINESS_RE.search(extracted):
                return None
            if len(extracted.split()) >= 2:
                return extracted

    return None


SYSTEM_PROMPT = (
    "You analyze obituaries to determine if a deceased person matches a property owner. "
    "Return ONLY valid JSON with no markdown formatting, no code fences, no explanation."
)

OBITUARY_PROMPT = """\
I have a property record with this owner information:
- Owner name: {owner_name}
- Property city: {city}
- Property state: {state}
- Property address: {address}

Below is text from a potential obituary. Determine if this obituary is for the same person \
as the property owner. Consider: name match (first + last name must match; middle name/initial \
is bonus confirmation), location match (same city, county, or state as the property), and timeline \
plausibility (death within last 5 years is typical for active foreclosure/tax sale records).

Return a JSON object with these exact keys:
- "match": true if this obituary is very likely for the property owner, false otherwise
- "confidence": "high", "medium", or "low"
- "full_name": the deceased person's full name from the obituary
- "date_of_death": date of death in YYYY-MM-DD format (empty string if not found)
- "city": city where they lived/died
- "age_at_death": integer age at death (0 if not found)
- "survivors": array of objects with "name", "relationship", and "city" keys for each surviving family member. \
Extract ALL survivors mentioned anywhere in the text. Look for "survived by", "leaves behind", "cherished by", \
"loving family includes", and any named persons. Include: spouse/wife/husband, children (sons, daughters), \
stepchildren, grandchildren, siblings (brothers, sisters), parents, nieces, nephews, and in-laws. \
If a relationship is unclear from context, use "family_member" rather than omitting the person. \
Include partial names (e.g. just a first name) — use the deceased's last name if only a first name is given. \
Include full names when available. (city is where the survivor lives if mentioned, empty string if not stated)
- "preceded_in_death": array of names of family members who predeceased them
- "executor_named": name of executor/personal representative if mentioned, empty string if not

Important: Only set "match" to true if the first AND last name match the owner. \
Common names need location confirmation. Be conservative — a false negative is better \
than a false positive.

Obituary text:
{obituary_text}"""


def parse_tax_owner_name(raw: str) -> list[str]:
    """Convert tax API owner name to search-friendly format(s).

    Tax API names are "LAST FIRST MIDDLE" with possible legal suffixes.
    Returns a list of names including middle initial when available,
    since common names need the initial for accurate obituary matching.

    Examples:
        "WILLIAMS DANIEL H" → ["Daniel H Williams"]
        "WILLIAMS DANIEL H & CHRISTINE C" → ["Daniel H Williams", "Christine Williams"]
        "BLALOCK GARY W % BLALOCK MISTY D" → ["Gary W Blalock"]
        "JONES ROBERT (LIFE EST)" → ["Robert Jones"]
        "EASTSIDE REAL ESTATE LLC" → [] (business entity)
    """
    if not raw or not raw.strip():
        return []

    name = raw.strip()

    # Extract personal name from trust/estate structures before business check
    # "ESTATE OF X" → FIRST LAST format, return directly
    # "X TRUST" → still LAST FIRST in tax records, continue parsing below
    if _ESTATE_OF_RE.match(name):
        personal = _extract_personal_from_trust_estate(name)
        if personal:
            return [personal.title()]
    personal = _extract_personal_from_trust_estate(name)
    if personal:
        name = personal

    # Skip business entities
    if _BUSINESS_RE.search(name):
        return []

    # Strip legal suffixes
    name = _SUFFIX_RE.sub("", name).strip()

    # Handle care-of (%) — only search the primary name (before %)
    if "%" in name:
        name = name.split("%")[0].strip()

    # Handle joint owners with &
    parts = re.split(r"\s*&\s*", name)

    results = []
    last_name = ""

    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue

        tokens = part.split()
        if len(tokens) < 2:
            continue

        if i == 0:
            # First part: "LAST FIRST [MIDDLE]"
            last_name = tokens[0]
            first_name = tokens[1]
            middle = tokens[2] if len(tokens) >= 3 else ""
            if middle and len(middle) <= 2:
                results.append(f"{first_name.title()} {middle.title()} {last_name.title()}")
            else:
                results.append(f"{first_name.title()} {last_name.title()}")
        else:
            # Subsequent parts: "FIRST [MIDDLE]" or "LAST FIRST [MIDDLE]"
            # If second token is a single char (middle initial), it's "FIRST MIDDLE"
            # inheriting previous last name. Otherwise check for new last name.
            is_middle_initial = len(tokens) == 2 and len(tokens[1]) <= 2
            if not is_middle_initial and len(tokens) >= 2 and tokens[0].upper() != last_name.upper():
                # Different last name: "BLALOCK MISTY D" (3+ tokens, first differs)
                new_last = tokens[0]
                first_name = tokens[1]
                middle = tokens[2] if len(tokens) >= 3 else ""
                if middle and len(middle) <= 2:
                    results.append(f"{first_name.title()} {middle.title()} {new_last.title()}")
                else:
                    results.append(f"{first_name.title()} {new_last.title()}")
            else:
                # Same last name: "CHRISTINE C" (inherits WILLIAMS)
                first_name = tokens[0]
                results.append(f"{first_name.title()} {last_name.title()}")

    return results


def _parse_notice_owner_name(raw: str) -> list[str]:
    """Parse owner_name from notice text (already in FIRST [MIDDLE] LAST order).

    Unlike parse_tax_owner_name (which handles LAST FIRST MIDDLE tax API format),
    notice owner names are in natural order and just need cleanup + splitting.

    Examples:
        "DEBRA BELL" → ["Debra Bell"]
        "MICHAEL BRANDON HASTING" → ["Michael Brandon Hasting"]
        "STEPHEN D. HANSON AND CHELSEA HANSON" → ["Stephen D. Hanson", "Chelsea Hanson"]
        "BRANDY N. HUMPHREY AND HUSBAND MICHAEL A. HUMPHREY, TENANTS BY THE ENTIRETY"
            → ["Brandy N. Humphrey", "Michael A. Humphrey"]
    """
    if not raw or not raw.strip():
        return []

    name = raw.strip()

    # Extract personal name from trust/estate structures before business check
    personal = _extract_personal_from_trust_estate(name)
    if personal:
        name = personal

    # Skip business entities
    if _BUSINESS_RE.search(name):
        return []

    # Strip notice-specific suffixes (Tenants By The Entirety, etc.)
    name = _NOTICE_SUFFIX_RE.sub("", name).strip()
    # Strip tax-API suffixes too (LIFE EST, ETAL, etc.)
    name = _SUFFIX_RE.sub("", name).strip()

    # Split on " AND " (word) or "&" (symbol)
    parts = re.split(r"\s+AND\s+|\s*&\s*", name, flags=re.IGNORECASE)

    results = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Strip relational prefixes: "Husband ", "Wife ", "Spouse "
        part = re.sub(r"^\s*(?:husband|wife|spouse)\s+", "", part, flags=re.IGNORECASE).strip()
        if part:
            results.append(part.title())

    return [r for r in results if r]


def _is_obituary_url(url: str) -> bool:
    """Check if a URL is from a known obituary site."""
    url_lower = url.lower()
    for domain in OBITUARY_DOMAINS:
        if domain in url_lower:
            return True
    # Also match generic obituary URL patterns
    if "/obituar" in url_lower or "/memorial" in url_lower:
        return True
    return False


def _search_obituary(name: str, city: str, extra_terms: str = "") -> list[dict]:
    """Search DuckDuckGo for obituary pages matching the person.

    Args:
        name: Person's full name.
        city: City for geo-filtering (empty string to omit).
        extra_terms: Additional search terms to replace "obituary" keyword
                     (e.g. '"death notice" OR "funeral"').

    Returns list of {url, title, snippet} for obituary-domain results.
    """
    keyword = extra_terms if extra_terms else "obituary"
    query = f'{name} {keyword} Tennessee' if not city else f'{name} {keyword} {city} Tennessee'

    try:
        results = DDGS().text(query, max_results=8, backend="google,duckduckgo,brave")
    except Exception as e:
        logger.debug("Search failed for '%s': %s", query, e)
        return []

    obituary_results = []
    for r in results:
        url = r.get("href", "")
        if _is_obituary_url(url):
            obituary_results.append({
                "url": url,
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
            })

    # Also include non-obituary-domain results that mention "obituary" in title/snippet
    for r in results:
        url = r.get("href", "")
        if url in [o["url"] for o in obituary_results]:
            continue
        title = r.get("title", "").lower()
        snippet = r.get("body", "").lower()
        if ("obituary" in title or "obituary" in snippet or "passed away" in snippet
                or "death notice" in title or "death notice" in snippet
                or "funeral" in title or "funeral" in snippet):
            obituary_results.append({
                "url": url,
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
            })

    return obituary_results[:8]  # Process all DDG results (was 5, raised for coverage)


def _extract_structured_text(html: str, url: str) -> str:
    """Extract obituary text from structured data in React SPA pages.

    legacy.com renders client-side — BeautifulSoup gets ~75 chars from a 390KB
    page. The actual obituary text lives in:
    1. JSON-LD (<script type="application/ld+json">) → articleBody
    2. window.__INITIAL_STATE__ → personStore.displayText.text
    """
    # Method 1: JSON-LD
    try:
        for match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            try:
                data = json.loads(match.group(1))
                # Handle both single object and array
                items = data if isinstance(data, list) else [data]
                for item in items:
                    article_body = ""
                    if item.get("@type") in ("NewsArticle", "Article", "Obituary"):
                        article_body = item.get("articleBody", "")
                    elif item.get("@type") == "Person" and item.get("description"):
                        article_body = item.get("description", "")
                    if article_body and len(article_body) >= 100:
                        # Clean HTML tags from articleBody
                        text = re.sub(r"<br\s*/?>", "\n", article_body)
                        text = re.sub(r"<[^>]+>", "", text)
                        text = re.sub(r"\n{3,}", "\n\n", text)
                        logger.debug("JSON-LD extracted %d chars from %s", len(text), url)
                        return text.strip()
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
    except Exception:
        pass

    # Method 2: window.__INITIAL_STATE__ (legacy.com old format)
    try:
        state_match = re.search(
            r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*(?:</script>|window\.)",
            html, re.DOTALL,
        )
        if state_match:
            state = json.loads(state_match.group(1))
            # Navigate: personStore.displayText.text
            person_store = state.get("personStore", {})
            display_text = person_store.get("displayText", {})
            text = display_text.get("text", "")
            if text and len(text) >= 100:
                text = re.sub(r"<br\s*/?>", "\n", text)
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
                logger.debug("__INITIAL_STATE__ extracted %d chars from %s", len(text), url)
                return text.strip()
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    except Exception:
        pass

    return ""


def _fetch_cached_text(url: str) -> str:
    """Fetch obituary text from Google Cache or Wayback Machine.

    Called when primary fetch gets a 403 (e.g., DignityMemorial.com).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    # Try Wayback Machine
    try:
        wb_url = f"https://web.archive.org/web/2/{url}"
        resp = requests.get(wb_url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code == 200:
            content = resp.text[:200000]
            # Try structured extraction first
            structured = _extract_structured_text(content, wb_url)
            if structured and len(structured) >= 100:
                logger.debug("Wayback Machine extracted %d chars for %s", len(structured), url)
                return structured[:MAX_OBITUARY_TEXT]
            # BeautifulSoup fallback
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            if len(text) >= 100:
                logger.debug("Wayback Machine BS4 extracted %d chars for %s", len(text), url)
                return text[:MAX_OBITUARY_TEXT]
    except Exception as e:
        logger.debug("Wayback Machine fetch failed for %s: %s", url, e)

    return ""


def _is_listing_url(url: str) -> bool:
    """Check if a URL is a generic listing page rather than a specific obituary."""
    url_lower = url.lower()
    # legacy.com listing pages: /us/obituaries/local/tennessee/...
    if "legacy.com" in url_lower and "/local/" in url_lower:
        return True
    # Generic search/listing patterns
    if any(p in url_lower for p in ["/search?", "/results?", "/browse/", "/listing"]):
        return True
    return False


def _refetch_specific_obituary(
    name: str,
    city: str,
    original_url: str,
    api_key: str,
    address: str = "",
) -> tuple[dict | None, str, str]:
    """Re-search for a specific obituary page when original match was a listing page.

    Returns (parsed, url, source_type) or (None, "", "") if unsuccessful.
    """
    queries = [
        f'"{name}" obituary site:legacy.com',
        f'"{name}" obituary {city} Tennessee "survived by"',
    ]

    for query in queries:
        try:
            results = DDGS().text(query, max_results=5, backend="google,duckduckgo,brave")
        except Exception:
            continue

        for r in results:
            url = r.get("href", "")
            if not url or not _is_obituary_url(url):
                continue
            if _is_listing_url(url):
                continue
            if url == original_url:
                continue

            page_text = _fetch_page_text(url)
            if not page_text or len(page_text) < 100:
                continue

            parsed = _parse_obituary_with_llm(
                obituary_text=page_text,
                owner_name=name,
                city=city,
                address=address,
                api_key=api_key,
            )
            if parsed and parsed.get("confidence") in ("high", "medium"):
                logger.info("  Re-search found specific obituary: %s", url)
                return parsed, url, "full_page"

        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    return None, "", ""


AGGRESSIVE_SURVIVOR_PROMPT = """\
This is an obituary for {owner_name}. List every person named in the text who \
appears to still be alive (a survivor, not someone who predeceased them).

Include partial names if that is all the text provides — use the deceased's last name \
if only a first name is given. Infer the relationship from context words like wife, husband, \
son, daughter, brother, sister, child, grandchild, stepson, stepdaughter, niece, nephew, \
parent, mother, father, in-law, etc. If the relationship is ambiguous, use "family_member" \
rather than omitting the person. Do NOT skip anyone — even distant relatives matter.

Return a JSON object with ONE key:
- "survivors": array of objects, each with "name" (string) and "relationship" (string)

Return an empty array if no living persons are named.

Obituary text:
{obituary_text}"""


SNIPPET_SURVIVOR_PROMPT = """\
I have confirmed this property owner is deceased. Extract ANY family member \
names from these search result snippets.

Owner name: {owner_name}
Property city: {city}

Look for: names after "survived by", "wife", "husband", "son", "daughter", \
"brother", "sister", "mother", "father". Include partial names (e.g., just \
"Mary" without last name — use the deceased's last name). Use best-guess \
relationship if not explicit.

Return a JSON object with:
- "survivors": array of objects with "name" and "relationship" keys
- "executor_named": empty string (not available from snippets)
- "confidence": "high" if clear survivor names found, "low" otherwise

Search snippets:
{snippets}"""


def _search_survivors_targeted(
    name: str,
    city: str,
    api_key: str,
) -> list[dict]:
    """Run targeted searches for survivor names when standard snippet lacks them.

    Returns list of survivor dicts [{name, relationship}] or empty list.
    """
    queries = [
        f'"{name}" "survived by" {city} Tennessee',
        f'"{name}" "preceded in death" {city} Tennessee',
        f'"{name}" obituary wife OR husband OR son OR daughter {city}',
        f'"{name}" funeral OR memorial service {city} Tennessee',
    ]

    all_snippets = []
    for query in queries:
        try:
            results = DDGS().text(query, max_results=5, backend="google,duckduckgo,brave")
            for r in results:
                snippet = r.get("body", "")
                title = r.get("title", "")
                if snippet:
                    all_snippets.append(f"Title: {title}\nSnippet: {snippet}")
        except Exception:
            continue
        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    if not all_snippets:
        return []

    combined = "\n\n".join(all_snippets[:6])
    prompt = SNIPPET_SURVIVOR_PROMPT.format(
        owner_name=name,
        city=city or "unknown",
        snippets=combined,
    )

    try:
        parsed = llm_client.chat_json(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS, api_key=api_key)
        if not parsed:
            return []
        survivors = parsed.get("survivors", [])
        confidence = parsed.get("confidence", "")
        if survivors and confidence == "high":
            logger.info("  Targeted snippet: %d survivors (high conf) for %s", len(survivors), name)
            return survivors
        if survivors and confidence == "medium":
            # Accept medium-confidence if every survivor has both name and relationship
            all_complete = all(
                s.get("name", "").strip() and s.get("relationship", "").strip()
                for s in survivors
            )
            if all_complete:
                logger.info(
                    "  Targeted snippet: %d survivors (medium conf, all complete) for %s",
                    len(survivors), name,
                )
                return survivors
            logger.debug(
                "  Targeted snippet: medium confidence but incomplete survivor data for %s",
                name,
            )
    except Exception as e:
        logger.debug("Targeted survivor search LLM failed for %s: %s", name, e)

    return []


def _extract_survivors_aggressive(
    obituary_text: str,
    owner_name: str,
    api_key: str,
) -> list[dict]:
    """Aggressive fallback: extract any named living persons from obituary text.

    Called when standard LLM extraction returned empty survivors[] from a
    confirmed full-page obituary. Uses a permissive prompt with no confidence
    requirement. All returned survivors are treated as 'unverified'.
    """
    if not obituary_text or not obituary_text.strip() or not api_key:
        return []

    prompt = AGGRESSIVE_SURVIVOR_PROMPT.format(
        owner_name=owner_name,
        obituary_text=obituary_text[:MAX_OBITUARY_TEXT],
    )

    try:
        parsed = llm_client.chat_json(prompt, system=SYSTEM_PROMPT, max_tokens=512, api_key=api_key)
        if not parsed:
            return []
        survivors = parsed.get("survivors", [])
        survivors = [s for s in survivors if s.get("name", "").strip()]
        if survivors:
            logger.info(
                "  Aggressive extraction found %d survivor(s) for %s",
                len(survivors), owner_name,
            )
        return survivors
    except Exception as e:
        logger.debug("Aggressive survivor extraction failed for %s: %s", owner_name, e)
        return []


# ── DM address lookup ──────────────────────────────────────────────

PEOPLE_SEARCH_DOMAINS = {
    "truepeoplesearch.com",
    "fastpeoplesearch.com",
    "cyberbackgroundchecks.com",
    "peoplefinder.com",
    "spokeo.com",
    "whitepages.com",
}

ADDRESS_EXTRACT_PROMPT = """\
Extract the current residential mailing address for this person from the web page text.

Person: {name}
Expected area: {city}, Tennessee (or nearby)

Instructions:
1. The page may list MULTIPLE people. Scan ALL result blocks to find the one that \
best matches "{name}" in {city}, Tennessee.
2. Within that block, prefer the "Lives at" or "Current address" over "Used to live" addresses.
3. If you find an exact name + state match, return it even if the city differs slightly \
(people move within Tennessee).
4. If multiple exact matches exist (common name), pick the Tennessee address closest \
to {city}.
5. If no confident match exists, return empty strings — do not guess.

Return ONLY valid JSON with these exact keys:
- "street": street address (e.g., "1234 Oak Street") — empty string if not found
- "city": city name — empty string if not found
- "state": 2-letter state code — "TN" if Tennessee
- "zip": 5-digit zip code — empty string if not found
- "confidence": "high" if name+state match found, "medium" if likely match, "low" if uncertain

Web page text:
{page_text}"""


def _lookup_dm_address_knox_tax(name: str) -> dict | None:
    """Search Knox County tax API by DM name for a property address.

    The /parcels/ endpoint accepts owner name searches.
    Returns {street, city, state, zip} or None.
    """
    from urllib.parse import quote

    search_url = f"{KNOX_TAX_API}/parcels/{quote(name)}?detail_level=public&start=0&length=3"
    try:
        resp = requests.get(search_url, timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        parcels = data.get("parcels", [])
        if not parcels:
            return None

        # Take first result — check owner name roughly matches
        p = parcels[0]
        addr = p.get("parcel_address", "").strip()
        if not addr:
            return None

        # parcel_address is just the street — we need city/zip from other sources
        # The tax API doesn't provide city/zip reliably, so return street only
        # and let Smarty fill in city/state/zip later
        logger.debug("Knox tax API found address for %s: %s", name, addr)
        return {"street": addr, "city": "Knoxville", "state": "TN", "zip": ""}
    except Exception as e:
        logger.debug("Knox tax API DM lookup failed for %s: %s", name, e)
        return None


def _lookup_dm_address_web(name: str, city: str, api_key: str) -> dict | None:
    """Search free people search sites for DM's residential address.

    Uses DuckDuckGo to find pages on people search sites, then Claude Haiku
    to extract the address from page content.
    """
    # Targeted people search query
    site_filter = " OR ".join(f"site:{d}" for d in list(PEOPLE_SEARCH_DOMAINS)[:4])
    queries = [
        f'"{name}" {city} Tennessee {site_filter}',
        f'"{name}" Tennessee address {city}',
    ]

    for query in queries:
        try:
            results = DDGS().text(query, max_results=5, backend="google,duckduckgo,brave")
        except Exception:
            time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
            continue

        for r in results:
            url = r.get("href", "")
            url_lower = url.lower()

            # Prioritize known people search domains
            is_people_site = any(d in url_lower for d in PEOPLE_SEARCH_DOMAINS)
            if not is_people_site:
                continue

            page_text = _fetch_page_text(url)
            if not page_text or len(page_text) < 50:
                continue

            # LLM extraction
            prompt = ADDRESS_EXTRACT_PROMPT.format(
                name=name,
                city=city or "",
                page_text=page_text[:MAX_OBITUARY_TEXT],
            )
            try:
                parsed = llm_client.chat_json(prompt, system=SYSTEM_PROMPT, max_tokens=256, api_key=api_key)
                if parsed:
                    street = parsed.get("street", "").strip()
                    if street and parsed.get("confidence") in ("high", "medium"):
                        logger.info("  People search found address for %s: %s, %s",
                                    name, street, parsed.get("city", ""))
                        return {
                            "street": street,
                            "city": parsed.get("city", ""),
                            "state": parsed.get("state", "TN"),
                            "zip": parsed.get("zip", ""),
                        }
            except Exception as e:
                logger.debug("Address LLM extraction failed for %s: %s", name, e)

            time.sleep(random.uniform(0.5, 1.0))

        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    return None


def _build_people_search_urls(name: str, city: str) -> list[str]:
    """Build direct URLs for free people search sites that show addresses.

    CyberBackgroundChecks is the only reliable free site — Firecrawl
    renders it successfully with full address history, phones, and relatives.
    TruePeopleSearch times out and FastPeopleSearch is Cloudflare-blocked.
    Uses first + last name only (no middle name) for best match rates.
    """
    parts = name.strip().split()
    if len(parts) < 2:
        return []
    first = parts[0].lower()
    last = parts[-1].lower()
    city_clean = (city or "").strip().lower().replace(" ", "-")

    urls = [
        # CyberBackgroundChecks — shows full address history, phones, relatives
        f"https://www.cyberbackgroundchecks.com/people/"
        f"{first}-{last}/{city_clean}-tn",
    ]
    return urls


def _search_serper(name: str, city: str) -> list[str]:
    """Search Google via Serper.dev for people search site URLs.

    Returns a list of URLs from known people search domains.
    Falls through gracefully if SERPER_API_KEY is not configured.
    Uses first+last name only to avoid middle name mismatches.
    """
    import config as cfg
    if not cfg.SERPER_API_KEY:
        return []

    parts = name.strip().split()
    if len(parts) < 2:
        return []
    first = parts[0]
    last = parts[-1]

    # CyberBackgroundChecks is the only free site Firecrawl can scrape reliably.
    # TruePeopleSearch times out, FastPeopleSearch is Cloudflare-blocked.
    city_clean = (city or "").strip()
    query = f'"{first} {last}" {city_clean} TN site:cyberbackgroundchecks.com'

    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": cfg.SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": 5},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        urls = []
        for item in data.get("organic", []):
            url = item.get("link", "")
            if any(d in url.lower() for d in PEOPLE_SEARCH_DOMAINS):
                urls.append(url)
        return urls[:3]
    except Exception as e:
        logger.debug("Serper search failed for %s: %s", name, e)
        return []


def _filter_cbc_markdown(markdown: str) -> str:
    """Strip noise from CyberBackgroundChecks markdown to fit more results.

    Removes phone numbers, relatives, associates, and navigation boilerplate.
    Keeps name, age, "Lives at", and "Used to live" sections.
    """
    filtered_lines = []
    skip_section = False
    for line in markdown.split("\n"):
        # Skip phone/relative/associate/navigation sections
        if line.startswith("### Phones") or line.startswith("### Related to") or \
           line.startswith("### Associated with") or line.startswith("### Other observed") or \
           line.startswith("This is me..."):
            skip_section = True
            continue
        # Resume at next person heading or address section
        if line.startswith("## ") or line.startswith("### Lives at") or \
           line.startswith("### Used to live") or line.startswith("[VIEW DETAILS]"):
            skip_section = False
        if skip_section:
            continue
        # Skip boilerplate navigation lines
        if line.startswith("[People Directory]") or line.startswith("[Displaying page") or \
           line.startswith("- [**") or line.startswith("Clear"):
            continue
        filtered_lines.append(line)
    return "\n".join(filtered_lines)


_firecrawl_credits_exhausted = False
_firecrawl_budget_total = int(os.environ.get("FIRECRAWL_BUDGET", "3000"))
_firecrawl_calls_used = 0
_firecrawl_lock = threading.Lock()


def _fetch_firecrawl(
    url: str, wait_ms: int = 5000, max_text: int = 0, priority: str = "high"
) -> str:
    """Fetch a page using Firecrawl API with JS rendering.

    Returns clean markdown text. Falls back to empty string if not configured
    or on error. Uses waitFor to let JS-heavy pages fully render.
    max_text: override truncation limit (0 = use MAX_OBITUARY_TEXT).
    priority: "high" (obituary pages, always allowed), "medium" (Phase B upgrades,
              allowed if >50% budget remains), "low" (DM address lookups, allowed
              if >75% budget remains).
    """
    global _firecrawl_credits_exhausted, _firecrawl_calls_used
    import config as cfg
    if not cfg.FIRECRAWL_API_KEY or _firecrawl_credits_exhausted:
        return ""

    # Budget-based priority gating (thread-safe read)
    with _firecrawl_lock:
        budget_remaining_pct = 1.0 - (_firecrawl_calls_used / max(_firecrawl_budget_total, 1))
        if priority == "medium" and budget_remaining_pct < 0.50:
            logger.debug("Firecrawl budget <50%% — skipping medium-priority fetch for %s", url)
            return ""
        if priority == "low" and budget_remaining_pct < 0.75:
            logger.debug("Firecrawl budget <75%% — skipping low-priority fetch for %s", url)
            return ""
        _firecrawl_calls_used += 1

    limit = max_text or MAX_OBITUARY_TEXT
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {cfg.FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown"], "waitFor": wait_ms},
            timeout=45,
        )
        if resp.status_code == 402:
            with _firecrawl_lock:
                _firecrawl_credits_exhausted = True
            logger.warning(
                "Firecrawl credits exhausted after %d calls — falling back to other methods",
                _firecrawl_calls_used,
            )
            return ""
        resp.raise_for_status()
        data = resp.json()
        markdown = data.get("data", {}).get("markdown", "")
        if not markdown:
            return ""
        # Filter CBC pages to strip noise and fit more results
        if "cyberbackgroundchecks.com" in url:
            markdown = _filter_cbc_markdown(markdown)
        if _firecrawl_calls_used % 50 == 0:
            logger.info(
                "Firecrawl budget: %d/%d used (%.0f%% remaining)",
                _firecrawl_calls_used, _firecrawl_budget_total, budget_remaining_pct * 100,
            )
        return markdown[:limit]
    except Exception as e:
        logger.debug("Firecrawl fetch failed for %s: %s", url, e)
        return ""


def _extract_address_from_page(
    page_text: str, name: str, city: str, api_key: str
) -> dict | None:
    """Use Claude Haiku to extract a mailing address from page text."""
    prompt = ADDRESS_EXTRACT_PROMPT.format(
        name=name,
        city=city or "",
        page_text=page_text[:MAX_ADDRESS_TEXT],
    )
    try:
        parsed = llm_client.chat_json(prompt, system=SYSTEM_PROMPT, max_tokens=256, api_key=api_key)
        if not parsed:
            return None

        street = parsed.get("street", "").strip()
        if street and parsed.get("confidence") in ("high", "medium"):
            return {
                "street": street,
                "city": parsed.get("city", ""),
                "state": parsed.get("state", "TN"),
                "zip": parsed.get("zip", ""),
            }
    except Exception as e:
        logger.debug("Address LLM extraction failed for %s: %s", name, e)
    return None


def _lookup_dm_address_serper_firecrawl(
    name: str, city: str, api_key: str
) -> dict | None:
    """Look up DM address via direct people search URLs + Firecrawl rendering.

    Tier 2: First tries direct TruePeopleSearch/FastPeopleSearch URLs (which
    show actual street addresses for free), then falls back to Serper Google
    search for additional people search sites. Uses Claude Haiku to extract
    the address from rendered page content.
    """
    # Phase 1: Direct people search URLs (no Google search needed)
    direct_urls = _build_people_search_urls(name, city)
    for url in direct_urls:
        page_text = _fetch_firecrawl(url, max_text=MAX_ADDRESS_TEXT, priority="low")
        if not page_text or len(page_text) < 100:
            page_text = _fetch_page_text(url)
        if not page_text or len(page_text) < 100:
            continue

        result = _extract_address_from_page(page_text, name, city, api_key)
        if result:
            logger.debug("Direct URL hit for %s: %s", name, url)
            return result
        time.sleep(random.uniform(0.5, 1.0))

    # Phase 2: Serper Google search fallback
    serper_urls = _search_serper(name, city)
    for url in serper_urls:
        # Skip URLs we already tried via direct
        if any(url.startswith(d.rsplit("/", 1)[0]) for d in direct_urls):
            continue

        page_text = _fetch_firecrawl(url, max_text=MAX_ADDRESS_TEXT, priority="low")
        if not page_text or len(page_text) < 100:
            page_text = _fetch_page_text(url)
        if not page_text or len(page_text) < 100:
            continue

        result = _extract_address_from_page(page_text, name, city, api_key)
        if result:
            logger.debug("Serper URL hit for %s: %s", name, url)
            return result
        time.sleep(random.uniform(0.5, 1.0))

    return None


def _lookup_dm_address_tracerfy(name: str, city: str,
                                 address: str = "", zip_code: str = "") -> dict | None:
    """Look up DM mailing address via Tracerfy Instant Trace API.

    Uses POST /v1/api/trace/lookup/ (synchronous, single-record).
    Cost: 5 credits ($0.10) per hit, 0 on miss. Rate limit: 500 RPM.
    """
    import config as cfg

    if not cfg.TRACERFY_API_KEY:
        return None

    # Split name into first/last
    parts = name.strip().split()
    if len(parts) < 2:
        return None
    first_name = parts[0]
    last_name = parts[-1]

    try:
        resp = requests.post(
            "https://tracerfy.com/v1/api/trace/lookup/",
            headers={
                "Authorization": f"Bearer {cfg.TRACERFY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "address": address or "",
                "city": city or "",
                "state": "TN",
                "zip": zip_code or "",
                "find_owner": False,
                "first_name": first_name,
                "last_name": last_name,
            },
            timeout=30,
        )
        if resp.status_code == 402:
            # Only surface this as WARNING once per run; subsequent calls
            # in the same process will still hit 402 and repeat the message.
            logger.warning(
                "Tracerfy instant 402 for %s — INSUFFICIENT CREDITS: %s",
                name, resp.text[:200],
            )
        elif resp.status_code != 200:
            logger.debug("Tracerfy instant %d for %s: %s",
                         resp.status_code, name, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()

        if not data.get("hit") or not data.get("persons"):
            return None

        person = data["persons"][0]
        mail = person.get("mailing_address") or {}
        street = (mail.get("street") or "").strip()
        if street:
            return {
                "street": street,
                "city": (mail.get("city") or "").strip(),
                "state": (mail.get("state") or "TN").strip(),
                "zip": (mail.get("zip") or "").strip(),
            }
        return None
    except Exception as e:
        logger.debug("Tracerfy instant lookup failed for %s: %s", name, e)
        return None


def _batch_tracerfy_lookup(notices: list) -> None:
    """Batch skip-trace all notices that still need a DM mailing address.

    Submits a single CSV with all DM names to Tracerfy, polls for results,
    and updates NoticeData fields in-place. Much faster than per-record calls.
    """
    import config as cfg
    import csv as csv_mod
    import io

    if not cfg.TRACERFY_API_KEY or not notices:
        return

    # Build batch CSV — Tracerfy requires address data for skip tracing
    csv_buffer = io.StringIO()
    writer = csv_mod.writer(csv_buffer)
    writer.writerow(["first_name", "last_name", "address", "city", "state",
                      "zip", "mail_address", "mail_city", "mail_state"])

    lookup_map: list[tuple] = []  # [(notice, first_name, last_name), ...]
    for n in notices:
        parts = n.decision_maker_name.strip().split()
        if len(parts) < 2:
            continue
        first_name = parts[0]
        last_name = parts[-1]
        # Use property address as the known address for skip tracing
        addr = n.address.strip()
        city_hint = n.city.strip() or ""
        zip_code = n.zip.strip()
        writer.writerow([first_name, last_name, addr, city_hint, "TN",
                         zip_code, "", "", ""])
        lookup_map.append((n, first_name, last_name))

    if not lookup_map:
        return

    csv_content = csv_buffer.getvalue()
    csv_buffer.close()

    try:
        resp = requests.post(
            "https://tracerfy.com/v1/api/trace/",
            headers={"Authorization": f"Bearer {cfg.TRACERFY_API_KEY}"},
            data={
                "first_name_column": "first_name",
                "last_name_column": "last_name",
                "address_column": "address",
                "city_column": "city",
                "state_column": "state",
                "zip_column": "zip",
                "mail_address_column": "mail_address",
                "mail_city_column": "mail_city",
                "mail_state_column": "mail_state",
            },
            files={"csv_file": ("dm_batch.csv", csv_content, "text/csv")},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.debug("Tracerfy batch %d response: %s",
                         resp.status_code, resp.text[:500])
        resp.raise_for_status()
        queue_data = resp.json()
        queue_id = queue_data.get("queue_id")
        if not queue_id:
            logger.warning("Tracerfy batch returned no queue_id")
            return

        logger.info("  Tracerfy batch job %s submitted (%d records)", queue_id, len(lookup_map))

        # Poll for results — batch jobs take longer
        # GET /queue/:id may return a list (records) or dict (status wrapper)
        for _attempt in range(30):
            time.sleep(5)
            result_resp = requests.get(
                f"https://tracerfy.com/v1/api/queue/{queue_id}",
                headers={"Authorization": f"Bearer {cfg.TRACERFY_API_KEY}"},
                timeout=15,
            )
            result_resp.raise_for_status()
            result_data = result_resp.json()

            # Handle both response formats
            if isinstance(result_data, list):
                records = result_data
            elif isinstance(result_data, dict):
                status = result_data.get("status", "")
                if status == "failed":
                    logger.warning("Tracerfy batch job %s failed", queue_id)
                    return
                if status != "completed":
                    continue  # still pending
                records = result_data.get("records", [])
            else:
                continue

            matched = 0
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                street = (rec.get("mail_address") or "").strip()
                if not street:
                    continue
                # Match back to notice by first+last name
                rec_first = (rec.get("first_name") or "").strip().lower()
                rec_last = (rec.get("last_name") or "").strip().lower()
                for notice, first, last in lookup_map:
                    if (first.lower() == rec_first
                            and last.lower() == rec_last
                            and not notice.decision_maker_street):
                        notice.decision_maker_street = street
                        notice.decision_maker_city = (rec.get("mail_city") or "").strip()
                        notice.decision_maker_state = (rec.get("mail_state") or "TN").strip()
                        notice.decision_maker_zip = (rec.get("mail_zip") or "").strip()
                        matched += 1
                        logger.info(
                            "    Tracerfy batch: %s -> %s, %s %s",
                            notice.decision_maker_name, street,
                            notice.decision_maker_city, notice.decision_maker_state,
                        )
                        break
            logger.info("  Tracerfy batch complete: %d/%d matched", matched, len(lookup_map))
            return

        logger.warning("Tracerfy batch job %s timed out after 150s", queue_id)
    except Exception as e:
        logger.warning("Tracerfy batch lookup failed: %s", e)


def _lookup_dm_address(
    name: str, city: str, api_key: str, tracerfy_tier1: bool = False,
) -> dict:
    """Look up decision-maker's mailing address using tiered sources.

    Tier 0 (opt-in): Tracerfy skip tracing (paid, highest hit rate)
    Tier 1: Knox County Tax API (free, fast, Knox only)
    Tier 2: Serper.dev + Firecrawl + LLM (cheap, national)
    Tier 2b: DuckDuckGo fallback (free, unreliable -- used when Serper not configured)

    Returns {street, city, state, zip, source} (may have empty values).
    """
    result = {"street": "", "city": "", "state": "", "zip": "", "source": ""}

    if not name or not name.strip():
        return result

    # Tier 0 (opt-in): Tracerfy as primary lookup
    if tracerfy_tier1:
        import config as cfg
        if cfg.TRACERFY_API_KEY:
            tf_result = _lookup_dm_address_tracerfy(
                name, city or "", address="", zip_code=""
            )
            if tf_result and tf_result.get("street"):
                result.update(tf_result)
                result["source"] = "tracerfy_tier1"
                logger.info("    Tier 0 (Tracerfy): %s, %s",
                            result["street"], result["city"])
                return result

    # Tier 1: Knox County Tax API (free, fast) — ONLY fires for known Knox-area
    # cities. OH records (Columbus, Dayton, Xenia, etc.) skip this tier entirely.
    # Previously also ran when city was empty — which caused OH records with
    # missing city data to falsely query the Knox TN tax API. Tightened to
    # require an actual Knox-area city match.
    knox_cities = {"knoxville", "powell", "corryton", "mascot", "halls",
                   "farragut", "karns", "gibbs", "fountain city"}
    dm_city = (city or "").lower().strip()
    if dm_city in knox_cities:
        name_parts = name.split()
        if len(name_parts) >= 2:
            tax_name = f"{name_parts[-1]} {' '.join(name_parts[:-1])}"
            tax_result = _lookup_dm_address_knox_tax(tax_name)
            if tax_result and tax_result.get("street"):
                result.update(tax_result)
                result["source"] = "knox_tax_api"
                logger.info("    Tier 1 (Knox Tax): %s", result["street"])
                return result
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    # Tier 2: Direct people search URLs + Firecrawl + LLM
    import config as cfg
    sf_result = _lookup_dm_address_serper_firecrawl(
        name, city or "", api_key
    )
    if sf_result and sf_result.get("street"):
        result.update(sf_result)
        result["source"] = "people_search"
        logger.info("    Tier 2 (People Search): %s, %s",
                    result["street"], result["city"])
        return result

    # Tier 2b: DuckDuckGo fallback (when Serper/Firecrawl not configured)
    if not cfg.SERPER_API_KEY and not cfg.FIRECRAWL_API_KEY:
        web_result = _lookup_dm_address_web(name, city or "", api_key)
        if web_result and web_result.get("street"):
            result.update(web_result)
            result["source"] = "ddg_people_search"
            logger.info("    Tier 2b (DDG): %s, %s",
                        result["street"], result["city"])
            return result

    return result


KNOX_TAX_API = "https://knox-tn.mygovonline.com/api/v2"
REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0


def _fetch_page_text(url: str) -> str:
    """Fetch a URL and extract readable text content."""
    # Check if domain is known to block direct HTTP — skip straight to Firecrawl
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower().replace("www.", "")
    if any(blocked in domain for blocked in KNOWN_403_DOMAINS):
        logger.debug("Known-403 domain %s — skipping HTTP, trying Firecrawl first", domain)
        fc_text = _fetch_firecrawl(url)
        if fc_text and len(fc_text) >= 100:
            return fc_text
        return _fetch_cached_text(url)

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        resp = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()

        # Increased limit for JSON-heavy React SPA pages
        content = resp.text[:200000]

        # Try structured data extraction first (JSON-LD, __INITIAL_STATE__)
        structured = _extract_structured_text(content, url)
        if structured and len(structured) >= 100:
            return structured[:MAX_OBITUARY_TEXT]

        # BeautifulSoup fallback for non-SPA sites
        soup = BeautifulSoup(content, "html.parser")

        # Remove script and style elements
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # Collapse excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:MAX_OBITUARY_TEXT]

    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            logger.debug("403 blocked for %s, trying Firecrawl", url)
            # Firecrawl can render JS and bypass many 403 blocks
            fc_text = _fetch_firecrawl(url)
            if fc_text and len(fc_text) >= 100:
                logger.debug("Firecrawl fetched %d chars for %s", len(fc_text), url)
                return fc_text
            logger.debug("Firecrawl failed, trying cached sources for %s", url)
            return _fetch_cached_text(url)
        logger.debug("Failed to fetch %s: %s", url, e)
        return ""
    except requests.exceptions.ReadTimeout:
        # Timeout — try Firecrawl, then Wayback Machine
        logger.debug("Timeout for %s, trying Firecrawl", url)
        fc_text = _fetch_firecrawl(url)
        if fc_text and len(fc_text) >= 100:
            return fc_text
        logger.debug("Firecrawl failed on timeout, trying cached sources for %s", url)
        return _fetch_cached_text(url)
    except Exception as e:
        logger.debug("Failed to fetch %s: %s", url, e)
        return ""


def _parse_obituary_with_llm(
    obituary_text: str,
    owner_name: str,
    city: str,
    address: str,
    api_key: str,
    state: str = "Ohio",
) -> dict | None:
    """Use Claude Haiku to validate and parse an obituary.

    Returns parsed dict if the obituary matches the owner, None otherwise.

    NOTE: `state` defaults to "Ohio" — current scope is OH-only. When/if
    SiftStack expands to other states, callers should pass the notice's
    state field (mapped from "OH" → "Ohio", "TN" → "Tennessee", etc.).
    """
    if not obituary_text.strip():
        return None
    # For Ollama backend, api_key not required
    import config as _cfg
    if getattr(_cfg, "LLM_BACKEND", "anthropic") == "anthropic" and not api_key:
        return None

    prompt = OBITUARY_PROMPT.format(
        owner_name=owner_name,
        city=city or "unknown",
        state=state or "Ohio",
        address=address or "unknown",
        obituary_text=obituary_text[:MAX_OBITUARY_TEXT],
    )

    try:
        parsed = llm_client.chat_json(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS, api_key=api_key)
        if not parsed:
            return None

        if not parsed.get("match"):
            return None

        # Validate minimum fields
        if not parsed.get("full_name"):
            return None

        return parsed

    except Exception as e:
        logger.debug("Obituary LLM parsing failed: %s", e)
        return None


def identify_decision_maker(survivors: list[dict]) -> tuple[str, str]:
    """Identify the decision-maker from a list of survivors.

    Priority (from SKILL.md):
    1. Named executor (if mentioned in survivors)
    2. Surviving spouse
    3. Oldest child (first listed son/daughter)
    4. Sibling (if no spouse/children)
    5. First listed survivor (fallback)

    Returns (name, relationship) tuple.
    """
    if not survivors:
        return ("", "")

    spouse = None
    children = []
    siblings = []
    others = []

    spouse_terms = {"wife", "husband", "spouse", "partner"}
    child_terms = {"son", "daughter", "child", "stepson", "stepdaughter"}
    sibling_terms = {"brother", "sister", "sibling"}

    for s in survivors:
        name = s.get("name", "").strip()
        rel = s.get("relationship", "").strip().lower()
        if not name:
            continue

        # Check for executor
        if "executor" in rel or "personal representative" in rel:
            return (name, "executor")

        if any(t in rel for t in spouse_terms):
            spouse = (name, rel)
        elif any(t in rel for t in child_terms):
            children.append((name, rel))
        elif any(t in rel for t in sibling_terms):
            siblings.append((name, rel))
        else:
            others.append((name, rel))

    if spouse:
        return spouse
    if children:
        return children[0]
    if siblings:
        return siblings[0]
    if others:
        return others[0]
    return ("", "")


# ── Deep prospecting: ranked DMs, heir verification, error map ────────


def rank_decision_makers(
    survivors: list[dict],
    executor_name: str = "",
    heir_statuses: dict[str, str] | None = None,
) -> list[dict]:
    """Rank all survivors as potential decision-makers and classify signing authority.

    Returns list of dicts sorted by priority, each with:
        name, relationship, rank, status, source, signing_authority

    Priority: executor > spouse > children > siblings > others.
    Within each group: verified_living first, then unverified, then deceased.

    Signing authority is based on Tennessee intestate succession law:
      - Executor/PR: always has signing authority
      - Spouse: always has signing authority
      - Children (NOT step-children): signing authority if alive
      - Grandchildren: only if their parent (deceased's child) is also deceased
      - Parents: only if no living spouse AND no living children
      - Siblings: only if no living spouse, children, OR parents
      - In-laws, step-children, friends: never have signing authority
    """
    if not survivors and not executor_name:
        return []

    statuses = heir_statuses or {}

    # ── Relationship term sets ──
    spouse_terms = {"wife", "husband", "spouse", "partner"}
    child_terms = {"son", "daughter", "child", "stepson", "stepdaughter"}
    sibling_terms = {"brother", "sister", "sibling"}
    # Extended sets for signing authority classification
    stepchild_terms = {"stepson", "stepdaughter", "step-son", "step-daughter"}
    inlaw_terms = {
        "mother-in-law", "father-in-law", "sister-in-law", "brother-in-law",
        "son-in-law", "daughter-in-law", "in-law",
    }
    parent_terms = {"mother", "father", "parent"}
    grandchild_terms = {"grandson", "granddaughter", "grandchild"}
    # Spouses of relatives — non-inheriting (e.g., "grandchild's spouse", "grandson's wife")
    spouse_of_patterns = {"'s spouse", "'s wife", "'s husband", "'s partner"}

    executors: list[dict] = []
    spouses: list[dict] = []
    children: list[dict] = []
    siblings: list[dict] = []
    parents: list[dict] = []
    grandchildren: list[dict] = []
    others: list[dict] = []

    # Named executor (may not be in survivors list)
    if executor_name:
        status = statuses.get(executor_name, "unverified")
        executors.append({
            "name": executor_name,
            "relationship": "executor",
            "status": status,
            "source": "obituary_survivors",
        })

    seen_names = {executor_name.lower()} if executor_name else set()

    for s in survivors:
        name = s.get("name", "").strip()
        rel = s.get("relationship", "").strip().lower()
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())

        status = statuses.get(name, "unverified")
        entry = {
            "name": name,
            "relationship": rel,
            "status": status,
            "source": "obituary_survivors",
        }

        # Check in-laws FIRST (before child_terms, since "daughter-in-law" contains "daughter")
        if any(t in rel for t in inlaw_terms) or "in-law" in rel or "in law" in rel:
            others.append(entry)
        elif any(p in rel for p in spouse_of_patterns):
            others.append(entry)  # Spouse of a relative — no inheritance rights
        elif "executor" in rel or "personal representative" in rel:
            executors.append(entry)
        elif any(t in rel for t in spouse_terms):
            spouses.append(entry)
        elif any(t in rel for t in stepchild_terms):
            others.append(entry)  # Step-children go to others (no signing authority)
        elif any(t in rel for t in grandchild_terms):
            grandchildren.append(entry)
        elif any(t in rel for t in child_terms):
            children.append(entry)
        elif any(t in rel for t in parent_terms):
            parents.append(entry)
        elif any(t in rel for t in sibling_terms):
            siblings.append(entry)
        else:
            others.append(entry)

    # Build ranked list: within each priority group, living > unverified > deceased
    def _sort_by_status(group: list[dict]) -> list[dict]:
        living = [e for e in group if e["status"] == "verified_living"]
        unverified = [e for e in group if e["status"] == "unverified"]
        deceased = [e for e in group if e["status"] == "deceased"]
        return living + unverified + deceased

    ranked: list[dict] = []
    for group in [executors, spouses, children, grandchildren, parents, siblings, others]:
        ranked.extend(_sort_by_status(group))

    for i, entry in enumerate(ranked):
        entry["rank"] = i + 1

    # ── Pass 2: Determine signing authority (TN intestate succession) ──
    # Check which priority tiers have living/unverified members
    def _has_living(group: list[dict]) -> bool:
        return any(e["status"] in ("verified_living", "unverified") for e in group)

    has_living_spouse = _has_living(spouses)
    has_living_children = _has_living(children)
    has_living_parents = _has_living(parents)

    # Track which children are deceased (for grandchild right of representation)
    deceased_child_names = {
        e["name"].lower() for e in children if e["status"] == "deceased"
    }

    for entry in ranked:
        rel = entry["relationship"].lower()

        # In-laws: NEVER signing authority (check FIRST — "daughter-in-law" contains "daughter")
        if any(t in rel for t in inlaw_terms) or "in-law" in rel or "in law" in rel:
            entry["signing_authority"] = False

        # Spouses of relatives: NEVER signing authority (e.g., "grandchild's spouse")
        elif any(p in rel for p in spouse_of_patterns):
            entry["signing_authority"] = False

        # Step-children: NEVER signing authority (don't inherit intestate)
        elif any(t in rel for t in stepchild_terms):
            entry["signing_authority"] = False

        # Executors/PR: always signing authority
        elif "executor" in rel or "personal representative" in rel:
            entry["signing_authority"] = True

        # Spouse: always signing authority
        elif any(t in rel for t in spouse_terms):
            entry["signing_authority"] = True

        # Grandchildren: only if their parent (a child of deceased) is also deceased
        # Since we can't always trace parent→grandchild, we use a heuristic:
        # if ANY child is deceased, grandchildren MAY have right of representation
        elif any(t in rel for t in grandchild_terms):
            entry["signing_authority"] = bool(deceased_child_names)

        # Biological children: signing authority if not deceased
        elif any(t in rel for t in child_terms):
            entry["signing_authority"] = entry["status"] != "deceased"

        # Parents: only if no living spouse AND no living children
        elif any(t in rel for t in parent_terms):
            entry["signing_authority"] = not has_living_spouse and not has_living_children

        # Siblings: only if no living spouse, children, or parents
        elif any(t in rel for t in sibling_terms):
            entry["signing_authority"] = (
                not has_living_spouse and not has_living_children and not has_living_parents
            )

        # Everything else (friends, nieces/nephews, etc.): no signing authority
        elif any(t in rel for t in inlaw_terms):
            entry["signing_authority"] = False

        # Everything else (friends, nieces/nephews, etc.): no signing authority
        else:
            entry["signing_authority"] = False

    return ranked


def verify_heir_status(
    heir_name: str,
    city: str,
    api_key: str,
    depth: int = 0,
    max_depth: int = 2,
) -> dict:
    """Search for an obituary for a single heir to verify alive/dead.

    Returns dict: name, status, confidence, obituary_url, date_of_death,
                  sub_heirs (if deceased), search_log.
    """
    result = {
        "name": heir_name,
        "status": "unverified",
        "confidence": "low",
        "obituary_url": "",
        "date_of_death": "",
        "sub_heirs": [],
        "search_log": {"query": "", "result": "not_searched"},
    }

    results = _search_obituary(heir_name, city)
    result["search_log"]["query"] = f"{heir_name} obituary {city} Tennessee"

    if not results:
        # No search results at all — likely alive (no obituary exists online)
        result["status"] = "verified_living"
        result["confidence"] = "medium"
        result["search_log"]["result"] = "no_obituary_found"
        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
        return result

    # Try each result — fetch full page, then LLM parse
    for search_result in results:
        page_text = _fetch_page_text(search_result["url"])
        if page_text and len(page_text) >= 100:
            parsed = _parse_obituary_with_llm(
                obituary_text=page_text,
                owner_name=heir_name,
                city=city,
                address="",  # don't know heir's address
                api_key=api_key,
            )
            if parsed and parsed.get("confidence") in ("high", "medium"):
                result["status"] = "deceased"
                result["confidence"] = parsed.get("confidence", "medium")
                result["obituary_url"] = search_result["url"]
                result["date_of_death"] = parsed.get("date_of_death", "")
                result["search_log"]["result"] = "obituary_confirmed_deceased"
                if depth < max_depth:
                    result["sub_heirs"] = parsed.get("survivors", [])
                time.sleep(random.uniform(0.5, 1.0))
                return result
            time.sleep(random.uniform(0.5, 1.0))

    # Snippet fallback
    best_snippet = next((r for r in results if r.get("snippet")), None)
    if best_snippet:
        snippet_text = (
            f"Search result title: {best_snippet['title']}\n"
            f"URL: {best_snippet['url']}\n"
            f"Snippet: {best_snippet['snippet']}"
        )
        parsed = _parse_obituary_with_llm(
            obituary_text=snippet_text,
            owner_name=heir_name,
            city=city,
            address="",
            api_key=api_key,
        )
        if parsed and parsed.get("confidence") == "high":
            result["status"] = "deceased"
            result["confidence"] = "high"
            result["obituary_url"] = best_snippet["url"]
            result["date_of_death"] = parsed.get("date_of_death", "")
            result["search_log"]["result"] = "obituary_confirmed_deceased_snippet"
            return result

    # Searched but no obituary matched — likely alive
    result["status"] = "verified_living"
    result["confidence"] = "medium"
    result["search_log"]["result"] = "no_obituary_match"
    time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
    return result


def build_heir_map(
    parsed: dict,
    city: str,
    api_key: str,
    raw_name: str = "",
    max_depth: int = 2,
) -> tuple[list[dict], dict]:
    """Build heir map with verification, return (ranked_dms, error_info).

    For each survivor from the obituary, verifies alive/dead status.
    If a survivor is deceased and depth allows, recursively checks their heirs.
    Returns ranked decision-makers and an error/metadata dict.
    """
    survivors = list(parsed.get("survivors", []))
    executor = parsed.get("executor_named", "")
    preceded = set(parsed.get("preceded_in_death", []))

    error_info: dict = {
        "heirs_verified_living": 0,
        "heirs_verified_deceased": 0,
        "heirs_unverified": 0,
        "missing_flags": [],
        "dm_confidence": "",
        "dm_confidence_reason": "",
    }

    if not survivors and not executor:
        # Second-pass: try aggressive extraction if raw obituary text is available
        raw_text = parsed.get("_raw_obituary_text", "")
        search_name = parsed.get("_search_name", raw_name)
        if raw_text and api_key:
            logger.info("  No survivors in first pass — trying aggressive extraction for %s", search_name)
            survivors = _extract_survivors_aggressive(raw_text, search_name, api_key)
            if survivors:
                error_info["missing_flags"].append("survivors_from_aggressive_pass")
            else:
                error_info["missing_flags"].append("no_survivors")
                return [], error_info
        else:
            error_info["missing_flags"].append("no_survivors")
            return [], error_info

    # Verify each survivor (parallelized for speed)
    heir_statuses: dict[str, str] = {}
    verified_names: set[str] = set()

    # Pre-filter: separate preceded_in_death from those needing verification
    to_verify: list[tuple[str, dict]] = []
    for s in survivors:
        name = s.get("name", "").strip()
        if not name or name in verified_names:
            continue
        verified_names.add(name)

        # Skip names known to be deceased from preceded_in_death
        if any(p.lower() in name.lower() or name.lower() in p.lower() for p in preceded):
            heir_statuses[name] = "deceased"
            error_info["heirs_verified_deceased"] += 1
            logger.debug("    Heir %s: preceded in death (skipped search)", name)
            continue

        to_verify.append((name, s))

    # Parallel depth-0 heir verification
    def _verify_depth0(args):
        vname, _ = args
        logger.info("    Verifying heir: %s", vname)
        return vname, verify_heir_status(
            heir_name=vname, city=city, api_key=api_key,
            depth=0, max_depth=max_depth,
        )

    sub_heirs_to_check: list[tuple[str, list]] = []
    if to_verify:
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            for vname, verification in pool.map(_verify_depth0, to_verify):
                heir_statuses[vname] = verification["status"]
                if verification["status"] == "deceased":
                    error_info["heirs_verified_deceased"] += 1
                    error_info["missing_flags"].append("heir_also_deceased")
                    if verification.get("sub_heirs"):
                        logger.info("    Heir %s is deceased, checking their survivors...", vname)
                        sub_heirs_to_check.append((vname, verification["sub_heirs"]))
                else:
                    error_info["heirs_unverified"] += 1

    # Parallel depth-1 sub-heir verification
    sub_verify_tasks: list[tuple[str, dict]] = []
    for _, sub_heirs in sub_heirs_to_check:
        for sub in sub_heirs:
            sub_name = sub.get("name", "").strip()
            if not sub_name or sub_name in verified_names:
                continue
            verified_names.add(sub_name)
            sub_verify_tasks.append((sub_name, sub))

    def _verify_depth1(args):
        vname, _ = args
        logger.info("      Verifying sub-heir: %s", vname)
        return vname, _, verify_heir_status(
            heir_name=vname, city=city, api_key=api_key,
            depth=1, max_depth=max_depth,
        )

    if sub_verify_tasks:
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            for sub_name, sub, sub_v in pool.map(_verify_depth1, sub_verify_tasks):
                survivors.append({
                    "name": sub_name,
                    "relationship": sub.get("relationship", "grandchild"),
                })
                heir_statuses[sub_name] = sub_v["status"]
                if sub_v["status"] == "deceased":
                    error_info["heirs_verified_deceased"] += 1
                else:
                    error_info["heirs_unverified"] += 1

    # Rank decision-makers with verified statuses
    ranked = rank_decision_makers(survivors, executor, heir_statuses)

    # Set confidence
    living_dms = [r for r in ranked if r["status"] == "verified_living"]
    unverified_dms = [r for r in ranked if r["status"] == "unverified"]
    if living_dms:
        error_info["dm_confidence"] = "high"
        error_info["dm_confidence_reason"] = (
            f"DM verified living: {living_dms[0]['name']} ({living_dms[0]['relationship']})"
        )
    elif unverified_dms:
        error_info["dm_confidence"] = "medium"
        error_info["dm_confidence_reason"] = (
            f"{len(unverified_dms)} unverified heir(s), "
            f"top: {unverified_dms[0]['name']} ({unverified_dms[0]['relationship']})"
        )
    elif ranked:
        error_info["dm_confidence"] = "low"
        error_info["dm_confidence_reason"] = "all heirs confirmed deceased"
    else:
        error_info["dm_confidence"] = "low"
        error_info["dm_confidence_reason"] = "no usable heirs identified"

    return ranked, error_info


def _apply_obituary_match(
    notice: NoticeData,
    parsed: dict,
    url: str,
    source_type: str = "full_page",
    ranked_dms: list[dict] | None = None,
    error_info: dict | None = None,
) -> None:
    """Apply a confirmed obituary match to a notice record."""
    notice.owner_deceased = "yes"
    notice.date_of_death = parsed.get("date_of_death", "")
    notice.obituary_url = url
    notice.obituary_source_type = source_type

    if ranked_dms:
        # Deep prospecting: apply top 3 ranked decision-makers
        if len(ranked_dms) >= 1:
            dm = ranked_dms[0]
            notice.decision_maker_name = dm["name"]
            notice.decision_maker_relationship = dm["relationship"]
            notice.decision_maker_status = dm["status"]
            notice.decision_maker_source = dm.get("source", "obituary_survivors")
            # DM mailing address (populated by _lookup_dm_address)
            notice.decision_maker_street = dm.get("street", "")
            notice.decision_maker_city = dm.get("city", "")
            notice.decision_maker_state = dm.get("state", "")
            notice.decision_maker_zip = dm.get("zip", "")
        if len(ranked_dms) >= 2:
            dm = ranked_dms[1]
            notice.decision_maker_2_name = dm["name"]
            notice.decision_maker_2_relationship = dm["relationship"]
            notice.decision_maker_2_status = dm["status"]
        if len(ranked_dms) >= 3:
            dm = ranked_dms[2]
            notice.decision_maker_3_name = dm["name"]
            notice.decision_maker_3_relationship = dm["relationship"]
            notice.decision_maker_3_status = dm["status"]
        # Persist the full ranked heir list as JSON (all heirs, not just top 3)
        try:
            notice.heir_map_json = json.dumps(ranked_dms, ensure_ascii=False)
        except (TypeError, ValueError):
            notice.heir_map_json = ""
        # Signing chain summary: living heirs with signing authority
        signers = [
            e for e in ranked_dms
            if e.get("signing_authority") and e.get("status") != "deceased"
        ]
        notice.signing_chain_count = str(len(signers)) if signers else ""
        notice.signing_chain_names = ", ".join(e["name"] for e in signers) if signers else ""
    else:
        # Simple single-DM pick (Phase A only, no heir verification)
        survivors = parsed.get("survivors", [])
        executor = parsed.get("executor_named", "")
        if executor:
            notice.decision_maker_name = executor
            notice.decision_maker_relationship = "executor"
            notice.decision_maker_source = "obituary_survivors"
        elif survivors:
            dm_name, dm_rel = identify_decision_maker(survivors)
            notice.decision_maker_name = dm_name
            notice.decision_maker_relationship = dm_rel
            notice.decision_maker_source = "obituary_survivors"
        notice.decision_maker_status = "unverified"

    # Apply error map info
    if error_info:
        notice.heir_search_depth = str(error_info.get("heir_search_depth", "1"))
        living = error_info.get("heirs_verified_living", 0)
        deceased = error_info.get("heirs_verified_deceased", 0)
        unverified = error_info.get("heirs_unverified", 0)
        notice.heirs_verified_living = str(living) if living else ""
        notice.heirs_verified_deceased = str(deceased) if deceased else ""
        notice.heirs_unverified = str(unverified) if unverified else ""
        notice.dm_confidence = error_info.get("dm_confidence", "")
        notice.dm_confidence_reason = error_info.get("dm_confidence_reason", "")
        flags = error_info.get("missing_flags", [])
        notice.missing_data_flags = "|".join(flags) if flags else ""
    else:
        # No heir verification — set basic error map from source type
        notice.heir_search_depth = "0"
        flags = []
        if source_type == "snippet":
            flags.append("snippet_only")
        if not notice.decision_maker_name:
            flags.append("no_survivors")
            notice.dm_confidence = "low"
            notice.dm_confidence_reason = f"{source_type} match, no survivors extracted"
        else:
            notice.dm_confidence = "medium"
            notice.dm_confidence_reason = f"{source_type} match, DM unverified"
        notice.missing_data_flags = "|".join(flags)


def enrich_obituary_data(
    notices: list[NoticeData],
    api_key: str,
    skip_heir_verification: bool = False,
    max_heir_depth: int = 2,
    skip_dm_address: bool = False,
    tracerfy_tier1: bool = False,
    skip_ancestry: bool = False,
) -> None:
    """Search for obituaries and enrich notices with deceased owner data.

    Two-phase pipeline:
      Phase A: Search for obituaries, confirm deceased owners.
      Phase B: For each confirmed deceased, verify heirs alive/dead,
               rank decision-makers, and apply joint-owner fallback
               for snippet-only matches without survivors.

    Updates notices in-place.
    """
    if not api_key:
        logger.warning("No Anthropic API key — skipping obituary enrichment")
        return

    # Build candidate list: notices with owner names to search
    # Tuple: (notice, raw_name, is_tax_name)
    # is_tax_name=True → LAST FIRST MIDDLE format (use parse_tax_owner_name)
    # is_tax_name=False → FIRST LAST format from notice (use _parse_notice_owner_name)
    candidates = []
    for n in notices:
        # Skip records already enriched (e.g., from partial CSV re-import)
        if n.owner_deceased == "yes" and n.decision_maker_name:
            continue
        # Probate: search for decedent (the deceased), not executor/PR
        if n.notice_type == "probate" and n.decedent_name.strip():
            raw_name = n.decedent_name.strip()
            is_tax_name = False  # decedent_name is "First Last" format
        elif n.tax_owner_name.strip():
            raw_name = n.tax_owner_name.strip()
            is_tax_name = True
        elif n.owner_name.strip():
            raw_name = n.owner_name.strip()
            is_tax_name = False
        else:
            continue
        if _BUSINESS_RE.search(raw_name):
            continue
        # Probate notice is proof of death — set regardless of obituary search
        if n.notice_type == "probate" and n.decedent_name.strip():
            n.owner_deceased = "yes"
        candidates.append((n, raw_name, is_tax_name))

    if not candidates:
        logger.info("No notices with owner names for obituary search")
        return

    # ── Phase A: Obituary search ──────────────────────────────────────

    logger.info("Phase A: Searching obituaries for %d property owners...", len(candidates))

    # Search result cache: normalized_name → (parsed, url, source_type) or None (miss)
    # Prevents duplicate DDG + LLM calls when the same owner appears on multiple notices
    search_cache: dict[str, tuple[dict, str, str] | None] = {}
    cache_hits = 0

    confirmed = 0
    searched = 0
    skipped = 0
    miss_no_results = 0    # DDG returned 0 results even after fallbacks
    miss_fetch_failed = 0  # Got results but all page fetches returned < 100 chars
    miss_llm_rejected = 0  # Got page text but LLM returned no match / low confidence
    # Store match data for Phase B: [(notice, parsed, url, source_type, raw_name)]
    matches: list[tuple[NoticeData, dict, str, str, str, bool]] = []

    probate_preset_count = 0

    for i, (notice, raw_name, is_tax_name) in enumerate(candidates, 1):
        # Probate executor pre-set: use named PR as DM when available.
        # Courthouse terminal records have PR name but no address — still use preset
        # to avoid overriding the court-named PR with a wrong obituary match.
        if (
            notice.notice_type == "probate"
            and notice.owner_name
            and notice.decedent_name
            and notice.owner_name.strip().upper() != notice.decedent_name.strip().upper()
        ):
            synthetic_parsed = {
                "confidence": "high",
                "full_name": notice.decedent_name or notice.owner_name,
                "date_of_death": "",
                "survivors": [],
                "executor_named": notice.owner_name,
                "_probate_preset": True,
            }
            matches.append((notice, synthetic_parsed, notice.source_url or "", "probate_preset", raw_name, is_tax_name))
            confirmed += 1
            probate_preset_count += 1
            logger.info(
                "  [%d/%d] Probate preset: executor=%s, decedent=%s",
                i, len(candidates), notice.owner_name, notice.decedent_name,
            )
            continue

        if is_tax_name:
            search_names = parse_tax_owner_name(raw_name)
        else:
            search_names = _parse_notice_owner_name(raw_name)
        if not search_names:
            skipped += 1
            continue

        city = notice.city.strip() or ""
        found = False

        for search_name in search_names[:2]:  # Primary + secondary (joint owner)
            # Check cache — same owner name across multiple notices
            cache_key = search_name.lower().strip()
            if cache_key in search_cache:
                cached = search_cache[cache_key]
                cache_hits += 1
                if cached is not None:
                    c_parsed, c_url, c_source = cached
                    matches.append((notice, c_parsed, c_url, c_source, raw_name, is_tax_name))
                    confirmed += 1
                    found = True
                    logger.debug("  [%d/%d] %s: cache hit (deceased)", i, len(candidates), search_name)
                else:
                    logger.debug("  [%d/%d] %s: cache hit (no match)", i, len(candidates), search_name)
                break

            # Run primary + no-city searches and merge results (dedup by URL)
            results = _search_obituary(search_name, city)
            no_city_results = _search_obituary(search_name, "")
            seen_urls = {r["url"] for r in results}
            for r in no_city_results:
                if r["url"] not in seen_urls:
                    results.append(r)
                    seen_urls.add(r["url"])
            searched += 1

            if not results:
                # Fallback 2: drop middle initial ("Daniel H Williams" → "Daniel Williams")
                # Some sites only index first + last; middle initial reduces recall
                parts = search_name.split()
                if len(parts) == 3:
                    name_no_mi = f"{parts[0]} {parts[2]}"
                    results = _search_obituary(name_no_mi, city)
                    if results:
                        logger.debug(
                            "  [%d/%d] %s: fallback query (no MI) found %d results",
                            i, len(candidates), name_no_mi, len(results),
                        )

            if not results:
                # Fallback 3: try nickname variants ("Robert" → "Bob", etc.)
                parts = search_name.split()
                first = parts[0] if parts else ""
                last = parts[-1] if len(parts) >= 2 else ""
                if first and last:
                    for variant in _get_name_variants(first):
                        nick_name = f"{variant} {last}".title()
                        results = _search_obituary(nick_name, city)
                        if results:
                            logger.debug(
                                "  [%d/%d] %s: nickname fallback (%s) found %d results",
                                i, len(candidates), search_name, nick_name, len(results),
                            )
                            break

            if not results:
                # Fallback 4: "death notice" / funeral home query
                results = _search_obituary(
                    search_name, city,
                    extra_terms='"death notice" OR "funeral"',
                )
                if results:
                    logger.debug(
                        "  [%d/%d] %s: death notice fallback found %d results",
                        i, len(candidates), search_name, len(results),
                    )

            if not results:
                logger.debug("  [%d/%d] %s: no obituary results", i, len(candidates), search_name)
                time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))
                continue

            # Try each result — fetch full page, then LLM parse
            best_snippet_result = None
            any_fetch_succeeded = False
            for result in results:
                page_text = _fetch_page_text(result["url"])
                if not page_text or len(page_text) < 100:
                    if not best_snippet_result and result.get("snippet"):
                        best_snippet_result = result
                    continue
                any_fetch_succeeded = True

                time.sleep(random.uniform(0.5, 1.0))

                parsed = _parse_obituary_with_llm(
                    obituary_text=page_text,
                    owner_name=search_name,
                    city=city,
                    address=notice.address,
                    api_key=api_key,
                )

                if parsed and parsed.get("confidence") in ("high", "medium"):
                    # DOD sanity check — reject matches where death is implausibly old
                    if not _dod_sanity_check(parsed.get("date_of_death", ""), notice):
                        logger.info(
                            "  [%d/%d] %s: Rejected obituary (DOD too far from filing date)",
                            i, len(candidates), search_name,
                        )
                        continue  # try next search result
                    # Store raw text for second-pass aggressive extraction in build_heir_map()
                    parsed["_raw_obituary_text"] = page_text
                    parsed["_search_name"] = search_name
                    # Mark deceased now (Phase A.5 checks this to skip resolved candidates)
                    notice.owner_deceased = "yes"
                    # Store for Phase B heir processing
                    matches.append((notice, parsed, result["url"], "full_page", raw_name, is_tax_name))
                    search_cache[cache_key] = (parsed, result["url"], "full_page")
                    confirmed += 1
                    found = True
                    logger.info(
                        "  [%d/%d] %s: DECEASED (DOD: %s)",
                        i, len(candidates), search_name,
                        parsed.get("date_of_death", "unknown"),
                    )
                    break

            # Snippet fallback
            if not found and best_snippet_result:
                snippet_text = (
                    f"Search result title: {best_snippet_result['title']}\n"
                    f"URL: {best_snippet_result['url']}\n"
                    f"Snippet: {best_snippet_result['snippet']}"
                )
                for r in results:
                    if r["url"] != best_snippet_result["url"] and r.get("snippet"):
                        snippet_text += f"\n\nAdditional result: {r['title']}\nSnippet: {r['snippet']}"

                logger.debug("  Page fetches failed, trying snippet fallback for %s", search_name)
                parsed = _parse_obituary_with_llm(
                    obituary_text=snippet_text,
                    owner_name=search_name,
                    city=city,
                    address=notice.address,
                    api_key=api_key,
                )

                _conf = parsed.get("confidence", "") if parsed else ""
                _snippet_ok = _conf == "high" or (
                    _conf == "medium"
                    and parsed.get("full_name", "").strip()
                    and parsed.get("date_of_death", "").strip()
                )
                if parsed and _snippet_ok:
                    # DOD sanity check — reject matches where death is implausibly old
                    if not _dod_sanity_check(parsed.get("date_of_death", ""), notice):
                        logger.info(
                            "  [%d/%d] %s: Rejected snippet match (DOD too far from filing date)",
                            i, len(candidates), search_name,
                        )
                        parsed = None
                        _snippet_ok = False
                if parsed and _snippet_ok:
                    # Mark deceased now (Phase A.5 checks this to skip resolved candidates)
                    notice.owner_deceased = "yes"
                    matches.append((notice, parsed, best_snippet_result["url"], "snippet", raw_name, is_tax_name))
                    search_cache[cache_key] = (parsed, best_snippet_result["url"], "snippet")
                    confirmed += 1
                    found = True
                    logger.info(
                        "  [%d/%d] %s: DECEASED (snippet/%s, DOD: %s)",
                        i, len(candidates), search_name, _conf,
                        parsed.get("date_of_death", "unknown"),
                    )

            if found:
                break

        # Cache miss for this name
        if not found and cache_key and cache_key not in search_cache:
            search_cache[cache_key] = None

        if not found and searched > 0:
            logger.debug(
                "  [%d/%d] %s: no obituary match", i, len(candidates),
                search_names[0] if search_names else raw_name,
            )
            # Track WHY the record failed (for end-of-phase diagnostic summary)
            if not results:
                miss_no_results += 1
            elif not any_fetch_succeeded:
                miss_fetch_failed += 1
            else:
                miss_llm_rejected += 1

        if i % 25 == 0:
            logger.info(
                "Phase A progress: %d/%d (confirmed=%d, searched=%d)",
                i, len(candidates), confirmed, searched,
            )

        time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    logger.info(
        "Phase A complete: %d confirmed deceased (%d probate preset), %d searched, %d skipped, %d cache hits | "
        "no-match: no_results=%d, fetch_failed=%d, llm_rejected=%d",
        confirmed, probate_preset_count, searched, skipped, cache_hits,
        miss_no_results, miss_fetch_failed, miss_llm_rejected,
    )

    # ── Phase A.5: Ancestry fallback for unresolved candidates ─────────
    # Only search Ancestry for candidates that Phase A couldn't confirm.
    # Saves page loads (100/day limit) and time (~15s/lookup).

    import config as cfg  # noqa: PLC0415
    if not skip_ancestry and cfg.ANCESTRY_EMAIL and cfg.ANCESTRY_PASSWORD:
        import asyncio
        import ancestry_enricher

        unresolved = [
            (idx, notice, raw_name, is_tax_name)
            for idx, (notice, raw_name, is_tax_name) in enumerate(candidates)
            if notice.owner_deceased != "yes"
            and not (notice.notice_type == "probate" and notice.owner_name and notice.owner_street)
        ]

        if unresolved:
            logger.info("Ancestry fallback: %d unresolved candidates (of %d total)",
                        len(unresolved), len(candidates))
            ancestry_match_data = []  # collect (notice, raw_name, is_tax_name, result) for Phase B

            async def _ancestry_fallback():
                ancestry_hits = 0
                pw, context, page = await ancestry_enricher.launch_browser()
                if not page:
                    logger.warning("Ancestry: could not launch browser — skipping fallback")
                    return ancestry_hits

                try:
                    for idx, notice, raw_name, is_tax_name in unresolved:
                        if ancestry_enricher.is_circuit_broken() or not ancestry_enricher._can_load_page():
                            logger.warning("Ancestry: circuit breaker or daily limit — stopping fallback")
                            break

                        if is_tax_name:
                            names = parse_tax_owner_name(raw_name)
                        else:
                            names = _parse_notice_owner_name(raw_name)
                        if not names:
                            continue

                        search_name = names[0]
                        city = notice.city.strip() or ""

                        result = await ancestry_enricher.lookup_deceased(
                            page, name=search_name, city=city, state="TN"
                        )
                        if result and result.get("confirmed_deceased"):
                            notice.owner_deceased = "yes"
                            notice.date_of_death = result.get("date_of_death", "")
                            notice.obituary_url = result.get("source_url", "")
                            notice.obituary_source_type = result.get("source_type", "ancestry")
                            ancestry_match_data.append((notice, raw_name, is_tax_name, result))
                            ancestry_hits += 1
                            logger.info(
                                "  Ancestry hit [%d/%d]: %s → %s (source: %s)",
                                ancestry_hits, len(unresolved), search_name,
                                result.get("full_name", ""), result.get("source_type", ""),
                            )
                finally:
                    await ancestry_enricher.close_browser(pw, context)

                return ancestry_hits

            try:
                ancestry_hits = asyncio.run(_ancestry_fallback())
                if ancestry_hits:
                    confirmed += ancestry_hits
                    # Enrich Ancestry hits with DuckDuckGo obituary text for heir extraction
                    for notice, raw_name, is_tax_name, result in ancestry_match_data:
                        confirmed_name = result.get("full_name", "")
                        city = notice.city.strip() or ""
                        source_url = result.get("source_url", "")
                        source_type = "ancestry"

                        # Try DuckDuckGo search using the Ancestry-confirmed name
                        ancestry_parsed = None
                        if confirmed_name:
                            obit_results = _search_obituary(confirmed_name, city)
                            if obit_results:
                                for obit_r in obit_results[:3]:
                                    page_text = _fetch_page_text(obit_r["url"])
                                    if page_text and len(page_text) > 200:
                                        parsed = _parse_obituary_with_llm(
                                            obituary_text=page_text,
                                            owner_name=confirmed_name,
                                            city=city,
                                            address=notice.address,
                                            api_key=api_key,
                                        )
                                        if parsed and parsed.get("confidence") in ("high", "medium"):
                                            parsed["_raw_obituary_text"] = page_text
                                            parsed["_search_name"] = confirmed_name
                                            # Preserve Ancestry death confirmation
                                            if not parsed.get("date_of_death"):
                                                parsed["date_of_death"] = result.get("date_of_death", "")
                                            ancestry_parsed = parsed
                                            source_url = obit_r["url"]
                                            source_type = "ancestry+full_page"
                                            notice.obituary_url = source_url
                                            logger.info(
                                                "  Ancestry enriched: %s → full obituary from %s",
                                                confirmed_name, obit_r["url"],
                                            )
                                            break

                        # Fallback: minimal Ancestry-only parsed dict
                        if not ancestry_parsed:
                            ancestry_parsed = {
                                "confidence": "high",
                                "date_of_death": result.get("date_of_death", ""),
                                "deceased_name": confirmed_name,
                                "survivors": [],
                                "executor_named": "",
                            }
                            # If Ancestry returned family members, add them as survivors
                            for fm in result.get("family_members", []):
                                ancestry_parsed["survivors"].append({
                                    "name": fm.get("name", ""),
                                    "relationship": fm.get("relationship", ""),
                                })

                        matches.append((notice, ancestry_parsed, source_url,
                                        source_type, raw_name, is_tax_name))
                    logger.info("Ancestry fallback: resolved %d/%d unresolved candidates",
                                ancestry_hits, len(unresolved))
            except Exception as e:
                logger.warning("Ancestry fallback failed: %s", e)
    elif not skip_ancestry:
        logger.debug("Ancestry: no credentials configured — skipping")

    # ── Phase B: Heir verification + ranked DMs + joint-owner fallback ─

    full_page_matches = sum(1 for _, _, _, st, *_ in matches if st == "full_page")
    snippet_matches = sum(1 for _, _, _, st, *_ in matches if st == "snippet")
    logger.info(
        "Phase B: Processing %d deceased records (%d full-page, %d snippet)...",
        len(matches), full_page_matches, snippet_matches,
    )

    heir_verified_count = 0
    joint_owner_dm_count = 0
    research_dm_count = 0
    snippet_dm_count = 0
    no_dm_possible_count = 0
    estate_fallback_count = 0
    dm_addr_sources = {"knox_tax_api": 0, "people_search": 0,
                       "ddg_people_search": 0, "inline_tracerfy": 0, "batch_tracerfy": 0}

    for j, (notice, parsed, url, source_type, raw_name, is_tax_name) in enumerate(matches, 1):
        city = notice.city.strip() or ""
        survivors = parsed.get("survivors", [])
        has_survivors = bool(survivors) or bool(parsed.get("executor_named", ""))

        ranked_dms = None
        error_info = None

        # Path -1: Joint co-owner takes priority as DM
        # When property has 2+ owners and one is confirmed deceased,
        # the surviving co-owner is the most actionable contact (on title, likely spouse)
        if not parsed.get("_probate_preset"):
            # Try tax name format first, then notice owner_name
            co_owner_names = (
                parse_tax_owner_name(raw_name) if is_tax_name
                else _parse_notice_owner_name(raw_name)
            )
            # Also check notice.owner_name for "X And Y" not captured above
            if len(co_owner_names) < 2 and notice.owner_name.strip():
                co_owner_names = _parse_notice_owner_name(notice.owner_name.strip())

            if len(co_owner_names) >= 2:
                # Deceased is first name (matched in Phase A); co-owner is second
                co_owner_name = co_owner_names[1]
                co_owner_status = "unverified"

                if not skip_heir_verification:
                    logger.info(
                        "  [%d/%d] Joint co-owner on title: %s — verifying alive...",
                        j, len(matches), co_owner_name,
                    )
                    verification = verify_heir_status(
                        heir_name=co_owner_name, city=city, api_key=api_key,
                    )
                    co_owner_status = verification["status"]

                if co_owner_status != "deceased":
                    ranked_dms = [{
                        "name": co_owner_name,
                        "relationship": "spouse",
                        "status": co_owner_status,
                        "source": "joint_owner_on_title",
                        "rank": 1,
                    }]
                    _co_conf = "high" if co_owner_status == "verified_living" else "medium"
                    error_info = {
                        "heir_search_depth": 1 if not skip_heir_verification else 0,
                        "heirs_verified_living": 1 if co_owner_status == "verified_living" else 0,
                        "heirs_verified_deceased": 0,
                        "heirs_unverified": 1 if co_owner_status == "unverified" else 0,
                        "missing_flags": [],
                        "dm_confidence": _co_conf,
                        "dm_confidence_reason": f"joint co-owner on title ({co_owner_status})",
                    }
                    joint_owner_dm_count += 1
                    logger.info(
                        "  [%d/%d] DM = co-owner %s (joint_owner_on_title, %s)",
                        j, len(matches), co_owner_name, _co_conf,
                    )
                else:
                    logger.info(
                        "  [%d/%d] Co-owner %s is also deceased — falling through to heir search",
                        j, len(matches), co_owner_name,
                    )

        # Path 0: Probate preset — executor is the DM, address from notice
        if parsed.get("_probate_preset"):
            ranked_dms = [{
                "name": notice.owner_name,
                "relationship": "executor",
                "status": "verified_living",
                "source": "probate_notice",
                "rank": 1,
                "street": notice.owner_street,
                "city": notice.owner_city or "",
                "state": "TN",
                "zip": notice.owner_zip,
            }]
            error_info = {
                "heir_search_depth": 0,
                "heirs_verified_living": 1,
                "heirs_verified_deceased": 0,
                "heirs_unverified": 0,
                "dm_confidence": "high",
                "dm_confidence_reason": "executor named in probate notice with mailing address",
            }
            # Mark decedent as deceased
            if notice.decedent_name:
                notice.owner_deceased = "yes"
            logger.info(
                "  [%d/%d] Probate preset DM: %s (executor) at %s",
                j, len(matches), notice.owner_name, notice.owner_street,
            )

        # Path 1: Full-page match with survivors → run heir verification
        # Skip if Path -1 (joint co-owner) or Path 0 (probate) already set DM
        if has_survivors and not skip_heir_verification and not ranked_dms:
            logger.info(
                "  [%d/%d] Verifying heirs for %s (%d survivors)...",
                j, len(matches), notice.owner_name, len(survivors),
            )
            ranked_dms, error_info = build_heir_map(
                parsed=parsed,
                city=city,
                api_key=api_key,
                raw_name=raw_name,
                max_depth=max_heir_depth,
            )
            error_info["heir_search_depth"] = 1
            heir_verified_count += 1

        # Path 2: No survivors + listing URL → re-search for specific obituary
        if not has_survivors and not ranked_dms and source_type == "snippet" and _is_listing_url(url):
            search_names = parse_tax_owner_name(raw_name)
            if search_names:
                logger.info(
                    "  [%d/%d] Re-searching for specific obituary (listing URL): %s",
                    j, len(matches), search_names[0],
                )
                new_parsed, new_url, new_source = _refetch_specific_obituary(
                    name=search_names[0],
                    city=city,
                    original_url=url,
                    api_key=api_key,
                    address=notice.address,
                )
                if new_parsed:
                    parsed = new_parsed
                    url = new_url
                    source_type = new_source
                    survivors = parsed.get("survivors", [])
                    has_survivors = bool(survivors) or bool(parsed.get("executor_named", ""))
                    if has_survivors and not skip_heir_verification:
                        ranked_dms, error_info = build_heir_map(
                            parsed=parsed,
                            city=city,
                            api_key=api_key,
                            raw_name=raw_name,
                            max_depth=max_heir_depth,
                        )
                        error_info["heir_search_depth"] = 1
                        research_dm_count += 1

        # Path 3a: No survivors + snippet → try Firecrawl on the snippet URL
        #   The URL was likely 403-blocked during Phase A; Firecrawl may bypass it.
        if not has_survivors and not ranked_dms and source_type == "snippet" and url:
            search_names = parse_tax_owner_name(raw_name)
            search_name = search_names[0] if search_names else raw_name
            logger.info(
                "  [%d/%d] Trying Firecrawl on snippet URL for %s: %s",
                j, len(matches), search_name, url,
            )
            fc_text = _fetch_firecrawl(url, priority="medium")
            if fc_text and len(fc_text) >= 200:
                fc_parsed = _parse_obituary_with_llm(
                    obituary_text=fc_text,
                    owner_name=search_name,
                    city=city,
                    address=notice.address,
                    api_key=api_key,
                )
                if fc_parsed:
                    parsed = fc_parsed
                    parsed["_raw_obituary_text"] = fc_text
                    source_type = "full_page"
                    survivors = parsed.get("survivors", [])
                    has_survivors = bool(survivors) or bool(parsed.get("executor_named", ""))
                    if has_survivors:
                        logger.info(
                            "  [%d/%d] Firecrawl upgraded snippet → full page (%d survivors)",
                            j, len(matches), len(survivors),
                        )
                        if not skip_heir_verification:
                            ranked_dms, error_info = build_heir_map(
                                parsed=parsed,
                                city=city,
                                api_key=api_key,
                                raw_name=raw_name,
                                max_depth=max_heir_depth,
                            )
                            error_info["heir_search_depth"] = 1
                            error_info["missing_flags"] = error_info.get("missing_flags", [])
                            error_info["missing_flags"].append("dm_from_firecrawl_upgrade")
                            research_dm_count += 1

        # Path 3b: No survivors + snippet → targeted survivor search
        if not has_survivors and not ranked_dms and source_type == "snippet":
            search_names = parse_tax_owner_name(raw_name)
            if search_names:
                logger.info(
                    "  [%d/%d] Targeted snippet survivor search: %s",
                    j, len(matches), search_names[0],
                )
                extra_survivors = _search_survivors_targeted(
                    name=search_names[0],
                    city=city,
                    api_key=api_key,
                )
                if extra_survivors:
                    parsed["survivors"] = extra_survivors
                    survivors = extra_survivors
                    has_survivors = True
                    if not skip_heir_verification:
                        ranked_dms, error_info = build_heir_map(
                            parsed=parsed,
                            city=city,
                            api_key=api_key,
                            raw_name=raw_name,
                            max_depth=max_heir_depth,
                        )
                        error_info["heir_search_depth"] = 1
                        error_info["missing_flags"] = error_info.get("missing_flags", [])
                        error_info["missing_flags"].append("dm_from_targeted_snippet")
                        snippet_dm_count += 1

        # Path 4: No survivors — try joint owner from tax record as probable spouse
        if not has_survivors and not ranked_dms:
            search_names = parse_tax_owner_name(raw_name)
            if len(search_names) >= 2:
                # Second name = probable spouse (e.g., "WILLIAMS DANIEL H & CHRISTINE C")
                spouse_name = search_names[1]
                spouse_status = "unverified"

                # Optionally verify spouse is alive
                if not skip_heir_verification:
                    logger.info(
                        "  [%d/%d] No survivors in obituary, checking joint owner: %s",
                        j, len(matches), spouse_name,
                    )
                    verification = verify_heir_status(
                        heir_name=spouse_name,
                        city=city,
                        api_key=api_key,
                    )
                    spouse_status = verification["status"]

                ranked_dms = [{
                    "name": spouse_name,
                    "relationship": "spouse",
                    "status": spouse_status,
                    "source": "tax_record_joint_owner",
                    "rank": 1,
                }]
                _jt_conf = "high" if spouse_status == "verified_living" else (
                    "medium" if spouse_status != "deceased" else "low"
                )
                error_info = {
                    "heir_search_depth": 1 if not skip_heir_verification else 0,
                    "heirs_verified_living": 1 if spouse_status == "verified_living" else 0,
                    "heirs_verified_deceased": 1 if spouse_status == "deceased" else 0,
                    "heirs_unverified": 1 if spouse_status == "unverified" else 0,
                    "missing_flags": ["dm_from_tax_record"],
                    "dm_confidence": _jt_conf,
                    "dm_confidence_reason": f"joint owner from tax record ({spouse_status})",
                }
                if source_type == "snippet":
                    error_info["missing_flags"].append("snippet_only")
                joint_owner_dm_count += 1

        # Path 4b: No survivors — try joint owner from notice text (owner_name)
        #   Catches cases where tax record is single-name but notice text has "X and Y"
        if not has_survivors and not ranked_dms and notice.owner_name.strip():
            owner = notice.owner_name.strip()
            # Split on " & " or " and " (case-insensitive)
            joint_parts = re.split(r"\s+(?:&|and)\s+", owner, flags=re.IGNORECASE)
            if len(joint_parts) >= 2:
                # Deceased is likely first name; co-owner is second
                deceased_name = joint_parts[0].strip()
                co_owner = joint_parts[1].strip()
                # If co-owner is just a first name, append the deceased's last name
                if co_owner and " " not in co_owner:
                    deceased_tokens = deceased_name.split()
                    if len(deceased_tokens) >= 2:
                        co_owner = f"{co_owner} {deceased_tokens[-1]}"
                if co_owner and len(co_owner.split()) >= 2:
                    spouse_status = "unverified"
                    if not skip_heir_verification:
                        logger.info(
                            "  [%d/%d] No survivors, checking notice co-owner: %s",
                            j, len(matches), co_owner,
                        )
                        verification = verify_heir_status(
                            heir_name=co_owner,
                            city=city,
                            api_key=api_key,
                        )
                        spouse_status = verification["status"]
                    ranked_dms = [{
                        "name": co_owner,
                        "relationship": "spouse",
                        "status": spouse_status,
                        "source": "notice_text_joint_owner",
                        "rank": 1,
                    }]
                    _nt_conf = "high" if spouse_status == "verified_living" else (
                        "medium" if spouse_status != "deceased" else "low"
                    )
                    error_info = {
                        "heir_search_depth": 1 if not skip_heir_verification else 0,
                        "heirs_verified_living": 1 if spouse_status == "verified_living" else 0,
                        "heirs_verified_deceased": 1 if spouse_status == "deceased" else 0,
                        "heirs_unverified": 1 if spouse_status == "unverified" else 0,
                        "missing_flags": ["dm_from_notice_text"],
                        "dm_confidence": _nt_conf,
                        "dm_confidence_reason": f"joint owner from notice text ({spouse_status})",
                    }
                    if source_type == "snippet":
                        error_info["missing_flags"].append("snippet_only")
                    joint_owner_dm_count += 1

        # Path 5: Estate-of fallback — no survivors, mail to property address
        if not has_survivors and not ranked_dms:
            estate_name = f"Estate of {notice.owner_name}"
            ranked_dms = [{
                "name": estate_name,
                "relationship": "estate",
                "status": "estate_fallback",
                "source": "estate_fallback",
                "rank": 1,
                "street": notice.address,
                "city": notice.city or "",
                "state": "TN",
                "zip": notice.zip,
            }]
            error_info = {
                "heir_search_depth": 0,
                "heirs_verified_living": 0,
                "heirs_verified_deceased": 0,
                "heirs_unverified": 0,
                "missing_flags": ["no_survivors", "dm_from_estate_fallback"],
                "dm_confidence": "low",
                "dm_confidence_reason": "no living relative found; mailing to property address",
            }
            estate_fallback_count += 1
            logger.info(
                "  [%d/%d] %s: estate fallback -> %s at %s",
                j, len(matches), notice.owner_name, estate_name, notice.address,
            )

        # ── Address Lookup for ALL signing-authority heirs ──────────
        # Look up mailing addresses for every heir in the signing chain,
        # not just DM #1. This enables multi-heir skip tracing for deal closing.
        import config as cfg  # noqa: PLC0415
        if not skip_dm_address and ranked_dms:
            # Identify which heirs need address lookup
            signing_heirs = [
                dm for dm in ranked_dms
                if dm.get("signing_authority")
                and dm.get("status") != "deceased"
                and dm.get("name")
                and not dm.get("street")
            ]
            # Always include DM #1 even if not signing authority (primary contact)
            if ranked_dms and not ranked_dms[0].get("street") and ranked_dms[0] not in signing_heirs:
                signing_heirs.insert(0, ranked_dms[0])

            for dm in signing_heirs:
                dm_name = dm["name"]
                # Get DM's city from survivor data if available
                dm_city_hint = ""
                for s in parsed.get("survivors", []):
                    if s.get("name", "").lower() == dm_name.lower():
                        dm_city_hint = s.get("city", "")
                        break
                if not dm_city_hint:
                    dm_city_hint = city  # fall back to property city

                logger.info(
                    "  [%d/%d] Looking up address for DM: %s (city hint: %s)",
                    j, len(matches), dm_name, dm_city_hint or "unknown",
                )
                addr = _lookup_dm_address(dm_name, dm_city_hint, api_key,
                                          tracerfy_tier1=tracerfy_tier1)
                if addr.get("street"):
                    dm.update(addr)
                    source = addr.get("source", "unknown")
                    if source in dm_addr_sources:
                        dm_addr_sources[source] += 1
                    else:
                        dm_addr_sources[source] = 1
                    logger.info(
                        "  [%d/%d] Found DM address (%s): %s, %s %s %s",
                        j, len(matches), source, addr["street"], addr["city"],
                        addr["state"], addr["zip"],
                    )
                    continue

                # Tier 3 inline: Tracerfy when all preceding tiers missed
                if cfg.TRACERFY_API_KEY:
                    tracerfy_result = _lookup_dm_address_tracerfy(
                        dm_name, dm_city_hint or city,
                        address=notice.address, zip_code=notice.zip,
                    )
                    if tracerfy_result and tracerfy_result.get("street"):
                        dm.update(tracerfy_result)
                        dm_addr_sources["inline_tracerfy"] = (
                            dm_addr_sources.get("inline_tracerfy", 0) + 1
                        )
                        logger.info(
                            "  [%d/%d] Inline Tracerfy found address for %s: %s",
                            j, len(matches), dm_name, tracerfy_result["street"],
                        )
                        continue

                # Tier 4: Property address fallback (DM #1 only — others left empty)
                if dm is ranked_dms[0] and dm.get("source") != "estate_fallback":
                    dm["street"] = notice.address
                    dm["city"] = notice.city or ""
                    dm["state"] = "TN"
                    dm["zip"] = notice.zip
                    dm_addr_sources["property_fallback"] = (
                        dm_addr_sources.get("property_fallback", 0) + 1
                    )
                    logger.info(
                        "  [%d/%d] Using property address as DM fallback: %s",
                        j, len(matches), notice.address,
                    )

        # Apply the match with all collected data
        _apply_obituary_match(
            notice=notice,
            parsed=parsed,
            url=url,
            source_type=source_type,
            ranked_dms=ranked_dms,
            error_info=error_info,
        )

        if j % 10 == 0:
            logger.info(
                "Phase B progress: %d/%d (heir-verified=%d, joint-owner-DM=%d)",
                j, len(matches), heir_verified_count, joint_owner_dm_count,
            )

    # ── Phase C: Batch Tracerfy for remaining DMs without addresses ──
    import config as cfg
    if not skip_dm_address and cfg.TRACERFY_API_KEY:
        dm_needs_addr = [
            n for n in notices
            if n.decision_maker_name and not n.decision_maker_street
        ]
        if dm_needs_addr:
            logger.info(
                "Phase C: Batch Tracerfy skip trace for %d DMs without addresses...",
                len(dm_needs_addr),
            )
            before_count = sum(1 for n in dm_needs_addr if n.decision_maker_street)
            _batch_tracerfy_lookup(dm_needs_addr)
            batch_found = sum(1 for n in dm_needs_addr if n.decision_maker_street) - before_count
            dm_addr_sources["batch_tracerfy"] += batch_found

    # ── Summary ───────────────────────────────────────────────────────

    dm_identified = sum(1 for n in notices if n.decision_maker_name)
    dm_verified_living = sum(1 for n in notices if n.decision_maker_status == "verified_living")
    dm_with_addr = sum(1 for n in notices if n.decision_maker_street)

    logger.info("Obituary enrichment complete:")
    logger.info("  Confirmed deceased:     %d", confirmed)
    logger.info("  DM identified:          %d/%d (%.0f%%)",
                dm_identified, confirmed, 100 * dm_identified / confirmed if confirmed else 0)
    logger.info("  DM verified living:     %d", dm_verified_living)
    logger.info("  Heir-verified records:  %d", heir_verified_count)
    logger.info("  Joint-owner DM added:   %d", joint_owner_dm_count)
    logger.info("  Re-search DM added:     %d", research_dm_count)
    logger.info("  Snippet DM added:       %d", snippet_dm_count)
    logger.info("  Estate-of fallback:     %d", estate_fallback_count)
    logger.info("  No DM possible:         %d", no_dm_possible_count)
    if dm_identified:
        logger.info("  DM addresses found:     %d/%d (%.0f%%)",
                    dm_with_addr, dm_identified,
                    100 * dm_with_addr / dm_identified)
        active_sources = {k: v for k, v in dm_addr_sources.items() if v > 0}
        if active_sources:
            logger.info("  DM address sources:")
            for source, count in active_sources.items():
                logger.info("    %-20s %d", source, count)
