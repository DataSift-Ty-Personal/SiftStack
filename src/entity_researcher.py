"""Entity research enricher — finds the real person behind entity-owned properties.

Uses web search (DuckDuckGo) + Claude Haiku LLM to identify the signing member,
registered agent, trustee, or officer behind LLCs, corporations, trusts, and
other business entities. Follows the same pattern as obituary_enricher.py.

Pipeline integration: runs BEFORE the entity filter so researched entities
can optionally survive filtering. Opt-in via --research-entities CLI flag.
"""

import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
from ddgs import DDGS

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 256
SEARCH_DELAY_MIN = 0.5
SEARCH_DELAY_MAX = 1.0
PARALLEL_WORKERS = 4

# ── Entity Classification ──────────────────────────────────────────────

_LLC_RE = re.compile(r"\b(?:LLC|L\.L\.C)\b", re.IGNORECASE)
_CORP_RE = re.compile(
    r"\b(?:INC|INCORPORATED|CORP|CORPORATION)\b", re.IGNORECASE
)
_LP_RE = re.compile(r"\b(?:LP|L\.P|LLP|LIMITED\s+PARTNERSHIP)\b", re.IGNORECASE)
_TRUST_RE = re.compile(r"\bTRUST\b", re.IGNORECASE)
_ESTATE_RE = re.compile(r"\bESTATE\b", re.IGNORECASE)


def _classify_entity(name: str) -> str:
    """Classify an entity name into a type category.

    Returns one of: llc, corp, trust, estate, lp, other, or "" if not an entity.
    """
    if not name:
        return ""
    upper = name.upper().strip()

    if _LLC_RE.search(upper):
        return "llc"
    if _CORP_RE.search(upper):
        return "corp"
    if _LP_RE.search(upper):
        return "lp"
    if _TRUST_RE.search(upper):
        return "trust"
    if _ESTATE_RE.search(upper):
        return "estate"
    if config.BUSINESS_RE.search(upper):
        return "other"
    return ""


# ── Name Parsing (Free Fast Path) ──────────────────────────────────────


def _try_parse_entity_name(name: str, entity_type: str) -> dict | None:
    """Try to extract a person name directly from the entity name.

    This is the free fast path — no API calls. Returns a dict with
    person_name, role, confidence, or None if no parse possible.
    """
    if not name:
        return None

    # Trust: use config.TRUST_NAME_RE to extract the grantor/trustee name
    if entity_type == "trust":
        m = config.TRUST_NAME_RE.match(name)
        if m:
            extracted = m.group(1).strip()
            parts = extracted.split()
            # "JOHN DOE REVOCABLE TRUST" → "John Doe" (high confidence)
            # Check if it looks like a person name (2-4 words, no business keywords)
            business_words = {
                "FIRST", "NATIONAL", "AMERICAN", "COMMUNITY", "BANK",
                "FINANCIAL", "INVESTMENT", "CAPITAL", "HOLDINGS",
                "PROPERTIES", "MANAGEMENT", "GROUP", "SERVICES",
            }
            if len(parts) >= 2 and not any(w.upper() in business_words for w in parts):
                return {
                    "person_name": extracted.title(),
                    "role": "trustee",
                    "confidence": "high",
                }
            # Single word like "SMITH" — last name only, medium confidence
            if len(parts) == 1 and parts[0].upper() not in business_words:
                return {
                    "person_name": parts[0].title(),
                    "role": "trustee",
                    "confidence": "medium",
                }
        return None

    # Estate: use config.ESTATE_OF_RE to extract the decedent name
    if entity_type == "estate":
        m = config.ESTATE_OF_RE.match(name)
        if m:
            extracted = m.group(1).strip()
            if extracted:
                return {
                    "person_name": extracted.title(),
                    "role": "executor",
                    "confidence": "high",
                }
        return None

    # LLC with a personal surname: "JOHNSON PROPERTIES LLC" → "Johnson" (low)
    if entity_type == "llc":
        cleaned = _LLC_RE.sub("", name).strip()
        cleaned = re.sub(r"[,.]", "", cleaned).strip()
        words = cleaned.split()
        # Only single-word-before-generic: "JOHNSON PROPERTIES" → "Johnson"
        generic = {
            "PROPERTIES", "HOLDINGS", "INVESTMENTS", "CAPITAL", "GROUP",
            "ENTERPRISES", "VENTURES", "REALTY", "HOMES", "REAL",
            "BUILDERS", "CONSTRUCTION", "MANAGEMENT", "SOLUTIONS",
            "SERVICES", "ASSOCIATES", "FUNDING", "ACQUISITIONS",
            "BUYERS", "RENOVATIONS", "DEVELOPMENT", "CONSULTING",
        }
        if len(words) == 2 and words[1].upper() in generic:
            candidate = words[0]
            # Reject if it's a common non-name word
            non_names = {
                "FIRST", "SECOND", "THIRD", "BEST", "PRIME", "TOP",
                "QUICK", "FAST", "SMART", "GOOD", "GREAT", "FAIR",
                "NEW", "OLD", "BIG", "LITTLE", "GLOBAL", "NATIONAL",
                "AMERICAN", "SOUTHERN", "EASTERN", "WESTERN", "NORTHERN",
                "CENTRAL", "PACIFIC", "ATLANTIC", "MOUNTAIN", "VALLEY",
                "LAKE", "RIVER", "HILL", "SUMMIT", "PEAK",
            }
            if candidate.upper() not in non_names and len(candidate) >= 3:
                return {
                    "person_name": candidate.title(),
                    "role": "member",
                    "confidence": "low",
                }

    return None


# ── Web Search ──────────────────────────────────────────────────────────


def _search_entity(entity_name: str, state: str = "Tennessee") -> list[dict]:
    """Search DuckDuckGo for entity registration info.

    Returns list of {url, title, snippet} results.
    """
    query = f'"{entity_name}" {state} registered agent OR member OR officer'

    try:
        results = DDGS().text(query, max_results=8, backend="google,duckduckgo,brave")
    except Exception as e:
        logger.debug("Search failed for '%s': %s", query, e)
        return []

    filtered = []
    for r in results:
        url = r.get("href", "")
        title = r.get("title", "")
        snippet = r.get("body", "")
        if url and (title or snippet):
            filtered.append({"url": url, "title": title, "snippet": snippet})

    return filtered


# ── LLM Parsing ─────────────────────────────────────────────────────────

ENTITY_SYSTEM_PROMPT = (
    "You are a business entity research assistant. You analyze search results "
    "to identify the real person behind a business entity. Return only valid JSON."
)

ENTITY_EXTRACT_PROMPT = """You are analyzing search results to find the real person behind a business entity.

Entity: "{entity_name}" (type: {entity_type})

Search results:
{snippets}

Extract the following if found:
- person_name: Full name of a person associated with this entity (first and last name)
- role: Their role (registered_agent, member, manager, officer, trustee, partner, principal, organizer)
- confidence: high (exact match from official records or business listing), medium (likely match from directory), low (name mentioned but unclear relationship)

If multiple people are found, return the one most likely to be the decision-maker (owner/member > registered agent > officer).

Return JSON: {{"person_name": "...", "role": "...", "confidence": "..."}}
If no person can be identified, return: {{"person_name": "", "role": "", "confidence": ""}}"""


def _parse_entity_with_llm(
    entity_name: str,
    entity_type: str,
    search_results: list[dict],
    api_key: str,
) -> dict | None:
    """Use Claude Haiku to extract person info from search results."""
    if not search_results or not api_key:
        return None

    # Build snippet text from search results
    snippets = []
    for r in search_results[:6]:
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        url = r.get("url", "")
        snippets.append(f"[{title}] ({url})\n{snippet}")

    combined = "\n\n".join(snippets)
    if len(combined) > 4000:
        combined = combined[:4000]

    prompt = ENTITY_EXTRACT_PROMPT.format(
        entity_name=entity_name,
        entity_type=entity_type,
        snippets=combined,
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=ENTITY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        # Strip markdown code fences if present
        result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
        result_text = re.sub(r"\s*```$", "", result_text)
        # Extract first JSON object if model returned extra text after it
        brace_match = re.search(r"\{[^{}]*\}", result_text)
        if brace_match:
            result_text = brace_match.group(0)
        parsed = json.loads(result_text)

        person_name = parsed.get("person_name", "").strip()
        if person_name:
            return {
                "person_name": person_name,
                "role": parsed.get("role", "").strip(),
                "confidence": parsed.get("confidence", "medium").strip(),
            }
        return None

    except (json.JSONDecodeError, anthropic.APIError, KeyError, IndexError) as e:
        logger.debug("LLM parse failed for '%s': %s", entity_name, e)
        return None


# ── Single-Record Processor ─────────────────────────────────────────────


def _research_single_entity(
    notice: NoticeData,
    api_key: str,
    search_cache: dict,
    cache_lock: threading.Lock,
) -> bool:
    """Research a single entity-owned notice. Returns True if person found."""
    name = (notice.tax_owner_name or notice.owner_name or "").strip()
    if not name:
        return False

    # Classify entity type
    entity_type = _classify_entity(name)
    if not entity_type:
        return False
    notice.entity_type = entity_type

    # Normalize for cache key
    cache_key = name.upper().strip()

    # Check cache
    with cache_lock:
        if cache_key in search_cache:
            cached = search_cache[cache_key]
            if cached:
                notice.entity_person_name = cached["person_name"]
                notice.entity_person_role = cached["role"]
                notice.entity_research_confidence = cached["confidence"]
                notice.entity_research_source = cached["source"]
                return True
            return False

    # Phase 1: Name parsing (free fast path)
    parsed = _try_parse_entity_name(name, entity_type)
    if parsed and parsed.get("person_name"):
        result = {
            "person_name": parsed["person_name"],
            "role": parsed["role"],
            "confidence": parsed["confidence"],
            "source": "name_parse",
        }
        notice.entity_person_name = result["person_name"]
        notice.entity_person_role = result["role"]
        notice.entity_research_confidence = result["confidence"]
        notice.entity_research_source = result["source"]

        with cache_lock:
            search_cache[cache_key] = result

        logger.debug("  Name parsed: %s → %s (%s)", name, result["person_name"], result["confidence"])
        return True

    # Phase 2: Web search + LLM
    time.sleep(random.uniform(SEARCH_DELAY_MIN, SEARCH_DELAY_MAX))

    state = notice.state or "Tennessee"
    if state == "TN":
        state = "Tennessee"

    search_results = _search_entity(name, state)
    if not search_results:
        with cache_lock:
            search_cache[cache_key] = None
        logger.debug("  No search results for: %s", name)
        return False

    llm_result = _parse_entity_with_llm(name, entity_type, search_results, api_key)
    if llm_result and llm_result.get("person_name"):
        result = {
            "person_name": llm_result["person_name"],
            "role": llm_result["role"],
            "confidence": llm_result["confidence"],
            "source": "web_search",
        }
        notice.entity_person_name = result["person_name"]
        notice.entity_person_role = result["role"]
        notice.entity_research_confidence = result["confidence"]
        notice.entity_research_source = result["source"]

        with cache_lock:
            search_cache[cache_key] = result

        logger.debug("  Web search: %s → %s (%s)", name, result["person_name"], result["confidence"])
        return True

    with cache_lock:
        search_cache[cache_key] = None
    logger.debug("  No person found for: %s", name)
    return False


# ── Entry Point ─────────────────────────────────────────────────────────


def enrich_entity_data(
    notices: list[NoticeData],
    api_key: str,
) -> None:
    """Research entity-owned properties to find the person behind each entity.

    Updates notices in-place with entity_type, entity_person_name,
    entity_person_role, entity_research_source, and entity_research_confidence.

    Args:
        notices: List of NoticeData objects (modified in-place).
        api_key: Anthropic API key for Claude Haiku calls.
    """
    # Build candidate list: records with entity owner names
    candidates = []
    for n in notices:
        name = (n.tax_owner_name or n.owner_name or "").strip()
        if not name:
            continue
        # Already researched
        if n.entity_person_name:
            continue
        # Check if it's an entity (use _classify_entity which covers all types
        # including trusts and estates that BUSINESS_RE misses)
        if not _classify_entity(name):
            continue
        # Personal trusts/estates handled by name parsing only (no web search needed)
        # but still include them as candidates for the fast path
        candidates.append(n)

    if not candidates:
        logger.info("── Step 3a: Entity Research (no entity candidates) ──")
        return

    logger.info("── Step 3a: Entity Research (%d candidates) ──", len(candidates))

    search_cache: dict = {}
    cache_lock = threading.Lock()

    # Separate fast-path (name-parseable) from web-search candidates
    name_parse_count = 0
    web_search_candidates = []

    for n in candidates:
        name = (n.tax_owner_name or n.owner_name or "").strip()
        entity_type = _classify_entity(name)
        parsed = _try_parse_entity_name(name, entity_type)
        if parsed and parsed.get("person_name"):
            n.entity_type = entity_type
            n.entity_person_name = parsed["person_name"]
            n.entity_person_role = parsed["role"]
            n.entity_research_confidence = parsed["confidence"]
            n.entity_research_source = "name_parse"
            cache_key = name.upper().strip()
            search_cache[cache_key] = {
                "person_name": parsed["person_name"],
                "role": parsed["role"],
                "confidence": parsed["confidence"],
                "source": "name_parse",
            }
            name_parse_count += 1
        else:
            web_search_candidates.append(n)

    if name_parse_count:
        logger.info("  Name-parsed: %d entities (free, no API calls)", name_parse_count)

    # Web search + LLM for remaining candidates
    web_found = 0
    if web_search_candidates and api_key:
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            futures = {
                executor.submit(
                    _research_single_entity, n, api_key, search_cache, cache_lock
                ): n
                for n in web_search_candidates
            }
            for future in as_completed(futures):
                try:
                    if future.result():
                        web_found += 1
                except Exception as e:
                    notice = futures[future]
                    logger.debug("  Research failed for %s: %s", notice.owner_name, e)

        logger.info("  Web search: %d/%d entities found", web_found, len(web_search_candidates))
    elif web_search_candidates:
        logger.info("  Web search: skipped (no API key) — %d entities unresearched",
                     len(web_search_candidates))

    total_found = name_parse_count + web_found
    total = len(candidates)
    logger.info("  Entity research: %d/%d persons identified (%.0f%%)",
                total_found, total, 100 * total_found / total if total else 0)
